"""P0-B: validators are selected by the semantic change surface."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillforge.diff import compute_semantic_diff
from skillforge.evolver import _archive_suggestion, _publish_patch, _validate_patch
from skillforge.evaluator.validators import (
    validate_dependency_patch,
    validate_router_patch,
)
from skillforge.models import EvalResult, Patch, RatchetVerdict, RouteResult
from skillforge.registry import SkillRegistry


class ToolCallingLLM:
    """Scripted function-calling model for the dependency harness."""

    model = "gpt-3.5-turbo"

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        if messages[-1]["role"] == "tool":
            payload = json.loads(messages[-1]["content"])
            forecast = payload["forecasts"][0]
            cast = forecast["casts"][0]
            content = " ".join(
                str(value)
                for value in (
                    forecast["city"],
                    cast["date"],
                    cast["dayweather"],
                    cast["nightweather"],
                    cast["daytemp"],
                    cast["nighttemp"],
                    cast["daywind"],
                    cast["daypower"],
                )
            )
            message = SimpleNamespace(content=content, tool_calls=[])
        else:
            query = messages[-1]["content"]
            city = next(
                city for city in ("北京市", "上海市", "广州市", "深圳市") if city in query
            )
            tool_call = SimpleNamespace(
                id="fixture-call-1",
                function=SimpleNamespace(
                    name="amap_weather_api",
                    arguments=json.dumps(
                        {"city": city, "extensions": "all"}, ensure_ascii=False
                    ),
                ),
            )
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=None,
        )

    def invoke(self, messages, **kwargs):
        return SimpleNamespace(content="", usage=None)


class NoToolLLM(ToolCallingLLM):
    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        message = SimpleNamespace(content="凭常识编造：晴，30度", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=None,
        )


class DoubleToolLLM(ToolCallingLLM):
    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        tool_messages = [message for message in messages if message["role"] == "tool"]
        if len(tool_messages) != 1:
            return super().invoke_with_tools(messages, tools, tool_choice, **kwargs)
        payload = json.loads(tool_messages[0]["content"])
        city = payload["forecasts"][0]["city"]
        tool_call = SimpleNamespace(
            id="fixture-call-2",
            function=SimpleNamespace(
                name="amap_weather_api",
                arguments=json.dumps(
                    {"city": city, "extensions": "all"}, ensure_ascii=False
                ),
            ),
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=None,
        )


def _old_result() -> EvalResult:
    return EvalResult(
        release_id="baseline",
        structure_score={"schema": 15.0},
        effect_score={"task": 25.0},
        objective_metrics={},
        p0_pass=True,
    )


def _patch(old_md: str, new_md: str, level: str) -> Patch:
    semantic = compute_semantic_diff(old_md, new_md, level)
    assert semantic.is_valid
    return Patch(
        skill_name=semantic.skill_name,
        level=level,
        diff=new_md,
        rationale="acceptance",
        computed_level=semantic.computed_level,
        unified_diff=semantic.unified_diff,
        downgrade_attempt=semantic.downgrade_attempt,
        changed_frontmatter=semantic.changed_frontmatter,
        changed_body_sections=semantic.changed_body_sections,
    )


def test_metadata_l1_triggers_router_and_p0_gate(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(
        db_path=tmp_path / "registry.db",
        skills_dir=repo_root / "skills",
        repo_root=repo_root,
        router_log=tmp_path / "router.jsonl",
    )
    registry.load_skills_from_dir()
    old_md = (repo_root / "skills" / "write_weekly_report" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    new_md = old_md.replace("version: 1.0.0", "version: 1.0.1", 1).replace(
        "  - 写周计划（周报是回顾，不是规划）",
        "  - 写周计划（周报是回顾，不是规划）\n  - 撰写会议议程",
        1,
    )
    patch = _patch(old_md, new_md, "L1")
    calls: list[str] = []

    def router_pass(*args, **kwargs):
        calls.append("router")
        return RatchetVerdict(decision="PASS")

    monkeypatch.setattr("skillforge.evolver.validate_router_patch", router_pass)

    class FakeEvaluatorWithP0:
        llm = ToolCallingLLM()

        def __init__(self, *args, **kwargs):
            pass

        def evaluate_skill(self, skill_name, eval_set=None, cases=None, p0_ids=None, verbose=False):
            if eval_set != "p0_cases":
                raise AssertionError(f"metadata-only patch unexpectedly evaluated non-p0 eval_set: {eval_set}")
            calls.append("p0_gate")
            case_verdicts = [
                {"case_id": c["id"], "task_completion": "tied", "robustness": "tied", "readability": "tied"}
                for c in (cases or [])
            ]
            return EvalResult(
                release_id="candidate",
                structure_score={"schema": 15.0},
                effect_score={"task": 25.0, "robust": 15.0, "readability": 10.0, "efficiency": 10.0},
                objective_metrics={},
                p0_pass=True,
                case_verdicts=case_verdicts,
                valid=True,
            )

    monkeypatch.setattr("skillforge.evaluator.SkillEvaluator", FakeEvaluatorWithP0)

    try:
        result, verdict = _validate_patch(
            FakeEvaluatorWithP0(),
            registry,
            "write_weekly_report",
            patch,
            _old_result(),
            "repair_set",
        )
    finally:
        registry.close()

    assert patch.changed_frontmatter == ["not_for"]
    assert patch.changed_body_sections == []
    assert calls == ["router", "p0_gate"]
    assert verdict.decision == "PASS"
    assert result.validation_channels == ["router"]
    assert result.p0_pass is True


def test_metadata_broadening_caught_by_hard_negatives(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fallback_modes: list[bool] = []

    class FakeRouter:
        def __init__(self, registry, llm=None, model_dir=None):
            self.broadened = registry.broadened
            self.fallback = model_dir is not None
            fallback_modes.append(self.fallback)

        def route(self, query: str) -> RouteResult:
            chosen = (
                "write_weekly_report"
                if self.broadened
                and self.fallback
                and query in {"帮我写会议纪要", "讲一下 lookahead 是什么"}
                else None
            )
            return RouteResult(
                chosen=chosen,
                hit_layer="rule",
                scores={"rule": {}, "embed": {}},
                latency_ms=0.0,
            )

    monkeypatch.setattr("skillforge.evaluator.validators.IntentRouter", FakeRouter)
    baseline = SimpleNamespace(repo_root=repo_root, broadened=False)
    candidate = SimpleNamespace(repo_root=repo_root, broadened=True)

    verdict = validate_router_patch(
        baseline,
        candidate,
        "write_weekly_report",
        changed_frontmatter=["trigger"],
        computed_level="L2",
    )

    assert verdict.decision == "DECLINED"
    assert fallback_modes == [False, False, True, True]
    assert any("wr_n_01" in reason for reason in verdict.reasons)
    assert any("er_p_07" in reason for reason in verdict.reasons)


def test_body_l2_triggers_behavior_evaluator_only(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(
        db_path=tmp_path / "registry.db",
        skills_dir=repo_root / "skills",
        repo_root=repo_root,
        router_log=tmp_path / "router.jsonl",
    )
    registry.load_skills_from_dir()
    old_md = (repo_root / "skills" / "explain_regex" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    new_md = old_md.replace("version: 1.0.0", "version: 1.0.1", 1).replace(
        "3. 优先用小例子",
        "3. 先复述用户要理解的模式。\n4. 优先用小例子",
        1,
    )
    patch = _patch(old_md, new_md, "L2")
    calls: list[str] = []

    def router_must_not_run(*args, **kwargs):
        raise AssertionError("body-only patch reached Router validator")

    class FakeSkillEvaluator:
        def __init__(self, registry, llm, judge_llm):
            pass

        def _load_cases(self, eval_set, skill_name):
            return [{"id": "case", "skill": skill_name, "query": "q"}]

        def evaluate_skill(self, *args, **kwargs):
            if "behavior" not in calls:
                calls.append("behavior")
            res = _old_result()
            res.case_verdicts = [
                {"case_id": "er_d01", "task_completion": "A_better", "robustness": "tied", "readability": "tied"},
                {"case_id": "er_d02", "task_completion": "tied", "robustness": "tied", "readability": "tied"},
                {"case_id": "er_d04", "task_completion": "tied", "robustness": "tied", "readability": "tied"},
            ]
            return res

    monkeypatch.setattr("skillforge.evolver.validate_router_patch", router_must_not_run)
    monkeypatch.setattr("skillforge.evaluator.SkillEvaluator", FakeSkillEvaluator)

    try:
        result, verdict = _validate_patch(
            SimpleNamespace(llm=object(), judge=SimpleNamespace(llm=object())),
            registry,
            "explain_regex",
            patch,
            _old_result(),
            "repair_set",
        )
    finally:
        registry.close()

    assert patch.changed_frontmatter == []
    assert patch.changed_body_sections == ["Instructions"]
    assert calls == ["behavior"]
    assert result.validation_channels == ["behavior"]
    assert verdict.decision == "PASS"


def test_dependency_l3_triggers_fixture_and_records_provenance(
    tmp_path: Path
) -> None:
    skill_dir = tmp_path / "skills" / "weather_query"
    skill_dir.mkdir(parents=True)
    old_md = """---
name: weather_query
version: 1.0.0
description: 查询天气
use_when: 用户查询天气
dependencies: []
trigger:
  keywords: [天气]
---

## Overview

查询天气。
"""
    new_md = old_md.replace("version: 1.0.0", "version: 1.0.1", 1).replace(
        "dependencies: []", "dependencies: [amap_weather_api]", 1
    )
    (skill_dir / "SKILL.md").write_text(old_md, encoding="utf-8")
    registry = SkillRegistry(
        db_path=tmp_path / "registry.db",
        skills_dir=tmp_path / "skills",
        repo_root=tmp_path,
        router_log=tmp_path / "router.jsonl",
    )
    registry.load_skills_from_dir()
    patch = _patch(old_md, new_md, "L3")

    class EvaluatorMustNotRun:
        llm = ToolCallingLLM()

        def evaluate_skill(self, *args, **kwargs):
            raise AssertionError("dependency-only patch reached behavior evaluator")

    try:
        result, verdict = _validate_patch(
            EvaluatorMustNotRun(),
            registry,
            "weather_query",
            patch,
            _old_result(),
            "repair_set",
        )
    finally:
        registry.close()

    assert verdict.decision == "PASS"
    assert result.validation_channels == ["dependency_fixture"]
    assert len(result.provenances) == len(patch.provenances) == 1
    provenance = patch.provenances[0]
    assert provenance.tool_name == "amap_weather_api"
    assert provenance.is_fixture is True
    assert provenance.fixture_case_id == "amap_weather_api.smoke.v1"
    assert provenance.input_params["city"].endswith("市")
    assert provenance.input_params["extensions"] == "all"
    assert provenance.tool_required is True
    assert provenance.tool_called is True
    assert provenance.tool_success is True
    assert provenance.authenticity_pass is True
    assert provenance.output_status == "SUCCESS"
    assert len(provenance.signature) == 64

    archive = _archive_suggestion(tmp_path, "weather_query", patch, verdict, result)
    archive_text = archive.read_text(encoding="utf-8")
    assert "## 工具调用存据 (Tool Call Provenance)" in archive_text
    assert "amap_weather_api" in archive_text
    assert provenance.signature in archive_text

    outcome = _publish_patch(
        tmp_path,
        registry=object(),
        state_machine=object(),
        skill_name="weather_query",
        patch=patch,
        verdict=verdict,
        new_result=result,
    )
    assert outcome["status"] == "REVIEW"

    removal_verdict, removal_provenance = validate_dependency_patch(
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=["amap_weather_api"])
        ),
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=[]),
            _bodies={"weather_query": "不再调用天气工具"},
        ),
        "weather_query",
    )
    assert removal_verdict.decision == "PASS"
    assert len(removal_provenance) == 1
    assert removal_provenance[0].tool_required is False
    assert removal_provenance[0].tool_called is False
    assert removal_provenance[0].tool_success is False
    assert removal_provenance[0].authenticity_pass is True

    no_tool_verdict, no_tool_provenance = validate_dependency_patch(
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=[])
        ),
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=["amap_weather_api"]),
            _bodies={"weather_query": "禁止调用工具；凭常识编造实时天气"},
        ),
        "weather_query",
        llm=NoToolLLM(),
    )
    assert no_tool_verdict.decision == "DECLINED"
    assert no_tool_provenance[0].tool_called is False
    assert no_tool_provenance[0].authenticity_pass is False

    double_verdict, double_provenance = validate_dependency_patch(
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=[])
        ),
        SimpleNamespace(
            get_meta=lambda name: SimpleNamespace(dependencies=["amap_weather_api"]),
            _bodies={"weather_query": "调用天气工具并如实回答"},
        ),
        "weather_query",
        llm=DoubleToolLLM(),
    )
    assert double_verdict.decision == "PASS"
    assert len(double_provenance) == 2
    assert [item.call_index for item in double_provenance] == [1, 2]
    assert all(item.call_count == 2 for item in double_provenance)


def test_pseudo_l1_rewrite_blocked_to_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    old_md = (repo_root / "skills" / "explain_regex" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    new_md = old_md.replace("version: 1.0.0", "version: 1.0.1", 1).replace(
        "1. 识别用户问的是：", "1. 完全重写并扩展回答：", 1
    )
    patch = _patch(old_md, new_md, "L1")

    def publish_must_not_run(*args, **kwargs):
        raise AssertionError("downgrade attempt reached automatic publishing")

    monkeypatch.setattr("skillforge.evolver._apply_and_publish_L1", publish_must_not_run)
    outcome = _publish_patch(
        tmp_path,
        registry=object(),
        state_machine=object(),
        skill_name="explain_regex",
        patch=patch,
        verdict=RatchetVerdict(decision="PASS"),
        new_result=_old_result(),
    )

    assert patch.computed_level == "L2"
    assert patch.downgrade_attempt is True
    assert outcome["status"] == "REVIEW"
    assert "[REVIEW / 降级拦截 / L1→L2]" in Path(outcome["path"]).read_text(
        encoding="utf-8"
    )
