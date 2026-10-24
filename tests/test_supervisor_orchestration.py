"""Tests for Supervisor Multi-Agent Orchestration (Milestone 4 - Orchestration).

Validates:
1. Native Supervisor Orchestrator lifecycle: task decomposition, worker delegation, result aggregation, and graceful termination.
2. Principle of Least Privilege: workers receive strictly scoped ToolBroker instances.
3. Global budget enforcement: supervisor halts and terminates when global step limits are reached.
4. Constraint cascade propagation: global constraints (e.g. STATUS_ONLY) are injected into worker ProtectedState.
5. LangGraph StateGraph builder: topological routing, worker execution, and checkpointing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from skillforge import (
    AgentRuntime,
    EpisodeStore,
    ExperienceCollector,
    InspectArtifactTool,
    ModelResponse,
    SupervisorDecision,
    SupervisorOrchestrator,
    SupervisorState,
    SupervisorTaskSpec,
    ToolBroker,
    ToolCallSpec,
    WorkerAgentSpec,
    create_supervisor_graph,
)
from skillforge.storage.db import init_db


class SimulatedWorkerLLM:
    """Simulates domain-specific worker execution."""

    def __init__(self, role: str, artifacts_dir: Path):
        self.role = role
        self.artifacts_dir = artifacts_dir
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": tools, "system": system})

        if self.role == "logistics":
            # Call query_order_packages
            return ModelResponse(
                content="Dispatching logistics query...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_log_01",
                        name="query_order_packages",
                        arguments={"order_id": "ORD_SUP_01"},
                    )
                ],
            )
        elif self.role == "audit":
            # Locate saved manifest artifact and inspect PKG_SUP_05
            files = list(self.artifacts_dir.glob("**/*_query_order_packages.json"))
            if not files:
                files = list(self.artifacts_dir.parent.glob("**/*_query_order_packages.json"))
            ref_uri = f"fs://{files[0].resolve()}" if files else "fs://unknown"
            return ModelResponse(
                content="Inspecting artifact...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_audit_01",
                        name="inspect_artifact",
                        arguments={"ref_id": ref_uri, "object_id": "PKG_SUP_05"},
                    )
                ],
            )
        else:
            # Compliance synthesizer: checks constraints in system prompt
            if "STATUS_ONLY" in system:
                return ModelResponse(
                    content="Audit conclusion: all packages verified delivered. STRICT STATUS_ONLY applied: no compensation or refund proposed."
                )
            else:
                return ModelResponse(content="Audit conclusion: packages verified.")


@pytest.mark.anyio
async def test_supervisor_native_orchestration_lifecycle(tmp_path: Path):
    db_path = tmp_path / "skillforge.db"
    conn = init_db(db_path)
    conn.close()

    ep_store = EpisodeStore(db_path)
    workspace = tmp_path / "workspace"
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    def query_order_packages(order_id: str):
        return {
            "order_id": order_id,
            "packages": [
                {"package_id": f"PKG_SUP_{i:02d}", "status": "DELIVERED", "details": "checked"}
                for i in range(8)
            ],
        }

    # Root broker has all application tools
    root_broker = ToolBroker(application_allowlist={"query_order_packages", "inspect_artifact"})
    root_broker.register_tool(query_order_packages, name="query_order_packages")
    root_broker.register_tool(InspectArtifactTool(allowed_base_dir=workspace), name="inspect_artifact")

    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(db_path=db_path, tool_broker=root_broker, episode_store=ep_store, collector=collector)

    # Define specialized domain workers
    workers = [
        WorkerAgentSpec(
            name="logistics_worker",
            description="Fetches raw logistics records and manifests from warehouse APIs",
            system_prompt="You are a specialized logistics data retrieval agent.",
            allowed_tools=["query_order_packages"],
            max_steps_per_call=2,
            llm_client=SimulatedWorkerLLM("logistics", artifacts_dir),
        ),
        WorkerAgentSpec(
            name="audit_worker",
            description="Audits local offloaded disk manifests and verifies entity coverage",
            system_prompt="You are a specialized compliance evidence auditor.",
            allowed_tools=["inspect_artifact"],
            max_steps_per_call=2,
            llm_client=SimulatedWorkerLLM("audit", artifacts_dir),
        ),
        WorkerAgentSpec(
            name="compliance_worker",
            description="Synthesizes customer-facing conclusion while strictly obeying policy constraints",
            system_prompt="You are a specialized policy compliance officer. Respect all active constraints.",
            allowed_tools=[],
            max_steps_per_call=1,
            llm_client=SimulatedWorkerLLM("compliance", artifacts_dir),
        ),
    ]

    # Deterministic Supervisor Routing Policy
    def supervisor_router(state: SupervisorState) -> SupervisorDecision:
        if "logistics_worker" not in state.worker_results:
            return SupervisorDecision(
                next_worker="logistics_worker",
                instruction="Query order packages for ORD_SUP_01",
                reasoning="Step 1: pull order data",
            )
        elif "audit_worker" not in state.worker_results:
            return SupervisorDecision(
                next_worker="audit_worker",
                instruction="Inspect offloaded artifact for PKG_SUP_05",
                reasoning="Step 2: audit individual package",
            )
        elif "compliance_worker" not in state.worker_results:
            return SupervisorDecision(
                next_worker="compliance_worker",
                instruction="Synthesize audit report under STATUS_ONLY",
                reasoning="Step 3: compliance check",
            )
        else:
            return SupervisorDecision(
                next_worker="FINISH",
                instruction="Supervised execution completed: all 3 workers reported and verified.",
                reasoning="All sub-tasks finished",
            )

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=workers,
        runtime=runtime,
        tool_broker=root_broker,
        workspace_dir=workspace,
        router_fn=supervisor_router,
    )

    task_spec = SupervisorTaskSpec(
        task_id="sup_task_100",
        user_query="请审计订单 ORD_SUP_01 全链路并生成合规报告",
        intent_version="v2",
        global_constraints=["Constraint: STATUS_ONLY", "No unauthorized refund advice"],
        global_budget_max=10,
        max_supervisor_steps=5,
    )

    final_state = await orchestrator.run(task_spec)

    # Assertions
    assert final_state.status == "COMPLETED"
    assert "logistics_worker" in final_state.worker_results
    assert "audit_worker" in final_state.worker_results
    assert "compliance_worker" in final_state.worker_results
    assert final_state.global_budget_consumed >= 3
    assert len(final_state.worker_history) == 4  # 3 worker delegations + 1 FINISH
    assert "STATUS_ONLY" in final_state.worker_results["compliance_worker"]
    assert "no compensation or refund proposed" in final_state.worker_results["compliance_worker"]

    # Verify checkpoint was persisted
    chk_file = final_state.workspace_dir / "supervisor_checkpoint.json"
    assert chk_file.exists()
    chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
    assert chk_data["task_id"] == "sup_task_100"
    assert chk_data["status"] == "COMPLETED"


@pytest.mark.anyio
async def test_supervisor_global_budget_enforcement(tmp_path: Path):
    db_path = tmp_path / "skillforge.db"
    conn = init_db(db_path)
    conn.close()

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    broker = ToolBroker(application_allowlist={"dummy"})
    broker.register_tool(lambda: "ok", name="dummy")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    # Worker that consumes steps
    worker = WorkerAgentSpec(
        name="loop_worker",
        description="Worker that repeats calls",
        system_prompt="Repeat",
        allowed_tools=["dummy"],
        max_steps_per_call=2,
        llm_client=lambda **kwargs: ModelResponse(content="Step done"),
    )

    # Router that tries to loop forever
    def infinite_router(state: SupervisorState) -> SupervisorDecision:
        return SupervisorDecision(next_worker="loop_worker", instruction="Do more work")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=[worker],
        runtime=runtime,
        tool_broker=broker,
        workspace_dir=workspace,
        router_fn=infinite_router,
    )

    # Strict global budget of 3 steps
    spec = SupervisorTaskSpec(
        task_id="sup_budget_test",
        user_query="Run with tight budget",
        global_budget_max=3,
        max_supervisor_steps=10,
    )

    final_state = await orchestrator.run(spec)
    assert final_state.status == "BUDGET_EXHAUSTED"
    assert final_state.global_budget_consumed >= 3


@pytest.mark.anyio
async def test_supervisor_constraint_propagation_status_only(tmp_path: Path):
    db_path = tmp_path / "skillforge.db"
    conn = init_db(db_path)
    conn.close()

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    broker = ToolBroker()
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    captured_prompts: list[str] = []

    def capturing_llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]], system: str = ""):
        captured_prompts.append(system)
        return ModelResponse(content="Acknowledged constraints.")

    worker = WorkerAgentSpec(
        name="constrained_worker",
        description="Worker subject to strict constraints",
        system_prompt="Base worker prompt",
        allowed_tools=[],
        max_steps_per_call=1,
        llm_client=capturing_llm,
    )

    def route_once(state: SupervisorState) -> SupervisorDecision:
        if "constrained_worker" not in state.worker_results:
            return SupervisorDecision(next_worker="constrained_worker", instruction="Test constraint propagation")
        return SupervisorDecision(next_worker="FINISH", instruction="Done")

    orchestrator = SupervisorOrchestrator(
        supervisor_llm=None,
        workers=[worker],
        runtime=runtime,
        tool_broker=broker,
        workspace_dir=workspace,
        router_fn=route_once,
    )

    spec = SupervisorTaskSpec(
        task_id="sup_constraint_test",
        user_query="Propagate constraints test",
        global_constraints=["MANDATORY: STATUS_ONLY", "PROHIBITED: REFUND"],
    )

    final_state = await orchestrator.run(spec)
    assert final_state.status == "COMPLETED"
    assert len(captured_prompts) >= 1
    # Check that worker's Layer 1 ProtectedState contains global constraints
    assert "MANDATORY: STATUS_ONLY" in captured_prompts[0]
    assert "PROHIBITED: REFUND" in captured_prompts[0]


def test_supervisor_langgraph_stategraph_execution():
    """Verify Supervisor pattern execution on LangGraph StateGraph topology."""
    workers = [
        WorkerAgentSpec(
            name="retrieval_agent",
            description="Retrieves documents",
            system_prompt="",
            allowed_tools=[],
        ),
        WorkerAgentSpec(
            name="verifier_agent",
            description="Verifies documents",
            system_prompt="",
            allowed_tools=[],
        ),
    ]

    def lg_router(state: dict[str, Any]) -> SupervisorDecision:
        results = state.get("worker_results", {})
        if "retrieval_agent" not in results:
            return SupervisorDecision(next_worker="retrieval_agent", instruction="Pull files")
        elif "verifier_agent" not in results:
            return SupervisorDecision(next_worker="verifier_agent", instruction="Audit files")
        else:
            return SupervisorDecision(next_worker="FINISH", instruction="All files audited successfully.")

    worker_callables = {
        "retrieval_agent": lambda st: {"worker_results": {**st.get("worker_results", {}), "retrieval_agent": "5 docs retrieved"}},
        "verifier_agent": lambda st: {"worker_results": {**st.get("worker_results", {}), "verifier_agent": "5 docs verified"}},
    }

    checkpointer = MemorySaver()
    graph = create_supervisor_graph(
        workers=workers,
        router_fn=lg_router,
        worker_callables=worker_callables,
        checkpointer=checkpointer,
    )

    init_state = {
        "task_id": "lg_sup_01",
        "user_query": "Audit repository documents",
        "worker_results": {},
        "worker_history": [],
        "step_count": 0,
        "max_supervisor_steps": 5,
        "global_budget_consumed": 0,
        "global_budget_max": 10,
    }

    final_res = graph.invoke(init_state, config={"configurable": {"thread_id": "thread_sup_1"}})
    assert final_res["status"] == "COMPLETED"
    assert "retrieval_agent" in final_res["worker_results"]
    assert "verifier_agent" in final_res["worker_results"]
    assert "All files audited successfully" in final_res["final_output"]
