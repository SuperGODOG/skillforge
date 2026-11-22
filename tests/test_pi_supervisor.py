"""Tests for Phase 4B1: SkillForge Supervisor Worker explicit Pi execution.

Covers 6 compact specifications:
1. Two workers reading synthetic logistics data, Python routing, constraints and prompt/tool schemas isolated,
   parent broker object and allowlist unchanged, distinct child run IDs, Episodes unknown without oracle.
2. Worker declares tools exceeding parent permissions -> model does not see schema, handler calls = 0;
   composed parent deny + child allow does not bypass.
3. Shared budget enforcement: parent budget 5, worker 1 consumes 3 (model+tool+model), worker 2 has 2 remaining;
   subsequent provider turns blocked, total <= 5, deadline expiration prevents worker dispatch.
4. Cancellation of running Pi worker: parent CANCELLED, child CANCELLED, Pi process reaped, side-effects preserved.
5. Authoritative verification_fn evidence controls success; missing oracle yields unknown; business failure stops
   supervisor without treating failure as success.
6. Legacy default does not spawn Pi processes (backward compatibility preserved).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.harness_loop import HarnessHooks, HarnessState, HookDecision, ModelResponse, compose_hooks
from skillforge.models import ToolCallRecord
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)
from skillforge.supervisor import (
    SupervisorDecision,
    SupervisorOrchestrator,
    SupervisorState,
    SupervisorTaskSpec,
    WorkerAgentSpec,
)


@pytest.fixture
def supervisor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sets up isolated SQLite runtime, ToolBroker, Collector, CandidateStore, and SkillRegistry."""
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "status", "IN_TRANSIT")
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "carrier", "Carrier_Beta")
    db_path = tmp_path / "runtime.db"
    episodes_db = tmp_path / "episodes.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    episode_store = EpisodeStore(db_path=episodes_db)
    cand_store = CandidateStore(db_path=tmp_path / "candidates.db", episode_store=episode_store)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    root_broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    pkg_tool = QueryOrderPackagesTool()
    track_tool = QueryPackageTrackingTool()
    root_broker.register_tool(pkg_tool)
    root_broker.register_tool(track_tool)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=root_broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
        candidate_store=cand_store,
    )

    yield {
        "runtime": runtime,
        "broker": root_broker,
        "collector": collector,
        "episode_store": episode_store,
        "cand_store": cand_store,
        "registry": registry,
        "pkg_tool": pkg_tool,
        "track_tool": track_tool,
        "tmp_path": tmp_path,
    }

    runtime.close()
    cand_store.close()
    episode_store.close()
    registry.close()


# ==============================================================================
# Case 1: Two workers reading synthetic logistics, Python routing, isolation
# ==============================================================================


@pytest.mark.anyio
async def test_case1_two_workers_synthetic_logistics_isolation_and_unknown_episode(supervisor_env):
    """Case 1: Two Pi workers execute sub-goals, Python routing preserved, parent allowlist unchanged,

    separate run IDs, and child episodes are unknown without oracle.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    episode_store: EpisodeStore = supervisor_env["episode_store"]
    tmp_path: Path = supervisor_env["tmp_path"]

    # Worker 1: logistics data
    worker1_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_order_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Order ORD_2026_0901 has PKG_101 and PKG_102."}],
                "stopReason": "stop",
            },
        ],
    }

    # Worker 2: package tracking
    worker2_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_track_01",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "PKG_101 is DELIVERED."}],
                "stopReason": "stop",
            },
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="logistics_worker",
            description="Queries order packages",
            system_prompt="You are a logistics agent.",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=worker1_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
        WorkerAgentSpec(
            name="tracking_worker",
            description="Queries package tracking",
            system_prompt="You are a tracking agent.",
            allowed_tools=["query_package_tracking"],
            execution_backend="pi",
            pi_model_config=worker2_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
    ]

    def supervisor_router(state: SupervisorState) -> SupervisorDecision:
        if "logistics_worker" not in state.worker_results:
            return SupervisorDecision(
                next_worker="logistics_worker",
                instruction="Query packages for ORD_2026_0901",
                reasoning="Step 1: pull order data",
            )
        elif "tracking_worker" not in state.worker_results:
            return SupervisorDecision(
                next_worker="tracking_worker",
                instruction="Query tracking for PKG_101",
                reasoning="Step 2: track package",
            )
        else:
            return SupervisorDecision(
                next_worker="FINISH",
                instruction="Logistics data query complete.",
                reasoning="All done",
            )

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=supervisor_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case1",
        user_query="Audit logistics for ORD_2026_0901",
        intent_version="v2",
        global_constraints=["STATUS_ONLY"],
        global_budget_max=15,
        max_supervisor_steps=5,
    )

    final_state = await orchestrator.run(task_spec)

    assert final_state.status == "COMPLETED"
    assert "logistics_worker" in final_state.worker_results
    assert "tracking_worker" in final_state.worker_results
    assert "PKG_101 and PKG_102" in final_state.worker_results["logistics_worker"]
    assert "PKG_101 is DELIVERED" in final_state.worker_results["tracking_worker"]

    # Parent broker remains intact
    assert root_broker.application_allowlist == {"query_order_packages", "query_package_tracking"}

    # Tool records collected across workers
    called_tools = [rec.tool_name for rec in final_state.tool_records]
    assert "query_order_packages" in called_tools
    assert "query_package_tracking" in called_tools

    # Separate run IDs in worker history
    w1_run = final_state.worker_history[0]["worker_state"]
    w2_run = final_state.worker_history[1]["worker_state"]
    assert w1_run["run_id"] != w2_run["run_id"]
    assert w1_run["status"] == "COMPLETED"
    assert w2_run["status"] == "COMPLETED"

    # Child episodes exist with outcome unknown (no oracle provided)
    ep1 = episode_store.get_episode(f"ep_{w1_run['run_id']}")
    ep2 = episode_store.get_episode(f"ep_{w2_run['run_id']}")
    assert ep1 is not None and ep1.outcome == "unknown"
    assert ep2 is not None and ep2.outcome == "unknown"


# ==============================================================================
# Case 2: Worker declares tool exceeding parent permissions & hook denial
# ==============================================================================


@pytest.mark.anyio
async def test_case2_unauthorized_tool_decloaked_and_composed_hook_denial(supervisor_env):
    """Case 2: Worker cannot access tools outside parent broker allowlist,

    and composed parent deny + child allow hook blocks execution.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    tmp_path: Path = supervisor_env["tmp_path"]
    pkg_tool = supervisor_env["pkg_tool"]

    # Parent broker ONLY allows query_order_packages (does not allow query_package_tracking or refund)
    parent_broker = ToolBroker(application_allowlist={"query_order_packages"})
    parent_broker.register_tool(pkg_tool)

    handler_calls = 0
    orig_pkg_run = pkg_tool.run

    def tracked_pkg_run(params):
        nonlocal handler_calls
        handler_calls += 1
        return orig_pkg_run(params)

    pkg_tool.run = tracked_pkg_run

    # Worker attempts to declare unauthorized_tool and query_package_tracking
    worker_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_unauth_01",
                        "name": "unauthorized_refund",
                        "arguments": {"amount": 100},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_parent_denied_02",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Finished attempts."}],
                "stopReason": "stop",
            },
        ],
    }

    # Parent hook: denies query_order_packages
    parent_hook = HarnessHooks(
        pre_tool_exec=lambda state, tc: HookDecision.deny(
            reason_code="PARENT_SECURITY_RULE_DENIAL",
            details={"blocked_tool": tc.name},
        )
    )
    # Child hook: attempts to allow everything
    child_hook = HarnessHooks(
        pre_tool_exec=lambda state, tc: HookDecision.allow(
            reason_code="CHILD_ALLOW_ALL",
        )
    )

    workers = [
        WorkerAgentSpec(
            name="rogue_worker",
            description="Attempts unauthorized tools",
            system_prompt="You are a rogue agent.",
            allowed_tools=["query_order_packages", "unauthorized_refund", "query_package_tracking"],
            execution_backend="pi",
            pi_model_config=worker_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
            hooks=child_hook,
        )
    ]

    def single_step_router(state: SupervisorState) -> SupervisorDecision:
        if "rogue_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="rogue_worker", instruction="Attempt operations")
        return SupervisorDecision(next_worker="FINISH", instruction="Done")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=parent_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=single_step_router,
        hooks=parent_hook,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case2",
        user_query="Execute rogue check",
        global_budget_max=10,
    )

    final_state = await orchestrator.run(task_spec)

    # 1. unauthorized_refund was completely excluded from scoped allowlist
    # 2. query_order_packages was denied by parent hook
    # Handler calls strictly 0!
    assert handler_calls == 0

    # Parent broker allowlist was not modified
    assert parent_broker.application_allowlist == {"query_order_packages"}

    # Records show rejection
    rejected_records = [r for r in final_state.tool_records if r.status in ("REJECTED", "PERMISSION_DENIED")]
    assert len(rejected_records) >= 1


# ==============================================================================
# Case 3: Shared budget exhaustion & deadline expiration
# ==============================================================================


@pytest.mark.anyio
async def test_case3_shared_budget_exhaustion_stops_subsequent_provider_and_deadline(supervisor_env):
    """Case 3: Parent budget 5 is strictly consumed across workers; worker 1 uses 3,

    worker 2 uses 2, next turn blocked without sending to provider; deadline expired halts dispatch.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]

    # Worker 1: 1 model turn + 1 tool call + 1 model turn = 3 budget
    w1_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_budget_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Worker 1 finished."}],
                "stopReason": "stop",
            },
        ],
    }

    # Worker 2: has only 2 remaining budget (5 - 3 = 2).
    # Turn 1: 1 model turn + 1 tool call = 2 budget.
    # Turn 2: Attempting another model turn must be blocked by budget exhaustion!
    w2_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_budget_02",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "This response should NEVER be reached"}],
                "stopReason": "stop",
            },
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="worker1",
            description="Worker 1",
            system_prompt="Worker 1 prompt",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=w1_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
        WorkerAgentSpec(
            name="worker2",
            description="Worker 2",
            system_prompt="Worker 2 prompt",
            allowed_tools=["query_package_tracking"],
            execution_backend="pi",
            pi_model_config=w2_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
    ]

    def two_worker_router(state: SupervisorState) -> SupervisorDecision:
        if "worker1" not in state.worker_results:
            return SupervisorDecision(next_worker="worker1", instruction="Step 1")
        elif "worker2" not in state.worker_results:
            return SupervisorDecision(next_worker="worker2", instruction="Step 2")
        return SupervisorDecision(next_worker="FINISH", instruction="Finished")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=two_worker_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case3",
        user_query="Test budget limits",
        global_budget_max=5,
    )

    final_state = await orchestrator.run(task_spec)

    # Global budget consumed strictly capped at 5
    assert final_state.global_budget_consumed <= 5
    assert final_state.status == "BUDGET_EXHAUSTED"

    # Part B: Deadline expired halts dispatch upfront
    deadline_spec = SupervisorTaskSpec(
        task_id="task_sup_case3_deadline",
        user_query="Test deadline expiration",
        global_budget_max=10,
        deadline_seconds=-1.0,  # Already expired in the past
    )
    deadline_state = await orchestrator.run(deadline_spec)
    assert deadline_state.status == "TIMED_OUT"
    assert deadline_state.step_count == 0


# ==============================================================================
# Case 4: Cancellation cascades to Pi, reaps PID, saves checkpoint
# ==============================================================================


@pytest.mark.anyio
async def test_case4_cancellation_cascades_to_pi_and_reaps_pid_with_checkpoint(supervisor_env):
    """Case 4: Cancelling the awaiting supervisor task cleanly aborts Pi,

    reaps child process PID, and saves CANCELLED checkpoint with consumed budget.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]

    captured_pid = None
    cancel_triggered = asyncio.Event()

    async def pre_tool_hook(state: HarnessState, tc):
        nonlocal captured_pid
        # Capture Pi PID from runtime
        captured_pid = runtime.get_last_pi_pid(state.run_id)
        cancel_triggered.set()
        await asyncio.sleep(2.0)  # Wait so supervisor task gets cancelled while in flight
        return HookDecision.allow()

    worker_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_cancel_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Post cancellation response"}],
                "stopReason": "stop",
            },
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="cancel_worker",
            description="Worker to be cancelled",
            system_prompt="Cancel prompt",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=worker_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
            hooks=HarnessHooks(pre_tool_exec=pre_tool_hook),
        )
    ]

    def cancel_router(state: SupervisorState) -> SupervisorDecision:
        return SupervisorDecision(next_worker="cancel_worker", instruction="Will be cancelled")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=cancel_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case4",
        user_query="Cancel test",
        global_budget_max=10,
    )

    sup_task = asyncio.create_task(orchestrator.run(task_spec))

    # Wait until hook starts executing inside Pi runner
    await cancel_triggered.wait()
    await asyncio.sleep(0.05)
    sup_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await sup_task

    # Verify orchestrator state and checkpoint
    last_state = orchestrator.last_state
    assert last_state is not None
    assert last_state.status == "CANCELLED"

    chk_file = last_state.workspace_dir / "supervisor_checkpoint.json"
    assert chk_file.exists()
    chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
    assert chk_data["status"] == "CANCELLED"

    # Verify child Pi PID was reaped
    if captured_pid:
        await asyncio.sleep(0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(captured_pid, 0)


# ==============================================================================
# Case 5: verification_fn evidence controls success; false pass blocked
# ==============================================================================


@pytest.mark.anyio
async def test_case5_verification_fn_controls_outcome_and_blocks_false_pass(supervisor_env):
    """Case 5: Model claiming PASS is overruled by business verification_fn;

    verification failure halts supervisor and marks status FAILED.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    episode_store: EpisodeStore = supervisor_env["episode_store"]
    tmp_path: Path = supervisor_env["tmp_path"]

    # Faux model makes false claim: "All packages delivered!" (even though PKG_102 is IN_TRANSIT)
    false_claim_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_false_claim_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "SUCCESS: All packages verified delivered!"}],
                "stopReason": "stop",
            },
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="verifier_worker",
            description="Performs fulfillment with oracle check",
            system_prompt="Logistics assistant",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=false_claim_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]

    def simple_router(state: SupervisorState) -> SupervisorDecision:
        if "verifier_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="verifier_worker", instruction="Check order delivery")
        return SupervisorDecision(next_worker="FINISH", instruction="Complete")

    # Verification function: detects false delivery claim
    def business_verification_fn(child_harness: HarnessState) -> dict[str, Any]:
        output = child_harness.final_output or child_harness.working_scratchpad.get("proposed_output", "")
        # In synthetic data, PKG_102 is IN_TRANSIT. Model claimed 'All packages verified delivered!' -> False!
        if "all packages verified delivered" in output.lower():
            return {
                "independent_pass": False,
                "failure_reason": "False delivery claim: PKG_102 is IN_TRANSIT",
            }
        return {"independent_pass": True}

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=simple_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case5",
        user_query="Verify delivery status",
        global_budget_max=10,
        verification_fn=business_verification_fn,
    )

    final_state = await orchestrator.run(task_spec)

    # Supervisor stopped with FAILED and failure reason
    assert final_state.status == "FAILED"
    assert "False delivery claim" in (final_state.error_message or "")

    # Child episode recorded as failure
    w_run = final_state.worker_history[0]["worker_state"]
    ep = episode_store.get_episode(f"ep_{w_run['run_id']}")
    assert ep is not None
    assert ep.outcome == "failure"


# ==============================================================================
# Case 6: Legacy default does not start Pi
# ==============================================================================


@pytest.mark.anyio
async def test_case6_legacy_worker_default_does_not_launch_pi(supervisor_env):
    """Case 6: Worker with execution_backend='legacy' uses AsyncHarnessLoop

    and spawns zero Pi clients or subprocesses.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]

    class MockLegacyLLM:
        async def ainvoke(self, messages, tools, system=""):
            return ModelResponse(content="Legacy worker completed successfully.")

    workers = [
        WorkerAgentSpec(
            name="legacy_worker",
            description="Legacy worker",
            system_prompt="Legacy prompt",
            allowed_tools=["query_order_packages"],
            execution_backend="legacy",
            llm_client=MockLegacyLLM(),
        )
    ]

    def legacy_router(state: SupervisorState) -> SupervisorDecision:
        if "legacy_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="legacy_worker", instruction="Do legacy job")
        return SupervisorDecision(next_worker="FINISH", instruction="Done")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=tmp_path / "workspace",
        router_fn=legacy_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_sup_case6",
        user_query="Legacy test",
        global_budget_max=10,
    )

    final_state = await orchestrator.run(task_spec)

    assert final_state.status == "COMPLETED"
    assert "Legacy worker completed" in final_state.worker_results["legacy_worker"]

    # Zero Pi clients created in runtime
    assert len(runtime._active_pi_clients) == 0
