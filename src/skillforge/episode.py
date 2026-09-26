"""Episode & Candidate Storage Module

Provides data persistence, integrity validation, and isolation for:
1. EpisodeStore: Execution experience records with tool call provenances and verification evidence.
2. CandidateStore: Isolated candidate skills linked to valid source episodes, strictly separated
   from active retrieval and execution.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .models import (
    Episode,
    CandidateSkill,
    CandidateDecision,
    CandidateStatus,
    ToolCallProvenance,
    SkillMeta,
)
from .storage.db import init_db


class EpisodeStore:
    """SQLite-backed store for execution episodes."""

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

    def has_episode(self, episode_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return row is not None

    def save_episode(
        self,
        episode: Episode,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> str:
        """Persist an Episode record.

        Args:
            episode: Episode instance with verified acceptance evidence.
            on_conflict: 'error' (raise ValueError if exists) or 'ignore' (keep existing).

        Returns:
            The episode_id.
        """
        episode.validate()
        conn = self._get_conn()

        if self.has_episode(episode.episode_id):
            if on_conflict == "error":
                raise ValueError(
                    f"Episode with id '{episode.episode_id}' already exists"
                )
            return episode.episode_id

        provenances_data = [asdict(p) for p in episode.provenances]
        acceptance_data = (
            episode.acceptance_criteria
            if isinstance(episode.acceptance_criteria, (dict, list))
            else {"text": str(episode.acceptance_criteria)}
        )
        created_at = episode.created_at or datetime.now(timezone.utc).isoformat()

        conn.execute(
            """INSERT INTO episodes (
                episode_id, task_id, run_id, skill_name, skill_version,
                environment_json, provenances_json, acceptance_json,
                verification_json, outcome, outcome_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.episode_id,
                episode.task_id,
                episode.run_id,
                episode.skill_name,
                episode.skill_version,
                json.dumps(episode.environment, ensure_ascii=False),
                json.dumps(provenances_data, ensure_ascii=False),
                json.dumps(acceptance_data, ensure_ascii=False),
                json.dumps(episode.verification_evidence, ensure_ascii=False)
                if episode.verification_evidence is not None
                else None,
                episode.outcome,
                episode.outcome_reason,
                created_at,
            ),
        )
        conn.commit()
        return episode.episode_id

    def get_episode(self, episode_id: str) -> Optional[Episode]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                episode_id, task_id, run_id, skill_name, skill_version,
                environment_json, provenances_json, acceptance_json,
                verification_json, outcome, outcome_reason, created_at
            FROM episodes WHERE episode_id = ?""",
            (episode_id,),
        ).fetchone()

        if not row:
            return None

        (
            eid,
            tid,
            rid,
            sname,
            sver,
            env_j,
            prov_j,
            acc_j,
            ver_j,
            outcome,
            reason,
            cat,
        ) = row

        raw_provs = json.loads(prov_j) if prov_j else []
        provenances = [ToolCallProvenance(**p) for p in raw_provs]
        environment = json.loads(env_j) if env_j else {}
        acceptance = json.loads(acc_j) if acc_j else {}
        verification = json.loads(ver_j) if ver_j else None

        return Episode(
            episode_id=eid,
            task_id=tid,
            run_id=rid,
            skill_name=sname,
            skill_version=sver,
            environment=environment,
            provenances=provenances,
            acceptance_criteria=acceptance,
            outcome=outcome,
            verification_evidence=verification,
            outcome_reason=reason or "",
            created_at=cat,
        )

    def list_episodes(
        self,
        skill_name: Optional[str] = None,
        outcome: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[Episode]:
        conn = self._get_conn()
        query = """SELECT
            episode_id, task_id, run_id, skill_name, skill_version,
            environment_json, provenances_json, acceptance_json,
            verification_json, outcome, outcome_reason, created_at
        FROM episodes WHERE 1=1"""
        params: list[Any] = []

        if skill_name is not None:
            query += " AND skill_name = ?"
            params.append(skill_name)
        if outcome is not None:
            query += " AND outcome = ?"
            params.append(outcome)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)

        query += " ORDER BY created_at ASC"
        rows = conn.execute(query, tuple(params)).fetchall()

        results: list[Episode] = []
        for r in rows:
            raw_provs = json.loads(r[6]) if r[6] else []
            provs = [ToolCallProvenance(**p) for p in raw_provs]
            results.append(
                Episode(
                    episode_id=r[0],
                    task_id=r[1],
                    run_id=r[2],
                    skill_name=r[3],
                    skill_version=r[4],
                    environment=json.loads(r[5]) if r[5] else {},
                    provenances=provs,
                    acceptance_criteria=json.loads(r[7]) if r[7] else {},
                    outcome=r[9],
                    verification_evidence=json.loads(r[8]) if r[8] else None,
                    outcome_reason=r[10] or "",
                    created_at=r[11],
                )
            )
        return results


class CandidateStore:
    """SQLite-backed isolated store for candidate skills.

    Candidates in this store are strictly isolated and NEVER exposed to
    regular skill registries or active execution routing.
    """

    def __init__(self, db_path: Path, episode_store: Optional[EpisodeStore] = None):
        self.db_path = db_path
        self._episode_store = episode_store or EpisodeStore(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._episode_store is not None:
            self._episode_store.close()

    def has_candidate(self, candidate_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM candidate_skills WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return row is not None

    def save_candidate(
        self,
        candidate: CandidateSkill,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> str:
        """Persist an isolated CandidateSkill.

        Validates:
        1. Decision must be 'create', 'revise', or 'abandon'.
        2. source_episode_ids must not be empty.
        3. All source_episode_ids must exist in EpisodeStore.
        4. Rejects overwriting an existing candidate_id (revisions must use a new candidate_id).
        """
        if candidate.decision not in ("create", "revise", "abandon"):
            raise ValueError(f"Invalid decision: '{candidate.decision}'")

        if not candidate.source_episode_ids and not candidate.source_doc_id:
            raise ValueError("Candidate must be linked to at least one valid source episode or document")

        # Verify source episodes existence ("不存在来源拒绝")
        for eid in candidate.source_episode_ids:
            if not self._episode_store.has_episode(eid):
                raise KeyError(
                    f"Invalid source_episode_id: '{eid}' does not exist in EpisodeStore"
                )

        if self.has_candidate(candidate.candidate_id):
            if on_conflict == "error":
                raise ValueError(
                    f"Candidate with id '{candidate.candidate_id}' already exists. "
                    "Cannot overwrite existing candidate; revisions must use a new candidate_id."
                )
            return candidate.candidate_id

        status = candidate.status
        if candidate.decision == "abandon" and status == "DRAFT":
            status = "ABANDONED"

        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = candidate.created_at or now_iso
        updated_at = now_iso

        meta_dict = (
            candidate.meta.model_dump()
            if hasattr(candidate.meta, "model_dump")
            else candidate.meta.dict()
        )
        meta_json = json.dumps(meta_dict, ensure_ascii=False)
        snips_json = (
            json.dumps(candidate.source_snippet_ids, ensure_ascii=False)
            if candidate.source_snippet_ids
            else None
        )

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO candidate_skills (
                candidate_id, skill_name, decision, source_episode_ids,
                status, meta_json, body_md, rationale,
                source_doc_id, source_doc_version, source_snippet_ids,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.candidate_id,
                candidate.skill_name,
                candidate.decision,
                json.dumps(candidate.source_episode_ids, ensure_ascii=False),
                status,
                meta_json,
                candidate.body,
                candidate.rationale,
                candidate.source_doc_id,
                candidate.source_doc_version,
                snips_json,
                created_at,
                updated_at,
            ),
        )
        conn.commit()
        return candidate.candidate_id

    def get_candidate(self, candidate_id: str) -> Optional[CandidateSkill]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                candidate_id, skill_name, decision, source_episode_ids,
                status, meta_json, body_md, rationale,
                source_doc_id, source_doc_version, source_snippet_ids,
                created_at, updated_at
            FROM candidate_skills WHERE candidate_id = ?""",
            (candidate_id,),
        ).fetchone()

        if not row:
            return None

        (
            cid, sname, decision, eids_j, status, meta_j, body, rationale,
            doc_id, doc_ver, snips_j, cat, uat
        ) = row
        meta_data = json.loads(meta_j) if meta_j else {}
        meta = SkillMeta(**meta_data)
        source_eids = json.loads(eids_j) if eids_j else []
        source_snips = json.loads(snips_j) if snips_j else []

        return CandidateSkill(
            candidate_id=cid,
            skill_name=sname,
            decision=decision,
            source_episode_ids=source_eids,
            meta=meta,
            body=body,
            rationale=rationale,
            status=status,
            source_doc_id=doc_id,
            source_doc_version=doc_ver,
            source_snippet_ids=source_snips,
            created_at=cat,
            updated_at=uat,
        )

    def list_candidates(
        self,
        skill_name: Optional[str] = None,
        status: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> list[CandidateSkill]:
        conn = self._get_conn()
        query = """SELECT
            candidate_id, skill_name, decision, source_episode_ids,
            status, meta_json, body_md, rationale,
            source_doc_id, source_doc_version, source_snippet_ids,
            created_at, updated_at
        FROM candidate_skills WHERE 1=1"""
        params: list[Any] = []

        if skill_name is not None:
            query += " AND skill_name = ?"
            params.append(skill_name)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if decision is not None:
            query += " AND decision = ?"
            params.append(decision)

        query += " ORDER BY created_at ASC"
        rows = conn.execute(query, tuple(params)).fetchall()

        results: list[CandidateSkill] = []
        for r in rows:
            meta_data = json.loads(r[5]) if r[5] else {}
            results.append(
                CandidateSkill(
                    candidate_id=r[0],
                    skill_name=r[1],
                    decision=r[2],
                    source_episode_ids=json.loads(r[3]) if r[3] else [],
                    status=r[4],
                    meta=SkillMeta(**meta_data),
                    body=r[6],
                    rationale=r[7],
                    source_doc_id=r[8],
                    source_doc_version=r[9],
                    source_snippet_ids=json.loads(r[10]) if r[10] else [],
                    created_at=r[11],
                    updated_at=r[12],
                )
            )
        return results

    def update_decision(
        self,
        candidate_id: str,
        decision: CandidateDecision,
        rationale: str,
    ) -> CandidateSkill:
        """Update decision and rationale on an existing candidate."""
        candidate = self.get_candidate(candidate_id)
        if not candidate:
            raise KeyError(f"Candidate '{candidate_id}' does not exist")

        if decision not in ("create", "revise", "abandon"):
            raise ValueError(f"Invalid decision: '{decision}'")

        status = candidate.status
        if decision == "abandon":
            status = "ABANDONED"

        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """UPDATE candidate_skills
               SET decision = ?, rationale = ?, status = ?, updated_at = ?
               WHERE candidate_id = ?""",
            (decision, rationale, status, now_iso, candidate_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)  # type: ignore[return-value]

    def update_status(self, candidate_id: str, status: CandidateStatus) -> None:
        """Update lifecycle status of a candidate skill."""
        if not self.has_candidate(candidate_id):
            raise KeyError(f"Candidate '{candidate_id}' does not exist")
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE candidate_skills SET status = ?, updated_at = ? WHERE candidate_id = ?",
            (status, now_iso, candidate_id),
        )
        conn.commit()

