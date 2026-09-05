"""P1-I 测试套件：enable_a2 开关机制验证与 C/R/B/RB 四配置语义支持

包含验证项：
1. 【模型字段与默认值】：EvolveBudget.enable_a2 默认为 True，支持显式置 False；EvolveContext 具备 enable_a2 跟踪。
2. 【(a) 根因 LLM 零调用 (mock spy)】：enable_a2=False 时，_analyze_root_cause 完全跳过，LLM spy 捕获 0 次根因调用。
3. 【(b) dependency 类失败不走 REVIEW 短路】：含 tool_trace ERROR 特征时，enable_a2=True 走 REVIEW 短路诊断归档；
   而 enable_a2=False 跳过 Step 2.5 出口，走通用候选生成与 DECLINED/候选路径。
4. 【(c) enable_a2=True 默认行为不变】：既有行为完整保持，Step 2 根因与 Step 2.5 依赖短路照常执行。
5. 【(d) C vs B 归档差异】：Config B (enable_a2=True) 归档含 root-cause 标注；
   Config C (enable_a2=False) 归档不含 root-cause 标注。
6. 【参数覆盖优先级】：evolve_full(enable_a2=...) 优先级高于 budget.enable_a2。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from skillforge.models import (
    EvalResult,
    EvolveBudget,
    EvolveContext,
    Patch,
    RatchetVerdict,
    SkillMeta,
    ToolCallProvenance,
)
from skillforge.registry import SkillRegistry
from skillforge.evolver import SkillEvolver


class MockSpyLLM:
    """Spy LLM that captures all invoked prompts and returns configured responses."""

    def __init__(self, responses: list[str] | None = None, model: str = "spy-model"):
        self.responses = list(responses or [])
        self.model = model
        self.captured_prompts: list[str] = []

    def invoke(self, messages, **kwargs):
        content = ""
        for m in messages:
            if isinstance(m, dict) and "content" in m:
                content += str(m["content"])
        self.captured_prompts.append(content)
        resp_text = self.responses.pop(0) if self.responses else "[]"
        return SimpleNamespace(
            content=resp_text,
            usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
        )


def _setup_test_environment(tmp_path: Path):
    """构建独立测试沙箱与 Skill 目录"""
    skills_dir = tmp_path / "skills" / "weather_query"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_content = """---
name: weather_query
version: 1.0.0
description: 查询天气
use_when: 用户查询天气
not_for: []
dependencies: []
trigger:
  keywords:
    - 天气
examples: []
---

## Overview
查询天气

## Instructions
查询天气

## Examples
无

## Constraints
无
"""
    (skills_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

    # 创建必要的 evaluation_sets 目录（防止数据层校验误报）
    eval_dir = tmp_path / "evaluation_sets"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "repair_set.json").write_text(
        json.dumps({"cases": [{"id": "c1", "query": "北京天气"}]}),
        encoding="utf-8",
    )
    (eval_dir / "router_negatives.json").write_text(
        json.dumps({"cases": []}),
        encoding="utf-8",
    )

    registry = SkillRegistry(
        db_path=tmp_path / "t.db",
        skills_dir=tmp_path / "skills",
        repo_root=tmp_path,
    )
    registry.load_skills_from_dir()
    return registry, skill_content


def _build_dependency_failure_eval_result():
    """构建携带 tool_trace ERROR (503 Service Unavailable) 特征的 EvalResult"""
    bad_prov = ToolCallProvenance(
        tool_name="amap_weather_api",
        fixture_case_id="c1",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=False,
        authenticity_pass=False,
        input_params={"city": "北京"},
        output_status="ERROR",
        output_summary="503 Service Unavailable",
        latency_ms=5000.0,
        timestamp="2026-09-05T00:00:00Z",
        signature="mock_sig_503",
    )

    return EvalResult(
        release_id="base_ok_01",
        structure_score={"schema": 15.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=False,
        case_verdicts=[
            {"case_id": "c1", "query": "北京天气", "task_completion": "B_better"}
        ],
        case_outputs=[
            {"case_id": "c1", "query": "北京天气", "reference": "晴天", "output_skill": "503", "output_baseline": "晴天"}
        ],
        validation_channels=["tools"],
        provenances=[bad_prov],
        valid=True,
        invalid_reasons=[],
    )


def test_evolve_budget_and_context_enable_a2_defaults():
    """验证 EvolveBudget 与 EvolveContext 中 enable_a2 字段的默认值与显式赋值"""
    # 默认值必须为 True
    default_budget = EvolveBudget()
    assert default_budget.enable_a2 is True

    # 显式置 False
    custom_budget = EvolveBudget(enable_a2=False)
    assert custom_budget.enable_a2 is False

    # EvolveContext 具备 enable_a2 跟踪
    ctx = EvolveContext(skill_name="test_skill")
    assert ctx.enable_a2 is True
    ctx_disabled = EvolveContext(skill_name="test_skill", enable_a2=False)
    assert ctx_disabled.enable_a2 is False


def test_enable_a2_false_zero_root_cause_llm_calls_and_no_dependency_review(tmp_path: Path):
    """验收 (a) 与 (b):
    当 enable_a2=False 时：
    (a) 根因 LLM 零调用（mock spy 捕获 0 次根因 prompt）；
    (b) dependency 类失败（tool_trace ERROR 特征）不走 Step 2.5 REVIEW 短路，走通用候选与 DECLINED 路径。
    """
    registry, _ = _setup_test_environment(tmp_path)
    base_eval = _build_dependency_failure_eval_result()

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    # 构造 L1 candidate patch，修改 description 触发通用验证
    cand_patch_json = json.dumps([{
        "level": "L1",
        "rationale": "更新 description 尝试修复",
        "new_skill_md": """---
name: weather_query
version: 1.0.1
description: 查询天气更新版
use_when: 用户查询天气
not_for: []
dependencies: []
trigger:
  keywords:
    - 天气
examples: []
---

## Overview
查询天气

## Instructions
查询天气

## Examples
无

## Constraints
无
"""
    }])

    # Spy LLM: 只有 1 个 candidate 生成响应
    # 如果根因分析被调用，它会消耗掉这个响应或解析失败导致 candidate 为空
    llm = MockSpyLLM([cand_patch_json])

    evolver = SkillEvolver(
        registry=registry,
        evaluator=MockEvaluator(),
        llm=llm,
    )

    outcome = evolver.evolve_full(
        "weather_query",
        eval_set_for_iter="repair_set",
        max_candidates=1,
        enable_a2=False,  # 显式关闭 A2
        verbose=False,
    )

    # 断言 (a): 根因 LLM 零调用！
    # LLM 仅被调用 1 次（Round 1 生成 patch），且 Prompt 不包含根因分析提示
    assert len(llm.captured_prompts) == 1
    prompt = llm.captured_prompts[0]
    assert "你是 SkillForge 的元 Agent。为 Skill 生成 1 个改进候选" in prompt
    assert "根因分析专家" not in prompt
    assert "A2-lite" not in prompt

    # 断言 (b): 不走 Step 2.5 REVIEW 短路，走通用候选生成与 DECLINED 路径
    assert len(outcome.patches_review) == 0  # 无 REVIEW 诊断短路
    assert outcome.patches_generated == 1   # 正常生成了 1 个候选 patch
    assert len(outcome.patches_declined) == 1  # 候选在沙箱验证后走通用 DECLINED
    assert len(outcome.records) == 1
    assert outcome.records[0].status == "DECLINED"


def test_enable_a2_true_default_triggers_root_cause_and_dependency_review(tmp_path: Path):
    """验收 (c): enable_a2=True（默认）时保持原有 P1-G 行为逐字节不变：
    执行根因分析，且识别到 tool_trace ERROR 依赖失败后直接 Step 2.5 REVIEW 短路。
    """
    registry, _ = _setup_test_environment(tmp_path)
    base_eval = _build_dependency_failure_eval_result()

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    # Mock 根因响应
    root_cause_resp = json.dumps({
        "deps_broken": {"prob": 0.95, "why": "amap_weather_api 503 超时"},
        "prompt_vague": {"prob": 0.05, "why": ""},
        "trigger_inaccurate": {"prob": 0.0, "why": ""},
        "boundary_missing": {"prob": 0.0, "why": ""},
        "eval_noise": {"prob": 0.0, "why": ""},
    })
    llm = MockSpyLLM([root_cause_resp])

    evolver = SkillEvolver(
        registry=registry,
        evaluator=MockEvaluator(),
        llm=llm,
    )

    # 默认 enable_a2 为 None/budget.enable_a2(True)
    outcome = evolver.evolve_full(
        "weather_query",
        eval_set_for_iter="repair_set",
        max_candidates=1,
        verbose=False,
    )

    # 根因 LLM 必须被调用
    assert len(llm.captured_prompts) == 1
    assert "503 Service Unavailable" in llm.captured_prompts[0]

    # 触发 Step 2.5 依赖短路归档
    assert outcome.patches_generated == 0
    assert len(outcome.patches_review) == 1
    diag_file = Path(outcome.patches_review[0])
    assert diag_file.exists()
    report_text = diag_file.read_text(encoding="utf-8")
    assert "[REVIEW / DEPENDENCY_DIAGNOSTIC] weather_query" in report_text
    assert outcome.records[0].status == "REVIEW"


def test_archive_difference_c_vs_b_configuration(tmp_path: Path):
    """验收 (d): C vs B 配置归档差异：
    - Config B (enable_a2=True): 候选 DECLINED 归档包含 '## 根因辅助阅读 (Root Cause Diagnostics - Informational Only)'
    - Config C (enable_a2=False): 候选 DECLINED 归档不含 root-cause 标注
    """
    # 构造无依赖故障的普通失败 baseline (让 Config B 不走 Step 2.5 短路，而是进入候选生成)
    normal_failure_eval = EvalResult(
        release_id="base_normal_fail",
        structure_score={"schema": 15.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=False,
        case_verdicts=[
            {"case_id": "c1", "query": "北京天气", "task_completion": "B_better"}
        ],
        case_outputs=[
            {"case_id": "c1", "query": "北京天气", "reference": "晴天", "output_skill": "阴天", "output_baseline": "晴天"}
        ],
        validation_channels=[],
        provenances=[],
        valid=True,
        invalid_reasons=[],
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return normal_failure_eval

    cand_patch_json = json.dumps([{
        "level": "L1",
        "rationale": "优化描述",
        "new_skill_md": """---
name: weather_query
version: 1.0.1
description: 查询天气全新描述
use_when: 用户查询天气
not_for: []
dependencies: []
trigger:
  keywords:
    - 天气
examples: []
---

## Overview
查询天气

## Instructions
查询天气

## Examples
无

## Constraints
无
"""
    }])

    # 1. 运行 Config B (enable_a2=True)
    registry_b, _ = _setup_test_environment(tmp_path / "b_root")
    llm_b = MockSpyLLM([
        json.dumps({"prompt_vague": {"prob": 0.88, "why": "提示词不够详尽"}}),
        cand_patch_json,
    ])
    evolver_b = SkillEvolver(
        registry=registry_b,
        evaluator=MockEvaluator(),
        llm=llm_b,
    )
    outcome_b = evolver_b.evolve_full(
        "weather_query",
        eval_set_for_iter="repair_set",
        max_candidates=1,
        enable_a2=True,
        verbose=False,
    )
    assert len(outcome_b.patches_declined) == 1
    archive_b_path = Path(outcome_b.patches_declined[0])
    archive_b_text = archive_b_path.read_text(encoding="utf-8")
    # 断言 Config B 归档中包含根因标注段落
    assert "## 根因辅助阅读 (Root Cause Diagnostics - Informational Only)" in archive_b_text
    assert "cause: prompt_vague (prob=0.88) - 提示词不够详尽" in archive_b_text

    # 2. 运行 Config C (enable_a2=False)
    registry_c, _ = _setup_test_environment(tmp_path / "c_root")
    llm_c = MockSpyLLM([cand_patch_json])
    evolver_c = SkillEvolver(
        registry=registry_c,
        evaluator=MockEvaluator(),
        llm=llm_c,
    )
    outcome_c = evolver_c.evolve_full(
        "weather_query",
        eval_set_for_iter="repair_set",
        max_candidates=1,
        enable_a2=False,
        verbose=False,
    )
    assert len(outcome_c.patches_declined) == 1
    archive_c_path = Path(outcome_c.patches_declined[0])
    archive_c_text = archive_c_path.read_text(encoding="utf-8")
    # 断言 Config C 归档中绝无根因标注段落
    assert "## 根因辅助阅读" not in archive_c_text
    assert "Root Cause Diagnostics" not in archive_c_text


def test_enable_a2_parameter_precedence(tmp_path: Path):
    """验证参数与 budget.enable_a2 的解析优先级：
    evolve_full 参数 > budget.enable_a2 > 默认 True
    """
    base_eval = _build_dependency_failure_eval_result()

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    cand_patch_json = json.dumps([{
        "level": "L1",
        "rationale": "优化描述",
        "new_skill_md": """---
name: weather_query
version: 1.0.1
description: 查询天气全新描述
use_when: 用户查询天气
not_for: []
dependencies: []
trigger:
  keywords:
    - 天气
examples: []
---

## Overview
查询天气

## Instructions
查询天气

## Examples
无

## Constraints
无
"""
    }])

    # 情况 1: budget.enable_a2=True，但 evolve_full(enable_a2=False) 显式覆盖 → 判定为 False (生成 candidate)
    reg1, _ = _setup_test_environment(tmp_path / "p1")
    llm1 = MockSpyLLM([cand_patch_json])
    evolver1 = SkillEvolver(registry=reg1, evaluator=MockEvaluator(), llm=llm1)
    outcome1 = evolver1.evolve_full(
        "weather_query",
        budget=EvolveBudget(enable_a2=True),
        enable_a2=False,
        verbose=False,
    )
    assert outcome1.patches_generated == 1
    assert len(outcome1.patches_review) == 0

    # 情况 2: budget.enable_a2=False，evolve_full 未传 enable_a2 (None) → 继承 budget 取 False
    reg2, _ = _setup_test_environment(tmp_path / "p2")
    llm2 = MockSpyLLM([cand_patch_json])
    evolver2 = SkillEvolver(registry=reg2, evaluator=MockEvaluator(), llm=llm2)
    outcome2 = evolver2.evolve_full(
        "weather_query",
        budget=EvolveBudget(enable_a2=False),
        verbose=False,
    )
    assert outcome2.patches_generated == 1
    assert len(outcome2.patches_review) == 0

    # 情况 3: budget.enable_a2=False，但 evolve_full(enable_a2=True) 显式覆盖 → 判定为 True (走 Step 2.5 REVIEW)
    reg3, _ = _setup_test_environment(tmp_path / "p3")
    llm3 = MockSpyLLM([
        json.dumps({"deps_broken": {"prob": 0.95, "why": "503 error"}}),
    ])
    evolver3 = SkillEvolver(registry=reg3, evaluator=MockEvaluator(), llm=llm3)
    outcome3 = evolver3.evolve_full(
        "weather_query",
        budget=EvolveBudget(enable_a2=False),
        enable_a2=True,
        verbose=False,
    )
    assert outcome3.patches_generated == 0
    assert len(outcome3.patches_review) == 1
