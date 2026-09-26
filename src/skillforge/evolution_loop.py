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
from dataclasses import dataclass, field
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


@dataclass
class ValidationRecord:
    """Evaluation gate record binding candidate identity, content hash, and ratchet verdict."""

    candidate_id: str
    content_hash: str
    baseline_version: Optional[str]
    ratchet_decision: Literal["PASS", "REVIEW", "DECLINED"]
    eval_result: Optional[EvalResult]
    ratchet_verdict: Optional[RatchetVerdict]
    promoted: bool = False
    release_id: Optional[str] = None
    verification_episode_ids: list[str] = field(default_factory=list)


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
        if purpose == "evaluation":
            raise ValueError(
                f"Source episode '{ep.episode_id}' has purpose='evaluation'; "
                "evaluation/heldout episodes are strictly isolated from candidate mining (A8 boundary)"
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
) -> ValidationRecord:
    """Run real evaluation and ratchet gate on a candidate skill in sandbox isolation.

    Validates candidate against baseline (if revise) or cold-start (if create).
    Does NOT modify active registry or disk.
    """
    content_hash = compute_candidate_hash(candidate)
    baseline_version: Optional[str] = None
    if candidate.decision == "revise":
        baseline_version = registry.get_meta(candidate.skill_name).version

    # Evaluate baseline if revise and not already provided
    if candidate.decision == "revise" and baseline_eval_result is None:
        baseline_eval_result = evaluator.evaluate_skill(
            candidate.skill_name,
            cases=eval_cases,
        )

    # Mount candidate into isolated sandbox registry
    sandbox_reg = SandboxSkillRegistry(registry, candidate)
    sandbox_evaluator = SkillEvaluator(
        registry=sandbox_reg,
        llm=evaluator.llm,
        judge_llm=getattr(evaluator, "judge_llm", None) or getattr(getattr(evaluator, "judge", None), "llm", None),
        output_cache=evaluator.output_cache,
        ledger=getattr(evaluator, "ledger", None),
    )

    candidate_eval_result = sandbox_evaluator.evaluate_skill(
        candidate.skill_name,
        cases=eval_cases,
    )

    if not candidate_eval_result.valid:
        ratchet_verdict = RatchetVerdict(
            decision="DECLINED",
            reasons=candidate_eval_result.invalid_reasons or ["Candidate evaluation is marked invalid"],
        )
    else:
        # check_ratchet handles old=None as PASS (for cold-start create)
        ratchet_verdict = check_ratchet(baseline_eval_result, candidate_eval_result)

    return ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=content_hash,
        baseline_version=baseline_version,
        ratchet_decision=ratchet_verdict.decision,
        eval_result=candidate_eval_result,
        ratchet_verdict=ratchet_verdict,
        promoted=False,
    )


def promote_candidate(
    candidate: CandidateSkill,
    validation_record: ValidationRecord,
    state_machine: ReleaseStateMachine,
    registry: SkillRegistry,
    candidate_store: CandidateStore,
    caller_confirmed: bool = False,
) -> Release:
    """Promote a validated candidate via existing ReleaseStateMachine.

    Invariants:
    1. Candidate content hash must match validation_record.content_hash.
    2. Baseline version must match validation_record.baseline_version (if revise).
    3. Ratchet decision must be PASS. (REVIEW or DECLINED is strictly rejected).
    4. Explicit caller confirmation (caller_confirmed=True) is mandatory.
    5. Duplicate promotion of the same candidate is rejected.
    """
    if candidate.candidate_id != validation_record.candidate_id:
        raise ValueError(
            f"Candidate ID mismatch: {candidate.candidate_id} vs {validation_record.candidate_id}"
        )

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

    # Gate on explicit caller confirmation
    if not caller_confirmed:
        raise ValueError(
            "Promotion blocked: requires explicit caller confirmation (caller_confirmed=True)"
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
    full_content = f"---\n{frontmatter}---\n\n{candidate.body.strip()}\n"
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

    # Update candidate status in CandidateStore
    candidate_store.update_status(candidate.candidate_id, "APPROVED")
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
