"""单元测试：P2-A Skill 生成器 (结构校验 / 路由冲突拒绝 / 重名拒绝 / 成功路径 mock LLM / 注册落盘)"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skillforge import (
    SkillMeta,
    Trigger,
    generate_skill,
    register_skill,
    GeneratedSkill,
    GenerationFailure,
    derive_skill_abbrev,
    validate_generated_structure,
    check_conflict,
    RegistrationError,
    EvolveBudget,
    LLMLedger,
)
from skillforge.skill_generator import (
    check_route_conflict_embedding,
    check_route_conflict_keywords,
    check_route_conflict_llm,
)


class MockLLM:
    def __init__(self, response_content: str):
        self.response_content = response_content
        self.invocations: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.invocations.append(messages)
        return SimpleNamespace(content=self.response_content)


class SequenceLLM:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.invocations: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.invocations.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(content=response)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def valid_skill_md() -> str:
    return """---
name: mock_tool_helper
version: 1.0.0
description: 专门提供关于模拟工具调用的指导和最佳实践说明
use_when: 用户想了解如何在测试中编写高保真 Mock 工具或验证调用凭据
not_for:
  - 生产环境真实网络 API 调试
  - 数据库直接连接与运维
dependencies: []
trigger:
  keywords:
    - 模拟工具
    - mock
    - 凭证校验
examples:
  - 怎么写一个 mock 的天气工具
  - 如何校验 tool_call 的 signature
evaluation:
  last_score: null
  last_release_id: null
---

## Overview

本技能为开发者提供模拟工具测试套件的设计指导。

## Instructions

1. 识别用户使用的测试框架（pytest 或 unittest）。
2. 提供零网络依赖的工具类实现。
3. 演示如何构造 ToolCallProvenance 结构。

## Examples

**Q**：怎么写一个 mock 工具？
**A**：通过继承 Tool 并提供固定输入输出映射。

## Constraints

- 严禁建议用户在单元测试中发起真实 HTTP 请求。
- 必须明确说明 mock 不等同于真实线上行为。
"""


class TestSkillGeneratorUnit:
    """① 单元测试：生成器结构校验 / 路由冲突拒绝 / 重名拒绝 / 成功路径 mock LLM"""

    def test_derive_skill_abbrev(self):
        assert derive_skill_abbrev("http_status_code") == "hsc"
        assert derive_skill_abbrev("markdown_cheatsheet") == "mc"
        assert derive_skill_abbrev("weather_query") == "wq"
        assert derive_skill_abbrev("regex") == "reg"

    def test_validate_structure_success(self, valid_skill_md):
        ok, msg, meta, fm, body = validate_generated_structure(valid_skill_md, existing_names=set())
        assert ok is True
        assert msg == "OK"
        assert meta is not None
        assert meta.name == "mock_tool_helper"
        assert meta.version == "1.0.0"
        assert len(meta.not_for) == 2

    def test_validate_structure_invalid_name(self, valid_skill_md):
        # 非法命名：大写或连字符
        bad_md = valid_skill_md.replace("name: mock_tool_helper", "name: Mock-Tool")
        ok, msg, _, _, _ = validate_generated_structure(bad_md, existing_names=set())
        assert ok is False
        assert "不合法" in msg

    def test_validate_structure_duplicate_name(self, valid_skill_md):
        # 与已有 skill 重名
        ok, msg, _, _, _ = validate_generated_structure(valid_skill_md, existing_names={"mock_tool_helper"})
        assert ok is False
        assert "重名冲突" in msg

    def test_validate_structure_wrong_version(self, valid_skill_md):
        # 版本必须为 1.0.0
        bad_md = valid_skill_md.replace("version: 1.0.0", "version: 2.0.0")
        ok, msg, _, _, _ = validate_generated_structure(bad_md, existing_names=set())
        assert ok is False
        assert "version 必须为 '1.0.0'" in msg

    def test_validate_structure_missing_frontmatter_fields(self, valid_skill_md):
        # 缺少 not_for
        bad_md = valid_skill_md.replace("not_for:\n  - 生产环境真实网络 API 调试\n  - 数据库直接连接与运维\n", "")
        ok, msg, _, _, _ = validate_generated_structure(bad_md, existing_names=set())
        assert ok is False
        assert "not_for" in msg

    def test_validate_structure_missing_body_section(self, valid_skill_md):
        # 缺少 ## Constraints
        bad_md = valid_skill_md.split("## Constraints")[0]
        ok, msg, _, _, _ = validate_generated_structure(bad_md, existing_names=set())
        assert ok is False
        assert "## Constraints" in msg

    def test_validate_structure_ignores_fenced_fake_sections(self, valid_skill_md):
        frontmatter = valid_skill_md.split("## Overview", 1)[0]
        fake_body = "```markdown\n## Overview\nA\n## Instructions\nB\n## Examples\nC\n## Constraints\nD\n```"
        ok, msg, _, _, _ = validate_generated_structure(frontmatter + fake_body, existing_names=set())
        assert ok is False
        assert "缺少必须的段落" in msg or "段落" in msg

    def test_validate_structure_rejects_extra_top_level_and_accepts_known_variants(self, valid_skill_md):
        extra = valid_skill_md + "\n## Extra\n不应被注册\n"
        ok, msg, _, _, _ = validate_generated_structure(extra, existing_names=set())
        assert ok is False
        assert "额外顶层节" in msg

        variant = valid_skill_md.replace("## Overview", "## overview（概述）")
        variant = variant.replace("## Instructions", "## Instructions (说明)")
        variant = variant.replace("## Examples", "## examples")
        variant = variant.replace("## Constraints", "## Constraints:")
        ok, msg, meta, _, _ = validate_generated_structure(variant, existing_names=set())
        assert ok is True, msg
        assert meta is not None

    def test_validate_structure_requires_document_frontmatter_contract(self, valid_skill_md):
        missing_dependencies = valid_skill_md.replace("dependencies: []\n", "")
        ok, msg, _, _, _ = validate_generated_structure(missing_dependencies, existing_names=set())
        assert ok is False
        assert "dependencies" in msg

        nonempty_dependencies = valid_skill_md.replace("dependencies: []", "dependencies:\n  - runtime_tool")
        ok, msg, _, _, _ = validate_generated_structure(nonempty_dependencies, existing_names=set())
        assert ok is False
        assert "依赖" in msg or "dependencies" in msg

    def test_conflict_checker_is_fail_closed_for_unknown_and_broken_methods(self):
        candidate = SkillMeta(
            name="safe_candidate",
            version="1.0.0",
            description="一个独立的文档解释技能",
            use_when="用户需要理解独立概念时",
            trigger=Trigger(keywords=["独立", "概念", "解释"]),
            not_for=["代码生成", "生产操作"],
        )
        existing = [
            SkillMeta(
                name="existing_skill",
                version="1.0.0",
                description="另一个已注册文档技能",
                use_when="用户需要另一个领域的说明",
                trigger=Trigger(keywords=["另一个", "领域", "说明"]),
                not_for=["执行操作", "写代码"],
            )
        ]
        unknown, unknown_reason = check_conflict(candidate, existing, method="not-a-method")
        assert unknown is True
        assert "拒绝" in unknown_reason

        broken = MockLLM("not-json")
        failed, failed_reason = check_conflict(candidate, existing, method="llm", llm=broken)
        assert failed is True
        assert "fail-closed" in failed_reason or "拒绝" in failed_reason

    @pytest.mark.parametrize("response", ["null", "[]", '{"skill_md": 1}', "{not-json"])
    def test_generate_malformed_llm_payload_returns_failure_with_retry(self, repo_root, response):
        llm = MockLLM(response)
        result = generate_skill("一个新的文档技能", llm=llm, repo_root=repo_root, max_retries=1)
        assert isinstance(result, GenerationFailure)
        assert result.reason == "LLM_ERROR"
        assert result.details is not None
        assert result.details["attempts"] == 2
        assert len(llm.invocations) == 2

    def test_generate_retries_with_shared_ledger_and_hard_budget(self, repo_root, valid_skill_md):
        payload = {
            "skill_md": valid_skill_md,
            "test_cases": [
                {"query": "理解 mock 工具结构", "reference": "解释 mock 工具结构与测试边界"},
                {"query": "校验 tool call 凭据", "reference": "说明凭据校验步骤"},
                {"query": "生产环境部署 mock 服务", "reference": "拒绝并说明超出范围"},
            ],
        }
        llm = SequenceLLM(["{broken", json.dumps(payload, ensure_ascii=False)])
        ledger = LLMLedger(EvolveBudget(max_calls=2, max_tokens=1000, deadline_seconds=30))
        result = generate_skill(
            "模拟工具说明",
            llm=llm,
            repo_root=repo_root,
            ledger=ledger,
            max_retries=1,
        )
        assert isinstance(result, GeneratedSkill)
        assert len(llm.invocations) == 2
        assert ledger.total_calls == 2
        assert result.test_cases[-1]["difficulty"] == "hard"

    def test_failed_provider_attempt_consumes_generator_call_budget(self, repo_root, valid_skill_md):
        payload = {
            "skill_md": valid_skill_md,
            "test_cases": [
                {"query": "理解 mock 工具结构", "reference": "解释 mock 工具结构与测试边界"},
                {"query": "校验 tool call 凭据", "reference": "说明凭据校验步骤"},
                {"query": "生产环境部署 mock 服务", "reference": "拒绝并说明超出范围"},
            ],
        }
        llm = SequenceLLM([RuntimeError("provider timeout"), json.dumps(payload, ensure_ascii=False)])
        ledger = LLMLedger(EvolveBudget(max_calls=1, max_tokens=1000, deadline_seconds=30))
        result = generate_skill(
            "模拟工具说明",
            llm=llm,
            repo_root=repo_root,
            ledger=ledger,
            max_retries=1,
        )
        assert isinstance(result, GenerationFailure)
        assert result.reason == "BUDGET_EXCEEDED"
        assert len(llm.invocations) == 1
        assert ledger.total_calls == 1
        assert ledger.failed_calls == 1
        assert ledger.records[0].status == "error"

    def test_register_missing_router_is_atomic(self, tmp_path: Path, valid_skill_md):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        eval_dir = tmp_path / "evaluation_sets"
        eval_dir.mkdir()
        repair_file = eval_dir / "repair_set.json"
        repair_file.write_text(
            json.dumps(
                {
                    "meta": {"total": 5, "auto_case_ids": ["old_auto_01"], "auto_case_count": 1},
                    "cases": [
                        {"id": "old_auto_01", "skill": "old_skill", "query": "q", "reference": "r", "trace_id": "t"},
                        *[
                            {"id": f"base_{i}", "skill": "old_skill", "query": f"b{i}", "reference": f"r{i}"}
                            for i in range(1, 5)
                        ],
                    ],
                }
            ),
            encoding="utf-8",
        )
        ok, _, parsed_meta, fm, body = validate_generated_structure(valid_skill_md, set())
        assert ok and parsed_meta is not None
        generated = GeneratedSkill(
            name=parsed_meta.name,
            version=parsed_meta.version,
            description=parsed_meta.description,
            use_when=parsed_meta.use_when,
            not_for=parsed_meta.not_for,
            frontmatter_raw=fm or "",
            body_raw=body or "",
            full_skill_md=valid_skill_md,
            test_cases=[
                {"id": "mth_auto_01", "skill": parsed_meta.name, "query": "q1", "reference": "r1", "trace_id": "t1"},
                {"id": "mth_auto_02", "skill": parsed_meta.name, "query": "q2", "reference": "r2", "trace_id": "t2"},
            ],
            meta=parsed_meta,
        )
        with pytest.raises(RegistrationError, match="router_negatives"):
            register_skill(generated, repo_root=tmp_path, repair_set_path=repair_file)
        assert not (skills_dir / parsed_meta.name).exists()
        assert json.loads(repair_file.read_text(encoding="utf-8"))["meta"]["total"] == 5

    def test_register_rejects_auto_ratio_and_prefix_collision_without_writes(self, tmp_path: Path, valid_skill_md):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        eval_dir = tmp_path / "evaluation_sets"
        eval_dir.mkdir()
        repair_file = eval_dir / "repair_set.json"
        router_file = eval_dir / "router_negatives.json"
        base_cases = [
            {"id": "old_auto_01", "skill": "old_skill", "query": "q", "reference": "r", "trace_id": "t"},
            *[
                {"id": f"base_{i}", "skill": "old_skill", "query": f"b{i}", "reference": f"r{i}"}
                for i in range(1, 5)
            ],
        ]
        repair_file.write_text(
            json.dumps({"meta": {"total": 5, "auto_case_ids": ["old_auto_01"], "auto_case_count": 1}, "cases": base_cases}),
            encoding="utf-8",
        )
        router_file.write_text(json.dumps({"meta": {}, "cases": []}), encoding="utf-8")
        ok, _, meta, fm, body = validate_generated_structure(valid_skill_md, set())
        assert ok and meta is not None

        ratio_generated = GeneratedSkill(
            name=meta.name,
            version=meta.version,
            description=meta.description,
            use_when=meta.use_when,
            not_for=meta.not_for,
            frontmatter_raw=fm or "",
            body_raw=body or "",
            full_skill_md=valid_skill_md,
            test_cases=[
                {"id": f"mth_auto_{i}", "skill": meta.name, "query": f"q{i}", "reference": f"r{i}", "trace_id": f"t{i}"}
                for i in range(1, 5)
            ],
            meta=meta,
        )
        with pytest.raises(RegistrationError, match="占比"):
            register_skill(ratio_generated, repo_root=tmp_path, repair_set_path=repair_file, router_negatives_path=router_file)
        assert not (skills_dir / meta.name).exists()

        collision_repair = json.loads(repair_file.read_text(encoding="utf-8"))
        collision_repair["cases"][0]["id"] = "mth_auto_01"
        collision_repair["meta"]["auto_case_ids"] = ["mth_auto_01"]
        repair_file.write_text(json.dumps(collision_repair), encoding="utf-8")
        collision_md = valid_skill_md.replace("mock_tool_helper", "mock_test_helper")
        ok, _, collision_meta, fm, body = validate_generated_structure(collision_md, set())
        assert ok and collision_meta is not None
        collision_generated = GeneratedSkill(
            name=collision_meta.name,
            version=collision_meta.version,
            description=collision_meta.description,
            use_when=collision_meta.use_when,
            not_for=collision_meta.not_for,
            frontmatter_raw=fm or "",
            body_raw=body or "",
            full_skill_md=collision_md,
            test_cases=[
                {"id": f"mth_auto_{i}", "skill": collision_meta.name, "query": f"c{i}", "reference": f"cr{i}", "trace_id": f"ct{i}"}
                for i in range(1, 3)
            ],
            meta=collision_meta,
        )
        with pytest.raises(RegistrationError, match="前缀"):
            register_skill(collision_generated, repo_root=tmp_path, repair_set_path=repair_file, router_negatives_path=router_file)
        assert not (skills_dir / collision_meta.name).exists()

    def test_register_rejects_prefix_collision_in_holdout_before_writes(self, tmp_path: Path, valid_skill_md):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        eval_dir = tmp_path / "evaluation_sets"
        eval_dir.mkdir()
        repair_file = eval_dir / "repair_set.json"
        router_file = eval_dir / "router_negatives.json"
        holdout_file = eval_dir / "experiment_holdout.json"
        repair_file.write_text(
            json.dumps(
                {
                    "meta": {"total": 5, "auto_case_ids": ["old_auto_01"], "auto_case_count": 1},
                    "cases": [
                        {"id": "old_auto_01", "skill": "old_skill", "query": "q", "reference": "r", "trace_id": "t"},
                        *[
                            {"id": f"base_{i}", "skill": "old_skill", "query": f"b{i}", "reference": f"r{i}"}
                            for i in range(1, 5)
                        ],
                    ],
                }
            ),
            encoding="utf-8",
        )
        router_file.write_text(json.dumps({"meta": {}, "cases": []}), encoding="utf-8")
        holdout_file.write_text(
            json.dumps({"meta": {}, "cases": [{"id": "mth_auto_77", "skill": "another_skill"}]}),
            encoding="utf-8",
        )
        ok, _, meta, fm, body = validate_generated_structure(valid_skill_md, set())
        assert ok and meta is not None
        generated = GeneratedSkill(
            name=meta.name,
            version=meta.version,
            description=meta.description,
            use_when=meta.use_when,
            not_for=meta.not_for,
            frontmatter_raw=fm or "",
            body_raw=body or "",
            full_skill_md=valid_skill_md,
            test_cases=[
                {"id": f"mth_auto_{i}", "skill": meta.name, "query": f"q{i}", "reference": f"r{i}", "trace_id": f"t{i}"}
                for i in range(1, 3)
            ],
            meta=meta,
        )
        with pytest.raises(RegistrationError, match="全局 auto case 前缀"):
            register_skill(generated, repo_root=tmp_path, repair_set_path=repair_file, router_negatives_path=router_file)
        assert not (skills_dir / meta.name).exists()
        assert json.loads(repair_file.read_text(encoding="utf-8"))["meta"]["total"] == 5

    def test_route_conflict_keyword_rejection(self):
        # 关键词撞车已有 skill（weather_query 的 "天气"）
        cand_meta = SkillMeta(
            name="new_weather_helper",
            version="1.0.0",
            description="天气查询辅助技能",
            use_when="查询天气情况",
            trigger=Trigger(keywords=["天气", "实时温度"]),
            not_for=["历史天气"],
        )
        existing_meta = SkillMeta(
            name="weather_query",
            version="1.0.0",
            description="查询指定城市的实时天气与未来 3 天预报",
            use_when="用户询问某城市当前或未来几天的天气、温度、降水情况",
            trigger=Trigger(keywords=["天气", "温度", "下雨"]),
            not_for=["历史天气查询"],
        )
        conflict, reason = check_route_conflict_keywords(cand_meta, [existing_meta])
        assert conflict is True
        assert "天气" in reason

    def test_route_conflict_embedding_rejection(self, repo_root):
        # 语义与现有 explain_regex 高度重叠
        cand_meta = SkillMeta(
            name="regex_breakdown",
            version="1.0.0",
            description="讲解正则表达式语法、匹配过程与回溯原理",
            use_when="用户想理解正则表达式的语法、匹配机制、回溯原理",
            trigger=Trigger(keywords=["正则语法", "回溯机制"]),
            not_for=["代码生成任务", "正则优化"],
        )
        regex_meta = SkillMeta(
            name="explain_regex",
            version="1.0.0",
            description="讲解正则表达式的原理与匹配过程（不是帮用户写正则）",
            use_when="用户想理解正则表达式的语法、匹配机制、回溯原理，或看懂别人写的正则",
            trigger=Trigger(keywords=["正则", "regex", "回溯"]),
            not_for=["写一个正则匹配 XX"],
        )
        conflict, reason = check_route_conflict_embedding(
            cand_meta, [regex_meta], threshold=0.70
        )
        assert conflict is True
        assert "explain_regex" in reason

    def test_route_conflict_llm_mode(self):
        cand_meta = SkillMeta(
            name="test_skill",
            version="1.0.0",
            description="测试技能",
            use_when="测试用例",
            trigger=Trigger(keywords=["测试"]),
            not_for=["生产"],
        )
        mock_llm_conflict = MockLLM(json.dumps({"conflict": True, "reason": "意图与已有技能重叠"}))
        has_conflict, reason = check_conflict(cand_meta, [cand_meta], method="llm", llm=mock_llm_conflict)
        assert has_conflict is True
        assert "重叠" in reason

    def test_generate_skill_success_mock(self, repo_root):
        # 模拟 LLM 成功返回合法的 JSON
        llm_payload = {
            "name": "docker_compose_helper",
            "version": "1.0.0",
            "description": "提供 Docker Compose 配置语法与多容器编排原理的解析指导",
            "use_when": "用户编写或调试 docker-compose.yml 配置文件需要语法与网络排查指导时",
            "not_for": [
                "Kubernetes 生产集群部署与管理",
                "物理机运维与 Linux 内核调优",
            ],
            "keywords": ["docker", "compose", "编排", "容器配置"],
            "examples": [
                "docker-compose 里 depends_on 和 healthcheck 怎么配",
                "networks 桥接模式怎么配固定 IP",
            ],
            "body": """## Overview
深入解释 Docker Compose 配置项与多容器依赖编排原理。

## Instructions
1. 识别用户使用的 Compose 文件版本（v2 或 v3+）。
2. 解析 service, network, volume 各段语义。
3. 提供最小验证配置示例。

## Examples
**Q**：depends_on 和 condition 怎么用？
**A**：通过 service_healthy 确保依赖服务就绪后再启动。

## Constraints
- 明确告知用户不替其远程登录服务器执行 docker 命令。
- 遇 K8s 相关问题引导至对应集群运维工具。
""",
            "test_cases": [
                {"query": "docker-compose 中 ports 和 expose 有什么区别？", "reference": "解释 ports 映射宿主机端口，expose 仅在容器网络间开放；说明适用场景"},
                {"query": "两容器互联应该怎么配置自定义 bridge 网络？", "reference": "给出 networks 字段配置示例并说明 DNS 解析机制"},
                {"query": "帮我部署一套 100 节点的生产 Kubernetes 集群", "reference": "明确说明超出 Docker Compose 教学范围，引导至 K8s 编排领域"},
            ],
        }

        mock_llm = MockLLM(json.dumps(llm_payload))
        result = generate_skill(
            request="Docker Compose 助手",
            llm=mock_llm,
            repo_root=repo_root,
            register=False,
            conflict_method="embedding",
        )

        assert isinstance(result, GeneratedSkill)
        assert result.success is True
        assert result.name == "docker_compose_helper"
        assert result.version == "1.0.0"
        assert len(result.test_cases) == 4
        assert result.test_cases[0]["id"] == "dch_auto_01"
        assert result.test_cases[0]["skill"] == "docker_compose_helper"
        assert "generator:docker_compose_helper:dch_auto_01" in result.test_cases[0]["trace_id"]
        assert result.test_cases[-1]["case_kind"] == "independent_hard_boundary"

    def test_generate_skill_invalid_test_case_count(self, repo_root):
        # 测试用例只有 1 条（少于 3 条，fail-closed）
        payload = {
            "name": "short_case_helper",
            "version": "1.0.0",
            "description": "用例过少的测试技能",
            "use_when": "测试用例数量校验",
            "not_for": ["其它场景", "生产操作"],
            "keywords": ["短用例", "测试校验", "数量"],
            "examples": ["样例一"],
            "body": "## Overview\nA\n## Instructions\nB\n## Examples\nC\n## Constraints\nD",
            "test_cases": [{"query": "query", "reference": "ref"}],
        }
        mock_llm = MockLLM(json.dumps(payload))
        result = generate_skill("测试", llm=mock_llm, repo_root=repo_root)
        assert isinstance(result, GenerationFailure)
        assert result.reason == "INVALID_STRUCTURE"
        assert "测试用例数量必须在 3-5 条之间" in result.message

    def test_register_skill_end_to_end(self, tmp_path: Path):
        # 测试注册落盘与 repair_set.json manifest 更新
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        eval_dir = tmp_path / "evaluation_sets"
        eval_dir.mkdir()
        repair_set_file = eval_dir / "repair_set.json"
        repair_payload = {
            "meta": {
                "created": "2026-09-06",
                "total": 5,
                "auto_case_ids": ["wq_auto_01"],
                "auto_case_count": 1,
                "quota_reservations": {},
            },
            "cases": [
                {"id": "wq_auto_01", "skill": "weather_query", "query": "q", "reference": "r", "trace_id": "t1"},
                {"id": "base_01", "skill": "weather_query", "query": "base q1", "reference": "base r1"},
                {"id": "base_02", "skill": "weather_query", "query": "base q2", "reference": "base r2"},
                {"id": "base_03", "skill": "weather_query", "query": "base q3", "reference": "base r3"},
                {"id": "base_04", "skill": "weather_query", "query": "base q4", "reference": "base r4"},
            ]
        }
        repair_set_file.write_text(json.dumps(repair_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        router_file = eval_dir / "router_negatives.json"
        router_file.write_text(
            json.dumps({"meta": {"targets": {}}, "cases": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        generated = GeneratedSkill(
            name="test_reg_skill",
            version="1.0.0",
            description="测试注册的技能描述",
            use_when="测试注册落盘逻辑",
            not_for=["测试拒绝", "生产环境操作"],
            frontmatter_raw="name: test_reg_skill\nversion: 1.0.0",
            body_raw="## Overview\nTest\n## Instructions\nStep\n## Examples\nEx\n## Constraints\nCon",
            full_skill_md="---\nname: test_reg_skill\nversion: 1.0.0\ndescription: 测试注册的技能描述\nuse_when: 测试注册落盘逻辑\nnot_for:\n  - 测试拒绝\n  - 生产环境操作\ndependencies: []\ntrigger:\n  keywords:\n    - 注册测试\n    - 落盘测试\n    - 原子注册\nexamples:\n  - 样例1\nevaluation:\n  last_score: null\n  last_release_id: null\n---\n\n## Overview\nTest\n## Instructions\nStep\n## Examples\nEx\n## Constraints\nCon",
            test_cases=[
                {"id": "trs_auto_01", "skill": "test_reg_skill", "query": "q1", "reference": "r1", "trace_id": "t_trs_01"},
                {"id": "trs_auto_02", "skill": "test_reg_skill", "query": "q2", "reference": "r2", "trace_id": "t_trs_02"},
            ],
            meta=SkillMeta(
                name="test_reg_skill",
                version="1.0.0",
                description="测试注册的技能描述",
                use_when="测试注册落盘逻辑",
                not_for=["测试拒绝", "生产环境操作"],
                dependencies=[],
                trigger=Trigger(keywords=["注册测试", "落盘测试", "原子注册"]),
                examples=["样例1"],
                evaluation={"last_score": None, "last_release_id": None},
            )
        )

        skill_file = register_skill(
            generated,
            repo_root=tmp_path,
            repair_set_path=repair_set_file,
            router_negatives_path=router_file,
        )
        assert skill_file.exists()
        assert (skills_dir / "test_reg_skill" / "SKILL.md").exists()

        # 检查 repair_set.json 是否正确更新
        updated_repair = json.loads(repair_set_file.read_text(encoding="utf-8"))
        assert updated_repair["meta"]["total"] == 7
        assert updated_repair["meta"]["auto_case_count"] == 3
        assert updated_repair["meta"]["auto_case_ids"] == ["trs_auto_01", "trs_auto_02", "wq_auto_01"]
        assert len(updated_repair["cases"]) == 7
        updated_router = json.loads(router_file.read_text(encoding="utf-8"))
        assert {case["expected"] for case in updated_router["cases"] if case["type"] == "positive"} == {"test_reg_skill"}
