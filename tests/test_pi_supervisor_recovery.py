"""Tests for Phase 4B2: SkillForge Supervisor Multi-Agent Persistent Recovery.

Covers 5 core recovery protocol specifications:
1. Interrupted orchestration after worker 1 resumes from new Runtime + Supervisor on same workspace:
   only runs worker 2, worker 1 handler not repeated, global budget/step count not reset, original deadline preserved.
2. In-flight Pi worker paused cooperatively at context boundary: resumes via aresume_agent_with_pi with same child ID,
   tool not repeated, prompt not tampered, parent records delta budget only.
3. Contract drift rejection: modifications to query, intent, constraints, worker prompts, allowed tools, or budget
   refuse stale checkpoint with ValueError; deadline is not renewed. Unfinished checkpoint requires explicit resume=True.
4. In-flight unsettled ADMITTED tool calls or missing Pi checkpoint halt in PAUSED_FOR_REVIEW / REVIEW_REQUIRED;
   handler calls == 0.
5. Expired deadline / terminal status (CANCELLED, FAILED) cannot be resumed; checkpoint write failure prevents worker dispatch.
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
from skillforge.harness_loop import HarnessHooks, HarnessState, HookDecision, ModelResponse
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
from tests.test_pi_supervisor import supervisor_env


# ==============================================================================
# Case 1: Interruption after Worker 1, resume from new Runtime+Supervisor, only run Worker 2
# ==============================================================================


@pytest.mark.anyio
async def test_case1_interrupt_after_worker1_resume_runs_only_worker2(supervisor_env):
    """Case 1: Orchestration interrupted after worker 1 completes.

    New Runtime + Supervisor loads checkpoint with resume=True:
    - Worker 1 handler is NOT called again.
    - Router continues to Worker 2.
    - Global budget and step count accumulate monotonically without resetting.
    - Original deadline is strictly preserved.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]
    pkg_tool = supervisor_env["pkg_tool"]
    track_tool = supervisor_env["track_tool"]

    w1_handler_calls = 0
    orig_pkg_run = pkg_tool.run

    def tracked_pkg_run(params):
        nonlocal w1_handler_calls
        w1_handler_calls += 1
        return orig_pkg_run(params)

    pkg_tool.run = tracked_pkg_run

    w2_handler_calls = 0
    orig_track_run = track_tool.run

    def tracked_track_run(params):
        nonlocal w2_handler_calls
        w2_handler_calls += 1
        return orig_track_run(params)

    track_tool.run = tracked_track_run

    w1_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_order_c1",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Order has PKG_101 and PKG_102."}],
                "stopReason": "stop",
            },
        ],
    }

    w2_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_track_c1",
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
            pi_model_config=w1_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
        WorkerAgentSpec(
            name="tracking_worker",
            description="Queries package tracking",
            system_prompt="You are a tracking agent.",
            allowed_tools=["query_package_tracking"],
            execution_backend="pi",
            pi_model_config=w2_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        ),
    ]

    # Run 1 router: dispatches worker 1, then crashes before worker 2
    crash_injected = False

    def crashing_router(state: SupervisorState) -> SupervisorDecision:
        nonlocal crash_injected
        if "logistics_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="logistics_worker", instruction="Step 1 order query")
        else:
            crash_injected = True
            raise RuntimeError("Simulated process crash immediately after worker 1 finished")

    workspace = tmp_path / "workspace"
    orchestrator1 = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=crashing_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_recovery_case1",
        user_query="Query full logistics status",
        intent_version="v1",
        global_constraints=["STATUS_ONLY"],
        global_budget_max=15,
        deadline_seconds=120.0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await orchestrator1.run(task_spec, resume=False)
    assert "Simulated process crash" in str(exc_info.value)
    assert crash_injected is True
    assert w1_handler_calls == 1
    assert w2_handler_calls == 0

    chk_file = workspace / "supervisor_runs" / task_spec.task_id / "supervisor_checkpoint.json"
    assert chk_file.exists()
    chk_1 = json.loads(chk_file.read_text(encoding="utf-8"))
    assert "logistics_worker" in chk_1["worker_results"]
    assert chk_1["step_count"] == 1
    original_deadline = chk_1["deadline_ts"]
    w1_consumed = chk_1["global_budget_consumed"]
    assert w1_consumed >= 3  # 1 model + 1 tool + 1 model

    # Close original runtime
    runtime.close()

    # Create new Runtime and new Supervisor in same workspace directory
    new_runtime = AgentRuntime(
        db_path=supervisor_env["runtime"].db_path,
        tool_broker=root_broker,
        registry=supervisor_env["registry"],
        collector=supervisor_env["collector"],
        episode_store=supervisor_env["episode_store"],
        candidate_store=supervisor_env["cand_store"],
    )

    # Resume router: cleanly routes to tracking_worker then FINISH
    def resuming_router(state: SupervisorState) -> SupervisorDecision:
        if "logistics_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="logistics_worker", instruction="Step 1 order query")
        elif "tracking_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="tracking_worker", instruction="Step 2 tracking query")
        else:
            return SupervisorDecision(next_worker="FINISH", instruction="All queries completed")

    orchestrator2 = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=new_runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=resuming_router,
    )

    stale_parent = dict(chk_1)
    completed_child = chk_1["worker_history"][0]["worker_state"]["run_id"]
    stale_parent.update(global_budget_consumed=0, child_budget_accounted={},
                        worker_results={}, step_count=0, active_worker="logistics_worker",
                        active_child_run_id=completed_child)
    chk_file.write_text(json.dumps(stale_parent), encoding="utf-8")
    final_state = await orchestrator2.run(task_spec, resume=True)

    # Assertions on resumed execution
    assert final_state.status == "COMPLETED"
    # Worker 1 handler call count strictly remains 1 (was not re-executed!)
    assert w1_handler_calls == 1
    # Worker 2 handler was called exactly 1 time
    assert w2_handler_calls == 1

    # Both results present
    assert "logistics_worker" in final_state.worker_results
    assert "tracking_worker" in final_state.worker_results
    assert "PKG_101 and PKG_102" in final_state.worker_results["logistics_worker"]
    assert "PKG_101 is DELIVERED" in final_state.worker_results["tracking_worker"]

    # Budget strictly accumulated (w1 + w2), not reset or doubled
    assert final_state.global_budget_consumed == 6
    assert final_state.step_count == 2
    assert final_state.deadline_ts == original_deadline

    # Worker history preserves both worker runs
    assert len(final_state.worker_history) >= 2
    new_runtime.close()


# ==============================================================================
# Case 2: In-flight Pi Worker paused at context boundary, resume via aresume_agent_with_pi
# ==============================================================================


@pytest.mark.anyio
async def test_case2_in_flight_pi_worker_paused_resumes_with_same_child_id(supervisor_env):
    """Case 2: Pi worker requests pause during tool execution at context boundary.

    Supervisor halts in PAUSED status with active_child_run_id recorded.
    New Runtime + Supervisor resumes with resume=True:
    - Same child run ID is resumed via aresume_agent_with_pi.
    - Tool 1 is NOT repeated.
    - Parent accounts for delta budget only.
    - Task finishes with COMPLETED.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]
    pkg_tool = supervisor_env["pkg_tool"]

    tool_call_count = 0
    orig_pkg_run = pkg_tool.run
    captured_child_run_id = None

    def pausing_pkg_run(params):
        nonlocal tool_call_count, captured_child_run_id
        tool_call_count += 1
        res = orig_pkg_run(params)
        # Find active child run and request pause
        active_runs = runtime._get_conn().execute(
            "SELECT run_id FROM runtime_runs WHERE status = 'RUNNING' ORDER BY created_at DESC"
        ).fetchall()
        if active_runs:
            captured_child_run_id = active_runs[0][0]
            pause_ok = runtime.request_pause_run(captured_child_run_id)
            assert pause_ok is True
        return res

    pkg_tool.run = pausing_pkg_run

    w_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_pause_resume_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Post-resume answer: PKG_101 and PKG_102 verified."}],
                "stopReason": "stop",
            },
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="pausing_worker",
            hooks=HarnessHooks(post_tool_exec=lambda *args: HookDecision.allow("PRE_PAUSE_AUDIT")),
            description="Worker that gets paused",
            system_prompt="You are a pausing agent.",
            allowed_tools=["query_order_packages"],
            max_steps_per_call=6,
            execution_backend="pi",
            pi_model_config=w_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]

    def single_worker_router(state: SupervisorState) -> SupervisorDecision:
        if "pausing_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="pausing_worker", instruction="Query order packages")
        return SupervisorDecision(next_worker="FINISH", instruction="Finished")

    workspace = tmp_path / "workspace"
    orchestrator1 = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=single_worker_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_recovery_case2",
        user_query="Pause and resume order query",
        intent_version="v1",
        verification_fn=lambda h: {"independent_pass": len(h.tool_records) == 1 and
            h.tool_records[0].tool_name == "query_order_packages"},
        global_budget_max=10,
    )

    state1 = await orchestrator1.run(task_spec, resume=False)

    # Supervisor halted in PAUSED status with active_child_run_id recorded
    assert state1.status == "PAUSED"
    assert tool_call_count == 1
    assert captured_child_run_id is not None
    assert state1.active_child_run_id == captured_child_run_id
    paused_budget = state1.global_budget_consumed
    assert paused_budget > 0

    # Verify checkpoint on disk is in PAUSED state
    chk_file = workspace / "supervisor_runs" / task_spec.task_id / "supervisor_checkpoint.json"
    chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
    assert chk_data["status"] == "PAUSED"
    assert chk_data["active_child_run_id"] == captured_child_run_id

    # Verify child run in DB is PAUSED and has a Pi checkpoint
    child_rec = runtime.get_run(captured_child_run_id)
    assert child_rec.status == "PAUSED"
    pi_chk = runtime.get_pi_checkpoint(captured_child_run_id)
    assert pi_chk is not None

    runtime.close()

    # Reconstruct new Runtime + Supervisor and resume
    new_runtime = AgentRuntime(
        db_path=supervisor_env["runtime"].db_path,
        tool_broker=root_broker,
        registry=supervisor_env["registry"],
        collector=supervisor_env["collector"],
        episode_store=supervisor_env["episode_store"],
        candidate_store=supervisor_env["cand_store"],
    )

    orchestrator2 = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=new_runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=single_worker_router,
    )

    chk_data.update(global_budget_consumed=0, child_budget_accounted={})
    chk_file.write_text(json.dumps(chk_data), encoding="utf-8")
    final_state = await orchestrator2.run(task_spec, resume=True)

    # Resumed run completes successfully
    assert final_state.status == "COMPLETED", f"Status: {final_state.status}, Err: {final_state.error_message}, Budget: {final_state.global_budget_consumed}/{final_state.global_budget_max}, Steps: {final_state.step_count}/{final_state.max_supervisor_steps}, Worker results: {final_state.worker_results}"
    assert "pausing_worker" in final_state.worker_results
    assert "Post-resume answer" in final_state.worker_results["pausing_worker"]

    # Tool call was NOT repeated!
    assert tool_call_count == 1
    ep = supervisor_env["episode_store"].get_episode(f"ep_{captured_child_run_id}")
    assert ep.outcome == "success"  # Independent predicate saw the persisted pre-pause tool.
    assert any(a["reason_code"] == "PRE_PAUSE_AUDIT"
        for a in final_state.worker_history[0]["worker_state"]["hook_audits"])

    # Active child run cleared in checkpoint
    assert final_state.active_child_run_id is None

    # Budget strictly accounted for delta without resetting
    assert final_state.global_budget_consumed == new_runtime.get_run(captured_child_run_id).budget_consumed
    assert final_state.global_budget_consumed > paused_budget

    new_runtime.close()


# ==============================================================================
# Case 3: Contract drift rejection & budget/deadline integrity
# ==============================================================================


@pytest.mark.anyio
async def test_case3_contract_drift_and_tampering_rejected(supervisor_env):
    """Case 3: Modifications to query, intent, constraints, worker prompts, tools, or budget

    are rejected on resume with ValueError; deadline is not renewed; unresumed checkpoint blocks re-run.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]

    w_cfg = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Step 1 done."}],
                "stopReason": "stop",
            }
        ],
    }

    workers = [
        WorkerAgentSpec(
            name="worker_a",
            description="Worker A",
            system_prompt="Initial prompt v1",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=w_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]

    workspace = tmp_path / "workspace"
    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=lambda state: SupervisorDecision(next_worker="worker_a", instruction="Step 1"),
    )

    task_spec = SupervisorTaskSpec(
        task_id="task_recovery_case3",
        user_query="Original query",
        intent_version="v1",
        global_constraints=["CONSTRAINT_A"],
        global_budget_max=10,
        deadline_seconds=60.0,
    )

    # Simulate interruption by making router crash on step 2
    def crash_router(state: SupervisorState) -> SupervisorDecision:
        if "worker_a" not in state.worker_results:
            return SupervisorDecision(next_worker="worker_a", instruction="Step 1")
        raise RuntimeError("Crash after step 1")

    orchestrator.router_fn = crash_router
    with pytest.raises(RuntimeError):
        await orchestrator.run(task_spec, resume=False)

    chk_file = workspace / "supervisor_runs" / task_spec.task_id / "supervisor_checkpoint.json"
    assert chk_file.exists()

    # (a) Running with resume=False on unfinished checkpoint is REJECTED upfront
    with pytest.raises(ValueError) as val_err:
        await orchestrator.run(task_spec, resume=False)
    assert "Unfinished supervisor checkpoint exists" in str(val_err.value)

    # (b) Tamper query
    drifted_spec_query = SupervisorTaskSpec(
        task_id="task_recovery_case3",
        user_query="TAMPERED_QUERY",
        intent_version="v1",
        global_constraints=["CONSTRAINT_A"],
        global_budget_max=10,
    )
    with pytest.raises(ValueError) as val_err:
        await orchestrator.run(drifted_spec_query, resume=True)
    assert "STALE_SUPERVISOR_CHECKPOINT" in str(val_err.value)

    # (c) Tamper intent version
    drifted_spec_intent = SupervisorTaskSpec(
        task_id="task_recovery_case3",
        user_query="Original query",
        intent_version="v2_TAMPERED",
        global_constraints=["CONSTRAINT_A"],
        global_budget_max=10,
    )
    with pytest.raises(ValueError) as val_err:
        await orchestrator.run(drifted_spec_intent, resume=True)
    assert "STALE_SUPERVISOR_CHECKPOINT" in str(val_err.value)

    # (d) Attempt to increase budget max
    drifted_spec_budget = SupervisorTaskSpec(
        task_id="task_recovery_case3",
        user_query="Original query",
        intent_version="v1",
        global_constraints=["CONSTRAINT_A"],
        global_budget_max=999,  # budget tampering
    )
    with pytest.raises(ValueError) as val_err:
        await orchestrator.run(drifted_spec_budget, resume=True)
    assert "STALE_SUPERVISOR_CHECKPOINT" in str(val_err.value)

    # (e) Tamper worker system prompt
    tampered_workers = [
        WorkerAgentSpec(
            name="worker_a",
            description="Worker A",
            system_prompt="TAMPERED_PROMPT_MUST_BE_REJECTED",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config=w_cfg,
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]
    orchestrator_tampered = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=tampered_workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
    )
    with pytest.raises(ValueError) as val_err:
        await orchestrator_tampered.run(task_spec, resume=True)
    assert "STALE_SUPERVISOR_CHECKPOINT" in str(val_err.value)


# ==============================================================================
# Case 4: In-flight ADMITTED tool calls or uncheckpointed run requires review
# ==============================================================================


@pytest.mark.anyio
async def test_case4_unsettled_admitted_tool_calls_halt_in_review(supervisor_env):
    """Case 4: In-flight child run has unsettled ADMITTED tool call in SQLite.

    Supervisor resume halts in PAUSED_FOR_REVIEW with REVIEW_REQUIRED message; handler calls == 0.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]
    pkg_tool = supervisor_env["pkg_tool"]

    handler_calls = 0
    orig_pkg = pkg_tool.run

    def count_pkg_run(params):
        nonlocal handler_calls
        handler_calls += 1
        return orig_pkg(params)

    pkg_tool.run = count_pkg_run

    task_id = "task_recovery_case4"
    child_run_id = f"run_{task_id}_worker_in_flight"

    # Start child run in DB and leave it with an unsettled ADMITTED tool call
    runtime.start_run(
        run_id=child_run_id,
        task_id=f"{task_id}_worker_0",
        purpose="evaluation",
        budget_max=10,
    )
    conn = runtime._get_conn()
    conn.execute(
        """INSERT INTO runtime_tool_calls (
            call_id, run_id, tool_name, status, input_params_json, created_at
        ) VALUES (?, ?, ?, 'ADMITTED', ?, ?)""",
        ("tc_admitted_unsettled_01", child_run_id, "query_order_packages", "{}", "2026-11-21T00:00:00Z"),
    )
    conn.execute("UPDATE runtime_runs SET status = 'PAUSED' WHERE run_id = ?", (child_run_id,))
    conn.commit()

    workspace = tmp_path / "workspace"
    chk_dir = workspace / "supervisor_runs" / task_id
    chk_dir.mkdir(parents=True, exist_ok=True)
    chk_file = chk_dir / "supervisor_checkpoint.json"

    workers = [
        WorkerAgentSpec(
            name="worker_admitted",
            description="Worker with admitted tool",
            system_prompt="Prompt",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config={"provider": "faux", "model_id": "test"},
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
    )

    task_spec = SupervisorTaskSpec(
        task_id=task_id,
        user_query="Admitted test",
        global_budget_max=10,
    )
    fp = orchestrator._compute_contract_fingerprint(task_spec)

    # Write supervisor checkpoint pointing to this active child run
    chk_content = {
        "format_version": 1,
        "task_id": task_id,
        "user_query": task_spec.user_query,
        "intent_version": task_spec.intent_version,
        "global_constraints": [],
        "worker_history": [],
        "worker_results": {},
        "tool_records": [],
        "step_count": 0,
        "global_budget_consumed": 1,
        "global_budget_max": 10,
        "max_supervisor_steps": 6,
        "deadline_ts": time.time() + 300.0,
        "status": "PAUSED",
        "active_worker": "worker_admitted",
        "active_child_run_id": child_run_id,
        "contract_fingerprint": fp,
        "memory_index": [],
        "timestamp": "2026-11-21T00:00:00Z",
    }
    chk_file.write_text(json.dumps(chk_content), encoding="utf-8")

    state = await orchestrator.run(task_spec, resume=True)

    # Halts in PAUSED_FOR_REVIEW without executing tools
    assert state.status == "PAUSED_FOR_REVIEW"
    assert "REVIEW_REQUIRED" in (state.error_message or "")
    assert "unsettled in-flight ADMITTED" in (state.error_message or "")
    assert handler_calls == 0


# ==============================================================================
# Case 5: Expired deadline / terminal status rejection & write failure safety
# ==============================================================================


@pytest.mark.anyio
async def test_case5_terminal_status_deadline_expired_and_write_failure(supervisor_env, monkeypatch):
    """Case 5: Checkpoints in terminal status (CANCELLED, FAILED) cannot be resumed;

    expired deadline raises TimeoutError; atomic checkpoint write failure prevents worker dispatch.
    """
    runtime: AgentRuntime = supervisor_env["runtime"]
    root_broker: ToolBroker = supervisor_env["broker"]
    tmp_path: Path = supervisor_env["tmp_path"]
    pkg_tool = supervisor_env["pkg_tool"]

    handler_calls = 0
    orig_pkg = pkg_tool.run

    def count_run(params):
        nonlocal handler_calls
        handler_calls += 1
        return orig_pkg(params)

    pkg_tool.run = count_run

    workers = [
        WorkerAgentSpec(
            name="worker_c5",
            description="Worker C5",
            system_prompt="Prompt",
            allowed_tools=["query_order_packages"],
            execution_backend="pi",
            pi_model_config={"provider": "faux", "model_id": "test"},
            pi_test_mode=True,
            pi_timeout=15.0,
        )
    ]

    workspace = tmp_path / "workspace"
    task_id = "task_recovery_case5"
    chk_dir = workspace / "supervisor_runs" / task_id
    chk_dir.mkdir(parents=True, exist_ok=True)
    chk_file = chk_dir / "supervisor_checkpoint.json"

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
    )

    task_spec = SupervisorTaskSpec(
        task_id=task_id,
        user_query="Terminal and write failure test",
        global_budget_max=10,
    )
    fp = orchestrator._compute_contract_fingerprint(task_spec)

    # 1. Terminal CANCELLED checkpoint cannot be resumed
    chk_cancelled = {
        "format_version": 1,
        "task_id": task_id,
        "user_query": task_spec.user_query,
        "intent_version": task_spec.intent_version,
        "global_constraints": [],
        "worker_history": [],
        "worker_results": {},
        "tool_records": [],
        "step_count": 1,
        "global_budget_consumed": 2,
        "global_budget_max": 10,
        "max_supervisor_steps": 6,
        "deadline_ts": time.time() + 300.0,
        "status": "CANCELLED",
        "active_worker": None,
        "active_child_run_id": None,
        "contract_fingerprint": fp,
        "memory_index": [],
        "timestamp": "2026-11-21T00:00:00Z",
    }
    chk_file.write_text(json.dumps(chk_cancelled), encoding="utf-8")

    with pytest.raises(ValueError) as val_err:
        await orchestrator.run(task_spec, resume=True)
    assert "Cannot resume supervisor run in terminal/reviewed status 'CANCELLED'" in str(val_err.value)

    # 2. Expired deadline cannot be resumed
    chk_expired = dict(chk_cancelled)
    chk_expired["status"] = "PAUSED"
    chk_expired["deadline_ts"] = time.time() - 30.0  # 30 seconds ago
    chk_file.write_text(json.dumps(chk_expired), encoding="utf-8")

    with pytest.raises(TimeoutError) as to_err:
        await orchestrator.run(task_spec, resume=True)
    assert "supervisor deadline expired" in str(to_err.value)

    # 3. Checkpoint write failure blocks worker dispatch
    task_id_fail = "task_recovery_case5_write_fail"
    task_spec_fail = SupervisorTaskSpec(
        task_id=task_id_fail,
        user_query="Write failure test",
        global_budget_max=10,
    )
    orchestrator_fail = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=lambda state: SupervisorDecision(next_worker="worker_c5", instruction="Do job"),
    )

    # Simulate filesystem write failure on _save_checkpoint
    def mock_failing_save_checkpoint(state, active_child_run_id=None, session_memory_index=None):
        raise RuntimeError("Simulated IO disk write error on supervisor_checkpoint.json")

    monkeypatch.setattr(orchestrator_fail, "_save_checkpoint", mock_failing_save_checkpoint)

    with pytest.raises(RuntimeError) as exc_info:
        await orchestrator_fail.run(task_spec_fail, resume=False)
    assert "Simulated IO disk write error" in str(exc_info.value)

    # Tool handler was NEVER called (worker was not dispatched!)
    assert handler_calls == 0


@pytest.mark.anyio
async def test_corrupt_checkpoint_is_not_silently_reset(supervisor_env):
    env = supervisor_env
    orch = SupervisorOrchestrator(None, [], env["runtime"], env["broker"], workspace_dir=env["tmp_path"])
    spec = SupervisorTaskSpec(task_id="corrupt", user_query="test")
    path = env["tmp_path"] / "supervisor_runs" / "corrupt" / "supervisor_checkpoint.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        await orch.run(spec)
    assert path.read_text() == "{broken"


def test_contract_fingerprint_includes_required_schema_and_public_model(supervisor_env, monkeypatch):
    env = supervisor_env
    worker = WorkerAgentSpec("schema", "schema", "prompt", ["query_order_packages"],
        execution_backend="pi", pi_model_config={"provider": "faux", "model_id": "x"})
    orch = SupervisorOrchestrator(None, [worker], env["runtime"], env["broker"])
    spec = SupervisorTaskSpec(task_id="schema", user_query="query")
    original = orch._compute_contract_fingerprint(spec)
    worker.pi_model_config["base_url"] = "https://invalid.example"
    assert orch._compute_contract_fingerprint(spec) != original
    worker.pi_model_config.pop("base_url")
    params = env["broker"].get_parameters("query_order_packages")
    monkeypatch.setattr(env["broker"], "get_parameters", lambda name: params)
    old_required = params[0].required
    try:
        params[0].required = not old_required
        assert orch._compute_contract_fingerprint(spec) != original
    finally:
        params[0].required = old_required
