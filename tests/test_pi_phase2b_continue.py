"""Phase 2B R3a: Python to Pi continue protocol integration test.

Verifies:
1. Spawns real official Pi npm runner subprocess (not a fake runner).
2. Initializes run with pre-existing message history containing:
   - User message
   - Assistant message with toolCall (tc_history_001)
   - Completed toolResult message (tc_history_001)
3. Issues `continue_run` without sending new user prompt or replaying tools.
4. Python tool handler invocation count is strictly 0 (no replay).
5. Provider input captured inside Pi contains the old toolCall ID and toolResult.
6. No duplicate user message is added.
7. Model final response text is received and matches the expected continuation.
8. Subprocess is reaped and cleanly terminated (exit code 0).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from skillforge.pi_adapter import (
    PiExecutionResult,
    PiRunnerClient,
    PROTOCOL_VERSION,
)


def test_continue_from_existing_history_without_replaying_tools():
    """Verify resuming execution from pre-existing toolCall/toolResult history via Pi continue."""
    async def _test():
        client = PiRunnerClient()
        await client.start()

        run_id = "run_phase2b_continue_001"
        tool_handler_invocations = 0

        # Define tool schema for query_order_packages
        tools_schema = [
            {
                "name": "query_order_packages",
                "description": "Query package IDs for an order",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "Order ID"}
                    },
                    "required": ["order_id"],
                },
            }
        ]

        async def counting_tool_handler(r_id: str, tool_name: str, params: dict[str, Any], call_id: str) -> dict[str, Any]:
            nonlocal tool_handler_invocations
            tool_handler_invocations += 1
            return {"status": "COMPLETED", "output": "should_not_be_called"}

        # Scripted faux provider for the continuation turn
        continuation_answer = "All packages PKG_101 and PKG_102 have been delivered to recipient."
        model_config = {
            "provider": "faux",
            "test_mode": True,
            "responses": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": continuation_answer}],
                    "stopReason": "stop",
                }
            ],
        }

        # Pre-existing matched history:
        # Turn 1: user asked to check order
        # Turn 2: assistant called query_order_packages (tc_history_001)
        # Turn 3: toolResult returned for tc_history_001
        initial_history = [
            {
                "role": "user",
                "content": "Check order packages for ORD_CONTINUE_901",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_history_001",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_CONTINUE_901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "toolResult",
                "toolCallId": "tc_history_001",
                "toolName": "query_order_packages",
                "content": json.dumps({"packages": ["PKG_101", "PKG_102"], "status": "DELIVERED"}),
            },
        ]

        # 1. Initialize runner with pre-existing history and tool handler
        await client.init_run(
            run_id=run_id,
            system_prompt="You are a logistics assistant.",
            tools=tools_schema,
            model_config=model_config,
            tool_handler=counting_tool_handler,
            is_test=True,
            initial_messages=initial_history,
        )

        child_pid = client.pid
        assert child_pid is not None and child_pid > 0, "Subprocess must have a valid PID"

        # 2. Invoke continue_run without sending any new prompt
        result: PiExecutionResult = await client.continue_run(run_id=run_id)

        # 3. Assertions
        # (a) No replay: Tool handler was NOT executed (history was already completed)
        assert tool_handler_invocations == 0, f"Expected 0 tool handler invocations, got {tool_handler_invocations}"

        # (b) Correct final response received
        assert result.text == continuation_answer, f"Expected final answer '{continuation_answer}', got '{result.text}'"
        assert result.stop_reason == "stop"

        # (c) Provider input verification
        assert len(result.captured_provider_inputs) >= 1, "Expected at least one captured provider input turn"
        last_provider_input = result.captured_provider_inputs[-1]
        input_messages = last_provider_input.get("messages", [])

        # Check: Contains old toolCall ID and toolResult
        found_tool_call = False
        found_tool_result = False
        user_msg_count = 0

        for msg in input_messages:
            role = msg.get("role")
            if role == "user":
                user_msg_count += 1
            elif role == "assistant":
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "toolCall" and block.get("id") == "tc_history_001":
                        found_tool_call = True
            elif role in ("toolResult", "tool"):
                call_id = msg.get("toolCallId") or msg.get("tool_call_id")
                if call_id == "tc_history_001":
                    found_tool_result = True

        assert found_tool_call is True, "Captured provider input must preserve old toolCall ID 'tc_history_001'"
        assert found_tool_result is True, "Captured provider input must preserve old toolResult for 'tc_history_001'"
        assert user_msg_count == 1, f"Expected exactly 1 user message (no duplicate prompt appended), got {user_msg_count}"

        # 4. Clean process reaping & lifecycle termination
        exit_code = await client.close(grace_period=2.0)
        assert client.is_running is False
        assert exit_code == 0

        # Verify child process is dead
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

    asyncio.run(_test())
