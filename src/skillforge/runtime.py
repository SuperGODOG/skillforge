"""Milestone 5a: Agent Runtime Lifecycle + Tool Broker.

Provides:
- ToolBroker: Unified tool execution engine enforcing parameter schema validation,
  application allowlists, skill dependency intersection, sensitive data redaction,
  execution timeouts, and full provenance tracing.
- AgentRuntime: Manages run lifecycle (PENDING -> RUNNING -> TERMINAL), pre-dispatch
  budget reservation, concurrency controls, deadline enforcement, idempotent execution,
  canary version snapshot binding, and automatic terminal Episode generation via
  ExperienceCollector.
- BrokeredTool: hello_agents.tools.Tool adapter allowing seamless plug-in to
  SimpleAgent and SkillEvaluator without bypassing the Broker.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union

from hello_agents.tools import Tool, ToolParameter, ToolResponse
from hello_agents.tools.response import ToolStatus

from .models import (
    Episode,
    FutureRetrievalResult,
    RetrievalContext,
    RunRecord,
    RunVersionBinding,
    RuntimeStatus,
    SkillRecommendation,
    ToolCallProvenance,
    ToolCallRecord,
)
from .sandbox import (
    DependencyProbe,
    MacSeatbeltSandbox,
    SandboxBackend,
    SandboxConfig,
    SandboxedToolSpec,
)
from .storage.db import init_db

REDACT_KEYS_RE = re.compile(
    r"(?i)(password|secret|token|credential|api_key|private_key|auth|bearer)"
)


def sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask sensitive parameter keys (passwords, tokens, keys) in traces."""
    if not isinstance(params, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for k, v in params.items():
        if REDACT_KEYS_RE.search(k):
            sanitized[k] = "***REDACTED***"
        elif isinstance(v, dict):
            sanitized[k] = sanitize_params(v)
        elif isinstance(v, list):
            sanitized[k] = [
                sanitize_params(x) if isinstance(x, dict) else x for x in v
            ]
        else:
            sanitized[k] = v
    return sanitized


def validate_parameter_schema(
    tool_params: list[ToolParameter], input_params: dict[str, Any]
) -> tuple[bool, Optional[str]]:
    """Validate input parameters against a tool's defined schema."""
    if not isinstance(input_params, dict):
        return False, f"Expected parameter dict, got {type(input_params).__name__}"

    # 1. Required parameters check
    for p in tool_params:
        if p.required and p.name not in input_params:
            return False, f"Missing required parameter '{p.name}'"

    # 2. Type validation
    for k, v in input_params.items():
        param_def = next((p for p in tool_params if p.name == k), None)
        if param_def is None:
            continue
        expected = (param_def.type or "string").lower()
        if expected == "string" and not isinstance(v, str):
            return False, f"Parameter '{k}' expected string, got {type(v).__name__}"
        elif expected == "integer":
            if not isinstance(v, int) or isinstance(v, bool):
                return False, f"Parameter '{k}' expected integer, got {type(v).__name__}"
        elif expected == "number":
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return False, f"Parameter '{k}' expected number, got {type(v).__name__}"
        elif expected == "boolean":
            if not isinstance(v, bool):
                return False, f"Parameter '{k}' expected boolean, got {type(v).__name__}"
        elif expected in ("array", "list"):
            if not isinstance(v, list):
                return False, f"Parameter '{k}' expected list/array, got {type(v).__name__}"
        elif expected in ("object", "dict"):
            if not isinstance(v, dict):
                return False, f"Parameter '{k}' expected dict/object, got {type(v).__name__}"

    return True, None


class ToolBroker:
    """Unified application-layer tool broker and permission gateway."""

    def __init__(
        self,
        application_allowlist: Optional[set[str]] = None,
        sandbox_backend: Optional[SandboxBackend] = None,
        dependency_prober: Optional[DependencyProbe] = None,
    ):
        self._tools: dict[str, Any] = {}
        self.application_allowlist: set[str] = set(application_allowlist or [])
        self.sandbox_backend = sandbox_backend
        self.dependency_prober = dependency_prober or (
            DependencyProbe(backend=sandbox_backend) if sandbox_backend else None
        )
        self._lock = threading.Lock()

    def register_tool(
        self,
        tool: Union[Tool, Callable, str],
        name: Optional[Union[str, Tool, Callable]] = None,
        description: str = "",
        parameters: Optional[list[ToolParameter]] = None,
    ) -> None:
        """Register a Tool instance or callable function into the broker."""
        with self._lock:
            # Handle swapped args: register_tool("name", tool_obj)
            if isinstance(tool, str) and (isinstance(name, Tool) or callable(name)):
                actual_name = tool
                actual_tool = name
            else:
                actual_tool = tool
                actual_name = name if isinstance(name, str) else None

            if isinstance(actual_tool, Tool):
                tool_name = actual_name or actual_tool.name
                self._tools[tool_name] = actual_tool
            elif callable(actual_tool):
                tool_name = actual_name or getattr(actual_tool, "__name__", "unnamed_tool")
                params = parameters or []
                if not params and hasattr(inspect, "signature"):
                    try:
                        sig = inspect.signature(actual_tool)
                        for param_name, param in sig.parameters.items():
                            p_type = "string"
                            if param.annotation == int:
                                p_type = "integer"
                            elif param.annotation == float:
                                p_type = "number"
                            elif param.annotation == bool:
                                p_type = "boolean"
                            elif param.annotation in (list, list[str], list[int]):
                                p_type = "array"
                            elif param.annotation in (dict, dict[str, Any]):
                                p_type = "object"
                            required = param.default is inspect.Parameter.empty
                            default_val = None if required else param.default
                            params.append(
                                ToolParameter(
                                    name=param_name,
                                    type=p_type,
                                    required=required,
                                    default=default_val,
                                )
                            )
                    except Exception:
                        pass
                self._tools[tool_name] = {
                    "callable": actual_tool,
                    "description": description or (actual_tool.__doc__ or ""),
                    "parameters": params,
                }
            else:
                raise TypeError(f"Unsupported tool type: {type(actual_tool)}")

    def register_sandboxed_tool(
        self,
        spec_or_name: Union[SandboxedToolSpec, str],
        command_template: Optional[list[str]] = None,
        description: str = "",
        parameters: Optional[list[ToolParameter]] = None,
        required_dependencies: Optional[list[str]] = None,
        allowed_read_paths: Optional[list[Path]] = None,
        denied_read_paths: Optional[list[Path]] = None,
        max_output_bytes: int = 65536,
        timeout_seconds: float = 10.0,
        host_handler_counter: Optional[Callable[[], None]] = None,
    ) -> None:
        """Register a tool designed for isolated subprocess sandbox execution."""
        with self._lock:
            if isinstance(spec_or_name, SandboxedToolSpec):
                spec = spec_or_name
            else:
                spec = SandboxedToolSpec(
                    name=spec_or_name,
                    command_template=command_template or [],
                    description=description,
                    parameters=parameters or [],
                    required_dependencies=required_dependencies or [],
                    allowed_read_paths=allowed_read_paths or [],
                    denied_read_paths=denied_read_paths or [],
                    max_output_bytes=max_output_bytes,
                    timeout_seconds=timeout_seconds,
                    host_handler_counter=host_handler_counter,
                )
            self._tools[spec.name] = spec

    def get_tool(self, name: str) -> Optional[Any]:
        """Fetch registered tool object or descriptor."""
        with self._lock:
            return self._tools.get(name)

    def get_parameters(self, name: str) -> list[ToolParameter]:
        """Fetch schema parameters for a tool."""
        with self._lock:
            t = self._tools.get(name)
        if t is None:
            return []
        if isinstance(t, Tool):
            return t.get_parameters()
        if isinstance(t, SandboxedToolSpec):
            return t.parameters
        if isinstance(t, dict):
            return t.get("parameters", [])
        return []

    def dispatch(
        self,
        run_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        call_id: Optional[str] = None,
        skill_required_tools: Optional[set[str]] = None,
        timeout: Optional[float] = None,
    ) -> ToolCallRecord:
        """Validate permissions & schema, then safely dispatch tool execution."""
        eff_call_id = call_id or f"call_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        sanitized_input = sanitize_params(parameters)

        # 1. Existence check
        with self._lock:
            tool_obj = self._tools.get(tool_name)

        if tool_obj is None:
            return ToolCallRecord(
                call_id=eff_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status="REJECTED",
                input_params=sanitized_input,
                error_type="UNKNOWN_TOOL",
                error_message=f"Tool '{tool_name}' is not registered in broker",
                latency_ms=0.0,
                created_at=now_iso,
            )

        # 2. Permission check: intersection of application allowlist & skill requirements
        if skill_required_tools is not None:
            effective_allowlist = self.application_allowlist & skill_required_tools
        else:
            effective_allowlist = self.application_allowlist

        if tool_name not in effective_allowlist:
            if skill_required_tools is not None and tool_name in skill_required_tools:
                err_msg = (
                    f"Tool '{tool_name}' requested by skill dependencies is not in "
                    f"application allowlist"
                )
            else:
                err_msg = f"Tool '{tool_name}' is not authorized by application policy"
            return ToolCallRecord(
                call_id=eff_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status="REJECTED",
                input_params=sanitized_input,
                error_type="PERMISSION_DENIED",
                error_message=err_msg,
                latency_ms=0.0,
                created_at=now_iso,
            )

        # 3. Schema validation
        tool_params = self.get_parameters(tool_name)
        valid, schema_err = validate_parameter_schema(tool_params, parameters)
        if not valid:
            return ToolCallRecord(
                call_id=eff_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status="REJECTED",
                input_params=sanitized_input,
                error_type="SCHEMA_VALIDATION_ERROR",
                error_message=schema_err or "Invalid parameters",
                latency_ms=0.0,
                created_at=now_iso,
            )

        # 3b. Sandboxed tool isolation path
        if isinstance(tool_obj, SandboxedToolSpec):
            for k in parameters:
                if k.startswith("_"):
                    return ToolCallRecord(
                        call_id=eff_call_id,
                        run_id=run_id,
                        tool_name=tool_name,
                        status="REJECTED",
                        input_params=sanitized_input,
                        error_type="PERMISSION_DENIED",
                        error_message=f"Unauthorized privilege parameter '{k}' in tool inputs",
                        latency_ms=0.0,
                        created_at=now_iso,
                    )

            if self.sandbox_backend is None or not self.sandbox_backend.is_available():
                return ToolCallRecord(
                    call_id=eff_call_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="REJECTED",
                    input_params=sanitized_input,
                    error_type="SANDBOX_UNAVAILABLE",
                    error_message="Sandbox backend is unavailable or not configured",
                    latency_ms=0.0,
                    created_at=now_iso,
                )

            if tool_obj.required_dependencies:
                prober = self.dependency_prober or DependencyProbe(backend=self.sandbox_backend)
                for dep in tool_obj.required_dependencies:
                    probe_res = prober.probe_dependency(dep)
                    if not probe_res.satisfied:
                        return ToolCallRecord(
                            call_id=eff_call_id,
                            run_id=run_id,
                            tool_name=tool_name,
                            status="REJECTED",
                            input_params=sanitized_input,
                            error_type="DEPENDENCY_MISSING",
                            error_message=f"Required dependency '{dep}' unsatisfied in sandbox: {probe_res.error_reason}",
                            latency_ms=0.0,
                            created_at=now_iso,
                        )

            ws_dir = Path(tempfile.mkdtemp(prefix=f"sf_sb_{eff_call_id}_"))
            sb_cfg = SandboxConfig(
                workspace_dir=ws_dir,
                allowed_read_paths=tool_obj.allowed_read_paths,
                denied_read_paths=tool_obj.denied_read_paths,
                allow_network=False,
                timeout_seconds=timeout or tool_obj.timeout_seconds,
                max_output_bytes=tool_obj.max_output_bytes,
            )

            sb_res = self.sandbox_backend.execute(
                tool_obj.command_template,
                sb_cfg,
                input_str=json.dumps(parameters),
                run_id=run_id,
            )

            if sb_res.is_timeout:
                sb_handler_status = "TIMED_OUT"
                sb_error_type = "TIMEOUT"
                sb_error_msg = sb_res.error_message or "Tool execution timed out"
                sb_out_text = ""
            elif sb_res.exit_code != 0:
                sb_handler_status = "ERROR"
                sb_error_type = sb_res.error_type or "SANDBOX_EXEC_ERROR"
                sb_error_msg = sb_res.error_message or f"Process exited with code {sb_res.exit_code}"
                sb_out_text = sb_res.stdout
            else:
                sb_handler_status = "EXECUTED"
                sb_error_type = None
                sb_error_msg = None
                sb_out_text = sb_res.stdout

            fp = (
                self.dependency_prober.compute_fingerprint(sb_cfg)
                if self.dependency_prober
                else "unknown"
            )
            sb_out_data: dict[str, Any] = {
                "backend": self.sandbox_backend.name,
                "environment_fingerprint": fp,
                "exit_code": sb_res.exit_code,
                "is_truncated": sb_res.is_truncated,
                "workspace": str(ws_dir),
            }
            if sb_out_text:
                try:
                    parsed = json.loads(sb_out_text)
                    if isinstance(parsed, dict):
                        sb_out_data.update(parsed)
                except Exception:
                    pass

            prov_sig = hashlib.sha256(
                f"{tool_name}:{eff_call_id}:{now_iso}:{sb_handler_status}".encode("utf-8")
            ).hexdigest()
            prov = ToolCallProvenance(
                tool_name=tool_name,
                fixture_case_id=eff_call_id,
                call_index=0,
                call_count=1,
                is_fixture=True,
                tool_required=True,
                tool_called=True,
                tool_success=(sb_handler_status == "EXECUTED"),
                authenticity_pass=True,
                input_params=sanitized_input,
                output_status="SUCCESS" if sb_handler_status == "EXECUTED" else "ERROR",
                output_summary=(
                    sb_out_text[:200]
                    if sb_out_text
                    else (sb_error_msg or sb_handler_status)
                ),
                latency_ms=sb_res.latency_ms,
                timestamp=now_iso,
                signature=f"sha256:{prov_sig}",
                snapshot_id=f"snap_{eff_call_id}",
                snapshot_content=sb_out_text or (sb_error_msg or ""),
            )

            return ToolCallRecord(
                call_id=eff_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status=sb_handler_status,
                input_params=sanitized_input,
                output_text=sb_out_text,
                output_data=sb_out_data,
                error_type=sb_error_type,
                error_message=sb_error_msg,
                latency_ms=sb_res.latency_ms,
                created_at=now_iso,
                provenance=prov,
            )

        # 4. Handler execution
        start_time = time.perf_counter()
        handler_status: Literal["EXECUTED", "ERROR", "TIMED_OUT"] = "EXECUTED"
        output_text = ""
        output_data: dict[str, Any] = {}
        error_type: Optional[str] = None
        error_message: Optional[str] = None

        try:
            target_fn: Callable
            if isinstance(tool_obj, Tool):
                target_fn = tool_obj.run
            elif isinstance(tool_obj, dict):
                target_fn = tool_obj["callable"]
            else:
                target_fn = tool_obj

            # Check if coroutine function (cooperative async)
            if inspect.iscoroutinefunction(target_fn):
                coro = target_fn(parameters)
                if timeout is not None and timeout > 0:
                    try:
                        # Use running event loop or new one
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = None
                        if loop and loop.is_running():
                            fut = asyncio.wait_for(coro, timeout=timeout)
                            # Synchronously wait for fut in thread if nested
                            res = loop.run_until_complete(fut)
                        else:
                            res = asyncio.run(asyncio.wait_for(coro, timeout=timeout))
                    except (asyncio.TimeoutError, TimeoutError):
                        handler_status = "TIMED_OUT"
                        error_type = "INFRASTRUCTURE_ERROR"
                        error_message = f"Tool execution timed out after {timeout}s"
                        res = None
                else:
                    res = asyncio.run(coro)
            else:
                res = target_fn(parameters)

            if handler_status != "TIMED_OUT":
                if isinstance(res, ToolResponse):
                    if res.status == ToolStatus.ERROR:
                        handler_status = "ERROR"
                        error_type = (
                            res.error_info.get("code", "HANDLER_ERROR")
                            if res.error_info
                            else "HANDLER_ERROR"
                        )
                        error_message = res.text
                    else:
                        handler_status = "EXECUTED"
                        output_text = res.text
                        output_data = res.data or {}
                elif isinstance(res, dict):
                    output_text = json.dumps(res, ensure_ascii=False)
                    output_data = res
                else:
                    output_text = str(res if res is not None else "")
        except (TimeoutError, asyncio.TimeoutError) as exc:
            handler_status = "TIMED_OUT"
            error_type = "INFRASTRUCTURE_ERROR"
            error_message = f"TimeoutError: {exc}"
        except Exception as exc:
            handler_status = "ERROR"
            error_type = "HANDLER_ERROR"
            error_message = f"{type(exc).__name__}: {exc}"

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Construct signed provenance
        prov_sig = hashlib.sha256(
            f"{tool_name}:{eff_call_id}:{now_iso}:{handler_status}".encode("utf-8")
        ).hexdigest()
        prov = ToolCallProvenance(
            tool_name=tool_name,
            fixture_case_id=eff_call_id,
            call_index=0,
            call_count=1,
            is_fixture=True,
            tool_required=True,
            tool_called=True,
            tool_success=(handler_status == "EXECUTED"),
            authenticity_pass=True,
            input_params=sanitized_input,
            output_status="SUCCESS" if handler_status == "EXECUTED" else "ERROR",
            output_summary=(
                output_text[:200]
                if output_text
                else (error_message or handler_status)
            ),
            latency_ms=latency_ms,
            timestamp=now_iso,
            signature=f"sha256:{prov_sig}",
            snapshot_id=f"snap_{eff_call_id}",
            snapshot_content=output_text or (error_message or ""),
        )

        return ToolCallRecord(
            call_id=eff_call_id,
            run_id=run_id,
            tool_name=tool_name,
            status=handler_status,
            input_params=sanitized_input,
            output_text=output_text,
            output_data=output_data,
            error_type=error_type,
            error_message=error_message,
            latency_ms=latency_ms,
            created_at=now_iso,
            provenance=prov,
        )


class BrokeredTool(Tool):
    """Adapter wrapping a tool to route all calls through AgentRuntime & ToolBroker."""

    def __init__(
        self,
        tool_name: str,
        runtime: AgentRuntime,
        run_id: str,
        description: str = "",
        parameters: Optional[list[ToolParameter]] = None,
        tool_timeout: Optional[float] = None,
    ):
        super().__init__(name=tool_name, description=description or f"Brokered {tool_name}")
        self.runtime = runtime
        self.run_id = run_id
        self._parameters = parameters or []
        self.tool_timeout = tool_timeout

    def get_parameters(self) -> list[ToolParameter]:
        if self._parameters:
            return self._parameters
        return self.runtime.tool_broker.get_parameters(self.name)

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        record = self.runtime.execute_tool(
            run_id=self.run_id,
            tool_name=self.name,
            parameters=parameters,
            tool_timeout=self.tool_timeout,
        )
        if record.status == "EXECUTED":
            return ToolResponse.success(
                text=record.output_text,
                data=record.output_data,
                stats={"latency_ms": record.latency_ms},
            )
        elif record.status == "REJECTED":
            return ToolResponse.error(
                code=record.error_type or "REJECTED",
                message=record.error_message or "Tool call rejected by policy",
                stats={"latency_ms": record.latency_ms},
            )
        else:
            return ToolResponse.error(
                code=record.error_type or "ERROR",
                message=record.error_message or "Tool execution failed",
                stats={"latency_ms": record.latency_ms},
            )


class AgentRuntime:
    """Manages the full lifecycle of agent runs and unified tool execution."""

    def __init__(
        self,
        db_path: Path,
        tool_broker: Optional[ToolBroker] = None,
        registry: Optional[Any] = None,
        deployment_manager: Optional[Any] = None,
        episode_store: Optional[Any] = None,
        collector: Optional[Any] = None,
        memory_manager: Optional[Any] = None,
    ):
        self.db_path = db_path
        self.tool_broker = tool_broker or ToolBroker()
        self.registry = registry
        self.deployment_manager = deployment_manager
        self.collector = collector
        self.episode_store = episode_store or (
            getattr(collector, "episode_store", None) if collector else None
        )
        if self.collector is None and self.episode_store is not None:
            from .collector import ExperienceCollector
            self.collector = ExperienceCollector(
                episode_store=self.episode_store,
                registry=self.registry,
            )
        self.memory_manager = memory_manager
        self._lock = threading.Lock()
        self._local = threading.local()
        self._provenances: dict[str, list[ToolCallProvenance]] = {}
        self._skill_required_tools: dict[str, set[str]] = {}
        self._run_retrieval_results: dict[str, FutureRetrievalResult] = {}
        self._run_artifact_validators: dict[str, Any] = {}

    def register_artifact_validator(self, run_id: str, validator: Any) -> None:
        """Bind an authoritative validator to a run for end-to-end receipt verification."""
        with self._lock:
            self._run_artifact_validators[run_id] = validator

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = init_db(self.db_path)
        return self._local.conn

    def close(self) -> None:
        with self._lock:
            if hasattr(self._local, "conn") and self._local.conn is not None:
                try:
                    self._local.conn.close()
                except Exception:
                    pass
                self._local.conn = None

    def start_run(
        self,
        run_id: str,
        task_id: str,
        skill_name: Optional[str] = None,
        purpose: Literal["evaluation", "learning"] = "evaluation",
        budget_max: int = 10,
        deadline_ts: Optional[float] = None,
        skill_required_tools: Optional[set[str]] = None,
        enable_reuse: bool = False,
        task_description: Optional[str] = None,
        retrieval_context: Optional[RetrievalContext] = None,
        memory_manager: Optional[Any] = None,
        require_reuse: bool = False,
    ) -> RunRecord:
        """Start or re-attach to an execution run.

        Guarantees:
        - Binds and freezes skill version and content hash at start time.
        - Supports optional application-driven reuse mode via bounded P3 retrieval.
        - Idempotent on identical run_id parameters; rejects conflicting input.
        - Preserves run state across SQLite reopen.
        """
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                """SELECT run_id, task_id, skill_name, skill_version, content_hash,
                          status, purpose, budget_max, budget_consumed, deadline_ts,
                          error_type, error_message, created_at, updated_at, terminal_at
                   FROM runtime_runs WHERE run_id = ?""",
                (run_id,),
            ).fetchone()

            if row:
                # Idempotency vs conflict check
                existing_task = row[1]
                existing_skill = row[2]
                existing_purpose = row[6]
                if (
                    existing_task != task_id
                    or (skill_name is not None and existing_skill != skill_name)
                    or existing_purpose != purpose
                ):
                    raise ValueError(
                        f"Conflicting run definition for existing run_id '{run_id}': "
                        f"task_id={existing_task} vs {task_id}, skill={existing_skill} vs {skill_name}, "
                        f"purpose={existing_purpose} vs {purpose}"
                    )
                return RunRecord(
                    run_id=row[0],
                    task_id=row[1],
                    skill_name=row[2],
                    skill_version=row[3],
                    content_hash=row[4],
                    status=row[5],
                    purpose=row[6],
                    budget_max=row[7],
                    budget_consumed=row[8],
                    deadline_ts=row[9],
                    error_type=row[10],
                    error_message=row[11],
                    created_at=row[12],
                    updated_at=row[13],
                    terminal_at=row[14],
                )

            # Resolve skill dependencies placeholder
            assigned_ver: Optional[str] = None
            assigned_hash: Optional[str] = None
            req_tools = (
                set(skill_required_tools)
                if skill_required_tools is not None
                else None
            )

            # Auto-retrieval in reuse mode
            retrieval_hit = False
            retrieval_reasons: list[str] = []
            retrieval_empty_reason: Optional[str] = None
            retrieval_filtered: list[dict[str, Any]] = []

            if enable_reuse and not skill_name:
                eff_mm = memory_manager or self.memory_manager
                if eff_mm is None and (self.registry or self.deployment_manager or self.episode_store):
                    from .memory import ThreeTierMemoryManager
                    from .episode import CandidateStore
                    eff_mm = ThreeTierMemoryManager(
                        db_path=self.db_path,
                        registry=self.registry,
                        deployment_manager=self.deployment_manager,
                        episode_store=self.episode_store,
                        candidate_store=CandidateStore(self.db_path),
                    )

                if eff_mm is not None:
                    query_text = (task_description or task_id or "").strip()
                    allowed_tools = (
                        set(self.tool_broker.application_allowlist)
                        if self.tool_broker and self.tool_broker.application_allowlist
                        else None
                    )
                    ctx = retrieval_context or RetrievalContext(
                        task_id=task_id,
                        run_id=run_id,
                        allowed_tools=allowed_tools,
                    )
                    if ctx.run_id is None:
                        ctx.run_id = run_id
                    if ctx.task_id is None:
                        ctx.task_id = task_id
                    if ctx.allowed_tools is None and allowed_tools:
                        ctx.allowed_tools = allowed_tools

                    retrieval_res = eff_mm.retrieve(query=query_text, context=ctx)
                    self._run_retrieval_results[run_id] = retrieval_res

                    if retrieval_res.skills:
                        rec = retrieval_res.skills[0]
                        skill_name = rec.skill_name
                        assigned_ver = rec.version
                        assigned_hash = rec.content_hash
                        if req_tools is None and rec.dependencies:
                            req_tools = set(rec.dependencies)
                        retrieval_hit = True
                        retrieval_reasons = list(rec.match_reasons)
                    else:
                        retrieval_hit = False
                        retrieval_empty_reason = retrieval_res.empty_reason
                        retrieval_filtered = list(retrieval_res.filtered_out)
                else:
                    retrieval_hit = False
                    retrieval_empty_reason = "No memory manager configured for retrieval"

            # Strict reuse mode fallback: terminal rejection if required but not found/allowed
            if enable_reuse and require_reuse and not retrieval_hit:
                now_iso = datetime.now(timezone.utc).isoformat()
                err_type = "NO_REUSABLE_SKILL"
                if retrieval_filtered:
                    first_ft = retrieval_filtered[0].get("filter_type")
                    if first_ft == "permission_or_dependency_denied":
                        err_type = "PERMISSION_DENIED"
                err_msg = retrieval_empty_reason or "No reusable formal skill matched or available"
                conn.execute(
                    """INSERT INTO runtime_runs (
                        run_id, task_id, skill_name, skill_version, content_hash,
                        status, purpose, budget_max, budget_consumed, deadline_ts,
                        error_type, error_message, created_at, updated_at, terminal_at
                    ) VALUES (?, ?, NULL, NULL, NULL, 'FAILED', ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        task_id,
                        purpose,
                        budget_max,
                        deadline_ts,
                        err_type,
                        err_msg,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )
                conn.commit()
                if self.collector:
                    env_info: dict[str, Any] = {
                        "purpose": purpose,
                        "enable_reuse": True,
                        "retrieval_hit": False,
                        "retrieval_empty_reason": retrieval_empty_reason,
                        "retrieval_filtered_out": retrieval_filtered,
                    }
                    if task_description:
                        env_info["task_description"] = task_description
                    if self.tool_broker and getattr(self.tool_broker, "sandbox_backend", None):
                        env_info["backend"] = self.tool_broker.sandbox_backend.name
                    self.collector.start_run(
                        run_id=run_id,
                        task_id=task_id,
                        skill_name=None,
                        skill_version=None,
                        environment=env_info,
                    )
                    self.collector.finish_run(
                        run_id=run_id,
                        infra_error=err_msg,
                        verification_evidence={
                            "independent_pass": False,
                            "failure_reason": err_msg,
                        },
                    )
                return RunRecord(
                    run_id=run_id,
                    task_id=task_id,
                    skill_name=None,
                    skill_version=None,
                    content_hash=None,
                    status="FAILED",
                    purpose=purpose,
                    budget_max=budget_max,
                    budget_consumed=0,
                    deadline_ts=deadline_ts,
                    error_type=err_type,
                    error_message=err_msg,
                    created_at=now_iso,
                    updated_at=now_iso,
                    terminal_at=now_iso,
                )

            # Resolve skill version & hash via DeploymentManager or Registry
            if skill_name:
                if self.deployment_manager:
                    assigned_ver, assigned_hash, _ = self.deployment_manager.route_version(
                        skill_name, run_id=run_id
                    )
                    if req_tools is None:
                        try:
                            snap = self.deployment_manager.get_version_snapshot(skill_name, assigned_ver)
                            if snap and snap.dependencies:
                                req_tools = set(snap.dependencies)
                        except Exception:
                            pass
                elif self.registry:
                    try:
                        meta = self.registry.get_meta(skill_name)
                        if not assigned_ver:
                            assigned_ver = meta.version
                        if not assigned_hash:
                            assigned_hash = hashlib.sha256(
                                self.registry.get_body(skill_name).strip().encode("utf-8")
                            ).hexdigest()
                        if req_tools is None and getattr(meta, "dependencies", None):
                            req_tools = set(meta.dependencies)
                    except Exception:
                        pass

            self._skill_required_tools[run_id] = req_tools
            self._provenances[run_id] = []

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO runtime_runs (
                    run_id, task_id, skill_name, skill_version, content_hash,
                    status, purpose, budget_max, budget_consumed, deadline_ts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?, 0, ?, ?, ?)""",
                (
                    run_id,
                    task_id,
                    skill_name,
                    assigned_ver,
                    assigned_hash,
                    purpose,
                    budget_max,
                    deadline_ts,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

            # Start ExperienceCollector if configured
            if self.collector:
                env_info: dict[str, Any] = {"purpose": purpose}
                if self.tool_broker and getattr(self.tool_broker, "sandbox_backend", None):
                    env_info["backend"] = self.tool_broker.sandbox_backend.name
                    if getattr(self.tool_broker, "dependency_prober", None):
                        env_info["environment_fingerprint"] = (
                            self.tool_broker.dependency_prober.compute_fingerprint()
                        )
                if task_description:
                    env_info["task_description"] = task_description
                if enable_reuse:
                    env_info["enable_reuse"] = True
                    env_info["retrieval_hit"] = retrieval_hit
                    if retrieval_hit:
                        env_info["retrieval_reasons"] = retrieval_reasons
                        env_info["selected_skill"] = skill_name
                        env_info["selected_version"] = assigned_ver
                    else:
                        env_info["retrieval_empty_reason"] = retrieval_empty_reason
                        env_info["retrieval_filtered_out"] = retrieval_filtered

                self.collector.start_run(
                    run_id=run_id,
                    task_id=task_id,
                    skill_name=skill_name,
                    skill_version=assigned_ver,
                    environment=env_info,
                )

            return RunRecord(
                run_id=run_id,
                task_id=task_id,
                skill_name=skill_name,
                skill_version=assigned_ver,
                content_hash=assigned_hash,
                status="RUNNING",
                purpose=purpose,
                budget_max=budget_max,
                budget_consumed=0,
                deadline_ts=deadline_ts,
                created_at=now_iso,
                updated_at=now_iso,
            )

    def execute_tool(
        self,
        run_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        call_id: Optional[str] = None,
        tool_timeout: Optional[float] = None,
    ) -> ToolCallRecord:
        """Execute a tool call under runtime budget, deadline, and permission guards."""
        eff_call_id = call_id or f"call_{uuid.uuid4().hex[:12]}"
        conn = self._get_conn()

        with self._lock:
            # 1. Idempotency on call_id
            existing = conn.execute(
                """SELECT call_id, run_id, tool_name, status, input_params_json,
                          output_text, output_data_json, error_type, error_message,
                          latency_ms, created_at
                   FROM runtime_tool_calls WHERE call_id = ?""",
                (eff_call_id,),
            ).fetchone()
            if existing:
                return ToolCallRecord(
                    call_id=existing[0],
                    run_id=existing[1],
                    tool_name=existing[2],
                    status=existing[3],
                    input_params=json.loads(existing[4]) if existing[4] else {},
                    output_text=existing[5] or "",
                    output_data=json.loads(existing[6]) if existing[6] else {},
                    error_type=existing[7],
                    error_message=existing[8],
                    latency_ms=existing[9] or 0.0,
                    created_at=existing[10],
                )

            # 2. Check run state & budget
            run_row = conn.execute(
                """SELECT status, budget_max, budget_consumed, deadline_ts, purpose
                   FROM runtime_runs WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run '{run_id}' not found in runtime")

            status, b_max, b_consumed, deadline_ts, purpose = run_row
            sanitized = sanitize_params(parameters)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Reject if already terminal
            if status in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
                "INTERRUPTED",
            ):
                err_type = (
                    status
                    if status in ("BUDGET_EXHAUSTED", "TIMED_OUT", "CANCELLED")
                    else "DISPATCH_AFTER_TERMINAL"
                )
                rec = ToolCallRecord(
                    call_id=eff_call_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="REJECTED",
                    input_params=sanitized,
                    error_type=err_type,
                    error_message=f"Cannot dispatch tool after run entered terminal state '{status}'",
                    latency_ms=0.0,
                    created_at=now_iso,
                )
                self._persist_tool_call(rec)
                return rec

            # Check deadline
            if deadline_ts and time.time() > deadline_ts:
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'TIMED_OUT', error_type = 'TIMEOUT',
                           error_message = 'Task execution deadline exceeded',
                           updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (now_iso, now_iso, run_id),
                )
                conn.commit()
                if self.collector:
                    self.collector.finish_run(
                        run_id=run_id,
                        infra_error="Task execution deadline exceeded",
                        verification_evidence={
                            "independent_pass": None,
                            "failure_reason": "Task execution deadline exceeded",
                        },
                    )
                rec = ToolCallRecord(
                    call_id=eff_call_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="TIMED_OUT",
                    input_params=sanitized,
                    error_type="TIMEOUT",
                    error_message="Task execution deadline exceeded",
                    latency_ms=0.0,
                    created_at=now_iso,
                )
                self._persist_tool_call(rec)
                return rec

            # Check budget: rejected attempts also consume request budget!
            if b_consumed >= b_max:
                conn.execute(
                    "UPDATE runtime_runs SET status = 'BUDGET_EXHAUSTED', updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                    (run_id,),
                )
                conn.commit()
                rec = ToolCallRecord(
                    call_id=eff_call_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="REJECTED",
                    input_params=sanitized,
                    error_type="BUDGET_EXHAUSTED",
                    error_message=f"max_tool_calls limit ({b_max}) reached",
                    latency_ms=0.0,
                    created_at=now_iso,
                )
                self._persist_tool_call(rec)
                return rec

            # Pre-consume 1 budget ticket atomically
            new_consumed = b_consumed + 1
            conn.execute(
                "UPDATE runtime_runs SET budget_consumed = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (new_consumed, run_id),
            )
            conn.commit()

            skill_reqs = self._skill_required_tools.get(run_id)

        # Dispatch via broker
        call_rec = self.tool_broker.dispatch(
            run_id=run_id,
            tool_name=tool_name,
            parameters=parameters,
            call_id=eff_call_id,
            skill_required_tools=skill_reqs,
            timeout=tool_timeout,
        )

        with self._lock:
            self._persist_tool_call(call_rec)
            if call_rec.provenance:
                self._provenances.setdefault(run_id, []).append(call_rec.provenance)
                if self.collector:
                    self.collector.record_tool_call(
                        run_id=run_id,
                        provenance=call_rec.provenance,
                        action_summary=f"Tool {tool_name} dispatch: {call_rec.status}",
                    )

            # Check if budget was just reached
            if new_consumed >= b_max:
                conn.execute(
                    "UPDATE runtime_runs SET status = 'BUDGET_EXHAUSTED', updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                    (run_id,),
                )
                conn.commit()

        return call_rec

    def _persist_tool_call(self, record: ToolCallRecord) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO runtime_tool_calls (
                call_id, run_id, tool_name, status, input_params_json,
                output_text, output_data_json, error_type, error_message,
                latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.call_id,
                record.run_id,
                record.tool_name,
                record.status,
                json.dumps(record.input_params),
                record.output_text,
                json.dumps(record.output_data),
                record.error_type,
                record.error_message,
                record.latency_ms,
                record.created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    def cancel_run(self, run_id: str, reason: str = "User cancelled") -> RunRecord:
        """Explicitly cancel an ongoing run."""
        if self.tool_broker and getattr(self.tool_broker, "sandbox_backend", None):
            self.tool_broker.sandbox_backend.cancel_run(run_id)
        conn = self._get_conn()
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            row = conn.execute(
                "SELECT status FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Run '{run_id}' not found")
            if row[0] not in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
            ):
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'CANCELLED', error_type = 'CANCELLED',
                           error_message = ?, updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (reason, now_iso, now_iso, run_id),
                )
                conn.commit()
                if self.collector:
                    self.collector.finish_run(
                        run_id=run_id,
                        infra_error=f"CANCELLED: {reason}",
                        verification_evidence={
                            "independent_pass": None,
                            "failure_reason": f"CANCELLED: {reason}",
                        },
                    )

        return self.get_run(run_id)  # type: ignore

    def finalize_run(
        self,
        run_id: str,
        model_output: str = "",
        verification_evidence: Optional[dict[str, Any]] = None,
        acceptance_criteria: Optional[dict[str, Any]] = None,
        infra_error: Optional[str] = None,
    ) -> tuple[RunRecord, Optional[Episode]]:
        """Transition run to terminal state and produce immutable Episode.

        Invariants:
        - Model self-assertion does NOT determine business success (separated outcome).
        - Idempotent: repeated calls do not duplicate Episodes or alter terminal status.
        """
        conn = self._get_conn()
        with self._lock:
            run = self.get_run(run_id)
            if not run:
                raise KeyError(f"Run '{run_id}' not found")

            # Idempotent terminal return
            if run.status in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
            ):
                ep = (
                    self.episode_store.get_episode(f"ep_{run_id}")
                    if self.episode_store
                    else None
                )
                return run, ep

            now_iso = datetime.now(timezone.utc).isoformat()
            terminal_status: RuntimeStatus = (
                "FAILED" if infra_error else "COMPLETED"
            )
            err_type = "INFRASTRUCTURE_ERROR" if infra_error else None

            conn.execute(
                """UPDATE runtime_runs
                   SET status = ?, error_type = ?, error_message = ?,
                       updated_at = ?, terminal_at = ?
                   WHERE run_id = ?""",
                (terminal_status, err_type, infra_error, now_iso, now_iso, run_id),
            )
            conn.commit()

            eff_verif = dict(verification_evidence) if verification_evidence else {}
            bound_validator = self._run_artifact_validators.get(run_id) or eff_verif.get("validator")

            if bound_validator is not None:
                # Authoritative validator bound to this run: strictly verify receipt and content
                if "receipt" not in eff_verif or "content" not in eff_verif:
                    eff_verif["independent_pass"] = False
                    eff_verif["failure_reason"] = "Missing receipt or content for validated run"
                else:
                    rcpt = eff_verif["receipt"]
                    rcpt_dict = rcpt.to_dict() if hasattr(rcpt, "to_dict") else (rcpt if isinstance(rcpt, dict) else {})
                    rcpt_fp = rcpt_dict.get("content_fingerprint")
                    rcpt_val_id = rcpt_dict.get("validator_id")
                    rcpt_val_ver = rcpt_dict.get("validator_version")
                    rcpt_cfg_hash = rcpt_dict.get("validator_config_hash")
                    rcpt_status = rcpt_dict.get("status")

                    from .receipt import compute_artifact_fingerprint
                    act_fp = compute_artifact_fingerprint(eff_verif["content"])

                    if not rcpt_fp or act_fp != rcpt_fp:
                        eff_verif["independent_pass"] = False
                        eff_verif["failure_reason"] = "Mismatched content fingerprint for validation receipt"
                    elif getattr(bound_validator, "validator_id", None) != rcpt_val_id or getattr(bound_validator, "validator_version", None) != rcpt_val_ver:
                        eff_verif["independent_pass"] = False
                        eff_verif["failure_reason"] = f"Mismatched validator identity ({rcpt_val_id} vs {getattr(bound_validator, 'validator_id', None)})"
                    elif getattr(bound_validator, "config_hash", None) != rcpt_cfg_hash:
                        eff_verif["independent_pass"] = False
                        eff_verif["failure_reason"] = "Validator configuration hash drift detected"
                    else:
                        # Authoritative re-validation using bound validator
                        authoritative_rcpt = bound_validator.validate(
                            eff_verif["content"],
                            task_id=run.task_id,
                            run_id=run_id,
                            skill_version=run.skill_version,
                        )
                        if authoritative_rcpt.status != "PASS" or rcpt_status != "PASS":
                            eff_verif["independent_pass"] = False
                            eff_verif["failure_reason"] = f"Authoritative re-validation failed with status {authoritative_rcpt.status}"
                        else:
                            eff_verif["independent_pass"] = True
                            eff_verif["authoritative_receipt"] = authoritative_rcpt.to_dict()
            elif "receipt" in eff_verif and "content" in eff_verif:
                rcpt = eff_verif["receipt"]
                rcpt_fp = (
                    rcpt.content_fingerprint
                    if hasattr(rcpt, "content_fingerprint")
                    else (rcpt.get("content_fingerprint") if isinstance(rcpt, dict) else None)
                )
                from .receipt import compute_artifact_fingerprint
                act_fp = compute_artifact_fingerprint(eff_verif["content"])
                if rcpt_fp and act_fp != rcpt_fp:
                    eff_verif["independent_pass"] = False
                    eff_verif["failure_reason"] = "Mismatched content fingerprint for validation receipt"

            ep = None
            if self.collector:
                ep = self.collector.finish_run(
                    run_id=run_id,
                    model_output=model_output,
                    verification_evidence=eff_verif if eff_verif else verification_evidence,
                    acceptance_criteria=acceptance_criteria,
                    infra_error=infra_error,
                )

        return self.get_run(run_id), ep  # type: ignore

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        """Fetch current run record, marking active unfinished runs after reopen as INTERRUPTED."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT run_id, task_id, skill_name, skill_version, content_hash,
                      status, purpose, budget_max, budget_consumed, deadline_ts,
                      error_type, error_message, created_at, updated_at, terminal_at
               FROM runtime_runs WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return RunRecord(
            run_id=row[0],
            task_id=row[1],
            skill_name=row[2],
            skill_version=row[3],
            content_hash=row[4],
            status=row[5],
            purpose=row[6],
            budget_max=row[7],
            budget_consumed=row[8],
            deadline_ts=row[9],
            error_type=row[10],
            error_message=row[11],
            created_at=row[12],
            updated_at=row[13],
            terminal_at=row[14],
        )

    def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        """List all tool call records for a run in chronological order."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT call_id, run_id, tool_name, status, input_params_json,
                      output_text, output_data_json, error_type, error_message,
                      latency_ms, created_at
               FROM runtime_tool_calls WHERE run_id = ?
               ORDER BY created_at ASC""",
            (run_id,),
        ).fetchall()
        return [
            ToolCallRecord(
                call_id=r[0],
                run_id=r[1],
                tool_name=r[2],
                status=r[3],
                input_params=json.loads(r[4]) if r[4] else {},
                output_text=r[5] or "",
                output_data=json.loads(r[6]) if r[6] else {},
                error_type=r[7],
                error_message=r[8],
                latency_ms=r[9] or 0.0,
                created_at=r[10],
            )
            for r in rows
        ]

    def create_brokered_tool(
        self,
        tool_name: str,
        run_id: str,
        description: str = "",
        tool_timeout: Optional[float] = None,
    ) -> BrokeredTool:
        """Create a BrokeredTool adapter bound to this runtime and run_id."""
        return BrokeredTool(
            tool_name=tool_name,
            runtime=self,
            run_id=run_id,
            description=description,
            parameters=self.tool_broker.get_parameters(tool_name),
            tool_timeout=tool_timeout,
        )

    def get_provenances(self, run_id: str) -> list[ToolCallProvenance]:
        """Fetch all provenances recorded for a run."""
        with self._lock:
            return list(self._provenances.get(run_id, []))

    def get_retrieval_result(self, run_id: str) -> Optional[FutureRetrievalResult]:
        """Fetch the FutureRetrievalResult if reuse mode was triggered for this run."""
        return self._run_retrieval_results.get(run_id)

    def attribute_run_failure(
        self,
        run_id: str,
        llm: Any = None,
    ) -> Optional[Any]:
        """Route a failed run's terminal episode through M4a failure attribution."""
        if not self.episode_store:
            return None
        ep = self.episode_store.get_episode(f"ep_{run_id}")
        if not ep or ep.outcome != "failure" or not ep.skill_name:
            return None
        from .repair import attribute_failure
        return attribute_failure([ep], skill_name=ep.skill_name, llm=llm)

    def create_repair_job_for_run(
        self,
        run_id: str,
        candidate_store: Optional[Any] = None,
        llm: Any = None,
        max_attempts: int = 2,
    ) -> Optional[Any]:
        """Create bounded RepairJob from a failed run's terminal episode without auto-publishing."""
        diag = self.attribute_run_failure(run_id, llm=llm)
        if not diag:
            return None
        ep = self.episode_store.get_episode(f"ep_{run_id}")
        if not ep:
            return None
        from .repair import RepairJob, _save_job
        conn = self._get_conn()
        fingerprint = hashlib.sha256(
            f"{ep.skill_name}:{diag.responsibility_layer}:{ep.episode_id}".encode("utf-8")
        ).hexdigest()[:16]
        job = RepairJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            fingerprint=fingerprint,
            skill_name=ep.skill_name,
            baseline_version=ep.skill_version or "1.0.0",
            source_episode_ids=[ep.episode_id],
            diagnosis=diag,
            status="PENDING" if diag.responsibility_layer == "skill" else "BLOCKED",
            max_attempts=max_attempts,
            current_attempt=0,
            attempts=[],
            stop_reason=diag.handoff_info if diag.responsibility_layer != "skill" else None,
        )
        _save_job(conn, job)
        return job

    def repair_artifact(
        self,
        run_id: str,
        initial_content: dict[str, Any] | str,
        validator: Any,
        fixer: Optional[Any] = None,
        correction_policy: Optional[Any] = None,
    ) -> tuple[dict[str, Any] | str, Any, list[dict[str, Any]], Optional[str]]:
        """Drive narrow local repair of a structured artifact using actionable ValidationReceipts.

        Invariants:
        - Bounded by correction_policy (hard ceiling max_corrections = 2).
        - Respects AgentRuntime lifecycle, budget, timeout, and cancellation.
        - Enforces strict allowed_paths, allowed_ops, and supported_fixes.
        - Detects no-progress and cyclic modifications.
        - Re-validates with the SAME validator on each iteration.
        - Preserves run state and refuses execution after terminal state.
        """
        # Automatically register authoritative validator for this run
        self.register_artifact_validator(run_id, validator)

        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"Run '{run_id}' not found in runtime")

        # Reject if already terminal
        if run.status in (
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "BUDGET_EXHAUSTED",
            "INTERRUPTED",
        ):
            initial_rcpt = validator.validate(
                initial_content,
                task_id=run.task_id,
                run_id=run_id,
                skill_version=run.skill_version,
            )
            return initial_content, initial_rcpt, [], f"RUN_ALREADY_TERMINAL_{run.status}"

        # Initial validation
        initial_receipt = validator.validate(
            initial_content,
            task_id=run.task_id,
            run_id=run_id,
            skill_version=run.skill_version,
        )
        if initial_receipt.status == "PASS":
            return initial_content, initial_receipt, [], "ALREADY_PASSED"

        # Check policy enablement and fixer presence
        if correction_policy is None or not getattr(correction_policy, "enabled", True) or fixer is None:
            return initial_content, initial_receipt, [], "CORRECTION_DISABLED"

        # Check for policy/environmental rejections
        policy_diags = [
            d for d in initial_receipt.diagnostics
            if getattr(d, "responsibility_layer", "") == "policy"
        ]
        if policy_diags:
            return initial_content, initial_receipt, [], "POLICY_DENIED_NO_REPAIR"

        # Check if all diagnostics are unfixable / non-retryable
        retryable_diags = [
            d for d in initial_receipt.diagnostics
            if getattr(d, "retryable", True) and getattr(d, "supported_fixes", None)
        ]
        if not retryable_diags:
            return initial_content, initial_receipt, [], "UNFIXABLE_RULE_NO_REPAIR"

        # Local repair loop
        from .receipt import compute_artifact_fingerprint, apply_action_to_content
        import copy
        # ponytail: hard ceiling of max 2 narrow repair attempts even if caller configures more
        configured_max = getattr(correction_policy, "max_corrections", 2)
        max_corrections = min(max(1, configured_max), 2)
        current_content = copy.deepcopy(initial_content)
        current_receipt = initial_receipt
        history: list[dict[str, Any]] = []
        seen_fingerprints = {current_receipt.content_fingerprint}
        conn = self._get_conn()

        for attempt in range(1, max_corrections + 1):
            with self._lock:
                row = conn.execute(
                    "SELECT status, budget_max, budget_consumed, deadline_ts FROM runtime_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if not row:
                    break
                st, b_max, b_consumed, dl_ts = row
                now_iso = datetime.now(timezone.utc).isoformat()

                if st in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXHAUSTED", "INTERRUPTED"):
                    return current_content, current_receipt, history, f"RUN_ALREADY_TERMINAL_{st}"

                if dl_ts and time.time() > dl_ts:
                    conn.execute(
                        "UPDATE runtime_runs SET status = 'TIMED_OUT', error_type = 'TIMEOUT', updated_at = ?, terminal_at = ? WHERE run_id = ?",
                        (now_iso, now_iso, run_id),
                    )
                    conn.commit()
                    return current_content, current_receipt, history, "TIMEOUT"

                if b_consumed >= b_max:
                    conn.execute(
                        "UPDATE runtime_runs SET status = 'BUDGET_EXHAUSTED', error_type = 'BUDGET_EXHAUSTED', updated_at = ?, terminal_at = ? WHERE run_id = ?",
                        (now_iso, now_iso, run_id),
                    )
                    conn.commit()
                    return current_content, current_receipt, history, "BUDGET_EXHAUSTED"

                conn.execute(
                    "UPDATE runtime_runs SET budget_consumed = budget_consumed + 1, updated_at = ? WHERE run_id = ?",
                    (now_iso, run_id),
                )
                conn.commit()

            # Propose fixes
            actions = fixer.suggest_fixes(current_receipt, current_content)
            if not actions:
                return current_content, current_receipt, history, "NO_ACTIONS_SUGGESTED"

            # Check if run became terminal or timed out during external fixer execution
            # ponytail: Python cooperative boundary — user-space fixers cannot be forcibly preempted
            # within the same process; runtime re-checks status immediately upon return to discard late patches.
            with self._lock:
                row_post = conn.execute(
                    "SELECT status, deadline_ts FROM runtime_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row_post:
                    st_post, dl_ts_post = row_post
                    now_iso = datetime.now(timezone.utc).isoformat()
                    if st_post in ("CANCELLED", "TIMED_OUT", "COMPLETED", "FAILED", "BUDGET_EXHAUSTED", "INTERRUPTED"):
                        return current_content, current_receipt, history, f"DISCARDED_LATE_PATCH_RUN_{st_post}"
                    if dl_ts_post and time.time() > dl_ts_post:
                        conn.execute(
                            "UPDATE runtime_runs SET status = 'TIMED_OUT', error_type = 'TIMEOUT', updated_at = ?, terminal_at = ? WHERE run_id = ?",
                            (now_iso, now_iso, run_id),
                        )
                        conn.commit()
                        return current_content, current_receipt, history, "DISCARDED_LATE_PATCH_TIMEOUT"

            # Validate proposed actions against application governance policy
            allowed_paths = getattr(correction_policy, "allowed_paths", set())
            allowed_ops = getattr(correction_policy, "allowed_ops", {"set", "replace"})
            active_supported_paths = {
                fix.get("path")
                for d in current_receipt.diagnostics
                for fix in getattr(d, "supported_fixes", [])
            }

            for act in actions:
                if allowed_paths and act.path not in allowed_paths:
                    return current_content, current_receipt, history, f"OUT_OF_BOUNDS_PATH_{act.path}"
                if active_supported_paths and act.path not in active_supported_paths:
                    return current_content, current_receipt, history, f"UNSUPPORTED_FIX_PATH_{act.path}"
                if act.op not in allowed_ops:
                    return current_content, current_receipt, history, f"UNSUPPORTED_OP_{act.op}"
                # Prohibit privilege escalation / policy tampering in payload
                path_lower = act.path.lower()
                if any(p in path_lower for p in ("policy", "permission", "command", "network", "mount", "token", "secret")):
                    return current_content, current_receipt, history, "SECURITY_POLICY_VIOLATION"

            # Apply actions
            next_content = copy.deepcopy(current_content)
            for act in actions:
                next_content = apply_action_to_content(next_content, act)

            # Check no-progress
            next_fp = compute_artifact_fingerprint(next_content)
            if next_fp == current_receipt.content_fingerprint:
                return current_content, current_receipt, history, "NO_PROGRESS"

            # Check cycle
            if next_fp in seen_fingerprints:
                return current_content, current_receipt, history, "CYCLE_DETECTED"
            seen_fingerprints.add(next_fp)

            # Re-validate with SAME validator
            next_receipt = validator.validate(
                next_content,
                task_id=run.task_id,
                run_id=run_id,
                skill_version=run.skill_version,
            )
            history.append({
                "attempt": attempt,
                "actions": [
                    a.to_dict() if hasattr(a, "to_dict") else asdict(a)
                    for a in actions
                ],
                "prev_fingerprint": current_receipt.content_fingerprint,
                "new_fingerprint": next_receipt.content_fingerprint,
                "receipt_status": next_receipt.status,
            })
            current_content = next_content
            current_receipt = next_receipt

            if next_receipt.status == "PASS":
                return current_content, current_receipt, history, "SUCCESS"

        return current_content, current_receipt, history, "MAX_CORRECTIONS_REACHED"


