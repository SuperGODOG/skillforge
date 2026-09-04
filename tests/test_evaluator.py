"""SkillEvaluator 子模块 + 装配测试（Phase 3）

覆盖：
    - structure.py:  合规打分 / 缺字段扣分 / Constraints 段识别
    - metrics.py:    turns 兜底 / tokens 汇总 / latency 透传
    - judge.py:      _parse 三种判定 / LLM 失败 fail-closed INVALID
    - ratchet.py:    首次 PASS / 硬门槛 5 条 / 软门槛 REVIEW / P0 变失败
    - SkillEvaluator: _dim_score 加权 / _efficiency_score ratio→分 / evaluate_skill 端到端（FakeLLM）
"""
from __future__ import annotations
import subprocess
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillforge import SkillRegistry, SkillEvaluator
from skillforge.models import SkillMeta, Trigger, EvalResult
from skillforge.evaluator.structure import score_structure, structure_total
from skillforge.evaluator.metrics import collect_objective_metrics
from skillforge.evaluator.judge import PairwiseJudge, skill_is_presented_as_a
from skillforge.evaluator.ratchet import check_ratchet


# ============ FakeLLM ============

def _judge_json(verdict: str) -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": ["TEST_REASON"],
        "evidence_summary": "test evidence",
    })

class FakeLLM:
    """按预定序列返回 content 的假 LLM"""

    def __init__(self, contents: list[str], usage_tokens: int = 100):
        self.contents = list(contents)
        self.usage_tokens = usage_tokens
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.contents.pop(0) if self.contents else "tied"
        return SimpleNamespace(
            content=content,
            usage={"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": self.usage_tokens},
        )


# ============ structure.py ============

def _mk_meta(**over):
    d = dict(
        name="s1", version="1.0.0", description="做一件具体的事情",
        use_when="用户明确要做这件事", not_for=["其他情况"],
        dependencies=[], trigger=Trigger(keywords=["k1", "k2"]),
        examples=["示例"],
    )
    d.update(over)
    return SkillMeta(**d)


def test_structure_full_marks():
    meta = _mk_meta()
    body = "## Overview\nX\n## Constraints\n不做 A"
    s = score_structure(meta, body)
    assert s["schema"] == 15.0
    assert s["trigger"] == 10.0
    assert s["prompt"] == 10.0
    assert s["deps"] == 5.0
    assert structure_total(s) == 40.0


def test_structure_no_constraints_zero():
    meta = _mk_meta()
    body = "## Overview\nX\n没有约束段"
    assert score_structure(meta, body)["prompt"] == 0.0


def test_structure_thin_trigger():
    meta = _mk_meta(trigger=Trigger(keywords=["only_one"]), not_for=[])
    s = score_structure(meta, "## Constraints\n...")
    assert s["trigger"] == 0.0


# ============ metrics.py ============

def test_metrics_turns_and_tokens():
    log = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ],
        "usage": {"total_tokens": 500},
        "latency_ms": 1234.5,
    }
    m = collect_objective_metrics(log)
    assert m["turns"] == 1.0
    assert m["tokens"] == 500.0
    assert m["latency_ms"] == 1234.5


def test_metrics_empty_messages_fallback_1_turn():
    m = collect_objective_metrics({"messages": [], "usage": {}, "latency_ms": 0})
    assert m["turns"] == 1.0


# ============ judge.py ============

def test_judge_parse_a_better():
    assert PairwiseJudge._parse(_judge_json("A_better")) == "A_better"


def test_judge_parse_b_better():
    assert PairwiseJudge._parse(_judge_json("B_better")) == "B_better"


def test_judge_parse_tied():
    assert PairwiseJudge._parse(_judge_json("tied")) == "tied"


def test_judge_parse_chinese_wording_is_invalid():
    assert PairwiseJudge._parse("A 更好") == "INVALID"


def test_judge_parse_unknown_is_invalid():
    assert PairwiseJudge._parse("我觉得都还行") == "INVALID"


def test_judge_llm_exception_returns_invalid():
    class BadLLM:
        def invoke(self, *a, **kw):
            raise RuntimeError("boom")

    j = PairwiseJudge(BadLLM())
    assert j.compare("q", "a", "b", "task_completion") == "INVALID"


# ============ ratchet.py ============

def _mk_result(task=25.0, robust=15.0, effic=10.0, read=10.0,
               struct=(15, 10, 10, 5), p0_pass=True) -> EvalResult:
    return EvalResult(
        release_id="r-x",
        structure_score={"schema": struct[0], "trigger": struct[1],
                         "prompt": struct[2], "deps": struct[3]},
        effect_score={"task": task, "robust": robust, "efficiency": effic, "readability": read},
        objective_metrics={},
        p0_pass=p0_pass,
    )


def test_ratchet_first_run_passes():
    v = check_ratchet(None, _mk_result())
    assert v.decision == "PASS"


def test_ratchet_total_drop_declined():
    old = _mk_result()
    new = _mk_result(task=20.0)
    v = check_ratchet(old, new)
    assert v.decision == "DECLINED"


def test_ratchet_task_drop_5pct_declined():
    old = _mk_result(task=20.0)
    new = _mk_result(task=19.0)  # -5%
    v = check_ratchet(old, new)
    assert v.decision == "DECLINED"


def test_ratchet_robust_drop_declined():
    old = _mk_result(robust=15.0)
    new = _mk_result(robust=14.0)  # -6.67%
    v = check_ratchet(old, new)
    assert v.decision == "DECLINED"


def test_ratchet_p0_broken_declined():
    old = _mk_result(p0_pass=True)
    new = _mk_result(p0_pass=False)
    v = check_ratchet(old, new)
    assert v.decision == "DECLINED"
    assert any("P0" in r for r in v.reasons)


def test_ratchet_soft_threshold_review():
    old = _mk_result(read=10.0)
    new = _mk_result(read=11.5)  # +15% 上升，触发软门槛
    v = check_ratchet(old, new)
    assert v.decision == "REVIEW"


# ============ SkillEvaluator 装配 ============

def test_evaluator_dim_score_weighted():
    vs = [
        ("c1", "A_better"),   # 1
        ("c2", "tied"),        # 0.5
        ("c3", "B_better"),    # 0
        ("c4", "A_better"),    # 1
    ]
    # (1 + 0.5 + 0 + 1) / 4 * 25 = 15.625
    assert SkillEvaluator._dim_score(vs, max_score=25.0) == 15.62 or \
           SkillEvaluator._dim_score(vs, max_score=25.0) == 15.63


def test_evaluator_efficiency_ratio_1_to_10():
    base = [{"tokens": 100}] * 3
    skill = [{"tokens": 100}] * 3
    assert SkillEvaluator._efficiency_score(base, skill) == 10.0


def test_evaluator_efficiency_ratio_2_to_5():
    base = [{"tokens": 100}] * 3
    skill = [{"tokens": 200}] * 3
    assert SkillEvaluator._efficiency_score(base, skill) == 5.0


def test_evaluator_efficiency_empty_returns_5():
    assert SkillEvaluator._efficiency_score([], []) == 5.0


# ============ evaluate_skill 端到端（FakeLLM）============

@pytest.fixture
def tmp_repo_with_skill(tmp_path: Path) -> Path:
    """临时 git 仓库 + 一个 skill + evaluation_sets/"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "skills" / "test_skill").mkdir(parents=True)
    (tmp_path / "skills" / "test_skill" / "SKILL.md").write_text(
        """---
name: test_skill
version: 1.0.0
description: 测试用的示例 skill
use_when: 处于测试场景时使用
not_for: [其他]
dependencies: []
trigger:
  keywords: [test, 测试]
examples: [test example]
---

## Overview
测试用。

## Instructions
按 X 做。

## Constraints
不做 Y。
""",
        encoding="utf-8",
    )
    (tmp_path / "evaluation_sets").mkdir()
    (tmp_path / "evaluation_sets" / "baseline_dev.json").write_text(
        '{"cases":[{"id":"c1","skill":"test_skill","query":"test q","reference":"do X"}]}',
        encoding="utf-8",
    )
    (tmp_path / "evaluation_sets" / "p0_cases.json").write_text(
        '{"p0_ids":[]}', encoding="utf-8",
    )
    (tmp_path / "runs").mkdir()
    return tmp_path


def test_evaluate_skill_end_to_end(tmp_repo_with_skill: Path):
    """1 条 case × 3 维度 Judge = 3 次 Judge + 2 次 Agent = 5 次 LLM 调用"""
    reg = SkillRegistry(
        db_path=tmp_repo_with_skill / "runs" / "t.db",
        skills_dir=tmp_repo_with_skill / "skills",
        repo_root=tmp_repo_with_skill,
    )
    reg.load_skills_from_dir()

    # 2 次 Agent（bare + skill）+ 3 次 Judge（task/robust/read）
    # Judge 全部 A_better = skill 版更好
    execution = FakeLLM(["bare_out", "skill_out"])
    judge_outputs = [
        _judge_json(
            "A_better" if skill_is_presented_as_a(0, dim, "baseline_dev:test_skill")
            else "B_better"
        )
        for dim in range(3)
    ]
    judge = FakeLLM(judge_outputs)
    evaluator = SkillEvaluator(registry=reg, llm=execution, judge_llm=judge)

    result = evaluator.evaluate_skill("test_skill", eval_set="baseline_dev")

    # 结构分：满分（SKILL.md 完整）
    assert result.structure_score["schema"] == 15.0
    assert result.structure_score["prompt"] == 10.0
    # 效果分：3 维全 A_better → 满分
    assert result.effect_score["task"] == 25.0
    assert result.effect_score["robust"] == 15.0
    assert result.effect_score["readability"] == 10.0
    # 效率：base=skill=100 → 10
    assert result.effect_score["efficiency"] == 10.0
    assert result.p0_pass is True
    reg.close()
