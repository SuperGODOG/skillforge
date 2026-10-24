"""Context Rebuilder: Reassembles prompts ensuring strict prefix alignment and authoritative state isolation."""
from __future__ import annotations

import json
from typing import Any, List, Optional

import tiktoken

from .schemas import HierarchicalContext, ProtectedState, WorkSummary, MemoryIndex


class ContextRebuilder:
    """Renders 4-layer HierarchicalContext into standard model messages with prefix alignment."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            self._enc = tiktoken.get_encoding(encoding_name)
        except Exception:
            self._enc = None

    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken cl100k_base with conservative fallback."""
        if not text:
            return 0
        if self._enc:
            try:
                return len(self._enc.encode(text))
            except Exception:
                pass
        return max(1, len(text) // 4)

    def count_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Calculate total tokens across all messages including keys and formatting."""
        total = 0
        for msg in messages:
            total += 4  # message envelope overhead
            content = msg.get("content")
            if isinstance(content, str):
                total += self.count_tokens(content)
            elif isinstance(content, list):
                total += self.count_tokens(json.dumps(content, ensure_ascii=False))

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += self.count_tokens(json.dumps(tool_calls, ensure_ascii=False))
        return total

    def render_to_messages(
        self,
        context: HierarchicalContext,
        base_system_prompt: str = "",
    ) -> list[dict[str, Any]]:
        """Render HierarchicalContext into messages list for LLM inference."""
        system_blocks: list[str] = []

        if base_system_prompt:
            system_blocks.append(base_system_prompt.strip())

        # Layer 1: Protected State (Authoritative, non-malleable by model)
        system_blocks.append(context.protected.to_yaml_block())

        # Layer 2: Historical Work Summary
        system_blocks.append(context.summary.to_yaml_block())

        # Layer 3: Attachments / External Working Memory Index
        system_blocks.append(context.memory_index.to_yaml_block())

        combined_system_prompt = "\n\n".join(system_blocks)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": combined_system_prompt}
        ]

        # Layer 4: Recent working context (verbatim)
        for msg in context.recent_messages:
            # Skip any existing system message in recent messages to avoid duplicate system prompts
            if msg.get("role") == "system":
                continue
            messages.append(msg)

        context.token_count = self.count_messages_tokens(messages)
        return messages
