#!/usr/bin/env python3
"""P2-A 真实生成 2 个测试 Skill 并注册落盘验收脚本"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from skillforge import (
    EvolveBudget,
    GeneratedSkill,
    IntentRouter,
    LLMLedger,
    SkillRegistry,
    build_manifest_report,
    generate_skill,
    render_manifest_report,
)
from skillforge.evaluator.llm_factory import build_execution_llm


def _require_generated(result: object, label: str) -> GeneratedSkill:
    if not isinstance(result, GeneratedSkill) or not result.success or not result.registered:
        print(
            f"❌ {label} 生成/注册失败: "
            f"{getattr(result, 'reason', 'unknown')}: {getattr(result, 'message', '')}"
        )
        raise SystemExit(1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="生成并注册 P2-A 文档型 Skill")
    parser.add_argument("--report", type=Path, help="将实际 repair manifest 摘要写入 Markdown 文件")
    args = parser.parse_args()

    ledger = LLMLedger(
        EvolveBudget(max_calls=4, max_tokens=20_000, deadline_seconds=300)
    )
    llm = build_execution_llm(ledger=ledger)
    print("▶ [Step 1] 真实生成 Skill 1: HTTP 状态码助手...")
    req1 = "解释 HTTP 状态码的助手——用户报 4xx/5xx 错误时给出含义与排查方向"
    res1 = _require_generated(
        generate_skill(
            req1,
            llm=llm,
            ledger=ledger,
            repo_root=ROOT,
            register=True,
            conflict_method="embedding",
        ),
        "Skill 1",
    )

    print(f"  ✅ Skill 1 生成并注册成功: {res1.name} (version: {res1.version})")
    print(f"     文件路径: {res1.skill_file}")
    print(f"     测试集数量: {len(res1.test_cases)}")
    for tc in res1.test_cases:
        print(f"       - [{tc['id']}] {tc['query'][:35]}...")

    print("\n▶ [Step 2] 真实生成 Skill 2: Markdown 语法速查...")
    req2 = "Markdown 语法速查——写文档时快速查表格/代码块/链接语法"
    res2 = _require_generated(
        generate_skill(
            req2,
            llm=llm,
            ledger=ledger,
            repo_root=ROOT,
            register=True,
            conflict_method="embedding",
        ),
        "Skill 2",
    )

    print(f"  ✅ Skill 2 生成并注册成功: {res2.name} (version: {res2.version})")
    print(f"     文件路径: {res2.skill_file}")
    print(f"     测试集数量: {len(res2.test_cases)}")
    for tc in res2.test_cases:
        print(f"       - [{tc['id']}] {tc['query'][:35]}...")

    print("\n▶ [Step 3] 验证 Registry 与 Router 加载 5 个 Skill...")
    reg = SkillRegistry(
        db_path=ROOT / "runs" / "skillforge.db",
        skills_dir=ROOT / "skills",
        repo_root=ROOT,
    )
    reg.load_skills_from_dir()
    loaded_skills = reg.list_names()
    print(f"  已注册 Skill 列表 ({len(loaded_skills)} 个): {loaded_skills}")
    expected_skills = {res1.name, res2.name}
    if len(loaded_skills) < 5 or not expected_skills.issubset(loaded_skills):
        print(f"❌ Registry 校验失败: expected={sorted(expected_skills)}, actual={loaded_skills}")
        reg.close()
        raise SystemExit(1)

    router = IntentRouter(registry=reg, llm=None)
    res_test1 = router.route("HTTP 502 错误排查")
    print(f"  Router 测试 1 ('HTTP 502 错误排查'): chosen={res_test1.chosen}, hit_layer={res_test1.hit_layer}")
    res_test2 = router.route("Markdown 怎么做表格对齐")
    print(f"  Router 测试 2 ('Markdown 怎么做表格对齐'): chosen={res_test2.chosen}, hit_layer={res_test2.hit_layer}")

    manifest_report = build_manifest_report(ROOT, [res1.name, res2.name])
    report_text = render_manifest_report(manifest_report)
    print("\n▶ [Step 4] 从 repair_set manifest 生成实际 case 报告...")
    print(report_text, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report_text, encoding="utf-8")
        print(f"  报告已写入: {args.report}")
    reg.close()

    print("\n✅ 真实生成与注册验证全部完成！")


if __name__ == "__main__":
    main()
