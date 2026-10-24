"""Tests for AsyncProcessExecutor: process group isolation, cancellation, streaming, and bounded memory."""
import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
import pytest

from skillforge.process_executor import (
    AsyncProcessExecutor,
    ProcessConfig,
    ProcessResult,
)


def test_normal_execution():
    async def _test():
        executor = AsyncProcessExecutor()
        config = ProcessConfig(
            cmd=["echo", "hello world"],
            timeout_seconds=5.0,
        )
        res = await executor.execute(config)
        assert res.exit_code == 0
        assert res.stdout.strip() == "hello world"
        assert res.is_timeout is False
        assert res.is_cancelled is False
        assert res.latency_ms > 0

    asyncio.run(_test())


def test_stdin_input():
    async def _test():
        executor = AsyncProcessExecutor()
        config = ProcessConfig(
            cmd=["cat"],
            input_str="streaming input test\nline 2",
            timeout_seconds=5.0,
        )
        res = await executor.execute(config)
        assert res.exit_code == 0
        assert "streaming input test" in res.stdout
        assert "line 2" in res.stdout

    asyncio.run(_test())


def test_timeout_and_process_group_cleanup():
    async def _test():
        executor = AsyncProcessExecutor()
        # Spawn background children that would become zombies if not in process group
        config = ProcessConfig(
            cmd=["sh", "-c", "sleep 10 & sleep 10 & sleep 10"],
            timeout_seconds=0.3,
            run_id="timeout_test_run",
        )
        res = await executor.execute(config)
        assert res.is_timeout is True
        assert res.exit_code != 0
        assert "timed out" in res.stderr.lower()

    asyncio.run(_test())


def test_manual_cancellation():
    async def _test():
        executor = AsyncProcessExecutor()
        config = ProcessConfig(
            cmd=["sh", "-c", "sleep 10"],
            timeout_seconds=10.0,
            run_id="cancel_target_run",
        )

        async def cancel_later():
            await asyncio.sleep(0.2)
            success = await executor.cancel_run("cancel_target_run")
            assert success is True

        exec_task = asyncio.create_task(executor.execute(config))
        cancel_task = asyncio.create_task(cancel_later())

        res, _ = await asyncio.gather(exec_task, cancel_task)
        assert res.is_cancelled is True
        assert res.exit_code != 0

    asyncio.run(_test())


def test_large_output_streaming_to_disk_and_bounded_memory(tmp_path: Path):
    async def _test():
        executor = AsyncProcessExecutor()
        log_file = tmp_path / "proc_large.log"

        # Generate 500,000 characters
        cmd = [sys.executable, "-c", "print('A' * 500000)"]
        config = ProcessConfig(
            cmd=cmd,
            max_memory_output_bytes=2048,
            log_file_path=log_file,
            timeout_seconds=5.0,
        )
        res = await executor.execute(config)

        assert res.exit_code == 0
        assert res.is_truncated is True
        assert res.total_stdout_bytes >= 500000
        # In-memory stdout must be bounded
        assert len(res.stdout.encode("utf-8")) < 5000
        assert "... [TRUNCATED" in res.stdout
        # Disk log file must contain the full output
        assert log_file.exists()
        assert log_file.stat().st_size >= 500000

    asyncio.run(_test())
