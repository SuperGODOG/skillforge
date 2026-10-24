"""Tests for M3: Governed Skill Evolution, Working Memory Indexing, and Structured Promotion Cards."""
import json
import tempfile
from pathlib import Path
import pytest

from skillforge import (
    CandidateSkill,
    CandidateStore,
    Episode,
    EpisodeStore,
    ReleaseStateMachine,
    SkillMeta,
    SkillRegistry,
    ToolCallProvenance,
    Trigger,
    compute_candidate_hash,
)
from skillforge.context_engine.schemas import (
    FailedAttempt,
    VerifiedFact,
    WorkSummary,
)
from skillforge.evolution_loop import (
    MiningResult,
    ValidationRecord,
    mine_candidate,
    promote_candidate,
)
from skillforge.models import EvalResult, RatchetVerdict
from skillforge.promotion_card import (
    PromotionCard,
    build_promotion_card,
    render_promotion_card,
    interactive_confirm_promotion,
)


class FakeLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts_received: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts_received.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        return ""


def _create_sample_episode(episode_id: str, outcome: str = "success") -> Episode:
    prov = ToolCallProvenance(
        tool_name="query_order_packages",
        fixture_case_id="c_1",
        call_index=0,
        call_count=1,
        is_fixture=False,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"order_id": "ORD_01"},
        output_status="SUCCESS",
        output_summary="Found 3 packages: PKG_1, PKG_2, PKG_3",
        latency_ms=12.0,
        timestamp="2026-10-23T14:00:00Z",
        signature="sig_123",
        snapshot_id="snap_123",
        snapshot_content="{}",
    )
    return Episode(
        episode_id=episode_id,
        task_id=f"task_{episode_id}",
        run_id=f"run_{episode_id}",
        skill_name="logistics_checker",
        skill_version="1.0.0",
        outcome=outcome,
        outcome_reason="Fulfillment verified" if outcome == "success" else "Missing package coverage",
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={},
        verification_evidence={"pass": outcome == "success"},
    )


def test_miner_with_working_memory_and_sanitized_feedback(tmp_path: Path):
    db_path = tmp_path / "sf.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)

    ep1 = _create_sample_episode("ep_learn_01", outcome="success")
    ep2 = _create_sample_episode("ep_learn_02", outcome="failure")
    ep_store.save_episode(ep1)
    ep_store.save_episode(ep2)

    # Prepare working memory summary from M2
    work_summary = WorkSummary(
        verified_facts=[
            VerifiedFact(fact="All 3 packages must be checked individually", evidence_ref="tms_01", timestamp="14:00"),
        ],
        failed_attempts=[
            FailedAttempt(action="Declare delivered after checking 2 packages", reason="Package 3 was still in transit", resolution="Check set completeness"),
        ],
        open_questions=["How to handle 504 gateway timeout"],
    )

    # Validation feedback (e.g. from Oracle)
    validation_feedback = {
        "rule_id": "RULE_SET_COMPLETENESS",
        "reason": "Declared full delivery but only 2 of 3 packages were scanned. [HIDDEN_TEST_CASE_IGNORED]",
    }

    mock_skill_md = """---
name: logistics_checker
version: 1.1.0
description: Robust multi-package fulfillment verification
use_when: Verifying multi-package order statuses
not_for:
  - Unauthorized refunds
  - Payment operations
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords: [logistics, package]
---

# Instructions
1. Query all packages for the order.
2. Verify every package has a definitive scan.
3. Check set completeness before concluding full delivery.
"""

    llm = FakeLLM([mock_skill_md])

    result = mine_candidate(
        episodes=[ep1, ep2],
        target_skill_name="logistics_checker",
        llm=llm,
        candidate_store=cand_store,
        decision_override="create",
        working_summary=work_summary,
        validation_feedback=validation_feedback,
    )

    assert result.decision == "create"
    assert result.candidate is not None
    assert result.candidate.skill_name == "logistics_checker"

    # Verify miner prompt contains working memory findings
    prompt_sent = llm.prompts_received[0]
    assert "Working Memory Index & Key Findings" in prompt_sent
    assert "All 3 packages must be checked individually" in prompt_sent
    assert "Declare delivered after checking 2 packages" in prompt_sent
    assert "RULE_SET_COMPLETENESS" in prompt_sent
    # Ensure hidden test case label was stripped/sanitized
    assert "[HIDDEN_TEST_CASE_IGNORED]" not in prompt_sent

    # Verify A8 Purpose Isolation: evaluation feedback is rejected before prompt assembly
    eval_feedback = {
        "rule_id": "RULE_HELDOUT_CHECK",
        "reason": "Failed hidden evaluation test",
        "purpose": "evaluation",
    }
    with pytest.raises(ValueError, match="Purpose isolation violation"):
        mine_candidate(
            episodes=[ep1, ep2],
            target_skill_name="logistics_checker",
            llm=llm,
            candidate_store=cand_store,
            decision_override="create",
            validation_feedback=eval_feedback,
        )


def test_promotion_card_generation_and_rendering(tmp_path: Path):
    db_path = tmp_path / "sf.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)

    ep1 = _create_sample_episode("ep_1")
    ep_store.save_episode(ep1)

    meta = SkillMeta(
        name="logistics_checker",
        version="1.1.0",
        description="Multi-package verification",
        use_when="When customer inquires about delivery status",
        not_for=["Financial refunds", "Address changes"],
        dependencies=["query_order_packages"],
        trigger=Trigger(keywords=["package"]),
    )

    candidate = CandidateSkill(
        candidate_id="cand_test_01",
        skill_name="logistics_checker",
        decision="revise",
        source_episode_ids=["ep_1"],
        meta=meta,
        body="## Instructions\nVerify set completeness.",
        status="DRAFT",
    )
    cand_store.save_candidate(candidate)

    val_record = ValidationRecord(
        candidate_id="cand_test_01",
        content_hash=compute_candidate_hash(candidate),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=EvalResult(
            release_id="rel_01",
            structure_score={"format": 40.0},
            effect_score={"task": 60.0},
            objective_metrics={},
            p0_pass=True,
            valid=True,
        ),
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=[]),
        promoted=False,
    )
    cand_store.save_validation_record(val_record)

    card = build_promotion_card(
        candidate=candidate,
        validation_record=val_record,
        selection_total=18,
        selection_passed=18,
        business_invariants=["RULE_SET_COMPLETENESS: PASS", "RULE_NO_FALSE_DELIVERY: PASS"],
        unverified_boundaries=["Carrier API 429 exponential backoff"],
    )

    assert card.can_promote is True
    assert card.selection_test_passed == 18
    assert card.selection_test_total == 18
    assert card.critical_invariants_pass is True

    rendered = render_promotion_card(card)
    assert "SkillForge 候选技能晋升审批卡片" in rendered
    assert "cand_test_01" in rendered
    assert "1.0.0 -> 1.1.0" in rendered
    assert "18 / 18 PASS" in rendered
    assert "RULE_SET_COMPLETENESS: PASS" in rendered
    assert "Carrier API 429 exponential backoff" in rendered
    assert "[y/N]" in rendered


def test_promotion_card_rejects_declined_candidate(tmp_path: Path):
    db_path = tmp_path / "sf.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)

    ep_bad = _create_sample_episode("ep_bad")
    ep_store.save_episode(ep_bad)

    meta = SkillMeta(
        name="bad_skill",
        version="1.0.1",
        description="Bad",
        use_when="Always",
        not_for=[],
        dependencies=[],
        trigger=Trigger(keywords=["bad"]),
    )
    candidate = CandidateSkill(
        candidate_id="cand_bad_01",
        skill_name="bad_skill",
        decision="create",
        source_episode_ids=["ep_bad"],
        meta=meta,
        body="Bad body",
        status="DRAFT",
    )
    cand_store.save_candidate(candidate)

    val_record = ValidationRecord(
        candidate_id="cand_bad_01",
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="DECLINED", reasons=["Regression on critical invariant"]),
        promoted=False,
    )
    cand_store.save_validation_record(val_record)

    card = build_promotion_card(
        candidate=candidate,
        validation_record=val_record,
        selection_total=10,
        selection_passed=7,
        business_invariants=["RULE_SET_COMPLETENESS: FAIL"],
    )

    assert card.can_promote is False
    assert "Regression on critical invariant" in (card.rejection_reason or "")


def test_interactive_confirm_promotion(tmp_path: Path):
    import subprocess
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    db_path = repo_dir / "sf.db"
    skills_dir = repo_dir / "skills"
    skills_dir.mkdir()
    registry = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_dir)
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_dir)

    ep1 = _create_sample_episode("ep_1")
    ep_store.save_episode(ep1)

    meta = SkillMeta(
        name="shipping_verifier",
        version="1.0.0",
        description="Verify shipments",
        use_when="Customer asks for shipment details",
        not_for=["Refunds"],
        dependencies=[],
        trigger=Trigger(keywords=["shipment"]),
    )
    candidate = CandidateSkill(
        candidate_id="cand_ship_01",
        skill_name="shipping_verifier",
        decision="create",
        source_episode_ids=["ep_1"],
        meta=meta,
        body="## Instructions\nVerify accurately.",
        status="DRAFT",
    )
    cand_store.save_candidate(candidate)

    val_record = ValidationRecord(
        candidate_id="cand_ship_01",
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=EvalResult(
            release_id="rel_01",
            structure_score={"format": 40.0},
            effect_score={"task": 60.0},
            objective_metrics={},
            p0_pass=True,
            valid=True,
        ),
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=[]),
        promoted=False,
    )
    cand_store.save_validation_record(val_record)

    card = build_promotion_card(
        candidate=candidate,
        validation_record=val_record,
        selection_total=5,
        selection_passed=5,
    )

    # 1. User inputs 'n' -> Rejected
    promoted_no = interactive_confirm_promotion(
        card=card,
        candidate=candidate,
        validation_record=val_record,
        registry=registry,
        candidate_store=cand_store,
        state_machine=sm,
        input_fn=lambda _prompt="": "n",
    )
    assert promoted_no is False
    assert "shipping_verifier" not in registry.list_names()

    # 2. User inputs 'y' -> Promoted
    promoted_yes = interactive_confirm_promotion(
        card=card,
        candidate=candidate,
        validation_record=val_record,
        registry=registry,
        candidate_store=cand_store,
        state_machine=sm,
        input_fn=lambda _prompt="": "y",
    )
    assert promoted_yes is True
    assert "shipping_verifier" in registry.list_names()
