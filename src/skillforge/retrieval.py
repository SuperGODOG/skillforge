"""Future Retrieval & Task-Context Memory Retrieval Module (Phase P3)

Provides a bounded, read-only, task-context-aware retrieval entry point across:
- Semantic Memory (scoped facts and explicit conflict preservation)
- Episodic Memory (execution experiences, isolating evaluation and failure from positive evidence)
- Procedural Memory (formal published skills with immutable version snapshots and verified lineage)

Invariants:
1. Read-Only & Zero Side-Effects:
   Does not execute tools, mutate registry, promote candidates, switch versions, or write episodes.
2. Context-Aware & Boundary-Enforcing:
   Enforces task version bindings, canary/rollback active states, tool permissions, and dependencies.
3. Provenance & Verification Integrity:
   Preserves typed sources (DocumentSnippet -> Candidate -> verification Episode -> Formal Skill, or
   Episode -> Candidate -> Formal Skill). Broken chains and fact conflicts are explicitly surfaced.
4. Evaluation & Failure Isolation:
   Evaluation episodes cannot serve as positive learning or recommendation evidence;
   failure experiences cannot be counted as success evidence.
5. Deterministic & Explainable:
   Multi-word token scoring with stable tie-breaking; explicit match and filtered reasons; bounded results.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

from .models import (
    CandidateSkill,
    Episode,
    FutureRetrievalResult,
    MemoryLineage,
    RetrievalContext,
    SemanticConflict,
    SemanticFact,
    SkillMeta,
    SkillRecommendation,
    VersionSnapshot,
)
from .registry import SkillRegistry


class FutureMemoryRetriever:
    """Bounded, read-only retriever discovering verified formal skills and their provenance for future tasks."""

    def __init__(
        self,
        memory_manager: Any,
        registry: Optional[SkillRegistry] = None,
        deployment_manager: Optional[Any] = None,
    ):
        self.memory_manager = memory_manager
        self.registry = registry or getattr(memory_manager, "registry", None)
        self.deployment_manager = deployment_manager or getattr(memory_manager, "deployment_manager", None)
        self.episode_store = memory_manager.episode_store
        self.candidate_store = memory_manager.candidate_store
        self.semantic_store = memory_manager.semantic_store
        self.document_store = memory_manager.document_store

    def retrieve(
        self,
        query: str,
        context: Optional[RetrievalContext] = None,
        tier: Optional[Literal["semantic", "episodic", "procedural"]] = None,
    ) -> FutureRetrievalResult:
        """Perform bounded, read-only, context-aware memory retrieval."""
        ctx = context or RetrievalContext()
        limit = max(1, ctx.limit)
        q_raw = query.strip()
        q_lower = q_raw.lower()
        terms = [t for t in re.split(r"[^\w\-]+", q_lower) if t]

        filtered_out: list[dict[str, Any]] = []
        conflicts: list[SemanticConflict] = []

        # ==================== 1. Semantic Memory Retrieval ====================
        matched_facts: list[SemanticFact] = []
        if tier in ("semantic", None):
            all_facts = self.semantic_store.list_facts()
            for fact in all_facts:
                fact_text = f"{fact.statement} {fact.topic} {fact.scope} {' '.join(fact.tags)}".lower()
                is_match = False
                if not terms:
                    is_match = True
                elif any(t in fact_text for t in terms) or q_lower in fact_text:
                    is_match = True

                if is_match:
                    # Scope filtering: if context.scope provided, facts must match scope or be universal
                    if ctx.scope is not None and not fact.is_universal and fact.scope != ctx.scope:
                        filtered_out.append({
                            "item_id": fact.fact_id,
                            "type": "semantic_fact",
                            "reason": f"Fact scope '{fact.scope}' does not match context scope '{ctx.scope}'",
                            "filter_type": "scope_mismatch",
                        })
                    else:
                        matched_facts.append(fact)

            # Surface conflicts on matched topics
            matched_topics = {f.topic for f in matched_facts}
            for top in matched_topics:
                conflicts.extend(self.semantic_store.detect_conflicts(topic=top))

        if tier == "semantic":
            empty_reason = None
            if not matched_facts:
                empty_reason = f"No semantic facts matched query '{query}'"
            return FutureRetrievalResult(
                query=query,
                evidence_facts=matched_facts[:limit],
                conflicts=conflicts,
                filtered_out=filtered_out,
                empty_reason=empty_reason,
            )

        # ==================== 2. Episodic Memory Retrieval ====================
        matched_episodes: list[Episode] = []
        if tier in ("episodic", None):
            all_episodes = self.episode_store.list_episodes()
            for ep in all_episodes:
                ep_text = (
                    f"{ep.task_id} {ep.skill_name} {ep.outcome} {ep.outcome_reason or ''} "
                    f"{str(ep.environment)} {str(ep.acceptance_criteria)}"
                ).lower()
                is_match = False
                if not terms:
                    is_match = True
                elif any(t in ep_text for t in terms) or q_lower in ep_text:
                    is_match = True

                if is_match:
                    # If querying specifically by episodic tier, return raw episode
                    if tier == "episodic":
                        matched_episodes.append(ep)
                    else:
                        # R4: When gathering evidence for recommendations:
                        # 1. evaluation episodes strictly isolated from learning/positive evidence
                        purpose = ep.environment.get("purpose") if isinstance(ep.environment, dict) else ""
                        if purpose == "evaluation":
                            filtered_out.append({
                                "item_id": ep.episode_id,
                                "type": "episode",
                                "reason": "Evaluation episode excluded from positive evidence (purpose isolation)",
                                "filter_type": "evaluation_isolation",
                            })
                            continue

                        # 2. failed experiences strictly cannot be counted as success evidence
                        if ep.outcome != "success":
                            filtered_out.append({
                                "item_id": ep.episode_id,
                                "type": "episode",
                                "reason": f"Episode with outcome '{ep.outcome}' cannot count as positive success evidence",
                                "filter_type": "failure_isolation",
                            })
                            continue

                        matched_episodes.append(ep)

        if tier == "episodic":
            empty_reason = None
            if not matched_episodes:
                empty_reason = f"No episodes matched query '{query}'"
            return FutureRetrievalResult(
                query=query,
                evidence_episodes=matched_episodes[:limit],
                filtered_out=filtered_out,
                empty_reason=empty_reason,
            )

        # ==================== 3. Procedural Memory Retrieval ====================
        matched_candidates: list[CandidateSkill] = []
        all_candidates = self.candidate_store.list_candidates()
        for cand in all_candidates:
            cand_text = f"{cand.skill_name} {cand.body} {cand.rationale} {cand.meta.description if cand.meta else ''}".lower()
            is_match = False
            if not terms:
                is_match = True
            elif any(t in cand_text for t in terms) or q_lower in cand_text:
                is_match = True

            if is_match:
                matched_candidates.append(cand)
                # Invariant R4: unpromoted candidates (DRAFT, EVALUATING, REJECTED) cannot appear in formal skills!
                if cand.status != "APPROVED":
                    filtered_out.append({
                        "item_id": cand.candidate_id,
                        "type": "candidate",
                        "skill_name": cand.skill_name,
                        "status": cand.status,
                        "reason": f"Candidate is {cand.status}, not a formal published skill",
                        "filter_type": "unpromoted_candidate",
                    })

        # Discover and filter Formal Skills
        recommendations: list[SkillRecommendation] = []
        known_skill_names: list[str] = []
        if self.registry:
            known_skill_names = self.registry.list_names()

        # Resolve fixed version binding for run_id (M4b)
        run_bound_version: Optional[str] = None
        if ctx.run_id and self.deployment_manager:
            conn = self.deployment_manager._get_conn()
            row = conn.execute(
                "SELECT assigned_version FROM run_version_bindings WHERE run_id = ?",
                (ctx.run_id,),
            ).fetchone()
            if row:
                run_bound_version = row[0]

        for skill_name in known_skill_names:
            # 1. Determine target version in context
            target_version: Optional[str] = None
            is_canary = False

            if ctx.run_id and self.deployment_manager:
                try:
                    assigned_ver, _, canary_flag = self.deployment_manager.route_version(skill_name, run_id=ctx.run_id)
                    target_version = assigned_ver
                    is_canary = canary_flag
                except Exception:
                    pass

            if target_version is None and ctx.assigned_version is not None:
                target_version = ctx.assigned_version

            if target_version is None and self.deployment_manager:
                try:
                    dep = self.deployment_manager.get_deployment(skill_name)
                    target_version = dep.stable_version
                except Exception:
                    pass

            if target_version is None and self.registry:
                meta = self.registry.get_meta(skill_name)
                target_version = meta.version if meta else "1.0.0"

            if not target_version:
                continue

            # 2. Fetch immutable version snapshot
            snap: Optional[VersionSnapshot] = None
            if self.deployment_manager:
                try:
                    snap = self.deployment_manager.get_version_snapshot(skill_name, target_version)
                except Exception as e:
                    filtered_out.append({
                        "skill_name": skill_name,
                        "version": target_version,
                        "reason": f"Version snapshot unavailable: {e}",
                        "filter_type": "snapshot_error",
                    })
                    continue
            else:
                meta = self.registry.get_meta(skill_name) if self.registry else None
                body = self.registry.get_body(skill_name) if self.registry else ""
                snap = VersionSnapshot(
                    skill_name=skill_name,
                    version=target_version,
                    content_hash="",
                    meta=meta,
                    body=body,
                    status="PUBLISHED",
                    is_verified=True,
                    dependencies=meta.dependencies if meta else [],
                )

            # 3. Check verification status
            if not snap.is_verified or snap.status in ("UNVERIFIED", "UNAVAILABLE", "DECLINED"):
                filtered_out.append({
                    "skill_name": skill_name,
                    "version": snap.version,
                    "reason": f"Skill version '{snap.version}' is not verified (status={snap.status})",
                    "filter_type": "unverified",
                })
                continue

            # 4. Check active deployment status (R3: cannot recommend rolled back/inactive versions)
            is_bound_to_run = bool(ctx.run_id and run_bound_version and target_version == run_bound_version)
            if not is_bound_to_run and self.deployment_manager:
                try:
                    dep = self.deployment_manager.get_deployment(skill_name)
                    if snap.version not in (dep.stable_version, dep.canary_version):
                        filtered_out.append({
                            "skill_name": skill_name,
                            "version": snap.version,
                            "reason": f"Skill version '{snap.version}' is rolled back or inactive (active stable={dep.stable_version})",
                            "filter_type": "inactive_version",
                        })
                        continue
                except Exception:
                    pass

            # 5. Check tool permissions and physical dependencies (R3)
            all_deps = snap.dependencies or (snap.meta.dependencies if snap.meta else [])
            permission_blocked = False
            block_reason = ""

            for dep in all_deps:
                # Check tool permission allowlist
                if ctx.allowed_tools is not None and dep not in ctx.allowed_tools:
                    # Could it be satisfied as a physical dependency?
                    if ctx.available_dependencies is not None and dep in ctx.available_dependencies:
                        continue
                    permission_blocked = True
                    block_reason = f"Tool '{dep}' not in caller's allowed tools"
                    break

                # Check physical dependencies
                if ctx.available_dependencies is not None and dep not in ctx.available_dependencies:
                    if ctx.allowed_tools is not None and dep in ctx.allowed_tools:
                        continue
                    permission_blocked = True
                    block_reason = f"Missing required dependency '{dep}'"
                    break

            if permission_blocked:
                filtered_out.append({
                    "skill_name": skill_name,
                    "version": snap.version,
                    "reason": block_reason,
                    "filter_type": "permission_or_dependency_denied",
                })
                continue

            # 6. Multi-word matching and deterministic scoring (R5)
            score = 0.0
            match_reasons: list[str] = []

            if not terms:
                score = 1.0
                match_reasons.append("Wildcard match")
            else:
                # Exact skill name match
                if q_lower == skill_name.lower():
                    score += 10.0
                    match_reasons.append(f"Exact name match: '{skill_name}'")

                # Term matches in skill name
                for t in terms:
                    if t in skill_name.lower():
                        score += 5.0
                        match_reasons.append(f"Name contains '{t}'")

                # Trigger keyword matches
                if snap.meta and snap.meta.trigger and snap.meta.trigger.keywords:
                    for kw in snap.meta.trigger.keywords:
                        kw_lower = kw.lower()
                        if kw_lower in q_lower:
                            score += 4.0
                            match_reasons.append(f"Trigger keyword query match: '{kw}'")
                        for t in terms:
                            if t == kw_lower:
                                score += 4.0
                                match_reasons.append(f"Trigger keyword exact match: '{kw}'")
                            elif t in kw_lower or kw_lower in t:
                                score += 2.0
                                match_reasons.append(f"Trigger keyword partial match: '{kw}'")

                # Description & use_when metadata matches
                if snap.meta:
                    meta_text = f"{snap.meta.description} {snap.meta.use_when}".lower()
                    for t in terms:
                        if t in meta_text:
                            score += 2.0
                            match_reasons.append(f"Metadata match: '{t}'")

                # Body text matches
                body_lower = snap.body.lower()
                for t in terms:
                    if t in body_lower:
                        score += 1.0
                        match_reasons.append(f"Body text match: '{t}'")

            if score <= 0.0:
                continue

            # 7. Trace lineage and determine source type (R2)
            lineage: Optional[MemoryLineage] = None
            source_type: Literal["episode_mined", "document_derived", "manual_or_unknown"] = "manual_or_unknown"
            verification_episodes: list[Episode] = []

            try:
                lineage = self.memory_manager.trace_lineage(skill_name)
                if lineage.source_document is not None:
                    source_type = "document_derived"
                    verification_episodes = [ep for ep in lineage.supporting_episodes if ep.outcome == "success"]
                elif lineage.supporting_episodes:
                    source_type = "episode_mined"
                    verification_episodes = [ep for ep in lineage.supporting_episodes if ep.outcome == "success"]
            except Exception:
                pass

            rec = SkillRecommendation(
                skill_name=skill_name,
                version=snap.version,
                content_hash=snap.content_hash,
                meta=snap.meta,
                body=snap.body,
                relevance_score=score,
                match_reasons=match_reasons,
                lineage=lineage,
                source_type=source_type,
                verification_episodes=verification_episodes,
                is_verified=snap.is_verified,
                is_canary=is_canary,
                dependencies=all_deps,
                lineage_broken=lineage.lineage_broken if lineage else False,
                broken_reasons=lineage.broken_reasons if lineage else [],
            )
            recommendations.append(rec)

        # 8. Deterministic sorting and tie-breaking (R5)
        # Higher score first, then alphabetical by skill_name, then by version
        recommendations.sort(
            key=lambda r: (-round(r.relevance_score, 4), r.skill_name, r.version)
        )
        recommendations = recommendations[:limit]

        # 9. Formulate explainable empty reason if no recommendations
        empty_reason: Optional[str] = None
        if not recommendations:
            if filtered_out:
                reasons_summary = "; ".join(
                    f"{item.get('skill_name') or item.get('item_id')}: {item['reason']}"
                    for item in filtered_out
                )
                empty_reason = f"All matching skills filtered out: [{reasons_summary}]"
            else:
                empty_reason = f"No formal skills matched query '{query}'"

        return FutureRetrievalResult(
            query=query,
            skills=recommendations,
            evidence_episodes=matched_episodes[:limit],
            evidence_facts=matched_facts[:limit],
            candidates=matched_candidates[:limit],
            conflicts=conflicts,
            filtered_out=filtered_out,
            empty_reason=empty_reason,
        )
