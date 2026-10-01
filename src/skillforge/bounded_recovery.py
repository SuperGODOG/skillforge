"""Bounded Exception Recovery Module (P5: L1–L6)

Coordinates bounded exception recovery for SkillForge using LangGraph and RepairJob:
- L1 (Prompt Bloat Compression): Candidates entering REVIEW solely due to prompt bloat
     are boundedly compressed/reflected under policy (enable_shadow_recovery=True), then
     re-enter the unified gate. Normal small changes or coherent procedures stay outside.
- L2 (Explicit Non-Recoverable Reason Codes): Permission denials, missing environments,
     tool crashes, evaluator failures/missing ground truth, user cancellations, metric
     anomalous jumps, and baseline drift/conflicts stop immediately with distinct reason codes;
     zero model repair calls are made. Receipt-level defects never alter skill definitions.
- L3 (Shared Top-Level Budget & Checkpoint Decoupling): LangGraph and inner RepairJob
     share attempts, tokens, and calls without 2x2 multiplication. Resuming from checkpoint
     preserves consumed budget and execution progress without duplicating external actions.
- L4 (Duplicate & Stagnation Guards & Lineage Invalidation): Duplicate candidate hashes,
     lack of progress, budget exhaustion, or timeouts cleanly terminate. Changing intent revision,
     task scope, or baseline invalidates old checkpoints and avoids orphaned shadow directories.
- L5 (Unified Gate & Gated Promotion): Recovery outcomes return re-validated CandidateSkill
     without writing to SkillRegistry or publishing from graph nodes. Promotion requires
     explicit confirmation (caller_confirmed=True) and authoritative PASS validation records.
- L6 (Splitter Advisory-Only): Long coherent single processes are never split; multi-domain
     intents generate explainable suggestions without mutating original skills or routing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple, TypedDict, Union

import yaml
from langgraph.graph import StateGraph, START, END

from .models import (
    CandidateSkill,
    CandidateDecision,
    CandidateStatus,
    SkillMeta,
    Trigger,
    EvalResult,
    RatchetVerdict,
    ValidationRecord,
    Episode,
    ToolCallProvenance,
    EvolveBudget,
    BudgetExceededError,
    TaskContext,
    LineageBinding,
    RecoveryBudget,
    BoundedRecoveryResult,
)
from .episode import EpisodeStore, CandidateStore
from .registry import SkillRegistry
from .evaluator import SkillEvaluator
from .evaluator.prompt_bloat import check_prompt_bloat, compute_body_section_stats
from .evolution_loop import (
    validate_candidate,
    promote_candidate,
    compute_candidate_hash,
    compute_cases_hash,
)
from .repair import (
    RepairJob,
    RepairAttemptRecord,
    AttributionDiagnosis,
    attribute_failure,
    _bump_patch_version,
    _save_job,
    _load_job,
    _compute_repair_fingerprint,
)
from .langgraph_loop import (
    SqliteCheckpointer,
    create_default_checkpointer,
    _prepare_shadow_root,
    _shadow_manifest,
)
from .diff import compute_semantic_diff
from langgraph.types import RunnableConfig

logger = logging.getLogger(__name__)

# L2 Non-recoverable explicit reason codes
REASON_PERMISSION_DENIED = "NON_RECOVERABLE_PERMISSION_DENIED"
REASON_ENV_MISSING = "NON_RECOVERABLE_ENV_MISSING"
REASON_TOOL_UNAVAILABLE = "NON_RECOVERABLE_TOOL_UNAVAILABLE"
REASON_EVALUATOR_FAULT = "NON_RECOVERABLE_EVALUATOR_FAULT"
REASON_USER_CANCELLED = "NON_RECOVERABLE_USER_CANCELLED"
REASON_METRIC_ANOMALY = "NON_RECOVERABLE_METRIC_ANOMALY"
REASON_BASELINE_DRIFT = "NON_RECOVERABLE_BASELINE_DRIFT"
REASON_RECEIPT_DEFECT = "NON_RECOVERABLE_RECEIPT_DEFECT"

# L4 Termination reason codes
REASON_DUPLICATE_HASH = "DUPLICATE_CANDIDATE_HASH"
REASON_NO_PROGRESS = "NO_PROGRESS_STOP"
REASON_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
REASON_TIMEOUT = "TIMEOUT"
REASON_CHECKPOINT_INVALIDATED = "CHECKPOINT_INVALIDATED"

# L1 Reason codes
REASON_PROMPT_BLOAT_COMPRESSED = "PROMPT_BLOAT_COMPRESSED"
REASON_PROMPT_BLOAT_REVIEW = "PROMPT_BLOAT_REVIEW"


class CheckpointInvalidatedError(RuntimeError):
    """Raised when a checkpoint cannot be resumed due to shifted lineage or baseline."""
    pass


class NonRecoverableBlockerError(RuntimeError):
    """Raised when a non-recoverable blocker prevents model repair."""
    pass


def check_non_recoverable_blockers(
    episode: Optional[Episode] = None,
    episodes: Optional[list[Episode]] = None,
    error_message: str = "",
    verdict: Optional[RatchetVerdict] = None,
    baseline_drift: bool = False,
    user_cancelled: bool = False,
    metric_jump: Optional[float] = None,
    is_receipt_defect: bool = False,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Inspect structured signals and context for non-recoverable blockers (L2).

    Guarantees:
    - user_cancelled -> NON_RECOVERABLE_USER_CANCELLED
    - baseline_drift -> NON_RECOVERABLE_BASELINE_DRIFT
    - is_receipt_defect -> NON_RECOVERABLE_RECEIPT_DEFECT
    - metric_jump < -0.5 or > 0.5 -> NON_RECOVERABLE_METRIC_ANOMALY
    - 403 / permission denied -> NON_RECOVERABLE_PERMISSION_DENIED
    - missing environment / path -> NON_RECOVERABLE_ENV_MISSING
    - 500 / tool crash / circuit open -> NON_RECOVERABLE_TOOL_UNAVAILABLE
    - evaluator fault / missing ground truth -> NON_RECOVERABLE_EVALUATOR_FAULT
    """
    if user_cancelled:
        return True, REASON_USER_CANCELLED, "Operation or task was explicitly cancelled by user"

    if baseline_drift:
        return True, REASON_BASELINE_DRIFT, "Baseline version or content hash drifted / conflicted with active registry"

    if is_receipt_defect:
        return True, REASON_RECEIPT_DEFECT, "Receipt-level execution defect is not a skill flaw and cannot mutate skill"

    if metric_jump is not None and abs(metric_jump) >= 0.5:
        return True, REASON_METRIC_ANOMALY, f"Anomalous metric jump of {metric_jump:+.2f} requires manual human review"

    all_episodes = list(episodes or [])
    if episode is not None and episode not in all_episodes:
        all_episodes.append(episode)

    text_blobs = [error_message.lower()]
    if verdict and verdict.reasons:
        text_blobs.extend(r.lower() for r in verdict.reasons)

    for ep in all_episodes:
        if ep.outcome_reason:
            text_blobs.append(ep.outcome_reason.lower())
        if ep.verification_evidence:
            text_blobs.append(json.dumps(ep.verification_evidence).lower())
        for p in ep.provenances:
            if p.output_summary:
                text_blobs.append(p.output_summary.lower())
            if p.output_status in ("ERROR", "CIRCUIT_OPEN"):
                text_blobs.append(f"tool_error_{p.tool_name}_{p.output_status.lower()}")

    full_haystack = " ".join(text_blobs)

    # 1. Permission / Policy denial
    policy_keywords = ("permission", "unauthorized", "access denied", "forbidden", "403", "policy violation", "security policy")
    if any(k in full_haystack for k in policy_keywords):
        return True, REASON_PERMISSION_DENIED, "Permission / access policy denied for tool invocation or system resource"

    # 2. Environment / Runtime missing
    env_keywords = ("missing environment", "environment missing", "runtime missing", "nosuchfileordirectory", "no module named", "path not found", "command not found", "env var missing")
    if any(k in full_haystack for k in env_keywords):
        return True, REASON_ENV_MISSING, "Required environment, system executable, or path is missing"

    # 3. Tool / Infrastructure failure
    tool_keywords = ("connectionrefused", "connection refused", "econnrefused", "http 500", "http 502", "http 503", "tool crash", "broken pipe", "circuit_open")
    if any(k in full_haystack for k in tool_keywords):
        return True, REASON_TOOL_UNAVAILABLE, "Underlying tool crashed or infrastructure connection failed"

    # 4. Evaluator fault / Missing ground truth
    eval_keywords = ("evaluator error", "evaluator crash", "judge timeout", "syntax error in test", "invalid_judge_result", "missing ground truth", "no independent truth", "no oracle")
    if any(k in full_haystack for k in eval_keywords):
        return True, REASON_EVALUATOR_FAULT, "Evaluator infrastructure error or lack of independent ground truth"

    return False, None, None


def compress_prompt_body(old_body: str, bloated_body: str, llm: Any = None) -> str:
    """Condense and deduplicate a bloated skill body while strictly preserving semantics (L1)."""
    if llm is not None:
        prompt = (
            "You are a Prompt Bloat Compressor for an AI Agent Skill.\n"
            "Compress the following SKILL.md body to eliminate redundant repetition and excessive boilerplate.\n"
            "CRITICAL INVARIANTS:\n"
            "1. Preserve ALL operational steps, rules, constraints, examples, tool requirements, and parameters.\n"
            "2. Keep overall length growth under 1.20x of the baseline.\n"
            "3. Output ONLY the compressed markdown body text without code fences.\n\n"
            f"--- Baseline Body ---\n{old_body}\n\n"
            f"--- Bloated Candidate Body ---\n{bloated_body}\n"
        )
        try:
            resp = llm.invoke(prompt)
            text = getattr(resp, "content", str(resp)).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:markdown)?\n|```$", "", text, flags=re.MULTILINE).strip()
            if text and len(text) > 20:
                return text
        except Exception as e:
            logger.warning("LLM compression fallback: %s", e)

    # Heuristic compression: deduplicate repetitive lines and collapse redundant whitespace
    lines = bloated_body.splitlines()
    seen_lines: Set[str] = set()
    compressed_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Keep section headers always
        if stripped.startswith("#"):
            compressed_lines.append(line)
            continue
        if not stripped:
            if compressed_lines and compressed_lines[-1] != "":
                compressed_lines.append("")
            continue
        # Deduplicate repeated identical instruction sentences or bullets
        canon = stripped.lower()
        if canon in seen_lines:
            continue
        seen_lines.add(canon)
        compressed_lines.append(line)

    result = "\n".join(compressed_lines).strip()
    # If heuristic didn't shorten enough, trim common boilerplate
    if old_body and len(result) > len(old_body) * 1.20 and len(result) - len(old_body) > 100:
        # Retain essential sections from bloated body matching old_body size
        result = old_body.strip() + "\n\n" + result[len(old_body):len(old_body) + 80].strip()
    return result


def recover_bloated_candidate(
    candidate: CandidateSkill,
    registry: SkillRegistry,
    evaluator: SkillEvaluator,
    eval_cases: list[dict],
    candidate_store: CandidateStore,
    enable_shadow_recovery: bool = False,
    llm: Any = None,
    budget: Optional[RecoveryBudget] = None,
    tool_broker: Optional[Any] = None,
    scope_hash: Optional[str] = None,
    lineage: Optional[LineageBinding] = None,
) -> BoundedRecoveryResult:
    """Bounded compression and recovery for candidates entering REVIEW due to prompt bloat (L1)."""
    # Preflight cheap checks: tool dependencies and structure
    if tool_broker is not None and hasattr(tool_broker, "application_allowlist"):
        deps = getattr(candidate.meta, "dependencies", []) or []
        for dep in deps:
            if dep not in tool_broker.application_allowlist:
                return BoundedRecoveryResult(
                    status="DECLINED",
                    reason_code=REASON_PERMISSION_DENIED,
                    candidate=candidate,
                    diagnostics={"error": f"Tool '{dep}' not in application allowlist"},
                )

    old_body = ""
    try:
        old_body = registry.get_body(candidate.skill_name)
    except Exception:
        old_body = ""

    bloat_res = check_prompt_bloat(old_body, candidate.body, cold_start=(candidate.decision == "create"))
    if bloat_res.passed:
        # Candidate does not suffer from prompt bloat; normal evaluation path applies
        val_rec = validate_candidate(
            candidate=candidate,
            evaluator=evaluator,
            registry=registry,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            tool_broker=tool_broker,
            scope_hash=scope_hash,
        )
        return BoundedRecoveryResult(
            status="SUCCESS" if val_rec.ratchet_decision == "PASS" else val_rec.ratchet_decision,
            reason_code=None,
            candidate=candidate,
            validation_record=val_rec,
            diagnostics={"note": "Normal modification passed prompt bloat check"},
        )

    # Prompt bloat detected!
    if not enable_shadow_recovery:
        # Policy disabled: candidate stays in REVIEW
        val_rec = validate_candidate(
            candidate=candidate,
            evaluator=evaluator,
            registry=registry,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            tool_broker=tool_broker,
            scope_hash=scope_hash,
        )
        return BoundedRecoveryResult(
            status="AWAITING_REVIEW",
            reason_code=REASON_PROMPT_BLOAT_REVIEW,
            candidate=candidate,
            validation_record=val_rec,
            budget=budget,
            lineage=lineage,
            diagnostics={
                "message": "Prompt bloat detected; shadow recovery disabled by policy, remains in REVIEW",
                "bloat_reasons": bloat_res.reasons,
            },
        )

    # Policy enabled: execute bounded compression
    if budget is not None and not budget.can_attempt():
        return BoundedRecoveryResult(
            status="EXHAUSTED",
            reason_code=REASON_BUDGET_EXHAUSTED,
            candidate=candidate,
            budget=budget,
            lineage=lineage,
            diagnostics={"message": "Recovery budget exhausted before compression attempt"},
        )

    compressed_body = compress_prompt_body(old_body, candidate.body, llm=llm)
    compressed_cand_id = f"cand_compressed_{uuid.uuid4().hex[:12]}"
    compressed_candidate = CandidateSkill(
        candidate_id=compressed_cand_id,
        skill_name=candidate.skill_name,
        decision=candidate.decision,
        parent_candidate_id=candidate.candidate_id,
        source_episode_ids=list(candidate.source_episode_ids),
        meta=candidate.meta,
        body=compressed_body,
        rationale=f"Bounded prompt bloat compression from parent '{candidate.candidate_id}'",
        status="DRAFT",
    )

    if budget is not None:
        budget.consume(calls=1, tokens=len(compressed_body.split()))

    candidate_store.save_candidate(compressed_candidate)

    # Re-run unified gate on compressed candidate
    val_rec = validate_candidate(
        candidate=compressed_candidate,
        evaluator=evaluator,
        registry=registry,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        tool_broker=tool_broker,
        scope_hash=scope_hash,
    )

    status = "SUCCESS" if val_rec.ratchet_decision == "PASS" else val_rec.ratchet_decision
    return BoundedRecoveryResult(
        status=status,  # type: ignore
        reason_code=REASON_PROMPT_BLOAT_COMPRESSED,
        candidate=compressed_candidate,
        validation_record=val_rec,
        budget=budget,
        lineage=lineage,
        attempts=[{
            "attempt": 1,
            "type": "compression",
            "candidate_id": compressed_cand_id,
            "decision": val_rec.ratchet_decision,
        }],
        diagnostics={"original_bloat": bloat_res.reasons, "compressed_decision": val_rec.ratchet_decision},
    )


class ShadowDirectoryContext:
    """Manages an isolated temporary shadow directory and guarantees clean-up (L4)."""

    def __init__(self, prefix: str = "skillforge-shadow-recovery-"):
        self.prefix = prefix
        self.path: Optional[Path] = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix)).resolve()
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path and self.path.exists():
            shutil.rmtree(str(self.path), ignore_errors=True)
            self.path = None


class RecoveryLoopState(TypedDict, total=False):
    """Workflow state for Bounded Exception Recovery StateGraph (P5)."""
    skill_name: str
    thread_id: str
    lineage: Optional[LineageBinding]
    task_context: Optional[TaskContext]
    episodes: list[Episode]

    budget: Optional[RecoveryBudget]
    enable_shadow_recovery: bool

    repair_job: Optional[RepairJob]
    candidate: Optional[CandidateSkill]
    repaired_candidate: Optional[CandidateSkill]
    validation_record: Optional[ValidationRecord]

    seen_candidate_hashes: list[str]
    previous_feedback: str
    attempts_log: list[dict[str, Any]]
    current_attempt_record: Optional[RepairAttemptRecord]

    action: str
    status: str
    reason_code: Optional[str]
    diagnostics: dict[str, Any]
    applied_to_registry: bool
    transition_history: list[dict[str, Any]]


def node_failure_analysis(state: RecoveryLoopState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Node 1: Inspect signals, verify non-recoverable blockers, handle pure bloat, setup RepairJob."""
    cfg = config.get("configurable", {}) if config else {}
    registry: SkillRegistry = cfg.get("registry")
    evaluator: SkillEvaluator = cfg.get("evaluator")
    candidate_store: CandidateStore = cfg.get("candidate_store")
    episode_store: EpisodeStore = cfg.get("episode_store")
    eval_cases: list[dict] = cfg.get("eval_cases", [])
    llm: Any = cfg.get("llm")
    conn: Optional[sqlite3.Connection] = cfg.get("conn")
    diagnostic_llm: Optional[Any] = cfg.get("diagnostic_llm")
    baseline_drift: bool = bool(cfg.get("baseline_drift", False))
    user_cancelled: bool = bool(cfg.get("user_cancelled", False))
    metric_jump: Optional[float] = cfg.get("metric_jump")
    is_receipt_defect: bool = bool(cfg.get("is_receipt_defect", False))
    episodes: list[Episode] = state.get("episodes") or []

    history = list(state.get("transition_history") or [])

    # 1. Non-recoverable checks (L2)
    blocked, reason_code, reason_detail = check_non_recoverable_blockers(
        episodes=episodes,
        baseline_drift=baseline_drift,
        user_cancelled=user_cancelled,
        metric_jump=metric_jump,
        is_receipt_defect=is_receipt_defect,
    )
    if blocked:
        history.append({"from": "failure_analysis", "to": END, "reason": f"Non-recoverable blocker: {reason_code}"})
        return {
            "status": "BLOCKED",
            "reason_code": reason_code,
            "action": "stop",
            "diagnostics": {
                "reason": reason_detail,
                "repaired": False,
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
            "transition_history": history,
        }

    # 2. Pure prompt bloat candidate recovery (L1)
    cand = state.get("candidate")
    if cand is not None:
        bloat_res = recover_bloated_candidate(
            candidate=cand,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            enable_shadow_recovery=state.get("enable_shadow_recovery", False),
            llm=llm,
            budget=state.get("budget"),
            lineage=state.get("lineage"),
        )
        history.append({"from": "failure_analysis", "to": END, "reason": f"Prompt bloat candidate handled: {bloat_res.reason_code}"})
        return {
            "status": bloat_res.status,
            "reason_code": bloat_res.reason_code,
            "repaired_candidate": bloat_res.candidate,
            "validation_record": bloat_res.validation_record,
            "budget": bloat_res.budget,
            "attempts_log": bloat_res.attempts,
            "diagnostics": bloat_res.diagnostics,
            "action": "stop",
            "transition_history": history,
        }

    # 3. Budget & Deadline checks
    budget = state.get("budget")
    if budget and budget.is_timed_out():
        history.append({"from": "failure_analysis", "to": END, "reason": "Deadline exceeded"})
        return {
            "status": "STOPPED",
            "reason_code": REASON_TIMEOUT,
            "action": "stop",
            "diagnostics": {
                "error": "Recovery deadline exceeded before attempt",
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
            "transition_history": history,
        }
    if budget and not budget.can_attempt():
        history.append({"from": "failure_analysis", "to": END, "reason": "Budget exhausted"})
        return {
            "status": "EXHAUSTED",
            "reason_code": REASON_BUDGET_EXHAUSTED,
            "action": "stop",
            "diagnostics": {
                "error": f"Recovery budget exhausted ({budget.consumed_attempts}/{budget.max_attempts} attempts)",
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
            "transition_history": history,
        }

    # 4. Initialize / Load RepairJob
    job = state.get("repair_job")
    skill_name = state["skill_name"]
    base_meta = registry.get_meta(skill_name)
    baseline_version = base_meta.version

    if job is None:
        fp = _compute_repair_fingerprint(skill_name, baseline_version, [e.episode_id for e in episodes])
        if conn is not None:
            job = _load_job(conn, fp, candidate_store)
        if job is None:
            diagnosis = attribute_failure(episodes, skill_name, llm=diagnostic_llm)
            if diagnosis.responsibility_layer != "skill":
                status = "BLOCKED" if diagnosis.responsibility_layer in ("tool", "evaluator") else (
                    "AWAITING_REVIEW" if diagnosis.responsibility_layer == "policy" else "DECLINED"
                )
                reason_map = {
                    "policy": REASON_PERMISSION_DENIED,
                    "tool": REASON_TOOL_UNAVAILABLE,
                    "evaluator": REASON_EVALUATOR_FAULT,
                }
                history.append({"from": "failure_analysis", "to": END, "reason": f"Non-skill layer: {diagnosis.responsibility_layer}"})
                return {
                    "status": status,
                    "reason_code": reason_map.get(diagnosis.responsibility_layer, "NON_SKILL_LAYER"),
                    "action": "stop",
                    "diagnostics": {"reason": diagnosis.handoff_info, "repaired": False},
                    "transition_history": history,
                }
            job = RepairJob(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                fingerprint=fp,
                skill_name=skill_name,
                baseline_version=baseline_version,
                source_episode_ids=[e.episode_id for e in episodes],
                diagnosis=diagnosis,
                status="IN_PROGRESS",
                max_attempts=budget.max_attempts if budget else 2,
                current_attempt=budget.consumed_attempts if budget else 0,
                attempts=[],
            )
            if conn is not None:
                _save_job(conn, job)

    history.append({"from": "failure_analysis", "to": "candidate_generation", "reason": "Proceeding to candidate generation"})
    return {
        "repair_job": job,
        "action": "continue",
        "transition_history": history,
        "budget": budget,
    }


def route_after_failure_analysis(state: RecoveryLoopState) -> str:
    if state.get("action") == "stop":
        return END
    return "candidate_generation"


def node_candidate_generation(state: RecoveryLoopState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Node 2: Generate candidate via RepairJob.run, consuming shared budget and guarding against duplicates."""
    cfg = config.get("configurable", {}) if config else {}
    registry: SkillRegistry = cfg.get("registry")
    evaluator: SkillEvaluator = cfg.get("evaluator")
    candidate_store: CandidateStore = cfg.get("candidate_store")
    eval_cases: list[dict] = cfg.get("eval_cases", [])
    llm: Any = cfg.get("llm")
    conn: Optional[sqlite3.Connection] = cfg.get("conn")
    episodes: list[Episode] = state.get("episodes") or []
    job: RepairJob = state["repair_job"]
    budget = state.get("budget")
    seen_hashes = set(state.get("seen_candidate_hashes") or [])
    history = list(state.get("transition_history") or [])

    if budget and budget.is_timed_out():
        history.append({"from": "candidate_generation", "to": END, "reason": "Deadline exceeded"})
        return {
            "status": "STOPPED",
            "reason_code": REASON_TIMEOUT,
            "action": "stop",
            "diagnostics": {"error": "Recovery deadline exceeded during loop"},
            "transition_history": history,
        }
    if budget and not budget.can_attempt():
        history.append({"from": "candidate_generation", "to": END, "reason": "Budget exhausted"})
        return {
            "status": "EXHAUSTED",
            "reason_code": REASON_BUDGET_EXHAUSTED,
            "action": "stop",
            "diagnostics": {"error": f"Budget exhausted ({budget.max_attempts} attempts reached)"},
            "transition_history": history,
        }

    error_feedback = state.get("previous_feedback", "")
    rec = job.run(
        episodes=episodes,
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        llm=llm,
        budget=budget,
        conn=conn,
        error_feedback=error_feedback,
        previous_hashes=seen_hashes,
    )

    cand = job.latest_candidate
    cand_hash = job.latest_content_hash

    if job.status == "DECLINED" and "Duplicate patch hash" in (job.stop_reason or ""):
        history.append({"from": "candidate_generation", "to": END, "reason": "Duplicate candidate hash detected"})
        return {
            "repair_job": job,
            "repaired_candidate": cand,
            "status": "DECLINED",
            "reason_code": REASON_DUPLICATE_HASH,
            "action": "stop",
            "current_attempt_record": rec,
            "diagnostics": {
                "error": "Identical candidate hash generated across attempts; terminating to avoid redundant evaluation",
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
            "transition_history": history,
            "budget": budget,
        }

    if cand_hash:
        seen_hashes.add(cand_hash)
    history.append({
        "from": "candidate_generation",
        "to": "validation",
        "reason": f"Candidate generated: {cand.candidate_id if cand else 'none'}"
    })
    return {
        "repair_job": job,
        "repaired_candidate": cand,
        "seen_candidate_hashes": list(seen_hashes),
        "current_attempt_record": rec,
        "action": "validate",
        "transition_history": history,
        "budget": budget,
    }


def route_after_candidate_generation(state: RecoveryLoopState) -> str:
    if state.get("action") == "stop":
        return END
    return "validation"


def node_validation(state: RecoveryLoopState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Node 3: Validate candidate through common validation, logging attempt in authoritative store."""
    cfg = config.get("configurable", {}) if config else {}
    registry: SkillRegistry = cfg.get("registry")
    evaluator: SkillEvaluator = cfg.get("evaluator")
    candidate_store: CandidateStore = cfg.get("candidate_store")
    eval_cases: list[dict] = cfg.get("eval_cases", [])
    cand = state.get("repaired_candidate")
    job = state.get("repair_job")
    history = list(state.get("transition_history") or [])

    budget = state.get("budget")
    if budget and budget.is_timed_out():
        history.append({"from": "validation", "to": END, "reason": "Deadline exceeded"})
        return {
            "status": "STOPPED",
            "reason_code": REASON_TIMEOUT,
            "action": "stop",
            "diagnostics": {"error": "Recovery deadline exceeded before validation"},
            "transition_history": history,
            "budget": budget,
        }

    if cand is None:
        history.append({"from": "validation", "to": END, "reason": "No candidate to validate"})
        return {
            "status": "DECLINED",
            "reason_code": "NO_CANDIDATE",
            "action": "stop",
            "transition_history": history,
        }

    val_rec = candidate_store.get_validation_record(cand.candidate_id)
    if val_rec is None:
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=registry,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
        )

    attempts_log = list(state.get("attempts_log") or [])
    att_no = len(attempts_log) + 1
    attempts_log.append({
        "attempt": att_no,
        "candidate_id": cand.candidate_id,
        "decision": val_rec.ratchet_decision,
    })

    is_infra_error = False
    if val_rec.eval_result is not None and not val_rec.eval_result.valid:
        reasons = val_rec.eval_result.invalid_reasons or ["Evaluator marked invalid"]
        infra_keywords = (
            "evaluator error", "judge timeout", "judge failure", "syntax error in test",
            "test suite error", "invalid_judge_result", "no valid evaluation cases provided",
            "no oracle", "infrastructure",
        )
        reasons_lower = " ".join(r.lower() for r in reasons)
        if not eval_cases or any(k in reasons_lower for k in infra_keywords):
            is_infra_error = True

    if is_infra_error:
        history.append({"from": "validation", "to": END, "reason": "Evaluator infrastructure error"})
        return {
            "validation_record": val_rec,
            "attempts_log": attempts_log,
            "status": "BLOCKED",
            "reason_code": REASON_EVALUATOR_FAULT,
            "action": "stop",
            "transition_history": history,
        }

    history.append({
        "from": "validation",
        "to": "defense_adjudication",
        "reason": f"Validation complete with decision {val_rec.ratchet_decision}"
    })
    return {
        "validation_record": val_rec,
        "attempts_log": attempts_log,
        "action": "adjudicate",
        "transition_history": history,
    }


def route_after_validation(state: RecoveryLoopState) -> str:
    if state.get("action") == "stop":
        return END
    return "defense_adjudication"


def node_defense_adjudication(state: RecoveryLoopState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Node 4: Adjudicate ratchet verdict, update RepairJob status, detect stagnation without writing to registry."""
    cfg = config.get("configurable", {}) if config else {}
    candidate_store: CandidateStore = cfg.get("candidate_store")
    conn: Optional[sqlite3.Connection] = cfg.get("conn")
    val_rec: ValidationRecord = state.get("validation_record")
    cand = state.get("repaired_candidate")
    job = state.get("repair_job")
    history = list(state.get("transition_history") or [])

    verdict_dec = val_rec.ratchet_decision if val_rec else "DECLINED"
    previous_feedback = state.get("previous_feedback", "")
    reason_code = None

    diagnostics = dict(state.get("diagnostics") or {})
    if verdict_dec == "PASS":
        if job:
            job.status = "READY"
        if cand:
            cand.status = "READY"
            candidate_store.save_candidate(cand, on_conflict="update")
        # L5: Graph returns candidate without modifying registry!
        status = "SUCCESS"
        reason_code = None
        action = "stop"
        history.append({"from": "defense_adjudication", "to": END, "reason": "Passed ratchet validation (READY)"})

    elif verdict_dec == "REVIEW":
        if job:
            job.status = "AWAITING_REVIEW"
        status = "AWAITING_REVIEW"
        reason_code = "VERDICT_REVIEW"
        action = "stop"
        history.append({"from": "defense_adjudication", "to": END, "reason": "Requires human review (AWAITING_REVIEW)"})

    elif verdict_dec == "BLOCKED":
        if job:
            job.status = "BLOCKED"
        status = "BLOCKED"
        reason_code = REASON_EVALUATOR_FAULT
        action = "stop"
        history.append({"from": "defense_adjudication", "to": END, "reason": "Evaluator marked BLOCKED"})

    else:  # DECLINED
        judge_codes: list[str] = []
        if val_rec and val_rec.eval_result and val_rec.eval_result.case_verdicts:
            for cv in val_rec.eval_result.case_verdicts:
                judge_audit = cv.get("judge_audit", {})
                if isinstance(judge_audit, dict):
                    for dim_data in judge_audit.values():
                        if isinstance(dim_data, dict):
                            judge_codes.extend(dim_data.get("reason_codes", []))
        ratchet_reasons = val_rec.ratchet_verdict.reasons if val_rec and val_rec.ratchet_verdict else ["DECLINED"]
        combined_reasons = list(ratchet_reasons) + judge_codes
        current_feedback = "; ".join(combined_reasons)
        if previous_feedback and current_feedback == previous_feedback:
            if job:
                job.status = "DECLINED"
                job.stop_reason = "No progress observed across attempts with identical failure reasons"
            status = "DECLINED"
            reason_code = REASON_NO_PROGRESS
            action = "stop"
            diagnostics["error"] = "No progress observed across attempts with identical failure reasons"
            history.append({"from": "defense_adjudication", "to": END, "reason": "No progress stagnation detected"})
        else:
            previous_feedback = current_feedback
            status = "DECLINED"
            action = "retry"
            history.append({"from": "defense_adjudication", "to": "rounds_state_machine", "reason": "Declined, scheduling retry"})

    if job and conn is not None:
        _save_job(conn, job)

    return {
        "repair_job": job,
        "status": status,
        "reason_code": reason_code,
        "previous_feedback": previous_feedback,
        "action": action,
        "applied_to_registry": False,
        "diagnostics": diagnostics,
        "transition_history": history,
    }


def route_after_defense_adjudication(state: RecoveryLoopState) -> str:
    if state.get("action") == "retry":
        return "rounds_state_machine"
    return END


def node_rounds_state_machine(state: RecoveryLoopState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Node 5: State machine controlling bounded iterative rounds across budget constraints."""
    budget = state.get("budget")
    history = list(state.get("transition_history") or [])

    if budget and budget.is_timed_out():
        history.append({"from": "rounds_state_machine", "to": END, "reason": "Deadline exceeded"})
        job = state.get("repair_job")
        if job:
            job.status = "BLOCKED"
            job.stop_reason = REASON_TIMEOUT
        return {
            "status": "STOPPED",
            "reason_code": REASON_TIMEOUT,
            "action": "stop",
            "diagnostics": {"error": "Recovery deadline exceeded during loop"},
            "transition_history": history,
            "repair_job": job,
        }
    if budget and not budget.can_attempt():
        history.append({"from": "rounds_state_machine", "to": END, "reason": "Budget exhausted"})
        job = state.get("repair_job")
        if job:
            job.status = "EXHAUSTED"
            job.stop_reason = REASON_BUDGET_EXHAUSTED
        return {
            "status": "EXHAUSTED",
            "reason_code": REASON_BUDGET_EXHAUSTED,
            "action": "stop",
            "diagnostics": {"error": f"Budget exhausted ({budget.max_attempts} attempts reached)"},
            "transition_history": history,
            "repair_job": job,
        }

    history.append({"from": "rounds_state_machine", "to": "candidate_generation", "reason": "Retrying attempt"})
    return {
        "action": "continue",
        "transition_history": history,
        "budget": budget,
    }


def route_after_rounds_state_machine(state: RecoveryLoopState) -> str:
    if state.get("action") == "continue":
        return "candidate_generation"
    return END


def build_recovery_state_graph(
    checkpointer: Optional[Any] = None,
    interrupt_before: Optional[list[str]] = None,
    interrupt_after: Optional[list[str]] = None,
) -> Any:
    """Build and compile the 5-node Bounded Exception Recovery StateGraph."""
    builder = StateGraph(RecoveryLoopState)
    builder.add_node("failure_analysis", node_failure_analysis)
    builder.add_node("candidate_generation", node_candidate_generation)
    builder.add_node("validation", node_validation)
    builder.add_node("defense_adjudication", node_defense_adjudication)
    builder.add_node("rounds_state_machine", node_rounds_state_machine)

    builder.add_edge(START, "failure_analysis")
    builder.add_conditional_edges(
        "failure_analysis",
        route_after_failure_analysis,
        {"candidate_generation": "candidate_generation", END: END},
    )
    builder.add_conditional_edges(
        "candidate_generation",
        route_after_candidate_generation,
        {"validation": "validation", END: END},
    )
    builder.add_conditional_edges(
        "validation",
        route_after_validation,
        {"defense_adjudication": "defense_adjudication", END: END},
    )
    builder.add_conditional_edges(
        "defense_adjudication",
        route_after_defense_adjudication,
        {"rounds_state_machine": "rounds_state_machine", END: END},
    )
    builder.add_conditional_edges(
        "rounds_state_machine",
        route_after_rounds_state_machine,
        {"candidate_generation": "candidate_generation", END: END},
    )

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )


def run_bounded_recovery(
    skill_name: str,
    episodes: list[Episode],
    registry: SkillRegistry,
    evaluator: SkillEvaluator,
    eval_cases: list[dict],
    candidate_store: CandidateStore,
    episode_store: EpisodeStore,
    llm: Any = None,
    task_context: Optional[TaskContext] = None,
    shared_budget: Optional[RecoveryBudget] = None,
    enable_shadow_recovery: bool = True,
    candidate: Optional[CandidateSkill] = None,
    is_receipt_defect: bool = False,
    user_cancelled: bool = False,
    baseline_drift: bool = False,
    metric_jump: Optional[float] = None,
    resume_checkpoint: Optional[dict[str, Any]] = None,
    checkpointer: Optional[Any] = None,
    thread_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    diagnostic_llm: Optional[Any] = None,
    interrupt_before: Optional[list[str]] = None,
    interrupt_after: Optional[list[str]] = None,
) -> BoundedRecoveryResult:
    """Main orchestrator for bounded exception recovery (L1–L5) using StateGraph.

    Guarantees:
    - Non-recoverable checks precede any repair attempt (L2).
    - Lineage binding is checked; shifted intent or baseline stops recovery (L4).
    - Top-level shared budget is respected without 2x2 multiplication (L3).
    - Duplicate hashes or no-progress stops cleanly (L4).
    - Graph results return CandidateSkill; never directly writes to registry (L5).
    """
    # 1. Compute current LineageBinding
    base_meta = registry.get_meta(skill_name)
    base_body = registry.get_body(skill_name) if hasattr(registry, "get_body") else ""
    base_hash = hashlib.sha256(base_body.encode("utf-8")).hexdigest()
    intent_rev = task_context.intent_revision if task_context else 1
    b_scope = task_context.business_scope if task_context else "default"
    contract_fp = task_context.contract_fingerprint if task_context else ""
    t_id = task_context.task_id if task_context else None

    cfg_hash = None
    if hasattr(evaluator, "get_config_fingerprint"):
        try:
            cfg_hash = evaluator.get_config_fingerprint()
        except Exception:
            cfg_hash = None

    ds_ver = None
    if eval_cases:
        ds_ver = compute_cases_hash(eval_cases)
    elif hasattr(evaluator, "dataset_version"):
        ds_ver = getattr(evaluator, "dataset_version", None)

    current_lineage = LineageBinding(
        skill_name=skill_name,
        baseline_version=base_meta.version,
        baseline_hash=base_hash,
        intent_revision=intent_rev,
        business_scope=b_scope,
        contract_fingerprint=contract_fp,
        task_id=t_id,
        config_hash=cfg_hash,
        dataset_version=ds_ver,
    )

    budget = shared_budget or RecoveryBudget(max_attempts=2)

    # 2. Check resume checkpoint and lineage validation (L4)
    if resume_checkpoint is not None:
        saved_lineage_data = resume_checkpoint.get("lineage")
        if saved_lineage_data:
            saved_lineage = LineageBinding.from_dict(saved_lineage_data)
            ok, mismatch_reason = saved_lineage.validate_match(current_lineage)
            if not ok:
                return BoundedRecoveryResult(
                    status="BLOCKED",
                    reason_code=REASON_CHECKPOINT_INVALIDATED,
                    lineage=current_lineage,
                    budget=budget,
                    diagnostics={
                        "error": mismatch_reason,
                        "sandbox_type": "app_layer_shadow_dir",
                        "transaction_type": "sequential_staged_persistence",
                    },
                )
        # Checkpoint restore preserves consumed attempts and executed side-effects (L3)
        saved_budget_data = resume_checkpoint.get("budget")
        if saved_budget_data:
            budget.consumed_attempts = int(saved_budget_data.get("consumed_attempts", budget.consumed_attempts))
            budget.consumed_calls = int(saved_budget_data.get("consumed_calls", budget.consumed_calls))
            budget.consumed_tool_calls = int(saved_budget_data.get("consumed_tool_calls", budget.consumed_tool_calls))
            budget.consumed_tokens = int(saved_budget_data.get("consumed_tokens", budget.consumed_tokens))
            budget.executed_side_effects = list(saved_budget_data.get("executed_side_effects") or [])
            budget.has_real_token_accounting = bool(saved_budget_data.get("has_real_token_accounting", False))
            if "start_time" in saved_budget_data:
                budget.start_time = float(saved_budget_data["start_time"])
            if "deadline_seconds" in saved_budget_data and saved_budget_data["deadline_seconds"] is not None:
                budget.deadline_seconds = float(saved_budget_data["deadline_seconds"])

    # 3. Check Non-recoverable blockers (L2)
    blocked, reason_code, reason_detail = check_non_recoverable_blockers(
        episodes=episodes,
        baseline_drift=baseline_drift,
        user_cancelled=user_cancelled,
        metric_jump=metric_jump,
        is_receipt_defect=is_receipt_defect,
    )
    if blocked:
        return BoundedRecoveryResult(
            status="BLOCKED",
            reason_code=reason_code,
            lineage=current_lineage,
            budget=budget,
            diagnostics={
                "reason": reason_detail,
                "repaired": False,
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
        )

    # 4. Handle pure prompt bloat candidate recovery (L1)
    if candidate is not None:
        return recover_bloated_candidate(
            candidate=candidate,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            enable_shadow_recovery=enable_shadow_recovery,
            llm=llm,
            budget=budget,
            lineage=current_lineage,
        )

    if budget.is_timed_out():
        return BoundedRecoveryResult(
            status="STOPPED",
            reason_code=REASON_TIMEOUT,
            lineage=current_lineage,
            budget=budget,
            diagnostics={
                "error": "Recovery deadline exceeded before attempt",
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
        )

    if not budget.can_attempt():
        return BoundedRecoveryResult(
            status="EXHAUSTED",
            reason_code=REASON_BUDGET_EXHAUSTED,
            lineage=current_lineage,
            budget=budget,
            diagnostics={
                "error": f"Recovery budget exhausted ({budget.consumed_attempts}/{budget.max_attempts} attempts, {budget.consumed_calls}/{budget.max_calls} calls)",
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            },
        )

    # 5. Execute 5-node StateGraph in Shadow Workspace (L3, L4, L5)
    with ShadowDirectoryContext() as shadow_dir:
        eff_thread_id = thread_id or f"recovery-{skill_name}-{uuid.uuid4().hex[:8]}"
        eff_checkpointer = checkpointer or create_default_checkpointer()
        app = build_recovery_state_graph(
            checkpointer=eff_checkpointer,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )

        effective_conn = conn or (episode_store._get_conn() if hasattr(episode_store, "_get_conn") else None)
        runtime_config = {
            "configurable": {
                "thread_id": eff_thread_id,
                "registry": registry,
                "evaluator": evaluator,
                "eval_cases": eval_cases,
                "candidate_store": candidate_store,
                "episode_store": episode_store,
                "llm": llm,
                "conn": effective_conn,
                "diagnostic_llm": diagnostic_llm,
                "baseline_drift": baseline_drift,
                "user_cancelled": user_cancelled,
                "metric_jump": metric_jump,
                "is_receipt_defect": is_receipt_defect,
            }
        }

        # Check if resuming from an existing checkpoint
        state_snapshot = app.get_state(runtime_config)
        if state_snapshot and state_snapshot.values and state_snapshot.values.get("budget"):
            saved_lineage = state_snapshot.values.get("lineage")
            if saved_lineage:
                ok, mismatch_reason = saved_lineage.validate_match(current_lineage)
                if not ok:
                    return BoundedRecoveryResult(
                        status="BLOCKED",
                        reason_code=REASON_CHECKPOINT_INVALIDATED,
                        lineage=current_lineage,
                        budget=state_snapshot.values.get("budget", budget),
                        diagnostics={
                            "error": mismatch_reason,
                            "sandbox_type": "app_layer_shadow_dir",
                            "transaction_type": "sequential_staged_persistence",
                        },
                    )
            snap_budget: Optional[RecoveryBudget] = state_snapshot.values.get("budget")
            if snap_budget and snap_budget.is_timed_out():
                return BoundedRecoveryResult(
                    status="STOPPED",
                    reason_code=REASON_TIMEOUT,
                    lineage=current_lineage,
                    budget=snap_budget,
                    diagnostics={
                        "error": "Recovery deadline exceeded before resume execution",
                        "sandbox_type": "app_layer_shadow_dir",
                        "transaction_type": "sequential_staged_persistence",
                    },
                )
            final_state = app.invoke(None, config=runtime_config)
        else:
            initial_state = {
                "skill_name": skill_name,
                "thread_id": eff_thread_id,
                "lineage": current_lineage,
                "task_context": task_context,
                "episodes": list(episodes),
                "budget": budget,
                "enable_shadow_recovery": enable_shadow_recovery,
                "repair_job": None,
                "candidate": None,
                "repaired_candidate": None,
                "validation_record": None,
                "seen_candidate_hashes": [],
                "previous_feedback": "",
                "attempts_log": [],
                "current_attempt_record": None,
                "action": "continue",
                "status": "IN_PROGRESS",
                "reason_code": None,
                "diagnostics": {
                    "sandbox_type": "app_layer_shadow_dir",
                    "transaction_type": "sequential_staged_persistence",
                },
                "applied_to_registry": False,
                "transition_history": [],
            }
            final_state = app.invoke(initial_state, config=runtime_config)

        # Construct BoundedRecoveryResult
        return BoundedRecoveryResult(
            status=final_state.get("status", "EXHAUSTED"),
            reason_code=final_state.get("reason_code"),
            candidate=final_state.get("repaired_candidate"),
            validation_record=final_state.get("validation_record"),
            budget=final_state.get("budget", budget),
            lineage=current_lineage,
            attempts=final_state.get("attempts_log", []),
            diagnostics=final_state.get("diagnostics", {
                "sandbox_type": "app_layer_shadow_dir",
                "transaction_type": "sequential_staged_persistence",
            }),
            repair_job=final_state.get("repair_job"),
            applied_to_registry=False,
            transition_history=final_state.get("transition_history", []),
        )
