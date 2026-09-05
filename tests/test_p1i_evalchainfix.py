"""P1-I evalchainfix verification tests:
1. [A case-level fault tolerance]
2. [B Judge retry]
3. [C tool snapshot binding]
4. [D effective failure semantics]
5. [E data acceptance across repair_set 22 cases]
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from skillforge import SkillRegistry, SkillEvaluator, SkillEvolver
from skillforge.evaluator.fixtures import (
    AmapWeatherFixture,
    _weather_agent_output_matches,
    _weather_query_intent,
    build_provenances_for_fixture,
    create_nonce_weather_fixture,
)
from skillforge.evaluator.judge import (
    PairwiseJudge,
    JudgeResult,
    JUDGE_PARSER_CONTRACT,
    _has_authentic_tool_evidence,
)
from skillforge.evolver import (
    _collect_failures,
    _is_judge_infrastructure_error,
    _is_effective_failure,
    EvolveBudget,
    EvolveOutcome,
)
from skillforge.models import EvalResult, ToolCallProvenance
from skillforge.evolver import Failure


class FakeScriptedLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self.responses:
            res = self.responses.pop(0)
        else:
            res = json.dumps({
                "verdict": "tied",
                "reason_codes": ["EVIDENCE_SUFFICIENT"],
                "evidence_summary": "default tied",
            })
        return SimpleNamespace(content=res, usage=None)


def test_judge_retry_on_malformed():
    """【B Judge 重试】Judge 调用输出 MALFORMED 自动重试 1-2 次，重试间不换 prompt。"""
    # 第一次输出格式坏（非 JSON），第二次输出合法 JSON
    malformed_output = "I think answer A is better because..."
    valid_output = json.dumps({
        "verdict": "A_better",
        "reason_codes": ["EVIDENCE_SUFFICIENT"],
        "evidence_summary": "A 给出明确论证",
    })
    llm = FakeScriptedLLM([malformed_output, valid_output])
    judge = PairwiseJudge(llm, max_retries=2)

    res = judge.compare_detailed(
        query="讲讲 \\1 反向引用",
        output_a="A 的回答",
        output_b="B 的回答",
        dimension="task_completion",
    )
    assert res.verdict == "A_better"
    assert "EVIDENCE_SUFFICIENT" in res.reason_codes
    # 验证确实发生了重试（调用了 2 次）
    assert len(llm.calls) == 2
    # 验证重试间 prompt 不换
    assert llm.calls[0] == llm.calls[1]


def test_judge_retry_exhaustion_returns_malformed():
    """【B Judge 重试】多次重试仍 MALFORMED 则返回 INVALID，走 A 的容错。"""
    llm = FakeScriptedLLM(["bad json 1", "bad json 2", "bad json 3"])
    judge = PairwiseJudge(llm, max_retries=2)

    res = judge.compare_detailed(
        query="讲讲 \\1 反向引用",
        output_a="A 的回答",
        output_b="B 的回答",
        dimension="task_completion",
    )
    assert res.verdict == "INVALID"
    assert "MALFORMED_JUDGE_RESPONSE" in res.reason_codes
    assert len(llm.calls) == 3  # 1次原始 + 2次重试


def test_tool_snapshot_binding_and_flexible_date_matching():
    """【C 工具快照绑定】验证自然语言日期匹配（今天/明天/后天/9月6日）与快照 ID 记录。"""
    fixture = create_nonce_weather_fixture(nonce="test_snapshot_nonce")
    resp = fixture.run_with_timing({"city": "南京市", "extensions": "all"})
    casts = resp.data["forecasts"][0]["casts"]

    # 模拟 Agent 用自然语言表达多天天气（无精确 ISO YYYY-MM-DD）
    agent_output = (
        f"南京今天气温{casts[0]['daytemp']}-{casts[0]['nighttemp']}度；"
        f"明天天气{casts[1]['dayweather']}，气温{casts[1]['daytemp']}-{casts[1]['nighttemp']}度；"
        f"后天气温{casts[2]['daytemp']}-{casts[2]['nighttemp']}度。穿衣建议分层搭配。"
    )

    provs = build_provenances_for_fixture(
        dependency="amap_weather_api",
        fixture=fixture,
        agent_output=agent_output,
        skill_body="## Instructions\n查询天气",
        query="我要去南京出差，看下这几天穿衣建议",
    )
    assert len(provs) == 1
    p = provs[0]
    assert p.tool_called is True
    assert p.authenticity_pass is True
    assert p.output_status == "SUCCESS"
    assert "[snapshot:" in p.output_summary  # 快照 ID 已记录
    assert p.snapshot_id
    assert json.loads(p.snapshot_content)["forecasts"][0]["city"] == "南京市"
    assert _has_authentic_tool_evidence(p) is True

    # Relative markers are accepted, but an explicit date outside the requested
    # forecast window must invalidate the tool-backed answer.
    tomorrow = casts[1]
    today_label = f"{date.today().month}月{date.today().day}日"
    wrong_date_output = (
        f"南京市明天（{today_label}）天气{tomorrow['dayweather']}"
        f"{tomorrow['nightweather']} {tomorrow['daytemp']}-{tomorrow['nighttemp']}"
        f" {tomorrow['daywind']} {tomorrow['daypower']}"
    )
    assert _weather_agent_output_matches(
        wrong_date_output, resp, query="南京明天天气"
    ) is False

    # A changed response body cannot retain an authentic snapshot binding.
    assert _has_authentic_tool_evidence(
        replace(p, snapshot_content='{"forged":true}')
    ) is False


def test_effective_failure_classification_semantics():
    """【D 有效失败语义】区分 Judge 基础设施异常与模型真实差/编造。"""
    # 基础设施异常
    assert _is_judge_infrastructure_error(["MALFORMED_JUDGE_RESPONSE"], "INVALID", {}) is True
    assert _is_judge_infrastructure_error(["JUDGE_CALL_FAILED"], "INVALID", {}) is True
    assert _is_judge_infrastructure_error(["TIMEOUT"], "TIMEOUT", {}) is True

    # Reference 模糊导致 Judge 拒判，不足以证明 baseline 真实失败。
    assert _is_effective_failure(["INSUFFICIENT_EVIDENCE"], "INVALID", {}) is False
    assert _is_effective_failure(["EVIDENCE_INSUFFICIENT"], "INVALID", {}) is False

    # 只有明确侧别且该侧是 baseline 的外部事实失真才算有效失败。
    baseline_on_b = {"presented_order": {"A": "skill", "B": "baseline"}}
    assert _is_effective_failure(
        ["UNVERIFIED_EXTERNAL_FACT_B"], "INVALID", baseline_on_b
    ) is True
    assert _is_effective_failure(
        ["UNVERIFIED_EXTERNAL_FACT_A"], "INVALID", baseline_on_b
    ) is False
    assert _is_effective_failure(
        ["REFERENCE_UNVERIFIED"], "INVALID", baseline_on_b
    ) is False

    # 基础设施异常绝不是有效失败
    assert _is_effective_failure(["MALFORMED_JUDGE_RESPONSE"], "INVALID", {}) is False


def test_isolation_assertion_and_effective_failure_in_collect_failures():
    """【红线隔离断言 & D 项】跳过的 case 绝对不进 failures；有效失败必须进 failures。"""
    mock_result = EvalResult(
        release_id="rel_test",
        structure_score={"s": 10.0},
        effect_score={"task": 10.0, "robust": 10.0, "readability": 10.0, "efficiency": 5.0},
        objective_metrics={},
        p0_pass=True,
        valid=False,
        case_verdicts=[
            # Case 1: 正常 B_better 失败
            {"case_id": "case_normal_fail", "task_completion": "B_better", "query": "q1"},
            # Case 2: 基础设施异常被跳过的 case (er_h02 型)
            {
                "case_id": "case_skipped_infra",
                "task_completion": "INVALID",
                "query": "q2",
                "judge_audit": {
                    "task_completion": {"reason_codes": ["MALFORMED_JUDGE_RESPONSE"], "status": "ERROR"}
                },
            },
            # Case 3: 有效失败 (wr_d01 / wr_d11 型)
            {
                "case_id": "case_effective_fail",
                "task_completion": "INVALID",
                "query": "q3",
                "judge_audit": {
                    "task_completion": {
                        "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_B"],
                        "presented_order": {"A": "skill", "B": "baseline"},
                    }
                },
            },
        ],
        case_outputs=[
            {"case_id": "case_normal_fail", "query": "q1", "output_skill": "s1", "output_baseline": "b1"},
            {"case_id": "case_skipped_infra", "query": "q2", "output_skill": "s2", "output_baseline": "b2"},
            {"case_id": "case_effective_fail", "query": "q3", "output_skill": "s3", "output_baseline": "b3"},
        ],
    )

    failures = _collect_failures(
        mock_result,
        effective_failed_case_ids={"case_effective_fail"},
        skipped_case_ids={"case_skipped_infra"},
    )

    fail_ids = {f.case_id for f in failures}
    # 隔离断言：跳过的基础设施异常 case 严禁回流
    assert "case_skipped_infra" not in fail_ids
    # 正常 B_better 必须收集
    assert "case_normal_fail" in fail_ids
    # D 项有效失败必须进入修复流
    assert "case_effective_fail" in fail_ids
    eff_f = next(f for f in failures if f.case_id == "case_effective_fail")
    assert "task_completion" in eff_f.losing_dims


def test_baseline_case_fault_tolerance_evolve_full_mock(tmp_path):
    """【A case 级容错】非关键 case 偶发 invalid 跳过不报废整次；P0 或超阈值则停止。"""
    repo_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(db_path=tmp_path / "test.db", skills_dir=repo_root / "skills", repo_root=repo_root)
    registry.load_skills_from_dir()

    class MockEvaluator:
        def __init__(self, case_verdicts, valid=True, invalid_reasons=None):
            self.case_verdicts = case_verdicts
            self.valid = valid
            self.invalid_reasons = invalid_reasons or []
            self.structure_score = {"structure": 30.0}
            self.effect_score = {"task": 20.0, "robust": 15.0, "readability": 10.0, "efficiency": 5.0}
            self.registry = registry

        def evaluate_skill(self, *args, **kwargs):
            return EvalResult(
                release_id="mock_baseline",
                structure_score=self.structure_score,
                effect_score=self.effect_score,
                objective_metrics={},
                p0_pass=True,
                case_verdicts=self.case_verdicts,
                case_outputs=[{"case_id": cv["case_id"], "query": "q"} for cv in self.case_verdicts],
                valid=self.valid,
                invalid_reasons=self.invalid_reasons,
            )

    class MockEvolverLLM:
        model = "mock-model"
        def invoke(self, *args, **kwargs):
            return SimpleNamespace(content='{"trigger_inaccurate":{"prob":0.1,"why":""},"prompt_vague":{"prob":0.9,"why":"vague"},"deps_broken":{"prob":0.0,"why":""},"boundary_missing":{"prob":0.0,"why":""},"eval_noise":{"prob":0.0,"why":""}}', usage=None)

    # 场景 1: 1 个非 P0 case (er_h02) 发生 MALFORMED 基础设施异常
    # 22 个 case 中只有 1 个跳过 (1/22 = 4.5% <= 20%)，非 P0 -> 应该容错跳过，不报废整次！
    cases_22 = []
    for i in range(21):
        cid = f"test_c_{i}"
        cases_22.append({
            "case_id": cid,
            "task_completion": "B_better" if i == 0 else "tied",
            "robustness": "tied",
            "readability": "tied",
            "judge_audit": {},
        })
    cases_22.append({
        "case_id": "er_h02",  # 非 P0 用例
        "task_completion": "INVALID",
        "robustness": "tied",
        "readability": "tied",
        "judge_audit": {
            "task_completion": {"reason_codes": ["MALFORMED_JUDGE_RESPONSE"], "status": "ERROR"}
        },
    })

    mock_eval = MockEvaluator(cases_22, valid=False, invalid_reasons=["er_h02/task_completion: MALFORMED_JUDGE_RESPONSE"])
    evolver = SkillEvolver(
        registry=registry,
        evaluator=mock_eval,
        llm=MockEvolverLLM(),
    )

    budget = EvolveBudget(
        enable_reflection=False,
        enable_a2=False,
        shadow_mode=True,
        invalid_case_ratio_threshold=0.20,
        p0_fail_on_invalid=True,
        critical_case_ids=["er_d01", "er_d02", "er_d04"],  # er_h02 不在 P0 中
    )

    outcome = evolver.evolve_full(
        skill_name="explain_regex",
        eval_set_for_iter="repair_set",
        budget=budget,
        verbose=False,
    )

    # 容错验证：不报废整次 evolve
    assert outcome.error is None or "baseline 评估无效" not in outcome.error
    assert len(outcome.skipped_cases) == 1
    assert outcome.skipped_cases[0]["case_id"] == "er_h02"

    # 场景 2: P0 关键 case (er_d01) 发生基础设施异常 -> 必须坚决停止整次 evolve
    cases_p0_bad = list(cases_22)
    cases_p0_bad[0] = {
        "case_id": "er_d01",  # P0 关键用例！
        "task_completion": "INVALID",
        "robustness": "tied",
        "readability": "tied",
        "judge_audit": {
            "task_completion": {"reason_codes": ["JUDGE_CALL_FAILED"], "status": "ERROR"}
        },
    }
    mock_eval_p0 = MockEvaluator(cases_p0_bad, valid=False, invalid_reasons=["er_d01/task_completion: JUDGE_CALL_FAILED"])
    evolver_p0 = SkillEvolver(
        registry=registry,
        evaluator=mock_eval_p0,
        llm=MockEvolverLLM(),
    )
    outcome_p0 = evolver_p0.evolve_full(
        skill_name="explain_regex",
        eval_set_for_iter="repair_set",
        budget=budget,
        verbose=False,
    )
    assert outcome_p0.error is not None
    assert "P0 关键用例 'er_d01' 发生 Judge/基础设施异常" in outcome_p0.error

    # 场景 3: 异常 case 数超阈值（如 5/22 > 20%）-> 必须停止整次 evolve
    cases_overflow = []
    for i in range(22):
        cid = f"non_p0_{i}"
        is_bad = i < 5  # 5 个异常 = 5/22 = 22.7% > 20%
        cases_overflow.append({
            "case_id": cid,
            "task_completion": "INVALID" if is_bad else "tied",
            "robustness": "tied",
            "readability": "tied",
            "judge_audit": {
                "task_completion": {"reason_codes": ["MALFORMED_JUDGE_RESPONSE"], "status": "ERROR"} if is_bad else {}
            },
        })
    mock_eval_over = MockEvaluator(cases_overflow, valid=False, invalid_reasons=["too many bad cases"])
    evolver_over = SkillEvolver(
        registry=registry,
        evaluator=mock_eval_over,
        llm=MockEvolverLLM(),
    )
    outcome_over = evolver_over.evolve_full(
        skill_name="explain_regex",
        eval_set_for_iter="repair_set",
        budget=budget,
        verbose=False,
    )
    assert outcome_over.error is not None
    assert "超阈值" in outcome_over.error
