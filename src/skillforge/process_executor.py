"""Managed Asynchronous Process Execution with Process Group Isolation and Bounded Streaming.

Provides:
- ProcessConfig: Parameters for command execution (cmd, cwd, env, timeout, memory limit, log file).
- ProcessResult: Execution outcome with exit code, in-memory bounded tail, disk log reference, and timings.
- AsyncProcessExecutor: Executes processes using asyncio.create_subprocess_exec with start_new_session=True,
  bounded memory ring buffers, streaming to disk, and process-group-level cancellation (SIGTERM -> SIGKILL).
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set


@dataclass
class ProcessConfig:
    """Configuration for an asynchronous subprocess execution."""

    cmd: list[str]
    cwd: Optional[Path] = None
    env: Optional[dict[str, str]] = None
    input_str: Optional[str] = None
    timeout_seconds: float = 30.0
    max_memory_output_bytes: int = 65536  # 64KB in memory
    log_file_path: Optional[Path] = None  # Full stream written to disk if set
    run_id: Optional[str] = None


@dataclass
class ProcessResult:
    """Result of an asynchronous subprocess execution."""

    exit_code: int
    stdout: str
    stderr: str
    is_timeout: bool = False
    is_cancelled: bool = False
    is_truncated: bool = False
    total_stdout_bytes: int = 0
    total_stderr_bytes: int = 0
    latency_ms: float = 0.0
    log_file_path: Optional[Path] = None
    error_message: Optional[str] = None


class AsyncProcessExecutor:
    """Manages asynchronous subprocess execution with process group isolation and bounded memory."""

    def __init__(self) -> None:
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_runs: Set[str] = set()
        self._lock = asyncio.Lock()

    async def execute(self, config: ProcessConfig) -> ProcessResult:
        """Execute command asynchronously with process group isolation, disk streaming, and timeout."""
        start_time = time.perf_counter()
        run_id = config.run_id or f"proc_{int(start_time * 1000)}"

        # Prepare directory for log file if specified
        log_file_handle = None
        if config.log_file_path:
            config.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            log_file_handle = open(config.log_file_path, "wb")

        is_timeout = False
        is_cancelled = False
        is_truncated = False
        exit_code = -1
        error_message = None

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        total_stdout = 0
        total_stderr = 0

        # Convert cwd to string if Path
        cwd_str = str(config.cwd) if config.cwd else None

        try:
            # start_new_session=True creates a new POSIX session and process group (pgid = pid)
            proc = await asyncio.create_subprocess_exec(
                *config.cmd,
                stdin=asyncio.subprocess.PIPE if config.input_str is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_str,
                env=config.env,
                start_new_session=True,
            )

            async with self._lock:
                self._active_procs[run_id] = proc

            async def _stream_reader(
                stream: Optional[asyncio.StreamReader],
                is_out: bool,
            ) -> None:
                nonlocal total_stdout, total_stderr, is_truncated
                if stream is None:
                    return

                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        break

                    # 1. Stream full bytes to disk log
                    if log_file_handle:
                        log_file_handle.write(chunk)
                        log_file_handle.flush()

                    # 2. Maintain bounded in-memory representation
                    if is_out:
                        total_stdout += len(chunk)
                        if total_stdout <= config.max_memory_output_bytes:
                            stdout_chunks.append(chunk)
                        else:
                            is_truncated = True
                    else:
                        total_stderr += len(chunk)
                        if total_stderr <= config.max_memory_output_bytes:
                            stderr_chunks.append(chunk)
                        else:
                            is_truncated = True

            # Task for writing stdin if any
            async def _write_stdin() -> None:
                if proc.stdin and config.input_str is not None:
                    proc.stdin.write(config.input_str.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()

            stdin_task = asyncio.create_task(_write_stdin())
            stdout_task = asyncio.create_task(_stream_reader(proc.stdout, True))
            stderr_task = asyncio.create_task(_stream_reader(proc.stderr, False))

            try:
                # Wait for process and stream consumption within timeout
                await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stdout_task, stderr_task, stdin_task),
                    timeout=config.timeout_seconds,
                )
                exit_code = proc.returncode if proc.returncode is not None else -1
            except asyncio.TimeoutError:
                is_timeout = True
                error_message = f"Process timed out after {config.timeout_seconds}s"
                await self._terminate_proc_group(proc, grace_period=0.5)
                exit_code = proc.returncode if proc.returncode is not None else -1
                # Cancel remaining reading tasks
                for t in (stdout_task, stderr_task, stdin_task):
                    if not t.done():
                        t.cancel()

            # Check if this run was explicitly cancelled
            if run_id in self._cancelled_runs:
                is_cancelled = True
                if not error_message:
                    error_message = "Process cancelled by user"

        except Exception as exc:
            exit_code = -1
            error_message = f"{type(exc).__name__}: {exc}"
        finally:
            if log_file_handle:
                try:
                    log_file_handle.close()
                except Exception:
                    pass

            async with self._lock:
                self._active_procs.pop(run_id, None)
                self._cancelled_runs.discard(run_id)

        # Assemble bounded in-memory string representations
        raw_stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        raw_stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if is_truncated and total_stdout > config.max_memory_output_bytes:
            raw_stdout += f"\n... [TRUNCATED ({total_stdout} total bytes)]"
        if is_truncated and total_stderr > config.max_memory_output_bytes:
            raw_stderr += f"\n... [TRUNCATED ({total_stderr} total bytes)]"

        if is_timeout and not raw_stderr:
            raw_stderr = f"Execution timed out after {config.timeout_seconds} seconds"

        latency_ms = (time.perf_counter() - start_time) * 1000

        return ProcessResult(
            exit_code=exit_code,
            stdout=raw_stdout,
            stderr=raw_stderr,
            is_timeout=is_timeout,
            is_cancelled=is_cancelled,
            is_truncated=is_truncated,
            total_stdout_bytes=total_stdout,
            total_stderr_bytes=total_stderr,
            latency_ms=latency_ms,
            log_file_path=config.log_file_path,
            error_message=error_message,
        )

    async def cancel_run(self, run_id: str, grace_period: float = 0.5) -> bool:
        """Cancel an ongoing run by terminating its entire process group."""
        async with self._lock:
            proc = self._active_procs.get(run_id)
            self._cancelled_runs.add(run_id)

        if not proc or proc.returncode is not None:
            return False

        await self._terminate_proc_group(proc, grace_period=grace_period)
        return True

    async def _terminate_proc_group(
        self,
        proc: asyncio.subprocess.Process,
        grace_period: float = 0.5,
    ) -> None:
        """Safely terminate an entire process group using SIGTERM -> SIGKILL."""
        if proc.returncode is not None:
            return

        pid = proc.pid
        try:
            # Because start_new_session=True was used, pid is pgid
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except (ProcessLookupError, PermissionError):
                pass

        # Give grace period for clean termination
        t0 = time.time()
        while time.time() - t0 < grace_period:
            if proc.returncode is not None:
                return
            await asyncio.sleep(0.05)

        # Escalate to SIGKILL if still running
        if proc.returncode is None:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError):
                    pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass
