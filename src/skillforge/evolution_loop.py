"""Evolution Loop: Mining, Consolidation, Validation, and Controlled Promotion (Milestone 2)

Key invariants:
1. Controlled Mining from persisted Episodes only; unverified/unknown-only inputs are abandoned.
2. Source episode references are bound by the application, never forged by LLM.
3. Policy, Tool, and Evaluator remain strictly immutable.
4. Independent test inputs / heldout sentinels are NEVER passed into miner prompts.
5. Candidates remain isolated until passing real evaluation gates and explicit confirmation.
6. Only PASS verdict can be promoted with caller_confirmed=True; REVIEW/DECLINED are never promoted.
7. Promoted skills use existing ReleaseStateMachine and become visible via SkillRegistry.
"""
from __future__ import annotations

import hashlib
import json
import re
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
    Patch,
    Release,
    EvalResult,
    RatchetVerdict,
    ValidationRecord,
)
from .episode import EpisodeStore, CandidateStore
from .registry import SkillRegistry
from .evaluator import SkillEvaluator
from .evaluator.ratchet import check_ratchet
from .state_machine import ReleaseStateMachine


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class MiningResult:
    """Outcome of a mining & consolidation run."""

    decision: Literal["create", "revise", "abandon"]
    candidate: Optional[CandidateSkill] = None
    abandon_reason: Optional[str] = None
    miner_prompt_used: str = ""


def compute_candidate_hash(candidate: CandidateSkill) -> str:
    """Compute canonical hash of candidate metadata and body to detect any post-validation mutations."""
    meta_dict = (
        candidate.meta.model_dump()
        if hasattr(candidate.meta, "model_dump")
        else candidate.meta.dict()
    )
    raw = json.dumps(meta_dict, sort_keys=True, ensure_ascii=False) + "\n---\n" + candidate.body.strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def compute_cases_hash(cases: list[Any]) -> str:
    """Compute deterministic content hash of evaluation cases."""
    if not cases:
        return "empty_cases"
    serialized = []
    for c in cases:
        if hasattr(c, "to_dict"):
            serialized.append(c.to_dict())
        elif hasattr(c, "__dict__"):
            try:
                serialized.append(asdict(c))
            except Exception:
                serialized.append(str(c))
        elif isinstance(c, dict):
            serialized.append(c)
        else:
            serialized.append(str(c))
    return hashlib.sha256(json.dumps(serialized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]



def mine_candidate(
    episodes: list[Episode],
    target_skill_name: str,
    llm: Any,
    candidate_store: CandidateStore,
    registry: Optional[SkillRegistry] = None,
    decision_override: Optional[Literal["create", "revise", "abandon"]] = None,
) -> MiningResult:
    """Mine and consolidate candidate skill from a list of persisted episodes.

    Validates:
    - episodes cannot be empty.
    - All episodes must exist in candidate_store's episode_store.
    - Strict purpose isolation (V1): Only purpose='learning' accepted; rejects evaluation, heldout, unknown.
    - Anti-tampering check (V2): In-memory episode content must strictly match canonical EpisodeStore record.
    - If all episodes are 'unknown' or lack valid evidence -> returns abandon without candidate.
    - Source episode IDs are attached strictly by application logic (not from LLM).
    - If revising, target skill must exist in registry; old skill remains untouched.
    """
    if not episodes:
        raise ValueError("Cannot mine candidate from empty episode list")

    # Verify all episodes exist in store ("空/不存在来源拒绝") and respect A8 purpose isolation
    for ep in episodes:
        if not candidate_store._episode_store.has_episode(ep.episode_id):
            raise KeyError(
                f"Source episode '{ep.episode_id}' not found in EpisodeStore"
            )
        purpose = ep.environment.get("purpose", "") if isinstance(ep.environment, dict) else ""
        if purpose != "learning":
            raise ValueError(
                f"Source episode '{ep.episode_id}' has purpose='{purpose or 'unknown'}'; "
                "only 'learning' episodes are accepted for candidate mining (A8 boundary: strictly isolated from candidate mining)"
            )
        # Anti-tampering check: verify in-memory content matches canonical record
        canonical = candidate_store._episode_store.get_episode(ep.episode_id)
        if canonical is None:
            raise KeyError(
                f"Source episode '{ep.episode_id}' not found in EpisodeStore"
            )
        if (
            canonical.task_id != ep.task_id
            or canonical.run_id != ep.run_id
            or canonical.skill_name != ep.skill_name
            or canonical.skill_version != ep.skill_version
            or canonical.outcome != ep.outcome
            or canonical.outcome_reason != ep.outcome_reason
            or canonical.environment != ep.environment
            or canonical.provenances != ep.provenances
            or canonical.acceptance_criteria != ep.acceptance_criteria
            or canonical.verification_evidence != ep.verification_evidence
        ):
            raise ValueError(
                f"Episode '{ep.episode_id}' content does not match canonical EpisodeStore record "
                "(tampered in-memory episode rejected)"
            )

    # Check evidence sufficiency: if ONLY unknown or missing valid evidence -> abandon
    has_valid_evidence = False
    for ep in episodes:
        if ep.outcome in ("success", "failure"):
            if ep.provenances or ep.verification_evidence:
                has_valid_evidence = True
                break

    if not has_valid_evidence or decision_override == "abandon":
        return MiningResult(
            decision="abandon",
            candidate=None,
            abandon_reason="All source episodes are unknown or lack valid execution evidence",
            miner_prompt_used="",
        )

    # Determine decision
    is_existing = registry is not None and target_skill_name in registry.list_names()
    decision: Literal["create", "revise", "abandon"]

    if decision_override is not None:
        decision = decision_override
    else:
        decision = "revise" if is_existing else "create"

    if decision == "revise" and not is_existing:
        raise ValueError(
            f"Cannot revise non-existent skill '{target_skill_name}'"
        )

    # Build prompt summarizing ONLY source episode observations
    # CRITICAL: Independent evaluation sets / heldout cases are NEVER included here!
    lines = [
        f"You are a Skill Miner. Target skill: '{target_skill_name}'. Decision: '{decision}'.",
        "Synthesize instructions and metadata based strictly on the following observed episodes:",
    ]
    for ep in episodes:
        lines.append(f"\n--- Episode {ep.episode_id} ---")
        lines.append(f"Outcome: {ep.outcome} (Reason: {ep.outcome_reason})")
        lines.append(f"Environment: {json.dumps(ep.environment)}")
        for prov in ep.provenances:
            lines.append(
                f"Tool '{prov.tool_name}': status={prov.output_status}, output={prov.output_summary[:100]}"
            )

    lines.append("\nOutput a complete SKILL.md format with YAML frontmatter (---) and markdown body.")
    miner_prompt = "\n".join(lines)

    # Invoke LLM
    response = llm.invoke(miner_prompt)
    raw_content = getattr(response, "content", str(response))
    cleaned_content = _strip_code_fence(raw_content)

    m = _FRONTMATTER_RE.match(cleaned_content)
    if not m:
        raise ValueError(
            f"LLM output for '{target_skill_name}' lacks valid YAML frontmatter (--- ... ---)"
        )

    frontmatter_text, body = m.group(1), m.group(2)
    try:
        data = yaml.safe_load(frontmatter_text) or {}
        meta = SkillMeta(**data)
    except Exception as e:
        raise ValueError(f"Illegal skill structure generated: {e}")

    # Security check: ensure target_skill_name matches meta.name
    if meta.name != target_skill_name:
        raise ValueError(
            f"Model returned mismatched skill name '{meta.name}' for target '{target_skill_name}'"
        )

    # Application strictly fixes source_episode_ids; model cannot spoof them
    candidate_id = f"cand_{uuid.uuid4().hex[:12]}"
    candidate = CandidateSkill(
        candidate_id=candidate_id,
        skill_name=target_skill_name,
        decision=decision,
        source_episode_ids=[ep.episode_id for ep in episodes],
        meta=meta,
        body=body.strip(),
        rationale=f"Mined from {len(episodes)} episodes: {', '.join(e.episode_id for e in episodes)}",
        status="DRAFT",
    )

    candidate_store.save_candidate(candidate)
    return MiningResult(
        decision=decision,
        candidate=candidate,
        miner_prompt_used=miner_prompt,
    )


class SandboxSkillRegistry(SkillRegistry):
    """Temporary isolated registry that mounts a CandidateSkill without modifying active registry."""

    def __init__(self, base_registry: SkillRegistry, candidate: CandidateSkill):
        super().__init__(
            db_path=base_registry.db_path,
            skills_dir=base_registry.skills_dir,
            repo_root=base_registry.repo_root,
            router_log=base_registry.router_log,
        )
        self._metas = dict(base_registry._metas)
        self._bodies = dict(base_registry._bodies)
        # Mount candidate strictly in sandbox memory
        self._metas[candidate.skill_name] = candidate.meta
        self._bodies[candidate.skill_name] = candidate.body


def validate_candidate(
    candidate: CandidateSkill,
    evaluator: SkillEvaluator,
    registry: SkillRegistry,
    eval_cases: list[dict],
    baseline_eval_result: Optional[EvalResult] = None,
    candidate_store: Optional[CandidateStore] = None,
    tool_broker: Optional[Any] = None,
    scope_hash: Optional[str] = None,
    config_hash: Optional[str] = None,
    dataset_version: Optional[str] = None,
    enable_shadow_recovery: bool = False,
    recovery_budget: Optional[Any] = None,
    recovery_llm: Optional[Any] = None,
) -> ValidationRecord:
    """Run real evaluation and ratchet gate on a candidate skill in sandbox isolation.

    Validates candidate against baseline (if revise) or cold-start (if create).
    Runs cheap checks (tool permissions, prompt bloat) before running expensive LLM evaluations.
    Does NOT modify active registry or disk.
    """
    content_hash = compute_candidate_hash(candidate)
    baseline_version: Optional[str] = None
    if candidate.decision == "revise":
        try:
            baseline_version = registry.get_meta(candidate.skill_name).version
        except Exception:
            baseline_version = None

    effective_scope_hash = getattr(candidate, "task_spec_hash", None) or scope_hash
    if candidate_store is not None and effective_scope_hash:
        task_ctx = candidate_store.get_task_context_by_fingerprint(effective_scope_hash)
        if task_ctx is not None:
            recomputed = task_ctx.compute_fingerprint()
            if recomputed != effective_scope_hash:
                raise ValueError(
                    f"Validation rejected: TaskContext contract drift detected "
                    f"(stored '{effective_scope_hash}' != computed '{recomputed}')"
                )

    effective_config_hash = (
        getattr(evaluator, "config_hash", None)
        or (evaluator.get_config_hash() if hasattr(evaluator, "get_config_hash") else None)
        or config_hash
    )
    # Canonical cases content hash takes precedence over caller-provided arbitrary string
    canonical_cases_hash = compute_cases_hash(eval_cases) if eval_cases else None
    effective_dataset_version = canonical_cases_hash or getattr(eval_cases, "dataset_version", None) or dataset_version

    # Cheap Check 1: Tool permissions / allowlist check (V5)
    if tool_broker is not None and hasattr(tool_broker, "application_allowlist"):
        deps = getattr(candidate.meta, "dependencies", []) or []
        for dep in deps:
            if dep not in tool_broker.application_allowlist:
                ratchet_verdict = RatchetVerdict(
                    decision="DECLINED",
                    reasons=[f"TOOL_DEPENDENCY_ERROR: Tool '{dep}' is not in application allowlist"],
                )
                rec = ValidationRecord(
                    candidate_id=candidate.candidate_id,
                    content_hash=content_hash,
                    baseline_version=baseline_version,
                    ratchet_decision="DECLINED",
                    eval_result=None,
                    ratchet_verdict=ratchet_verdict,
                    promoted=False,
                    scope_hash=effective_scope_hash,
                    config_hash=effective_config_hash,
                    dataset_version=effective_dataset_version,
                )
                if candidate_store is not None:
                    candidate_store.save_validation_record(rec)
                return rec

    # Cheap Check 2: Prompt Bloat check (V3)
    from .evaluator.prompt_bloat import check_prompt_bloat
    if candidate.decision == "revise":
        old_body = ""
        try:
            old_body = registry.get_body(candidate.skill_name)
        except Exception:
            old_body = ""
        bloat_res = check_prompt_bloat(old_body, candidate.body)
    else:
        bloat_res = check_prompt_bloat("", candidate.body, cold_start=True)

    if not bloat_res.passed:
        if enable_shadow_recovery and candidate_store is not None:
            # Production bounded shadow recovery path for prompt bloat (L1)
            from .bounded_recovery import recover_bloated_candidate
            recovery_res = recover_bloated_candidate(
                candidate=candidate,
                registry=registry,
                evaluator=evaluator,
                eval_cases=eval_cases,
                candidate_store=candidate_store,
                enable_shadow_recovery=True,
                llm=recovery_llm,
                budget=recovery_budget,
                tool_broker=tool_broker,
                scope_hash=effective_scope_hash,
            )
            if recovery_res.validation_record is not None:
                return recovery_res.validation_record

        ratchet_verdict = bloat_res.to_ratchet_verdict()
        rec = ValidationRecord(
            candidate_id=candidate.candidate_id,
            content_hash=content_hash,
            baseline_version=baseline_version,
            ratchet_decision=bloat_res.decision,
            eval_result=None,
            ratchet_verdict=ratchet_verdict,
            promoted=False,
            scope_hash=effective_scope_hash,
            config_hash=effective_config_hash,
            dataset_version=effective_dataset_version,
        )
        if candidate_store is not None:
            candidate_store.save_validation_record(rec)
        return rec

    # Evaluate baseline if revise and not already provided
    if candidate.decision == "revise" and baseline_eval_result is None:
        try:
            baseline_eval_result = evaluator.evaluate_skill(
                candidate.skill_name,
                cases=eval_cases,
            )
        except Exception:
            baseline_eval_result = None

    orig_registry = getattr(evaluator, "registry", None)
    try:
        # Mount candidate into isolated sandbox registry
        sandbox_reg = SandboxSkillRegistry(registry, candidate)
        if isinstance(evaluator, SkillEvaluator):
            sandbox_evaluator = SkillEvaluator(
                registry=sandbox_reg,
                llm=evaluator.llm,
                judge_llm=getattr(evaluator, "judge_llm", None) or getattr(getattr(evaluator, "judge", None), "llm", None),
                output_cache=evaluator.output_cache,
                ledger=getattr(evaluator, "ledger", None),
                scoring_policy=getattr(evaluator, "scoring_policy", "criteria_v1"),
                rubric=getattr(evaluator, "rubric", None),
            )
            candidate_eval_result = sandbox_evaluator.evaluate_skill(
                candidate.skill_name,
                cases=eval_cases,
            )
        else:
            # Custom evaluator double / fixture evaluator
            if hasattr(evaluator, "registry"):
                evaluator.registry = sandbox_reg
            candidate_eval_result = evaluator.evaluate_skill(
                candidate.skill_name,
                cases=eval_cases,
            )
    finally:
        if orig_registry is not None and hasattr(evaluator, "registry"):
            evaluator.registry = orig_registry

    if not candidate_eval_result.valid:
        ratchet_verdict = RatchetVerdict(
            decision="DECLINED",
            reasons=candidate_eval_result.invalid_reasons or ["Candidate evaluation is marked invalid"],
        )
    elif candidate.decision == "revise" and (baseline_eval_result is None or not baseline_eval_result.valid):
        # Fail-closed: revision requires an actual valid baseline evaluation under identical evaluation scope/dataset
        invalid_reasons = (
            baseline_eval_result.invalid_reasons
            if baseline_eval_result is not None
            else ["MISSING_BASELINE_EVAL: Revision candidate requires actual baseline evaluation under same scope/dataset; unverified baseline cannot pass gate"]
        )
        ratchet_verdict = RatchetVerdict(
            decision="DECLINED",
            reasons=invalid_reasons,
        )
    else:
        # check_ratchet handles old=None as PASS (for cold-start create)
        ratchet_verdict = check_ratchet(baseline_eval_result, candidate_eval_result)

    rec = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=content_hash,
        baseline_version=baseline_version,
        ratchet_decision=ratchet_verdict.decision,
        eval_result=candidate_eval_result,
        ratchet_verdict=ratchet_verdict,
        promoted=False,
        scope_hash=effective_scope_hash,
        config_hash=effective_config_hash,
        dataset_version=effective_dataset_version,
    )
    if candidate_store is not None:
        candidate_store.save_validation_record(rec)
    return rec


def promote_candidate(
    candidate: CandidateSkill,
    validation_record: ValidationRecord,
    state_machine: ReleaseStateMachine,
    registry: SkillRegistry,
    candidate_store: CandidateStore,
    caller_confirmed: bool = False,
    expected_config_hash: Optional[str] = None,
    expected_dataset_version: Optional[str] = None,
    expected_scope_hash: Optional[str] = None,
) -> Release:
    """Promote a validated candidate via existing ReleaseStateMachine.

    Invariants:
    1. Candidate content hash must match validation_record.content_hash.
    2. Baseline version must match validation_record.baseline_version (if revise).
    3. Ratchet decision must be PASS. (REVIEW or DECLINED is strictly rejected).
    4. Explicit caller confirmation (caller_confirmed=True) is mandatory.
    5. Duplicate promotion of the same candidate is rejected.
    6. Scope hash, config hash, and dataset version must match when specified.
    """
    if candidate.candidate_id != validation_record.candidate_id:
        raise ValueError(
            f"Candidate ID mismatch: {candidate.candidate_id} vs {validation_record.candidate_id}"
        )

    # Fail-closed check on incomplete or corrupted records
    if not validation_record.content_hash or not validation_record.ratchet_decision:
        raise ValueError("Validation record is incomplete or corrupted (fail-closed)")

    # Invalidate if candidate content mutated
    current_hash = compute_candidate_hash(candidate)
    if current_hash != validation_record.content_hash:
        raise ValueError(
            "Validation invalidated: candidate content was mutated after evaluation"
        )

    # Invalidate if baseline version changed
    if candidate.decision == "revise":
        current_baseline = registry.get_meta(candidate.skill_name).version
        if current_baseline != validation_record.baseline_version:
            raise ValueError(
                f"Validation invalidated: baseline version changed from "
                f"'{validation_record.baseline_version}' to '{current_baseline}'"
            )

    # Reject duplicate promotion
    if validation_record.promoted:
        raise ValueError(
            f"Candidate '{candidate.candidate_id}' has already been promoted"
        )

    # Gate on ratchet verdict: ONLY PASS is allowed!
    if validation_record.ratchet_decision != "PASS":
        raise ValueError(
            f"Cannot promote candidate with ratchet decision '{validation_record.ratchet_decision}'. "
            "Only PASS verdict may be promoted."
        )

    # J6 Hard Invariant: critical_fail=True can NEVER be promoted even if ratchet verdict was PASS
    if validation_record.eval_result and getattr(validation_record.eval_result, "critical_fail", False):
        raise ValueError(
            f"Promotion rejected: validation record has critical_fail=True in eval_result "
            f"({getattr(validation_record.eval_result, 'critical_reasons', [])})"
        )

    # Authoritative CandidateStore verification (V4 & P1 G6)
    stored_record = candidate_store.get_validation_record(candidate.candidate_id)
    if stored_record is None:
        raise ValueError(
            f"Promotion rejected: candidate '{candidate.candidate_id}' has no persisted validation record in CandidateStore (forged in-memory record rejected)"
        )

    # Fail-closed check on incomplete or corrupted records
    if not stored_record.content_hash or not stored_record.ratchet_decision:
        raise ValueError("Validation record is incomplete or corrupted (fail-closed)")

    if stored_record.ratchet_decision != "PASS":
        raise ValueError(
            f"Cannot promote candidate with ratchet decision '{stored_record.ratchet_decision}'. "
            "Only PASS verdict may be promoted."
        )

    # J6 Hard Invariant: stored critical_fail=True can NEVER be promoted (prevents forged Store pass)
    if stored_record.eval_result and getattr(stored_record.eval_result, "critical_fail", False):
        raise ValueError(
            f"Promotion rejected: stored record has critical_fail=True in eval_result "
            f"({getattr(stored_record.eval_result, 'critical_reasons', [])})"
        )
    if current_hash != stored_record.content_hash:
        raise ValueError(
            "Validation invalidated: candidate content was mutated after stored evaluation"
        )
    if candidate.decision == "revise":
        if not stored_record.baseline_version:
            raise ValueError(
                f"Promotion rejected: revision candidate '{candidate.candidate_id}' has no baseline_version in validation record"
            )
        current_baseline = registry.get_meta(candidate.skill_name).version
        if current_baseline != stored_record.baseline_version:
            raise ValueError(
                f"Validation invalidated: baseline version changed from "
                f"'{stored_record.baseline_version}' to '{current_baseline}'"
            )
    if stored_record.scope_hash and getattr(candidate, "task_spec_hash", None):
        if stored_record.scope_hash != candidate.task_spec_hash:
            raise ValueError(
                f"Validation invalidated: task scope hash changed from "
                f"'{stored_record.scope_hash}' to '{candidate.task_spec_hash}'"
            )
    if stored_record.scope_hash and candidate_store is not None:
        conn = candidate_store._get_conn()
        try:
            has_contexts = conn.execute("SELECT 1 FROM task_contexts LIMIT 1").fetchone()
        except Exception:
            has_contexts = None
        if has_contexts:
            task_ctx = candidate_store.get_task_context_by_fingerprint(stored_record.scope_hash)
            if task_ctx is None:
                raise ValueError(
                    f"Validation invalidated: Unknown or unauthorized scope hash '{stored_record.scope_hash}' "
                    f"(not found in canonical TaskContext store)"
                )
            recomputed = task_ctx.compute_fingerprint()
            if recomputed != stored_record.scope_hash:
                raise ValueError(
                    f"Validation invalidated: TaskContext contract drift detected for scope '{stored_record.scope_hash}' "
                    f"(computed '{recomputed}')"
                )
            if task_ctx.active_body_snapshot and candidate.body and task_ctx.active_body_snapshot != candidate.body:
                raise ValueError(
                    f"Validation invalidated: TaskContext active_body_snapshot does not match candidate body "
                    f"(tampered candidate body for scope '{stored_record.scope_hash}')"
                )
    if expected_scope_hash is not None and stored_record.scope_hash != expected_scope_hash:
        raise ValueError(
            f"Validation invalidated: task scope hash changed from '{stored_record.scope_hash}' to '{expected_scope_hash}'"
        )
    if expected_config_hash is not None and stored_record.config_hash != expected_config_hash:
        raise ValueError(
            f"Validation invalidated: validator config hash changed from '{stored_record.config_hash}' to '{expected_config_hash}'"
        )
    if expected_dataset_version is not None and stored_record.dataset_version != expected_dataset_version:
        raise ValueError(
            f"Validation invalidated: evaluation dataset version changed from '{stored_record.dataset_version}' to '{expected_dataset_version}'"
        )
    if stored_record.promoted:
        raise ValueError(
            f"Candidate '{candidate.candidate_id}' has already been promoted"
        )
    # Gate on explicit caller confirmation
    if not caller_confirmed:
        raise ValueError(
            "Promotion blocked: requires explicit caller confirmation (caller_confirmed=True)"
        )

    if stored_record.ratchet_decision == "PASS":
        if (
            stored_record.eval_result is None
            and not stored_record.verification_episode_ids
            and (not stored_record.ratchet_verdict or not stored_record.ratchet_verdict.reasons)
        ):
            raise ValueError(
                f"Promotion rejected: candidate '{candidate.candidate_id}' has no evaluation result in validation record (fabricated PASS without evaluation rejected)"
            )

    # Write SKILL.md to skills directory in test repo
    skill_dir = state_machine.repo_root / "skills" / candidate.skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"

    meta_dict = (
        candidate.meta.model_dump()
        if hasattr(candidate.meta, "model_dump")
        else candidate.meta.dict()
    )
    frontmatter = yaml.safe_dump(meta_dict, sort_keys=False, allow_unicode=True)
    body_content = candidate.body.strip()
    m_body = _FRONTMATTER_RE.match(body_content)
    if m_body:
        body_content = m_body.group(2).strip()
    full_content = f"---\n{frontmatter}---\n\n{body_content}\n"
    skill_md.write_text(full_content, encoding="utf-8")

    level = "L1" if candidate.decision == "revise" else "L2"
    patch = Patch(
        skill_name=candidate.skill_name,
        level=level,
        diff=full_content,
        rationale=candidate.rationale,
    )

    release_id = state_machine.begin_release(
        skill_name=candidate.skill_name,
        version=candidate.meta.version,
        level=level,
    )
    state_machine.write_commit(release_id, patch)

    if validation_record.eval_result is not None:
        state_machine.append_evaluation(release_id, validation_record.eval_result)

    lineage_data = {
        "candidate_id": candidate.candidate_id,
        "source_episode_ids": candidate.source_episode_ids,
        "source_doc_id": candidate.source_doc_id,
        "source_doc_version": candidate.source_doc_version,
        "source_snippet_ids": candidate.source_snippet_ids,
        "verification_episode_ids": getattr(validation_record, "verification_episode_ids", []),
    }
    conn = state_machine._get_conn()
    conn.execute(
        "UPDATE releases SET source_lineage_json = ? WHERE release_id = ?",
        (json.dumps(lineage_data), release_id),
    )
    conn.commit()

    state_machine.commit_release(release_id)

    # Reload active registry so new version becomes active
    registry._metas.clear()
    registry._bodies.clear()
    registry.load_skills_from_dir()

    # Mark promoted in CandidateStore
    candidate_store.mark_promoted(candidate.candidate_id, release_id)
    validation_record.promoted = True
    validation_record.release_id = release_id

    rel_dict = state_machine.get_release(release_id)
    return Release(
        release_id=rel_dict["release_id"],
        skill_name=rel_dict["skill_name"],
        version=rel_dict["version"],
        commit_hash=rel_dict["commit_hash"],
        status=rel_dict["status"],
        level=rel_dict["level"],
    )
