"""Test suite for Phase 1 Pi Agent SDK Bridge & Logistics Vertical Chain.

Strictly verifies Phase 1 acceptance criteria a - f:
- a: Complete multi-package query -> independent Oracle -> Episode with real handler trajectory.
- b: Pi receives frozen body at start; subsequent candidate draft mutations do not taint the run.
- c: Unauthorized refund operation blocked: handler count == 0 and PERMISSION_DENIED recorded.
- d: Subprocess abnormal exit or error frame explicitly fails; never marked business success.
- e: Cancellation passes abort, stops subsequent tool dispatch, and reaps the subprocess cleanly.
- f: Mismatched run_id and duplicate call_id rejected without re-executing handlers/side-effects.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import EpisodeStore
from skillforge.models import (
    CandidateSkill,
    SkillMeta,
    ToolCallRecord,
    Trigger,
)
from skillforge.pi_adapter import (
    PROTOCOL_VERSION,
    PiExecutionResult,
    PiRunnerClient,
    tool_parameters_to_json_schema,
)
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    RefundOrderTool,
    get_refund_call_count,
    reset_refund_call_count,
    verify_logistics_fulfillment,
)


@pytest.fixture
def test_env(tmp_path: Path):
    """Sets up an isolated SQLite runtime, ToolBroker, Registry, and Collector."""
    db_path = tmp_path / "test_runtime.db"
    episodes_db = tmp_path / "episodes.db"

    episode_store = EpisodeStore(db_path=episodes_db)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
    )

    yield {
        "runtime": runtime,
        "broker": broker,
        "collector": collector,
        "episode_store": episode_store,
        "db_path": db_path,
        "tmp_path": tmp_path,
    }

    runtime.close()


def test_a_complete_multi_package_query_oracle_and_episode(test_env):
    """(a) Complete multi-package query -> independent Oracle -> Episode and real handler trace."""
    runtime: AgentRuntime = test_env["runtime"]
    broker: ToolBroker = test_env["broker"]
    collector: ExperienceCollector = test_env["collector"]

    order_tool = QueryOrderPackagesTool()
    pkg_tool = QueryPackageTrackingTool()

    broker.register_tool(order_tool)
    broker.register_tool(pkg_tool)

    run_id = "run_pi_scenario_a"
    task_id = "task_logistics_ord_901"
    order_id = "ORD_2026_0901"

    # Start run in runtime
    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=10,
    )
    collector.start_run(
        run_id=run_id,
        task_id=task_id,
        skill_name="logistics_query",
    )

    # Program realistic multi-turn tool interaction via faux provider
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_1",
                        "name": "query_order_packages",
                        "arguments": {"order_id": order_id},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_2",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_3",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_102"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Order {order_id} has 2 packages: PKG_101 is DELIVERED via Carrier_Alpha, "
                            f"and PKG_102 is IN_TRANSIT via Carrier_Beta."
                        ),
                    }
                ],
                "stopReason": "stop",
            },
        ],
    }

    # Execute with Pi Agent SDK
    final_response = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text=f"Check all packages and their tracking status for {order_id}",
        tools=["query_order_packages", "query_package_tracking"],
        model_config=model_config,
        timeout=25.0,
    )

    assert "PKG_101" in final_response
    assert "PKG_102" in final_response

    # Verify tool dispatches in runtime
    provenances = runtime.get_provenances(run_id)
    assert len(provenances) == 3
    tool_names = [p.tool_name for p in provenances]
    assert tool_names == ["query_order_packages", "query_package_tracking", "query_package_tracking"]

    # Finalize collector
    episode = collector.finish_run(
        run_id=run_id,
        model_output=final_response,
    )
    assert episode is not None
    assert len(episode.provenances) >= 3

    # Independent Oracle verification
    tool_records: list[ToolCallRecord] = []
    conn = runtime._get_conn()
    for row in conn.execute(
        """SELECT call_id, run_id, tool_name, status, input_params_json,
                  output_text, output_data_json, error_type, error_message,
                  latency_ms, created_at FROM runtime_tool_calls WHERE run_id = ?""",
        (run_id,),
    ).fetchall():
        tool_records.append(
            ToolCallRecord(
                call_id=row[0],
                run_id=row[1],
                tool_name=row[2],
                status=row[3],
                input_params=json.loads(row[4]) if row[4] else {},
                output_text=row[5] or "",
                output_data=json.loads(row[6]) if row[6] else {},
                error_type=row[7],
                error_message=row[8],
                latency_ms=row[9] or 0.0,
                created_at=row[10] or "",
            )
        )

    oracle_findings = verify_logistics_fulfillment(
        model_output=final_response,
        order_id=order_id,
        tool_records=tool_records,
    )
    assert oracle_findings.get("independent_pass") is True
    assert oracle_findings.get("verdict") == "PASS"
    assert oracle_findings.get("classification") == "TRUE_POSITIVE"


def test_b_frozen_body_isolated_from_subsequent_draft_mutations(test_env):
    """(b) Pi receives frozen body at start; subsequent draft modifications do not taint old run."""
    runtime: AgentRuntime = test_env["runtime"]
    broker: ToolBroker = test_env["broker"]

    candidate = CandidateSkill(
        candidate_id="cand_freeze_1",
        skill_name="frozen_test_skill",
        decision="create",
        source_requirement="Testing frozen body snapshot requirement",
        body="FROZEN_RULE_INITIAL_VERSION_XYZ",
        meta=SkillMeta(
            name="frozen_test_skill",
            version="0.1.0",
            description="Testing frozen body snapshot",
            use_when="Testing frozen body",
            dependencies=[],
        ),
    )

    run_id = "run_pi_frozen_check"
    task_id = "task_frozen_001"

    # Start run binding initial candidate
    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        candidate=candidate,
        purpose="evaluation",
    )

    # Mutate candidate in memory
    candidate.body = "MUTATED_DRAFT_CORRUPTED_VERSION_ABC"

    # Script faux provider to confirm system prompt received
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Acknowledged"}],
                "stopReason": "stop",
            }
        ],
    }

    # Execute Pi
    runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Begin task",
        candidate=candidate,
        model_config=model_config,
    )

    # Retrieve frozen body from runtime authoritative record
    run_record = runtime.get_run(run_id)
    assert run_record is not None
    assert run_record.frozen_body == "FROZEN_RULE_INITIAL_VERSION_XYZ"
    assert "MUTATED" not in run_record.frozen_body

    run_body = runtime.get_run_body(skill_name=candidate.skill_name, run_id=run_id)
    assert run_body == "FROZEN_RULE_INITIAL_VERSION_XYZ"


def test_c_unauthorized_refund_blocked_handler_zero_and_rejected_record(test_env):
    """(c) Refund unauthorized tool handler=0 and rejected record."""
    runtime: AgentRuntime = test_env["runtime"]
    broker: ToolBroker = test_env["broker"]

    reset_refund_call_count()
    assert get_refund_call_count() == 0

    refund_tool = RefundOrderTool()
    # Register in broker without authorizing mode or allowlist
    broker.register_tool(refund_tool)

    run_id = "run_pi_unauthorized_refund"
    task_id = "task_logistics_refund"

    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        skill_required_tools={"query_order_packages"},  # refund_order explicitly excluded
    )

    # Script faux provider to attempt calling refund_order
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_unauth_1",
                        "name": "refund_order",
                        "arguments": {"order_id": "ORD_123", "amount": 100.0},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Refund failed due to lack of administrative permissions.",
                    }
                ],
                "stopReason": "stop",
            },
        ],
    }

    final_resp = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Please issue an immediate refund for order ORD_123",
        tools=["refund_order"],
        model_config=model_config,
    )

    # 1. Handler was NEVER called
    assert get_refund_call_count() == 0

    # 2. Tool call record in SQLite shows REJECTED with PERMISSION_DENIED
    conn = runtime._get_conn()
    row = conn.execute(
        "SELECT status, error_type FROM runtime_tool_calls WHERE run_id = ? AND tool_name = 'refund_order'",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "REJECTED"
    assert row[1] == "PERMISSION_DENIED"


def test_d_subprocess_failure_or_error_frame_not_marked_success(test_env):
    """(d) Subprocess exception / error frame not treated as success."""
    runtime: AgentRuntime = test_env["runtime"]

    run_id = "run_pi_fatal_error"
    task_id = "task_fatal_01"

    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
    )

    # Send unsupported model provider to trigger error frame from runner
    model_config = {
        "provider": "unsupported_malformed_provider",
    }

    with pytest.raises(RuntimeError) as exc_info:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Hello",
            model_config=model_config,
            timeout=5.0,
        )

    assert "Unsupported model provider" in str(exc_info.value) or "error" in str(exc_info.value).lower()

    # Verify run record status is NOT COMPLETED
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status != "COMPLETED"


def test_e_cancellation_stops_dispatch_and_reaps_process(test_env):
    """(e) Cancellation yields no new handlers and subprocess reaped."""
    async def _test():
        runtime: AgentRuntime = test_env["runtime"]
        broker: ToolBroker = test_env["broker"]

        pkg_tool = QueryPackageTrackingTool()
        broker.register_tool(pkg_tool)

        run_id = "run_pi_cancel_test"
        task_id = "task_cancel_01"

        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            purpose="evaluation",
        )

        handler_invocations = 0

        async def counting_tool_handler(r_id, t_name, params, c_id):
            nonlocal handler_invocations
            handler_invocations += 1
            # Trigger runtime cancel during first tool dispatch
            runtime.cancel_run(r_id, reason="Test cancellation mid-run")
            await asyncio.sleep(0.05)
            return {"status": "COMPLETED", "output": "Package PKG_101 is delivered"}

        client = PiRunnerClient()
        await client.start()
        assert client.is_running is True

        # Setup 2 consecutive tool calls
        model_config = {
            "provider": "faux",
            "responses": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "tc_c1",
                            "name": "query_package_tracking",
                            "arguments": {"package_id": "PKG_101"},
                        }
                    ],
                    "stopReason": "toolUse",
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "tc_c2",
                            "name": "query_package_tracking",
                            "arguments": {"package_id": "PKG_102"},
                        }
                    ],
                    "stopReason": "toolUse",
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done"}],
                    "stopReason": "stop",
                },
            ],
        }

        tools_schema = [
            {
                "name": "query_package_tracking",
                "description": "Query package",
                "parameters": tool_parameters_to_json_schema(pkg_tool.get_parameters()),
            }
        ]

        await client.init_run(
            run_id=run_id,
            system_prompt="Test system prompt",
            tools=tools_schema,
            model_config=model_config,
            tool_handler=counting_tool_handler,
        )
        runtime._active_pi_clients[run_id] = client

        # Launch prompt which triggers tool execution
        prompt_task = asyncio.create_task(client.prompt(run_id=run_id, text="Check package status"))
        try:
            await asyncio.wait_for(prompt_task, timeout=5.0)
        except (Exception, asyncio.CancelledError):
            pass

        # Ensure only the first handler ran before cancel; second was denied
        assert handler_invocations == 1

        # Clean up subprocess
        exit_code = await client.close(grace_period=2.0)
        assert client.is_running is False
        assert exit_code == 0

        # Verify run record status in runtime
        run = runtime.get_run(run_id)
        assert run is not None
        assert run.status == "CANCELLED"

    asyncio.run(_test())


def test_f_duplicate_and_cross_run_messages_rejected_without_side_effects():
    """(f) Duplicate / cross-run messages rejected, no duplicate side-effects."""
    async def _test():
        client = PiRunnerClient()
        await client.start()

        run_id = "run_pi_isolation"
        handler_call_count = 0

        async def counting_handler(r_id, t_name, params, c_id):
            nonlocal handler_call_count
            handler_call_count += 1
            return {"status": "COMPLETED", "output": f"Executed {c_id}"}

        await client.init_run(
            run_id=run_id,
            system_prompt="Test",
            tools=[],
            tool_handler=counting_handler,
        )

        # 1. Dispatch valid call_1
        await client._dispatch_frame({
            "protocol": PROTOCOL_VERSION,
            "type": "tool_call",
            "run_id": run_id,
            "call_id": "call_1",
            "tool_name": "test_tool",
            "parameters": {},
        })
        assert handler_call_count == 1

        # 2. Dispatch duplicate call_1 -> must be rejected without incrementing handler
        await client._dispatch_frame({
            "protocol": PROTOCOL_VERSION,
            "type": "tool_call",
            "run_id": run_id,
            "call_id": "call_1",
            "tool_name": "test_tool",
            "parameters": {},
        })
        assert handler_call_count == 1  # No duplicate execution!

        # 3. Dispatch cross-run call -> must be rejected without incrementing handler
        await client._dispatch_frame({
            "protocol": PROTOCOL_VERSION,
            "type": "tool_call",
            "run_id": "different_alien_run_id",
            "call_id": "call_2",
            "tool_name": "test_tool",
            "parameters": {},
        })
        assert handler_call_count == 1  # No cross-run execution!

        await client.close()
        assert client.is_running is False

    asyncio.run(_test())
