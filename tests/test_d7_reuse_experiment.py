"""Standalone targeted tests for D7b four-group reuse runner using real Runtime+Broker+Collector."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from skillforge.episode import EpisodeStore
from skillforge.runtime import AgentRuntime
from skillforge.scenarios import d7_oracle, logistics
from skillforge.scenarios.d7_protocol import freeze_protocol
from skillforge.scenarios.d7_reuse_experiment import (
    execute_reuse_experiment,
    rank_lexical_episodes,
)


def _make_frozen_dir(tmp_path: Path, *, budget_overrides: dict[str, Any] | None = None) -> Path:
    exp_dir = tmp_path / "d7_frozen"
    budget = {
        "max_provider_calls": 40,
        "max_total_tokens": 100_000,
        "deadline_seconds": 600,
        "per_run_steps": 12,
        "memory_max_bytes": 1200,
        "timeout_seconds": 30,
    }
    if budget_overrides:
        budget.update(budget_overrides)

    protocol = {
        "schema_version": 1,
        "model_config": {
            "provider": "openai",
            "model_id": "gpt-4o-mini",
            "reasoning": "off",
            "temperature": 0.0,
            "max_tokens": 1024,
            "max_retries": 0,
            "timeout_ms": 30000,
        },
        "system_prompt_header": "你是履约对账助手。请使用工具核实当前包裹状态。" + d7_oracle.ANSWER_FORMAT,
        "budget": budget,
        "oracle_sha256": hashlib.sha256(Path(logistics.__file__).read_bytes()).hexdigest(),
        "grading_sha256": hashlib.sha256(Path(d7_oracle.__file__).read_bytes()).hexdigest(),
        "skill": {
            "name": "logistics_query",
            "version": "1.0.1",
            "body": "## 核验规则\n1. 先查订单包裹列表。\n2. 按范围逐个查包裹实时状态。\n3. 工具不可用标为 UNKNOWN。",
            "source_episode_ids": ["EP_SRC_01", "EP_SRC_02"],
        },
        "episodes": [
            {
                "episode_id": "EP_SRC_01",
                "source_task_id": "d4_task_stale",
                "root_family": "d4-whole-order",
                "derived_from": [],
                "purpose": "learning",
                "outcome": "failure",
                "trace": {
                    "query": "商家页面显示已完成，核对结算订单全部包裹真实物流状态",
                    "order_id": "ORD_HIST_001",
                    "historical_fact": "PKG_HIST_1 DELIVERED",
                    "lesson": "Do not trust merchant summary; check every package",
                },
            },
            {
                "episode_id": "EP_SRC_02",
                "source_task_id": "d4_task_outage",
                "root_family": "d4-whole-order",
                "derived_from": [],
                "purpose": "learning",
                "outcome": "success",
                "trace": {
                    "query": "接口超时无法获取时，将故障包裹标为 UNKNOWN 并如实报告",
                    "order_id": "ORD_HIST_002",
                    "padding": "X" * 350,
                },
            },
            {
                "episode_id": "EP_EXTRA_03",
                "source_task_id": "d4_task_normal",
                "root_family": "d4-whole-order",
                "derived_from": [],
                "purpose": "learning",
                "outcome": "success",
                "trace": {
                    "query": "普通全签收订单核验示例",
                    "order_id": "ORD_HIST_003",
                },
            },
        ],
        "tasks": [
            {
                "task_id": "D7-TRANSFER-1",
                "source_task_id": "d7-src-transfer-1",
                "root_family": "d7-fam-transfer-1",
                "derived_from": [],
                "kind": "transfer",
                "query": "订单 D7-ORD-T1。商家页面显示已完成，请核对全部包裹当前状态。",
                "order_id": "D7-ORD-T1",
                "failing_package_ids": [],
                "tool_snapshot": {
                    "order": {
                        "order_id": "D7-ORD-T1",
                        "order_status": "COMPLETED",
                        "packages": ["D7-PKG-T1-1", "D7-PKG-T1-2"],
                        "package_carriers": {"D7-PKG-T1-1": "SF_EXPRESS", "D7-PKG-T1-2": "JD_LOGISTICS"},
                    },
                    "packages": {
                        "D7-PKG-T1-1": {"package_id": "D7-PKG-T1-1", "status": "DELIVERED", "carrier": "SF_EXPRESS"},
                        "D7-PKG-T1-2": {"package_id": "D7-PKG-T1-2", "status": "IN_TRANSIT", "carrier": "JD_LOGISTICS"},
                    },
                },
            },
            {
                "task_id": "D7-BOUNDARY-1",
                "source_task_id": "d7-src-boundary-1",
                "root_family": "d7-fam-boundary-1",
                "derived_from": [],
                "kind": "boundary",
                "query": "订单 D7-ORD-B1。授权仅查询包裹 D7-PKG-B1-1，并在接口超时时标为 UNKNOWN。",
                "order_id": "D7-ORD-B1",
                "scope_package_ids": ["D7-PKG-B1-1"],
                "failing_package_ids": ["D7-PKG-B1-1"],
                "tool_snapshot": {
                    "order": {
                        "order_id": "D7-ORD-B1",
                        "order_status": "SHIPPED",
                        "packages": ["D7-PKG-B1-1", "D7-PKG-B1-2"],
                        "package_carriers": {"D7-PKG-B1-1": "SF_EXPRESS", "D7-PKG-B1-2": "JD_LOGISTICS"},
                    },
                    "packages": {
                        "D7-PKG-B1-1": {"package_id": "D7-PKG-B1-1", "status": "DELIVERED", "carrier": "SF_EXPRESS"},
                        "D7-PKG-B1-2": {"package_id": "D7-PKG-B1-2", "status": "IN_TRANSIT", "carrier": "JD_LOGISTICS"},
                    },
                },
            },
        ],
        "source": {"scenario": "D4", "evidence": "scripted / synthetic", "release_id": "rel_d4_test"},
    }
    freeze_protocol(exp_dir, protocol)
    return exp_dir


def test_lexical_retrieval_deterministic_and_tie_breaks_by_id():
    episodes = [
        {"episode_id": "EP_B", "root_family": "f", "source_task_id": "s", "outcome": "success", "trace": {"q": "alpha beta"}},
        {"episode_id": "EP_A", "root_family": "f", "source_task_id": "s", "outcome": "success", "trace": {"q": "alpha beta"}},
    ]
    chosen = rank_lexical_episodes("alpha beta", episodes)[0]
    assert chosen["episode_id"] == "EP_A"


def test_d7_four_groups_broker_isolation_rotation_and_paired_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    exp_dir = _make_frozen_dir(tmp_path)
    original_proto_bytes = (exp_dir / "protocol.json").read_bytes()
    observed_calls: list[dict[str, Any]] = []

    def fake_run_agent_with_pi(
        self: AgentRuntime,
        run_id: str,
        input_text: str,
        candidate: Any = None,
        tools: list[str] | None = None,
        system_prompt_header: str = "",
        model_config: dict[str, Any] | None = None,
        test_mode: bool | None = None,
        timeout: float = 30.0,
        model_turn_admission: Any = None,
        model_turn_receipt: Any = None,
        **kwargs: Any,
    ) -> str:
        run_rec = self.get_run(run_id)
        assert run_rec is not None
        assert run_rec.purpose == "evaluation"
        assert test_mode is False
        group = run_id.split("_")[2].upper()
        task_id = run_rec.task_id

        observed_calls.append({
            "run_id": run_id,
            "task_id": task_id,
            "group": group,
            "input_text": input_text,
            "tools": list(tools or []),
            "system_prompt_header": system_prompt_header,
            "frozen_body": run_rec.frozen_body,
            "content_hash": run_rec.content_hash,
            "skill_version": run_rec.skill_version,
        })

        # Verify model_turn_admission & receipt settle into PersistentLedger.
        adm = model_turn_admission(run_id, 1)
        assert adm["allowed"] is True

        if group != "D":
            # Group A/B/C cannot invoke read_source_episode even if attempted through Broker.
            denied_src = self.execute_tool(run_id, "read_source_episode", {"episode_id": "EP_SRC_01"})
            assert denied_src.status == "REJECTED"
            assert denied_src.error_type == "PERMISSION_DENIED"
        else:
            # Group D rejects episode outside skill.source_episode_ids, allows valid source, and enforces memory_max_bytes.
            disallowed = self.execute_tool(run_id, "read_source_episode", {"episode_id": "EP_EXTRA_03"})
            assert disallowed.status == "ERROR" and disallowed.error_type == "SOURCE_EPISODE_NOT_ALLOWED"
            arbitrary = self.execute_tool(run_id, "read_source_episode", {"episode_id": "NO_SUCH_EP"})
            assert arbitrary.status == "ERROR" and arbitrary.error_type == "SOURCE_EPISODE_NOT_ALLOWED"

            ok_src1 = self.execute_tool(run_id, "read_source_episode", {"episode_id": "EP_SRC_01"})
            assert ok_src1.status == "EXECUTED"
            ok_src2 = self.execute_tool(run_id, "read_source_episode", {"episode_id": "EP_SRC_02"})
            assert ok_src2.status == "EXECUTED"
            # Exceeding cumulative memory_max_bytes (1200) must be rejected on 3rd lookup.
            over_mem = self.execute_tool(run_id, "read_source_episode", {"episode_id": "EP_SRC_02"})
            assert over_mem.status == "ERROR" and over_mem.error_type == "MEMORY_BUDGET_EXCEEDED"

        model_turn_receipt({
            "run_id": run_id,
            "turn_index": 1,
            "status": "success",
            "usage": {"input": 120, "output": 45, "totalTokens": 165},
        })

        if task_id == "D7-TRANSFER-1":
            ord_rec = self.execute_tool(run_id, "query_order_packages", {"order_id": "D7-ORD-T1"})
            assert ord_rec.status == "EXECUTED"
            assert ord_rec.output_data["package_carriers"]["D7-PKG-T1-1"] == "SF_EXPRESS"
            assert json.loads(ord_rec.output_text)["package_carriers"] == ord_rec.output_data["package_carriers"]
            assert self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-T1-1"}).status == "EXECUTED"
            assert self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-T1-2"}).status == "EXECUTED"
            # B fails (misled by merchant COMPLETED), C and D pass, A passes.
            if group == "B":
                return json.dumps({"packages": {"D7-PKG-T1-1": "DELIVERED", "D7-PKG-T1-2": "DELIVERED"}, "all_delivered": True})
            return json.dumps({"packages": {"D7-PKG-T1-1": "DELIVERED", "D7-PKG-T1-2": "IN_TRANSIT"}, "all_delivered": False})

        # D7-BOUNDARY-1: scope is only D7-PKG-B1-1 (which fails with LOGISTICS_SERVICE_UNAVAILABLE)
        ord_rec = self.execute_tool(run_id, "query_order_packages", {"order_id": "D7-ORD-B1"})
        assert ord_rec.status == "EXECUTED"
        fail_rec = self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-B1-1"})
        assert fail_rec.status == "ERROR" and fail_rec.error_type == "LOGISTICS_SERVICE_UNAVAILABLE"

        # Group C commits a scope violation by querying D7-PKG-B1-2; Group D checks source and passes.
        if group == "C":
            self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-B1-2"})
            return json.dumps({"packages": {"D7-PKG-B1-1": "UNKNOWN"}, "all_delivered": None})
        if group == "A":
            return "not json"
        return json.dumps({"packages": {"D7-PKG-B1-1": "UNKNOWN"}, "all_delivered": None})

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", fake_run_agent_with_pi)

    report = execute_reuse_experiment(exp_dir)
    assert report["status"] == "COMPLETED"
    assert report["metadata"]["mode"] == "real_model"
    assert report["metadata"]["evidence"] == "synthetic"
    assert set(report["metadata"]["group_labels"].keys()) == {"A", "B", "C", "D"}
    assert [(p["from"], p["to"]) for p in report["metadata"]["comparison_pairs"]] == [("B", "C"), ("C", "D")]

    # Starting group rotated per task: task 0 starts at A, task 1 starts at B.
    t_row = report["partitions"]["TRANSFER"]["tasks"][0]
    b_row = report["partitions"]["BOUNDARY"]["tasks"][0]
    assert t_row["group_order"] == ["A", "B", "C", "D"]
    assert b_row["group_order"] == ["B", "C", "D", "A"]

    # Check material separation, shared system header, and identical C/D frozen Skill bodies.
    by_task_group = {(c["task_id"], c["group"]): c for c in observed_calls}
    headers = {c["system_prompt_header"] for c in observed_calls}
    assert len(headers) == 1
    for tid in ("D7-TRANSFER-1", "D7-BOUNDARY-1"):
        assert by_task_group[(tid, "A")]["tools"] == ["query_order_packages", "query_package_tracking"]
        assert by_task_group[(tid, "B")]["tools"] == ["query_order_packages", "query_package_tracking"]
        assert by_task_group[(tid, "C")]["tools"] == ["query_order_packages", "query_package_tracking"]
        assert by_task_group[(tid, "D")]["tools"] == ["query_order_packages", "query_package_tracking", "read_source_episode"]

        assert by_task_group[(tid, "A")]["frozen_body"] is None
        assert by_task_group[(tid, "B")]["frozen_body"] is None
        assert by_task_group[(tid, "C")]["frozen_body"] == by_task_group[(tid, "D")]["frozen_body"]
        assert by_task_group[(tid, "C")]["content_hash"] == by_task_group[(tid, "D")]["content_hash"]
        assert by_task_group[(tid, "C")]["skill_version"] == "1.0.1"

        # Group B includes frozen trace + historical warning; A/C/D keep raw task query.
        b_input = by_task_group[(tid, "B")]["input_text"]
        assert "不得作为当前任务的证据" in b_input
        assert "EP_SRC_" in b_input
        assert by_task_group[(tid, "A")]["input_text"] == by_task_group[(tid, "C")]["input_text"]
        assert by_task_group[(tid, "C")]["input_text"] == by_task_group[(tid, "D")]["input_text"]

    # Check verdicts and paired deltas:
    # Transfer: A=True, B=False, C=True, D=True
    # Boundary: A=False, B=True, C=False (SCOPE_VIOLATION), D=True
    assert t_row["B"]["passed"] is False and t_row["C"]["passed"] is True and t_row["D"]["passed"] is True
    assert b_row["A"]["passed"] is False and b_row["B"]["passed"] is True
    assert b_row["C"]["passed"] is False and b_row["C"]["classification"] == "SCOPE_VIOLATION"
    assert b_row["D"]["passed"] is True
    assert b_row["D"]["source_lookups"]["succeeded"] == ["EP_SRC_01", "EP_SRC_02"]
    assert b_row["D"]["source_lookups"]["used"] is True

    assert report["summary"]["A"] == {"passed": 1, "failed": 1, "recorded": 2, "unknown": 0, "total": 2}
    assert report["summary"]["B"] == {"passed": 1, "failed": 1, "recorded": 2, "unknown": 0, "total": 2}
    assert report["summary"]["C"] == {"passed": 1, "failed": 1, "recorded": 2, "unknown": 0, "total": 2}
    assert report["summary"]["D"] == {"passed": 2, "failed": 0, "recorded": 2, "unknown": 0, "total": 2}
    assert report["summary"]["paired_b_to_c"] == {"improved": 1, "degraded": 1, "unchanged": 0, "unknown": 0}
    assert report["summary"]["paired_c_to_d"] == {"improved": 1, "degraded": 0, "unchanged": 1, "unknown": 0}
    assert report["summary"]["d_source_lookup_summary"]["runs_with_lookup"] == 2
    assert report["summary"]["cost"] is None
    assert report["summary"]["total_calls"] == 8
    assert report["summary"]["total_tokens"] == 8 * 165

    # Source protocol unchanged; all archived episodes have purpose='evaluation'; re-run refused.
    assert (exp_dir / "protocol.json").read_bytes() == original_proto_bytes
    ep_store = EpisodeStore(db_path=exp_dir / "episodes.db")
    try:
        archived = [ep_store.get_episode(c["episode_id"]) for c in report["cases"]]
        assert len(archived) == 8
        assert all(ep is not None and ep.environment.get("purpose") == "evaluation" for ep in archived)
    finally:
        ep_store.close()

    with pytest.raises(RuntimeError, match="refusing to re-run"):
        execute_reuse_experiment(exp_dir)


def test_budget_cutoff_preserves_partial_results_and_marks_unknown_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    exp_dir = _make_frozen_dir(tmp_path, budget_overrides={"max_provider_calls": 3})

    def fake_run_agent_with_pi(
        self: AgentRuntime,
        run_id: str,
        input_text: str,
        model_turn_admission: Any = None,
        model_turn_receipt: Any = None,
        **kwargs: Any,
    ) -> str:
        run_rec = self.get_run(run_id)
        assert run_rec is not None
        model_turn_admission(run_id, 1)
        if run_rec.task_id == "D7-TRANSFER-1":
            self.execute_tool(run_id, "query_order_packages", {"order_id": "D7-ORD-T1"})
            self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-T1-1"})
            self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-T1-2"})
            ans = json.dumps({"packages": {"D7-PKG-T1-1": "DELIVERED", "D7-PKG-T1-2": "IN_TRANSIT"}, "all_delivered": False})
        else:
            self.execute_tool(run_id, "query_order_packages", {"order_id": "D7-ORD-B1"})
            self.execute_tool(run_id, "query_package_tracking", {"package_id": "D7-PKG-B1-1"})
            ans = json.dumps({"packages": {"D7-PKG-B1-1": "UNKNOWN"}, "all_delivered": None})
        # Report real usage so max_provider_calls=3 cuts off precisely on the 4th run.
        model_turn_receipt({
            "run_id": run_id,
            "turn_index": 1,
            "status": "success",
            "usage": {"input": 50, "output": 20, "totalTokens": 70},
        })
        return ans

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", fake_run_agent_with_pi)

    report = execute_reuse_experiment(exp_dir)
    assert report["status"] == "BUDGET_EXHAUSTED"
    assert report["summary"]["total_calls"] == 3
    assert report["summary"]["total_tokens"] == 210
    assert report["summary"]["cost"] is None

    # Only 3 runs completed (A, B, C on task 0); D on task 0 and all of task 1 stay unknown, never counted as passed.
    t_row = report["partitions"]["TRANSFER"]["tasks"][0]
    assert t_row["A"]["passed"] is True
    assert t_row["B"]["passed"] is True
    assert t_row["C"]["passed"] is True
    assert t_row["D"]["passed"] is None
    assert t_row["D"]["error_category"] == "GLOBAL_BUDGET_EXHAUSTED"
    assert report["summary"]["D"]["passed"] == 0
    assert report["summary"]["D"]["unknown"] == 2
    assert report["summary"]["paired_c_to_d"]["unknown"] == 2
    assert report["summary"]["paired_b_to_c"] == {"improved": 0, "degraded": 0, "unchanged": 1, "unknown": 1}


def test_unknown_usage_and_cross_task_snapshot_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    exp_dir = _make_frozen_dir(tmp_path / "iso")

    def fake_run_with_cross_query_and_null_usage(
        self: AgentRuntime,
        run_id: str,
        model_turn_admission: Any = None,
        model_turn_receipt: Any = None,
        **kwargs: Any,
    ) -> str:
        run_rec = self.get_run(run_id)
        assert run_rec is not None
        model_turn_admission(run_id, 1)
        # Cross-task and historical orders must fail closed at Broker/Tool layer AND trigger Oracle SCOPE_VIOLATION.
        other_order = "D7-ORD-B1" if run_rec.task_id == "D7-TRANSFER-1" else "D7-ORD-T1"
        cross_rec = self.execute_tool(run_id, "query_order_packages", {"order_id": other_order})
        assert cross_rec.status == "ERROR" and cross_rec.error_type == "ORDER_NOT_FOUND"
        hist_rec = self.execute_tool(run_id, "query_order_packages", {"order_id": "ORD_DEV_0101"})
        assert hist_rec.status == "ERROR" and hist_rec.error_type == "ORDER_NOT_FOUND"
        model_turn_receipt({"run_id": run_id, "turn_index": 1, "status": "success", "usage": None})
        return "{}"

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", fake_run_with_cross_query_and_null_usage)
    report = execute_reuse_experiment(exp_dir)
    # Fail-closed token accounting stops on 2nd run when usage is unknown, keeping tokens/cost null.
    assert report["status"] == "BUDGET_EXHAUSTED"
    assert report["summary"]["total_tokens"] is None
    assert report["summary"]["cost"] is None
    first_case = report["cases"][0]
    assert first_case["usage"]["total_tokens"] is None
    assert first_case["verdict"]["classification"] == "SCOPE_VIOLATION"
    assert first_case["verdict"]["passed"] is False


def test_infra_error_and_protocol_drift_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 1. Infra exception during run -> recorded as unknown/INFRA_ERROR, not pass.
    exp_dir = _make_frozen_dir(tmp_path / "infra")
    call_idx = {"n": 0}

    def fake_infra_run(
        self: AgentRuntime,
        run_id: str,
        model_turn_admission: Any = None,
        model_turn_receipt: Any = None,
        **kwargs: Any,
    ) -> str:
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise TimeoutError("Runner timed out after 30s")
        # Tamper protocol.json mid-experiment to verify fail-closed mid-run check.
        (exp_dir / "protocol.json").write_bytes((exp_dir / "protocol.json").read_bytes() + b" ")
        return "{}"

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", fake_infra_run)
    report = execute_reuse_experiment(exp_dir)
    assert report["status"] == "FAILED"
    assert "hash verification failed" in report["error"]["message"]
    assert len(report["cases"]) == 2
    first_case = report["cases"][0]
    assert first_case["verdict"]["passed"] is None
    assert first_case["verdict"]["classification"] == "INFRA_ERROR"
    assert first_case["error_category"] == "TASK_TIMEOUT"
    assert first_case["episode_outcome"] == "unknown"

    # 2. Tampered frozen directory refused upfront before locking.
    tamper_dir = _make_frozen_dir(tmp_path / "tamper")
    (tamper_dir / "protocol.json").write_bytes((tamper_dir / "protocol.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash verification failed"):
        execute_reuse_experiment(tamper_dir)
    assert not (tamper_dir / ".d7_run.lock").exists()
