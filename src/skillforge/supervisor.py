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
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Union

from .context_engine.schemas import MemoryIndex, ProtectedState
from .harness_loop import AsyncHarnessLoop, HarnessHooks, HarnessState, HarnessTaskSpec, ModelResponse, compose_hooks
from .models import ToolCallRecord
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
    hooks: Optional[HarnessHooks] = None
    execution_backend: Literal["legacy", "pi"] = "legacy"
    pi_model_config: Optional[dict[str, Any]] = None
    pi_test_mode: Optional[bool] = None
    pi_timeout: float = 30.0


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
    deadline_seconds: Optional[float] = None


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
    status: Literal[
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "BUDGET_EXHAUSTED",
        "CANCELLED",
        "PAUSED",
        "PAUSED_FOR_REVIEW",
        "TIMED_OUT",
    ] = "RUNNING"
    final_output: Optional[str] = None
    error_message: Optional[str] = None
    workspace_dir: Path = field(default_factory=Path.cwd)
    tool_records: list[ToolCallRecord] = field(default_factory=list)
    deadline_ts: Optional[float] = None
    active_child_run_id: Optional[str] = None
    contract_fingerprint: Optional[str] = None
    child_budget_accounted: dict[str, int] = field(default_factory=dict)


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
        hooks: Optional[HarnessHooks] = None,
    ) -> None:
        self.supervisor_llm = supervisor_llm
        self.workers = {w.name: w for w in workers}
        self.runtime = runtime
        self.tool_broker = tool_broker
        self.workspace_dir = Path(workspace_dir or Path.cwd())
        self.router_fn = router_fn
        self.hooks = hooks
        self.last_state: Optional[SupervisorState] = None

    def _compute_contract_fingerprint(self, spec: SupervisorTaskSpec) -> str:
        """Compute deterministic fingerprint of task contract and worker configurations."""
        parent_allowlist = set(self.tool_broker.application_allowlist or set())
        workers_info = []
        for name in sorted(self.workers.keys()):
            w = self.workers[name]
            scoped_tools = sorted(list(set(w.allowed_tools) & parent_allowlist))
            from .pi_adapter import tool_parameters_to_json_schema
            tool_schemas = {
                tname: tool_parameters_to_json_schema([
                    p if isinstance(p, dict) else p.model_dump()
                    for p in self.tool_broker.get_parameters(tname)
                ])
                for tname in scoped_tools
            }
            model_cfg_clean = None
            if w.pi_model_config and isinstance(w.pi_model_config, dict):
                model_cfg_clean = {
                    k: w.pi_model_config[k]
                    for k in ("provider", "model_id", "model", "temperature", "max_tokens", "api", "base_url", "reasoning")
                    if k in w.pi_model_config and w.pi_model_config[k] is not None
                }
            workers_info.append({
                "name": w.name,
                "system_prompt": w.system_prompt,
                "execution_backend": w.execution_backend,
                "scoped_tools": scoped_tools,
                "tool_schemas": tool_schemas,
                "model_config": model_cfg_clean,
                "max_steps_per_call": w.max_steps_per_call,
                "pi_timeout": w.pi_timeout,
            })

        fp_data = {
            "task_id": spec.task_id,
            "deadline_seconds": spec.deadline_seconds,
            "user_query": spec.user_query,
            "intent_version": spec.intent_version,
            "global_constraints": sorted(list(spec.global_constraints)),
            "global_budget_max": spec.global_budget_max,
            "max_supervisor_steps": spec.max_supervisor_steps,
            "workers": workers_info,
        }
        return hashlib.sha256(json.dumps(fp_data, sort_keys=True).encode("utf-8")).hexdigest()

    def _save_checkpoint(
        self,
        state: SupervisorState,
        active_child_run_id: Optional[str] = None,
        session_memory_index: Optional[MemoryIndex] = None,
    ) -> None:
        """Persist supervisor state checkpoint atomically to disk."""
        chk_file = state.workspace_dir / "supervisor_checkpoint.json"
        try:
            chk_file.parent.mkdir(parents=True, exist_ok=True)

            def _serialize(obj: Any) -> Any:
                if is_dataclass(obj) and not isinstance(obj, type):
                    return asdict(obj)
                if isinstance(obj, dict):
                    return {k: _serialize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_serialize(item) for item in obj]
                if isinstance(obj, (str, int, float, bool)) or obj is None:
                    return obj
                return str(obj)

            memory_refs = []
            if session_memory_index and hasattr(session_memory_index, "attachments"):
                for att in session_memory_index.attachments:
                    memory_refs.append(_serialize(att))

            chk_data = {
                "format_version": 1,
                "task_id": state.task_id,
                "user_query": state.user_query,
                "intent_version": state.intent_version,
                "global_constraints": state.global_constraints,
                "worker_history": _serialize(state.worker_history),
                "worker_results": state.worker_results,
                "tool_records": _serialize(state.tool_records),
                "step_count": state.step_count,
                "global_budget_consumed": state.global_budget_consumed,
                "global_budget_max": state.global_budget_max,
                "child_budget_accounted": state.child_budget_accounted,
                "max_supervisor_steps": state.max_supervisor_steps,
                "deadline_ts": state.deadline_ts,
                "status": state.status,
                "active_worker": state.active_worker,
                "active_child_run_id": active_child_run_id if active_child_run_id is not None else state.active_child_run_id,
                "contract_fingerprint": state.contract_fingerprint,
                "memory_index": memory_refs,
                "object_coverage": _serialize(session_memory_index.object_coverage) if session_memory_index else {},
                "final_output": state.final_output,
                "error_message": state.error_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            content = json.dumps(chk_data, ensure_ascii=False, indent=2)
            tmp_file = chk_file.parent / f"{chk_file.name}.tmp.{uuid.uuid4().hex[:8]}"
            tmp_file.write_text(content, encoding="utf-8")
            os.replace(tmp_file, chk_file)
        except Exception as exc:
            logger.error("Failed to save supervisor checkpoint: %s", exc)
            raise RuntimeError(f"Failed to persist supervisor checkpoint: {exc}") from exc

    def _account_child_budget(self, state: SupervisorState, child: Any) -> None:
        if child is None:
            return
        previous = state.child_budget_accounted.get(child.run_id, 0)
        state.global_budget_consumed += max(0, child.budget_consumed - previous)
        state.child_budget_accounted[child.run_id] = max(previous, child.budget_consumed)

    async def _execute_pi_worker(
        self,
        worker_spec: WorkerAgentSpec,
        decision: SupervisorDecision,
        state: SupervisorState,
        scoped_broker: ToolBroker,
        scoped_allowlist: list[str],
        worker_protected: ProtectedState,
        session_memory_index: Any,
        worker_step_budget: int,
        worker_workspace: Path,
        effective_hooks: Optional[HarnessHooks],
        verification_fn: Optional[Callable[[Any], Optional[dict[str, Any]]]],
        decision_entry: dict[str, Any],
    ) -> None:
        """Execute a specialized domain worker using the native Pi Agent SDK execution backend."""
        if not worker_spec.pi_model_config or not isinstance(worker_spec.pi_model_config, dict):
            raise ValueError(
                f"Worker '{worker_spec.name}' has execution_backend='pi' but missing or invalid pi_model_config"
            )

        child_runtime = AgentRuntime(
            db_path=self.runtime.db_path,
            tool_broker=scoped_broker,
            registry=self.runtime.registry,
            deployment_manager=self.runtime.deployment_manager,
            collector=self.runtime.collector,
            episode_store=self.runtime.episode_store,
            candidate_store=self.runtime.candidate_store,
            memory_manager=self.runtime.memory_manager,
        )
        child_runtime._pause_requested_runs = self.runtime._pause_requested_runs

        child_run_id = f"run_{state.task_id}_{worker_spec.name}_s{state.step_count}_{uuid.uuid4().hex[:8]}"
        child_task_id = f"{state.task_id}_{worker_spec.name}_{state.step_count}"

        state.active_worker = worker_spec.name
        state.active_child_run_id = child_run_id
        decision_entry["worker_state"] = {
            "worker": worker_spec.name,
            "run_id": child_run_id,
            "task_id": child_task_id,
            "status": "PENDING",
        }

        # Pre-dispatch checkpoint: if writing fails, child run is never started
        self._save_checkpoint(state, active_child_run_id=child_run_id, session_memory_index=session_memory_index)

        accounted = False
        try:
            child_runtime.start_run(
                run_id=child_run_id,
                task_id=child_task_id,
                purpose="evaluation",
                budget_max=worker_step_budget,
                deadline_ts=state.deadline_ts,
                task_description=decision.instruction,
            )

            initial_scratchpad = {
                "protected_state": worker_protected,
                "memory_index": session_memory_index,
            }

            pi_output = await child_runtime.arun_agent_with_pi(
                run_id=child_run_id,
                input_text=decision.instruction,
                tools=list(scoped_allowlist),
                system_prompt_header=worker_spec.system_prompt,
                model_config=worker_spec.pi_model_config,
                test_mode=worker_spec.pi_test_mode,
                timeout=worker_spec.pi_timeout,
                context_compressor=worker_spec.context_compressor,
                hooks=effective_hooks,
                initial_scratchpad=initial_scratchpad,
                goal=decision.instruction,
                intent_version=state.intent_version,
                active_constraints=list(state.global_constraints),
                workspace_dir=worker_workspace,
            )

            child_harness = child_runtime.get_harness_state(child_run_id)
            child_tool_calls = child_runtime.list_tool_calls(child_run_id)
            child_rec = child_runtime.get_run(child_run_id)
            if child_harness:
                child_harness.tool_records = list(child_tool_calls)

            if child_rec and child_rec.status == "PAUSED":
                child_consumed = child_rec.budget_consumed
                self._account_child_budget(state, child_rec)
                accounted = True
                state.tool_records.extend(child_tool_calls)
                state.status = "PAUSED"
                state.error_message = "Supervisor worker execution paused"
                decision_entry["worker_state"] = {
                    "worker": worker_spec.name,
                    "run_id": child_run_id,
                    "task_id": child_task_id,
                    "status": "PAUSED",
                    "output": "Execution paused cooperatively",
                    "tool_records": child_tool_calls,
                    "hook_audits": list(child_harness.hook_audits) if child_harness else [],
                }
                self._save_checkpoint(state, active_child_run_id=child_run_id, session_memory_index=session_memory_index)
                return

            evidence = None
            is_verif_fail = False
            if verification_fn:
                try:
                    evidence = verification_fn(child_harness)
                    if evidence and isinstance(evidence, dict):
                        if evidence.get("independent_pass") is False:
                            is_verif_fail = True
                except Exception as verif_err:
                    is_verif_fail = True
                    evidence = {
                        "independent_pass": False,
                        "failure_reason": f"Verification error: {verif_err}",
                    }
            else:
                evidence = {
                    "independent_pass": None,
                    "verdict": "unknown",
                    "reason": "No verification_fn provided; model self-assertion cannot declare success",
                }

            for entry in state.worker_history:
                ws = entry.get("worker_state", {})
                if ws.get("run_id") == child_run_id:
                    ws["output"] = pi_output
            self._save_checkpoint(state, session_memory_index=session_memory_index)
            fin_rec, fin_ep = child_runtime.finalize_run(
                run_id=child_run_id,
                model_output=pi_output,
                verification_evidence=evidence,
            )

            child_consumed = (
                fin_rec.budget_consumed
                if fin_rec
                else (child_rec.budget_consumed if child_rec else 0)
            )
            self._account_child_budget(state, fin_rec or child_rec)
            accounted = True
            state.tool_records.extend(child_tool_calls)

            if child_harness and "memory_index" in child_harness.working_scratchpad:
                w_mi = child_harness.working_scratchpad["memory_index"]
                if hasattr(w_mi, "attachments") and isinstance(w_mi.attachments, list):
                    for att in w_mi.attachments:
                        if att not in session_memory_index.attachments:
                            session_memory_index.attachments.append(att)

            output_text = pi_output or "Worker executed."
            state.worker_results[worker_spec.name] = output_text

            decision_entry["worker_state"] = {
                "worker": worker_spec.name,
                "run_id": child_run_id,
                "task_id": child_task_id,
                "status": "FAILED" if is_verif_fail else (fin_rec.status if fin_rec else "COMPLETED"),
                "output": output_text,
                "tool_records": child_tool_calls,
                "hook_audits": list(child_harness.hook_audits) if child_harness else [],
                "episode_id": fin_ep.episode_id if fin_ep else None,
                "episode_outcome": fin_ep.outcome if fin_ep else "unknown",
            }

            state.active_child_run_id = None
            if is_verif_fail:
                fail_reason = (evidence.get("failure_reason") if evidence else None) or "Business verification failed"
                state.status = "FAILED"
                state.error_message = fail_reason
            elif state.global_budget_consumed >= state.global_budget_max:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = f"Global budget exhausted ({state.global_budget_consumed}/{state.global_budget_max})"

        except (Exception, asyncio.CancelledError) as exc:
            child_harness = child_runtime.get_harness_state(child_run_id)
            child_tool_calls = child_runtime.list_tool_calls(child_run_id)
            child_rec = child_runtime.get_run(child_run_id)
            if child_harness:
                child_harness.tool_records = list(child_tool_calls)

            child_consumed = child_rec.budget_consumed if child_rec else 0
            if not accounted:
                self._account_child_budget(state, child_rec)
                state.tool_records.extend(child_tool_calls)

            child_status = child_rec.status if child_rec else "FAILED"
            err_msg = str(exc) or (child_rec.error_message if child_rec else "Worker failed")

            if isinstance(exc, asyncio.CancelledError) or child_status == "CANCELLED":
                state.status = "CANCELLED"
                state.error_message = err_msg or "Supervisor worker cancelled"
            elif child_status in ("INTERRUPTED", "PAUSED_FOR_REVIEW") or "review" in err_msg.lower():
                state.status = "PAUSED_FOR_REVIEW"
                state.error_message = err_msg or "Supervisor worker paused for review"
            elif child_status == "BUDGET_EXHAUSTED" or "budget" in err_msg.lower() or state.global_budget_consumed >= state.global_budget_max:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = err_msg or "Budget exhausted during worker execution"
            elif child_status == "TIMED_OUT" or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                state.status = "TIMED_OUT"
                state.error_message = err_msg or "Supervisor worker timed out"
            else:
                state.status = "FAILED"
                state.error_message = err_msg or "Supervisor worker failed"

            decision_entry["worker_state"] = {
                "worker": worker_spec.name,
                "run_id": child_run_id,
                "task_id": child_task_id,
                "status": child_status,
                "output": err_msg,
                "tool_records": child_tool_calls,
                "hook_audits": list(child_harness.hook_audits) if child_harness else [],
            }
            state.active_child_run_id = None
            self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            child_runtime.close()

    async def _resume_pi_worker(
        self,
        worker_spec: WorkerAgentSpec,
        child_run_id: str,
        state: SupervisorState,
        scoped_broker: ToolBroker,
        scoped_allowlist: list[str],
        worker_workspace: Path,
        effective_hooks: Optional[HarnessHooks],
        verification_fn: Optional[Callable[[Any], Optional[dict[str, Any]]]],
        session_memory_index: Any,
    ) -> bool:
        """Resume an existing paused child worker run using native Pi checkpoint."""
        child_runtime = AgentRuntime(
            db_path=self.runtime.db_path,
            tool_broker=scoped_broker,
            registry=self.runtime.registry,
            deployment_manager=self.runtime.deployment_manager,
            collector=self.runtime.collector,
            episode_store=self.runtime.episode_store,
            candidate_store=self.runtime.candidate_store,
            memory_manager=self.runtime.memory_manager,
        )
        child_runtime._pause_requested_runs = self.runtime._pause_requested_runs

        prev_rec = self.runtime.get_run(child_run_id)
        initial_consumed = prev_rec.budget_consumed if prev_rec else 0

        accounted = False
        try:
            pi_output = await child_runtime.aresume_agent_with_pi(
                run_id=child_run_id,
                model_config=worker_spec.pi_model_config,
                test_mode=worker_spec.pi_test_mode,
                timeout=worker_spec.pi_timeout,
                context_compressor=worker_spec.context_compressor,
                hooks=effective_hooks,
                workspace_dir=worker_workspace,
            )

            child_harness = child_runtime.get_harness_state(child_run_id)
            child_tool_calls = child_runtime.list_tool_calls(child_run_id)
            child_rec = child_runtime.get_run(child_run_id)
            if child_harness:
                child_harness.tool_records = list(child_tool_calls)

            delta = (child_rec.budget_consumed if child_rec else initial_consumed) - initial_consumed
            self._account_child_budget(state, child_rec)
            accounted = True

            for tc in child_tool_calls:
                if not any(r.call_id == tc.call_id for r in state.tool_records):
                    state.tool_records.append(tc)

            if child_rec and child_rec.status == "PAUSED":
                state.status = "PAUSED"
                state.error_message = "Supervisor worker execution paused"
                self._save_checkpoint(state, active_child_run_id=child_run_id, session_memory_index=session_memory_index)
                return False

            evidence = None
            is_verif_fail = False
            if verification_fn:
                try:
                    evidence = verification_fn(child_harness)
                    if evidence and isinstance(evidence, dict):
                        if evidence.get("independent_pass") is False:
                            is_verif_fail = True
                except Exception as verif_err:
                    is_verif_fail = True
                    evidence = {
                        "independent_pass": False,
                        "failure_reason": f"Verification error: {verif_err}",
                    }
            else:
                evidence = {
                    "independent_pass": None,
                    "verdict": "unknown",
                    "reason": "No verification_fn provided; model self-assertion cannot declare success",
                }

            for entry in state.worker_history:
                ws = entry.get("worker_state", {})
                if ws.get("run_id") == child_run_id:
                    ws["output"] = pi_output
            self._save_checkpoint(state, session_memory_index=session_memory_index)
            fin_rec, fin_ep = child_runtime.finalize_run(
                run_id=child_run_id,
                model_output=pi_output,
                verification_evidence=evidence,
            )

            if child_harness and "memory_index" in child_harness.working_scratchpad:
                w_mi = child_harness.working_scratchpad["memory_index"]
                if hasattr(w_mi, "attachments") and isinstance(w_mi.attachments, list):
                    for att in w_mi.attachments:
                        if att not in session_memory_index.attachments:
                            session_memory_index.attachments.append(att)

            output_text = pi_output or "Worker resumed."
            state.worker_results[worker_spec.name] = output_text

            for h in state.worker_history:
                ws = h.get("worker_state")
                if ws and ws.get("run_id") == child_run_id:
                    ws["status"] = "FAILED" if is_verif_fail else (fin_rec.status if fin_rec else "COMPLETED")
                    ws["output"] = output_text
                    ws["tool_records"] = child_tool_calls
                    old_audits = list(ws.get("hook_audits", []))
                    for audit in (child_harness.hook_audits if child_harness else []):
                        if audit not in old_audits:
                            old_audits.append(audit)
                    ws["hook_audits"] = old_audits
                    ws["episode_id"] = fin_ep.episode_id if fin_ep else None
                    ws["episode_outcome"] = fin_ep.outcome if fin_ep else "unknown"

            state.active_child_run_id = None
            state.step_count += 1

            if is_verif_fail:
                fail_reason = (evidence.get("failure_reason") if evidence else None) or "Business verification failed"
                state.status = "FAILED"
                state.error_message = fail_reason
                self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
                return False
            elif state.global_budget_consumed >= state.global_budget_max:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = f"Global budget exhausted ({state.global_budget_consumed}/{state.global_budget_max})"
                self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
                return False
            else:
                self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
                return True

        except (Exception, asyncio.CancelledError) as exc:
            child_harness = child_runtime.get_harness_state(child_run_id)
            child_tool_calls = child_runtime.list_tool_calls(child_run_id)
            child_rec = child_runtime.get_run(child_run_id)
            if child_harness:
                child_harness.tool_records = list(child_tool_calls)

            if not accounted and child_rec:
                delta = child_rec.budget_consumed - initial_consumed
                self._account_child_budget(state, child_rec)
                for tc in child_tool_calls:
                    if not any(r.call_id == tc.call_id for r in state.tool_records):
                        state.tool_records.append(tc)

            child_status = child_rec.status if child_rec else "FAILED"
            err_msg = str(exc) or (child_rec.error_message if child_rec else "Resumed worker failed")

            if isinstance(exc, asyncio.CancelledError) or child_status == "CANCELLED":
                state.status = "CANCELLED"
                state.error_message = err_msg or "Supervisor worker cancelled during resume"
            elif child_status in ("INTERRUPTED", "PAUSED_FOR_REVIEW") or "review" in err_msg.lower():
                state.status = "PAUSED_FOR_REVIEW"
                state.error_message = err_msg or "Supervisor worker paused for review during resume"
            elif child_status == "BUDGET_EXHAUSTED" or "budget" in err_msg.lower() or state.global_budget_consumed >= state.global_budget_max:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = err_msg or "Budget exhausted during resume"
            elif child_status == "TIMED_OUT" or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                state.status = "TIMED_OUT"
                state.error_message = err_msg or "Supervisor worker timed out during resume"
            else:
                state.status = "FAILED"
                state.error_message = err_msg or "Supervisor worker failed during resume"

            state.active_child_run_id = None
            self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False
        finally:
            child_runtime.close()

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

    async def run(self, spec: SupervisorTaskSpec, resume: bool = False) -> SupervisorState:
        """Run or resume the supervisor multi-agent orchestration session to completion."""
        from .context_engine.schemas import AttachmentRef, MemoryIndex
        session_workspace = self.workspace_dir / "supervisor_runs" / spec.task_id
        session_workspace.mkdir(parents=True, exist_ok=True)
        chk_file = session_workspace / "supervisor_checkpoint.json"

        contract_fp = self._compute_contract_fingerprint(spec)

        if not resume:
            if chk_file.exists():
                try:
                    raw_chk = json.loads(chk_file.read_text(encoding="utf-8"))
                    chk_status = raw_chk.get("status")
                    if chk_status not in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXHAUSTED"):
                        raise ValueError(
                            f"Unfinished supervisor checkpoint exists for task_id '{spec.task_id}' (status='{chk_status}'). "
                            "Explicit resume=True or a new task_id is required."
                        )
                except json.JSONDecodeError as exc:
                    raise ValueError("Cannot overwrite corrupt supervisor checkpoint; use a new task_id") from exc

            deadline_ts = (
                time.time() + spec.deadline_seconds
                if spec.deadline_seconds is not None
                else None
            )

            session_memory_index = MemoryIndex()
            state = SupervisorState(
                task_id=spec.task_id,
                user_query=spec.user_query,
                intent_version=spec.intent_version,
                global_constraints=list(spec.global_constraints),
                max_supervisor_steps=spec.max_supervisor_steps,
                global_budget_max=spec.global_budget_max,
                workspace_dir=session_workspace,
                deadline_ts=deadline_ts,
                contract_fingerprint=contract_fp,
            )
        else:
            if not chk_file.exists():
                raise FileNotFoundError(f"Cannot resume: checkpoint '{chk_file}' does not exist")
            try:
                chk_data = json.loads(chk_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"Cannot resume: corrupt checkpoint JSON: {exc}") from exc

            if chk_data.get("format_version") != 1:
                raise ValueError(f"Cannot resume: unsupported checkpoint format_version: {chk_data.get('format_version')}")

            chk_status = chk_data.get("status")
            if chk_status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXHAUSTED", "PAUSED_FOR_REVIEW"):
                raise ValueError(f"Cannot resume supervisor run in terminal/reviewed status '{chk_status}'")

            stored_fp = chk_data.get("contract_fingerprint")
            if not stored_fp or stored_fp != contract_fp:
                raise ValueError(
                    f"STALE_SUPERVISOR_CHECKPOINT: Contract fingerprint drift detected: stored={stored_fp}, current={contract_fp}"
                )

            deadline_ts = chk_data.get("deadline_ts")
            if deadline_ts is not None and time.time() >= deadline_ts:
                raise TimeoutError(f"Cannot resume: supervisor deadline expired ({deadline_ts})")

            raw_tool_recs = chk_data.get("tool_records", [])
            rebuilt_tool_records = []
            for r in raw_tool_recs:
                if isinstance(r, dict):
                    rebuilt_tool_records.append(ToolCallRecord(
                        call_id=r.get("call_id", ""),
                        run_id=r.get("run_id", ""),
                        tool_name=r.get("tool_name", ""),
                        status=r.get("status", "SUCCESS"),
                        input_params=r.get("input_params", {}),
                        output_text=r.get("output_text", ""),
                        output_data=r.get("output_data", {}),
                        error_type=r.get("error_type"),
                        error_message=r.get("error_message"),
                        latency_ms=r.get("latency_ms", 0.0),
                        created_at=r.get("created_at", ""),
                    ))

            session_memory_index = MemoryIndex()
            for att_data in chk_data.get("memory_index", []):
                if isinstance(att_data, dict):
                    session_memory_index.attachments.append(AttachmentRef(
                        ref_id=att_data.get("ref_id", ""),
                        description=att_data.get("description", ""),
                        total_items=att_data.get("total_items", 0),
                        expanded_items=att_data.get("expanded_items", 0),
                        item_keys=att_data.get("item_keys", []),
                        access_method=att_data.get("access_method", ""),
                    ))

            session_memory_index.object_coverage = chk_data.get("object_coverage", {})
            state = SupervisorState(
                task_id=chk_data["task_id"],
                user_query=chk_data["user_query"],
                intent_version=chk_data["intent_version"],
                global_constraints=chk_data.get("global_constraints", []),
                worker_history=chk_data.get("worker_history", []),
                worker_results=chk_data.get("worker_results", {}),
                active_worker=chk_data.get("active_worker"),
                step_count=chk_data.get("step_count", 0),
                max_supervisor_steps=chk_data.get("max_supervisor_steps", spec.max_supervisor_steps),
                global_budget_consumed=chk_data.get("global_budget_consumed", 0),
                global_budget_max=chk_data.get("global_budget_max", spec.global_budget_max),
                child_budget_accounted=chk_data.get("child_budget_accounted", {}),
                status="RUNNING",
                workspace_dir=session_workspace,
                tool_records=rebuilt_tool_records,
                deadline_ts=deadline_ts,
                active_child_run_id=chk_data.get("active_child_run_id"),
                contract_fingerprint=contract_fp,
            )

        self.last_state = state

        try:
            if resume:
                # Canonical usage repairs a stale parent checkpoint before any new dispatch.
                known_ids = {h.get("worker_state", {}).get("run_id") for h in state.worker_history}
                if state.active_child_run_id:
                    known_ids.add(state.active_child_run_id)
                for run_id in known_ids - {None}:
                    self._account_child_budget(state, self.runtime.get_run(run_id))
                state.global_budget_consumed = max(
                    state.global_budget_consumed, sum(state.child_budget_accounted.values())
                )
            # Handle in-flight child run on resume
            if resume and state.active_child_run_id:
                in_flight_id = state.active_child_run_id
                child_rec = self.runtime.get_run(in_flight_id)
                active_worker_name = state.active_worker
                worker_spec = self.workers.get(active_worker_name) if active_worker_name else None

                if not child_rec:
                    state.status = "PAUSED_FOR_REVIEW"
                    state.error_message = f"REVIEW_REQUIRED: In-flight child run '{in_flight_id}' not found in runtime database"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    return state

                if child_rec.status == "COMPLETED":
                    ep = self.runtime.episode_store.get_episode(f"ep_{in_flight_id}") if self.runtime.episode_store else None
                    recovered_output = next((h.get("worker_state", {}).get("output")
                        for h in state.worker_history
                        if h.get("worker_state", {}).get("run_id") == in_flight_id), None)
                    if ep and isinstance(recovered_output, str) and recovered_output:
                        if ep.outcome == "failure":
                            state.status = "FAILED"
                            state.error_message = "Recovered worker failed independent verification"
                            return state
                        if active_worker_name and active_worker_name not in state.worker_results:
                            state.worker_results[active_worker_name] = recovered_output
                            state.step_count += 1
                        for entry in state.worker_history:
                            ws = entry.get("worker_state", {})
                            if ws.get("run_id") == in_flight_id:
                                ws.update(status="COMPLETED", episode_id=ep.episode_id, episode_outcome=ep.outcome)
                        child_tools = self.runtime.list_tool_calls(in_flight_id)
                        for tc in child_tools:
                            if not any(r.call_id == tc.call_id for r in state.tool_records):
                                state.tool_records.append(tc)
                        state.active_child_run_id = None
                        self._save_checkpoint(state, active_child_run_id=None, session_memory_index=session_memory_index)
                    else:
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"REVIEW_REQUIRED: Child run '{in_flight_id}' is COMPLETED but output/Episode could not be confirmed"
                        self._save_checkpoint(state, session_memory_index=session_memory_index)
                        return state

                elif child_rec.status == "PAUSED":
                    conn = self.runtime._get_conn()
                    admitted = conn.execute(
                        "SELECT count(*) FROM runtime_tool_calls WHERE run_id = ? AND status = 'ADMITTED'",
                        (in_flight_id,),
                    ).fetchone()[0]
                    if admitted > 0:
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"REVIEW_REQUIRED: Child run '{in_flight_id}' has unsettled in-flight ADMITTED tool calls"
                        self._save_checkpoint(state, session_memory_index=session_memory_index)
                        return state

                    pi_chk = self.runtime.get_pi_checkpoint(in_flight_id)
                    if not pi_chk:
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"REVIEW_REQUIRED: Paused child run '{in_flight_id}' has no Pi checkpoint"
                        self._save_checkpoint(state, session_memory_index=session_memory_index)
                        return state

                    if worker_spec and worker_spec.execution_backend == "pi":
                        parent_allowlist = set(self.tool_broker.application_allowlist or set())
                        scoped_allowlist = sorted(list(set(worker_spec.allowed_tools) & parent_allowlist))
                        scoped_broker = ToolBroker(
                            application_allowlist=set(scoped_allowlist),
                            sandbox_backend=getattr(self.tool_broker, "sandbox_backend", None),
                            dependency_prober=getattr(self.tool_broker, "dependency_prober", None),
                        )
                        for t_name in scoped_allowlist:
                            tool_impl = self.tool_broker.get_tool(t_name)
                            if tool_impl is not None:
                                scoped_broker._tools[t_name] = tool_impl
                        effective_hooks = compose_hooks(self.hooks, worker_spec.hooks)
                        worker_workspace = session_workspace / "workers" / f"{worker_spec.name}_step_{state.step_count}"
                        worker_workspace.mkdir(parents=True, exist_ok=True)

                        resumed_ok = await self._resume_pi_worker(
                            worker_spec=worker_spec,
                            child_run_id=in_flight_id,
                            state=state,
                            scoped_broker=scoped_broker,
                            scoped_allowlist=scoped_allowlist,
                            worker_workspace=worker_workspace,
                            effective_hooks=effective_hooks,
                            verification_fn=spec.verification_fn,
                            session_memory_index=session_memory_index,
                        )
                        if state.status != "RUNNING":
                            self._save_checkpoint(state, session_memory_index=session_memory_index)
                            return state
                    else:
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"REVIEW_REQUIRED: In-flight worker '{active_worker_name}' cannot be resumed"
                        self._save_checkpoint(state, session_memory_index=session_memory_index)
                        return state

                elif child_rec.status in ("FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXHAUSTED"):
                    state.status = child_rec.status
                    state.error_message = child_rec.error_message or f"Child run failed with status {child_rec.status}"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    return state

                else:
                    state.status = "PAUSED_FOR_REVIEW"
                    state.error_message = f"REVIEW_REQUIRED: In-flight child run '{in_flight_id}' in unsafe status '{child_rec.status}' without checkpoint"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    return state

            # Main orchestration loop
            while state.status == "RUNNING":
                # 1. Physical deadline check
                if state.deadline_ts is not None and time.time() >= state.deadline_ts:
                    state.status = "TIMED_OUT"
                    state.error_message = f"Supervisor deadline expired ({state.deadline_ts})"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    break

                # 2. Global budget checks
                if state.step_count >= state.max_supervisor_steps:
                    state.status = "BUDGET_EXHAUSTED"
                    state.error_message = f"Supervisor reached max steps limit ({state.max_supervisor_steps})"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    break

                if state.global_budget_consumed >= state.global_budget_max:
                    state.status = "BUDGET_EXHAUSTED"
                    state.error_message = f"Global budget exhausted ({state.global_budget_consumed}/{state.global_budget_max})"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    break

                # 3. Routing Decision
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

                # 4. Termination Check
                if decision.next_worker == "FINISH":
                    state.status = "COMPLETED"
                    state.final_output = decision.instruction or "\n\n".join(
                        [f"[{k} 报告]: {v}" for k, v in state.worker_results.items()]
                    )
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    break

                # 5. Resolve Domain Worker
                worker_spec = self.workers.get(decision.next_worker)
                if not worker_spec:
                    state.status = "FAILED"
                    state.error_message = f"Supervisor selected unknown worker: '{decision.next_worker}'"
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    break

                state.active_worker = worker_spec.name

                # 6. Create Scoped ToolBroker (Least Privilege)
                parent_allowlist = set(self.tool_broker.application_allowlist or set())
                scoped_allowlist = sorted(list(set(worker_spec.allowed_tools) & parent_allowlist))

                scoped_broker = ToolBroker(
                    application_allowlist=set(scoped_allowlist),
                    sandbox_backend=getattr(self.tool_broker, "sandbox_backend", None),
                    dependency_prober=getattr(self.tool_broker, "dependency_prober", None),
                )
                for t_name in scoped_allowlist:
                    tool_impl = self.tool_broker.get_tool(t_name)
                    if tool_impl is not None:
                        scoped_broker._tools[t_name] = tool_impl

                # 7. Build Scoped Task & Cascade Global Constraints
                worker_protected = ProtectedState(
                    intent_version=state.intent_version,
                    current_goal=decision.instruction,
                    active_constraints=list(state.global_constraints),
                    allowed_tools=list(scoped_allowlist),
                )

                remaining_budget = max(0, state.global_budget_max - state.global_budget_consumed)
                worker_step_budget = min(worker_spec.max_steps_per_call, remaining_budget)

                worker_workspace = session_workspace / "workers" / f"{worker_spec.name}_step_{state.step_count}"
                worker_workspace.mkdir(parents=True, exist_ok=True)
                effective_hooks = compose_hooks(self.hooks, worker_spec.hooks)

                # 8. Execute Worker: Pi or Legacy
                if worker_spec.execution_backend == "pi":
                    await self._execute_pi_worker(
                        worker_spec=worker_spec,
                        decision=decision,
                        state=state,
                        scoped_broker=scoped_broker,
                        scoped_allowlist=scoped_allowlist,
                        worker_protected=worker_protected,
                        session_memory_index=session_memory_index,
                        worker_step_budget=worker_step_budget,
                        worker_workspace=worker_workspace,
                        effective_hooks=effective_hooks,
                        verification_fn=spec.verification_fn,
                        decision_entry=decision_entry,
                    )
                    if state.status != "RUNNING":
                        self._save_checkpoint(state, session_memory_index=session_memory_index)
                        break
                else:
                    if worker_spec.execution_backend != "legacy":
                        raise ValueError(f"Unknown worker backend: {worker_spec.execution_backend}")
                    legacy_run_id = f"run_{state.task_id}_{worker_spec.name}_{uuid.uuid4().hex[:8]}"
                    state.active_child_run_id = legacy_run_id
                    decision_entry["worker_state"] = {"run_id": legacy_run_id, "status": "PENDING"}
                    self._save_checkpoint(state, session_memory_index=session_memory_index)
                    worker_task_spec = HarnessTaskSpec(
                        run_id=legacy_run_id,
                        task_id=f"{state.task_id}_{worker_spec.name}_{state.step_count}",
                        user_query=decision.instruction,
                        system_prompt=worker_spec.system_prompt,
                        intent_version=state.intent_version,
                        budget_max=worker_step_budget,
                        context_compressor=worker_spec.context_compressor,
                        initial_scratchpad={"protected_state": worker_protected, "memory_index": session_memory_index},
                    )

                    worker_loop = AsyncHarnessLoop(
                        runtime=self.runtime,
                        tool_broker=scoped_broker,
                        llm_client=worker_spec.llm_client or self.supervisor_llm,
                        hooks=effective_hooks,
                        workspace_dir=worker_workspace,
                    )

                    worker_state = await worker_loop.run(worker_task_spec)

                    # Sync newly offloaded artifacts across workers in this supervisor session
                    if "memory_index" in worker_state.working_scratchpad:
                        w_mi = worker_state.working_scratchpad["memory_index"]
                        if hasattr(w_mi, "attachments") and isinstance(w_mi.attachments, list):
                            for att in w_mi.attachments:
                                if att not in session_memory_index.attachments:
                                    session_memory_index.attachments.append(att)

                    # Record Aggregated Results & Consume Budget
                    # Legacy loop tracks its own steps; its Runtime counts only tools.
                    legacy_consumed = max(1, worker_state.step_count)
                    state.global_budget_consumed += legacy_consumed
                    state.child_budget_accounted[worker_state.run_id] = legacy_consumed
                    state.active_child_run_id = None
                    output_text = worker_state.final_output or worker_state.error_message or "Worker executed."
                    state.worker_results[worker_spec.name] = output_text
                    state.tool_records.extend(worker_state.tool_records)
                    decision_entry["worker_state"] = {
                        "worker": worker_spec.name,
                        "run_id": worker_state.run_id,
                        "task_id": worker_task_spec.task_id,
                        "status": worker_state.status,
                        "output": output_text,
                        "tool_records": worker_state.tool_records,
                        "hook_audits": list(worker_state.hook_audits),
                    }

                state.step_count += 1
                self._save_checkpoint(state, session_memory_index=session_memory_index)

        except asyncio.CancelledError:
            state.status = "CANCELLED"
            if not state.error_message:
                state.error_message = "Supervisor orchestration cancelled"
            self._save_checkpoint(state, session_memory_index=session_memory_index)
            raise
        finally:
            try:
                self._save_checkpoint(state, session_memory_index=session_memory_index)
            except Exception:
                pass

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
