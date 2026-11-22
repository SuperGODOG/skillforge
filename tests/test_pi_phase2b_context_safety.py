"""Tests for real Pi transformContext async canonical drift and compression hooks (Phase 2B B3b2)."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from skillforge import pi_adapter
from skillforge.collector import ExperienceCollector
from skillforge.context_engine.compressor import ContextCompressor
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.harness_loop import HarnessHooks, HookDecision
from skillforge.models import TaskContext
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import QueryOrderPackagesTool
from skillforge.task_context import revise_task_context
from tests.test_pi_phase2b_recovery import assert_reaped, cfg, paused_factory, remaining


class GateSummarizerClient:
    """Gate client holding the summarizer until test triggers external canonical drift."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def ainvoke(self, *args, **kwargs) -> str:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return json.dumps({
            "verified_facts": [],
            "failed_attempts": [],
            "open_questions": ["STALE_SUMMARY_MARKER"],
        })


def test_async_drift_during_compression_blocks_stale_summary_and_preserves_new_intent(
    tmp_path: Path, monkeypatch
):
    """Case 1: Real Pi async summarization in-flight canonical intent drift.

    Guarantees:
    - Real Pi runner client subprocess and real ContextCompressor L2 summarization.
    - During async summarization wait, canonical TaskContext is revised via revise_task_context API.
    - Checkpoint / context guard rejects stale summary with STALE_INTENT.
    - Summary was invoked exactly once, but turn budget is 0 (not consumed for model turn).
    - Tool handler calls == 0.
    - Last result captured_provider_inputs == [].
    - CandidateStore retains new goal, revision 2, and constraints (not overwritten by old summary).
    - No success Episode created.
    - Subprocess PID is cleanly reaped (os.kill raises ProcessLookupError).
    """
    pids = []
    original_start = pi_adapter.PiRunnerClient.start

    async def observed_start(client):
        await original_start(client)
        pids.append(client.pid)

    monkeypatch.setattr(pi_adapter.PiRunnerClient, "start", observed_start)

    async def _run_test():
        task_id = "task_p2b_async_drift"
        run_id = "run_p2b_async_drift"

        cand_store = CandidateStore(db_path=tmp_path / "candidates.db")
        init_tc = TaskContext(
            task_id=task_id,
            intent_revision=1,
            goal="Original logistics goal: check packages",
            constraints=[],
        )
        init_tc.contract_fingerprint = init_tc.compute_fingerprint()
        cand_store.save_task_context(init_tc)

        order_tool_calls = 0

        class TrackedOrderTool(QueryOrderPackagesTool):
            def run(self, *args, **kwargs):
                nonlocal order_tool_calls
                order_tool_calls += 1
                return super().run(*args, **kwargs)

        broker = ToolBroker(application_allowlist={"query_order_packages"})
        broker.register_tool(TrackedOrderTool())

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        episodes = EpisodeStore(db_path=tmp_path / "episodes.db")
        registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
        collector = ExperienceCollector(episode_store=episodes, registry=registry)
        runtime = AgentRuntime(
            db_path=tmp_path / "runtime.db",
            tool_broker=broker,
            registry=registry,
            collector=collector,
            episode_store=episodes,
            candidate_store=cand_store,
        )

        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            purpose="evaluation",
            budget_max=10,
            deadline_ts=time.time() + 300,
        )
        collector.start_run(
            run_id=run_id,
            task_id=task_id,
            skill_name="logistics_query",
        )

        model_config = cfg([
            {"role": "assistant", "content": [{"type": "text", "text": "SHOULD_NOT_REACH_PROVIDER"}], "stopReason": "stop"}
        ])

        gate_client = GateSummarizerClient()
        compressor = ContextCompressor(
            trigger_watermark_tokens=20,
            target_watermark_tokens=10,
            recent_turns_to_keep=0,
            offload_threshold_chars=100000,
            hard_capacity_tokens=10000,
            artifacts_dir=tmp_path / "artifacts",
            summarizer_client=gate_client,
        )

        input_text = "Previous observation detail. " * 1000

        task = asyncio.create_task(
            runtime.arun_agent_with_pi(
                run_id=run_id,
                input_text=input_text,
                tools=["query_order_packages"],
                model_config=model_config,
                test_mode=True,
                context_compressor=compressor,
                workspace_dir=tmp_path,
                timeout=15,
            )
        )

        try:
            await asyncio.wait_for(gate_client.entered.wait(), timeout=5.0)
            revised_ctx, _ = revise_task_context(
                init_tc,
                new_goal="New redirected logistics goal",
                new_constraints=["STATUS_ONLY"],
                candidate_store=cand_store,
            )
            assert revised_ctx.intent_revision == 2
        finally:
            gate_client.release.set()

        with pytest.raises(Exception) as exc_info:
            await task

        assert "STALE_INTENT" in str(exc_info.value)
        assert gate_client.calls == 1

        rec = runtime.get_run(run_id)
        assert rec.budget_consumed == 0
        assert order_tool_calls == 0

        last_pi_result = runtime.get_last_pi_result(run_id)
        if last_pi_result is not None:
            assert getattr(last_pi_result, "captured_provider_inputs", []) == []

        stored_tc = cand_store.get_task_context(task_id)
        assert stored_tc is not None
        assert stored_tc.intent_revision == 2
        assert stored_tc.goal == "New redirected logistics goal"
        assert stored_tc.constraints == ["STATUS_ONLY"]

        ep = episodes.get_episode("ep_" + run_id)
        assert ep is None or ep.outcome != "success"

        assert len(pids) == 1
        assert_reaped(pids[0])
        runtime.close()

    asyncio.run(_run_test())


def test_async_drift_via_contract_fingerprint_drift_blocks_stale_summary(
    tmp_path: Path, monkeypatch
):
    """Case 1b: TaskContext goal/constraints fingerprint drifts without revision bump."""
    pids = []
    original_start = pi_adapter.PiRunnerClient.start

    async def observed_start(client):
        await original_start(client)
        pids.append(client.pid)

    monkeypatch.setattr(pi_adapter.PiRunnerClient, "start", observed_start)

    async def _run_test():
        task_id = "task_p2b_fp_drift"
        run_id = "run_p2b_fp_drift"

        cand_store = CandidateStore(db_path=tmp_path / "candidates.db")
        init_tc = TaskContext(
            task_id=task_id,
            intent_revision=1,
            goal="Initial goal before contract drift",
            constraints=[],
        )
        init_tc.contract_fingerprint = init_tc.compute_fingerprint()
        cand_store.save_task_context(init_tc)

        order_tool_calls = 0

        class TrackedOrderTool(QueryOrderPackagesTool):
            def run(self, *args, **kwargs):
                nonlocal order_tool_calls
                order_tool_calls += 1
                return super().run(*args, **kwargs)

        broker = ToolBroker(application_allowlist={"query_order_packages"})
        broker.register_tool(TrackedOrderTool())

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        episodes = EpisodeStore(db_path=tmp_path / "episodes.db")
        registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
        collector = ExperienceCollector(episode_store=episodes, registry=registry)
        runtime = AgentRuntime(
            db_path=tmp_path / "runtime.db",
            tool_broker=broker,
            registry=registry,
            collector=collector,
            episode_store=episodes,
            candidate_store=cand_store,
        )

        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            purpose="evaluation",
            budget_max=10,
            deadline_ts=time.time() + 300,
        )

        model_config = cfg([
            {"role": "assistant", "content": [{"type": "text", "text": "SHOULD_NOT_REACH_PROVIDER"}], "stopReason": "stop"}
        ])

        gate_client = GateSummarizerClient()
        compressor = ContextCompressor(
            trigger_watermark_tokens=20,
            target_watermark_tokens=10,
            recent_turns_to_keep=0,
            offload_threshold_chars=100000,
            hard_capacity_tokens=10000,
            artifacts_dir=tmp_path / "artifacts",
            summarizer_client=gate_client,
        )

        input_text = "Previous observation detail. " * 1000

        task = asyncio.create_task(
            runtime.arun_agent_with_pi(
                run_id=run_id,
                input_text=input_text,
                tools=["query_order_packages"],
                model_config=model_config,
                test_mode=True,
                context_compressor=compressor,
                workspace_dir=tmp_path,
                timeout=15,
            )
        )

        try:
            await asyncio.wait_for(gate_client.entered.wait(), timeout=5.0)
            mutated_tc = TaskContext(
                task_id=task_id,
                intent_revision=1,  # Intent revision deliberately unchanged
                goal="Mutated goal that changes fingerprint",
                constraints=["STATUS_ONLY"],
            )
            mutated_tc.contract_fingerprint = mutated_tc.compute_fingerprint()
            cand_store.save_task_context(mutated_tc)
        finally:
            gate_client.release.set()

        with pytest.raises(Exception) as exc_info:
            await task

        assert "STALE_INTENT" in str(exc_info.value)
        assert gate_client.calls == 1

        rec = runtime.get_run(run_id)
        assert rec.budget_consumed == 0
        assert order_tool_calls == 0

        assert len(pids) == 1
        assert_reaped(pids[0])
        runtime.close()

    asyncio.run(_run_test())


@pytest.mark.parametrize("hook_name", ["pre_compress", "post_compress"])
def test_resume_compression_hook_denial_blocks_execution_and_reaps_process(
    paused_factory, tmp_path: Path, hook_name: str
):
    """Case 2/3: Real Pi resume blocked by pre_compress or post_compress hook denial.

    Guarantees:
    - Reuses real paused snapshot from paused_factory.
    - Real ContextCompressor with high watermark (100000) so post_compress also runs.
    - Hook callback returns HookDecision.deny("TEST_COMPRESSION_DENIED") exactly once.
    - Tracking tool calls (Phase 2) are never dispatched.
    - Remaining budget consumed stays at snapshot value (2).
    - No success Episode created.
    - New resume child PID (PID2) is cleanly reaped.
    - Exception contains HOOK_DENIED.
    """
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    hook_calls = {"pre_compress": 0, "post_compress": 0}

    def pre_hook(state: Any) -> HookDecision:
        hook_calls["pre_compress"] += 1
        return HookDecision.deny("TEST_COMPRESSION_DENIED")

    def post_hook(state: Any, report: Any) -> HookDecision:
        hook_calls["post_compress"] += 1
        return HookDecision.deny("TEST_COMPRESSION_DENIED")

    if hook_name == "pre_compress":
        hooks = HarnessHooks(pre_compress=pre_hook)
    else:
        hooks = HarnessHooks(post_compress=post_hook)

    compressor = ContextCompressor(
        trigger_watermark_tokens=100000,
        target_watermark_tokens=50000,
        hard_capacity_tokens=200000,
        artifacts_dir=tmp_path / "artifacts",
    )

    with pytest.raises(Exception) as exc_info:
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            context_compressor=compressor,
            hooks=hooks,
            workspace_dir=tmp_path,
            timeout=15,
        )

    assert "HOOK_DENIED" in str(exc_info.value)
    assert hook_calls[hook_name] == 1
    if hook_name == "pre_compress":
        assert hook_calls["post_compress"] == 0

    assert e["counts"] == {"order": 1, "track": 0}

    rec = rt.get_run(rid)
    assert rec.budget_consumed == 2
    assert rec.status == "FAILED"

    ep = e["episodes"].get_episode("ep_" + rid)
    assert ep is None or ep.outcome != "success"

    assert len(e["pids"]) == 2
    assert_reaped(e["pids"][1])
