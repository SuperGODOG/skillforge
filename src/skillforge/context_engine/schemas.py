"""Data models and schemas for the 4-layer Hierarchical Context Engine.

Layer 1: Protected State (Authoritative, immutable by model)
Layer 2: Historical Work Summary (Model-distilled facts, failed paths, open questions)
Layer 3: External Working Memory & Material Index (Offloaded artifacts, object coverage)
Layer 4: Recent Working Context (Verbatim recent turns)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class ProtectedState:
    """Layer 1: Immutable authoritative task state reinjected directly by Harness."""

    intent_version: str = "v1"
    current_goal: str = ""
    active_constraints: list[str] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    def to_yaml_block(self) -> str:
        """Render protected state into strict YAML-like block for prompt inclusion."""
        lines = ["[PROTECTED_SYSTEM_STATE (AUTHORITATIVE)]"]
        lines.append(f"intent_version: {self.intent_version}")
        lines.append(f"current_goal: {self.current_goal}")
        lines.append("active_constraints:")
        for c in self.active_constraints:
            lines.append(f"  - {c}")
        if self.pending_approvals:
            lines.append("pending_approvals:")
            for p in self.pending_approvals:
                lines.append(f"  - {p}")
        else:
            lines.append("pending_approvals: []")
        if self.allowed_tools:
            lines.append(f"allowed_tools: [{', '.join(self.allowed_tools)}]")
        return "\n".join(lines)


@dataclass
class VerifiedFact:
    """Single verified business fact backed by tool evidence."""

    fact: str
    evidence_ref: str
    timestamp: str = ""
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"


@dataclass
class FailedAttempt:
    """Record of an action that failed or encountered an anomaly."""

    action: str
    reason: str
    resolution: str = ""


@dataclass
class WorkSummary:
    """Layer 2: Structured summary of historical facts and attempts."""

    verified_facts: list[VerifiedFact] = field(default_factory=list)
    failed_attempts: list[FailedAttempt] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    structured_entities: dict[str, Any] = field(default_factory=dict)

    def to_yaml_block(self) -> str:
        """Render knowledge base into clean structured section."""
        lines = ["[HISTORICAL_KNOWLEDGE_BASE]"]
        if self.verified_facts:
            lines.append("verified_facts:")
            for f in self.verified_facts:
                lines.append(f"  - fact: {f.fact}")
                lines.append(f"    evidence_ref: {f.evidence_ref}")
        else:
            lines.append("verified_facts: []")

        if self.failed_attempts:
            lines.append("failed_attempts:")
            for fa in self.failed_attempts:
                lines.append(f"  - action: {fa.action}")
                lines.append(f"    reason: {fa.reason}")
                if fa.resolution:
                    lines.append(f"    resolution: {fa.resolution}")
        else:
            lines.append("failed_attempts: []")

        if self.open_questions:
            lines.append("open_questions:")
            for q in self.open_questions:
                lines.append(f"  - {q}")
        else:
            lines.append("open_questions: []")

        if self.structured_entities:
            lines.append("structured_entities:")
            for k, v in self.structured_entities.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


@dataclass
class AttachmentRef:
    """Reference to an offloaded large observation or disk artifact."""

    ref_id: str
    description: str
    total_items: int = 0
    expanded_items: int = 0
    item_keys: list[str] = field(default_factory=list)
    access_method: str = ""


@dataclass
class MemoryIndex:
    """Layer 3: Registry of offloaded attachments and object coverage."""

    attachments: list[AttachmentRef] = field(default_factory=list)
    object_coverage: dict[str, Any] = field(default_factory=dict)

    def to_yaml_block(self) -> str:
        """Render attachment registry for prompt inclusion."""
        lines = ["[ATTACHMENTS_INDEX]"]
        if self.attachments:
            for att in self.attachments:
                keys_preview = f" ({', '.join(att.item_keys[:5])}...)" if att.item_keys else ""
                lines.append(
                    f"- {att.ref_id}: {att.description} [total_items: {att.total_items}, "
                    f"expanded_items: {att.expanded_items}{keys_preview}]"
                )
                if att.access_method:
                    lines.append(f"  access_via: {att.access_method}")
        else:
            lines.append("- (No external attachments offloaded)")
        return "\n".join(lines)


@dataclass
class HierarchicalContext:
    """Unified 4-layer context structure."""

    protected: ProtectedState
    summary: WorkSummary = field(default_factory=WorkSummary)
    memory_index: MemoryIndex = field(default_factory=MemoryIndex)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    token_count: int = 0
