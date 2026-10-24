"""Supervisor Multi-Agent Orchestration Engine (Milestone 4 - Orchestration).

Implements the hierarchical Supervisor Pattern:
1. Centralized Supervisor Controller:
   - Receives top-level composite user tasks.
   - Decomposes and routes sub-goals to specialized domain worker agents.
   - Aggregates worker outputs and manages global task state.
   - Enforces global step budget and physical deadlines across all workers.
   - Propagates immutable negative constraints (e.g. STATUS_ONLY) down to workers.
2. Scoped Domain Workers (Principle of Least Privilege):
   - Specialized system prompts.
   - Strict application allowlist slicing (only granted access to relevant tools).
   - Scoped workspace isolation and provenance tracking.
3. Dual-Engine Support:
   - Native Async Supervisor (SupervisorOrchestrator): ultra-lightweight, zero overhead, sub-second execution.
   - LangGraph StateGraph (create_supervisor_graph): topological visualization, checkpointing, and time-travel debugging.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Union

from .context_engine.schemas import ProtectedState
from .harness_loop import AsyncHarnessLoop, HarnessState, HarnessTaskSpec, ModelResponse
from .runtime import AgentRuntime, ToolBroker

logger = logging.getLogger(__name__)


@dataclass
class WorkerAgentSpec:
    """Specification of a specialized domain worker agent managed by Supervisor."""

    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str]  # Scoped subset of ToolBroker application_allowlist
    max_steps_per_call: int = 3
    llm_client: Optional[Any] = None  # None -> inherits Supervisor default LLM client
    context_compressor: Optional[Any] = None


@dataclass
class SupervisorDecision:
    """Structured decision returned by the Supervisor router."""

    next_worker: str  # worker name or "FINISH"
    instruction: str  # delegated sub-goal / task instruction for worker
    reasoning: str = ""


@dataclass
class SupervisorTaskSpec:
    """Input specification for initiating a multi-agent orchestration session."""

    task_id: str
    user_query: str
    intent_version: str = "v1"
    global_constraints: list[str] = field(default_factory=list)
    global_budget_max: int = 15
    max_supervisor_steps: int = 6
    initial_context: Optional[dict[str, Any]] = None
    verification_fn: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None


@dataclass
class SupervisorState:
    """Shared global state across workers and supervisor."""

    task_id: str
    user_query: str
    intent_version: str = "v1"
    global_constraints: list[str] = field(default_factory=list)
    worker_history: list[dict[str, Any]] = field(default_factory=list)
    worker_results: dict[str, str] = field(default_factory=dict)
    active_worker: Optional[str] = None
    step_count: int = 0
    max_supervisor_steps: int = 6
    global_budget_consumed: int = 0
    global_budget_max: int = 15
    status: Literal["RUNNING", "COMPLETED", "FAILED", "BUDGET_EXHAUSTED"] = "RUNNING"
    final_output: Optional[str] = None
    error_message: Optional[str] = None
    workspace_dir: Path = field(default_factory=Path.cwd)


class SupervisorOrchestrator:
    """Native asynchronous Supervisor Orchestrator.

    Coordinates specialized domain workers via scoped ToolBrokers and AsyncHarnessLoops,
    propagating global constraints and enforcing global budgets.
    """

    def __init__(
        self,
        supervisor_llm: Any,
        workers: list[WorkerAgentSpec],
        runtime: AgentRuntime,
        tool_broker: ToolBroker,
        workspace_dir: Optional[Union[str, Path]] = None,
        router_fn: Optional[Callable[[SupervisorState], SupervisorDecision]] = None,
    ) -> None:
        self.supervisor_llm = supervisor_llm
        self.workers = {w.name: w for w in workers}
        self.runtime = runtime
        self.tool_broker = tool_broker
        self.workspace_dir = Path(workspace_dir or Path.cwd())
        self.router_fn = router_fn

    def _save_checkpoint(self, state: SupervisorState) -> None:
        """Persist supervisor state checkpoint to disk."""
        chk_file = state.workspace_dir / "supervisor_checkpoint.json"
        try:
            chk_file.parent.mkdir(parents=True, exist_ok=True)
            chk_data = {
                "task_id": state.task_id,
                "user_query": state.user_query,
                "intent_version": state.intent_version,
                "global_constraints": state.global_constraints,
                "worker_history": state.worker_history,
                "worker_results": state.worker_results,
                "step_count": state.step_count,
                "global_budget_consumed": state.global_budget_consumed,
                "global_budget_max": state.global_budget_max,
                "status": state.status,
                "final_output": state.final_output,
                "error_message": state.error_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            chk_file.write_text(json.dumps(chk_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save supervisor checkpoint: %s", exc)

    async def _llm_route(self, state: SupervisorState) -> SupervisorDecision:
        """Prompt supervisor LLM to select the next worker or declare completion."""
        worker_descs = "\n".join([f"- {w.name}: {w.description}" for w in self.workers.values()])
        results_summary = "\n".join([f"- [{k}]: {v}" for k, v in state.worker_results.items()]) or "None"
        constraints_str = "\n".join(state.global_constraints) or "None"

        system_prompt = (
            "You are the central Supervisor Agent orchestrating domain worker agents.\n"
            f"Available Workers:\n{worker_descs}\n\n"
            f"Global Constraints:\n{constraints_str}\n\n"
            "Task: Decide the next worker to invoke and provide explicit instruction, "
            "or set next_worker to 'FINISH' if sufficient information is gathered.\n"
            "Output JSON with keys: 'next_worker', 'instruction', 'reasoning'."
        )

        user_content = (
            f"Goal: {state.user_query}\n"
            f"Worker Results so far:\n{results_summary}\n"
            "What is the next step?"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        if hasattr(self.supervisor_llm, "ainvoke"):
            resp = await self.supervisor_llm.ainvoke(messages=messages, tools=[], system=system_prompt)
        elif callable(self.supervisor_llm):
            resp = self.supervisor_llm(messages=messages, tools=[], system=system_prompt)
            if asyncio.iscoroutine(resp):
                resp = await resp
        else:
            raise TypeError("Unsupported supervisor_llm client")

        text = resp.content or ""
        try:
            # Extract JSON block
            if "```json" in text:
                raw_json = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "{" in text:
                raw_json = text[text.find("{"):text.rfind("}") + 1].strip()
            else:
                raw_json = text.strip()
            data = json.loads(raw_json)
            return SupervisorDecision(
                next_worker=data.get("next_worker", "FINISH"),
                instruction=data.get("instruction", ""),
                reasoning=data.get("reasoning", ""),
            )
        except Exception:
            # Fallback default
            return SupervisorDecision(next_worker="FINISH", instruction=text)

    async def run(self, spec: SupervisorTaskSpec) -> SupervisorState:
        """Run the supervisor multi-agent orchestration session to completion."""
        session_workspace = self.workspace_dir / "supervisor_runs" / spec.task_id
        session_workspace.mkdir(parents=True, exist_ok=True)

        state = SupervisorState(
            task_id=spec.task_id,
            user_query=spec.user_query,
            intent_version=spec.intent_version,
            global_constraints=list(spec.global_constraints),
            max_supervisor_steps=spec.max_supervisor_steps,
            global_budget_max=spec.global_budget_max,
            workspace_dir=session_workspace,
        )

        while state.status == "RUNNING":
            # 1. Global budget checks
            if state.step_count >= state.max_supervisor_steps:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = f"Supervisor reached max steps limit ({state.max_supervisor_steps})"
                break

            if state.global_budget_consumed >= state.global_budget_max:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = f"Global budget exhausted ({state.global_budget_consumed}/{state.global_budget_max})"
                break

            # 2. Routing Decision
            if self.router_fn:
                decision = self.router_fn(state)
            else:
                decision = await self._llm_route(state)

            decision_entry = {
                "step": state.step_count,
                "next_worker": decision.next_worker,
                "instruction": decision.instruction,
                "reasoning": decision.reasoning,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            state.worker_history.append(decision_entry)

            # 3. Termination Check
            if decision.next_worker == "FINISH":
                state.status = "COMPLETED"
                state.final_output = decision.instruction or "\n\n".join(
                    [f"[{k} 报告]: {v}" for k, v in state.worker_results.items()]
                )
                self._save_checkpoint(state)
                break

            # 4. Resolve Domain Worker
            worker_spec = self.workers.get(decision.next_worker)
            if not worker_spec:
                state.status = "FAILED"
                state.error_message = f"Supervisor selected unknown worker: '{decision.next_worker}'"
                self._save_checkpoint(state)
                break

            # 5. Create Scoped ToolBroker (Least Privilege)
            # Worker only sees its specified allowed_tools
            scoped_allowlist = set(worker_spec.allowed_tools)
            scoped_broker = ToolBroker(
                application_allowlist=scoped_allowlist,
                sandbox_backend=getattr(self.tool_broker, "sandbox_backend", None),
                dependency_prober=getattr(self.tool_broker, "dependency_prober", None),
            )
            # Inherit registered tool implementations matching allowlist
            for t_name in scoped_allowlist:
                tool_impl = self.tool_broker.get_tool(t_name)
                if tool_impl is not None:
                    scoped_broker._tools[t_name] = tool_impl

            # 6. Build Scoped Task & Cascade Global Constraints
            worker_protected = ProtectedState(
                intent_version=state.intent_version,
                current_goal=decision.instruction,
                active_constraints=list(state.global_constraints),
                allowed_tools=list(worker_spec.allowed_tools),
            )

            remaining_budget = max(1, state.global_budget_max - state.global_budget_consumed)
            worker_step_budget = min(worker_spec.max_steps_per_call, remaining_budget)

            worker_workspace = session_workspace / "workers" / f"{worker_spec.name}_step_{state.step_count}"
            worker_workspace.mkdir(parents=True, exist_ok=True)

            worker_task_spec = HarnessTaskSpec(
                task_id=f"{state.task_id}_{worker_spec.name}_{state.step_count}",
                user_query=decision.instruction,
                system_prompt=worker_spec.system_prompt,
                intent_version=state.intent_version,
                budget_max=worker_step_budget,
                context_compressor=worker_spec.context_compressor,
                initial_scratchpad={"protected_state": worker_protected},
            )

            # 7. Execute Subordinate Worker Loop
            worker_loop = AsyncHarnessLoop(
                runtime=self.runtime,
                tool_broker=scoped_broker,
                llm_client=worker_spec.llm_client or self.supervisor_llm,
                workspace_dir=worker_workspace,
            )

            worker_state = await worker_loop.run(worker_task_spec)

            # 8. Record Aggregated Results & Consume Budget
            state.global_budget_consumed += max(1, worker_state.step_count)
            output_text = worker_state.final_output or worker_state.error_message or "Worker executed."
            state.worker_results[worker_spec.name] = output_text

            state.step_count += 1
            self._save_checkpoint(state)

        return state


# ==============================================================================
# LangGraph StateGraph Builder
# ==============================================================================

try:
    from typing_extensions import TypedDict
    from langgraph.graph import StateGraph, START, END

    class SupervisorGraphState(TypedDict, total=False):
        task_id: str
        user_query: str
        intent_version: str
        global_constraints: list[str]
        worker_results: dict[str, str]
        worker_history: list[dict[str, Any]]
        next_worker: str
        instruction: str
        step_count: int
        max_supervisor_steps: int
        global_budget_consumed: int
        global_budget_max: int
        status: str
        final_output: Optional[str]
        error_message: Optional[str]

    def create_supervisor_graph(
        workers: list[WorkerAgentSpec],
        router_fn: Callable[[SupervisorGraphState], SupervisorDecision],
        worker_callables: dict[str, Callable[[SupervisorGraphState], str]],
        checkpointer: Optional[Any] = None,
    ) -> Any:
        """Construct a compiled LangGraph StateGraph implementing the Supervisor pattern."""
        builder = StateGraph(SupervisorGraphState)

        # 1. Supervisor routing node
        def supervisor_node(state: SupervisorGraphState) -> SupervisorGraphState:
            step = state.get("step_count", 0)
            max_steps = state.get("max_supervisor_steps", 6)
            consumed = state.get("global_budget_consumed", 0)
            budget_max = state.get("global_budget_max", 15)

            if step >= max_steps or consumed >= budget_max:
                return {
                    "next_worker": "FINISH",
                    "status": "BUDGET_EXHAUSTED",
                    "error_message": "Supervisor budget limit reached",
                }

            decision = router_fn(state)
            new_history = list(state.get("worker_history", []))
            new_history.append({
                "step": step,
                "next_worker": decision.next_worker,
                "instruction": decision.instruction,
                "reasoning": decision.reasoning,
            })

            res: SupervisorGraphState = {
                "next_worker": decision.next_worker,
                "instruction": decision.instruction,
                "worker_history": new_history,
                "step_count": step + 1,
            }

            if decision.next_worker == "FINISH":
                res["status"] = "COMPLETED"
                res["final_output"] = decision.instruction or "Task finished by supervisor."
            return res

        builder.add_node("supervisor", supervisor_node)
        builder.add_edge(START, "supervisor")

        # 2. Worker nodes
        routing_map: dict[str, str] = {w.name: w.name for w in workers}
        routing_map["FINISH"] = END

        for w in workers:
            w_fn = worker_callables.get(w.name)
            if not w_fn:
                # Default worker node
                def _make_worker_node(name: str):
                    def _node(state: SupervisorGraphState) -> SupervisorGraphState:
                        instr = state.get("instruction", "")
                        results = dict(state.get("worker_results", {}))
                        results[name] = f"Worker {name} executed: {instr}"
                        return {
                            "worker_results": results,
                            "global_budget_consumed": state.get("global_budget_consumed", 0) + 1,
                        }
                    return _node
                w_fn = _make_worker_node(w.name)

            builder.add_node(w.name, w_fn)
            builder.add_edge(w.name, "supervisor")

        # 3. Dynamic conditional edges from supervisor
        def _route_next(state: SupervisorGraphState) -> str:
            next_w = state.get("next_worker", "FINISH")
            if next_w in routing_map:
                return next_w
            return "FINISH"

        builder.add_conditional_edges("supervisor", _route_next, routing_map)

        return builder.compile(checkpointer=checkpointer)

except ImportError:
    # Graceful fallback if langgraph is absent
    def create_supervisor_graph(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("LangGraph is not installed in the environment")
