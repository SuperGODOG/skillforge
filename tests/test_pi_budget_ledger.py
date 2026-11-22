"""Tests for Phase 4A2a: Unified LLM Ledger & Budget integration with Pi Evaluation Backend.

Verifies:
1. Unit: Idempotent reservation, duplicate/cross-run receipt protection.
2. Unit: Usage aliases (zero tokens valid vs partial tokens unknown, as_dict null/lower-bound).
3. E2E Pi: Prior Judge call + multi-turn top cap (denied next turn not in captured_provider_inputs).
4. E2E Pi: Known native usage token cap stops next turn.
5. E2E Pi: Missing usage with token ceiling stops unknown.
6. E2E Pi: Failed attempt retained in ledger with no leak across runs.
7. E2E Pi: Deadline hard stop before provider invocation.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator.llm_factory import LLMCallRecord, LLMLedger
from skillforge.models import BudgetExceededError, EvolveBudget
from skillforge.pi_evaluation import PiEvaluationBackend
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)


@pytest.fixture
def eval_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())

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
        "tmp_path": tmp_path,
    }

    runtime.close()


# ============================================================================
# 1. Unit: Idempotent Reservation & Duplicate/Cross-Run Receipt
# ============================================================================


def test_ledger_idempotent_reservation_and_duplicate_receipt():
    ledger = LLMLedger(budget=EvolveBudget(max_calls=5))

    # Initial reservation
    r1 = ledger.reserve_turn(role="agent", call_id="call_001")
    assert r1.call_id == "call_001"
    assert ledger.total_calls == 1

    # Idempotent re-reservation with same call_id must reuse existing record, not add duplicate
    r2 = ledger.reserve_turn(role="agent", call_id="call_001")
    assert r2.call_id == "call_001"
    assert ledger.total_calls == 1

    # Settle turn with real usage
    settled = ledger.settle_turn(call_id="call_001", usage={"input": 40, "output": 20, "totalTokens": 60})
    assert settled is not None
    assert settled.total_tokens == 60
    assert settled.status == "success"

    # Duplicate settlement on already settled record must return settled record and not double-count
    settled_dup = ledger.settle_turn(call_id="call_001", usage={"input": 100, "output": 100})
    assert settled_dup.total_tokens == 60
    assert ledger.total_tokens == 60
    assert ledger.total_calls == 1

    # Settlement for unknown call_id returns None and does not create new records
    settled_unknown = ledger.settle_turn(call_id="non_existent", usage={"input": 50, "output": 50})
    assert settled_unknown is None
    assert ledger.total_calls == 1

    ledger.reserve_turn(call_id="other_run:1")
    assert ledger.cancel_pending(call_id="call_001") == []
    assert ledger.records[-1].status == "pending"


# ============================================================================
# 2. Unit: Usage Aliases (Zero Tokens Valid vs Partial Tokens Unknown)
# ============================================================================


def test_ledger_usage_aliases_zero_valid_and_partial_unknown():
    ledger = LLMLedger(budget=EvolveBudget(max_calls=10))

    # Case A: 0 tokens explicitly provided (is not None, valid measured 0)
    ledger.reserve_turn(role="agent", call_id="c_zero")
    s_zero = ledger.settle_turn(call_id="c_zero", usage={"input": 0, "output": 0})
    assert s_zero.usage_unknown is False
    assert s_zero.total_tokens == 0
    assert s_zero.prompt_tokens == 0
    assert s_zero.completion_tokens == 0

    d_zero = ledger.as_dict()
    assert d_zero["total_tokens"] == 0
    assert d_zero.get("tokens_unknown") is not True

    # Case B: Partial usage (only input provided, output missing and totalTokens missing)
    ledger.reserve_turn(role="agent", call_id="c_partial")
    s_part = ledger.settle_turn(call_id="c_partial", usage={"input": 25})
    assert s_part.usage_unknown is True
    assert s_part.completion_unknown is True
    assert s_part.prompt_tokens == 25
    assert s_part.completion_tokens == 0

    d_part = ledger.as_dict()
    assert d_part["tokens_unknown"] is True
    assert d_part["total_tokens"] is None
    assert d_part["completion_tokens"] is None
    assert d_part["total_tokens_lower_bound"] == 25
    assert d_part["prompt_tokens"] == 25


# ============================================================================
# 3. E2E Pi: Prior Judge Call + Multi-Turn Top Cap
# ============================================================================


def test_pi_budget_prior_judge_call_and_multi_turn_top_cap(eval_env):
    """Prior Judge call + multi-turn agent reaches call ceiling; denied turn is not captured."""
    runtime: AgentRuntime = eval_env["runtime"]
    budget = EvolveBudget(max_calls=3)
    ledger = LLMLedger(budget=budget)

    # 1. Prior Judge call consumed 1 call in the shared ledger
    rec_judge = ledger.reserve_turn(role="judge", call_id="judge_call_1")
    ledger.settle_turn(call_id="judge_call_1", usage={"totalTokens": 50})
    assert ledger.total_calls == 1

    # 2. Pi multi-turn backend configured with responses for 3 potential turns
    order_id = "ORD_2026_0901"
    responses = [
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
            "usage": {"input": 30, "output": 20, "totalTokens": 50},
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
            "usage": {"input": 40, "output": 20, "totalTokens": 60},
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "订单包裹查询完毕"}],
            "stopReason": "stop",
            "usage": {"input": 50, "output": 20, "totalTokens": 70},
        },
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": responses},
        test_mode=True,
        ledger=ledger,
    )

    # 3. Run query: Turn 1 (ledger call 2) allowed, Turn 2 (ledger call 3) allowed, Turn 3 denied!
    with pytest.raises((RuntimeError, BudgetExceededError)):
        backend.run(query=f"请查询订单 {order_id} 物流")

    # Verify: Exactly 2 requests were admitted and captured in runner's captured_provider_inputs.
    # The 3rd request was rejected BEFORE provider invocation.
    last_run_id = list(runtime._last_pi_results.keys())[-1]
    pi_res = runtime._last_pi_results[last_run_id]
    assert len(pi_res.captured_provider_inputs) == 2
    assert ledger.total_calls == 3


# ============================================================================
# 4. E2E Pi: Known Native Usage Token Cap Stops Next Turn
# ============================================================================


def test_pi_budget_known_native_usage_token_cap_stops_next(eval_env):
    """Native usage reported by provider triggers token limit and halts subsequent turn."""
    runtime: AgentRuntime = eval_env["runtime"]
    budget = EvolveBudget(max_tokens=100)
    ledger = LLMLedger(budget=budget)

    order_id = "ORD_2026_0901"
    # Native faux recomputes usage from context, rather than preserving this seed.
    responses = [
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
            "usage": {"input": 70, "output": 50, "totalTokens": 120},
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "完成"}],
            "stopReason": "stop",
            "usage": {"input": 10, "output": 10, "totalTokens": 20},
        },
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": responses},
        test_mode=True,
        ledger=ledger,
    )

    with pytest.raises((RuntimeError, BudgetExceededError)):
        backend.run(query=f"请查询订单 {order_id}")

    # The ledger must match native usage, not the scripted 120; turn 2 is denied.
    last_run_id = list(runtime._last_pi_results.keys())[-1]
    pi_res = runtime._last_pi_results[last_run_id]
    assert len(pi_res.captured_provider_inputs) == 1
    actual_usage = next(m["usage"] for m in pi_res.messages if m.get("role") == "assistant")
    assert actual_usage["totalTokens"] > budget.max_tokens
    assert ledger.total_tokens == actual_usage["totalTokens"]
    assert ledger.last_budget_error is not None
    assert ledger.last_budget_error.cap_type == "token"


# ============================================================================
# 5. E2E Pi: Missing Usage With Token Ceiling Stops Unknown
# ============================================================================


def test_pi_budget_missing_usage_token_limit_stops_unknown(eval_env):
    """Missing turn usage prevents verifying max_tokens ceiling and halts execution."""
    runtime: AgentRuntime = eval_env["runtime"]
    budget = EvolveBudget(max_tokens=500)
    ledger = LLMLedger(budget=budget)

    order_id = "ORD_2026_0901"
    # Turn 1 has no usage information
    responses = [
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
            "usage": None,
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "完成"}],
            "stopReason": "stop",
            "usage": {"input": 10, "output": 10, "totalTokens": 20},
        },
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": responses},
        test_mode=True,
        ledger=ledger,
    )

    with pytest.raises((RuntimeError, BudgetExceededError)):
        backend.run(query=f"请查询订单 {order_id}")

    # Turn 1 admitted; upon receipt settlement, TOKEN_USAGE_UNKNOWN blocks turn 2
    last_run_id = list(runtime._last_pi_results.keys())[-1]
    pi_res = runtime._last_pi_results[last_run_id]
    assert len(pi_res.captured_provider_inputs) == 1
    assert ledger.has_unknown_tokens is True
    assert ledger.last_budget_error is not None
    assert "TOKEN_USAGE_UNKNOWN" in ledger.last_budget_error.reason


# ============================================================================
# 6. E2E Pi: Failed Attempt Retained In Ledger With No Leak
# ============================================================================


def test_pi_budget_failed_attempt_retained_no_leak(eval_env):
    """Subprocess error settles pending reservations as error without polluting other runs."""
    runtime: AgentRuntime = eval_env["runtime"]
    budget = EvolveBudget(max_calls=5)
    ledger = LLMLedger(budget=budget)

    # Model returns an assistant-level error
    responses = [
        {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": "Simulated provider failure",
        }
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        model_config={"provider": "faux", "responses": responses},
        test_mode=True,
        ledger=ledger,
    )

    with pytest.raises(RuntimeError, match="error: Simulated provider failure"):
        backend.run(query="test failure attempt retention")

    # Verification: Failed call remains in ledger.records, counting towards total_calls
    assert ledger.total_calls == 1
    assert ledger.failed_calls == 1
    rec = ledger.records[0]
    assert rec.status == "error"
    assert rec.error_type == "Simulated provider failure"


# ============================================================================
# 7. E2E Pi: Deadline Hard Stop
# ============================================================================


def test_pi_budget_deadline_hard_stop(eval_env):
    """Expired deadline blocks admission before sending request to provider."""
    runtime: AgentRuntime = eval_env["runtime"]
    budget = EvolveBudget(deadline_seconds=0.01)
    ledger = LLMLedger(budget=budget)

    # Let deadline expire
    time.sleep(0.02)

    backend = PiEvaluationBackend(
        runtime=runtime,
        model_config={
            "provider": "faux",
            "responses": [{"role": "assistant", "content": [{"type": "text", "text": "hello"}]}],
        },
        test_mode=True,
        ledger=ledger,
    )

    with pytest.raises((RuntimeError, BudgetExceededError)):
        backend.run(query="test deadline")

    # Should not have admitted any turns to the runner
    assert ledger.last_budget_error is not None
    assert ledger.last_budget_error.cap_type == "deadline"
