"""P1-G 测试套件：A2-lite 根因分支增强全项验收与 Codex 审缺陷修复

包含：
1. 【P0-1 route trace 真实接线】：生产路径 SkillEvaluator.evaluate_skill → EvalResult → evolve_full
   验证真实 Router 判定链（hit_layer, chosen_skill, scores, latency_ms, matched_keywords, routing_notes）
2. 【P0-2 dependency 判定去自报】：彻底删除 LLM 自报触发路径，只由执行层证据触发（反例实测）
3. 【P1-1 er_h02 断言补强】：隔离断言覆盖 repair/seen_regression/hidden，排除 holdout/audit，走真实 repair 层完整 patch 分支
4. 【P1-2 bundle 真实驱动】：4×3 bundle 由真实 trace/body 特征驱动（不给 FakeLLM 回灌预期标签）；
   补"有效 baseline + 根因后 tool trace 触发 Step 2.5 dependency 出口"的端到端测试
5. 【P1-3 fragment 契约实校验】：Schema 实例校验生产 payload；兼容 format_execution_behavior list[dict]
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
import pytest

from skillforge.models import (
    EvalResult,
    Patch,
    RatchetVerdict,
    SkillMeta,
    Trigger,
    ToolCallProvenance,
    RouteResult,
)
from skillforge.registry import SkillRegistry
from skillforge.evaluator import (
    SkillEvaluator,
    ROUTING_METADATA,
    ROUTING_METADATA_SCHEMA,
    EXECUTION_BEHAVIOR,
    EXECUTION_BEHAVIOR_SCHEMA,
    format_routing_metadata,
    format_execution_behavior,
    extract_relevant_body_sections,
    format_body_sections,
    validate_payload_against_schema,
)
from skillforge.evaluator.fixtures import (
    FAILURE_BUNDLES,
    get_failure_bundle,
    get_all_failure_bundles,
)
from skillforge.evolver import (
    Failure,
    RootCause,
    SkillEvolver,
    _analyze_root_cause,
    _publish_patch,
    _archive_dependency_diagnostic,
    is_dependency_issue,
    _build_tool_trace_from_eval_result,
)
from skillforge.data_partition import load_json_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "evaluation_sets"


class CapturingFakeLLM:
    """捕获 prompt 调用的测试 LLM"""

    def __init__(self, responses: list[str], model: str = "test-model"):
        self.responses = list(responses)
        self.model = model
        self.captured_prompts: list[str] = []

    def invoke(self, messages, **kwargs):
        content = ""
        for m in messages:
            if isinstance(m, dict) and "content" in m:
                content += str(m["content"])
        self.captured_prompts.append(content)
        resp_text = self.responses.pop(0) if self.responses else "{}"
        return SimpleNamespace(
            content=resp_text,
            usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
        )


class FeatureDrivenFakeLLM:
    """基于 Prompt 中注入的真实 trace/body 特征进行根因判定模拟，绝不依赖测试预灌预期标签。"""

    def __init__(self, model: str = "test-model"):
        self.model = model
        self.captured_prompts: list[str] = []

    def invoke(self, messages, **kwargs):
        content = ""
        for m in messages:
            if isinstance(m, dict) and "content" in m:
                content += str(m["content"])
        self.captured_prompts.append(content)

        # 依据 Prompt 内容中的真实特征做分类决策，而非测试标签回灌
        if any(err in content for err in ("503 Service Unavailable", "CIRCUIT_OPEN", "ToolNotFound", "DEPENDENCY_ERROR")):
            resp = {
                "deps_broken": {"prob": 0.90, "why": "识别到工具/网络执行层故障特征"},
                "prompt_vague": {"prob": 0.05, "why": ""},
                "trigger_inaccurate": {"prob": 0.05, "why": ""},
                "boundary_missing": {"prob": 0.0, "why": ""},
                "eval_noise": {"prob": 0.0, "why": ""},
            }
        elif any(mis in content for mis in ("MISROUTED", "ROUTING_AMBIGUOUS", "FALSE_NEGATIVE_NOT_FOR", "ROUTE_MISMATCH", "ROUTE_REJECT")) or "(validation_channels): [\"router\"]" in content:
            resp = {
                "trigger_inaccurate": {"prob": 0.88, "why": "识别到路由判定链异常或触发词/边界覆盖偏差"},
                "prompt_vague": {"prob": 0.06, "why": ""},
                "deps_broken": {"prob": 0.0, "why": ""},
                "boundary_missing": {"prob": 0.06, "why": ""},
                "eval_noise": {"prob": 0.0, "why": ""},
            }
        elif "EVAL_NOISE" in content or "failed_phase: judge" in content or "failed_phase): judge" in content:
            resp = {
                "eval_noise": {"prob": 0.82, "why": "识别为评测判定层噪声"},
                "prompt_vague": {"prob": 0.10, "why": ""},
                "trigger_inaccurate": {"prob": 0.05, "why": ""},
                "deps_broken": {"prob": 0.0, "why": ""},
                "boundary_missing": {"prob": 0.03, "why": ""},
            }
        elif "Section: Constraints" in content:
            resp = {
                "boundary_missing": {"prob": 0.85, "why": "识别为约束边界缺失"},
                "prompt_vague": {"prob": 0.10, "why": ""},
                "trigger_inaccurate": {"prob": 0.05, "why": ""},
                "deps_broken": {"prob": 0.0, "why": ""},
                "eval_noise": {"prob": 0.0, "why": ""},
            }
        else:
            resp = {
                "prompt_vague": {"prob": 0.85, "why": "识别为 Instructions 指令说明不详"},
                "trigger_inaccurate": {"prob": 0.05, "why": ""},
                "deps_broken": {"prob": 0.0, "why": ""},
                "boundary_missing": {"prob": 0.05, "why": ""},
                "eval_noise": {"prob": 0.0, "why": ""},
            }
        return SimpleNamespace(
            content=json.dumps(resp),
            usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
        )


def _create_mock_meta(name: str = "explain_regex") -> SkillMeta:
    return SkillMeta(
        name=name,
        version="1.0.0",
        description="讲解正则表达式原理",
        use_when="用户想理解正则表达式语法和回溯原理",
        not_for=["写代码"],
        trigger=Trigger(keywords=["正则", "regex", "讲解"]),
    )


# =======================================================================
# 1. 根因输入增强与生产接线 (Three-way inputs & P0-1 Production Wiring)
# =======================================================================

def test_analyze_root_cause_three_way_inputs_injected_into_prompt():
    """验证相关 Body 段、route trace、tool trace 均完整格式化并注入到 LLM Prompt 中"""
    meta = _create_mock_meta()
    body = """## Overview
讲解正则原理。

## Instructions
1. 识别类型。
2. 给出解释。

## Examples
Q: .*
A: 贪婪匹配

## Constraints
禁止生成代码。"""
    fails = [
        Failure(
            case_id="c01",
            query="讲讲 \\1",
            reference="解释反向引用",
            output_skill="不知道",
            output_baseline="反向引用是指...",
            losing_dims=["task_completion"],
        )
    ]
    route_trace = {
        "hit_layer": "rule",
        "chosen_skill": "explain_regex",
        "scores": {"rule": {"explain_regex": 1.0}},
        "latency_ms": 0.1,
        "trigger_keywords": ["正则", "regex"],
        "matched_keywords": ["正则"],
        "use_when": "讲解正则表达式",
        "not_for": ["写代码"],
            "routing_notes": "ROUTE_MATCH_TEST：命中规则路由",
    }
    tool_trace = {
        "failed_phase": "tool_execution",
        "failure_type": "DEPENDENCY_ERROR",
        "is_dependency_failure": True,
        "error_message": "Connection refused to external mock service",
        "validation_channels": ["tools"],
        "tool_provenances": [
            {
                "tool_name": "mock_tool",
                "output_status": "ERROR",
                "tool_success": False,
                "output_summary": "503 timeout",
            }
        ],
    }
    relevant_body = extract_relevant_body_sections(body, target_sections=["Instructions", "Constraints"])

    mock_resp = json.dumps({
        "prompt_vague": {"prob": 0.7, "why": "Instructions 缺失反向引用"},
        "trigger_inaccurate": {"prob": 0.2, "why": ""},
        "deps_broken": {"prob": 0.1, "why": ""},
        "boundary_missing": {"prob": 0.0, "why": ""},
        "eval_noise": {"prob": 0.0, "why": ""},
    })
    llm = CapturingFakeLLM([mock_resp])

    causes = _analyze_root_cause(
        llm,
        meta,
        body,
        fails,
        relevant_body_sections=relevant_body,
        route_trace=route_trace,
        tool_trace=tool_trace,
    )

    assert len(causes) == 5
    assert causes[0].label == "prompt_vague"
    assert causes[0].prob == 0.7

    # 验证 Prompt 捕获内容包含三路输入
    prompt_sent = llm.captured_prompts[0]
    assert "Section: Instructions" in prompt_sent
    assert "Section: Constraints" in prompt_sent
    assert "ROUTE_MATCH_TEST" in prompt_sent
    assert "(hit_layer): rule" in prompt_sent
    assert "Connection refused to external mock service" in prompt_sent
    assert "mock_tool" in prompt_sent


def test_route_trace_production_wiring_evaluate_to_evolve(tmp_path: Path):
    """【P0-1 生产接线】验证从 SkillEvaluator.evaluate_skill 产生真实 RouteResult，
    保存到 EvalResult 后进入 evolve_full，最终真实注入到根因 Prompt，不使用默认硬编码。
    """
    # 建立测试用真实 Skill
    skills_dir = tmp_path / "skills" / "explain_regex"
    skills_dir.mkdir(parents=True, exist_ok=True)
    body_text = """## Overview
讲解正则表达式原理。

## Instructions
1. 识别正则表达式类型。
2. 解释回溯与匹配。

## Examples
Q: (a|b)*
A: 分组与星号

## Constraints
不写代码。"""
    frontmatter = """---
name: explain_regex
version: 1.0.0
description: 讲解正则表达式原理
use_when: 用户想理解正则表达式语法
not_for:
  - 写业务代码
dependencies: []
trigger:
  keywords:
    - 正则
    - regex
    - 讲解
examples:
  - 讲解正则表达式的原理与匹配过程
  - 讲讲 (a|b)*
evaluation:
  last_score: null
  last_release_id: null
---
"""
    (skills_dir / "SKILL.md").write_text(f"{frontmatter}\n{body_text}", encoding="utf-8")

    registry = SkillRegistry(db_path=tmp_path / "t.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    registry.load_skills_from_dir()

    # 创建带真实 IntentRouter 的 SkillEvaluator
    judge_json = json.dumps({
        "verdict": "B_better",
        "reason_codes": ["EVIDENCE_SUFFICIENT"],
        "evidence_summary": "Baseline is better on task_completion",
    })
    eval_llm = CapturingFakeLLM(["A_better"])
    judge_llm = CapturingFakeLLM([judge_json, judge_json, judge_json])
    evaluator = SkillEvaluator(registry=registry, llm=eval_llm, judge_llm=judge_llm)

    test_cases = [
        {
            "id": "er_h02",
            "query": "讲解正则表达式的原理与匹配过程",
            "reference": "定义 backreference",
            "skill": "explain_regex",
        }
    ]

    # 1. 生产调用 evaluate_skill
    eval_result = evaluator.evaluate_skill("explain_regex", cases=test_cases, verbose=False)

    # 断言 EvalResult 真实字段正确落地
    assert eval_result.hit_layer in ("rule", "embed", "llm")
    assert eval_result.verdict == "ROUTE_MATCH"
    assert any(k in eval_result.matched_keywords for k in ("讲解", "正则"))
    assert eval_result.route_result is not None
    assert isinstance(eval_result.route_result, RouteResult)
    assert len(eval_result.routing_notes) > 0
    assert not hasattr(eval_result, "declared_level")

    # 2. 生产调用 evolve_full（传入真实已接线的 evaluator 生成的 eval_result）
    evaluator.evaluate_skill = lambda *a, **k: eval_result
    llm_responses = [
        json.dumps({
            "prompt_vague": {"prob": 0.85, "why": "Instructions 未说明反向引用"},
            "trigger_inaccurate": {"prob": 0.10, "why": ""},
            "deps_broken": {"prob": 0.0, "why": ""},
            "boundary_missing": {"prob": 0.05, "why": ""},
            "eval_noise": {"prob": 0.0, "why": ""},
        }),
        # Step 3 生成 patch
        json.dumps([]),
    ]
    evolver_llm = CapturingFakeLLM(llm_responses)
    evolver = SkillEvolver(
        registry=registry,
        evaluator=evaluator,
        llm=evolver_llm,
    )

    # 运行 evolve_full
    outcome = evolver.evolve_full("explain_regex", max_candidates=1, verbose=False)

    # 断言 LLM 捕获的 Prompt 中包含了真实 Router 判定链依据（而非硬编码的 BASELINE_EVAL）
    assert len(evolver_llm.captured_prompts) >= 1
    prompt = evolver_llm.captured_prompts[0]

    # 生产验证：判定链来自真实 Router
    assert f"(hit_layer): {eval_result.hit_layer}" in prompt
    assert "(chosen_skill): explain_regex" in prompt
    assert "declared_level" not in prompt
    assert "computed_level" not in prompt
    assert any(k in prompt for k in ("讲解", "正则"))
    assert eval_result.routing_notes in prompt


def test_route_exception_is_diagnostic_and_never_synthesizes_route_trace(tmp_path: Path):
    """Router 异常必须进入 route_error；不得降级成 L1/ROUTE_MATCH 假轨迹。"""
    skills_dir = tmp_path / "skills" / "explain_regex"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_md = "\n".join([
        "---",
        "name: explain_regex",
        "version: 1.0.0",
        "description: 讲解正则表达式",
        "use_when: 讲解正则表达式",
        "not_for: []",
        "dependencies: []",
        "trigger:",
        "  keywords: [正则]",
        "examples: [讲解正则]",
        "---",
        "",
        "## Overview",
        "解释正则表达式。",
        "",
        "## Instructions",
        "给出准确解释。",
        "",
        "## Constraints",
        "不编造。",
        "",
    ])
    (skills_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    registry = SkillRegistry(db_path=tmp_path / "t.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    registry.load_skills_from_dir()

    class RaisingRouter:
        def route(self, query):
            raise RuntimeError("router backend unavailable")

    judge_json = json.dumps({
        "verdict": "tied",
        "reason_codes": ["EVIDENCE_SUFFICIENT"],
        "evidence_summary": "equal",
    })
    evaluator = SkillEvaluator(
        registry=registry,
        llm=CapturingFakeLLM(["same output", "same output"]),
        judge_llm=CapturingFakeLLM([judge_json, judge_json, judge_json]),
        router=RaisingRouter(),
    )
    result = evaluator.evaluate_skill(
        "explain_regex",
        cases=[{"id": "route-error", "query": "讲解正则", "reference": "解释"}],
        p0_ids=[],
        verbose=False,
    )

    assert result.route_result is None
    assert result.route_error == "ROUTER_ERROR: RuntimeError: router backend unavailable"
    assert result.verdict == "ROUTE_UNAVAILABLE"
    assert result.hit_layer == "unknown"
    assert "不推断路由等级" in format_routing_metadata(None)
    assert not hasattr(result, "declared_level")
    assert not hasattr(result, "computed_level")

    evolver = SkillEvolver(
        registry=registry,
        evaluator=SimpleNamespace(
            evaluate_skill=lambda *args, **kwargs: result,
            output_cache=None,
            llm=CapturingFakeLLM([]),
            judge=SimpleNamespace(llm=CapturingFakeLLM([])),
        ),
        llm=CapturingFakeLLM([]),
    )
    outcome = evolver.evolve_full("explain_regex", max_candidates=1, verbose=False)
    assert outcome.patches_generated == 0
    assert "路由判定不可用" in outcome.error


def test_extract_relevant_body_sections_and_stats_cohesion():
    """验证 Body 段提取与 P1-E 的 baseline_stats 呼应"""
    body = """## Overview
概述文本

## Instructions
说明文本

## Constraints
约束文本"""
    sections = extract_relevant_body_sections(body)
    assert "Overview" in sections
    assert "Instructions" in sections
    assert "Constraints" in sections

    filtered = extract_relevant_body_sections(body, target_sections=["Instructions"])
    assert "Instructions" in filtered
    assert "Overview" not in filtered

    rendered = format_body_sections(filtered, body=body)
    assert "### Section: Instructions" in rendered


# =======================================================================
# 2. 两个 prompt fragment 常量模块契约与 Schema 实例校验 (P1-3)
# =======================================================================

def test_prompt_fragments_contracts_and_schemas():
    """验证两个 prompt fragment 常量模块可测，具备 schema/description/template 契约"""
    # 验证 ROUTING_METADATA
    assert ROUTING_METADATA.name == "ROUTING_METADATA"
    assert "Router 判定链" in ROUTING_METADATA.description
    assert isinstance(ROUTING_METADATA.schema, dict)
    assert ROUTING_METADATA.schema["type"] == "object"
    assert "hit_layer" in ROUTING_METADATA.schema["properties"]
    assert "chosen_skill" in ROUTING_METADATA.schema["properties"]
    assert "scores" in ROUTING_METADATA.schema["properties"]
    assert "latency_ms" in ROUTING_METADATA.schema["properties"]
    assert "declared_level" not in ROUTING_METADATA.schema["properties"]
    assert "computed_level" not in ROUTING_METADATA.schema["properties"]
    assert ROUTING_METADATA["schema"] == ROUTING_METADATA_SCHEMA
    assert ROUTING_METADATA["description"] == ROUTING_METADATA.description

    # 验证 EXECUTION_BEHAVIOR
    assert EXECUTION_BEHAVIOR.name == "EXECUTION_BEHAVIOR"
    assert "执行行为轨迹" in EXECUTION_BEHAVIOR.description
    assert isinstance(EXECUTION_BEHAVIOR.schema, dict)
    assert EXECUTION_BEHAVIOR.schema["type"] == "object"
    assert "failed_phase" in EXECUTION_BEHAVIOR.schema["properties"]
    assert "failure_type" in EXECUTION_BEHAVIOR.schema["properties"]
    assert "is_dependency_failure" in EXECUTION_BEHAVIOR.schema["properties"]
    assert EXECUTION_BEHAVIOR["schema"] == EXECUTION_BEHAVIOR_SCHEMA


def test_fragment_schema_instance_validation_with_production_payloads():
    """【P1-3 Schema 实例校验】对实际生产 payload 实例过 Schema 校验，且验证非法 payload 拦截"""
    # 1. 实际生产 route_trace 校验
    prod_route = {
        "hit_layer": "rule",
        "chosen_skill": "explain_regex",
        "scores": {"rule": {"explain_regex": 1.0}},
        "latency_ms": 0.1,
        "trigger_keywords": ["正则", "regex"],
        "matched_keywords": ["正则"],
        "use_when": "讲解正则表达式",
        "not_for": ["写业务代码"],
        "routing_notes": "规则层唯一命中",
    }
    is_valid, errors = validate_payload_against_schema(prod_route, ROUTING_METADATA_SCHEMA)
    assert is_valid is True, f"生产 route_trace Schema 校验失败: {errors}"
    assert len(errors) == 0

    # 2. 实际生产 tool_trace 校验
    prod_tool = {
        "failed_phase": "tool_execution",
        "failure_type": "DEPENDENCY_ERROR",
        "is_dependency_failure": True,
        "error_message": "503 timeout",
        "validation_channels": ["tools", "execution"],
        "tool_provenances": [
            {
                "tool_name": "amap_weather_api",
                "output_status": "ERROR",
                "tool_success": False,
            }
        ],
    }
    is_valid_tool, errors_tool = validate_payload_against_schema(prod_tool, EXECUTION_BEHAVIOR_SCHEMA)
    assert is_valid_tool is True, f"生产 tool_trace Schema 校验失败: {errors_tool}"
    assert len(errors_tool) == 0

    # 3. 反例测试：非法 hit_layer 与缺失必填字段
    bad_route = {"hit_layer": "invalid_layer_foo"}
    ok, bad_errs = validate_payload_against_schema(bad_route, ROUTING_METADATA_SCHEMA)
    assert ok is False
    assert any("not in enum" in e for e in bad_errs)

    bad_tool = {"is_dependency_failure": True}  # 缺少 failed_phase 和 failure_type
    ok_t, bad_t_errs = validate_payload_against_schema(bad_tool, EXECUTION_BEHAVIOR_SCHEMA)
    assert ok_t is False
    assert any("missing required field" in e for e in bad_t_errs)


def test_format_execution_behavior_list_of_dict():
    """【P1-3 契约修复】验证 format_execution_behavior 正确解析 list[dict] 并识别 ERROR/CIRCUIT_OPEN"""
    # 测试包含 ERROR 的 dict 列表
    dict_list_error = [
        {
            "tool_name": "weather_api",
            "output_status": "ERROR",
            "tool_success": False,
            "output_summary": "503 Service Unavailable",
        }
    ]
    rendered_error = format_execution_behavior(dict_list_error)
    assert "failure_type): DEPENDENCY_ERROR" in rendered_error
    assert "is_dependency_failure): true" in rendered_error
    assert "weather_api" in rendered_error
    assert "ERROR" in rendered_error

    # 测试包含 CIRCUIT_OPEN 的 dict 列表
    dict_list_circuit = [
        {
            "tool_name": "weather_api",
            "output_status": "CIRCUIT_OPEN",
            "tool_success": False,
            "output_summary": "circuit broken",
        }
    ]
    rendered_circuit = format_execution_behavior(dict_list_circuit)
    assert "failure_type): CIRCUIT_OPEN" in rendered_circuit
    assert "is_dependency_failure): true" in rendered_circuit

    # 测试正常 dict 列表
    dict_list_ok = [
        {
            "tool_name": "calc_tool",
            "output_status": "SUCCESS",
            "tool_success": True,
            "output_summary": "result=42",
        }
    ]
    rendered_ok = format_execution_behavior(dict_list_ok)
    assert "failure_type): UNKNOWN" in rendered_ok
    assert "is_dependency_failure): false" in rendered_ok


# =======================================================================
# 3. dependency 出口与严格去自报 (P0-2)
# =======================================================================

def test_is_dependency_issue_requires_execution_evidence_and_rejects_self_report():
    """【P0-2 严格去自报】验证 dependency 出口只由执行层证据触发，完全删除 LLM 自报触发路径。
    Codex 重点反例实测：RootCause("deps_broken", 0.8, "self-report") + {failed_phase:none,failure_type:NONE} 必须返回 False。
    """
    # 1. 执行层证据触发（通过）
    tool_trace_err = {
        "failed_phase": "tool_execution",
        "failure_type": "DEPENDENCY_ERROR",
        "is_dependency_failure": True,
        "error_message": "503 timeout",
    }
    is_dep, reason = is_dependency_issue([], tool_trace_err)
    assert is_dep is True
    assert "tool trace" in reason

    tool_trace_circuit = {
        "failed_phase": "tool_execution",
        "failure_type": "CIRCUIT_OPEN",
        "tool_provenances": [{"tool_name": "t1", "output_status": "CIRCUIT_OPEN", "tool_success": False}],
    }
    is_dep, reason = is_dependency_issue([], tool_trace_circuit)
    assert is_dep is True

    # 2. Codex 审查报告核心反例：LLM 自报 deps_broken，但无执行层证据
    # 必须返回 False！绝不允许阻断 patch 生成或进入 dependency 出口！
    causes_self_report = [RootCause(label="deps_broken", prob=0.8, why="LLM 自行推测依赖坏了但无任何工具报错")]
    normal_tool_trace = {"failed_phase": "none", "failure_type": "NONE"}
    is_dep_codex, reason_codex = is_dependency_issue(causes_self_report, normal_tool_trace)
    assert is_dep_codex is False, "【P0-2 违规】无执行层证据时，LLM 自报 deps_broken 绝不可触发 dependency 出口！"
    assert reason_codex == ""

    # 即使 prob=0.99，无执行层证据依然坚决返回 False
    causes_high_prob = [RootCause(label="deps_broken", prob=0.99, why="self-report")]
    is_dep_high, _ = is_dependency_issue(causes_high_prob, None)
    assert is_dep_high is False

    # 3. 纯 prompt 缺陷反例
    causes_prompt = [RootCause(label="prompt_vague", prob=0.9, why="指令不清")]
    is_dep_p, _ = is_dependency_issue(causes_prompt, normal_tool_trace)
    assert is_dep_p is False


def test_archive_dependency_diagnostic_creates_valid_report(tmp_path: Path):
    """验证外部依赖故障诊断归档生成规范 markdown 报告，状态为 REVIEW"""
    meta = _create_mock_meta("weather_query")
    root_causes = [RootCause(label="deps_broken", prob=0.88, why="高德天气 API 报 503 错误")]
    tool_trace = {
        "failed_phase": "tool_execution",
        "failure_type": "DEPENDENCY_ERROR",
        "error_message": "503 Service Unavailable",
        "tool_provenances": [{"tool_name": "amap_weather_api", "output_status": "ERROR", "output_summary": "503"}],
    }
    path = _archive_dependency_diagnostic(
        repo_root=tmp_path,
        skill_name="weather_query",
        root_causes=root_causes,
        tool_trace=tool_trace,
        dep_reason="外部气象接口 503 超时",
        meta=meta,
        body="## Overview",
        failures=[],
    )

    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "[REVIEW / DEPENDENCY_DIAGNOSTIC] weather_query" in content
    assert "- status: REVIEW" in content
    assert "- patch_generated: false" in content
    assert "deps_broken" in content
    assert "prob=0.88" in content
    assert "修改 Prompt/SKILL.md 文案对解决依赖问题无效" in content
    assert "diff" not in content


# =======================================================================
# 4. probability 定位 (只进诊断报告辅助阅读，不参与自动发布决策)
# =======================================================================

def test_probability_does_not_affect_publish_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """验证自动发布门仅受 L1+computed L1+PASS 约束，完全不受 root cause probability 影响"""
    patch = Patch(
        skill_name="s1",
        level="L1",
        computed_level="L1",
        diff="---\nname: s1\nversion: 1.0.1\ndescription: X\nuse_when: Y\ntrigger:\n  keywords: [a]\n---\n\n## Overview\n...",
        rationale="add example",
        downgrade_attempt=False,
    )
    verdict = RatchetVerdict(decision="PASS", reasons=[])
    eval_result = EvalResult(
        release_id="rel_01",
        structure_score={"s": 20.0},
        effect_score={"e": 30.0},
        objective_metrics={},
        p0_pass=True,
    )

    class MockStateMachine:
        def begin_release(self, *args): return "rel_test"
        def write_commit(self, *args): pass
        def append_evaluation(self, *args): pass
        def commit_release(self, *args): pass

    sm = MockStateMachine()
    (tmp_path / "skills" / "s1").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "skillforge.evolver.run_final_audit_gate",
        lambda **_: SimpleNamespace(passed=True, verdict="PASS", reasons=[], audit_score=1.0),
    )

    # 1. 即使 root cause prob 极低 (0.01) 或很高 (0.99)，只要满足 L1+PASS，仍然正常自动发布
    rc_low_prob = [RootCause(label="prompt_vague", prob=0.01, why="")]
    outc1 = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm,
        skill_name="s1",
        patch=patch,
        verdict=verdict,
        new_result=eval_result,
        root_causes=rc_low_prob,
    )
    assert outc1["status"] == "PUBLISHED"

    rc_high_prob = [RootCause(label="prompt_vague", prob=0.99, why="")]
    outc2 = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm,
        skill_name="s1",
        patch=patch,
        verdict=verdict,
        new_result=eval_result,
        root_causes=rc_high_prob,
    )
    assert outc2["status"] == "PUBLISHED"

    # 2. 如果是 L2 patch，即使 prob=0.99，也绝不自动发布，走建议归档
    patch_l2 = Patch(
        skill_name="s1",
        level="L2",
        computed_level="L2",
        diff=patch.diff,
        rationale="instructions tweak",
    )
    outc3 = _publish_patch(
        repo_root=tmp_path,
        registry=None,
        state_machine=sm,
        skill_name="s1",
        patch=patch_l2,
        verdict=verdict,
        new_result=eval_result,
        root_causes=rc_high_prob,
    )
    assert outc3["status"] in ("SUGGESTION", "REVIEW")
    assert outc3["path"] != ""
    report_text = Path(outc3["path"]).read_text(encoding="utf-8")
    assert "根因辅助阅读" in report_text
    assert "prob=0.99" in report_text
    assert "不参与自动发布门判定" in report_text


# =======================================================================
# 5. Failure bundle 真实特征驱动与 Step 2.5 dependency 出口 (P1-2)
# =======================================================================

def test_failure_bundles_structure_and_count():
    """验证 4 个根因策略分类，每类至少 3 个独立失败样本"""
    all_bundles = get_all_failure_bundles()
    required_strategies = ["prompt_defect", "route_misjudgment", "execution_dependency", "evaluation_noise"]
    for strat in required_strategies:
        assert strat in all_bundles, f"缺失根因策略: {strat}"
        items = all_bundles[strat]
        assert len(items) >= 3, f"策略 {strat} 样例数不足 3: {len(items)}"
        for item in items:
            assert "case_id" in item
            assert "query" in item
            assert "expected_root_cause" in item
            assert "route_trace" in item
            assert "tool_trace" in item


def test_bundle_driven_branch_verification():
    """【P1-2 特征驱动】4×3 bundle 全部由真实 trace/body 特征驱动推理，
    不给 FakeLLM 回灌预期标签；并且严格断言 execution_dependency 与 prompt_defect 的 dependency 分支判定。
    """
    all_bundles = get_all_failure_bundles()
    llm = FeatureDrivenFakeLLM()

    # 1. 验证 prompt_defect 样本
    for sample in all_bundles["prompt_defect"]:
        fails = [Failure(case_id=sample["case_id"], query=sample["query"], reference=sample["reference"], output_skill=sample["output_skill"], output_baseline=sample["output_baseline"], losing_dims=sample["losing_dims"])]
        meta = _create_mock_meta(sample["skill_name"])
        causes = _analyze_root_cause(llm, meta, "## Overview", fails, relevant_body_sections=sample.get("relevant_body_sections"), route_trace=sample["route_trace"], tool_trace=sample["tool_trace"])
        assert causes[0].label in ("prompt_vague", "boundary_missing")
        # 纯 prompt 缺陷不触发 dependency
        is_dep, _ = is_dependency_issue(causes, sample["tool_trace"])
        assert is_dep is False

    # 2. 验证 route_misjudgment 样本
    for sample in all_bundles["route_misjudgment"]:
        fails = [Failure(case_id=sample["case_id"], query=sample["query"], reference=sample["reference"], output_skill=sample["output_skill"], output_baseline=sample["output_baseline"], losing_dims=sample["losing_dims"])]
        meta = _create_mock_meta(sample["skill_name"])
        causes = _analyze_root_cause(llm, meta, "## Overview", fails, relevant_body_sections=sample.get("relevant_body_sections"), route_trace=sample["route_trace"], tool_trace=sample["tool_trace"])
        assert causes[0].label == "trigger_inaccurate"
        is_dep, _ = is_dependency_issue(causes, sample["tool_trace"])
        assert is_dep is False

    # 3. 验证 execution_dependency 样本：只凭 tool_trace 执行层失败特征触发 dependency
    for sample in all_bundles["execution_dependency"]:
        fails = [Failure(case_id=sample["case_id"], query=sample["query"], reference=sample["reference"], output_skill=sample["output_skill"], output_baseline=sample["output_baseline"], losing_dims=sample["losing_dims"])]
        meta = _create_mock_meta(sample["skill_name"])
        causes = _analyze_root_cause(llm, meta, "## Overview", fails, relevant_body_sections=sample.get("relevant_body_sections"), route_trace=sample["route_trace"], tool_trace=sample["tool_trace"])
        assert causes[0].label == "deps_broken"
        # 严格执行层驱动断言：即使不传 causes，仅凭 tool_trace 就必须判定为 True！
        is_dep, reason = is_dependency_issue([], sample["tool_trace"])
        assert is_dep is True
        assert ("tool trace" in reason)

    # 4. 验证 evaluation_noise 样本
    for sample in all_bundles["evaluation_noise"]:
        fails = [Failure(case_id=sample["case_id"], query=sample["query"], reference=sample["reference"], output_skill=sample["output_skill"], output_baseline=sample["output_baseline"], losing_dims=sample["losing_dims"])]
        meta = _create_mock_meta(sample["skill_name"])
        causes = _analyze_root_cause(llm, meta, "## Overview", fails, relevant_body_sections=sample.get("relevant_body_sections"), route_trace=sample["route_trace"], tool_trace=sample["tool_trace"])
        assert causes[0].label == "eval_noise"
        is_dep, _ = is_dependency_issue(causes, sample["tool_trace"])
        assert is_dep is False


def test_valid_baseline_with_tool_failure_triggers_step_2_5_dependency_exit(tmp_path: Path):
    """【P1-2 端到端补齐】覆盖'有效 baseline (valid=True) + 根因分析后 tool trace 触发 Step 2.5 dependency 出口'。
    断言进入 Step 2.5 外部依赖出口并直接归档 REVIEW，patches_generated == 0。
    """
    skills_dir = tmp_path / "skills" / "weather_query"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_content = """---
name: weather_query
version: 1.0.0
description: 查询天气
use_when: 查询天气
not_for: []
dependencies:
  - amap_weather_api
trigger:
  keywords:
    - 天气
examples:
  - 北京天气
evaluation:
  last_score: null
  last_release_id: null
---

## Overview
天气查询 Skill

## Instructions
1. 调用天气 API
2. 格式化输出

## Constraints
无
"""
    (skills_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

    registry = SkillRegistry(db_path=tmp_path / "t.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    registry.load_skills_from_dir()

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
        signature="mock_sig",
    )

    # 关键设计：valid=True，通过 Step 1 基线校验；但在 case_verdicts 存在失败，且 provenances 含有工具失败
    valid_baseline_eval = EvalResult(
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
        valid=True,  # 确保不走 evolve_full:224 的 invalid 早退，而是进入 Step 2 & Step 2.5！
        invalid_reasons=[],
    )

    class MockEval:
        def __init__(self): self.llm = None
        def evaluate_skill(self, *a, **k): return valid_baseline_eval

    # Mock LLM 响应
    llm = CapturingFakeLLM([
        json.dumps({
            "deps_broken": {"prob": 0.95, "why": "amap_weather_api 503 超时"},
            "prompt_vague": {"prob": 0.05, "why": ""},
            "trigger_inaccurate": {"prob": 0.0, "why": ""},
            "boundary_missing": {"prob": 0.0, "why": ""},
            "eval_noise": {"prob": 0.0, "why": ""},
        })
    ])

    evolver = SkillEvolver(
        registry=registry,
        evaluator=MockEval(),
        llm=llm,
    )

    outcome = evolver.evolve_full("weather_query", eval_set_for_iter="repair", max_candidates=1, verbose=False)

    # 确认 Step 2 根因分析确实被执行调用了（LLM 收到 prompt）
    assert len(llm.captured_prompts) == 1
    assert "503 Service Unavailable" in llm.captured_prompts[0]

    # 确认触发 Step 2.5 依赖出口
    assert outcome.patches_generated == 0
    assert len(outcome.patches_published) == 0
    assert len(outcome.patches_review) == 1
    diag_file = Path(outcome.patches_review[0])
    assert diag_file.exists()
    report_text = diag_file.read_text(encoding="utf-8")
    assert "[REVIEW / DEPENDENCY_DIAGNOSTIC] weather_query" in report_text
    assert "- patch_generated: false" in report_text
    assert outcome.records[0].status == "REVIEW"
    assert outcome.records[0].patch is None


# =======================================================================
# 6. 验收反例证据 (P1-1 & 6b)
# =======================================================================

def test_acceptance_6a_er_h02_localization_and_full_repair_branch(tmp_path: Path):
    """验收 6(a):
    1. 定位 er_h02 在 evaluation_sets 数据集中的分布：
       覆盖 repair_set.json、baseline_seen_regression.json 与 baseline_hidden.json；
       严格断言其不在 experiment_holdout.json 与 final_audit.json 中。
    2. 走真实 repair 层完整 patch 生成分支。
    """
    repair_data = load_json_dataset(EVAL_DIR / "repair_set.json")
    seen_reg_data = load_json_dataset(EVAL_DIR / "baseline_seen_regression.json")
    hidden_data = load_json_dataset(EVAL_DIR / "baseline_hidden.json")
    holdout_data = load_json_dataset(EVAL_DIR / "experiment_holdout.json")
    audit_data = load_json_dataset(EVAL_DIR / "final_audit.json")

    repair_cases = {c["id"]: c for c in repair_data["cases"]}
    seen_reg_ids = {c["id"] for c in seen_reg_data["cases"]}
    hidden_ids = {c["id"] for c in hidden_data["cases"]}
    holdout_ids = {c["id"] for c in holdout_data["cases"]}
    audit_ids = {c["id"] for c in audit_data["cases"]}

    # 【P1-1 数据隔离断言全面覆盖】
    assert "er_h02" in repair_cases, "er_h02 必须存在于 repair_set.json"
    assert "er_h02" in seen_reg_ids, "er_h02 同时存在于 baseline_seen_regression.json"
    assert "er_h02" in hidden_ids, "er_h02 同时存在于 baseline_hidden.json"
    assert "er_h02" not in holdout_ids, "er_h02 严禁泄漏到 experiment_holdout.json"
    assert "er_h02" not in audit_ids, "er_h02 严禁泄漏到 final_audit.json"

    er_case = repair_cases["er_h02"]
    assert er_case["skill"] == "explain_regex"

    # 构造并走真实 repair 层完整 patch 分支
    skills_dir = tmp_path / "skills" / "explain_regex"
    skills_dir.mkdir(parents=True, exist_ok=True)
    body = REPO_ROOT.joinpath("skills", "explain_regex", "SKILL.md").read_text(encoding="utf-8")
    (skills_dir / "SKILL.md").write_text(body, encoding="utf-8")

    registry = SkillRegistry(db_path=tmp_path / "t.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    registry.load_skills_from_dir()
    shutil.copytree(REPO_ROOT / "evaluation_sets", tmp_path / "evaluation_sets")

    # 构造评估器：返回包含 er_h02 失败样本的有效 EvalResult
    eval_result = EvalResult(
        release_id="base_er_h02",
        structure_score={"schema": 15.0},
        effect_score={"task": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[
            {"case_id": "er_h02", "query": er_case["query"], "task_completion": "B_better", "readability": "B_better"}
        ],
        case_outputs=[
            {
                "case_id": "er_h02",
                "query": er_case["query"],
                "reference": er_case["reference"],
                "output_skill": "\\1 就是反向引用",
                "output_baseline": "反向引用 \\1 用于匹配与前面第 1 个捕获分组完全相同的内容...",
            }
        ],
        valid=True,
        invalid_reasons=[],
        hit_layer="rule",
        verdict="ROUTE_MATCH",
        matched_keywords=["正则", "讲解"],
        routing_notes="规则层唯一命中",
    )

    # 构造合法的新版 SKILL.md 用于生成 L1 patch
    updated_skill_md = body.replace(
        "version: 1.0.0", "version: 1.0.1", 1
    ).replace(
        "## Examples",
        "## Examples\n- 讲讲反向引用 \\1 的作用与原理",
        1,
    )

    evolver_llm = CapturingFakeLLM([
        # 1. 根因分析：识别为 prompt_vague
        json.dumps({
            "prompt_vague": {"prob": 0.85, "why": "Instructions 缺失反向引用详细解释"},
            "trigger_inaccurate": {"prob": 0.05, "why": ""},
            "deps_broken": {"prob": 0.0, "why": ""},
            "boundary_missing": {"prob": 0.05, "why": ""},
            "eval_noise": {"prob": 0.05, "why": ""},
        }),
        # 2. 生成候选 patch
        json.dumps([
            {
                "level": "L1",
                "rationale": "补充反向引用 \\1 示例到 Examples 中",
                "new_skill_md": updated_skill_md,
            }
        ]),
    ])

    class StableAgentLLM:
        model = "er-h02-candidate-agent"

        def invoke(self, messages, **kwargs):
            return SimpleNamespace(content="候选评估回答：完整解释反向引用。", usage={"total_tokens": 20})

    class StableJudgeLLM:
        model = "er-h02-candidate-judge"

        def invoke(self, messages, **kwargs):
            return SimpleNamespace(
                content=json.dumps({
                    "verdict": "tied",
                    "reason_codes": ["EVIDENCE_SUFFICIENT"],
                    "evidence_summary": "候选回答完成了用例要求",
                }),
                usage={"total_tokens": 20},
            )

    class StaticRouter:
        def route(self, query):
            return RouteResult(
                chosen="explain_regex",
                hit_layer="rule",
                scores={"rule": {"explain_regex": 1.0}},
                latency_ms=0.1,
                matched_keywords=["正则", "讲解"],
                routing_notes="测试用稳定 Router 提供真实 RouteResult 形状",
            )

    evaluator = SimpleNamespace(
        evaluate_skill=lambda *args, **kwargs: eval_result,
        output_cache=None,
        llm=StableAgentLLM(),
        judge=SimpleNamespace(llm=StableJudgeLLM()),
        router=StaticRouter(),
    )

    evolver = SkillEvolver(
        registry=registry,
        evaluator=evaluator,
        llm=evolver_llm,
    )

    outcome = evolver.evolve_full("explain_regex", eval_set_for_iter="repair_set", max_candidates=1, verbose=False)

    # 真实候选 SkillEvaluator 与真实 P0 gate 链均未 monkeypatch；Router 使用稳定的真实 RouteResult 形状替身。
    assert outcome.patches_generated >= 1
    assert len(outcome.records) >= 1
    assert outcome.records[0].patch is not None
    assert outcome.records[0].patch.level == "L1"
    assert outcome.records[0].status in ("PUBLISHED", "REVIEW", "SUGGESTION")


def test_acceptance_6b_dependency_failure_generates_no_patch_and_outputs_review(tmp_path: Path):
    """验收 6(b):
    构造 dependency 类失败（如外部工具 503 超时/缺失）→
    断言不产出文案 patch（patches_generated == 0，无任何 prompt 提案），
    直接输出 REVIEW 诊断归档！
    """
    # 初始化受测环境
    skills_dir = tmp_path / "skills" / "weather_query"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_content = """---
name: weather_query
version: 1.0.0
description: 查询天气
use_when: 查询天气
not_for: []
dependencies:
  - amap_weather_api
trigger:
  keywords:
    - 天气
examples:
  - 北京天气
evaluation:
  last_score: null
  last_release_id: null
---

## Overview
天气查询 Skill

## Instructions
1. 调用 amap_weather_api 工具查询天气
2. 返回气温与天气

## Examples
Q: 北京天气
A: 晴天 20度

## Constraints
禁止编造虚假天气
"""
    (skills_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

    registry = SkillRegistry(db_path=tmp_path / "t.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    registry.load_skills_from_dir()

    # 构造一个因外部工具 503 导致失败的基线评估结果
    bad_provenance = ToolCallProvenance(
        tool_name="amap_weather_api",
        fixture_case_id="wq_d01",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=False,
        authenticity_pass=False,
        input_params={"city": "北京"},
        output_status="ERROR",
        output_summary="503 Service Unavailable: Remote weather server down",
        latency_ms=5000.0,
        timestamp="2026-09-05T00:00:00Z",
        signature="mock_sig",
    )

    baseline_eval = EvalResult(
        release_id="base_01",
        structure_score={"schema": 10.0},
        effect_score={"task": 0.0},
        objective_metrics={},
        p0_pass=False,
        case_verdicts=[
            {"case_id": "wq_d01", "task_completion": "B_better", "robustness": "B_better"}
        ],
        case_outputs=[
            {
                "case_id": "wq_d01",
                "query": "北京今天天气",
                "reference": "返回北京实时天气",
                "output_skill": "服务不可用 503",
                "output_baseline": "北京今天晴天 22℃",
            }
        ],
        validation_channels=["tools", "execution"],
        provenances=[bad_provenance],
        valid=False,
        invalid_reasons=["503 Service Unavailable connecting to amap_weather_api"],
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return baseline_eval

    evaluator = MockEvaluator()

    llm = CapturingFakeLLM([
        json.dumps({
            "deps_broken": {"prob": 0.95, "why": "amap_weather_api 报 503 错误，外部服务不可用"},
            "prompt_vague": {"prob": 0.05, "why": ""},
            "trigger_inaccurate": {"prob": 0.0, "why": ""},
            "boundary_missing": {"prob": 0.0, "why": ""},
            "eval_noise": {"prob": 0.0, "why": ""},
        })
    ])

    evolver = SkillEvolver(
        registry=registry,
        evaluator=evaluator,
        llm=llm,
    )

    outcome = evolver.evolve_full(
        skill_name="weather_query",
        eval_set_for_iter="repair",
        max_candidates=3,
        verbose=True,
    )

    # ========== 核心断言 ==========
    assert outcome.patches_generated == 0, f"预期 patches_generated 为 0，实际为 {outcome.patches_generated}"
    assert len(outcome.patches_published) == 0, "依赖故障绝不应发布 patch"
    assert len(outcome.patches_review) == 1, f"预期进入 REVIEW 诊断归档，实际 review 列表: {outcome.patches_review}"
    diag_file = Path(outcome.patches_review[0])
    assert diag_file.exists(), f"诊断归档文件不存在: {diag_file}"

    report_content = diag_file.read_text(encoding="utf-8")
    assert "[REVIEW / DEPENDENCY_DIAGNOSTIC] weather_query" in report_content
    assert "- status: REVIEW" in report_content
    assert "- patch_generated: false" in report_content
    assert "deps_broken" in report_content
    assert "修改 Prompt/SKILL.md 文案对解决依赖问题无效" in report_content
    assert "diff" not in report_content.lower() or "（诊断归档：外部依赖问题，不产出文案 patch" in report_content

    assert len(outcome.records) == 1
    assert outcome.records[0].status == "REVIEW"
    assert outcome.records[0].patch is None
