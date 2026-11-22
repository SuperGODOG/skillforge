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
    CandidateSkill,
    Episode,
    FutureRetrievalResult,
    PiCheckpointRecord,
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

    def register_mcp_client(
        self,
        client: Any,
        auto_allow: bool = True,
    ) -> list[str]:
        """Discover and register tools from an McpStdioClient into the broker."""
        from .mcp_client import McpTool

        tool_defs = client.list_tools()
        registered: list[str] = []
        for t_def in tool_defs:
            tool_instance = McpTool(client, t_def)
            self.register_tool(tool_instance)
            if auto_allow:
                self.application_allowlist.add(tool_instance.name)
            registered.append(tool_instance.name)
        return registered

    def register_mcp_server(
        self,
        command: Sequence[str],
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[dict[str, str]] = None,
        auto_allow: bool = True,
    ) -> tuple[Any, list[str]]:
        """Spawn a local stdio MCP server subprocess and register all its exported tools."""
        from .mcp_client import McpStdioClient

        client = McpStdioClient(command=command, cwd=cwd, env=env)
        client.start()
        registered_names = self.register_mcp_client(client, auto_allow=auto_allow)
        return client, registered_names

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
            prov = ToolCallProvenance(
                tool_name=tool_name,
                fixture_case_id=eff_call_id,
                call_index=0,
                call_count=1,
                is_fixture=False,
                tool_required=False,
                tool_called=True,
                tool_success=False,
                authenticity_pass=True,
                input_params=sanitized_input,
                output_status="ERROR",
                output_summary=f"Tool '{tool_name}' is not registered in broker",
                latency_ms=0.0,
                timestamp=now_iso,
                signature=f"sha256:{hashlib.sha256(f'{tool_name}:{eff_call_id}:UNKNOWN'.encode()).hexdigest()}",
                snapshot_id=f"snap_{eff_call_id}",
            )
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
                provenance=prov,
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
            prov = ToolCallProvenance(
                tool_name=tool_name,
                fixture_case_id=eff_call_id,
                call_index=0,
                call_count=1,
                is_fixture=False,
                tool_required=False,
                tool_called=True,
                tool_success=False,
                authenticity_pass=True,
                input_params=sanitized_input,
                output_status="ERROR",
                output_summary=err_msg,
                latency_ms=0.0,
                timestamp=now_iso,
                signature=f"sha256:{hashlib.sha256(f'{tool_name}:{eff_call_id}:DENIED'.encode()).hexdigest()}",
                snapshot_id=f"snap_{eff_call_id}",
            )
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
                provenance=prov,
            )

        # 3. Schema validation
        tool_params = self.get_parameters(tool_name)
        valid, schema_err = validate_parameter_schema(tool_params, parameters)
        if not valid:
            prov = ToolCallProvenance(
                tool_name=tool_name,
                fixture_case_id=eff_call_id,
                call_index=0,
                call_count=1,
                is_fixture=False,
                tool_required=False,
                tool_called=True,
                tool_success=False,
                authenticity_pass=True,
                input_params=sanitized_input,
                output_status="ERROR",
                output_summary=schema_err or "Invalid parameters",
                latency_ms=0.0,
                timestamp=now_iso,
                signature=f"sha256:{hashlib.sha256(f'{tool_name}:{eff_call_id}:SCHEMA'.encode()).hexdigest()}",
                snapshot_id=f"snap_{eff_call_id}",
            )
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
                provenance=prov,
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

            # Statically determine calling arguments before invocation
            # to guarantee target_fn is executed exactly once (avoiding double-invocation on internal TypeError)
            call_args: tuple = (parameters,)
            call_kwargs: dict = {}
            if isinstance(parameters, dict):
                try:
                    sig = inspect.signature(target_fn)
                    can_bind_single = False
                    try:
                        sig.bind(parameters)
                        can_bind_single = True
                    except TypeError:
                        can_bind_single = False

                    can_bind_kwargs = False
                    try:
                        sig.bind(**parameters)
                        can_bind_kwargs = True
                    except TypeError:
                        can_bind_kwargs = False

                    if isinstance(tool_obj, Tool):
                        call_args = (parameters,)
                        call_kwargs = {}
                    elif can_bind_kwargs and not can_bind_single:
                        call_args = ()
                        call_kwargs = parameters
                    elif can_bind_single and not can_bind_kwargs:
                        call_args = (parameters,)
                        call_kwargs = {}
                    elif can_bind_kwargs and can_bind_single:
                        p0 = list(sig.parameters.keys())[0]
                        if p0 in ("parameters", "params", "input", "data", "payload") or p0 not in parameters:
                            call_args = (parameters,)
                            call_kwargs = {}
                        else:
                            call_args = ()
                            call_kwargs = parameters
                except (ValueError, TypeError):
                    call_args = (parameters,)
                    call_kwargs = {}

            # Check if coroutine function (cooperative async)
            if inspect.iscoroutinefunction(target_fn):
                coro = target_fn(*call_args, **call_kwargs)
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
                res = target_fn(*call_args, **call_kwargs)

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
        candidate_store: Optional[Any] = None,
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
        self.candidate_store = candidate_store
        self._lock = threading.Lock()
        self._local = threading.local()
        self._provenances: dict[str, list[ToolCallProvenance]] = {}
        self._skill_required_tools: dict[str, set[str]] = {}
        self._run_retrieval_results: dict[str, FutureRetrievalResult] = {}
        self._run_artifact_validators: dict[str, Any] = {}
        self._run_candidate_bodies: dict[str, str] = {}
        self._active_pi_clients: dict[str, Any] = {}
        self._last_pi_pids: dict[str, int] = {}
        self._last_pi_result: Optional[Any] = None
        self._last_pi_results: dict[str, Any] = {}
        self._run_harness_states: dict[str, Any] = {}
        self._pause_requested_runs: set[str] = set()

    def get_last_pi_pid(self, run_id: str) -> Optional[int]:
        """Fetch the PID of the most recent Pi subprocess for a run."""
        with self._lock:
            return self._last_pi_pids.get(run_id)

    def get_last_pi_result(self, run_id: Optional[str] = None) -> Optional[Any]:
        """Return the last PiExecutionResult from a prompt execution."""
        with self._lock:
            if run_id and run_id in self._last_pi_results:
                return self._last_pi_results[run_id]
            return self._last_pi_result

    def get_harness_state(self, run_id: str) -> Optional[Any]:
        """Return the live HarnessState for an ongoing or completed run."""
        with self._lock:
            return self._run_harness_states.get(run_id)

    def register_artifact_validator(self, run_id: str, validator: Any) -> None:
        """Bind an authoritative validator to a run for end-to-end receipt verification."""
        with self._lock:
            self._run_artifact_validators[run_id] = validator

    def request_pause_run(self, run_id: str) -> bool:
        """Request a cooperative pause for an active run at the next context checkpoint.

        Idempotent: repeated calls for the same active run succeed without error.
        Explicitly rejects non-existent runs with KeyError and terminal/paused runs with ValueError.
        """
        with self._lock:
            if run_id in self._pause_requested_runs:
                return True

            run = self.get_run(run_id)
            if not run:
                raise KeyError(f"Run '{run_id}' not found")

            if run.status in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
                "INTERRUPTED",
                "PAUSED",
            ):
                raise ValueError(
                    f"Cannot pause run '{run_id}' in terminal/paused status '{run.status}'"
                )

            self._pause_requested_runs.add(run_id)
            return True

    def get_pi_checkpoint(self, run_id: str) -> Optional[PiCheckpointRecord]:
        """Fetch the latest Pi checkpoint for a run joined with authoritative runtime bindings."""
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                """SELECT
                    c.checkpoint_id,
                    c.run_id,
                    c.format_version,
                    c.messages_json,
                    c.system_prompt,
                    c.frozen_body,
                    c.intent_version,
                    c.epoch,
                    c.working_state_json,
                    c.model_config_json,
                    c.tools_json,
                    c.created_at,
                    r.budget_max,
                    r.budget_consumed,
                    r.deadline_ts,
                    r.status
                FROM runtime_pi_checkpoints c
                JOIN runtime_runs r ON c.run_id = r.run_id
                WHERE c.run_id = ?
                ORDER BY c.created_at DESC, c.rowid DESC
                LIMIT 1""",
                (run_id,),
            ).fetchone()

            if not row:
                return None

            fmt_ver = row[2]
            if fmt_ver != 1:
                raise ValueError(
                    f"INVALID_CHECKPOINT: Unsupported format_version '{fmt_ver}' in checkpoint '{row[0]}', expected 1"
                )

            try:
                messages = json.loads(row[3]) if row[3] is not None else []
            except Exception as exc:
                raise ValueError(
                    f"INVALID_CHECKPOINT: Corrupted messages_json in checkpoint '{row[0]}': {exc}"
                ) from exc

            if not isinstance(messages, list):
                raise ValueError(
                    f"INVALID_CHECKPOINT: messages must be a list in checkpoint '{row[0]}'"
                )

            try:
                working_state = json.loads(row[8]) if row[8] is not None else {}
            except Exception as exc:
                raise ValueError(
                    f"INVALID_CHECKPOINT: Corrupted working_state_json in checkpoint '{row[0]}': {exc}"
                ) from exc

            if not isinstance(working_state, dict):
                raise ValueError(
                    f"INVALID_CHECKPOINT: working_state must be a dict in checkpoint '{row[0]}'"
                )

            try:
                model_config = json.loads(row[9]) if row[9] is not None else {}
            except Exception as exc:
                raise ValueError(
                    f"INVALID_CHECKPOINT: Corrupted model_config_json in checkpoint '{row[0]}': {exc}"
                ) from exc

            if not isinstance(model_config, dict):
                raise ValueError(
                    f"INVALID_CHECKPOINT: model_config must be a dict in checkpoint '{row[0]}'"
                )

            try:
                tools = json.loads(row[10]) if row[10] is not None else []
            except Exception as exc:
                raise ValueError(
                    f"INVALID_CHECKPOINT: Corrupted tools_json in checkpoint '{row[0]}': {exc}"
                ) from exc

            if not isinstance(tools, list):
                raise ValueError(
                    f"INVALID_CHECKPOINT: tools must be a list in checkpoint '{row[0]}'"
                )

            return PiCheckpointRecord(
                checkpoint_id=row[0],
                run_id=row[1],
                format_version=int(row[2]) if row[2] is not None else 1,
                messages=messages,
                system_prompt=row[4] or "",
                frozen_body=row[5] or "",
                intent_version=str(row[6]) if row[6] is not None else "",
                epoch=int(row[7]) if row[7] is not None else 0,
                working_state=working_state,
                model_config=model_config,
                tools=tools,
                created_at=str(row[11]) if row[11] is not None else "",
                budget_max=int(row[12]) if row[12] is not None else 0,
                budget_consumed=int(row[13]) if row[13] is not None else 0,
                deadline_ts=float(row[14]) if row[14] is not None else None,
                run_status=str(row[15]) if row[15] is not None else "",
            )

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = init_db(self.db_path)
            self._local.conn.execute(
                """CREATE TABLE IF NOT EXISTS runtime_pi_checkpoints (
                    checkpoint_id        TEXT PRIMARY KEY,
                    run_id               TEXT NOT NULL,
                    format_version       INTEGER NOT NULL DEFAULT 1,
                    messages_json        TEXT NOT NULL,
                    system_prompt        TEXT,
                    frozen_body          TEXT,
                    intent_version       TEXT,
                    epoch                INTEGER NOT NULL DEFAULT 0,
                    working_state_json   TEXT,
                    model_config_json    TEXT,
                    tools_json           TEXT,
                    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id)
                )"""
            )
            self._local.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_pi_checkpoints_run ON runtime_pi_checkpoints(run_id)"
            )
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
        candidate: Optional[CandidateSkill] = None,
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
                          error_type, error_message, created_at, updated_at, terminal_at,
                          frozen_body, intent_revision, task_spec_hash, candidate_id
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
                if len(row) > 15 and row[15] is not None and run_id not in self._run_candidate_bodies:
                    self._run_candidate_bodies[run_id] = row[15]
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
                    frozen_body=row[15] if len(row) > 15 else None,
                    intent_revision=int(row[16]) if len(row) > 16 and row[16] is not None else 1,
                    task_spec_hash=row[17] if len(row) > 17 else None,
                    candidate_id=row[18] if len(row) > 18 else None,
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
                        error_type, error_message, created_at, updated_at, terminal_at,
                        frozen_body, intent_revision, task_spec_hash, candidate_id
                    ) VALUES (?, ?, NULL, NULL, NULL, 'FAILED', ?, ?, 0, ?, ?, ?, ?, ?, ?, NULL, 1, NULL, NULL)""",
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

            # Resolve skill version & hash via candidate, DeploymentManager, or Registry
            if candidate is not None:
                if not skill_name:
                    skill_name = candidate.skill_name
                if not assigned_ver:
                    assigned_ver = candidate.meta.version or "0.1.0-draft"
                if not assigned_hash:
                    assigned_hash = hashlib.sha256(candidate.body.encode("utf-8")).hexdigest()
                self._run_candidate_bodies[run_id] = str(candidate.body)
                if req_tools is None and candidate.meta and candidate.meta.dependencies:
                    req_tools = set(candidate.meta.dependencies)
            elif skill_name:
                if self.deployment_manager:
                    assigned_ver, assigned_hash, _ = self.deployment_manager.route_version(
                        skill_name, run_id=run_id
                    )
                    try:
                        dep_body = self.deployment_manager.get_run_body(skill_name, run_id)
                        if dep_body:
                            self._run_candidate_bodies[run_id] = str(dep_body)
                    except Exception:
                        pass
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
                        body_text = self.registry.get_body(skill_name)
                        if not assigned_hash:
                            assigned_hash = hashlib.sha256(
                                body_text.strip().encode("utf-8")
                            ).hexdigest()
                        self._run_candidate_bodies[run_id] = str(body_text)
                        if req_tools is None and getattr(meta, "dependencies", None):
                            req_tools = set(meta.dependencies)
                    except Exception:
                        pass

            self._skill_required_tools[run_id] = req_tools
            self._provenances[run_id] = []

            now_iso = datetime.now(timezone.utc).isoformat()
            frozen_body_val = self._run_candidate_bodies.get(run_id)
            intent_revision_val = getattr(candidate, "intent_revision", 1) if candidate else 1
            task_spec_hash_val = candidate.task_spec_hash if candidate else None
            candidate_id_val = candidate.candidate_id if candidate else None

            if self.candidate_store and hasattr(self.candidate_store, "get_task_context"):
                tc = self.candidate_store.get_task_context(task_id)
                if tc:
                    if not candidate or getattr(candidate, "intent_revision", None) is None:
                        intent_revision_val = tc.intent_revision
                    if not task_spec_hash_val:
                        task_spec_hash_val = tc.contract_fingerprint or tc.compute_fingerprint()

            conn.execute(
                """INSERT INTO runtime_runs (
                    run_id, task_id, skill_name, skill_version, content_hash,
                    status, purpose, budget_max, budget_consumed, deadline_ts,
                    created_at, updated_at,
                    frozen_body, intent_revision, task_spec_hash, candidate_id
                ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
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
                    frozen_body_val,
                    intent_revision_val,
                    task_spec_hash_val,
                    candidate_id_val,
                ),
            )
            conn.commit()

            # Start ExperienceCollector if configured
            if self.collector:
                env_info: dict[str, Any] = {"purpose": purpose}
                if candidate is not None:
                    env_info["candidate_id"] = candidate.candidate_id
                    env_info["candidate_hash"] = assigned_hash
                    if candidate.task_spec_hash:
                        env_info["task_spec_hash"] = candidate.task_spec_hash
                    if candidate.source_requirement:
                        env_info["source_requirement"] = candidate.source_requirement
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
                frozen_body=frozen_body_val,
                intent_revision=intent_revision_val,
                task_spec_hash=task_spec_hash_val,
                candidate_id=candidate_id_val,
            )

    def resume_run(
        self,
        run_id: str,
        budget_max: Optional[int] = None,
        deadline_ts: Optional[float] = None,
        task_description: Optional[str] = None,
        intent_revision: Optional[int] = None,
    ) -> RunRecord:
        """Resume an existing paused or checkpointed run with updated budget/deadline."""
        conn = self._get_conn()
        with self._lock:
            run = self.get_run(run_id)
            if not run:
                raise KeyError(f"Run '{run_id}' not found")

            TERMINAL_STATUSES = {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
                "INTERRUPTED",
            }
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"Cannot resume run '{run_id}' from terminal state '{run.status}'")

            # Check unsettled ADMITTED tool calls
            admitted = conn.execute(
                "SELECT call_id FROM runtime_tool_calls WHERE run_id = ? AND status = 'ADMITTED' ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            if admitted:
                admitted_ids = [r[0] for r in admitted]
                now_iso = datetime.now(timezone.utc).isoformat()
                msg = f"Unsettled in-flight tool calls requiring review: {', '.join(admitted_ids)}"
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'INTERRUPTED', error_type = 'REVIEW_REQUIRED',
                           error_message = ?, updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (msg, now_iso, now_iso, run_id),
                )
                conn.commit()
                raise ValueError(f"REVIEW_REQUIRED: {msg}")

            now_iso = datetime.now(timezone.utc).isoformat()
            new_budget = budget_max if budget_max is not None else run.budget_max
            new_deadline = deadline_ts if deadline_ts is not None else run.deadline_ts
            new_intent_rev = intent_revision if intent_revision is not None else run.intent_revision

            conn.execute(
                """UPDATE runtime_runs
                   SET status = 'RUNNING',
                       budget_max = ?,
                       deadline_ts = ?,
                       error_type = NULL,
                       error_message = NULL,
                       terminal_at = NULL,
                       intent_revision = ?,
                       updated_at = ?
                   WHERE run_id = ?""",
                (new_budget, new_deadline, new_intent_rev, now_iso, run_id),
            )
            conn.commit()

            # Ensure ExperienceCollector has active run context
            if self.collector:
                from .collector import RunContext
                existing_ctx = self.collector._runs.get(run_id)
                prev_provs = list(existing_ctx.provenances) if existing_ctx else list(self._provenances.get(run_id, []))
                prev_sums = list(existing_ctx.action_summaries) if existing_ctx else []
                env = dict(existing_ctx.environment) if existing_ctx else {"purpose": run.purpose}
                if task_description:
                    env["task_description"] = task_description
                ctx = RunContext(
                    run_id=run_id,
                    task_id=run.task_id,
                    skill_name=run.skill_name or "",
                    skill_version=run.skill_version or "",
                    environment=env,
                    provenances=prev_provs,
                    action_summaries=prev_sums,
                    completed=False,
                )
                self.collector._runs[run_id] = ctx

            # If an episode was written for this run_id during a previous segment, remove it so the final episode can be saved
            if self.episode_store and self.episode_store.has_episode(f"ep_{run_id}"):
                ep_conn = self.episode_store._get_conn()
                ep_conn.execute("DELETE FROM episodes WHERE episode_id = ?", (f"ep_{run_id}",))
                ep_conn.commit()

            return self.get_run(run_id)  # type: ignore

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
        sanitized = sanitize_params(parameters)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock, conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Idempotency on call_id
            existing = conn.execute(
                """SELECT call_id, run_id, tool_name, status, input_params_json,
                          output_text, output_data_json, error_type, error_message,
                          latency_ms, created_at
                   FROM runtime_tool_calls WHERE call_id = ?""",
                (eff_call_id,),
            ).fetchone()
            if existing:
                ex_cid, ex_rid, ex_tname, ex_st, ex_pjson, ex_otext, ex_odjson, ex_err_type, ex_err_msg, ex_lat, ex_created = existing
                ex_params = json.loads(ex_pjson) if ex_pjson else {}

                # Verify run_id, tool_name, and sanitize(input_params) match
                if ex_rid != run_id or ex_tname != tool_name or ex_params != sanitized:
                    # Conflict: explicitly reject and do not expose or overwrite original record
                    return ToolCallRecord(
                        call_id=eff_call_id,
                        run_id=run_id,
                        tool_name=tool_name,
                        status="REJECTED",
                        input_params=sanitized,
                        error_type="IDEMPOTENCY_CONFLICT",
                        error_message=f"call_id '{eff_call_id}' already exists with conflicting parameters or run",
                        latency_ms=0.0,
                        created_at=now_iso,
                    )

                if ex_st == "ADMITTED":
                    # Same ADMITTED: in flight / unknown, cannot treat as success, cannot re-execute
                    return ToolCallRecord(
                        call_id=ex_cid,
                        run_id=ex_rid,
                        tool_name=ex_tname,
                        status="ADMITTED",
                        input_params=ex_params,
                        output_text="",
                        output_data={},
                        error_type="IN_FLIGHT",
                        error_message="Tool call is already admitted and currently in flight",
                        latency_ms=0.0,
                        created_at=ex_created,
                    )

                # Same completed call: return original result without re-executing or charging budget
                return ToolCallRecord(
                    call_id=ex_cid,
                    run_id=ex_rid,
                    tool_name=ex_tname,
                    status=ex_st,
                    input_params=ex_params,
                    output_text=ex_otext or "",
                    output_data=json.loads(ex_odjson) if ex_odjson else {},
                    error_type=ex_err_type,
                    error_message=ex_err_msg,
                    latency_ms=ex_lat or 0.0,
                    created_at=ex_created,
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

            # Reject if not RUNNING (PAUSED, INTERRUPTED, CANCELLED, COMPLETED, FAILED, TIMED_OUT, BUDGET_EXHAUSTED)
            if status != "RUNNING":
                err_type = (
                    status
                    if status in ("BUDGET_EXHAUSTED", "TIMED_OUT", "CANCELLED", "PAUSED", "INTERRUPTED")
                    else "DISPATCH_AFTER_TERMINAL"
                )
                rec = ToolCallRecord(
                    call_id=eff_call_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="REJECTED",
                    input_params=sanitized,
                    error_type=err_type,
                    error_message=f"Cannot dispatch tool while run is in status '{status}'",
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
                # Release SQLite's write lock before Collector writes via its connection.
                self._persist_tool_call(rec)
                if (
                    self.collector
                    and hasattr(self.collector, "_runs")
                    and run_id in self.collector._runs
                ):
                    self.collector.finish_run(
                        run_id=run_id,
                        infra_error="Task execution deadline exceeded",
                        verification_evidence={
                            "independent_pass": None,
                            "failure_reason": "Task execution deadline exceeded",
                        },
                    )
                return rec

            # Check budget: rejected attempts also consume request budget!
            if b_consumed >= b_max:
                conn.execute(
                    "UPDATE runtime_runs SET status = 'BUDGET_EXHAUSTED', updated_at = ? WHERE run_id = ?",
                    (now_iso, run_id),
                )
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

            # Pre-consume 1 budget ticket and insert ADMITTED record atomically
            new_consumed = b_consumed + 1
            conn.execute(
                "UPDATE runtime_runs SET budget_consumed = ?, updated_at = ? WHERE run_id = ?",
                (new_consumed, now_iso, run_id),
            )
            admitted_rec = ToolCallRecord(
                call_id=eff_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status="ADMITTED",
                input_params=sanitized,
                output_text="",
                output_data={},
                error_type=None,
                error_message=None,
                latency_ms=0.0,
                created_at=now_iso,
            )
            self._persist_tool_call(admitted_rec)

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

        with self._lock, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._persist_tool_call(call_rec)
            if call_rec.provenance:
                self._provenances.setdefault(run_id, []).append(call_rec.provenance)
                if self.collector:
                    collector_ctx = getattr(self.collector, "_runs", {}).get(run_id)
                    if collector_ctx and not getattr(collector_ctx, "completed", False):
                        self.collector.record_tool_call(
                            run_id=run_id,
                            provenance=call_rec.provenance,
                            action_summary=f"Tool {tool_name} dispatch: {call_rec.status}",
                        )

            # Check if budget was just reached (only update if still RUNNING)
            if new_consumed >= b_max:
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'BUDGET_EXHAUSTED', updated_at = ?
                       WHERE run_id = ? AND status = 'RUNNING'""",
                    (datetime.now(timezone.utc).isoformat(), run_id),
                )

        return call_rec

    dispatch_tool = execute_tool

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
        with self._lock:
            pi_client = self._active_pi_clients.get(run_id)
        if pi_client:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(getattr(pi_client, "abort", None)):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(pi_client.abort(run_id=run_id, reason=reason))
                    except RuntimeError:
                        asyncio.run(pi_client.abort(run_id=run_id, reason=reason))
                else:
                    pi_client.abort(run_id=run_id, reason=reason)
            except Exception:
                pass
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
                "INTERRUPTED",
            ):
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'CANCELLED', error_type = 'CANCELLED',
                           error_message = ?, updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (reason, now_iso, now_iso, run_id),
                )
                conn.commit()
                if (
                    self.collector
                    and hasattr(self.collector, "_runs")
                    and run_id in self.collector._runs
                ):
                    ctx = self.collector._runs[run_id]
                    if not getattr(ctx, "completed", False):
                        self.collector.finish_run(
                            run_id=run_id,
                            infra_error=f"CANCELLED: {reason}",
                            verification_evidence={
                                "independent_pass": None,
                                "failure_reason": f"CANCELLED: {reason}",
                            },
                        )

        return self.get_run(run_id)  # type: ignore

    def get_cancellation_report(self, run_id: str) -> dict[str, Any]:
        """Audit tool side-effects and execution status for a cancelled or stopped run.

        Honest accounting principle:
        - Executed tool calls are tracked and declared irreversible (no synthetic rollback claims).
        - Documents cancellation capability differences across asynchronous coroutines,
          sandboxed OS subprocesses, and synchronous Python callbacks.
        """
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"Run '{run_id}' not found")

        conn = self._get_conn()
        rows = conn.execute(
            """SELECT call_id, tool_name, status, input_params_json,
                      output_text, error_type, error_message, latency_ms, created_at
               FROM runtime_tool_calls
               WHERE run_id = ?
               ORDER BY created_at ASC""",
            (run_id,),
        ).fetchall()

        calls = []
        executed_count = 0
        rejected_count = 0
        cancelled_count = 0
        for r in rows:
            c_status = r[2]
            if c_status == "EXECUTED":
                executed_count += 1
            elif c_status == "REJECTED":
                rejected_count += 1
            elif c_status in ("CANCELLED", "TIMED_OUT"):
                cancelled_count += 1
            calls.append(
                {
                    "call_id": r[0],
                    "tool_name": r[1],
                    "status": c_status,
                    "error_type": r[5],
                    "error_message": r[6],
                    "latency_ms": r[7],
                    "created_at": r[8],
                }
            )

        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "run_status": run.status,
            "total_tool_calls": len(calls),
            "executed_tool_calls_count": executed_count,
            "rejected_tool_calls_count": rejected_count,
            "cancelled_tool_calls_count": cancelled_count,
            "tool_calls": calls,
            "side_effects_reversible": False,
            "reversibility_statement": (
                "Executed tool side effects are non-reversible; underlying system modifications "
                "persist and cannot be rolled back via intent or version transitions."
            ),
            "channel_cancellation_capabilities": {
                "async_coroutine": {
                    "mechanism": "asyncio.wait_for / Task.cancel",
                    "preemption": "Cooperative: halts execution at next await point or raises TimeoutError/CancelledError.",
                },
                "process_sandbox": {
                    "mechanism": "POSIX SIGTERM -> SIGKILL escalation via SandboxBackend",
                    "preemption": "Forcible: terminates process tree and isolates uncommitted workspace writes.",
                },
                "sync_python_callback": {
                    "mechanism": "State check prior to dispatch (DISPATCH_AFTER_TERMINAL)",
                    "preemption": "Non-preemptible mid-function; future tool dispatches are immediately rejected as CANCELLED.",
                },
            },
        }

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
            ) and run.terminal_at is not None:
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
                      error_type, error_message, created_at, updated_at, terminal_at,
                      frozen_body, intent_revision, task_spec_hash, candidate_id
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
            frozen_body=row[15] if len(row) > 15 else None,
            intent_revision=int(row[16]) if len(row) > 16 and row[16] is not None else 1,
            task_spec_hash=row[17] if len(row) > 17 else None,
            candidate_id=row[18] if len(row) > 18 else None,
        )

    def get_run_body(self, skill_name: Optional[str] = None, run_id: Optional[str] = None) -> str:
        """Fetch frozen body text for an execution run.

        Prioritizes frozen candidate snapshot in the current run, then falls back
        to deployment_manager or registry.
        """
        eff_run_id = run_id or (skill_name if skill_name and skill_name.startswith("run_") else None)
        with self._lock:
            if eff_run_id and eff_run_id in self._run_candidate_bodies:
                return self._run_candidate_bodies[eff_run_id]
            if skill_name and skill_name in self._run_candidate_bodies:
                return self._run_candidate_bodies[skill_name]

        # If not in memory cache, attempt to restore from persistent SQLite runtime_runs
        if eff_run_id:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT frozen_body, content_hash, skill_name, skill_version, candidate_id FROM runtime_runs WHERE run_id = ?",
                (eff_run_id,),
            ).fetchone()
            if row:
                f_body, c_hash, s_name, s_ver, cand_id = row
                if f_body is not None:
                    # Anti-tamper verification
                    if c_hash:
                        raw_hash = hashlib.sha256(f_body.encode("utf-8")).hexdigest()
                        strip_hash = hashlib.sha256(f_body.strip().encode("utf-8")).hexdigest()
                        valid_hashes = {raw_hash, strip_hash, raw_hash[:16], strip_hash[:16]}
                        if c_hash not in valid_hashes:
                            raise ValueError(
                                f"Integrity check failed for run_id='{eff_run_id}': "
                                f"persisted frozen_body hash mismatch (expected {c_hash}, got raw={raw_hash}, strip={strip_hash})"
                            )
                    with self._lock:
                        self._run_candidate_bodies[eff_run_id] = f_body
                    return f_body
                else:
                    # Legacy run without persisted frozen_body (is NULL)
                    # For unpromoted drafts or unpersisted draft runs, fail closed with diagnostic
                    target_skill = skill_name or s_name
                    is_draft = cand_id is not None or (s_ver and "draft" in s_ver.lower())
                    if is_draft or not self.registry or not target_skill or target_skill not in self.registry.list_names():
                        raise KeyError(
                            f"historical draft run body was not persisted in legacy DB for run_id='{eff_run_id}'; "
                            "cannot reconstruct without verified snapshot"
                        )
                    # If formal promoted skill in registry, fall back to deployment or registry
                    if self.deployment_manager:
                        return self.deployment_manager.get_run_body(target_skill, eff_run_id)
                    return self.registry.get_body(target_skill)

        if not skill_name and eff_run_id:
            run_rec = self.get_run(eff_run_id)
            if run_rec and run_rec.skill_name:
                skill_name = run_rec.skill_name
        if self.deployment_manager and eff_run_id and skill_name:
            return self.deployment_manager.get_run_body(skill_name, eff_run_id)
        if self.registry and skill_name:
            return self.registry.get_body(skill_name)
        raise KeyError(f"No body found for run_id={eff_run_id or run_id}, skill_name={skill_name}")

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

    def run_agent(
        self,
        run_id: str,
        input_text: str,
        llm: Any,
        candidate: Optional[Any] = None,
        tools: Optional[list[str]] = None,
        system_prompt_header: str = "",
        max_tool_iterations: int = 5,
    ) -> str:
        """Execute a SimpleAgent using frozen skill body snapshot and brokered tools.

        Invariants:
        - System prompt is built strictly from the runtime's frozen body snapshot (via get_run_body),
          so subsequent candidate mutations in memory cannot alter execution.
        - Tool dispatches are mediated by BrokeredTool, enforcing ToolBroker allowlists,
          budget consumption, deadline checks, and ExperienceCollector logging.
        """
        skill_name = candidate.skill_name if candidate else None
        run_rec = self.get_run(run_id)
        if not skill_name and run_rec and run_rec.skill_name:
            skill_name = run_rec.skill_name
        body = ""
        if candidate or (run_rec and run_rec.skill_name):
            try:
                body = self.get_run_body(skill_name=skill_name, run_id=run_id)
            except KeyError:
                body = ""
        system_prompt = f"{system_prompt_header}\n{body}".strip() if system_prompt_header else body

        from hello_agents import Config, SimpleAgent, ToolRegistry

        tool_registry = ToolRegistry()
        target_tools: list[str] = []
        if tools is not None:
            target_tools = list(tools)
        elif candidate and getattr(candidate, "meta", None) and getattr(candidate.meta, "dependencies", None):
            target_tools = list(candidate.meta.dependencies)
        elif self.tool_broker:
            target_tools = list(self.tool_broker._tools.keys())

        for tname in target_tools:
            brokered = self.create_brokered_tool(tname, run_id=run_id)
            tool_registry.register_tool(brokered)

        agent = SimpleAgent(
            name=f"agent_{run_id}",
            llm=llm,
            system_prompt=system_prompt,
            config=Config(
                trace_enabled=False,
                skills_enabled=False,
                session_enabled=False,
                subagent_enabled=False,
                todowrite_enabled=False,
                devlog_enabled=False,
            ),
            tool_registry=tool_registry,
            enable_tool_calling=len(target_tools) > 0,
            max_tool_iterations=max_tool_iterations,
        )
        return agent.run(input_text)

    def consume_turn_budget(self, run_id: str) -> tuple[bool, Optional[str], Optional[str]]:
        """Authoritatively check and consume 1 budget ticket for a model turn.

        Returns: (allowed, error_type, error_message)
        """
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT status, budget_max, budget_consumed, deadline_ts FROM runtime_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return False, "RUN_NOT_FOUND", f"Run '{run_id}' not found"

            status, b_max, b_consumed, dl_ts = row
            now_iso = datetime.now(timezone.utc).isoformat()

            if status != "RUNNING":
                return False, status, f"Run is not running ('{status}')"

            if dl_ts and time.time() > dl_ts:
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'TIMED_OUT', error_type = 'TIMEOUT', error_message = 'Deadline exceeded',
                           updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (now_iso, now_iso, run_id),
                )
                conn.commit()
                if self.collector and run_id in getattr(self.collector, "_runs", {}):
                    try:
                        self.collector.finish_run(
                            run_id=run_id,
                            model_output="",
                            verification_evidence={
                                "independent_pass": False,
                                "failure_reason": "Execution deadline exceeded in consume_turn_budget",
                            },
                            infra_error="Execution deadline exceeded",
                        )
                    except Exception:
                        pass
                return False, "TIMED_OUT", "Execution deadline exceeded"

            if b_consumed >= b_max:
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'BUDGET_EXHAUSTED', error_type = 'BUDGET_EXHAUSTED', error_message = 'Budget limit reached',
                           updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (now_iso, now_iso, run_id),
                )
                conn.commit()
                return False, "BUDGET_EXHAUSTED", f"Budget limit reached ({b_consumed}/{b_max})"

            new_consumed = b_consumed + 1
            conn.execute(
                "UPDATE runtime_runs SET budget_consumed = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (new_consumed, run_id),
            )
            if new_consumed >= b_max:
                conn.execute(
                    "UPDATE runtime_runs SET status = 'BUDGET_EXHAUSTED', updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                    (run_id,),
                )
            conn.commit()
            return True, None, None

    async def arun_agent_with_pi(
        self,
        run_id: str,
        input_text: str = "",
        candidate: Optional[Any] = None,
        tools: Optional[list[str]] = None,
        system_prompt_header: str = "",
        model_config: Optional[dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
        timeout: float = 30.0,
        context_compressor: Optional[Any] = None,
        hooks: Optional[Any] = None,
        initial_scratchpad: Optional[dict[str, Any]] = None,
        goal: Optional[str] = None,
        intent_version: Optional[str] = None,
        active_constraints: Optional[list[str]] = None,
        workspace_dir: Optional[Path] = None,
        checkpoint: Optional[PiCheckpointRecord] = None,
        model_turn_admission: Optional[Callable[[str, int], Any]] = None,
        model_turn_receipt: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> str:
        """Execute a Pi Agent SDK run using frozen skill body snapshot and brokered tools.

        Guarantees:
        - System prompt strictly uses get_run_body to prevent mutable draft leakage.
        - Tool execution strictly mediates through AgentRuntime.execute_tool -> ToolBroker.
        - Model turns and tool calls share authoritative runtime step/budget/deadline.
        - ContextCompressor integration through native Pi transformContext protocol.
        - Zero default tools (read/write/bash) enabled on Pi.
        - Subprocess lifecycle managed with graceful abort and reaping.
        - Terminal exceptions/aborts settle run and collector, preventing success Episode.
        """
        import asyncio
        import copy
        import sys
        from .pi_adapter import PiRunnerClient, tool_parameters_to_json_schema
        from .context_engine.schemas import ProtectedState, WorkSummary, MemoryIndex, AttachmentRef, VerifiedFact, FailedAttempt
        from .harness_loop import (
            AsyncHarnessLoop,
            HarnessHooks,
            HarnessState,
            HookDecision,
            ToolCallSpec,
            ModelResponse,
            normalize_model_usage,
        )

        if test_mode is None:
            is_test = bool(
                "pytest" in sys.modules
                or os.environ.get("SKILLFORGE_ENV") == "test"
                or (model_config and (model_config.get("is_test") or model_config.get("test_mode")))
            )
        else:
            is_test = bool(test_mode)

        if model_config is None:
            if checkpoint and checkpoint.model_config:
                model_config = dict(checkpoint.model_config)
            else:
                model_config = {"provider": "anthropic", "model_id": "claude-haiku-4-5"}

        provider = model_config.get("provider", "anthropic")
        if provider == "faux" and not is_test:
            raise ValueError("Faux provider is restricted to explicit test mode (test_mode=True)")
        if "responses" in model_config and not is_test:
            raise ValueError("Pre-written response scripts are restricted to explicit test mode (test_mode=True)")

        run_rec = self.get_run(run_id)

        if checkpoint:
            if tools is not None:
                raise ValueError("Cannot override tools when resuming from checkpoint")
            if candidate is not None:
                raise ValueError("Cannot override candidate when resuming from checkpoint")
            if goal is not None:
                raise ValueError("Cannot override goal when resuming from checkpoint")
            if intent_version is not None:
                raise ValueError("Cannot override intent_version when resuming from checkpoint")
            if initial_scratchpad is not None:
                raise ValueError("Cannot override initial_scratchpad when resuming from checkpoint")
            if active_constraints is not None:
                raise ValueError("Cannot override active_constraints when resuming from checkpoint")
            if bool(system_prompt_header):
                raise ValueError("Cannot override system_prompt_header when resuming from checkpoint")

            if not run_rec:
                raise KeyError(f"Run '{run_id}' not found")

            TERMINAL_STATUSES = {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
                "BUDGET_EXHAUSTED",
                "INTERRUPTED",
            }
            if run_rec.status in TERMINAL_STATUSES:
                raise ValueError(f"Cannot resume run '{run_id}' in terminal status '{run_rec.status}'")
            if run_rec.status != "PAUSED":
                raise ValueError(f"Cannot resume run '{run_id}' in non-PAUSED status '{run_rec.status}'")

            # Check unsettled ADMITTED tool calls
            conn = self._get_conn()
            admitted = conn.execute(
                "SELECT call_id FROM runtime_tool_calls WHERE run_id = ? AND status = 'ADMITTED' ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            if admitted:
                admitted_ids = [r[0] for r in admitted]
                now_iso = datetime.now(timezone.utc).isoformat()
                msg = f"Unsettled in-flight tool calls requiring review: {', '.join(admitted_ids)}"
                with self._lock:
                    conn.execute(
                        """UPDATE runtime_runs
                           SET status = 'INTERRUPTED', error_type = 'REVIEW_REQUIRED',
                               error_message = ?, updated_at = ?, terminal_at = ?
                           WHERE run_id = ?""",
                        (msg, now_iso, now_iso, run_id),
                    )
                    conn.commit()
                    if (
                        self.collector
                        and hasattr(self.collector, "_runs")
                        and run_id in self.collector._runs
                    ):
                        try:
                            self.collector.finish_run(
                                run_id=run_id,
                                infra_error=msg,
                                verification_evidence={
                                    "independent_pass": None,
                                    "failure_reason": f"REVIEW_REQUIRED: {msg}",
                                },
                            )
                        except Exception:
                            pass
                raise ValueError(f"REVIEW_REQUIRED: {msg}")

            canonical_checkpoint = self.get_pi_checkpoint(run_id)
            if not canonical_checkpoint:
                raise ValueError(f"INVALID_CHECKPOINT: No canonical checkpoint found for run '{run_id}'")

            if (
                checkpoint.checkpoint_id != canonical_checkpoint.checkpoint_id
                or checkpoint.run_id != canonical_checkpoint.run_id
                or checkpoint.format_version != canonical_checkpoint.format_version
                or checkpoint.messages != canonical_checkpoint.messages
                or checkpoint.frozen_body != canonical_checkpoint.frozen_body
                or checkpoint.working_state != canonical_checkpoint.working_state
                or checkpoint.system_prompt != canonical_checkpoint.system_prompt
                or checkpoint.epoch != canonical_checkpoint.epoch
                or checkpoint.intent_version != canonical_checkpoint.intent_version
                or checkpoint.tools != canonical_checkpoint.tools
            ):
                raise ValueError(
                    f"INVALID_CHECKPOINT: Caller-provided checkpoint differs from canonical store checkpoint for run '{run_id}'"
                )

            checkpoint = canonical_checkpoint
            working_state = checkpoint.working_state or {}
            binding = working_state.get("recovery_binding")
            if not binding or not isinstance(binding, dict):
                raise ValueError(
                    f"STALE_CHECKPOINT: Checkpoint '{checkpoint.checkpoint_id}' missing valid recovery_binding (unsupported legacy or unverified checkpoint)"
                )

            # 1. Compare against canonical RunRecord
            if binding.get("run_id") != run_rec.run_id:
                raise ValueError(f"STALE_CHECKPOINT: run_id drift: expected {binding.get('run_id')}, got {run_rec.run_id}")
            if binding.get("task_id") != run_rec.task_id:
                raise ValueError(f"STALE_CHECKPOINT: task_id drift: expected {binding.get('task_id')}, got {run_rec.task_id}")
            if binding.get("intent_revision") != run_rec.intent_revision:
                raise ValueError(f"STALE_CHECKPOINT: intent_revision drift in RunRecord: expected {binding.get('intent_revision')}, got {run_rec.intent_revision}")
            if (binding.get("task_spec_hash") or "") != (run_rec.task_spec_hash or ""):
                raise ValueError(f"STALE_CHECKPOINT: task_spec_hash drift in RunRecord: expected {binding.get('task_spec_hash')}, got {run_rec.task_spec_hash}")
            if (binding.get("content_hash") or "") != (run_rec.content_hash or ""):
                raise ValueError(f"STALE_CHECKPOINT: content_hash drift in RunRecord: expected {binding.get('content_hash')}, got {run_rec.content_hash}")

            cur_body_sha256 = hashlib.sha256((run_rec.frozen_body or "").encode("utf-8")).hexdigest()
            if binding.get("body_sha256") != cur_body_sha256:
                raise ValueError(f"STALE_CHECKPOINT: frozen_body sha256 drift in RunRecord: expected {binding.get('body_sha256')}, got {cur_body_sha256}")

            # 2. Compare model_config whitelist (provider, model_id, model, temperature, max_tokens)
            bound_mc = binding.get("model_config") or {}
            for k in ("provider", "model_id", "model", "temperature", "max_tokens"):
                if k in bound_mc:
                    expected_v = bound_mc[k]
                    actual_v = model_config.get(k) if isinstance(model_config, dict) else None
                    if actual_v != expected_v:
                        raise ValueError(f"STALE_CHECKPOINT: model_config drift for '{k}': expected {expected_v}, got {actual_v}")
                elif isinstance(model_config, dict) and k in model_config and model_config[k] is not None:
                    raise ValueError(f"STALE_CHECKPOINT: model_config introduced parameter '{k}' not present in bound checkpoint")

            # 3. Compare ToolBroker allowlist & tool schemas
            if self.tool_broker:
                curr_allowlist = (
                    sorted(list(self.tool_broker.application_allowlist))
                    if self.tool_broker.application_allowlist is not None
                    else None
                )
                if binding.get("application_allowlist") != curr_allowlist:
                    raise ValueError(
                        f"STALE_CHECKPOINT: ToolBroker application_allowlist drift: expected {binding.get('application_allowlist')}, got {curr_allowlist}"
                    )

                bound_tools = binding.get("tools") or []
                bound_schemas = binding.get("tool_schemas") or {}
                for tname in bound_tools:
                    if self.tool_broker.get_tool(tname) is None:
                        raise ValueError(f"STALE_CHECKPOINT: Tool '{tname}' missing from ToolBroker")
                    curr_params = self.tool_broker.get_parameters(tname)
                    curr_schema = tool_parameters_to_json_schema(curr_params)
                    if tname in bound_schemas and curr_schema != bound_schemas[tname]:
                        raise ValueError(f"STALE_CHECKPOINT: Tool parameter schema drift for tool '{tname}'")

            # 4. Compare bound artifact validator if present
            bound_val = binding.get("artifact_validator")
            if bound_val is not None:
                curr_val = self._run_artifact_validators.get(run_id)
                if not curr_val:
                    raise ValueError("STALE_CHECKPOINT: Missing required artifact_validator")
                c_id = getattr(curr_val, "validator_id", getattr(curr_val, "id", str(type(curr_val).__name__)))
                c_ver = getattr(curr_val, "validator_version", getattr(curr_val, "version", "1.0.0"))
                c_hash = getattr(curr_val, "config_hash", getattr(curr_val, "hash", ""))
                if (
                    c_id != bound_val.get("id")
                    or c_ver != bound_val.get("version")
                    or c_hash != bound_val.get("config_hash")
                ):
                    raise ValueError("STALE_CHECKPOINT: Artifact validator drift detected")

            # 5. Compare TaskContext from CandidateStore if present
            if binding.get("task_context") is not None and self.candidate_store is None:
                raise ValueError("STALE_CHECKPOINT: Missing required canonical TaskContext store")
            if self.candidate_store and hasattr(self.candidate_store, "get_task_context"):
                tc = self.candidate_store.get_task_context(run_rec.task_id)
                bound_ctx = binding.get("task_context")
                if bound_ctx is not None:
                    if not tc:
                        raise ValueError(f"STALE_CHECKPOINT: Canonical TaskContext missing for task '{run_rec.task_id}'")
                    if tc.intent_revision != bound_ctx.get("intent_revision"):
                        raise ValueError(
                            f"STALE_CHECKPOINT: TaskContext intent_revision drift: expected {bound_ctx.get('intent_revision')}, got {tc.intent_revision}"
                        )
                    curr_fp = tc.compute_fingerprint()
                    if curr_fp != bound_ctx.get("contract_fingerprint"):
                        raise ValueError(
                            f"STALE_CHECKPOINT: TaskContext contract_fingerprint drift: expected {bound_ctx.get('contract_fingerprint')}, got {curr_fp}"
                        )
                    if sorted(list(tc.constraints)) != bound_ctx.get("constraints"):
                        raise ValueError("STALE_CHECKPOINT: TaskContext constraints drift")
                    if tc.acceptance_criteria != bound_ctx.get("acceptance_criteria"):
                        raise ValueError("STALE_CHECKPOINT: TaskContext acceptance_criteria drift")

            # Pre-execution deadline check
            if run_rec.deadline_ts and time.time() > run_rec.deadline_ts:
                with self._lock:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn = self._get_conn()
                    conn.execute(
                        """UPDATE runtime_runs
                           SET status = 'TIMED_OUT', error_type = 'TIMEOUT', error_message = 'Deadline exceeded',
                               updated_at = ?, terminal_at = ?
                           WHERE run_id = ? AND terminal_at IS NULL""",
                        (now_iso, now_iso, run_id),
                    )
                    conn.commit()
                    if self.collector and run_id in getattr(self.collector, "_runs", {}):
                        try:
                            self.collector.finish_run(
                                run_id=run_id,
                                model_output="",
                                verification_evidence={
                                    "independent_pass": False,
                                    "failure_reason": "Execution deadline exceeded before run started",
                                },
                                infra_error="Execution deadline exceeded",
                            )
                        except Exception:
                            pass
                raise TimeoutError("Execution deadline exceeded before run started")

            # CAS update from PAUSED to RUNNING without altering budget_max or deadline_ts
            now_iso = datetime.now(timezone.utc).isoformat()
            conn = self._get_conn()
            with self._lock:
                cur = conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'RUNNING', updated_at = ?
                       WHERE run_id = ? AND status = 'PAUSED' AND terminal_at IS NULL""",
                    (now_iso, run_id),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"Cannot resume run '{run_id}': CAS update failed, run is not in PAUSED state")
                conn.commit()
            run_rec = self.get_run(run_id)

            working_state = checkpoint.working_state or {}
            body = checkpoint.frozen_body or (run_rec.frozen_body if run_rec else "")
            self._run_candidate_bodies[run_id] = body
            base_system_prompt = working_state.get("base_system_prompt") or body
            system_prompt = base_system_prompt

            target_tools: list[str] = list(checkpoint.tools or [])
            self._skill_required_tools[run_id] = set(target_tools)

            eff_goal = working_state.get("goal") or (run_rec.task_id if run_rec else input_text)
            eff_intent = checkpoint.intent_version or working_state.get("intent_version") or "v1"
            curr_epoch = checkpoint.epoch or working_state.get("context_epoch", 1)
            curr_step = working_state.get("step_count", 0)

            raw_scratch = working_state.get("working_scratchpad", {})
            scratchpad: dict[str, Any] = {}
            if "protected_state" in raw_scratch:
                ps_data = raw_scratch["protected_state"]
                if isinstance(ps_data, ProtectedState):
                    scratchpad["protected_state"] = ps_data
                elif isinstance(ps_data, dict):
                    scratchpad["protected_state"] = ProtectedState(
                        intent_version=str(ps_data.get("intent_version", eff_intent)),
                        current_goal=str(ps_data.get("current_goal", eff_goal)),
                        active_constraints=list(ps_data.get("active_constraints", [])),
                        pending_approvals=list(ps_data.get("pending_approvals", [])),
                        allowed_tools=list(ps_data.get("allowed_tools", target_tools)),
                    )
            else:
                scratchpad["protected_state"] = ProtectedState(
                    intent_version=eff_intent,
                    current_goal=eff_goal,
                    active_constraints=list(active_constraints or []),
                    allowed_tools=list(target_tools),
                )

            if "work_summary" in raw_scratch:
                ws_d = raw_scratch["work_summary"]
                if isinstance(ws_d, WorkSummary):
                    scratchpad["work_summary"] = ws_d
                elif isinstance(ws_d, dict):
                    facts = [
                        f if isinstance(f, VerifiedFact) else VerifiedFact(
                            fact=str(f.get("fact", "")),
                            evidence_ref=str(f.get("evidence_ref", "")),
                            timestamp=str(f.get("timestamp", "")),
                            confidence=f.get("confidence", "HIGH"),
                        )
                        for f in ws_d.get("verified_facts", [])
                    ]
                    attempts = [
                        a if isinstance(a, FailedAttempt) else FailedAttempt(
                            action=str(a.get("action", "")),
                            reason=str(a.get("reason", "")),
                            resolution=str(a.get("resolution", "")),
                        )
                        for a in ws_d.get("failed_attempts", [])
                    ]
                    scratchpad["work_summary"] = WorkSummary(
                        verified_facts=facts,
                        failed_attempts=attempts,
                        open_questions=list(ws_d.get("open_questions", [])),
                        structured_entities=dict(ws_d.get("structured_entities", {})),
                    )
            else:
                scratchpad["work_summary"] = WorkSummary()

            if "memory_index" in raw_scratch:
                mi_d = raw_scratch["memory_index"]
                if isinstance(mi_d, MemoryIndex):
                    scratchpad["memory_index"] = mi_d
                elif isinstance(mi_d, dict):
                    atts = [
                        a if isinstance(a, AttachmentRef) else AttachmentRef(
                            ref_id=str(a.get("ref_id", "")),
                            description=str(a.get("description", "")),
                            total_items=int(a.get("total_items", 0)),
                            expanded_items=int(a.get("expanded_items", 0)),
                            item_keys=list(a.get("item_keys", [])),
                            access_method=str(a.get("access_method", "")),
                        )
                        for a in mi_d.get("attachments", [])
                    ]
                    scratchpad["memory_index"] = MemoryIndex(
                        attachments=atts,
                        object_coverage=dict(mi_d.get("object_coverage", {})),
                    )
            else:
                scratchpad["memory_index"] = MemoryIndex()

            for k, v in raw_scratch.items():
                if k not in scratchpad:
                    scratchpad[k] = v

            harness_state = HarnessState(
                run_id=run_id,
                task_id=run_rec.task_id if run_rec else "task_default",
                intent_version=eff_intent,
                goal=eff_goal,
                system_prompt=checkpoint.system_prompt or system_prompt,
                messages=list(checkpoint.messages),
                step_count=curr_step,
                budget_max=run_rec.budget_max if run_rec else 10,
                working_scratchpad=scratchpad,
                workspace_dir=Path(workspace_dir) if workspace_dir else Path.cwd(),
                deadline_ts=run_rec.deadline_ts if run_rec else None,
            )
            setattr(harness_state, "context_epoch", curr_epoch)

            def _rebuild_provenance(d: Any) -> Optional[ToolCallProvenance]:
                if isinstance(d, ToolCallProvenance):
                    return d
                if not isinstance(d, dict):
                    return None
                try:
                    return ToolCallProvenance(
                        tool_name=str(d.get("tool_name", "")),
                        fixture_case_id=str(d.get("fixture_case_id", "")),
                        call_index=int(d.get("call_index", 0)),
                        call_count=int(d.get("call_count", 0)),
                        is_fixture=bool(d.get("is_fixture", False)),
                        tool_required=bool(d.get("tool_required", False)),
                        tool_called=bool(d.get("tool_called", False)),
                        tool_success=bool(d.get("tool_success", False)),
                        authenticity_pass=bool(d.get("authenticity_pass", False)),
                        input_params=dict(d.get("input_params", {})),
                        output_status=d.get("output_status", "SUCCESS"),
                        output_summary=str(d.get("output_summary", "")),
                        latency_ms=float(d.get("latency_ms", 0.0)),
                        timestamp=str(d.get("timestamp", "")),
                        signature=str(d.get("signature", "")),
                        snapshot_id=str(d.get("snapshot_id", "")),
                        snapshot_content=str(d.get("snapshot_content", "")),
                    )
                except Exception:
                    return None

            raw_rt_provs = working_state.get("runtime_provenances", [])
            rebuilt_rt_provs = [p for p in (_rebuild_provenance(x) for x in raw_rt_provs) if p is not None]
            with self._lock:
                self._provenances[run_id] = rebuilt_rt_provs

            if self.collector:
                from .collector import RunContext
                raw_c_provs = working_state.get("collector_provenances", [])
                rebuilt_c_provs = [p for p in (_rebuild_provenance(x) for x in raw_c_provs) if p is not None]
                if not rebuilt_c_provs and rebuilt_rt_provs:
                    rebuilt_c_provs = list(rebuilt_rt_provs)
                c_env = working_state.get("collector_environment") or {"purpose": run_rec.purpose}
                c_sums = working_state.get("collector_action_summaries") or []
                self.collector._runs[run_id] = RunContext(
                    run_id=run_id,
                    task_id=run_rec.task_id,
                    skill_name=run_rec.skill_name or "",
                    skill_version=run_rec.skill_version or "",
                    environment=dict(c_env),
                    provenances=list(rebuilt_c_provs),
                    action_summaries=list(c_sums),
                    completed=False,
                )
        else:
            # Pre-execution deadline check
            if run_rec and run_rec.deadline_ts and time.time() > run_rec.deadline_ts:
                with self._lock:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn = self._get_conn()
                    conn.execute(
                        """UPDATE runtime_runs
                           SET status = 'TIMED_OUT', error_type = 'TIMEOUT', error_message = 'Deadline exceeded',
                               updated_at = ?, terminal_at = ?
                           WHERE run_id = ? AND terminal_at IS NULL""",
                        (now_iso, now_iso, run_id),
                    )
                    conn.commit()
                    if self.collector and run_id in getattr(self.collector, "_runs", {}):
                        try:
                            self.collector.finish_run(
                                run_id=run_id,
                                model_output="",
                                verification_evidence={
                                    "independent_pass": False,
                                    "failure_reason": "Execution deadline exceeded before run started",
                                },
                                infra_error="Execution deadline exceeded",
                            )
                        except Exception:
                            pass
                raise TimeoutError("Execution deadline exceeded before run started")

            skill_name = candidate.skill_name if candidate else None
            if not skill_name and run_rec and run_rec.skill_name:
                skill_name = run_rec.skill_name
            body = ""
            if candidate or (run_rec and run_rec.skill_name):
                try:
                    body = self.get_run_body(skill_name=skill_name, run_id=run_id)
                except KeyError:
                    body = ""
            base_system_prompt = f"{system_prompt_header}\n{body}".strip() if system_prompt_header else body
            system_prompt = base_system_prompt

            target_tools = []
            if tools is not None:
                target_tools = list(tools)
            elif candidate and getattr(candidate, "meta", None) and getattr(candidate.meta, "dependencies", None):
                target_tools = list(candidate.meta.dependencies)
            elif self.tool_broker:
                target_tools = list(self.tool_broker._tools.keys())

            # Authoritative goal, intent, constraints from runtime snapshot
            eff_goal = goal or (run_rec.task_id if run_rec else input_text)
            eff_intent = intent_version or (run_rec.intent_revision if run_rec and run_rec.intent_revision else "v1")
            eff_constraints = list(active_constraints or [])

            scratchpad = dict(initial_scratchpad or {})
            if "protected_state" not in scratchpad:
                scratchpad["protected_state"] = ProtectedState(
                    intent_version=eff_intent,
                    current_goal=eff_goal,
                    active_constraints=eff_constraints,
                    allowed_tools=list(target_tools),
                )
            if "work_summary" not in scratchpad:
                scratchpad["work_summary"] = WorkSummary()
            if "memory_index" not in scratchpad:
                scratchpad["memory_index"] = MemoryIndex()

            harness_state = HarnessState(
                run_id=run_id,
                task_id=run_rec.task_id if run_rec else "task_default",
                intent_version=eff_intent,
                goal=eff_goal,
                system_prompt=system_prompt,
                messages=[],
                step_count=0,
                budget_max=run_rec.budget_max if run_rec else 10,
                working_scratchpad=scratchpad,
                workspace_dir=Path(workspace_dir) if workspace_dir else Path.cwd(),
                deadline_ts=run_rec.deadline_ts if run_rec else None,
            )
        with self._lock:
            self._run_harness_states[run_id] = harness_state

        bound_run_rec = run_rec
        bound_task_id = run_rec.task_id if run_rec else ""
        bound_intent_revision = run_rec.intent_revision if run_rec and run_rec.intent_revision is not None else 1
        bound_task_spec_hash = run_rec.task_spec_hash or "" if run_rec else ""
        bound_content_hash = run_rec.content_hash or "" if run_rec else ""
        bound_f_body = run_rec.frozen_body if (run_rec and run_rec.frozen_body is not None) else body
        bound_body_sha256 = hashlib.sha256((bound_f_body or "").encode("utf-8")).hexdigest()

        bound_requires_store = False
        bound_tc_present = False
        bound_tc_revision = None
        bound_tc_fingerprint = None

        if checkpoint and checkpoint.working_state:
            rec_b = checkpoint.working_state.get("recovery_binding") or {}
            tc_b = rec_b.get("task_context")
            if tc_b:
                bound_requires_store = True
                bound_tc_present = True
                bound_tc_revision = tc_b.get("intent_revision")
                bound_tc_fingerprint = tc_b.get("contract_fingerprint")
        elif self.candidate_store and hasattr(self.candidate_store, "get_task_context") and bound_task_id:
            bound_requires_store = True
            init_tc = self.candidate_store.get_task_context(bound_task_id)
            if init_tc:
                bound_tc_present = True
                bound_tc_revision = init_tc.intent_revision
                bound_tc_fingerprint = init_tc.compute_fingerprint()

        # Construct explicit tool list for Pi
        pi_tools = []
        for tname in target_tools:
            tool_obj = self.tool_broker.get_tool(tname)
            desc = ""
            if hasattr(tool_obj, "description"):
                desc = getattr(tool_obj, "description", "")
            elif isinstance(tool_obj, dict):
                desc = tool_obj.get("description", "")
            params = self.tool_broker.get_parameters(tname)
            schema = tool_parameters_to_json_schema(params)
            pi_tools.append({
                "name": tname,
                "description": desc,
                "parameters": schema,
            })

        client = PiRunnerClient()
        await client.start()

        with self._lock:
            self._active_pi_clients[run_id] = client
            if client.pid:
                self._last_pi_pids[run_id] = client.pid

        async def pause_for_hook_review(r_id: str, message: str) -> dict[str, Any]:
            harness_state.status = "PAUSED_FOR_REVIEW"
            harness_state.error_message = message
            conn = self._get_conn()
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE runtime_runs SET status='INTERRUPTED', error_type='REVIEW_REQUIRED',
                   error_message=?, updated_at=?, terminal_at=?
                   WHERE run_id=? AND terminal_at IS NULL""",
                (message, now_iso, now_iso, r_id),
            )
            conn.commit()
            # Stop queued tool calls, not just the current observation.
            await client.abort(run_id=r_id, reason=message)
            return {"status": "REJECTED", "error_type": "REVIEW_REQUIRED", "output": message}

        try:
            async def handle_tool_call(r_id: str, t_name: str, params: dict[str, Any], c_id: str) -> dict[str, Any]:
                tc = ToolCallSpec(id=c_id, name=t_name, arguments=params)

                # Hook: pre_tool_exec
                if hooks and hasattr(hooks, "pre_tool_exec") and hooks.pre_tool_exec:
                    decision = await AsyncHarnessLoop._execute_hook(
                        hooks.pre_tool_exec,
                        "pre_tool_exec",
                        harness_state,
                        tc,
                        default_critical=True,
                    )
                    if decision.action == "deny":
                        allowed, err_type, err_msg = self.consume_turn_budget(r_id)
                        if not allowed:
                            raise RuntimeError(f"{err_type}: {err_msg}")
                        # Hook details stay in audit, not in model-visible observations.
                        err_msg = f"ERROR [PERMISSION_DENIED]: Tool '{tc.name}' denied by business constraint hook ({decision.reason_code})"
                        now_iso = datetime.now(timezone.utc).isoformat()
                        rec = ToolCallRecord(
                            call_id=c_id,
                            run_id=r_id,
                            tool_name=tc.name,
                            status="REJECTED",
                            input_params=tc.arguments,
                            output_text=err_msg,
                            error_type="HOOK_DENIED",
                            error_message=decision.reason_code or "Denied by business constraint hook",
                            latency_ms=0.0,
                            created_at=now_iso,
                        )
                        harness_state.tool_records.append(rec)
                        self._persist_tool_call(rec)

                        # Hook: post_tool_exec (observation)
                        if hooks and hasattr(hooks, "post_tool_exec") and hooks.post_tool_exec:
                            await AsyncHarnessLoop._execute_hook(
                                hooks.post_tool_exec,
                                "post_tool_exec",
                                harness_state,
                                rec,
                                default_critical=False,
                            )

                        return {
                            "status": "REJECTED",
                            "error_type": "HOOK_DENIED",
                            "error_message": err_msg,
                            "output": err_msg,
                        }

                    elif decision.action == "review":
                        return await pause_for_hook_review(
                            r_id, f"Pre-tool review ({decision.reason_code}): {tc.name}"
                        )

                call_rec = self.execute_tool(
                    run_id=r_id,
                    tool_name=t_name,
                    parameters=params,
                    call_id=c_id,
                )
                harness_state.tool_records.append(call_rec)

                output_str = call_rec.output_text or ""
                output_data = call_rec.output_data or {}

                # Hook: post_tool_exec
                if hooks and hasattr(hooks, "post_tool_exec") and hooks.post_tool_exec:
                    post_dec = await AsyncHarnessLoop._execute_hook(
                        hooks.post_tool_exec,
                        "post_tool_exec",
                        harness_state,
                        call_rec,
                        default_critical=False,
                    )
                    if isinstance(post_dec, HookDecision):
                        if post_dec.action == "deny":
                            output_str = f"ERROR [RESULT_DENIED]: Post-tool hook denied observation ({post_dec.reason_code})"
                            output_data = {}
                            return {
                                "status": "COMPLETED",
                                "output": output_str,
                                "data": output_data,
                            }
                        elif post_dec.action == "review":
                            return await pause_for_hook_review(
                                r_id, f"Post-tool review ({post_dec.reason_code}): {t_name}"
                            )
                        elif post_dec.observation_view is not None:
                            output_str = post_dec.observation_view
                            output_data = {}
                            try:
                                parsed = json.loads(post_dec.observation_view)
                                if isinstance(parsed, dict):
                                    output_data = parsed
                            except Exception:
                                pass

                if call_rec.status in ("COMPLETED", "EXECUTED"):
                    return {
                        "status": "COMPLETED",
                        "output": output_str,
                        "data": output_data,
                    }
                else:
                    return {
                        "status": call_rec.status,
                        "error_type": call_rec.error_type or "TOOL_ERROR",
                        "error_message": call_rec.error_message or output_str or "Tool execution failed",
                        "output": output_str,
                    }

            async def handle_turn_request(r_id: str, t_idx: int, p_input: Optional[dict[str, Any]] = None) -> dict[str, Any]:
                allowed, err_type, err_msg = self.consume_turn_budget(r_id)
                if not allowed:
                    return {
                        "allowed": False,
                        "error_type": err_type,
                        "error": err_msg,
                    }
                # Hook: pre_model_call
                if hooks and hasattr(hooks, "pre_model_call") and hooks.pre_model_call:
                    harness_state.step_count = t_idx
                    m_dec = await AsyncHarnessLoop._execute_hook(
                        hooks.pre_model_call,
                        "pre_model_call",
                        harness_state,
                        default_critical=False,
                    )
                    if isinstance(m_dec, HookDecision):
                        if m_dec.action == "deny":
                            harness_state.status = "FAILED"
                            harness_state.error_message = f"Pre-model hook denied execution: {m_dec.reason_code}"
                            return {
                                "allowed": False,
                                "error_type": "HOOK_DENIED",
                                "error": harness_state.error_message,
                            }
                        elif m_dec.action == "review":
                            harness_state.status = "PAUSED_FOR_REVIEW"
                            harness_state.error_message = f"Pre-model hook paused for review: {m_dec.reason_code}"
                            return {
                                "allowed": False,
                                "error_type": "PAUSED_FOR_REVIEW",
                                "error": harness_state.error_message,
                            }

                if model_turn_admission is not None:
                    try:
                        import inspect
                        res = model_turn_admission(r_id, t_idx)
                        if inspect.isawaitable(res):
                            res = await res
                        if isinstance(res, dict) and not res.get("allowed", True):
                            return {
                                "allowed": False,
                                "error_type": res.get("error_type", "BUDGET_EXCEEDED"),
                                "error": res.get("error", "Model turn admission denied"),
                            }
                    except Exception as e:
                        err_cap = getattr(e, "cap_type", None)
                        err_name = "BUDGET_EXCEEDED" if ("budget" in type(e).__name__.lower() or "budget" in str(e).lower() or err_cap) else "ADMISSION_ERROR"
                        return {
                            "allowed": False,
                            "error_type": err_cap or err_name,
                            "error": str(e),
                        }

                return {
                    "allowed": True,
                    "error_type": None,
                    "error": None,
                }

            async def handle_turn_receipt(frame: dict[str, Any]) -> None:
                # 1. 4A2a model_turn_receipt for shared ledger
                if model_turn_receipt is not None:
                    import inspect
                    res = model_turn_receipt(frame)
                    if inspect.isawaitable(res):
                        await res

                # 2. Hook: post_model_call
                if hooks and hasattr(hooks, "post_model_call") and hooks.post_model_call:
                    raw_usage = frame.get("usage")
                    eff_usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
                    mapped_usage = dict(eff_usage)
                    if "input" in mapped_usage and "input_tokens" not in mapped_usage:
                        mapped_usage["input_tokens"] = mapped_usage["input"]
                    if "output" in mapped_usage and "output_tokens" not in mapped_usage:
                        mapped_usage["output_tokens"] = mapped_usage["output"]
                    if "totalTokens" in mapped_usage and "total_tokens" not in mapped_usage:
                        mapped_usage["total_tokens"] = mapped_usage["totalTokens"]

                    struct_resp = ModelResponse(
                        content="",
                        tool_calls=[],
                        usage=eff_usage,
                        normalized_usage=normalize_model_usage(mapped_usage),
                    )
                    if "stop_reason" in frame:
                        setattr(struct_resp, "stop_reason", frame["stop_reason"])
                    await AsyncHarnessLoop._execute_hook(
                        hooks.post_model_call,
                        "post_model_call",
                        harness_state,
                        struct_resp,
                        default_critical=False,
                    )

            async def handle_context_transform(
                r_id: str,
                req_id: str,
                intent_ver: str,
                t_idx: int,
                raw_messages: list[dict[str, Any]],
            ) -> dict[str, Any]:
                auth_protected = harness_state.working_scratchpad.get("protected_state")
                if auth_protected and intent_ver != auth_protected.intent_version:
                    return {
                        "allowed": False,
                        "error_type": "STALE_INTENT",
                        "error": f"Intent mismatch: authoritative={auth_protected.intent_version}, requested={intent_ver}",
                    }

                def check_canonical_integrity() -> dict[str, Any]:
                    live_run = self.get_run(r_id)
                    if not live_run:
                        return {"allowed": False, "error_type": "RUN_NOT_FOUND", "error": f"Run '{r_id}' not found"}
                    if live_run.status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "BUDGET_EXHAUSTED"):
                        return {"allowed": False, "error_type": live_run.status, "error": f"Run is already terminal ({live_run.status})"}
                    if live_run.deadline_ts and time.time() > live_run.deadline_ts:
                        self.consume_turn_budget(r_id)
                        return {"allowed": False, "error_type": "TIMED_OUT", "error": "Execution deadline exceeded"}

                    if bound_run_rec:
                        if live_run.intent_revision != bound_intent_revision:
                            return {
                                "allowed": False,
                                "error_type": "STALE_INTENT",
                                "error": f"RunRecord intent_revision drifted: expected {bound_intent_revision}, got {live_run.intent_revision}",
                            }
                        if (live_run.task_spec_hash or "") != bound_task_spec_hash:
                            return {
                                "allowed": False,
                                "error_type": "STALE_CHECKPOINT",
                                "error": f"RunRecord task_spec_hash drifted: expected {bound_task_spec_hash}, got {live_run.task_spec_hash}",
                            }
                        if (live_run.content_hash or "") != bound_content_hash:
                            return {
                                "allowed": False,
                                "error_type": "STALE_CHECKPOINT",
                                "error": f"RunRecord content_hash drifted: expected {bound_content_hash}, got {live_run.content_hash}",
                            }
                        cur_f_body = live_run.frozen_body or ""
                        if not cur_f_body:
                            try:
                                cur_f_body = self.get_run_body(run_id=r_id)
                            except Exception:
                                cur_f_body = ""
                        cur_body_sha256 = hashlib.sha256((cur_f_body or "").encode("utf-8")).hexdigest()
                        if cur_body_sha256 != bound_body_sha256:
                            return {
                                "allowed": False,
                                "error_type": "STALE_CHECKPOINT",
                                "error": "RunRecord frozen_body drifted",
                            }

                    if bound_requires_store:
                        if self.candidate_store is None or not hasattr(self.candidate_store, "get_task_context"):
                            return {
                                "allowed": False,
                                "error_type": "STALE_CHECKPOINT",
                                "error": "Missing required canonical TaskContext store",
                            }

                    if bound_tc_present:
                        if self.candidate_store is None or not hasattr(self.candidate_store, "get_task_context"):
                            return {
                                "allowed": False,
                                "error_type": "STALE_CHECKPOINT",
                                "error": "Missing required canonical TaskContext store",
                            }
                        live_tc = self.candidate_store.get_task_context(live_run.task_id)
                        if not live_tc:
                            return {
                                "allowed": False,
                                "error_type": "STALE_INTENT",
                                "error": f"Canonical TaskContext missing or deleted for task '{live_run.task_id}'",
                            }
                        if live_tc.intent_revision != bound_tc_revision:
                            return {
                                "allowed": False,
                                "error_type": "STALE_INTENT",
                                "error": f"Canonical TaskContext intent drift: canonical={live_tc.intent_revision}, active={bound_tc_revision}",
                            }
                        live_fp = live_tc.compute_fingerprint()
                        if live_fp != bound_tc_fingerprint:
                            return {
                                "allowed": False,
                                "error_type": "STALE_INTENT",
                                "error": f"Canonical TaskContext contract_fingerprint drifted: expected {bound_tc_fingerprint}, got {live_fp}",
                            }
                        if auth_protected and str(live_tc.intent_revision) != str(auth_protected.intent_version):
                            return {
                                "allowed": False,
                                "error_type": "STALE_INTENT",
                                "error": f"Canonical TaskContext intent drift: canonical={live_tc.intent_revision}, active={auth_protected.intent_version}",
                            }
                    elif self.candidate_store and hasattr(self.candidate_store, "get_task_context") and live_run:
                        live_tc = self.candidate_store.get_task_context(live_run.task_id)
                        if live_tc and auth_protected:
                            if str(live_tc.intent_revision) != str(auth_protected.intent_version):
                                return {
                                    "allowed": False,
                                    "error_type": "STALE_INTENT",
                                    "error": f"Canonical TaskContext intent drift: canonical={live_tc.intent_revision}, active={auth_protected.intent_version}",
                                }

                    return {"allowed": True}

                integrity = check_canonical_integrity()
                if not integrity["allowed"]:
                    return integrity

                # Convert incoming messages to standard dict structure
                converted_msgs: list[dict[str, Any]] = []
                for m in raw_messages:
                    m_role = m.get("role")
                    if m_role == "system":
                        continue
                    elif m_role in ("tool", "toolResult"):
                        tool_data = m.get("details") or m.get("data")
                        content_val = m.get("content")
                        if not tool_data and isinstance(content_val, str):
                            try:
                                tool_data = json.loads(content_val)
                            except Exception:
                                pass
                        converted_msgs.append({
                            "role": "tool",
                            "tool_call_id": m.get("tool_call_id") or m.get("toolCallId") or "",
                            "tool_name": m.get("tool_name") or m.get("toolName") or "",
                            "content": content_val if isinstance(content_val, str) else json.dumps(content_val),
                            "data": tool_data,
                            "is_error": bool(m.get("is_error") or m.get("isError")),
                        })
                    elif m_role == "assistant":
                        tool_calls = m.get("tool_calls") or []
                        if not tool_calls and isinstance(m.get("content"), list):
                            for block in m.get("content"):
                                if isinstance(block, dict) and block.get("type") == "toolCall":
                                    tool_calls.append({
                                        "id": block.get("id"),
                                        "type": "function",
                                        "name": block.get("name"),
                                        "function": {
                                            "name": block.get("name"),
                                            "arguments": json.dumps(block.get("arguments", {})) if isinstance(block.get("arguments"), dict) else str(block.get("arguments", "")),
                                        },
                                        "arguments": block.get("arguments", {}),
                                    })
                        converted_msgs.append({
                            "role": "assistant",
                            "content": m.get("content") if isinstance(m.get("content"), str) else "",
                            "tool_calls": tool_calls,
                        })
                    else:
                        converted_msgs.append({
                            "role": "user",
                            "content": m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")),
                        })

                harness_state.messages = converted_msgs
                harness_state.step_count = t_idx

                def checkpoint_response(
                    msgs: list[dict[str, Any]],
                    combined_sys: str,
                    rep: Any,
                ) -> dict[str, Any]:
                    with self._lock:
                        is_pause_requested = r_id in self._pause_requested_runs

                    report_dict = (
                        rep
                        if isinstance(rep, dict)
                        else {
                            "triggered": rep.triggered,
                            "level": rep.level,
                            "tokens_before": rep.tokens_before,
                            "tokens_after": rep.tokens_after,
                            "items_offloaded": rep.items_offloaded,
                            "stale_turns_evicted": rep.stale_turns_evicted,
                            "epoch": rep.epoch,
                            "reason": rep.reason,
                            "details": rep.details,
                        }
                    )

                    if not is_pause_requested:
                        return {
                            "allowed": True,
                            "intent_version": harness_state.intent_version,
                            "system_prompt": combined_sys,
                            "messages": msgs,
                            "report": report_dict,
                        }

                    # Safe recursive serialization helper
                    def _sanitize_val(v: Any) -> Any:
                        if v is None or isinstance(v, (int, float, str, bool)):
                            return v
                        if hasattr(v, "__dataclass_fields__"):
                            return {k: _sanitize_val(val) for k, val in asdict(v).items() if not callable(val)}
                        if hasattr(v, "model_dump") and callable(getattr(v, "model_dump")):
                            return {k: _sanitize_val(val) for k, val in v.model_dump().items() if not callable(val)}
                        if isinstance(v, dict):
                            return {str(k): _sanitize_val(val) for k, val in v.items() if not callable(val)}
                        if isinstance(v, (list, tuple, set)):
                            return [_sanitize_val(x) for x in v if not callable(x)]
                        if isinstance(v, Path):
                            return str(v)
                        return None

                    sanitized_scratchpad: dict[str, Any] = {}
                    for k, v in (harness_state.working_scratchpad or {}).items():
                        if not callable(v):
                            sanitized_scratchpad[str(k)] = _sanitize_val(v)

                    curr_epoch = (
                        report_dict.get("epoch")
                        if "epoch" in report_dict and report_dict["epoch"] is not None
                        else getattr(harness_state, "context_epoch", 1)
                    )

                    collector_ctx = getattr(self.collector, "_runs", {}).get(r_id) if self.collector else None
                    collector_provs = []
                    collector_summaries = []
                    collector_env = {}
                    if collector_ctx:
                        for p in collector_ctx.provenances:
                            if hasattr(p, "__dataclass_fields__"):
                                collector_provs.append(asdict(p))
                            elif isinstance(p, dict):
                                collector_provs.append(p)
                        collector_summaries = list(collector_ctx.action_summaries)
                        collector_env = {
                            k: _sanitize_val(val)
                            for k, val in collector_ctx.environment.items()
                            if not callable(val)
                        }

                    rt_provs = []
                    for p in self._provenances.get(r_id, []):
                        if hasattr(p, "__dataclass_fields__"):
                            rt_provs.append(asdict(p))
                        elif isinstance(p, dict):
                            rt_provs.append(p)

                    cur_run = self.get_run(r_id)
                    f_body = body
                    if not f_body:
                        try:
                            f_body = self.get_run_body(run_id=r_id)
                        except Exception:
                            f_body = ""
                    body_sha256 = hashlib.sha256((f_body or "").encode("utf-8")).hexdigest()

                    model_binding = {
                        k: model_config[k]
                        for k in ("provider", "model_id", "model", "temperature", "max_tokens")
                        if isinstance(model_config, dict) and k in model_config and model_config[k] is not None
                    }

                    tool_schemas = {}
                    for tname in sorted(target_tools):
                        params = self.tool_broker.get_parameters(tname)
                        tool_schemas[tname] = tool_parameters_to_json_schema(params)

                    allowlist = (
                        sorted(list(self.tool_broker.application_allowlist))
                        if self.tool_broker.application_allowlist is not None
                        else None
                    )

                    val = self._run_artifact_validators.get(r_id)
                    val_binding = None
                    if val:
                        val_binding = {
                            "id": getattr(val, "validator_id", getattr(val, "id", str(type(val).__name__))),
                            "version": getattr(val, "validator_version", getattr(val, "version", "1.0.0")),
                            "config_hash": getattr(val, "config_hash", getattr(val, "hash", "")),
                        }

                    task_ctx_binding = None
                    if self.candidate_store and hasattr(self.candidate_store, "get_task_context") and cur_run:
                        tc = self.candidate_store.get_task_context(cur_run.task_id)
                        if tc:
                            task_ctx_binding = {
                                "intent_revision": tc.intent_revision,
                                "contract_fingerprint": tc.compute_fingerprint(),
                                "constraints": sorted(list(tc.constraints)),
                                "acceptance_criteria": tc.acceptance_criteria,
                            }

                    recovery_binding = {
                        "run_id": cur_run.run_id if cur_run else r_id,
                        "task_id": cur_run.task_id if cur_run else "",
                        "intent_revision": cur_run.intent_revision if cur_run else 1,
                        "task_spec_hash": cur_run.task_spec_hash or "" if cur_run else "",
                        "content_hash": cur_run.content_hash or "" if cur_run else "",
                        "body_sha256": body_sha256,
                        "model_config": model_binding,
                        "tools": sorted(target_tools),
                        "tool_schemas": tool_schemas,
                        "application_allowlist": allowlist,
                        "artifact_validator": val_binding,
                        "task_context": task_ctx_binding,
                    }

                    working_state_data = {
                        "goal": harness_state.goal,
                        "intent_version": harness_state.intent_version,
                        "context_epoch": curr_epoch,
                        "step_count": harness_state.step_count,
                        "working_scratchpad": sanitized_scratchpad,
                        "base_system_prompt": base_system_prompt,
                        "collector_provenances": collector_provs,
                        "collector_action_summaries": collector_summaries,
                        "collector_environment": collector_env,
                        "runtime_provenances": rt_provs,
                        "recovery_binding": recovery_binding,
                    }

                    model_cfg_clean = {
                        k: model_config[k]
                        for k in ("provider", "model_id", "model", "temperature", "max_tokens")
                        if isinstance(model_config, dict) and k in model_config
                    }

                    checkpoint_id = f"chk_{uuid.uuid4().hex[:16]}"
                    now_iso = datetime.now(timezone.utc).isoformat()

                    conn = self._get_conn()
                    with self._lock:
                        try:
                            with conn:
                                conn.execute(
                                    """INSERT INTO runtime_pi_checkpoints (
                                        checkpoint_id, run_id, format_version, messages_json,
                                        system_prompt, frozen_body, intent_version, epoch,
                                        working_state_json, model_config_json, tools_json, created_at
                                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (
                                        checkpoint_id,
                                        r_id,
                                        json.dumps(msgs),
                                        combined_sys,
                                        f_body,
                                        harness_state.intent_version,
                                        int(curr_epoch or 1),
                                        json.dumps(working_state_data),
                                        json.dumps(model_cfg_clean),
                                        json.dumps(target_tools),
                                        now_iso,
                                    ),
                                )
                                conn.execute(
                                    """UPDATE runtime_runs
                                       SET status = 'PAUSED', updated_at = ?
                                       WHERE run_id = ? AND terminal_at IS NULL""",
                                    (now_iso, r_id),
                                )
                            self._pause_requested_runs.discard(r_id)
                        except Exception:
                            raise

                    return {
                        "allowed": False,
                        "error_type": "PAUSED",
                        "error": f"Execution paused cooperatively at checkpoint {checkpoint_id}",
                        "intent_version": harness_state.intent_version,
                        "system_prompt": combined_sys,
                        "messages": msgs,
                        "report": report_dict,
                    }

                if not context_compressor:
                    from .context_engine.rebuilder import ContextRebuilder
                    from .context_engine.schemas import HierarchicalContext
                    combined_sys = harness_state.system_prompt
                    if auth_protected:
                        h_ctx = HierarchicalContext(
                            protected=auth_protected,
                            summary=harness_state.working_scratchpad.get("work_summary", WorkSummary()),
                            memory_index=harness_state.working_scratchpad.get("memory_index", MemoryIndex()),
                            recent_messages=converted_msgs,
                        )
                        rebuilder = ContextRebuilder()
                        rendered = rebuilder.render_to_messages(h_ctx, base_system_prompt=base_system_prompt)
                        for r_m in rendered:
                            if r_m.get("role") == "system":
                                combined_sys = r_m.get("content", "")
                                break
                    return checkpoint_response(converted_msgs, combined_sys, {"triggered": False, "level": "none"})

                # Hook: pre_compress
                if hooks and hasattr(hooks, "pre_compress") and hooks.pre_compress:
                    pre_dec = await AsyncHarnessLoop._execute_hook(hooks.pre_compress, "pre_compress", harness_state, default_critical=False)
                    if isinstance(pre_dec, HookDecision) and pre_dec.action in ("deny", "abort"):
                        return {"allowed": False, "error_type": "HOOK_DENIED", "error": f"Pre-compress hook denied: {pre_dec.reason_code}"}
                    if harness_state.status in ("BUDGET_EXHAUSTED", "FAILED", "PAUSED_FOR_REVIEW"):
                        return {"allowed": False, "error_type": harness_state.status, "error": harness_state.error_message or "Terminated by pre_compress"}

                pre_compress_messages = copy.deepcopy(harness_state.messages)
                pre_compress_scratchpad = copy.deepcopy(harness_state.working_scratchpad)
                pre_compress_epoch = getattr(harness_state, "context_epoch", 1)

                report = await context_compressor.compress(harness_state)

                post_compress_integrity = check_canonical_integrity()
                if not post_compress_integrity["allowed"]:
                    harness_state.messages = pre_compress_messages
                    harness_state.working_scratchpad = pre_compress_scratchpad
                    harness_state.context_epoch = pre_compress_epoch
                    if hasattr(report, "deprecated_stale_summary"):
                        report.deprecated_stale_summary = True
                    return post_compress_integrity

                # Hook: post_compress
                if hooks and hasattr(hooks, "post_compress") and hooks.post_compress:
                    post_dec = await AsyncHarnessLoop._execute_hook(hooks.post_compress, "post_compress", harness_state, report, default_critical=False)
                    if isinstance(post_dec, HookDecision) and post_dec.action in ("deny", "abort"):
                        return {"allowed": False, "error_type": "HOOK_DENIED", "error": f"Post-compress hook denied: {post_dec.reason_code}"}
                    if harness_state.status in ("BUDGET_EXHAUSTED", "FAILED", "PAUSED_FOR_REVIEW"):
                        return {"allowed": False, "error_type": harness_state.status, "error": harness_state.error_message or "Terminated by post_compress"}

                rendered = harness_state.messages
                combined_system_prompt = harness_state.system_prompt
                final_recent_messages: list[dict[str, Any]] = []

                for r_msg in rendered:
                    if r_msg.get("role") == "system":
                        combined_system_prompt = r_msg.get("content", "")
                    else:
                        final_recent_messages.append(r_msg)

                if not report.triggered and auth_protected:
                    from .context_engine.rebuilder import ContextRebuilder
                    from .context_engine.schemas import HierarchicalContext
                    h_ctx = HierarchicalContext(
                        protected=auth_protected,
                        summary=harness_state.working_scratchpad.get("work_summary", WorkSummary()),
                        memory_index=harness_state.working_scratchpad.get("memory_index", MemoryIndex()),
                        recent_messages=final_recent_messages,
                    )
                    rebuilder = ContextRebuilder()
                    full_rendered = rebuilder.render_to_messages(h_ctx, base_system_prompt=base_system_prompt)
                    for f_msg in full_rendered:
                        if f_msg.get("role") == "system":
                            combined_system_prompt = f_msg.get("content", "")
                            break

                return checkpoint_response(final_recent_messages, combined_system_prompt, report)

            eff_sys_prompt = (
                checkpoint.system_prompt
                if (checkpoint and checkpoint.system_prompt)
                else system_prompt
            )
            await asyncio.wait_for(
                client.init_run(
                    run_id=run_id,
                    system_prompt=eff_sys_prompt,
                    tools=pi_tools,
                    model_config=model_config,
                    tool_handler=handle_tool_call,
                    turn_handler=handle_turn_request,
                    context_handler=handle_context_transform,
                    receipt_handler=handle_turn_receipt,
                    intent_version=eff_intent,
                    is_test=is_test,
                    initial_messages=checkpoint.messages if checkpoint else None,
                ),
                timeout=timeout,
            )

            if checkpoint:
                res = await asyncio.wait_for(
                    client.continue_run(run_id=run_id, intent_version=eff_intent, timeout=timeout),
                    timeout=timeout,
                )
            else:
                res = await asyncio.wait_for(
                    client.prompt(run_id=run_id, text=input_text, intent_version=eff_intent),
                    timeout=timeout,
                )

            with self._lock:
                self._last_pi_result = res
                self._last_pi_results[run_id] = res

            if res.stop_reason in ("error", "aborted"):
                # Pi can return an error assistant message instead of raising.
                detail = next(
                    (m.get("errorMessage") for m in reversed(res.messages)
                     if m.get("role") == "assistant" and m.get("errorMessage")),
                    res.stop_reason,
                )
                raise RuntimeError(f"Pi execution {res.stop_reason}: {detail}")

            proposed_output = res.text
            harness_state.working_scratchpad["proposed_output"] = proposed_output

            # Hook: before_finalize
            if hooks and hasattr(hooks, "before_finalize") and hooks.before_finalize:
                fin_dec = await AsyncHarnessLoop._execute_hook(
                    hooks.before_finalize,
                    "before_finalize",
                    harness_state,
                    proposed_output,
                    default_critical=True,
                )
                if fin_dec.action == "deny":
                    harness_state.status = "FAILED"
                    harness_state.final_output = f"[DELIVERY_BLOCKED]: {fin_dec.reason_code}"
                    harness_state.error_message = f"Final delivery blocked by BeforeFinalize hook ({fin_dec.reason_code})"
                    if "verification_evidence" not in harness_state.working_scratchpad:
                        harness_state.working_scratchpad["verification_evidence"] = {
                            "independent_pass": False,
                            "failure_reason": f"Denied by BeforeFinalize hook: {fin_dec.reason_code}",
                        }
                    raise RuntimeError(f"BeforeFinalize hook denied: {fin_dec.reason_code}")
                elif fin_dec.action == "review":
                    harness_state.status = "PAUSED_FOR_REVIEW"
                    harness_state.working_scratchpad["proposed_output"] = proposed_output
                    harness_state.error_message = f"Final delivery paused for review: {fin_dec.reason_code}"
                    raise RuntimeError(f"REVIEW_REQUIRED: BeforeFinalize hook requested review: {fin_dec.reason_code}")

                if fin_dec.observation_view is not None:
                    final_text = fin_dec.observation_view
                else:
                    final_text = proposed_output
            else:
                final_text = proposed_output

            harness_state.final_output = final_text
            harness_state.status = "COMPLETED"
            return final_text
        except (Exception, asyncio.CancelledError) as exc:
            with self._lock:
                now_iso = datetime.now(timezone.utc).isoformat()
                is_cancelled = (
                    isinstance(exc, asyncio.CancelledError)
                    or "abort" in str(exc).lower()
                    or "cancel" in str(exc).lower()
                )
                is_timeout = (
                    isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    or "timed out" in str(exc).lower()
                    or "timed_out" in str(exc).lower()
                    or "timeout" in str(exc).lower()
                    or "deadline" in str(exc).lower()
                )
                is_review = (
                    "review" in str(exc).lower()
                    or "paused_for_review" in str(exc).lower()
                    or getattr(harness_state, "status", None) == "PAUSED_FOR_REVIEW"
                )
                is_hook_denied = (
                    "hook_denied" in str(exc).lower()
                    or "delivery blocked" in str(exc).lower()
                    or "beforefinalize hook denied" in str(exc).lower()
                    or getattr(harness_state, "status", None) == "FAILED"
                )
                is_budget = not is_hook_denied and not is_review and "budget" in str(exc).lower()

                term_status = (
                    "INTERRUPTED" if is_review
                    else "CANCELLED" if is_cancelled
                    else "TIMED_OUT" if is_timeout
                    else "BUDGET_EXHAUSTED" if is_budget
                    else "FAILED"
                )
                err_type = "REVIEW_REQUIRED" if is_review else "HOOK_DENIED" if is_hook_denied else term_status
                err_msg = str(exc) or term_status
                harness_state.status = "PAUSED_FOR_REVIEW" if is_review else term_status
                harness_state.error_message = err_msg

                conn = self._get_conn()
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = ?, error_type = ?, error_message = ?, updated_at = ?, terminal_at = ?
                       WHERE run_id = ? AND terminal_at IS NULL""",
                    (term_status, err_type, err_msg, now_iso, now_iso, run_id),
                )
                conn.commit()

                if self.collector and run_id in getattr(self.collector, "_runs", {}):
                    collector_ctx = self.collector._runs[run_id]
                    if not getattr(collector_ctx, "completed", False):
                        try:
                            self.collector.finish_run(
                                run_id=run_id,
                                model_output="",
                                verification_evidence={
                                    "independent_pass": None if is_review else False,
                                    "failure_reason": f"Execution abnormal termination: {err_msg}",
                                },
                                infra_error=err_msg,
                            )
                        except Exception:
                            pass
            raise
        finally:
            try:
                if hooks and hasattr(hooks, "task_end") and hooks.task_end:
                    await AsyncHarnessLoop._execute_hook(
                        hooks.task_end,
                        "task_end",
                        harness_state,
                        default_critical=False,
                    )
            except Exception:
                pass
            finally:
                with self._lock:
                    self._active_pi_clients.pop(run_id, None)
                await client.close()

    def run_agent_with_pi(
        self,
        run_id: str,
        input_text: str,
        candidate: Optional[Any] = None,
        tools: Optional[list[str]] = None,
        system_prompt_header: str = "",
        model_config: Optional[dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
        timeout: float = 30.0,
        context_compressor: Optional[Any] = None,
        hooks: Optional[Any] = None,
        initial_scratchpad: Optional[dict[str, Any]] = None,
        goal: Optional[str] = None,
        intent_version: Optional[str] = None,
        active_constraints: Optional[list[str]] = None,
        workspace_dir: Optional[Path] = None,
        model_turn_admission: Optional[Callable[[str, int], Any]] = None,
        model_turn_receipt: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> str:
        """Synchronous wrapper for arun_agent_with_pi."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = self.arun_agent_with_pi(
            run_id=run_id,
            input_text=input_text,
            candidate=candidate,
            tools=tools,
            system_prompt_header=system_prompt_header,
            model_config=model_config,
            test_mode=test_mode,
            timeout=timeout,
            context_compressor=context_compressor,
            hooks=hooks,
            initial_scratchpad=initial_scratchpad,
            goal=goal,
            intent_version=intent_version,
            active_constraints=active_constraints,
            workspace_dir=workspace_dir,
            model_turn_admission=model_turn_admission,
            model_turn_receipt=model_turn_receipt,
        )

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

    async def aresume_agent_with_pi(
        self,
        run_id: str,
        model_config: Optional[dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
        timeout: float = 30.0,
        context_compressor: Optional[Any] = None,
        hooks: Optional[Any] = None,
        workspace_dir: Optional[Path] = None,
    ) -> str:
        """Resume execution of a cooperatively paused Pi agent run from its latest checkpoint.

        Guarantees:
        - Atomic CAS transition from PAUSED -> RUNNING without mutating budget_max or deadline.
        - Original frozen skill body snapshot is preserved (never reads modified candidate drafts).
        - Reconstructs typed HarnessState (ProtectedState, WorkSummary, MemoryIndex) and collector context.
        - Previous tool calls and results are restored without redispatching.
        - Model turns and tool calls continue sharing authoritative runtime budget and deadline.
        """
        run_rec = self.get_run(run_id)
        if not run_rec:
            raise KeyError(f"Run '{run_id}' not found")

        TERMINAL_STATUSES = {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "BUDGET_EXHAUSTED",
            "INTERRUPTED",
        }
        if run_rec.status in TERMINAL_STATUSES:
            raise ValueError(f"Cannot resume run '{run_id}' in terminal state '{run_rec.status}'")

        # Query pending unsettled ADMITTED tool calls before reading/resuming checkpoint
        conn = self._get_conn()
        admitted = conn.execute(
            "SELECT call_id FROM runtime_tool_calls WHERE run_id = ? AND status = 'ADMITTED' ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        if admitted:
            admitted_ids = [r[0] for r in admitted]
            now_iso = datetime.now(timezone.utc).isoformat()
            msg = f"Unsettled in-flight tool calls requiring review: {', '.join(admitted_ids)}"
            with self._lock:
                conn.execute(
                    """UPDATE runtime_runs
                       SET status = 'INTERRUPTED', error_type = 'REVIEW_REQUIRED',
                           error_message = ?, updated_at = ?, terminal_at = ?
                       WHERE run_id = ?""",
                    (msg, now_iso, now_iso, run_id),
                )
                conn.commit()
                if (
                    self.collector
                    and hasattr(self.collector, "_runs")
                    and run_id in self.collector._runs
                ):
                    try:
                        self.collector.finish_run(
                            run_id=run_id,
                            infra_error=msg,
                            verification_evidence={
                                "independent_pass": None,
                                "failure_reason": f"REVIEW_REQUIRED: {msg}",
                            },
                        )
                    except Exception:
                        pass
            raise ValueError(f"REVIEW_REQUIRED: {msg}")

        chk = self.get_pi_checkpoint(run_id)
        if not chk:
            raise KeyError(f"No checkpoint found for run '{run_id}'")

        return await self.arun_agent_with_pi(
            run_id=run_id,
            input_text="",
            model_config=model_config,
            test_mode=test_mode,
            timeout=timeout,
            context_compressor=context_compressor,
            hooks=hooks,
            workspace_dir=workspace_dir,
            checkpoint=chk,
        )

    def resume_agent_with_pi(
        self,
        run_id: str,
        model_config: Optional[dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
        timeout: float = 30.0,
        context_compressor: Optional[Any] = None,
        hooks: Optional[Any] = None,
        workspace_dir: Optional[Path] = None,
    ) -> str:
        """Synchronous wrapper for aresume_agent_with_pi."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = self.aresume_agent_with_pi(
            run_id=run_id,
            model_config=model_config,
            test_mode=test_mode,
            timeout=timeout,
            context_compressor=context_compressor,
            hooks=hooks,
            workspace_dir=workspace_dir,
        )

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

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


