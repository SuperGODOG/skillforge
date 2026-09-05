"""
数据划分体系 (Data Partition System - P0-D)

遵循 codex2 架构决策 F-4 / F-5:
1. 彻底解决 baseline_hidden 污染问题：明确数据角色与三层可见性边界。
   - repair (回归/修复集): 元 Agent 可见，用于失败归因、反思上下文与 Prompt 修补。
   - experiment_holdout (实验留出集): 专用于多候选横向比对与排序，禁止将明细传回生成器。
   - final_audit (终审集): 全流程冻结，仅在发布前对基线与最终 winner 运行一次双盲终审。
2. baseline_hidden 降级为 baseline_seen_regression，纳入已见回归范畴。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class DatasetLayer(str, Enum):
    """数据集三层架构层级定义"""
    REPAIR = "repair"
    EXPERIMENT_HOLDOUT = "experiment_holdout"
    FINAL_AUDIT = "final_audit"


@dataclass(frozen=True)
class LayerPolicy:
    layer: DatasetLayer
    filename: str
    visibility: str
    description: str
    allow_in_reflection: bool
    allow_in_candidate_ranking: bool
    allow_in_final_gate: bool


LAYER_POLICIES: dict[DatasetLayer, LayerPolicy] = {
    DatasetLayer.REPAIR: LayerPolicy(
        layer=DatasetLayer.REPAIR,
        filename="repair_set.json",
        visibility="visible_to_meta_agent",
        description="回归与修复用例集，包含核心 P0 用例与已知失败案例，用于失败分析与 Prompt 修复",
        allow_in_reflection=True,
        allow_in_candidate_ranking=False,
        allow_in_final_gate=False,
    ),
    DatasetLayer.EXPERIMENT_HOLDOUT: LayerPolicy(
        layer=DatasetLayer.EXPERIMENT_HOLDOUT,
        filename="experiment_holdout.json",
        visibility="black_box_ranking_only",
        description="实验留出集，专用于候选补丁间的相对比选与排序，禁止向模型回传具体 query/output 明细",
        allow_in_reflection=False,
        allow_in_candidate_ranking=True,
        allow_in_final_gate=False,
    ),
    DatasetLayer.FINAL_AUDIT: LayerPolicy(
        layer=DatasetLayer.FINAL_AUDIT,
        filename="final_audit.json",
        visibility="frozen_unseen",
        description="终审集，全流程绝对冻结，仅在候选被推荐发布前对 control 与 winner 运行一次",
        allow_in_reflection=False,
        allow_in_candidate_ranking=False,
        allow_in_final_gate=True,
    ),
}


@dataclass
class PartitionResult:
    repair_cases: list[dict[str, Any]]
    holdout_cases: list[dict[str, Any]]
    audit_cases: list[dict[str, Any]]
    seen_regression_cases: list[dict[str, Any]]
    p0_cases: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)


def load_json_dataset(path: Path) -> dict[str, Any]:
    """读取评估集 JSON 文件"""
    if not path.exists():
        raise FileNotFoundError(f"评估集文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"解析评估集文件失败 {path}: {e}") from e


def partition_cases_three_tier(
    dev_cases: list[dict[str, Any]],
    hidden_cases: list[dict[str, Any]],
    p0_ids: set[str],
) -> PartitionResult:
    """
    确定性分层划分核心逻辑：
    - 输入：原 baseline_dev (32 条), 原 baseline_hidden (8 条), p0_ids (10 条).
    - 规则：
      1. baseline_hidden 全部 8 条降级为 seen_regression_cases，并全部归入 repair 层。
      2. p0_ids 对应的 10 条 dev cases，全部归入 repair 层（保证核心链路在回归中必测）。
      3. 剩余非 P0 的 dev cases（22 条），按 skill 分组进行均衡分流：
         - 每个 skill 分配 3 条到 experiment_holdout
         - 每个 skill 分配 3 条到 final_audit
         - 剩余用例（write_weekly_report 2 条, explain_regex 2 条）留在 repair 层作为边界/补充样本。
    """
    dev_by_id = {c["id"]: c for c in dev_cases}
    hidden_by_id = {c["id"]: c for c in hidden_cases}

    # 1. 验证 P0 用例完整存在
    p0_cases_list: list[dict[str, Any]] = []
    for pid in sorted(p0_ids):
        if pid not in dev_by_id:
            raise ValueError(f"P0 case ID {pid} 在 baseline_dev 中不存在")
        p0_cases_list.append(dev_by_id[pid])

    # 2. baseline_hidden 降级
    seen_regression_cases = list(hidden_cases)

    # 3. 按 skill 聚类非 P0 dev cases
    non_p0_by_skill: dict[str, list[dict[str, Any]]] = {}
    for c in dev_cases:
        if c["id"] not in p0_ids:
            non_p0_by_skill.setdefault(c["skill"], []).append(c)

    # 4. 分流到 holdout / audit / repair 补充
    holdout_cases: list[dict[str, Any]] = []
    audit_cases: list[dict[str, Any]] = []
    repair_boundary_cases: list[dict[str, Any]] = []

    for skill, cases in sorted(non_p0_by_skill.items()):
        # 稳定排序保证完全确定性
        sorted_cases = sorted(cases, key=lambda x: x["id"])
        if len(sorted_cases) < 6:
            raise ValueError(f"Skill {skill} 非 P0 dev 用例不足 6 条 (当前 {len(sorted_cases)} 条)，无法分配 holdout/audit")

        # 前 3 条进入 experiment_holdout
        holdout_cases.extend(sorted_cases[0:3])
        # 中间 3 条进入 final_audit
        audit_cases.extend(sorted_cases[3:6])
        # 剩余进入 repair boundary
        repair_boundary_cases.extend(sorted_cases[6:])

    # 5. 合并 repair 层
    repair_cases = p0_cases_list + seen_regression_cases + repair_boundary_cases
    # 按照 id 排序去重确认
    repair_cases = sorted(repair_cases, key=lambda x: x["id"])
    holdout_cases = sorted(holdout_cases, key=lambda x: x["id"])
    audit_cases = sorted(audit_cases, key=lambda x: x["id"])

    stats = {
        "total_source_cases": len(dev_cases) + len(hidden_cases),
        "repair_count": len(repair_cases),
        "holdout_count": len(holdout_cases),
        "audit_count": len(audit_cases),
        "seen_regression_count": len(seen_regression_cases),
        "p0_count": len(p0_cases_list),
        "skills": sorted(list({c["skill"] for c in dev_cases})),
    }

    return PartitionResult(
        repair_cases=repair_cases,
        holdout_cases=holdout_cases,
        audit_cases=audit_cases,
        seen_regression_cases=seen_regression_cases,
        p0_cases=p0_cases_list,
        stats=stats,
    )


def validate_partition_invariants(partition: PartitionResult, p0_ids: set[str]) -> list[str]:
    """验证三层划分的不变量，返回违规错误列表（为空即全部合规）"""
    errors: list[str] = []

    repair_ids = {c["id"] for c in partition.repair_cases}
    holdout_ids = {c["id"] for c in partition.holdout_cases}
    audit_ids = {c["id"] for c in partition.audit_cases}

    # 1. 互斥性检验 (Disjointness)
    rh_overlap = repair_ids & holdout_ids
    if rh_overlap:
        errors.append(f"Repair 集与 Holdout 集存在泄露交集: {sorted(rh_overlap)}")

    ra_overlap = repair_ids & audit_ids
    if ra_overlap:
        errors.append(f"Repair 集与 Final Audit 集存在泄露交集: {sorted(ra_overlap)}")

    ha_overlap = holdout_ids & audit_ids
    if ha_overlap:
        errors.append(f"Holdout 集与 Final Audit 集存在重叠交集: {sorted(ha_overlap)}")

    # 2. P0 完整性检验 (All P0 in repair)
    missing_p0 = p0_ids - repair_ids
    if missing_p0:
        errors.append(f"P0 用例未完全包含在 Repair 集内: {sorted(missing_p0)}")

    # 3. P0 隔离检验 (P0 不得污染 holdout 和 audit)
    if p0_ids & holdout_ids:
        errors.append(f"P0 用例不应存在于 Holdout 集: {sorted(p0_ids & holdout_ids)}")
    if p0_ids & audit_ids:
        errors.append(f"P0 用例不应存在于 Audit 集: {sorted(p0_ids & audit_ids)}")

    # 4. 分层平衡性检验
    for name, s_cases in [("holdout", partition.holdout_cases), ("audit", partition.audit_cases)]:
        by_skill: dict[str, int] = {}
        for c in s_cases:
            by_skill[c["skill"]] = by_skill.get(c["skill"], 0) + 1
        for skill in partition.stats.get("skills", []):
            if by_skill.get(skill, 0) == 0:
                errors.append(f"{name} 集中缺失技能 {skill} 的测试用例")

    return errors
