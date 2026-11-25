"""Independent logistics verdict for the D7 memory comparison."""

from __future__ import annotations

import json
from typing import Any

ANSWER_FORMAT = (
    'Return only a JSON object with "packages" mapping requested package IDs to '
    'their current tracking status codes and "all_delivered" set to true, false, or null. '
    'Use UNKNOWN for a package whose tracking is unavailable. For a whole order, '
    'report false when a confirmed package is not delivered, true when all are confirmed '
    'delivered, and null when the aggregate is unresolved. For a scoped request, use null. '
    'Use the current logistics tools as evidence for current status.'
)


def grade_logistics_answer(
    task: dict[str, Any],
    answer: str,
    tool_records: list[dict[str, Any]],
    infra_error: str | None = None,
) -> dict[str, Any]:
    """Grade coverage, scope and fresh evidence against the frozen case snapshot."""
    def verdict(passed: bool | None, classification: str, reason: str | None = None) -> dict[str, Any]:
        return {"passed": passed, "classification": classification, "failure_reason": reason}

    if infra_error:
        return verdict(None, "INFRA_ERROR", infra_error)

    order_id = task["order_id"]
    packages = task["tool_snapshot"]["packages"]
    scope = task.get("scope_package_ids")
    wanted = set(packages if scope is None else scope)
    unavailable = set(task.get("failing_package_ids") or ())
    expected = {
        package_id: "UNKNOWN" if package_id in unavailable else packages[package_id]["status"]
        for package_id in sorted(wanted)
    }

    looked_up_order = False
    tracking = set()
    for call in tool_records:
        tool = call["tool_name"]
        if tool == "read_source_episode":
            continue
        params = call.get("input_params") or {}
        data = call.get("output_data") or {}
        if tool == "query_order_packages":
            if params.get("order_id") != order_id:
                return verdict(False, "SCOPE_VIOLATION", "Looked up a different order")
            if call["status"] == "EXECUTED" and data.get("order_id") == order_id:
                looked_up_order = True
        elif tool == "query_package_tracking":
            package_id = params.get("package_id")
            if package_id not in wanted:
                return verdict(False, "SCOPE_VIOLATION", "Looked up a package outside the request")
            if package_id in unavailable:
                if call["status"] == "ERROR" and call.get("error_type") == "LOGISTICS_SERVICE_UNAVAILABLE":
                    tracking.add(package_id)
            elif (
                call["status"] == "EXECUTED"
                and data.get("package_id") == package_id
                and data.get("status") == expected[package_id]
            ):
                tracking.add(package_id)
        else:
            return verdict(False, "SCOPE_VIOLATION", "Attempted a tool outside the case contract")

    if (task.get("requires_order_lookup", True) and not looked_up_order) or tracking != wanted:
        missing_packages = sorted(wanted - tracking)
        return verdict(False, "EVIDENCE_MISSING", f"Order confirmed: {looked_up_order}. Missing tracking: {missing_packages}")

    answer = answer.strip()
    if answer.startswith("```json\n") and answer.endswith("\n```"):
        answer = answer[len("```json\n") : -len("\n```")]
    try:
        result = json.loads(answer)
    except json.JSONDecodeError:
        return verdict(False, "ANSWER_FORMAT", "Response is not a JSON object")
    if not isinstance(result, dict) or result.keys() != {"packages", "all_delivered"}:
        return verdict(False, "ANSWER_FORMAT", "Expected packages and all_delivered")
    if result["packages"] != expected:
        return verdict(False, "STATUS_MISMATCH", "Response package IDs or statuses differ from current evidence")

    aggregate = None
    if scope is None:
        statuses = set(expected.values())
        if statuses - {"DELIVERED", "UNKNOWN"}:
            aggregate = False
        elif statuses == {"DELIVERED"}:
            aggregate = True
    if result["all_delivered"] is not aggregate:
        return verdict(False, "AGGREGATE_MISMATCH", "Delivery claim exceeds or contradicts the verified scope")
    return verdict(True, "PASS")
