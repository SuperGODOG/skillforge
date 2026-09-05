#!/usr/bin/env python3
"""路由评测：读 evaluation_sets/router_negatives.json → Recall@1/@3 报告

Recall 定义：
    正例：expected 是 skill_name
        R@1: chosen == expected
        R@3: expected 出现在 top-3 候选里
    负例：expected 是 null（应拒绝路由）
        R@1: chosen is None
        R@3: chosen is None（拒绝的语义 R@1 = R@3）
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def route_with_topk(router, query: str, top_k: int = 3):
    """返回 (chosen, top_k_names)——top-K 从 embed 分数取，chosen 强插到最前"""
    result = router.route(query)
    embed_scores = result.scores.get("embed", {}) if isinstance(result.scores, dict) else {}
    ranked = sorted(embed_scores.items(), key=lambda x: -x[1])
    top_k_names = [n for n, _ in ranked[:top_k]]
    if result.chosen and result.chosen not in top_k_names:
        top_k_names = [result.chosen] + top_k_names[: top_k - 1]
    return result.chosen, top_k_names, result.hit_layer


def evaluate(cases, router, top_k: int = 3) -> dict:
    ok_at_1 = 0
    ok_at_3 = 0
    fails = []
    per_type = {}

    for c in cases:
        expected = c["expected"]
        chosen, top3, layer = route_with_topk(router, c["query"], top_k=top_k)

        if expected is None:
            r1 = chosen is None
            r3 = chosen is None
        else:
            r1 = chosen == expected
            r3 = expected in top3

        if r1:
            ok_at_1 += 1
        if r3:
            ok_at_3 += 1

        typ = c["type"]
        stats = per_type.setdefault(typ, {"total": 0, "r1": 0, "r3": 0})
        stats["total"] += 1
        stats["r1"] += int(r1)
        stats["r3"] += int(r3)

        if not r1:
            fails.append({
                "id": c["id"], "type": typ,
                "query": c["query"], "expected": expected,
                "chosen": chosen, "hit_layer": layer, "top3": top3,
                "why": c.get("why", ""),
            })

    return {
        "total": len(cases),
        "recall_at_1": ok_at_1 / len(cases),
        "recall_at_3": ok_at_3 / len(cases),
        "per_type": per_type,
        "fails": fails,
    }


def main():
    from skillforge import SkillRegistry, IntentRouter

    parser = argparse.ArgumentParser(description="路由 Recall 评测")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--use-llm", action="store_true", help="启用 LLM 兜底")
    parser.add_argument(
        "--eval-set", type=Path,
        default=Path("evaluation_sets/router_negatives.json"),
    )
    parser.add_argument("--verbose", action="store_true", help="显示所有 case 的详细结果")
    args = parser.parse_args()

    repo_root: Path = args.root
    eval_path = repo_root / args.eval_set

    reg = SkillRegistry(
        db_path=repo_root / "runs" / "skillforge.db",
        skills_dir=repo_root / "skills",
        repo_root=repo_root,
    )
    reg.load_skills_from_dir()

    llm = None
    if args.use_llm:
        import os
        from dotenv import load_dotenv
        from hello_agents import HelloAgentsLLM
        load_dotenv(repo_root / ".env")
        llm = HelloAgentsLLM(
            api_key=os.environ["LLM_API_KEY"],
            model=os.environ["LLM_MODEL_ID"],
            base_url=os.environ["LLM_BASE_URL"],
        )

    router = IntentRouter(registry=reg, llm=llm)

    eval_set = json.loads(eval_path.read_text(encoding="utf-8"))
    cases = eval_set["cases"]
    targets = eval_set["meta"]["targets"]

    print(f"评测集：{eval_path.relative_to(repo_root)} · 共 {len(cases)} 条")
    print(f"目标：Recall@1 ≥ {targets['recall_at_1']:.0%}, Recall@3 ≥ {targets['recall_at_3']:.0%}")
    print(f"LLM 兜底：{'启用' if llm else '关闭（规则+embed 两层）'}")
    print()

    report = evaluate(cases, router, top_k=3)

    total = report["total"]
    r1_cnt = int(round(report["recall_at_1"] * total))
    r3_cnt = int(round(report["recall_at_3"] * total))
    print(f"Recall@1: {report['recall_at_1']:.2%}  ({r1_cnt}/{total})")
    print(f"Recall@3: {report['recall_at_3']:.2%}  ({r3_cnt}/{total})")
    print()

    print("分类型：")
    for typ, s in report["per_type"].items():
        print(f"  {typ:20s}  R@1={s['r1']/s['total']:.2%}  R@3={s['r3']/s['total']:.2%}  ({s['total']} 条)")
    print()

    r1_pass = report["recall_at_1"] >= targets["recall_at_1"]
    r3_pass = report["recall_at_3"] >= targets["recall_at_3"]
    print(f"Recall@1 门槛：{'✅ 通过' if r1_pass else '❌ 未达'}")
    print(f"Recall@3 门槛：{'✅ 通过' if r3_pass else '❌ 未达'}")

    if report["fails"]:
        print()
        print(f"=== R@1 失败 case（{len(report['fails'])} 条）===")
        for f in report["fails"]:
            print(f"  [{f['id']}][{f['type']}] hit={f['hit_layer']}")
            print(f"    query    : {f['query']}")
            print(f"    expected : {f['expected']}")
            print(f"    chosen   : {f['chosen']}   top3: {f['top3']}")
            if f["why"]:
                print(f"    why      : {f['why']}")

    reg.close()
    return 0 if (r1_pass and r3_pass) else 1


if __name__ == "__main__":
    raise SystemExit(main())
