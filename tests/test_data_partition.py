"""
测试数据三层划分体系 (P0-D Data Partition Tests)
"""

import json
import subprocess
import sys
from pathlib import Path
import pytest

from skillforge.data_partition import (
    DatasetLayer,
    LAYER_POLICIES,
    load_json_dataset,
    partition_cases_three_tier,
    validate_partition_invariants,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "evaluation_sets"


def test_migrated_evaluation_sets_exist_and_valid():
    """验证所有生成的数据集文件存在且结构完备"""
    required_files = [
        "baseline_dev.json",
        "baseline_hidden.json",
        "baseline_seen_regression.json",
        "repair_set.json",
        "experiment_holdout.json",
        "final_audit.json",
        "p0_cases.json",
    ]
    for filename in required_files:
        filepath = EVAL_DIR / filename
        assert filepath.exists(), f"缺失数据集文件: {filename}"
        data = load_json_dataset(filepath)
        assert "meta" in data, f"{filename} 缺失 meta 字段"
        if filename != "p0_cases.json":
            assert "cases" in data, f"{filename} 缺失 cases 列表"
            assert isinstance(data["cases"], list)


def test_baseline_hidden_downgraded_status():
    """验证 baseline_hidden 已标记为降级状态，且 baseline_seen_regression 完整保留原用例"""
    hidden_data = load_json_dataset(EVAL_DIR / "baseline_hidden.json")
    assert hidden_data["meta"].get("status") == "downgraded_to_seen_regression"

    seen_reg_data = load_json_dataset(EVAL_DIR / "baseline_seen_regression.json")
    assert seen_reg_data["meta"].get("status") == "downgraded_to_seen_regression"
    assert len(seen_reg_data["cases"]) == 8

    # 验证原 hidden 的 8 个 case ID 完整保留在 seen_regression
    seen_ids = {c["id"] for c in seen_reg_data["cases"]}
    assert seen_ids == {"wq_h01", "wq_h02", "wq_h03", "wr_h01", "wr_h02", "er_h01", "er_h02", "er_h03"}


def test_three_tier_partition_invariants():
    """验证三层划分的不变量：隔离性、P0覆盖、分层平衡与全量覆盖"""
    repair = load_json_dataset(EVAL_DIR / "repair_set.json")
    holdout = load_json_dataset(EVAL_DIR / "experiment_holdout.json")
    audit = load_json_dataset(EVAL_DIR / "final_audit.json")
    p0 = load_json_dataset(EVAL_DIR / "p0_cases.json")

    repair_cases = repair["cases"]
    holdout_cases = holdout["cases"]
    audit_cases = audit["cases"]
    p0_ids = set(p0["p0_ids"])

    repair_ids = {c["id"] for c in repair_cases}
    holdout_ids = {c["id"] for c in holdout_cases}
    audit_ids = {c["id"] for c in audit_cases}

    # 1. 严格互斥与零泄露 (Disjointness)
    assert len(repair_ids & holdout_ids) == 0, "Repair 与 Holdout 存在交集泄露！"
    assert len(repair_ids & audit_ids) == 0, "Repair 与 Audit 存在交集泄露！"
    assert len(holdout_ids & audit_ids) == 0, "Holdout 与 Audit 存在交集泄露！"

    # 2. 全量覆盖：总共 40 条用例（32 dev + 8 原 hidden 降级）无遗漏
    all_partitioned_ids = repair_ids | holdout_ids | audit_ids
    assert len(all_partitioned_ids) == 40
    assert len(repair_cases) == 22
    assert len(holdout_cases) == 9
    assert len(audit_cases) == 9

    # 3. P0 完整性：所有 10 条 P0 用例必须完全包含在 repair 集中
    assert p0_ids.issubset(repair_ids), f"P0 用例未完全包含在 Repair 集: {p0_ids - repair_ids}"
    assert len(p0_ids & holdout_ids) == 0, "P0 用例被错误分配到 Holdout 集！"
    assert len(p0_ids & audit_ids) == 0, "P0 用例被错误分配到 Audit 集！"

    # 4. 技能分层平衡性：Holdout 与 Audit 中每个 Skill 均有恰好 3 条用例
    for name, c_set in [("holdout", holdout_cases), ("audit", audit_cases)]:
        by_skill = {}
        for c in c_set:
            by_skill[c["skill"]] = by_skill.get(c["skill"], 0) + 1
        assert by_skill == {
            "weather_query": 3,
            "write_weekly_report": 3,
            "explain_regex": 3,
        }, f"{name} 集未能达到 3-3-3 均衡分布: {by_skill}"


def test_layer_policies_and_visibility():
    """验证数据分层访问控制策略与元数据规范"""
    assert LAYER_POLICIES[DatasetLayer.REPAIR].allow_in_reflection is True
    assert LAYER_POLICIES[DatasetLayer.REPAIR].allow_in_candidate_ranking is False
    assert LAYER_POLICIES[DatasetLayer.REPAIR].allow_in_final_gate is False

    assert LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].allow_in_reflection is False
    assert LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].allow_in_candidate_ranking is True
    assert LAYER_POLICIES[DatasetLayer.EXPERIMENT_HOLDOUT].allow_in_final_gate is False

    assert LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].allow_in_reflection is False
    assert LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].allow_in_candidate_ranking is False
    assert LAYER_POLICIES[DatasetLayer.FINAL_AUDIT].allow_in_final_gate is True


def test_partition_dataset_cli_verify():
    """验证 CLI 脚本 scripts/partition_dataset.py --verify 可直接执行并返回 0"""
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "partition_dataset.py"), "--verify"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"CLI verify 失败: stdout={res.stdout}, stderr={res.stderr}"
    assert "校验全部通过" in res.stdout


def test_partition_verify_detects_disk_tampering(tmp_path: Path):
    """验证 scripts/partition_dataset.py --verify 能真正检出磁盘文件被篡改"""
    import shutil
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import partition_dataset

    # 复制真实 evaluation_sets 到临时目录
    temp_eval = tmp_path / "evaluation_sets"
    shutil.copytree(EVAL_DIR, temp_eval)

    # 1. 干净磁盘文件校验必须为 0 (通过)
    assert partition_dataset.run_verification(temp_eval) == 0

    # 2. 篡改场景 A：删除 repair_set.json 中的一条用例（破坏全量覆盖断言）
    repair_file = temp_eval / "repair_set.json"
    data = json.loads(repair_file.read_text(encoding="utf-8"))
    removed_case = data["cases"].pop(0)
    repair_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

    # 恢复该用例
    data["cases"].insert(0, removed_case)
    repair_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) == 0

    # 3. 篡改场景 B：造假泄漏，将 holdout 用例 ID 注入 final_audit.json（破坏交集为 0 断言）
    audit_file = temp_eval / "final_audit.json"
    audit_data = json.loads(audit_file.read_text(encoding="utf-8"))
    holdout_data = json.loads((temp_eval / "experiment_holdout.json").read_text(encoding="utf-8"))
    leaked_case = holdout_data["cases"][0]
    audit_data["cases"].append(leaked_case)
    audit_file.write_text(json.dumps(audit_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

    # 恢复 audit 文件
    audit_data["cases"].pop()
    audit_file.write_text(json.dumps(audit_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) == 0

    # 4. 篡改场景 C：在 repair_set.json 内部引入重复用例 ID
    dup_data = json.loads(repair_file.read_text(encoding="utf-8"))
    dup_data["cases"].append(dup_data["cases"][0])
    repair_file.write_text(json.dumps(dup_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0


def test_partition_verify_detects_content_tampering_and_invariants(tmp_path: Path):
    """验证 scripts/partition_dataset.py --verify 能检出同 ID 内容篡改、P0 子集缩减与 hidden/seen 不一致"""
    import shutil
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import partition_dataset

    temp_eval = tmp_path / "evaluation_sets"
    shutil.copytree(EVAL_DIR, temp_eval)

    # 1. 场景 D：同 ID 内容篡改（仅修改 repair_set.json 某 case 的 query，ID 不变）
    repair_file = temp_eval / "repair_set.json"
    repair_data = json.loads(repair_file.read_text(encoding="utf-8"))
    orig_query = repair_data["cases"][0]["query"]
    repair_data["cases"][0]["query"] = "被恶意篡改的查询文本，ID未变"
    repair_file.write_text(json.dumps(repair_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

    # 恢复
    repair_data["cases"][0]["query"] = orig_query
    repair_file.write_text(json.dumps(repair_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) == 0

    # 2. 场景 E：P0 内嵌 cases 缩为合法子集（p0_ids 为 10 条，但 cases 只留 1 条）
    p0_file = temp_eval / "p0_cases.json"
    p0_data = json.loads(p0_file.read_text(encoding="utf-8"))
    orig_p0_cases = list(p0_data["cases"])
    p0_data["cases"] = [p0_data["cases"][0]]
    p0_file.write_text(json.dumps(p0_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

    # 恢复
    p0_data["cases"] = orig_p0_cases
    p0_file.write_text(json.dumps(p0_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) == 0

    # 3. 场景 F：hidden/seen 内容不一致（篡改 baseline_hidden.json 某 case 的 reference）
    hidden_file = temp_eval / "baseline_hidden.json"
    hidden_data = json.loads(hidden_file.read_text(encoding="utf-8"))
    orig_hidden_ref = hidden_data["cases"][0]["reference"]
    hidden_data["cases"][0]["reference"] = "篡改后的 reference 内容"
    hidden_file.write_text(json.dumps(hidden_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

    # 恢复
    hidden_data["cases"][0]["reference"] = orig_hidden_ref
    hidden_file.write_text(json.dumps(hidden_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) == 0

    # 4. 场景 G：降级标记不一致（将 baseline_hidden.json 的 status 篡改为 active）
    hidden_data["meta"]["status"] = "active"
    hidden_file.write_text(json.dumps(hidden_data, ensure_ascii=False), encoding="utf-8")
    assert partition_dataset.run_verification(temp_eval) != 0

