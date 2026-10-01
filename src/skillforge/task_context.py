"""Task Context & Intent Revision Module (P3)

Manages bounded task contracts, goal shift detection, intent revisions,
and candidate supersession. Ensures task contracts freeze during execution
and isolates late-arriving results across SQLite reopen.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .models import CandidateSkill, TaskContext
from .episode import CandidateStore

VAGUE_KEYWORDS = [
    "不好", "不行", "不对", "有问题", "有毛病", "差点意思",
    "重做", "重新来", "再做一次", "改一下", "修改一下", "优化一下", "调整一下",
    "你看着办", "随便", "无所谓", "看着改",
    "bad", "wrong", "fix it", "redo", "not good", "poor",
]

SPECIFIC_ACTION_KEYWORDS = [
    "改为", "换成", "增加", "新增", "删除", "禁止", "导出", "生成", "返回", "转为", "变成"
]

COURTESY_WORDS = [
    "请", "麻烦", "帮我", "请帮我", "劳驾", "一下", "好的", "谢谢", "hello", "hi", "please"
]


def _normalize_text(text: str) -> str:
    """Strip punctuation, courtesy words, and extra whitespace for parity checking."""
    t = text.strip().lower()
    for w in COURTESY_WORDS:
        t = t.replace(w, "")
    # Remove punctuation
    t = re.sub(r"[，。！？、,.!?\s]+", "", t)
    return t


def detect_intent_shift(
    current_ctx: TaskContext,
    new_prompt: str,
    new_constraints: Optional[list[str]] = None,
) -> tuple[str, Optional[str]]:
    """Detect whether a new user instruction constitutes:
    - 'NO_OP': Wording variations, politeness, or identical constraints without semantic intent change.
    - 'CONFIRMATION_REQUIRED': Vague / ambiguous feedback lacking concrete target direction.
    - 'REVISION': Concrete change in user goals, constraints, prohibitions, or delivery format.
    """
    clean_prompt = new_prompt.strip()

    # 1. Check for vague / ambiguous input requiring user confirmation
    norm_check = _normalize_text(clean_prompt)
    has_vague = any(vk in clean_prompt.lower() or vk in norm_check for vk in VAGUE_KEYWORDS)
    has_specific = any(sk in clean_prompt for sk in SPECIFIC_ACTION_KEYWORDS)

    if has_vague and not has_specific:
        return (
            "CONFIRMATION_REQUIRED",
            f"Vague feedback '{clean_prompt}' lacks actionable intent; confirmation required before revising goal.",
        )

    if len(clean_prompt) < 4 and clean_prompt in ("改", "错", "换", "停", "no"):
        return (
            "CONFIRMATION_REQUIRED",
            f"Short ambiguous prompt '{clean_prompt}' requires clarification.",
        )

    # 2. Check for NO_OP: identical or purely cosmetic paraphrase
    norm_orig = _normalize_text(current_ctx.goal)
    norm_new = _normalize_text(clean_prompt)

    curr_constraints = sorted(current_ctx.constraints)
    new_constrs = sorted(new_constraints) if new_constraints is not None else curr_constraints

    if norm_orig == norm_new and curr_constraints == new_constrs:
        return (
            "NO_OP",
            "Prompt is semantically identical or a cosmetic rephrasing of the existing goal; no revision needed.",
        )

    # 3. Definite goal / constraint shift -> REVISION
    reasons = []
    if norm_orig != norm_new:
        reasons.append(f"Goal updated from '{current_ctx.goal}' to '{clean_prompt}'")
    if curr_constraints != new_constrs:
        reasons.append(f"Constraints updated from {curr_constraints} to {new_constrs}")

    return ("REVISION", "; ".join(reasons))


def revise_task_context(
    current_ctx: TaskContext,
    new_goal: str,
    new_constraints: Optional[list[str]] = None,
    new_business_scope: Optional[str] = None,
    new_acceptance_criteria: Optional[dict[str, Any]] = None,
    new_body: Optional[str] = None,
    candidate_store: Optional[CandidateStore] = None,
    new_candidate_id: Optional[str] = None,
    new_assumptions: Optional[list[str]] = None,
) -> tuple[TaskContext, Optional[CandidateSkill]]:
    """Produce a new TaskContext intent revision, superseding the old candidate if present.

    Guarantees:
    - Increments intent_revision (independent from formal skill release version).
    - Preserves old candidate in CandidateStore with status='SUPERSEDED' and superseded_by pointer.
    - Generates new candidate linked via supersedes=old_candidate_id.
    - Re-computes contract fingerprint for the revised intent.
    - Persists updated TaskContext in CandidateStore.
    """
    new_revision = current_ctx.intent_revision + 1
    now_iso = datetime.now(timezone.utc).isoformat()

    effective_constrs = (
        list(new_constraints)
        if new_constraints is not None
        else list(current_ctx.constraints)
    )
    effective_scope = (
        new_business_scope
        if new_business_scope is not None
        else current_ctx.business_scope
    )
    effective_criteria = (
        dict(new_acceptance_criteria)
        if new_acceptance_criteria is not None
        else dict(current_ctx.acceptance_criteria)
    )
    effective_assumptions = (
        list(new_assumptions)
        if new_assumptions is not None
        else list(current_ctx.assumptions)
    )

    superseded_cands = list(current_ctx.superseded_candidate_ids)
    old_cand_id = current_ctx.active_candidate_id
    if old_cand_id and old_cand_id not in superseded_cands:
        superseded_cands.append(old_cand_id)

    new_candidate: Optional[CandidateSkill] = None
    active_cand_id = None
    active_body = None

    if new_body is not None and candidate_store is not None:
        import uuid
        from .models import SkillMeta
        cid = new_candidate_id or f"cand_{uuid.uuid4().hex[:12]}"
        skill_name = current_ctx.active_skill_name or "logistics_task_skill"
        new_candidate = CandidateSkill(
            candidate_id=cid,
            skill_name=skill_name,
            decision="revise",
            source_requirement=new_goal,
            meta=SkillMeta(
                name=skill_name,
                version=f"0.1.{new_revision}-draft",
                description=f"Draft skill revised for intent revision {new_revision}",
                use_when=f"Intent: {new_goal}",
            ),
            body=new_body,
            rationale=f"Revised due to goal shift in intent revision {new_revision}",
            status="DRAFT",
            intent_revision=new_revision,
            supersedes=old_cand_id,
            created_at=now_iso,
            updated_at=now_iso,
        )
        if old_cand_id:
            candidate_store.supersede_candidate(old_cand_id, cid)
        candidate_store.save_candidate(new_candidate)
        active_cand_id = cid
        active_body = new_body

    new_ctx = TaskContext(
        task_id=current_ctx.task_id,
        goal=new_goal,
        business_scope=effective_scope,
        constraints=effective_constrs,
        acceptance_criteria=effective_criteria,
        intent_revision=new_revision,
        active_candidate_id=active_cand_id or current_ctx.active_candidate_id,
        active_skill_name=current_ctx.active_skill_name,
        active_skill_version=current_ctx.active_skill_version,
        active_body_snapshot=active_body or current_ctx.active_body_snapshot,
        superseded_candidate_ids=superseded_cands,
        assumptions=effective_assumptions,
        created_at=current_ctx.created_at or now_iso,
        updated_at=now_iso,
    )
    new_ctx.contract_fingerprint = new_ctx.compute_fingerprint()

    if candidate_store is not None:
        candidate_store.save_task_context(new_ctx)

    return new_ctx, new_candidate
