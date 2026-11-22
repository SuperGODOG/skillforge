"""Phase 2B B3b1: Action Logging, Idempotency & Cancellation Safety Test Suite.

Verifies:
1. paused_factory real Pi paused, then cancel_run (new Collector memory is empty):
   - Both resume entrypoints (Pi resume and legacy resume_run) are rejected.
   - Budget consumed remains 2, deadline unchanged, no new PID spawned, no tool executed, no success Episode.
   - PAUSED run calling execute_tool directly is rejected without consuming budget ticket.
2. Controlled HostLost(BaseException) simulation between Broker.dispatch return and final persist:
   - Database persists ADMITTED record and handler count is 1.
   - New Runtime on same DB calling Pi resume is rejected with REVIEW_REQUIRED.
   - Handler count remains 1, budget consumed is not zeroed, no new Pi process started.
   - Run status is marked INTERRUPTED with error_type REVIEW_REQUIRED.
3. Call ID idempotency & concurrency claim:
   - Identical parameters return original record without re-calling handler or deducting budget.
   - Conflicting run_id or parameters are rejected without overwriting or leaking existing record.
   - Two concurrent Runtime connections claiming same call_id result in single handler execution and single budget deduction.
4. arun with checkpoint rejects contract overrides (tools, candidate, goal, intent_version,
   initial_scratchpad, active_constraints, system_prompt_header):
   - No new Pi process spawned, run status remains PAUSED, database not polluted.
   - Normal recovery path still succeeds.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from hello_agents.tools import Tool, ToolParameter, ToolResponse, ToolStatus

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.models import CandidateSkill, SkillMeta, ToolCallRecord
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker

try:
    from test_pi_phase2b_recovery import (
        BODY,
        FINAL,
        ORDER,
        assert_reaped,
        cfg,
        paused_factory,
        remaining,
    )
except ImportError:
    from tests.test_pi_phase2b_recovery import (
        BODY,
        FINAL,
        ORDER,
        assert_reaped,
        cfg,
        paused_factory,
        remaining,
    )


class HostLost(BaseException):
    """Simulated host failure before tool status finalization."""


def test_lifecycle_cancel_paused_run_and_refuse_resumes(paused_factory, tmp_path: Path):
    """Scenario 1: cancel_run on paused run with empty collector memory, then refuse resumes and direct tool calls."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # 1. PAUSED run calling execute_tool directly is rejected without consuming budget ticket
    assert rt.get_run(rid).status == "PAUSED"
    tool_rec = rt.execute_tool(
        run_id=rid,
        tool_name="query_order_packages",
        parameters={"order_id": ORDER},
    )
    assert tool_rec.status == "REJECTED"
    assert "PAUSED" in (tool_rec.error_type or "") or "PAUSED" in (tool_rec.error_message or "")
    rec_paused = rt.get_run(rid)
    assert rec_paused.status == "PAUSED"
    assert rec_paused.budget_consumed == 2
    assert e["counts"] == {"order": 1, "track": 0}
    allowed, reason, _ = rt.consume_turn_budget(rid)
    assert not allowed and reason == "PAUSED"
    assert rt.get_run(rid).budget_consumed == 2

    # 2. Cancel the run using fresh Runtime instance (where collector._runs is empty)
    cancelled_rec = rt.cancel_run(rid, reason="User cancelled paused run")
    assert cancelled_rec.status == "CANCELLED"
    assert cancelled_rec.error_type == "CANCELLED"
    assert cancelled_rec.budget_consumed == 2
    assert cancelled_rec.deadline_ts == e["deadline"]
    assert e["episodes"].get_episode("ep_" + rid) is None

    # 3. Both resume entrypoints must reject resuming a CANCELLED run
    with pytest.raises(ValueError, match="terminal"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )

    with pytest.raises(ValueError, match="terminal"):
        rt.resume_run(run_id=rid, budget_max=20)

    # 4. Assert invariants: no new PID spawned, tool counts unchanged, budget preserved
    assert len(e["pids"]) == 1
    assert e["counts"] == {"order": 1, "track": 0}
    rec = rt.get_run(rid)
    assert rec.status == "CANCELLED"
    assert rec.budget_consumed == 2
    assert rec.deadline_ts == e["deadline"]
    assert e["episodes"].get_episode("ep_" + rid) is None


def test_lifecycle_unsettled_admitted_crash_blocks_recovery(tmp_path: Path):
    """Scenario 2: Controlled crash injection leaving ADMITTED call blocks recovery with REVIEW_REQUIRED."""
    handler_calls = {"count": 0}

    class SyntheticTool(Tool):
        def __init__(self) -> None:
            super().__init__(name="synth_tool", description="Synthetic test tool")

        def get_parameters(self) -> list[ToolParameter]:
            return [
                ToolParameter(name="test", type="string", description="Test parameter", required=False)
            ]

        def run(self, *args, **kwargs):
            handler_calls["count"] += 1
            return ToolResponse(ToolStatus.SUCCESS, "synth_executed_ok")

    db_path = tmp_path / "lifecycle_crash.db"
    broker = ToolBroker(application_allowlist={"synth_tool"})
    broker.register_tool(SyntheticTool())

    rt_old = AgentRuntime(db_path=db_path, tool_broker=broker)
    rt_old.start_run(run_id="run_admitted_crash", task_id="task_crash", budget_max=5)

    # Controlled crash injection: allow ADMITTED insertion, but raise HostLost before final EXECUTED status persist
    orig_persist = rt_old._persist_tool_call

    def failing_persist(record: ToolCallRecord):
        if record.status == "ADMITTED":
            return orig_persist(record)
        raise HostLost("Simulated host loss after tool dispatch before final status persist")

    rt_old._persist_tool_call = failing_persist

    with pytest.raises(HostLost):
        rt_old.execute_tool(
            run_id="run_admitted_crash",
            tool_name="synth_tool",
            parameters={"test": "crash"},
            call_id="call_admitted_1",
        )

    # Verify ADMITTED state in SQLite and handler executed once
    conn = rt_old._get_conn()
    row = conn.execute(
        "SELECT status, tool_name FROM runtime_tool_calls WHERE call_id = 'call_admitted_1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "ADMITTED"
    assert row[1] == "synth_tool"
    assert handler_calls["count"] == 1
    assert rt_old.get_run("run_admitted_crash").budget_consumed == 1

    rt_old.close()

    # New runtime instance on same DB attempts Pi resume -> must be rejected with REVIEW_REQUIRED
    broker_new = ToolBroker(application_allowlist={"synth_tool"})
    broker_new.register_tool(SyntheticTool())
    rt_new = AgentRuntime(db_path=db_path, tool_broker=broker_new)

    with pytest.raises(ValueError, match="REVIEW_REQUIRED"):
        rt_new.resume_agent_with_pi(
            run_id="run_admitted_crash",
            model_config=cfg([]),
            test_mode=True,
            timeout=15,
        )

    # Invariants: handler count still 1, budget not zeroed out, status marked INTERRUPTED
    assert handler_calls["count"] == 1
    rec = rt_new.get_run("run_admitted_crash")
    assert rec.status == "INTERRUPTED"
    assert rec.error_type == "REVIEW_REQUIRED"
    assert "call_admitted_1" in (rec.error_message or "")
    assert rec.budget_consumed == 1
    rt_new.close()


def test_lifecycle_idempotent_call_dedup_and_concurrency(tmp_path: Path):
    """Scenario 3: Call ID idempotency, cross-run/cross-param conflict isolation, and concurrent claim serialization."""
    calls = {"count": 0}

    class DummyTool(Tool):
        def __init__(self) -> None:
            super().__init__(name="dummy_tool", description="Dummy idempotency test tool")

        def get_parameters(self) -> list[ToolParameter]:
            return [
                ToolParameter(name="key", type="string", description="Key parameter", required=False)
            ]

        def run(self, *args, **kwargs):
            calls["count"] += 1
            return ToolResponse(ToolStatus.SUCCESS, "result_42")

    db_path = tmp_path / "lifecycle_idempotency.db"
    broker = ToolBroker(application_allowlist={"dummy_tool"})
    broker.register_tool(DummyTool())

    rt = AgentRuntime(db_path=db_path, tool_broker=broker)
    rt.start_run(run_id="run_idem_1", task_id="task_idem", budget_max=10)

    # 1. First execution
    r1 = rt.execute_tool(
        run_id="run_idem_1",
        tool_name="dummy_tool",
        parameters={"key": "val1"},
        call_id="call_idem_unique",
    )
    assert r1.status == "EXECUTED"
    assert r1.output_text == "result_42"
    assert calls["count"] == 1
    assert rt.get_run("run_idem_1").budget_consumed == 1

    # 2. Duplicate call with identical parameters -> return original record, no re-execution, no extra budget
    r1_dup = rt.execute_tool(
        run_id="run_idem_1",
        tool_name="dummy_tool",
        parameters={"key": "val1"},
        call_id="call_idem_unique",
    )
    assert r1_dup.status == "EXECUTED"
    assert r1_dup.output_text == "result_42"
    assert calls["count"] == 1
    assert rt.get_run("run_idem_1").budget_consumed == 1

    # 3. Conflicting run_id with same call_id -> REJECTED without overwriting original row or leaking output
    rt.start_run(run_id="run_idem_2", task_id="task_idem_2", budget_max=10)
    r_conflict_run = rt.execute_tool(
        run_id="run_idem_2",
        tool_name="dummy_tool",
        parameters={"key": "val1"},
        call_id="call_idem_unique",
    )
    assert r_conflict_run.status == "REJECTED"
    assert r_conflict_run.error_type == "IDEMPOTENCY_CONFLICT"
    assert r_conflict_run.output_text == ""
    assert calls["count"] == 1
    # Verify original row in DB is completely intact
    conn = rt._get_conn()
    row = conn.execute(
        "SELECT run_id, status, output_text FROM runtime_tool_calls WHERE call_id = 'call_idem_unique'"
    ).fetchone()
    assert row[0] == "run_idem_1"
    assert row[1] == "EXECUTED"
    assert row[2] == "result_42"

    # 4. Conflicting parameters with same call_id -> REJECTED without overwriting
    r_conflict_params = rt.execute_tool(
        run_id="run_idem_1",
        tool_name="dummy_tool",
        parameters={"key": "DIFFERENT_PARAM"},
        call_id="call_idem_unique",
    )
    assert r_conflict_params.status == "REJECTED"
    assert r_conflict_params.error_type == "IDEMPOTENCY_CONFLICT"
    assert calls["count"] == 1
    assert rt.get_run("run_idem_1").budget_consumed == 1

    # 5. Concurrent claim serialization with two Runtime connections using threading.Event
    start_event = threading.Event()
    proceed_event = threading.Event()
    conc_calls = {"count": 0}

    class ConcurrentTool(Tool):
        def __init__(self) -> None:
            super().__init__(name="conc_tool", description="Concurrent test tool")

        def get_parameters(self) -> list[ToolParameter]:
            return [
                ToolParameter(name="p", type="integer", description="P parameter", required=False)
            ]

        def run(self, *args, **kwargs):
            conc_calls["count"] += 1
            start_event.set()
            proceed_event.wait(timeout=5.0)
            return ToolResponse(ToolStatus.SUCCESS, "conc_ok")

    broker1 = ToolBroker(application_allowlist={"conc_tool"})
    broker1.register_tool(ConcurrentTool())
    broker2 = ToolBroker(application_allowlist={"conc_tool"})
    broker2.register_tool(ConcurrentTool())

    rt1 = AgentRuntime(db_path=db_path, tool_broker=broker1)
    rt2 = AgentRuntime(db_path=db_path, tool_broker=broker2)
    rt1.start_run(run_id="run_concurrent", task_id="task_conc", budget_max=5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            rt1.execute_tool,
            run_id="run_concurrent",
            tool_name="conc_tool",
            parameters={"p": 1},
            call_id="call_conc_shared",
        )
        assert start_event.wait(timeout=5.0)
        # rt1 has reserved ticket, inserted ADMITTED, and is in handler
        # rt2 attempts to claim same call_id concurrently
        r2 = rt2.execute_tool(
            run_id="run_concurrent",
            tool_name="conc_tool",
            parameters={"p": 1},
            call_id="call_conc_shared",
        )
        # rt2 must return ADMITTED in-flight without re-executing handler
        assert r2.status == "ADMITTED"
        assert conc_calls["count"] == 1

        proceed_event.set()
        r1 = f1.result(timeout=5.0)
        assert r1.status == "EXECUTED"
        assert r1.output_text == "conc_ok"

    assert conc_calls["count"] == 1
    rec_conc = rt1.get_run("run_concurrent")
    assert rec_conc.budget_consumed == 1

    rt.close()
    rt1.close()
    rt2.close()


def test_lifecycle_arun_checkpoint_rejects_contract_overrides(paused_factory, tmp_path: Path):
    """Scenario 4: arun with checkpoint strictly rejects contract overrides without spawning Pi."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]
    chk = e["checkpoint"]

    # 1. tools override
    with pytest.raises(ValueError, match="Cannot override.*tools"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                tools=["query_order_packages"],
                test_mode=True,
            )
        )

    # 2. candidate override
    with pytest.raises(ValueError, match="Cannot override.*candidate"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                candidate=e["candidate"],
                test_mode=True,
            )
        )

    # 3. goal override
    with pytest.raises(ValueError, match="Cannot override.*goal"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                goal="tampered goal",
                test_mode=True,
            )
        )

    # 4. intent_version override
    with pytest.raises(ValueError, match="Cannot override.*intent_version"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                intent_version="v99",
                test_mode=True,
            )
        )

    # 5. initial_scratchpad override
    with pytest.raises(ValueError, match="Cannot override.*initial_scratchpad"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                initial_scratchpad={"corrupt": True},
                test_mode=True,
            )
        )

    # 6. active_constraints override
    with pytest.raises(ValueError, match="Cannot override.*active_constraints"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                active_constraints=["no_side_effects"],
                test_mode=True,
            )
        )

    # 7. system_prompt_header override
    with pytest.raises(ValueError, match="Cannot override.*system_prompt_header"):
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=chk,
                system_prompt_header="injected header",
                test_mode=True,
            )
        )

    # Assertions: state remains PAUSED, no new PID spawned, no tool handler calls
    assert len(e["pids"]) == 1
    assert e["counts"] == {"order": 1, "track": 0}
    rec = rt.get_run(rid)
    assert rec.status == "PAUSED"
    assert rec.budget_consumed == 2
    assert rec.deadline_ts == e["deadline"]

    # Verify normal canonical recovery still passes cleanly
    answer = rt.resume_agent_with_pi(
        run_id=rid,
        model_config=cfg(remaining()),
        test_mode=True,
        context_compressor=e["compressor"],
        workspace_dir=tmp_path,
        timeout=20,
    )
    assert answer == FINAL
    assert len(e["pids"]) == 2
    assert e["counts"] == {"order": 1, "track": 2}
    rec_done = rt.get_run(rid)
    assert rec_done.budget_consumed == 7
    assert rec_done.deadline_ts == e["deadline"]
