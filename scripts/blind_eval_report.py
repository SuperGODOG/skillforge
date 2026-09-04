"""Phase 3 保底盲评·偏差报告

读 runs/blind_eval_samples.json，对比 judge_verdicts vs golden_verdicts：
  - 每维一致率
  - 分歧样本明细
  - 交付判定：分歧 < 30% ✓ / ≥ 30% 需调 Judge prompt

Phase 3 保底：跑一次 ≥ 20 条人工校准，出偏差报告
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


MAX_DISAGREEMENTS = {
    "task_completion": 6,
    "robustness": 4,
    "readability": 5,
}
JUDGE_VERDICTS = {"A_better", "tied", "B_better", "INVALID"}
GOLDEN_VERDICTS = {"A_better", "tied", "B_better"}
REJUDGE_SCHEMA = "skillforge.rejudge_frozen.v1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path,
                        default=Path("runs/blind_eval_samples.json"))
    args = parser.parse_args()

    data = json.loads(args.samples.read_text(encoding="utf-8"))
    samples = data["samples"]
    dims = data["meta"]["dims"]
    total = len(samples)
    artifact_errors = []
    if data.get("meta", {}).get("total") != total:
        artifact_errors.append("meta.total 与 samples 数量不一致")
    if set(dims) != set(MAX_DISAGREEMENTS):
        artifact_errors.append("meta.dims 必须完整包含 task_completion/robustness/readability")

    print("=" * 72)
    print(f"Phase 3 保底盲评偏差报告 · {total} 条 samples · {len(dims)} 维")
    print("=" * 72)

    per_dim_pass = {}
    invalid_total = 0

    for dim in dims:
        pairs = [
            (
                s.get("judge_verdicts", {}).get(dim),
                s.get("golden_verdicts", {}).get(dim),
            )
            for s in samples
        ]
        labeled = [(j, g) for j, g in pairs if g is not None]
        n = len(labeled)
        labels_valid = all(j in JUDGE_VERDICTS and g in GOLDEN_VERDICTS for j, g in pairs)
        complete = n == total and labels_valid

        invalid = sum(1 for j, _ in labeled if j == "INVALID")
        invalid_total += invalid
        agree = sum(1 for j, g in labeled if j == g and j != "INVALID")
        disagree_rate = (n - agree) / n if n else 1.0

        j_dist = dict(Counter(j for j, _ in labeled))
        g_dist = dict(Counter(g for _, g in labeled))

        print(f"\n[{dim}]  {n} 条已填 / {total} 总")
        print(f"  一致率：{agree}/{n} ({agree / n:.1%})" if n else "  一致率：不可计算")
        print(f"  分歧率：{n - agree}/{n} ({disagree_rate:.1%})")
        print(f"  INVALID：{invalid}/{n}")
        print(f"  Judge 分布：{j_dist}")
        print(f"  Golden 分布：{g_dist}")

        max_disagree = MAX_DISAGREEMENTS.get(dim, int(n * 0.299999))
        passed = complete and invalid == 0 and (n - agree) <= max_disagree
        per_dim_pass[dim] = passed
        if not complete:
            artifact_errors.append(f"{dim} 判定不完整或包含非法标签（{n}/{total}）")
        print(
            f"  {'✅ 交付通过' if passed else '❌ 未通过'}"
            f"（要求分歧 ≤ {max_disagree} 且 INVALID=0）"
        )

    print("\n" + "=" * 72)
    print("分歧样本明细")
    print("=" * 72)
    any_disagree = False
    for s in samples:
        for dim in dims:
            j = s.get("judge_verdicts", {}).get(dim)
            g = s.get("golden_verdicts", {}).get(dim)
            if g is not None and j != g:
                any_disagree = True
                print(f"[{s['case_id']}]  {dim}  judge={j}  golden={g}")
                print(f"  query: {s['query'][:70]}")
    if not any_disagree:
        print("（无分歧）")

    # 顶层交付判定
    print("\n" + "=" * 72)
    meta = data.get("meta", {})
    sentinel_failures = meta.get("truth_sentinel_candidate_A_better")
    schema_valid = meta.get("schema_version") == REJUDGE_SCHEMA
    sentinel_valid = (
        isinstance(sentinel_failures, int)
        and not isinstance(sentinel_failures, bool)
        and sentinel_failures >= 0
    )
    if not schema_valid:
        artifact_errors.append(f"缺少 {REJUDGE_SCHEMA} schema 标记；不能作为新 Judge 验收产物")
    if not sentinel_valid:
        artifact_errors.append("truth sentinel 指标缺失或非法；不能按 0 次处理")
    displayed_sentinel = sentinel_failures if sentinel_valid else "不可用"
    print(f"truth sentinel（无证据实时数值却判 candidate A_better）：{displayed_sentinel}")
    if artifact_errors:
        print("产物完整性错误：")
        for error in artifact_errors:
            print(f"  - {error}")
    all_pass = (
        all(per_dim_pass.values())
        and invalid_total == 0
        and sentinel_valid
        and sentinel_failures == 0
        and not artifact_errors
    )
    print(f"顶层交付判定：{'✅ 全维度通过' if all_pass else '❌ 至少一维不通过'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
