"""ContextCompressor: Step-Bounded 4-Tier Compression Ladder for Long-Running Tasks.

Compression Pipeline Order (Cheapest to Most Expensive):
1. Level 0: Large output offloading to disk artifacts with object-aware coverage summary.
2. Level 1: Evict stale/redundant tool observations from older turns, keeping recent N turns verbatim.
3. Level 2: Model-driven structured summarization (facts, failed attempts, open questions) if still over budget.
4. Level 3: Authoritative state reinjection (ProtectedState strictly guaranteed at prompt head).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import (
    AttachmentRef,
    HierarchicalContext,
    MemoryIndex,
    ProtectedState,
    WorkSummary,
    VerifiedFact,
    FailedAttempt,
)
from .offloader import LargeOutputOffloader
from .rebuilder import ContextRebuilder


@dataclass
class CompressionReport:
    """Detailed audit report of a context compression operation."""

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


def find_safe_split_index(messages: list[dict[str, Any]], recent_turns_to_keep: int) -> int:
    """Find a split index that never separates an assistant message with tool_calls
    from its corresponding tool response messages, and never leaves unclosed tool calls.

    Verifies closure by matching assistant tool_call IDs with tool message tool_call_ids.
    Unclosed interaction blocks (e.g. 2 calls with 1 result) are always kept in recent.
    """
    if recent_turns_to_keep < 0:
        raise ValueError(f"recent_turns_to_keep must be non-negative, got {recent_turns_to_keep}")
    if not messages:
        return 0

    # 1. Collect all tool message IDs and positions
    tool_msg_by_id: dict[str, list[int]] = {}
    for idx, msg in enumerate(messages):
        if msg.get("role") == "tool":
            t_id = msg.get("tool_call_id")
            if t_id:
                tool_msg_by_id.setdefault(t_id, []).append(idx)

    # 2. Build atomic interaction blocks
    blocks: list[dict[str, Any]] = []
    claimed_indices: set[int] = set()

    for i, msg in enumerate(messages):
        if i in claimed_indices:
            continue

        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            tcs = msg.get("tool_calls", [])
            call_ids = [tc.get("id") for tc in tcs if tc.get("id")]

            all_resolved = True
            matched_tool_indices: list[int] = []
            for cid in call_ids:
                if cid in tool_msg_by_id and tool_msg_by_id[cid]:
                    matched_tool_indices.extend(tool_msg_by_id[cid])
                else:
                    all_resolved = False

            block_indices = {i} | set(matched_tool_indices)
            start_idx = min(block_indices)
            end_idx = max(block_indices) + 1

            # Span any in-between messages to avoid breaking adjacent contexts
            span_indices = set(range(start_idx, end_idx))
            claimed_indices.update(span_indices)

            blocks.append({
                "start": start_idx,
                "end": end_idx,
                "is_closed": all_resolved and len(call_ids) > 0,
                "role": "assistant_tools",
            })
        elif role == "tool":
            # Orphaned or already claimed tool message
            claimed_indices.add(i)
            blocks.append({
                "start": i,
                "end": i + 1,
                "is_closed": False,
                "role": "tool",
            })
        else:
            claimed_indices.add(i)
            blocks.append({
                "start": i,
                "end": i + 1,
                "is_closed": True,
                "role": role or "user",
            })

    blocks.sort(key=lambda b: b["start"])

    # 3. An unclosed block can NEVER be in the historical portion
    first_unclosed_idx = len(messages)
    for b in blocks:
        if not b["is_closed"]:
            first_unclosed_idx = min(first_unclosed_idx, b["start"])

    if recent_turns_to_keep == 0:
        return first_unclosed_idx

    user_blocks = [b for b in blocks if b["is_closed"] and b["role"] == "user"]
    if len(user_blocks) > recent_turns_to_keep:
        candidate_split = user_blocks[-recent_turns_to_keep]["start"]
    else:
        closed_interaction_blocks = [
            b for b in blocks
            if b["is_closed"] and b["role"] in ("user", "assistant_tools", "assistant")
        ]
        if len(closed_interaction_blocks) > recent_turns_to_keep:
            candidate_split = closed_interaction_blocks[-recent_turns_to_keep]["start"]
        else:
            candidate_split = 0

    return min(candidate_split, first_unclosed_idx)


class ContextCompressor:
    """Manages context size and preserves task fidelity across long agent trajectories."""

    def __init__(
        self,
        trigger_watermark_tokens: int = 12000,
        target_watermark_tokens: int = 6000,
        recent_turns_to_keep: int = 2,
        offload_threshold_chars: int = 800,
        artifacts_dir: Optional[Path] = None,
        summarizer_client: Optional[Any] = None,
        summary_validator: Optional[Any] = None,
        hard_capacity_tokens: Optional[int] = None,
    ) -> None:
        self.trigger_watermark_tokens = trigger_watermark_tokens
        self.target_watermark_tokens = target_watermark_tokens
        self.hard_capacity_tokens = hard_capacity_tokens
        self.recent_turns_to_keep = recent_turns_to_keep
        self.artifacts_dir = Path(artifacts_dir or Path.cwd() / "artifacts")
        self.summarizer_client = summarizer_client
        self.summary_validator = summary_validator
        self._last_summarizer_usage: Optional[dict[str, Any]] = None
        self.rebuilder = ContextRebuilder()
        self.offloader = LargeOutputOffloader(threshold_chars=offload_threshold_chars)

    async def compress(self, state: Any) -> CompressionReport:
        """Execute step-bounded compression ladder on current state."""
        import time
        t_start = time.perf_counter()
        self._last_summarizer_usage = None
        artifacts_dir = getattr(state, "workspace_dir", self.artifacts_dir) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        initial_tokens = self.rebuilder.count_messages_tokens(state.messages)
        if initial_tokens < self.trigger_watermark_tokens:
            return CompressionReport(
                triggered=False,
                level="none",
                tokens_before=initial_tokens,
                tokens_after=initial_tokens,
                epoch=getattr(state, "context_epoch", 1),
                estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                reason="under_watermark",
            )

        tokens_before = initial_tokens
        current_level = "none"
        items_offloaded = 0
        stale_turns_evicted = 0

        # Snapshot input boundary signature before any computation
        orig_intent = str(getattr(state, "intent_version", "v1"))
        orig_goal = str(getattr(state, "goal", ""))
        orig_step = getattr(state, "step_count", 0)
        orig_tools_len = len(getattr(state, "tool_records", []))
        orig_msgs_len = len(state.messages)
        orig_epoch = getattr(state, "context_epoch", 1)

        # Build candidate working copies (all compression stages operate on candidates)
        candidate_messages = copy.deepcopy(state.messages)
        candidate_scratchpad = copy.deepcopy(getattr(state, "working_scratchpad", {}) or {})
        candidate_protected = candidate_scratchpad.get("protected_state")
        if not candidate_protected:
            candidate_protected = ProtectedState(
                intent_version=orig_intent,
                current_goal=orig_goal,
                active_constraints=[],
                allowed_tools=[],
            )
            candidate_scratchpad["protected_state"] = candidate_protected

        candidate_summary = candidate_scratchpad.get("work_summary", WorkSummary())
        candidate_memory_index = candidate_scratchpad.get("memory_index", MemoryIndex())
        orig_constraints = list(getattr(candidate_protected, "active_constraints", []))

        # ----------------------------------------------------------------------
        # Level 0: Offload any oversized tool observations in message history
        # ----------------------------------------------------------------------
        for i, msg in enumerate(candidate_messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                data = None
                try:
                    data = json.loads(content)
                except Exception:
                    pass

                if self.offloader.should_offload(content, data):
                    call_id = msg.get("tool_call_id") or f"call_{i}"
                    tool_name = "tool_output"
                    # Find tool name from prior assistant call if possible
                    for prev_msg in reversed(candidate_messages[:i]):
                        for tc in prev_msg.get("tool_calls", []):
                            if tc.get("id") == call_id:
                                tool_name = tc.get("function", {}).get("name", "tool")
                                break

                    res = self.offloader.offload(
                        run_id=getattr(state, "run_id", "run"),
                        tool_name=tool_name,
                        call_id=call_id,
                        raw_text=content,
                        data=data,
                        artifacts_dir=artifacts_dir,
                    )

                    msg["content"] = res.in_context_replacement
                    items_offloaded += 1

                    # Register in memory index (deduplicating by ref_id)
                    ref_uri = f"fs://{res.artifact_file.resolve()}"
                    if not any(att.ref_id == ref_uri for att in candidate_memory_index.attachments):
                        candidate_memory_index.attachments.append(
                            AttachmentRef(
                                ref_id=ref_uri,
                                description=f"Output of {tool_name}",
                                total_items=res.total_count,
                                expanded_items=res.expanded_count,
                                item_keys=res.object_ids,
                                access_method=f"inspect_artifact(ref_id='{ref_uri}', object_id='<ITEM_ID>')",
                            )
                        )

        def _add_fact(f: VerifiedFact) -> None:
            if not any(ef.fact == f.fact for ef in candidate_summary.verified_facts):
                candidate_summary.verified_facts.append(f)

        def _eval_context_tokens(msgs: list[dict[str, Any]]) -> int:
            h_ctx = HierarchicalContext(
                protected=candidate_protected,
                summary=candidate_summary,
                memory_index=candidate_memory_index,
                recent_messages=msgs,
            )
            rendered = self.rebuilder.render_to_messages(
                context=h_ctx,
                base_system_prompt=getattr(state, "system_prompt", ""),
            )
            return self.rebuilder.count_messages_tokens(rendered)

        current_tokens = _eval_context_tokens(candidate_messages)
        if items_offloaded > 0:
            current_level = "level_0_offload"

        # ----------------------------------------------------------------------
        # Level 1: Evict stale tool observations older than recent_turns_to_keep
        # ----------------------------------------------------------------------
        split_idx = find_safe_split_index(candidate_messages, self.recent_turns_to_keep)

        if current_tokens > self.target_watermark_tokens and split_idx > 0:
            # In older portion (before split_idx), replace bulky tool results with 1-line stubs
            for idx in range(split_idx):
                m = candidate_messages[idx]
                if m.get("role") == "tool":
                    old_content = m.get("content", "")
                    if len(old_content) > 100:
                        m["content"] = (
                            f"[Historical Tool Result: Observation recorded and verified. "
                            f"Refer to MemoryIndex for attachments.]"
                        )
                        stale_turns_evicted += 1

            current_tokens = _eval_context_tokens(candidate_messages)
            current_level = "level_1_evict_stale"

        # ----------------------------------------------------------------------
        # Level 2: Structured Summarization / Consolidation if still over budget
        # ----------------------------------------------------------------------
        if current_tokens > self.target_watermark_tokens and split_idx > 0:
            if self.summarizer_client:
                try:
                    summary_prompt = (
                        "Extract key verified business facts, failed attempts, and open questions "
                        "from the following history as JSON with keys 'verified_facts', 'failed_attempts', 'open_questions':\n"
                        + json.dumps(candidate_messages[:split_idx], ensure_ascii=False)
                    )
                    summary_resp = await self._call_summarizer(summary_prompt)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    return CompressionReport(
                        triggered=True,
                        level="no_progress",
                        tokens_before=tokens_before,
                        tokens_after=initial_tokens,
                        epoch=orig_epoch,
                        latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                        usage=self._last_summarizer_usage,
                        estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                        reason=f"summarizer_error: {str(err)}",
                    )

                # 1. Reject stale summary if intent, goal, steps, or messages changed mid-compression
                curr_intent = str(getattr(state, "intent_version", "v1"))
                curr_goal = str(getattr(state, "goal", ""))
                curr_step = getattr(state, "step_count", 0)
                curr_tools_len = len(getattr(state, "tool_records", []))
                curr_msgs_len = len(state.messages)
                live_ps = getattr(state, "working_scratchpad", {}).get("protected_state") if hasattr(state, "working_scratchpad") and state.working_scratchpad else None
                curr_ps_intent = str(live_ps.intent_version) if live_ps else curr_intent
                curr_constraints = list(live_ps.active_constraints) if live_ps else orig_constraints

                if (curr_intent != orig_intent or curr_ps_intent != orig_intent or
                    curr_goal != orig_goal or curr_step != orig_step or
                    curr_tools_len != orig_tools_len or curr_msgs_len != orig_msgs_len or
                    curr_constraints != orig_constraints):
                    return CompressionReport(
                        triggered=True,
                        level="rejected_stale_intent",
                        tokens_before=tokens_before,
                        tokens_after=initial_tokens,
                        epoch=orig_epoch,
                        latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                        usage=self._last_summarizer_usage,
                        estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                        reason="intent_changed_mid_compression",
                        deprecated_stale_summary=True,
                        details={
                            "deprecated_stale_summary": True,
                            "expected_intent": orig_intent,
                            "current_intent": curr_intent,
                        },
                    )

                # 2. Canonical structural must-keep validation
                # Authoritative ProtectedState fields are preserved by canonical object, not LLM regurgitation.
                # Validate that candidate_protected retains structural integrity matching snapshot,
                # summary is non-empty, and all evidence_refs are traceable.
                if not summary_resp or not str(summary_resp).strip():
                    return CompressionReport(
                        triggered=True,
                        level="rejected_invalid_summary",
                        tokens_before=tokens_before,
                        tokens_after=initial_tokens,
                        epoch=orig_epoch,
                        latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                        usage=self._last_summarizer_usage,
                        estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                        reason="summary_validation_failed_empty",
                        details={"validation_failed": True, "reason": "empty_summary"},
                    )

                if (candidate_protected.intent_version != orig_intent or
                    candidate_protected.current_goal != orig_goal or
                    candidate_protected.active_constraints != orig_constraints or
                    not candidate_protected.intent_version or
                    not candidate_protected.current_goal):
                    return CompressionReport(
                        triggered=True,
                        level="rejected_invalid_summary",
                        tokens_before=tokens_before,
                        tokens_after=initial_tokens,
                        epoch=orig_epoch,
                        latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                        usage=self._last_summarizer_usage,
                        estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                        reason="summary_validation_failed_structural_integrity",
                        details={"validation_failed": True, "reason": "protected_state_structural_corruption"},
                    )

                # 3. User-injected summary_validator (can only tighten checks; cannot bypass default)
                if self.summary_validator:
                    try:
                        user_valid = self.summary_validator(summary_resp, candidate_protected)
                    except Exception:
                        user_valid = False
                    if not user_valid:
                        return CompressionReport(
                            triggered=True,
                            level="rejected_invalid_summary",
                            tokens_before=tokens_before,
                            tokens_after=initial_tokens,
                            epoch=orig_epoch,
                            latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                            usage=self._last_summarizer_usage,
                            estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                            reason="summary_validation_failed_user_validator",
                            details={"validation_failed": True, "reason": "user_validator_rejected"},
                        )

                parsed = None
                try:
                    raw_json = str(summary_resp).strip()
                    if "```json" in raw_json:
                        raw_json = raw_json.split("```json")[1].split("```")[0].strip()
                    elif "```" in raw_json:
                        raw_json = raw_json.split("```")[1].split("```")[0].strip()
                    parsed = json.loads(raw_json)
                except Exception:
                    parsed = None

                new_facts: list[VerifiedFact] = []
                if isinstance(parsed, dict):
                    for f in parsed.get("verified_facts", []):
                        if isinstance(f, dict) and f.get("fact"):
                            new_facts.append(
                                VerifiedFact(
                                    fact=str(f["fact"]),
                                    evidence_ref=str(f.get("evidence_ref", "historical_summary")),
                                    confidence=str(f.get("confidence", "HIGH")),
                                )
                            )
                        elif isinstance(f, str):
                            new_facts.append(
                                VerifiedFact(fact=f, evidence_ref="historical_summary", confidence="HIGH")
                            )
                    for fa in parsed.get("failed_attempts", []):
                        if isinstance(fa, dict) and fa.get("action"):
                            candidate_summary.failed_attempts.append(
                                FailedAttempt(
                                    action=str(fa["action"]),
                                    reason=str(fa.get("reason", "")),
                                    resolution=str(fa.get("resolution", "")),
                                )
                            )
                    for oq in parsed.get("open_questions", []):
                        if isinstance(oq, str) and oq:
                            candidate_summary.open_questions.append(oq)
                else:
                    new_facts.append(
                        VerifiedFact(
                            fact=str(summary_resp)[:300],
                            evidence_ref="historical_summary",
                            confidence="HIGH",
                        )
                    )

                # Evidence reference traceability check:
                # Evidence refs must refer to known attachments or historical turns
                known_attachment_refs = {a.ref_id for a in candidate_memory_index.attachments}
                for fact in new_facts:
                    ref = fact.evidence_ref
                    if ref and not (ref.startswith("historical_") or ref in known_attachment_refs):
                        return CompressionReport(
                            triggered=True,
                            level="rejected_invalid_summary",
                            tokens_before=tokens_before,
                            tokens_after=initial_tokens,
                            epoch=orig_epoch,
                            latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                            usage=self._last_summarizer_usage,
                            estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                            reason="summary_validation_failed_untraceable_ref",
                            details={"validation_failed": True, "reason": f"untraceable_evidence_ref: {ref}"},
                        )
                    candidate_summary.verified_facts.append(fact)

                candidate_messages = candidate_messages[split_idx:]
                current_level = "level_2_summarize"
                current_tokens = _eval_context_tokens(candidate_messages)
            else:
                # Deterministic structured consolidation
                for idx in range(split_idx):
                    m = candidate_messages[idx]
                    role = m.get("role")
                    content = str(m.get("content", ""))
                    if role == "tool":
                        try:
                            t_data = json.loads(content)
                            if isinstance(t_data, dict) and t_data.get("_type") == "OBJECT_AWARE_OFFLOADED_SUMMARY":
                                _add_fact(
                                    VerifiedFact(
                                        fact=f"Retrieved {t_data.get('total_count')} items via {t_data.get('tool_name', 'tool')}; {t_data.get('expanded_count')} previewed, {t_data.get('unexpanded_count')} unexpanded.",
                                        evidence_ref=f"historical_turn_{idx}",
                                        confidence="HIGH",
                                    )
                                )
                            elif isinstance(t_data, dict):
                                for k in ("packages", "order_id", "status", "result", "state"):
                                    if k in t_data:
                                        _add_fact(
                                            VerifiedFact(
                                                fact=f"Observation {k}: {str(t_data[k])[:300]}",
                                                evidence_ref=f"historical_turn_{idx}",
                                                confidence="HIGH",
                                            )
                                        )
                        except Exception:
                            if len(content) > 15:
                                _add_fact(
                                    VerifiedFact(
                                        fact=f"Observed historical tool output: {content[:80]}",
                                        evidence_ref=f"historical_turn_{idx}",
                                        confidence="MEDIUM",
                                    )
                                )
                    elif role == "user":
                        _add_fact(
                            VerifiedFact(
                                fact=f"User request: {content[:100]}",
                                evidence_ref=f"historical_turn_{idx}",
                                confidence="HIGH",
                            )
                        )
                candidate_messages = candidate_messages[split_idx:]
                current_level = "level_2_summarize"
                current_tokens = _eval_context_tokens(candidate_messages)

        # ----------------------------------------------------------------------
        # Pruning & Invalidation on Goal Redirection (e.g. status-only constraint)
        # ----------------------------------------------------------------------
        is_status_only = any("STATUS_ONLY" in c for c in candidate_protected.active_constraints)
        if is_status_only:
            cleaned_questions = []
            for q in candidate_summary.open_questions:
                if any(kw in q for kw in ("退款", "refund", "建议", "催查", "contact support")):
                    continue
                cleaned_questions.append(q)
            candidate_summary.open_questions = cleaned_questions

        # ----------------------------------------------------------------------
        # Level 3: Authoritative State Re-injection (Immutable prefix alignment)
        # ----------------------------------------------------------------------
        h_ctx = HierarchicalContext(
            protected=candidate_protected,
            summary=candidate_summary,
            memory_index=candidate_memory_index,
            recent_messages=candidate_messages,
        )

        candidate_messages = self.rebuilder.render_to_messages(
            context=h_ctx,
            base_system_prompt=getattr(state, "system_prompt", ""),
        )

        tokens_after = self.rebuilder.count_messages_tokens(candidate_messages)

        hard_cap = self.hard_capacity_tokens or int(self.trigger_watermark_tokens * 1.5)
        if items_offloaded == 0 and stale_turns_evicted == 0 and current_level == "none":
            failures = getattr(state, "_consecutive_compression_failures", 0) + 1
            state._consecutive_compression_failures = failures
            if failures >= 2 or tokens_after > hard_cap:
                state.status = "BUDGET_EXHAUSTED"
                state.error_message = (
                    f"Context exceeds watermark ({tokens_after} >= {self.trigger_watermark_tokens}) "
                    f"and compression cannot make progress (bounded retries exhausted)"
                )
            return CompressionReport(
                triggered=True,
                level="no_progress",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                epoch=orig_epoch,
                latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
                estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
                reason="compression_no_progress",
            )
        elif tokens_after < tokens_before or items_offloaded > 0 or stale_turns_evicted > 0 or current_level != "none":
            state._consecutive_compression_failures = 0

        # Atomic commit: candidate copies replace live state only after all checks pass
        state.messages = candidate_messages
        if not hasattr(state, "working_scratchpad") or state.working_scratchpad is None:
            state.working_scratchpad = {}
        state.working_scratchpad["work_summary"] = candidate_summary
        state.working_scratchpad["memory_index"] = candidate_memory_index
        state.working_scratchpad["protected_state"] = candidate_protected

        new_epoch = orig_epoch
        if items_offloaded > 0 or stale_turns_evicted > 0 or current_level == "level_2_summarize":
            new_epoch = orig_epoch + 1
            state.context_epoch = new_epoch

        return CompressionReport(
            triggered=True,
            level=current_level,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            items_offloaded=items_offloaded,
            stale_turns_evicted=stale_turns_evicted,
            epoch=new_epoch,
            latency_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
            usage=self._last_summarizer_usage,
            estimation_method="tiktoken:cl100k_base" if self.rebuilder._enc else "heuristic_char_div_4",
            reason="watermark_exceeded",
            details={
                "intent_version": candidate_protected.intent_version,
                "attachments_count": len(candidate_memory_index.attachments),
            },
        )

    async def _call_summarizer(self, prompt: str) -> Optional[str]:
        """Invoke optional summarizer LLM."""
        if hasattr(self.summarizer_client, "ainvoke"):
            res = await self.summarizer_client.ainvoke(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system="You are a precise business fact condenser.",
            )
            if hasattr(res, "usage") and isinstance(res.usage, dict):
                self._last_summarizer_usage = dict(res.usage)
            return getattr(res, "content", str(res))
        return None
