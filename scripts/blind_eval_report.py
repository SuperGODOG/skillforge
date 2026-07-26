"""Phase 3 保底盲评·偏差报告

读 runs/blind_eval_samples.json，对比 judge_verdicts vs golden_verdicts：
  - 每维一致率
  - 分歧样本明细
  - 交付判定：分歧 < 30% ✓ / ≥ 30% 需调 Judge prompt

方案书 §4.4：Phase 3 保底跑一次 ≥ 20 条人工校准，出偏差报告
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path,
                        default=Path("runs/blind_eval_samples.json"))
    args = parser.parse_args()

    data = json.loads(args.samples.read_text(encoding="utf-8"))
    samples = data["samples"]
    dims = data["meta"]["dims"]
    total = len(samples)

    print("=" * 72)
    print(f"Phase 3 保底盲评偏差报告 · {total} 条 samples · {len(dims)} 维")
    print("=" * 72)

    per_dim_pass = {}

    for dim in dims:
        labeled = [
            (s["judge_verdicts"].get(dim), s["golden_verdicts"].get(dim))
            for s in samples
            if s["golden_verdicts"].get(dim) is not None
        ]
        n = len(labeled)
        if n == 0:
            print(f"\n[{dim}]  ⚠️  0 条已填 golden，跳过（待人工完成）")
            per_dim_pass[dim] = None
            continue

        agree = sum(1 for j, g in labeled if j == g)
        disagree_rate = (n - agree) / n

        j_dist = dict(Counter(j for j, _ in labeled))
        g_dist = dict(Counter(g for _, g in labeled))

        print(f"\n[{dim}]  {n} 条已填 / {total} 总")
        print(f"  一致率：{agree}/{n} ({agree / n:.1%})")
        print(f"  分歧率：{n - agree}/{n} ({disagree_rate:.1%})")
        print(f"  Judge 分布：{j_dist}")
        print(f"  Golden 分布：{g_dist}")

        passed = disagree_rate < 0.30
        per_dim_pass[dim] = passed
        print(f"  {'✅ 交付通过（分歧 < 30%）' if passed else '❌ 需调 Judge prompt（分歧 ≥ 30%）'}")

    print("\n" + "=" * 72)
    print("分歧样本明细")
    print("=" * 72)
    any_disagree = False
    for s in samples:
        for dim in dims:
            j = s["judge_verdicts"].get(dim)
            g = s["golden_verdicts"].get(dim)
            if g is not None and j != g:
                any_disagree = True
                print(f"[{s['case_id']}]  {dim}  judge={j}  golden={g}")
                print(f"  query: {s['query'][:70]}")
    if not any_disagree:
        print("（无分歧）")

    # 顶层交付判定
    print("\n" + "=" * 72)
    valid = [p for p in per_dim_pass.values() if p is not None]
    if not valid:
        print("⚠️  尚未填 golden，无法出交付判定")
        return 1
    all_pass = all(valid)
    print(f"顶层交付判定：{'✅ 全维度通过' if all_pass else '❌ 至少一维不通过'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
