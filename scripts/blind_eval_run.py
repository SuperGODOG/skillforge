"""Phase 3 保底盲评·采样脚本

流程：
  1. 从 baseline_dev.json 每 skill 挑前 N 条（默认 7 → 3 skills × 7 ≈ 21 条 >20 条门槛）
  2. 对每 case 跑 Agent 双版本（bare + skill body）
  3. 对每 case × 每维（默认 task_completion + robustness + readability）跑 Judge
  4. 保存到 runs/blind_eval_samples.json，golden_verdicts 字段留空供人工填

输出结构：
  {
    "meta": {...},
    "samples": [
      {
        "case_id": "...",
        "skill": "...",
        "query": "...",
        "reference": "...",
        "output_A_skill": "...",
        "output_B_baseline": "...",
        "judge_verdicts": {"task_completion": "A_better", ...},
        "golden_verdicts": {"task_completion": null, ...}  # 待人工填
      }
    ]
  }
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

from skillforge import SkillRegistry, SkillEvaluator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--limit-per-skill", type=int, default=7,
                        help="每 skill 取前 N 条（默认 7 → 总 21 条 > 20 门槛）")
    parser.add_argument("--dims", nargs="+",
                        default=["task_completion", "robustness", "readability"])
    parser.add_argument("--eval-set", default="baseline_dev")
    parser.add_argument("--out", type=Path, default=Path("runs/blind_eval_samples.json"))
    args = parser.parse_args()

    repo_root = args.root
    reg = SkillRegistry(
        db_path=repo_root / "runs" / "skillforge.db",
        skills_dir=repo_root / "skills",
        repo_root=repo_root,
    )
    reg.load_skills_from_dir()

    from dotenv import load_dotenv
    from hello_agents import HelloAgentsLLM
    load_dotenv(repo_root / ".env")
    llm = HelloAgentsLLM(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ["LLM_MODEL_ID"],
        base_url=os.environ["LLM_BASE_URL"],
    )
    evaluator = SkillEvaluator(registry=reg, llm=llm)

    eval_file = repo_root / "evaluation_sets" / f"{args.eval_set}.json"
    all_cases = json.loads(eval_file.read_text(encoding="utf-8"))["cases"]

    by_skill: dict[str, list] = {}
    for c in all_cases:
        by_skill.setdefault(c["skill"], []).append(c)

    selected = []
    for skill_name, cases in by_skill.items():
        selected.extend(cases[: args.limit_per_skill])

    print(f"采样 {len(selected)} 条（{args.limit_per_skill}/skill × {len(by_skill)} skills）")
    print(f"每 case = 2 次 Agent + {len(args.dims)} 次 Judge = {2 + len(args.dims)} 次 LLM 调用")
    print(f"共 {len(selected) * (2 + len(args.dims))} 次 LLM 调用（预计 3-5 分钟）")
    print()

    samples = []
    for i, case in enumerate(selected):
        print(f"[{i + 1}/{len(selected)}] {case['id']}: {case['query'][:50]}...", flush=True)
        body = reg._bodies[case["skill"]]

        skill_out, _ = evaluator._run_with_skill(case["query"], body)
        base_out, _ = evaluator._run_bare(case["query"])

        judges = {}
        for dim in args.dims:
            judges[dim] = evaluator.judge.compare(
                case["query"], skill_out, base_out, dim,
                reference=case.get("reference"),
            )

        samples.append({
            "case_id": case["id"],
            "skill": case["skill"],
            "query": case["query"],
            "reference": case.get("reference", ""),
            "output_A_skill": skill_out,
            "output_B_baseline": base_out,
            "judge_verdicts": judges,
            "golden_verdicts": {dim: None for dim in args.dims},
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({
            "meta": {
                "total": len(samples),
                "dims": args.dims,
                "purpose": "Phase 3 保底盲评校准 · 待人工填 golden_verdicts",
                "how_to_fill": "对每 sample，看 output_A_skill vs output_B_baseline，对每维填 A_better/tied/B_better",
                "note": "A=有 Skill 版本，B=无 Skill 版本（baseline）",
            },
            "samples": samples,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✓ 保存 {len(samples)} 条到 {args.out}")


if __name__ == "__main__":
    sys.exit(main() or 0)
