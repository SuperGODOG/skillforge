"""Generate context cache and compression benchmark fixtures for SkillForge.

Executes real AsyncHarnessLoop, AgentRuntime, and ToolBroker across 3 policies
on both short (4-turn) and long (8-turn) logistics verification traces:
1. dynamic_prefix: dynamic per-turn timestamp/state mutation in system prompt
2. stable_prefix: frozen system rules and tool schemas, append-only history (no compression)
3. stable_prefix_with_compression: stable prefix + tiered context compressor

Measures:
- Deterministic canonical static prefix matches (system + tools)
- Full prompt longest common prefix tokens (local tiktoken:cl100k_base match)
- Total main model input tokens sum & final turn input tokens
- Real tool handler invocation counts
- Independent business oracle validation
- Honest accounting of short-trace metadata expansion vs long-trace token reduction

Note: All cache matching is computed locally on captured client requests.
Local metrics do NOT claim server-side KV cache hits (which require remote provider support).
Saves raw metrics and trace summaries to docs/fixtures/context_cache_samples.json.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import tiktoken

from skillforge.context_engine.compressor import ContextCompressor
from skillforge.context_engine.rebuilder import ContextRebuilder
from skillforge.context_engine.schemas import ProtectedState
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    HookDecision,
    ModelResponse,
    ToolCallSpec,
)
from skillforge.runtime import AgentRuntime, ToolBroker


class SimulatedProviderTraceLLM:
    """Mock LLM capturing request state and recording deterministic client prefix metrics.

    Does NOT invent fake provider cache usage (no prompt_tokens * 0.75).
    Evaluates continuous prefix tokens and static prefix invariance locally via cl100k_base.
    """

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.turn = 0
        self.responses = responses
        self.enc = tiktoken.get_encoding("cl100k_base")
        self.last_payload_tokens: list[int] = []
        self.last_static_fp: Optional[str] = None
        self.request_records: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        resp = self.responses[self.turn]
        self.turn += 1

        norm_system = system.strip()
        tools_json = json.dumps(tools, sort_keys=True, ensure_ascii=False)
        messages_json = json.dumps(messages, sort_keys=True, ensure_ascii=False)

        sys_fp = hashlib.sha256(norm_system.encode("utf-8")).hexdigest()[:16]
        tools_fp = hashlib.sha256(tools_json.encode("utf-8")).hexdigest()[:16]
        messages_fp = hashlib.sha256(messages_json.encode("utf-8")).hexdigest()[:16]

        static_prefix_str = f"{norm_system}\n{tools_json}"
        static_fp = hashlib.sha256(static_prefix_str.encode("utf-8")).hexdigest()[:16]
        static_tokens = len(self.enc.encode(static_prefix_str))

        # Full prompt canonical payload for continuous prefix matching
        canonical_payload = json.dumps(
            {"system": norm_system, "tools": tools, "messages": messages},
            sort_keys=True,
            ensure_ascii=False,
        )
        curr_tokens = self.enc.encode(canonical_payload)

        # Longest common continuous token prefix with previous turn
        longest_common_tok = 0
        if self.last_payload_tokens:
            for a, b in zip(curr_tokens, self.last_payload_tokens):
                if a == b:
                    longest_common_tok += 1
                else:
                    break

        static_matched = bool(self.last_static_fp is not None and self.last_static_fp == static_fp)
        self.last_static_fp = static_fp
        self.last_payload_tokens = curr_tokens

        turn_input_tokens = len(curr_tokens)

        # Provider usage without fabricated cache read tokens
        raw_usage = {
            "prompt_tokens": turn_input_tokens,
            "completion_tokens": 40,
            "total_tokens": turn_input_tokens + 40,
        }

        self.request_records.append({
            "turn": self.turn,
            "system_fingerprint": sys_fp,
            "tools_fingerprint": tools_fp,
            "messages_fingerprint": messages_fp,
            "static_prefix_fingerprint": static_fp,
            "static_prefix_matched": static_matched,
            "static_prefix_tokens_est": static_tokens,
            "longest_common_prefix_tokens_est": longest_common_tok,
            "turn_input_tokens_est": turn_input_tokens,
            "synthetic_prompt_text_preview": canonical_payload[:120],
        })

        return ModelResponse(
            content=resp.content,
            tool_calls=resp.tool_calls,
            usage=raw_usage,
        )


def query_packages(batch_id: int) -> dict[str, Any]:
    """Simulates bulky package observation triggering offload and tiered compression."""
    return {
        "batch_id": batch_id,
        "packages": [
            {
                "pkg_id": f"PKG_{batch_id}_{i}",
                "status": "DELIVERED",
                "details": "logistics_inspection_scan_carrier_node_dispatch_verified_" * 10,
            }
            for i in range(10)
        ],
    }


def make_verify_logistics(min_batches: int):
    """Factory for authoritative business oracle for logistics fulfillment."""
    def verify_logistics(state: HarnessState) -> dict[str, Any]:
        has_executed_tools = len([r for r in state.tool_records if r.status == "EXECUTED"]) >= min_batches
        honest_output = "successfully delivered" in (state.final_output or "")
        is_valid = has_executed_tools and honest_output
        return {
            "independent_pass": is_valid,
            "is_hallucination": not is_valid,
            "classification": "TRUE_POSITIVE" if is_valid else "FALSE_POSITIVE",
        }
    return verify_logistics


async def run_scenario(
    policy: str,
    trace_type: str,
    tmp_dir: Path,
) -> dict[str, Any]:
    is_long = (trace_type == "long_trace")
    num_batches = 7 if is_long else 3

    scenario_name = f"{trace_type}_{policy}"
    db_path = tmp_dir / f"{scenario_name}.db"
    ws_dir = tmp_dir / f"ws_{scenario_name}"
    ws_dir.mkdir(parents=True, exist_ok=True)

    tool_call_counter = 0

    def query_packages_tracked(batch_id: int) -> dict[str, Any]:
        nonlocal tool_call_counter
        tool_call_counter += 1
        return query_packages(batch_id)

    broker = ToolBroker(application_allowlist={"query_packages"})
    broker.register_tool(query_packages_tracked, name="query_packages")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    responses = [
        ModelResponse(
            content="",
            tool_calls=[ToolCallSpec(id=f"c{b}", name="query_packages", arguments={"batch_id": b})],
        )
        for b in range(1, num_batches + 1)
    ] + [
        ModelResponse(
            content=f"All batches verified: PKG_1_0 to PKG_{num_batches}_9 successfully delivered.",
            tool_calls=[],
        )
    ]

    llm = SimulatedProviderTraceLLM(responses)

    compression_commits = 0
    compression_rejects = 0
    compression_durations: list[float] = []

    async def on_post_compress(state: HarnessState, rep: Any) -> None:
        nonlocal compression_commits, compression_rejects
        if rep.triggered:
            if rep.level in ("level_0_offload", "level_1_evict_stale", "level_2_summarize"):
                compression_commits += 1
            elif rep.level.startswith("rejected_") or rep.level == "no_progress":
                compression_rejects += 1
            if rep.latency_ms is not None:
                compression_durations.append(rep.latency_ms)

    # Dynamic prefix hook injects timestamp and step counter into system prompt per turn
    async def dynamic_prefix_pre_model(state: HarnessState) -> HookDecision:
        if policy == "dynamic_prefix":
            dynamic_sys = (
                f"[SYSTEM_POLICY_V1]\n"
                f"Dynamic_Epoch_Timestamp: {time.time_ns()}\n"
                f"Step_Sequence_Counter: {state.step_count}\n"
                f"Run_Session: {state.run_id}"
            )
            state.system_prompt = dynamic_sys
            state._base_system_prompt = dynamic_sys
            state.working_scratchpad["protected_state"] = ProtectedState(
                intent_version=f"v{state.step_count + 1}",
                current_goal=f"Verify logistics batches 1 to {num_batches}",
            )
        return HookDecision(action="allow")

    hooks = HarnessHooks(
        pre_model_call=dynamic_prefix_pre_model if policy == "dynamic_prefix" else None,
        post_compress=on_post_compress if policy == "stable_prefix_with_compression" else None,
    )

    compressor = None
    if policy == "stable_prefix_with_compression":
        compressor = ContextCompressor(
            trigger_watermark_tokens=1000 if is_long else 800,
            target_watermark_tokens=600 if is_long else 500,
            recent_turns_to_keep=1,
            artifacts_dir=ws_dir / "artifacts",
        )

    initial_ps = ProtectedState(
        intent_version="v1",
        current_goal=f"Verify logistics batches 1 to {num_batches}",
        active_constraints=["INDEPENDENT_VERIFICATION"],
        allowed_tools=["query_packages"],
    )

    base_system = (
        "[SYSTEM_RULES]\n"
        "You are an automated logistics verification agent.\n"
        f"Rule 1: Always verify batches 1 to {num_batches} independently before claiming delivery."
    )

    spec = HarnessTaskSpec(
        task_id=f"bench_{scenario_name}",
        user_query=f"Verify logistics batches 1 to {num_batches}",
        budget_max=12 if is_long else 6,
        system_prompt=base_system,
        initial_scratchpad={"protected_state": initial_ps},
        context_compressor=compressor,
        verification_fn=make_verify_logistics(num_batches),
    )

    loop = AsyncHarnessLoop(
        runtime=runtime,
        tool_broker=broker,
        llm_client=llm,
        hooks=hooks,
        workspace_dir=ws_dir,
    )

    final_state = await loop.run(spec)

    rebuilder = ContextRebuilder()
    final_messages_tokens = rebuilder.count_messages_tokens(final_state.messages)

    telemetry = list(final_state.request_telemetry)
    canonical_matches = sum(1 for t in telemetry if t.get("client_prefix_matched") is True)
    prefix_changes = sum(1 for t in telemetry if t.get("prefix_change_layer") not in ("none", "initial"))

    turns_detail = []
    for idx, (entry, req_rec) in enumerate(zip(telemetry, llm.request_records)):
        turns_detail.append({
            "turn": entry.get("turn"),
            "epoch": entry.get("epoch"),
            "system_fingerprint": req_rec["system_fingerprint"],
            "tools_fingerprint": req_rec["tools_fingerprint"],
            "messages_fingerprint": req_rec["messages_fingerprint"],
            "static_prefix_fingerprint": req_rec["static_prefix_fingerprint"],
            "static_prefix_matched": req_rec["static_prefix_matched"],
            "static_prefix_tokens_est": req_rec["static_prefix_tokens_est"],
            "longest_common_prefix_tokens_est": req_rec["longest_common_prefix_tokens_est"],
            "turn_input_tokens_est": req_rec["turn_input_tokens_est"],
            "latency_ms": entry.get("latency_ms"),
            "ttft_ms": entry.get("ttft_ms"),
            "provider_usage": entry.get("usage"),
            "synthetic_prompt_text_preview": req_rec["synthetic_prompt_text_preview"],
        })

    oracle_ev = final_state.working_scratchpad.get("verification_evidence") or {}

    total_main_input = sum(r["turn_input_tokens_est"] for r in llm.request_records)
    final_turn_input = llm.request_records[-1]["turn_input_tokens_est"] if llm.request_records else 0
    total_common_prefix = sum(r["longest_common_prefix_tokens_est"] for r in llm.request_records)

    return {
        "policy": policy,
        "trace_type": trace_type,
        "status": final_state.status,
        "turns_count": len(telemetry),
        "total_main_input_tokens_est": total_main_input,
        "final_turn_input_tokens_est": final_turn_input,
        "final_context_messages_tokens": final_messages_tokens,
        "canonical_prefix_matches": canonical_matches,
        "canonical_prefix_changes": prefix_changes,
        "total_longest_common_prefix_tokens_est": total_common_prefix,
        "summary_calls_count": 0,
        "summary_usage": None,
        "compression_commit_count": compression_commits,
        "compression_rejected_count": compression_rejects,
        "compression_overhead_ms": round(sum(compression_durations), 2),
        "tool_handler_actual_invocations": tool_call_counter,
        "final_oracle_pass": oracle_ev.get("independent_pass", False),
        "oracle_classification": oracle_ev.get("classification", "UNKNOWN"),
        "final_epoch": final_state.context_epoch,
        "final_output_preview": (final_state.final_output or "")[:120],
        "turns": turns_detail,
        "estimation_method": "tiktoken:cl100k_base on canonical serialized payloads",
        "note_on_cache": "Local prefix matching measures client-side invariant prefix tokens. It is NOT server-side KV cache hit (server hit requires remote provider cache allocation and is unmeasured locally).",
    }


async def main() -> None:
    output_path = Path("docs/fixtures/context_cache_samples.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        policies = [
            "dynamic_prefix",
            "stable_prefix",
            "stable_prefix_with_compression",
        ]
        results: dict[str, Any] = {
            "meta": {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "description": "Deterministic benchmark across dynamic_prefix, stable_prefix, and stable_prefix_with_compression on short (4-turn) and long (8-turn) traces. All metrics represent client-side input token estimations (cl100k_base).",
                "harness_version": "AsyncHarnessLoop v2 (prefix caching + tiered compression candidate copy isolation)",
                "estimation_method": "tiktoken:cl100k_base",
                "client_common_prefix_definition": "Longest common continuous token sequence matched on canonical client messages, excluding provider internal chat templates and physical KV allocation.",
                "summarizer_usage_note": "Deterministic summarization performs rule-based extraction without invoking an external LLM; summary_calls_count=0 and summary_usage=null.",
                "caveat": "All cache hit and common prefix metrics are calculated locally from actual captured requests. Server KV cache allocation is unmeasured locally.",
            },
            "comparative_summary": {
                "short_trace": {},
                "long_trace": {},
            },
            "traces": {
                "short_trace": {},
                "long_trace": {},
            },
        }

        for trace_type in ("short_trace", "long_trace"):
            for pol in policies:
                res = await run_scenario(pol, trace_type, tmp_dir)
                results["traces"][trace_type][pol] = res
                results["comparative_summary"][trace_type][pol] = {
                    "status": res["status"],
                    "turns_count": res["turns_count"],
                    "total_main_input_tokens_est": res["total_main_input_tokens_est"],
                    "final_turn_input_tokens_est": res["final_turn_input_tokens_est"],
                    "final_context_messages_tokens": res["final_context_messages_tokens"],
                    "canonical_prefix_matches": res["canonical_prefix_matches"],
                    "canonical_prefix_changes": res["canonical_prefix_changes"],
                    "total_longest_common_prefix_tokens_est": res["total_longest_common_prefix_tokens_est"],
                    "summary_calls_count": res["summary_calls_count"],
                    "summary_usage": res["summary_usage"],
                    "compression_commit_count": res["compression_commit_count"],
                    "compression_rejected_count": res["compression_rejected_count"],
                    "compression_overhead_ms": res["compression_overhead_ms"],
                    "tool_handler_invocations": res["tool_handler_actual_invocations"],
                    "final_oracle_pass": res["final_oracle_pass"],
                }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"Generated {output_path} successfully.")
        print(json.dumps(results["comparative_summary"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
