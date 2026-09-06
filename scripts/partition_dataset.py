#!/usr/bin/env python3
"""
数据划分与迁移脚本 (Data Partition & Migration Script - P0-D)

用法:
    python scripts/partition_dataset.py --verify
    python scripts/partition_dataset.py --migrate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证可直接导入 skillforge 模块
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "src"))

from skillforge.data_partition import (
    DatasetLayer,
    LAYER_POLICIES,
    PartitionResult,
    load_json_dataset,
    partition_cases_three_tier,
    validate_partition_invariants,
)


def _is_auto_case(case: dict) -> bool:
    """Dynamic extracted cases are explicitly marked by their stable ID."""
    return "_auto_" in str(case.get("id", ""))


def _load_existing_auto_cases(repair_file: Path) -> list[dict]:
    """Load auto cases before migration so source repartition cannot drop them."""
    if not repair_file.exists():
        return []
    data = load_json_dataset(repair_file)
    cases = data.get("cases", [])
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise ValueError("repair_set cases 必须是对象数组")
    autos = [case for case in cases if isinstance(case, dict) and _is_auto_case(case)]
    seen: set[str] = set()
    for case in autos:
        case_id = str(case.get("id", ""))
        if not case_id or case_id in seen:
            raise ValueError(f"repair_set 中 auto case ID 重复或为空: {case_id!r}")
        if not str(case.get("trace_id", "")).strip():
            raise ValueError(f"auto case {case_id} 缺失 trace_id")
        for field_name in ("skill", "query", "reference"):
            if not str(case.get(field_name, "")).strip():
                raise ValueError(f"auto case {case_id} 缺失 {field_name}")
        seen.add(case_id)
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("repair_set meta 必须是对象")
    declared_raw = meta.get("auto_case_ids")
    if declared_raw is None and not autos:
        # A pristine source partition has no dynamic tail yet.
        declared_ids: set[str] = set()
    elif (
        not isinstance(declared_raw, list)
        or any(not isinstance(case_id, str) or not case_id for case_id in declared_raw)
        or any("_auto_" not in case_id for case_id in declared_raw)
    ):
        raise ValueError("repair_set meta.auto_case_ids 必须是非空 _auto_ ID 列表")
    else:
        declared_ids = set(declared_raw)
        if len(declared_ids) != len(declared_raw):
            raise ValueError("repair_set meta.auto_case_ids 存在重复 ID")
    actual_ids = {str(case.get("id", "")) for case in autos}
    if actual_ids != declared_ids:
        raise ValueError(
            "repair_set _auto_ 与 meta.auto_case_ids 不一致: "
            f"actual={sorted(actual_ids)}, declared={sorted(declared_ids)}"
        )
    if declared_raw is not None or declared_ids:
        auto_count = meta.get("auto_case_count")
        if type(auto_count) is not int or auto_count != len(declared_ids):
            raise ValueError(
                f"repair_set meta.auto_case_count ({auto_count!r}) != manifest 数量 ({len(declared_ids)})"
            )
    return sorted(autos, key=lambda case: str(case.get("id", "")))


def run_migration(eval_dir: Path) -> int:
    print(f"▶ 开始数据划分与迁移：读取目录 {eval_dir}")
    dev_path = eval_dir / "baseline_dev.json"
    hidden_path = eval_dir / "baseline_hidden.json"
    p0_path = eval_dir / "p0_cases.json"

    dev_data = load_json_dataset(dev_path)
    hidden_data = load_json_dataset(hidden_path)
    p0_data = load_json_dataset(p0_path)

    p0_ids = set(p0_data.get("p0_ids", []))
    dev_cases = dev_data.get("cases", [])
    hidden_cases = hidden_data.get("cases", [])

    print(f"  源数据：dev={len(dev_cases)}, hidden={len(hidden_cases)}, p0_ids={len(p0_ids)}")

    try:
        existing_auto_cases = _load_existing_auto_cases(eval_dir / "repair_set.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 读取现有 repair auto manifest 失败: {exc}")
        return 1

    source_ids = {str(case.get("id", "")) for case in dev_cases + hidden_cases}
    auto_ids = {str(case.get("id", "")) for case in existing_auto_cases}
    overlap = sorted(source_ids & auto_ids)
    if overlap:
        print(f"❌ auto case 与权威源 case-ID 冲突: {overlap}")
        return 1

    partition = partition_cases_three_tier(dev_cases, hidden_cases, p0_ids)
    # The deterministic source partition has no dynamic cases.  Preserve the
    # previously extracted repair tail across every migration.
    partition.repair_cases = sorted(
        partition.repair_cases + existing_auto_cases,
        key=lambda case: str(case.get("id", "")),
    )
    partition.stats["repair_count"] = len(partition.repair_cases)
    partition.stats["auto_repair_count"] = len(existing_auto_cases)
    errors = validate_partition_invariants(partition, p0_ids)
    if errors:
        print("❌ 数据划分不变量校验失败:")
        for err in errors:
            print(f"  - {err}")
        return 1

    # 1. 写入 baseline_seen_regression.json
    seen_regression_file = eval_dir / "baseline_seen_regression.json"
    seen_regression_payload = {
        "meta": {
            "created": "2026-09-04",
            "purpose": "Phase 3 八维评估器·已见回归集（由 baseline_hidden 降级）",
            "status": "downgraded_to_seen_regression",
            "note": "原 baseline_hidden 因在 evolve_full 中被作为迭代评测使用已遭污染 (codex2 F-4)；降级为回归修复验证集",
            "total": len(partition.seen_regression_cases),
            "visibility": "visible_to_meta_agent",
        },
        "cases": partition.seen_regression_cases,
    }
    seen_regression_file.write_text(json.dumps(seen_regression_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已生成: {seen_regression_file.name} ({len(partition.seen_regression_cases)} 条)")

    # 2. 写入 repair_set.json
    repair_file = eval_dir / "repair_set.json"
    repair_payload = {
        "meta": {
            "created": "2026-09-04",
            "layer": DatasetLayer.REPAIR.value,
            "purpose": LAYER_POLICIES[DatasetLayer.REPAIR].description,
            "visibility": LAYER_POLICIES[DatasetLayer.REPAIR].visibility,
            "allow_in_reflection": LAYER_POLICIES[DatasetLayer.REPAIR].allow_in_reflection,
            "total": len(partition.repair_cases),
            "p0_included_count": len(partition.p0_cases),
            "seen_regression_count": len(partition.seen_regression_cases),
            "auto_case_ids": [case["id"] for case in existing_auto_cases],
            "auto_case_count": len(existing_auto_cases),
        },
        "cases": partition.repair_cases,
    }
    repair_file.write_text(json.dumps(repair_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已生成: {repair_file.name} ({len(partition.repair_cases)} 条)")

    # 3. 写入 experiment_holdout.json
    holdout_file = eval_dir / "experiment_holdout.json"
    holdout_payload = {
        "meta": {
            "created": "2026-09-04",
            "layer": DatasetLayer.EXPERIMENT_HOLDOUT.value,
            "purpose": LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].description,
            "visibility": LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].visibility,
            "allow_in_reflection": LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].allow_in_reflection,
            "total": len(partition.holdout_cases),
            "rule": "禁止将用例详情/失败样本反馈到 Prompt 反思或生成上下文",
        },
        "cases": partition.holdout_cases,
    }
    holdout_file.write_text(json.dumps(holdout_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已生成: {holdout_file.name} ({len(partition.holdout_cases)} 条)")

    # 4. 写入 final_audit.json
    audit_file = eval_dir / "final_audit.json"
    audit_payload = {
        "meta": {
            "created": "2026-09-04",
            "layer": DatasetLayer.FINAL_AUDIT.value,
            "purpose": LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].description,
            "visibility": LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].visibility,
            "allow_in_reflection": LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].allow_in_reflection,
            "total": len(partition.audit_cases),
            "rule": "全流程冻结；仅在候选者发布前对基线与最终胜出补丁运行一次终审",
        },
        "cases": partition.audit_cases,
    }
    audit_file.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已生成: {audit_file.name} ({len(partition.audit_cases)} 条)")

    # 5. 更新 p0_cases.json（注入嵌入式的完整 cases 定义，同时保留 p0_ids 与原有 meta）
    p0_payload = dict(p0_data)
    p0_payload["meta"]["standalone_loader_ready"] = True
    p0_payload["meta"]["note"] = "P0 case ID 引用自 baseline_dev.json / repair_set.json；并内嵌完整用例定义支持独立加载与评测"
    p0_payload["cases"] = partition.p0_cases
    p0_path.write_text(json.dumps(p0_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已更新: {p0_path.name} (注入 {len(partition.p0_cases)} 条完整定义，保留兼容 p0_ids)")

    # 6. 更新 baseline_hidden.json 元数据（保留原 cases 结构以确保旧逻辑 100% 兼容，明确标记已降级）
    hidden_payload = dict(hidden_data)
    hidden_payload["meta"]["status"] = "downgraded_to_seen_regression"
    hidden_payload["meta"]["migrated_to"] = "baseline_seen_regression.json"
    hidden_payload["meta"]["note"] = "【已降级】此集在 evolve 迭代中已被多次使用，不再具有盲测效力。新规范请使用 repair_set / experiment_holdout / final_audit"
    hidden_path.write_text(json.dumps(hidden_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ 已标记降级: {hidden_path.name}")

    print("\n✅ 数据划分与迁移成功完成！")
    return 0


def run_verification(eval_dir: Path) -> int:
    print(f"▶ 校验数据划分与不变量：目录 {eval_dir}")
    required_files = [
        "baseline_dev.json",
        "baseline_hidden.json",
        "baseline_seen_regression.json",
        "repair_set.json",
        "experiment_holdout.json",
        "final_audit.json",
        "p0_cases.json",
    ]

    for rf in required_files:
        p = eval_dir / rf
        if not p.exists():
            print(f"❌ 缺失必要文件: {rf}")
            return 1

    from collections import Counter

    repair = load_json_dataset(eval_dir / "repair_set.json")
    holdout = load_json_dataset(eval_dir / "experiment_holdout.json")
    audit = load_json_dataset(eval_dir / "final_audit.json")
    p0 = load_json_dataset(eval_dir / "p0_cases.json")
    seen_reg = load_json_dataset(eval_dir / "baseline_seen_regression.json")
    dev = load_json_dataset(eval_dir / "baseline_dev.json")
    hidden = load_json_dataset(eval_dir / "baseline_hidden.json")
    p0_ids = set(p0.get("p0_ids", []))
    p0_ids_list = p0.get("p0_ids", [])

    repair_cases = repair.get("cases", [])
    holdout_cases = holdout.get("cases", [])
    audit_cases = audit.get("cases", [])
    seen_reg_cases = seen_reg.get("cases", [])
    dev_cases = dev.get("cases", [])
    hidden_cases = hidden.get("cases", [])
    p0_cases = p0.get("cases", [])

    errors: list[str] = []

    # 1. 检查各划分文件内部 ID 重复
    for name, c_list in [
        ("repair_set", repair_cases),
        ("experiment_holdout", holdout_cases),
        ("final_audit", audit_cases),
        ("baseline_seen_regression", seen_reg_cases),
        ("baseline_dev", dev_cases),
    ]:
        ids = [c["id"] for c in c_list]
        counts = Counter(ids)
        dups = [cid for cid, count in counts.items() if count > 1]
        if dups:
            errors.append(f"文件 {name}.json 内部存在重复用例 ID: {dups}")

    # 2. 检查全量覆盖与无损：按权威源对象全字段等值比较（不只 ID）
    all_source_cases = {c["id"]: c for c in dev_cases + seen_reg_cases}
    all_disk_cases = {c["id"]: c for c in repair_cases + holdout_cases + audit_cases}
    repair_ids = {c["id"] for c in repair_cases}
    holdout_ids = {c["id"] for c in holdout_cases}
    audit_ids = {c["id"] for c in audit_cases}
    all_source_ids = set(all_source_cases.keys())
    all_disk_ids = set(all_disk_cases.keys())

    # Dynamic cases are accepted only when the repair manifest declares them.
    repair_meta = repair.get("meta", {})
    if not isinstance(repair_meta, dict):
        errors.append("repair_set meta 必须是对象")
        repair_meta = {}
    declared_auto_raw = repair_meta.get("auto_case_ids")
    if (
        not isinstance(declared_auto_raw, list)
        or any(not isinstance(cid, str) or not cid or "_auto_" not in cid for cid in declared_auto_raw)
    ):
        errors.append("repair_set meta 缺失合法 _auto_ case manifest")
        declared_auto_ids: set[str] = set()
    else:
        declared_auto_ids = set(declared_auto_raw)
        if len(declared_auto_ids) != len(declared_auto_raw):
            errors.append("repair_set meta.auto_case_ids 存在重复 ID")
    auto_repair_ids = {c["id"] for c in repair_cases if _is_auto_case(c)}
    if auto_repair_ids != declared_auto_ids:
        errors.append(
            "repair_set _auto_ 与 meta.auto_case_ids 不一致: "
            f"actual={sorted(auto_repair_ids)}, declared={sorted(declared_auto_ids)}"
        )
    if type(repair_meta.get("auto_case_count")) is not int or repair_meta.get("auto_case_count") != len(declared_auto_ids):
        errors.append(
            f"repair_set meta.auto_case_count ({repair_meta.get('auto_case_count')}) != manifest 数量 ({len(declared_auto_ids)})"
        )
    if type(repair_meta.get("total")) is not int or repair_meta.get("total") != len(repair_cases):
        errors.append(
            f"repair_set meta.total ({repair_meta.get('total')}) != cases 数量 ({len(repair_cases)})"
        )
    for case in repair_cases:
        if _is_auto_case(case):
            if not str(case.get("trace_id", "")).strip():
                errors.append(f"auto case {case.get('id')} 缺失 trace_id")
            for field_name in ("skill", "query", "reference"):
                if not str(case.get(field_name, "")).strip():
                    errors.append(f"auto case {case.get('id')} 缺失 {field_name}")
    all_auto_disk_ids = {cid for cid in all_disk_ids if "_auto_" in str(cid)}
    non_repair_auto_ids = all_auto_disk_ids - repair_ids
    if non_repair_auto_ids:
        errors.append(f"auto case 不得出现在 Holdout/Audit: {sorted(non_repair_auto_ids)}")
    if declared_auto_ids & (holdout_ids | audit_ids):
        errors.append(f"manifest auto case 不得出现在 Holdout/Audit: {sorted(declared_auto_ids & (holdout_ids | audit_ids))}")
    if declared_auto_ids & all_source_ids:
        errors.append(f"auto case 不得与权威源 case-ID 冲突: {sorted(declared_auto_ids & all_source_ids)}")
    missing_from_disk = all_source_ids - all_disk_ids
    extra_on_disk = (all_disk_ids - all_source_ids) - declared_auto_ids

    if missing_from_disk:
        errors.append(f"磁盘划分用例缺失源用例: {sorted(missing_from_disk)}")
    if extra_on_disk:
        errors.append(f"磁盘划分用例包含未知额外用例: {sorted(extra_on_disk)}")

    for cid, src_c in all_source_cases.items():
        if cid in all_disk_cases:
            disk_c = all_disk_cases[cid]
            for field_name in ("id", "skill", "query", "reference"):
                if src_c.get(field_name) != disk_c.get(field_name):
                    errors.append(
                        f"用例 {cid} 在磁盘划分集中的字段 '{field_name}' 与权威源不一致: "
                        f"{disk_c.get(field_name)!r} != {src_c.get(field_name)!r}"
                    )

    # 3. 校验 baseline_hidden cases 内容 == baseline_seen_regression cases 内容 + 降级标记一致性
    hidden_meta = hidden.get("meta", {})
    seen_reg_meta = seen_reg.get("meta", {})
    if hidden_meta.get("status") != "downgraded_to_seen_regression":
        errors.append(f"baseline_hidden.json 状态标记不是 downgraded_to_seen_regression: {hidden_meta.get('status')}")
    if seen_reg_meta.get("status") != "downgraded_to_seen_regression":
        errors.append(f"baseline_seen_regression.json 状态标记不是 downgraded_to_seen_regression: {seen_reg_meta.get('status')}")
    if hidden_meta.get("migrated_to") != "baseline_seen_regression.json":
        errors.append(f"baseline_hidden.json 缺失或错误的 migrated_to 标记: {hidden_meta.get('migrated_to')}")

    if len(hidden_cases) != len(seen_reg_cases):
        errors.append(f"baseline_hidden cases 数量 ({len(hidden_cases)}) != baseline_seen_regression cases 数量 ({len(seen_reg_cases)})")
    else:
        hidden_map = {c["id"]: c for c in hidden_cases}
        seen_map = {c["id"]: c for c in seen_reg_cases}
        if set(hidden_map.keys()) != set(seen_map.keys()):
            errors.append(f"baseline_hidden 与 baseline_seen_regression ID 集不一致: {set(hidden_map.keys()) ^ set(seen_map.keys())}")
        else:
            for cid, h_c in hidden_map.items():
                s_c = seen_map[cid]
                for field_name in ("id", "skill", "query", "reference"):
                    if h_c.get(field_name) != s_c.get(field_name):
                        errors.append(
                            f"用例 {cid} 在 baseline_hidden 与 baseline_seen_regression 中字段 '{field_name}' 不一致: "
                            f"{h_c.get(field_name)!r} != {s_c.get(field_name)!r}"
                        )

    # 4. P0 cases 与 p0_ids 对应 repair/dev cases 全对象一致 + 校验 meta.total
    p0_meta = p0.get("meta", {})
    if p0_meta.get("total") is not None and p0_meta.get("total") != len(p0_ids_list):
        errors.append(f"p0_cases.json meta.total ({p0_meta.get('total')}) != p0_ids 数量 ({len(p0_ids_list)})")

    if p0_cases:
        p0_case_ids = [c.get("id") for c in p0_cases]
        if set(p0_case_ids) != set(p0_ids_list):
            errors.append(f"p0_cases.json 中 cases ID 集与 p0_ids 不一致: 差异={set(p0_case_ids) ^ set(p0_ids_list)}")
        if len(p0_case_ids) != len(p0_ids_list):
            errors.append(f"p0_cases.json 中 cases 数量 ({len(p0_case_ids)}) != p0_ids 数量 ({len(p0_ids_list)})")
        for c in p0_cases:
            cid = c.get("id")
            src_c = all_source_cases.get(cid)
            if not src_c:
                errors.append(f"P0 内嵌用例 {cid} 在权威源中不存在")
            else:
                for field_name in ("id", "skill", "query", "reference"):
                    if c.get(field_name) != src_c.get(field_name):
                        errors.append(
                            f"P0 用例 {cid} 字段 '{field_name}' 与权威源不一致: "
                            f"{c.get(field_name)!r} != {src_c.get(field_name)!r}"
                        )

    # 5. 构造磁盘划分实体并校验不变量（互斥性、P0包含与隔离、技能均衡）
    skills = sorted(list({c["skill"] for c in dev_cases}))
    stats = {
        "total_source_cases": len(all_source_ids),
        "repair_count": len(repair_cases),
        "holdout_count": len(holdout_cases),
        "audit_count": len(audit_cases),
        "seen_regression_count": len(seen_reg_cases),
        "p0_count": len(p0.get("cases", []) or p0_ids),
        "skills": skills,
    }

    disk_partition = PartitionResult(
        repair_cases=repair_cases,
        holdout_cases=holdout_cases,
        audit_cases=audit_cases,
        seen_regression_cases=seen_reg_cases,
        p0_cases=p0.get("cases", []),
        stats=stats,
    )

    errors.extend(validate_partition_invariants(disk_partition, p0_ids))
    if errors:
        print("❌ 磁盘数据集划分存在违规:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("  ✓ 磁盘文件不变量校验通过:")
    print(f"    - Repair: {len(repair_cases)} cases (包含 10 条 P0 与 8 条降级已见用例)")
    print(f"    - Holdout: {len(holdout_cases)} cases (严格隔离，黑盒比对)")
    print(f"    - Audit: {len(audit_cases)} cases (严格隔离，发布终审)")
    print(f"    - Disjointness: Holdout 与 Audit 交集为 0，与 Repair 交集为 0")
    print("✅ 校验全部通过！")

    return 0


def main():
    parser = argparse.ArgumentParser(description="SkillForge 数据集三层划分与迁移工具")
    parser.add_argument("--eval-dir", default=str(repo_root / "evaluation_sets"), help="evaluation_sets 目录路径")
    parser.add_argument("--migrate", action="store_true", help="执行数据划分与文件迁移落盘")
    parser.add_argument("--verify", action="store_true", help="校验现有数据集划分不变量")

    args = parser.parse_args()
    eval_dir = Path(args.eval_dir)

    if args.migrate:
        sys.exit(run_migration(eval_dir))
    elif args.verify:
        sys.exit(run_verification(eval_dir))
    else:
        # 默认两步都跑
        res = run_migration(eval_dir)
        if res != 0:
            sys.exit(res)
        sys.exit(run_verification(eval_dir))


if __name__ == "__main__":
    main()
