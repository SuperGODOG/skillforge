"""E2: 软质量成对比较与候选筛选入口验证测试 (Soft-Quality Pairwise Selection).

Methodology source:
- Darwin-Skill v2.1 pairwise comparison (https://github.com/alchaincyf/darwin-skill/blob/master/SKILL.md)

Tests:
1. Real selection entry point (pairwise_select_candidate) with fixed hash bindings and position swapping via evaluator runner.
2. Hard gate invariant: "只改文风但业务错不能赢" — style preference cannot rescue business failure.
3. Position swapping & conflict: Order bias / conflict conservatively retains parent.
4. Tie & unknown handling: Tied or invalid judge outcomes never automatically promote; outputs REVIEW.
5. Judge exception / infrastructure failure: Conservative fallback retains parent without crashing.
6. Explicit parent version vs candidate version binding (avoids git HEAD comparison bug).
7. Purpose isolation: Locked final_audit test cases are strictly forbidden in selection.
8. Rejection of naked caller outputs bypass (candidate_outputs/parent_outputs caller bypass prohibited).
9. Missing runner returns UNKNOWN without fabricating text.
10. Missing oracle fails closed as UNKNOWN.
11. Single conflict strictly retains parent even when candidate wins plurality ("多数先行" blocked).
12. Identical candidate and parent content short-circuits as tie with 0 judge calls.
13. End-to-end integration: repair_skill_failure reads evaluator.pairwise_selector_config, executes bidirectional comparison, and records results into ValidationRecord.
14. Pairwise disabled on evaluator makes zero extra judge calls.
15. Evaluator pairwise config change invalidates old PASS validation records on promotion.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import yaml

from skillforge.evaluator import SkillEvaluator
from skillforge.evaluator.judge import JudgeResult, PairwiseJudge
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evolution_loop import (
    PairwiseSelectionResult,
    compute_candidate_hash,
    pairwise_select_candidate,
    promote_candidate,
    serialize_pairwise_config,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    RuleTraceEvidence,
    SkillMeta,
    Trigger,
)
from skillforge.registry import SkillRegistry
from skillforge.repair import repair_skill_failure, approve_repair_job, promote_repaired_skill
from skillforge.state_machine import ReleaseStateMachine


class ScriptedPairwiseLLM:
    """Deterministic Fake LLM for PairwiseJudge testing without paid external API calls."""

    def __init__(self, forward_verdict: str = "A_better", reverse_verdict: str = "B_better", exception_mode: bool = False):
        self.forward_verdict = forward_verdict
        self.reverse_verdict = reverse_verdict
        self.exception_mode = exception_mode
        self.call_count = 0
        self.prompts: list[str] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        if self.exception_mode:
            raise RuntimeError("Scripted judge simulated infrastructure connection timeout")

        self.call_count += 1
        prompt = str(messages)
        self.prompts.append(prompt)

        # Handle criteria evaluation prompt if evaluator.evaluate_skill invokes judge_llm
        if "rubric" in prompt.lower() or "findings" in prompt.lower() or "评测标准" in prompt:
            resp = json.dumps({
                "findings": [
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "q", "reason": "Good"},
                ]
            })
            return SimpleNamespace(content=resp, usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80})

        if "<answer_a>Parent response" in prompt and "<answer_b>Parent response" in prompt:
            verdict = "A_better"
        else:
            is_reverse = "<answer_a>Parent response" in prompt or "Parent response" in prompt[:prompt.find("<answer_b>")] or "<answer_a>parent" in prompt.lower()
            verdict = self.reverse_verdict if is_reverse else self.forward_verdict

        resp = json.dumps({
            "verdict": verdict,
            "reason_codes": ["READABILITY_CLEAR" if "better" in verdict else "READABILITY_TIED"],
            "evidence_summary": f"Evaluator judged {verdict} on readability clarity and structure",
        })
        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80},
        )


class ScriptedEvaluatorLLM:
    """Deterministic Fake LLM for Agent execution distinguishing Candidate vs Parent skill body."""

    def __init__(self, candidate_reply: str, parent_reply: str):
        self.candidate_reply = candidate_reply
        self.parent_reply = parent_reply
        self.call_count = 0

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        prompt = str(messages)
        if "1.0.1" in prompt or "v1.1.0" in prompt or "candidate" in prompt.lower() or "cand_" in prompt.lower() or "enhanced" in prompt.lower():
            reply = self.candidate_reply
        else:
            reply = self.parent_reply
        return SimpleNamespace(
            content=reply,
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        )


def _setup_repo(tmp_path: Path, skill_name: str = "fulfillment_helper") -> tuple[SkillRegistry, Path, Path, SkillMeta, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "test@test.local"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True)

    skills_dir = repo_root / "skills"
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    meta = SkillMeta(
        name=skill_name,
        version="1.0.0",
        description="Fulfillment helper",
        use_when="When assisting order fulfillment",
        not_for=["Refunds"],
        dependencies=[],
        trigger=Trigger(keywords=["fulfillment", "order"]),
    )
    body = (
        "## Overview\nFulfillment parent body v1.0.0.\n\n"
        "## Instructions\n1. Check orders.\n2. Report findings.\n"
    )
    (skill_dir / "SKILL.md").write_text(f"---\n{yaml.dump(meta.model_dump())}---\n\n{body}", encoding="utf-8")
    (repo_root / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    db_path = repo_root / "skillforge.db"
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()
    return reg, repo_root, db_path, meta, body


# ==============================================================================
# 1. Real Entrypoint & Consistent Candidate Win (E2)
# ==============================================================================

def test_pairwise_selection_real_entrypoint_with_hard_gate_and_swapped_soft_comparison():
    """Verify pairwise_select_candidate runs actual outputs via runner, binds hashes, and selects candidate on consistent win."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_entry_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        cand_meta = SkillMeta(
            name="fulfillment_helper",
            version="1.1.0",
            description="Enhanced fulfillment helper",
            use_when="When assisting order fulfillment",
        )
        cand_body = (
            "## Overview\nFulfillment candidate body v1.1.0 with structured formatting.\n\n"
            "## Instructions\n1. Check orders with clean summary.\n2. Detail per-package status concisely.\n"
        )
        candidate = CandidateSkill(
            candidate_id="cand_select_01",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_selection_01",
            meta=cand_meta,
            body=cand_body,
        )

        selection_cases = [
            {"id": "sel_case_01", "query": "查询订单 ORD_DEV_0101 状态", "reference": "PKG_101: DELIVERED"},
            {"id": "sel_case_02", "query": "查询订单 ORD_DEV_0102 状态", "reference": "PKG_102: DELIVERED"},
        ]

        # Agent runner outputs: candidate structured, parent plain
        agent_llm = ScriptedEvaluatorLLM(
            candidate_reply="【订单详情】ORD_DEV_0101: 包裹已妥投送达 (DELIVERED)",
            parent_reply="Parent response: delivered",
        )

        def business_oracle(output: str, query: str, case: dict) -> bool:
            return "DELIVERED" in output.upper()

        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge_llm)

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=selection_cases,
            evaluator=evaluator,
            judge=evaluator.judge,
            oracle_fn=business_oracle,
        )

        assert res.winner == "candidate"
        assert res.decision == "RECOMMEND_CANDIDATE"
        assert res.selected_candidate_id == "cand_select_01"
        assert res.hard_gate_pass is True
        assert res.soft_score_candidate_wins == 2
        assert res.soft_score_parent_wins == 0
        assert res.soft_score_ties == 0
        assert res.soft_score_conflicts == 0

        # Cryptographic bindings
        assert res.candidate_hash == compute_candidate_hash(candidate)
        assert res.parent_version == "1.0.0"
        assert len(res.parent_hash) == 64
        assert res.config_hash == evaluator.get_config_hash()

        # Case audits
        assert len(res.case_audits) == 2
        audit1 = res.case_audits[0]
        assert audit1.case_id == "sel_case_01"
        assert audit1.forward_verdict == "A_better"
        assert audit1.reverse_verdict == "B_better"
        assert audit1.consistent_verdict == "candidate_better"
        assert any("Evaluator judged" in q for q in audit1.evidence_quotes)


# ==============================================================================
# 2. Hard Gate Invariant: "只改文风但业务错不能赢" (E2)
# ==============================================================================

def test_pairwise_hard_gate_style_cannot_rescue_business_failure():
    """Verify that when a candidate has superior style/readability but makes a business correctness error,
    hard gate immediately blocks candidate and retains parent."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_hard_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_eloquent_but_wrong",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate eloquent body v1.1.0\n",
        )

        selection_cases = [
            {"id": "sel_01", "query": "查询 ORD_001", "reference": "PKG_01: IN_TRANSIT, PKG_02: EXCEPTION"},
        ]

        agent_llm = ScriptedEvaluatorLLM(
            candidate_reply="尊敬的客户您好！经系统全面核查，您的所有包裹均已圆满送达，祝您生活愉快！",
            parent_reply="Parent response: ORD_001 部分包裹运输中，部分异常",
        )

        def strict_oracle(output: str, query: str, case: dict) -> bool:
            return "均已圆满送达" not in output

        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge_llm)

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=selection_cases,
            evaluator=evaluator,
            judge=evaluator.judge,
            oracle_fn=strict_oracle,
        )

        assert res.winner == "retained_parent"
        assert res.decision == "HARD_GATE_FAILED"
        assert res.hard_gate_pass is False
        assert res.selected_candidate_id is None
        assert any("failed business oracle verification" in r for r in res.reasons)
        assert "Style preference cannot rescue business failure" in res.rationale


# ==============================================================================
# 3. Position Conflict / Order Bias Handled Conservatively (E2)
# ==============================================================================

def test_pairwise_position_conflict_conservatively_retains_parent():
    """Verify order bias / position conflict (judge favors A in both forward and reverse)
    results in order conflict and conservatively retains parent."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_conflict_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_bias_test",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )

        selection_cases = [{"id": "sel_01", "query": "q", "reference": "r"}]
        agent_llm = ScriptedEvaluatorLLM(candidate_reply="Candidate response", parent_reply="Parent response")
        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="A_better")
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge_llm)

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=selection_cases,
            evaluator=evaluator,
            judge=evaluator.judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.winner == "retained_parent"
        assert res.decision == "UNKNOWN"
        assert res.soft_score_conflicts == 1
        assert res.soft_score_candidate_wins == 0
        assert res.case_audits[0].consistent_verdict == "conflict"
        assert any("Order conflict detected" in r for r in res.case_audits[0].reasons)


# ==============================================================================
# 4. Tie & Unknown Does Not Trigger Promotion (E2)
# ==============================================================================

def test_pairwise_tie_does_not_automatically_promote():
    """Verify that tied judge results retain parent version."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_tie_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_tie_test",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )

        selection_cases = [{"id": "sel_01", "query": "q", "reference": "r"}]
        agent_llm = ScriptedEvaluatorLLM(candidate_reply="Response", parent_reply="Parent response")
        judge_llm = ScriptedPairwiseLLM(forward_verdict="tied", reverse_verdict="tied")
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge_llm)

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=selection_cases,
            evaluator=evaluator,
            judge=evaluator.judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.winner == "retained_parent"
        assert res.decision == "TIE"
        assert res.selected_candidate_id is None
        assert res.soft_score_ties == 1
        assert "tied outcome" in res.rationale


def test_pairwise_unknown_and_judge_exception_conservatively_handled():
    """Verify that simulated judge exceptions or timeouts fail closed and conservatively retain parent."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_exc_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_exc_test",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )

        selection_cases = [{"id": "sel_01", "query": "q", "reference": "r"}]
        agent_llm = ScriptedEvaluatorLLM(candidate_reply="Response", parent_reply="Parent response")
        judge_llm = ScriptedPairwiseLLM(exception_mode=True)
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge_llm)

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=selection_cases,
            evaluator=evaluator,
            judge=evaluator.judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.winner == "retained_parent"
        assert res.decision == "UNKNOWN"
        assert res.soft_score_conflicts == 1
        assert res.case_audits[0].consistent_verdict == "unknown"


# ==============================================================================
# 5. Purpose Isolation: Locked Tier Forbidden in Selection (E2)
# ==============================================================================

def test_locked_final_audit_cases_strictly_rejected_for_selection():
    """Verify selection rejects cases from locked final_audit tier."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_iso_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_iso_test",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="body",
        )

        locked_cases = [
            {"id": "audit_case_01", "query": "secret", "reference": "secret_ref", "layer": "final_audit"}
        ]

        judge = PairwiseJudge(ScriptedPairwiseLLM())
        fake_evaluator = SimpleNamespace(config_hash="cfg_01", judge=judge)

        with pytest.raises(ValueError, match="Purpose isolation violation: selection case 'audit_case_01' is from locked tier 'final_audit'"):
            pairwise_select_candidate(
                candidate=candidate,
                parent_meta=parent_meta,
                parent_body=parent_body,
                selection_cases=locked_cases,
                evaluator=fake_evaluator,
                judge=judge,
            )


# ==============================================================================
# 6. Direct Caller Output Bypass Prohibition (E2 Audit Requirement)
# ==============================================================================

def test_pairwise_disallows_naked_caller_outputs_bypass():
    """Verify passing naked candidate_outputs or parent_outputs raises ValueError."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_bypass_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_bypass",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="body",
        )
        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        evaluator = SkillEvaluator(registry=reg, llm=ScriptedEvaluatorLLM("a", "b"), judge_llm=ScriptedPairwiseLLM())

        with pytest.raises(ValueError, match="Direct caller injection of candidate_outputs or parent_outputs is prohibited"):
            pairwise_select_candidate(
                candidate=candidate,
                parent_meta=parent_meta,
                parent_body=parent_body,
                selection_cases=cases,
                evaluator=evaluator,
                judge=evaluator.judge,
                candidate_outputs={"c1": "fake output"},
            )


def test_missing_runner_does_not_fabricate_fallback_text():
    """Verify missing outputs without runner returns UNKNOWN and never fabricates fallback text."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_no_runner_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_no_runner",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="body",
        )

        cases = [{"id": "c1", "query": "执行测试查询", "reference": "ref"}]
        evaluator_no_runner = SimpleNamespace(config_hash="cfg_01", judge=PairwiseJudge(ScriptedPairwiseLLM()))

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=cases,
            evaluator=evaluator_no_runner,
            judge=evaluator_no_runner.judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.winner == "retained_parent"
        assert res.decision == "UNKNOWN"
        assert any("runner unavailable" in r for r in res.reasons)
        assert res.soft_score_candidate_wins == 0


def test_missing_oracle_fails_closed_as_unknown():
    """Verify pairwise_select_candidate fails closed if no oracle or criteria evaluator is provided."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_no_oracle_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_no_oracle",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        agent_llm = ScriptedEvaluatorLLM("cand response", "parent response")
        evaluator_bare = SimpleNamespace(
            config_hash="cfg_01",
            judge=PairwiseJudge(ScriptedPairwiseLLM()),
            _run_with_skill=lambda q, b, **kw: ("resp", {}),
        )

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=cases,
            evaluator=evaluator_bare,
            judge=evaluator_bare.judge,
            oracle_fn=None,  # Missing oracle!
        )

        assert res.winner == "retained_parent"
        assert res.decision == "UNKNOWN"
        assert res.hard_gate_pass is False
        assert any("neither oracle_fn nor criteria evaluator provided" in r for r in res.reasons)


def test_single_conflict_strictly_retains_parent_even_if_candidate_wins_plurality():
    """Verify that even when candidate wins multiple cases, ANY position conflict strictly retains parent."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_single_conflict_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_conflict_plurality",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )

        cases = [
            {"id": "c1", "query": "q1", "reference": "r1"},
            {"id": "c2", "query": "q2", "reference": "r2"},
            {"id": "c3", "query": "q3", "reference": "r3"},
        ]
        agent_llm = ScriptedEvaluatorLLM("cand out", "parent out")

        # Custom judge: c1 and c2 are candidate wins; c3 has position conflict (A_better in both fwd and rev)
        class CustomMultiCaseJudgeLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                prompt = str(messages)
                is_reverse = "<answer_a>parent" in prompt.lower()
                if "q3" in prompt:
                    verdict = "A_better"  # Position bias conflict on case 3
                else:
                    verdict = "B_better" if is_reverse else "A_better"  # Candidate wins cases 1 & 2
                return SimpleNamespace(content=json.dumps({"verdict": verdict, "reason_codes": ["OK"], "evidence_summary": "ev"}))

        judge = PairwiseJudge(CustomMultiCaseJudgeLLM())
        evaluator = SkillEvaluator(registry=reg, llm=agent_llm, judge_llm=judge.llm)
        evaluator.judge = judge

        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=cases,
            evaluator=evaluator,
            judge=judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.soft_score_candidate_wins == 2
        assert res.soft_score_conflicts == 1
        # Invariant: despite cand_wins=2, ANY conflict strictly prevents candidate selection!
        assert res.winner == "retained_parent"
        assert res.decision == "UNKNOWN"
        assert res.selected_candidate_id is None


def test_identical_candidate_and_parent_short_circuits_without_calling_judge():
    """Verify that identical candidate content short-circuits as tie and wastes 0 judge calls."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_identical_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)

        candidate = CandidateSkill(
            candidate_id="cand_identical",
            skill_name=parent_meta.name,
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(
                name=parent_meta.name,
                version="1.0.0",
                description=parent_meta.description,
                use_when=parent_meta.use_when,
            ),
            body=parent_body,  # Identical body
        )

        judge_llm = ScriptedPairwiseLLM()
        judge = PairwiseJudge(judge_llm)
        evaluator = SkillEvaluator(registry=reg, llm=ScriptedEvaluatorLLM("a", "b"), judge_llm=judge_llm)

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        res = pairwise_select_candidate(
            candidate=candidate,
            parent_meta=parent_meta,
            parent_body=parent_body,
            selection_cases=cases,
            evaluator=evaluator,
            judge=judge,
            oracle_fn=lambda out, q, c: True,
        )

        assert res.decision == "TIE"
        assert judge_llm.call_count == 0
        assert res.winner == "retained_parent"


# ==============================================================================
# 7. End-to-End RepairJob & Evaluator Pairwise Configuration Integration (E2)
# ==============================================================================

def test_repair_skill_failure_integration_reads_evaluator_pairwise_config_and_records_audits():
    """Verify RepairJob -> validate_candidate reads evaluator.pairwise_selector_config directly,
    runs bidirectional comparison, and stores PairwiseCaseAudit in ValidationRecord."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_repair_e2e_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        # 1. Existing failure episode
        ep = Episode(
            episode_id="ep_repair_01",
            task_id="task_e2_01",
            run_id="run_e2_01",
            skill_name="fulfillment_helper",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria="Check status correctly",
            outcome="failure",
            outcome_reason="Missing structured package details",
        )
        ep_store.save_episode(ep)

        # 2. Configure SkillEvaluator with explicit pairwise_selector_config
        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
        agent_llm = ScriptedEvaluatorLLM(
            candidate_reply="【履约跟踪】ORD_100: 包裹 PKG_1 已妥投 (DELIVERED)",
            parent_reply="Parent response: PKG_1 delivered",
        )
        pairwise_cfg = {
            "enabled": True,
            "strategy": "pairwise_abba",
            "soft_dimension": "readability",
            "cases": [{"id": "sel_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}],
            "oracle_fn": lambda out, q, c: "DELIVERED" in out.upper(),
        }
        evaluator = SkillEvaluator(
            registry=reg,
            llm=agent_llm,
            judge_llm=judge_llm,
            pairwise_selector_config=pairwise_cfg,
        )

        # Patcher LLM that repairs the skill
        class ScriptedPatcherLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                fixed_meta = SkillMeta(
                    name="fulfillment_helper",
                    version="1.0.1",
                    description="Fixed fulfillment helper",
                    use_when="When assisting order fulfillment",
                    not_for=["Refunds"],
                    dependencies=[],
                    trigger=Trigger(keywords=["fulfillment", "order"]),
                )
                fixed_body = (
                    "## Overview\nCandidate fixed fulfillment body v1.0.1 with bullet points.\n\n"
                    "## Instructions\n1. Detail each package cleanly.\n"
                )
                content = f"---\n{yaml.dump(fixed_meta.model_dump())}---\n\n{fixed_body}"
                return SimpleNamespace(content=content, usage={})

        eval_cases = [{"id": "case_eval_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}]

        # Run repair_skill_failure: normal repair pipeline without sneaky parameters
        job = repair_skill_failure(
            episodes=[ep],
            skill_name="fulfillment_helper",
            episode_store=ep_store,
            candidate_store=cand_store,
            registry=reg,
            evaluator=evaluator,
            eval_cases=eval_cases,
            llm=ScriptedPatcherLLM(),
        )

        assert job.status in ("READY", "COMPLETED", "PROMOTED", "AWAITING_REVIEW", "DECLINED")
        assert len(job.attempts) >= 1
        cand_id = job.attempts[0].candidate_id
        val_rec = cand_store.get_validation_record(cand_id)

        assert val_rec is not None
        assert val_rec.pairwise_selection is not None
        pw = val_rec.pairwise_selection
        assert pw["winner"] == "candidate"
        assert pw["decision"] == "RECOMMEND_CANDIDATE"
        assert len(pw["case_audits"]) == 1
        audit = pw["case_audits"][0]
        assert audit["forward_verdict"] == "A_better"
        assert audit["reverse_verdict"] == "B_better"
        assert audit["consistent_verdict"] == "candidate_better"

        # Re-open CandidateStore to verify persistence and deserialization across instances
        store_reopened = CandidateStore(db_path, episode_store=EpisodeStore(db_path))
        reopened_rec = store_reopened.get_validation_record(cand_id)
        assert reopened_rec is not None
        assert reopened_rec.pairwise_selection is not None
        assert reopened_rec.pairwise_selection["decision"] == "RECOMMEND_CANDIDATE"
        assert reopened_rec.pairwise_selection["case_audits"][0]["forward_verdict"] == "A_better"


def test_pairwise_disabled_on_evaluator_makes_zero_extra_judge_calls():
    """Verify that when pairwise_selector_config is disabled, zero extra pairwise judge calls are made."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_disabled_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)

        pairwise_judge_llm = ScriptedPairwiseLLM()
        pairwise_judge = PairwiseJudge(pairwise_judge_llm)
        pairwise_cfg = {
            "enabled": False,  # Explicitly disabled
            "judge": pairwise_judge,
        }

        evaluator = SkillEvaluator(
            registry=reg,
            llm=ScriptedEvaluatorLLM("cand", "parent"),
            judge_llm=ScriptedPairwiseLLM(),
            pairwise_selector_config=pairwise_cfg,
        )

        cand = CandidateSkill(
            candidate_id="cand_disabled",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(name="fulfillment_helper", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nCandidate body v1.1.0\n",
        )
        cand_store.save_candidate(cand)

        eval_cases = [{"id": "c1", "query": "q", "reference": "r"}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=eval_cases,
            candidate_store=cand_store,
        )

        assert val_rec.pairwise_selection is None
        assert pairwise_judge_llm.call_count == 0


@pytest.mark.parametrize("case_mode", ["tie", "exception", "unknown_conflict", "missing_oracle"])
def test_pairwise_gate_blocks_unknown_and_tie_from_passing(case_mode: str):
    """Verify that when pairwise selection is enabled, UNKNOWN, exception, missing oracle, or TIE outputs REVIEW and blocks promotion."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_gate_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path)

        pairwise_judge = None
        if case_mode == "tie":
            judge_llm = ScriptedPairwiseLLM(forward_verdict="tied", reverse_verdict="tied")
            oracle = lambda out, q, c: True
        elif case_mode == "exception":
            judge_llm = ScriptedPairwiseLLM()
            pairwise_judge = PairwiseJudge(ScriptedPairwiseLLM(exception_mode=True))
            oracle = lambda out, q, c: True
        elif case_mode == "unknown_conflict":
            judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="A_better")
            oracle = lambda out, q, c: True
        else:  # missing_oracle
            judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
            oracle = None

        pairwise_cfg = {
            "enabled": True,
            "strategy": "pairwise_abba",
            "soft_dimension": "readability",
            "enforce_recommendation": True,
            "oracle_fn": oracle,
        }
        if pairwise_judge is not None:
            pairwise_cfg["judge"] = pairwise_judge
        evaluator = SkillEvaluator(
            registry=reg,
            llm=ScriptedEvaluatorLLM("cand response", "parent response"),
            judge_llm=judge_llm,
            pairwise_selector_config=pairwise_cfg,
        )

        cand = CandidateSkill(
            candidate_id=f"cand_{case_mode}_gate",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(
                name="fulfillment_helper",
                version="1.0.1",
                description="Fulfillment helper",
                use_when="When assisting order fulfillment",
                not_for=["Refunds"],
                dependencies=[],
                trigger=Trigger(keywords=["fulfillment", "order"]),
            ),
            body="## Overview\nCandidate body v1.0.1\n\n## Instructions\n1. Check orders.\n2. Report findings.\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        base_res = evaluator.evaluate_skill("fulfillment_helper", cases=cases)
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            baseline_eval_result=base_res,
            candidate_store=cand_store,
        )

        # Invariant: Non-passing outcome must NOT pass gate; must be REVIEW to retain parent!
        assert val_rec.ratchet_decision == "REVIEW"
        if case_mode == "tie":
            assert val_rec.pairwise_selection["decision"] == "TIE"
        else:
            assert val_rec.pairwise_selection["decision"] == "UNKNOWN"

        # Formal promotion must strictly block REVIEW
        with pytest.raises(ValueError, match="Only PASS verdict may be promoted"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


def test_pairwise_config_change_invalidates_old_validation_record():
    """Verify that modifying pairwise selector configuration on evaluator changes config_hash and invalidates previous PASS."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_inval_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path, repo_root=repo_root)

        cfg1 = {"enabled": True, "soft_dimension": "readability", "oracle_fn": lambda out, q, c: True}
        evaluator1 = SkillEvaluator(
            registry=reg,
            llm=ScriptedEvaluatorLLM("cand response", "parent response"),
            judge_llm=ScriptedPairwiseLLM("A_better", "B_better"),
            pairwise_selector_config=cfg1,
        )
        hash1 = evaluator1.get_config_hash()

        cand = CandidateSkill(
            candidate_id="cand_inval_test",
            skill_name="fulfillment_helper",
            decision="revise",
            source_requirement="req_01",
            meta=SkillMeta(
                name="fulfillment_helper",
                version="1.0.1",
                description="Fulfillment helper",
                use_when="When assisting order fulfillment",
                not_for=["Refunds"],
                dependencies=[],
                trigger=Trigger(keywords=["fulfillment", "order"]),
            ),
            body="## Overview\nCandidate body v1.0.1\n\n## Instructions\n1. Check orders.\n2. Report findings.\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        base_res = evaluator1.evaluate_skill("fulfillment_helper", cases=cases)
        base_res.effect_score["readability"] = 10.0
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator1,
            registry=reg,
            eval_cases=cases,
            baseline_eval_result=base_res,
            candidate_store=cand_store,
        )
        assert val_rec.config_hash == hash1
        assert val_rec.ratchet_decision == "PASS"

        # Now change pairwise configuration to efficiency dimension
        cfg2 = {"enabled": True, "soft_dimension": "efficiency", "oracle_fn": lambda out, q, c: True}
        evaluator2 = SkillEvaluator(
            registry=reg,
            llm=evaluator1.llm,
            judge_llm=evaluator1.judge_llm,
            pairwise_selector_config=cfg2,
        )
        hash2 = evaluator2.get_config_hash()
        assert hash1 != hash2

        # Promotion checking against new configuration hash must fail
        with pytest.raises(ValueError, match="validator config hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash=hash2,
            )

        # Automatic evaluator context check: passing evaluator2 directly without manual hash must also fail
        with pytest.raises(ValueError, match="validator config hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                evaluator=evaluator2,
            )

        # Omitting both evaluator and expected_config_hash on new config record must fail closed
        with pytest.raises(ValueError, match="missing evaluation context"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


# ==============================================================================
# 8. Real repair_skill_failure -> approve_repair_job Context Enforcement (E2)
# ==============================================================================

def test_approve_repair_job_and_promote_candidate_enforces_current_evaluator_context():
    """Verify approve_repair_job -> promote_candidate automatically validates current config hash:
    1. Same profile/config automatically confirms and promotes without manual hash copying.
    2. Modifying soft_dimension/profile causes approval rejection of old PASS without caller providing hash.
    3. Low-level promote_candidate rejects promotion if current evaluator/context is omitted on new config."""
    with tempfile.TemporaryDirectory(prefix="sf_e2_approve_ctx_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path, parent_meta, parent_body = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path, repo_root=repo_root)

        # 1. Existing failure episode
        ep = Episode(
            episode_id="ep_repair_appr_01",
            task_id="task_e2_appr",
            run_id="run_e2_appr",
            skill_name="fulfillment_helper",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria="Check status correctly",
            outcome="failure",
            outcome_reason="Missing package structure",
        )
        ep_store.save_episode(ep)

        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
        agent_llm = ScriptedEvaluatorLLM(
            candidate_reply="【履约跟踪】ORD_100: 包裹 PKG_1 已妥投 (DELIVERED)",
            parent_reply="Parent response: PKG_1 delivered",
        )
        pairwise_cfg1 = {
            "enabled": True,
            "strategy": "pairwise_abba",
            "soft_dimension": "readability",
            "cases": [{"id": "sel_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}],
            "oracle_fn": lambda out, q, c: "DELIVERED" in out.upper(),
        }
        evaluator1 = SkillEvaluator(
            registry=reg,
            llm=agent_llm,
            judge_llm=judge_llm,
            pairwise_selector_config=pairwise_cfg1,
        )

        class ScriptedPatcherLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                fixed_meta = SkillMeta(
                    name="fulfillment_helper",
                    version="1.0.1",
                    description="Fixed fulfillment helper",
                    use_when="When assisting order fulfillment",
                    not_for=["Refunds"],
                    dependencies=[],
                    trigger=Trigger(keywords=["fulfillment", "order"]),
                )
                fixed_body = (
                    "## Overview\nCandidate fixed fulfillment body v1.0.1 with bullet points.\n\n"
                    "## Instructions\n1. Detail each package cleanly.\n"
                )
                content = f"---\n{yaml.dump(fixed_meta.model_dump())}---\n\n{fixed_body}"
                return SimpleNamespace(content=content, usage={})

        eval_cases = [{"id": "case_eval_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}]

        job = repair_skill_failure(
            episodes=[ep],
            skill_name="fulfillment_helper",
            episode_store=ep_store,
            candidate_store=cand_store,
            registry=reg,
            evaluator=evaluator1,
            eval_cases=eval_cases,
            llm=ScriptedPatcherLLM(),
        )
        assert job.status == "READY"
        cand = job.latest_candidate

        # Test 1: Low-level promote_candidate without current evaluator or expected_config_hash fails closed
        with pytest.raises(ValueError, match="missing evaluation context"):
            promote_candidate(
                candidate=cand,
                validation_record=cand_store.get_validation_record(cand.candidate_id),
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )

        # Test 2: Modified config evaluator (soft_dimension: efficiency) causes approve_repair_job rejection without manual hash
        evaluator_diff = SkillEvaluator(
            registry=reg,
            llm=agent_llm,
            judge_llm=judge_llm,
            pairwise_selector_config={**pairwise_cfg1, "soft_dimension": "efficiency"},
        )
        with pytest.raises(ValueError, match="validator config hash changed"):
            approve_repair_job(
                job=job,
                candidate_store=cand_store,
                registry=reg,
                state_machine=sm,
                caller_confirmed=True,
                evaluator=evaluator_diff,
            )

        # Test 3: Matching evaluator automatically confirms without caller typing hash
        rel = approve_repair_job(
            job=job,
            candidate_store=cand_store,
            registry=reg,
            state_machine=sm,
            caller_confirmed=True,
            evaluator=evaluator1,
        )
        assert rel is not None
        assert job.status == "PROMOTED"
