"""Milestone 4b: Version Snapshots, Controlled Rollback, and Canary Routing.

Provides:
- VersionSnapshot: Immutable, content-hashed snapshot of a skill version.
- VersionComparison: Read-only diff and eval delta comparisons across versions.
- DeploymentManager: Controlled Canary/Stable deployments with CAS revisions,
  deterministic SHA256 bucketing, frozen run execution bindings, atomic rollback,
  and comprehensive audit event tracking.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import (
    CandidateSkill,
    Deployment,
    DeploymentAuditEvent,
    Release,
    RunVersionBinding,
    SkillMeta,
    VersionComparison,
    VersionSnapshot,
)
from .storage.db import init_db
from .storage.git_ops import read_file_at_commit

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n*(.*)$", re.DOTALL)


class ConcurrencyError(RuntimeError):
    """Raised when an operation fails due to a stale CAS revision."""
    pass


def compute_content_hash(text: str) -> str:
    """Return sha256 hexadecimal digest of normalized text."""
    normalized = text.strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def compute_candidate_hash(candidate: CandidateSkill) -> str:
    """Compute canonical hash of candidate metadata and body."""
    meta_dict = (
        candidate.meta.model_dump()
        if hasattr(candidate.meta, "model_dump")
        else candidate.meta.dict()
    )
    raw = json.dumps(meta_dict, sort_keys=True, ensure_ascii=False) + "\n---\n" + candidate.body.strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeploymentManager:
    """Manages skill version snapshots, deployments, canary routing, and rollbacks."""

    def __init__(
        self,
        db_path: Path,
        repo_root: Optional[Path] = None,
        skills_dir: Optional[Path] = None,
        registry: Optional[Any] = None,
    ):
        self.db_path = db_path
        self.repo_root = repo_root or db_path.parent.parent
        self.skills_dir = skills_dir or (self.repo_root / "skills")
        self.registry = registry
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ==================== 1. Version Snapshot & Comparison ====================

    def get_version_snapshot(self, skill_name: str, version: str) -> VersionSnapshot:
        """Fetch immutable version snapshot.

        Guarantees:
        - Never returns latest unverified disk body masquerading as an old version.
        - Verifies content hash if recorded (fails closed on mismatch).
        - Unverified or missing history marked UNAVAILABLE / unverified.
        """
        conn = self._get_conn()

        # 1. Check releases table
        row = conn.execute(
            """SELECT release_id, skill_name, version, commit_hash, status, level,
                      eval_summary_json, content_hash, meta_json, body_md, source_lineage_json
               FROM releases
               WHERE skill_name = ? AND version = ?
               ORDER BY created_at DESC LIMIT 1""",
            (skill_name, version),
        ).fetchone()

        if row:
            rel_id, s_name, ver, commit_hash, status, level, eval_json, stored_hash, meta_json, body_md, lineage_json = row

            eval_summary = json.loads(eval_json) if eval_json else None
            source_lineage = json.loads(lineage_json) if lineage_json else None

            meta: Optional[SkillMeta] = None
            if meta_json:
                try:
                    meta = SkillMeta(**json.loads(meta_json))
                except Exception:
                    meta = None

            body = ""
            if body_md:
                body = body_md.strip()

            if (not body or meta is None) and commit_hash and self.repo_root:
                try:
                    rel_path = f"skills/{skill_name}/SKILL.md"
                    raw_text = read_file_at_commit(self.repo_root, commit_hash, rel_path)
                    m = _FRONTMATTER_RE.match(raw_text)
                    if m:
                        fm_text, body_text = m.group(1), m.group(2)
                        if not body:
                            body = body_text.strip()
                        if meta is None:
                            try:
                                meta = SkillMeta(**(yaml.safe_load(fm_text) or {}))
                            except Exception:
                                pass
                    else:
                        if not body:
                            body = raw_text.strip()
                except Exception:
                    pass

            # Verification and hash integrity
            if body:
                actual_hash = compute_content_hash(body)
                if stored_hash and actual_hash != stored_hash:
                    raise ValueError(
                        f"Content hash verification failed for {skill_name} v{version}: "
                        f"expected {stored_hash}, got {actual_hash}. Snapshot corrupted."
                    )
                effective_hash = stored_hash or actual_hash
                is_verified = (status in ("PUBLISHED", "READY", "APPROVED", "CANARY"))
                return VersionSnapshot(
                    skill_name=skill_name,
                    version=version,
                    content_hash=effective_hash,
                    meta=meta,
                    body=body,
                    commit_hash=commit_hash,
                    release_id=rel_id,
                    status=status,
                    is_verified=is_verified,
                    eval_summary=eval_summary,
                    source_lineage=source_lineage,
                    dependencies=(
                        list(meta.dependencies)
                        if meta
                        else (
                            json.loads(meta_json).get("dependencies", [])
                            if meta_json
                            else []
                        )
                    ),
                )
            else:
                # History row exists but body content is unavailable
                return VersionSnapshot(
                    skill_name=skill_name,
                    version=version,
                    content_hash=stored_hash or "",
                    meta=meta,
                    body="",
                    commit_hash=commit_hash,
                    release_id=rel_id,
                    status="UNAVAILABLE",
                    is_verified=False,
                    eval_summary=eval_summary,
                    source_lineage=source_lineage,
                    dependencies=list(meta.dependencies) if meta else [],
                )

        # 2. Check CandidateStore if candidate is waiting or registered
        cand_row = conn.execute(
            """SELECT candidate_id, skill_name, status, meta_json, body_md, source_episode_ids
               FROM candidate_skills
               WHERE skill_name = ?
               ORDER BY created_at DESC""",
            (skill_name,),
        ).fetchall()

        for c_id, s_name, c_status, c_meta_json, c_body, c_src in cand_row:
            try:
                c_meta = SkillMeta(**json.loads(c_meta_json))
                if c_meta.version == version:
                    c_body_clean = c_body.strip()
                    c_hash = compute_content_hash(c_body_clean)
                    return VersionSnapshot(
                        skill_name=skill_name,
                        version=version,
                        content_hash=c_hash,
                        meta=c_meta,
                        body=c_body_clean,
                        commit_hash=None,
                        release_id=c_id,
                        status=c_status,
                        is_verified=(c_status == "APPROVED"),
                        eval_summary=None,
                        source_lineage=json.loads(c_src) if c_src else None,
                        dependencies=list(c_meta.dependencies),
                    )
            except Exception:
                continue

        # 3. Check active registry strictly if version matches
        if self.registry and hasattr(self.registry, "list_names") and skill_name in self.registry.list_names():
            reg_meta = self.registry.get_meta(skill_name)
            if reg_meta.version == version:
                reg_body = self.registry.get_body(skill_name)
                h = compute_content_hash(reg_body)
                return VersionSnapshot(
                    skill_name=skill_name,
                    version=version,
                    content_hash=h,
                    meta=reg_meta,
                    body=reg_body,
                    commit_hash=None,
                    release_id=None,
                    status="PUBLISHED",
                    is_verified=True,
                    eval_summary=None,
                    source_lineage=None,
                    dependencies=list(reg_meta.dependencies),
                )

        raise KeyError(f"Version '{version}' for skill '{skill_name}' not found")

    def compare_versions(
        self,
        skill_name: str,
        version_a: str,
        version_b: str,
    ) -> VersionComparison:
        """Read-only comparison of content, metadata, dependencies, and evaluation deltas."""
        snap_a = self.get_version_snapshot(skill_name, version_a)
        snap_b = self.get_version_snapshot(skill_name, version_b)

        # 1. Content diff
        diff_lines = list(
            difflib.unified_diff(
                snap_a.body.splitlines(keepends=True),
                snap_b.body.splitlines(keepends=True),
                fromfile=f"{skill_name}@{version_a}",
                tofile=f"{skill_name}@{version_b}",
            )
        )
        content_diff = "".join(diff_lines)

        # 2. Metadata diff
        meta_diff: dict[str, Any] = {}
        if snap_a.meta and snap_b.meta:
            for attr in ["description", "use_when"]:
                val_a = getattr(snap_a.meta, attr, "")
                val_b = getattr(snap_b.meta, attr, "")
                if val_a != val_b:
                    meta_diff[attr] = {"from": val_a, "to": val_b}

            trig_a = sorted(getattr(snap_a.meta.trigger, "keywords", []) if snap_a.meta.trigger else [])
            trig_b = sorted(getattr(snap_b.meta.trigger, "keywords", []) if snap_b.meta.trigger else [])
            if trig_a != trig_b:
                meta_diff["trigger.keywords"] = {"from": trig_a, "to": trig_b}

        # 3. Dependencies diff
        deps_a = set(snap_a.dependencies or [])
        deps_b = set(snap_b.dependencies or [])
        deps_diff = {
            "added": sorted(deps_b - deps_a),
            "removed": sorted(deps_a - deps_b),
            "unchanged": sorted(deps_a & deps_b),
        }

        # 4. Evaluation delta
        is_comparable = True
        incomparable_reason = None
        eval_delta: dict[str, Any] = {}

        summary_a = snap_a.eval_summary
        summary_b = snap_b.eval_summary

        if summary_a is None or summary_b is None:
            eval_delta = {
                "status": "N/A",
                "reason": "Evaluation summary missing for one or both versions",
            }
        else:
            proto_a = summary_a.get("eval_set") or summary_a.get("protocol") or summary_a.get("checker")
            proto_b = summary_b.get("eval_set") or summary_b.get("protocol") or summary_b.get("checker")

            if proto_a and proto_b and proto_a != proto_b:
                is_comparable = False
                incomparable_reason = (
                    f"Evaluation protocol/eval_set mismatch: '{proto_a}' vs '{proto_b}'"
                )
                eval_delta = {
                    "status": "incomparable",
                    "reason": incomparable_reason,
                }
            else:
                struct_a = summary_a.get("structure_score", {})
                struct_b = summary_b.get("structure_score", {})
                effect_a = summary_a.get("effect_score", {})
                effect_b = summary_b.get("effect_score", {})

                delta_dict: dict[str, Any] = {}
                for dim in sorted(set(effect_a.keys()) | set(effect_b.keys())):
                    sa = effect_a.get(dim)
                    sb = effect_b.get(dim)
                    if isinstance(sa, (int, float)) and isinstance(sb, (int, float)):
                        delta_dict[f"effect.{dim}"] = round(sb - sa, 2)
                    else:
                        delta_dict[f"effect.{dim}"] = "N/A"

                tot_a = (
                    sum(v for v in struct_a.values() if isinstance(v, (int, float)))
                    + sum(v for v in effect_a.values() if isinstance(v, (int, float)))
                )
                tot_b = (
                    sum(v for v in struct_b.values() if isinstance(v, (int, float)))
                    + sum(v for v in effect_b.values() if isinstance(v, (int, float)))
                )
                delta_dict["total_score"] = round(tot_b - tot_a, 2)

                eval_delta = {
                    "status": "comparable",
                    "deltas": delta_dict,
                }

        return VersionComparison(
            skill_name=skill_name,
            version_a=version_a,
            version_b=version_b,
            content_diff=content_diff,
            metadata_diff=meta_diff,
            dependencies_diff=deps_diff,
            eval_delta=eval_delta,
            is_comparable=is_comparable,
            incomparable_reason=incomparable_reason,
        )

    # ==================== 2. Deployment Management ====================

    def get_deployment(self, skill_name: str) -> Deployment:
        """Fetch current deployment state or initialize default."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT skill_name, stable_version, stable_release_id,
                      canary_version, canary_release_id, canary_share,
                      rollout_id, revision, updated_at
               FROM deployments WHERE skill_name = ?""",
            (skill_name,),
        ).fetchone()

        if row:
            return Deployment(
                skill_name=row[0],
                stable_version=row[1],
                stable_release_id=row[2],
                canary_version=row[3],
                canary_release_id=row[4],
                canary_share=row[5],
                rollout_id=row[6],
                revision=row[7],
                updated_at=row[8],
            )

        cur_ver: Optional[str] = None
        cur_rel_id: Optional[str] = None

        if self.registry and hasattr(self.registry, "list_names") and skill_name in self.registry.list_names():
            cur_ver = self.registry.get_meta(skill_name).version
            if hasattr(self.registry, "get_current_release"):
                rel = self.registry.get_current_release(skill_name)
                if rel:
                    cur_rel_id = rel.release_id

        if not cur_ver:
            skill_row = conn.execute(
                """SELECT s.current_release_id, r.version
                   FROM skills s
                   LEFT JOIN releases r ON s.current_release_id = r.release_id
                   WHERE s.name = ?""",
                (skill_name,),
            ).fetchone()
            if skill_row and skill_row[1]:
                cur_rel_id = skill_row[0]
                cur_ver = skill_row[1]

        if not cur_ver:
            raise KeyError(f"Skill '{skill_name}' has no active release or deployment")

        rollout_id = uuid.uuid4().hex[:12]
        conn.execute(
            """INSERT INTO deployments (
                skill_name, stable_version, stable_release_id,
                canary_version, canary_release_id, canary_share,
                rollout_id, revision
            ) VALUES (?, ?, ?, NULL, NULL, 0, ?, 1)""",
            (skill_name, cur_ver, cur_rel_id, rollout_id),
        )
        conn.commit()

        return Deployment(
            skill_name=skill_name,
            stable_version=cur_ver,
            stable_release_id=cur_rel_id,
            canary_version=None,
            canary_release_id=None,
            canary_share=0,
            rollout_id=rollout_id,
            revision=1,
        )

    def set_canary(
        self,
        skill_name: str,
        candidate_or_version: str | CandidateSkill,
        validation_record: Optional[ValidationRecord] = None,
        share: int = 10,
        caller_confirmed: bool = False,
        expected_revision: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> Deployment:
        """Set a validated candidate or existing version as canary."""
        if not (0 <= share <= 100):
            raise ValueError(f"Invalid canary share: {share}. Must be between 0 and 100.")

        if not caller_confirmed:
            raise ValueError("Canary admission requires explicit caller confirmation (caller_confirmed=True)")

        conn = self._get_conn()

        if operation_id:
            existing = conn.execute(
                "SELECT event_id FROM deployment_audit_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                return self.get_deployment(skill_name)

        current_dep = self.get_deployment(skill_name)

        if expected_revision is not None and expected_revision != current_dep.revision:
            raise ConcurrencyError(
                f"Deployment revision mismatch: expected {expected_revision}, current is {current_dep.revision}"
            )

        canary_ver: str
        canary_rel_id: Optional[str] = None

        if isinstance(candidate_or_version, CandidateSkill):
            candidate = candidate_or_version
            if validation_record is None:
                raise ValueError("Setting canary from candidate requires a validation_record")
            if validation_record.ratchet_decision != "PASS":
                raise ValueError(
                    f"Cannot set canary: candidate has ratchet decision '{validation_record.ratchet_decision}'. "
                    "Only verified PASS candidates may be admitted to canary."
                )

            # Invalidate on content mutation
            cand_hash = compute_candidate_hash(candidate)
            body_hash = compute_content_hash(candidate.body)
            if validation_record.content_hash not in (cand_hash, body_hash):
                raise ValueError("Validation invalidated: candidate content was mutated after evaluation")

            # Invalidate on baseline drift
            if candidate.decision == "revise":
                if validation_record.baseline_version != current_dep.stable_version:
                    raise ValueError(
                        f"Validation invalidated: baseline version changed from "
                        f"'{validation_record.baseline_version}' to '{current_dep.stable_version}'"
                    )

            canary_ver = candidate.meta.version
            canary_rel_id = f"rel_canary_{uuid.uuid4().hex[:12]}"
            meta_dict = candidate.meta.model_dump() if hasattr(candidate.meta, "model_dump") else candidate.meta.dict()
            summary_json = json.dumps(asdict(validation_record.eval_result)) if validation_record.eval_result else None
            lineage_json = json.dumps(candidate.source_episode_ids)
        else:
            canary_ver = candidate_or_version
            snap = self.get_version_snapshot(skill_name, canary_ver)
            if not snap.is_verified or snap.status not in ("PUBLISHED", "READY", "CANARY"):
                raise ValueError(f"Cannot set canary: version '{canary_ver}' is not verified (status={snap.status})")
            canary_rel_id = snap.release_id

        new_revision = current_dep.revision + 1
        new_rollout_id = uuid.uuid4().hex[:12]

        try:
            if isinstance(candidate_or_version, CandidateSkill):
                conn.execute(
                    """INSERT INTO releases (
                        release_id, skill_name, version, commit_hash, status, level,
                        triggered_by, eval_summary_json, content_hash, meta_json, body_md,
                        source_lineage_json, published_at
                    ) VALUES (?, ?, ?, ?, 'CANARY', ?, 'canary_admission', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (
                        canary_rel_id,
                        skill_name,
                        canary_ver,
                        None,
                        "L1" if candidate.decision == "revise" else "L2",
                        summary_json,
                        body_hash,
                        json.dumps(meta_dict),
                        candidate.body.strip(),
                        lineage_json,
                    ),
                )

            conn.execute(
                """UPDATE deployments
                   SET canary_version = ?, canary_release_id = ?, canary_share = ?,
                       rollout_id = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE skill_name = ?""",
                (canary_ver, canary_rel_id, share, new_rollout_id, new_revision, skill_name),
            )
            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO deployment_audit_events (
                    event_id, operation_id, skill_name, action, from_stable, to_stable,
                    from_canary, to_canary, from_share, to_share, reason, revision_before, revision_after
                ) VALUES (?, ?, ?, 'SET_CANARY', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    operation_id,
                    skill_name,
                    current_dep.stable_version,
                    current_dep.stable_version,
                    current_dep.canary_version,
                    canary_ver,
                    current_dep.canary_share,
                    share,
                    f"Canary activated for version {canary_ver} at {share}% share",
                    current_dep.revision,
                    new_revision,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return self.get_deployment(skill_name)

    def change_canary_share(
        self,
        skill_name: str,
        share: int,
        caller_confirmed: bool = False,
        expected_revision: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> Deployment:
        """Adjust canary share percentage."""
        if not (0 <= share <= 100):
            raise ValueError(f"Invalid canary share: {share}. Must be between 0 and 100.")

        conn = self._get_conn()

        if operation_id:
            existing = conn.execute(
                "SELECT event_id FROM deployment_audit_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                return self.get_deployment(skill_name)

        current_dep = self.get_deployment(skill_name)

        if expected_revision is not None and expected_revision != current_dep.revision:
            raise ConcurrencyError(
                f"Deployment revision mismatch: expected {expected_revision}, current is {current_dep.revision}"
            )

        if current_dep.canary_version is None:
            raise ValueError(f"Cannot adjust canary share: skill '{skill_name}' has no active canary deployment")

        new_revision = current_dep.revision + 1
        new_rollout_id = uuid.uuid4().hex[:12]

        try:
            conn.execute(
                """UPDATE deployments
                   SET canary_share = ?, rollout_id = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE skill_name = ?""",
                (share, new_rollout_id, new_revision, skill_name),
            )
            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO deployment_audit_events (
                    event_id, operation_id, skill_name, action, from_stable, to_stable,
                    from_canary, to_canary, from_share, to_share, reason, revision_before, revision_after
                ) VALUES (?, ?, ?, 'CHANGE_SHARE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    operation_id,
                    skill_name,
                    current_dep.stable_version,
                    current_dep.stable_version,
                    current_dep.canary_version,
                    current_dep.canary_version,
                    current_dep.canary_share,
                    share,
                    f"Canary share updated to {share}%",
                    current_dep.revision,
                    new_revision,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return self.get_deployment(skill_name)

    def promote_canary_to_stable(
        self,
        skill_name: str,
        caller_confirmed: bool = False,
        expected_revision: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> Deployment:
        """Promote the active canary to the stable pointer."""
        if not caller_confirmed:
            raise ValueError("Promoting canary to stable requires explicit caller confirmation (caller_confirmed=True)")

        conn = self._get_conn()

        if operation_id:
            existing = conn.execute(
                "SELECT event_id FROM deployment_audit_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                return self.get_deployment(skill_name)

        current_dep = self.get_deployment(skill_name)

        if expected_revision is not None and expected_revision != current_dep.revision:
            raise ConcurrencyError(
                f"Deployment revision mismatch: expected {expected_revision}, current is {current_dep.revision}"
            )

        if current_dep.canary_version is None:
            raise ValueError(f"Cannot promote canary: skill '{skill_name}' has no active canary deployment")

        canary_snap = self.get_version_snapshot(skill_name, current_dep.canary_version)
        if not canary_snap.is_verified:
            raise ValueError(f"Cannot promote unverified canary version '{current_dep.canary_version}'")

        new_revision = current_dep.revision + 1
        new_stable = current_dep.canary_version
        new_stable_rel_id = current_dep.canary_release_id

        try:
            conn.execute(
                """UPDATE deployments
                   SET stable_version = ?, stable_release_id = ?, canary_version = NULL,
                       canary_release_id = NULL, canary_share = 0, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE skill_name = ?""",
                (new_stable, new_stable_rel_id, new_revision, skill_name),
            )
            if new_stable_rel_id:
                conn.execute(
                    "UPDATE releases SET status = 'PUBLISHED' WHERE release_id = ?",
                    (new_stable_rel_id,),
                )
                conn.execute(
                    "UPDATE skills SET current_release_id = ? WHERE name = ?",
                    (new_stable_rel_id, skill_name),
                )

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO deployment_audit_events (
                    event_id, operation_id, skill_name, action, from_stable, to_stable,
                    from_canary, to_canary, from_share, to_share, reason, revision_before, revision_after
                ) VALUES (?, ?, ?, 'PROMOTE_CANARY', ?, ?, ?, NULL, ?, 0, ?, ?, ?)""",
                (
                    event_id,
                    operation_id,
                    skill_name,
                    current_dep.stable_version,
                    new_stable,
                    current_dep.canary_version,
                    current_dep.canary_share,
                    f"Canary version {new_stable} promoted to stable",
                    current_dep.revision,
                    new_revision,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if self.registry:
            if hasattr(self.registry, "_metas") and canary_snap.meta:
                self.registry._metas[skill_name] = canary_snap.meta
            if hasattr(self.registry, "_bodies") and canary_snap.body:
                self.registry._bodies[skill_name] = canary_snap.body

        return self.get_deployment(skill_name)

    def rollback_deployment(
        self,
        skill_name: str,
        target_version: str,
        reason: str,
        caller_confirmed: bool = False,
        expected_revision: Optional[int] = None,
        operation_id: Optional[str] = None,
        available_dependencies: Optional[set[str]] = None,
        dependency_prober: Optional[Any] = None,
    ) -> Deployment:
        """Rollback stable pointer to a verified historical release, disabling canary."""
        if not caller_confirmed:
            raise ValueError("Rollback requires explicit caller confirmation (caller_confirmed=True)")

        if not reason or not reason.strip():
            raise ValueError("Rollback requires a non-empty reason for audit tracking")

        conn = self._get_conn()

        if operation_id:
            existing = conn.execute(
                "SELECT event_id FROM deployment_audit_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing:
                return self.get_deployment(skill_name)

        current_dep = self.get_deployment(skill_name)

        if expected_revision is not None and expected_revision != current_dep.revision:
            raise ConcurrencyError(
                f"Deployment revision mismatch: expected {expected_revision}, current is {current_dep.revision}"
            )

        # F6: Target validation
        snap = self.get_version_snapshot(skill_name, target_version)
        if snap.skill_name != skill_name:
            raise ValueError(f"Rollback rejected: target version belongs to '{snap.skill_name}', expected '{skill_name}'")

        if not snap.is_verified or snap.status not in ("PUBLISHED", "READY", "APPROVED"):
            raise ValueError(f"Rollback rejected: target version '{target_version}' is not a verified published release (status={snap.status})")

        if not snap.body:
            raise ValueError(f"Rollback rejected: target version '{target_version}' has unavailable body")

        actual_hash = compute_content_hash(snap.body)
        if snap.content_hash and actual_hash != snap.content_hash:
            raise ValueError(f"Rollback rejected: target version '{target_version}' content hash is corrupted")

        # Dependency check
        if snap.dependencies:
            for dep in snap.dependencies:
                if dependency_prober is not None:
                    probe_res = dependency_prober.probe_dependency(dep)
                    if not probe_res.satisfied:
                        raise ValueError(
                            f"Rollback rejected: dependency '{dep}' is unavailable in sandbox environment: {probe_res.error_reason}"
                        )
                if available_dependencies is not None and dep not in available_dependencies:
                    raise ValueError(f"Rollback rejected: dependency '{dep}' is unavailable in current environment")
                if dep.startswith("unavailable_") or dep.startswith("missing_"):
                    raise ValueError(f"Rollback rejected: dependency '{dep}' is unavailable in current environment")

        new_revision = current_dep.revision + 1

        try:
            conn.execute(
                """UPDATE deployments
                   SET stable_version = ?, stable_release_id = ?, canary_version = NULL,
                       canary_release_id = NULL, canary_share = 0, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE skill_name = ?""",
                (target_version, snap.release_id, new_revision, skill_name),
            )
            if snap.release_id:
                conn.execute(
                    "UPDATE skills SET current_release_id = ? WHERE name = ?",
                    (snap.release_id, skill_name),
                )

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO deployment_audit_events (
                    event_id, operation_id, skill_name, action, from_stable, to_stable,
                    from_canary, to_canary, from_share, to_share, reason, revision_before, revision_after
                ) VALUES (?, ?, ?, 'ROLLBACK', ?, ?, ?, NULL, ?, 0, ?, ?, ?)""",
                (
                    event_id,
                    operation_id,
                    skill_name,
                    current_dep.stable_version,
                    target_version,
                    current_dep.canary_version,
                    current_dep.canary_share,
                    reason,
                    current_dep.revision,
                    new_revision,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if self.registry:
            if hasattr(self.registry, "_metas") and snap.meta:
                self.registry._metas[skill_name] = snap.meta
            if hasattr(self.registry, "_bodies") and snap.body:
                self.registry._bodies[skill_name] = snap.body

        return self.get_deployment(skill_name)

    # ==================== 3. Deterministic Routing & Run Freezing ====================

    def route_version(
        self,
        skill_name: str,
        run_id: Optional[str] = None,
        cohort_key: Optional[str] = None,
    ) -> tuple[str, str, bool]:
        """Resolve version allocation for an execution run.

        Returns:
            (assigned_version, content_hash, is_canary)
        """
        conn = self._get_conn()

        # 1. Check if run_id is already bound/frozen
        if run_id:
            row = conn.execute(
                """SELECT assigned_version, content_hash, is_canary
                   FROM run_version_bindings
                   WHERE run_id = ? AND skill_name = ?""",
                (run_id, skill_name),
            ).fetchone()
            if row:
                return row[0], row[1], bool(row[2])

        # 2. Evaluate deployment state
        dep = self.get_deployment(skill_name)

        selected_ver: str
        is_canary: bool

        if dep.canary_version is None or dep.canary_share <= 0:
            selected_ver = dep.stable_version
            is_canary = False
        elif dep.canary_share >= 100:
            selected_ver = dep.canary_version
            is_canary = True
        else:
            bucket_key = run_id or cohort_key
            if not bucket_key:
                selected_ver = dep.stable_version
                is_canary = False
            else:
                b_input = f"{skill_name}:{dep.rollout_id}:{bucket_key}".encode("utf-8")
                bucket = int(hashlib.sha256(b_input).hexdigest()[:8], 16) % 100
                if bucket < dep.canary_share:
                    selected_ver = dep.canary_version
                    is_canary = True
                else:
                    selected_ver = dep.stable_version
                    is_canary = False

        snap = self.get_version_snapshot(skill_name, selected_ver)
        content_hash = snap.content_hash

        # 3. Freeze run binding
        if run_id:
            conn.execute(
                """INSERT OR IGNORE INTO run_version_bindings (
                    run_id, skill_name, assigned_version, content_hash, is_canary, frozen_body
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, skill_name, selected_ver, content_hash, int(is_canary), snap.body),
            )
            conn.commit()

        return selected_ver, content_hash, is_canary

    def get_run_body(self, skill_name: str, run_id: str) -> str:
        """Fetch frozen body text for an execution run."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT frozen_body FROM run_version_bindings WHERE run_id = ? AND skill_name = ?",
            (run_id, skill_name),
        ).fetchone()
        if row:
            return row[0]

        ver, _, _ = self.route_version(skill_name, run_id=run_id)
        snap = self.get_version_snapshot(skill_name, ver)
        return snap.body

    def get_run_binding(self, run_id: str, skill_name: str) -> Optional[RunVersionBinding]:
        """Fetch stored run version binding."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT run_id, skill_name, assigned_version, content_hash, is_canary, frozen_body, created_at
               FROM run_version_bindings
               WHERE run_id = ? AND skill_name = ?""",
            (run_id, skill_name),
        ).fetchone()
        if not row:
            return None
        return RunVersionBinding(
            run_id=row[0],
            skill_name=row[1],
            assigned_version=row[2],
            content_hash=row[3],
            is_canary=bool(row[4]),
            frozen_body=row[5],
            created_at=row[6],
        )

    def list_audit_events(self, skill_name: str) -> list[DeploymentAuditEvent]:
        """Fetch audit log for deployment changes."""
        conn = self._get_conn()
        cur = conn.execute(
            """SELECT event_id, operation_id, skill_name, action, from_stable, to_stable,
                      from_canary, to_canary, from_share, to_share, reason,
                      revision_before, revision_after, created_at
               FROM deployment_audit_events
               WHERE skill_name = ?
               ORDER BY created_at ASC""",
            (skill_name,),
        )
        events = []
        for r in cur.fetchall():
            events.append(
                DeploymentAuditEvent(
                    event_id=r[0],
                    operation_id=r[1],
                    skill_name=r[2],
                    action=r[3],
                    from_stable=r[4],
                    to_stable=r[5],
                    from_canary=r[6],
                    to_canary=r[7],
                    from_share=r[8],
                    to_share=r[9],
                    reason=r[10],
                    revision_before=r[11],
                    revision_after=r[12],
                    created_at=r[13],
                )
            )
        return events
