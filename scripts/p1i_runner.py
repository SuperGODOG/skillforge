#!/usr/bin/env python3
"""P1-I 四配置验收 runner —— C/R/B/RB × 3 skills × 轮次，shadow 禁发布，断点续跑。

用法:
  uv run --no-sync python scripts/p1i_runner.py --config C --runs 10     # 单配置全 skill 轮转
  uv run --no-sync python scripts/p1i_runner.py --smoke                   # C 配置 weather_query 1 次（成本实测）
  uv run --no-sync python scripts/p1i_runner.py --all                     # 四配置全量

协议（codex2 定稿 9.4/9.5）:
  - 所有 run 从同一 commit 起跑（启动时记录 HEAD，dirty 工作区拒绝）
  - shadow_mode=True, auto_publish_enabled=False（禁止自动发布）
  - 每配置 ≥10 次 top-level evolve，跨 3 skill 轮转（4/3/3）
  - 每次 evolve 用 repair_set（可见集，22 cases）
  - 记录: run-id/config/skill/commit/outcome 分类/时间
  - 断点: progress 文件记录完成项，重跑自动跳过
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ["weather_query", "explain_regex", "write_weekly_report"]  # 4/3/3 轮转
CONFIGS = {
    "C":  {"enable_reflection": False, "enable_a2": False},
    "R":  {"enable_reflection": True,  "enable_a2": False},
    "B":  {"enable_reflection": False, "enable_a2": True},
    "RB": {"enable_reflection": True,  "enable_a2": True},
}
PROGRESS = ROOT / "runs" / "p1i_progress.json"
OUT_DIR = ROOT / "runs" / "p1i"


def git_head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip()


def git_dirty() -> bool:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if not l.startswith("?? logs/")]
    return bool(lines)


def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text())
    return {"runs": [], "head": None, "started_at": None}


def save_progress(p: dict) -> None:
    PROGRESS.write_text(json.dumps(p, ensure_ascii=False, indent=1))


def run_evolve(cfg_name: str, skill: str, run_no: int, head: str, dry: bool = False, eval_set: str = "repair_set", max_candidates: int = 3) -> dict:
    """单次 top-level evolve，返回结果字典。"""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from skillforge.evaluator.llm_factory import build_llm_pair
    from skillforge.models import EvolveBudget
    from skillforge import SkillRegistry, SkillEvaluator, SkillEvolver

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # The runner supplies an explicit ID to evolve_full, so it must carry the
    # same collision resistance as the default evolution entry point.
    run_id = (
        f"p1i-{cfg_name}-{skill}-{run_no:02d}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}-"
        f"{time.time_ns()}-{uuid.uuid4().hex}"
    )

    budget = EvolveBudget(
        enable_reflection=CONFIGS[cfg_name]["enable_reflection"],
        enable_a2=CONFIGS[cfg_name]["enable_a2"],
        shadow_mode=True,
        auto_publish_enabled=False,
    )

    reg = SkillRegistry(db_path=ROOT / "runs" / "skillforge.db", skills_dir=ROOT / "skills", repo_root=ROOT)
    reg.load_skills_from_dir()
    llm, judge_llm = build_llm_pair()
    evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)
    sm = __import__("skillforge").ReleaseStateMachine(db_path=ROOT / "runs" / "skillforge.db", repo_root=ROOT)
    evolver = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm, state_machine=sm)

    t0 = time.time()
    outcome = evolver.evolve_full(
        skill,
        max_candidates=max_candidates,
        eval_set_for_iter=eval_set,
        verbose=False,
        budget=budget,
        run_id=run_id,
    )
    elapsed = time.time() - t0

    rec = {
        "run_id": run_id,
        "config": cfg_name,
        "skill": skill,
        "run_no": run_no,
        "commit": head,
        "baseline_score": round(getattr(outcome, "baseline_score", 0.0) or 0.0, 4),
        "patches_generated": getattr(outcome, "patches_generated", 0),
        "published": len(getattr(outcome, "patches_published", []) or []),
        "review": len(getattr(outcome, "patches_review", []) or []),
        "declined": len(getattr(outcome, "patches_declined", []) or []),
        "invalid": 1 if getattr(outcome, "error", None) else 0,
        "error": str(getattr(outcome, "error", "") or "")[:300],
        "elapsed_s": round(elapsed, 1),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    out_file = OUT_DIR / f"{run_id}.json"
    out_file.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS))
    ap.add_argument("--runs", type=int, default=10, help="每配置总 evolve 次数（默认 10，跨 3 skill 轮转 4/3/3）")
    ap.add_argument("--all", action="store_true", help="四配置全量")
    ap.add_argument("--smoke", action="store_true", help="C 配置 weather_query 1 次成本实测")
    ap.add_argument("--eval-set", default="repair_set", help="迭代评估集（默认 repair_set）")
    ap.add_argument("--max-candidates", type=int, default=3, help="候选生成数上限（默认 3）")
    args = ap.parse_args()

    head = git_head()
    if not args.smoke and git_dirty():
        print(f"REFUSE: 工作区 dirty（HEAD={head}），需同 commit 起跑——先 commit/stash")
        return 1

    if args.smoke:
        rec = run_evolve("C", "weather_query", 0, head, eval_set=args.eval_set, max_candidates=args.max_candidates)
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 0

    targets = []
    if args.all:
        for c in CONFIGS:  # 每配置 runs 次，4/3/3 轮转（与 --config 同逻辑）
            per = [args.runs // 3 + (1 if i < args.runs % 3 else 0) for i in range(3)]
            targets += [(c, s) for s, cnt in zip(SKILLS, per) for _ in range(cnt)]
    elif args.config:
        n = args.runs
        per = [n // 3 + (1 if i < n % 3 else 0) for i in range(3)]
        targets = [(args.config, s) for s, cnt in zip(SKILLS, per) for _ in range(cnt)]
    else:
        ap.print_help()
        return 1

    prog = load_progress()
    if prog.get("head") != head:
        prog = {"runs": [], "head": head, "started_at": datetime.now(timezone.utc).isoformat()}
        save_progress(prog)

    done_keys = {(r["config"], r["skill"], r["run_no"]) for r in prog["runs"]}
    print(f"HEAD={head} | 目标 {len(targets)} 次 | 已完成 {len(done_keys)} 次")

    for cfg, skill in targets:
        run_no = sum(1 for r in prog["runs"] if r["config"] == cfg and r["skill"] == skill) + 1
        if (cfg, skill, run_no) in done_keys:
            continue
        print(f"\n▶ [{cfg}] {skill} run#{run_no} @ {datetime.now().strftime('%H:%M:%S')}", flush=True)
        try:
            rec = run_evolve(cfg, skill, run_no, head)
        except Exception as e:  # noqa: BLE001 —— 单次失败不杀整批，记录后继续
            rec = {"run_id": f"p1i-{cfg}-{skill}-{run_no:02d}-FAIL", "config": cfg, "skill": skill,
                   "run_no": run_no, "commit": head, "error": f"EXC: {e}"[:300], "elapsed_s": 0,
                   "ts": datetime.now(timezone.utc).isoformat()}
        prog["runs"].append(rec)
        save_progress(prog)
        print(f"  → {rec.get('published', '?')}pub/{rec.get('review', '?')}rev/{rec.get('declined', '?')}dec "
              f"{rec.get('elapsed_s', 0)}s {rec.get('error', '')[:100]}", flush=True)

    # 汇总
    from collections import Counter
    agg = Counter()
    for r in prog["runs"]:
        agg[(r["config"], "runs")] += 1
        agg[(r["config"], "published")] += r.get("published", 0) or 0
        agg[(r["config"], "review")] += r.get("review", 0) or 0
        agg[(r["config"], "declined")] += r.get("declined", 0) or 0
        agg[(r["config"], "invalid")] += r.get("invalid", 0) or 0
    print("\n=== 汇总 ===")
    for c in CONFIGS:
        print(f"{c}: runs={agg[(c,'runs')]} pub={agg[(c,'published')]} rev={agg[(c,'review')]} "
              f"dec={agg[(c,'declined')]} inv={agg[(c,'invalid')]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
