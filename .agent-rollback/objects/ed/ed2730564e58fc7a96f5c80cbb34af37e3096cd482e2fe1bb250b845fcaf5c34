"""Construct isolated execution and Judge clients with unified ledger tracking."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import BudgetExceededError, EvolveBudget


def extract_tokens(resp: Any) -> tuple[int, int, int]:
    """Robustly extract (prompt_tokens, completion_tokens, total_tokens) from LLM responses."""
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return 0, 0, 0
    if isinstance(usage, dict):
        p = int(usage.get("prompt_tokens", 0) or 0)
        c = int(usage.get("completion_tokens", 0) or 0)
        t = int(usage.get("total_tokens", 0) or 0)
        if t == 0 and (p > 0 or c > 0):
            t = p + c
        return p, c, t
    p = int(getattr(usage, "prompt_tokens", 0) or 0)
    c = int(getattr(usage, "completion_tokens", 0) or 0)
    t = int(getattr(usage, "total_tokens", 0) or 0)
    if t == 0 and (p > 0 or c > 0):
        t = p + c
    return p, c, t


@dataclass(frozen=True)
class LLMCallRecord:
    role: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    timestamp: float


class LLMLedger:
    """Unified ledger tracking all LLM invocations across evolve iterations."""

    def __init__(self, budget: Optional[EvolveBudget] = None) -> None:
        self.budget = budget or EvolveBudget()
        self.records: list[LLMCallRecord] = []
        self.start_time: float = time.time()
        self.last_budget_error: Optional[BudgetExceededError] = None

    def reset(self, budget: Optional[EvolveBudget] = None) -> None:
        """Reset records and clock for a new evolve round while preserving/updating budget."""
        if budget is not None:
            self.budget = budget
        self.records.clear()
        self.start_time = time.time()
        self.last_budget_error = None

    @property
    def total_calls(self) -> int:
        return len(self.records)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.records)

    @property
    def prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self.records)

    @property
    def completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.records)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def calls_by_role(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            counts[r.role] = counts.get(r.role, 0) + 1
        return counts

    @property
    def tokens_by_role(self) -> dict[str, int]:
        tokens: dict[str, int] = {}
        for r in self.records:
            tokens[r.role] = tokens.get(r.role, 0) + r.total_tokens
        return tokens

    def check_budget(self, role: str = "llm") -> None:
        """Pre-invocation check for deadline, call ceiling, and token ceiling."""
        if self.last_budget_error is not None:
            raise self.last_budget_error

        if self.budget.deadline_seconds is not None:
            elapsed = self.elapsed_seconds
            if elapsed > self.budget.deadline_seconds:
                err = BudgetExceededError(
                    f"DEADLINE_EXCEEDED: elapsed {elapsed:.2f}s > deadline {self.budget.deadline_seconds}s",
                    cap_type="deadline",
                    limit=self.budget.deadline_seconds,
                    current=elapsed,
                )
                self.last_budget_error = err
                raise err

        if self.budget.max_calls is not None:
            if self.total_calls >= self.budget.max_calls:
                err = BudgetExceededError(
                    f"CALL_LIMIT_EXCEEDED: call count {self.total_calls} >= limit {self.budget.max_calls}",
                    cap_type="call",
                    limit=self.budget.max_calls,
                    current=self.total_calls,
                )
                self.last_budget_error = err
                raise err

        if self.budget.max_tokens is not None:
            if self.total_tokens >= self.budget.max_tokens:
                err = BudgetExceededError(
                    f"TOKEN_LIMIT_EXCEEDED: token usage {self.total_tokens} >= limit {self.budget.max_tokens}",
                    cap_type="token",
                    limit=self.budget.max_tokens,
                    current=self.total_tokens,
                )
                self.last_budget_error = err
                raise err

    def check_post_run_budget(self) -> None:
        """Post-run verification that checks for already occurred budget violations,
        unlike check_budget() which verifies eligibility for the next invocation."""
        if self.last_budget_error is not None:
            raise self.last_budget_error

        if self.budget.deadline_seconds is not None:
            elapsed = self.elapsed_seconds
            if elapsed > self.budget.deadline_seconds:
                err = BudgetExceededError(
                    f"DEADLINE_EXCEEDED: elapsed {elapsed:.2f}s > deadline {self.budget.deadline_seconds}s",
                    cap_type="deadline",
                    limit=self.budget.deadline_seconds,
                    current=elapsed,
                )
                self.last_budget_error = err
                raise err

        if self.budget.max_tokens is not None:
            if self.total_tokens > self.budget.max_tokens:
                err = BudgetExceededError(
                    f"TOKEN_LIMIT_EXCEEDED: token usage {self.total_tokens} > limit {self.budget.max_tokens}",
                    cap_type="token",
                    limit=self.budget.max_tokens,
                    current=self.total_tokens,
                )
                self.last_budget_error = err
                raise err

        if self.budget.max_calls is not None:
            if self.total_calls > self.budget.max_calls:
                err = BudgetExceededError(
                    f"CALL_LIMIT_EXCEEDED: call count {self.total_calls} > limit {self.budget.max_calls}",
                    cap_type="call",
                    limit=self.budget.max_calls,
                    current=self.total_calls,
                )
                self.last_budget_error = err
                raise err

    def record_call(self, role: str, resp: Any, latency_ms: float) -> LLMCallRecord:
        """Post-invocation recording and ceiling verification."""
        p_tok, c_tok, t_tok = extract_tokens(resp)
        rec = LLMCallRecord(
            role=role,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            latency_ms=latency_ms,
            timestamp=time.time(),
        )
        self.records.append(rec)

        if self.budget.max_tokens is not None:
            if self.total_tokens > self.budget.max_tokens:
                err = BudgetExceededError(
                    f"TOKEN_LIMIT_EXCEEDED: token usage {self.total_tokens} > limit {self.budget.max_tokens}",
                    cap_type="token",
                    limit=self.budget.max_tokens,
                    current=self.total_tokens,
                )
                self.last_budget_error = err
                raise err

        if self.budget.deadline_seconds is not None:
            elapsed = self.elapsed_seconds
            if elapsed > self.budget.deadline_seconds:
                err = BudgetExceededError(
                    f"DEADLINE_EXCEEDED: elapsed {elapsed:.2f}s > deadline {self.budget.deadline_seconds}s",
                    cap_type="deadline",
                    limit=self.budget.deadline_seconds,
                    current=elapsed,
                )
                self.last_budget_error = err
                raise err

        return rec

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "calls_by_role": self.calls_by_role,
            "tokens_by_role": self.tokens_by_role,
        }


class TrackedLLM:
    """Transparent proxy wrapper for LLM instances routing through an LLMLedger."""

    def __init__(self, underlying_llm: Any, ledger: LLMLedger, role: str = "llm") -> None:
        self.underlying_llm = underlying_llm
        self.ledger = ledger
        self.role = role

    def with_role(self, role: str) -> TrackedLLM:
        return TrackedLLM(self.underlying_llm, self.ledger, role=role)

    def invoke(self, messages: Any, role: Optional[str] = None, **kwargs: Any) -> Any:
        active_role = role or self.role
        self.ledger.check_budget(role=active_role)
        started = time.perf_counter()
        resp = self.underlying_llm.invoke(messages, **kwargs)
        latency_ms = (time.perf_counter() - started) * 1000
        self.ledger.record_call(role=active_role, resp=resp, latency_ms=latency_ms)
        return resp

    def invoke_with_tools(self, *args: Any, role: Optional[str] = None, **kwargs: Any) -> Any:
        active_role = role or self.role
        self.ledger.check_budget(role=active_role)
        started = time.perf_counter()
        if hasattr(self.underlying_llm, "invoke_with_tools"):
            resp = self.underlying_llm.invoke_with_tools(*args, **kwargs)
        else:
            resp = self.underlying_llm.invoke(*args, **kwargs)
        latency_ms = (time.perf_counter() - started) * 1000
        self.ledger.record_call(role=active_role, resp=resp, latency_ms=latency_ms)
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self.underlying_llm, name)


def _is_mock(obj: Any) -> bool:
    if obj is None:
        return False
    mod = getattr(type(obj), "__module__", "") or ""
    return (
        mod.startswith("unittest.mock")
        or hasattr(obj, "_mock_return_value")
        or hasattr(obj, "_mock_self")
    )


def wrap_with_ledger(llm: Any, ledger: Optional[LLMLedger], role: str = "llm") -> Any:
    """Wrap an LLM instance with a ledger if ledger is provided."""
    if llm is None or ledger is None:
        return llm
    if _is_mock(llm):
        return llm
    if isinstance(llm, TrackedLLM):
        if llm.ledger is ledger and llm.role == role:
            return llm
        return TrackedLLM(llm.underlying_llm, ledger, role=role)
    return TrackedLLM(llm, ledger, role=role)


def compute_evaluator_fingerprint(execution_llm: Any = None, judge_llm: Any = None) -> str:
    """Compute a deterministic 16-character SHA-256 fingerprint of execution & judge configurations."""
    from .judge import JUDGE_PROMPT_VERSION, judge_prompt_sha256, judge_semantic_digest

    def _extract_meta(client: Any, prefix: str) -> dict[str, Any]:
        if client is None:
            return {
                "configured": False,
                "model": os.environ.get(f"{prefix}_MODEL_ID", ""),
                "base_url": os.environ.get(f"{prefix}_BASE_URL", ""),
                "temperature": float(os.environ.get(f"{prefix}_TEMPERATURE", "0")),
            }
        if _is_mock(client):
            return {
                "type": "mock",
                "model": "mock",
                "base_url": "",
                "temperature": 0.0,
                "obj_id": id(client),
            }

        curr = client
        while isinstance(curr, TrackedLLM):
            curr = curr.underlying_llm

        raw_model = getattr(curr, "model", None)
        if _is_mock(raw_model):
            raw_model = None
        model = (
            raw_model
            or getattr(curr, "model_id", None)
            or getattr(curr, "model_name", None)
            or os.environ.get(f"{prefix}_MODEL_ID", "")
        )
        if _is_mock(model):
            model = "mock"

        raw_base = getattr(curr, "base_url", None)
        base_url = raw_base if not _is_mock(raw_base) else ""
        if not base_url:
            base_url = os.environ.get(f"{prefix}_BASE_URL", "")

        temp_val = getattr(curr, "temperature", None)
        if _is_mock(temp_val) or temp_val is None:
            raw_temp = os.environ.get(f"{prefix}_TEMPERATURE")
            temp_val = float(raw_temp) if raw_temp is not None else 0.0

        meta: dict[str, Any] = {
            "type": type(curr).__name__,
            "model": str(model),
            "base_url": str(base_url),
            "temperature": float(temp_val),
        }
        if hasattr(curr, "fingerprint") and not _is_mock(curr.fingerprint):
            meta["fingerprint"] = str(curr.fingerprint)
        elif hasattr(curr, "_test_id") and not _is_mock(curr._test_id):
            meta["_test_id"] = str(curr._test_id)
        elif not model:
            meta["obj_id"] = id(curr)
        return meta

    payload = {
        "execution": _extract_meta(execution_llm, "LLM"),
        "judge": _extract_meta(judge_llm, "JUDGE_LLM"),
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_prompt_sha256": judge_prompt_sha256(),
        "judge_semantic_digest": judge_semantic_digest(),
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_execution_llm(ledger: Optional[LLMLedger] = None) -> Any:
    from hello_agents import HelloAgentsLLM

    llm = HelloAgentsLLM(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ["LLM_MODEL_ID"],
        base_url=os.environ["LLM_BASE_URL"],
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0")),
        timeout=int(os.environ.get("LLM_TIMEOUT", "300")),
    )
    if ledger is not None:
        return wrap_with_ledger(llm, ledger, role="agent")
    return llm


def build_judge_llm(ledger: Optional[LLMLedger] = None) -> Any:
    from hello_agents import HelloAgentsLLM

    llm = HelloAgentsLLM(
        api_key=os.environ["JUDGE_LLM_API_KEY"],
        model=os.environ["JUDGE_LLM_MODEL_ID"],
        base_url=os.environ["JUDGE_LLM_BASE_URL"],
        temperature=float(os.environ.get("JUDGE_LLM_TEMPERATURE", "0")),
        timeout=int(os.environ.get("JUDGE_LLM_TIMEOUT", "300")),
    )
    if ledger is not None:
        return wrap_with_ledger(llm, ledger, role="judge")
    return llm


def build_llm_pair(ledger: Optional[LLMLedger] = None) -> tuple[Any, Any]:
    """Return distinct, deterministic execution and Judge client instances."""
    return build_execution_llm(ledger=ledger), build_judge_llm(ledger=ledger)
