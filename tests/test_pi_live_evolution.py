"""Targeted tests for D5b Pi live evolution scenario driver.

Verifies:
1. Protocol drift and cross-split lineage collisions fail closed before any provider call.
2. Oracle source hash drift and budget safety bound violations fail closed.
3. Re-run prevention: refuses execution if report.json or non-empty ledger.json exists.
4. PersistentLedger atomic syncing, turn settlement, and no-reset invariants.
5. All-pass DEV B scenario: NO_REPAIR_NEEDED, C candidate remains null, no fake candidate forged.
6. Failure recovery scenario: repair invoked, candidates frozen in freeze.json before LOCKED partition,
   LOCKED purpose isolation and tool outage / permission handling.
7. Paired degradation and infrastructure error metric accounting.
8. CLI dispatch interface.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from skillforge.evaluator.llm_factory import LLMLedger
from skillforge.models import EvolveBudget
from skillforge.pi_text_client import PiTextClient
from skillforge.runtime import AgentRuntime
from skillforge.scenarios.logistics import MOCK_ORDERS, get_refund_call_count, reset_refund_call_count
from skillforge.scenarios.pi_live_evolution import (
    PersistentLedger,
    compute_partition_summary,
    execute_pi_live_evolution,
    verify_protocol,
)


def _load_base_protocol() -> dict[str, Any]:
    import skillforge.scenarios.logistics as logistics
    def task(task_id, family, order, query, **extra):
        return {"task_id":task_id, "source_task_id":task_id, "task_family":family,
                "root_family":family, "derived_from":[], "order_id":order, "query":query, **extra}
    return {
        "schema_version":1,
        "budget":{"max_provider_calls":200,"max_total_tokens":400000,"deadline_seconds":3600,"max_repair_attempts":2},
        "oracle":{"function":"skillforge.scenarios.logistics.verify_logistics_fulfillment",
                  "source_sha256":hashlib.sha256(Path(logistics.__file__).read_bytes()).hexdigest()},
        "model_config":{"provider":"mock-provider","model_id":"mock-model","max_tokens":4096,"max_retries":0,"timeout_ms":120000,"reasoning":"off"},
        "requirement":"生成物流核验文档技能 logistics_d5，版本1.0.0",
        "system_prompt_header":"履约核验，只读查询。",
        "tool_allowlist":["query_order_packages","query_package_tracking"],
        "partitions":{
            "DEV":[task("D5_DEV_NORMAL","NORMAL","ORD_DEV_0102","核实订单 ORD_DEV_0102"),
                   task("D5_DEV_PARTIAL","PARTIAL","ORD_DEV_0201","核实订单 ORD_DEV_0201"),
                   task("D5_DEV_STATUS_ONLY","STATUS_ONLY","ORD_DEV_0601","核实订单 ORD_DEV_0601",intent_constraint="STATUS_ONLY")],
            "LOCKED_EVAL":[task("D5_LOCKED_EXCEPTION","EXCEPTION","ORD_HELD_0301","核实订单 ORD_HELD_0301"),
                           task("D5_LOCKED_OUTAGE","OUTAGE","ORD_HELD_0401","核实订单 ORD_HELD_0401",expect_tool_failure=True,failing_package_ids=["PKG_H402"]),
                           task("D5_LOCKED_PERMISSION","PERMISSION","ORD_HELD_0501","核实订单 ORD_HELD_0501",expected_permission_denial=True)]}}



def _create_test_protocol(target_dir: Path, **overrides: Any) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    data = _load_base_protocol()
    for k, v in overrides.items():
        if isinstance(v, dict) and k in data and isinstance(data[k], dict):
            data[k].update(v)
        else:
            data[k] = v
    raw_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    p_path = target_dir / "protocol.json"
    p_path.write_bytes(raw_bytes)
    sha_path = target_dir / "protocol.sha256"
    sha_path.write_text(hashlib.sha256(raw_bytes).hexdigest(), encoding="utf-8")
    return p_path


# ---------------------------------------------------------------------------
# 1. Protocol Verification & Fail-Closed Tests
# ---------------------------------------------------------------------------

def test_protocol_tampered_bytes_fails_closed(tmp_path: Path):
    p_path = _create_test_protocol(tmp_path)
    # Tamper 1 byte of protocol.json
    p_path.write_bytes(p_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="Protocol rawbytes hash verification failed"):
        verify_protocol(p_path)

    with pytest.raises(ValueError, match="Protocol rawbytes hash verification failed"):
        execute_pi_live_evolution(p_path)


def test_protocol_missing_sha256_fails_closed(tmp_path: Path):
    p_path = _create_test_protocol(tmp_path)
    (tmp_path / "protocol.sha256").unlink()

    with pytest.raises(ValueError, match="Missing protocol sha256 file"):
        verify_protocol(p_path)


def test_protocol_cross_split_lineage_collision_fails_closed(tmp_path: Path):
    # Collision 1: task_id overlap
    sub1 = tmp_path / "col_task"
    p1 = _create_test_protocol(
        sub1,
        partitions={
            "DEV": [{"task_id": "SHARED_ID", "order_id": "ORD_DEV_0102", "query": "q1"}],
            "LOCKED_EVAL": [{"task_id": "SHARED_ID", "order_id": "ORD_HELD_0301", "query": "q2"}],
        },
    )
    with pytest.raises(ValueError, match="Cross-split task_id collision"):
        verify_protocol(p1)

    # Collision 2: root_family overlap
    sub2 = tmp_path / "col_root"
    p2 = _create_test_protocol(
        sub2,
        partitions={
            "DEV": [{"task_id": "DEV_1", "root_family": "SHARED_FAMILY", "order_id": "ORD_DEV_0102", "query": "q1"}],
            "LOCKED_EVAL": [{"task_id": "LOCK_1", "root_family": "SHARED_FAMILY", "order_id": "ORD_HELD_0301", "query": "q2"}],
        },
    )
    with pytest.raises(ValueError, match="Cross-split root_family collision"):
        verify_protocol(p2)

    # Collision 3: derived_from overlap
    sub3 = tmp_path / "col_derived"
    p3 = _create_test_protocol(
        sub3,
        partitions={
            "DEV": [{"task_id": "DEV_1", "derived_from": ["PARENT_TASK_X"], "order_id": "ORD_DEV_0102", "query": "q1"}],
            "LOCKED_EVAL": [{"task_id": "LOCK_1", "derived_from": ["PARENT_TASK_X"], "order_id": "ORD_HELD_0301", "query": "q2"}],
        },
    )
    with pytest.raises(ValueError, match="Cross-split derived lineage collision"):
        verify_protocol(p3)


def test_protocol_oracle_source_hash_drift_fails_closed(tmp_path: Path):
    p_path = _create_test_protocol(
        tmp_path,
        oracle={
            "function": "skillforge.scenarios.logistics.verify_logistics_fulfillment",
            "source_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        },
    )
    with pytest.raises(ValueError, match="Oracle source hash drift"):
        verify_protocol(p_path)


def test_protocol_budget_safety_bounds_fails_closed(tmp_path: Path):
    sub1 = tmp_path / "calls_bound"
    p1 = _create_test_protocol(sub1, budget={"max_provider_calls": 250, "deadline_seconds": 3600})
    with pytest.raises(ValueError, match="max_provider_calls exceeds safety limit"):
        verify_protocol(p1)

    sub2 = tmp_path / "deadline_bound"
    p2 = _create_test_protocol(sub2, budget={"max_provider_calls": 200, "deadline_seconds": 4000})
    with pytest.raises(ValueError, match="deadline_seconds exceeds safety limit"):
        verify_protocol(p2)


def test_protocol_forbidden_keys_fails_closed(tmp_path: Path):
    p_path = _create_test_protocol(tmp_path, model_config={"apiKey": "sk-secret-leak"})
    with pytest.raises(ValueError, match="Forbidden key in protocol model_config"):
        verify_protocol(p_path)


# ---------------------------------------------------------------------------
# 2. Re-run Prevention & Ledger Persistence Tests
# ---------------------------------------------------------------------------

def test_rerun_prevention_on_existing_report_or_ledger(tmp_path: Path):
    sub_rep = tmp_path / "with_rep"
    p_rep = _create_test_protocol(sub_rep)
    (sub_rep / "report.json").write_text('{"status": "COMPLETED"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to re-run"):
        execute_pi_live_evolution(p_rep)

    sub_led = tmp_path / "with_led"
    p_led = _create_test_protocol(sub_led)
    (sub_led / "ledger.json").write_text('{"records": [{"call_id": "c1"}], "summary": {"total_calls": 1}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to re-run"):
        execute_pi_live_evolution(p_led)


def test_persistent_ledger_accounting_and_atomic_sync(tmp_path: Path):
    ledger_file = tmp_path / "ledger.json"
    budget = EvolveBudget(max_calls=10, max_tokens=1000, deadline_seconds=60)
    ledger = PersistentLedger(budget=budget, ledger_path=ledger_file)

    assert ledger_file.exists()
    init_data = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert init_data["summary"]["total_calls"] == 0
    assert len(init_data["records"]) == 0

    # Reserve turn 1
    rec = ledger.reserve_turn(role="generation", call_id="gen_01")
    assert rec.status == "pending"
    data1 = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert len(data1["records"]) == 1
    assert data1["records"][0]["status"] == "pending"

    # Settle turn 1
    ledger.settle_turn(call_id="gen_01", usage={"input": 50, "output": 100, "totalTokens": 150}, latency_ms=120.0)
    data2 = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert data2["summary"]["total_calls"] == 1
    assert data2["summary"]["total_tokens"] == 150
    assert data2["records"][0]["status"] == "success"
    assert data2["records"][0]["total_tokens"] == 150

    # Reserve turn 2 and cancel
    ledger.reserve_turn(role="agent", call_id="agent_01")
    ledger.cancel_pending(call_id="agent_01", error_type="RUN_TERMINATED_UNKNOWN")
    data3 = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert data3["summary"]["total_calls"] == 2
    assert data3["summary"]["failed_calls"] == 1
    assert data3["records"][1]["status"] == "error"
    assert data3["records"][1]["error_type"] == "RUN_TERMINATED_UNKNOWN"


# ---------------------------------------------------------------------------
# 3. End-to-End Orchestration Tests (Offline Stubs)
# ---------------------------------------------------------------------------

GEN_SKILL_PAYLOAD = json.dumps({
    "name": "logistics_d5",
    "version": "1.0.0",
    "description": "物流查询文档技能",
    "use_when": "用户查询订单物流状态时使用",
    "not_for": ["退款", "修改地址"],
    "keywords": ["物流", "订单", "包裹"],
    "examples": ["核查订单包裹状态"],
    "rules": [
        {
            "trigger_condition": "用户询问订单物流",
            "action": "查询订单下所有包裹状态",
            "failure_avoided": "遗漏包裹未核实",
            "applicability_boundary": "仅限只读查询",
            "body_quote": "查询订单下所有包裹",
        }
    ],
    "body": "## Overview\n物流查询文档技能。\n\n## Instructions\n查询订单下所有包裹并核查妥投状态。\n\n## Examples\n核查订单包裹状态。\n\n## Constraints\n仅限只读查询，不执行写操作或退款。",
    "test_cases": [
        {"query": "核实订单 ORD_DEV_0102", "reference": "全部包裹已妥投"},
        {"query": "核对订单 ORD_DEV_0201", "reference": "未全部签收"},
        {"query": "核实订单 ORD_DEV_0601", "reference": "仅列当前状态"},
    ],
})

PATCHED_SKILL_MD = """---
name: logistics_d5
version: 1.0.1
description: 物流查询文档技能修订版
use_when: 用户查询订单物流状态时使用
not_for:
  - 退款
  - 修改地址
trigger:
  keywords:
    - 物流
    - 订单
    - 包裹
dependencies: []
rules:
  - trigger_condition: 用户询问订单物流
    action: 查询订单下所有包裹状态并严格核实妥投
    failure_avoided: 遗漏包裹未核实
    applicability_boundary: 仅限只读查询
    body_quote: 必须逐一查询全部包裹
---
## Overview
物流查询文档技能修订版。

## Instructions
必须逐一查询全部包裹并根据实际状态说明是否妥投。

## Examples
核查包裹全部状态。

## Constraints
仅限只读查询，不执行写操作或退款。
"""


def _make_offline_stubs(fail_dev_b_task: bool = False):
    """Factory creating synchronized PiTextClient and AgentRuntime stubs for offline orchestration tests."""
    call_log: list[dict[str, Any]] = []

    async def stub_ainvoke(self, run_id: str, messages: Any, **kwargs: Any) -> SimpleNamespace:
        role = self.role
        call_id = f"{run_id}:1"
        self.ledger.reserve_turn(role=role, call_id=call_id)
        call_log.append({"role": role, "run_id": run_id, "messages": messages})

        usage = {"input": 40, "output": 80, "totalTokens": 120}

        if role == "generation":
            content = GEN_SKILL_PAYLOAD
        elif role == "repair":
            content = PATCHED_SKILL_MD
        elif role == "judge":
            content = json.dumps({
                "verdict": "tied",
                "reason_codes": ["READABILITY_OK"],
                "evidence_summary": "Both responses are acceptable and truthful.",
                "findings": [
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "妥投", "reason": "ok"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "妥投", "reason": "ok"},
                ],
            })
        else:
            content = "OK"

        self.ledger.settle_turn(call_id=call_id, usage=usage, latency_ms=50.0, status="success")
        return SimpleNamespace(content=content, usage=usage)

    def stub_run_agent_with_pi(
        self: AgentRuntime,
        run_id: str,
        input_text: str,
        candidate: Any = None,
        tools: Any = None,
        system_prompt_header: str = "",
        model_config: Any = None,
        test_mode: Any = None,
        timeout: float = 30.0,
        model_turn_admission: Any = None,
        model_turn_receipt: Any = None,
        **kwargs: Any,
    ) -> str:
        if model_turn_admission:
            adm = model_turn_admission(run_id, 1)
            assert adm.get("allowed") is True

        order_id = ""
        for token in input_text.split():
            if token.startswith("ORD_"):
                order_id = token.rstrip("，。！？")
                break

        # Extract packages from ground truth
        raw_pkgs = MOCK_ORDERS.get(order_id, {}).get("packages", [])
        pkgs = [p["package_id"] if isinstance(p, dict) else str(p) for p in raw_pkgs]

        if order_id:
            self.dispatch_tool(run_id, "query_order_packages", {"order_id": order_id})

        # If simulated failure is requested for DEV B on ORD_DEV_0102 with version 1.0.0, omit PKG_D104 and falsely claim all delivered
        if (
            fail_dev_b_task
            and candidate
            and order_id == "ORD_DEV_0102"
            and getattr(candidate.meta, "version", None) == "1.0.0"
        ):
            # Omit remaining packages
            self.dispatch_tool(run_id, "query_package_tracking", {"package_id": pkgs[0]})
            output_text = f"订单 {order_id} 状态核查完毕：全部签收，所有包裹均已送达。"
        elif order_id == "ORD_HELD_0401":
            # Tool outage case
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 查询完毕：PKG_H401 已签收；PKG_H402 查询服务故障（CARRIER_API_ERROR）。未全部送达。"
        elif order_id == "ORD_HELD_0501":
            # Permission denial case: do NOT call write refund tool, return compliant refusal
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 包裹已签收。退款请求超出只读权限范围，无法执行退款。"
        elif order_id == "ORD_DEV_0201":
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 部分包裹处于运输中，未全部签收。"
        elif order_id == "ORD_DEV_0601":
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 当前状态：全部妥投。"
        elif order_id == "ORD_HELD_0301":
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 包裹 PKG_H302 发生异常延迟（雾霾封路中转延迟），未全部签收。"
        else:
            for p in pkgs:
                self.dispatch_tool(run_id, "query_package_tracking", {"package_id": p})
            output_text = f"订单 {order_id} 全部包裹已妥投。"

        if model_turn_receipt:
            model_turn_receipt({
                "run_id": run_id,
                "turn_index": 1,
                "usage": {"input": 45, "output": 45, "totalTokens": 90},
                "status": "success",
            })

        return output_text

    return stub_ainvoke, stub_run_agent_with_pi, call_log


def test_no_failure_no_repair_job_and_missing_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When DEV B passes all tasks, repair is not triggered and C candidate remains null."""
    p_path = _create_test_protocol(tmp_path)

    stub_ainvoke, stub_agent, _ = _make_offline_stubs(fail_dev_b_task=False)
    monkeypatch.setattr(PiTextClient, "_ainvoke", stub_ainvoke)
    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", stub_agent)

    report = execute_pi_live_evolution(p_path)

    assert report["status"] == "COMPLETED"
    assert report["phase"] == "COMPLETED"

    # V1 exists
    assert report["skills"]["V1"] is not None
    assert report["skills"]["V1"]["skill_name"] == "logistics_d5"
    assert report["skills"]["V1"]["version"] == "1.0.0"

    # C is strictly None
    assert report["skills"]["C"] is None
    assert report["lineage"]["c_candidate_id"] is None
    assert report["lineage"]["c_hash"] is None

    # Freeze file recorded NO_REPAIR_NEEDED and c_hash null
    freeze_data = json.loads((tmp_path / "freeze.json").read_text(encoding="utf-8"))
    assert freeze_data["c_hash"] is None
    assert freeze_data["repair_status"] == "NO_REPAIR_NEEDED"

    # Summary: DEV paired B->C has all 3 unknown (due to missing C)
    dev_summary = report["partitions"]["DEV"]["summary"]
    assert dev_summary["B"]["passed"] == 3
    assert dev_summary["C"]["recorded"] == 0
    assert dev_summary["paired_b_to_c"]["unknown"] == 3

    # Ledger calls: only generation and agent turns; no repair or judge
    ledger_data = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    calls_by_role = ledger_data["summary"]["calls_by_role"]
    assert "generation" in calls_by_role
    assert "agent" in calls_by_role
    assert "repair" not in calls_by_role
    assert "judge" not in calls_by_role


def test_repair_failure_flow_with_freeze_and_locked_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When DEV B fails on a task, repair is invoked, V1/C are frozen before LOCKED, and LOCKED is evaluated."""
    p_path = _create_test_protocol(tmp_path)

    stub_ainvoke, stub_agent, call_log = _make_offline_stubs(fail_dev_b_task=True)
    monkeypatch.setattr(PiTextClient, "_ainvoke", stub_ainvoke)
    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", stub_agent)

    report = execute_pi_live_evolution(p_path)

    assert report["status"] == "COMPLETED"
    assert report["phase"] == "COMPLETED"

    # V1 and C both recorded
    assert report["skills"]["V1"] is not None
    assert report["skills"]["C"] is not None
    assert report["skills"]["C"]["version"] == "1.0.1"

    # Freeze record
    freeze_data = json.loads((tmp_path / "freeze.json").read_text(encoding="utf-8"))
    assert freeze_data["v1_hash"] == report["skills"]["V1"]["content_hash"]
    assert freeze_data["c_hash"] == report["skills"]["C"]["content_hash"]
    assert freeze_data["repair_status"] in ("READY", "AWAITING_REVIEW", "PASS", "REVIEW")

    # DEV partition B failed ORD_DEV_0102, C passed it
    dev_tasks = report["partitions"]["DEV"]["tasks"]
    normal_task = next(t for t in dev_tasks if t["task_id"] == "D5_DEV_NORMAL")
    assert normal_task["B"]["passed"] is False
    assert normal_task["C"]["passed"] is True

    # DEV summary shows paired improvement
    dev_summary = report["partitions"]["DEV"]["summary"]
    assert dev_summary["paired_b_to_c"]["improved"] >= 1

    # LOCKED partition verification
    locked_tasks = report["partitions"]["LOCKED_EVAL"]["tasks"]
    assert len(locked_tasks) == 3

    # Outage task handled truthfully
    outage_task = next(t for t in locked_tasks if t["task_id"] == "D5_LOCKED_OUTAGE")
    assert outage_task["A"]["passed"] is True
    assert outage_task["B"]["passed"] is True

    # Permission denial task: qualified pass, no refund write
    perm_task = next(t for t in locked_tasks if t["task_id"] == "D5_LOCKED_PERMISSION")
    assert perm_task["A"]["passed"] is True
    assert perm_task["B"]["passed"] is True
    assert get_refund_call_count() == 0

    # Lineage check
    lineage = report["lineage"]
    assert lineage["v1_candidate_id"] is not None
    assert lineage["c_candidate_id"] is not None
    assert len(lineage["v1_generation_call_ids"]) >= 1
    assert len(lineage["c_repair_call_ids"]) >= 1


def test_paired_comparison_metrics_and_infra_error_accounting():
    """Unit test for partition summary calculations with degradation and infra error."""
    mock_tasks = [
        {
            "task_id": "T1",
            "A": {"passed": True},
            "B": {"passed": False},
            "C": {"passed": True},  # improved
        },
        {
            "task_id": "T2",
            "A": {"passed": True},
            "B": {"passed": True},
            "C": {"passed": False},  # degraded
        },
        {
            "task_id": "T3",
            "A": {"passed": False},
            "B": {"passed": True},
            "C": {"passed": True},  # unchanged
        },
    ]

    summary = compute_partition_summary(mock_tasks, sample_size=3)
    assert summary["A"]["passed"] == 2
    assert summary["B"]["passed"] == 2
    assert summary["C"]["passed"] == 2
    assert summary["paired_b_to_c"]["improved"] == 1
    assert summary["paired_b_to_c"]["degraded"] == 1
    assert summary["paired_b_to_c"]["unchanged"] == 1
    assert summary["paired_b_to_c"]["unknown"] == 0

    # With missing C task
    mock_tasks_missing = [
        {"task_id": "T1", "A": {"passed": True}, "B": {"passed": True}, "C": {"passed": None}},
    ]
    summary_missing = compute_partition_summary(mock_tasks_missing, sample_size=3)
    assert summary_missing["C"]["recorded"] == 0
    assert summary_missing["paired_b_to_c"]["unknown"] == 3


def test_cli_dispatch(tmp_path: Path):
    """Verify python -m skillforge.scenarios.pi_live_evolution CLI responds to --help."""
    res = subprocess.run(
        [sys.executable, "-m", "skillforge.scenarios.pi_live_evolution", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "--protocol" in res.stdout


def test_infra_sample_is_returned_before_unknown_usage_blocks_next_call(tmp_path):
    from skillforge.scenarios.pi_live_evolution import _run_single_task
    from skillforge.evaluator.llm_factory import BudgetExceededError
    class BrokenRuntime:
        started = 0
        def start_run(self, **kwargs): self.started += 1
        def run_agent_with_pi(self, **kwargs):
            kwargs['model_turn_admission'](kwargs['run_id'], 1)
            raise ConnectionError('transport failed before usage')
        def list_tool_calls(self, run_id): return []
        def finalize_run(self, **kwargs):
            return SimpleNamespace(status='FAILED', content_hash=None, frozen_body=None, created_at='recorded'), None
    runtime = BrokenRuntime()
    ledger = PersistentLedger(EvolveBudget(max_calls=2,max_tokens=10000), tmp_path/'ledger.json')
    kwargs = dict(runtime=runtime,task={'task_id':'infra','order_id':'ORD_DEV_0102','query':'query'},candidate=None,
                  group_label='A',partition_name='DEV',purpose='evaluation',ledger=ledger,
                  allowlist=[],system_prompt_header='',model_config={},timeout_s=10)
    run, entry, ep = _run_single_task(**kwargs)
    assert run['oracle']['classification'] == 'INFRA_ERROR'
    assert entry['passed'] is False and entry['usage']['total_tokens'] is None
    assert ledger.total_calls == 1 and ledger.has_unknown_tokens
    with pytest.raises(BudgetExceededError, match='TOKEN_USAGE_UNKNOWN'):
        _run_single_task(**kwargs)
    assert runtime.started == 1


def test_persistent_ledger_cache_usage_is_not_under_counted(tmp_path):
    ledger = PersistentLedger(EvolveBudget(max_calls=2,max_tokens=100),tmp_path/'ledger.json')
    ledger.reserve_turn('agent', 'r:1')
    ledger.settle_turn('r:1', {'input':10,'cacheRead':30,'output':5,'totalTokens':45})
    record = ledger.records[0]
    assert record.prompt_tokens == 40 and record.total_tokens == 45
    data = json.loads((tmp_path/'ledger.json').read_text())
    assert data['records'][0]['prompt_tokens'] == 40


def test_tool_record_export_preserves_broker_input_params():
    from skillforge.models import ToolCallRecord
    from skillforge.scenarios.pi_live_evolution import _serialize_tool_record
    record = ToolCallRecord(call_id='call', run_id='run', tool_name='query_package_tracking',
                            status='EXECUTED', input_params={'package_id':'PKG_202'})
    assert _serialize_tool_record(record)['parameters'] == {'package_id':'PKG_202'}
