#!/usr/bin/env python3
"""E 项验收：repair_set 固定基础集 + manifest auto cases 的三维状态清单验证。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from skillforge import SkillRegistry, SkillEvaluator
from skillforge.evaluator.fixtures import (
    create_nonce_weather_fixture,
    extract_weather_query_city,
    _weather_query_intent,
)
from skillforge.evolver import (
    _is_judge_infrastructure_error,
    _is_effective_failure,
)

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_CASE_IDS = frozenset({
    "er_d01", "er_d02", "er_d04", "er_d10", "er_d11", "er_h01", "er_h02", "er_h03",
    "wq_d01", "wq_d05", "wq_d07", "wq_d10", "wq_h01", "wq_h02", "wq_h03",
    "wr_d01", "wr_d03", "wr_d04", "wr_d10", "wr_d11", "wr_h01", "wr_h02",
})
REQUIRED_DIMS = ("task_completion", "robustness", "readability")
ALLOWED_VERDICTS = {"A_better", "tied", "B_better", "INVALID"}


def _is_auto_case(case: dict[str, Any]) -> bool:
    return "_auto_" in str(case.get("id", ""))


def validate_repair_manifest(cases: list[dict[str, Any]], meta: dict[str, Any]) -> list[str]:
    """Fail-closed validation for the fixed 22-case base plus auto manifest."""
    errors: list[str] = []
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        return ["repair_set cases 必须是对象数组"]
    if not isinstance(meta, dict):
        return ["repair_set meta 必须是对象"]
    ids = [str(case.get("id", "")) for case in cases]
    id_set = set(ids)
    duplicate_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicate_ids:
        errors.append(f"repair_set 存在重复 case-ID: {duplicate_ids}")

    declared_raw = meta.get("auto_case_ids")
    if (
        not isinstance(declared_raw, list)
        or any(not isinstance(item, str) or not item or "_auto_" not in item for item in declared_raw)
    ):
        errors.append("repair_set meta 缺失合法 _auto_ case manifest")
        declared_ids: set[str] = set()
    else:
        declared_ids = set(declared_raw)
        if len(declared_ids) != len(declared_raw):
            errors.append("repair_set meta.auto_case_ids 存在重复 ID")

    actual_auto_ids = {case_id for case_id in ids if "_auto_" in case_id}
    if actual_auto_ids != declared_ids:
        errors.append(
            "repair_set _auto_ 与 meta.auto_case_ids 不一致: "
            f"actual={sorted(actual_auto_ids)}, declared={sorted(declared_ids)}"
        )
    if type(meta.get("auto_case_count")) is not int or meta.get("auto_case_count") != len(declared_ids):
        errors.append(
            f"repair_set meta.auto_case_count ({meta.get('auto_case_count')}) != manifest 数量 ({len(declared_ids)})"
        )
    if type(meta.get("total")) is not int or meta.get("total") != len(cases):
        errors.append(f"repair_set meta.total ({meta.get('total')}) != cases 数量 ({len(cases)})")

    expected_ids = set(EXPECTED_CASE_IDS) | declared_ids
    missing = sorted(expected_ids - id_set)
    extra = sorted(id_set - expected_ids)
    if missing:
        errors.append(f"repair_set case-ID 不完整: missing={missing}")
    if extra:
        errors.append(f"repair_set 存在未在固定基础集或 manifest 中声明的 extra case-ID: extra={extra}")
    for case in cases:
        if _is_auto_case(case) and not str(case.get("trace_id", "")).strip():
            errors.append(f"auto case {case.get('id')} 缺失 trace_id")
        if _is_auto_case(case):
            for field_name in ("skill", "query", "reference"):
                if not str(case.get(field_name, "")).strip():
                    errors.append(f"auto case {case.get('id')} 缺失 {field_name}")
    return errors


def _validate_judge_case_shape(case_id: str, cv: dict[str, Any]) -> list[str]:
    """Require the complete auditable three-dimension Judge contract."""
    errors: list[str] = []
    audits = cv.get("judge_audit")
    if not isinstance(audits, dict):
        return [f"{case_id}: 缺失 judge_audit 三维审计对象"]
    for dim in REQUIRED_DIMS:
        value = cv.get(dim)
        if value not in ALLOWED_VERDICTS:
            errors.append(f"{case_id}/{dim}: 非法或缺失 verdict {value!r}")
            continue
        audit = audits.get(dim)
        if not isinstance(audit, dict):
            errors.append(f"{case_id}/{dim}: 缺失 judge_audit")
            continue
        order = audit.get("presented_order")
        if (
            not isinstance(order, dict)
            or order.get("A") not in {"skill", "baseline"}
            or order.get("B") not in {"skill", "baseline"}
            or {order.get("A"), order.get("B")} != {"skill", "baseline"}
        ):
            errors.append(f"{case_id}/{dim}: presented_order 缺失或侧别重复")
        reason_codes = audit.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or not reason_codes
            or any(not isinstance(code, str) or not code.strip() for code in reason_codes)
        ):
            errors.append(f"{case_id}/{dim}: reason_codes 缺失或为空")
        if not isinstance(audit.get("evidence_summary"), str) or not audit.get("evidence_summary", "").strip():
            errors.append(f"{case_id}/{dim}: evidence_summary 缺失或为空")
        if audit.get("canonical_verdict") != value:
            errors.append(
                f"{case_id}/{dim}: canonical_verdict 与 case verdict 不一致 "
                f"({audit.get('canonical_verdict')!r} != {value!r})"
            )
    return errors

class HighFidelityEvalLLM:
    """高保真 LLM，为 repair_set cases 提供贴近真实的 baseline 和 skill 输出。"""
    model = "hf-eval-llm"

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if tool_messages:
            payload = json.loads(tool_messages[0]["content"])
            forecast = payload["forecasts"][0]
            city = forecast["city"]
            query = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
            intent = _weather_query_intent(query)
            reports = []
            for offset in intent["offsets"]:
                cast = forecast["casts"][offset]
                reports.append(
                    f"{cast['date']} {city}天气{cast['dayweather']}/{cast['nightweather']}，"
                    f"{cast['daytemp']}-{cast['nighttemp']}度，{cast['daywind']}{cast['daypower']}级，"
                    f"降水概率{cast['precipitation_probability']}%。"
                )
            reply = "；".join(reports)
            if "穿衣" in query:
                reply += " 建议结合气温分层穿衣。"
            if "出海" in query:
                reply += " 出海请根据风力谨慎安排，注意安全。"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply, tool_calls=[]))],
                usage=None,
            )

        query = messages[-1]["content"]
        city = extract_weather_query_city(query)
        if city:
            tool_call = SimpleNamespace(
                id=f"call-{city}",
                function=SimpleNamespace(
                    name="amap_weather_api",
                    arguments=json.dumps({"city": city, "extensions": "all"}, ensure_ascii=False),
                ),
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))],
                usage=None,
            )
        # 边界拒绝（例如查昨天）
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="抱歉，我只能查询今天及未来 3 天的天气，无法查询历史天气。", tool_calls=[]))],
            usage=None,
        )

    def invoke(self, messages, **kwargs):
        query = messages[-1]["content"]
        # Baseline bare 回答（模拟无 skill 时的弱/空泛/编造表现）
        if "周报" in query:
            if "截图" in query:
                # wr_d11: baseline 侧编造数据
                return SimpleNamespace(content="周报已为您生成：根据数据看板截图，本周访问量达到10000人次，转化率5%。", usage=None)
            if "三件事" in query:
                # wr_d01: baseline 侧空泛未充分展开
                return SimpleNamespace(content="本周工作总结：完成了项目A上线，修复了部分bug，与产品进行了沟通。", usage=None)
            if "李四" in query:
                return SimpleNamespace(content="李四本周主要负责了模块开发和代码评审工作。", usage=None)
            if "啥也没干" in query:
                return SimpleNamespace(content="本周周报：本周主要进行系统维护和日常沟通。", usage=None)
            return SimpleNamespace(content="本周周报已经草拟完毕，请查收。", usage=None)

        if "天气" in query or "穿衣" in query or "下雨" in query or "风大" in query or "降水" in query:
            if "昨天" in query:
                return SimpleNamespace(content="昨天的天气请查询历史气象记录。", usage=None)
            # baseline 给出无工具的假定数值
            return SimpleNamespace(content="当前城市气温约22度，天气晴朗，风力3级。", usage=None)

        # 正则表达式
        if "(a|b)*c" in query:
            return SimpleNamespace(content="(a|b)*c 匹配以 c 结尾，前面有任意个 a 或 b 的字符串。", usage=None)
        if ".*" in query:
            return SimpleNamespace(content=".* 是贪婪匹配，会先匹配到底然后再逐个回溯，因此在长文本中可能变慢。", usage=None)
        if "NFA" in query:
            return SimpleNamespace(content="NFA是不确定有限状态自动机，DFA是确定有限状态自动机，两者的匹配状态回溯机制不同。", usage=None)
        return SimpleNamespace(content="这是一个关于正则表达式的解答。", usage=None)


class HighFidelityJudgeLLM:
    """高保真 Judge LLM，遵循真实判定逻辑。"""
    model = "hf-judge-llm"

    def invoke(self, messages, **kwargs):
        content = messages[-1]["content"]
        # 针对不同 case 给出客观的评判
        if "数据看板截图" in content and "10000" in content:
            # 涉及未核验外部事实/编造
            return SimpleNamespace(content=json.dumps({
                "verdict": "A_better",
                "reason_codes": ["ACCURATE_BOUNDARY_COMPLIANCE"],
                "evidence_summary": "A 正确说明文本限制拒绝编造，B 编造截图数值",
            }), usage=None)

        if "三件事" in content and "四段结构" in content:
            return SimpleNamespace(content=json.dumps({
                "verdict": "A_better",
                "reason_codes": ["EVIDENCE_SUFFICIENT"],
                "evidence_summary": "A 结构完整符合四段规范，B 侧空泛未展开",
            }), usage=None)

        return SimpleNamespace(content=json.dumps({
            "verdict": "A_better",
            "reason_codes": ["EVIDENCE_SUFFICIENT"],
            "evidence_summary": "Skill 回答具备更规范的格式与工具/知识对齐",
        }), usage=None)


def run_e_acceptance(case_id: Optional[str] = None, repair_file_path: Optional[Path] = None):
    registry = SkillRegistry(db_path=ROOT / "runs" / "test_acceptance.db", skills_dir=ROOT / "skills", repo_root=ROOT)
    registry.load_skills_from_dir()

    llm = HighFidelityEvalLLM()
    judge_llm = HighFidelityJudgeLLM()
    evaluator = SkillEvaluator(registry=registry, llm=llm, judge_llm=judge_llm)

    repair_file = repair_file_path or (ROOT / "evaluation_sets" / "repair_set.json")
    data = json.loads(repair_file.read_text(encoding="utf-8"))
    cases = data["cases"]
    source_ids = [c.get("id") for c in cases]
    errors = []

    errors.extend(validate_repair_manifest(cases, data.get("meta", {})))
    if errors:
        report = {
            "acceptance_mode": "FAKE_SIMULATION",
            "fake_simulation": True,
            "acceptance_scope": "FAKE_SIMULATION_ACCEPTANCE_ONLY",
            "production_equivalence": False,
            "note": "仅为高保真 FakeLLM/FakeJudge 链路验收，不代表生产 DeepSeek Judge 结果",
            "case_count": 0,
            "invalid_count": 0,
            "skipped_count": 0,
            "errors": errors,
            "cases": [],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    if case_id:
        allowed_case_ids = set(EXPECTED_CASE_IDS) | set(data.get("meta", {}).get("auto_case_ids", []))
        if case_id not in allowed_case_ids:
            print(json.dumps({"error": f"case_id 不在固定基础集或 auto manifest 中: {case_id}"}, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        target_case = next((c for c in cases if c.get("id") == case_id), None)
        if not target_case:
            print(json.dumps({"error": f"未在 {repair_file.name} 中找到 case_id '{case_id}'"}, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        cases = [target_case]
    else:
        missing_base = sorted(EXPECTED_CASE_IDS - set(source_ids))
        if missing_base:
            errors.append(
                f"repair_set 基础 case-ID 不完整: missing={missing_base}"
            )
        if len(source_ids) != len(set(source_ids)):
            errors.append("repair_set 存在重复 case-ID")

    skills = list(dict.fromkeys(c.get("skill") for c in cases if c.get("skill")))
    results_by_case = []

    for skill in skills:
        skill_cases = [c for c in cases if c.get("skill") == skill]
        res = evaluator.evaluate_skill(skill, cases=skill_cases, eval_set="repair_set", verbose=False)
        for cv in (res.case_verdicts or []):
            cid = cv["case_id"]
            ja = cv.get("judge_audit", {})
            reasons = []
            is_skipped = False
            is_eff_fail = False
            has_invalid = False

            shape_errors = _validate_judge_case_shape(cid, cv)
            errors.extend(shape_errors)

            for dim in REQUIRED_DIMS:
                v = cv.get(dim)
                if v not in ALLOWED_VERDICTS:
                    errors.append(f"{cid}/{dim}: 非法或缺失 verdict {v!r}")
                    has_invalid = True
                    continue
                dim_audit = ja.get(dim, {}) if isinstance(ja, dict) else {}
                codes = [str(c) for c in dim_audit.get("reason_codes", [])]
                if v == "INVALID" or v in ("TIMEOUT", "FIXTURE_ERROR"):
                    if _is_judge_infrastructure_error(codes, v, dim_audit):
                        is_skipped = True
                        reasons.append(f"{dim}:{','.join(codes) or v}")
                    elif _is_effective_failure(codes, v, dim_audit):
                        is_eff_fail = True
                        reasons.append(f"{dim}:{','.join(codes) or v}")
                    else:
                        has_invalid = True
                        reasons.append(f"{dim}:{','.join(codes) or v}")
                elif v == "B_better":
                    is_eff_fail = True
                    reasons.append(f"{dim}:B_better")

            if is_skipped:
                status = "INVALID_SKIPPED"
            elif has_invalid:
                status = "INVALID"
            elif is_eff_fail:
                status = "VALID_FAILURE"
            else:
                status = "PASS"

            results_by_case.append({
                "skill": skill,
                "case_id": cid,
                "status": status,
                "verdicts": {d: cv.get(d) for d in ("task_completion", "robustness", "readability")},
                "reasons": reasons,
            })

    result_ids = [r["case_id"] for r in results_by_case]
    invalid_count = sum(r["status"] == "INVALID" for r in results_by_case)
    skipped_count = sum(r["status"] == "INVALID_SKIPPED" for r in results_by_case)
    if case_id:
        if len(results_by_case) != 1:
            errors.append(f"单用例独立评估缺少唯一结果: case_id={case_id}, actual={len(results_by_case)}")
        if invalid_count or skipped_count:
            status = results_by_case[0]["status"] if results_by_case else "MISSING_RESULT"
            errors.append(f"单用例独立评估未通过: case_id={case_id}, status={status}, invalid={invalid_count}, skipped={skipped_count}")
    else:
        expected_result_ids = set(source_ids)
        missing_res = sorted(expected_result_ids - set(result_ids))
        extra_res = sorted(set(result_ids) - expected_result_ids)
        if missing_res:
            errors.append(
                f"评估结果缺失基础 case-ID: missing={missing_res}"
            )
        if extra_res:
            errors.append(f"评估结果存在未请求的 extra case-ID: extra={extra_res}")
        if len(result_ids) != len(expected_result_ids):
            errors.append(
                f"评估结果数量不严格匹配 repair_set: expected={len(expected_result_ids)}, actual={len(result_ids)}"
            )
        if len(result_ids) != len(set(result_ids)):
            errors.append("评估结果存在重复 case-ID")
        if invalid_count or skipped_count:
            errors.append(
                f"验收未通过：invalid={invalid_count}, skipped={skipped_count}（必须均为 0）"
            )

    report = {
        "acceptance_mode": "FAKE_SIMULATION",
        "fake_simulation": True,
        "acceptance_scope": "FAKE_SIMULATION_ACCEPTANCE_ONLY",
        "production_equivalence": False,
        "note": "仅为高保真 FakeLLM/FakeJudge 链路验收，不代表生产 DeepSeek Judge 结果",
        "eval_model": llm.model,
        "judge_model": judge_llm.model,
        "case_count": len(results_by_case),
        "invalid_count": invalid_count,
        "skipped_count": skipped_count,
        "errors": errors,
        "cases": results_by_case,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)

def main():
    import argparse
    ap = argparse.ArgumentParser(description="repair_set baseline 评估验收脚本")
    ap.add_argument("--case-id", help="仅独立评估指定的单个用例 ID")
    ap.add_argument("--repair-set", help="repair_set.json 路径")
    args = ap.parse_args()

    rpath = Path(args.repair_set) if args.repair_set else None
    run_e_acceptance(case_id=args.case_id, repair_file_path=rpath)

if __name__ == "__main__":
    main()
