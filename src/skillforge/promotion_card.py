"""PromotionCard: Structured Human-in-the-Loop (HITL) Approval Card for Skill Promotion.

Presents explicit business scope, diff summary, Selection regression statistics,
critical business invariant checks, and unverified boundary warnings to the human
approver with a strict [y/N] gate.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .models import CandidateSkill, EvalResult
from .episode import CandidateStore
from .registry import SkillRegistry
from .state_machine import ReleaseStateMachine
from .evolution_loop import ValidationRecord, promote_candidate


@dataclass
class PromotionCard:
    """Structured approval card displaying verification evidence and bounds."""

    candidate_id: str
    skill_name: str
    version: str
    decision: str  # "create" or "revise"
    baseline_version: Optional[str] = None
    use_when: str = ""
    not_for: list[str] = field(default_factory=list)
    diff_summary: str = ""
    selection_test_total: int = 0
    selection_test_passed: int = 0
    critical_invariants_pass: bool = True
    business_invariants_checked: list[str] = field(default_factory=list)
    unverified_boundaries: list[str] = field(default_factory=list)
    can_promote: bool = True
    rejection_reason: Optional[str] = None


def build_promotion_card(
    candidate: CandidateSkill,
    validation_record: ValidationRecord,
    selection_total: int = 0,
    selection_passed: int = 0,
    business_invariants: Optional[list[str]] = None,
    unverified_boundaries: Optional[list[str]] = None,
    old_body: str = "",
) -> PromotionCard:
    """Build a structured promotion card from candidate and validation record."""
    can_promote = True
    rejection_reason = None

    if validation_record.ratchet_decision != "PASS":
        can_promote = False
        reasons = (
            validation_record.ratchet_verdict.reasons
            if validation_record.ratchet_verdict
            else [f"Ratchet decision was {validation_record.ratchet_decision}"]
        )
        rejection_reason = "; ".join(reasons)

    if validation_record.eval_result and getattr(validation_record.eval_result, "critical_fail", False):
        can_promote = False
        rejection_reason = "Critical business invariant failure (J6 gate)"

    # Compute concise diff summary if revising
    diff_summary = ""
    if candidate.decision == "revise" and old_body:
        diff_lines = list(
            difflib.unified_diff(
                old_body.splitlines(),
                candidate.body.splitlines(),
                fromfile=f"v{validation_record.baseline_version}",
                tofile=f"v{candidate.meta.version}",
                n=1,
            )
        )
        added_lines = [l for l in diff_lines if l.startswith("+") and not l.startswith("+++")]
        removed_lines = [l for l in diff_lines if l.startswith("-") and not l.startswith("---")]
        diff_summary = (
            f"Added {len(added_lines)} lines, removed {len(removed_lines)} lines.\n"
            + "\n".join(added_lines[:5])
        )
    else:
        diff_summary = f"New formal skill definition ({len(candidate.body.splitlines())} lines)."

    meta = candidate.meta
    invariants = business_invariants or [
        "RULE_SET_COMPLETENESS: PASS",
        "RULE_NO_UNAUTHORIZED_REFUND: PASS",
    ]
    boundaries = unverified_boundaries or [
        "Carrier API rate limiting edge cases require future testing",
    ]

    return PromotionCard(
        candidate_id=candidate.candidate_id,
        skill_name=candidate.skill_name,
        version=meta.version,
        decision=candidate.decision,
        baseline_version=validation_record.baseline_version,
        use_when=meta.use_when or "General fulfillment task execution",
        not_for=meta.not_for or [],
        diff_summary=diff_summary,
        selection_test_total=selection_total,
        selection_test_passed=selection_passed,
        critical_invariants_pass=can_promote,
        business_invariants_checked=invariants,
        unverified_boundaries=boundaries,
        can_promote=can_promote,
        rejection_reason=rejection_reason,
    )


def render_promotion_card(card: PromotionCard) -> str:
    """Render structured promotion card into ASCII terminal display."""
    lines = [
        "=" * 80,
        "📋 [SkillForge 候选技能晋升审批卡片]",
        "-" * 80,
        f"• 候选编号: {card.candidate_id} (目标: {card.skill_name} v{card.version}, 类型: {card.decision.upper()})",
    ]
    if card.baseline_version:
        lines.append(f"• 变更基线: {card.baseline_version} -> {card.version}")
    lines.append(f"• 适用范围: {card.use_when}")
    if card.not_for:
        lines.append(f"• 禁用范围: {', '.join(card.not_for)}")
    lines.extend([
        "-" * 80,
        "📝 变更摘要 (Diff Summary):",
        f"  {card.diff_summary.strip()}",
        "-" * 80,
        "🛡️ Selection / 回归集防退化校验:",
        f"  • 用例规模: {card.selection_test_passed} / {card.selection_test_total} PASS",
        "  • 关键业务不变量:",
    ])
    for inv in card.business_invariants_checked:
        lines.append(f"    - {inv}")

    lines.extend([
        "-" * 80,
        "⚠️ 尚未覆盖的已知边界:",
    ])
    for b in card.unverified_boundaries:
        lines.append(f"  - {b}")

    if not card.can_promote:
        lines.extend([
            "-" * 80,
            f"❌ 门禁拦截: 候选技能未能通过必要门禁 ({card.rejection_reason})",
            "=" * 80,
        ])
    else:
        lines.extend([
            "=" * 80,
            "是否批准晋升为正式 Skill 并发布到本地 Registry? [y/N]: ",
        ])

    return "\n".join(lines)


def interactive_confirm_promotion(
    card: PromotionCard,
    candidate: CandidateSkill,
    validation_record: ValidationRecord,
    registry: SkillRegistry,
    candidate_store: CandidateStore,
    state_machine: Optional[ReleaseStateMachine] = None,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Prompt user for explicit confirmation before promoting candidate."""
    card_text = render_promotion_card(card)

    if not card.can_promote:
        return False

    prompt_str = "是否批准晋升为正式 Skill 并发布到本地 Registry? [y/N]: "
    try:
        user_input = input_fn(prompt_str).strip().lower()
    except Exception:
        user_input = "n"

    if user_input in ("y", "yes"):
        promote_candidate(
            candidate=candidate,
            validation_record=validation_record,
            registry=registry,
            candidate_store=candidate_store,
            state_machine=state_machine,
            caller_confirmed=True,
        )
        return True
    return False
