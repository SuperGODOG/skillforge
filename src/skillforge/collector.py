"""Automatic Experience Collector (Milestone 3a)

Hooks into agent/tool execution lifecycle and automatically persists immutable Episodes
at terminal state.

Core Invariants:
1. Automatic terminal persistence: Episode is assembled and saved to EpisodeStore upon run finish.
2. Trusted verification boundary: Outcome (success/failure/unknown) is strictly derived from
   independent verification evidence, NEVER from untrusted model text or tool output.
3. Start-time version capture: Skill version is captured at run start and cannot drift if registry updates mid-run.
4. Failure & recovery fidelity: Intermittent tool failures and subsequent recovery calls are preserved in order.
5. Idempotent & tamper-proof: Duplicate identical terminal reports are idempotent; conflicting reports for the same run are rejected.
6. Data/Control separation: Injection attempts inside tool outputs or agent responses cannot alter verification or registry state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .models import Episode, ToolCallProvenance
from .episode import EpisodeStore
from .registry import SkillRegistry


@dataclass
class RunContext:
    """In-flight execution context for an agent run."""

    run_id: str
    task_id: str
    skill_name: Optional[str]
    skill_version: Optional[str]
    environment: dict[str, Any]
    provenances: list[ToolCallProvenance] = field(default_factory=list)
    action_summaries: list[str] = field(default_factory=list)
    completed: bool = False
    terminal_episode: Optional[Episode] = None


class ExperienceCollector:
    """Collects tool observations and terminal verification evidence into immutable Episodes."""

    def __init__(
        self,
        episode_store: EpisodeStore,
        registry: Optional[SkillRegistry] = None,
    ):
        self.episode_store = episode_store
        self.registry = registry
        self._runs: dict[str, RunContext] = {}

    def start_run(
        self,
        run_id: str,
        task_id: str,
        skill_name: Optional[str] = None,
        skill_version: Optional[str] = None,
        environment: Optional[dict[str, Any]] = None,
    ) -> RunContext:
        """Initialize an active execution run and snapshot the current skill version."""
        if skill_version is None:
            if skill_name:
                if self.registry and skill_name in self.registry.list_names():
                    skill_version = self.registry.get_meta(skill_name).version
                else:
                    skill_version = ""
            else:
                skill_version = ""

        ctx = RunContext(
            run_id=run_id,
            task_id=task_id,
            skill_name=skill_name or "",
            skill_version=skill_version or "",
            environment=environment or {},
        )
        self._runs[run_id] = ctx
        return ctx

    def collect_execution(
        self,
        run_id: str,
        task_id: str,
        fn: Callable[[], Any],
        skill_name: Optional[str] = None,
        skill_version: Optional[str] = None,
        environment: Optional[dict[str, Any]] = None,
        verification_fn: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
        acceptance_criteria: Optional[dict[str, Any]] = None,
    ) -> Episode:
        """Execute an arbitrary callable within the collection lifecycle.

        For ordinary runtimes without authoritative verification (verification_fn is None),
        verification_evidence is omitted, ensuring the outcome strictly evaluates to 'unknown'
        regardless of any model self-assertions in the returned text.
        """
        self.start_run(
            run_id=run_id,
            task_id=task_id,
            skill_name=skill_name,
            skill_version=skill_version,
            environment=environment,
        )
        try:
            output = fn()
            evidence = verification_fn(output) if verification_fn else None
            return self.finish_run(
                run_id=run_id,
                model_output=str(output),
                verification_evidence=evidence,
                acceptance_criteria=acceptance_criteria,
            )
        except Exception as exc:
            return self.finish_run(
                run_id=run_id,
                infra_error=f"{type(exc).__name__}: {exc}",
                acceptance_criteria=acceptance_criteria,
            )

    def record_tool_call(
        self,
        run_id: str,
        provenance: ToolCallProvenance,
        action_summary: str = "",
    ) -> None:
        """Record an observed tool invocation in chronological order."""
        if run_id not in self._runs:
            raise KeyError(f"Run '{run_id}' not found in active collector runs")
        ctx = self._runs[run_id]
        if ctx.completed:
            raise ValueError(f"Cannot record tool call on completed run '{run_id}'")
        ctx.provenances.append(provenance)
        if action_summary:
            ctx.action_summaries.append(action_summary)

    def finish_run(
        self,
        run_id: str,
        model_output: str = "",
        verification_evidence: Optional[dict[str, Any]] = None,
        acceptance_criteria: Optional[dict[str, Any]] = None,
        infra_error: Optional[str] = None,
    ) -> Episode:
        """Complete an execution run, determine verified outcome, and persist immutable Episode."""
        if run_id not in self._runs:
            raise KeyError(f"Run '{run_id}' not found in active collector runs")

        ctx = self._runs[run_id]

        # Determine outcome strictly by trusted evidence, NOT untrusted model_output or tool text
        outcome: Literal["success", "failure", "unknown"]
        outcome_reason: str

        if infra_error:
            outcome = "unknown"
            outcome_reason = f"Infrastructure error: {infra_error}"
        elif (
            verification_evidence is None
            or not isinstance(verification_evidence, dict)
            or verification_evidence.get("independent_pass") is None
            or verification_evidence.get("is_invalid_eval") is True
        ):
            # No trusted verification evidence or invalid evaluation: outcome is unknown
            outcome = "unknown"
            reason = (
                str(verification_evidence.get("failure_reason"))
                if isinstance(verification_evidence, dict) and verification_evidence.get("failure_reason")
                else ""
            )
            outcome_reason = reason or "No valid independent verification evidence; model self-assertion ignored"
        elif verification_evidence.get("independent_pass") is False:
            outcome = "failure"
            outcome_reason = str(verification_evidence.get("failure_reason") or "Business verification failed")
        elif verification_evidence.get("independent_pass") is True:
            outcome = "success"
            outcome_reason = "Independent verification passed"
        else:
            outcome = "unknown"
            outcome_reason = "Inconclusive verification"

        episode_id = f"ep_{run_id}"
        new_episode = Episode(
            episode_id=episode_id,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            skill_name=ctx.skill_name or "",
            skill_version=ctx.skill_version or "",
            environment=dict(ctx.environment),
            provenances=list(ctx.provenances),
            acceptance_criteria=acceptance_criteria or {},
            outcome=outcome,
            verification_evidence=verification_evidence,
            outcome_reason=outcome_reason,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Check existing episode for run_id in store (idempotent vs conflict rejection)
        existing = self.episode_store.get_episode(episode_id)
        if existing is not None:
            # Compare canonical representations
            is_same = (
                existing.task_id == new_episode.task_id
                and existing.skill_name == new_episode.skill_name
                and existing.skill_version == new_episode.skill_version
                and existing.outcome == new_episode.outcome
                and len(existing.provenances) == len(new_episode.provenances)
            )
            if is_same:
                ctx.completed = True
                ctx.terminal_episode = existing
                return existing
            else:
                raise ValueError(
                    f"Conflicting terminal episode for run '{run_id}' already exists; overwrite forbidden"
                )

        self.episode_store.save_episode(new_episode, on_conflict="error")
        ctx.completed = True
        ctx.terminal_episode = new_episode
        return new_episode
