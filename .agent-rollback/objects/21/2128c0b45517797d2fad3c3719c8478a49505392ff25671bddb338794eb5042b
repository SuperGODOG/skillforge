"""SkillForge CLI 入口

用法：
    skillforge demo [--query "..." ] [--skill weather_query]
    skillforge route <query> [--use-llm] [--top-k 5]      # Phase 2
    skillforge evaluate                                    # Phase 3
    skillforge evolve --skill X                            # Phase 4
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

from . import SkillRegistry, IntentRouter, SkillEvaluator, SkillEvolver, ReleaseStateMachine


def cmd_demo(args: argparse.Namespace) -> None:
    """Phase 1 演示：SkillRegistry 元数据索引 + use_skill 完整链路"""
    repo_root: Path = args.root
    db_path = repo_root / "runs" / "skillforge.db"
    skills_dir = repo_root / "skills"

    _print_hr("SkillForge Phase 1 Demo · Agent 主导渐进式披露最小闭环")

    # Step 1: 加载
    print("\n▶ Step 1: 加载 skills/ 目录（Pydantic 解析 SKILL.md frontmatter）")
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()
    names = reg.list_names()
    print(f"  ✓ 已注册 {len(names)} 个 skill: {names}")

    # Step 2: 元数据索引
    print("\n▶ Step 2: 元数据索引（会追加到 Agent 的 system prompt）")
    _hr()
    print(reg.build_index())
    _hr()

    # Step 3: 决策哪个 Skill
    query = args.query
    chosen = args.skill or _simple_rule_match(reg, query)

    print(f"\n▶ Step 3: 模拟 Agent 决策")
    print(f"  用户查询：{query!r}")
    if not chosen:
        print(f"  ⚠️  未匹配到 skill（规则层无命中），Phase 2 会有 embedding + LLM 兜底")
        reg.close()
        return
    print(f"  Agent 决定：调用 use_skill({chosen!r}, ...)")

    # Step 4: 走 use_skill
    print(f"\n▶ Step 4: 加载 Skill Body（SQLite→Git 全链路，失败降级到磁盘）")
    reason = f"用户查询 '{query}'，判断需要 {chosen} 提供的能力"
    body = reg.use_skill(chosen, reason)
    lines = body.splitlines()
    preview = "\n".join(lines[:15])
    print(f"  ✓ body 长度 {len(body)} chars（{len(lines)} 行）\n")
    _hr()
    print(preview)
    if len(lines) > 15:
        print(f"  … （省略 {len(lines) - 15} 行）")
    _hr()

    # Step 5: 日志
    print(f"\n▶ Step 5: router.jsonl 最新一条")
    log_path = reg.router_log
    if log_path.exists():
        last = log_path.read_text(encoding="utf-8").splitlines()[-1]
        record = json.loads(last)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(f"  ⚠️  {log_path} 不存在")

    reg.close()

    _print_hr(f"Demo 完成 · 完整日志: {log_path}")


def cmd_route(args: argparse.Namespace) -> None:
    """Phase 2 演示：三层级联路由（规则 → embed → LLM 兜底）"""
    repo_root: Path = args.root
    db_path = repo_root / "runs" / "skillforge.db"
    skills_dir = repo_root / "skills"

    _print_hr(f"IntentRouter · query = {args.query!r}")

    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()

    llm = None
    if args.use_llm:
        try:
            from dotenv import load_dotenv
            from hello_agents import HelloAgentsLLM
            load_dotenv(repo_root / ".env")
            llm = HelloAgentsLLM(
                api_key=os.environ["LLM_API_KEY"],
                model=os.environ["LLM_MODEL_ID"],
                base_url=os.environ["LLM_BASE_URL"],
            )
            print("  ✓ LLM 兜底已启用 (DeepSeek)")
        except KeyError as e:
            print(f"  ⚠️  .env 缺失 {e}，退化为规则+embed 两层")
        except Exception as e:
            print(f"  ⚠️  LLM 初始化失败：{e}，退化为规则+embed 两层")

    router = IntentRouter(registry=reg, llm=llm)
    result = router.route(args.query)

    print()
    print(f"chosen    : {result.chosen or '(NONE — 拒绝路由)'}")
    print(f"hit_layer : {result.hit_layer}")
    print(f"latency   : {result.latency_ms} ms")
    print()
    print("scores:")
    for layer, sc in result.scores.items():
        if isinstance(sc, dict):
            if not sc:
                print(f"  {layer}: (空)")
            else:
                for name, val in sc.items():
                    v = f"{val:.4f}" if isinstance(val, float) else str(val)
                    print(f"  {layer}.{name}: {v}")
        else:
            print(f"  {layer}: {sc}")

    reg.close()


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Phase 3 演示：八维评估器（结构分 + Judge 配对 + 客观指标 + 棘轮）"""
    repo_root: Path = args.root
    db_path = repo_root / "runs" / "skillforge.db"
    skills_dir = repo_root / "skills"

    _print_hr(f"SkillEvaluator · skill={args.skill}  eval_set={args.eval_set}")

    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()

    # 执行端与 Judge 使用独立配置点和 client/session。
    try:
        from dotenv import load_dotenv
        from .evaluator.llm_factory import build_llm_pair
        load_dotenv(repo_root / ".env")
        llm, judge_llm = build_llm_pair()
    except KeyError as e:
        print(f"  ❌ .env 缺失 {e}，评估无法进行（Phase 3 强依赖 LLM）")
        return 2

    evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

    print(f"  ▶ 开始评估...（每个 case 3 次 LLM 调用 + Judge 3 维 × 每维 1 次 = 6 次调用）")
    result = evaluator.evaluate_skill(
        args.skill,
        eval_set=args.eval_set,
        verbose=args.verbose,
    )

    if not result.valid:
        print("\n❌ 评估 INVALID，已按 fail-closed 停止评分：")
        for reason in result.invalid_reasons:
            print(f"  - {reason}")
        reg.close()
        return 2

    print()
    _print_hr("评估报告")
    print(f"结构分（40 分，权重 40%，不阻断发布）:")
    for k, v in result.structure_score.items():
        print(f"  {k:12s}: {v:>5.2f}")
    print(f"  {'小计':12s}: {sum(result.structure_score.values()):>5.2f} / 40")

    print(f"\n效果分（60 分，权重 60%，发布门槛）:")
    for k, v in result.effect_score.items():
        print(f"  {k:12s}: {v:>5.2f}")
    print(f"  {'小计':12s}: {sum(result.effect_score.values()):>5.2f} / 60")

    total = sum(result.structure_score.values()) + sum(result.effect_score.values())
    print(f"\n总分: {total:.2f} / 100")

    print(f"\n客观指标（平均）:")
    for k, v in result.objective_metrics.items():
        print(f"  {k:24s}: {v:>8.2f}")

    print(f"\nP0 用例: {'✓ 全通过' if result.p0_pass else '❌ 有失败（棘轮硬门槛 5 触发）'}")

    reg.close()
    _print_hr("完成")
    return 0


def cmd_evolve(args: argparse.Namespace) -> None:
    """Phase 4 演示：SkillEvolver 元 Agent 六步迭代"""
    repo_root: Path = args.root
    db_path = repo_root / "runs" / "skillforge.db"
    skills_dir = repo_root / "skills"

    _print_hr(f"SkillEvolver · skill={args.skill}  eval_set={args.eval_set}  max_candidates={args.max_candidates}")

    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()

    try:
        from dotenv import load_dotenv
        from .evaluator.llm_factory import build_llm_pair
        load_dotenv(repo_root / ".env")
        llm, judge_llm = build_llm_pair()
    except KeyError as e:
        print(f"  ❌ .env 缺失 {e}，元 Agent 无法启动")
        return

    evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)

    evolver = SkillEvolver(
        registry=reg, evaluator=evaluator, llm=llm, state_machine=sm,
    )
    outcome = evolver.evolve_full(
        args.skill,
        max_candidates=args.max_candidates,
        eval_set_for_iter=args.eval_set,
        verbose=True,
    )

    print()
    _print_hr("迭代产出")
    print(f"skill:            {outcome.skill_name}")
    print(f"baseline_score:   {outcome.baseline_score:.2f}")
    print(f"patches_generated: {outcome.patches_generated}")
    print(f"published (L1 auto): {len(outcome.patches_published)}  {outcome.patches_published}")
    print(f"review (L2 建议):    {len(outcome.patches_review)}")
    for p in outcome.patches_review[:3]:
        print(f"    · {p}")
    print(f"declined (归档):     {len(outcome.patches_declined)}")
    for p in outcome.patches_declined[:3]:
        print(f"    · {p}")
    if outcome.error:
        print(f"error: {outcome.error}")

    reg.close()
    sm.close()
    _print_hr("完成")


def _simple_rule_match(reg: SkillRegistry, query: str) -> str | None:
    """简易规则匹配（Phase 2 会被 IntentRouter 替换）"""
    best_name, best_hits = None, 0
    for name in reg.list_names():
        meta = reg.get_meta(name)
        hits = sum(1 for kw in meta.trigger.keywords if kw in query)
        if hits > best_hits:
            best_hits, best_name = hits, name
    return best_name


def _hr() -> None:
    print("-" * 72)


def _print_hr(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="skillforge",
        description="Agent Skill 自进化元 Agent 系统",
    )
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(),
        help="项目根目录（默认: 当前工作目录）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    demo_p = sub.add_parser("demo", help="Phase 1 演示：use_skill 完整链路")
    demo_p.add_argument("--query", default="北京今天天气", help="用户查询")
    demo_p.add_argument("--skill", default=None, help="指定 skill_name（否则用规则匹配）")

    route_p = sub.add_parser("route", help="Phase 2：三层级联路由")
    route_p.add_argument("query", help="用户查询")
    route_p.add_argument("--use-llm", action="store_true", help="启用 LLM 兜底（需 .env 有 LLM_API_KEY）")
    route_p.add_argument("--top-k", type=int, default=5, help="embed 层 top-K（默认 5）")

    eval_p = sub.add_parser("evaluate", help="Phase 3：八维评估器 + 棘轮")
    eval_p.add_argument("--skill", required=True, help="要评估的 skill_name")
    eval_p.add_argument("--eval-set", default="baseline_dev",
                        help="评估集（默认 baseline_dev；可选 baseline_hidden）")
    eval_p.add_argument("--verbose", action="store_true", help="逐 case 打印进度")

    evolve_p = sub.add_parser("evolve", help="Phase 4：元 Agent 六步迭代（半自动）")
    evolve_p.add_argument("--skill", required=True, help="要迭代的 skill_name")
    evolve_p.add_argument("--max-candidates", type=int, default=3,
                          help="一次生成候选数（默认 3）")
    evolve_p.add_argument("--eval-set", default="baseline_hidden",
                          help="迭代评估用哪个集（默认 baseline_hidden，8 条快）")

    args = parser.parse_args()

    if args.cmd == "demo":
        cmd_demo(args)
    elif args.cmd == "route":
        cmd_route(args)
    elif args.cmd == "evaluate":
        return cmd_evaluate(args)
    elif args.cmd == "evolve":
        cmd_evolve(args)
    else:
        print(f"[skillforge] cmd={args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
