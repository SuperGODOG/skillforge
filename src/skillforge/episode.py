"""Episode & Candidate Storage Module

Provides data persistence, integrity validation, and isolation for:
1. EpisodeStore: Execution experience records with tool call provenances and verification evidence.
2. CandidateStore: Isolated candidate skills linked to valid source episodes, strictly separated
   from active retrieval and execution.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
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
    ValidationRecord,
    RatchetVerdict,
    EvalResult,
    TaskContext,
    TestCaseProposal,
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
        on_conflict: Literal["error", "ignore", "update"] = "error",
    ) -> str:
        """Persist an isolated CandidateSkill.

        Validates:
        1. Decision must be 'create', 'revise', or 'abandon'.
        2. source_episode_ids must not be empty.
        3. All source_episode_ids must exist in EpisodeStore.
        4. Rejects overwriting an existing candidate_id (revisions must use a new candidate_id),
           unless on_conflict='update' is explicitly specified.
        """
        if candidate.decision not in ("create", "revise", "abandon"):
            raise ValueError(f"Invalid decision: '{candidate.decision}'")

        if not candidate.source_episode_ids and not candidate.source_doc_id and not candidate.source_requirement:
            raise ValueError("Candidate must be linked to at least one valid source episode, document, or requirement")

        # Verify source episodes existence ("不存在来源拒绝")
        for eid in candidate.source_episode_ids:
            if not self._episode_store.has_episode(eid):
                raise KeyError(
                    f"Invalid source_episode_id: '{eid}' does not exist in EpisodeStore"
                )

        now_iso = datetime.now(timezone.utc).isoformat()
        if self.has_candidate(candidate.candidate_id):
            if on_conflict == "update":
                conn = self._get_conn()
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
                source_msg_ids_json = (
                    json.dumps(candidate.source_message_ids, ensure_ascii=False)
                    if getattr(candidate, "source_message_ids", None)
                    else None
                )
                conn.execute(
                    """UPDATE candidate_skills SET
                        skill_name = ?, decision = ?, source_episode_ids = ?,
                        status = ?, meta_json = ?, body_md = ?, rationale = ?,
                        source_doc_id = ?, source_doc_version = ?, source_snippet_ids = ?,
                        source_requirement = ?, source_type = ?, task_spec_hash = ?,
                        intent_revision = ?, superseded_by = ?, supersedes = ?,
                        source_session_id = ?, source_message_ids = ?,
                        updated_at = ?
                    WHERE candidate_id = ?""",
                    (
                        candidate.skill_name,
                        candidate.decision,
                        json.dumps(candidate.source_episode_ids, ensure_ascii=False),
                        candidate.status,
                        meta_json,
                        candidate.body,
                        candidate.rationale,
                        candidate.source_doc_id,
                        candidate.source_doc_version,
                        snips_json,
                        candidate.source_requirement,
                        candidate.source_type,
                        candidate.task_spec_hash,
                        getattr(candidate, "intent_revision", 1),
                        getattr(candidate, "superseded_by", None),
                        getattr(candidate, "supersedes", None),
                        getattr(candidate, "source_session_id", None),
                        source_msg_ids_json,
                        now_iso,
                        candidate.candidate_id,
                    ),
                )
                conn.commit()
                return candidate.candidate_id
            elif on_conflict == "error":
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
        source_msg_ids_json = (
            json.dumps(candidate.source_message_ids, ensure_ascii=False)
            if getattr(candidate, "source_message_ids", None)
            else None
        )

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO candidate_skills (
                candidate_id, skill_name, decision, source_episode_ids,
                status, meta_json, body_md, rationale,
                source_doc_id, source_doc_version, source_snippet_ids,
                source_requirement, source_type, task_spec_hash,
                intent_revision, superseded_by, supersedes,
                source_session_id, source_message_ids,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                candidate.source_requirement,
                candidate.source_type,
                candidate.task_spec_hash,
                getattr(candidate, "intent_revision", 1),
                getattr(candidate, "superseded_by", None),
                getattr(candidate, "supersedes", None),
                getattr(candidate, "source_session_id", None),
                source_msg_ids_json,
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
                source_requirement, source_type, task_spec_hash,
                intent_revision, superseded_by, supersedes,
                source_session_id, source_message_ids,
                created_at, updated_at
            FROM candidate_skills WHERE candidate_id = ?""",
            (candidate_id,),
        ).fetchone()

        if not row:
            return None

        (
            cid, sname, decision, eids_j, status, meta_j, body, rationale,
            doc_id, doc_ver, snips_j, s_req, s_type, spec_hash,
            i_rev, s_by, s_from, s_sess, s_msgs_j, cat, uat
        ) = row
        meta_data = json.loads(meta_j) if meta_j else {}
        meta = SkillMeta(**meta_data)
        source_eids = json.loads(eids_j) if eids_j else []
        source_snips = json.loads(snips_j) if snips_j else []
        source_msgs = json.loads(s_msgs_j) if s_msgs_j else []

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
            source_requirement=s_req,
            source_type=s_type,
            task_spec_hash=spec_hash,
            intent_revision=int(i_rev) if i_rev is not None else 1,
            superseded_by=s_by,
            supersedes=s_from,
            source_session_id=s_sess,
            source_message_ids=source_msgs,
            created_at=cat,
            updated_at=uat,
        )

    def get_candidate_by_spec_hash(
        self,
        spec_hash: str,
        status: Optional[str] = "DRAFT",
    ) -> Optional[CandidateSkill]:
        """Fetch the most recent candidate matching task_spec_hash and optional status."""
        conn = self._get_conn()
        query = """SELECT
            candidate_id, skill_name, decision, source_episode_ids,
            status, meta_json, body_md, rationale,
            source_doc_id, source_doc_version, source_snippet_ids,
            source_requirement, source_type, task_spec_hash,
            intent_revision, superseded_by, supersedes,
            source_session_id, source_message_ids,
            created_at, updated_at
        FROM candidate_skills WHERE task_spec_hash = ?"""
        params: list[Any] = [spec_hash]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = conn.execute(query, tuple(params)).fetchone()
        if not row:
            return None
        (
            cid, sname, decision, eids_j, st, meta_j, body, rationale,
            doc_id, doc_ver, snips_j, s_req, s_type, s_hash,
            i_rev, s_by, s_from, s_sess, s_msgs_j, cat, uat
        ) = row
        meta_data = json.loads(meta_j) if meta_j else {}
        meta = SkillMeta(**meta_data)
        source_eids = json.loads(eids_j) if eids_j else []
        source_snips = json.loads(snips_j) if snips_j else []
        source_msgs = json.loads(s_msgs_j) if s_msgs_j else []
        return CandidateSkill(
            candidate_id=cid,
            skill_name=sname,
            decision=decision,
            source_episode_ids=source_eids,
            meta=meta,
            body=body,
            rationale=rationale,
            status=st,
            source_doc_id=doc_id,
            source_doc_version=doc_ver,
            source_snippet_ids=source_snips,
            source_requirement=s_req,
            source_type=s_type,
            task_spec_hash=s_hash,
            intent_revision=int(i_rev) if i_rev is not None else 1,
            superseded_by=s_by,
            supersedes=s_from,
            source_session_id=s_sess,
            source_message_ids=source_msgs,
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
            source_requirement, source_type, task_spec_hash,
            intent_revision, superseded_by, supersedes,
            source_session_id, source_message_ids,
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
                    source_requirement=r[11],
                    source_type=r[12],
                    task_spec_hash=r[13],
                    intent_revision=int(r[14]) if r[14] is not None else 1,
                    superseded_by=r[15],
                    supersedes=r[16],
                    source_session_id=r[17],
                    source_message_ids=json.loads(r[18]) if r[18] else [],
                    created_at=r[19],
                    updated_at=r[20],
                )
            )
        return results

    def supersede_candidate(self, old_candidate_id: str, new_candidate_id: str) -> None:
        """Mark an existing candidate as SUPERSEDED and link it to the superseding candidate."""
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE candidate_skills
               SET status = 'SUPERSEDED', superseded_by = ?, updated_at = ?
               WHERE candidate_id = ?""",
            (new_candidate_id, now_iso, old_candidate_id),
        )
        conn.execute(
            """UPDATE candidate_skills
               SET supersedes = ?, updated_at = ?
               WHERE candidate_id = ?""",
            (old_candidate_id, now_iso, new_candidate_id),
        )
        conn.commit()

    def save_task_context(self, task_ctx: TaskContext) -> None:
        """Persist or update bounded TaskContext."""
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = task_ctx.created_at or now_iso
        updated_at = now_iso
        fp = task_ctx.contract_fingerprint or task_ctx.compute_fingerprint()
        task_ctx.contract_fingerprint = fp

        conn.execute(
            """INSERT INTO task_contexts (
                task_id, goal, business_scope, constraints_json,
                acceptance_criteria_json, intent_revision, contract_fingerprint,
                active_candidate_id, active_skill_name, active_skill_version,
                active_body_snapshot, superseded_cands_json, assumptions_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                goal = excluded.goal,
                business_scope = excluded.business_scope,
                constraints_json = excluded.constraints_json,
                acceptance_criteria_json = excluded.acceptance_criteria_json,
                intent_revision = excluded.intent_revision,
                contract_fingerprint = excluded.contract_fingerprint,
                active_candidate_id = excluded.active_candidate_id,
                active_skill_name = excluded.active_skill_name,
                active_skill_version = excluded.active_skill_version,
                active_body_snapshot = excluded.active_body_snapshot,
                superseded_cands_json = excluded.superseded_cands_json,
                assumptions_json = excluded.assumptions_json,
                updated_at = excluded.updated_at""",
            (
                task_ctx.task_id,
                task_ctx.goal,
                task_ctx.business_scope,
                json.dumps(task_ctx.constraints, ensure_ascii=False),
                json.dumps(task_ctx.acceptance_criteria, ensure_ascii=False),
                task_ctx.intent_revision,
                fp,
                task_ctx.active_candidate_id,
                task_ctx.active_skill_name,
                task_ctx.active_skill_version,
                task_ctx.active_body_snapshot,
                json.dumps(task_ctx.superseded_candidate_ids, ensure_ascii=False),
                json.dumps(task_ctx.assumptions, ensure_ascii=False),
                created_at,
                updated_at,
            ),
        )
        conn.commit()

    def get_task_context(self, task_id: str) -> Optional[TaskContext]:
        """Fetch TaskContext by task_id."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                task_id, goal, business_scope, constraints_json,
                acceptance_criteria_json, intent_revision, contract_fingerprint,
                active_candidate_id, active_skill_name, active_skill_version,
                active_body_snapshot, superseded_cands_json, assumptions_json,
                created_at, updated_at
            FROM task_contexts WHERE task_id = ?""",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return TaskContext(
            task_id=row[0],
            goal=row[1],
            business_scope=row[2] or "",
            constraints=json.loads(row[3]) if row[3] else [],
            acceptance_criteria=json.loads(row[4]) if row[4] else {},
            intent_revision=int(row[5]),
            contract_fingerprint=row[6],
            active_candidate_id=row[7],
            active_skill_name=row[8],
            active_skill_version=row[9],
            active_body_snapshot=row[10],
            superseded_candidate_ids=json.loads(row[11]) if row[11] else [],
            assumptions=json.loads(row[12]) if row[12] else [],
            created_at=row[13],
            updated_at=row[14],
        )

    def get_task_context_by_fingerprint(self, fingerprint: str) -> Optional[TaskContext]:
        """Fetch TaskContext by contract_fingerprint."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                task_id, goal, business_scope, constraints_json,
                acceptance_criteria_json, intent_revision, contract_fingerprint,
                active_candidate_id, active_skill_name, active_skill_version,
                active_body_snapshot, superseded_cands_json, assumptions_json,
                created_at, updated_at
            FROM task_contexts WHERE contract_fingerprint = ?
            ORDER BY updated_at DESC LIMIT 1""",
            (fingerprint,),
        ).fetchone()
        if not row:
            return None
        return TaskContext(
            task_id=row[0],
            goal=row[1],
            business_scope=row[2] or "",
            constraints=json.loads(row[3]) if row[3] else [],
            acceptance_criteria=json.loads(row[4]) if row[4] else {},
            intent_revision=int(row[5]),
            contract_fingerprint=row[6],
            active_candidate_id=row[7],
            active_skill_name=row[8],
            active_skill_version=row[9],
            active_body_snapshot=row[10],
            superseded_candidate_ids=json.loads(row[11]) if row[11] else [],
            assumptions=json.loads(row[12]) if row[12] else [],
            created_at=row[13],
            updated_at=row[14],
        )

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

    def has_validation_record(self, candidate_id: str) -> bool:
        """Check if a validation record exists for the candidate."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM validation_records WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return row is not None

    def save_validation_record(self, record: ValidationRecord) -> str:
        """Persist a ValidationRecord in SQLite validation_records table."""
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        record_id = record.record_id or f"vrec_{uuid.uuid4().hex[:12]}"
        created_at = record.created_at or now_iso
        updated_at = now_iso

        eval_j = None
        if record.eval_result is not None:
            try:
                if hasattr(record.eval_result, "model_dump"):
                    eval_j = json.dumps(record.eval_result.model_dump(), ensure_ascii=False)
                elif hasattr(record.eval_result, "dict"):
                    eval_j = json.dumps(record.eval_result.dict(), ensure_ascii=False)
                else:
                    eval_j = json.dumps(asdict(record.eval_result), ensure_ascii=False)
            except Exception:
                eval_j = json.dumps(
                    {
                        "score": getattr(record.eval_result, "score", 0.0),
                        "valid": getattr(record.eval_result, "valid", True),
                    },
                    ensure_ascii=False,
                )

        ratchet_j = None
        if record.ratchet_verdict is not None:
            try:
                ratchet_j = json.dumps(asdict(record.ratchet_verdict), ensure_ascii=False)
            except Exception:
                ratchet_j = json.dumps(
                    {
                        "decision": getattr(record.ratchet_verdict, "decision", "DECLINED"),
                        "reasons": getattr(record.ratchet_verdict, "reasons", []),
                    },
                    ensure_ascii=False,
                )

        veids_j = json.dumps(record.verification_episode_ids or [], ensure_ascii=False)

        conn.execute(
            """INSERT INTO validation_records (
                record_id, candidate_id, content_hash, baseline_version,
                ratchet_decision, eval_result_json, ratchet_verdict_json,
                promoted, release_id, verification_eids_json, scope_hash,
                config_hash, dataset_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                baseline_version = excluded.baseline_version,
                ratchet_decision = excluded.ratchet_decision,
                eval_result_json = excluded.eval_result_json,
                ratchet_verdict_json = excluded.ratchet_verdict_json,
                promoted = excluded.promoted,
                release_id = excluded.release_id,
                verification_eids_json = excluded.verification_eids_json,
                scope_hash = excluded.scope_hash,
                config_hash = excluded.config_hash,
                dataset_version = excluded.dataset_version,
                updated_at = excluded.updated_at""",
            (
                record_id,
                record.candidate_id,
                record.content_hash,
                record.baseline_version,
                record.ratchet_decision,
                eval_j,
                ratchet_j,
                1 if record.promoted else 0,
                record.release_id,
                veids_j,
                record.scope_hash,
                record.config_hash,
                record.dataset_version,
                created_at,
                updated_at,
            ),
        )

        # Sync candidate status if PASS
        if self.has_candidate(record.candidate_id):
            cand = self.get_candidate(record.candidate_id)
            if cand and cand.status == "DRAFT" and record.ratchet_decision == "PASS":
                conn.execute(
                    "UPDATE candidate_skills SET status = 'APPROVED', updated_at = ? WHERE candidate_id = ?",
                    (now_iso, record.candidate_id),
                )
        conn.commit()

        record.record_id = record_id
        record.created_at = created_at
        record.updated_at = updated_at
        return record_id

    def get_validation_record(self, candidate_id: str) -> Optional[ValidationRecord]:
        """Fetch stored ValidationRecord for a candidate."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT
                record_id, candidate_id, content_hash, baseline_version,
                ratchet_decision, eval_result_json, ratchet_verdict_json,
                promoted, release_id, verification_eids_json, scope_hash,
                config_hash, dataset_version, created_at, updated_at
            FROM validation_records WHERE candidate_id = ?""",
            (candidate_id,),
        ).fetchone()

        if not row:
            return None

        (
            rid,
            cid,
            chash,
            bver,
            rdec,
            eval_j,
            ratchet_j,
            promoted,
            rel_id,
            veids_j,
            sc_hash,
            cfg_hash,
            ds_ver,
            cat,
            uat,
        ) = row

        eval_result = None
        if eval_j:
            try:
                eval_data = json.loads(eval_j)
                eval_result = EvalResult(
                    release_id=eval_data.get("release_id", "stored_eval"),
                    structure_score=eval_data.get("structure_score", {}),
                    effect_score=eval_data.get("effect_score", {}),
                    objective_metrics=eval_data.get("objective_metrics", {}),
                    p0_pass=eval_data.get("p0_pass", True),
                    valid=eval_data.get("valid", True),
                    invalid_reasons=eval_data.get("invalid_reasons", []),
                    scoring_policy=eval_data.get("scoring_policy", "legacy_pairwise_v1"),
                    criteria_findings=eval_data.get("criteria_findings", []),
                    critical_fail=eval_data.get("critical_fail", False),
                    critical_reasons=eval_data.get("critical_reasons", []),
                )
            except Exception:
                eval_result = None

        ratchet_verdict = None
        if ratchet_j:
            try:
                ratchet_data = json.loads(ratchet_j)
                ratchet_verdict = RatchetVerdict(
                    decision=ratchet_data.get("decision", "DECLINED"),
                    reasons=ratchet_data.get("reasons", []),
                )
            except Exception:
                ratchet_verdict = None

        return ValidationRecord(
            record_id=rid,
            candidate_id=cid,
            content_hash=chash,
            baseline_version=bver,
            ratchet_decision=rdec,
            eval_result=eval_result,
            ratchet_verdict=ratchet_verdict,
            promoted=bool(promoted),
            release_id=rel_id,
            verification_episode_ids=json.loads(veids_j) if veids_j else [],
            scope_hash=sc_hash,
            config_hash=cfg_hash,
            dataset_version=ds_ver,
            created_at=cat,
            updated_at=uat,
        )

    def mark_promoted(self, candidate_id: str, release_id: Optional[str] = None) -> None:
        """Mark a validation record and candidate as promoted/approved."""
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """UPDATE validation_records
               SET promoted = 1,
                   release_id = COALESCE(?, release_id),
                   updated_at = ?
               WHERE candidate_id = ?""",
            (release_id, now_iso, candidate_id),
        )
        if cur.rowcount == 0 and not self.has_validation_record(candidate_id):
            raise KeyError(f"Validation record for candidate '{candidate_id}' does not exist")
        if self.has_candidate(candidate_id):
            conn.execute(
                "UPDATE candidate_skills SET status = 'APPROVED', updated_at = ? WHERE candidate_id = ?",
                (now_iso, candidate_id),
            )
        conn.commit()

    def save_proposal(self, proposal: TestCaseProposal) -> None:
        """Persist a TestCaseProposal to SQLite."""
        conn = self._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = proposal.created_at or now_iso
        updated_at = now_iso
        conn.execute(
            """INSERT INTO test_case_proposals (
                proposal_id, skill_name, source_task_id, intent_revision,
                contract_fingerprint, query, tool_snapshots_json, expected_output_json,
                expectation_source, status, failure_attribution, is_regression_case,
                actual_output, rejection_reason, partition_tier, variant_family,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                skill_name = excluded.skill_name,
                source_task_id = excluded.source_task_id,
                intent_revision = excluded.intent_revision,
                contract_fingerprint = excluded.contract_fingerprint,
                query = excluded.query,
                tool_snapshots_json = excluded.tool_snapshots_json,
                expected_output_json = excluded.expected_output_json,
                expectation_source = excluded.expectation_source,
                status = excluded.status,
                failure_attribution = excluded.failure_attribution,
                is_regression_case = excluded.is_regression_case,
                actual_output = excluded.actual_output,
                rejection_reason = excluded.rejection_reason,
                partition_tier = excluded.partition_tier,
                variant_family = excluded.variant_family,
                updated_at = excluded.updated_at
            """,
            (
                proposal.proposal_id,
                proposal.skill_name,
                proposal.source_task_id,
                proposal.intent_revision,
                proposal.contract_fingerprint,
                proposal.query,
                json.dumps(proposal.tool_snapshots),
                json.dumps(proposal.expected_output) if proposal.expected_output is not None else None,
                proposal.expectation_source,
                proposal.status,
                proposal.failure_attribution,
                1 if proposal.is_regression_case else 0,
                proposal.actual_output,
                proposal.rejection_reason,
                proposal.partition_tier,
                proposal.variant_family,
                created_at,
                updated_at,
            ),
        )
        conn.commit()

    def get_proposal(self, proposal_id: str) -> Optional[TestCaseProposal]:
        """Fetch a TestCaseProposal by proposal_id."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT proposal_id, skill_name, source_task_id, intent_revision,
                      contract_fingerprint, query, tool_snapshots_json, expected_output_json,
                      expectation_source, status, failure_attribution, is_regression_case,
                      actual_output, rejection_reason, partition_tier, variant_family,
                      created_at, updated_at
               FROM test_case_proposals WHERE proposal_id = ?""",
            (proposal_id,),
        ).fetchone()
        if not row:
            return None
        return TestCaseProposal(
            proposal_id=row[0],
            skill_name=row[1],
            source_task_id=row[2],
            intent_revision=int(row[3]),
            contract_fingerprint=row[4],
            query=row[5],
            tool_snapshots=json.loads(row[6]) if row[6] else [],
            expected_output=json.loads(row[7]) if row[7] is not None else None,
            expectation_source=row[8],
            status=row[9],
            failure_attribution=row[10],
            is_regression_case=bool(row[11]),
            actual_output=row[12],
            rejection_reason=row[13],
            partition_tier=row[14],
            variant_family=row[15],
            created_at=row[16],
            updated_at=row[17],
        )

    def list_proposals(
        self, skill_name: Optional[str] = None, status: Optional[str] = None
    ) -> list[TestCaseProposal]:
        """List TestCaseProposal records with optional filtering."""
        conn = self._get_conn()
        query = """SELECT proposal_id, skill_name, source_task_id, intent_revision,
                          contract_fingerprint, query, tool_snapshots_json, expected_output_json,
                          expectation_source, status, failure_attribution, is_regression_case,
                          actual_output, rejection_reason, partition_tier, variant_family,
                          created_at, updated_at
                   FROM test_case_proposals WHERE 1=1"""
        params: list[Any] = []
        if skill_name:
            query += " AND skill_name = ?"
            params.append(skill_name)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, tuple(params)).fetchall()
        return [
            TestCaseProposal(
                proposal_id=r[0],
                skill_name=r[1],
                source_task_id=r[2],
                intent_revision=int(r[3]),
                contract_fingerprint=r[4],
                query=r[5],
                tool_snapshots=json.loads(r[6]) if r[6] else [],
                expected_output=json.loads(r[7]) if r[7] is not None else None,
                expectation_source=r[8],
                status=r[9],
                failure_attribution=r[10],
                is_regression_case=bool(r[11]),
                actual_output=r[12],
                rejection_reason=r[13],
                partition_tier=r[14],
                variant_family=r[15],
                created_at=r[16],
                updated_at=r[17],
            )
            for r in rows
        ]



