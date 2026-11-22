"""SkillForge Pi Agent SDK Async Adapter.

Provides:
- PiRunnerClient: Manages a subprocess running the TypeScript Pi runner via JSONL over stdio.
- Implements strict protocol versioning, run_id / request_id / call_id correlation.
- Enforces Python as authoritative gateway for tool execution (ToolBroker / AgentRuntime).
- Handles graceful abort, deadline termination, and subprocess reaping without zombies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Set

logger = logging.getLogger("skillforge.pi_adapter")

PROTOCOL_VERSION = "pi_bridge_v1"
MINIMUM_NODE_MAJOR = 22


def validate_node_environment(node_bin: str) -> None:
    """Validate Node.js is >= 22.0.0 and PI_SOURCE_ROOT if configured."""
    try:
        ver_output = subprocess.check_output([node_bin, "-v"], text=True).strip()
        clean_ver = ver_output.lstrip("v")
        major = int(clean_ver.split(".")[0])
        if major < MINIMUM_NODE_MAJOR:
            raise RuntimeError(
                f"Node.js >= {MINIMUM_NODE_MAJOR}.0.0 required for Pi runner, detected {ver_output}"
            )
    except (subprocess.SubprocessError, FileNotFoundError) as err:
        if isinstance(err, RuntimeError):
            raise
        raise RuntimeError(f"Failed to execute node binary '{node_bin}': {err}")

    source_root = os.environ.get("PI_SOURCE_ROOT")
    if source_root:
        s_path = Path(source_root).resolve()
        if not s_path.is_dir() or not (s_path / "package.json").is_file():
            raise ValueError(
                f"Invalid PI_SOURCE_ROOT '{source_root}': directory must exist and contain package.json"
            )


def get_default_runner_paths() -> tuple[str, str, Path, Path]:
    """Locate node binary, tsx binary, register hook, and runner.ts path."""
    bridge_dir = Path(__file__).resolve().parent / "pi_runner"
    node_bin = shutil.which("node") or "node"
    tsx_bin = str(bridge_dir / "node_modules" / ".bin" / "tsx")
    runner_ts = bridge_dir / "runner.ts"
    register_mjs = bridge_dir / "register.mjs"

    if not runner_ts.exists():
        raise FileNotFoundError(f"Pi runner script not found at {runner_ts}")

    validate_node_environment(node_bin)
    return node_bin, tsx_bin, register_mjs, runner_ts


def tool_parameters_to_json_schema(parameters: list[Any]) -> dict[str, Any]:
    """Convert ToolParameter list to standard JSON schema with strict type validation.

    Supported types: 'string', 'number', 'integer', 'boolean', 'array', 'object'.
    Unsupported types are rejected immediately.
    """
    valid_types = {"string", "number", "integer", "boolean", "array", "object"}
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in parameters:
        p_name = getattr(param, "name", None) or str(param.get("name", ""))
        p_type = getattr(param, "type", None) or str(param.get("type", "string"))
        p_desc = getattr(param, "description", "") or str(param.get("description", ""))
        p_req = getattr(param, "required", False) or bool(param.get("required", False))

        if p_type not in valid_types:
            raise ValueError(f"Unsupported schema parameter type '{p_type}' for parameter '{p_name}'")

        properties[p_name] = {
            "type": p_type,
            "description": p_desc,
        }
        if p_req:
            required.append(p_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


@dataclass
class PiExecutionResult:
    """Structured result returned by the Pi runner prompt execution."""

    run_id: str
    request_id: str
    text: str
    stop_reason: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)
    usage: Optional[dict[str, Any]] = None
    captured_provider_inputs: list[dict[str, Any]] = field(default_factory=list)


class PiRunnerClient:
    """Manages the lifecycle of a dedicated Pi SDK Node.js subprocess."""

    def __init__(
        self,
        node_bin: Optional[str] = None,
        tsx_bin: Optional[str] = None,
        register_mjs: Optional[Path] = None,
        runner_ts: Optional[Path] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        def_node, def_tsx, def_reg, def_run = get_default_runner_paths()
        self.node_bin = node_bin or def_node
        self.tsx_bin = tsx_bin or def_tsx
        self.register_mjs = register_mjs or def_reg
        self.runner_ts = runner_ts or def_run
        self.cwd = cwd or self.runner_ts.parent

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

        self._active_run_id: Optional[str] = None
        self._ready_future: Optional[asyncio.Future[None]] = None
        self._prompt_future: Optional[asyncio.Future[dict[str, Any]]] = None
        self._tool_handler: Optional[
            Callable[[str, str, dict[str, Any], str], Awaitable[dict[str, Any]]]
        ] = None
        self._turn_handler: Optional[
            Callable[..., Awaitable[dict[str, Any]]]
        ] = None
        self._context_handler: Optional[
            Callable[[str, str, str, int, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
        ] = None
        self._receipt_handler: Optional[Callable[[dict[str, Any]], Any]] = None

        self._executed_call_ids: Set[str] = set()
        self._is_aborted: bool = False
        self._fatal_error: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    async def start(self) -> None:
        """Spawn the Pi runner subprocess and attach reader tasks."""
        if self.is_running:
            return

        cmd = [self.tsx_bin, "--import", str(self.register_mjs), str(self.runner_ts)]
        env = dict(os.environ)
        node_modules_dir = str(self.cwd / "node_modules")
        env["NODE_PATH"] = node_modules_dir

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def init_run(
        self,
        run_id: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        model_config: Optional[dict[str, Any]] = None,
        tool_handler: Optional[
            Callable[[str, str, dict[str, Any], str], Awaitable[dict[str, Any]]]
        ] = None,
        turn_handler: Optional[
            Callable[..., Awaitable[dict[str, Any]]]
        ] = None,
        context_handler: Optional[
            Callable[[str, str, str, int, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
        ] = None,
        receipt_handler: Optional[Callable[[dict[str, Any]], Any]] = None,
        intent_version: str = "v1",
        is_test: Optional[bool] = None,
        timeout: float = 15.0,
        initial_messages: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Initialize a run context on the runner with frozen system prompt and tools."""
        if not self.is_running:
            await self.start()

        if is_test is None:
            is_test = bool(
                "pytest" in sys.modules
                or os.environ.get("SKILLFORGE_ENV") == "test"
                or (model_config and (model_config.get("is_test") or model_config.get("test_mode")))
            )

        async with self._lock:
            self._active_run_id = run_id
            self._is_aborted = False
            self._executed_call_ids.clear()
            if initial_messages:
                for im in initial_messages:
                    if isinstance(im, dict):
                        if im.get("role") in ("tool", "toolResult"):
                            cid = im.get("tool_call_id") or im.get("toolCallId")
                            if cid:
                                self._executed_call_ids.add(str(cid))
                        elif im.get("role") == "assistant":
                            for tc in im.get("tool_calls", []):
                                if isinstance(tc, dict):
                                    cid = tc.get("id")
                                    if cid:
                                        self._executed_call_ids.add(str(cid))
                            for block in im.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "toolCall":
                                    cid = block.get("id")
                                    if cid:
                                        self._executed_call_ids.add(str(cid))
            self._tool_handler = tool_handler
            self._turn_handler = turn_handler
            self._context_handler = context_handler
            self._receipt_handler = receipt_handler

            loop = asyncio.get_running_loop()
            self._ready_future = loop.create_future()

            init_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "init",
                "run_id": run_id,
                "system_prompt": system_prompt,
                "tools": tools,
                "model_config": model_config or {"provider": "anthropic", "model_id": "claude-haiku-4-5"},
                "intent_version": intent_version,
                "is_test": is_test,
                "initial_messages": initial_messages or [],
            }
            await self._send_frame(init_frame)

        await asyncio.wait_for(self._ready_future, timeout=timeout)

    async def continue_run(
        self,
        run_id: str,
        request_id: str = "req_cont",
        intent_version: str = "v1",
        timeout: float = 30.0,
    ) -> PiExecutionResult:
        """Send a continue frame to the runner and await the resumed response without re-prompting."""
        if not self.is_running:
            raise RuntimeError("Pi runner subprocess is not running")

        async with self._lock:
            if run_id != self._active_run_id:
                raise ValueError(
                    f"Run ID mismatch: active run is {self._active_run_id}, requested {run_id}"
                )
            if self._is_aborted:
                raise RuntimeError(f"Run '{run_id}' has already been aborted")

            loop = asyncio.get_running_loop()
            self._prompt_future = loop.create_future()

            continue_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "continue",
                "run_id": run_id,
                "request_id": request_id,
                "intent_version": intent_version,
            }
            await self._send_frame(continue_frame)

        resp = await asyncio.wait_for(self._prompt_future, timeout=timeout)
        if resp.get("type") == "error":
            raise RuntimeError(f"Pi runner execution error: {resp.get('error')}")

        raw_usage = resp.get("usage")
        eff_usage = dict(raw_usage) if isinstance(raw_usage, dict) else None
        stop_reason = resp.get("stop_reason") or ("paused" if resp.get("type") == "paused" else "stop")

        return PiExecutionResult(
            run_id=run_id,
            request_id=request_id,
            text=resp.get("text", ""),
            stop_reason=stop_reason,
            messages=resp.get("messages", []),
            raw_response=resp,
            usage=eff_usage,
            captured_provider_inputs=resp.get("captured_provider_inputs", []),
        )

    async def prompt(
        self,
        run_id: str,
        text: str,
        request_id: str = "req_1",
        intent_version: str = "v1",
        timeout: float = 30.0,
    ) -> PiExecutionResult:
        """Send a prompt to the runner and await the final response."""
        if not self.is_running:
            raise RuntimeError("Pi runner subprocess is not running")

        async with self._lock:
            if run_id != self._active_run_id:
                raise ValueError(
                    f"Run ID mismatch: active run is {self._active_run_id}, requested {run_id}"
                )
            if self._is_aborted:
                raise RuntimeError(f"Run '{run_id}' has already been aborted")

            loop = asyncio.get_running_loop()
            self._prompt_future = loop.create_future()

            prompt_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "prompt",
                "run_id": run_id,
                "request_id": request_id,
                "intent_version": intent_version,
                "text": text,
            }
            await self._send_frame(prompt_frame)

        resp = await asyncio.wait_for(self._prompt_future, timeout=timeout)
        if resp.get("type") == "error":
            raise RuntimeError(f"Pi runner execution error: {resp.get('error')}")

        raw_usage = resp.get("usage")
        eff_usage = dict(raw_usage) if isinstance(raw_usage, dict) else None
        stop_reason = resp.get("stop_reason") or ("paused" if resp.get("type") == "paused" else "stop")

        return PiExecutionResult(
            run_id=run_id,
            request_id=request_id,
            text=resp.get("text", ""),
            stop_reason=stop_reason,
            messages=resp.get("messages", []),
            raw_response=resp,
            usage=eff_usage,
            captured_provider_inputs=resp.get("captured_provider_inputs", []),
        )

    async def abort(self, run_id: Optional[str] = None, reason: str = "User cancelled") -> None:
        """Request the Pi runner to abort the ongoing run immediately."""
        target_run_id = run_id or self._active_run_id
        self._is_aborted = True

        if target_run_id and self.is_running:
            abort_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "abort",
                "run_id": target_run_id,
                "reason": reason,
            }
            try:
                await self._send_frame(abort_frame)
            except Exception as e:
                logger.warning(f"Failed to send abort frame: {e}")

        # Fail any pending prompt future
        if self._prompt_future and not self._prompt_future.done():
            self._prompt_future.set_exception(asyncio.CancelledError(f"Run {target_run_id} aborted: {reason}"))

    async def pause(self, run_id: Optional[str] = None) -> None:
        """Send a pause frame to the runner to request immediate cooperative pause."""
        if not self.is_running:
            return
        target_id = run_id or self._active_run_id or ""
        pause_frame = {
            "protocol": PROTOCOL_VERSION,
            "type": "pause",
            "run_id": target_id,
        }
        try:
            await self._send_frame(pause_frame)
        except Exception as e:
            logger.warning(f"Failed to send pause frame: {e}")

    async def close(self, grace_period: float = 2.0) -> int:
        """Terminate the subprocess with finite grace period, reap exit code, and clean up."""
        proc = self._proc
        self._proc = None

        if proc is None:
            return 0

        # Try graceful stdin close first
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:
                pass

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_period)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    await proc.wait()

        # Cancel background reader tasks cleanly
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Converge pending futures so nothing dangles
        if self._ready_future and not self._ready_future.done():
            self._ready_future.set_exception(RuntimeError("Pi runner closed before ready"))
        if self._prompt_future and not self._prompt_future.done():
            self._prompt_future.set_exception(RuntimeError("Pi runner closed before prompt completed"))

        return proc.returncode or 0

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin or self._proc.stdin.is_closing():
            return
        line = json.dumps(frame) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

    async def _read_stdout(self) -> None:
        """Stream JSONL frames from runner stdout."""
        if not self._proc or not self._proc.stdout:
            return

        reader = self._proc.stdout
        while not reader.at_eof():
            try:
                raw_line = await reader.readline()
            except Exception as e:
                logger.error(f"Error reading runner stdout: {e}")
                break

            if not raw_line:
                break

            line_str = raw_line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                frame = json.loads(line_str)
            except json.JSONDecodeError as err:
                logger.warning(f"Invalid JSONL frame from runner: {line_str} ({err})")
                continue

            await self._dispatch_frame(frame)

        # Premature EOF detection
        if self._ready_future and not self._ready_future.done():
            self._ready_future.set_exception(
                RuntimeError("Pi runner process closed stdout unexpectedly before ready (EOF)")
            )
        if self._prompt_future and not self._prompt_future.done():
            self._prompt_future.set_exception(
                RuntimeError("Pi runner process closed stdout unexpectedly before prompt response (EOF)")
            )

    async def _read_stderr(self) -> None:
        """Stream diagnostic messages from runner stderr."""
        if not self._proc or not self._proc.stderr:
            return

        reader = self._proc.stderr
        while not reader.at_eof():
            try:
                raw_line = await reader.readline()
            except Exception:
                break
            if not raw_line:
                break
            line_str = raw_line.decode("utf-8", errors="replace").strip()
            if line_str:
                logger.debug(f"[Pi Runner Stderr] {line_str}")

    async def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        """Process protocol frame received from runner."""
        f_type = frame.get("type")
        run_id = frame.get("run_id")

        if frame.get("protocol") != PROTOCOL_VERSION:
            err = f"Protocol version mismatch: expected {PROTOCOL_VERSION}, got {frame.get('protocol')}"
            self._fatal_error = err
            if self._prompt_future and not self._prompt_future.done():
                self._prompt_future.set_exception(RuntimeError(err))
            return

        if self._fatal_error:
            return

        if f_type == "ready":
            if self._ready_future and not self._ready_future.done():
                if run_id == self._active_run_id:
                    self._ready_future.set_result(None)
                else:
                    self._ready_future.set_exception(
                        RuntimeError(f"Ready run_id mismatch: expected {self._active_run_id}, got {run_id}")
                    )

        elif f_type == "turn_request":
            t_idx = frame.get("turn_index", 1)
            p_input = frame.get("provider_input")
            if not self._turn_handler:
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "turn_decision",
                    "run_id": run_id,
                    "allowed": True,
                })
                return

            try:
                import inspect
                sig = inspect.signature(self._turn_handler)
                if len(sig.parameters) >= 3:
                    decision = await self._turn_handler(run_id, t_idx, p_input)
                else:
                    decision = await self._turn_handler(run_id, t_idx)
            except Exception as e:
                decision = {"allowed": False, "error_type": "HANDLER_EXCEPTION", "error": str(e)}

            await self._send_frame({
                "protocol": PROTOCOL_VERSION,
                "type": "turn_decision",
                "run_id": run_id,
                "allowed": decision.get("allowed", False),
                "error_type": decision.get("error_type"),
                "error": decision.get("error"),
            })

        elif f_type == "turn_receipt":
            receipt_id = frame.get("receipt_id", "")
            if run_id != self._active_run_id:
                logger.warning(
                    f"Cross-run turn receipt ignored: {run_id} != {self._active_run_id} (receipt_id={receipt_id})"
                )
                return

            if self._receipt_handler:
                try:
                    import inspect
                    res = self._receipt_handler(frame)
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    logger.exception(f"Exception during turn receipt handler for run {run_id}: {e}")
                    self._fatal_error = f"Receipt accounting failure: {e}"
                    if self._prompt_future and not self._prompt_future.done():
                        self._prompt_future.set_exception(e)

        elif f_type == "context_request":
            req_id = frame.get("request_id", "")
            intent_ver = frame.get("intent_version", "v1")
            t_idx = frame.get("turn_index", 1)
            raw_messages = frame.get("messages", [])

            # 1. Cross-run isolation check
            if run_id != self._active_run_id:
                logger.warning(f"Cross-run context request rejected: {run_id} != {self._active_run_id}")
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "context_response",
                    "run_id": run_id,
                    "request_id": req_id,
                    "allowed": False,
                    "error_type": "CROSS_RUN_MISMATCH",
                    "error": f"Run ID mismatch: expected {self._active_run_id}, got {run_id}",
                })
                return

            # 2. Abort status check
            if self._is_aborted:
                logger.info(f"Context request rejected due to active run abort")
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "context_response",
                    "run_id": run_id,
                    "request_id": req_id,
                    "allowed": False,
                    "error_type": "RUN_ABORTED",
                    "error": "Run was aborted; context transformation denied",
                })
                return

            if not self._context_handler:
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "context_response",
                    "run_id": run_id,
                    "request_id": req_id,
                    "intent_version": intent_ver,
                    "system_prompt": "",
                    "messages": raw_messages,
                    "report": {"triggered": False, "level": "none"},
                    "allowed": True,
                })
                return

            try:
                res = await self._context_handler(run_id, req_id, intent_ver, t_idx, raw_messages)
            except Exception as e:
                logger.exception(f"Exception during context transform handler for run {run_id}")
                res = {
                    "allowed": False,
                    "error_type": "HANDLER_EXCEPTION",
                    "error": str(e),
                    "messages": raw_messages,
                }

            resp_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "context_response",
                "run_id": run_id,
                "request_id": req_id,
                "intent_version": res.get("intent_version", intent_ver),
                "system_prompt": res.get("system_prompt", ""),
                "messages": res.get("messages", raw_messages),
                "report": res.get("report", {"triggered": False, "level": "none"}),
                "allowed": res.get("allowed", True),
                "error_type": res.get("error_type"),
                "error": res.get("error"),
            }
            await self._send_frame(resp_frame)

        elif f_type == "tool_call":
            call_id = frame.get("call_id", "")
            tool_name = frame.get("tool_name", "")
            parameters = frame.get("parameters", {})

            # 1. Cross-run isolation check
            if run_id != self._active_run_id:
                logger.warning(f"Cross-run tool call rejected: {run_id} != {self._active_run_id}")
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "tool_result",
                    "run_id": run_id,
                    "call_id": call_id,
                    "status": "REJECTED",
                    "error_type": "CROSS_RUN_MISMATCH",
                    "error": f"Run ID mismatch: expected {self._active_run_id}, got {run_id}",
                })
                return

            # 2. Duplicate call_id check
            if call_id in self._executed_call_ids:
                logger.warning(f"Duplicate tool call ID rejected without side effect: {call_id}")
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "tool_result",
                    "run_id": run_id,
                    "call_id": call_id,
                    "status": "REJECTED",
                    "error_type": "DUPLICATE_CALL_ID",
                    "error": f"Duplicate tool call ID '{call_id}' already executed",
                })
                return

            # 3. Abort status check
            if self._is_aborted:
                logger.info(f"Tool call {call_id} rejected due to active run abort")
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "tool_result",
                    "run_id": run_id,
                    "call_id": call_id,
                    "status": "REJECTED",
                    "error_type": "RUN_ABORTED",
                    "error": "Run was aborted; subsequent tool execution denied",
                })
                return

            # Mark call_id executed to prevent duplicate execution
            self._executed_call_ids.add(call_id)

            if not self._tool_handler:
                await self._send_frame({
                    "protocol": PROTOCOL_VERSION,
                    "type": "tool_result",
                    "run_id": run_id,
                    "call_id": call_id,
                    "status": "REJECTED",
                    "error_type": "NO_TOOL_HANDLER",
                    "error": "No Python tool handler registered on Pi adapter",
                })
                return

            # Execute via authoritative Python handler
            try:
                res = await self._tool_handler(run_id, tool_name, parameters, call_id)
            except Exception as e:
                logger.exception(f"Exception during tool execution handler for {tool_name}")
                res = {
                    "status": "FAILED",
                    "error_type": "HANDLER_EXCEPTION",
                    "error": str(e),
                }

            # Return tool_result frame to runner
            result_frame = {
                "protocol": PROTOCOL_VERSION,
                "type": "tool_result",
                "run_id": run_id,
                "call_id": call_id,
                "status": res.get("status", "COMPLETED"),
                "error_type": res.get("error_type"),
                "error": res.get("error") or res.get("error_message"),
                "output": res.get("output", ""),
                "data": res.get("data", {}),
            }
            await self._send_frame(result_frame)

        elif f_type in ("response", "aborted", "paused"):
            if self._prompt_future and not self._prompt_future.done():
                self._prompt_future.set_result(frame)

        elif f_type == "error":
            err_msg = frame.get("error", "Unknown Pi runner error")
            logger.error(f"Error frame received from Pi runner: {err_msg}")
            if frame.get("fatal", False):
                self._fatal_error = err_msg
            if self._ready_future and not self._ready_future.done():
                self._ready_future.set_exception(RuntimeError(f"Pi runner protocol error: {err_msg}"))
            if self._prompt_future and not self._prompt_future.done():
                self._prompt_future.set_exception(RuntimeError(f"Pi runner protocol error: {err_msg}"))
