#!/usr/bin/env python3
"""P2-A 验收 ③：对新生成的 Skill 跑 1 次真实 evolve smoke（DeepSeek，C 配置语义）"""
from __future__ import annotations

import sys
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
)
from skillforge.evaluator.llm_factory import build_llm_pair


def main():
    skill_name = "explain_http_status"
    print(f"▶ 开始对新 Skill '{skill_name}' 运行真实 Evolve Smoke (DeepSeek, Config C)...")

    reg = SkillRegistry(
        db_path=ROOT / "runs" / "skillforge.db",
        skills_dir=ROOT / "skills",
        repo_root=ROOT,
    )
    reg.load_skills_from_dir()

    # C 配置语义：无反思、无 A2、Shadow 模式；同时给 smoke 设置明确硬帽。
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

    run_id = f"p2a-smoke-C-{skill_name}-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"

    t0 = time.time()
    outcome = evolver.evolve_full(
        skill_name=skill_name,
        max_candidates=1,
        eval_set_for_iter="repair_set",
        verbose=True,
        budget=budget,
        run_id=run_id,
    )
    elapsed = time.time() - t0

    print(f"\n================ 真实 Evolve Smoke 结果 ================")
    print(f"Skill: {skill_name}")
    print(f"Run ID: {outcome.run_id}")
    print(f"耗时: {elapsed:.2f}s")
    print(f"Baseline 得分: {outcome.baseline_score}")
    print(f"生成 Patch 数: {outcome.patches_generated}")
    print(f"审核 (REVIEW) 建议数: {len(outcome.patches_review)}")
    print(f"轨迹文件: {outcome.trace_file}")
    if outcome.error:
        print(f"错误信息: {outcome.error}")

    # 验证 baseline 评估有效性与轨迹存在；“无有效失败”是正常终态，不等同于异常。
    print(f"\n▶ 验证 Baseline 评估有效性:")
    assert outcome.baseline_score is not None, "Baseline score 不能为 None"
    assert Path(outcome.trace_file).exists(), f"审计轨迹文件不存在: {outcome.trace_file}"
    if outcome.error and not outcome.error.startswith("无 B_better 失败样本"):
        raise RuntimeError(f"Evolve smoke 失败: {outcome.error}")
    assert outcome.ledger is not None
    print(f"  ✓ baseline_score = {outcome.baseline_score}")
    print(f"  ✓ trace_file = {outcome.trace_file}")
    print(f"  ✓ ledger = {outcome.ledger.as_dict()}")
    print("✅ 真实 Evolve Smoke 验证成功！")


if __name__ == "__main__":
    main()
