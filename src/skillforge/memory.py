"""Three-Tier Memory Architecture Module (Milestone 5c)

Provides boundary enforcement, provenance linking, and conflict preservation across:
1. Semantic Memory (SemanticStore): Facts and observations with explicit source IDs, scopes,
   and conflict preservation. Single execution observations cannot be silently promoted to universal facts.
2. Episodic Memory (EpisodeStore): Immutable execution experiences preserving task, timestamp,
   version, outcome, and tool provenances. Success and failure records are cleanly separated.
3. Procedural Memory (CandidateStore + SkillRegistry): Candidate skills and registered active skills
   linked to supporting episodes and versions; controlled promotion requires real gates and explicit confirmation.

Core Invariants:
- Distinct ID prefixes and types: 'fact_' for semantic, 'ep_' for episodic, 'cand_' / skill name for procedural.
- Typed retrieval: search filters strictly by tier, never conflating types or IDs.
- Lineage tracing: Procedural -> Supporting Episodes -> Source Facts.
- Evaluation data isolation: purpose='evaluation' episodes are strictly isolated from candidate learning.
- Failure isolation: Failed experiences cannot produce successful procedural skills.
- Conflict preservation: Contradictory observations on the same topic/attribute coexist with distinct sources
  and are explicitly exposed as SemanticConflict without arbitrary overwrite or auto-resolution.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .models import (
    SemanticFact,
    SemanticConflict,
    MemoryLineage,
    Episode,
    CandidateSkill,
    VersionSnapshot,
    Release,
    DocumentSource,
    DocumentSnippet,
    DocumentExtractionResult,
    RetrievalContext,
    SkillRecommendation,
    FutureRetrievalResult,
)
from .storage.db import init_db
from .episode import EpisodeStore, CandidateStore
from .documents import DocumentStore, extract_candidate_from_document
from .registry import SkillRegistry
from .evolution_loop import mine_candidate, promote_candidate, ValidationRecord, MiningResult
from .state_machine import ReleaseStateMachine
from .retrieval import FutureMemoryRetriever


class SemanticStore:
    """SQLite-backed store for scoped semantic observations and verified facts."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def has_fact(self, fact_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM semantic_facts WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        return row is not None

    def save_fact(
        self,
        fact: SemanticFact,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> str:
        """Persist a SemanticFact.

        Invariants:
        - fact_id must start with 'fact_'.
        - Single execution observation (run_..., ep_..., or single_execution tag)
          cannot be marked as is_universal=True.
        - Existing fact with same fact_id follows on_conflict rule.
        - Observations on the same topic from different sources are BOTH stored
          without overwriting or silent auto-resolution.
        """
        fact.__post_init__()
        conn = self._get_conn()

        if self.has_fact(fact.fact_id):
            if on_conflict == "error":
                raise ValueError(f"SemanticFact with id '{fact.fact_id}' already exists")
            return fact.fact_id

        created_at = fact.created_at or datetime.now(timezone.utc).isoformat()
        tags_json = json.dumps(fact.tags, ensure_ascii=False)

        conn.execute(
            """INSERT INTO semantic_facts (
                fact_id, statement, source_id, scope, topic, is_universal, tags_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact.fact_id,
                fact.statement,
                fact.source_id,
                fact.scope,
                fact.topic,
                1 if fact.is_universal else 0,
                tags_json,
                created_at,
            ),
        )
        conn.commit()
        return fact.fact_id

    def get_fact(self, fact_id: str) -> Optional[SemanticFact]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT fact_id, statement, source_id, scope, topic, is_universal, tags_json, created_at
               FROM semantic_facts WHERE fact_id = ?""",
            (fact_id,),
        ).fetchone()
        if not row:
            return None

        fid, stmt, src_id, scope, topic, is_univ, tags_j, cat = row
        tags = json.loads(tags_j) if tags_j else []
        return SemanticFact(
            fact_id=fid,
            statement=stmt,
            source_id=src_id,
            scope=scope,
            topic=topic,
            is_universal=bool(is_univ),
            tags=tags,
            created_at=cat,
        )

    def list_facts(
        self,
        source_id: Optional[str] = None,
        scope: Optional[str] = None,
        topic: Optional[str] = None,
        is_universal: Optional[bool] = None,
    ) -> list[SemanticFact]:
        conn = self._get_conn()
        query = """SELECT fact_id, statement, source_id, scope, topic, is_universal, tags_json, created_at
                   FROM semantic_facts WHERE 1=1"""
        params: list[Any] = []

        if source_id is not None:
            query += " AND source_id = ?"
            params.append(source_id)
        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)
        if topic is not None:
            query += " AND topic = ?"
            params.append(topic)
        if is_universal is not None:
            query += " AND is_universal = ?"
            params.append(1 if is_universal else 0)

        query += " ORDER BY created_at ASC"
        rows = conn.execute(query, tuple(params)).fetchall()

        results: list[SemanticFact] = []
        for r in rows:
            tags = json.loads(r[6]) if r[6] else []
            results.append(
                SemanticFact(
                    fact_id=r[0],
                    statement=r[1],
                    source_id=r[2],
                    scope=r[3],
                    topic=r[4],
                    is_universal=bool(r[5]),
                    tags=tags,
                    created_at=r[7],
                )
            )
        return results

    def detect_conflicts(self, topic: Optional[str] = None) -> list[SemanticConflict]:
        """Detect and explicitly surface contradictory observations within topics across different sources.

        Does NOT overwrite or arbitrate; surfaces conflicting facts cleanly for inspection/decision.
        """
        all_facts = self.list_facts(topic=topic)
        grouped_by_topic: dict[str, list[SemanticFact]] = {}
        for f in all_facts:
            grouped_by_topic.setdefault(f.topic, []).append(f)

        conflicts: list[SemanticConflict] = []
        for top, facts in grouped_by_topic.items():
            if len(facts) < 2:
                continue

            # Check if there are distinct/divergent statements on this topic
            distinct_statements = {f.statement.strip().lower() for f in facts}
            if len(distinct_statements) > 1:
                sources = list({f.source_id for f in facts})
                conflicts.append(
                    SemanticConflict(
                        topic=top,
                        facts=facts,
                        description=(
                            f"Contradictory observations on topic '{top}' detected across "
                            f"{len(sources)} sources ({', '.join(sources)}): "
                            f"{len(distinct_statements)} divergent statements."
                        ),
                    )
                )

        return conflicts


class ThreeTierMemoryManager:
    """Unified manager enforcing boundaries and provenance across Semantic, Episodic, and Procedural memory."""

    def __init__(
        self,
        db_path: Path,
        registry: Optional[SkillRegistry] = None,
        deployment_manager: Optional[Any] = None,
        episode_store: Optional[EpisodeStore] = None,
        candidate_store: Optional[CandidateStore] = None,
        semantic_store: Optional[SemanticStore] = None,
        document_store: Optional[DocumentStore] = None,
    ):
        self.db_path = db_path
        self.registry = registry
        self.deployment_manager = deployment_manager
        self.episode_store = episode_store or EpisodeStore(db_path)
        self.candidate_store = candidate_store or CandidateStore(db_path, self.episode_store)
        self.semantic_store = semantic_store or SemanticStore(db_path)
        self.document_store = document_store or DocumentStore(db_path)

    def close(self) -> None:
        self.document_store.close()
        self.semantic_store.close()
        self.candidate_store.close()
        self.episode_store.close()

    def search(
        self,
        query: str,
        tier: Optional[Literal["semantic", "episodic", "procedural"]] = None,
    ) -> list[Any] | dict[str, list[Any]]:
        """Query memory with strict tier type and ID boundary isolation.

        Args:
            query: Keyword to search for.
            tier: 'semantic', 'episodic', 'procedural', or None (returns dict with 3 separate tiers).

        Returns:
            If tier is specified: list containing ONLY elements of that tier.
            If tier is None: dict with keys 'semantic', 'episodic', 'procedural'.
        """
        q = query.lower().strip()

        semantic_results: list[SemanticFact] = []
        episodic_results: list[Episode] = []
        procedural_results: list[Any] = []

        if tier in ("semantic", None):
            for fact in self.semantic_store.list_facts():
                if (
                    q in fact.statement.lower()
                    or q in fact.topic.lower()
                    or q in fact.scope.lower()
                    or any(q in tag.lower() for tag in fact.tags)
                ):
                    semantic_results.append(fact)

        if tier in ("episodic", None):
            for ep in self.episode_store.list_episodes():
                if (
                    q in ep.task_id.lower()
                    or q in ep.skill_name.lower()
                    or q in ep.outcome.lower()
                    or q in (ep.outcome_reason or "").lower()
                    or (isinstance(ep.environment, dict) and any(q in str(v).lower() for v in ep.environment.values()))
                    or (isinstance(ep.acceptance_criteria, dict) and any(q in str(v).lower() for v in ep.acceptance_criteria.values()))
                ):
                    episodic_results.append(ep)

        if tier in ("procedural", None):
            # 1. Candidates in candidate store
            for cand in self.candidate_store.list_candidates():
                if (
                    q in cand.skill_name.lower()
                    or q in cand.body.lower()
                    or q in cand.rationale.lower()
                    or (cand.meta and q in cand.meta.description.lower())
                ):
                    procedural_results.append(cand)
            # 2. Registered active skills if registry provided
            if self.registry:
                for name in self.registry.list_names():
                    if q in name.lower():
                        meta = self.registry.get_meta(name)
                        body = self.registry.get_body(name)
                        procedural_results.append(
                            VersionSnapshot(
                                skill_name=name,
                                version=meta.version if meta else "1.0.0",
                                content_hash="",
                                meta=meta,
                                body=body,
                                status="PUBLISHED",
                                is_verified=True,
                            )
                        )

        if tier == "semantic":
            return semantic_results
        if tier == "episodic":
            return episodic_results
        if tier == "procedural":
            return procedural_results

        return {
            "semantic": semantic_results,
            "episodic": episodic_results,
            "procedural": procedural_results,
        }

    def retrieve(
        self,
        query: str,
        context: Optional[RetrievalContext] = None,
        tier: Optional[Literal["semantic", "episodic", "procedural"]] = None,
    ) -> FutureRetrievalResult:
        """Perform bounded, read-only, task-context-aware memory retrieval."""
        retriever = FutureMemoryRetriever(
            memory_manager=self,
            registry=self.registry,
            deployment_manager=self.deployment_manager,
        )
        return retriever.retrieve(query=query, context=context, tier=tier)

    def trace_lineage(self, procedural_id: str) -> MemoryLineage:
        """Trace provenance from procedural candidate or skill back to supporting episodes and semantic facts."""
        if procedural_id.startswith("cand_"):
            candidate = self.candidate_store.get_candidate(procedural_id)
            if not candidate:
                raise KeyError(f"Candidate '{procedural_id}' not found in CandidateStore")

            supporting_episodes: list[Episode] = []
            source_facts: list[SemanticFact] = []
            lineage_broken = False
            broken_reasons: list[str] = []

            for eid in candidate.source_episode_ids:
                ep = self.episode_store.get_episode(eid)
                if ep:
                    supporting_episodes.append(ep)
                    # Associate semantic facts whose source_id is the episode_id or run_id
                    facts = self.semantic_store.list_facts(source_id=eid)
                    source_facts.extend(facts)
                    if ep.run_id:
                        run_facts = self.semantic_store.list_facts(source_id=ep.run_id)
                        for rf in run_facts:
                            if rf not in source_facts:
                                source_facts.append(rf)
                else:
                    lineage_broken = True
                    broken_reasons.append(f"Missing supporting episode: {eid}")

            source_doc = None
            source_snips = []
            if candidate.source_doc_id:
                source_doc = self.document_store.get_document(
                    candidate.source_doc_id, candidate.source_doc_version
                )
                if source_doc:
                    source_snips = [
                        s for s in source_doc.snippets
                        if s.snippet_id in candidate.source_snippet_ids
                    ]
                else:
                    lineage_broken = True
                    broken_reasons.append(f"Missing source document: {candidate.source_doc_id} (v{candidate.source_doc_version})")

            return MemoryLineage(
                procedural_id=procedural_id,
                procedural_type="candidate",
                procedural_item=candidate,
                supporting_episodes=supporting_episodes,
                source_facts=source_facts,
                source_document=source_doc,
                source_snippets=source_snips,
                lineage_broken=lineage_broken,
                broken_reasons=broken_reasons,
            )

        # Registered skill lookup
        if self.registry and self.registry.has_skill(procedural_id):
            meta = self.registry.get_meta(procedural_id)
            body = self.registry.get_body(procedural_id)
            snapshot = VersionSnapshot(
                skill_name=procedural_id,
                version=meta.version if meta else "1.0.0",
                content_hash="",
                meta=meta,
                body=body,
                status="PUBLISHED",
                is_verified=True,
            )
            episodes = self.episode_store.list_episodes(skill_name=procedural_id)
            source_facts: list[SemanticFact] = []
            for ep in episodes:
                facts = self.semantic_store.list_facts(source_id=ep.episode_id)
                source_facts.extend(facts)
                if ep.run_id:
                    for rf in self.semantic_store.list_facts(source_id=ep.run_id):
                        if rf not in source_facts:
                            source_facts.append(rf)

            source_doc = None
            source_snips = []
            lineage_broken = False
            broken_reasons: list[str] = []
            cands = self.candidate_store.list_candidates(skill_name=procedural_id)
            if not cands and not episodes:
                lineage_broken = True
                broken_reasons.append(f"Skill '{procedural_id}' has no recorded supporting candidates or episodes")

            for c in cands:
                for eid in c.source_episode_ids:
                    if not self.episode_store.get_episode(eid):
                        lineage_broken = True
                        broken_reasons.append(f"Missing supporting episode for candidate {c.candidate_id}: {eid}")
                if c.source_doc_id:
                    source_doc = self.document_store.get_document(
                        c.source_doc_id, c.source_doc_version
                    )
                    if source_doc:
                        source_snips = [
                            s for s in source_doc.snippets
                            if s.snippet_id in c.source_snippet_ids
                        ]
                    else:
                        lineage_broken = True
                        broken_reasons.append(f"Missing source document for candidate {c.candidate_id}: {c.source_doc_id}")
                    break

            return MemoryLineage(
                procedural_id=procedural_id,
                procedural_type="skill",
                procedural_item=snapshot,
                supporting_episodes=episodes,
                source_facts=source_facts,
                source_document=source_doc,
                source_snippets=source_snips,
                lineage_broken=lineage_broken,
                broken_reasons=broken_reasons,
            )

        # Release lookup
        conn = init_db(self.db_path)
        row = conn.execute(
            "SELECT release_id, skill_name, version, commit_hash, status, level, source_lineage_json FROM releases WHERE release_id = ?",
            (procedural_id,),
        ).fetchone()
        if row:
            rel_id, s_name, s_ver, s_commit, s_status, s_level, s_lin_json = row
            rel_obj = Release(
                release_id=rel_id,
                skill_name=s_name,
                version=s_ver,
                commit_hash=s_commit,
                status=s_status,
                level=s_level,
            )
            supporting_episodes = []
            source_facts = []
            source_doc = None
            source_snips = []
            lineage_broken = False
            broken_reasons: list[str] = []
            if s_lin_json:
                lin_data = json.loads(s_lin_json)
                ep_ids = (lin_data.get("source_episode_ids") or []) + (lin_data.get("verification_episode_ids") or [])
                for eid in ep_ids:
                    ep = self.episode_store.get_episode(eid)
                    if ep and ep not in supporting_episodes:
                        supporting_episodes.append(ep)
                        facts = self.semantic_store.list_facts(source_id=eid)
                        for f in facts:
                            if f not in source_facts:
                                source_facts.append(f)
                    elif not ep:
                        lineage_broken = True
                        broken_reasons.append(f"Missing supporting/verification episode: {eid}")
                doc_id = lin_data.get("source_doc_id")
                doc_ver = lin_data.get("source_doc_version")
                snip_ids = lin_data.get("source_snippet_ids") or []
                if doc_id:
                    source_doc = self.document_store.get_document(doc_id, doc_ver)
                    if source_doc:
                        source_snips = [s for s in source_doc.snippets if s.snippet_id in snip_ids]
                    else:
                        lineage_broken = True
                        broken_reasons.append(f"Missing source document: {doc_id} (v{doc_ver})")
            else:
                lineage_broken = True
                broken_reasons.append(f"Release '{rel_id}' has no recorded source lineage")

            return MemoryLineage(
                procedural_id=procedural_id,
                procedural_type="release",
                procedural_item=rel_obj,
                supporting_episodes=supporting_episodes,
                source_facts=source_facts,
                source_document=source_doc,
                source_snippets=source_snips,
                lineage_broken=lineage_broken,
                broken_reasons=broken_reasons,
            )

        raise KeyError(f"Procedural entity '{procedural_id}' not found in CandidateStore, SkillRegistry, or releases")

    def ingest_document(self, doc: DocumentSource, on_conflict: Literal["error", "ignore"] = "error") -> str:
        """Persist a DocumentSource into DocumentStore."""
        return self.document_store.save_document(doc, on_conflict=on_conflict)

    def extract_candidate_from_document(
        self,
        doc: DocumentSource,
        target_skill_name: Optional[str] = None,
        override_decision: Optional[CandidateDecision] = None,
    ) -> DocumentExtractionResult:
        """Extract candidate from DocumentSource without forging fake episodes."""
        return extract_candidate_from_document(
            doc=doc,
            candidate_store=self.candidate_store,
            target_skill_name=target_skill_name,
            override_decision=override_decision,
            registry=self.registry,
        )

    def detect_conflicts(self, topic: Optional[str] = None) -> list[SemanticConflict]:
        """Expose contradictory observations on the same topic/attribute."""
        return self.semantic_store.detect_conflicts(topic=topic)

    def create_candidate_from_episodes(
        self,
        episodes: list[Episode],
        target_skill_name: str,
        llm: Any,
        decision_override: Optional[Literal["create", "revise", "abandon"]] = None,
    ) -> MiningResult:
        """Synthesize CandidateSkill with strict evaluation and failure isolation boundaries.

        Invariants:
        1. purpose='evaluation' episodes cannot be used for learning (A8 purpose isolation).
        2. Purely failed experiences cannot produce successful procedural skills.
        """
        for ep in episodes:
            purpose = ep.environment.get("purpose", "") if isinstance(ep.environment, dict) else ""
            if purpose == "evaluation":
                raise ValueError(
                    f"Episode '{ep.episode_id}' has purpose='evaluation'; "
                    "evaluation episodes cannot flow back into learning/candidate generation"
                )

        all_failed = all(ep.outcome == "failure" for ep in episodes)
        if all_failed and decision_override != "revise":
            raise ValueError(
                "Cannot create candidate skill from purely failed episodes "
                "(failed experiences cannot produce successful procedural skills)"
            )

        return mine_candidate(
            episodes=episodes,
            target_skill_name=target_skill_name,
            llm=llm,
            candidate_store=self.candidate_store,
            registry=self.registry,
            decision_override=decision_override,
        )

    def promote_candidate(
        self,
        candidate_id: str,
        validation_record: ValidationRecord,
        state_machine: ReleaseStateMachine,
        caller_confirmed: bool = False,
    ) -> Release:
        """Controlled promotion of CandidateSkill into registered active Skill.

        Invariants:
        1. Candidate must exist in candidate store.
        2. Validation record must have ratchet_decision == 'PASS'.
        3. Caller explicit confirmation (caller_confirmed=True) is mandatory.
        """
        candidate = self.candidate_store.get_candidate(candidate_id)
        if not candidate:
            raise KeyError(f"Candidate '{candidate_id}' not found")

        if not self.registry:
            raise ValueError("Cannot promote candidate without an active SkillRegistry")

        self.candidate_store.save_validation_record(validation_record)

        return promote_candidate(
            candidate=candidate,
            validation_record=validation_record,
            state_machine=state_machine,
            registry=self.registry,
            candidate_store=self.candidate_store,
            caller_confirmed=caller_confirmed,
        )
