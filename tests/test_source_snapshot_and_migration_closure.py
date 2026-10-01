"""Batch 1 Acceptance Test Suite: Source Citations + Runtime Snapshot Recovery + Legacy Migration Closure.

Validates acceptance criteria S, R, M:
- S1/S2: Real conversation session/message citations in CandidateSkill and generate_candidate_from_requirement.
  Default source_type="requirement" when citations are absent.
  source_type="conversation" with session_id and message_ids when citations are provided.
  CandidateStore accurately persists and reloads citations across SQLite reopen without forging fake episodes.
- R1/R2: start_run freezes and persists frozen_body, content_hash, intent_revision, task_spec_hash, candidate_id
  into SQLite runtime_runs. Fresh AgentRuntime instance reopened on the same DB restores the exact frozen body
  via get_run_body without reading from mutable registry.
- R3: Late-arrival isolation: subsequent changes to CandidateSkill in memory or registry on disk do not alter
  the restored run body snapshot.
- R4: Legacy unrecoverable diagnostic: for legacy rows where frozen_body is NULL, get_run_body fails closed with
  diagnostic KeyError explaining historical draft body was not persisted.
- M1: Dynamic schema migration via ALTER TABLE in init_db preserves existing rows and is idempotent.
- M2: Anti-tamper verification: tampering with frozen_body in SQLite triggers ValueError on hash mismatch.
- M3: get_run returns populated RunRecord with all 4 snapshot fields across DB reopen.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skillforge.episode import CandidateStore
from skillforge.models import CandidateSkill, SkillMeta
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.skill_generator import generate_candidate_from_requirement
from skillforge.storage.db import init_db


class MockLLM:
    """Predictable mock LLM returning valid skill generation payload."""

    def __init__(
        self,
        name: str = "order_parser",
        body: str = (
            "## Overview\nOrder parser body.\n\n"
            "## Instructions\n1. Parse order documents safely.\n2. Emit structured JSON.\n\n"
            "## Examples\nQ: Parse order 123\nA: {'id': 123}\n\n"
            "## Constraints\nOnly parse valid order documents."
        ),
    ):
        self.payload = {
            "name": name,
            "version": "1.0.0",
            "description": "Parser for orders",
            "use_when": "Parsing raw orders",
            "not_for": ["invoices", "tax_filing"],
            "keywords": ["order", "parse", "document"],
            "examples": ["parse order #123"],
            "body": body,
            "test_cases": [
                {"query": "order 1", "reference": "ok"},
                {"query": "order 2", "reference": "ok"},
                {"query": "order 3", "reference": "ok"},
            ],
        }

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(content=json.dumps(self.payload))


def test_s1_s2_source_conversation_and_requirement_isolation(tmp_path: Path):
    """S1/S2: Citation preservation and source_type isolation across CandidateStore and generator."""
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "source_test.db"
    store = CandidateStore(db_path)
    llm = MockLLM()

    # 1. S1: Default source_type="requirement" when no session/message citations provided
    cand_req = generate_candidate_from_requirement(
        request="Parse order documents into structured JSON",
        candidate_store=store,
        llm=llm,
        repo_root=tmp_path,
        task_id="task_req_01",
    )
    assert isinstance(cand_req, CandidateSkill)
    assert cand_req.source_type == "requirement"
    assert cand_req.source_session_id is None
    assert cand_req.source_message_ids == []
    assert "requirement" in cand_req.rationale

    # Verify CandidateStore read
    loaded_req = store.get_candidate(cand_req.candidate_id)
    assert loaded_req is not None
    assert loaded_req.source_type == "requirement"
    assert loaded_req.source_session_id is None
    assert loaded_req.source_message_ids == []

    # 2. S2: Explicit conversation citations provided -> source_type="conversation"
    cand_conv = generate_candidate_from_requirement(
        request="Parse order documents from user chat session",
        candidate_store=store,
        llm=llm,
        repo_root=tmp_path,
        task_id="task_conv_01",
        session_id="session_chat_888",
        message_ids=["msg_user_01", "msg_agent_02"],
    )
    assert isinstance(cand_conv, CandidateSkill)
    assert cand_conv.source_type == "conversation"
    assert cand_conv.source_session_id == "session_chat_888"
    assert cand_conv.source_message_ids == ["msg_user_01", "msg_agent_02"]
    assert "conversation" in cand_conv.rationale

    # Reopen CandidateStore on new instance to verify persistent SQLite serialization
    store.close()
    fresh_store = CandidateStore(db_path)
    loaded_conv = fresh_store.get_candidate(cand_conv.candidate_id)
    assert loaded_conv is not None
    assert loaded_conv.source_type == "conversation"
    assert loaded_conv.source_session_id == "session_chat_888"
    assert loaded_conv.source_message_ids == ["msg_user_01", "msg_agent_02"]

    # Also verify get_candidate_by_spec_hash and list_candidates preserve citations
    by_hash = fresh_store.get_candidate_by_spec_hash(cand_conv.task_spec_hash)
    assert by_hash is not None
    assert by_hash.candidate_id == cand_conv.candidate_id
    assert by_hash.source_session_id == "session_chat_888"

    all_cands = fresh_store.list_candidates()
    assert len(all_cands) == 2
    c_map = {c.candidate_id: c for c in all_cands}
    assert c_map[cand_req.candidate_id].source_session_id is None
    assert c_map[cand_conv.candidate_id].source_session_id == "session_chat_888"
    assert c_map[cand_conv.candidate_id].source_message_ids == ["msg_user_01", "msg_agent_02"]

    fresh_store.close()


def test_r1_r2_runtime_freeze_and_fresh_instance_recovery(tmp_path: Path):
    """R1/R2: start_run freezes draft snapshot into SQLite, fresh runtime restores exact body."""
    db_path = tmp_path / "runtime_snap.db"
    broker = ToolBroker()
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    cand_body = "## Instructions\n1. Strictly process inputs without halluncinating.\n2. Emit valid JSON."
    candidate = CandidateSkill(
        candidate_id="cand_order_001",
        skill_name="order_processor",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(name="order_processor", version="0.1.0-draft", description="Order handler", use_when="Handle orders"),
        body=cand_body,
        rationale="Draft for trial execution",
        source_requirement="Process orders safely",
        source_type="requirement",
        task_spec_hash="spec_hash_1234",
        intent_revision=2,
    )

    run_rec = runtime.start_run(
        run_id="run_frozen_001",
        task_id="task_order_01",
        purpose="learning",
        candidate=candidate,
    )
    assert run_rec.status == "RUNNING"
    assert run_rec.frozen_body == cand_body
    assert run_rec.intent_revision == 2
    assert run_rec.candidate_id == "cand_order_001"
    assert run_rec.task_spec_hash == "spec_hash_1234"

    # Verify SQLite row directly
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT frozen_body, content_hash, intent_revision, task_spec_hash, candidate_id FROM runtime_runs WHERE run_id = ?",
        ("run_frozen_001",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == cand_body
    assert row[1] == hashlib.sha256(cand_body.encode("utf-8")).hexdigest()
    assert row[2] == 2
    assert row[3] == "spec_hash_1234"
    assert row[4] == "cand_order_001"

    # Close previous runtime and construct a FRESH AgentRuntime instance on the same DB
    runtime.close()
    fresh_runtime = AgentRuntime(db_path=db_path, tool_broker=broker)
    assert "run_frozen_001" not in fresh_runtime._run_candidate_bodies

    # R2: Fresh runtime restores exact frozen body from SQLite
    restored_body = fresh_runtime.get_run_body(run_id="run_frozen_001")
    assert restored_body == cand_body
    # Verify cached in memory after first read
    assert fresh_runtime._run_candidate_bodies["run_frozen_001"] == cand_body

    # Verify get_run also returns all 4 fields
    fresh_rec = fresh_runtime.get_run("run_frozen_001")
    assert fresh_rec is not None
    assert fresh_rec.frozen_body == cand_body
    assert fresh_rec.intent_revision == 2
    assert fresh_rec.task_spec_hash == "spec_hash_1234"
    assert fresh_rec.candidate_id == "cand_order_001"

    fresh_runtime.close()


def test_r3_r4_late_arrival_isolation_and_legacy_unrecoverable_diagnostic(tmp_path: Path):
    """R3: Late-arrival isolation against memory/registry mutations.
    R4: Diagnostic KeyError on legacy un-persisted draft runs.
    """
    db_path = tmp_path / "late_arrival.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    # Formal registry has an existing skill "logistics_tracker"
    skill_dir = skills_dir / "logistics_tracker"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: logistics_tracker\nversion: 1.0.0\ndescription: Formal tracker\nuse_when: Tracking packages\n---\n## Instructions\nFormal V1 body",
        encoding="utf-8",
    )
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    runtime = AgentRuntime(db_path=db_path, registry=reg)

    # 1. R3: Start a run with a candidate draft body
    candidate_original_body = "## Instructions\nORIGINAL CANDIDATE DRAFT BODY"
    candidate = CandidateSkill(
        candidate_id="cand_logistics_v2",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=[],
        meta=SkillMeta(name="logistics_tracker", version="1.1.0-draft", description="Draft revise", use_when="Tracking packages"),
        body=candidate_original_body,
        rationale="Draft for trial",
        source_requirement="Logistics update",
        source_type="requirement",
    )

    runtime.start_run(run_id="run_isolated_01", task_id="task_iso_1", candidate=candidate)

    # Late arrival changes:
    # (a) mutate Candidate in memory
    candidate.body = "## Instructions\nMUTATED CANDIDATE MEMORY BODY"
    # (b) mutate Formal Skill on disk / registry
    (skill_dir / "SKILL.md").write_text(
        "---\nname: logistics_tracker\nversion: 2.0.0\ndescription: Formal tracker V2\nuse_when: Tracking packages\n---\n## Instructions\nMUTATED FORMAL REGISTRY BODY",
        encoding="utf-8",
    )
    mutated_reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    mutated_reg.load_skills_from_dir()

    runtime.close()

    # Reopen fresh runtime with mutated registry
    fresh_runtime = AgentRuntime(db_path=db_path, registry=mutated_reg)
    body_restored = fresh_runtime.get_run_body(run_id="run_isolated_01")
    # Must equal original frozen body snapshot, not mutated candidate or mutated registry
    assert body_restored == candidate_original_body
    assert "ORIGINAL CANDIDATE DRAFT BODY" in body_restored
    assert "MUTATED" not in body_restored

    # 2. R4: Legacy draft run without persisted frozen_body (is NULL)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO runtime_runs (
            run_id, task_id, skill_name, skill_version, content_hash,
            status, purpose, budget_max, budget_consumed, deadline_ts,
            created_at, updated_at, frozen_body, candidate_id
        ) VALUES (
            'legacy_draft_run_999', 'task_leg_01', 'custom_unregistered_draft', '0.1.0-draft',
            'hash_legacy_draft', 'RUNNING', 'learning', 10, 0, NULL,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, 'cand_legacy_unregistered'
        )"""
    )
    conn.commit()
    conn.close()

    # Fresh runtime trying to fetch legacy draft body without snapshot must fail closed
    with pytest.raises(KeyError) as exc_info:
        fresh_runtime.get_run_body(run_id="legacy_draft_run_999")

    err_str = str(exc_info.value)
    assert "historical draft run body was not persisted in legacy DB" in err_str
    assert "cannot reconstruct without verified snapshot" in err_str

    fresh_runtime.close()


def test_m1_m2_m3_legacy_schema_migration_fixture_and_tamper_detection(tmp_path: Path):
    """M1: Dynamic migration from ancient DB schema without new columns.
    M2: Anti-tamper verification detects SQLite body manipulation.
    M3: Idempotent migration loops preserve all historical rows.
    """
    db_path = tmp_path / "legacy_fixture.db"
    conn = sqlite3.connect(db_path)

    # Create ancient tables without the newly added columns
    conn.execute(
        """CREATE TABLE candidate_skills (
            candidate_id         TEXT PRIMARY KEY,
            skill_name           TEXT NOT NULL,
            decision             TEXT NOT NULL,
            source_episode_ids   TEXT NOT NULL,
            status               TEXT NOT NULL,
            meta_json            TEXT NOT NULL,
            body_md              TEXT NOT NULL,
            rationale            TEXT NOT NULL,
            source_doc_id        TEXT,
            source_doc_version   TEXT,
            source_snippet_ids   TEXT,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE runtime_runs (
            run_id               TEXT PRIMARY KEY,
            task_id              TEXT NOT NULL,
            skill_name           TEXT,
            skill_version        TEXT,
            content_hash         TEXT,
            status               TEXT NOT NULL DEFAULT 'PENDING',
            purpose              TEXT NOT NULL DEFAULT 'evaluation',
            budget_max           INTEGER NOT NULL DEFAULT 10,
            budget_consumed      INTEGER NOT NULL DEFAULT 0,
            deadline_ts          REAL,
            error_type           TEXT,
            error_message        TEXT,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            terminal_at          TIMESTAMP
        )"""
    )
    # Insert legacy records
    meta_json = json.dumps({"name": "legacy_skill", "version": "1.0.0", "description": "Legacy", "use_when": "legacy"})
    conn.execute(
        """INSERT INTO candidate_skills (
            candidate_id, skill_name, decision, source_episode_ids,
            status, meta_json, body_md, rationale
        ) VALUES ('cand_leg_1', 'legacy_skill', 'create', '[]', 'DRAFT', ?, 'Body text', 'Legacy rationale')""",
        (meta_json,),
    )
    conn.execute(
        """INSERT INTO runtime_runs (run_id, task_id, skill_name, status)
        VALUES ('run_leg_1', 'task_leg_1', 'legacy_skill', 'COMPLETED')"""
    )
    conn.commit()
    conn.close()

    # M1: Run init_db which dynamically performs ALTER TABLE ADD COLUMN
    migrated_conn = init_db(db_path)

    # Check candidate_skills columns
    cand_info = [r[1] for r in migrated_conn.execute("PRAGMA table_info(candidate_skills)").fetchall()]
    assert "source_session_id" in cand_info
    assert "source_message_ids" in cand_info
    assert "source_type" in cand_info
    assert "task_spec_hash" in cand_info

    # Check runtime_runs columns
    run_info = [r[1] for r in migrated_conn.execute("PRAGMA table_info(runtime_runs)").fetchall()]
    assert "frozen_body" in run_info
    assert "intent_revision" in run_info
    assert "task_spec_hash" in run_info
    assert "candidate_id" in run_info

    # Verify existing rows preserved
    row_c = migrated_conn.execute("SELECT candidate_id, skill_name, source_session_id FROM candidate_skills WHERE candidate_id='cand_leg_1'").fetchone()
    assert row_c == ("cand_leg_1", "legacy_skill", None)

    row_r = migrated_conn.execute("SELECT run_id, task_id, frozen_body, intent_revision FROM runtime_runs WHERE run_id='run_leg_1'").fetchone()
    assert row_r == ("run_leg_1", "task_leg_1", None, 1)

    migrated_conn.close()

    # Test migration idempotency by calling init_db again
    re_conn = init_db(db_path)
    assert re_conn is not None
    re_conn.close()

    # M2: Anti-tamper verification on frozen_body
    broker = ToolBroker()
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    cand_body = "## Instructions\nValid un-tampered body"
    candidate = CandidateSkill(
        candidate_id="cand_anti_tamper",
        skill_name="tamper_check",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(name="tamper_check", version="0.1.0-draft", description="Tamper check", use_when="Check tamper"),
        body=cand_body,
        rationale="For tamper detection",
        source_requirement="Check tamper",
        source_type="requirement",
    )
    runtime.start_run(run_id="run_tamper_test", task_id="task_tamper", candidate=candidate)
    runtime.close()

    # Directly tamper with SQLite frozen_body
    raw_conn = sqlite3.connect(db_path)
    raw_conn.execute(
        "UPDATE runtime_runs SET frozen_body = '## Instructions\\nMALICIOUS INJECTED BODY' WHERE run_id = 'run_tamper_test'"
    )
    raw_conn.commit()
    raw_conn.close()

    # Fresh runtime instance reads tampered run
    tamper_runtime = AgentRuntime(db_path=db_path, tool_broker=broker)
    with pytest.raises(ValueError) as exc_info:
        tamper_runtime.get_run_body(run_id="run_tamper_test")

    tamper_err = str(exc_info.value)
    assert "Integrity check failed for run_id='run_tamper_test'" in tamper_err
    assert "persisted frozen_body hash mismatch" in tamper_err

    tamper_runtime.close()
