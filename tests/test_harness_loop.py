"""Tests for AsyncHarnessLoop: main loop orchestration, 4 pairs of hooks, and cancellation."""
import asyncio
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional
import pytest

from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ToolCallSpec,
)


class MockLLM:
    """Mock LLM returning pre-programmed responses."""

    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.call_history: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.call_history.append({"messages": messages, "tools": tools, "system": system})
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse(content="Default final answer", tool_calls=[])


def test_harness_loop_normal_execution(tmp_path: Path):
    async def _test():
        db_path = tmp_path / "sf.db"
        broker = ToolBroker(application_allowlist={"calc_add"})

        # Register simple tool
        def calc_add(a: int, b: int) -> int:
            return a + b

        broker.register_tool(calc_add, name="calc_add")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # 2-step mock LLM: step 1 calls calc_add(10, 20), step 2 outputs final conclusion
        mock_llm = MockLLM([
            ModelResponse(
                content="I will compute 10 + 20",
                tool_calls=[ToolCallSpec(id="call_1", name="calc_add", arguments={"a": 10, "b": 20})],
            ),
            ModelResponse(content="The sum is 30", tool_calls=[]),
        ])

        hook_events: list[str] = []

        async def pre_model(state: HarnessState):
            hook_events.append(f"pre_model_{state.step_count}")

        async def post_model(state: HarnessState, resp: ModelResponse):
            hook_events.append(f"post_model_{state.step_count}")

        async def pre_tool(state: HarnessState, tool_spec: ToolCallSpec):
            hook_events.append(f"pre_tool_{tool_spec.name}")

        async def post_tool(state: HarnessState, record: Any):
            hook_events.append(f"post_tool_{record.tool_name}_{record.status}")

        async def pre_compress(state: HarnessState):
            hook_events.append(f"pre_compress_{state.step_count}")

        async def post_compress(state: HarnessState, report: Any):
            hook_events.append(f"post_compress_{state.step_count}")

        async def task_end(state: HarnessState):
            hook_events.append(f"task_end_{state.status}")

        hooks = HarnessHooks(
            pre_model_call=pre_model,
            post_model_call=post_model,
            pre_tool_exec=pre_tool,
            post_tool_exec=post_tool,
            pre_compress=pre_compress,
            post_compress=post_compress,
            task_end=task_end,
        )

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace",
        )

        spec = HarnessTaskSpec(
            task_id="task_calc_1",
            user_query="Please add 10 and 20",
            budget_max=5,
        )

        final_state = await loop.run(spec)

        assert final_state.status == "COMPLETED"
        assert final_state.final_output == "The sum is 30"
        assert len(final_state.tool_records) == 1
        assert final_state.tool_records[0].output_text == "30"

        # Verify exact hook sequence
        expected_seq = [
            "pre_model_0",
            "post_model_0",
            "pre_tool_calc_add",
            "post_tool_calc_add_EXECUTED",
            "pre_compress_0",
            "post_compress_0",
            "pre_model_1",
            "post_model_1",
            "task_end_COMPLETED",
        ]
        assert hook_events == expected_seq

    asyncio.run(_test())


def test_harness_loop_budget_exhaustion(tmp_path: Path):
    async def _test():
        db_path = tmp_path / "sf.db"
        broker = ToolBroker(application_allowlist={"ping"})
        broker.register_tool(lambda: "pong", name="ping")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Infinite tool call loop
        infinite_llm = MockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id=f"c_{i}", name="ping", arguments={})])
            for i in range(10)
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=infinite_llm,
            workspace_dir=tmp_path / "workspace",
        )

        spec = HarnessTaskSpec(
            task_id="task_budget_test",
            user_query="Keep calling ping",
            budget_max=3,  # Strict limit
        )

        final_state = await loop.run(spec)
        assert final_state.status == "BUDGET_EXHAUSTED"
        assert final_state.step_count == 3
        assert len(final_state.tool_records) == 3

    asyncio.run(_test())


def test_harness_loop_cancellation(tmp_path: Path):
    async def _test():
        db_path = tmp_path / "sf.db"
        broker = ToolBroker(application_allowlist={"sleep_tool"})

        def slow_tool():
            time.sleep(0.5)
            return "done"

        broker.register_tool(slow_tool, name="sleep_tool")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        mock_llm = MockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_1", name="sleep_tool", arguments={})]),
            ModelResponse(content="Final", tool_calls=[]),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            workspace_dir=tmp_path / "workspace",
        )

        spec = HarnessTaskSpec(
            task_id="task_cancel_test",
            user_query="Run slow tool",
            budget_max=5,
        )

        async def cancel_mid_run():
            await asyncio.sleep(0.1)
            await loop.cancel("User aborted task")

        exec_task = asyncio.create_task(loop.run(spec))
        cancel_task = asyncio.create_task(cancel_mid_run())

        final_state, _ = await asyncio.gather(exec_task, cancel_task)
        assert final_state.status == "CANCELLED"
        assert "User aborted task" in (final_state.error_message or "")

    asyncio.run(_test())


def test_tool_broker_exact_single_call_on_internal_type_error(tmp_path: Path):
    """Verify that ToolBroker inspects parameter binding beforehand and executes target_fn
    EXACTLY ONCE, even if target_fn raises an internal TypeError during execution."""
    db_path = tmp_path / "sf_err.db"
    call_counts = {"buggy_tool": 0}

    def buggy_tool(x: int, y: int):
        call_counts["buggy_tool"] += 1
        # Tool internally causes a TypeError (e.g. invalid string concat with None)
        return "val" + None  # raises TypeError inside body

    broker = ToolBroker(application_allowlist={"buggy_tool"})
    broker.register_tool(buggy_tool, name="buggy_tool")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    run_id = "test_type_error_single_call"
    runtime.start_run(run_id=run_id, task_id="t_err", budget_max=5)
    rec = runtime.execute_tool(run_id, "buggy_tool", {"x": 1, "y": 2})

    assert rec.status == "ERROR"
    assert rec.error_type == "HANDLER_ERROR"
    assert "TypeError" in (rec.error_message or "")
    # MUST be called exactly once, no blind retry on TypeError!
    assert call_counts["buggy_tool"] == 1


def test_harness_loop_checkpoint_and_resume(tmp_path: Path):
    """Verify that AsyncHarnessLoop persists checkpoints to disk and can resume
    execution seamlessly with updated intent constraints."""
    async def _test():
        db_path = tmp_path / "sf_chk.db"
        broker = ToolBroker(application_allowlist={"step1_tool", "step2_tool"})

        def step1_tool():
            return "step1_ok"

        def step2_tool():
            return "step2_ok"

        broker.register_tool(step1_tool, name="step1_tool")
        broker.register_tool(step2_tool, name="step2_tool")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        llm = MockLLM([
            ModelResponse(content="Calling step 1", tool_calls=[ToolCallSpec(id="c_1", name="step1_tool", arguments={})]),
            ModelResponse(content="Calling step 2", tool_calls=[ToolCallSpec(id="c_2", name="step2_tool", arguments={})]),
            ModelResponse(content="Task completed under v2", tool_calls=[]),
        ])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=llm,
            workspace_dir=tmp_path / "workspace",
        )

        run_id = "run_checkpoint_test_01"
        spec1 = HarnessTaskSpec(
            task_id="task_chk",
            user_query="Do step 1 then pause",
            run_id=run_id,
            budget_max=1,  # Exhausts after 1 step
            intent_version="v1",
        )

        state1 = await loop.run(spec1)
        assert state1.status == "BUDGET_EXHAUSTED"
        assert state1.step_count == 1

        chk_path = tmp_path / "workspace" / "runs" / run_id / "checkpoint.json"
        assert chk_path.exists(), "checkpoint.json must be written to disk"
        chk_data = json.loads(chk_path.read_text(encoding="utf-8"))
        assert chk_data["step_count"] == 1
        assert chk_data["intent_version"] == "v1"

        # Now resume the run with updated intent version v2 and more budget
        spec2 = HarnessTaskSpec(
            task_id="task_chk",
            user_query="Continue with step 2 under v2",
            run_id=run_id,
            budget_max=5,
            intent_version="v2",
        )

        state2 = await loop.resume(run_id=run_id, updated_spec=spec2)
        assert state2.status == "COMPLETED"
        assert state2.final_output == "Task completed under v2"
        assert state2.intent_version == "v2"
        assert state2.step_count == 2

    asyncio.run(_test())


