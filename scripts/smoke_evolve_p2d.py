#!/usr/bin/env python3
"""P2-D 真实载体验收：对 weather_query 运行 1 次真实 LangGraph Evolve Smoke (DeepSeek, Config C)

验收重点：
1. 验证 LangGraph StateGraph 在真实 LLM 与沙箱评测环境下的端到端执行；
2. 验证 Config C（无反思、无 A2、Shadow 模式、硬预算硬帽）；
3. 验证 baseline 评估有效性、audit trace 文件留痕与 LLMLedger 消耗记录；
4. 验证 LangGraph 状态机转移轨迹正常沉淀。
"""
from __future__ import annotations

import sys
import hashlib
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from skillforge import (
    SkillRegistry,
    SkillEvaluator,
    SkillEvolver,
    ReleaseStateMachine,
    EvolveBudget,
    LLMLedger,
    run_evolve_langgraph,
)
from skillforge.evaluator.llm_factory import build_llm_pair


def _tree_snapshot(root: Path) -> list[tuple[str, str]]:
    if not root.exists():
        return []
    return [
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(path for path in root.rglob('*') if path.is_file())
    ]


def main():
    skill_name = "weather_query"
    print(f"▶ [P2-D Smoke] 开始对 '{skill_name}' 运行真实 LangGraph Evolve Smoke (DeepSeek, Config C)...")

    reg = SkillRegistry(
        db_path=ROOT / "runs" / "skillforge.db",
        skills_dir=ROOT / "skills",
        repo_root=ROOT,
    )
    reg.load_skills_from_dir()

    # C 配置语义：无反思、无 A2、Shadow 模式；给 smoke 设置明确安全硬帽。
    budget = EvolveBudget(
        enable_reflection=False,
        enable_a2=False,
        shadow_mode=True,
        auto_publish_enabled=False,
        max_calls=32,
        max_tokens=40_000,
        deadline_seconds=900,
    )
    ledger = LLMLedger(budget)
    llm, judge_llm = build_llm_pair(ledger=ledger)
    evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)
    sm = ReleaseStateMachine(db_path=ROOT / "runs" / "skillforge.db", repo_root=ROOT)
    evolver = SkillEvolver(
        registry=reg,
        evaluator=evaluator,
        llm=llm,
        state_machine=sm,
        budget=budget,
        ledger=ledger,
    )

    run_id = f"p2d-smoke-C-{skill_name}-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"

    shadow_root = Path(tempfile.mkdtemp(prefix="skillforge-p2d-smoke-shadow-"))
    live_runs_before = _tree_snapshot(ROOT / "runs")
    t0 = time.time()
    outcome = run_evolve_langgraph(
        evolver,
        skill_name=skill_name,
        max_candidates=1,
        eval_set_for_iter="repair_set",
        verbose=True,
        budget=budget,
        run_id=run_id,
        shadow_root=shadow_root,
    )
    elapsed = time.time() - t0
    live_runs_after = _tree_snapshot(ROOT / "runs")

    print(f"\n================ 真实 LangGraph Evolve Smoke 结果 ================")
    print(f"Skill: {skill_name}")
    print(f"Run ID: {outcome.run_id}")
    print(f"耗时: {elapsed:.2f}s")
    print(f"Baseline 得分: {outcome.baseline_score}")
    print(f"生成 Patch 数: {outcome.patches_generated}")
    print(f"审核 (REVIEW) 建议数: {len(outcome.patches_review)}")
    print(f"拒绝 (DECLINED) 建议数: {len(outcome.patches_declined)}")
    print(f"终止原因码: {getattr(outcome.context, 'stop_reason', None)}")
    print(f"审计轨迹文件: {outcome.trace_file}")
    if outcome.error:
        print(f"错误信息: {outcome.error}")

    transitions = getattr(outcome, "graph_transitions", [])
    print(f"\n▶ 状态转移序列 (共 {len(transitions)} 步):")
    for idx, t in enumerate(transitions, 1):
        print(f"  [{idx:02d}] R{t['round_no']} | {t['from_node']:<22} -> {t['to_node']:<22} | {t['reason']}")

    print(f"\n▶ 验证 Baseline 评估有效性与 Trace 留痕:")
    assert outcome.baseline_score is not None, "Baseline score 不能为 None"
    assert Path(outcome.trace_file).exists(), f"审计轨迹文件不存在: {outcome.trace_file}"
    assert outcome.ledger is not None, "LLMLedger 必须被正确初始化与记录"
    ledger_data = outcome.ledger.as_dict()
    assert outcome.shadow_root == str(shadow_root.resolve())
    assert Path(outcome.trace_file).resolve().is_relative_to(shadow_root.resolve())
    assert all((shadow_root / item["path"]).resolve().is_relative_to(shadow_root.resolve()) for item in outcome.side_effect_manifest)
    assert outcome.patches_published == [], "P2-D smoke 不得发布到主链"
    assert ledger_data["total_calls"] <= budget.max_calls
    assert ledger_data["total_tokens"] <= budget.max_tokens
    assert live_runs_before == live_runs_after, "LangGraph shadow 运行改变了主链 runs 目录"
    print(f"  ✓ baseline_score = {outcome.baseline_score}")
    print(f"  ✓ trace_file = {outcome.trace_file} (已落盘)")
    print(f"  ✓ ledger calls = {ledger_data.get('total_calls')}, tokens = {ledger_data.get('total_tokens')}")
    print(f"  ✓ LangGraph 转移数 = {len(transitions)}")
    print("✅ P2-D 真实 LangGraph Evolve Smoke 验收成功！")


if __name__ == "__main__":
    main()
