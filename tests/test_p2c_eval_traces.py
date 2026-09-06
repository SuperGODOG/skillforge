"""Unit tests for P2-C evaluation traces and badcase extractor.

Covers:
1. Trace disk persistence with all 11 required fields and INVALID_SKIPPED annotation
2. Quality Gate 3: Conflict sample extraction vs Non-conflict rejection
3. Quality Gate 3: Judge infrastructure error & vague reference rejection
4. Quality Gate 1: Duplicate query rejection with normalization
5. Quality Gate 2: Verifiability rejection on missing snapshot
6. End-to-end extraction into repair_set.json schema compatibility
"""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import pytest

from skillforge.eval_tracer import record_eval_traces_from_eval_result, append_eval_trace
from skillforge.models import EvalResult, ToolCallProvenance
from skillforge.evaluator.judge import _provenance_signature
from scripts.extract_cases_from_traces import (
    extract_cases_from_traces,
    normalize_query,
    check_whitelist_semantics,
    check_and_build_reference,
)


def _snapshot_id(content: str) -> str:
    data = json.loads(content)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tool_response(content: str, *, tool_name: str = "amap_weather_api") -> dict:
    """Build the complete trace-side provenance projection used by the extractor."""
    snapshot_id = _snapshot_id(content)
    prov = ToolCallProvenance(
        tool_name=tool_name,
        fixture_case_id="amap_weather_api.smoke.v1",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"city": "深圳市", "extensions": "all"},
        output_status="SUCCESS",
        output_summary=f"[snapshot:{snapshot_id}] verified",
        latency_ms=1.0,
        timestamp="2026-09-06T10:00:00Z",
        signature="",
        snapshot_id=snapshot_id,
        snapshot_content=content,
    )
    prov = replace(prov, signature=_provenance_signature(prov))
    projected = vars(prov).copy()
    projected["content"] = projected.pop("snapshot_content")
    projected["parameters"] = projected.pop("input_params")
    return projected


def test_normalize_query():
    assert normalize_query("北京 今天 天气？") == "北京今天天气"
    assert normalize_query("  What's the weather in Beijing, today?  ") == "whatstheweatherinbeijingtoday"
    assert normalize_query("杭州今天有雨吗？！，。") == "杭州今天有雨吗"


def test_trace_recording_and_fields(tmp_path):
    trace_file = tmp_path / "test_trace.jsonl"

    snapshot_content = json.dumps({"city": "北京", "temp": "25"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_id = _snapshot_id(snapshot_content)
    prov = ToolCallProvenance(
        tool_name="amap_weather_api",
        fixture_case_id="wq_d01",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"city": "北京"},
        output_status="SUCCESS",
        output_summary=f"[snapshot:{snapshot_id}] tool=sunny | agent=sunny",
        latency_ms=12.5,
        timestamp="2026-09-06T10:00:00Z",
        signature="",
        snapshot_id=snapshot_id,
        snapshot_content=snapshot_content,
    )
    prov = replace(prov, signature=_provenance_signature(prov))

    eval_result = EvalResult(
        release_id="rel_01",
        structure_score={"static": 40.0},
        effect_score={"task": 25.0, "robust": 15.0, "readability": 10.0, "efficiency": 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[
            {
                "case_id": "wq_d01",
                "query": "北京今天天气",
                "task_completion": "A_better",
                "robustness": "A_better",
                "readability": "A_better",
                "judge_audit": {
                    "task_completion": {
                        "presented_order": {"A": "skill", "B": "baseline"},
                        "reason_codes": ["EVIDENCE_SUFFICIENT"],
                        "evidence_summary": "A 表现良好",
                    },
                    "robustness": {
                        "presented_order": {"A": "skill", "B": "baseline"},
                        "reason_codes": ["EVIDENCE_SUFFICIENT"],
                        "evidence_summary": "A 表现良好",
                    },
                    "readability": {
                        "presented_order": {"A": "skill", "B": "baseline"},
                        "reason_codes": ["EVIDENCE_SUFFICIENT"],
                        "evidence_summary": "A 表现良好",
                    }
                },
            },
            {
                "case_id": "wq_d99",
                "query": "异常用例",
                "task_completion": "INVALID",
                "robustness": "INVALID",
                "readability": "INVALID",
                "judge_audit": {
                    "task_completion": {
                        "presented_order": {"A": "skill", "B": "baseline"},
                        "reason_codes": ["MALFORMED_JUDGE_RESPONSE"],
                    }
                },
            },
        ],
        case_outputs=[
            {
                "case_id": "wq_d01",
                "query": "北京今天天气",
                "output_skill": "北京今天天气晴朗，气温25度。",
                "output_baseline": "今天天气还可以。",
                "provenances": [vars(prov)],
            },
            {
                "case_id": "wq_d99",
                "query": "异常用例",
                "output_skill": "回答",
                "output_baseline": "回答",
                "provenances": [],
            },
        ],
    )

    records = record_eval_traces_from_eval_result(
        trace_file=trace_file,
        run_id="run_test_01",
        skill="weather_query",
        eval_result=eval_result,
        evaluand_type="baseline",
        skipped_cases=[{"case_id": "wq_d99", "reasons": ["task:MALFORMED"]}],
        effective_failed_cases=set(),
    )

    assert len(records) == 2
    assert trace_file.exists()

    lines = [json.loads(l) for l in trace_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2

    r1 = lines[0]
    # Check all 11 required fields + trace_id
    required_fields = [
        "ts", "run_id", "trace_id", "skill", "case_id", "query",
        "evaluand_type", "answer", "tool_responses", "provenance_check",
        "judge_verdict", "presented_side",
    ]
    for field in required_fields:
        assert field in r1, f"Missing required field: {field}"

    assert r1["case_id"] == "wq_d01"
    assert r1["evaluand_type"] == "baseline"
    assert r1["presented_side"] == "B"
    assert r1["baseline_presented_side"] == "B"
    assert r1["evaluand_answer"] == "今天天气还可以。"
    assert r1["answer"] == r1["evaluand_answer"]
    assert r1["tool_responses"][0]["snapshot_id"] == snapshot_id
    assert r1["provenance_check"]["status"] == "pass"
    assert r1["judge_verdict"]["verdict"] == "PASS"

    # Check skipped case annotation
    r2 = lines[1]
    assert r2["case_id"] == "wq_d99"
    assert r2["judge_verdict"]["verdict"] == "INVALID_SKIPPED"
    for dimension in ("task_completion", "robustness", "readability"):
        assert r2["judge_verdict"][dimension]["verdict"] == "INVALID_SKIPPED"


def test_quality_gate_conflict_sample_vs_non_conflict(tmp_path):
    """四型验证：冲突样本提取成功 vs 无冲突样本被白名单拒绝。"""
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(json.dumps({
        "meta": {"total": 1},
        "cases": [{"id": "wq_d01", "skill": "weather_query", "query": "北京今天天气", "reference": "ref"}]
    }), encoding="utf-8")

    traces_dir = tmp_path / "eval_traces"
    traces_dir.mkdir()

    # 1. 冲突样本：回答声称外部事实，且 Judge truth sentinel 判定 UNVERIFIED_EXTERNAL_FACT_A
    # 具备合法工具响应快照
    valid_snapshot_content = json.dumps({
        "forecasts": [{
            "city": "深圳",
            "casts": [{
                "date": "2026-09-07",
                "dayweather": "雷阵雨",
                "nightweather": "阴",
                "daytemp": "31",
                "nighttemp": "26",
                "daywind": "南",
                "daypower": "3",
                "precipitation_probability": 80,
            }]
        }]
    })

    conflict_trace = {
        "ts": "2026-09-06T10:05:00Z",
        "run_id": "run_test_conflict",
        "trace_id": "run_test_conflict:wq_c01:baseline",
        "skill": "weather_query",
        "case_id": "wq_c01",
        "query": "深圳明天会下雨吗",
        "evaluand_type": "baseline",
        "evaluand_side": "baseline",
        "evaluand_answer": "深圳明天晴天，气温35度，完全不下雨。",
        "answer": "深圳明天晴天，气温35度，完全不下雨。",
        "baseline_answer": "深圳明天晴天，气温35度，完全不下雨。",
        "skill_answer": "深圳明天的天气请查快照。",
        "tool_responses": [_tool_response(valid_snapshot_content)],
        "provenance_check": {"status": "fail", "pass": False, "details": []},
        "judge_verdict": {
            "verdict": "VALID_FAILURE",
            "task_completion": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "robustness": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "readability": {"verdict": "tied", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["EVIDENCE_SUFFICIENT"], "evidence_summary": "两侧表达清晰度相当"},
            "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"],
        },
        "presented_side": "A",
        "baseline_presented_side": "A",
        "skill_presented_side": "B",
    }

    # 2. 无冲突样本：正常 PASS，无任何外部事实失真
    normal_pass_trace = {
        "ts": "2026-09-06T10:06:00Z",
        "run_id": "run_test_pass",
        "trace_id": "run_test_pass:wq_p01:baseline",
        "skill": "weather_query",
        "case_id": "wq_p01",
        "query": "广州今天天气怎么样",
        "evaluand_type": "baseline",
        "evaluand_side": "baseline",
        "evaluand_answer": "广州今天晴天28度。",
        "answer": "广州今天晴天28度。",
        "baseline_answer": "广州今天晴天28度。",
        "skill_answer": "广州今天的天气请查快照。",
        "tool_responses": [_tool_response(valid_snapshot_content)],
        "provenance_check": {"status": "pass", "pass": True, "details": []},
        "judge_verdict": {
            "verdict": "PASS",
            "task_completion": {"verdict": "A_better", "reason_codes": ["EVIDENCE_SUFFICIENT"]},
            "reason_codes": ["EVIDENCE_SUFFICIENT"],
        },
        "presented_side": "A",
        "baseline_presented_side": "A",
        "skill_presented_side": "B",
    }

    trace_file = traces_dir / "trace_samples.jsonl"
    trace_file.write_text(
        json.dumps(conflict_trace, ensure_ascii=False) + "\n" +
        json.dumps(normal_pass_trace, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    summary = extract_cases_from_traces(
        traces_dir=traces_dir,
        repair_set_path=repair_file,
        dry_run=False,
        verbose=False,
    )

    assert summary["scanned"] == 2
    assert summary["candidates"] == 1
    assert summary["accepted"] == 1
    assert summary["rejected"] == 1
    assert "NOT_IN_EFFECTIVE_FAILURE_WHITELIST" in summary["reasons_distribution"]

    new_case = summary["new_cases"][0]
    assert new_case["query"] == "深圳明天会下雨吗"
    assert new_case["skill"] == "weather_query"
    assert "深圳" in new_case["reference"]
    assert "雷阵雨" in new_case["reference"]
    assert new_case["trace_id"] == "run_test_conflict:wq_c01:baseline"

    # Verify repair_set was updated
    updated_data = json.loads(repair_file.read_text(encoding="utf-8"))
    assert updated_data["meta"]["total"] == 2
    assert len(updated_data["cases"]) == 2


def test_quality_gate_judge_infrastructure_and_vague_rejected(tmp_path):
    """四型验证：Judge 拒判样本（MALFORMED / INSUFFICIENT_EVIDENCE）一律被拒绝。"""
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(json.dumps({
        "meta": {"total": 0},
        "cases": []
    }), encoding="utf-8")

    traces_dir = tmp_path / "eval_traces"
    traces_dir.mkdir()

    # MALFORMED 样本
    malformed_trace = {
        "ts": "2026-09-06T10:00:00Z",
        "run_id": "run_m",
        "trace_id": "run_m:c1:baseline",
        "skill": "weather_query",
        "case_id": "c1",
        "query": "武汉后天天气",
        "answer": "武汉后天晴朗",
        "tool_responses": [{"snapshot_id": "s1", "content": json.dumps({"city": "武汉"})}],
        "provenance_check": {"status": "fail"},
        "judge_verdict": {
            "verdict": "INVALID_SKIPPED",
            "reason_codes": ["MALFORMED_JUDGE_RESPONSE"],
        },
        "presented_side": "A",
    }

    # INSUFFICIENT_EVIDENCE 样本（参考模糊拒判）
    vague_trace = {
        "ts": "2026-09-06T10:01:00Z",
        "run_id": "run_v",
        "trace_id": "run_v:c2:baseline",
        "skill": "weather_query",
        "case_id": "c2",
        "query": "长沙今天天气",
        "answer": "长沙今天多云",
        "tool_responses": [{"snapshot_id": "s2", "content": json.dumps({"city": "长沙"})}],
        "provenance_check": {"status": "fail"},
        "judge_verdict": {
            "verdict": "INVALID",
            "reason_codes": ["INSUFFICIENT_EVIDENCE"],
        },
        "presented_side": "A",
    }

    trace_file = traces_dir / "infra_samples.jsonl"
    trace_file.write_text(
        json.dumps(malformed_trace, ensure_ascii=False) + "\n" +
        json.dumps(vague_trace, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    summary = extract_cases_from_traces(
        traces_dir=traces_dir,
        repair_set_path=repair_file,
        dry_run=False,
        verbose=False,
    )

    assert summary["accepted"] == 0
    assert summary["candidates"] == 0
    assert summary["rejected"] == 2
    assert "INFRASTRUCTURE_ERROR: INVALID_SKIPPED" in summary["reasons_distribution"]
    assert "VAGUE_REFERENCE: INSUFFICIENT_EVIDENCE" in summary["reasons_distribution"]


def test_quality_gate_duplicate_query_rejected(tmp_path):
    """四型验证：重复样本（与 repair_set 归一化碰撞）被质量门 ① 拒绝。"""
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(json.dumps({
        "meta": {"total": 1},
        "cases": [{
            "id": "wq_d01",
            "skill": "weather_query",
                "query": "深圳明天会下雨吗",
            "reference": "ref",
        }]
    }), encoding="utf-8")

    traces_dir = tmp_path / "eval_traces"
    traces_dir.mkdir()

    duplicate_content = _weather_content(["深圳市"], ["2026-09-07"])

    # Query 归一化后与 "北京今天天气" 相同
    dup_trace = {
        "ts": "2026-09-06T10:00:00Z",
        "run_id": "run_dup",
        "trace_id": "run_dup:c1:baseline",
        "skill": "weather_query",
        "case_id": "c1",
        "query": "  深圳 明天 会下雨吗？？ ",  # Normalized to 深圳明天会下雨吗
        "evaluand_type": "baseline",
        "evaluand_side": "baseline",
        "evaluand_answer": "深圳明天晴天，气温30度",
        "answer": "深圳明天晴天，气温30度",
        "baseline_answer": "深圳明天晴天，气温30度",
        "skill_answer": "深圳明天的天气请查快照。",
        "tool_responses": [_tool_response(duplicate_content)],
        "provenance_check": {"status": "fail"},
        "presented_side": "A",
        "baseline_presented_side": "A",
        "skill_presented_side": "B",
        "judge_verdict": {
            "verdict": "VALID_FAILURE",
            "task_completion": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "robustness": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "readability": {"verdict": "tied", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["EVIDENCE_SUFFICIENT"], "evidence_summary": "两侧表达清晰度相当"},
            "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"],
        },
    }

    trace_file = traces_dir / "dup_sample.jsonl"
    trace_file.write_text(json.dumps(dup_trace, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = extract_cases_from_traces(
        traces_dir=traces_dir,
        repair_set_path=repair_file,
        dry_run=False,
        verbose=False,
    )

    assert summary["accepted"] == 0
    assert summary["rejected"] == 1
    assert "DUPLICATE_QUERY" in summary["reasons_distribution"]


def test_quality_gate_no_snapshot_support_rejected(tmp_path):
    """可验证性验证：无工具快照支撑的样本被质量门 ② 拒绝。"""
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(json.dumps({
        "meta": {"total": 0},
        "cases": []
    }), encoding="utf-8")

    traces_dir = tmp_path / "eval_traces"
    traces_dir.mkdir()

    # 无快照支撑（tool_responses 为空）
    no_snap_trace = {
        "ts": "2026-09-06T10:00:00Z",
        "run_id": "run_nosnap",
        "trace_id": "run_nosnap:c1:baseline",
        "skill": "weather_query",
        "case_id": "c1",
        "query": "海口明天几度",
            "evaluand_type": "baseline",
            "evaluand_side": "baseline",
            "evaluand_answer": "海口明天33度",
            "answer": "海口明天33度",
            "baseline_answer": "海口明天33度",
            "skill_answer": "请根据工具快照回答。",
        "tool_responses": [],  # 无快照
        "provenance_check": {"status": "fail"},
        "presented_side": "A",
        "baseline_presented_side": "A",
        "skill_presented_side": "B",
        "judge_verdict": {
            "verdict": "VALID_FAILURE",
            "task_completion": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "robustness": {"verdict": "INVALID", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"], "evidence_summary": "baseline 外部事实无工具支持"},
            "readability": {"verdict": "tied", "presented_order": {"A": "baseline", "B": "skill"}, "reason_codes": ["EVIDENCE_SUFFICIENT"], "evidence_summary": "两侧表达清晰度相当"},
            "reason_codes": ["UNVERIFIED_EXTERNAL_FACT_A"],
        },
    }

    trace_file = traces_dir / "nosnap_sample.jsonl"
    trace_file.write_text(json.dumps(no_snap_trace, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = extract_cases_from_traces(
        traces_dir=traces_dir,
        repair_set_path=repair_file,
        dry_run=False,
        verbose=False,
    )

    assert summary["accepted"] == 0
    assert summary["rejected"] == 1
    assert "NO_SNAPSHOT_SUPPORT" in summary["reasons_distribution"]


def _weather_content(cities: list[str], dates: list[str]) -> str:
    casts = [
        {
            "date": wanted,
            "dayweather": "晴",
            "nightweather": "多云",
            "daytemp": str(25 + index),
            "nighttemp": str(18 + index),
            "daywind": "东风",
            "daypower": "3",
            "precipitation_probability": 20 + index,
        }
        for index, wanted in enumerate(dates)
    ]
    return json.dumps(
        {"forecasts": [{"city": city, "casts": casts} for city in cities]},
        ensure_ascii=False,
    )


def _effective_weather_trace(query: str, content: str, *, baseline_side: str = "A") -> dict:
    other_side = "B" if baseline_side == "A" else "A"
    order = {baseline_side: "baseline", other_side: "skill"}
    dimensions = {
        dimension: {
            "verdict": "INVALID",
            "presented_order": order,
            "reason_codes": [f"UNVERIFIED_EXTERNAL_FACT_{baseline_side}"],
            "evidence_summary": "baseline 外部事实无工具支持",
        }
        for dimension in ("task_completion", "robustness")
    }
    dimensions["readability"] = {
        "verdict": "tied",
        "presented_order": order,
        "reason_codes": ["EVIDENCE_SUFFICIENT"],
        "evidence_summary": "两侧表达清晰度相当",
    }
    answer = "明天温度是 99 度。"
    return {
        "ts": "2026-09-06T10:00:00Z",
        "run_id": "run_contract",
        "trace_id": "run_contract:c1:baseline",
        "skill": "weather_query",
        "case_id": "c1",
        "query": query,
        "evaluand_type": "baseline",
        "evaluand_side": "baseline",
        "evaluand_answer": answer,
        "answer": answer,
        "baseline_answer": answer,
        "skill_answer": "请根据天气工具快照回答。",
        "tool_responses": [_tool_response(content)],
        "provenance_check": {"status": "fail", "pass": False},
        "judge_verdict": {
            "verdict": "VALID_FAILURE",
            **dimensions,
            "reason_codes": [f"UNVERIFIED_EXTERNAL_FACT_{baseline_side}"],
        },
        "presented_side": baseline_side,
        "baseline_presented_side": baseline_side,
        "skill_presented_side": other_side,
    }


def test_snapshot_hash_and_side_binding_are_fail_closed():
    content = _weather_content(["北京市"], ["2026-09-06", "2026-09-07"])
    trace = _effective_weather_trace("北京明天温度多少", content)

    assert check_whitelist_semantics(trace) == (True, "VALID_EXTERNAL_FACT_CONFLICT")
    assert check_and_build_reference(trace)[0] is True

    wrong_hash = deepcopy(trace)
    wrong_hash["tool_responses"][0]["snapshot_id"] = "0" * 64
    assert check_and_build_reference(wrong_hash)[2] == "SNAPSHOT_HASH_UNVERIFIED"

    wrong_side = deepcopy(trace)
    wrong_side["baseline_presented_side"] = "B"
    assert check_whitelist_semantics(wrong_side)[0] is False


def test_reference_is_multi_city_and_relative_date_aligned():
    beijing = _weather_content(["北京市"], ["2026-09-06", "2026-09-07"])
    shanghai = _weather_content(["上海市"], ["2026-09-06", "2026-09-07"])
    trace = _effective_weather_trace("北京和上海明天温差多少", beijing)
    trace["tool_responses"].append(_tool_response(shanghai))

    ok, reference, reason = check_and_build_reference(trace)
    assert ok, reason
    assert "北京市" in reference and "上海市" in reference
    assert "2026-09-07" in reference
    assert "2026-09-06" not in reference


def test_terminal_trace_is_unique_and_three_dimensional(tmp_path):
    from skillforge.eval_tracer import record_invalid_skipped_trace

    trace_file = tmp_path / "terminal.jsonl"
    first = record_invalid_skipped_trace(trace_file, "same-run", "weather_query", "candidate", "budget")
    second = record_invalid_skipped_trace(trace_file, "same-run", "weather_query", "candidate", "exception")
    assert first["trace_id"] != second["trace_id"]
    for record in (first, second):
        assert record["judge_verdict"]["verdict"] == "INVALID_SKIPPED"
        assert all(
            record["judge_verdict"][dimension]["verdict"] == "INVALID_SKIPPED"
            for dimension in ("task_completion", "robustness", "readability")
        )


def test_extraction_stats_include_skill_intent_city_distribution(tmp_path):
    repair_file = tmp_path / "repair_set.json"
    repair_file.write_text(json.dumps({"meta": {"total": 0}, "cases": []}), encoding="utf-8")
    traces_dir = tmp_path / "eval_traces"
    traces_dir.mkdir()
    content = _weather_content(["北京市"], ["2026-09-06"])
    trace = _effective_weather_trace("北京今天天气", content)
    (traces_dir / "one.jsonl").write_text(json.dumps(trace, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = extract_cases_from_traces(traces_dir, repair_file, dry_run=True, verbose=False)
    assert summary["skill_distribution"]["weather_query"] == 1
    assert summary["intent_distribution"]["weather.forecast"] == 1
    assert summary["city_distribution"]["北京市"] == 1
