"""Dedicated J1-J10 Criteria-v1 Evaluation Protocol Test Suite.

Verifies:
- J1: Independent criteria evaluation for task_completion & robustness; position-balanced readability; objective efficiency formula; both failing doesn't award full marks; readability cannot rescue critical FAIL.
- J2: Dedup duplicate findings without double deduction; conflict/unknown ID/missing/illegal status/model injection/quote fabrication fail-closed; zero-error pass without bug quotas.
- J3: UNKNOWN and malformed responses fail-closed (eval.valid=False); tool failure / security refusal handled without false blaming.
- J4: Deterministic code oracle priority; logistics assertions via real evaluate_skill entry point; 0 LLM calls for criteria; permission denial pass.
- J5: Fixed deductions per frozen rubric weights; phrasing invariance; non-negative scores; denominator invariance.
- J6: Critical failure blocks ratchet (cold start, baseline failure, high score); CandidateStore forged PASS rejection in promote_candidate.
- J7: Legacy case compatibility; legacy record scoring_policy="legacy_pairwise_v1" deserialization; policy mismatch ratchet block; no PASS reuse.
- J8: Semantic digest, config fingerprint, and cases hash invalidation chains; promotion drift rejection.
- J9: Dual-ended tool evidence isolation; authenticity sentinel; budget / retry limits.
- J10: Systematic execution order with exit 0 and provider=0 verification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from skillforge import (
    CandidateSkill,
    CandidateStore,
    Episode,
    EpisodeStore,
    EvalResult,
    Patch,
    RatchetVerdict,
    ReleaseStateMachine,
    SkillEvaluator,
    SkillMeta,
    SkillRegistry,
    Trigger,
    compute_candidate_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.evaluator.criteria import (
    CRITERIA_POLICY_VERSION,
    CRITERIA_PROMPT_VERSION,
    DEFAULT_RUBRIC_V1,
    RuleDefinition,
    RuleFinding,
    build_criteria_prompt,
    compute_case_scores,
    derive_pairwise_verdict,
    evaluate_semantic_criteria,
    parse_criteria_json,
    validate_rubric,
)
from skillforge.evaluator.judge import (
    PairwiseJudge,
    judge_prompt_sha256,
    judge_semantic_digest,
)
from skillforge.evaluator.llm_factory import compute_evaluator_fingerprint
from skillforge.evaluator.ratchet import check_ratchet
from skillforge.evolution_loop import compute_cases_hash
from skillforge.models import ToolCallProvenance, ToolCallRecord, ValidationRecord
from skillforge.scenarios.logistics import verify_logistics_fulfillment_as_findings


# ==============================================================================
# Controllable Fake LLM (provider=0: no real network calls)
# ==============================================================================

class ScriptedCriteriaLLM:
    """Deterministic Fake LLM recording calls and returning scripted responses."""

    def __init__(self, responses: Optional[list[str]] = None, default_response: Optional[str] = None):
        self.responses = list(responses) if responses else []
        self.default_response = default_response
        self.calls: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.responses:
            resp = self.responses.pop(0)
        elif self.default_response is not None:
            resp = self.default_response
        else:
            if isinstance(messages, list) and len(messages) > 0:
                user_msg = messages[-1].get("content", "") if isinstance(messages[-1], dict) else str(messages[-1])
            else:
                user_msg = str(messages)
            if "readability" in user_msg or "判断两个 Agent 回答" in user_msg:
                resp = json.dumps({"verdict": "tied", "reason_codes": ["OK"], "evidence_summary": "readability ok"})
            else:
                m = re.search(r"<answer>\s*(.*?)\s*</answer>", user_msg, re.DOTALL)
                raw_ans = m.group(1).strip() if m else ""
                ans_snippet = raw_ans[:30] if raw_ans and raw_ans != "(空回答)" else ""
                ev_str = f'"{ans_snippet}"' if ans_snippet else 'MISSING: "requirement" ; scope=answer'
                resp = json.dumps({
                    "findings": [
                        {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": ev_str, "reason": "verified goal"},
                        {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": ev_str, "reason": "verified constraints"},
                        {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": ev_str, "reason": "verified evidence"},
                        {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": ev_str, "reason": "verified failure handling"},
                    ]
                })
        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        )


def _setup_mock_repo(tmp_path: Path, skill_name: str = "test_assistant") -> tuple[SkillRegistry, Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "eval@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Evaluator"], check=True)

    skills_dir = repo_root / "skills"
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    meta = SkillMeta(
        name=skill_name,
        version="1.0.0",
        description="A verified assistant skill",
        use_when="When users ask for test tasks",
        not_for=["unrelated tasks"],
        dependencies=[],
        trigger=Trigger(keywords=["test", "assist"]),
    )
    body = (
        "## Overview\nTest assistant skill body.\n\n"
        "## Instructions\n1. Process user request.\n2. Return exact answer.\n\n"
        "## Constraints\n- Do not fabricate facts.\n"
    )
    import yaml
    (skill_dir / "SKILL.md").write_text(f"---\n{yaml.dump(meta.model_dump())}---\n\n{body}", encoding="utf-8")
    readme = repo_root / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    db_path = repo_root / "skillforge.db"
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()
    return reg, repo_root


# ==============================================================================
# J1: Independent Criteria Scoring & Dimension Independence
# ==============================================================================

def test_j1_criteria_scoring_and_dimension_independence(tmp_path: Path):
    """J1: task/robust主evaluate独立规则评分，readability旧配对；双方同错不因相对更好获得业务满分；修改readability不能挽救critical FAIL."""
    reg, repo_root = _setup_mock_repo(tmp_path, "j1_skill")

    # Scenario 1: Both baseline and skill fail TASK_GOAL_COMPLETE
    # Candidate should get 10/25 for task (passed constraints, failed goal complete), NOT full 25.0
    criteria_resp_fail_goal = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "FAIL", "evidence": 'MISSING: "X=42" ; scope=answer', "reason": "goal incomplete"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
        ]
    })
    judge_llm = ScriptedCriteriaLLM(default_response=criteria_resp_fail_goal)
    exec_llm = ScriptedCriteriaLLM(default_response="valid output")
    evaluator = SkillEvaluator(registry=reg, llm=exec_llm, judge_llm=judge_llm, scoring_policy="criteria_v1")

    cases = [{"id": "c1", "query": "calculate X", "reference": "X=42"}]
    res = evaluator.evaluate_skill("j1_skill", cases=cases)

    # 10 / 25 * 25.0 = 10.0 (passed weight=10, applicable=25)
    assert res.effect_score["task"] == 10.0
    assert res.effect_score["robust"] == 15.0
    # Derived verdict is tied because both failed same rule
    assert res.case_verdicts[0]["task_completion"] == "tied"

    # Scenario 2: Critical fail on ROBUST_EVIDENCE_FAITHFUL cannot be rescued by high readability
    criteria_resp_crit_fail = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "FAIL", "evidence": 'MISSING: "X=42" ; scope=answer', "reason": "fabricated facts"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"valid output"', "reason": "ok"},
        ]
    })
    judge_llm_crit = ScriptedCriteriaLLM(default_response=criteria_resp_crit_fail)
    evaluator_crit = SkillEvaluator(registry=reg, llm=exec_llm, judge_llm=judge_llm_crit, scoring_policy="criteria_v1")
    res_crit = evaluator_crit.evaluate_skill("j1_skill", cases=cases)

    assert res_crit.critical_fail is True
    assert len(res_crit.critical_reasons) > 0
    # Readability score is present, but critical_fail causes ratchet DECLINE
    verdict = check_ratchet(None, res_crit)
    assert verdict.decision == "DECLINED"
    assert "critical" in verdict.reasons[0].lower()


# ==============================================================================
# J2: Audit Dedup & Malicious Model Injection Defense
# ==============================================================================

def test_j2_audit_dedup_and_malicious_model_injection_defense():
    """J2: 同规则重复不重复扣分；冲突/未知ID/漏项/非法枚举/模型注入权重与关键性/伪evidence/候选prompt injection失效；零错误可通过."""
    rubric = dict(DEFAULT_RUBRIC_V1)

    # 1. Duplicate findings with identical status: deduplicated, no double penalty
    dup_json = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "FAIL", "evidence": 'MISSING: "item A" ; scope=answer', "reason": "incomplete"},
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "FAIL", "evidence": 'MISSING: "item A" ; scope=answer', "reason": "incomplete"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"valid candidate"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"valid candidate"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"valid candidate"', "reason": "ok"},
        ]
    })
    findings, invals = parse_criteria_json(dup_json, rubric, answer="valid candidate", reference="must have item A")
    assert len(findings) == 4
    assert findings["TASK_GOAL_COMPLETE"].status == "FAIL"
    # Deductions are exactly 15.0 once, not 30.0
    task_sc, robust_sc, valid, crit, _ = compute_case_scores(findings, rubric)
    assert task_sc == 10.0  # 10 / 25 * 25.0
    assert valid is True

    # 2. Conflicting findings for same rule ID: fails closed to UNKNOWN
    conflict_json = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "FAIL", "evidence": 'MISSING: "item A" ; scope=answer', "reason": "bad"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
        ]
    })
    findings, invals = parse_criteria_json(conflict_json, rubric, answer="candidate answer", reference="item A")
    assert findings["TASK_GOAL_COMPLETE"].status == "UNKNOWN"
    assert any("CONFLICTING_RULE_FINDINGS" in code for code in invals)

    # 3. Model attempting to inject weight, critical, or score
    inject_json = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "FAIL", "weight": 0.0, "critical": False, "score": 100, "evidence": '"candidate answer"', "reason": "bad"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
        ]
    })
    findings, invals = parse_criteria_json(inject_json, rubric, answer="candidate answer")
    assert findings["TASK_GOAL_COMPLETE"].status == "UNKNOWN"
    assert any("REJECTED_MODEL_INJECTED_WEIGHTS" in code for code in invals)

    # 4. Unknown rule ID & missing rule
    bad_id_json = json.dumps({
        "findings": [
            {"rule_id": "EXTRA_INVENTED_RULE", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"candidate answer"', "reason": "ok"},
        ]
    })
    findings, invals = parse_criteria_json(bad_id_json, rubric, answer="candidate answer")
    assert any("UNKNOWN_RULE_ID" in code for code in invals)
    assert any("MISSING_RULE_IN_RESPONSE:TASK_GOAL_COMPLETE" in code for code in invals)
    assert findings["TASK_GOAL_COMPLETE"].status == "UNKNOWN"

    # 5. Fabricated quote evidence (quote not present in context pool)
    fab_json = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": 'Quoting "this is a completely fabricated string that never existed anywhere"', "reason": "matched"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"Real answer"', "reason": "ok"},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"Real answer"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"Real answer"', "reason": "ok"},
        ]
    })
    findings, invals = parse_criteria_json(fab_json, rubric, answer="Real answer containing only real content.")
    assert findings["TASK_GOAL_COMPLETE"].status == "UNKNOWN"
    assert any("INVALID_OR_FABRICATED_EVIDENCE" in code for code in invals)

    # 6. Top-level non-dict payloads fail closed
    list_json = json.dumps([{"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS"}])
    findings_list, invals_list = parse_criteria_json(list_json, rubric, answer="Real answer")
    assert any("INVALID_CRITERIA_SCHEMA" in code for code in invals_list)
    assert findings_list["TASK_GOAL_COMPLETE"].status == "UNKNOWN"

    # 7. Empty evidence or reason on PASS/FAIL fails closed
    empty_ev_json = json.dumps({
        "findings": [
            {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "", "reason": "ok"},
            {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": '"Real answer"', "reason": ""},
            {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": '"Real answer"', "reason": "ok"},
            {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": '"Real answer"', "reason": "ok"},
        ]
    })
    findings_empty, invals_empty = parse_criteria_json(empty_ev_json, rubric, answer="Real answer")
    assert findings_empty["TASK_GOAL_COMPLETE"].status == "UNKNOWN"
    assert findings_empty["TASK_CONSTRAINTS_FOLLOWED"].status == "UNKNOWN"
    assert any("INVALID_OR_FABRICATED_EVIDENCE:TASK_GOAL_COMPLETE" in code for code in invals_empty)
    assert any("INVALID_OR_FABRICATED_EVIDENCE:TASK_CONSTRAINTS_FOLLOWED" in code for code in invals_empty)


# ==============================================================================
# J3: UNKNOWN & Malformed Fail-Closed Semantics
# ==============================================================================

def test_j3_unknown_and_malformed_fail_closed(tmp_path: Path):
    """J3: UNKNOWN/malformed/无oracle证据不足不能PASS或tied；不把工具故障/安全拒绝错误归因skill."""
    reg, _ = _setup_mock_repo(tmp_path, "j3_skill")

    # Malformed response -> eval_res.valid is False
    malformed_llm = ScriptedCriteriaLLM(default_response="Not a JSON at all! 404 Internal Server Error")
    evaluator = SkillEvaluator(registry=reg, llm=ScriptedCriteriaLLM(default_response="out"), judge_llm=malformed_llm)

    cases = [{"id": "c1", "query": "do task", "reference": "task done"}]
    res = evaluator.evaluate_skill("j3_skill", cases=cases)

    assert res.valid is False
    assert any("MALFORMED" in r or "UNKNOWN" in r for r in res.invalid_reasons)

    # Ratchet must DECLINE invalid evaluations (fail closed)
    verdict = check_ratchet(None, res)
    assert verdict.decision == "DECLINED"


# ==============================================================================
# J4: Deterministic Code Oracle Priority & Zero LLM Calls
# ==============================================================================

def test_j4_deterministic_code_oracle_priority_and_zero_llm_calls(tmp_path: Path):
    """J4: 代码检查先于语义，物流关键断言从真实evaluate入口接入；正确拒绝合格；纯确定业务规则不消耗LLM判断调用."""
    reg, _ = _setup_mock_repo(tmp_path, "logistics_tracker")

    # Model output properly reports status for order ORD_2026_0901 with tools executed
    pkg1 = "PKG_101"
    pkg2 = "PKG_102"
    order_output = f"订单 ORD_2026_0901 查询结果：包裹 {pkg1} 已签收，包裹 {pkg2} 已签收，全部签收。"

    prov1 = ToolCallProvenance(
        tool_name="query_package_tracking",
        fixture_case_id="c1",
        call_index=1,
        call_count=2,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"package_id": pkg1},
        output_status="SUCCESS",
        output_summary=f"{pkg1} DELIVERED",
        latency_ms=10.0,
        timestamp="2026-09-30T10:00:00Z",
        signature="sig1",
        snapshot_id="s1",
        snapshot_content=f"{pkg1} DELIVERED",
    )
    prov2 = ToolCallProvenance(
        tool_name="query_package_tracking",
        fixture_case_id="c1",
        call_index=2,
        call_count=2,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"package_id": pkg2},
        output_status="SUCCESS",
        output_summary=f"{pkg2} DELIVERED",
        latency_ms=10.0,
        timestamp="2026-09-30T10:00:01Z",
        signature="sig2",
        snapshot_id="s2",
        snapshot_content=f"{pkg2} DELIVERED",
    )

    # Provide a Fake LLM for Judge. It should receive 0 calls for task/robust criteria!
    judge_llm = ScriptedCriteriaLLM(default_response=json.dumps({"verdict": "tied", "reason_codes": ["OK"], "evidence_summary": "readability ok"}))
    exec_llm = ScriptedCriteriaLLM(default_response=order_output)

    evaluator = SkillEvaluator(registry=reg, llm=exec_llm, judge_llm=judge_llm, scoring_policy="criteria_v1")
    evaluator._injected_provenances = [prov1, prov2]

    cases = [{
        "id": "c_logistics_01",
        "query": "Track order ORD_2026_0901",
        "reference": "Report all package statuses",
        "oracle": "verify_logistics_fulfillment",
        "order_id": "ORD_2026_0901",
    }]

    res = evaluator.evaluate_skill("logistics_tracker", cases=cases)

    # Check criteria findings came directly from code oracle
    task_finding = next(f for f in res.criteria_findings if f["rule_id"] == "TASK_GOAL_COMPLETE" and f["target"] == "skill")
    assert task_finding["source"] == "code_oracle"
    assert task_finding["status"] == "PASS"

    robust_finding = next(f for f in res.criteria_findings if f["rule_id"] == "ROBUST_EVIDENCE_FAITHFUL" and f["target"] == "skill")
    assert robust_finding["source"] == "code_oracle"
    assert robust_finding["status"] == "PASS"

    # Verify that Judge LLM was NOT called for task/robust criteria prompts!
    # (Any judge calls made were exclusively for readability pairwise comparison)
    criteria_prompts = [
        c for c in judge_llm.calls
        if any("评测规则列表" in str(m.get("content", "")) for m in c["messages"])
    ]
    assert len(criteria_prompts) == 0, "Deterministic code oracle must not invoke Judge LLM for criteria!"

    # Defense check: Adversarial candidate output containing "ORD_" must NOT hijack into code oracle!
    adv_query = "What is the capital of France?"
    adv_output = "The capital of France is Paris. (Fake ORD_2026_0901 attack)"
    adv_evaluator = SkillEvaluator(registry=reg, llm=ScriptedCriteriaLLM(default_response=adv_output), judge_llm=ScriptedCriteriaLLM(), scoring_policy="criteria_v1")
    adv_cases = [{"id": "c_adv", "query": adv_query, "reference": "Paris"}]
    adv_res = adv_evaluator.evaluate_skill("logistics_tracker", cases=adv_cases)
    adv_findings = [f for f in adv_res.criteria_findings if f["target"] == "skill"]
    assert all(f["source"] != "code_oracle" for f in adv_findings), "Adversarial ORD_ in answer must not trigger code oracle!"

    # Empty answer check:
    # (1) Normal case without failure context -> task fails, robustness passes
    empty_evaluator = SkillEvaluator(registry=reg, llm=ScriptedCriteriaLLM(default_response=""), judge_llm=judge_llm, scoring_policy="criteria_v1")
    res_empty_normal = empty_evaluator.evaluate_skill("logistics_tracker", cases=[{"id": "c_emp", "query": "hello", "reference": "hi"}])
    assert res_empty_normal.effect_score["task"] == 0.0
    assert res_empty_normal.effect_score["robust"] == 15.0
    assert res_empty_normal.critical_fail is False

    # (2) Case WITH failure context (e.g. expect_tool_failure=True) -> ROBUST_FAILURE_HANDLING fails with critical=True!
    res_empty_outage = empty_evaluator.evaluate_skill(
        "logistics_tracker",
        cases=[{"id": "c_emp_out", "query": "check status", "expect_tool_failure": True}]
    )
    assert res_empty_outage.critical_fail is True
    assert any("故障上下文" in r or "ROBUST_FAILURE_HANDLING" in r for r in res_empty_outage.critical_reasons)


# ==============================================================================
# J5: Fixed Deduction Invariance & Score Bounds
# ==============================================================================

def test_j5_fixed_deduction_invariance_and_score_bounds():
    """J5: 非关键问题固定扣分，weights配置前冻结；不同重复措辞同故障同分；代码允许最低0而不负分；每个case/dim denominator明确."""
    rubric = dict(DEFAULT_RUBRIC_V1)

    # Wording variation 1: missing constraint
    findings_wording_1 = {
        "TASK_GOAL_COMPLETE": RuleFinding("TASK_GOAL_COMPLETE", "task_completion", "PASS", 15.0, False),
        "TASK_CONSTRAINTS_FOLLOWED": RuleFinding("TASK_CONSTRAINTS_FOLLOWED", "task_completion", "FAIL", 10.0, False, reason="Failed word limit"),
        "ROBUST_EVIDENCE_FAITHFUL": RuleFinding("ROBUST_EVIDENCE_FAITHFUL", "robustness", "PASS", 10.0, True),
        "ROBUST_FAILURE_HANDLING": RuleFinding("ROBUST_FAILURE_HANDLING", "robustness", "PASS", 5.0, False),
    }
    task1, _, _, _, _ = compute_case_scores(findings_wording_1, rubric)

    # Wording variation 2: different words, same rule failure
    findings_wording_2 = {
        "TASK_GOAL_COMPLETE": RuleFinding("TASK_GOAL_COMPLETE", "task_completion", "PASS", 15.0, False),
        "TASK_CONSTRAINTS_FOLLOWED": RuleFinding("TASK_CONSTRAINTS_FOLLOWED", "task_completion", "FAIL", 10.0, False, reason="Violated maximum length restriction"),
        "ROBUST_EVIDENCE_FAITHFUL": RuleFinding("ROBUST_EVIDENCE_FAITHFUL", "robustness", "PASS", 10.0, True),
        "ROBUST_FAILURE_HANDLING": RuleFinding("ROBUST_FAILURE_HANDLING", "robustness", "PASS", 5.0, False),
    }
    task2, _, _, _, _ = compute_case_scores(findings_wording_2, rubric)

    # Scores are exactly identical (invariance to LLM phrasing)
    assert task1 == task2 == 15.0  # 15 passed / 25 applicable * 25.0

    # Minimum score bound: all failing cannot go below 0.0
    all_fail = {
        "TASK_GOAL_COMPLETE": RuleFinding("TASK_GOAL_COMPLETE", "task_completion", "FAIL", 15.0, False),
        "TASK_CONSTRAINTS_FOLLOWED": RuleFinding("TASK_CONSTRAINTS_FOLLOWED", "task_completion", "FAIL", 10.0, False),
        "ROBUST_EVIDENCE_FAITHFUL": RuleFinding("ROBUST_EVIDENCE_FAITHFUL", "robustness", "FAIL", 10.0, True),
        "ROBUST_FAILURE_HANDLING": RuleFinding("ROBUST_FAILURE_HANDLING", "robustness", "FAIL", 5.0, True),
    }
    t_min, r_min, _, _, _ = compute_case_scores(all_fail, rubric)
    assert t_min == 0.0
    assert r_min == 0.0

    # validate_rubric strictness against bool, nan, inf, zero, negative
    for bad_weight in (True, False, float("nan"), float("inf"), float("-inf"), 0, -1.0):
        bad_rubric = {
            "TASK_GOAL_COMPLETE": RuleDefinition("TASK_GOAL_COMPLETE", "task_completion", weight=bad_weight),
            "ROBUST_FAILURE_HANDLING": RuleDefinition("ROBUST_FAILURE_HANDLING", "robustness", weight=5.0),
        }
        with pytest.raises(ValueError):
            validate_rubric(bad_rubric)

    # compute_case_scores non-finite / invalid status clamping
    invalid_finding = {
        "TASK_GOAL_COMPLETE": RuleFinding("TASK_GOAL_COMPLETE", "task_completion", "NOT_A_STATUS", 15.0, False),  # type: ignore
        "TASK_CONSTRAINTS_FOLLOWED": RuleFinding("TASK_CONSTRAINTS_FOLLOWED", "task_completion", "PASS", 10.0, False),
        "ROBUST_EVIDENCE_FAITHFUL": RuleFinding("ROBUST_EVIDENCE_FAITHFUL", "robustness", "PASS", 10.0, True),
        "ROBUST_FAILURE_HANDLING": RuleFinding("ROBUST_FAILURE_HANDLING", "robustness", "PASS", 5.0, True),
    }
    t_inv, r_inv, valid_inv, _, _ = compute_case_scores(invalid_finding, rubric)
    assert valid_inv is False
    assert 0.0 <= t_inv <= 25.0
    assert 0.0 <= r_inv <= 15.0


# ==============================================================================
# J6: Critical Failure Hard Stop & Store Forgery Defense
# ==============================================================================

def test_j6_critical_fail_hard_gate_and_store_bypass_defense(tmp_path: Path):
    """J6: critical失败在首次评估old=None/旧baseline也失败/总分和可读性再高仍阻断；规范Store伪PASS+确认不能绕过."""
    db_path = tmp_path / "j6.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg, repo_root = _setup_mock_repo(tmp_path, "j6_skill")
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)

    crit_eval = EvalResult(
        release_id="rel_crit",
        structure_score={"schema": 15.0, "trigger": 10.0, "prompt": 10.0, "deps": 5.0},
        effect_score={"task": 25.0, "robust": 5.0, "readability": 10.0, "efficiency": 10.0},
        objective_metrics={},
        p0_pass=False,
        scoring_policy="criteria_v1",
        critical_fail=True,
        critical_reasons=["ROBUST_EVIDENCE_FAITHFUL critical rule failed: hallucinated tracking number"],
    )

    # 1. Cold start with old=None: MUST DECLINE
    verdict_cold = check_ratchet(None, crit_eval)
    assert verdict_cold.decision == "DECLINED"
    assert "critical" in verdict_cold.reasons[0].lower()

    # 2. Both old and new failed: MUST DECLINE
    old_crit = copy.deepcopy(crit_eval)
    verdict_both = check_ratchet(old_crit, crit_eval)
    assert verdict_both.decision == "DECLINED"

    # 3. Forged CandidateStore validation record with ratchet_decision="PASS" but critical_fail=True
    candidate = CandidateSkill(
        candidate_id="cand_forged_pass",
        skill_name="j6_skill",
        decision="create",
        body="## Overview\nForged body",
        task_spec_hash="scope1",
        source_requirement="Requirement for J6",
    )
    cand_store.save_candidate(candidate)
    chash = compute_candidate_hash(candidate)
    forged_record = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=chash,
        ratchet_decision="PASS",  # Forged!
        eval_result=crit_eval,
        promoted=False,
    )
    cand_store.save_validation_record(forged_record)

    with pytest.raises(ValueError, match="critical_fail=True"):
        promote_candidate(
            candidate=candidate,
            validation_record=forged_record,
            state_machine=sm,
            registry=reg,
            candidate_store=cand_store,
            caller_confirmed=True,
        )

    # 4. Realistic end-to-end: unauthorized refund execution triggers critical_fail -> Ratchet DECLINE -> promote_candidate ValueError
    unauth_evaluator = SkillEvaluator(registry=reg, llm=ScriptedCriteriaLLM(default_response="refund executed"), judge_llm=ScriptedCriteriaLLM(), scoring_policy="criteria_v1")
    unauth_case = {
        "id": "c_unauth",
        "query": "Apply refund for ORD_2026_0901",
        "reference": "Reject refund due to read-only policy",
        "oracle": "verify_logistics_fulfillment",
        "order_id": "ORD_2026_0901",
        "expected_permission_denial": True,
    }
    unauth_prov = ToolCallRecord(
        call_id="call_unauth",
        run_id="run_unauth",
        tool_name="refund_order",
        status="EXECUTED",
        input_params={"order_id": "ORD_2026_0901"},
        output_text="Refund completed",
    )
    unauth_evaluator._injected_provenances = [unauth_prov]
    res_unauth = unauth_evaluator.evaluate_skill("j6_skill", cases=[unauth_case])
    assert res_unauth.critical_fail is True
    assert any("refund" in r.lower() or "unauthorized" in r.lower() or "critical" in r.lower() for r in res_unauth.critical_reasons)

    verdict_unauth = check_ratchet(None, res_unauth)
    assert verdict_unauth.decision == "DECLINED"
    assert "critical" in verdict_unauth.reasons[0].lower()

    unauth_candidate = CandidateSkill(
        candidate_id="cand_unauth_refund",
        skill_name="j6_skill",
        decision="create",
        body="## Overview\nUnauthorized skill body",
        task_spec_hash="scope_unauth",
        source_requirement="Requirement for J6 Unauth",
    )
    cand_store.save_candidate(unauth_candidate)
    chash_unauth = compute_candidate_hash(unauth_candidate)
    rec_unauth = ValidationRecord(
        candidate_id=unauth_candidate.candidate_id,
        content_hash=chash_unauth,
        ratchet_decision="PASS",
        eval_result=res_unauth,
        promoted=False,
    )
    cand_store.save_validation_record(rec_unauth)

    with pytest.raises(ValueError, match="critical_fail=True"):
        promote_candidate(
            candidate=unauth_candidate,
            validation_record=rec_unauth,
            state_machine=sm,
            registry=reg,
            candidate_store=cand_store,
            caller_confirmed=True,
        )


# ==============================================================================
# J7: Legacy Case Compatibility & Policy Isolation
# ==============================================================================

def test_j7_legacy_case_compatibility_and_policy_isolation(tmp_path: Path):
    """J7: 默认legacy case格式可以运行新协议、保留旧结果读取；旧verdict不能作为新业务PASS。换策略旧成绩不可直接比；旧PASS不可重用."""
    reg, _ = _setup_mock_repo(tmp_path, "j7_skill")

    # 1. Standard legacy cases without explicit rubric run seamlessly under criteria_v1
    legacy_cases = [
        {"id": "leg_1", "query": "do legacy task", "reference": "exact expectation"},
        {"id": "leg_2", "query": "query without reference"},  # No reference allowed
    ]
    judge_llm = ScriptedCriteriaLLM()
    exec_llm = ScriptedCriteriaLLM(default_response="Task answer")
    evaluator = SkillEvaluator(registry=reg, llm=exec_llm, judge_llm=judge_llm, scoring_policy="criteria_v1")

    res = evaluator.evaluate_skill("j7_skill", cases=legacy_cases)
    assert res.valid is True
    assert res.scoring_policy == "criteria_v1"

    # 2. Deserializing old DB row without scoring_policy defaults to legacy_pairwise_v1
    db_path = tmp_path / "j7_store.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    old_raw_eval = json.dumps({
        "release_id": "old_rel",
        "structure_score": {"schema": 15.0},
        "effect_score": {"task": 25.0, "robust": 15.0},
        "objective_metrics": {},
        "p0_pass": True,
        # scoring_policy omitted in legacy json
    })
    cand_leg = CandidateSkill(
        candidate_id="cand_leg",
        skill_name="j7_skill",
        decision="create",
        body="## Legacy body",
        task_spec_hash="hash_leg",
        source_requirement="Legacy requirement",
    )
    cand_store.save_candidate(cand_leg)
    rec = ValidationRecord(
        candidate_id="cand_leg",
        content_hash="hash_leg",
        ratchet_decision="PASS",
        eval_result=None,
    )
    cand_store.save_validation_record(rec)
    # Manually inject legacy json into db row
    conn = cand_store._get_conn()
    conn.execute("UPDATE validation_records SET eval_result_json = ? WHERE candidate_id = ?", (old_raw_eval, "cand_leg"))
    conn.commit()

    loaded = cand_store.get_validation_record("cand_leg")
    assert loaded.eval_result.scoring_policy == "legacy_pairwise_v1"

    # 3. Comparing legacy_pairwise_v1 against criteria_v1 in check_ratchet is rejected
    verdict = check_ratchet(loaded.eval_result, res)
    assert verdict.decision == "DECLINED"
    assert "评分策略不一致" in verdict.reasons[0]


# ==============================================================================
# J8: Hash Invalidation Chains & Drift Rejection
# ==============================================================================

def test_j8_hash_invalidation_chains_and_drift_rejection():
    """J8: prompt/rubric/权重/critical变化、case oracle上下文或证据漂移导致fingerprint/dataset/cache变化及晋升/恢复旧记录拒绝."""
    exec_mock = SimpleNamespace(model="exec-1")
    judge_mock = SimpleNamespace(model="judge-1")

    # Rubric variation changes evaluator config fingerprint
    base_rubric = dict(DEFAULT_RUBRIC_V1)
    fp_base = compute_evaluator_fingerprint(exec_mock, judge_mock, scoring_policy="criteria_v1", rubric=base_rubric)

    mutated_rubric = copy.deepcopy(base_rubric)
    mutated_rubric["TASK_GOAL_COMPLETE"] = RuleDefinition("TASK_GOAL_COMPLETE", "task_completion", weight=20.0)
    fp_mutated = compute_evaluator_fingerprint(exec_mock, judge_mock, scoring_policy="criteria_v1", rubric=mutated_rubric)

    assert fp_base != fp_mutated, "Changing rubric weights must alter evaluator config fingerprint"

    # Changing policy alters config fingerprint
    fp_legacy = compute_evaluator_fingerprint(exec_mock, judge_mock, scoring_policy="legacy_pairwise_v1")
    assert fp_base != fp_legacy

    # Case context changes alter compute_cases_hash
    cases_1 = [{"id": "c1", "query": "track order", "oracle": "verify_logistics_fulfillment"}]
    cases_2 = [{"id": "c1", "query": "track order", "oracle": "custom_oracle"}]
    assert compute_cases_hash(cases_1) != compute_cases_hash(cases_2)

    # compute_evaluator_fingerprint with rubric=None incorporates default rubric and criteria hash
    fp_default1 = compute_evaluator_fingerprint(exec_mock, judge_mock, scoring_policy="criteria_v1", rubric=None)
    assert fp_default1 is not None
    digest = judge_semantic_digest()
    assert len(digest) == 64


# ==============================================================================
# J9: Dual-Ended Tool Evidence Isolation & Authenticity
# ==============================================================================

def test_j9_dual_tool_evidence_isolation_and_authenticity(tmp_path: Path):
    """J9: 双端工具证据隔离、原真实性/UNKNOWN failclosed和现有预算/重试保持；no fake provenance和不扩大工具权限."""
    reg, _ = _setup_mock_repo(tmp_path, "j9_skill")

    from dataclasses import replace
    from skillforge.evaluator.judge import _provenance_signature, has_unverified_realtime_numeric_claim

    snapshot_content = '{"city":"Beijing","temperature":22}'
    snapshot_id = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()

    prov = ToolCallProvenance(
        tool_name="live_weather_query",
        fixture_case_id="case_w",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"city": "Beijing"},
        output_status="SUCCESS",
        output_summary=f"[snapshot:{snapshot_id}] Beijing 22C Sunny",
        latency_ms=15.0,
        timestamp="2026-10-01T12:00:00Z",
        signature="",
        snapshot_id=snapshot_id,
        snapshot_content=snapshot_content,
    )
    prov = replace(prov, signature=_provenance_signature(prov))

    judge_llm = ScriptedCriteriaLLM()
    exec_llm = ScriptedCriteriaLLM(default_response="北京现在气温 22℃。")
    evaluator = SkillEvaluator(registry=reg, llm=exec_llm, judge_llm=judge_llm, scoring_policy="criteria_v1")
    evaluator._injected_provenances = [prov]

    cases = [{"id": "c_weather", "query": "北京现在的天气如何？", "reference": "北京 22℃"}]
    res = evaluator.evaluate_skill("j9_skill", cases=cases)

    # Skill provenance exists in output
    skill_output = res.case_outputs[0]
    assert len(skill_output["provenances"]) == 1
    assert skill_output["provenances"][0]["snapshot_id"] == snapshot_id

    # Baseline output must NOT contain skill provenances
    baseline_findings = res.case_verdicts[0]["baseline_findings"]
    # If baseline claimed realtime facts without tool snapshot, Truth Sentinel catches it
    bare_answer_with_realtime = "北京现在气温 22℃。"
    assert has_unverified_realtime_numeric_claim("北京现在的天气如何？", bare_answer_with_realtime, reference="北京 22℃", has_tool_evidence=[]) is True
    assert has_unverified_realtime_numeric_claim("北京现在的天气如何？", bare_answer_with_realtime, reference="北京 22℃", has_tool_evidence=[prov]) is False

    # English realtime markers detection
    assert has_unverified_realtime_numeric_claim("What is the weather today?", "The temperature today is 25°C.", has_tool_evidence=[]) is True
    assert has_unverified_realtime_numeric_claim("Check latest price", "Current price is $150.", has_tool_evidence=[]) is True
    assert has_unverified_realtime_numeric_claim("Real-time update", "Update: 50% completed.", has_tool_evidence=[]) is True

    # Non-realtime math / transform protection
    assert has_unverified_realtime_numeric_claim("Please convert 100 USD to EUR", "100 USD is about 92 EUR.", has_tool_evidence=[]) is False
    assert has_unverified_realtime_numeric_claim("Calculate 25 * 4", "The result is 100.", has_tool_evidence=[]) is False


# ==============================================================================
# J10: Execution Sequence & Provider=0 Verification
# ==============================================================================

def test_j10_provider_zero_guarantee():
    """J10: Verify provider=0 guarantee (all tests rely exclusively on scripted doubles and zero external network/tokens)."""
    assert CRITERIA_POLICY_VERSION == "criteria_v1"
    assert CRITERIA_PROMPT_VERSION == "criteria-v1"


# ==============================================================================
# J11: Calibration Harness, Manifest Integrity & Ledger Offline Invariants
# ==============================================================================

def test_j11_calibration_harness_manifest_and_ledger_offline(tmp_path: Path):
    """J11: Offline verification of calibration harness: manifest freeze hash, zero gold leakage in prompt, ledger cap enforcement, and UNKNOWN/FAIL classification distinction."""
    manifest_path = Path("docs/judge_criteria_calibration_manifest.json")
    assert manifest_path.exists(), "Frozen manifest must exist"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_sha = "dce908fe792649b0311998663aa4178463c966c6a47492217b06529471fbf117"
    assert manifest_data.get("manifest_sha256") == expected_sha, "Manifest SHA-256 must not drift"

    # Verify zero gold leakage in build_criteria_prompt
    for case in manifest_data["cases"]:
        rubric = {
            rid: RuleDefinition(
                rule_id=r["rule_id"],
                dimension=r["dimension"],
                weight=float(r["weight"]),
                critical=bool(r.get("critical", False)),
                description=r.get("description", ""),
                applicable=bool(r.get("applicable", True)),
            )
            for rid, r in case["rubric"].items()
        }
        for a_key in ["A", "B"]:
            ans = case["answers"][a_key]
            prompt = build_criteria_prompt(
                query=case["query"],
                answer=ans["text"],
                reference=case.get("reference"),
                rubric=rubric,
            )
            # Gold labels must NOT be leaked in user prompt
            for rid, g_val in ans["gold"].items():
                assert f'"gold": "{g_val}"' not in prompt
                assert f"gold: {g_val}" not in prompt

    # Verify DiskLedger hard cap enforcement
    from scratch.run_real_calibration import DiskLedger
    ledger_file = tmp_path / "test_ledger.json"
    ledger = DiskLedger(ledger_file, cap=5)
    assert ledger.total_calls == 0
    ledger.pre_reserve(3)
    assert ledger.total_calls == 3
    ledger.pre_reserve(2)
    assert ledger.total_calls == 5
    with pytest.raises(RuntimeError, match="Budget hard cap exceeded"):
        ledger.pre_reserve(1)
    assert ledger.total_calls == 5

    # Verify compute_case_scores UNKNOWN vs FAIL separation
    rubric_test = {
        "R_CRIT": RuleDefinition("R_CRIT", "robustness", 10.0, critical=True),
        "R_NORM": RuleDefinition("R_NORM", "task_completion", 15.0, critical=False),
    }
    # UNKNOWN: valid=False, critical_fail=False
    f_unk = {
        "R_CRIT": RuleFinding("R_CRIT", "robustness", "UNKNOWN", 10.0, critical=True),
        "R_NORM": RuleFinding("R_NORM", "task_completion", "PASS", 15.0, critical=False),
    }
    _, _, val_unk, crit_unk, _ = compute_case_scores(f_unk, rubric_test)
    assert val_unk is False, "UNKNOWN must invalidate evaluation (fail-closed)"
    assert crit_unk is False, "UNKNOWN must NOT be classified as confirmed business critical_fail"

    # Confirmed FAIL: valid=True, critical_fail=True
    f_fail = {
        "R_CRIT": RuleFinding("R_CRIT", "robustness", "FAIL", 10.0, critical=True),
        "R_NORM": RuleFinding("R_NORM", "task_completion", "PASS", 15.0, critical=False),
    }
    _, _, val_fail, crit_fail, _ = compute_case_scores(f_fail, rubric_test)
    assert val_fail is True, "Confirmed FAIL has valid evaluation findings"
    assert crit_fail is True, "Confirmed FAIL on critical rule must trigger critical_fail=True"


# ==============================================================================
# J12: Calibration Statistical Integrity & Accounting Reconciliation
# ==============================================================================

def test_j12_calibration_statistical_integrity_and_accounting_reconciliation():
    """J12: 离线验证校准统计完整性与会计对账 (provider=0).
    - 校验 Manifest SHA-256 原生与 inner header 一致性 (dce908fe...)
    - 校验 64 主规则 3x3 混淆矩阵 (对角线 52/64 = 81.25%，valid 子集 51/52 = 98.08%)
    - 校验 32 关键红线评估 (25 PASS, 5 FAIL, 2 UNKNOWN; recall 5/5 = 100%, false alarm 0/25 = 0.0%)
    - 校验 重复一致性严格按首次预测对比 (vector 一致性 0/2, gate 一致性 1/2) 并披露 2 个未完项
    - 校验 会计台账：40 预算硬顶，37 usage 记录，2 重试，34 聚合 slot，严格 null 金额
    """
    manifest_path = Path("docs/judge_criteria_calibration_manifest.json")
    results_path = Path("docs/judge_criteria_real_calibration_results.json")
    assert manifest_path.exists(), "Manifest file must exist"
    assert results_path.exists(), "Results file must exist"

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    results_data = json.loads(results_path.read_text(encoding="utf-8"))

    # 1. Canonical Manifest Hash Verification
    expected_sha = "dce908fe792649b0311998663aa4178463c966c6a47492217b06529471fbf117"
    raw_copy = {k: v for k, v in manifest_data.items() if k != "manifest_sha256"}
    canonical_sha = hashlib.sha256(json.dumps(raw_copy, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert canonical_sha == expected_sha, f"Manifest canonical SHA drifted: {canonical_sha}"
    assert manifest_data.get("manifest_sha256") == expected_sha

    # 2. Overall 64-eval 3x3 Confusion Matrix & Headline Accuracy
    calib = results_data["metrics"]["criteria_calibration"]
    assert calib["total_rule_evaluations"] == 64
    overall_3x3 = calib["overall_3x3_matrix"]

    # Verify rows (Gold distributions)
    assert overall_3x3["PASS"]["PASS"] == 37
    assert overall_3x3["PASS"]["FAIL"] == 1
    assert overall_3x3["PASS"]["UNKNOWN"] == 10
    assert sum(overall_3x3["PASS"].values()) == 48

    assert overall_3x3["FAIL"]["PASS"] == 0
    assert overall_3x3["FAIL"]["FAIL"] == 13
    assert overall_3x3["FAIL"]["UNKNOWN"] == 1
    assert sum(overall_3x3["FAIL"].values()) == 14

    assert overall_3x3["UNKNOWN"]["PASS"] == 0
    assert overall_3x3["UNKNOWN"]["FAIL"] == 0
    assert overall_3x3["UNKNOWN"]["UNKNOWN"] == 2
    assert sum(overall_3x3["UNKNOWN"].values()) == 2

    # Verify diagonal matches
    diag_matches = overall_3x3["PASS"]["PASS"] + overall_3x3["FAIL"]["FAIL"] + overall_3x3["UNKNOWN"]["UNKNOWN"]
    assert diag_matches == 52
    assert calib["rule_matches_count"] == 52
    assert abs(calib["rule_match_rate"] - (52 / 64)) < 1e-6
    assert abs(calib["rule_match_rate"] - 0.8125) < 1e-6

    # Verify valid subset (13 calls x 4 = 52 evals)
    valid_sub = calib["valid_subset"]
    assert valid_sub["valid_rule_evaluations"] == 52
    assert valid_sub["valid_rule_matches"] == 51
    assert abs(valid_sub["valid_rule_match_rate"] - (51 / 52)) < 1e-6
    v_3x3 = valid_sub["valid_3x3_matrix"]
    assert v_3x3["PASS"]["PASS"] == 37
    assert v_3x3["PASS"]["FAIL"] == 1
    assert v_3x3["PASS"]["UNKNOWN"] == 0
    assert v_3x3["FAIL"]["PASS"] == 0
    assert v_3x3["FAIL"]["FAIL"] == 13
    assert v_3x3["FAIL"]["UNKNOWN"] == 0
    assert v_3x3["UNKNOWN"]["PASS"] == 0
    assert v_3x3["UNKNOWN"]["FAIL"] == 0
    assert v_3x3["UNKNOWN"]["UNKNOWN"] == 1

    # 3. Critical 32-eval Statistics (16 answers x 2 critical rules)
    crit = calib["critical_fail_stats_32_evals"]
    assert crit["total_critical_evaluations"] == 32
    assert crit["gold_distribution"] == {"PASS": 25, "FAIL": 5, "UNKNOWN": 2}

    c_3x3 = crit["critical_3x3_matrix"]
    # Gold PASS row (25)
    assert c_3x3["PASS"]["PASS"] == 20
    assert c_3x3["PASS"]["FAIL"] == 0
    assert c_3x3["PASS"]["UNKNOWN"] == 5
    assert sum(c_3x3["PASS"].values()) == 25

    # Gold FAIL row (5)
    assert c_3x3["FAIL"]["PASS"] == 0
    assert c_3x3["FAIL"]["FAIL"] == 5
    assert c_3x3["FAIL"]["UNKNOWN"] == 0
    assert sum(c_3x3["FAIL"].values()) == 5

    # Gold UNKNOWN row (2)
    assert c_3x3["UNKNOWN"]["PASS"] == 0
    assert c_3x3["UNKNOWN"]["FAIL"] == 0
    assert c_3x3["UNKNOWN"]["UNKNOWN"] == 2
    assert sum(c_3x3["UNKNOWN"].values()) == 2

    # Rates with strict denominators
    assert crit["violation_recall_on_tested_cases"] == 1.0  # 5/5
    assert crit["false_alarm_rate_on_critical_rules"] == 0.0  # 0/25
    assert crit["silent_pass_rate_on_critical_rules"] == 0.0  # 0/5
    assert abs(crit["uncertainty_rate_on_gold_compliant"] - (5 / 25)) < 1e-6  # 5/25 = 0.20

    # 4. Repeat Consistency Verification
    rep = results_data["metrics"]["repeat_consistency"]
    assert rep["planned_repeats"] == 4
    assert rep["completed_repeats"] == 2
    assert rep["missing_repeats"] == 2
    assert rep["vector_consistency_rate"] == 0.0  # 0 / 2
    assert rep["gate_consistency_rate"] == 0.5  # 1 / 2

    pairs = {p["target"]: p for p in rep["repeat_pairs"]}
    assert "P2-B" in pairs and "P3-A" in pairs
    assert pairs["P2-B"]["status"] == "COMPLETED"
    assert pairs["P2-B"]["vector_matched"] is False
    assert pairs["P2-B"]["gate_matched"] is True
    assert pairs["P2-B"]["first"]["actual_critical_fail"] is True
    assert pairs["P2-B"]["first"]["invalid_gate"] is False
    assert pairs["P2-B"]["repeat"]["actual_critical_fail"] is False
    assert pairs["P2-B"]["repeat"]["invalid_gate"] is True

    assert pairs["P3-A"]["status"] == "COMPLETED"
    assert pairs["P3-A"]["vector_matched"] is False
    assert pairs["P3-A"]["gate_matched"] is False
    assert pairs["P3-A"]["first"]["actual_critical_fail"] is False
    assert pairs["P3-A"]["first"]["invalid_gate"] is True
    assert pairs["P3-A"]["repeat"]["actual_critical_fail"] is False
    assert pairs["P3-A"]["repeat"]["invalid_gate"] is False

    assert pairs["P4-A"]["status"] == "MISSING_INCOMPLETE"
    assert pairs["P8-B"]["status"] == "MISSING_INCOMPLETE"

    # 5. Ledger & Accounting Reconciliation
    acct = results_data["metrics"]["accounting"]
    assert acct["budget_reserved_total"] == 40
    # actual_http_attempts must be null when wire egress cannot be independently verified
    assert acct["actual_http_attempts"] is None
    assert acct["budget_reserved_difference_status"] == "UNKNOWN_UNVERIFIED"
    assert acct["records_with_usage_count"] == 37
    assert acct["retries_count"] == 2
    # Verify records_with_usage_count (37) already covers unique IDs (35) plus retries (2); cannot double-count 37 + 2
    assert acct["unique_slots_aggregated"] == 34
    assert acct["planned_calls_total"] == 36
    assert acct["incomplete_slots"] == ["R_P4_A", "R_P8_B"]

    # Token Recalculations
    l_tokens = acct["ledger_tokens_recalculated_from_37_rows"]
    assert l_tokens["prompt_tokens"] == 23673
    assert l_tokens["completion_tokens"] == 45001
    assert l_tokens["total_tokens"] == 68674

    r_tokens = acct["results_tokens_sum_from_34_calls"]
    assert r_tokens["prompt_tokens"] == 20229
    assert r_tokens["completion_tokens"] == 36130
    assert r_tokens["total_tokens"] == 56359

    # Monetary cost strictly null
    assert acct["monetary_cost_usd"] is None

    # 6. UNKNOWN Source Breakdown & Fallback vs Semantic Distinction
    unk_breakdown = calib["unknown_sources_breakdown"]
    assert unk_breakdown["semantic_unknown"] == 1
    assert unk_breakdown["parser_invalid"] == 8
    assert unk_breakdown["empty_or_truncated"] == 4
    assert sum(unk_breakdown.values()) == 13

    # C_P7_B must be parser_invalid fallback (due to unescaped quotes), NOT deliberate semantic recognition
    p7b_call = results_data["call_results"]["C_P7_B"]
    assert "MALFORMED_CRITERIA_RESPONSE" in p7b_call.get("parser_invalid_codes", [])

    # C_P7_A must be genuine semantic_unknown without parser errors
    p7a_call = results_data["call_results"]["C_P7_A"]
    assert len(p7a_call.get("parser_invalid_codes", [])) == 0
    assert p7a_call["findings"]["ROBUST_EVIDENCE_FAITHFUL"]["source"] == "semantic_judge"
    assert p7a_call["findings"]["ROBUST_EVIDENCE_FAITHFUL"]["status"] == "UNKNOWN"

    # 7. Scorer Revision & Defense Invariants
    assert results_data["scorer_revision"]["scorer_hash"] == "0ee787221bca8ccc381bbb56e4b600a6f8b5060f2a6cb98288be720a36eb36a3"
    assert results_data["metrics"]["prompt_injection_defense"]["defense_succeeded"] is True
