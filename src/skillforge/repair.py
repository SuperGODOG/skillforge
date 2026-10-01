"""Failure Attribution and Bounded Repair Module (Milestone 4a)

Provides controlled failure diagnosis, directional patching, sandboxed regression
validation, and gated promotion for SkillForge skills.

Key Invariants:
1. Strict Attribution Hierarchy:
   Root cause is categorized across 6 responsibility layers:
   'skill' | 'tool' | 'policy' | 'planner' | 'evaluator' | 'unknown'.
   High-confidence structured signals (permission denials, tool crashes, invalid test suites)
   strictly override LLM suggestions. LLM cannot force non-skill failures into 'skill'.
2. Controlled Scope of Repair:
   Only 'skill' failures trigger candidate patching (along trigger/prompt/dependencies/boundary strategies).
   All other responsibility layers return handoff/needs_review info without modifying Policy/Tool/Evaluator.
3. Bounded Repair Job & Budget Persistence:
   Repair jobs track attempts, candidate hashes, and diagnosis in SQLite (repair_jobs table).
   Default max_attempts = 2. Budget is preserved across DB reopen and duplicate calls.
   Duplicate patch hash detection terminates immediately to prevent redundant evaluation.
4. Retry Routing & Isolation:
   Only DECLINED verdicts from valid business evaluations can retry within budget.
   REVIEW verdicts stop immediately for human review.
   Evaluator/infrastructure errors mark the job as BLOCKED, not as a skill defect.
   Independent evaluation sets / heldout sentinels NEVER leak into patcher prompts.
5. Gated Promotion:
   Only PASS verdict produces a READY candidate.
   Promotion requires explicit caller confirmation (caller_confirmed=True) and uses the M2
   ReleaseStateMachine path. Baseline drift or candidate hash mutations invalidate promotion.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

from .models import (
    Episode,
    CandidateSkill,
    CandidateDecision,
    CandidateStatus,
    SkillMeta,
    Trigger,
    Release,
    EvalResult,
    RatchetVerdict,
    ToolCallProvenance,
    RecoveryBudget,
    TaskContext,
)
from .episode import EpisodeStore, CandidateStore
from .registry import SkillRegistry
from .evaluator import SkillEvaluator
from .evolution_loop import (
    validate_candidate,
    promote_candidate,
    compute_candidate_hash,
    ValidationRecord,
)
from .state_machine import ReleaseStateMachine
from .diff import compute_semantic_diff


ResponsibilityLayer = Literal["skill", "tool", "policy", "planner", "evaluator", "unknown"]
PatchStrategy = Literal["trigger", "prompt", "dependencies", "boundary"]
JobStatus = Literal[
    "PENDING",
    "IN_PROGRESS",
    "READY",
    "AWAITING_REVIEW",
    "DECLINED",
    "BLOCKED",
    "EXHAUSTED",
    "PROMOTED",
]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class AttributionDiagnosis:
    """Diagnostic outcome attributing failure to a specific responsibility layer."""

    responsibility_layer: ResponsibilityLayer
    strategy: Optional[PatchStrategy] = None
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    handoff_info: Optional[str] = None
    structured_signal_override: bool = False


@dataclass
class RepairAttemptRecord:
    """Record of a single repair attempt within a RepairJob."""

    attempt_number: int
    candidate_id: Optional[str] = None
    candidate_hash: Optional[str] = None
    patch_diff: Optional[str] = None
    validation_decision: Optional[Literal["PASS", "REVIEW", "DECLINED", "BLOCKED"]] = None
    eval_summary: dict[str, Any] = field(default_factory=dict)
    error_feedback: str = ""


@dataclass
class RepairJob:
    """Persistent repair job tracking diagnosis, attempt budget, and regression outcomes."""

    job_id: str
    fingerprint: str
    skill_name: str
    baseline_version: str
    source_episode_ids: list[str]
    diagnosis: AttributionDiagnosis
    status: JobStatus
    max_attempts: int
    current_attempt: int
    attempts: list[RepairAttemptRecord] = field(default_factory=list)
    latest_candidate: Optional[CandidateSkill] = None
    latest_content_hash: Optional[str] = None
    stop_reason: Optional[str] = None
    release_id: Optional[str] = None
    recovery_result: Optional[Any] = None
    bounded_recovery_result: Optional[Any] = None

    def save(self, conn: sqlite3.Connection) -> None:
        """Persist this RepairJob to SQLite."""
        _save_job(conn, self)

    @classmethod
    def load(cls, conn: sqlite3.Connection, fingerprint: str, candidate_store: CandidateStore) -> Optional[RepairJob]:
        """Load RepairJob from SQLite by fingerprint."""
        return _load_job(conn, fingerprint, candidate_store)

    def run(
        self,
        episodes: list[Episode],
        registry: SkillRegistry,
        evaluator: SkillEvaluator,
        eval_cases: list[dict],
        candidate_store: CandidateStore,
        llm: Any = None,
        budget: Optional[RecoveryBudget] = None,
        conn: Optional[sqlite3.Connection] = None,
        error_feedback: str = "",
        previous_hashes: Optional[set[str]] = None,
    ) -> RepairAttemptRecord:
        """Execute a single bounded repair attempt (RepairJob protocol execution)."""
        return self.run_attempt(
            episodes=episodes,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            llm=llm,
            budget=budget,
            conn=conn,
            error_feedback=error_feedback,
            previous_hashes=previous_hashes,
        )

    def run_attempt(
        self,
        episodes: list[Episode],
        registry: SkillRegistry,
        evaluator: SkillEvaluator,
        eval_cases: list[dict],
        candidate_store: CandidateStore,
        llm: Any = None,
        budget: Optional[RecoveryBudget] = None,
        conn: Optional[sqlite3.Connection] = None,
        error_feedback: str = "",
        previous_hashes: Optional[set[str]] = None,
    ) -> RepairAttemptRecord:
        """Execute a single bounded repair attempt on this RepairJob.

        Integrates with shared RecoveryBudget, validates against unified gate,
        detects duplicate candidate hashes, and updates SQLite repair_jobs table.
        """
        self.current_attempt += 1
        att_num = self.current_attempt

        if previous_hashes is None:
            previous_hashes = set(a.candidate_hash for a in self.attempts if a.candidate_hash)

        if not error_feedback and self.attempts:
            error_feedback = self.attempts[-1].error_feedback

        base_meta = registry.get_meta(self.skill_name)
        base_body = registry.get_body(self.skill_name) if hasattr(registry, "get_body") else registry._bodies.get(self.skill_name, "")
        target_patch_version = _bump_patch_version(self.baseline_version)

        if llm is None:
            cand_id = f"cand_repair_mock_{att_num}_{uuid.uuid4().hex[:8]}"
            if "3. Collate delivery carrier details and report progress to user." in base_body:
                patched_body = base_body.replace(
                    "3. Collate delivery carrier details and report progress to user.",
                    f"3. Collate carrier details and report progress to user (attempt {att_num}).",
                )
            else:
                patched_body = f"{base_body.rstrip()}\n\n<!-- attempt {att_num} fix -->\n"
            import copy
            cand_meta = copy.deepcopy(base_meta)
            cand_meta.version = target_patch_version
            candidate = CandidateSkill(
                candidate_id=cand_id,
                skill_name=self.skill_name,
                decision="revise",
                source_episode_ids=[e.episode_id for e in episodes],
                meta=cand_meta,
                body=patched_body,
                rationale=f"Mock repair attempt {att_num}",
                status="DRAFT",
            )
            if budget is not None:
                budget.consume(calls=1, tokens=None)
        else:
            prompt_lines = [
                f"You are a Skill Patcher repairing '{self.skill_name}'.",
                f"Current baseline version: {self.baseline_version}. Target patched version: {target_patch_version}.",
                f"Attributed strategy: {self.diagnosis.strategy}. Root cause: {self.diagnosis.reason}.",
                f"\n--- Current SKILL.md ---\n{registry.get_raw(self.skill_name) if hasattr(registry, 'get_raw') else base_body}",
                "\n--- Observed Learning Failures ---",
            ]
            for ep in episodes:
                prompt_lines.append(f"- Task: {ep.environment.get('query')}; Failure: {ep.outcome_reason}")

            if error_feedback:
                prompt_lines.append(f"\n--- Feedback from previous attempt ---\n{error_feedback}")

            prompt_lines.append(
                f"\nGenerate a revised SKILL.md for '{self.skill_name}' with version '{target_patch_version}'. "
                "Output valid YAML frontmatter between --- and markdown body."
            )
            patcher_prompt = "\n".join(prompt_lines)

            resp = llm.invoke(patcher_prompt)
            toks = None
            if hasattr(resp, "usage") and isinstance(resp.usage, dict) and "total_tokens" in resp.usage:
                toks = resp.usage["total_tokens"]
            if budget is not None:
                budget.consume(calls=1, tokens=toks)

            raw_output = getattr(resp, "content", str(resp)).strip()
            if raw_output.startswith("```"):
                raw_output = re.sub(r"^```(?:markdown)?\n|```$", "", raw_output, flags=re.MULTILINE).strip()

            m = _FRONTMATTER_RE.match(raw_output)
            if not m:
                rec = RepairAttemptRecord(
                    attempt_number=att_num,
                    validation_decision="DECLINED",
                    error_feedback="Malformed SKILL.md: lacks valid YAML frontmatter (--- ... ---)",
                )
                self.attempts.append(rec)
                if self.current_attempt >= self.max_attempts:
                    self.status = "EXHAUSTED"
                    self.stop_reason = f"Budget exhausted ({self.max_attempts} attempts reached) after malformed outputs"
                if conn is not None:
                    _save_job(conn, self)
                return rec

            fm_text, cand_body = m.group(1), m.group(2).strip()
            try:
                cand_meta_dict = yaml.safe_load(fm_text) or {}
                cand_meta = SkillMeta(**cand_meta_dict)
            except Exception as e:
                rec = RepairAttemptRecord(
                    attempt_number=att_num,
                    validation_decision="DECLINED",
                    error_feedback=f"Invalid frontmatter structure: {e}",
                )
                self.attempts.append(rec)
                if self.current_attempt >= self.max_attempts:
                    self.status = "EXHAUSTED"
                    self.stop_reason = f"Budget exhausted: {e}"
                if conn is not None:
                    _save_job(conn, self)
                return rec

            if cand_meta.name != self.skill_name:
                rec = RepairAttemptRecord(
                    attempt_number=att_num,
                    validation_decision="DECLINED",
                    error_feedback=f"Candidate name '{cand_meta.name}' does not match target '{self.skill_name}'",
                )
                self.attempts.append(rec)
                if self.current_attempt >= self.max_attempts:
                    self.status = "EXHAUSTED"
                    self.stop_reason = "Budget exhausted: mismatched skill name"
                if conn is not None:
                    _save_job(conn, self)
                return rec

            baseline_md = f"---\n{yaml.dump(base_meta.model_dump(), sort_keys=False)}---\n\n{base_body}"
            diff_res = compute_semantic_diff(baseline_md, raw_output, declared_level="L2")
            if not diff_res.is_valid:
                rec = RepairAttemptRecord(
                    attempt_number=att_num,
                    validation_decision="DECLINED",
                    error_feedback=diff_res.invalid_reason or "Diff invalid",
                )
                self.attempts.append(rec)
                if self.current_attempt >= self.max_attempts:
                    self.status = "EXHAUSTED"
                    self.stop_reason = diff_res.invalid_reason
                if conn is not None:
                    _save_job(conn, self)
                return rec

            cand_id = f"cand_repair_{uuid.uuid4().hex[:12]}"
            candidate = CandidateSkill(
                candidate_id=cand_id,
                skill_name=self.skill_name,
                decision="revise",
                source_episode_ids=[e.episode_id for e in episodes],
                meta=cand_meta,
                body=cand_body,
                rationale=f"Automated repair attempt {att_num} for {self.diagnosis.reason}",
                status="DRAFT",
            )

        cand_hash = compute_candidate_hash(candidate)

        # Duplicate patch hash detection: STOP immediately
        if cand_hash in previous_hashes:
            rec = RepairAttemptRecord(
                attempt_number=att_num,
                candidate_id=cand_id,
                candidate_hash=cand_hash,
                validation_decision="DECLINED",
                error_feedback="Duplicate patch hash detected",
            )
            self.attempts.append(rec)
            self.status = "DECLINED"
            self.stop_reason = "Duplicate patch hash detected: identical candidate generated across attempts"
            if conn is not None:
                _save_job(conn, self)
            return rec

        previous_hashes.add(cand_hash)
        candidate_store.save_candidate(candidate)
        self.latest_candidate = candidate
        self.latest_content_hash = cand_hash

        # Sandbox regression evaluation
        val_rec = validate_candidate(
            candidate=candidate,
            evaluator=evaluator,
            registry=registry,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
        )

        # Check for evaluator/infrastructure error (vs business evaluation decline)
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
            reasons = val_rec.eval_result.invalid_reasons or ["Evaluator marked invalid"]
            rec = RepairAttemptRecord(
                attempt_number=att_num,
                candidate_id=cand_id,
                candidate_hash=cand_hash,
                validation_decision="BLOCKED",
                eval_summary={"valid": False, "reasons": reasons},
                error_feedback="; ".join(reasons),
            )
            self.attempts.append(rec)
            self.status = "BLOCKED"
            self.stop_reason = f"Evaluator infrastructure error: {'; '.join(reasons)}"
            if conn is not None:
                _save_job(conn, self)
            return rec

        verdict_dec = val_rec.ratchet_decision
        if verdict_dec == "PASS":
            rec = RepairAttemptRecord(
                attempt_number=att_num,
                candidate_id=cand_id,
                candidate_hash=cand_hash,
                validation_decision="PASS",
                eval_summary={"ratchet": "PASS"},
            )
            self.attempts.append(rec)
            self.status = "READY"
            self.stop_reason = None
        elif verdict_dec == "REVIEW":
            reasons = val_rec.ratchet_verdict.reasons if val_rec.ratchet_verdict else ["Requires human review"]
            rec = RepairAttemptRecord(
                attempt_number=att_num,
                candidate_id=cand_id,
                candidate_hash=cand_hash,
                validation_decision="REVIEW",
                eval_summary={"ratchet": "REVIEW", "reasons": reasons},
                error_feedback="; ".join(reasons),
            )
            self.attempts.append(rec)
            self.status = "AWAITING_REVIEW"
            self.stop_reason = "Ratchet verdict is REVIEW: requires human approval, automatic retry blocked"
        else:  # DECLINED
            reasons = val_rec.ratchet_verdict.reasons if val_rec.ratchet_verdict else ["Declined by ratchet gate"]
            error_feedback = f"Declined by ratchet gate: {'; '.join(reasons)}"
            rec = RepairAttemptRecord(
                attempt_number=att_num,
                candidate_id=cand_id,
                candidate_hash=cand_hash,
                validation_decision="DECLINED",
                eval_summary={"ratchet": "DECLINED", "reasons": reasons},
                error_feedback=error_feedback,
            )
            self.attempts.append(rec)
            if self.current_attempt >= self.max_attempts:
                self.status = "EXHAUSTED"
                self.stop_reason = f"Budget exhausted ({self.max_attempts} attempts reached) with DECLINED verdict"

        if conn is not None:
            _save_job(conn, self)
        return rec


def _compute_repair_fingerprint(
    skill_name: str,
    baseline_version: str,
    source_episode_ids: list[str],
) -> str:
    """Deterministic hash of skill, baseline, and failure sources."""
    payload = {
        "skill_name": skill_name,
        "baseline_version": baseline_version,
        "source_episode_ids": sorted(source_episode_ids),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _detect_structured_signal(ep: Episode) -> Optional[tuple[ResponsibilityLayer, str, list[str]]]:
    """Inspect structured execution signals for non-skill failures.

    High-confidence signals:
    - Policy violation (403, permission denied, access denied)
    - Tool / infrastructure crash (500, ConnectionRefused, Timeout, BrokenPipe)
    - Evaluator invalidity (invalid eval, test syntax error, judge timeout)
    - Planner error (planner omitted parameters, routing failure)
    """
    provs = ep.provenances or []
    verif = ep.verification_evidence or {}
    verif_text = json.dumps(verif).lower()
    reason_text = (ep.outcome_reason or "").lower()

    # 1. Policy violation check
    policy_keywords = ("permission", "unauthorized", "access denied", "forbidden", "403", "auth error", "security policy")
    for p in provs:
        summary_lower = (p.output_summary or "").lower()
        if p.output_status in ("ERROR", "CIRCUIT_OPEN") and any(k in summary_lower for k in policy_keywords):
            return "policy", f"Permission/policy denied in tool '{p.tool_name}': {p.output_summary[:100]}", [p.tool_name, ep.episode_id]

    if any(k in verif_text for k in policy_keywords) or any(k in reason_text for k in policy_keywords):
        return "policy", f"Security or policy constraint violated: {ep.outcome_reason}", [ep.episode_id]

    # 2. Tool / Infrastructure failure check
    tool_infra_keywords = (
        "connectionrefused", "connection refused", "econnrefused", "timeout", "timed out",
        "500 internal", "http 500", "http 502", "http 503", "broken pipe",
        "tool crash", "tool execution error", "internal server error", "infrastructure error",
    )
    for p in provs:
        summary_lower = (p.output_summary or "").lower()
        if p.output_status in ("ERROR", "CIRCUIT_OPEN") and any(k in summary_lower for k in tool_infra_keywords):
            return "tool", f"Tool/infrastructure failure in '{p.tool_name}': {p.output_summary[:100]}", [p.tool_name, ep.episode_id]

    # 3. Evaluator invalidity check
    evaluator_keywords = ("is_invalid_eval", "evaluator error", "judge timeout", "judge failure", "syntax error in test", "test suite error", "invalid_judge_result")
    if any(k in verif_text for k in evaluator_keywords) or any(k in reason_text for k in evaluator_keywords):
        return "evaluator", f"Evaluator invalidity: {ep.outcome_reason}", [ep.episode_id]

    # 4. Planner error check
    planner_keywords = ("planner omitted", "wrong tool selected by planner", "planner error", "routing planner failure")
    if any(k in verif_text for k in planner_keywords) or any(k in reason_text for k in planner_keywords):
        return "planner", f"Planner error: {ep.outcome_reason}", [ep.episode_id]

    return None


def attribute_failure(
    episodes: list[Episode],
    skill_name: str,
    llm: Any = None,
) -> AttributionDiagnosis:
    """Attribute failure root cause across 6 responsibility layers.

    Guarantees:
    - High-confidence structured signals (policy, tool, evaluator, planner) take precedence.
    - LLM diagnostic recommendations are validated against legal categories and real evidence refs.
    - Conflicting or insufficient evidence falls back to 'unknown'.
    - Controlled heuristic proxy; does not claim absolute mathematical infallibility.
    """
    if not episodes:
        raise ValueError("Cannot attribute failure for empty episode list")

    # A8 Purpose Isolation: filter non-learning episodes
    valid_learning: list[Episode] = []
    for ep in episodes:
        purpose = ep.environment.get("purpose", "") if isinstance(ep.environment, dict) else ""
        if purpose == "learning" and ep.outcome == "failure":
            valid_learning.append(ep)

    if not valid_learning:
        return AttributionDiagnosis(
            responsibility_layer="unknown",
            reason="No verified learning failure episodes provided for attribution",
            handoff_info="Requires valid learning failure episodes with verified evidence",
        )

    # 1. High-confidence structured signals scan
    all_refs = set(ep.episode_id for ep in valid_learning)
    for ep in valid_learning:
        for p in ep.provenances:
            all_refs.add(p.tool_name)

    for ep in valid_learning:
        detected = _detect_structured_signal(ep)
        if detected is not None:
            layer, reason, refs = detected
            handoff = {
                "policy": "Security or policy constraint violated; requires security/admin review, cannot alter policy automatically.",
                "tool": "Tool/infrastructure failure detected; requires tool maintenance or environment recovery, not a skill defect.",
                "evaluator": "Evaluator invalidity detected; requires test suite correction, not a skill defect.",
                "planner": "Planner routing or parameter planning failure; requires planner model tuning, not skill modification.",
            }.get(layer, "Requires external review")
            return AttributionDiagnosis(
                responsibility_layer=layer,
                strategy=None,
                reason=reason,
                evidence_refs=refs,
                handoff_info=handoff,
                structured_signal_override=True,
            )

    # 2. Check LLM recommendation if available
    if llm is not None:
        lines = [
            f"Diagnose failure root cause for skill '{skill_name}'.",
            "Available responsibility layers: 'skill', 'tool', 'policy', 'planner', 'evaluator', 'unknown'.",
            "Available strategies (if skill): 'trigger', 'prompt', 'dependencies', 'boundary'.",
            "Observed failure evidence:",
        ]
        for ep in valid_learning:
            lines.append(f"- Episode {ep.episode_id}: reason={ep.outcome_reason}, query={ep.environment.get('query')}")
            for p in ep.provenances:
                lines.append(f"  Tool {p.tool_name}: status={p.output_status}, output={p.output_summary[:80]}")

        lines.append(
            "\nOutput JSON strictly: "
            '{"responsibility_layer": "...", "strategy": "...", "reason": "...", "evidence_refs": ["..."]}'
        )
        prompt = "\n".join(lines)

        try:
            resp = llm.invoke(prompt)
            raw = getattr(resp, "content", str(resp))
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\n|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)

            layer = data.get("responsibility_layer", "").lower().strip()
            if layer not in ("skill", "tool", "policy", "planner", "evaluator", "unknown"):
                return AttributionDiagnosis(
                    responsibility_layer="unknown",
                    reason=f"Model returned invalid responsibility layer: '{layer}'",
                    handoff_info="Model returned unknown responsibility layer; requires human review",
                )

            refs = data.get("evidence_refs", [])
            if not isinstance(refs, list) or not refs:
                return AttributionDiagnosis(
                    responsibility_layer="unknown",
                    reason="Model diagnosis lacks evidence references",
                    handoff_info="Diagnosis missing verifiable evidence references",
                )

            # Check for forged references
            for r in refs:
                if str(r) not in all_refs:
                    return AttributionDiagnosis(
                        responsibility_layer="unknown",
                        reason=f"Model provided unverified/forged evidence reference: '{r}'",
                        handoff_info="Model referenced non-existent evidence; rejected",
                    )

            strat = data.get("strategy")
            valid_strats = ("trigger", "prompt", "dependencies", "boundary")
            if strat not in valid_strats:
                strat = "prompt" if layer == "skill" else None

            handoff = None
            if layer != "skill":
                handoff = f"Diagnosed as {layer} failure; handed off to {layer} maintainers without modifying skill."

            return AttributionDiagnosis(
                responsibility_layer=layer,  # type: ignore
                strategy=strat,  # type: ignore
                reason=str(data.get("reason", "Diagnosed by LLM")),
                evidence_refs=[str(r) for r in refs],
                handoff_info=handoff,
            )
        except Exception as e:
            pass  # Fall through to default heuristic

    # 3. Evidence sufficiency check: if no provenances and no outcome_reason -> unknown
    has_evidence = any(
        (bool(ep.provenances) or bool(ep.outcome_reason and ep.outcome_reason.strip()))
        for ep in valid_learning
    )
    if not has_evidence:
        return AttributionDiagnosis(
            responsibility_layer="unknown",
            reason="Insufficient evidence: failure episodes lack tool provenances and outcome reasons",
            handoff_info="Insufficient execution evidence to attribute root cause",
        )

    # 4. Default heuristic for verified business failures: skill instruction issue
    first_ep = valid_learning[0]
    return AttributionDiagnosis(
        responsibility_layer="skill",
        strategy="prompt",
        reason=f"Verified business failure in {len(valid_learning)} episodes: {first_ep.outcome_reason}",
        evidence_refs=[ep.episode_id for ep in valid_learning],
    )


def _bump_patch_version(version: str) -> str:
    """Strict semver patch bump: 1.0.0 -> 1.0.1."""
    parts = version.strip().split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return f"{version}.1"


def _load_job(conn: sqlite3.Connection, fingerprint: str, candidate_store: CandidateStore) -> Optional[RepairJob]:
    """Load RepairJob from SQLite repair_jobs table if exists."""
    row = conn.execute(
        """SELECT job_id, fingerprint, skill_name, baseline_version, source_episode_ids,
                  responsibility_layer, strategy, status, attempts_json, max_attempts,
                  current_attempt, diagnosis_json, latest_candidate_id, latest_content_hash,
                  stop_reason, release_id
           FROM repair_jobs WHERE fingerprint = ?""",
        (fingerprint,),
    ).fetchone()
    if not row:
        return None

    (
        job_id, fp, name, base_ver, src_ids_json, layer, strat, status,
        attempts_json, max_att, curr_att, diag_json, latest_cid, latest_hash,
        stop_reason, rel_id,
    ) = row

    diag_data = json.loads(diag_json)
    diag = AttributionDiagnosis(
        responsibility_layer=diag_data.get("responsibility_layer", "unknown"),
        strategy=diag_data.get("strategy"),
        reason=diag_data.get("reason", ""),
        evidence_refs=diag_data.get("evidence_refs", []),
        handoff_info=diag_data.get("handoff_info"),
    )

    attempts_data = json.loads(attempts_json)
    attempts = [RepairAttemptRecord(**item) for item in attempts_data]

    cand = candidate_store.get_candidate(latest_cid) if latest_cid else None

    return RepairJob(
        job_id=job_id,
        fingerprint=fp,
        skill_name=name,
        baseline_version=base_ver,
        source_episode_ids=json.loads(src_ids_json),
        diagnosis=diag,
        status=status,
        max_attempts=max_att,
        current_attempt=curr_att,
        attempts=attempts,
        latest_candidate=cand,
        latest_content_hash=latest_hash,
        stop_reason=stop_reason,
        release_id=rel_id,
    )


def _save_job(conn: sqlite3.Connection, job: RepairJob) -> None:
    """Upsert RepairJob in SQLite repair_jobs table."""
    attempts_data = [asdict(a) for a in job.attempts]
    diag_data = asdict(job.diagnosis)

    conn.execute(
        """INSERT INTO repair_jobs (
            job_id, fingerprint, skill_name, baseline_version, source_episode_ids,
            responsibility_layer, strategy, status, attempts_json, max_attempts,
            current_attempt, diagnosis_json, latest_candidate_id, latest_content_hash,
            stop_reason, release_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(fingerprint) DO UPDATE SET
            status = excluded.status,
            attempts_json = excluded.attempts_json,
            current_attempt = excluded.current_attempt,
            latest_candidate_id = excluded.latest_candidate_id,
            latest_content_hash = excluded.latest_content_hash,
            stop_reason = excluded.stop_reason,
            release_id = excluded.release_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            job.job_id,
            job.fingerprint,
            job.skill_name,
            job.baseline_version,
            json.dumps(job.source_episode_ids),
            job.diagnosis.responsibility_layer,
            job.diagnosis.strategy,
            job.status,
            json.dumps(attempts_data),
            job.max_attempts,
            job.current_attempt,
            json.dumps(diag_data),
            job.latest_candidate.candidate_id if job.latest_candidate else None,
            job.latest_content_hash,
            job.stop_reason,
            job.release_id,
        ),
    )
    conn.commit()


def repair_skill_failure(
    episodes: list[Episode],
    skill_name: str,
    episode_store: EpisodeStore,
    candidate_store: CandidateStore,
    registry: SkillRegistry,
    evaluator: SkillEvaluator,
    eval_cases: list[dict],
    llm: Any,
    max_attempts: int = 2,
    p0_ids: Optional[list[str]] = None,
    diagnostic_llm: Optional[Any] = None,
    enable_shadow_recovery: bool = False,
    task_context: Optional[TaskContext] = None,
    shared_budget: Optional[RecoveryBudget] = None,
    checkpointer: Optional[Any] = None,
    thread_id: Optional[str] = None,
    candidate: Optional[CandidateSkill] = None,
    is_receipt_defect: bool = False,
    user_cancelled: bool = False,
    baseline_drift: bool = False,
    metric_jump: Optional[float] = None,
    resume_checkpoint: Optional[dict[str, Any]] = None,
    interrupt_before: Optional[list[str]] = None,
    interrupt_after: Optional[list[str]] = None,
) -> RepairJob:
    """Orchestrate bounded repair loop for failed skill episodes.

    Process:
    1. Filter & validate source episodes from learning pool.
    2. Attribute root cause across responsibility layers.
    3. If non-skill layer -> stop immediately with handoff/needs_review.
    4. If skill -> perform bounded patch attempts against regression evaluator.
    5. Terminate on PASS (READY), REVIEW (AWAITING_REVIEW), evaluator error (BLOCKED),
       duplicate patch hash (DECLINED), or budget exhausted (EXHAUSTED).
    """
    if not episodes:
        raise ValueError("Cannot repair failure from empty episode list")

    # Reject non-failure outcomes early
    for ep in episodes:
        if ep.outcome in ("success", "unknown"):
            raise ValueError(f"Episode '{ep.episode_id}' has outcome='{ep.outcome}'; only failed episodes may trigger repair")

    # A8 Purpose Isolation: filter learning episodes
    learning_episodes: list[Episode] = []
    for ep in episodes:
        if not episode_store.has_episode(ep.episode_id):
            raise KeyError(f"Source episode '{ep.episode_id}' not found in EpisodeStore")
        purpose = ep.environment.get("purpose", "") if isinstance(ep.environment, dict) else ""
        if purpose == "learning":
            learning_episodes.append(ep)

    if not learning_episodes:
        raise ValueError("No valid learning episodes found for repair (A8 isolation)")

    # D5 Purpose Isolation: regression evaluation in repair loop cannot contain locked heldout cases
    for case in eval_cases:
        layer = str(case.get("layer", case.get("partition_tier", "")))
        is_heldout = bool(case.get("is_heldout", layer in ("experiment_holdout", "final_audit")))
        if is_heldout or layer in ("experiment_holdout", "final_audit"):
            raise ValueError(
                f"Purpose isolation violation: case '{case.get('id')}' is from locked tier '{layer}' "
                "and cannot be used in repair regression without explicit demotion"
            )


    if skill_name not in registry.list_names():
        raise KeyError(f"Target skill '{skill_name}' not found in active registry")

    base_meta = registry.get_meta(skill_name)
    baseline_version = base_meta.version
    base_body = registry.get_body(skill_name) if hasattr(registry, "get_body") else registry._bodies.get(skill_name, "")

    conn = episode_store._get_conn()
    fp = _compute_repair_fingerprint(skill_name, baseline_version, [e.episode_id for e in learning_episodes])

    # Check existing job (idempotency)
    existing_job = _load_job(conn, fp, candidate_store)
    if existing_job is not None:
        if existing_job.status in ("READY", "AWAITING_REVIEW", "DECLINED", "BLOCKED", "EXHAUSTED", "PROMOTED"):
            return existing_job

    if candidate is not None:
        from .bounded_recovery import recover_bloated_candidate
        eff_budget = shared_budget or RecoveryBudget(max_attempts=max_attempts)
        rec_res = recover_bloated_candidate(
            candidate=candidate,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            enable_shadow_recovery=enable_shadow_recovery,
            llm=llm,
            budget=eff_budget,
            scope_hash=getattr(candidate, "task_spec_hash", None),
        )
        cand_status: JobStatus = (
            "AWAITING_REVIEW"
            if rec_res.status == "AWAITING_REVIEW"
            else ("READY" if rec_res.status == "SUCCESS" else "BLOCKED")
        )
        cand_job = RepairJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            fingerprint=fp,
            skill_name=skill_name,
            baseline_version=baseline_version,
            source_episode_ids=[e.episode_id for e in learning_episodes],
            diagnosis=AttributionDiagnosis(
                responsibility_layer="skill",
                strategy="prompt",
                reason="Prompt bloat candidate evaluation",
                evidence_refs=[e.episode_id for e in learning_episodes],
            ),
            status=cand_status,
            max_attempts=max_attempts,
            current_attempt=0,
            attempts=[],
            latest_candidate=rec_res.candidate or candidate,
            latest_content_hash=compute_candidate_hash(rec_res.candidate or candidate),
            stop_reason=rec_res.reason_code,
        )
        cand_job.recovery_result = rec_res
        cand_job.bounded_recovery_result = rec_res
        _save_job(conn, cand_job)
        return cand_job

    if enable_shadow_recovery:
        from .bounded_recovery import check_non_recoverable_blockers, run_bounded_recovery
        blocked, reason_code, reason_detail = check_non_recoverable_blockers(
            episodes=learning_episodes,
            baseline_drift=baseline_drift,
            user_cancelled=user_cancelled,
            metric_jump=metric_jump,
            is_receipt_defect=is_receipt_defect,
        )
        if blocked:
            status: JobStatus = "AWAITING_REVIEW" if reason_code == "NON_RECOVERABLE_PERMISSION_DENIED" else "BLOCKED"
            blocked_job = RepairJob(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                fingerprint=fp,
                skill_name=skill_name,
                baseline_version=baseline_version,
                source_episode_ids=[e.episode_id for e in learning_episodes],
                diagnosis=AttributionDiagnosis(
                    responsibility_layer="policy" if "PERMISSION" in reason_code else ("tool" if "TOOL" in reason_code or "ENV" in reason_code else ("evaluator" if "EVAL" in reason_code else "unknown")),
                    reason=reason_detail or reason_code,
                    handoff_info=reason_detail,
                    structured_signal_override=True,
                ),
                status=status,
                max_attempts=max_attempts,
                current_attempt=0,
                attempts=[],
                stop_reason=reason_code or reason_detail,
            )
            _save_job(conn, blocked_job)
            return blocked_job

        eff_budget = shared_budget or RecoveryBudget(max_attempts=max_attempts)
        rec_res = run_bounded_recovery(
            skill_name=skill_name,
            episodes=learning_episodes,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            episode_store=episode_store,
            llm=llm,
            task_context=task_context,
            shared_budget=eff_budget,
            enable_shadow_recovery=True,
            candidate=candidate,
            is_receipt_defect=is_receipt_defect,
            user_cancelled=user_cancelled,
            baseline_drift=baseline_drift,
            metric_jump=metric_jump,
            resume_checkpoint=resume_checkpoint,
            checkpointer=checkpointer,
            thread_id=thread_id,
            conn=conn,
            diagnostic_llm=diagnostic_llm,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )

        job = rec_res.repair_job
        if job is None:
            job = existing_job or RepairJob(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                fingerprint=fp,
                skill_name=skill_name,
                baseline_version=baseline_version,
                source_episode_ids=[e.episode_id for e in learning_episodes],
                diagnosis=AttributionDiagnosis(
                    responsibility_layer="skill",
                    strategy="prompt",
                    reason=f"Bounded shadow recovery for {skill_name}",
                    evidence_refs=[e.episode_id for e in learning_episodes],
                ),
                status="IN_PROGRESS",
                max_attempts=max_attempts,
                current_attempt=eff_budget.consumed_attempts,
                attempts=[],
            )

        if rec_res.status == "SUCCESS":
            job.status = "READY"
        elif rec_res.status == "AWAITING_REVIEW":
            job.status = "AWAITING_REVIEW"
        elif rec_res.status == "EXHAUSTED":
            job.status = "EXHAUSTED"
        elif rec_res.status == "DECLINED":
            job.status = "DECLINED"
        elif rec_res.status in ("BLOCKED", "STOPPED"):
            job.status = "BLOCKED"

        if rec_res.candidate:
            job.latest_candidate = rec_res.candidate
            job.latest_content_hash = compute_candidate_hash(rec_res.candidate)
        if rec_res.reason_code:
            job.stop_reason = rec_res.reason_code

        job.recovery_result = rec_res
        job.bounded_recovery_result = rec_res
        _save_job(conn, job)
        return job

    # Attribute failure
    diagnosis = attribute_failure(learning_episodes, skill_name, llm=diagnostic_llm)

    if diagnosis.responsibility_layer != "skill":
        status: JobStatus = "BLOCKED" if diagnosis.responsibility_layer in ("tool", "evaluator") else (
            "AWAITING_REVIEW" if diagnosis.responsibility_layer == "policy" else "DECLINED"
        )
        non_skill_job = RepairJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            fingerprint=fp,
            skill_name=skill_name,
            baseline_version=baseline_version,
            source_episode_ids=[e.episode_id for e in learning_episodes],
            diagnosis=diagnosis,
            status=status,
            max_attempts=max_attempts,
            current_attempt=0,
            attempts=[],
            stop_reason=diagnosis.handoff_info,
        )
        _save_job(conn, non_skill_job)
        return non_skill_job

    job = existing_job or RepairJob(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        fingerprint=fp,
        skill_name=skill_name,
        baseline_version=baseline_version,
        source_episode_ids=[e.episode_id for e in learning_episodes],
        diagnosis=diagnosis,
        status="IN_PROGRESS",
        max_attempts=max_attempts,
        current_attempt=0,
        attempts=[],
    )

    target_patch_version = _bump_patch_version(baseline_version)
    previous_hashes = set(a.candidate_hash for a in job.attempts if a.candidate_hash)

    error_feedback = ""
    if job.attempts:
        last_att = job.attempts[-1]
        error_feedback = last_att.error_feedback

    # Bounded repair loop
    while job.current_attempt < job.max_attempts:
        rec = job.run_attempt(
            episodes=learning_episodes,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            candidate_store=candidate_store,
            llm=llm,
            conn=conn,
            error_feedback=error_feedback,
            previous_hashes=previous_hashes,
        )
        if rec.candidate_hash:
            previous_hashes.add(rec.candidate_hash)
        error_feedback = rec.error_feedback

        if job.status in ("READY", "AWAITING_REVIEW", "BLOCKED", "DECLINED", "EXHAUSTED"):
            if job.status == "DECLINED" and job.stop_reason and "Duplicate patch hash" in job.stop_reason:
                break
            if job.status in ("READY", "AWAITING_REVIEW", "BLOCKED"):
                break
            if job.current_attempt >= job.max_attempts:
                break

    _save_job(conn, job)
    return job


def promote_repaired_skill(
    job: RepairJob,
    candidate_store: CandidateStore,
    registry: SkillRegistry,
    state_machine: ReleaseStateMachine,
    caller_confirmed: bool = False,
) -> Release:
    """Promote a READY repair candidate to active registry via ReleaseStateMachine.

    Guarantees:
    - Explicit caller confirmation is required.
    - Only READY jobs with PASS verdict may be promoted.
    - Candidate content hash must match recorded hash.
    - Registry baseline version must match job baseline version.
    - Duplicate promotion is rejected.
    """
    if not caller_confirmed:
        raise ValueError("Promotion blocked: requires explicit caller confirmation (caller_confirmed=True)")

    if job.status == "PROMOTED" or job.release_id is not None:
        raise ValueError(f"Job '{job.job_id}' has already been promoted with release '{job.release_id}'")

    if job.status != "READY":
        raise ValueError(f"Cannot promote job with status '{job.status}'. Only READY jobs may be promoted.")

    if job.latest_candidate is None:
        raise ValueError("No candidate available in repair job")

    current_cand = candidate_store.get_candidate(job.latest_candidate.candidate_id)
    if current_cand is None:
        raise KeyError(f"Candidate '{job.latest_candidate.candidate_id}' not found in CandidateStore")

    current_hash = compute_candidate_hash(current_cand)
    if current_hash != job.latest_content_hash:
        raise ValueError("Validation invalidated: candidate content was mutated after evaluation")

    current_baseline = registry.get_meta(job.skill_name).version
    if current_baseline != job.baseline_version:
        raise ValueError(
            f"Validation invalidated: baseline version changed from "
            f"'{job.baseline_version}' to '{current_baseline}'"
        )

    val_rec = ValidationRecord(
        candidate_id=job.latest_candidate.candidate_id,
        content_hash=job.latest_content_hash,
        baseline_version=job.baseline_version,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS"),
        promoted=False,
    )

    release = promote_candidate(
        candidate=current_cand,
        validation_record=val_rec,
        state_machine=state_machine,
        registry=registry,
        candidate_store=candidate_store,
        caller_confirmed=True,
    )

    job.status = "PROMOTED"
    job.release_id = release.release_id

    conn = state_machine._get_conn()
    _save_job(conn, job)

    return release
