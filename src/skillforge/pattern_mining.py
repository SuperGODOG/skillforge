"""Episode Pool Pattern Mining Module (Milestone 3b)

Provides automatic cluster discovery, evidence filtering, and candidate synthesis
over persisted learning episodes.

Core Invariants:
1. Strict A8 Purpose Isolation:
   Evaluation / heldout episodes and unverified / unknown purpose episodes are strictly
   filtered out prior to any grouping, embedding, or LLM generation.
2. Heuristic Discovery Proxy (not proven generalization):
   - At least min_support (3) distinct tasks
   - Known outcome coverage >= min_coverage (0.8)
   - Success rate >= min_success_rate (0.8)
   - Expression diversity >= min_expressions (2)
   - Observable step complexity >= min_steps (2)
3. Task Deduplication:
   Multiple runs of the same task do not inflate support; the latest run is selected deterministically.
4. Idempotency & Persistence:
   Batches are tracked by deterministic fingerprint in SQLite ledger (mined_batches).
   Re-running identical inputs reuses results with zero duplicate LLM calls or candidates.
5. Isolated Candidate Creation:
   Candidates produced are stored in CandidateStore with status DRAFT, never promoted to active registry.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import numpy as np

from .models import Episode, CandidateSkill, ToolCallProvenance
from .episode import EpisodeStore, CandidateStore
from .registry import SkillRegistry
from .evolution_loop import mine_candidate, MiningResult
from .storage.db import init_db


@dataclass
class PatternMiningConfig:
    """Configurable heuristic thresholds for Episode pool pattern mining."""

    min_support: int = 3  # Minimum independent tasks in cluster
    min_success_rate: float = 0.8  # success / (success + failure)
    min_coverage: float = 0.8  # (success + failure) / total
    min_expressions: int = 2  # Distinct normalized task expressions
    min_steps: int = 2  # Observable complexity proxy
    similarity_threshold: float = 0.80  # Cosine similarity for semantic grouping


@dataclass
class ClusterReport:
    """Detailed summary of pattern mining for an individual episode cluster."""

    cluster_id: str
    target_skill_name: str
    decision: Literal["create", "revise", "abandon", "abstain"]
    candidate: Optional[CandidateSkill] = None
    candidate_id: Optional[str] = None
    is_cached: bool = False
    source_episode_ids: list[str] = field(default_factory=list)
    counter_example_episode_ids: list[str] = field(default_factory=list)
    support_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    unknown_count: int = 0
    coverage: float = 0.0
    success_rate: float = 0.0
    distinct_expressions: list[str] = field(default_factory=list)
    complexity_steps: int = 0
    selection_basis: dict[str, str] = field(default_factory=dict)
    abstain_reasons: list[str] = field(default_factory=list)


@dataclass
class MiningBatchReport:
    """Batch-level outcome report for mine_pending execution."""

    total_episodes_scanned: int
    learning_episodes_count: int
    filtered_evaluation_episodes: int
    unique_tasks: int
    clusters: list[ClusterReport] = field(default_factory=list)
    candidates_created: list[CandidateSkill] = field(default_factory=list)
    candidates_revised: list[CandidateSkill] = field(default_factory=list)
    abstained_clusters: list[ClusterReport] = field(default_factory=list)


def normalize_task_text(text: str) -> str:
    """Normalize task expression for comparison and expression counting."""
    text = text.lower().strip()
    text = re.sub(r"[\s\-_/]+", " ", text)
    return text.strip()


def extract_task_expression(ep: Episode) -> str:
    """Extract human-readable task description from Episode."""
    if isinstance(ep.acceptance_criteria, dict):
        for k in ("query", "task_description", "description", "goal", "instruction"):
            v = ep.acceptance_criteria.get(k)
            if v:
                return str(v).strip()
    elif isinstance(ep.acceptance_criteria, str) and ep.acceptance_criteria.strip():
        return ep.acceptance_criteria.strip()

    if isinstance(ep.environment, dict):
        for k in ("query", "task_description", "description"):
            v = ep.environment.get(k)
            if v:
                return str(v).strip()

    return ep.task_id.replace("_", " ").strip()


def extract_observable_steps(ep: Episode) -> list[str]:
    """Extract observable steps (tool names or explicit action items)."""
    if ep.provenances:
        return [
            p.tool_name
            for p in ep.provenances
            if getattr(p, "tool_called", True)
        ]
    steps: list[Any] = []
    if isinstance(ep.environment, dict):
        steps = ep.environment.get("steps") or ep.environment.get("actions") or []
    if not steps and isinstance(ep.acceptance_criteria, dict):
        steps = ep.acceptance_criteria.get("steps") or ep.acceptance_criteria.get("actions") or []
    return [str(s) for s in steps]


def cosine_similarity(v1: list[float] | np.ndarray, v2: list[float] | np.ndarray) -> float:
    """Compute cosine similarity between two numeric vectors."""
    a = np.array(v1, dtype=float)
    b = np.array(v2, dtype=float)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _bow_embed(text: str, dim: int = 128) -> list[float]:
    """Deterministic hash embedding for environments without sentence-transformers."""
    vec = [0.0] * dim
    tokens = text.lower().split()
    for tok in tokens:
        idx = abs(hash(tok)) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        vec = [x / norm for x in vec]
    return vec


def _default_embed(texts: list[str]) -> list[list[float]]:
    """Try to use repo's local EmbedLayer; fallback to deterministic hash embedder."""
    try:
        from .router.embed import EmbedLayer
        layer = EmbedLayer()
        if layer.model_dir.exists():
            model = layer._get_model()
            vecs = model.encode(texts, normalize_embeddings=True)
            return [v.tolist() for v in vecs]
    except Exception:
        pass
    return [_bow_embed(t) for t in texts]


def cluster_tasks(
    tasks: list[tuple[str, str, Episode]],
    embedder: Optional[Callable[[list[str]], list[list[float]]]],
    threshold: float,
) -> list[list[tuple[str, str, Episode]]]:
    """Group deduplicated tasks into semantic clusters based on task expression embeddings."""
    if not tasks:
        return []

    expressions = [t[1] for t in tasks]
    if embedder is not None:
        vectors = embedder(expressions)
    else:
        vectors = _default_embed(expressions)

    clusters: list[list[int]] = []
    for i in range(len(tasks)):
        assigned = False
        for c in clusters:
            # Check maximum pairwise similarity with members of existing cluster
            sims = [cosine_similarity(vectors[i], vectors[j]) for j in c]
            if max(sims) >= threshold:
                c.append(i)
                assigned = True
                break
        if not assigned:
            clusters.append([i])

    return [[tasks[idx] for idx in c] for c in clusters]


def _compute_batch_fingerprint(
    source_ids: list[str],
    target_skill_name: str,
    baseline_version: str,
    config: PatternMiningConfig,
) -> str:
    """Compute canonical hash of source episodes and policy configuration."""
    payload = {
        "sources": sorted(source_ids),
        "target_skill": target_skill_name,
        "baseline_version": baseline_version,
        "policy": {
            "min_support": config.min_support,
            "min_success_rate": config.min_success_rate,
            "min_coverage": config.min_coverage,
            "min_expressions": config.min_expressions,
            "min_steps": config.min_steps,
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mine_pending(
    episode_store: EpisodeStore,
    candidate_store: CandidateStore,
    registry: Optional[SkillRegistry] = None,
    llm: Any = None,
    embedder: Optional[Callable[[list[str]], list[list[float]]]] = None,
    config: Optional[PatternMiningConfig] = None,
) -> MiningBatchReport:
    """Scan EpisodeStore for learning episodes, group into semantic patterns, and synthesize candidates.

    Args:
        episode_store: Source of execution episodes.
        candidate_store: Target storage for mined candidate skills.
        registry: Active SkillRegistry for version matching and existing skill revision.
        llm: LLM client for synthesis (FakeLLM or production client).
        embedder: Optional embedding callable for semantic task grouping.
        config: Threshold configuration.

    Returns:
        MiningBatchReport summarizing scanned pool, clusters, candidates, and abstain reasons.
    """
    cfg = config or PatternMiningConfig()
    all_episodes = episode_store.list_episodes()

    # 1. A8 Purpose Isolation: filter evaluation/heldout and untrusted purpose BEFORE any grouping
    learning_episodes: list[Episode] = []
    filtered_eval_count = 0

    for ep in all_episodes:
        purpose = ep.environment.get("purpose", "") if isinstance(ep.environment, dict) else ""
        if purpose == "learning":
            learning_episodes.append(ep)
        else:
            # Conservative: purpose='evaluation', 'heldout', unknown or missing are strictly excluded
            filtered_eval_count += 1

    if not learning_episodes:
        return MiningBatchReport(
            total_episodes_scanned=len(all_episodes),
            learning_episodes_count=0,
            filtered_evaluation_episodes=filtered_eval_count,
            unique_tasks=0,
        )

    # 2. Task Deduplication: multiple runs of same task_id do NOT inflate independent support
    task_groups: dict[str, list[Episode]] = {}
    for ep in learning_episodes:
        task_groups.setdefault(ep.task_id, []).append(ep)

    deduped_tasks: list[tuple[str, str, Episode]] = []
    selection_basis_map: dict[str, str] = {}

    for task_id, runs in sorted(task_groups.items()):
        # Deterministically select the latest run by (created_at, run_id)
        chosen = sorted(runs, key=lambda x: (x.created_at or "", x.run_id), reverse=True)[0]
        expr = extract_task_expression(chosen)
        deduped_tasks.append((task_id, expr, chosen))
        selection_basis_map[task_id] = (
            f"Selected latest run '{chosen.run_id}' (created_at={chosen.created_at}) "
            f"out of {len(runs)} total runs for task '{task_id}'"
        )

    # 3. Semantic Grouping & Observable Pattern Analysis
    task_clusters = cluster_tasks(deduped_tasks, embedder, cfg.similarity_threshold)

    conn = episode_store._get_conn()
    cluster_reports: list[ClusterReport] = []
    created_candidates: list[CandidateSkill] = []
    revised_candidates: list[CandidateSkill] = []
    abstained_reports: list[ClusterReport] = []

    for c_idx, cluster in enumerate(task_clusters):
        cluster_id = f"cluster_{c_idx + 1}"
        cluster_episodes = [t[2] for t in cluster]
        distinct_tasks = set(ep.task_id for ep in cluster_episodes)
        support_count = len(distinct_tasks)

        # Expression diversity
        expressions = [t[1] for t in cluster]
        distinct_expressions = sorted(list(set(normalize_task_text(e) for e in expressions)))

        # Step complexity proxy
        steps_per_episode = [len(extract_observable_steps(ep)) for ep in cluster_episodes]
        max_steps = max(steps_per_episode) if steps_per_episode else 0

        # Outcome statistics
        success_count = sum(1 for ep in cluster_episodes if ep.outcome == "success")
        failure_count = sum(1 for ep in cluster_episodes if ep.outcome == "failure")
        unknown_count = sum(1 for ep in cluster_episodes if ep.outcome == "unknown")
        total_cluster = len(cluster_episodes)
        known_count = success_count + failure_count

        coverage = known_count / total_cluster if total_cluster > 0 else 0.0
        success_rate = success_count / known_count if known_count > 0 else 0.0

        counter_examples = [ep.episode_id for ep in cluster_episodes if ep.outcome == "failure"]

        # Derive target skill name
        named_skills = [ep.skill_name for ep in cluster_episodes if ep.skill_name]
        if named_skills:
            # Most common non-empty skill name
            target_skill_name = max(set(named_skills), key=named_skills.count)
        else:
            # Fallback to normalized common keyword or cluster id
            first_word = normalize_task_text(expressions[0]).split()[0] if expressions else "task"
            target_skill_name = f"{first_word}_skill"

        is_existing = registry is not None and target_skill_name in registry.list_names()
        baseline_version = registry.get_meta(target_skill_name).version if is_existing else ""
        decision_type: Literal["create", "revise"] = "revise" if is_existing else "create"

        # 4. Conservative Threshold Validation
        # ponytail: keep minimal heuristic checks before calling expensive LLM synthesis
        abstain_reasons: list[str] = []

        if support_count < cfg.min_support:
            abstain_reasons.append(
                f"Insufficient independent task support: got {support_count}, required >= {cfg.min_support}"
            )
        if len(distinct_expressions) < cfg.min_expressions:
            abstain_reasons.append(
                f"Insufficient task expression diversity: got {len(distinct_expressions)}, required >= {cfg.min_expressions}"
            )
        if max_steps < cfg.min_steps:
            abstain_reasons.append(
                f"Insufficient observable step complexity: max steps={max_steps}, required >= {cfg.min_steps}"
            )
        if coverage < cfg.min_coverage:
            abstain_reasons.append(
                f"Known outcome coverage too low: {coverage:.2f} < {cfg.min_coverage} ({unknown_count} unknown runs)"
            )
        if known_count > 0 and success_rate < cfg.min_success_rate:
            abstain_reasons.append(
                f"Success rate too low: {success_rate:.2f} < {cfg.min_success_rate} ({success_count} success, {failure_count} failure)"
            )

        source_ids = [ep.episode_id for ep in cluster_episodes]
        cluster_selection_basis = {ep.task_id: selection_basis_map[ep.task_id] for ep in cluster_episodes}

        if abstain_reasons:
            rep = ClusterReport(
                cluster_id=cluster_id,
                target_skill_name=target_skill_name,
                decision="abstain",
                candidate=None,
                candidate_id=None,
                is_cached=False,
                source_episode_ids=source_ids,
                counter_example_episode_ids=counter_examples,
                support_count=support_count,
                success_count=success_count,
                failure_count=failure_count,
                unknown_count=unknown_count,
                coverage=coverage,
                success_rate=success_rate,
                distinct_expressions=distinct_expressions,
                complexity_steps=max_steps,
                selection_basis=cluster_selection_basis,
                abstain_reasons=abstain_reasons,
            )
            cluster_reports.append(rep)
            abstained_reports.append(rep)
            continue

        # 5. Idempotent Fingerprint & Ledger
        batch_fp = _compute_batch_fingerprint(source_ids, target_skill_name, baseline_version, cfg)
        cached_row = conn.execute(
            """SELECT decision, candidate_id, abstain_reason FROM mined_batches
               WHERE batch_fingerprint = ?""",
            (batch_fp,),
        ).fetchone()

        if cached_row:
            cached_dec, cached_cid, cached_abs = cached_row
            existing_candidate = candidate_store.get_candidate(cached_cid) if cached_cid else None
            rep = ClusterReport(
                cluster_id=cluster_id,
                target_skill_name=target_skill_name,
                decision=cached_dec,
                candidate=existing_candidate,
                candidate_id=cached_cid,
                is_cached=True,
                source_episode_ids=source_ids,
                counter_example_episode_ids=counter_examples,
                support_count=support_count,
                success_count=success_count,
                failure_count=failure_count,
                unknown_count=unknown_count,
                coverage=coverage,
                success_rate=success_rate,
                distinct_expressions=distinct_expressions,
                complexity_steps=max_steps,
                selection_basis=cluster_selection_basis,
                abstain_reasons=[cached_abs] if cached_abs else [],
            )
            cluster_reports.append(rep)
            if existing_candidate:
                if cached_dec == "create":
                    created_candidates.append(existing_candidate)
                elif cached_dec == "revise":
                    revised_candidates.append(existing_candidate)
            elif cached_dec in ("abandon", "abstain"):
                abstained_reports.append(rep)
            continue

        # 6. Candidate Synthesis via M2 miner
        try:
            mining_res: MiningResult = mine_candidate(
                episodes=cluster_episodes,
                target_skill_name=target_skill_name,
                llm=llm,
                candidate_store=candidate_store,
                registry=registry,
                decision_override=decision_type,
            )
        except Exception as err:
            mining_res = MiningResult(
                decision="abandon",
                candidate=None,
                abandon_reason=f"Synthesis abandoned or invalid: {err}",
            )

        cand = mining_res.candidate
        cid = cand.candidate_id if cand else None
        final_decision = mining_res.decision

        # Persist ledger record
        conn.execute(
            """INSERT INTO mined_batches (
                batch_fingerprint, cluster_id, target_skill_name, baseline_version,
                decision, candidate_id, abstain_reason, source_episode_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_fp,
                cluster_id,
                target_skill_name,
                baseline_version,
                final_decision,
                cid,
                mining_res.abandon_reason,
                json.dumps(source_ids),
            ),
        )
        conn.commit()

        rep = ClusterReport(
            cluster_id=cluster_id,
            target_skill_name=target_skill_name,
            decision=final_decision,
            candidate=cand,
            candidate_id=cid,
            is_cached=False,
            source_episode_ids=source_ids,
            counter_example_episode_ids=counter_examples,
            support_count=support_count,
            success_count=success_count,
            failure_count=failure_count,
            unknown_count=unknown_count,
            coverage=coverage,
            success_rate=success_rate,
            distinct_expressions=distinct_expressions,
            complexity_steps=max_steps,
            selection_basis=cluster_selection_basis,
            abstain_reasons=[mining_res.abandon_reason] if mining_res.abandon_reason else [],
        )
        cluster_reports.append(rep)

        if cand:
            if final_decision == "create":
                created_candidates.append(cand)
            elif final_decision == "revise":
                revised_candidates.append(cand)
        elif final_decision in ("abandon", "abstain"):
            abstained_reports.append(rep)

    return MiningBatchReport(
        total_episodes_scanned=len(all_episodes),
        learning_episodes_count=len(learning_episodes),
        filtered_evaluation_episodes=filtered_eval_count,
        unique_tasks=len(deduped_tasks),
        clusters=cluster_reports,
        candidates_created=created_candidates,
        candidates_revised=revised_candidates,
        abstained_clusters=abstained_reports,
    )
