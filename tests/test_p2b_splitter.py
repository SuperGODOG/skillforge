"""单元测试：P2-B Skill 拆分器 (耦合分析裁决 / 数据解耦 / 流程解耦 / 评测集分配 / 混淆用例拒绝 / 原子注册)"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from skillforge import (
    SkillMeta,
    SkillRegistry,
    IntentRouter,
    analyze_split,
    split_skill,
    deprecate_original_skill,
    SplitAnalysis,
    SplitResult,
    DomainSpec,
)
from skillforge.evaluator.llm_factory import LLMLedger
from skillforge.models import EvolveBudget
from skillforge.skill_splitter import (
    evaluate_data_coupling,
    evaluate_eval_coupling,
    evaluate_process_coupling,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def synthetic_composite_skill_md() -> str:
    """合成多域 Skill 范本：集成正则表达式与 HTTP 状态码两大正交子域"""
    return """---
name: coding_helper_hub
version: 1.0.0
description: 综合编程开发辅助助手，提供正则表达式原理讲解与常见 HTTP 状态码排查
use_when: 用户需要理解正则表达式的语法机制与回溯原理，或者排查 HTTP 4xx/5xx 报错状态码
not_for:
  - 编写业务生产代码、爬虫脚本或后端服务开发
  - 服务器终端远程运维与 Linux 系统调优
dependencies: []
trigger:
  keywords:
    - 正则
    - regex
    - 状态码
    - HTTP状态码
    - 回溯
    - 报错排查
examples:
  - 讲一下 (a|b)*c 是怎么匹配的
  - 为什么 .* 会回溯这么慢
  - 502 Bad Gateway 报错原因与网关排查
  - 429 Too Many Requests 限流排查
evaluation:
  last_score: null
  last_release_id: null
---

## Overview

本技能是面向开发者的综合编程辅助说明书，涵盖两大独立能力：
1. 正则表达式原理解析：字符类、量词、分组、锚点与回溯机制。
2. HTTP 状态码排查：4xx 客户端错误与 5xx 服务端错误的原因与排查思路。

## Instructions

### 正则表达式原理解析
1. 识别用户问的是具体正则含义、概念原理还是语法区别。
2. 按识别到的类型给出分步拆解、小例子演示与陷阱提示。
3. 纯原理解析，不生成业务生产代码。

### HTTP 状态码排查
1. 从用户问题中提取状态码或错误范围（4xx/5xx）。
2. 按状态码含义、错误类别、常见原因、排查方向分步说明。
3. 遇到非标准状态码明确标注来源。

## Examples

**Q**：讲一下 `.*?` 是什么意思？
**A**：`.*?` 是非贪婪匹配：尽量少匹配字符。

**Q**：网站出现 `502 Bad Gateway` 怎么排查？
**A**：502 是网关从上游收到无效响应，排查上游服务存活与反向代理配置。

## Constraints

- 纯说明型技能，不为用户生成业务可执行代码。
- 遇服务器运维与终端部署需求，明确告知边界并引导至专业运维流程。
- 不虚构非标准状态码与内部正则引擎实现。
"""


def test_weather_query_cannot_split_counterexample(repo_root: Path):
    """硬验收 ③：weather_query 共享 amap fixture 且生活建议依赖天气事实，必须裁决不可拆"""
    analysis = analyze_split("weather_query", repo_root=repo_root)

    assert isinstance(analysis, SplitAnalysis)
    assert analysis.can_split is False
    assert analysis.verdict == "CANNOT_SPLIT"
    # 数据耦合必须被捕获（共享 amap_weather_api）
    assert analysis.data_coupling.coupled is True
    assert analysis.data_coupling.score >= 0.25
    assert "amap_weather_api" in str(analysis.data_coupling.verdict_reason)
    # 流程耦合同样检出（穿衣/出海依赖查询事实）
    assert analysis.process_coupling.coupled is True
    assert "穿衣与出海" in analysis.process_coupling.verdict_reason or "依赖" in analysis.process_coupling.verdict_reason


def test_single_domain_skill_cannot_split(repo_root: Path):
    """单一聚焦技能无需拆分，fail-closed 默认裁决不可拆"""
    analysis = analyze_split("explain_regex", repo_root=repo_root)
    assert analysis.can_split is False
    assert analysis.verdict == "CANNOT_SPLIT"
    assert "单一意图" in analysis.primary_reason or "无需拆分" in analysis.primary_reason


def test_high_data_coupling_synthetic_rejected(tmp_path: Path):
    """高数据耦合合成反例：两意图域伪多域，但共享同一底层 mock 工具，裁决不可拆"""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "device_manager_hub"
    skill_dir.mkdir(parents=True)

    fake_skill_md = """---
name: device_manager_hub
version: 1.0.0
description: 设备综合管理助手，提供设备状态监控与固件更新指令
use_when: 用户需要查询设备运行状态或执行设备远程重启
not_for:
  - 硬件物理维修
  - 电路图纸设计
dependencies:
  - amap_weather_api
trigger:
  keywords:
    - 设备状态
    - 远程重启
    - 固件版本
examples:
  - 查询设备 A 当前电量
  - 远程重启设备 B
evaluation:
  last_score: null
  last_release_id: null
---

## Overview
管理与监控智能物联网设备。

## Instructions
### 状态监控
1. 调用 amap_weather_api 获取在线遥测数据并解析。
### 设备控制
1. 调用 amap_weather_api 发送控制重置心跳帧。

## Examples
**Q**: 重启设备？
**A**: 执行重启指令。

## Constraints
- 工具离线时如实告知。
"""
    (skill_dir / "SKILL.md").write_text(fake_skill_md, encoding="utf-8")

    domains = [
        DomainSpec(
            domain_id="monitor",
            name="device_monitor",
            description="监控设备状态",
            use_when="查询设备指标",
            dependencies=["amap_weather_api"],
        ),
        DomainSpec(
            domain_id="control",
            name="device_control",
            description="下发控制指令",
            use_when="执行控制重启",
            dependencies=["amap_weather_api"],
        ),
    ]

    analysis = analyze_split("device_manager_hub", repo_root=tmp_path, candidate_domains=domains)
    assert analysis.can_split is False
    assert analysis.verdict == "CANNOT_SPLIT"
    assert analysis.data_coupling.coupled is True
    assert "amap_weather_api" in analysis.primary_reason


def test_high_process_coupling_synthetic_rejected(tmp_path: Path):
    """高流程耦合合成反例：两子域 Instructions 存在显式前置依赖与跨步骤引用，裁决不可拆"""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "pipeline_hub"
    skill_dir.mkdir(parents=True)

    fake_skill_md = """---
name: pipeline_hub
version: 1.0.0
description: 数据清洗与图表可视化流水线
use_when: 用户需要清洗数据并生成对应的分析图表
not_for:
  - 数据库底层运维
  - 大数据集群部署
dependencies: []
trigger:
  keywords:
    - 数据清洗
    - 图表生成
    - 数据流
examples:
  - 清洗并出图
evaluation:
  last_score: null
  last_release_id: null
---

## Overview
数据处理流水线。

## Instructions
### 数据清洗
1. 解析输入原始文本并去除空值。
### 图表渲染
1. 基于前面步骤得到的数据结果进行字段聚合。
2. 依据前置步骤输出的统计指标绘制折线图。

## Examples
**Q**: 处理数据？
**A**: 步骤展示。

## Constraints
- 必须严格遵循前置数据清洗要求。
"""
    (skill_dir / "SKILL.md").write_text(fake_skill_md, encoding="utf-8")

    domains = [
        DomainSpec(domain_id="clean", name="data_clean", description="清洗数据", use_when="数据清洗"),
        DomainSpec(domain_id="chart", name="data_chart", description="生成图表", use_when="生成图表"),
    ]

    analysis = analyze_split("pipeline_hub", repo_root=tmp_path, candidate_domains=domains)
    assert analysis.can_split is False
    assert analysis.verdict == "CANNOT_SPLIT"
    assert analysis.process_coupling.coupled is True
    assert "流程耦合过高" in analysis.primary_reason


def test_synthetic_carrier_split_success_and_assignment(tmp_path: Path, synthetic_composite_skill_md: str):
    """验收 ① & ②：合成多域 skill 跑三维分析，裁决可拆，测试用例正确分类，混淆用例拒绝分配"""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "coding_helper_hub"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(synthetic_composite_skill_md, encoding="utf-8")

    eval_dir = tmp_path / "evaluation_sets"
    eval_dir.mkdir(parents=True)
    repair_file = eval_dir / "repair_set.json"
    router_file = eval_dir / "router_negatives.json"

    cases = [
        # 4 条正则用例
        {"id": "er_d01", "skill": "coding_helper_hub", "query": "讲一下 (a|b)*c 是怎么匹配的", "reference": "拆解 (a|b) 分组交替、* 零次或多次、c 字面"},
        {"id": "er_d02", "skill": "coding_helper_hub", "query": "为什么 .* 会回溯这么慢", "reference": "解释贪婪 + 回溯本质"},
        {"id": "er_d04", "skill": "coding_helper_hub", "query": "分组 (?:) 和 () 有什么区别", "reference": "命名/编号 vs 不捕获"},
        {"id": "er_h01", "skill": "coding_helper_hub", "query": "这段正则的意思是不是 ^abc.*$", "reference": "验证用户理解正确"},
        # 4 条 HTTP 状态码用例
        {"id": "ehs_a01", "skill": "coding_helper_hub", "query": "用户访问接口时收到 502 Bad Gateway，应该从哪些方向排查？", "reference": "先说明 502 属于 5xx 服务器错误"},
        {"id": "ehs_a02", "skill": "coding_helper_hub", "query": "接口返回 429 Too Many Requests，是什么意思？", "reference": "解释 429 是 4xx 客户端错误"},
        {"id": "ehs_a03", "skill": "coding_helper_hub", "query": "4xx 和 5xx 有什么区别？", "reference": "说明 4xx 客户端错误与 5xx 服务端错误区别"},
        {"id": "ehs_a04", "skill": "coding_helper_hub", "query": "Nginx 日志里有很多 499 状态码，这是 HTTP 标准错误吗？", "reference": "499 不是 IANA 注册的标准 HTTP 状态码"},
        # 1 条故意混淆跨域用例（同时提正则和状态码）
        {"id": "confused_01", "skill": "coding_helper_hub", "query": "请讲讲正则表达式和HTTP状态码这两者的区别与联系", "reference": "跨域混淆边界用例"},
    ]
    # 填充基础用例以满足 auto case 比例不超过 50%
    for i in range(15):
        cases.append({"id": f"base_{i:02d}", "skill": "other_system", "query": f"base query {i}", "reference": f"base ref {i}"})

    repair_file.write_text(json.dumps({"meta": {"total": len(cases), "auto_case_ids": [], "auto_case_count": 0, "quota_reservations": {}}, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
    router_file.write_text(json.dumps({"meta": {}, "cases": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1. 运行段 1 分析器
    analysis = analyze_split("coding_helper_hub", repo_root=tmp_path, repair_set_path=repair_file)

    assert analysis.can_split is True
    assert analysis.verdict == "SPLIT_RECOMMENDED"
    assert analysis.data_coupling.coupled is False
    assert analysis.process_coupling.coupled is False
    assert analysis.eval_coupling.coupled is False

    # 检查用例分类正确性
    regex_cases = [c["id"] for c in analysis.assigned_cases.get("regex", [])]
    http_cases = [c["id"] for c in analysis.assigned_cases.get("http", [])]

    assert set(regex_cases) == {"er_d01", "er_d02", "er_d04", "er_h01"}
    assert set(http_cases) == {"ehs_a01", "ehs_a02", "ehs_a03", "ehs_a04"}

    # 检查混淆用例是否被准确拒绝并记录
    unassigned_ids = [c["id"] for c in analysis.unassigned_cases]
    assert "confused_01" in unassigned_ids

    # 2. 运行段 2 拆分执行器并执行原子注册
    split_res = split_skill(
        analysis,
        repo_root=tmp_path,
        register=True,
        backup_original=True,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
    )

    assert split_res.success is True
    assert len(split_res.sub_skills) == 2
    sub_names = {s.name for s in split_res.sub_skills}
    assert sub_names == {"coding_helper_hub_regex", "coding_helper_hub_http"}

    # 检查原 skill 是否安全转移到备份目录且在 skills/ 中被移出
    assert (tmp_path / "skills_backup" / "coding_helper_hub").exists()
    assert not (tmp_path / "skills" / "coding_helper_hub").exists()
    assert (tmp_path / "skills_backup" / "coding_helper_hub" / "deprecated_meta.json").exists()

    # 3. 验证两个子 Skill 注册后的路由互斥性
    reg = SkillRegistry(db_path=tmp_path / "skillforge.db", skills_dir=tmp_path / "skills", repo_root=tmp_path)
    reg.load_skills_from_dir()
    assert set(reg.list_names()) == {"coding_helper_hub_regex", "coding_helper_hub_http"}

    router = IntentRouter(registry=reg)
    router._ensure_indexed()

    r_reg = router.embed.search("讲一下 (a|b)*c 是怎么匹配的")
    assert r_reg[0][0] == "coding_helper_hub_regex"
    assert r_reg[0][1] > r_reg[1][1] + 0.05

    r_http = router.embed.search("502 Bad Gateway 报错原因与网关排查")
    assert r_http[0][0] == "coding_helper_hub_http"
    assert r_http[0][1] > r_http[1][1] + 0.05

    migration = json.loads(split_res.migration_manifest_path.read_text(encoding="utf-8"))
    assert split_res.unassigned_report_path is not None
    assert split_res.unassigned_report_path.exists()
    source_ids = {"er_d01", "er_d02", "er_d04", "er_h01", "ehs_a01", "ehs_a02", "ehs_a03", "ehs_a04", "confused_01"}
    mapped_ids = {
        row["original_id"] for row in migration["mappings"]
        if row.get("partition") == "repair_set.json"
    }
    assert mapped_ids == source_ids
    assert all(
        row["new_id"] != row["original_id"]
        for row in migration["mappings"]
        if row.get("partition") == "repair_set.json"
    )
    assert migration["unassigned_count"] == 1
    assert json.loads(split_res.unassigned_report_path.read_text(encoding="utf-8"))["count"] == 1


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(0.2499, True), (0.25, True), (1.0, False)],
)
def test_data_coupling_threshold_boundary_matrix(threshold: float, expected: bool):
    meta = SkillMeta(
        name="boundary_data_skill",
        version="1.0.0",
        description="共享工具边界测试",
        use_when="用于边界测试",
        dependencies=["shared_fixture"],
    )
    domains = [
        DomainSpec("left", "boundary_data_left", "左域", "查询左域"),
        DomainSpec("right", "boundary_data_right", "右域", "查询右域"),
    ]
    result = evaluate_data_coupling(meta, "", domains, threshold=threshold)
    assert result.score == 1.0
    assert result.coupled is expected


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(0.5999, True), (0.6, False), (1.0, False)],
)
def test_process_coupling_threshold_boundary_matrix(threshold: float, expected: bool):
    meta = SkillMeta(
        name="boundary_process_skill",
        version="1.0.0",
        description="流程边界测试",
        use_when="用于边界测试",
    )
    body = """## Overview
流程测试。

## Instructions
### 左域
依据前置结果继续处理。
### 右域
输出结果。

## Examples
示例。

## Constraints
保持顺序。
"""
    domains = [
        DomainSpec("left", "boundary_process_left", "左域", "查询左域", instructions="依据前置结果继续处理"),
        DomainSpec("right", "boundary_process_right", "右域", "查询右域", instructions="输出结果"),
    ]
    result = evaluate_process_coupling(meta, body, domains, threshold=threshold)
    assert result.score == 0.6
    assert result.coupled is expected


class _BoundaryEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        rows = []
        for text in texts:
            value = str(text)
            if "domain_a" in value:
                rows.append([1.0, 0.0])
            elif "domain_b" in value:
                rows.append([0.0, 1.0])
            else:
                rows.append([1.0, 0.9])
        return rows


class _BoundaryEmbedLayer:
    def _get_model(self):
        return _BoundaryEmbeddingModel()


@pytest.mark.parametrize(
    ("margin_threshold", "expected_confused"),
    [(0.0, False), (0.05, True), (1.0, True)],
)
def test_eval_confusion_margin_boundary_matrix(margin_threshold: float, expected_confused: bool):
    domains = [
        DomainSpec("a", "boundary_a", "domain_a", "domain_a query", keywords=["a"], examples=["domain_a example"]),
        DomainSpec("b", "boundary_b", "domain_b", "domain_b query", keywords=["b"], examples=["domain_b example"]),
    ]
    coupling, assignments, _, unassigned = evaluate_eval_coupling(
        "boundary",
        domains,
        [{"id": "boundary_case", "query": "ambiguous query", "reference": "reference"}],
        _BoundaryEmbedLayer(),
        confusion_margin=margin_threshold,
        max_centroid_sim_threshold=0.75,
    )
    assert bool(unassigned) is expected_confused
    assert assignments[0].confused is expected_confused
    assert coupling.metrics["total_cases"] == 1


def test_eval_embedding_zero_norm_is_fail_closed():
    class ZeroModel:
        def encode(self, texts, normalize_embeddings=True):
            return [[0.0, 0.0] for _ in texts]

    domains = [
        DomainSpec("a", "zero_a", "A", "A query"),
        DomainSpec("b", "zero_b", "B", "B query"),
    ]
    with pytest.raises(ValueError, match="零范数"):
        evaluate_eval_coupling(
            "zero",
            domains,
            [{"id": "zero_case", "query": "query", "reference": "ref"}],
            type("Layer", (), {"_get_model": lambda self: ZeroModel()})(),
        )


def test_llm_discovery_failure_records_attempt_and_refuses(tmp_path: Path, synthetic_composite_skill_md: str):
    skill_dir = tmp_path / "skills" / "llm_failure_hub"
    skill_dir.mkdir(parents=True)
    skill_md = synthetic_composite_skill_md.replace("coding_helper_hub", "llm_failure_hub")
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(
        json.dumps({"meta": {}, "cases": [{"id": "failure_01", "skill": "llm_failure_hub", "query": "正则", "reference": "说明"}]}),
        encoding="utf-8",
    )

    class FailingLLM:
        def invoke(self, messages):
            raise TimeoutError("splitter timeout")

    ledger = LLMLedger(EvolveBudget(max_calls=4, max_tokens=1000, deadline_seconds=30))
    analysis = analyze_split(
        "llm_failure_hub",
        llm=FailingLLM(),
        repo_root=tmp_path,
        repair_set_path=repair_file,
        ledger=ledger,
    )
    assert analysis.can_split is False
    assert analysis.verdict == "CANNOT_SPLIT"
    assert "fail-closed" in analysis.primary_reason
    assert ledger.total_calls == 1
    assert ledger.failed_calls == 1
