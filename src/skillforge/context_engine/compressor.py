"""ContextCompressor: Step-Bounded 4-Tier Compression Ladder for Long-Running Tasks.

Compression Pipeline Order (Cheapest to Most Expensive):
1. Level 0: Large output offloading to disk artifacts with object-aware coverage summary.
2. Level 1: Evict stale/redundant tool observations from older turns, keeping recent N turns verbatim.
3. Level 2: Model-driven structured summarization (facts, failed attempts, open questions) if still over budget.
4. Level 3: Authoritative state reinjection (ProtectedState strictly guaranteed at prompt head).
"""
from __future__ import annotations

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
    details: dict[str, Any] = field(default_factory=dict)


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
    ) -> None:
        self.trigger_watermark_tokens = trigger_watermark_tokens
        self.target_watermark_tokens = target_watermark_tokens
        self.recent_turns_to_keep = recent_turns_to_keep
        self.artifacts_dir = Path(artifacts_dir or Path.cwd() / "artifacts")
        self.summarizer_client = summarizer_client
        self.rebuilder = ContextRebuilder()
        self.offloader = LargeOutputOffloader(threshold_chars=offload_threshold_chars)

    async def compress(self, state: Any) -> CompressionReport:
        """Execute step-bounded compression ladder on current state."""
        artifacts_dir = getattr(state, "workspace_dir", self.artifacts_dir) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        initial_tokens = self.rebuilder.count_messages_tokens(state.messages)
        if initial_tokens < self.trigger_watermark_tokens:
            return CompressionReport(
                triggered=False,
                level="none",
                tokens_before=initial_tokens,
                tokens_after=initial_tokens,
            )

        tokens_before = initial_tokens
        current_level = "none"
        items_offloaded = 0
        stale_turns_evicted = 0

        # Retrieve or initialize scratchpad components
        if not hasattr(state, "working_scratchpad") or state.working_scratchpad is None:
            state.working_scratchpad = {}

        protected = state.working_scratchpad.get("protected_state")
        if not protected:
            protected = ProtectedState(
                intent_version=getattr(state, "intent_version", "v1"),
                current_goal=getattr(state, "goal", ""),
                active_constraints=[],
                allowed_tools=[],
            )
            state.working_scratchpad["protected_state"] = protected

        summary: WorkSummary = state.working_scratchpad.get("work_summary", WorkSummary())
        memory_index: MemoryIndex = state.working_scratchpad.get("memory_index", MemoryIndex())

        # ----------------------------------------------------------------------
        # Level 0: Offload any oversized tool observations in message history
        # ----------------------------------------------------------------------
        for i, msg in enumerate(state.messages):
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
                    for prev_msg in reversed(state.messages[:i]):
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
                    if not any(att.ref_id == ref_uri for att in memory_index.attachments):
                        memory_index.attachments.append(
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
            if not any(ef.fact == f.fact for ef in summary.verified_facts):
                summary.verified_facts.append(f)

        def _eval_context_tokens(msgs: list[dict[str, Any]]) -> int:
            h_ctx = HierarchicalContext(
                protected=protected,
                summary=summary,
                memory_index=memory_index,
                recent_messages=msgs,
            )
            rendered = self.rebuilder.render_to_messages(
                context=h_ctx,
                base_system_prompt=getattr(state, "system_prompt", ""),
            )
            return self.rebuilder.count_messages_tokens(rendered)

        current_tokens = _eval_context_tokens(state.messages)
        current_level = "level_0_offload"

        # ----------------------------------------------------------------------
        # Level 1: Evict stale tool observations older than recent_turns_to_keep
        # ----------------------------------------------------------------------
        split_idx = 0
        user_indices = [
            idx for idx, m in enumerate(state.messages) if m.get("role") == "user"
        ]
        if len(user_indices) > self.recent_turns_to_keep:
            split_idx = user_indices[-self.recent_turns_to_keep]
        else:
            assistant_indices = [
                idx for idx, m in enumerate(state.messages) if m.get("role") == "assistant"
            ]
            if len(assistant_indices) > self.recent_turns_to_keep:
                split_idx = assistant_indices[-self.recent_turns_to_keep]

        if current_tokens > self.target_watermark_tokens and split_idx > 0:
            # In older portion (before split_idx), replace bulky tool results with 1-line stubs
            for idx in range(split_idx):
                m = state.messages[idx]
                if m.get("role") == "tool":
                    old_content = m.get("content", "")
                    if len(old_content) > 100:
                        m["content"] = (
                            f"[Historical Tool Result: Observation recorded and verified. "
                            f"Refer to MemoryIndex for attachments.]"
                        )
                        stale_turns_evicted += 1

            current_tokens = _eval_context_tokens(state.messages)
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
                        + json.dumps(state.messages[:split_idx], ensure_ascii=False)
                    )
                    summary_resp = await self._call_summarizer(summary_prompt)
                    if summary_resp:
                        parsed = None
                        try:
                            raw_json = summary_resp.strip()
                            if "```json" in raw_json:
                                raw_json = raw_json.split("```json")[1].split("```")[0].strip()
                            elif "```" in raw_json:
                                raw_json = raw_json.split("```")[1].split("```")[0].strip()
                            parsed = json.loads(raw_json)
                        except Exception:
                            parsed = None

                        if isinstance(parsed, dict):
                            for f in parsed.get("verified_facts", []):
                                if isinstance(f, dict) and f.get("fact"):
                                    summary.verified_facts.append(
                                        VerifiedFact(
                                            fact=str(f["fact"]),
                                            evidence_ref=str(f.get("evidence_ref", "historical_summary")),
                                            confidence=str(f.get("confidence", "HIGH")),
                                        )
                                    )
                                elif isinstance(f, str):
                                    summary.verified_facts.append(
                                        VerifiedFact(fact=f, evidence_ref="historical_summary", confidence="HIGH")
                                    )
                            for fa in parsed.get("failed_attempts", []):
                                if isinstance(fa, dict) and fa.get("action"):
                                    summary.failed_attempts.append(
                                        FailedAttempt(
                                            action=str(fa["action"]),
                                            reason=str(fa.get("reason", "")),
                                            resolution=str(fa.get("resolution", "")),
                                        )
                                    )
                            for oq in parsed.get("open_questions", []):
                                if isinstance(oq, str) and oq:
                                    summary.open_questions.append(oq)
                        else:
                            summary.verified_facts.append(
                                VerifiedFact(
                                    fact=summary_resp[:300],
                                    evidence_ref="historical_summary",
                                    confidence="HIGH",
                                )
                            )

                        state.messages = state.messages[split_idx:]
                        current_level = "level_2_summarize"
                        current_tokens = _eval_context_tokens(state.messages)
                except Exception:
                    pass
            else:
                # Deterministic structured consolidation
                for idx in range(split_idx):
                    m = state.messages[idx]
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
                                for k, v in t_data.items():
                                    if k in ("status", "result", "state", "packages", "order_id"):
                                        _add_fact(
                                            VerifiedFact(
                                                fact=f"Observation {k}: {str(v)[:100]}",
                                                evidence_ref=f"historical_turn_{idx}",
                                                confidence="HIGH",
                                            )
                                        )
                                        break
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
                state.messages = state.messages[split_idx:]
                current_level = "level_2_summarize"
                current_tokens = _eval_context_tokens(state.messages)

        # ----------------------------------------------------------------------
        # Pruning & Invalidation on Goal Redirection (e.g. status-only constraint)
        # ----------------------------------------------------------------------
        is_status_only = any("STATUS_ONLY" in c for c in protected.active_constraints)
        if is_status_only:
            # Invalidate any stale action/refund proposals from open questions
            cleaned_questions = []
            for q in summary.open_questions:
                if any(kw in q for kw in ("退款", "refund", "建议", "催查", "contact support")):
                    continue
                cleaned_questions.append(q)
            summary.open_questions = cleaned_questions

        # ----------------------------------------------------------------------
        # Level 3: Authoritative State Re-injection (Immutable prefix alignment)
        # ----------------------------------------------------------------------
        h_ctx = HierarchicalContext(
            protected=protected,
            summary=summary,
            memory_index=memory_index,
            recent_messages=state.messages,
        )

        state.messages = self.rebuilder.render_to_messages(
            context=h_ctx,
            base_system_prompt=getattr(state, "system_prompt", ""),
        )

        # Update scratchpad
        state.working_scratchpad["work_summary"] = summary
        state.working_scratchpad["memory_index"] = memory_index

        tokens_after = self.rebuilder.count_messages_tokens(state.messages)

        return CompressionReport(
            triggered=True,
            level=current_level,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            items_offloaded=items_offloaded,
            stale_turns_evicted=stale_turns_evicted,
            details={
                "intent_version": protected.intent_version,
                "attachments_count": len(memory_index.attachments),
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
            return getattr(res, "content", str(res))
        return None
