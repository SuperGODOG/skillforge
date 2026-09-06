"""Sample-level evaluation trace recording and fail-closed annotations.

The trace is an audit artifact, not a score cache.  It therefore keeps both
answers, the side ordering used by each Judge dimension, and the complete
fixture provenance needed to independently replay a snapshot binding.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from .evaluator.judge import (
    PROVENANCE_SIGNATURE_FIELDS,
    _provenance_signature,
    _snapshot_binding_is_valid,
)


_DIMENSIONS = ("task_completion", "robustness", "readability")
_EVALUAND_TYPES = {"baseline", "candidate", "attempt"}
_DIMENSION_VERDICTS = {"A_better", "tied", "B_better", "INVALID"}


def get_default_traces_dir(repo_root: Optional[Path] = None) -> Path:
    base = repo_root or Path(__file__).resolve().parents[2]
    traces_dir = base / "runs" / "eval_traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    gitignore = traces_dir / ".gitignore"
    if not gitignore.exists():
        try:
            gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
        except Exception:
            pass
    return traces_dir


def append_eval_trace(trace_file: Path | str, record: dict[str, Any]) -> None:
    """Append a single evaluation trace as one JSON line."""
    p = Path(trace_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _provenance_object(raw: Any) -> SimpleNamespace:
    """Adapt a dict/dataclass provenance to the Judge's canonical helpers."""
    values = {
        field: _value(raw, field, None)
        for field in PROVENANCE_SIGNATURE_FIELDS
    }
    values["signature"] = _value(raw, "signature", "")
    return SimpleNamespace(**values)


def _validate_provenance(raw: Any) -> dict[str, Any]:
    """Validate all provenance gates, including the signed snapshot binding."""
    p = _provenance_object(raw)
    required_present = all(
        _value(raw, field, None) is not None
        for field in PROVENANCE_SIGNATURE_FIELDS
    )
    checks = {
        "required_fields": required_present,
        "is_fixture": p.is_fixture is True,
        "tool_required": p.tool_required is True,
        "tool_called": p.tool_called is True,
        "tool_success": p.tool_success is True,
        "authenticity_pass": p.authenticity_pass is True,
        "output_status": p.output_status == "SUCCESS",
        "call_count": isinstance(p.call_count, int) and p.call_count > 0,
        "call_index": (
            isinstance(p.call_index, int)
            and isinstance(p.call_count, int)
            and p.call_count > 0
            and 1 <= p.call_index <= p.call_count
        ),
        "signature": False,
        "snapshot_binding": False,
    }
    try:
        checks["signature"] = bool(p.signature) and p.signature == _provenance_signature(p)
    except Exception:
        checks["signature"] = False
    try:
        checks["snapshot_binding"] = _snapshot_binding_is_valid(p)
    except Exception:
        checks["snapshot_binding"] = False
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "tool": _value(raw, "tool_name", ""),
        "snapshot_id": _value(raw, "snapshot_id", ""),
    }


def _side_order(cv: dict[str, Any], dimension: str, fallback: dict[str, Any]) -> dict[str, str]:
    ja = cv.get("judge_audit", {}) if isinstance(cv.get("judge_audit"), dict) else {}
    audit = ja.get(dimension, {}) if isinstance(ja.get(dimension), dict) else {}
    order = audit.get("presented_order")
    if (
        isinstance(order, dict)
        and order.get("A") in {"skill", "baseline"}
        and order.get("B") in {"skill", "baseline"}
        and {order.get("A"), order.get("B")} == {"skill", "baseline"}
    ):
        return {"A": order["A"], "B": order["B"]}
    if (
        isinstance(fallback, dict)
        and fallback.get("A") in {"skill", "baseline"}
        and fallback.get("B") in {"skill", "baseline"}
        and {fallback.get("A"), fallback.get("B")} == {"skill", "baseline"}
    ):
        return {"A": fallback["A"], "B": fallback["B"]}
    # Missing or ambiguous Judge ordering must remain unmapped.  Guessing a
    # side would make a malformed trace look like a baseline failure.
    return {}


def _presented_sides(order: dict[str, str]) -> tuple[str, str]:
    baseline_side = next(
        (side for side in ("A", "B") if order.get(side) == "baseline"), ""
    )
    skill_side = next(
        (side for side in ("A", "B") if order.get(side) == "skill"), ""
    )
    return baseline_side, skill_side


def _trace_provenances(raw_provs: Sequence[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tool_responses: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for raw in raw_provs:
        item = {
            "snapshot_id": _value(raw, "snapshot_id", ""),
            "content": _value(raw, "snapshot_content", ""),
            "tool_name": _value(raw, "tool_name", ""),
            "fixture_case_id": _value(raw, "fixture_case_id", ""),
            "parameters": _value(raw, "input_params", {}),
            "output_status": _value(raw, "output_status", ""),
            "call_index": _value(raw, "call_index", 0),
            "call_count": _value(raw, "call_count", 0),
            "is_fixture": _value(raw, "is_fixture", False),
            "tool_required": _value(raw, "tool_required", False),
            "tool_called": _value(raw, "tool_called", False),
            "tool_success": _value(raw, "tool_success", False),
            "authenticity_pass": _value(raw, "authenticity_pass", False),
            "output_summary": _value(raw, "output_summary", ""),
            "latency_ms": _value(raw, "latency_ms", 0.0),
            "timestamp": _value(raw, "timestamp", ""),
            "signature": _value(raw, "signature", ""),
        }
        tool_responses.append(item)
        checks.append(_validate_provenance(raw))
    # A no-tool answer is valid provenance-wise unless the Judge separately
    # marks an external-fact claim; tool-backed answers must pass every gate.
    passed = not checks or all(c["pass"] for c in checks)
    return tool_responses, {
        "status": "pass" if passed else "fail",
        "pass": passed,
        "details": checks or [{"info": "no_tool_invocations"}],
    }


def derive_eval_case_annotations(eval_result: Any) -> tuple[list[dict[str, Any]], set[str]]:
    """Return ``(skipped, effective_failure_ids)`` using evolver's one policy.

    Importing lazily avoids a module cycle while ensuring the tracer and the
    evolution decision path cannot grow two subtly different whitelists.
    """
    from .evolver import _is_effective_failure, _is_judge_infrastructure_error

    skipped: list[dict[str, Any]] = []
    effective: set[str] = set()
    for cv in getattr(eval_result, "case_verdicts", []) or []:
        if not isinstance(cv, dict):
            continue
        cid = str(cv.get("case_id", ""))
        audits = cv.get("judge_audit", {}) if isinstance(cv.get("judge_audit"), dict) else {}
        case_skipped = False
        reasons: list[str] = []
        for dim in _DIMENSIONS:
            if dim not in cv or not isinstance(audits.get(dim), dict):
                case_skipped = True
                reasons.append(f"{dim}: MISSING_DIMENSION_AUDIT")
                continue
            value = cv.get(dim)
            audit = audits[dim]
            if value not in _DIMENSION_VERDICTS:
                case_skipped = True
                reasons.append(f"{dim}: INVALID_VERDICT_SCHEMA")
                continue
            codes = [str(c) for c in audit.get("reason_codes", [])]
            if value in {"INVALID", "TIMEOUT", "FIXTURE_ERROR"}:
                if _is_judge_infrastructure_error(codes, value, audit):
                    case_skipped = True
                    reasons.append(f"{dim}: {','.join(codes) or value}")
                elif _is_effective_failure(codes, value, audit):
                    effective.add(cid)
                else:
                    case_skipped = True
                    reasons.append(f"{dim}: {','.join(codes) or value}")
        if case_skipped:
            skipped.append({"case_id": cid, "reasons": reasons})
    skipped_ids = {item["case_id"] for item in skipped}
    return skipped, effective - skipped_ids


def record_eval_traces_from_eval_result(
    trace_file: Path | str,
    run_id: str,
    skill: str,
    eval_result: Any,
    evaluand_type: str = "baseline",
    skipped_cases: Optional[Sequence[dict[str, Any] | str]] = None,
    effective_failed_cases: Optional[Sequence[str] | set[str]] = None,
) -> list[dict[str, Any]]:
    """Convert an EvalResult to auditable sample-level traces."""
    if evaluand_type not in _EVALUAND_TYPES:
        raise ValueError(f"unsupported evaluand_type: {evaluand_type}")

    trace_path = Path(trace_file)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    skipped_ids: set[str] = set()
    for sc in skipped_cases or []:
        skipped_ids.add(str(sc.get("case_id", "")) if isinstance(sc, dict) else str(sc))
    eff_fail_ids = {str(cid) for cid in (effective_failed_cases or set())}

    case_verdicts = getattr(eval_result, "case_verdicts", []) or []
    case_outputs = getattr(eval_result, "case_outputs", []) or []
    outputs_by_id = {
        co.get("case_id"): co
        for co in case_outputs
        if isinstance(co, dict) and co.get("case_id")
    }
    now_ts = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []

    for cv in case_verdicts:
        if not isinstance(cv, dict):
            continue
        cid = str(cv.get("case_id", ""))
        co = outputs_by_id.get(cid, {})
        query = co.get("query") or cv.get("query") or ""
        baseline_answer = co.get("output_baseline", "") or ""
        skill_answer = co.get("output_skill", "") or ""
        evaluand_side_name = "baseline" if evaluand_type == "baseline" else "skill"
        evaluand_answer = baseline_answer if evaluand_side_name == "baseline" else skill_answer

        raw_provs = co.get("provenances", []) or []
        tool_responses, provenance_check = _trace_provenances(raw_provs)

        ja_top = cv.get("judge_audit", {}) if isinstance(cv.get("judge_audit"), dict) else {}
        task_audit = ja_top.get("task_completion", {}) if isinstance(ja_top.get("task_completion"), dict) else {}
        fallback_order = task_audit.get("presented_order", {})
        top_order = _side_order(cv, "task_completion", fallback_order)
        baseline_side, skill_side = _presented_sides(top_order)
        presented_side = baseline_side if evaluand_side_name == "baseline" else skill_side
        # An absent mapping is itself auditable; do not silently label it A.
        if not presented_side:
            presented_side = "UNMAPPED"

        all_reason_codes: list[str] = []
        dims_data: dict[str, Any] = {}
        presented_orders: dict[str, dict[str, str]] = {}
        malformed_case = (
            not isinstance(ja_top, dict)
            or not isinstance(co, dict)
            or not isinstance(co.get("output_baseline"), str)
            or not isinstance(co.get("output_skill"), str)
        )
        for dim in _DIMENSIONS:
            dim_v = cv.get(dim)
            dim_audit = ja_top.get(dim, {}) if isinstance(ja_top.get(dim), dict) else {}
            codes = [str(c) for c in dim_audit.get("reason_codes", [])]
            all_reason_codes.extend(codes)
            order = _side_order(cv, dim, {})
            if (
                dim not in cv
                or dim_v not in _DIMENSION_VERDICTS
                or not order
                or not isinstance(dim_audit.get("reason_codes"), list)
                or not codes
                or not isinstance(dim_audit.get("evidence_summary"), str)
                or not dim_audit.get("evidence_summary", "").strip()
            ):
                malformed_case = True
            presented_orders[dim] = order
            dims_data[dim] = {
                # A skipped case is skipped in every dimension, including
                # dimensions where a Judge happened to return a value.
                "verdict": "INVALID_SKIPPED" if cid in skipped_ids else dim_v,
                "reason_codes": codes,
                "evidence_summary": dim_audit.get("evidence_summary", ""),
                "presented_order": order,
            }

        # A partial/malformed Judge result is never a usable sample.  Mark all
        # three dimensions consistently so candidate/attempt traces cannot
        # leak a mixture of real and missing verdicts downstream.
        if malformed_case:
            skipped_ids.add(cid)
            for dim in _DIMENSIONS:
                dims_data[dim]["verdict"] = "INVALID_SKIPPED"

        if cid in skipped_ids:
            overall_verdict = "INVALID_SKIPPED"
        elif cid in eff_fail_ids:
            overall_verdict = "VALID_FAILURE"
        elif any(dims_data[d]["verdict"] == "INVALID" for d in dims_data):
            overall_verdict = "INVALID"
        elif any(dims_data[d]["verdict"] == "B_better" for d in dims_data):
            overall_verdict = "VALID_FAILURE"
        else:
            overall_verdict = "PASS"

        unverified_code = f"UNVERIFIED_EXTERNAL_FACT_{presented_side}"
        has_unverified_flag = (
            unverified_code in all_reason_codes
            or "UNVERIFIED_EXTERNAL_FACT" in all_reason_codes
        )
        if has_unverified_flag:
            provenance_check["status"] = "fail"
            provenance_check["pass"] = False
            provenance_check.setdefault("details", []).append({
                "error": "UNVERIFIED_EXTERNAL_FACT: evaluand made an unverified external-fact claim"
            })

        # A run can evaluate several candidate patches for the same case and
        # evaluand type.  Keep the human-readable prefix, but make each record
        # independently addressable.
        trace_id = f"{run_id}:{cid}:{evaluand_type}:{uuid.uuid4().hex}"
        rec = {
            "ts": now_ts,
            "run_id": run_id,
            "trace_id": trace_id,
            "skill": skill,
            "case_id": cid,
            "query": query,
            "evaluand_type": evaluand_type,
            "evaluand_side": evaluand_side_name,
            "evaluand_answer": evaluand_answer,
            # Compatibility alias; it now always means the selected evaluand.
            "answer": evaluand_answer,
            "baseline_answer": baseline_answer,
            "skill_answer": skill_answer,
            "tool_responses": tool_responses,
            "provenance_check": provenance_check,
            "judge_verdict": {
                "verdict": overall_verdict,
                "task_completion": dims_data["task_completion"],
                "robustness": dims_data["robustness"],
                "readability": dims_data["readability"],
                "reason_codes": list(dict.fromkeys(all_reason_codes)),
            },
            "presented_side": presented_side,
            "baseline_presented_side": baseline_side,
            "skill_presented_side": skill_side,
            "presented_orders": presented_orders,
            "presented_sides": {
                dim: {
                    "baseline": _presented_sides(presented_orders[dim])[0],
                    "skill": _presented_sides(presented_orders[dim])[1],
                }
                for dim in _DIMENSIONS
            },
        }
        records.append(rec)
        append_eval_trace(trace_path, rec)
    return records


def record_invalid_skipped_trace(
    trace_file: Path | str,
    run_id: str,
    skill: str,
    evaluand_type: str,
    reason: str,
    *,
    case_id: str = "__run__",
) -> dict[str, Any]:
    """Record a terminal exception/budget stop without inventing a sample."""
    if evaluand_type not in _EVALUAND_TYPES:
        raise ValueError(f"unsupported evaluand_type: {evaluand_type}")
    dims = {
        dim: {
            "verdict": "INVALID_SKIPPED",
            "reason_codes": ["INVALID_SKIPPED", "TRACE_TERMINAL"],
            "evidence_summary": reason,
            "presented_order": {},
        }
        for dim in _DIMENSIONS
    }
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "trace_id": f"{run_id}:terminal:{uuid.uuid4().hex}",
        "skill": skill,
        "case_id": case_id,
        "query": "",
        "evaluand_type": evaluand_type,
        "evaluand_side": "baseline" if evaluand_type == "baseline" else "skill",
        "evaluand_answer": "",
        "answer": "",
        "baseline_answer": "",
        "skill_answer": "",
        "tool_responses": [],
        "provenance_check": {
            "status": "fail",
            "pass": False,
            "details": [{"error": "TRACE_TERMINAL", "reason": reason}],
        },
        "judge_verdict": {
            "verdict": "INVALID_SKIPPED",
            **dims,
            "reason_codes": ["INVALID_SKIPPED", "TRACE_TERMINAL"],
        },
        "presented_side": "UNMAPPED",
        "baseline_presented_side": "",
        "skill_presented_side": "",
        "presented_orders": {dim: {} for dim in _DIMENSIONS},
        "terminal": True,
        "terminal_reason": reason,
    }
    append_eval_trace(trace_file, rec)
    return rec
