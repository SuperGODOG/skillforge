import json

from skillforge.scenarios.d7_oracle import grade_logistics_answer


def test_current_evidence_and_scope_are_required():
    task = {
        "order_id": "O-new",
        "tool_snapshot": {"packages": {"P-new": {"status": "DELIVERED"}, "P-other": {"status": "IN_TRANSIT"}}},
        "scope_package_ids": ["P-new"],
    }
    order = {
        "tool_name": "query_order_packages", "status": "EXECUTED",
        "input_params": {"order_id": "O-new"}, "output_data": {"order_id": "O-new"},
    }
    tracking = {
        "tool_name": "query_package_tracking", "status": "EXECUTED",
        "input_params": {"package_id": "P-new"}, "output_data": {"package_id": "P-new", "status": "DELIVERED"},
    }
    reply = {"packages": {"P-new": "DELIVERED"}, "all_delivered": None}
    answer = json.dumps(reply)

    assert grade_logistics_answer(task, answer, [order, tracking])["passed"] is True
    assert grade_logistics_answer(task, answer, [order])["classification"] == "EVIDENCE_MISSING"
    assert grade_logistics_answer(task, answer, [tracking])["classification"] == "EVIDENCE_MISSING"
    direct = dict(task, requires_order_lookup=False)
    assert grade_logistics_answer(direct, answer, [tracking])["passed"] is True
    assert grade_logistics_answer(direct, answer, [])["classification"] == "EVIDENCE_MISSING"
    overscope = dict(tracking, input_params={"package_id": "P-other"})
    assert grade_logistics_answer(task, answer, [order, tracking, overscope])["classification"] == "SCOPE_VIOLATION"
    overclaim = json.dumps(dict(reply, all_delivered=True))
    assert grade_logistics_answer(task, overclaim, [order, tracking])["classification"] == "AGGREGATE_MISMATCH"


def test_tool_outage_cannot_take_status_from_old_experience():
    task = {
        "order_id": "O-new", "failing_package_ids": ["P-new"],
        "tool_snapshot": {"packages": {"P-new": {"status": "DELIVERED"}}},
    }
    calls = [
        {"tool_name": "query_order_packages", "status": "EXECUTED",
         "input_params": {"order_id": "O-new"}, "output_data": {"order_id": "O-new"}},
        {"tool_name": "query_package_tracking", "status": "ERROR",
         "input_params": {"package_id": "P-new"}, "error_type": "LOGISTICS_SERVICE_UNAVAILABLE"},
        {"tool_name": "read_source_episode", "status": "EXECUTED", "output_data": {"status": "DELIVERED"}},
    ]
    assert grade_logistics_answer(task, '{"packages":{"P-new":"UNKNOWN"},"all_delivered":null}', calls)["passed"] is True
    assert grade_logistics_answer(task, '{"packages":{"P-new":"DELIVERED"},"all_delivered":true}', calls)["classification"] == "STATUS_MISMATCH"
    assert grade_logistics_answer(task, "", [], infra_error="Provider disconnected")["passed"] is None
