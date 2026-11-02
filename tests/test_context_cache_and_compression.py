"""Focused acceptance and regression test suite for Context Cache and Compression (Tracks F1 - F5).

Covers:
- F1: Deterministic canonical prefix assembly, strip normalization, epoch exclusion, frozen skill retention, streaming TTFT on first text token, batch/stream consistency.
- F2: Candidate working copy isolation, CAS check preventing stale summary overwrite on concurrent tool append, CancelledError and exception safety, default structural must-keep validator with Chinese goal/constraints, bounded capacity pauses.
- F2b: Two consecutive valid compression commits with delivery of summary + canonical state to model and zero duplicate handler calls.
- F3: Tool call closure pairing (assistant.tool_calls IDs <-> tool.tool_call_id), safe split with recent_turns_to_keep=0 boundary and negative rejection.
- F4: Provider usage normalization (Anthropic total input uncached+read+write, OpenAI cached deduplication, missing uncached resulting in None denominator).
- F5: Review pause under compression commit -> SQLite rebuild with new Runtime/Loop -> resume with carried budget and zero handler duplicates; end-to-end model -> Broker -> inspect_artifact drilldown and cross-run path protection.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import pytest

from skillforge.context_engine.artifact_tool import InspectArtifactTool
from skillforge.context_engine.compressor import ContextCompressor, CompressionReport, find_safe_split_index
from skillforge.context_engine.rebuilder import ContextRebuilder
from skillforge.context_engine.schemas import AttachmentRef, MemoryIndex, ProtectedState, WorkSummary, VerifiedFact
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    HookDecision,
    ModelResponse,
    ToolCallSpec,
    compute_canonical_prefix_fingerprint,
    normalize_model_usage,
)
from skillforge.runtime import AgentRuntime, ToolBroker


class RequestCapturingMockLLM:
    """Mock LLM capturing incoming invocation payload and yielding deterministic responses."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.call_idx = 0
        self.captured_requests: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.captured_requests.append({
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "system": system,
        })
        resp = self.responses[min(self.call_idx, len(self.responses) - 1)]
        self.call_idx += 1
        return resp


class StreamingMockLLM:
    """Mock LLM supporting asynchronous generator streaming with configurable delay."""

    def __init__(self, chunks: list[Any], delay: float = 0.005) -> None:
        self.chunks = chunks
        self.delay = delay

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> AsyncIterator[Any]:
        for c in self.chunks:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            yield c


# ==============================================================================
# F1 Tests: Deterministic Prefix Invariance, Frozen Skill Body, Streaming TTFT
# ==============================================================================

def test_f1_canonical_prefix_and_frozen_skill(tmp_path: Path):
    """F1: Strip normalization, epoch exclusion from static fingerprint, and frozen skill retention across updates."""
    async def _test():
        # 1. Strip normalization and epoch exclusion
        tools_def = [
            {"name": "b_tool", "parameters": {"properties": {"z": {"type": "string"}, "a": {"type": "int"}}, "required": ["z", "a"]}},
            {"name": "a_tool", "parameters": {"properties": {"p": {"type": "string"}}, "required": []}},
        ]
        fp1 = compute_canonical_prefix_fingerprint("  System Rules  \n", tools_def, epoch=1)
        fp2 = compute_canonical_prefix_fingerprint("System Rules", tools_def, epoch=1)
        fp_epoch2 = compute_canonical_prefix_fingerprint("System Rules", tools_def, epoch=2)
        assert fp1 == fp2, "Prefix fingerprint must be identical after strip normalization"
        assert fp1 == fp_epoch2, "Epoch must not alter static prefix fingerprint"

        # 2. Frozen skill retention in harness loop
        db_path = tmp_path / "sf_f1.db"
        broker = ToolBroker(application_allowlist={"a_tool", "m_tool", "z_tool"})
        broker.register_tool(lambda: "a", name="a_tool")
        broker.register_tool(lambda: "m", name="m_tool")
        broker.register_tool(lambda: "z", name="z_tool")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        llm = RequestCapturingMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="a_tool", arguments={})]),
            ModelResponse(content="Step 2 complete", tool_calls=[]),
        ])

        class FrozenSkillRuntime(AgentRuntime):
            def get_run_body(self, run_id: str) -> Optional[str]:
                return "RULE 1: ALWAYS BE TRUTHFUL.\nRULE 2: FROZEN BODY CONTENT."

        fs_runtime = FrozenSkillRuntime(db_path=db_path, tool_broker=broker)

        # Hook updating protected_state mid-run
        async def post_step_hook(state: HarnessState, model_resp: ModelResponse) -> None:
            if state.step_count == 0:
                ps = state.working_scratchpad.get("protected_state")
                if ps:
                    ps.intent_version = "v2"
                    ps.current_goal = "Updated Goal"
                    ps.active_constraints.append("NEW_CONSTRAINT")

        hooks = HarnessHooks(post_model_call=post_step_hook)
        loop = AsyncHarnessLoop(
            runtime=fs_runtime,
            tool_broker=broker,
            llm_client=llm,
            hooks=hooks,
            workspace_dir=tmp_path / "ws_f1",
        )

        initial_ps = ProtectedState(
            intent_version="v1",
            current_goal="Execute workflow",
            active_constraints=["READ_ONLY"],
            allowed_tools=["a_tool", "m_tool", "z_tool"],
        )

        spec = HarnessTaskSpec(
            task_id="f1_task",
            user_query="Execute workflow",
            skill_name="test_skill",
            budget_max=4,
            system_prompt="[BASE_SYSTEM_RULES]",
            initial_scratchpad={"protected_state": initial_ps},
        )

        state = await loop.run(spec)
        assert state.status == "COMPLETED"
        assert len(llm.captured_requests) == 2

        # Frozen skill body remains intact across protected_state updates
        assert "[FROZEN_SKILL: test_skill]" in state.system_prompt
        assert "RULE 2: FROZEN BODY CONTENT." in state.system_prompt
        assert "[BASE_SYSTEM_RULES]" in state.system_prompt

        # Dynamic intent update is appended as authoritative directive in user messages, not prepended to system
        user_msgs = [m for m in state.messages if m.get("role") == "user"]
        assert any("[AUTHORITATIVE_UPDATE: INTENT_REDIRECTED]" in m.get("content", "") for m in user_msgs)

        # Deterministic tool ordering (a_tool -> m_tool -> z_tool)
        for req in llm.captured_requests:
            names = [t["name"] for t in req["tools"]]
            assert names == ["a_tool", "m_tool", "z_tool"]

    asyncio.run(_test())


def test_f1_streaming_and_batch_call_model():
    """F1: Streaming TTFT measured on first non-empty text token, incremental delta arguments safely rejected."""
    async def _test():
        broker = ToolBroker(application_allowlist=set())
        runtime = AgentRuntime(db_path=":memory:", tool_broker=broker)

        # 1. Streaming with leading empty metadata frames
        streaming_llm = StreamingMockLLM(chunks=["", "FirstToken ", "SecondToken."], delay=0.01)
        loop = AsyncHarnessLoop(runtime=runtime, tool_broker=broker, llm_client=streaming_llm)

        resp, ttft, latency = await loop._call_model(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="sys",
        )
        assert resp.content == "FirstToken SecondToken."
        assert ttft is not None
        assert ttft >= 5.0
        assert latency >= ttft

        # 2. Streaming dict protocol preserving tool_calls and usage
        dict_streaming_llm = StreamingMockLLM(chunks=[
            {"content": "", "tool_calls": [{"id": "tc_1", "function": {"name": "query", "arguments": {"x": 1}}}]},
            {"content": "", "usage": {"input_tokens": 100, "output_tokens": 20}},
        ], delay=0.001)
        loop_dict = AsyncHarnessLoop(runtime=runtime, tool_broker=broker, llm_client=dict_streaming_llm)
        resp_d, ttft_d, _ = await loop_dict._call_model(messages=[], tools=[], system="")
        assert len(resp_d.tool_calls) == 1
        assert resp_d.tool_calls[0].name == "query"
        assert resp_d.usage == {"input_tokens": 100, "output_tokens": 20}
        assert ttft_d is None

        # 3. Batch call has ttft as None
        batch_llm = RequestCapturingMockLLM([ModelResponse(content="batch_resp")])
        loop_b = AsyncHarnessLoop(runtime=runtime, tool_broker=broker, llm_client=batch_llm)
        _, ttft_b, _ = await loop_b._call_model(messages=[], tools=[], system="")
        assert ttft_b is None

        # 4. Incremental/corrupted streaming tool argument delta rejected with clear ValueError
        corrupt_streaming_llm = StreamingMockLLM(chunks=[
            {"content": "", "tool_calls": [{"id": "tc_bad", "function": {"name": "query", "arguments": '{"incomplete_json": 1'}}]},
        ], delay=0.001)
        loop_corrupt = AsyncHarnessLoop(runtime=runtime, tool_broker=broker, llm_client=corrupt_streaming_llm)
        with pytest.raises(ValueError, match="Incremental streaming tool argument delta cannot be safely executed"):
            await loop_corrupt._call_model(messages=[], tools=[], system="")

    asyncio.run(_test())


# ==============================================================================
# F2 Tests: Candidate Isolation, Concurrency CAS, Must-Keep, Cancellation Safety
# ==============================================================================

def test_f2_candidate_working_copy_and_atomic_commit(tmp_path: Path):
    """F2: Candidate working copy prevents in-place mutation; concurrent tool result preserved; exceptions/cancellation safe."""
    async def _test():
        orig_msgs = [
            {"role": "user", "content": "Turn 1 request " * 30},
            {"role": "assistant", "content": "Doing step", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": {}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "Large tool output " * 50},
        ]
        state = HarnessState(
            run_id="run_f2_atomic",
            task_id="t_f2",
            intent_version="v1",
            goal="Test atomic candidate",
            system_prompt="Base",
            messages=copy.deepcopy(orig_msgs),
            context_epoch=1,
        )
        state.working_scratchpad = {
            "protected_state": ProtectedState(intent_version="v1", current_goal="Goal", active_constraints=["READ_ONLY"]),
        }

        # 1. Concurrent tool result appended during async summarization:
        # Intent remains v1, but concurrent coroutine appends tool result to state.messages.
        # Stale summary is rejected; new tool result is NOT rolled back or erased!
        class ConcurrentToolAppendSummarizer:
            async def ainvoke(self, messages, tools, system=""):
                state.messages.append({"role": "tool", "tool_call_id": "c_concurrent", "content": "Concurrent observation from background"})
                return ModelResponse(content=json.dumps({"verified_facts": [{"fact": "Stale fact"}]}))

        concurrent_compressor = ContextCompressor(
            trigger_watermark_tokens=100,
            target_watermark_tokens=50,
            recent_turns_to_keep=1,
            summarizer_client=ConcurrentToolAppendSummarizer(),
        )

        rep_conc = await concurrent_compressor.compress(state)
        assert rep_conc.level == "rejected_stale_intent"
        assert rep_conc.deprecated_stale_summary is True
        # Summary was NOT committed
        assert "work_summary" not in state.working_scratchpad
        # Concurrent tool message was preserved and NOT rolled back
        assert state.messages[-1]["content"] == "Concurrent observation from background"
        assert state.context_epoch == 1

        # 2. Summarizer raises asyncio.CancelledError -> propagates out without swallowing into success
        class CancellingSummarizer:
            async def ainvoke(self, messages, tools, system=""):
                raise asyncio.CancelledError("Compression cancelled by runtime timeout")

        cancelling_compressor = ContextCompressor(
            trigger_watermark_tokens=100,
            target_watermark_tokens=50,
            recent_turns_to_keep=1,
            summarizer_client=CancellingSummarizer(),
        )
        msgs_before_cancel = copy.deepcopy(state.messages)
        with pytest.raises(asyncio.CancelledError):
            await cancelling_compressor.compress(state)
        assert state.messages == msgs_before_cancel
        assert state.context_epoch == 1

        # 3. Summarizer raises unhandled Exception -> returns no_progress, no partial commit
        class FailingSummarizer:
            async def ainvoke(self, messages, tools, system=""):
                raise RuntimeError("Remote summarizer provider unreachable")

        failing_compressor = ContextCompressor(
            trigger_watermark_tokens=100,
            target_watermark_tokens=50,
            recent_turns_to_keep=1,
            summarizer_client=FailingSummarizer(),
        )
        rep_fail = await failing_compressor.compress(state)
        assert rep_fail.level == "no_progress"
        assert "summarizer_error" in rep_fail.reason
        assert state.context_epoch == 1

    asyncio.run(_test())


def test_f2_default_must_keep_and_real_summary_integration(tmp_path: Path):
    """F2: Canonical structural integrity; Chinese goal/constraints; short factual summary passes; untraceable ref rejected."""
    async def _test():
        chinese_ps = ProtectedState(
            intent_version="v1",
            current_goal="核实北京至上海物流妥投批次",
            active_constraints=["约束: 仅限只读核验", "约束: 必须独立终验"],
            allowed_tools=["query_packages"],
        )

        state_ok = HarnessState(
            run_id="run_f2_chinese",
            task_id="t_chinese",
            intent_version="v1",
            goal="核实北京至上海物流妥投批次",
            system_prompt="[BASE_SYSTEM_RULES]",
            messages=[
                {"role": "user", "content": "请核对物流批次信息 " * 30},
                {"role": "assistant", "content": "已发起核查", "tool_calls": []},
                {"role": "user", "content": "请给出当前进展"},
            ],
            context_epoch=1,
        )
        state_ok.working_scratchpad = {
            "protected_state": chinese_ps,
            "memory_index": MemoryIndex(),
        }

        # 1. Legal short Chinese factual summary does NOT repeat the goal, but passes structural validation
        short_valid_summary = json.dumps({
            "verified_facts": [{"fact": "批次1已完成核验全部妥投", "evidence_ref": "historical_turn_0", "confidence": "HIGH"}],
            "failed_attempts": [],
            "open_questions": [],
        })
        compressor = ContextCompressor(
            trigger_watermark_tokens=100,
            target_watermark_tokens=50,
            recent_turns_to_keep=1,
            summarizer_client=RequestCapturingMockLLM([ModelResponse(content=short_valid_summary)]),
        )

        rep = await compressor.compress(state_ok)
        assert rep.level == "level_2_summarize"
        assert state_ok.context_epoch == 2
        summary = state_ok.working_scratchpad["work_summary"]
        assert len(summary.verified_facts) == 1
        assert summary.verified_facts[0].fact == "批次1已完成核验全部妥投"

        # Canonical ProtectedState with Chinese goal and constraints is preserved in rendered messages Layer 1
        rendered = state_ok.messages[0]["content"]
        assert "核实北京至上海物流妥投批次" in rendered
        assert "约束: 仅限只读核验" in rendered

        # 2. Untraceable evidence ref (citing non-existent attachment ref) triggers structural rejection
        untraceable_summary = json.dumps({
            "verified_facts": [{"fact": "虚构凭证数据", "evidence_ref": "fs://non_existent_fake_payload.json"}],
            "failed_attempts": [],
            "open_questions": [],
        })
        bad_compressor = ContextCompressor(
            trigger_watermark_tokens=100,
            target_watermark_tokens=50,
            recent_turns_to_keep=1,
            summarizer_client=RequestCapturingMockLLM([ModelResponse(content=untraceable_summary)]),
        )
        state_bad = HarnessState(
            run_id="run_f2_bad",
            task_id="t_bad",
            intent_version="v1",
            goal="核实北京至上海物流妥投批次",
            system_prompt="[BASE_SYSTEM_RULES]",
            messages=[
                {"role": "user", "content": "请核对物流批次信息 " * 30},
                {"role": "assistant", "content": "已发起核查", "tool_calls": []},
                {"role": "user", "content": "请给出当前进展"},
            ],
            context_epoch=1,
        )
        state_bad.working_scratchpad = {
            "protected_state": chinese_ps,
            "memory_index": MemoryIndex(),
        }
        rep_bad = await bad_compressor.compress(state_bad)
        assert rep_bad.level == "rejected_invalid_summary"
        assert "untraceable" in rep_bad.reason

    asyncio.run(_test())


def test_f2_two_consecutive_compression_commits_with_model_delivery(tmp_path: Path):
    """F2b: Two consecutive valid compression commits with delivery of summary + canonical state to model and zero duplicate handler calls."""
    async def _test():
        db_path = tmp_path / "sf_f2b.db"
        handler_calls: list[str] = []

        def query_batch(batch_id: int) -> dict[str, Any]:
            handler_calls.append(f"batch_{batch_id}")
            return {
                "batch_id": batch_id,
                "items": [{"id": f"ITEM_{batch_id}_{i}", "status": "DELIVERED", "desc": "long_metadata_scan_" * 20} for i in range(8)],
            }

        broker = ToolBroker(application_allowlist={"query_batch"})
        broker.register_tool(query_batch, name="query_batch")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        llm = RequestCapturingMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="query_batch", arguments={"batch_id": 1})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="query_batch", arguments={"batch_id": 2})]),
            ModelResponse(content="Both batches verified successfully.", tool_calls=[]),
        ])

        compressor = ContextCompressor(
            trigger_watermark_tokens=400,
            target_watermark_tokens=250,
            recent_turns_to_keep=1,
            artifacts_dir=tmp_path / "artifacts",
        )

        compression_events: list[CompressionReport] = []
        async def on_post_compress(state: HarnessState, rep: CompressionReport):
            if rep.triggered:
                compression_events.append(rep)

        hooks = HarnessHooks(post_compress=on_post_compress)
        initial_ps = ProtectedState(
            intent_version="v1",
            current_goal="Verify two consecutive batches",
            active_constraints=["INDEPENDENT_VERIFICATION"],
            allowed_tools=["query_batch"],
        )

        spec = HarnessTaskSpec(
            task_id="t_f2b_consecutive",
            user_query="Verify two consecutive batches",
            budget_max=6,
            system_prompt="[SYSTEM_RULES]",
            initial_scratchpad={"protected_state": initial_ps},
            context_compressor=compressor,
        )

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=llm,
            hooks=hooks,
            workspace_dir=tmp_path / "ws_f2b",
        )

        final_state = await loop.run(spec)
        assert final_state.status == "COMPLETED", f"Failed with {final_state.status}: {final_state.error_message}, steps: {final_state.step_count}"

        # Assert two distinct compression commit events occurred
        committed_events = [e for e in compression_events if e.level in ("level_0_offload", "level_1_evict_stale", "level_2_summarize")]
        assert len(committed_events) >= 2, f"Expected at least 2 commit events, got {len(committed_events)}"
        assert final_state.context_epoch == 3, f"Expected epoch 3 after 2 commits, got {final_state.context_epoch}"

        # Final model invocation received committed summary and canonical ProtectedState
        last_req = llm.captured_requests[-1]
        req_messages = last_req["messages"]
        sys_content = "\n".join(str(m.get("content", "")) for m in req_messages if m.get("role") == "system")
        assert "Verify two consecutive batches" in sys_content
        assert "INDEPENDENT_VERIFICATION" in sys_content
        assert "[HISTORICAL_KNOWLEDGE_BASE]" in sys_content or "[ATTACHMENTS_INDEX]" in sys_content

        # Assert zero duplicate handler calls
        assert handler_calls == ["batch_1", "batch_2"], f"Expected exactly ['batch_1', 'batch_2'], got {handler_calls}"

    asyncio.run(_test())


def test_f2_bounded_capacity_and_hard_pause(tmp_path: Path):
    """F2: Exceeding watermark with zero progress triggers BUDGET_EXHAUSTED bounded pause."""
    async def _test():
        compressor = ContextCompressor(
            trigger_watermark_tokens=50,
            target_watermark_tokens=30,
            hard_capacity_tokens=150,
            recent_turns_to_keep=10,
        )
        state = HarnessState(
            run_id="run_f2_exhaust",
            task_id="t_ex",
            intent_version="v1",
            goal="Goal",
            system_prompt="Base",
            messages=[{"role": "user", "content": "Word " * 60}],
            context_epoch=1,
        )

        rep1 = await compressor.compress(state)
        assert rep1.level == "no_progress"
        assert state.status == "RUNNING"

        # Second failure triggers bounded pause
        rep2 = await compressor.compress(state)
        assert rep2.level == "no_progress"
        assert state.status == "BUDGET_EXHAUSTED"

    asyncio.run(_test())


# ==============================================================================
# F3 Tests: Tool Call Closure & Safe Split Boundary
# ==============================================================================

def test_f3_tool_call_closure_and_safe_split():
    """F3: Match assistant tool_calls IDs with tool responses; keep=0 support; negative rejection."""
    # 1. Negative recent_turns_to_keep raises ValueError
    with pytest.raises(ValueError, match="non-negative"):
        find_safe_split_index([], recent_turns_to_keep=-1)

    # 2. Incomplete tool call: Assistant issued 2 tool calls, but only 1 tool result arrived
    partial_msgs = [
        {"role": "user", "content": "Request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": {}}},
                {"id": "call_2", "type": "function", "function": {"name": "f2", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result 1"},
    ]
    # Cannot split: call_2 is pending, so entire group is kept active
    assert find_safe_split_index(partial_msgs, recent_turns_to_keep=1) == 0

    # 3. Fully closed group splits cleanly
    closed_msgs = [
        {"role": "user", "content": "Turn 1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": {}}},
                {"id": "call_2", "type": "function", "function": {"name": "f2", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "result 2 (out of order)"},
        {"role": "tool", "tool_call_id": "call_1", "content": "result 1"},
        {"role": "user", "content": "Turn 2 (recent)"},
        {"role": "assistant", "content": "Recent answer"},
    ]
    assert find_safe_split_index(closed_msgs, recent_turns_to_keep=1) == 4

    # 4. recent_turns_to_keep = 0 safely yields boundary including entire closed history
    assert find_safe_split_index(closed_msgs, recent_turns_to_keep=0) == 6


# ==============================================================================
# F4 Tests: Provider Usage Normalization & Telemetry
# ==============================================================================

def test_f4_provider_usage_normalization():
    """F4: Anthropic uncached+read+write total; OpenAI cached not double-counted; missing uncached results in None denominator."""
    # 1. Anthropic complete: uncached(200) + read(800) + write(100) = 1100 total input
    anthropic_raw = {
        "input_tokens": 200,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
    }
    norm_ant = normalize_model_usage(anthropic_raw)
    assert norm_ant["input_tokens"] == 1100
    assert norm_ant["cache_read_tokens"] == 800
    assert norm_ant["cache_write_tokens"] == 100
    assert norm_ant["output_tokens"] == 50
    assert norm_ant["cache_hit_ratio"] == 0.7273

    # 2. OpenAI complete: prompt_tokens(1000) already includes cached(400)
    openai_raw = {
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 400},
        "completion_tokens": 120,
    }
    norm_oai = normalize_model_usage(openai_raw)
    assert norm_oai["input_tokens"] == 1000
    assert norm_oai["cache_read_tokens"] == 400
    assert norm_oai["cache_write_tokens"] is None
    assert norm_oai["output_tokens"] == 120
    assert norm_oai["cache_hit_ratio"] == 0.4

    # 3. Generic provider: lacks cache fields, must remain None (NOT 0.0)
    vanilla_raw = {"prompt_tokens": 350, "completion_tokens": 45}
    norm_vanilla = normalize_model_usage(vanilla_raw)
    assert norm_vanilla["input_tokens"] == 350
    assert norm_vanilla["cache_read_tokens"] is None
    assert norm_vanilla["cache_hit_ratio"] is None

    # 4. Explicit 0 vs None distinction
    zero_raw = {
        "input_tokens": 500,
        "cache_read_input_tokens": 0,
        "output_tokens": 50,
    }
    norm_zero = normalize_model_usage(zero_raw)
    assert norm_zero["cache_read_tokens"] == 0
    assert norm_zero["cache_hit_ratio"] == 0.0

    # 5. Missing uncached in Anthropic: denominator incomplete, input_tokens and cache_hit_ratio must be None
    missing_uncached = {
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
    }
    norm_miss = normalize_model_usage(missing_uncached)
    assert norm_miss["input_tokens"] is None
    assert norm_miss["cache_read_tokens"] == 500
    assert norm_miss["cache_write_tokens"] == 100
    assert norm_miss["cache_hit_ratio"] is None


# ==============================================================================
# F5 Tests: Review Pause/Resume with Compression, Manifest Drilldown Security
# ==============================================================================

def test_f5_compression_review_pause_and_resume(tmp_path: Path):
    """F5: Compression committed before review pause -> SQLite rebuild with new Runtime/Loop -> resume with carried budget and zero handler duplicates."""
    async def _test():
        broker = ToolBroker(application_allowlist={"step_action"})
        executed_actions: list[str] = []

        def step_action(name: str) -> str:
            executed_actions.append(name)
            return f"res_{name}_bulky_metadata_payload_" * 200

        broker.register_tool(step_action, name="step_action")
        db_path = tmp_path / "sf_f5_pause.db"
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Hook pausing on step 1 for review
        async def pause_hook(state: HarnessState) -> HookDecision:
            if state.step_count == 1:
                return HookDecision.review("NEED_HUMAN_APPROVAL")
            return HookDecision.allow()

        compression_events: list[CompressionReport] = []
        async def on_post_compress(state: HarnessState, rep: CompressionReport):
            if rep.triggered:
                compression_events.append(rep)

        hooks = HarnessHooks(pre_model_call=pause_hook, post_compress=on_post_compress)
        llm = RequestCapturingMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="step_action", arguments={"name": "step1"})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="step_action", arguments={"name": "step2"})]),
            ModelResponse(content="All steps complete", tool_calls=[]),
        ])

        compressor = ContextCompressor(
            trigger_watermark_tokens=500,
            target_watermark_tokens=300,
            recent_turns_to_keep=1,
            offload_threshold_chars=50,
            artifacts_dir=tmp_path / "artifacts",
        )

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=llm,
            hooks=hooks,
            workspace_dir=tmp_path / "ws_pause",
        )

        spec = HarnessTaskSpec(
            task_id="t_pause",
            user_query="Execute paused flow",
            budget_max=6,
            context_compressor=compressor,
        )

        paused_state = await loop.run(spec)
        assert paused_state.status == "PAUSED_FOR_REVIEW"
        assert executed_actions == ["step1"]

        # Verify real compression commit occurred before pause
        commits_before_pause = [e for e in compression_events if e.level in ("level_0_offload", "level_1_evict_stale", "level_2_summarize")]
        assert len(commits_before_pause) >= 1, "Expected at least 1 compression commit before review pause"

        # Record consumed budget before pause
        budget_consumed_before = paused_state.step_count
        assert budget_consumed_before == 1

        # Reconstruct new Runtime and loop from persisted SQLite checkpoint
        runtime_resumed = AgentRuntime(db_path=db_path, tool_broker=broker)
        llm_resumed = RequestCapturingMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="step_action", arguments={"name": "step2"})]),
            ModelResponse(content="All steps complete", tool_calls=[]),
        ])
        loop_resumed = AsyncHarnessLoop(
            runtime=runtime_resumed,
            tool_broker=broker,
            llm_client=llm_resumed,
            workspace_dir=tmp_path / "ws_pause",
        )

        resumed_state = await loop_resumed.resume(paused_state.run_id)
        assert resumed_state.status == "COMPLETED"

        # Assert budget carried over properly (consumed: 1 -> 2)
        assert resumed_state.step_count == 2
        # Assert zero duplicate handler calls
        assert executed_actions == ["step1", "step2"]
        assert resumed_state.final_output == "All steps complete"

    asyncio.run(_test())


def test_f5_manifest_safe_drilldown_under_compression(tmp_path: Path):
    """F5: End-to-end model -> Harness -> Broker -> inspect_artifact drilldown; cross-run rejected via broker."""
    async def _test():
        ws_dir = tmp_path / "ws_e2e_inspect"
        ws_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = ws_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 1. Tools setup
        inspect_tool = InspectArtifactTool(allowed_base_dir=ws_dir)
        def fetch_manifest() -> dict[str, Any]:
            return {
                "records": [
                    {"id": f"REC_{i}", "val": f"safe_val_{i}" * 10, "secret_key": "SK_SUPER_SECRET"}
                    for i in range(12)
                ]
            }

        broker = ToolBroker(application_allowlist={"fetch_manifest", "inspect_artifact"})
        broker.register_tool(fetch_manifest, name="fetch_manifest")
        broker.register_tool(inspect_tool, name="inspect_artifact")
        db_path = tmp_path / "sf_e2e_drill.db"
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        compressor = ContextCompressor(
            trigger_watermark_tokens=400,
            target_watermark_tokens=250,
            recent_turns_to_keep=1,
            offload_threshold_chars=50,
            artifacts_dir=artifacts_dir,
        )

        # Turn 1: Model calls fetch_manifest -> returns bulky output offloaded to artifact
        # Turn 2: Model calls inspect_artifact through Broker with ref_id
        # Turn 3: Model calls inspect_artifact targeting cross-run unauthorized path outside ws_dir
        # Turn 4: Final response
        captured_ref_ids: list[str] = []

        class AdaptiveDrilldownLLM:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, messages, tools, system=""):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(content="", tool_calls=[ToolCallSpec(id="c1", name="fetch_manifest", arguments={})])
                elif self.calls == 2:
                    # Model extracts ref_id from Layer 3 AttachmentsIndex in messages
                    all_text = json.dumps(messages)
                    ref_id = ""
                    for token in all_text.split():
                        if "fs://" in token:
                            ref_id = token.split("fs://")[-1].replace('"', '').replace("'", "").replace(",", "").replace(":", "")
                            ref_id = f"fs://{ref_id}"
                            break
                    captured_ref_ids.append(ref_id)
                    return ModelResponse(content="", tool_calls=[ToolCallSpec(id="c2", name="inspect_artifact", arguments={"ref_id": ref_id, "object_id": "REC_0"})])
                elif self.calls == 3:
                    # Cross-run path traversal attempt
                    outside_path = f"fs://{tmp_path.parent / 'unauthorized.json'}"
                    return ModelResponse(content="", tool_calls=[ToolCallSpec(id="c3", name="inspect_artifact", arguments={"ref_id": outside_path})])
                else:
                    return ModelResponse(content="Drilldown complete with security checks.", tool_calls=[])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=AdaptiveDrilldownLLM(),
            workspace_dir=ws_dir,
        )

        initial_ps = ProtectedState(
            intent_version="v1",
            current_goal="Inspect manifest records",
            active_constraints=["READ_ONLY"],
            allowed_tools=["fetch_manifest", "inspect_artifact"],
        )

        spec = HarnessTaskSpec(
            task_id="t_e2e_drill",
            user_query="Inspect manifest records",
            budget_max=6,
            context_compressor=compressor,
            initial_scratchpad={"protected_state": initial_ps},
        )

        final_state = await loop.run(spec)
        assert final_state.status == "COMPLETED", f"Failed with {final_state.status}: {final_state.error_message}, steps: {final_state.step_count}"

        # Assert all 3 tool records executed through Harness -> Broker
        records = final_state.tool_records
        assert len(records) == 3
        # 1. fetch_manifest executed
        assert records[0].tool_name == "fetch_manifest" and records[0].status == "EXECUTED"
        # 2. inspect_artifact on valid manifest executed successfully through Broker
        assert records[1].tool_name == "inspect_artifact" and records[1].status == "EXECUTED"
        assert "REC_0" in str(records[1].output_text)
        # 3. inspect_artifact on cross-run/outside directory rejected through Broker
        assert records[2].tool_name == "inspect_artifact" and records[2].status == "ERROR"
        assert "outside allowed directory" in (records[2].error_message or records[2].output_text)

    asyncio.run(_test())
