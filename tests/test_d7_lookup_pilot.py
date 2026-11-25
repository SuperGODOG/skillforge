import json

from skillforge.runtime import AgentRuntime
from skillforge.scenarios.d7_lookup_pilot import GROUPS, make_protocol, outcome
from skillforge.scenarios.d7_protocol import freeze_protocol
from skillforge.scenarios.d7_reuse_experiment import execute_reuse_experiment


def test_paired_lookup_pilot_uses_the_same_skill_and_keeps_abstention_distinct(tmp_path, monkeypatch):
    protocol = make_protocol()
    freeze_protocol(tmp_path, protocol)

    def script(runtime, *, run_id, input_text, tools, **kwargs):
        order_id = runtime.get_run(run_id).task_id
        assert input_text.startswith(f"订单 {order_id}。")
        order_call = runtime.execute_tool(run_id, "query_order_packages", {"order_id": order_id})
        order = json.loads(order_call.output_text)
        assert "scope_package_ids" not in order
        scope = order.get("current_approved_billing_scope", [])
        if not scope and "read_source_episode" in tools:
            for ref in order["archive_refs"]:
                call = runtime.execute_tool(run_id, "read_source_episode", {"episode_id": ref})
                trace = json.loads(call.output_text)["trace"]
                if {k: trace[k] for k in ("profile", "revision")} == order["billing_agreement"]:
                    scope = [pid for pid, tag in order["parcel_tags"].items() if tag in trace["seller_paid_tags"]]
                    break
        packages = {}
        for pid in scope:
            tracking = runtime.execute_tool(run_id, "query_package_tracking", {"package_id": pid})
            packages[pid] = tracking.output_data["status"]
        return json.dumps({"packages": packages, "all_delivered": None})

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", script)
    report = execute_reuse_experiment(tmp_path, groups=GROUPS)
    assert report["status"] == "COMPLETED"
    assert tuple(report["metadata"]["group_labels"]) == GROUPS
    assert "B" not in report["summary"] and "paired_b_to_c" not in report["summary"]
    assert report["summary"]["paired_c_to_d"] == {"improved": 4, "degraded": 0, "unchanged": 2, "unknown": 0}
    assert len(report["cases"]) == 12
    assert len({run["content_hash"] for run in report["cases"]}) == 1
    assert all(run["purpose"] == "evaluation" for run in report["cases"])
    tasks = {task["task_id"]: task for task in protocol["tasks"]}
    for run in report["cases"]:
        task = tasks[run["task_id"]]
        review = outcome(task, run)
        if task["kind"] == "transfer" and run["group"] == "C":
            assert review == {"category": "safe_abstention", "matching_sources_before_tracking": []}
        else:
            assert review["category"] == "completed"
    first_d = next(run for run in report["cases"] if run["task_id"] == "D7L-831" and run["group"] == "D")
    assert first_d["source_lookups"]["succeeded"] == ["EP-H53", "EP-Q27"]
    assert outcome(tasks["D7L-831"], first_d)["matching_sources_before_tracking"] == ["EP-Q27"]
    assert report["partitions"]["TRANSFER"]["tasks"][1]["group_order"] == ["D", "C"]
