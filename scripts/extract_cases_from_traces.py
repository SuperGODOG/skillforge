#!/usr/bin/env python3
"""Extract auditable badcases from sample-level evaluation traces.

The extractor is deliberately fail-closed.  A Judge failure is useful for
repair only when the shared evolver policy identifies an external-fact
failure on the baseline side and the referenced fixture response can be
independently verified from its content hash.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from skillforge.evaluator.fixtures import (  # noqa: E402
    WEATHER_SUPPORTED_CITIES,
    _weather_query_intent,
)
from skillforge.evolver import (  # noqa: E402
    _is_effective_failure,
    _is_judge_infrastructure_error,
)
from skillforge.eval_tracer import _validate_provenance  # noqa: E402


_DIMENSIONS = ("task_completion", "robustness", "readability")
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_CN_DATE_RE = re.compile(r"(?<!\d)(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
_UNSUPPORTED_WEATHER_FIELDS = re.compile(r"氧含量|含氧量|空气质量|PM\s*2\.5|湿度")


def normalize_query(text: str) -> str:
    """Normalize query text for deterministic collision detection."""
    if not text:
        return ""
    t = str(text).strip().lower()
    punct = set(" \t\n\r.,?!;:\"'()（），。？！；：“”‘’、-—_`~@#$%^&*+=|\\/<>[]{}")
    return "".join(ch for ch in t if ch not in punct)


def _query_cities(query: str) -> list[str]:
    """Return all query-mentioned supported cities in textual order."""
    matches: list[tuple[int, int, str]] = []
    for short, full in WEATHER_SUPPORTED_CITIES:
        for name in (full, short):
            start = str(query).find(name)
            if start >= 0:
                matches.append((start, -len(name), full))
    result: list[str] = []
    for _, _, city in sorted(matches):
        if city not in result:
            result.append(city)
    return result


def _intent_label(query: str) -> str:
    """Compact semantic bucket used by extraction statistics."""
    text = str(query or "")
    if any(token in text for token in ("天气", "气温", "温度", "几度", "降水", "下雨", "有雨", "风", "穿衣", "出海")):
        intent = _weather_query_intent(text)
        if intent.get("historical"):
            return "weather.historical"
        if "温差" in text:
            return "weather.temperature_difference"
        if "降水概率" in text or "降水情况" in text:
            return "weather.precipitation"
        if "穿衣" in text:
            return "weather.clothing"
        if "出海" in text or "风" in text:
            return "weather.wind"
        return "weather.forecast"
    if "正则" in text or "regex" in text.lower() or "匹配" in text:
        return "regex.explanation"
    if "周报" in text:
        return "weekly_report.drafting"
    return "other"


def _trace_distribution(trace: dict[str, Any]) -> tuple[str, str, list[str]]:
    skill = str(trace.get("skill", "") or "")
    query = str(trace.get("query", "") or "")
    return skill, _intent_label(query), _query_cities(query)


def check_whitelist_semantics(trace: dict[str, Any]) -> tuple[bool, str]:
    """Accept only the shared, side-aware effective-failure policy.

    Reference ambiguity is not a baseline fabrication.  In particular, a
    top-level ``presented_side`` or sentinel label cannot substitute for the
    per-dimension ``presented_order`` consumed by ``_is_effective_failure``.
    """
    if not isinstance(trace, dict):
        return False, "MALFORMED_TRACE"
    judge_verdict = trace.get("judge_verdict")
    if not isinstance(judge_verdict, dict):
        return False, "MALFORMED_TRACE: judge_verdict"
    if trace.get("terminal"):
        return False, "INFRASTRUCTURE_ERROR: TRACE_TERMINAL"
    top_codes_raw = judge_verdict.get("reason_codes")
    if (
        not isinstance(top_codes_raw, list)
        or not top_codes_raw
        or any(not isinstance(code, str) or not code.strip() for code in top_codes_raw)
    ):
        return False, "MALFORMED_TRACE: top reason_codes"
    top_codes = [str(c) for c in top_codes_raw]
    vague_top = next(
        (code for code in top_codes if code.upper() in {"INSUFFICIENT_EVIDENCE", "EVIDENCE_INSUFFICIENT"}),
        None,
    )
    if vague_top:
        return False, f"VAGUE_REFERENCE: {vague_top}"
    if judge_verdict.get("verdict") != "VALID_FAILURE":
        if judge_verdict.get("verdict") == "INVALID_SKIPPED":
            return False, "INFRASTRUCTURE_ERROR: INVALID_SKIPPED"
        if judge_verdict.get("verdict") == "INVALID":
            return False, "REFERENCE_OR_JUDGE_INVALID"
        return False, "NOT_IN_EFFECTIVE_FAILURE_WHITELIST"

    if trace.get("evaluand_type") != "baseline":
        return False, "NON_BASELINE_EVALUAND"
    if trace.get("evaluand_side") != "baseline":
        return False, "EVALUAND_SIDE_MISMATCH"

    task_audit = judge_verdict.get("task_completion")
    if not isinstance(task_audit, dict):
        return False, "MALFORMED_TRACE: task_completion audit"
    task_order = task_audit.get("presented_order")
    if (
        not isinstance(task_order, dict)
        or task_order.get("A") not in {"skill", "baseline"}
        or task_order.get("B") not in {"skill", "baseline"}
        or {task_order.get("A"), task_order.get("B")} != {"skill", "baseline"}
    ):
        return False, "PRESENTED_ORDER_MISMATCH: task_completion"
    baseline_side = next(side for side in ("A", "B") if task_order[side] == "baseline")
    presented_side = trace.get("presented_side")
    if trace.get("baseline_presented_side") != baseline_side or presented_side != baseline_side:
        return False, "PRESENTED_SIDE_BINDING_MISMATCH"
    if "evaluand_answer" not in trace:
        return False, "MISSING_EVALUAND_ANSWER"
    evaluand_answer = trace.get("evaluand_answer", "")
    if not isinstance(evaluand_answer, str) or not evaluand_answer.strip():
        return False, "MISSING_EVALUAND_ANSWER"
    if "evaluand_answer" in trace and trace.get("answer") != evaluand_answer:
        return False, "EVALUAND_ANSWER_BINDING_MISMATCH"
    if trace.get("baseline_answer") != evaluand_answer:
        return False, "BASELINE_ANSWER_BINDING_MISMATCH"
    if "skill_answer" not in trace or not isinstance(trace.get("skill_answer"), str):
        return False, "MISSING_SKILL_ANSWER"

    effective = False
    for dim in _DIMENSIONS:
        dim_data = judge_verdict.get(dim)
        if not isinstance(dim_data, dict):
            return False, f"MALFORMED_TRACE: missing {dim} audit"
        value = dim_data.get("verdict")
        if value not in {"A_better", "tied", "B_better", "INVALID"}:
            return False, f"MALFORMED_TRACE: invalid {dim} verdict"
        dim_codes_raw = dim_data.get("reason_codes")
        if (
            not isinstance(dim_codes_raw, list)
            or not dim_codes_raw
            or any(not isinstance(code, str) or not code.strip() for code in dim_codes_raw)
        ):
            return False, f"MALFORMED_TRACE: {dim} reason_codes"
        if not isinstance(dim_data.get("evidence_summary"), str) or not dim_data.get("evidence_summary", "").strip():
            return False, f"MALFORMED_TRACE: {dim} evidence_summary"
        codes = [str(c) for c in dim_codes_raw]
        if any(code.upper() in {"INSUFFICIENT_EVIDENCE", "EVIDENCE_INSUFFICIENT"} for code in codes):
            return False, f"VAGUE_REFERENCE: {next(code for code in codes if code.upper() in {'INSUFFICIENT_EVIDENCE', 'EVIDENCE_INSUFFICIENT'})}"
        if _is_judge_infrastructure_error(codes, value, dim_data):
            return False, f"INFRASTRUCTURE_ERROR: {next((code for code in codes if _is_judge_infrastructure_error([code], value, dim_data)), value)}"
        order = dim_data.get("presented_order")
        if (
            not isinstance(order, dict)
            or order.get("A") not in {"skill", "baseline"}
            or order.get("B") not in {"skill", "baseline"}
            or {order.get("A"), order.get("B")} != {"skill", "baseline"}
        ):
            return False, f"PRESENTED_ORDER_MISMATCH: {dim}"
        # This is the sole semantic whitelist.  Do not duplicate or broaden it.
        if _is_effective_failure(codes, value, dim_data):
            effective = True

    if not effective:
        # Keep the reason stable for existing operational counters.
        if any(str(c).upper().startswith("UNVERIFIED_EXTERNAL_FACT") for c in top_codes):
            return False, "NOT_IN_EFFECTIVE_FAILURE_WHITELIST"
        return False, "NOT_IN_EFFECTIVE_FAILURE_WHITELIST"
    return True, "VALID_EXTERNAL_FACT_CONFLICT"


def _parse_snapshot_content(content: Any) -> Optional[dict[str, Any]]:
    if isinstance(content, dict):
        data = content
    elif isinstance(content, str) and content.strip():
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return data if isinstance(data, dict) else None


def _verified_snapshot(tr: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return parsed content only when ``sha256(canonical(content)) == id``."""
    sid = str(tr.get("snapshot_id", "")).strip()
    data = _parse_snapshot_content(tr.get("content"))
    if not sid or data is None:
        return None
    try:
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", sid) or not hmac.compare_digest(sid, expected):
        return None
    return data


def _tool_response_is_successful(response: dict[str, Any]) -> bool:
    """Require the recorded tool invocation itself to be an accepted success.

    A correct content hash is not enough: an ERROR/CIRCUIT_OPEN response can
    still contain JSON, and must never become repair evidence.
    """
    if response.get("output_status") != "SUCCESS":
        return False
    raw = {
        "authenticity_pass": response.get("authenticity_pass"),
        "call_count": response.get("call_count"),
        "call_index": response.get("call_index"),
        "fixture_case_id": response.get("fixture_case_id"),
        "input_params": response.get("parameters"),
        "is_fixture": response.get("is_fixture"),
        "latency_ms": response.get("latency_ms"),
        "output_status": response.get("output_status"),
        "output_summary": response.get("output_summary"),
        "timestamp": response.get("timestamp"),
        "tool_called": response.get("tool_called"),
        "tool_name": response.get("tool_name"),
        "tool_required": response.get("tool_required"),
        "tool_success": response.get("tool_success"),
        "snapshot_id": response.get("snapshot_id"),
        "snapshot_content": response.get("content"),
        "signature": response.get("signature"),
    }
    return bool(_validate_provenance(raw).get("pass"))


def _trace_anchor_date(trace: dict[str, Any], snapshots: list[dict[str, Any]]) -> date:
    raw = str(trace.get("ts", ""))
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            # Relative words such as “明天” are evaluated in the execution
            # timezone, whereas trace timestamps are commonly stored in UTC.
            return parsed.astimezone().date()
        return parsed.date()
    except ValueError:
        available: list[date] = []
        for data in snapshots:
            for forecast in data.get("forecasts", []) if isinstance(data.get("forecasts"), list) else []:
                for cast in forecast.get("casts", []) if isinstance(forecast, dict) and isinstance(forecast.get("casts"), list) else []:
                    try:
                        available.append(date.fromisoformat(str(cast.get("date", ""))))
                    except ValueError:
                        pass
        return min(available, default=date.today())


def _query_explicit_dates(query: str, anchor: date) -> list[date]:
    values: list[date] = []
    for match in _ISO_DATE_RE.finditer(query):
        try:
            values.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            return []
    for match in _CN_DATE_RE.finditer(query):
        try:
            values.append(date(int(match.group(1) or anchor.year), int(match.group(2)), int(match.group(3))))
        except ValueError:
            return []
    return list(dict.fromkeys(values))


def _requested_dates(query: str, intent: dict[str, Any], anchor: date, available: set[date]) -> list[date]:
    explicit = _query_explicit_dates(query, anchor)
    if explicit:
        return explicit
    if intent.get("historical"):
        return []
    if "周末" in query:
        weekend = sorted(d for d in available if d >= anchor and d.weekday() >= 5)
        if weekend:
            return weekend
    return [anchor + timedelta(days=int(offset)) for offset in intent.get("offsets", (0,))]


def _weather_reference(
    trace: dict[str, Any],
    snapshots: list[dict[str, Any]],
) -> tuple[bool, str, str]:
    query = str(trace.get("query", "") or "")
    cities = _query_cities(query)
    if not cities:
        return False, "", "QUERY_CITY_UNRESOLVED"
    intent = _weather_query_intent(query)
    if intent.get("historical"):
        return False, "", "UNSUPPORTED_HISTORICAL_QUERY"

    by_city: dict[str, dict[date, dict[str, Any]]] = {}
    for data in snapshots:
        forecasts = data.get("forecasts")
        if not isinstance(forecasts, list):
            continue
        for forecast in forecasts:
            if not isinstance(forecast, dict):
                continue
            raw_city = str(forecast.get("city", ""))
            city = raw_city if raw_city.endswith("市") else f"{raw_city}市"
            casts = forecast.get("casts")
            if not isinstance(casts, list):
                continue
            bucket = by_city.setdefault(city, {})
            for cast in casts:
                if not isinstance(cast, dict):
                    continue
                try:
                    bucket[date.fromisoformat(str(cast.get("date", "")))] = cast
                except ValueError:
                    continue

    if any(city not in by_city for city in cities):
        missing = [city for city in cities if city not in by_city]
        return False, "", f"CITY_NOT_IN_SNAPSHOT: {','.join(missing)}"

    anchor = _trace_anchor_date(trace, snapshots)
    available = {dt for city in cities for dt in by_city[city]}
    dates = _requested_dates(query, intent, anchor, available)
    if not dates:
        return False, "", "QUERY_DATE_UNRESOLVED"
    required_fields = tuple(intent.get("required_fields", ()))
    chunks: list[str] = []
    for city in cities:
        city_casts = by_city[city]
        for wanted in dates:
            cast = city_casts.get(wanted)
            if cast is None:
                return False, "", f"DATE_NOT_IN_SNAPSHOT: {city}:{wanted.isoformat()}"
            missing_fields = [field for field in required_fields if cast.get(field) in (None, "")]
            if missing_fields:
                return False, "", f"FACT_NOT_IN_SNAPSHOT: {city}:{wanted.isoformat()}:{','.join(missing_fields)}"
            facts: list[str] = []
            if "dayweather" in required_fields or "nightweather" in required_fields:
                facts.append(f"天气={cast.get('dayweather', '')}/{cast.get('nightweather', '')}")
            if "daytemp" in required_fields or "nighttemp" in required_fields:
                facts.append(f"温度={cast.get('daytemp', '')}~{cast.get('nighttemp', '')}℃")
            if "daywind" in required_fields or "daypower" in required_fields:
                facts.append(f"风力={cast.get('daywind', '')}{cast.get('daypower', '')}级")
            if "precipitation_probability" in required_fields:
                facts.append(f"降水概率={cast.get('precipitation_probability')}%")
            chunks.append(f"{city} {wanted.isoformat()} " + "，".join(facts))

    ref = (
        f"识别城市={','.join(cities)}；查询日期={','.join(d.isoformat() for d in dates)}；"
        f"调用天气工具；快照事实=" + "; ".join(chunks) + "；"
        "据快照如实回复不编造"
    )
    if _UNSUPPORTED_WEATHER_FIELDS.search(query):
        ref += "；查询所需字段不在天气快照中，必须明确说明不可提供"
    return True, ref, "OK"


def check_and_build_reference(trace: dict[str, Any]) -> tuple[bool, str, str]:
    """Build a query-aligned reference from hash-verified snapshot content."""
    tool_responses = trace.get("tool_responses")
    if not isinstance(tool_responses, list) or not tool_responses:
        return False, "", "NO_SNAPSHOT_SUPPORT"

    valid_snapshots: list[dict[str, Any]] = []
    invalid_count = 0
    for response in tool_responses:
        if not isinstance(response, dict):
            invalid_count += 1
            continue
        if not str(response.get("tool_name", "")).strip():
            invalid_count += 1
            continue
        if not _tool_response_is_successful(response):
            invalid_count += 1
            continue
        content = _verified_snapshot(response)
        if content is None:
            invalid_count += 1
            continue
        valid_snapshots.append(content)
    if not valid_snapshots:
        return False, "", "SNAPSHOT_HASH_UNVERIFIED"
    if invalid_count:
        return False, "", "SNAPSHOT_HASH_UNVERIFIED"

    is_weather = str(trace.get("skill", "")) == "weather_query" or any(
        isinstance(response, dict) and response.get("tool_name") == "amap_weather_api"
        for response in tool_responses
    )
    if is_weather:
        return _weather_reference(trace, valid_snapshots)

    # There is no generic way to prove that arbitrary JSON fields answer a
    # query.  Refuse non-weather domains instead of turning a hash-consistent
    # but semantically unrelated object into a reference.
    return False, "", "UNSUPPORTED_REFERENCE_DOMAIN"


def generate_case_id(skill: str, existing_ids: set[str]) -> str:
    prefix_map = {
        "weather_query": "wq",
        "explain_regex": "er",
        "write_weekly_report": "wr",
    }
    prefix = prefix_map.get(skill, skill[:2].lower())
    index = 1
    while True:
        case_id = f"{prefix}_auto_{index:02d}"
        if case_id not in existing_ids:
            existing_ids.add(case_id)
            return case_id
        index += 1


def extract_cases_from_traces(
    traces_dir: Path | str = ROOT / "runs" / "eval_traces",
    repair_set_path: Path | str = ROOT / "evaluation_sets" / "repair_set.json",
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Scan traces, apply quality gates, and optionally append accepted cases."""
    traces_path = Path(traces_dir)
    repair_file = Path(repair_set_path)
    if not repair_file.exists():
        raise FileNotFoundError(f"repair_set 文件不存在: {repair_file}")

    repair_data = json.loads(repair_file.read_text(encoding="utf-8"))
    existing_cases = repair_data.get("cases", [])
    existing_ids = {c.get("id") for c in existing_cases if c.get("id")}
    existing_norm_queries = {normalize_query(c.get("query", "")) for c in existing_cases}
    seen_pairs = {
        (normalize_query(c.get("query", "")), normalize_query(c.get("reference", "")))
        for c in existing_cases
    }

    trace_files = sorted(traces_path.glob("*.jsonl")) if traces_path.exists() else []
    all_traces: list[dict[str, Any]] = []
    for trace_file in trace_files:
        try:
            for line in trace_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        if verbose:
                            print(f"  ⚠️ 轨迹行 JSON 无法解析: {trace_file.name}")
                        continue
                    all_traces.append(value)
        except Exception as exc:
            if verbose:
                print(f"  ⚠️ 读取轨迹文件异常 {trace_file.name}: {exc}")

    skill_counts: Counter[str] = Counter()
    intent_counts: Counter[str] = Counter()
    city_counts: Counter[str] = Counter()
    for trace in all_traces:
        if isinstance(trace, dict):
            skill, intent, cities = _trace_distribution(trace)
            if skill:
                skill_counts[skill] += 1
            if intent:
                intent_counts[intent] += 1
            city_counts.update(cities)

    scanned_count = len(all_traces)
    candidate_count = 0
    accepted_cases: list[dict[str, Any]] = []
    reject_reasons: list[str] = []
    accepted_skill_counts: Counter[str] = Counter()
    accepted_intent_counts: Counter[str] = Counter()
    accepted_city_counts: Counter[str] = Counter()

    for trace in all_traces:
        ok_whitelist, reason_whitelist = check_whitelist_semantics(trace)
        if not ok_whitelist:
            reject_reasons.append(reason_whitelist)
            continue
        candidate_count += 1

        ok_reference, reference, reason_reference = check_and_build_reference(trace)
        if not ok_reference:
            reject_reasons.append(reason_reference)
            continue

        query = str(trace.get("query", "") or "")
        norm_query = normalize_query(query)
        norm_reference = normalize_query(reference)
        if not norm_query:
            reject_reasons.append("EMPTY_QUERY")
            continue
        if norm_query in existing_norm_queries:
            reject_reasons.append("DUPLICATE_QUERY")
            continue
        if (norm_query, norm_reference) in seen_pairs:
            reject_reasons.append("DUPLICATE_PAIR")
            continue

        skill = str(trace.get("skill", "") or "")
        case_id = generate_case_id(skill, existing_ids)
        trace_id = trace.get("trace_id") or f"{trace.get('run_id')}:{trace.get('case_id')}:{trace.get('evaluand_type')}"
        new_case = {
            "id": case_id,
            "skill": skill,
            "query": query,
            "reference": reference,
            "trace_id": trace_id,
        }
        existing_norm_queries.add(norm_query)
        seen_pairs.add((norm_query, norm_reference))
        accepted_cases.append(new_case)
        intent = _intent_label(query)
        accepted_skill_counts[skill] += 1
        accepted_intent_counts[intent] += 1
        accepted_city_counts.update(_query_cities(query))

    if accepted_cases and not dry_run:
        repair_data.setdefault("cases", []).extend(accepted_cases)
        meta = repair_data.setdefault("meta", {})
        meta["total"] = len(repair_data["cases"])
        meta["auto_case_ids"] = sorted(
            c["id"] for c in repair_data["cases"] if "_auto_" in str(c.get("id", ""))
        )
        meta["auto_case_count"] = len(meta["auto_case_ids"])
        repair_file.write_text(
            json.dumps(repair_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if verbose:
            print(f"  ✅ 成功追加 {len(accepted_cases)} 个用例至 {repair_file.name} (总用例数: {len(repair_data['cases'])})")

    return {
        "scanned": scanned_count,
        "candidates": candidate_count,
        "accepted": len(accepted_cases),
        "rejected": len(reject_reasons),
        "reasons_distribution": dict(Counter(reject_reasons)),
        "skill_distribution": dict(skill_counts),
        "intent_distribution": dict(intent_counts),
        "city_distribution": dict(city_counts),
        "accepted_skill_distribution": dict(accepted_skill_counts),
        "accepted_intent_distribution": dict(accepted_intent_counts),
        "accepted_city_distribution": dict(accepted_city_counts),
        "distribution": {
            "skill": dict(skill_counts),
            "intent": dict(intent_counts),
            "city": dict(city_counts),
        },
        "new_cases": accepted_cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P2-C 评估轨迹提取器")
    parser.add_argument("--traces-dir", default=str(ROOT / "runs" / "eval_traces"), help="轨迹目录")
    parser.add_argument("--repair-set", default=str(ROOT / "evaluation_sets" / "repair_set.json"), help="repair_set 路径")
    parser.add_argument("--dry-run", action="store_true", help="不写磁盘，只输出统计")
    parser.add_argument("--verbose", action="store_true", default=True, help="详细输出")
    args = parser.parse_args()
    result = extract_cases_from_traces(
        traces_dir=args.traces_dir,
        repair_set_path=args.repair_set,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
