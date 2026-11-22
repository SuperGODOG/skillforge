"""Tests for Phase 4B0: Pi Lifecycle Hook Compatibility.

Covers mandatory 6 test specifications:
1. pre-model deny provider_inputs=0;
2. 父deny+子allow组成Hook handler=0且审计/REJECTED落盘;
3. post-tool observation_view不改rawprovenance;
4. before-finalize拒绝无正常结果;
5. exception/normal task_end各一次且PIDreap;
6. 带共享ledger回调仍结算真实Pi usage与Hook观察各一次（不预填usage覆盖原生）.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator.llm_factory import LLMLedger
from skillforge.harness_loop import (
    HarnessHooks,
    HarnessState,
    HookDecision,
    ModelResponse,
    compose_hooks,
)
from skillforge.models import EvolveBudget, ToolCallRecord
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)


@pytest.fixture
def pi_hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sets up isolated SQLite runtime, ToolBroker, Collector, CandidateStore, and SkillRegistry."""
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "status", "IN_TRANSIT")
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "carrier", "Carrier_Beta")
    db_path = tmp_path / "runtime.db"
    episodes_db = tmp_path / "episodes.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    episode_store = EpisodeStore(db_path=episodes_db)
    cand_store = CandidateStore(db_path=tmp_path / "candidates.db", episode_store=episode_store)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    pkg_tool = QueryOrderPackagesTool()
    track_tool = QueryPackageTrackingTool()
    broker.register_tool(pkg_tool)
    broker.register_tool(track_tool)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
        candidate_store=cand_store,
    )

    yield {
        "runtime": runtime,
        "broker": broker,
        "collector": collector,
        "episode_store": episode_store,
        "cand_store": cand_store,
        "registry": registry,
        "pkg_tool": pkg_tool,
        "track_tool": track_tool,
        "tmp_path": tmp_path,
    }

    runtime.close()
    cand_store.close()
    episode_store.close()
    registry.close()


# ==============================================================================
# Case 1: pre-model deny provider_inputs=0
# ==============================================================================


def test_case1_pre_model_deny_stops_provider_invocation_and_zero_inputs(pi_hook_env):
    """Case 1: pre_model_call deny stops provider invocation before sending; captured_provider_inputs == 0."""
    runtime: AgentRuntime = pi_hook_env["runtime"]
    run_id = "run_case1_pre_model_deny"
    task_id = "task_case1"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")

    pre_model_calls = 0

    def deny_pre_model(state: HarnessState):
        nonlocal pre_model_calls
        pre_model_calls += 1
        return HookDecision.deny(
            reason_code="PRE_MODEL_SECURITY_GATE_DENIAL",
            details={"step": state.step_count},
            evidence_refs=[f"run:{state.run_id}"],
        )

    hooks = HarnessHooks(pre_model_call=deny_pre_model)

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "This text must never be generated"}],
                "stopReason": "stop",
            }
        ],
    }

    with pytest.raises(Exception) as exc_info:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Track order ORD_2026_0901",
            tools=["query_order_packages"],
            model_config=model_config,
            test_mode=True,
            hooks=hooks,
            timeout=5.0,
        )

    assert "PRE_MODEL_SECURITY_GATE_DENIAL" in str(exc_info.value)
    assert pre_model_calls == 1

    # Provider was NEVER invoked -> captured_provider_inputs has 0 items!
    last_res = runtime.get_last_pi_result(run_id)
    assert last_res is not None
    assert len(last_res.captured_provider_inputs) == 0

    # Run terminated in FAILED with HOOK_DENIED
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status == "FAILED"
    assert run.error_type == "HOOK_DENIED"
    assert "PRE_MODEL_SECURITY_GATE_DENIAL" in (run.error_message or "")

    # HarnessState audit recorded
    h_state = runtime.get_harness_state(run_id)
    assert h_state is not None
    pre_audits = [a for a in h_state.hook_audits if a["event"] == "pre_model_call"]
    assert len(pre_audits) == 1
    assert pre_audits[0]["action"] == "deny"
    assert pre_audits[0]["reason_code"] == "PRE_MODEL_SECURITY_GATE_DENIAL"


# ==============================================================================
# Case 2: 父deny+子allow组成Hook handler=0且审计/REJECTED落盘
# ==============================================================================


def test_case2_composed_parent_deny_child_allow_handler_zero_persists_rejected(pi_hook_env):
    """Case 2: compose_hooks(parent_deny, child_allow) ensures handler_count=0 and REJECTED record is persisted to SQLite."""
    runtime: AgentRuntime = pi_hook_env["runtime"]
    pkg_tool = pi_hook_env["pkg_tool"]
    run_id = "run_case2_composed_deny"
    task_id = "task_case2"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")

    handler_calls = 0
    orig_run = pkg_tool.run

    def tracking_run(params):
        nonlocal handler_calls
        handler_calls += 1
        return orig_run(params)

    pkg_tool.run = tracking_run

    # Parent hook: denies query_order_packages
    parent_hook = HarnessHooks(
        pre_tool_exec=lambda state, tc: HookDecision.deny(
            reason_code="PARENT_SECURITY_RULE_DENIAL",
            details={"blocked_tool": tc.name},
            evidence_refs=[f"tool:{tc.name}"],
        )
    )
    # Child hook: allows
    child_hook = HarnessHooks(
        pre_tool_exec=lambda state, tc: HookDecision.allow(
            reason_code="CHILD_ALLOW_EVERYTHING",
        )
    )
    # Composed: parent denial takes priority over child allow
    composed = compose_hooks(parent_hook, child_hook)

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_denied_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I observed the tool was denied."}],
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
        hooks=composed,
        timeout=10.0,
    )
    assert "denied" in final_text.lower() or "observed" in final_text.lower()

    # 1. Handler was strictly NEVER called! handler=0
    assert handler_calls == 0

    # 2. Tool record in HarnessState
    h_state = runtime.get_harness_state(run_id)
    assert h_state is not None
    assert len(h_state.tool_records) == 1
    rec = h_state.tool_records[0]
    assert rec.status == "REJECTED"
    assert rec.error_type == "HOOK_DENIED"
    assert "PARENT_SECURITY_RULE_DENIAL" in (rec.error_message or "")

    # 3. Persistent REJECTED record in SQLite runtime_tool_calls
    conn = runtime._get_conn()
    row = conn.execute(
        "SELECT call_id, tool_name, status, error_type, error_message FROM runtime_tool_calls WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "tc_denied_01"
    assert row[1] == "query_order_packages"
    assert row[2] == "REJECTED"
    assert row[3] == "HOOK_DENIED"
    assert "PARENT_SECURITY_RULE_DENIAL" in row[4]

    # 4. Audit trail shows parent denial
    deny_audits = [a for a in h_state.hook_audits if a["event"] == "pre_tool_exec"]
    # Parent and composition each retain an audit; child allow never executes.
    assert len(deny_audits) == 2
    assert all(a["action"] == "deny" for a in deny_audits)
    assert all(a["reason_code"] == "PARENT_SECURITY_RULE_DENIAL" for a in deny_audits)


# ==============================================================================
# Case 3: post-tool observation_view不改rawprovenance
# ==============================================================================


@pytest.mark.parametrize("redacted_view", [json.dumps({"order_id": "ORD_REDACTED", "status": "HIDDEN"}), "REDACTED plain text"])
def test_case3_post_tool_observation_view_does_not_mutate_raw_provenance(pi_hook_env, redacted_view):
    """Case 3: post_tool_exec observation_view changes what model sees, but raw output/provenance are untouched."""
    runtime: AgentRuntime = pi_hook_env["runtime"]
    run_id = "run_case3_observation_view"
    task_id = "task_case3"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")


    def redact_post_tool(state: HarnessState, rec: ToolCallRecord):
        return HookDecision.allow(
            reason_code="REDACTED_FOR_SECURITY",
            observation_view=redacted_view,
        )

    hooks = HarnessHooks(post_tool_exec=redact_post_tool)

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_obs_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Summary concluded."}],
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
        hooks=hooks,
        timeout=10.0,
    )
    assert final_text == "Summary concluded."

    # 1. Check ToolCallRecord in HarnessState
    h_state = runtime.get_harness_state(run_id)
    assert h_state is not None
    assert len(h_state.tool_records) == 1
    rec = h_state.tool_records[0]
    assert rec.status == "EXECUTED"
    # Raw output retains original unredacted package details!
    assert "PKG_101" in rec.output_text
    assert "ORD_REDACTED" not in rec.output_text

    # 2. Check provenance in ToolCallRecord & runtime
    assert rec.provenance is not None
    assert "PKG_101" in rec.provenance.output_summary
    assert "ORD_REDACTED" not in rec.provenance.output_summary
    assert rec.provenance.authenticity_pass is True

    # 3. Check runtime._provenances
    provs = runtime.get_provenances(run_id)
    assert len(provs) == 1
    assert "PKG_101" in provs[0].output_summary
    assert "ORD_REDACTED" not in provs[0].output_summary

    # 4. Check SQLite runtime_tool_calls
    conn = runtime._get_conn()
    row = conn.execute(
        "SELECT output_text FROM runtime_tool_calls WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row is not None
    assert "PKG_101" in row[0]
    assert "ORD_REDACTED" not in row[0]


# ==============================================================================
# Case 4: before-finalize拒绝无正常结果
# ==============================================================================


def test_case4_before_finalize_denial_blocks_normal_result(pi_hook_env):
    """Case 4: before_finalize denial rejects normal return; model self-reported success is blocked."""
    runtime: AgentRuntime = pi_hook_env["runtime"]
    run_id = "run_case4_before_finalize_deny"
    task_id = "task_case4"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")

    def deny_final(state: HarnessState, proposed_output: str):
        return HookDecision.deny(
            reason_code="ORACLE_DELIVERY_REJECTED",
            details={"proposed_len": len(proposed_output)},
            evidence_refs=[f"run:{state.run_id}"],
        )

    hooks = HarnessHooks(before_finalize=deny_final)

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "All items delivered and verified 100%!"}],
                "stopReason": "stop",
            }
        ],
    }

    with pytest.raises(RuntimeError) as exc_info:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Finish task",
            tools=[],
            model_config=model_config,
            test_mode=True,
            hooks=hooks,
            timeout=5.0,
        )

    # Must raise and block delivery
    assert "BeforeFinalize hook denied: ORACLE_DELIVERY_REJECTED" in str(exc_info.value)

    # Run status in runtime_runs must be FAILED, NOT success/completed!
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status == "FAILED"
    assert run.error_type == "HOOK_DENIED"

    # HarnessState final_output contains delivery blocked message
    h_state = runtime.get_harness_state(run_id)
    assert h_state is not None
    assert "[DELIVERY_BLOCKED]: ORACLE_DELIVERY_REJECTED" in (h_state.final_output or "")

    # Audit recorded
    audits = [a for a in h_state.hook_audits if a["event"] == "before_finalize"]
    assert len(audits) == 1
    assert audits[0]["action"] == "deny"
    assert audits[0]["reason_code"] == "ORACLE_DELIVERY_REJECTED"


# ==============================================================================
# Case 5: exception/normal task_end各一次且PIDreap
# ==============================================================================


def test_case5_task_end_executed_once_on_normal_and_exception_and_pid_reaped(pi_hook_env):
    """Case 5: task_end is executed exactly once on both normal and exception paths, and PID is reaped in both."""
    runtime: AgentRuntime = pi_hook_env["runtime"]

    # --- Subcase A: Normal path ---
    run_id_normal = "run_case5_normal"
    runtime.start_run(run_id=run_id_normal, task_id="task_5a", purpose="evaluation")

    task_end_normal_count = 0

    def on_task_end_normal(state: HarnessState):
        nonlocal task_end_normal_count
        task_end_normal_count += 1

    hooks_normal = HarnessHooks(task_end=on_task_end_normal)
    model_config_normal = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Normal clean finish"}],
                "stopReason": "stop",
            }
        ],
    }

    ans_normal = runtime.run_agent_with_pi(
        run_id=run_id_normal,
        input_text="Do work",
        tools=[],
        model_config=model_config_normal,
        test_mode=True,
        hooks=hooks_normal,
        timeout=5.0,
    )
    assert ans_normal == "Normal clean finish"
    assert task_end_normal_count == 1

    pid_normal = runtime.get_last_pi_pid(run_id_normal)
    assert pid_normal is not None
    # Subprocess MUST be reaped
    with pytest.raises(ProcessLookupError):
        os.kill(pid_normal, 0)

    # --- Subcase B: Exception path ---
    run_id_exc = "run_case5_exception"
    runtime.start_run(run_id=run_id_exc, task_id="task_5b", purpose="evaluation")

    task_end_exc_count = 0

    def on_task_end_exc(state: HarnessState):
        nonlocal task_end_exc_count
        task_end_exc_count += 1

    hooks_exc = HarnessHooks(
        before_finalize=lambda s, o: HookDecision.deny("EXC_TEST_DENIAL"),
        task_end=on_task_end_exc,
    )
    model_config_exc = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Will fail at finalize"}],
                "stopReason": "stop",
            }
        ],
    }

    with pytest.raises(RuntimeError) as exc_info:
        runtime.run_agent_with_pi(
            run_id=run_id_exc,
            input_text="Do failing work",
            tools=[],
            model_config=model_config_exc,
            test_mode=True,
            hooks=hooks_exc,
            timeout=5.0,
        )
    # Original exception has priority
    assert "EXC_TEST_DENIAL" in str(exc_info.value)
    assert task_end_exc_count == 1

    pid_exc = runtime.get_last_pi_pid(run_id_exc)
    assert pid_exc is not None
    # Subprocess MUST be reaped
    with pytest.raises(ProcessLookupError):
        os.kill(pid_exc, 0)


# ==============================================================================
# Case 6: 带共享ledger回调仍结算真实Pi usage与Hook观察各一次
# ==============================================================================


def test_case6_shared_ledger_callback_and_hook_observation_authentic_usage(pi_hook_env):
    """Case 6: Shared ledger callback and post_model_call hook each called once per turn with authentic native usage."""
    runtime: AgentRuntime = pi_hook_env["runtime"]
    run_id = "run_case6_shared_ledger"
    task_id = "task_case6"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")

    budget = EvolveBudget(max_calls=5, max_tokens=10000)
    ledger = LLMLedger(budget=budget)

    # Pre-reserve turn 1
    ledger.reserve_turn(role="agent", call_id="turn_receipt_call_01")

    ledger_receipt_count = 0

    def on_turn_receipt(frame: dict[str, Any]):
        nonlocal ledger_receipt_count
        ledger_receipt_count += 1
        ledger.settle_turn(
            call_id="turn_receipt_call_01",
            usage=frame.get("usage"),
            status=frame.get("status", "success"),
            error_type=frame.get("error"),
        )

    hook_observation_count = 0
    hook_observed_usage: Optional[dict[str, Any]] = None
    hook_observed_resp: Optional[ModelResponse] = None

    def on_post_model_call(state: HarnessState, model_resp: ModelResponse):
        nonlocal hook_observation_count, hook_observed_usage, hook_observed_resp
        hook_observation_count += 1
        hook_observed_resp = model_resp
        hook_observed_usage = model_resp.usage

    hooks = HarnessHooks(post_model_call=on_post_model_call)

    # Native faux provider with response without manual prefilled usage override
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Authentic token computation response"}],
                "usage": {},  # Presence enables the official faux provider's estimator.
                "stopReason": "stop",
            }
        ],
    }

    final_text = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Generate text",
        tools=[],
        model_config=model_config,
        test_mode=True,
        hooks=hooks,
        model_turn_receipt=on_turn_receipt,
        timeout=5.0,
    )

    assert final_text == "Authentic token computation response"

    # 1. Both were called exactly ONCE
    assert ledger_receipt_count == 1
    assert hook_observation_count == 1

    # 2. Authentic native usage was calculated by faux provider (positive tokens)
    assert ledger.total_tokens is not None
    assert ledger.total_tokens > 0

    # 3. Hook observed the exact same authentic usage without overwriting or faking
    assert hook_observed_usage is not None
    assert hook_observed_usage.get("totalTokens") == ledger.total_tokens
    assert hook_observed_resp is not None
    assert hook_observed_resp.normalized_usage is not None
    assert hook_observed_resp.normalized_usage["input_tokens"] is not None

    # 4. No thinking leaked
    assert not hasattr(hook_observed_resp, "thinking") or not getattr(hook_observed_resp, "thinking")


@pytest.mark.parametrize("hook_event", ["pre_tool_exec", "post_tool_exec"])
def test_review_stops_queued_tools_and_preserves_interrupted_state(pi_hook_env, hook_event):
    runtime = pi_hook_env["runtime"]
    run_id = f"review_{hook_event}"
    runtime.start_run(run_id=run_id, task_id=run_id, purpose="evaluation")
    seen = []
    original = pi_hook_env["pkg_tool"].run
    def handler(params):
        seen.append(params)
        return original(params)
    pi_hook_env["pkg_tool"].run = handler
    responses = [{"content": [
        {"type": "toolCall", "id": f"review_call_{i}", "name": "query_order_packages",
         "arguments": {"order_id": "ORD_2026_0901"}} for i in range(2)
    ], "stopReason": "toolUse"}]
    hooks = HarnessHooks(**{hook_event: lambda *args: HookDecision.review("MANUAL_REVIEW")})
    import asyncio
    with pytest.raises((Exception, asyncio.CancelledError)):
        runtime.run_agent_with_pi(run_id=run_id, input_text="query", tools=["query_order_packages"],
            model_config={"provider": "faux", "responses": responses}, test_mode=True, hooks=hooks)
    assert len(seen) == (0 if hook_event == "pre_tool_exec" else 1)
    run = runtime.get_run(run_id)
    assert run.status == "INTERRUPTED"
    assert run.error_type == "REVIEW_REQUIRED"
    assert runtime.get_harness_state(run_id).status == "PAUSED_FOR_REVIEW"
    with pytest.raises(ProcessLookupError):
        os.kill(runtime.get_last_pi_pid(run_id), 0)
