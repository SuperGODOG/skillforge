"""AsyncHarnessLoop: Unified Native asyncio Main Loop for SkillForge.

Provides:
- HarnessTaskSpec: Input contract specifying task, constraints, budget, and system prompts.
- HarnessState: Observable mutable state across the execution trajectory.
- HarnessHooks: 4 explicit hook pairs (pre/post model, pre/post tool, pre/post compress, task_end).
- ModelResponse & ToolCallSpec: Protocol for model interaction.
- AsyncHarnessLoop: The core continuous asyncio execution engine.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional, Union

from .models import ToolCallRecord
from .runtime import AgentRuntime, ToolBroker


@dataclass
class ToolCallSpec:
    """Specification for a single tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    """Response returned from model inference."""

    content: Optional[str] = ""
    tool_calls: list[ToolCallSpec] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    normalized_usage: Optional[dict[str, Any]] = None
    ttft_ms: Optional[float] = None


def normalize_model_usage(raw_usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Standardize provider usage into standard fields without double-counting.

    Returns:
    - input_tokens: total prompt tokens (int or None). For Anthropic, equals
      uncached + cache_read + cache_write when uncached is reported; if uncached is missing,
      denominator is incomplete and input_tokens is None. For OpenAI, equals prompt_tokens.
    - cache_read_tokens: cached tokens read (int or None)
    - cache_write_tokens: cache creation tokens written (int or None)
    - output_tokens: completion/output tokens (int or None)
    - cache_hit_ratio: ratio in [0.0, 1.0] if provider reports cache tokens, else None
    """
    if not raw_usage or not isinstance(raw_usage, dict):
        return {
            "input_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": None,
            "cache_hit_ratio": None,
        }

    # 1. Anthropic format
    if "cache_read_input_tokens" in raw_usage or "cache_creation_input_tokens" in raw_usage:
        read = raw_usage.get("cache_read_input_tokens")
        write = raw_usage.get("cache_creation_input_tokens")
        uncached = raw_usage.get("input_tokens")
        out = raw_usage.get("output_tokens")
        total_prompt = None
        if uncached is not None:
            total_prompt = uncached + (read or 0) + (write or 0)
        hit_ratio = None
        if total_prompt and total_prompt > 0 and read is not None:
            hit_ratio = round(read / total_prompt, 4)
        return {
            "input_tokens": total_prompt,
            "cache_read_tokens": read,
            "cache_write_tokens": write,
            "output_tokens": out,
            "cache_hit_ratio": hit_ratio,
        }

    # 2. OpenAI format with prompt_tokens_details.cached_tokens
    details = raw_usage.get("prompt_tokens_details")
    if isinstance(details, dict) and "cached_tokens" in details:
        cached = details.get("cached_tokens")
        total_prompt = raw_usage.get("prompt_tokens")
        out = raw_usage.get("completion_tokens")
        hit_ratio = round(cached / total_prompt, 4) if total_prompt and total_prompt > 0 and cached is not None else None
        return {
            "input_tokens": total_prompt,
            "cache_read_tokens": cached,
            "cache_write_tokens": None,
            "output_tokens": out,
            "cache_hit_ratio": hit_ratio,
        }

    # 3. Vanilla / generic format (no provider cache fields)
    inp = raw_usage.get("input_tokens") if raw_usage.get("input_tokens") is not None else raw_usage.get("prompt_tokens")
    out = raw_usage.get("output_tokens") if raw_usage.get("output_tokens") is not None else raw_usage.get("completion_tokens")
    return {
        "input_tokens": inp,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": out,
        "cache_hit_ratio": None,
    }


def compute_canonical_prefix_fingerprint(
    system_prompt: str,
    tool_schemas: list[dict[str, Any]],
    epoch: Optional[int] = None,
) -> str:
    """Compute deterministic SHA-256 fingerprint for stable prefix (system rules + tool schemas).
    Excludes epoch, which is tracked separately as context compaction lifecycle."""
    import hashlib
    data = {
        "system": system_prompt.strip(),
        "tools": tool_schemas,
    }
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class HarnessTaskSpec:
    """Input specification for initiating an Agent run in AsyncHarnessLoop."""

    task_id: str
    user_query: str
    run_id: Optional[str] = None
    skill_name: Optional[str] = None
    intent_version: str = "v1"
    system_prompt: str = ""
    budget_max: int = 10
    deadline_seconds: Optional[float] = None
    context_compressor: Optional[Any] = None
    initial_scratchpad: Optional[dict[str, Any]] = None
    verification_fn: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None
    acceptance_criteria: Optional[dict[str, Any]] = None


@dataclass
class CompressionReport:
    """Report generated by context compression hook."""

    triggered: bool = False
    level: str = "none"
    tokens_before: int = 0
    tokens_after: int = 0
    items_offloaded: int = 0
    stale_turns_evicted: int = 0
    epoch: int = 1
    estimation_method: str = "tiktoken:cl100k_base"
    latency_ms: Optional[float] = None
    usage: Optional[dict[str, Any]] = None
    reason: str = "watermark_exceeded"
    deprecated_stale_summary: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookDecision:
    """Explicit structured decision returned by a lifecycle Hook."""

    action: Literal["allow", "deny", "review"] = "allow"
    reason_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    observation_view: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "reason_code": self.reason_code,
            "details": self.details,
            "evidence_refs": self.evidence_refs,
        }
        if self.observation_view is not None:
            d["observation_view"] = self.observation_view
        return d

    @classmethod
    def allow(
        cls,
        reason_code: str = "ALLOW_DEFAULT",
        details: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None,
        observation_view: Optional[str] = None,
    ) -> HookDecision:
        return cls(
            action="allow",
            reason_code=reason_code,
            details=details or {},
            evidence_refs=evidence_refs or [],
            observation_view=observation_view,
        )

    @classmethod
    def deny(
        cls,
        reason_code: str,
        details: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None,
        observation_view: Optional[str] = None,
    ) -> HookDecision:
        return cls(
            action="deny",
            reason_code=reason_code,
            details=details or {},
            evidence_refs=evidence_refs or [],
            observation_view=observation_view,
        )

    @classmethod
    def review(
        cls,
        reason_code: str,
        details: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None,
        observation_view: Optional[str] = None,
    ) -> HookDecision:
        return cls(
            action="review",
            reason_code=reason_code,
            details=details or {},
            evidence_refs=evidence_refs or [],
            observation_view=observation_view,
        )


@dataclass
class HookAuditRecord:
    """Audit record of a single hook execution."""

    run_id: str
    task_id: str
    intent_version: str
    event: str
    hook_id: str
    action: Literal["allow", "deny", "review"]
    reason_code: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: Optional[str] = None
    is_timeout: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


HookCallable = Union[
    Callable[..., Coroutine[Any, Any, Optional[Union[HookDecision, None]]]],
    Callable[..., Optional[Union[HookDecision, None]]],
    Callable[..., Any],
]


@dataclass
class HookSpec:
    """Configured lifecycle hook specification."""

    callback: HookCallable
    hook_id: str = "anonymous_hook"
    timeout_seconds: Optional[float] = None
    critical: Optional[bool] = None  # None -> infer based on event


HookEntry = Union[HookCallable, HookSpec]


@dataclass
class HarnessHooks:
    """The explicit lifecycle hook pairs for observation, instrumentation, and intervention."""

    pre_model_call: Optional[HookEntry] = None
    post_model_call: Optional[HookEntry] = None
    pre_tool_exec: Optional[HookEntry] = None
    post_tool_exec: Optional[HookEntry] = None
    pre_compress: Optional[HookEntry] = None
    post_compress: Optional[HookEntry] = None
    before_finalize: Optional[HookEntry] = None
    task_end: Optional[HookEntry] = None


@dataclass
class HarnessState:
    """Live state of an ongoing Harness run."""

    run_id: str
    task_id: str
    intent_version: str
    goal: str
    system_prompt: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    budget_max: int = 10
    tool_records: list[ToolCallRecord] = field(default_factory=list)
    working_scratchpad: dict[str, Any] = field(default_factory=dict)
    workspace_dir: Path = field(default_factory=Path.cwd)
    status: Literal[
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "BUDGET_EXHAUSTED",
        "PAUSED_FOR_REVIEW",
    ] = "RUNNING"
    final_output: Optional[str] = None
    error_message: Optional[str] = None
    is_cancelled: bool = False
    deadline_ts: Optional[float] = None
    hook_audits: list[dict[str, Any]] = field(default_factory=list)
    context_epoch: int = 1
    request_telemetry: list[dict[str, Any]] = field(default_factory=list)


def create_read_only_constraint_hook(
    tool_effects: Optional[dict[str, str]] = None,
    hook_id: str = "read_only_business_constraint",
    timeout_seconds: Optional[float] = 5.0,
) -> HookSpec:
    """Create a BeforeTool business constraint hook enforcing read-only task constraints.

    Evaluates authoritative frozen task intent (e.g. active_constraints in ProtectedState)
    against explicit tool effects or tool metadata. Does not rely on name matching ('write')
    or LLM self-attestation. Broker still retains final permission authority.
    """
    effects = dict(tool_effects or {})

    async def _check_read_only(state: HarnessState, tc: ToolCallSpec) -> HookDecision:
        active_constraints: list[str] = []
        if "protected_state" in state.working_scratchpad:
            ps = state.working_scratchpad["protected_state"]
            active_constraints = getattr(ps, "active_constraints", []) or []

        is_read_only_task = (
            "READ_ONLY" in active_constraints
            or "STATUS_ONLY" in active_constraints
            or (state.system_prompt and "CONSTRAINT: READ_ONLY" in state.system_prompt)
        )

        if not is_read_only_task:
            return HookDecision.allow(
                reason_code="TASK_NOT_READ_ONLY",
                details={"active_constraints": active_constraints},
            )

        tool_name = tc.name
        tool_effect = effects.get(tool_name)

        is_mutation = tool_effect in ("write", "side_effect", "mutation", "modify")

        evidence_refs = [
            f"run:{state.run_id}",
            f"task:{state.task_id}",
            f"intent:{state.intent_version}",
            f"tool:{tool_name}",
            f"call:{tc.id}",
        ]

        if is_mutation:
            return HookDecision.deny(
                reason_code="READ_ONLY_VIOLATION",
                details={
                    "tool_name": tool_name,
                    "tool_effect": tool_effect,
                    "active_constraints": active_constraints,
                },
                evidence_refs=evidence_refs,
            )

        return HookDecision.allow(
            reason_code="READ_ONLY_COMPLIANT",
            details={"tool_name": tool_name, "tool_effect": tool_effect or "read_only"},
            evidence_refs=evidence_refs,
        )

    return HookSpec(
        callback=_check_read_only,
        hook_id=hook_id,
        timeout_seconds=timeout_seconds,
        critical=True,
    )


def create_sensitive_data_redaction_hook(
    redact_patterns: Optional[dict[str, str]] = None,
    filter_keys: Optional[list[str]] = None,
    hook_id: str = "sensitive_data_redaction",
    timeout_seconds: Optional[float] = 5.0,
    critical: bool = False,
) -> HookSpec:
    """Create an AfterTool (post_tool_exec) hook that sanitizes tool observation views.

    Invariants enforced:
    1. Redacted Observation View: Injects sanitized text into HookDecision.observation_view
       which is placed in state.messages for the model.
    2. Raw Evidence Preserved: Does not alter rec.output_text or rec.output_data in ToolCallRecord.
       Authoritative evidence remains intact in runtime SQLite and Collector.
    3. Fail-Closed on Critical: If marked critical=True and redaction fails or times out,
       denies observation rather than leaking raw sensitive data to the model.
    """
    patterns = dict(redact_patterns or {})
    keys_to_filter = set(filter_keys or [])

    if not patterns and not keys_to_filter:
        patterns = {
            r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+": "[REDACTED_EMAIL]",
        }

    def _redact_value(val: Any) -> Any:
        import re
        if isinstance(val, dict):
            return {
                k: ("[REDACTED]" if k in keys_to_filter else _redact_value(v))
                for k, v in val.items()
            }
        elif isinstance(val, list):
            return [_redact_value(x) for x in val]
        elif isinstance(val, str):
            res = val
            for pat, repl in patterns.items():
                res = re.sub(pat, repl, res)
            return res
        return val

    def _redact_post_tool(state: HarnessState, rec: ToolCallRecord) -> HookDecision:
        if rec.status != "EXECUTED":
            return HookDecision.allow(reason_code="SKIPPED_NON_EXECUTED")

        # Determine sanitized view without modifying raw record
        if rec.output_data and isinstance(rec.output_data, (dict, list)):
            redacted_data = _redact_value(rec.output_data)
            obs_view = json.dumps(redacted_data, ensure_ascii=False)
        else:
            raw_s = rec.output_text or (json.dumps(rec.output_data, ensure_ascii=False) if rec.output_data else "")
            obs_view = _redact_value(raw_s)

        return HookDecision.allow(
            reason_code="DATA_REDACTED",
            details={"filtered_keys": list(keys_to_filter)},
            evidence_refs=[f"call:{rec.call_id}", f"tool:{rec.tool_name}"],
            observation_view=obs_view,
        )

    return HookSpec(
        callback=_redact_post_tool,
        hook_id=hook_id,
        timeout_seconds=timeout_seconds,
        critical=critical,
    )


def create_business_oracle_finalize_hook(
    oracle_fn: Optional[Callable[..., dict[str, Any]]] = None,
    order_id: Optional[str] = None,
    hook_id: str = "business_oracle_finalizer",
    timeout_seconds: Optional[float] = 5.0,
    critical: bool = True,
) -> HookSpec:
    """Create a BeforeFinalize lifecycle hook that evaluates business correctness with an independent Oracle.

    Invariants enforced:
    1. Independent Verification: Does not trust LLM self-attestation.
    2. Hallucination Block: Multi-package logistics claims claiming 'all delivered' when packages are in-transit
       or missing are rejected (action=deny), preventing false delivery to user.
    3. Qualified Refusal: Compliant refusal of unauthorized tools or tool failure admissions are evaluated as
       valid (action=allow).
    4. Deduplication: Caches verdict in state.working_scratchpad['verification_evidence'] so runtime.finalize_run
       reuses it without duplicate evaluation.
    """
    async def _check_finalize(state: HarnessState, proposed_output: str) -> HookDecision:
        fn = oracle_fn or state.working_scratchpad.get("oracle_fn")
        if fn is None:
            return HookDecision.allow(reason_code="NO_ORACLE_AVAILABLE")

        evidence_refs = [
            f"run:{state.run_id}",
            f"task:{state.task_id}",
            f"intent:{state.intent_version}",
        ]

        eff_order_id = order_id or state.working_scratchpad.get("order_id", state.task_id)
        if eff_order_id:
            evidence_refs.append(f"order:{eff_order_id}")

        original_final_output = state.final_output
        if state.final_output is None:
            state.final_output = proposed_output

        try:
            sig = inspect.signature(fn)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if "model_output" in sig.parameters or has_var_kw:
                kwargs: dict[str, Any] = {
                    "model_output": proposed_output,
                    "order_id": eff_order_id,
                    "tool_records": state.tool_records,
                }
                if "infra_error" in sig.parameters or has_var_kw:
                    kwargs["infra_error"] = state.error_message
                verdict = fn(**kwargs)
            elif len(sig.parameters) == 1:
                verdict = fn(state)
            else:
                verdict = fn(state, proposed_output)

            if inspect.isawaitable(verdict):
                verdict = await verdict
        except Exception as exc:
            verdict = {
                "independent_pass": False,
                "failure_reason": f"Oracle execution failed: {type(exc).__name__}: {exc}",
            }
        finally:
            if state.final_output == proposed_output and original_final_output is None:
                state.final_output = original_final_output

        # Cache verdict for ExperienceCollector in finally:
        state.working_scratchpad["verification_evidence"] = verdict
        state._oracle_eval_context = {
            "fn": fn,
            "text": proposed_output,
            "verdict": verdict,
        }

        if not isinstance(verdict, dict):
            return HookDecision.allow(reason_code="ORACLE_NON_DICT_RESULT", details={"raw": str(verdict)})

        is_pass = verdict.get("independent_pass")
        is_hallucination = verdict.get("is_hallucination", False)
        is_qualified_rejection = verdict.get("is_qualified_rejection", False)
        classification = verdict.get("classification", "")

        if is_qualified_rejection or classification == "TRUE_NEGATIVE":
            return HookDecision.allow(
                reason_code="QUALIFIED_REFUSAL_APPROVED",
                details=verdict,
                evidence_refs=evidence_refs,
            )

        if is_hallucination:
            return HookDecision.deny(
                reason_code="ORACLE_HALLUCINATION_DETECTED",
                details=verdict,
                evidence_refs=evidence_refs,
            )

        if is_pass is False:
            return HookDecision.deny(
                reason_code=str(verdict.get("failure_reason") or "ORACLE_VERIFICATION_FAILED"),
                details=verdict,
                evidence_refs=evidence_refs,
            )

        if is_pass is True:
            return HookDecision.allow(
                reason_code="ORACLE_VERIFIED_PASS",
                details=verdict,
                evidence_refs=evidence_refs,
            )

        return HookDecision.allow(
            reason_code="ORACLE_INCONCLUSIVE",
            details=verdict,
            evidence_refs=evidence_refs,
        )

    return HookSpec(
        callback=_check_finalize,
        hook_id=hook_id,
        timeout_seconds=timeout_seconds,
        critical=critical,
    )


def compose_hooks(
    parent: Optional[HarnessHooks],
    child: Optional[HarnessHooks],
) -> HarnessHooks:
    """Compose parent and child lifecycle hooks with strict priority ordering.

    Invariants enforced:
    1. Parent negative constraints (e.g. BeforeTool read-only gate) cannot be
       silently overridden or dropped by child workers.
    2. Before-events evaluate parent first; if parent denies or reviews, child is skipped.
    3. If parent allows, child hook evaluates and can impose additional constraints.
    4. Observation views from child or parent are preserved without raw leaks.
    """
    if parent is None:
        return child or HarnessHooks()
    if child is None:
        return parent

    def _compose_two_hooks(
        h_parent: Optional[HookEntry],
        h_child: Optional[HookEntry],
        event: str,
        is_decision: bool = True,
    ) -> Optional[HookEntry]:
        if h_parent is None:
            return h_child
        if h_child is None:
            return h_parent

        async def _composed_callback(state: HarnessState, *args: Any) -> Any:
            # Special post-processing chain for post_tool_exec:
            # Child worker/formatter evaluates FIRST; parent security gate evaluates SECOND as final defense.
            if event == "post_tool_exec":
                import copy
                rec = args[0] if args else None
                rec_child = copy.copy(rec) if isinstance(rec, ToolCallRecord) else rec

                # 1. Child post-processor evaluates first
                c_res = await _execute_hook(h_child, event, state, rec_child, *args[1:])

                # Prepare copy for parent post-processor reflecting child's transformed observation view
                rec_parent = copy.copy(rec) if isinstance(rec, ToolCallRecord) else rec
                if isinstance(c_res, HookDecision) and c_res.observation_view is not None and isinstance(rec_parent, ToolCallRecord):
                    rec_parent.output_text = c_res.observation_view
                    try:
                        rec_parent.output_data = json.loads(c_res.observation_view)
                    except Exception:
                        rec_parent.output_data = None

                # 2. Parent security gate evaluates second (final line of defense)
                p_res = await _execute_hook(h_parent, event, state, rec_parent, *args[1:])

                if not is_decision or not isinstance(c_res, HookDecision) or not isinstance(p_res, HookDecision):
                    return p_res if p_res is not None else c_res

                # Combine decisions: deny > review > allow (subsequent allow cannot erase prior deny/review)
                if p_res.action == "deny" or c_res.action == "deny":
                    final_action = "deny"
                    lead_dec = p_res if p_res.action == "deny" else c_res
                elif p_res.action == "review" or c_res.action == "review":
                    final_action = "review"
                    lead_dec = p_res if p_res.action == "review" else c_res
                else:
                    final_action = "allow"
                    lead_dec = p_res

                # Final observation_view: parent view overrides/filters child view; inherit child view if parent didn't alter view
                final_view = p_res.observation_view if p_res.observation_view is not None else c_res.observation_view
                final_refs = list(dict.fromkeys((p_res.evidence_refs or []) + (c_res.evidence_refs or [])))

                return HookDecision(
                    action=final_action,
                    reason_code=lead_dec.reason_code,
                    details={**(c_res.details or {}), **(p_res.details or {})},
                    evidence_refs=final_refs,
                    observation_view=final_view,
                )

            # Control gates (before_tool, before_finalize, pre_model_call):
            # Parent evaluates first; if parent denies or reviews, child is skipped immediately.
            p_res = await _execute_hook(h_parent, event, state, *args)
            if is_decision and isinstance(p_res, HookDecision) and p_res.action != "allow":
                return p_res

            # Child evaluates second
            c_res = await _execute_hook(h_child, event, state, *args)
            if is_decision and isinstance(c_res, HookDecision):
                if isinstance(p_res, HookDecision) and p_res.observation_view is not None and c_res.observation_view is None:
                    c_res.observation_view = p_res.observation_view
                return c_res

            return p_res

        p_id = getattr(h_parent, "hook_id", "parent") if isinstance(h_parent, HookSpec) else "parent"
        c_id = getattr(h_child, "hook_id", "child") if isinstance(h_child, HookSpec) else "child"
        return HookSpec(callback=_composed_callback, hook_id=f"{p_id}_plus_{c_id}")

    return HarnessHooks(
        pre_model_call=_compose_two_hooks(parent.pre_model_call, child.pre_model_call, "pre_model_call"),
        post_model_call=_compose_two_hooks(parent.post_model_call, child.post_model_call, "post_model_call", is_decision=False),
        pre_tool_exec=_compose_two_hooks(parent.pre_tool_exec, child.pre_tool_exec, "pre_tool_exec"),
        post_tool_exec=_compose_two_hooks(parent.post_tool_exec, child.post_tool_exec, "post_tool_exec"),
        pre_compress=_compose_two_hooks(parent.pre_compress, child.pre_compress, "pre_compress", is_decision=False),
        post_compress=_compose_two_hooks(parent.post_compress, child.post_compress, "post_compress", is_decision=False),
        before_finalize=_compose_two_hooks(parent.before_finalize, child.before_finalize, "before_finalize"),
        task_end=_compose_two_hooks(parent.task_end, child.task_end, "task_end", is_decision=False),
    )


async def _invoke_hook(hook: Optional[HookCallable], *args: Any) -> None:
    """Legacy helper: safely invoke either synchronous or asynchronous hook callable."""
    if hook is None:
        return
    res = hook(*args)
    if inspect.isawaitable(res):
        await res


async def _execute_hook(
    hook_entry: Optional[HookEntry],
    event: str,
    state: HarnessState,
    *args: Any,
    default_critical: bool = False,
    default_timeout: Optional[float] = None,
) -> HookDecision:
    """Execute a lifecycle hook with timeout, error handling, audit logging, and decision resolution."""
    if hook_entry is None:
        return HookDecision.allow(reason_code="NO_HOOK")

    if isinstance(hook_entry, HookSpec):
        cb = hook_entry.callback
        hook_id = hook_entry.hook_id or getattr(cb, "__name__", "hook")
        timeout = hook_entry.timeout_seconds if hook_entry.timeout_seconds is not None else default_timeout
        is_critical = hook_entry.critical if hook_entry.critical is not None else default_critical
    else:
        cb = hook_entry
        hook_id = getattr(cb, "__name__", "legacy_hook")
        timeout = default_timeout
        is_critical = default_critical

    start_t = time.perf_counter()
    decision = HookDecision.allow(reason_code="ALLOW_DEFAULT")
    error_msg: Optional[str] = None
    is_timeout = False

    try:
        if inspect.iscoroutinefunction(cb):
            coro = cb(state, *args)
            if timeout and timeout > 0:
                raw_res = await asyncio.wait_for(coro, timeout=timeout)
            else:
                raw_res = await coro
        else:
            if timeout and timeout > 0:
                # Execute sync hook in thread with wait_for timeout
                # Inputs use state as-is, late returns cannot mutate state
                raw_res = await asyncio.wait_for(
                    asyncio.to_thread(cb, state, *args),
                    timeout=timeout,
                )
            else:
                raw_res = cb(state, *args)
                if inspect.isawaitable(raw_res):
                    raw_res = await raw_res

        if raw_res is None:
            decision = HookDecision.allow(reason_code="ALLOW_DEFAULT")
        elif isinstance(raw_res, HookDecision):
            if raw_res.action not in ("allow", "deny", "review"):
                error_msg = f"Invalid hook action: {raw_res.action}"
                if is_critical:
                    decision = HookDecision.deny(
                        reason_code="INVALID_HOOK_DECISION",
                        details={"invalid_action": str(raw_res.action)},
                    )
                else:
                    decision = HookDecision.allow(
                        reason_code="OBSERVATION_INVALID_ACTION",
                        details={"invalid_action": str(raw_res.action)},
                    )
            else:
                decision = raw_res
        else:
            error_msg = f"Unsupported return type: {type(raw_res).__name__}"
            if is_critical:
                decision = HookDecision.deny(
                    reason_code="UNSUPPORTED_HOOK_RETURN",
                    details={"return_type": type(raw_res).__name__},
                )
            else:
                decision = HookDecision.allow(reason_code="ALLOW_DEFAULT")

    except asyncio.CancelledError:
        # async CancelledError must NEVER be swallowed; cancellation takes priority!
        raise
    except (asyncio.TimeoutError, TimeoutError):
        is_timeout = True
        error_msg = f"Hook '{hook_id}' timed out after {timeout}s"
        if is_critical:
            decision = HookDecision.deny(
                reason_code="HOOK_TIMEOUT",
                details={"timeout": timeout, "error": error_msg},
            )
        else:
            decision = HookDecision.allow(
                reason_code="OBSERVATION_HOOK_TIMEOUT",
                details={"timeout": timeout, "error": error_msg},
            )
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        if is_critical:
            decision = HookDecision.deny(
                reason_code="HOOK_EXECUTION_ERROR",
                details={"error": error_msg},
            )
        else:
            decision = HookDecision.allow(
                reason_code="OBSERVATION_HOOK_ERROR",
                details={"error": error_msg},
            )

    latency_ms = (time.perf_counter() - start_t) * 1000

    audit_rec = HookAuditRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        intent_version=state.intent_version,
        event=event,
        hook_id=hook_id,
        action=decision.action,
        reason_code=decision.reason_code,
        evidence_refs=list(decision.evidence_refs),
        latency_ms=round(latency_ms, 2),
        error=error_msg,
        is_timeout=is_timeout,
    )
    state.hook_audits.append(audit_rec.to_dict())
    state.working_scratchpad["hook_audits"] = state.hook_audits

    return decision


class AsyncHarnessLoop:
    """The continuous asynchronous ReAct execution engine for SkillForge Harness.

    Orchestrates:
    1. Context construction and prompt injection
    2. Model inference with token usage recording
    3. Tool execution via ToolBroker / AgentRuntime
    4. Observation recording and artifact offloading
    5. Token budget management and context compression hooks
    6. Process and task cancellation handling
    """

    _execute_hook = staticmethod(_execute_hook)

    def __init__(
        self,
        runtime: AgentRuntime,
        tool_broker: ToolBroker,
        llm_client: Any,
        hooks: Optional[HarnessHooks] = None,
        workspace_dir: Optional[Path] = None,
    ) -> None:
        self.runtime = runtime
        self.tool_broker = tool_broker
        self.llm_client = llm_client
        self.hooks = hooks or HarnessHooks()
        self.workspace_dir = Path(workspace_dir or Path.cwd())
        self._current_state: Optional[HarnessState] = None
        self._lock = asyncio.Lock()

        # Auto-register InspectArtifactTool if not already present
        if hasattr(self.tool_broker, "_tools") and "inspect_artifact" not in self.tool_broker._tools:
            try:
                from .context_engine.artifact_tool import InspectArtifactTool
                self.tool_broker.register_tool(
                    InspectArtifactTool(allowed_base_dir=self.workspace_dir),
                    name="inspect_artifact",
                )
            except Exception:
                pass

    def _save_checkpoint(self, state: HarnessState) -> None:
        """Persist point-in-time recovery checkpoint to disk in run workspace."""
        try:
            chk_file = state.workspace_dir / "checkpoint.json"

            def _serialize(obj: Any) -> Any:
                if is_dataclass(obj) and not isinstance(obj, type):
                    return asdict(obj)
                if hasattr(obj, "model_dump"):
                    return obj.model_dump()
                if hasattr(obj, "__dict__"):
                    return {k: _serialize(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
                if isinstance(obj, dict):
                    return {k: _serialize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_serialize(item) for item in obj]
                if isinstance(obj, (str, int, float, bool)) or obj is None:
                    return obj
                return str(obj)

            scratchpad_serializable = _serialize(state.working_scratchpad)

            chk_data = {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "intent_version": state.intent_version,
                "goal": state.goal,
                "system_prompt": state.system_prompt,
                "messages": _serialize(state.messages),
                "step_count": state.step_count,
                "budget_max": state.budget_max,
                "status": state.status,
                "tool_records": _serialize(state.tool_records),
                "working_scratchpad": scratchpad_serializable,
                "final_output": state.final_output,
                "error_message": state.error_message,
                "hook_audits": state.hook_audits,
                "context_epoch": state.context_epoch,
                "request_telemetry": _serialize(state.request_telemetry),
                "timestamp": time.time(),
            }
            chk_file.parent.mkdir(parents=True, exist_ok=True)
            chk_file.write_text(json.dumps(chk_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save checkpoint: %s", exc)

    async def run(self, task_spec: HarnessTaskSpec) -> HarnessState:
        """Run task to completion through the unified async harness loop."""
        run_id = task_spec.run_id or f"run_{uuid.uuid4().hex[:12]}"
        now = time.time()
        deadline_ts = (
            now + task_spec.deadline_seconds
            if task_spec.deadline_seconds
            else None
        )

        # 1. Initialize workspace directory
        run_workspace = self.workspace_dir / "runs" / run_id
        run_workspace.mkdir(parents=True, exist_ok=True)

        # 2. Start run in AgentRuntime
        self.runtime.start_run(
            run_id=run_id,
            task_id=task_spec.task_id,
            skill_name=task_spec.skill_name,
            budget_max=task_spec.budget_max,
            deadline_ts=deadline_ts,
            task_description=task_spec.user_query,
        )

        # 3. Initialize state
        scratchpad = dict(task_spec.initial_scratchpad or {})
        system_prompt = task_spec.system_prompt
        if "protected_state" in scratchpad:
            ps = scratchpad["protected_state"]
            if hasattr(ps, "to_yaml_block"):
                ps_yaml = ps.to_yaml_block()
                if system_prompt:
                    system_prompt = f"{system_prompt}\n\n{ps_yaml}".strip()
                else:
                    system_prompt = ps_yaml

        # Attach frozen skill body from runtime snapshot if skill_name is present
        if task_spec.skill_name and hasattr(self.runtime, "get_run_body"):
            frozen_body = self.runtime.get_run_body(run_id)
            if frozen_body:
                skill_header = f"[FROZEN_SKILL: {task_spec.skill_name}]\n{frozen_body}"
                system_prompt = f"{system_prompt}\n\n{skill_header}".strip() if system_prompt else skill_header

        base_system_prompt = system_prompt.strip()
        state = HarnessState(
            run_id=run_id,
            task_id=task_spec.task_id,
            intent_version=task_spec.intent_version,
            goal=task_spec.user_query,
            system_prompt=base_system_prompt,
            messages=[{"role": "user", "content": task_spec.user_query}],
            step_count=0,
            budget_max=task_spec.budget_max,
            workspace_dir=run_workspace,
            working_scratchpad=scratchpad,
            status="RUNNING",
            deadline_ts=deadline_ts,
            context_epoch=1,
            request_telemetry=[],
        )
        state._base_system_prompt = base_system_prompt

        return await self._run_loop(state, task_spec)

    async def resume(
        self,
        run_id: str,
        updated_spec: Optional[HarnessTaskSpec] = None,
    ) -> HarnessState:
        """Resume an existing run from its on-disk checkpoint with optional updated spec."""
        run_workspace = self.workspace_dir / "runs" / run_id
        chk_file = run_workspace / "checkpoint.json"
        if not chk_file.exists():
            raise FileNotFoundError(f"Checkpoint file not found for run '{run_id}' at '{chk_file}'")

        raw = json.loads(chk_file.read_text(encoding="utf-8"))
        raw_scratch = raw.get("working_scratchpad", {})
        scratchpad: dict[str, Any] = {}

        if "protected_state" in raw_scratch:
            from .context_engine.schemas import ProtectedState
            scratchpad["protected_state"] = ProtectedState(**raw_scratch["protected_state"])
            import hashlib
            ps_init_yaml = scratchpad["protected_state"].to_yaml_block()
            init_ps_hash = hashlib.sha256(ps_init_yaml.encode("utf-8")).hexdigest()
        else:
            init_ps_hash = None
        if "work_summary" in raw_scratch:
            ws_d = raw_scratch["work_summary"]
            from .context_engine.schemas import WorkSummary, VerifiedFact, FailedAttempt
            facts = [VerifiedFact(**f) for f in ws_d.get("verified_facts", [])]
            attempts = [FailedAttempt(**a) for a in ws_d.get("failed_attempts", [])]
            scratchpad["work_summary"] = WorkSummary(
                verified_facts=facts,
                failed_attempts=attempts,
                open_questions=ws_d.get("open_questions", []),
                structured_entities=ws_d.get("structured_entities", {}),
            )
        if "memory_index" in raw_scratch:
            mi_d = raw_scratch["memory_index"]
            from .context_engine.schemas import MemoryIndex, AttachmentRef
            atts = [AttachmentRef(**a) for a in mi_d.get("attachments", [])]
            scratchpad["memory_index"] = MemoryIndex(
                attachments=atts,
                object_coverage=mi_d.get("object_coverage", {}),
            )

        try:
            records = self.runtime.list_tool_calls(raw["run_id"])
        except Exception:
            records = []
        if not records and "tool_records" in raw:
            records = [ToolCallRecord(**r) if isinstance(r, dict) else r for r in raw["tool_records"]]

        audits = raw.get("hook_audits", [])
        if not audits and "hook_audits" in raw_scratch:
            audits = raw_scratch.get("hook_audits", [])

        state = HarnessState(
            run_id=raw["run_id"],
            task_id=raw["task_id"],
            intent_version=raw.get("intent_version", "v1"),
            goal=raw.get("goal", ""),
            system_prompt=raw.get("system_prompt", ""),
            messages=raw.get("messages", []),
            step_count=raw.get("step_count", 0),
            budget_max=raw.get("budget_max", 10),
            workspace_dir=run_workspace,
            working_scratchpad=scratchpad,
            tool_records=records,
            status="RUNNING",
            deadline_ts=None,
            hook_audits=list(audits),
            context_epoch=raw.get("context_epoch", 1),
            request_telemetry=raw.get("request_telemetry", []),
        )
        if init_ps_hash is not None:
            state._last_synced_ps_hash = init_ps_hash

        task_spec = updated_spec or HarnessTaskSpec(
            task_id=state.task_id,
            user_query=state.goal,
            run_id=state.run_id,
            intent_version=state.intent_version,
            budget_max=state.budget_max,
        )

        if updated_spec:
            state.intent_version = updated_spec.intent_version
            state.budget_max = updated_spec.budget_max
            if updated_spec.user_query and updated_spec.user_query != state.goal:
                state.goal = updated_spec.user_query
            if updated_spec.initial_scratchpad and "protected_state" in updated_spec.initial_scratchpad:
                new_ps = updated_spec.initial_scratchpad["protected_state"]
                if "protected_state" in state.working_scratchpad:
                    ps = state.working_scratchpad["protected_state"]
                    ps.intent_version = getattr(new_ps, "intent_version", updated_spec.intent_version)
                    ps.current_goal = getattr(new_ps, "current_goal", updated_spec.user_query or state.goal)
                    if hasattr(new_ps, "active_constraints") and new_ps.active_constraints:
                        ps.active_constraints = list(new_ps.active_constraints)
                    if hasattr(new_ps, "allowed_tools") and new_ps.allowed_tools:
                        ps.allowed_tools = list(new_ps.allowed_tools)
                else:
                    state.working_scratchpad["protected_state"] = new_ps
            elif "protected_state" in state.working_scratchpad:
                ps = state.working_scratchpad["protected_state"]
                ps.intent_version = updated_spec.intent_version
                if updated_spec.user_query:
                    ps.current_goal = updated_spec.user_query

        # Synchronize runtime state for resumed run
        if hasattr(self.runtime, "resume_run"):
            intent_rev = 1
            if state.intent_version.startswith("v") and state.intent_version[1:].isdigit():
                intent_rev = int(state.intent_version[1:])
            self.runtime.resume_run(
                run_id=state.run_id,
                budget_max=state.budget_max,
                deadline_ts=state.deadline_ts,
                intent_revision=intent_rev,
                task_description=state.goal,
            )

        return await self._run_loop(state, task_spec)

    def _get_authorized_artifacts(self, state: HarnessState) -> set[Path]:
        """Collect authorized canonical artifact paths for the current run."""
        authorized: set[Path] = set()
        mi = state.working_scratchpad.get("memory_index")
        if mi and hasattr(mi, "attachments") and isinstance(mi.attachments, list):
            for att in mi.attachments:
                raw_ref = getattr(att, "ref_id", "")
                s = str(raw_ref).strip()
                if s.startswith("fs://"):
                    s = s[5:]
                if s:
                    authorized.add(Path(s).resolve())
        return authorized

    async def _run_loop(self, state: HarnessState, task_spec: HarnessTaskSpec) -> HarnessState:
        """Internal execution loop shared by run() and resume()."""
        async with self._lock:
            self._current_state = state

        # Bind InspectArtifactTool manifest to current run's authorized attachments
        if hasattr(self.tool_broker, "_tools") and "inspect_artifact" in self.tool_broker._tools:
            t_obj = self.tool_broker._tools["inspect_artifact"]
            if hasattr(t_obj, "bind_run_manifest_fn"):
                t_obj.bind_run_manifest_fn(lambda: self._get_authorized_artifacts(state))

        infra_err: Optional[str] = None
        try:
            # Main execution loop
            while state.status == "RUNNING":
                # Check cancellation
                if state.is_cancelled:
                    state.status = "CANCELLED"
                    break

                # Check deadline
                if state.deadline_ts and time.time() > state.deadline_ts:
                    state.status = "TIMED_OUT"
                    state.error_message = "Task deadline exceeded"
                    infra_err = state.error_message
                    break

                # Check budget exhaustion
                if state.step_count >= state.budget_max:
                    state.status = "BUDGET_EXHAUSTED"
                    state.error_message = f"Budget exhausted at {state.step_count} steps"
                    break

                # Hook: pre_model_call
                m_dec = await self._execute_hook(self.hooks.pre_model_call, "pre_model_call", state, default_critical=False)
                if m_dec.action == "deny":
                    state.status = "FAILED"
                    state.error_message = f"Pre-model hook denied execution: {m_dec.reason_code}"
                    break
                elif m_dec.action == "review":
                    state.status = "PAUSED_FOR_REVIEW"
                    state.error_message = f"Pre-model hook paused for review: {m_dec.reason_code}"
                    break

                # Authoritative state synchronization: if protected_state was updated in scratchpad,
                # keep base system_prompt intact (never overwrite frozen skill/base rules)
                if "protected_state" in state.working_scratchpad:
                    ps = state.working_scratchpad["protected_state"]
                    if hasattr(ps, "to_yaml_block"):
                        ps_yaml = ps.to_yaml_block()
                        import hashlib
                        current_ps_hash = hashlib.sha256(ps_yaml.encode("utf-8")).hexdigest()
                        last_ps_hash = getattr(state, "_last_synced_ps_hash", None)
                        if last_ps_hash != current_ps_hash:
                            state._last_synced_ps_hash = current_ps_hash
                            base_sys = getattr(state, "_base_system_prompt", None) or state.system_prompt
                            state.system_prompt = base_sys.strip()
                            if last_ps_hash is not None:
                                update_directive = (
                                    f"[AUTHORITATIVE_UPDATE: INTENT_REDIRECTED]\n"
                                    f"Current Goal: {ps.current_goal}\n"
                                    f"Intent Version: {ps.intent_version}\n"
                                    f"Active Constraints: {', '.join(ps.active_constraints)}"
                                )
                                state.messages.append({"role": "user", "content": update_directive})

                            if state.messages and state.messages[0].get("role") == "system":
                                summary = state.working_scratchpad.get("work_summary")
                                memory_index = state.working_scratchpad.get("memory_index")
                                if summary is not None or memory_index is not None:
                                    from .context_engine.schemas import WorkSummary, MemoryIndex, HierarchicalContext
                                    from .context_engine.rebuilder import ContextRebuilder
                                    h_ctx = HierarchicalContext(
                                        protected=ps,
                                        summary=summary or WorkSummary(),
                                        memory_index=memory_index or MemoryIndex(),
                                        recent_messages=[m for m in state.messages if m.get("role") != "system"],
                                    )
                                    state.messages = ContextRebuilder().render_to_messages(
                                        context=h_ctx,
                                        base_system_prompt="",
                                    )

                # Prepare deterministic tools schema
                tool_schemas = self._get_tool_schemas()

                # Determine prefix fingerprint & telemetry change layer for cache observability
                norm_system = state.system_prompt.strip()
                prefix_fp = compute_canonical_prefix_fingerprint(
                    system_prompt=norm_system,
                    tool_schemas=tool_schemas,
                )

                prev_entry = state.request_telemetry[-1] if state.request_telemetry else None
                if prev_entry is None:
                    change_layer = "initial"
                    change_reason = "initial_turn"
                    client_prefix_matched = False
                elif prev_entry.get("prefix_fingerprint") != prefix_fp:
                    if getattr(state, "_last_synced_ps_hash", None) != getattr(state, "_prev_logged_ps_hash", None):
                        change_layer = "system"
                        change_reason = "authoritative_state_updated"
                    else:
                        change_layer = "tools"
                        change_reason = "tool_definitions_changed"
                    client_prefix_matched = False
                elif state.context_epoch > prev_entry.get("epoch", 1):
                    change_layer = "compaction"
                    change_reason = "tiered_context_compression"
                    client_prefix_matched = True
                else:
                    change_layer = "none"
                    change_reason = "append_turn"
                    client_prefix_matched = True

                state._prev_logged_ps_hash = getattr(state, "_last_synced_ps_hash", None)

                # Call Model
                model_resp, ttft_ms, total_latency_ms = await self._call_model(
                    messages=state.messages,
                    tools=tool_schemas,
                    system=norm_system,
                )

                raw_usage = getattr(model_resp, "usage", {}) or {}
                norm_usage = normalize_model_usage(raw_usage)
                model_resp.normalized_usage = norm_usage
                model_resp.ttft_ms = ttft_ms

                state.request_telemetry.append({
                    "turn": state.step_count,
                    "epoch": state.context_epoch,
                    "prefix_fingerprint": prefix_fp,
                    "client_prefix_matched": client_prefix_matched,
                    "prefix_change_layer": change_layer,
                    "prefix_change_reason": change_reason,
                    "latency_ms": round(total_latency_ms, 2),
                    "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
                    "usage": norm_usage,
                })

                # Hook: post_model_call
                await self._execute_hook(self.hooks.post_model_call, "post_model_call", state, model_resp, default_critical=False)

                # If model provides direct final output with no tool calls, task completes
                if not model_resp.tool_calls:
                    proposed_output = model_resp.content or ""
                    state.working_scratchpad["proposed_output"] = proposed_output

                    # Invalidate any prior single-attempt oracle evaluation cache if proposed text changed
                    prev_eval = getattr(state, "_oracle_eval_context", None)
                    if prev_eval and prev_eval.get("text") != proposed_output:
                        state._oracle_eval_context = None

                    # 1. Evaluate custom before_finalize hook if registered
                    fin_dec = HookDecision.allow()
                    if self.hooks.before_finalize is not None:
                        fin_dec = await self._execute_hook(
                            self.hooks.before_finalize,
                            "before_finalize",
                            state,
                            proposed_output,
                            default_critical=True,
                        )

                    # Custom hook denial or review takes immediate effect (skips expensive oracle)
                    if fin_dec.action == "deny":
                        state.status = "FAILED"
                        state.final_output = f"[DELIVERY_BLOCKED]: {fin_dec.reason_code}"
                        state.error_message = f"Final delivery blocked by BeforeFinalize hook ({fin_dec.reason_code})"
                        if "verification_evidence" not in state.working_scratchpad:
                            state.working_scratchpad["verification_evidence"] = {
                                "independent_pass": False,
                                "failure_reason": f"Denied by BeforeFinalize hook: {fin_dec.reason_code}",
                            }
                        state.messages.append({
                            "role": "assistant",
                            "content": f"[PROPOSED_REJECTED]: {proposed_output}",
                        })
                        break
                    elif fin_dec.action == "review":
                        state.status = "PAUSED_FOR_REVIEW"
                        state.working_scratchpad["proposed_output"] = proposed_output
                        state.error_message = f"Final delivery paused for review: {fin_dec.reason_code}"
                        break

                    # Candidate text to be delivered: judge what is actually delivered
                    effective_output = fin_dec.observation_view or proposed_output

                    # 2. Independent Oracle verification_fn check (if configured)
                    # Authoritative verification_fn cannot be overridden by custom hook allow or stale scratchpad evidence.
                    oracle_dec = HookDecision.allow()
                    if task_spec.verification_fn is not None:
                        eval_ctx = getattr(state, "_oracle_eval_context", None)
                        if eval_ctx and eval_ctx.get("fn") is task_spec.verification_fn and eval_ctx.get("text") == effective_output:
                            verdict = eval_ctx.get("verdict")
                            if isinstance(verdict, dict):
                                is_pass = verdict.get("independent_pass")
                                is_hallucination = verdict.get("is_hallucination", False)
                                is_qualified_rejection = verdict.get("is_qualified_rejection", False)
                                classification = verdict.get("classification", "")
                                if is_qualified_rejection or classification == "TRUE_NEGATIVE":
                                    oracle_dec = HookDecision.allow(reason_code="QUALIFIED_REFUSAL_APPROVED", details=verdict)
                                elif is_pass is True and not is_hallucination:
                                    oracle_dec = HookDecision.allow(reason_code="ORACLE_VERIFIED_PASS", details=verdict)
                                else:
                                    reason = "ORACLE_HALLUCINATION_DETECTED" if is_hallucination else "ORACLE_INVARIANT_VIOLATION"
                                    oracle_dec = HookDecision.deny(reason_code=reason, details=verdict)
                            else:
                                oracle_dec = HookDecision.allow(reason_code="ORACLE_NON_DICT_RESULT")
                        else:
                            oracle_gate = create_business_oracle_finalize_hook(
                                oracle_fn=task_spec.verification_fn,
                                hook_id="task_spec_verification_gate",
                                critical=True,
                            )
                            oracle_dec = await self._execute_hook(
                                oracle_gate,
                                "before_finalize",
                                state,
                                effective_output,
                                default_critical=True,
                            )

                        if oracle_dec.action == "deny":
                            state.status = "FAILED"
                            state.final_output = f"[DELIVERY_BLOCKED]: {oracle_dec.reason_code}"
                            state.error_message = f"Final delivery blocked by Verification Oracle ({oracle_dec.reason_code})"
                            state.messages.append({
                                "role": "assistant",
                                "content": f"[PROPOSED_REJECTED]: {proposed_output}",
                            })
                            break
                        elif oracle_dec.action == "review":
                            state.status = "PAUSED_FOR_REVIEW"
                            state.working_scratchpad["proposed_output"] = proposed_output
                            state.error_message = f"Final delivery paused for review: {oracle_dec.reason_code}"
                            break

                    state.status = "COMPLETED"
                    state.final_output = oracle_dec.observation_view or fin_dec.observation_view or proposed_output
                    state.messages.append({
                        "role": "assistant",
                        "content": state.final_output,
                    })
                    break

                # Record assistant message with tool calls
                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": model_resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": tc.arguments,
                            },
                        }
                        for tc in model_resp.tool_calls
                    ],
                }
                state.messages.append(asst_msg)

                # Execute requested tools
                for tc in model_resp.tool_calls:
                    if state.is_cancelled:
                        state.status = "CANCELLED"
                        break

                    # Hook: pre_tool_exec
                    decision = await self._execute_hook(
                        self.hooks.pre_tool_exec,
                        "pre_tool_exec",
                        state,
                        tc,
                        default_critical=True,
                    )

                    if decision.action == "deny":
                        # Deny: handler is NOT executed (handler_count remains 0)
                        err_msg = f"ERROR [PERMISSION_DENIED]: Tool '{tc.name}' denied by business constraint hook ({decision.reason_code})"
                        if decision.details:
                            err_msg += f" details={json.dumps(decision.details, ensure_ascii=False)}"
                        state.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": err_msg,
                        })
                        rec = ToolCallRecord(
                            call_id=f"hook_deny_{uuid.uuid4().hex[:8]}",
                            run_id=state.run_id,
                            tool_name=tc.name,
                            status="REJECTED",
                            input_params=tc.arguments,
                            error_type="HOOK_DENIED",
                            error_message=decision.reason_code or "Denied by business constraint hook",
                            latency_ms=0.0,
                            created_at=datetime.now(timezone.utc).isoformat(),
                        )
                        state.tool_records.append(rec)
                        if hasattr(self.runtime, "_persist_tool_call"):
                            try:
                                self.runtime._persist_tool_call(rec)
                            except Exception:
                                pass
                        # Hook: post_tool_exec (observation)
                        await self._execute_hook(
                            self.hooks.post_tool_exec,
                            "post_tool_exec",
                            state,
                            rec,
                            default_critical=False,
                        )
                        continue

                    if decision.action == "review":
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"Task paused for review by hook on tool '{tc.name}': {decision.reason_code}"
                        break

                    # Execute via AgentRuntime / ToolBroker in thread to avoid blocking event loop
                    rec = await asyncio.to_thread(
                        self.runtime.execute_tool,
                        run_id=state.run_id,
                        tool_name=tc.name,
                        parameters=tc.arguments,
                    )
                    state.tool_records.append(rec)

                    if state.is_cancelled:
                        state.status = "CANCELLED"
                        break

                    # Hook: post_tool_exec (Called BEFORE appending to messages so observation view can be tailored)
                    post_dec = await self._execute_hook(
                        self.hooks.post_tool_exec,
                        "post_tool_exec",
                        state,
                        rec,
                        default_critical=False,
                    )

                    if post_dec.action == "deny":
                        # F4: Public message contains only reason code and safe summary; details remain in audit
                        res_str = f"ERROR [RESULT_DENIED]: Post-tool hook denied observation ({post_dec.reason_code})"
                        state.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": res_str,
                        })
                        continue

                    if post_dec.action == "review":
                        state.status = "PAUSED_FOR_REVIEW"
                        state.error_message = f"Task paused for review after tool '{tc.name}': {post_dec.reason_code}"
                        break

                    # Format tool observation (allowing observation_view from hook)
                    if post_dec.observation_view is not None:
                        res_str = post_dec.observation_view
                        try:
                            parsed_view = json.loads(post_dec.observation_view)
                            data_for_offload = parsed_view if isinstance(parsed_view, (dict, list)) else None
                        except Exception:
                            data_for_offload = None
                    else:
                        if rec.status == "EXECUTED":
                            if rec.output_data and isinstance(rec.output_data, (dict, list)):
                                res_str = json.dumps(rec.output_data, ensure_ascii=False)
                            else:
                                res_str = rec.output_text or json.dumps(rec.output_data, ensure_ascii=False)
                            data_for_offload = rec.output_data
                        elif rec.status == "REJECTED":
                            res_str = f"ERROR [PERMISSION_DENIED]: {rec.error_message}"
                            data_for_offload = None
                        else:
                            res_str = f"ERROR [{rec.error_type}]: {rec.error_message}"
                            data_for_offload = None

                    # Check large output offloading to disk artifacts (only when context_compressor is not managing context)
                    if rec.status == "EXECUTED" and task_spec.context_compressor is None:
                        try:
                            from .context_engine.offloader import LargeOutputOffloader
                            offloader = LargeOutputOffloader(threshold_chars=1500)
                            if offloader.should_offload(res_str, data=data_for_offload):
                                artifacts_dir = state.workspace_dir / "artifacts"
                                offload_res = offloader.offload(
                                    run_id=state.run_id,
                                    tool_name=rec.tool_name,
                                    call_id=rec.call_id,
                                    raw_text=res_str,
                                    data=data_for_offload,
                                    artifacts_dir=artifacts_dir,
                                )
                                if offload_res.is_offloaded:
                                    res_str = offload_res.in_context_replacement
                                    if "memory_index" not in state.working_scratchpad:
                                        from .context_engine.schemas import MemoryIndex
                                        state.working_scratchpad["memory_index"] = MemoryIndex()
                                    if offload_res.artifact_file:
                                        from .context_engine.schemas import AttachmentRef
                                        state.working_scratchpad["memory_index"].attachments.append(
                                            AttachmentRef(
                                                ref_id=f"fs://{offload_res.artifact_file.resolve()}",
                                                description=f"{rec.tool_name}_artifact",
                                                total_items=getattr(offload_res, "total_items", 0),
                                            )
                                        )
                        except Exception:
                            pass

                    # Append tool result to context
                    state.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": res_str,
                    })

                if state.status == "PAUSED_FOR_REVIEW":
                    break

                # Hook: pre_compress
                await self._execute_hook(self.hooks.pre_compress, "pre_compress", state, default_critical=False)

                # Context compression hook point
                report = CompressionReport(triggered=False, level="none")
                if task_spec.context_compressor and hasattr(task_spec.context_compressor, "compress"):
                    report = await task_spec.context_compressor.compress(state)

                # Hook: post_compress
                await self._execute_hook(self.hooks.post_compress, "post_compress", state, report, default_critical=False)

                if state.status in ("BUDGET_EXHAUSTED", "FAILED", "PAUSED_FOR_REVIEW"):
                    break

                state.step_count += 1

                # Persist point-in-time checkpoint
                self._save_checkpoint(state)

        except Exception as exc:
            state.status = "FAILED"
            state.error_message = f"{type(exc).__name__}: {exc}"
            infra_err = state.error_message
        finally:
            verif_ev = None
            if "verification_evidence" in state.working_scratchpad:
                verif_ev = state.working_scratchpad["verification_evidence"]
            elif task_spec.verification_fn and state.status not in ("FAILED", "TIMED_OUT", "CANCELLED", "PAUSED_FOR_REVIEW"):
                try:
                    verif_ev = task_spec.verification_fn(state)
                except Exception as ve_err:
                    verif_ev = {"independent_pass": False, "failure_reason": f"Verification error: {ve_err}"}

            # Finalize run in runtime (only if not paused for review)
            if state.status != "PAUSED_FOR_REVIEW":
                self.runtime.finalize_run(
                    run_id=state.run_id,
                    model_output=state.final_output or "",
                    verification_evidence=verif_ev,
                    acceptance_criteria=task_spec.acceptance_criteria,
                    infra_error=infra_err,
                )

            # Hook: task_end (Execute BEFORE terminal checkpoint so task_end audit is saved!)
            if state.status != "PAUSED_FOR_REVIEW":
                await self._execute_hook(self.hooks.task_end, "task_end", state, default_critical=False)

            # Persist terminal checkpoint AFTER task_end
            self._save_checkpoint(state)

            async with self._lock:
                self._current_state = None

        return state

    async def cancel(self, reason: str = "User cancelled") -> None:
        """Cancel ongoing execution gracefully."""
        async with self._lock:
            state = self._current_state
            if state:
                state.is_cancelled = True
                state.error_message = reason
                self.runtime.cancel_run(state.run_id, reason=reason)

    def _get_tool_schemas(self) -> list[dict[str, Any]]:
        """Extract tool schemas from ToolBroker deterministically sorted by name and parameter keys."""
        schemas = []
        for name in sorted(self.tool_broker.application_allowlist):
            params = self.tool_broker.get_parameters(name)
            properties: dict[str, Any] = {}
            required: list[str] = []
            for p in sorted(params, key=lambda x: x.name):
                properties[p.name] = {
                    "type": p.type or "string",
                    "description": p.description or "",
                }
                if p.required:
                    required.append(p.name)

            schemas.append({
                "name": name,
                "description": f"Tool {name}",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": sorted(required),
                },
            })
        return schemas

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> tuple[ModelResponse, Optional[float], float]:
        """Invoke LLM client asynchronously, measuring streaming TTFT (null if batch) and total latency."""
        t_start = time.perf_counter()
        ttft_ms: Optional[float] = None

        if hasattr(self.llm_client, "chat_stream"):
            content_chunks: list[str] = []
            tool_calls: list[Any] = []
            stream_usage: Optional[dict[str, Any]] = None

            async for chunk in self.llm_client.chat_stream(
                messages=messages, tools=tools, system=system
            ):
                chunk_text = ""
                if isinstance(chunk, str):
                    chunk_text = chunk
                elif isinstance(chunk, dict):
                    chunk_text = chunk.get("content") or ""
                    if chunk.get("tool_calls"):
                        tool_calls.extend(chunk["tool_calls"])
                    if chunk.get("usage"):
                        stream_usage = dict(chunk["usage"])
                elif hasattr(chunk, "content"):
                    chunk_text = getattr(chunk, "content", "") or ""
                    if hasattr(chunk, "tool_calls") and chunk.tool_calls:
                        tool_calls.extend(chunk.tool_calls)
                    if hasattr(chunk, "usage") and chunk.usage:
                        stream_usage = dict(chunk.usage)

                if ttft_ms is None and chunk_text and len(chunk_text.strip()) > 0:
                    ttft_ms = (time.perf_counter() - t_start) * 1000.0

                if chunk_text:
                    content_chunks.append(chunk_text)

            norm_tool_calls = []
            for tc in tool_calls:
                if isinstance(tc, ToolCallSpec):
                    norm_tool_calls.append(tc)
                elif isinstance(tc, dict):
                    raw_args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                            if not isinstance(parsed_args, dict):
                                raise ValueError(f"Incremental or non-dict tool arguments unsupported: {raw_args}")
                            raw_args = parsed_args
                        except json.JSONDecodeError:
                            raise ValueError(f"Incremental streaming tool argument delta cannot be safely executed: {raw_args}")
                    norm_tool_calls.append(ToolCallSpec(
                        id=tc.get("id", f"call_{len(norm_tool_calls)}"),
                        name=tc.get("name") or tc.get("function", {}).get("name", ""),
                        arguments=raw_args if isinstance(raw_args, dict) else {},
                    ))
                else:
                    norm_tool_calls.append(tc)

            resp = ModelResponse(
                content="".join(content_chunks),
                tool_calls=norm_tool_calls,
                usage=stream_usage,
            )
        elif hasattr(self.llm_client, "ainvoke"):
            resp = await self.llm_client.ainvoke(
                messages=messages, tools=tools, system=system
            )
        elif callable(self.llm_client):
            res = self.llm_client(messages=messages, tools=tools, system=system)
            if inspect.isawaitable(res):
                resp = await res
            else:
                resp = res
        else:
            raise TypeError(f"Unsupported LLM client type: {type(self.llm_client)}")

        total_latency_ms = (time.perf_counter() - t_start) * 1000.0
        return resp, ttft_ms, total_latency_ms
