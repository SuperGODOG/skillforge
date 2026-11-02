"""Comprehensive tests for SkillForge H1: Lifecycle Hook Decision & BeforeTool Interception.

Covers mandatory test specifications T1 - T6:
- T1: Legacy hook compatibility (returning None treated as allow, normal order).
- T2: Real Harness BeforeTool denial on read-only business constraint, handler_count=0, structured observation rejection.
- T3: Hook allow does not bypass Broker allowlist/schema check, handler_count=0, PERMISSION_DENIED recorded.
- T4: Critical hook fail-closed on exception/timeout vs observation hook fault-tolerance preserving main outcome & Episode.
- T5: Cancellation during hook priority (CancelledError not swallowed), sync hook timeout boundary.
- T6: Repeated denial respects budget_max and terminates cleanly; checkpoint and audit trail fully restorable on resume.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    HookAuditRecord,
    HookDecision,
    HookSpec,
    ModelResponse,
    ToolCallSpec,
    create_read_only_constraint_hook,
    create_sensitive_data_redaction_hook,
    create_business_oracle_finalize_hook,
    compose_hooks,
)
from skillforge.models import ToolCallRecord
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.context_engine.schemas import ProtectedState
from skillforge.context_engine.artifact_tool import InspectArtifactTool
from skillforge.episode import EpisodeStore
from skillforge.collector import ExperienceCollector
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)
from skillforge.supervisor import (
    SupervisorOrchestrator,
    SupervisorTaskSpec,
    WorkerAgentSpec,
    SupervisorDecision,
)


class ScriptedMockLLM:
    """Mock LLM returning sequential pre-programmed responses."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.call_history: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.call_history.append({"messages": messages, "tools": tools, "system": system})
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse(content="Default final answer", tool_calls=[])


# ==============================================================================
# T1: Legacy Hook Compatibility & None Return
# ==============================================================================

def test_t1_legacy_hook_compatibility_and_none_return(tmp_path: Path):
    """T1: Verify legacy hooks returning None are treated as allow, preserving original order."""
    async def _test():
        db_path = tmp_path / "sf_t1.db"
        broker = ToolBroker(application_allowlist={"calc_add"})

        handler_call_count = 0

        def calc_add(a: int, b: int) -> int:
            nonlocal handler_call_count
            handler_call_count += 1
            return a + b

        broker.register_tool(calc_add, name="calc_add")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        mock_llm = ScriptedMockLLM([
            ModelResponse(
                content="Computing 10 + 20",
                tool_calls=[ToolCallSpec(id="call_t1_1", name="calc_add", arguments={"a": 10, "b": 20})],
            ),
            ModelResponse(content="Sum is 30", tool_calls=[]),
        ])

        hook_order: list[str] = []

        # Legacy async and sync hooks returning None
        async def legacy_pre_model(state: HarnessState):
            hook_order.append("pre_model")

        def legacy_post_model(state: HarnessState, resp: ModelResponse):
            hook_order.append("post_model")

        async def legacy_pre_tool(state: HarnessState, tc: ToolCallSpec):
            hook_order.append(f"pre_tool_{tc.name}")
            return None  # explicit None return

        def legacy_post_tool(state: HarnessState, rec: ToolCallRecord):
            hook_order.append(f"post_tool_{rec.tool_name}_{rec.status}")

        async def legacy_task_end(state: HarnessState):
            hook_order.append(f"task_end_{state.status}")

        hooks = HarnessHooks(
            pre_model_call=legacy_pre_model,
            post_model_call=legacy_post_model,
            pre_tool_exec=legacy_pre_tool,
            post_tool_exec=legacy_post_tool,
            task_end=legacy_task_end,
        )

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace_t1",
        )

        spec = HarnessTaskSpec(
            task_id="task_t1",
            user_query="Please compute 10 + 20",
            budget_max=5,
        )

        final_state = await loop.run(spec)

        assert final_state.status == "COMPLETED"
        assert final_state.final_output == "Sum is 30"
        assert handler_call_count == 1
        assert len(final_state.tool_records) == 1
        assert final_state.tool_records[0].status == "EXECUTED"
        assert final_state.tool_records[0].output_text == "30"

        # Check sequence
        assert "pre_model" in hook_order
        assert "pre_tool_calc_add" in hook_order
        assert "post_tool_calc_add_EXECUTED" in hook_order
        assert "task_end_COMPLETED" in hook_order

        # Verify audit records populated
        pre_tool_audits = [a for a in final_state.hook_audits if a["event"] == "pre_tool_exec"]
        assert len(pre_tool_audits) == 1
        assert pre_tool_audits[0]["action"] == "allow"
        assert pre_tool_audits[0]["reason_code"] == "ALLOW_DEFAULT"

    asyncio.run(_test())


# ==============================================================================
# T2: Real Harness BeforeTool Denial on Read-Only Business Constraint
# ==============================================================================

def test_t2_before_tool_business_constraint_denial_real_loop(tmp_path: Path):
    """T2: Real Harness loop intercepts write action in read-only task: handler_count=0,
    model receives structured rejection observation, audit records denial."""
    async def _test():
        db_path = tmp_path / "sf_t2.db"
        broker = ToolBroker(application_allowlist={"query_order", "refund_order"})

        query_calls = 0
        refund_handler_calls = 0

        def query_order(order_id: str) -> str:
            nonlocal query_calls
            query_calls += 1
            return f"Order {order_id} active, amount 100"

        def refund_order(order_id: str, amount: float) -> str:
            nonlocal refund_handler_calls
            refund_handler_calls += 1
            return f"SUCCESS: Refunded {amount}"

        broker.register_tool(query_order, name="query_order")
        broker.register_tool(refund_order, name="refund_order")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Configured read-only constraint hook
        read_only_hook = create_read_only_constraint_hook(
            tool_effects={
                "refund_order": "side_effect",
                "query_order": "read",
            },
            hook_id="security_read_only_gate",
        )

        hooks = HarnessHooks(pre_tool_exec=read_only_hook)

        # Mock LLM:
        # Step 1: queries order
        # Step 2: tries to call refund_order (a write action)
        # Step 3: observes rejection and outputs explanation
        mock_llm = ScriptedMockLLM([
            ModelResponse(
                content="Looking up order status",
                tool_calls=[ToolCallSpec(id="call_q1", name="query_order", arguments={"order_id": "ORD_999"})],
            ),
            ModelResponse(
                content="Attempting refund",
                tool_calls=[ToolCallSpec(id="call_r1", name="refund_order", arguments={"order_id": "ORD_999", "amount": 100.0})],
            ),
            ModelResponse(
                content="Refund was denied by policy. Order remains unchanged.",
                tool_calls=[],
            ),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace_t2",
        )

        # Frozen task intent with READ_ONLY constraint
        protected = ProtectedState(
            intent_version="v1",
            current_goal="Query order details only",
            active_constraints=["READ_ONLY"],
        )

        spec = HarnessTaskSpec(
            task_id="task_readonly_audit",
            user_query="Check order ORD_999 and refund if needed",
            budget_max=6,
            initial_scratchpad={"protected_state": protected},
        )

        final_state = await loop.run(spec)

        # CRITICAL ASSERTIONS:
        # 1. query_order executed once
        assert query_calls == 1
        # 2. refund_order handler was NEVER executed!
        assert refund_handler_calls == 0

        # 3. Exactly 2 tool records: 1 EXECUTED, 1 REJECTED (HOOK_DENIED)
        assert len(final_state.tool_records) == 2
        assert final_state.tool_records[0].tool_name == "query_order"
        assert final_state.tool_records[0].status == "EXECUTED"

        denied_rec = final_state.tool_records[1]
        assert denied_rec.tool_name == "refund_order"
        assert denied_rec.status == "REJECTED"
        assert denied_rec.error_type == "HOOK_DENIED"
        assert denied_rec.error_message == "READ_ONLY_VIOLATION"

        # 4. Model received explicit observation in message history
        tool_obs_msgs = [m for m in final_state.messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_r1"]
        assert len(tool_obs_msgs) == 1
        assert "ERROR [PERMISSION_DENIED]: Tool 'refund_order' denied by business constraint hook (READ_ONLY_VIOLATION)" in tool_obs_msgs[0]["content"]

        # 5. Model completed with final conclusion
        assert final_state.status == "COMPLETED"
        assert "Refund was denied by policy" in (final_state.final_output or "")

        # 6. Audit trail contains denial record with evidence refs
        deny_audits = [a for a in final_state.hook_audits if a["action"] == "deny"]
        assert len(deny_audits) == 1
        assert deny_audits[0]["hook_id"] == "security_read_only_gate"
        assert deny_audits[0]["reason_code"] == "READ_ONLY_VIOLATION"
        assert any("tool:refund_order" in ref for ref in deny_audits[0]["evidence_refs"])
        assert any(f"task:{spec.task_id}" in ref for ref in deny_audits[0]["evidence_refs"])

    asyncio.run(_test())


# ==============================================================================
# T3: Hook Allow Does NOT Bypass Broker Permission
# ==============================================================================

def test_t3_hook_allow_does_not_bypass_broker_permission(tmp_path: Path):
    """T3: Even if BeforeTool Hook returns allow, Broker retains ultimate permission authority;
    unauthorized tools result in handler_count=0 and Broker PERMISSION_DENIED record."""
    async def _test():
        db_path = tmp_path / "sf_t3.db"
        # secret_exec is registered in broker tools, but NOT in application_allowlist!
        broker = ToolBroker(application_allowlist={"public_tool"})

        secret_calls = 0

        def secret_exec():
            nonlocal secret_calls
            secret_calls += 1
            return "pawned"

        broker.register_tool(secret_exec, name="secret_exec")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Hook unconditionally allows every call
        permissive_hook = HookSpec(
            callback=lambda state, tc: HookDecision.allow(reason_code="PERMISSIVE_BYPASS_ATTEMPT"),
            hook_id="permissive_hook",
            critical=True,
        )

        hooks = HarnessHooks(pre_tool_exec=permissive_hook)

        mock_llm = ScriptedMockLLM([
            ModelResponse(
                content="Attempting unauthorized secret call",
                tool_calls=[ToolCallSpec(id="c_sec", name="secret_exec", arguments={})],
            ),
            ModelResponse(content="Done", tool_calls=[]),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace_t3",
        )

        spec = HarnessTaskSpec(
            task_id="task_t3_broker_check",
            user_query="Call secret_exec",
            budget_max=3,
        )

        final_state = await loop.run(spec)

        # Handler was NOT invoked
        assert secret_calls == 0

        # Recorded as Broker PERMISSION_DENIED
        assert len(final_state.tool_records) == 1
        rec = final_state.tool_records[0]
        assert rec.status == "REJECTED"
        assert rec.error_type == "PERMISSION_DENIED"
        assert "not authorized by application policy" in (rec.error_message or "")

        # Verify SQLite runtime audit recorded Broker rejection
        saved_calls = runtime.list_tool_calls(spec.run_id or final_state.run_id)
        assert len(saved_calls) == 1
        assert saved_calls[0].error_type == "PERMISSION_DENIED"

    asyncio.run(_test())


# ==============================================================================
# T4: Critical Hook Fail-Closed vs Observation Hook Fault Tolerance
# ==============================================================================

def test_t4_critical_hook_failclosed_and_observation_fault_tolerance(tmp_path: Path):
    """T4:
    - Critical hook exception or timeout fail-closed: handler_count=0, action=deny, error recorded.
    - Observation hook (post_tool_exec, task_end) exception does NOT crash main run or Episode.
    """
    async def _test():
        # --- Part A: Critical Hook Exception Fail-Closed ---
        db_path_a = tmp_path / "sf_t4_a.db"
        broker_a = ToolBroker(application_allowlist={"action_tool"})
        handler_a_calls = 0

        def action_tool():
            nonlocal handler_a_calls
            handler_a_calls += 1
            return "action_ok"

        broker_a.register_tool(action_tool, name="action_tool")
        runtime_a = AgentRuntime(db_path=db_path_a, tool_broker=broker_a)

        async def exploding_critical_hook(state: HarnessState, tc: ToolCallSpec):
            raise RuntimeError("Database connection died during security inspection")

        crit_hook = HookSpec(
            callback=exploding_critical_hook,
            hook_id="exploding_guard",
            critical=True,
        )

        loop_a = AsyncHarnessLoop(
            runtime=runtime_a,
            tool_broker=broker_a,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_crit", name="action_tool", arguments={})]),
                ModelResponse(content="Handled error", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=crit_hook),
            workspace_dir=tmp_path / "workspace_t4_a",
        )

        state_a = await loop_a.run(HarnessTaskSpec(task_id="t4_a", user_query="Run action", budget_max=3))

        # Fail-closed: handler was NOT called
        assert handler_a_calls == 0
        assert len(state_a.tool_records) == 1
        assert state_a.tool_records[0].status == "REJECTED"
        assert state_a.tool_records[0].error_type == "HOOK_DENIED"
        assert state_a.tool_records[0].error_message == "HOOK_EXECUTION_ERROR"

        audit_a = [a for a in state_a.hook_audits if a["event"] == "pre_tool_exec"][0]
        assert audit_a["action"] == "deny"
        assert audit_a["reason_code"] == "HOOK_EXECUTION_ERROR"
        assert "Database connection died" in (audit_a["error"] or "")

        # --- Part B: Observation Hook Exception Does Not Destroy Main Run or Episode ---
        db_path_b = tmp_path / "sf_t4_b.db"
        broker_b = ToolBroker(application_allowlist={"safe_tool"})
        handler_b_calls = 0

        def safe_tool():
            nonlocal handler_b_calls
            handler_b_calls += 1
            return "safe_success"

        broker_b.register_tool(safe_tool, name="safe_tool")
        ep_store = EpisodeStore(db_path=db_path_b)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime_b = AgentRuntime(db_path=db_path_b, tool_broker=broker_b, collector=collector)

        def buggy_post_tool_hook(state: HarnessState, rec: ToolCallRecord):
            raise ValueError("Telemetry telemetry buffer overflow")

        def buggy_task_end_hook(state: HarnessState):
            raise KeyError("Missing terminal metric key")

        obs_hooks = HarnessHooks(
            post_tool_exec=HookSpec(callback=buggy_post_tool_hook, hook_id="noisy_telemetry", critical=False),
            task_end=HookSpec(callback=buggy_task_end_hook, hook_id="noisy_cleaner", critical=False),
        )

        loop_b = AsyncHarnessLoop(
            runtime=runtime_b,
            tool_broker=broker_b,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_safe", name="safe_tool", arguments={})]),
                ModelResponse(content="Final normal conclusion", tool_calls=[]),
            ]),
            hooks=obs_hooks,
            workspace_dir=tmp_path / "workspace_t4_b",
        )

        state_b = await loop_b.run(HarnessTaskSpec(
            task_id="t4_b",
            user_query="Run safe tool",
            budget_max=3,
            verification_fn=lambda s: {"independent_pass": True, "source": "code_oracle"},
        ))

        # Main outcome is completely intact!
        assert handler_b_calls == 1
        assert state_b.status == "COMPLETED"
        assert state_b.final_output == "Final normal conclusion"
        assert len(state_b.tool_records) == 1
        assert state_b.tool_records[0].status == "EXECUTED"

        # Observation errors are inspectable in audits
        err_audits = [a for a in state_b.hook_audits if a["error"] is not None]
        assert len(err_audits) == 2
        assert any("telemetry buffer overflow" in a["error"] for a in err_audits)
        assert any("Missing terminal metric key" in a["error"] for a in err_audits)

        # Episode is preserved in EpisodeStore
        ep = ep_store.get_episode(f"ep_{state_b.run_id}")
        assert ep is not None
        assert ep.outcome == "success"

    asyncio.run(_test())


# ==============================================================================
# T5: Cancellation During Hook & Sync Timeout Boundary
# ==============================================================================

def test_t5_cancellation_during_hook_and_sync_timeout_boundary(tmp_path: Path):
    """T5:
    - Cancellation while awaiting async hook aborts immediately, does not dispatch tools.
    - Sync hook timeout fail-closed; late return does not modify state.
    """
    async def _test():
        # --- Part A: Cancellation While Awaiting Hook ---
        db_path_a = tmp_path / "sf_t5_a.db"
        broker_a = ToolBroker(application_allowlist={"slow_tool"})
        tool_a_calls = 0

        def slow_tool():
            nonlocal tool_a_calls
            tool_a_calls += 1
            return "done"

        broker_a.register_tool(slow_tool, name="slow_tool")
        runtime_a = AgentRuntime(db_path=db_path_a, tool_broker=broker_a)

        hook_started = asyncio.Event()

        async def slow_pre_tool_hook(state: HarnessState, tc: ToolCallSpec):
            hook_started.set()
            await asyncio.sleep(2.0)  # Waiting
            return HookDecision.allow()

        loop_a = AsyncHarnessLoop(
            runtime=runtime_a,
            tool_broker=broker_a,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_s", name="slow_tool", arguments={})]),
                ModelResponse(content="Final", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=slow_pre_tool_hook),
            workspace_dir=tmp_path / "workspace_t5_a",
        )

        async def cancel_while_in_hook():
            await hook_started.wait()
            await asyncio.sleep(0.05)
            await loop_a.cancel("Supervisor abort in hook")

        t_exec = asyncio.create_task(loop_a.run(HarnessTaskSpec(task_id="t5_a", user_query="Do slow", budget_max=3)))
        t_canc = asyncio.create_task(cancel_while_in_hook())

        res_state, _ = await asyncio.gather(t_exec, t_canc)

        assert res_state.status == "CANCELLED"
        assert "Supervisor abort in hook" in (res_state.error_message or "")
        # Tool was never executed
        assert tool_a_calls == 0

        # --- Part B: Sync Hook Timeout Boundary ---
        db_path_b = tmp_path / "sf_t5_b.db"
        broker_b = ToolBroker(application_allowlist={"my_tool"})
        my_tool_calls = 0

        def my_tool():
            nonlocal my_tool_calls
            my_tool_calls += 1
            return "ok"

        broker_b.register_tool(my_tool, name="my_tool")
        runtime_b = AgentRuntime(db_path=db_path_b, tool_broker=broker_b)

        sync_late_mutated = False

        def hanging_sync_hook(state: HarnessState, tc: ToolCallSpec):
            nonlocal sync_late_mutated
            time.sleep(0.3)
            # Try late mutation (should be completely inert because caller timed out and continues)
            sync_late_mutated = True
            return HookDecision.allow(reason_code="LATE_RETURN")

        timed_hook = HookSpec(
            callback=hanging_sync_hook,
            hook_id="hanging_sync",
            timeout_seconds=0.08,  # strict 80ms timeout
            critical=True,
        )

        loop_b = AsyncHarnessLoop(
            runtime=runtime_b,
            tool_broker=broker_b,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_b", name="my_tool", arguments={})]),
                ModelResponse(content="Handled timeout", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=timed_hook),
            workspace_dir=tmp_path / "workspace_t5_b",
        )

        state_b = await loop_b.run(HarnessTaskSpec(task_id="t5_b", user_query="Run with sync timeout", budget_max=3))

        # Tool was NOT called due to timeout fail-closed
        assert my_tool_calls == 0
        assert len(state_b.tool_records) == 1
        assert state_b.tool_records[0].status == "REJECTED"
        assert state_b.tool_records[0].error_message == "HOOK_TIMEOUT"

        audit_b = [a for a in state_b.hook_audits if a["event"] == "pre_tool_exec"][0]
        assert audit_b["action"] == "deny"
        assert audit_b["reason_code"] == "HOOK_TIMEOUT"
        assert audit_b["is_timeout"] is True

    asyncio.run(_test())


# ==============================================================================
# T6: Repeated Denial Step Bounded & Checkpoint Resume Audit
# ==============================================================================

def test_t6_repeated_denial_step_bounded_and_checkpoint_resume_audit(tmp_path: Path):
    """T6: Repeated denials terminate at budget_max; on-disk checkpoint and audit trail
    are fully preserved across reopen and resume."""
    async def _test():
        db_path = tmp_path / "sf_t6.db"
        broker = ToolBroker(application_allowlist={"forbidden_tool", "safe_step_tool"})

        broker.register_tool(lambda: "forbidden", name="forbidden_tool")
        broker.register_tool(lambda: "safe", name="safe_step_tool")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Pre-tool hook unconditionally denies forbidden_tool
        def deny_forbidden(state: HarnessState, tc: ToolCallSpec):
            if tc.name == "forbidden_tool":
                return HookDecision.deny(
                    reason_code="STRICT_FORBIDDEN_TOOL",
                    details={"tool": tc.name},
                    evidence_refs=[f"run:{state.run_id}", f"step:{state.step_count}"],
                )
            return HookDecision.allow()

        hooks = HarnessHooks(pre_tool_exec=deny_forbidden)

        # Loop that keeps calling forbidden_tool indefinitely
        repeated_llm = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id=f"c_{i}", name="forbidden_tool", arguments={})])
            for i in range(10)
        ])

        run_id = "run_t6_audit_resume"
        workspace = tmp_path / "workspace_t6"

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=repeated_llm,
            hooks=hooks,
            workspace_dir=workspace,
        )

        spec = HarnessTaskSpec(
            task_id="task_t6",
            user_query="Call forbidden repeatedly",
            run_id=run_id,
            budget_max=3,  # Strict budget
            intent_version="v1",
        )

        state1 = await loop.run(spec)

        # Must be bounded by budget_max
        assert state1.status == "BUDGET_EXHAUSTED"
        assert state1.step_count == 3
        assert len(state1.tool_records) == 3
        assert all(r.status == "REJECTED" and r.error_type == "HOOK_DENIED" for r in state1.tool_records)

        # Checkpoint file on disk verification
        chk_file = workspace / "runs" / run_id / "checkpoint.json"
        assert chk_file.exists()

        chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
        assert chk_data["step_count"] == 3
        assert chk_data["status"] == "BUDGET_EXHAUSTED"
        assert len(chk_data["hook_audits"]) >= 3

        # Verify audits in checkpoint
        deny_audits = [a for a in chk_data["hook_audits"] if a["action"] == "deny"]
        assert len(deny_audits) == 3
        assert all(a["reason_code"] == "STRICT_FORBIDDEN_TOOL" for a in deny_audits)

        # --- Resume from Checkpoint ---
        # Provide an updated spec with budget_max=5 and safe tool response
        resume_llm = ScriptedMockLLM([
            ModelResponse(content="Calling safe tool now", tool_calls=[ToolCallSpec(id="c_safe_res", name="safe_step_tool", arguments={})]),
            ModelResponse(content="Task completed safely after resume", tool_calls=[]),
        ])

        loop_resumed = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=resume_llm,
            hooks=hooks,
            workspace_dir=workspace,
        )

        updated_spec = HarnessTaskSpec(
            task_id="task_t6",
            user_query="Recover and complete",
            run_id=run_id,
            budget_max=6,
            intent_version="v2",
        )

        state2 = await loop_resumed.resume(run_id=run_id, updated_spec=updated_spec)

        assert state2.status == "COMPLETED"
        assert state2.final_output == "Task completed safely after resume"
        # Total steps advanced from 3 to 4
        assert state2.step_count == 4
        # Tool records: 3 rejections + 1 executed safe tool
        assert len(state2.tool_records) == 4
        assert state2.tool_records[3].status == "EXECUTED"
        assert state2.tool_records[3].tool_name == "safe_step_tool"

        # Previous 3 denial audits are faithfully preserved, plus new allow audit!
        assert len(state2.hook_audits) >= 4
        assert len([a for a in state2.hook_audits if a["action"] == "deny"]) == 3

    asyncio.run(_test())


# ==============================================================================
# H2A Group A: BeforeFinalize Verification Oracle Gate
# ==============================================================================

def test_h2a_group_a_before_finalize_oracle_fulfillment_pass(tmp_path: Path):
    """H2A-A1: Fully queried and delivered packages pass BeforeFinalize Oracle.
    Delivers answer, caches verification evidence without duplicate execution,
    and archives exactly one success Episode.
    """
    async def _test():
        db_path = tmp_path / "sf_h2a_a1.db"
        broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
        broker.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
        broker.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")

        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

        oracle_eval_count = 0

        def counting_oracle(**kwargs):
            nonlocal oracle_eval_count
            oracle_eval_count += 1
            return verify_logistics_fulfillment(**kwargs)

        finalize_hook = create_business_oracle_finalize_hook(
            oracle_fn=counting_oracle,
            order_id="ORD_2026_0901",
            hook_id="logistics_fulfillment_oracle",
            critical=True,
        )

        mock_llm = ScriptedMockLLM([
            ModelResponse(
                content="Checking order packages",
                tool_calls=[ToolCallSpec(id="c_pkgs", name="query_order_packages", arguments={"order_id": "ORD_2026_0901"})],
            ),
            ModelResponse(
                content="Checking package 1",
                tool_calls=[ToolCallSpec(id="c_pkg1", name="query_package_tracking", arguments={"package_id": "PKG_101"})],
            ),
            ModelResponse(
                content="Checking package 2",
                tool_calls=[ToolCallSpec(id="c_pkg2", name="query_package_tracking", arguments={"package_id": "PKG_102"})],
            ),
            ModelResponse(
                content="订单 ORD_2026_0901 的两个包裹 PKG_101 与 PKG_102 均已签收送达。",
                tool_calls=[],
            ),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=HarnessHooks(before_finalize=finalize_hook),
            workspace_dir=tmp_path / "workspace_h2a_a1",
        )

        spec = HarnessTaskSpec(
            task_id="task_h2a_a1",
            user_query="Query status of ORD_2026_0901",
            budget_max=6,
        )

        state = await loop.run(spec)

        # 1. Delivery succeeds
        assert state.status == "COMPLETED"
        assert "均已签收送达" in (state.final_output or "")

        # 2. BeforeFinalize evaluated Oracle and cached evidence (only evaluated once!)
        assert oracle_eval_count == 1
        fin_audits = [a for a in state.hook_audits if a["event"] == "before_finalize"]
        assert len(fin_audits) == 1
        assert fin_audits[0]["action"] == "allow"
        assert fin_audits[0]["reason_code"] == "ORACLE_VERIFIED_PASS"

        # 3. Exactly one Episode created in EpisodeStore with verified success
        ep = ep_store.get_episode(f"ep_{state.run_id}")
        assert ep is not None
        assert ep.outcome == "success"
        assert ep.verification_evidence["independent_pass"] is True

    asyncio.run(_test())


def test_h2a_group_a_before_finalize_hallucination_denied_delivery_blocked(tmp_path: Path):
    """H2A-A2: Premature claim of 'all delivered' when packages are in-transit/unqueried
    is denied by BeforeFinalize. Public delivery is blocked, proposed answer retained in
    audit/scratchpad, and Episode is marked failure.
    """
    async def _test():
        db_path = tmp_path / "sf_h2a_a2.db"
        broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
        broker.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
        broker.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")

        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

        finalize_hook = create_business_oracle_finalize_hook(
            oracle_fn=verify_logistics_fulfillment,
            order_id="ORD_2026_0902",
            hook_id="logistics_fulfillment_oracle",
            critical=True,
        )

        # ORD_2026_0902 has PKG_201 (DELIVERED) and PKG_202 (IN_TRANSIT).
        # Model queries PKG_201 and omits PKG_202, yet falsely claims all delivered.
        mock_llm = ScriptedMockLLM([
            ModelResponse(
                content="Checking order",
                tool_calls=[ToolCallSpec(id="c_pkgs", name="query_order_packages", arguments={"order_id": "ORD_2026_0902"})],
            ),
            ModelResponse(
                content="Checking PKG_201 only",
                tool_calls=[ToolCallSpec(id="c_pkg1", name="query_package_tracking", arguments={"package_id": "PKG_201"})],
            ),
            ModelResponse(
                content="订单 ORD_2026_0902 全部包裹已签收送达，请放心。",
                tool_calls=[],
            ),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=HarnessHooks(before_finalize=finalize_hook),
            workspace_dir=tmp_path / "workspace_h2a_a2",
        )

        spec = HarnessTaskSpec(
            task_id="task_h2a_a2",
            user_query="Query status of ORD_2026_0902",
            budget_max=6,
        )

        state = await loop.run(spec)

        # 1. Delivery blocked: state marked FAILED
        assert state.status == "FAILED"
        assert "[DELIVERY_BLOCKED]: ORACLE_HALLUCINATION_DETECTED" in (state.final_output or "")

        # 2. Proposed output retained in scratchpad for debugging
        assert "全部包裹已签收送达" in state.working_scratchpad.get("proposed_output", "")

        # 3. Audit trail records denial
        fin_audits = [a for a in state.hook_audits if a["event"] == "before_finalize"]
        assert len(fin_audits) == 1
        assert fin_audits[0]["action"] == "deny"
        assert fin_audits[0]["reason_code"] == "ORACLE_HALLUCINATION_DETECTED"

        # 4. Episode is NOT success
        ep = ep_store.get_episode(f"ep_{state.run_id}")
        assert ep is not None
        assert ep.outcome == "failure"
        assert ep.verification_evidence["is_hallucination"] is True

    asyncio.run(_test())


def test_h2a_group_a_generic_without_oracle_unknown_and_failclosed(tmp_path: Path):
    """H2A-A3:
    - Generic task without Oracle delivers output, but Collector records outcome='unknown'.
    - Honest tool failure reporting is accepted as qualified pass.
    - Critical hook exception fails closed with DELIVERY_BLOCKED.
    """
    async def _test():
        # --- Part 1: Generic task without Oracle ---
        db_path_1 = tmp_path / "sf_h2a_a3_1.db"
        broker_1 = ToolBroker(application_allowlist={"echo_tool"})
        broker_1.register_tool(lambda text: f"Echo: {text}", name="echo_tool")
        ep_store_1 = EpisodeStore(db_path=db_path_1)
        collector_1 = ExperienceCollector(episode_store=ep_store_1)
        runtime_1 = AgentRuntime(db_path=db_path_1, tool_broker=broker_1, collector=collector_1)

        loop_1 = AsyncHarnessLoop(
            runtime=runtime_1,
            tool_broker=broker_1,
            llm_client=ScriptedMockLLM([ModelResponse(content="2 + 2 = 4", tool_calls=[])]),
            workspace_dir=tmp_path / "ws_h2a_a3_1",
        )

        state_1 = await loop_1.run(HarnessTaskSpec(task_id="t_gen", user_query="What is 2+2?"))
        assert state_1.status == "COMPLETED"
        assert state_1.final_output == "2 + 2 = 4"

        # Collector does NOT falsify success; without independent verification evidence it is unknown
        ep_1 = ep_store_1.get_episode(f"ep_{state_1.run_id}")
        assert ep_1 is not None
        assert ep_1.outcome == "unknown"

        # --- Part 2: Critical BeforeFinalize hook exception fails closed ---
        db_path_2 = tmp_path / "sf_h2a_a3_2.db"
        broker_2 = ToolBroker(application_allowlist={"dummy"})
        broker_2.register_tool(lambda: "ok", name="dummy")
        runtime_2 = AgentRuntime(db_path=db_path_2, tool_broker=broker_2)

        async def exploding_finalizer(state: HarnessState, proposed: str):
            raise ConnectionError("Oracle service unreachable")

        crit_finalize = HookSpec(callback=exploding_finalizer, hook_id="exploding_oracle", critical=True)

        loop_2 = AsyncHarnessLoop(
            runtime=runtime_2,
            tool_broker=broker_2,
            llm_client=ScriptedMockLLM([ModelResponse(content="Result ready", tool_calls=[])]),
            hooks=HarnessHooks(before_finalize=crit_finalize),
            workspace_dir=tmp_path / "ws_h2a_a3_2",
        )

        state_2 = await loop_2.run(HarnessTaskSpec(task_id="t_crit", user_query="Do task"))
        assert state_2.status == "FAILED"
        assert "[DELIVERY_BLOCKED]: HOOK_EXECUTION_ERROR" in (state_2.final_output or "")

    asyncio.run(_test())


# ==============================================================================
# H2A Group B: Review Pause & Terminal Hook Audit Persistence
# ==============================================================================

def test_h2a_group_b_review_pause_no_premature_episode_resume_preserves_tools(tmp_path: Path):
    """H2A-B1: Pre-tool REVIEW pauses execution without sealing Episode.
    Resume continues from checkpoint without duplicate tool executions.
    """
    async def _test():
        db_path = tmp_path / "sf_h2a_b1.db"
        broker = ToolBroker(application_allowlist={"step1_tool", "step2_tool"})

        step1_calls = 0
        step2_calls = 0

        def step1_tool():
            nonlocal step1_calls
            step1_calls += 1
            return "step1_success"

        def step2_tool():
            nonlocal step2_calls
            step2_calls += 1
            return "step2_success"

        broker.register_tool(step1_tool, name="step1_tool")
        broker.register_tool(step2_tool, name="step2_tool")

        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

        # Hook triggers review on step2_tool
        def gate_step2(state: HarnessState, tc: ToolCallSpec):
            if tc.name == "step2_tool":
                return HookDecision.review(reason_code="REQUIRE_HUMAN_APPROVAL")
            return HookDecision.allow()

        hooks1 = HarnessHooks(pre_tool_exec=gate_step2)
        workspace = tmp_path / "ws_h2a_b1"
        run_id = "run_review_resume_test"

        mock_llm_1 = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="step1_tool", arguments={})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="step2_tool", arguments={})]),
        ])

        loop_1 = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm_1,
            hooks=hooks1,
            workspace_dir=workspace,
        )

        spec = HarnessTaskSpec(
            task_id="task_rev_test",
            user_query="Run two step workflow",
            run_id=run_id,
            budget_max=5,
        )

        state1 = await loop_1.run(spec)

        # 1. State is PAUSED_FOR_REVIEW
        assert state1.status == "PAUSED_FOR_REVIEW"
        assert step1_calls == 1
        assert step2_calls == 0

        # 2. Checkpoint saved with PAUSED_FOR_REVIEW
        chk_file = workspace / "runs" / run_id / "checkpoint.json"
        assert chk_file.exists()
        chk = json.loads(chk_file.read_text(encoding="utf-8"))
        assert chk["status"] == "PAUSED_FOR_REVIEW"

        # 3. Crucial: Episode was NOT prematurely sealed!
        assert ep_store.get_episode(f"ep_{run_id}") is None

        # --- Phase 2: Resume with approved hook ---
        hooks2 = HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.allow())
        mock_llm_2 = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2_res", name="step2_tool", arguments={})]),
            ModelResponse(content="All steps complete", tool_calls=[]),
        ])

        loop_2 = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm_2,
            hooks=hooks2,
            workspace_dir=workspace,
        )

        state2 = await loop_2.resume(run_id=run_id)

        assert state2.status == "COMPLETED"
        assert state2.final_output == "All steps complete"
        # step1 was NOT duplicated!
        assert step1_calls == 1
        assert step2_calls == 1

        # Now Episode is sealed
        ep = ep_store.get_episode(f"ep_{run_id}")
        assert ep is not None

    asyncio.run(_test())


def test_h2a_group_b_task_end_error_preserves_main_and_persists_audit(tmp_path: Path):
    """H2A-B2: task_end observer hook exception does not corrupt final output or Episode.
    The task_end audit record (with error) is faithfully persisted to checkpoint.json.
    """
    async def _test():
        db_path = tmp_path / "sf_h2a_b2.db"
        broker = ToolBroker(application_allowlist={"action"})
        broker.register_tool(lambda: "done", name="action")
        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

        def buggy_task_end(state: HarnessState):
            raise RuntimeError("Cleanup service transient fault")

        workspace = tmp_path / "ws_h2a_b2"
        run_id = "run_task_end_audit_test"

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_act", name="action", arguments={})]),
                ModelResponse(content="Workflow succeeded", tool_calls=[]),
            ]),
            hooks=HarnessHooks(task_end=HookSpec(callback=buggy_task_end, hook_id="faulty_cleaner", critical=False)),
            workspace_dir=workspace,
        )

        state = await loop.run(HarnessTaskSpec(
            task_id="t_b2",
            user_query="Run workflow",
            run_id=run_id,
            budget_max=4,
            verification_fn=lambda s: {"independent_pass": True},
        ))

        assert state.status == "COMPLETED"
        assert state.final_output == "Workflow succeeded"

        # Episode is preserved
        ep = ep_store.get_episode(f"ep_{run_id}")
        assert ep is not None
        assert ep.outcome == "success"

        # Checkpoint contains the task_end audit record with error
        chk_file = workspace / "runs" / run_id / "checkpoint.json"
        chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
        task_end_audits = [a for a in chk_data["hook_audits"] if a["event"] == "task_end"]
        assert len(task_end_audits) == 1
        assert "Cleanup service transient fault" in (task_end_audits[0]["error"] or "")

    asyncio.run(_test())


def test_h2a_group_b_tool_denials_recorded_in_sqlite_and_tool_records(tmp_path: Path):
    """H2A-B3: Both BeforeTool Hook denials and ToolBroker permission rejections
    are persisted to runtime SQLite and state.tool_records with explicit status/error.
    """
    async def _test():
        db_path = tmp_path / "sf_h2a_b3.db"
        # ToolBroker only allows allowed_tool
        broker = ToolBroker(application_allowlist={"allowed_tool", "hook_denied_tool"})
        broker.register_tool(lambda: "ok1", name="allowed_tool")
        broker.register_tool(lambda: "ok2", name="hook_denied_tool")
        # broker_denied_tool is NOT in allowlist
        broker.register_tool(lambda: "secret", name="broker_denied_tool")

        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        def deny_hook_tool(state: HarnessState, tc: ToolCallSpec):
            if tc.name == "hook_denied_tool":
                return HookDecision.deny(reason_code="HOOK_SECURITY_DENIAL")
            return HookDecision.allow()

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[
                    ToolCallSpec(id="c1", name="hook_denied_tool", arguments={}),
                    ToolCallSpec(id="c2", name="broker_denied_tool", arguments={}),
                ]),
                ModelResponse(content="Finalized after denials", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=deny_hook_tool),
            workspace_dir=tmp_path / "ws_h2a_b3",
        )

        state = await loop.run(HarnessTaskSpec(task_id="t_b3", user_query="Test denials", budget_max=4))

        # Check tool records in state
        assert len(state.tool_records) == 2
        r1, r2 = state.tool_records[0], state.tool_records[1]

        assert r1.status == "REJECTED"
        assert r1.error_type == "HOOK_DENIED"
        assert r1.error_message == "HOOK_SECURITY_DENIAL"

        assert r2.status == "REJECTED"
        assert r2.error_type == "PERMISSION_DENIED"

        # Check SQLite persistence
        db_records = runtime.list_tool_calls(state.run_id)
        assert len(db_records) == 2
        assert any(r.error_type == "HOOK_DENIED" for r in db_records)
        assert any(r.error_type == "PERMISSION_DENIED" for r in db_records)

    asyncio.run(_test())


# ==============================================================================
# H2B: AfterTool Safe View Offload, Supervisor Composition, & Robustness
# ==============================================================================

def test_h2b_aftertool_safe_observation_view_and_large_artifact_drilldown(tmp_path: Path):
    """H2B-1: AfterTool (post_tool_exec) generates sanitized observation_view.
    Large outputs offload to disk using the redacted view; model InspectArtifactTool
    can drill down without raw secrets entering messages or drill-down returns.
    Raw records and SQLite DB faithfully retain original unredacted text for audit.
    Critical hook errors fail closed without raw fallback.
    """
    async def _test():
        db_path = tmp_path / "sf_h2b_1.db"
        broker = ToolBroker(application_allowlist={"fetch_sensitive_dossier", "inspect_artifact"})

        # Large synthetic customer dossier with emails and auth tokens
        raw_customers = [
            {
                "id": f"CUST_{i:03d}",
                "name": f"User_{i}",
                "customer_email": f"customer_{i}_secret@synthetic-corp.com",
                "token": f"AUTH_SECRET_KEY_{i:04d}",
                "notes": "Large payload padding " * 8,
            }
            for i in range(25)
        ]

        def fetch_sensitive_dossier() -> dict[str, Any]:
            return {"total": len(raw_customers), "customers": raw_customers}

        broker.register_tool(fetch_sensitive_dossier, name="fetch_sensitive_dossier")
        workspace_dir = tmp_path / "ws_h2b_1"
        broker.register_tool(InspectArtifactTool(allowed_base_dir=workspace_dir), name="inspect_artifact")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        redaction_hook = create_sensitive_data_redaction_hook(
            redact_patterns={
                r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+": "[REDACTED_EMAIL]",
                r"AUTH_SECRET_KEY_\d+": "[REDACTED_TOKEN]",
            },
            filter_keys=["customer_email", "token"],
            hook_id="security_sanitizer",
            critical=True,
        )

        hooks = HarnessHooks(post_tool_exec=redaction_hook)

        # Dynamic mock LLM:
        # Step 1: calls fetch_sensitive_dossier
        # Step 2: reads the offloaded artifact ref from message and calls inspect_artifact for CUST_001
        # Step 3: finishes with answer
        class DrilldownMockLLM:
            def __init__(self):
                self.step = 0
                self.inspected_content = ""

            async def ainvoke(self, messages, tools, system=""):
                self.step += 1
                if self.step == 1:
                    return ModelResponse(
                        content="Fetching customer dossier",
                        tool_calls=[ToolCallSpec(id="c_fetch", name="fetch_sensitive_dossier", arguments={})],
                    )
                elif self.step == 2:
                    # Parse artifact ref from last tool message
                    last_tool_msg = [m for m in messages if m.get("role") == "tool"][-1]["content"]
                    # Extract fs:// URI
                    import re
                    m_uri = re.search(r"fs://[^\s\n\"',]+", last_tool_msg)
                    artifact_uri = m_uri.group(0) if m_uri else ""
                    return ModelResponse(
                        content="Inspecting CUST_001",
                        tool_calls=[ToolCallSpec(id="c_inspect", name="inspect_artifact", arguments={"ref_id": artifact_uri, "object_id": "CUST_001"})],
                    )
                else:
                    self.inspected_content = [m for m in messages if m.get("role") == "tool"][-1]["content"]
                    return ModelResponse(content="Drilldown complete", tool_calls=[])

        mock_llm = DrilldownMockLLM()
        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=workspace_dir,
        )

        state = await loop.run(HarnessTaskSpec(task_id="t_h2b_1", user_query="Audit customer data", budget_max=5))

        assert state.status == "COMPLETED"
        assert len(state.tool_records) == 2

        # 1. Check tool records in state: raw record retains original secrets!
        fetch_rec = state.tool_records[0]
        assert fetch_rec.status == "EXECUTED"
        raw_dump = json.dumps(fetch_rec.output_data)
        assert "customer_1_secret@synthetic-corp.com" in raw_dump
        assert "AUTH_SECRET_KEY_0001" in raw_dump

        # 2. Check SQLite runtime storage: DB retains raw original text for authoritative auditing!
        db_calls = runtime.list_tool_calls(state.run_id)
        assert len(db_calls) == 2
        db_fetch = [c for c in db_calls if c.tool_name == "fetch_sensitive_dossier"][0]
        assert "customer_1_secret@synthetic-corp.com" in json.dumps(db_fetch.output_data)

        # 3. Check model messages: Raw secret NEVER enters model messages!
        for msg in state.messages:
            msg_str = json.dumps(msg, ensure_ascii=False)
            assert "customer_1_secret@synthetic-corp.com" not in msg_str
            assert "AUTH_SECRET_KEY_0001" not in msg_str

        # 4. Check inspect_artifact drill-down return: contains sanitized data, NOT raw secrets!
        assert "CUST_001" in mock_llm.inspected_content
        assert "[REDACTED]" in mock_llm.inspected_content or "[REDACTED_EMAIL]" in mock_llm.inspected_content
        assert "customer_1_secret@synthetic-corp.com" not in mock_llm.inspected_content

        # 5. Security Boundary (F2): Model attempting to bypass manifest authorization fails
        # 5a. Attempting to inspect checkpoint.json directly:
        tool_inst = broker.get_tool("inspect_artifact")
        chk_path = workspace_dir / "runs" / state.run_id / "checkpoint.json"
        res_bypass = tool_inst.run({"ref_id": str(chk_path)})
        assert res_bypass.status.value == "error"
        assert res_bypass.error_info.get("code") == "UNAUTHORIZED_ARTIFACT"

        # 5b. Attempting cross-run artifact access inside workspace:
        other_run_file = workspace_dir / "runs" / "other_run_id" / "artifact.json"
        other_run_file.parent.mkdir(parents=True, exist_ok=True)
        other_run_file.write_text('{"secret": "leak"}', encoding="utf-8")
        res_cross = tool_inst.run({"ref_id": str(other_run_file)})
        assert res_cross.status.value == "error"
        assert res_cross.error_info.get("code") == "UNAUTHORIZED_ARTIFACT"

        # --- Part B: Critical Hook Error Fail-Closed & Sensitive Exception Isolation (F4) ---
        def exploding_sanitizer(state: HarnessState, rec: ToolCallRecord):
            raise ValueError("PII parser crashed on secret token AUTH_SECRET_KEY_9999")

        crit_hooks = HarnessHooks(post_tool_exec=HookSpec(callback=exploding_sanitizer, hook_id="crit_sanitizer", critical=True))
        loop_crit = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_err", name="fetch_sensitive_dossier", arguments={})]),
                ModelResponse(content="Handled error", tool_calls=[]),
            ]),
            hooks=crit_hooks,
            workspace_dir=tmp_path / "ws_crit",
        )
        state_crit = await loop_crit.run(HarnessTaskSpec(task_id="t_crit", user_query="Test critical sanitizer failclosed", budget_max=3))

        # Model receives ERROR [RESULT_DENIED] without raw sensitive leak or raw exception leak (F4)
        tool_err_msg = [m for m in state_crit.messages if m.get("role") == "tool"][-1]["content"]
        assert "ERROR [RESULT_DENIED]: Post-tool hook denied observation (HOOK_EXECUTION_ERROR)" in tool_err_msg
        assert "AUTH_SECRET_KEY_9999" not in tool_err_msg
        assert "customer_1_secret@synthetic-corp.com" not in tool_err_msg
        # Raw exception is faithfully preserved in HookAuditRecord for debugging
        crit_audit = [a for a in state_crit.hook_audits if a["hook_id"] == "crit_sanitizer"][0]
        assert "AUTH_SECRET_KEY_9999" in str(crit_audit.get("error"))

    asyncio.run(_test())


def test_h2b_supervisor_hook_composition_and_worker_inheritance(tmp_path: Path):
    """H2B-2: Supervisor propagates hooks down to subordinate workers.
    compose_hooks ensures parent negative constraints (e.g. BeforeTool read-only)
    cannot be dropped or bypassed by child workers with custom hooks.
    """
    async def _test():
        db_path = tmp_path / "sf_h2b_2.db"
        broker = ToolBroker(application_allowlist={"query_order", "refund_order"})

        query_count = 0
        refund_handler_calls = 0

        def query_order(order_id: str) -> str:
            nonlocal query_count
            query_count += 1
            return f"Order {order_id} total $100, status OK"

        def refund_order(order_id: str, amount: float) -> str:
            nonlocal refund_handler_calls
            refund_handler_calls += 1
            return f"Refunded {amount}"

        broker.register_tool(query_order, name="query_order")
        broker.register_tool(refund_order, name="refund_order")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Supervisor configured with read-only constraint hook
        supervisor_hooks = HarnessHooks(
            pre_tool_exec=create_read_only_constraint_hook(
                tool_effects={"refund_order": "side_effect", "query_order": "read"},
                hook_id="supervisor_read_only_gate",
            )
        )

        worker_custom_hook_called = 0

        def worker_child_hook(state: HarnessState, tc: ToolCallSpec):
            nonlocal worker_custom_hook_called
            worker_custom_hook_called += 1
            return HookDecision.allow(reason_code="WORKER_ALLOW")

        # Worker 1: no custom hooks (inherits supervisor hook directly)
        worker_1 = WorkerAgentSpec(
            name="worker_alpha",
            description="Alpha order worker",
            system_prompt="CONSTRAINT: READ_ONLY",
            allowed_tools=["query_order", "refund_order"],
            max_steps_per_call=3,
        )

        # Worker 2: has custom hook (must compose with supervisor hook, cannot bypass parent gate)
        worker_2 = WorkerAgentSpec(
            name="worker_beta",
            description="Beta order worker",
            system_prompt="CONSTRAINT: READ_ONLY",
            allowed_tools=["query_order", "refund_order"],
            max_steps_per_call=3,
            hooks=HarnessHooks(pre_tool_exec=worker_child_hook),
        )

        # Worker LLMs:
        # Worker 1 calls query_order (allowed)
        # Worker 2 attempts refund_order (must be denied by parent hook)
        worker_1_llm = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_q1", name="query_order", arguments={"order_id": "ORD_1"})]),
            ModelResponse(content="Order status checked", tool_calls=[]),
        ])
        worker_2_llm = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_r1", name="refund_order", arguments={"order_id": "ORD_1", "amount": 50.0})]),
            ModelResponse(content="Refund was rejected", tool_calls=[]),
        ])
        worker_1.llm_client = worker_1_llm
        worker_2.llm_client = worker_2_llm

        step_counter = 0

        def router(s):
            nonlocal step_counter
            step_counter += 1
            if step_counter == 1:
                return SupervisorDecision(next_worker="worker_alpha", instruction="Query order")
            elif step_counter == 2:
                return SupervisorDecision(next_worker="worker_beta", instruction="Try refund")
            return SupervisorDecision(next_worker="FINISH", instruction="All done")

        sup = SupervisorOrchestrator(
            supervisor_llm=ScriptedMockLLM([]),
            workers=[worker_1, worker_2],
            runtime=runtime,
            tool_broker=broker,
            workspace_dir=tmp_path / "ws_sup",
            router_fn=router,
            hooks=supervisor_hooks,
        )

        sup_state = await sup.run(SupervisorTaskSpec(
            task_id="sup_task_hooks",
            user_query="Audit and process",
            global_constraints=["READ_ONLY"],
        ))

        assert sup_state.status == "COMPLETED"
        assert query_count == 1
        # Crucial: refund handler was NEVER executed despite worker having custom hooks!
        assert refund_handler_calls == 0

        # Assert actual tool record status HOOK_DENIED (F5)
        refund_recs = [r for r in sup_state.tool_records if r.tool_name == "refund_order"]
        assert len(refund_recs) == 1
        assert refund_recs[0].error_type == "HOOK_DENIED"
        assert refund_recs[0].status == "REJECTED"

        # Assert audit trail binds run, task, and intent (F5)
        denied_audits = [
            a for h in sup_state.worker_history
            for a in h.get("worker_state", {}).get("hook_audits", [])
            if a.get("event") == "pre_tool_exec" and a.get("action") == "deny" and a.get("hook_id") == "supervisor_read_only_gate"
        ]
        assert len(denied_audits) == 1
        assert denied_audits[0]["action"] == "deny"
        assert denied_audits[0]["reason_code"] == "READ_ONLY_VIOLATION"
        assert denied_audits[0]["intent_version"] == "v1"
        assert "sup_task_hooks" in denied_audits[0]["task_id"]
        assert denied_audits[0]["run_id"]

    asyncio.run(_test())


def test_h2b_supervisor_hook_composition_child_view_sanitized_by_parent(tmp_path: Path):
    """H2B-F1: In composed hooks, post_tool_exec child hook formats/injects view first,
    and parent security hook executes second as final defense. Parent filters child-introduced
    secrets, messages do not leak secrets, SQLite/raw retain original data, and parent deny
    cannot be unblocked by child allow.
    """
    async def _test():
        db_path = tmp_path / "sf_f1.db"
        broker = ToolBroker(application_allowlist={"fetch_data"})

        raw_data = {"key": "value", "system_id": "SYS_001"}
        broker.register_tool(lambda: raw_data, name="fetch_data")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Parent hook: redacts emails and secret keys
        parent_hook = create_sensitive_data_redaction_hook(
            redact_patterns={
                r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+": "[REDACTED_EMAIL]",
                r"LEAK_SECRET_\d+": "[REDACTED_SECRET]",
            },
            filter_keys=["token", "secret"],
            hook_id="parent_dlp_gate",
            critical=True,
        )

        # Child worker hook: custom formatter that introduces a new secret view
        def child_formatter(state: HarnessState, rec: ToolCallRecord):
            # Child formatter injects email and secret into observation view
            tainted_view = json.dumps({
                "summary": "Formatted by child worker",
                "contact": "support_leak@synthetic-corp.com",
                "secret": "LEAK_SECRET_7777",
            })
            return HookDecision.allow(observation_view=tainted_view, reason_code="CHILD_FORMATTED")

        composed = compose_hooks(
            parent=HarnessHooks(post_tool_exec=parent_hook),
            child=HarnessHooks(post_tool_exec=child_formatter),
        )

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="fetch_data", arguments={})]),
                ModelResponse(content="Data verified", tool_calls=[]),
            ]),
            hooks=composed,
            workspace_dir=tmp_path / "ws_f1",
        )

        state = await loop.run(HarnessTaskSpec(task_id="t_f1", user_query="Run child formatter test", budget_max=3))

        assert state.status == "COMPLETED"
        assert len(state.tool_records) == 1

        # 1. Raw record in state and SQLite DB retains original raw_data without corruption
        rec = state.tool_records[0]
        assert rec.status == "EXECUTED"
        assert rec.output_data == raw_data

        # 2. Model messages: parent DLP filtered the child's tainted observation view!
        tool_msg = [m for m in state.messages if m.get("role") == "tool"][-1]["content"]
        assert "support_leak@synthetic-corp.com" not in tool_msg
        assert "LEAK_SECRET_7777" not in tool_msg
        assert "[REDACTED_EMAIL]" in tool_msg
        assert "[REDACTED]" in tool_msg or "[REDACTED_SECRET]" in tool_msg

        # 3. Parent deny cannot be unblocked by child allow
        blocking_parent = HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.deny("PARENT_POLICY_DENY"))
        permissive_child = HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.allow("CHILD_APPROVED"))
        composed_deny = compose_hooks(parent=blocking_parent, child=permissive_child)

        loop_deny = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="fetch_data", arguments={})]),
                ModelResponse(content="Stopped", tool_calls=[]),
            ]),
            hooks=composed_deny,
            workspace_dir=tmp_path / "ws_f1_deny",
        )
        state_deny = await loop_deny.run(HarnessTaskSpec(task_id="t_f1_deny", user_query="Deny test", budget_max=3))
        # Tool call was blocked with HOOK_DENIED despite child allow
        assert state_deny.tool_records[-1].status == "REJECTED"
        assert state_deny.tool_records[-1].error_type == "HOOK_DENIED"

    asyncio.run(_test())


def test_h2b_custom_before_finalize_allow_vetoed_by_failing_oracle(tmp_path: Path):
    """H2B-3: Custom before_finalize hook returning 'allow' CANNOT override an authoritative
    task_spec.verification_fn failure. False success claim is blocked with DELIVERY_BLOCKED,
    Episode is marked failure, and verification_fn runs exactly once.
    """
    async def _test():
        db_path = tmp_path / "sf_h2b_3.db"
        broker = ToolBroker(application_allowlist={"dummy_check"})
        broker.register_tool(lambda: "ok", name="dummy_check")

        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

        custom_hook_called = 0

        # Custom hook that is overly permissive (claims allow)
        def custom_permissive_finalizer(state: HarnessState, proposed: str):
            nonlocal custom_hook_called
            custom_hook_called += 1
            return HookDecision.allow(reason_code="CUSTOM_FORMAT_APPROVED")

        oracle_eval_count = 0

        # Authoritative Oracle that detects invariant failure
        def strict_business_oracle(state: HarnessState):
            nonlocal oracle_eval_count
            oracle_eval_count += 1
            return {
                "independent_pass": False,
                "is_hallucination": True,
                "failure_reason": "Logistics Invariant 2 Violated: Undelivered package PKG_202",
            }

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="dummy_check", arguments={})]),
                ModelResponse(content="All packages successfully delivered!", tool_calls=[]),
            ]),
            hooks=HarnessHooks(before_finalize=custom_permissive_finalizer),
            workspace_dir=tmp_path / "ws_h2b_3",
        )

        # Task spec pre-seeded with fake/stale verification_evidence claiming pass (F3)
        state = await loop.run(HarnessTaskSpec(
            task_id="t_h2b_3",
            user_query="Check delivery",
            budget_max=4,
            verification_fn=strict_business_oracle,
            initial_scratchpad={"verification_evidence": {"independent_pass": True, "fake_preseed": True}},
        ))

        # 1. Custom hook ran and gave allow, but Oracle was NOT bypassed and vetoed it!
        assert custom_hook_called == 1
        assert oracle_eval_count == 1
        assert state.status == "FAILED"
        assert "[DELIVERY_BLOCKED]: ORACLE_HALLUCINATION_DETECTED" in (state.final_output or "")

        # 2. Proposed output preserved for inspection
        assert "All packages successfully delivered!" in state.working_scratchpad["proposed_output"]

        # 3. Episode is NOT success
        ep = ep_store.get_episode(f"ep_{state.run_id}")
        assert ep is not None
        assert ep.outcome == "failure"
        assert ep.verification_evidence["is_hallucination"] is True

        # --- Part B (F3): Review pause on first proposal -> change candidate on resume -> Oracle evaluates fresh ---
        dyn_oracle_count = 0
        def dynamic_oracle(state: HarnessState, proposed: str = ""):
            nonlocal dyn_oracle_count
            dyn_oracle_count += 1
            if "honest" in proposed:
                return {"independent_pass": True, "is_hallucination": False, "classification": "TRUE_NEGATIVE"}
            return {"independent_pass": False, "is_hallucination": True}

        def review_first_proposal(state: HarnessState, proposed: str):
            if "first" in proposed:
                return HookDecision.review("NEED_VERIFICATION")
            return HookDecision.allow()

        loop_rev = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="first overclaim", tool_calls=[]),
            ]),
            hooks=HarnessHooks(before_finalize=review_first_proposal),
            workspace_dir=tmp_path / "ws_oracle_rev",
        )
        s_rev1 = await loop_rev.run(HarnessTaskSpec(
            task_id="t_dyn_oracle",
            user_query="Dynamic oracle check",
            run_id="run_dyn_oracle",
            verification_fn=dynamic_oracle,
        ))
        assert s_rev1.status == "PAUSED_FOR_REVIEW"
        # First proposal was paused for review before oracle execution
        assert dyn_oracle_count == 0

        # Resume with changed second proposal
        loop_rev2 = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="second honest output", tool_calls=[]),
            ]),
            hooks=HarnessHooks(before_finalize=lambda s, p: HookDecision.allow()),
            workspace_dir=tmp_path / "ws_oracle_rev",
        )
        s_rev2 = await loop_rev2.resume(
            run_id="run_dyn_oracle",
            updated_spec=HarnessTaskSpec(
                task_id="t_dyn_oracle",
                user_query="Dynamic oracle check",
                verification_fn=dynamic_oracle,
            ),
        )
        assert s_rev2.status == "COMPLETED"
        # Oracle evaluated fresh for the second proposed text!
        assert dyn_oracle_count == 1
        assert s_rev2.final_output == "second honest output"

    asyncio.run(_test())


def test_h2b_review_resume_rebuilding_runtime_from_sqlite(tmp_path: Path):
    """H2B-4: Review pause and resume with fully reconstructed Runtime and Loop instances
    from SQLite DB. Completed actions are not re-executed, budget is not reset,
    pending actions are validated by Broker, and Collector archives Episode once.
    """
    async def _test():
        db_path = tmp_path / "sf_h2b_4.db"
        workspace = tmp_path / "ws_h2b_4"
        run_id = "run_full_rebuild_resume"

        # Phase 1: Broker & Runtime
        step1_calls = 0
        step2_calls = 0

        def step1_action():
            nonlocal step1_calls
            step1_calls += 1
            return "step1_data"

        def step2_action():
            nonlocal step2_calls
            step2_calls += 1
            return "step2_data"

        broker_1 = ToolBroker(application_allowlist={"step1_action", "step2_action"})
        broker_1.register_tool(step1_action, name="step1_action")
        broker_1.register_tool(step2_action, name="step2_action")

        ep_store_1 = EpisodeStore(db_path=db_path)
        collector_1 = ExperienceCollector(episode_store=ep_store_1)
        runtime_1 = AgentRuntime(db_path=db_path, tool_broker=broker_1, collector=collector_1)

        # Hook pauses on step2
        def pause_on_step2(state: HarnessState, tc: ToolCallSpec):
            if tc.name == "step2_action":
                return HookDecision.review(reason_code="MANUAL_APPROVAL_REQUIRED")
            return HookDecision.allow()

        loop_1 = AsyncHarnessLoop(
            runtime=runtime_1,
            tool_broker=broker_1,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="step1_action", arguments={})]),
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="step2_action", arguments={})]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=pause_on_step2),
            workspace_dir=workspace,
        )

        state1 = await loop_1.run(HarnessTaskSpec(
            task_id="t_rebuild",
            user_query="Two step flow",
            run_id=run_id,
            budget_max=6,
        ))

        # Verify budget and steps before resume (F5)
        assert state1.status == "PAUSED_FOR_REVIEW"
        assert state1.step_count == 1
        assert state1.budget_max == 6
        assert runtime_1.get_run(run_id).budget_consumed == 1
        assert step1_calls == 1
        assert step2_calls == 0
        assert ep_store_1.get_episode(f"ep_{run_id}") is None

        # --- Phase 2: FULL RECONSTRUCTION FROM DISK ---
        # Completely re-instantiate EpisodeStore, ExperienceCollector, ToolBroker, AgentRuntime, and AsyncHarnessLoop
        ep_store_2 = EpisodeStore(db_path=db_path)
        collector_2 = ExperienceCollector(episode_store=ep_store_2)
        broker_2 = ToolBroker(application_allowlist={"step1_action", "step2_action"})
        broker_2.register_tool(step1_action, name="step1_action")
        broker_2.register_tool(step2_action, name="step2_action")
        runtime_2 = AgentRuntime(db_path=db_path, tool_broker=broker_2, collector=collector_2)

        loop_2 = AsyncHarnessLoop(
            runtime=runtime_2,
            tool_broker=broker_2,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2_res", name="step2_action", arguments={})]),
                ModelResponse(content="Full reconstruction completed", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.allow(), task_end=lambda s: None),
            workspace_dir=workspace,
        )

        state2 = await loop_2.resume(run_id=run_id)

        assert state2.status == "COMPLETED"
        assert state2.final_output == "Full reconstruction completed"
        # step1 was NOT re-executed!
        assert step1_calls == 1
        assert step2_calls == 1

        # Budget consumption and step count continuous accumulation (F5)
        assert state2.step_count == 2
        assert state2.budget_max == 6
        assert runtime_2.get_run(run_id).budget_consumed == 2
        assert runtime_2.get_run(run_id).budget_max == 6

        # Episode archived exactly once (F5)
        ep = ep_store_2.get_episode(f"ep_{run_id}")
        assert ep is not None
        assert ep.outcome == "unknown"  # No independent oracle, honest unknown
        assert len(ep_store_2.list_episodes()) == 1

        # Final checkpoint has task_end hook audit (F5)
        chk_file = workspace / "runs" / run_id / "checkpoint.json"
        assert chk_file.exists()
        chk = json.loads(chk_file.read_text(encoding="utf-8"))
        assert any(a.get("event") == "task_end" for a in chk.get("hook_audits", []))

    asyncio.run(_test())


def test_h2b_review_resume_pending_unauthorized_rejected_by_broker(tmp_path: Path):
    """H2B-F5: When resuming from review pause, if a pending tool is NOT authorized
    in the reconstructed ToolBroker's application allowlist, Broker blocks execution
    with PERMISSION_DENIED, handler count remains 0, and tool record status is REJECTED.
    """
    async def _test():
        db_path = tmp_path / "sf_h2b_perm.db"
        workspace = tmp_path / "ws_h2b_perm"
        run_id = "run_unauth_resume"

        step1_calls = 0
        step2_calls = 0

        def step1_action():
            nonlocal step1_calls
            step1_calls += 1
            return "step1_ok"

        def step2_sensitive():
            nonlocal step2_calls
            step2_calls += 1
            return "step2_sensitive"

        broker_1 = ToolBroker(application_allowlist={"step1_action", "step2_sensitive"})
        broker_1.register_tool(step1_action, name="step1_action")
        broker_1.register_tool(step2_sensitive, name="step2_sensitive")

        ep_store = EpisodeStore(db_path=db_path)
        collector = ExperienceCollector(episode_store=ep_store)
        runtime_1 = AgentRuntime(db_path=db_path, tool_broker=broker_1, collector=collector)

        loop_1 = AsyncHarnessLoop(
            runtime=runtime_1,
            tool_broker=broker_1,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="step1_action", arguments={})]),
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="step2_sensitive", arguments={})]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.review("MANUAL_APPROVAL") if tc.name == "step2_sensitive" else HookDecision.allow()),
            workspace_dir=workspace,
        )

        s1 = await loop_1.run(HarnessTaskSpec(task_id="t_perm", user_query="Audit check", run_id=run_id, budget_max=4))
        assert s1.status == "PAUSED_FOR_REVIEW"
        assert step1_calls == 1
        assert step2_calls == 0
        assert runtime_1.get_run(run_id).budget_consumed == 1

        # Phase 2: Broker rebuilt WITHOUT step2_sensitive in application_allowlist
        broker_2 = ToolBroker(application_allowlist={"step1_action"})
        broker_2.register_tool(step1_action, name="step1_action")
        broker_2.register_tool(step2_sensitive, name="step2_sensitive")
        runtime_2 = AgentRuntime(db_path=db_path, tool_broker=broker_2, collector=collector)

        loop_2 = AsyncHarnessLoop(
            runtime=runtime_2,
            tool_broker=broker_2,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2_unauth", name="step2_sensitive", arguments={})]),
                ModelResponse(content="Stopped after rejection", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.allow(), task_end=lambda s: None),
            workspace_dir=workspace,
        )

        s2 = await loop_2.resume(run_id=run_id)
        # Handler was NEVER executed!
        assert step2_calls == 0
        # Tool call record shows Broker rejection
        unauth_recs = [r for r in s2.tool_records if r.tool_name == "step2_sensitive"]
        assert len(unauth_recs) == 1
        assert unauth_recs[0].status == "REJECTED"
        assert unauth_recs[0].error_type == "PERMISSION_DENIED"
        # Pre-dispatch budget ticket consumed by runtime execute_tool on unauth tool (ticket rules)
        assert runtime_2.get_run(run_id).budget_consumed == 2

    asyncio.run(_test())


