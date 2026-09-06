#!/usr/bin/env python3
"""P2-B 真实载体验收与 Evolve Smoke 脚本 (DeepSeek, Config C)

1. 构造合成多域 Skill「编程助手合集」(coding_helper_hub) + 8 条真实 case
2. 执行 analyze_split 耦合分析并验证裁决
3. 执行 split_skill 生成子 Skill 并注册落盘
4. 对拆后子 Skill 'coding_helper_hub_http' 跑 1 次真实 DeepSeek Evolve Smoke
5. 验证 Baseline 有效性与评测 Trace 留痕
6. 演示完毕后安全清理合成载体，保障工作区零污染
"""
from __future__ import annotations

import json
import shutil
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
    analyze_split,
    split_skill,
)
from skillforge.evaluator.llm_factory import build_llm_pair
from skillforge.router import IntentRouter


SYNTHETIC_SKILL_MD = """---
name: coding_helper_hub
version: 1.0.0
description: 综合编程开发辅助助手，提供正则表达式原理讲解与常见 HTTP 状态码排查
use_when: 用户需要理解正则表达式的语法机制与回溯原理，或者排查 HTTP 4xx/5xx 报错状态码
not_for:
  - 编写业务生产代码、爬虫脚本或后端服务开发
  - 服务器终端远程运维与 Linux 系统调优
  - 自定义 API 状态码选型与架构设计决策
dependencies: []
trigger:
  keywords:
    - 正则
    - regex
    - 状态码
    - HTTP状态码
    - 回溯
    - 报错排查
examples:
  - 讲一下 (a|b)*c 是怎么匹配的
  - 为什么 .* 会回溯这么慢
  - 502 Bad Gateway 报错原因与网关排查
  - 429 Too Many Requests 限流排查
evaluation:
  last_score: null
  last_release_id: null
---

## Overview

本技能是面向开发者的综合编程辅助说明书，涵盖两大独立能力：
1. 正则表达式原理解析：字符类、量词、分组、锚点与回溯机制。
2. HTTP 状态码排查：4xx 客户端错误与 5xx 服务端错误的原因与排查思路。

## Instructions

### 正则表达式原理解析
1. 识别用户问的是具体正则含义、概念原理还是语法区别。
2. 按识别到的类型给出分步拆解、小例子演示与陷阱提示。
3. 纯原理解析，不生成业务生产代码。

### HTTP 状态码排查
1. 从用户问题中提取状态码或错误范围（4xx/5xx）。
2. 按状态码含义、错误类别、常见原因、排查方向分步说明。
3. 遇到非标准状态码明确标注来源。

## Examples

**Q**：讲一下 `.*?` 是什么意思？
**A**：`.*?` 是非贪婪匹配：尽量少匹配字符。

**Q**：网站出现 `502 Bad Gateway` 怎么排查？
**A**：502 是网关从上游收到无效响应，排查上游服务存活与反向代理配置。

## Constraints

- 纯说明型技能，不为用户生成业务可执行代码。
- 遇服务器运维与终端部署需求，明确告知边界并引导至专业运维流程。
- 不虚构非标准状态码与内部正则引擎实现。
"""

def _load_real_carrier_cases(repair_data: dict, router_data: dict) -> list[dict]:
    """Move real er_*/ehs_* rows into the temporary composite skill.

    The smoke fixture is deliberately not authored here: every carrier row is
    read from a committed evaluation manifest and retains its original ID and
    source metadata.  The caller restores the exact manifest bytes in finally.
    """
    cases = repair_data.get("cases")
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise AssertionError("repair_set cases 必须是对象数组")
    regex_rows = [
        case for case in cases
        if str(case.get("id", "")).startswith("er_") and case.get("skill") == "explain_regex"
    ][:4]
    router_cases = router_data.get("cases")
    if not isinstance(router_cases, list) or any(not isinstance(case, dict) for case in router_cases):
        raise AssertionError("router_negatives cases 必须是对象数组")
    http_rows = [
        case for case in router_cases
        if str(case.get("id", "")).startswith("ehs_") and case.get("expected") == "explain_http_status"
    ]
    http_negative_rows = [
        case for case in router_cases
        if str(case.get("id", "")).startswith("ehs_") and case.get("expected") is None
    ]
    if len(regex_rows) != 4 or len(http_rows) < 3 or not http_negative_rows:
        raise AssertionError(
            f"真实 carrier case 不足：er_={len(regex_rows)}, ehs_positive={len(http_rows)}, "
            f"ehs_negative={len(http_negative_rows)}；拒绝自产 smoke 数据"
        )
    selected = regex_rows + http_rows[:3] + [http_negative_rows[0]]
    selected_ids = {str(case["id"]) for case in selected}
    if len(selected_ids) != len(selected):
        raise AssertionError("真实 carrier case-ID 重复")
    carrier_cases: list[dict] = []
    for case in selected:
        if "expected" in case:
            carrier = {
                "id": case["id"],
                "skill": "coding_helper_hub",
                "query": case.get("query", ""),
                "reference": case.get("why", ""),
                "source_skill": case.get("expected") or "explain_http_status",
                "source_case_id": case["id"],
                "carrier_source": "evaluation_sets/router_negatives.json",
                "source_type": case.get("type"),
            }
            carrier_cases.append(carrier)
        else:
            case["source_skill"] = case.get("skill")
            case["source_case_id"] = case["id"]
            case["carrier_source"] = "evaluation_sets/repair_set.json"
            case["source_type"] = "positive"
            case["skill"] = "coding_helper_hub"
            carrier_cases.append(case)
    repair_ids = {str(case.get("id")) for case in cases}
    duplicate_ids = sorted(set(repair_ids) & {str(case["id"]) for case in carrier_cases if case not in regex_rows})
    if duplicate_ids:
        raise AssertionError(f"真实 carrier case 与 repair_set ID 冲突: {duplicate_ids}")
    cases.extend(case for case in carrier_cases if case not in regex_rows)
    return carrier_cases


def run_smoke():
    print("================================================================")
    print("▶ 开始执行 P2-B 真实载体拆分与 Evolve Smoke (DeepSeek)...")
    print("================================================================")

    skills_dir = ROOT / "skills"
    repair_file = ROOT / "evaluation_sets" / "repair_set.json"
    router_file = ROOT / "evaluation_sets" / "router_negatives.json"
    eval_dir = repair_file.parent
    runs_dir = ROOT / "runs"

    # Snapshot every file in the touched scopes.  The smoke is allowed to use
    # the real repository, but it must leave those scopes byte-for-byte equal
    # to their pre-run state, including SQLite/traces/suggestions.
    repair_backup = repair_file.read_text(encoding="utf-8")
    router_backup = router_file.read_text(encoding="utf-8")
    eval_snapshot = {
        path.resolve(): path.read_bytes()
        for path in eval_dir.rglob("*")
        if path.is_file()
    } 
    eval_dirs_snapshot = {
        path.resolve() for path in eval_dir.rglob("*") if path.is_dir()
    }
    runs_snapshot = {
        path.resolve(): path.read_bytes()
        for path in runs_dir.rglob("*")
        if path.is_file()
    } if runs_dir.exists() else {}
    runs_dirs_snapshot = {
        path.resolve() for path in runs_dir.rglob("*") if path.is_dir()
    } if runs_dir.exists() else set()

    # Validate the real source rows before mutating anything.
    rdata = json.loads(repair_backup)
    router_data = json.loads(router_backup)
    carrier_cases = _load_real_carrier_cases(rdata, router_data)
    original_names = ["coding_helper_hub", "coding_helper_hub_regex", "coding_helper_hub_http"]
    if any((skills_dir / name).exists() or (ROOT / "skills_backup" / name).exists() for name in original_names):
        raise AssertionError("smoke 载体或归档目录已存在，拒绝覆盖现有工作区内容")

    # 1. 注入合成载体
    carrier_dir = skills_dir / "coding_helper_hub"
    carrier_dir.mkdir(parents=True, exist_ok=True)
    (carrier_dir / "SKILL.md").write_text(SYNTHETIC_SKILL_MD, encoding="utf-8")

    # 注入从权威 repair manifest 读取并临时改归属的真实 case。
    rdata["meta"]["total"] = len(rdata["cases"])
    repair_file.write_text(json.dumps(rdata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    analysis = None
    split_results = None
    sub_skill_to_smoke = "coding_helper_hub_http"
    smoke_outcome = None
    smoke_elapsed = 0.0

    try:
        # 2. 运行段 1 耦合分析器
        print("\n▶ [步骤 1/4] 运行 analyze_split 对合成载体进行三维耦合评估...")
        # The splitter and Evolve receive the same ledger object.  Evolve
        # resets it at its own run boundary; the gate below still asserts the
        # complete Evolve budget/ledger, while this call proves the splitter
        # path has an explicit ledger contract when an LLM is introduced.
        smoke_budget = EvolveBudget(
            enable_reflection=False,
            enable_a2=False,
            shadow_mode=True,
            auto_publish_enabled=False,
            max_calls=48,
            max_tokens=80_000,
            deadline_seconds=900,
        )
        ledger = LLMLedger(smoke_budget)
        analysis = analyze_split("coding_helper_hub", repo_root=ROOT, ledger=ledger, budget=smoke_budget)
        print(f"  - 裁决结果: {analysis.verdict} (can_split={analysis.can_split})")
        print(f"  - 裁决理由: {analysis.primary_reason}")
        print(f"  - 数据耦合: score={analysis.data_coupling.score:.2f}, coupled={analysis.data_coupling.coupled}")
        print(f"  - 流程耦合: score={analysis.process_coupling.score:.2f}, coupled={analysis.process_coupling.coupled}")
        print(f"  - 评测耦合: score={analysis.eval_coupling.score:.2f}, coupled={analysis.eval_coupling.coupled}")
        assert analysis.can_split is True, "合成载体预期裁决可拆，实际不可拆！"

        carrier_ids = {str(case["id"]) for case in carrier_cases}
        assigned_ids = {
            str(case["id"])
            for rows in analysis.assigned_cases.values()
            for case in rows
        }
        unassigned_ids = {str(case["id"]) for case in analysis.unassigned_cases}
        assert assigned_ids.isdisjoint(unassigned_ids), "case 同时 assigned/unassigned"
        assert assigned_ids | unassigned_ids == carrier_ids, (
            f"carrier case 不守恒: expected={sorted(carrier_ids)}, "
            f"actual={sorted(assigned_ids | unassigned_ids)}"
        )
        assert assigned_ids or unassigned_ids, "carrier case 未产生任何分析归属记录"

        # 3. 运行段 2 拆分执行器
        print("\n▶ [步骤 2/4] 运行 split_skill 执行子 Skill 划分与原子注册...")
        split_results = split_skill(
            analysis,
            repo_root=ROOT,
            register=True,
            backup_original=True,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
        )
        assert split_results.success is True, f"拆分注册失败: {split_results.errors}"
        sub_names = [s.name for s in split_results.sub_skills]
        print(f"  - 产出子 Skill: {sub_names}")
        print(f"  - 测试集分配核对:")
        for sname, cids in split_results.assigned_cases_summary.items():
            print(f"    * {sname}: {cids}")
        assert split_results.migration_manifest_path is not None
        migration_path = Path(split_results.migration_manifest_path)
        assert migration_path.exists(), f"迁移 manifest 不存在: {migration_path}"
        assert split_results.unassigned_report_path is not None
        unassigned_report_path = Path(split_results.unassigned_report_path)
        assert unassigned_report_path.exists(), f"unassigned 报告不存在: {unassigned_report_path}"
        migration = json.loads(migration_path.read_text(encoding="utf-8"))
        unassigned_report = json.loads(unassigned_report_path.read_text(encoding="utf-8"))
        assert migration.get("immutable_mapping") is True
        assert unassigned_report.get("immutable_mapping") is True
        carrier_mappings = [
            row for row in migration.get("mappings", [])
            if row.get("original_id") in carrier_ids
        ]
        assert {row.get("original_id") for row in carrier_mappings} == carrier_ids
        assert all(row.get("new_id") and row.get("new_id") != row.get("original_id") for row in carrier_mappings)
        assert migration.get("unassigned_count") == len(migration.get("unassigned_cases", []))
        assert unassigned_report.get("count") == migration.get("unassigned_count")
        assert set(migration.get("scanned_manifests", [])) >= {
            "repair_set.json", "experiment_holdout.json", "final_audit.json",
            "baseline_seen_regression.json", "router_negatives.json",
        }
        for manifest_path in eval_dir.rglob("*.json"):
            if "split_migrations" in manifest_path.parts:
                continue
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for case in manifest_data.get("cases", []):
                assert case.get("skill") != "coding_helper_hub"
                if manifest_path.name == "router_negatives.json":
                    assert case.get("expected") != "coding_helper_hub"

        # 4. 验证三层路由互斥（两子 Skill 之间的互斥区分能力）
        print("\n▶ [步骤 3/4] 验证两子 Skill 之间的路由互斥与区分能力...")
        reg = SkillRegistry(db_path=ROOT / "runs" / "skillforge.db", skills_dir=skills_dir, repo_root=ROOT)
        reg.load_skills_from_dir()
        router = IntentRouter(registry=reg)
        router._ensure_indexed()

        r_reg = dict(router.embed.search("讲一下 (a|b)*c 是怎么匹配的", top_k=10))
        r_http = dict(router.embed.search("502 Bad Gateway 报错原因与网关排查", top_k=10))

        reg_score_on_reg = r_reg.get("coding_helper_hub_regex", 0.0)
        http_score_on_reg = r_reg.get("coding_helper_hub_http", 0.0)
        print(f"  - 正则查询在两子 Skill 间得分: regex_sub={reg_score_on_reg:.3f} vs http_sub={http_score_on_reg:.3f} (分差={reg_score_on_reg - http_score_on_reg:+.3f})")

        http_score_on_http = r_http.get("coding_helper_hub_http", 0.0)
        reg_score_on_http = r_http.get("coding_helper_hub_regex", 0.0)
        print(f"  - HTTP查询在两子 Skill 间得分: http_sub={http_score_on_http:.3f} vs regex_sub={reg_score_on_http:.3f} (分差={http_score_on_http - reg_score_on_http:+.3f})")

        assert reg_score_on_reg > http_score_on_reg + 0.05, "正则查询未清晰路由到 regex 子 Skill！"
        assert http_score_on_http > reg_score_on_http + 0.05, "HTTP查询未清晰路由到 http 子 Skill！"
        for case in carrier_cases:
            route_result = router.route(case["query"])
            expected = (
                "coding_helper_hub_regex"
                if str(case["id"]).startswith("er_")
                else ("coding_helper_hub_http" if case.get("source_type") == "positive" else None)
            )
            assert route_result.chosen == expected, (
                f"真实 carrier route 选择错误: {case['id']} -> {route_result.chosen!r}, expected={expected!r}"
            )
        print("  ✓ 子 Skill 间互斥区分校验完全通过！")

        # 5. 真实 DeepSeek Evolve Smoke
        print(f"\n▶ [步骤 4/4] 对子 Skill '{sub_skill_to_smoke}' 执行真实 Evolve Smoke (Config C)...")
        budget = smoke_budget
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

        run_id = f"p2b-smoke-C-{sub_skill_to_smoke}-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"

        t0 = time.time()
        smoke_outcome = evolver.evolve_full(
            skill_name=sub_skill_to_smoke,
            max_candidates=1,
            eval_set_for_iter="repair_set",
            verbose=True,
            budget=budget,
            run_id=run_id,
        )
        smoke_elapsed = time.time() - t0

        print(f"\n================ 真实 Evolve Smoke 结果 ================")
        print(f"Skill: {sub_skill_to_smoke}")
        print(f"Run ID: {smoke_outcome.run_id}")
        print(f"耗时: {smoke_elapsed:.2f}s")
        print(f"Baseline 得分: {smoke_outcome.baseline_score}")
        print(f"生成 Patch 数: {smoke_outcome.patches_generated}")
        print(f"审核 (REVIEW) 建议数: {len(smoke_outcome.patches_review)}")
        print(f"轨迹文件: {smoke_outcome.trace_file}")
        if smoke_outcome.error:
            print(f"错误信息: {smoke_outcome.error}")

        assert smoke_outcome.error is None, f"Evolve Smoke 失败: {smoke_outcome.error}"
        assert smoke_outcome.baseline_score is not None, "Baseline score 不能为 None"
        assert Path(smoke_outcome.trace_file).exists(), f"审计轨迹文件不存在: {smoke_outcome.trace_file}"
        trace_lines = Path(smoke_outcome.trace_file).read_text(encoding="utf-8").splitlines()
        assert trace_lines, "Evolve trace 为空"
        assert all(smoke_outcome.run_id in line for line in trace_lines), "trace 存在 run_id 漂移"
        assert ledger.total_calls > 0, "Evolve 未产生可审计 LLM ledger 记录"
        assert ledger.failed_calls == 0, f"Evolve ledger 存在失败调用: {ledger.failed_calls}"
        assert ledger.total_calls <= budget.max_calls
        assert ledger.total_tokens <= budget.max_tokens
        print("✅ 真实 Evolve Smoke 验证成功！Baseline 有效！")

    finally:
        # 清理合成载体与子技能，还原所有快照；任一清理/恢复失败都必须
        # 让 smoke 失败，不能被主体结果掩盖。
        print("\n▶ [清理] 安全清理合成载体与注册产物，恢复工作区干净状态...")
        cleanup_errors = []
        for name in original_names:
            p = skills_dir / name
            if p.exists():
                try:
                    shutil.rmtree(p)
                except Exception as exc:
                    cleanup_errors.append(f"删除 {p}: {exc}")
            bp = ROOT / "skills_backup" / name
            if bp.exists():
                try:
                    shutil.rmtree(bp)
                except Exception as exc:
                    cleanup_errors.append(f"删除 {bp}: {exc}")

        def restore_scope(scope: Path, snapshot: dict[Path, bytes], snapshot_dirs: set[Path]) -> None:
            current_files = {
                path.resolve() for path in scope.rglob("*") if path.is_file()
            } if scope.exists() else set()
            for path in sorted(current_files - set(snapshot), key=str, reverse=True):
                path.unlink()
            for path, payload in snapshot.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists() or path.read_bytes() != payload:
                    path.write_bytes(payload)
            if scope.exists():
                current_dirs = {
                    path.resolve() for path in scope.rglob("*") if path.is_dir()
                }
                for directory in sorted(current_dirs - snapshot_dirs, key=str, reverse=True):
                    if directory.exists():
                        try:
                            directory.rmdir()
                        except OSError:
                            # A non-empty or otherwise undeletable generated
                            # directory is a hard cleanup failure below.
                            pass
            current_after = {
                path.resolve(): path.read_bytes()
                for path in scope.rglob("*")
                if path.is_file()
            } if scope.exists() else {}
            current_dirs_after = {
                path.resolve() for path in scope.rglob("*") if path.is_dir()
            } if scope.exists() else set()
            if current_after != snapshot or current_dirs_after != snapshot_dirs:
                raise RuntimeError(f"快照恢复后仍有副作用: {scope}")

        try:
            restore_scope(eval_dir, eval_snapshot, eval_dirs_snapshot)
            restore_scope(runs_dir, runs_snapshot, runs_dirs_snapshot)
        except Exception as exc:
            cleanup_errors.append(f"恢复快照失败: {exc}")
        for name in original_names:
            for path in (skills_dir / name, ROOT / "skills_backup" / name):
                if path.exists():
                    cleanup_errors.append(f"清理后目录仍存在: {path}")
        if cleanup_errors:
            raise RuntimeError("smoke 副作用清理失败: " + "; ".join(cleanup_errors))
        print("  ✓ 工作区恢复完毕，无遗留污染。")

    return {
        "analysis": analysis,
        "split_results": split_results,
        "smoke_outcome": smoke_outcome,
        "smoke_elapsed": smoke_elapsed,
    }


if __name__ == "__main__":
    run_smoke()
