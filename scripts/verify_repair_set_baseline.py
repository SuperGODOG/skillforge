#!/usr/bin/env python3
"""E 项验收：repair_set 22 cases × 3 skill baseline 评估状态清单验证脚本。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

class HighFidelityEvalLLM:
    """高保真 LLM，为 22 cases 提供贴近真实的 baseline 和 skill 输出。"""
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


def run_e_acceptance():
    registry = SkillRegistry(db_path=ROOT / "runs" / "test_acceptance.db", skills_dir=ROOT / "skills", repo_root=ROOT)
    registry.load_skills_from_dir()

    llm = HighFidelityEvalLLM()
    judge_llm = HighFidelityJudgeLLM()
    evaluator = SkillEvaluator(registry=registry, llm=llm, judge_llm=judge_llm)

    repair_file = ROOT / "evaluation_sets" / "repair_set.json"
    data = json.loads(repair_file.read_text(encoding="utf-8"))
    cases = data["cases"]
    source_ids = [c.get("id") for c in cases]
    errors = []
    if len(cases) != len(EXPECTED_CASE_IDS) or set(source_ids) != EXPECTED_CASE_IDS:
        errors.append(
            f"repair_set case-ID 不完整: expected={len(EXPECTED_CASE_IDS)}, "
            f"actual={len(cases)}, missing={sorted(EXPECTED_CASE_IDS - set(source_ids))}, "
            f"extra={sorted(set(source_ids) - EXPECTED_CASE_IDS)}"
        )
    if len(source_ids) != len(set(source_ids)):
        errors.append("repair_set 存在重复 case-ID")

    skills = ["weather_query", "explain_regex", "write_weekly_report"]
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
    if len(results_by_case) != len(EXPECTED_CASE_IDS) or set(result_ids) != EXPECTED_CASE_IDS:
        errors.append(
            f"评估结果 case-ID 不完整: expected={len(EXPECTED_CASE_IDS)}, "
            f"actual={len(results_by_case)}, missing={sorted(EXPECTED_CASE_IDS - set(result_ids))}, "
            f"extra={sorted(set(result_ids) - EXPECTED_CASE_IDS)}"
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

if __name__ == "__main__":
    run_e_acceptance()
