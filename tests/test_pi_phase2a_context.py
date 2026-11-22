"""Phase 2A Context Integration Test Suite.

Verifies:
1. Large tool output offloaded to disk artifact -> next Pi provider input contains
   manifest/drill-down reference -> Broker inspect_artifact retrieves item & denies cross-run.
2. Two compressions increase epoch (1 -> 2 -> 3), frozen goal and constraints + full call-result
   pairs actually sent to model; token count is client estimated, not KV cache.
3. Async summarizer in-flight intent update -> stale candidate rejected without overwriting new state;
   cancellation halts dispatch.
4. Below watermark -> no summarizer call, no duplicate budget deduction, duplicate tool call rejected,
   pre/post compression hooks execute normally.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.context_engine.artifact_tool import InspectArtifactTool
from skillforge.context_engine.compressor import ContextCompressor
from skillforge.context_engine.schemas import ProtectedState
from skillforge.episode import EpisodeStore
from skillforge.harness_loop import HarnessHooks, HarnessState
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)


@pytest.fixture
def phase2a_env(tmp_path: Path):
    db_path = tmp_path / "test_runtime.db"
    episodes_db = tmp_path / "episodes.db"

    episode_store = EpisodeStore(db_path=episodes_db)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking", "inspect_artifact"})
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
        "tmp_path": tmp_path,
    }

    runtime.close()


def test_phase2a_scenario1_large_output_offloaded_provider_input_and_inspect_artifact(phase2a_env):
    """Scenario 1: Large tool output offloaded -> next Pi provider input contains manifest/drill-down reference -> Broker inspect_artifact retrieves item & denies cross-run."""
    runtime: AgentRuntime = phase2a_env["runtime"]
    broker: ToolBroker = phase2a_env["broker"]
    tmp_path: Path = phase2a_env["tmp_path"]

    order_tool = QueryOrderPackagesTool()
    inspect_tool = InspectArtifactTool(allowed_base_dir=tmp_path)
    broker.register_tool(order_tool)
    broker.register_tool(inspect_tool)

    run_id = "run_p2a_scenario_1"
    task_id = "task_p2a_01"

    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=10,
    )

    # Low watermark to trigger Level 0 offloading on the 3 packages result
    compressor = ContextCompressor(
        trigger_watermark_tokens=50,
        target_watermark_tokens=30,
        hard_capacity_tokens=5000,
        offload_threshold_chars=200,
        artifacts_dir=tmp_path / "artifacts",
    )

    # Multi-turn faux response:
    # Turn 1: toolCall to query_order_packages for ORD_DEV_0102 (has 3 packages: PKG_D103, PKG_D104, PKG_D105)
    # Turn 2: final assistant answer
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_s1_packages",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_DEV_0102"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Packages checked and offloaded."}],
                "stopReason": "stop",
            },
        ],
    }

    final_text = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Track order packages for ORD_DEV_0102",
        tools=["query_order_packages"],
        model_config=model_config,
        test_mode=True,
        context_compressor=compressor,
        workspace_dir=tmp_path,
        timeout=10.0,
    )

    assert "Packages checked and offloaded." in final_text

    # Extract captured provider inputs from Pi execution result
    last_res = runtime.get_last_pi_result(run_id)
    assert last_res is not None
    assert len(last_res.captured_provider_inputs) >= 2

    # The 2nd provider input was sent to model before Turn 2
    turn_2_provider_input = last_res.captured_provider_inputs[1]
    sys_prompt = turn_2_provider_input.get("systemPrompt", "")
    messages = turn_2_provider_input.get("messages", [])

    # 1. Provider input systemPrompt must contain Layer 3 MemoryIndex manifest
    assert "[ATTACHMENTS_INDEX]" in sys_prompt
    assert "inspect_artifact" in sys_prompt
    assert "fs://" in sys_prompt
    assert "PKG_D103" in sys_prompt

    # 2. Provider input messages must contain the in-context offloaded summary
    tool_msg = next((m for m in messages if m.get("role") in ("toolResult", "tool")), None)
    assert tool_msg is not None
    content_str = json.dumps(tool_msg.get("content"))
    assert "OBJECT_AWARE_OFFLOADED_SUMMARY" in content_str
    assert "PKG_D103" in content_str

    # 3. Dynamic manifest binding to Broker for inspect_artifact
    harness_state = runtime.get_harness_state(run_id)
    assert harness_state is not None
    memory_idx = harness_state.working_scratchpad.get("memory_index")
    assert memory_idx is not None and len(memory_idx.attachments) > 0
    ref_uri = memory_idx.attachments[0].ref_id

    current_active_run = run_id

    def get_run_manifest():
        if current_active_run == run_id:
            return [att.ref_id for att in harness_state.working_scratchpad.get("memory_index").attachments]
        return []

    inspect_tool.bind_run_manifest_fn(get_run_manifest)

    # 4. Same run: retrieve item successfully via Broker inspect_artifact
    rec_ok = runtime.execute_tool(
        run_id=run_id,
        tool_name="inspect_artifact",
        parameters={"ref_id": ref_uri, "object_id": "PKG_D103"},
    )
    assert rec_ok.status == "EXECUTED"
    assert "PKG_D103" in rec_ok.output_text

    # 5. Cross-run boundary: different run rejected as UNAUTHORIZED_ARTIFACT
    cross_run_id = "run_cross_run_denied"
    current_active_run = cross_run_id
    runtime.start_run(run_id=cross_run_id, task_id="task_other", purpose="evaluation")
    rec_denied = runtime.execute_tool(
        run_id=cross_run_id,
        tool_name="inspect_artifact",
        parameters={"ref_id": ref_uri, "object_id": "PKG_D103"},
    )
    assert rec_denied.status == "ERROR"
    assert rec_denied.error_type == "UNAUTHORIZED_ARTIFACT"


def test_phase2a_scenario2_multi_step_compression_epoch_frozen_state_client_tokens(phase2a_env):
    """Scenario 2: Two compressions increase epoch (1 -> 2 -> 3), frozen goal & constraints + full call-result pairs actually sent to model; token count is client estimated."""
    runtime: AgentRuntime = phase2a_env["runtime"]
    broker: ToolBroker = phase2a_env["broker"]
    tmp_path: Path = phase2a_env["tmp_path"]

    order_tool = QueryOrderPackagesTool()
    pkg_tool = QueryPackageTrackingTool()
    broker.register_tool(order_tool)
    broker.register_tool(pkg_tool)

    run_id = "run_p2a_scenario_2"
    task_id = "task_p2a_02"

    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=10,
    )

    # Initial protected state with authoritative constraints
    init_protected = ProtectedState(
        intent_version="v1",
        current_goal="Query logistics status strictly",
        active_constraints=["STATUS_ONLY", "NO_REFUND"],
        allowed_tools=["query_order_packages", "query_package_tracking"],
    )

    # Watermarks configured so each tool turn triggers compression
    compressor = ContextCompressor(
        trigger_watermark_tokens=50,
        target_watermark_tokens=30,
        hard_capacity_tokens=5000,
        offload_threshold_chars=200,
        artifacts_dir=tmp_path / "artifacts",
    )

    # Sequence of 2 tool turns + 1 final turn:
    # Turn 1: query_order_packages on ORD_DEV_0102 (3 packages -> triggers offload, epoch 1 -> 2)
    # Turn 2: query_order_packages on ORD_DEV_0203 (3 packages -> triggers offload, epoch 2 -> 3)
    # Turn 3: final text
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_s2_order1",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_DEV_0102"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_s2_order2",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_DEV_0203"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Status query complete"}],
                "stopReason": "stop",
            },
        ],
    }

    final_text = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Check order ORD_DEV_0102 and ORD_DEV_0203",
        tools=["query_order_packages", "query_package_tracking"],
        model_config=model_config,
        test_mode=True,
        context_compressor=compressor,
        initial_scratchpad={"protected_state": init_protected},
        goal="Query logistics status strictly",
        intent_version="v1",
        active_constraints=["STATUS_ONLY", "NO_REFUND"],
        workspace_dir=tmp_path,
        timeout=10.0,
    )

    assert "Status query complete" in final_text

    # 1. Verify epoch increased after two compression stages
    harness_state = runtime.get_harness_state(run_id)
    assert harness_state is not None
    assert harness_state.context_epoch >= 3  # Started at 1, incremented across turns

    # 2. Verify provider inputs captured across turns
    last_res = runtime.get_last_pi_result(run_id)
    assert last_res is not None
    assert len(last_res.captured_provider_inputs) >= 3

    # Check Turn 3 provider input (sent to model after two tool turns and two compressions)
    turn_3_input = last_res.captured_provider_inputs[2]
    sys_prompt = turn_3_input.get("systemPrompt", "")
    messages = turn_3_input.get("messages", [])

    # Authoritative goal and constraints strictly preserved at head
    assert "[PROTECTED_SYSTEM_STATE (AUTHORITATIVE)]" in sys_prompt
    assert "current_goal: Query logistics status strictly" in sys_prompt
    assert "- STATUS_ONLY" in sys_prompt
    assert "- NO_REFUND" in sys_prompt

    # Tool calls and results are fully paired in messages
    call_ids = set()
    result_ids = set()
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("content", []):
                if c.get("type") == "toolCall":
                    call_ids.add(c.get("id"))
        elif m.get("role") in ("toolResult", "tool"):
            t_id = m.get("toolCallId") or m.get("tool_call_id")
            if t_id:
                result_ids.add(t_id)

    # Every executed call has matching result, no orphans
    for cid in call_ids:
        assert cid in result_ids

    # 3. Token estimation is client-side, usage has no false KV hit
    assert compressor.rebuilder.count_messages_tokens(messages) > 0
    if last_res.usage:
        assert last_res.usage.get("cacheRead") is None or last_res.usage.get("cacheRead") == 0


def test_phase2a_scenario3_async_summarizer_intent_drift_rejected_and_cancel_stops(phase2a_env):
    """Scenario 3: Async summarizer detects mid-flight intent drift -> rejects stale candidate without corrupting state; cancellation stops subsequent dispatch."""
    runtime: AgentRuntime = phase2a_env["runtime"]
    broker: ToolBroker = phase2a_env["broker"]
    tmp_path: Path = phase2a_env["tmp_path"]

    pkg_tool = QueryPackageTrackingTool()
    broker.register_tool(pkg_tool)

    # Part A: Async summarizer intent drift rejection
    class DriftingSummarizerClient:
        def __init__(self, target_state):
            self.target_state = target_state

        async def ainvoke(self, messages, tools, system):
            # Mid-flight intent drift: state changes while summarizer LLM was asynchronously executing
            self.target_state.intent_version = "v2"
            self.target_state.goal = "New redirected goal v2"
            await asyncio.sleep(0.05)
            return json.dumps({
                "verified_facts": [{"fact": "Observed drift fact", "evidence_ref": "historical_turn_0"}],
                "failed_attempts": [],
                "open_questions": [],
            })

    test_state = HarnessState(
        run_id="run_drift_test",
        task_id="task_drift",
        intent_version="v1",
        goal="Original goal v1",
        system_prompt="Base prompt",
        messages=[
            {"role": "user", "content": "Query package 101"},
            {"role": "assistant", "content": "Calling tracking", "tool_calls": [{"id": "c1", "type": "function", "name": "query_package_tracking", "function": {"name": "query_package_tracking", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "tool_name": "query_package_tracking", "content": "Package in transit to transit hub with extra bulky details" * 15},
            {"role": "user", "content": "Now check package 102"},
            {"role": "assistant", "content": "Done"},
        ],
        step_count=2,
    )

    summarizer = DriftingSummarizerClient(test_state)
    compressor = ContextCompressor(
        trigger_watermark_tokens=50,
        target_watermark_tokens=30,
        summarizer_client=summarizer,
        recent_turns_to_keep=1,
        artifacts_dir=tmp_path / "artifacts",
    )

    report = asyncio.run(compressor.compress(test_state))

    # Stale candidate summary is rejected with deprecated_stale_summary
    assert report.level == "rejected_stale_intent"
    assert report.deprecated_stale_summary is True
    # The new state was NOT overwritten by the stale summary candidate
    assert test_state.intent_version == "v2"
    assert test_state.goal == "New redirected goal v2"

    # Part B: Cancellation halts dispatch and marks CANCELLED
    cancel_run_id = "run_p2a_cancel_stops"
    runtime.start_run(run_id=cancel_run_id, task_id="task_cancel", purpose="evaluation")

    orig_pkg_run = pkg_tool.run

    def cancelling_tool_run(params):
        runtime.cancel_run(cancel_run_id, reason="User requested cancel mid-flight")
        return orig_pkg_run(params)

    pkg_tool.run = cancelling_tool_run

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_s3_cancel",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Post cancel message should not execute"}],
                "stopReason": "stop",
            },
        ],
    }

    try:
        runtime.run_agent_with_pi(
            run_id=cancel_run_id,
            input_text="Track package PKG_101",
            tools=["query_package_tracking"],
            model_config=model_config,
            test_mode=True,
            timeout=5.0,
        )
    except (Exception, asyncio.CancelledError, BaseException):
        pass

    run = runtime.get_run(cancel_run_id)
    assert run is not None
    assert run.status == "CANCELLED"
    assert run.terminal_at is not None


def test_phase2a_scenario4_under_watermark_no_compression_no_duplicate_budget_hooks_active(phase2a_env):
    """Scenario 4: Below watermark -> no summarizer call, no duplicate budget deduction, duplicate tool call rejected, pre/post compression hooks execute."""
    runtime: AgentRuntime = phase2a_env["runtime"]
    broker: ToolBroker = phase2a_env["broker"]
    tmp_path: Path = phase2a_env["tmp_path"]

    order_tool = QueryOrderPackagesTool()
    broker.register_tool(order_tool)

    run_id = "run_p2a_scenario_4"
    task_id = "task_p2a_04"

    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=10,
    )

    # Watermark set very high (50,000 tokens) -> will NOT trigger compression
    compressor = ContextCompressor(
        trigger_watermark_tokens=50000,
        target_watermark_tokens=25000,
        artifacts_dir=tmp_path / "artifacts",
    )

    pre_compress_called = 0
    post_compress_called = 0

    async def _pre_hook(state):
        nonlocal pre_compress_called
        pre_compress_called += 1

    async def _post_hook(state, report):
        nonlocal post_compress_called
        post_compress_called += 1
        assert report.triggered is False
        assert report.level == "none"

    hooks = HarnessHooks(
        pre_compress=_pre_hook,
        post_compress=_post_hook,
    )

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_s4_single",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "All done under watermark"}],
                "stopReason": "stop",
            },
        ],
    }

    final_text = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Track order ORD_2026_0901",
        tools=["query_order_packages"],
        model_config=model_config,
        test_mode=True,
        context_compressor=compressor,
        hooks=hooks,
        workspace_dir=tmp_path,
        timeout=10.0,
    )

    assert "All done under watermark" in final_text

    # 1. Pre and post compress hooks executed
    assert pre_compress_called >= 1
    assert post_compress_called >= 1

    # 2. Budget: exactly 2 model turns + 1 tool call consumed, no duplicate deduction
    run = runtime.get_run(run_id)
    assert run is not None
    # 2 model turns + 1 tool call = 3 tickets consumed
    assert run.budget_consumed == 3

    # 3. Duplicate tool call ID rejection without handler re-execution
    call_rec_dup = runtime.execute_tool(
        run_id=run_id,
        tool_name="query_order_packages",
        parameters={"order_id": "ORD_2026_0901"},
        call_id="tc_s4_single",
    )
    # Returns existing completed record without re-executing
    assert call_rec_dup.call_id == "tc_s4_single"
    assert call_rec_dup.status == "EXECUTED"
