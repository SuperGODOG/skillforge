"""Milestone 1 Unit Tests: Episode Data Contract, Persistence, and Candidate Isolation

Tests cover:
1. Episode creation, validation, and rejection of model self-assertion for success.
2. Episode roundtrip persistence and handling of 'unknown' / 'failure' / 'success' outcomes.
3. Explicit duplicate episode ID policy (on_conflict error vs ignore).
4. Candidate rejection on non-existent or invalid source episode references.
5. Candidate lifecycle decisions: 'create', 'revise', and 'abandon'.
6. Candidate isolation: Candidate skills NEVER enter active SkillRegistry retrieval or index.
7. Compatibility with existing active skills in SkillRegistry.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from skillforge import (
    Episode,
    CandidateSkill,
    EpisodeStore,
    CandidateStore,
    ToolCallProvenance,
    SkillMeta,
    Trigger,
    SkillRegistry,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_skillforge.db"


@pytest.fixture
def mock_provenance() -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name="test_tool",
        fixture_case_id="case_001",
        call_index=0,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"key": "val"},
        output_status="SUCCESS",
        output_summary="Tool execution passed",
        latency_ms=10.0,
        timestamp="2026-09-25T11:00:00Z",
        signature="sha256:abc123",
        snapshot_id="snap_001",
        snapshot_content="content snapshot",
    )


# ==================== 1. Episode Contract & Anti-Self-Assertion ====================

def test_episode_rejects_success_without_verification(mock_provenance: ToolCallProvenance):
    """Cannot set outcome to 'success' without independent verification evidence."""
    with pytest.raises(ValueError, match="Cannot mark Episode as 'success' without independent verification evidence"):
        Episode(
            episode_id="ep_001",
            task_id="task_001",
            run_id="run_001",
            skill_name="test_skill",
            skill_version="1.0.0",
            environment={"os": "darwin"},
            provenances=[mock_provenance],
            acceptance_criteria={"status": 200},
            outcome="success",
            verification_evidence=None,  # Missing verification
        )


def test_episode_rejects_model_self_assertion_only(mock_provenance: ToolCallProvenance):
    """Model claiming success by self-assertion must be rejected."""
    with pytest.raises(ValueError, match="Cannot mark Episode as 'success' solely on model self-assertion"):
        Episode(
            episode_id="ep_002",
            task_id="task_001",
            run_id="run_001",
            skill_name="test_skill",
            skill_version="1.0.0",
            environment={"os": "darwin"},
            provenances=[mock_provenance],
            acceptance_criteria={"status": 200},
            outcome="success",
            verification_evidence={
                "source": "model_self_assertion",
                "self_asserted": True,
                "independent_pass": False,
            },
        )


def test_episode_unknown_outcome_allowed(mock_provenance: ToolCallProvenance):
    """Episode with 'unknown' outcome is valid even with no verification evidence."""
    ep = Episode(
        episode_id="ep_unknown",
        task_id="task_002",
        run_id="run_002",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"os": "darwin"},
        provenances=[mock_provenance],
        acceptance_criteria={"status": 200},
        outcome="unknown",
        outcome_reason="Timeout before independent assertion ran",
    )
    assert ep.outcome == "unknown"


def test_episode_failure_outcome_allowed(mock_provenance: ToolCallProvenance):
    """Episode with 'failure' outcome is valid."""
    ep = Episode(
        episode_id="ep_fail",
        task_id="task_003",
        run_id="run_003",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"os": "darwin"},
        provenances=[mock_provenance],
        acceptance_criteria={"status": 200},
        outcome="failure",
        outcome_reason="Exit code 1 returned from tool",
    )
    assert ep.outcome == "failure"


# ==================== 2. Persistence & Duplicate ID Policy ====================

def test_episode_roundtrip_persistence(temp_db: Path, mock_provenance: ToolCallProvenance):
    """Episode can be saved and accurately retrieved from EpisodeStore."""
    store = EpisodeStore(temp_db)
    ep = Episode(
        episode_id="ep_roundtrip",
        task_id="task_roundtrip",
        run_id="run_roundtrip",
        skill_name="my_skill",
        skill_version="1.2.0",
        environment={"python": "3.13", "arch": "arm64"},
        provenances=[mock_provenance],
        acceptance_criteria={"assertion": "len(res) > 0"},
        outcome="success",
        verification_evidence={"independent_pass": True, "checker": "pytest_fixture"},
        outcome_reason="Passed 3 assertions in sandbox",
    )

    store.save_episode(ep)
    loaded = store.get_episode("ep_roundtrip")

    assert loaded is not None
    assert loaded.episode_id == "ep_roundtrip"
    assert loaded.skill_name == "my_skill"
    assert loaded.skill_version == "1.2.0"
    assert loaded.outcome == "success"
    assert len(loaded.provenances) == 1
    assert loaded.provenances[0].tool_name == "test_tool"
    assert loaded.provenances[0].snapshot_content == "content snapshot"
    assert loaded.verification_evidence == {"independent_pass": True, "checker": "pytest_fixture"}
    store.close()


def test_episode_duplicate_id_policy(temp_db: Path, mock_provenance: ToolCallProvenance):
    """Duplicate episode_id raises ValueError under default policy ('error'), or passes with 'ignore'."""
    store = EpisodeStore(temp_db)
    ep = Episode(
        episode_id="ep_dup",
        task_id="task_dup",
        run_id="run_dup",
        skill_name="skill_dup",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="unknown",
    )
    store.save_episode(ep)

    # Attempting to re-save with same ID must raise ValueError under on_conflict="error"
    with pytest.raises(ValueError, match="already exists"):
        store.save_episode(ep, on_conflict="error")

    # on_conflict="ignore" succeeds idempotently
    saved_id = store.save_episode(ep, on_conflict="ignore")
    assert saved_id == "ep_dup"
    store.close()


# ==================== 3. Candidate Store & Source Episode Validation ====================

def test_candidate_rejects_missing_source_episode(temp_db: Path):
    """CandidateStore rejects candidates referencing non-existent source episodes."""
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db, episode_store=ep_store)

    candidate = CandidateSkill(
        candidate_id="cand_bad_source",
        skill_name="target_skill",
        decision="create",
        source_episode_ids=["non_existent_ep_id"],
        meta=SkillMeta(
            name="target_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["t"]),
        ),
        body="## Overview\nCandidate body",
        rationale="Created from hallucinated episode",
    )

    with pytest.raises(KeyError, match="does not exist in EpisodeStore"):
        cand_store.save_candidate(candidate)

    ep_store.close()
    cand_store.close()


def test_candidate_lifecycle_and_decisions(temp_db: Path, mock_provenance: ToolCallProvenance):
    """Candidate can be created, revised, and abandoned with valid source episode links."""
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db, episode_store=ep_store)

    # Save a valid source episode first
    ep = Episode(
        episode_id="ep_source_valid",
        task_id="task_001",
        run_id="run_001",
        skill_name="target_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="unknown",
    )
    ep_store.save_episode(ep)

    # 1. Create decision
    candidate = CandidateSkill(
        candidate_id="cand_valid",
        skill_name="target_skill",
        decision="create",
        source_episode_ids=["ep_source_valid"],
        meta=SkillMeta(
            name="target_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["target"]),
        ),
        body="## Overview\nCandidate body v1",
        rationale="Initial creation from episode",
    )
    cand_store.save_candidate(candidate)

    loaded = cand_store.get_candidate("cand_valid")
    assert loaded is not None
    assert loaded.decision == "create"
    assert loaded.status == "DRAFT"

    # 2. Revise decision
    cand_store.update_decision("cand_valid", decision="revise", rationale="Refined instructions")
    revised = cand_store.get_candidate("cand_valid")
    assert revised is not None
    assert revised.decision == "revise"
    assert revised.rationale == "Refined instructions"

    # 3. Abandon decision
    cand_store.update_decision("cand_valid", decision="abandon", rationale="Failed sanity check")
    abandoned = cand_store.get_candidate("cand_valid")
    assert abandoned is not None
    assert abandoned.decision == "abandon"
    assert abandoned.status == "ABANDONED"

    ep_store.close()
    cand_store.close()


# ==================== 4. Candidate Isolation from Active Registry ====================

def test_candidates_strictly_isolated_from_active_registry(
    tmp_path: Path,
    temp_db: Path,
    mock_provenance: ToolCallProvenance,
):
    """Candidate skills MUST NOT enter normal search/retrieval in SkillRegistry."""
    # 1. Set up active skill on disk
    skills_dir = tmp_path / "skills"
    active_skill_dir = skills_dir / "active_math"
    active_skill_dir.mkdir(parents=True)
    active_skill_md = active_skill_dir / "SKILL.md"
    active_skill_md.write_text(
        """---
name: active_math
version: 1.0.0
description: Active math skill
use_when: Doing active math
not_for: []
dependencies: []
trigger:
  keywords: [math, calc]
examples: []
---

## Overview
Active math instructions.
""",
        encoding="utf-8",
    )

    # 2. Set up SkillRegistry
    registry = SkillRegistry(
        db_path=temp_db,
        skills_dir=skills_dir,
        repo_root=tmp_path,
    )
    registry.load_skills_from_dir()

    # Active skill is properly loaded
    assert "active_math" in registry.list_names()
    assert "active_math" in registry.build_index()

    # 3. Add CandidateSkill into CandidateStore (sharing same database)
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db, episode_store=ep_store)

    ep = Episode(
        episode_id="ep_iso_source",
        task_id="t1",
        run_id="r1",
        skill_name="candidate_secret",
        skill_version="0.1.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="unknown",
    )
    ep_store.save_episode(ep)

    candidate = CandidateSkill(
        candidate_id="cand_secret",
        skill_name="candidate_secret",
        decision="create",
        source_episode_ids=["ep_iso_source"],
        meta=SkillMeta(
            name="candidate_secret",
            version="0.1.0",
            description="Secret unreleased candidate",
            use_when="Should never be seen",
            trigger=Trigger(keywords=["secret"]),
        ),
        body="## Overview\nCandidate secret body",
        rationale="Isolated candidate",
    )
    cand_store.save_candidate(candidate)

    # 4. CRITICAL VERIFICATION: Candidate is NOT in registry
    assert "candidate_secret" not in registry.list_names()
    index_text = registry.build_index()
    assert "candidate_secret" not in index_text
    assert "Secret unreleased candidate" not in index_text

    # 5. use_skill for candidate returns not registered error
    res = registry.use_skill("candidate_secret", reason="attempt to bypass isolation")
    assert "[ERROR]" in res
    assert "未注册" in res

    # 6. Active skill remains fully functional
    active_body = registry.use_skill("active_math", reason="audit active math")
    assert "Active math instructions." in active_body

    registry.close()
    ep_store.close()
    cand_store.close()


def test_db_close_and_reopen_persistence(temp_db: Path, mock_provenance: ToolCallProvenance):
    """Data persists across store instances and database connection close/reopen."""
    store1 = EpisodeStore(temp_db)
    cand_store1 = CandidateStore(temp_db, episode_store=store1)

    ep = Episode(
        episode_id="ep_persist",
        task_id="task_p",
        run_id="run_p",
        skill_name="persist_skill",
        skill_version="1.0.0",
        environment={"test": True},
        provenances=[mock_provenance],
        acceptance_criteria={"test": "pass"},
        outcome="unknown",
    )
    store1.save_episode(ep)

    cand = CandidateSkill(
        candidate_id="cand_persist",
        skill_name="persist_skill",
        decision="create",
        source_episode_ids=["ep_persist"],
        meta=SkillMeta(
            name="persist_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["p"]),
        ),
        body="## Instructions\nPersisted body",
        rationale="Testing persistence across close/reopen",
    )
    cand_store1.save_candidate(cand)

    cand_store1.close()
    store1.close()

    # Reopen fresh store instances on the same db file
    store2 = EpisodeStore(temp_db)
    cand_store2 = CandidateStore(temp_db, episode_store=store2)

    ep_loaded = store2.get_episode("ep_persist")
    assert ep_loaded is not None
    assert ep_loaded.episode_id == "ep_persist"
    assert ep_loaded.skill_name == "persist_skill"
    assert ep_loaded.outcome == "unknown"

    cand_loaded = cand_store2.get_candidate("cand_persist")
    assert cand_loaded is not None
    assert cand_loaded.candidate_id == "cand_persist"
    assert cand_loaded.body == "## Instructions\nPersisted body"
    assert cand_loaded.source_episode_ids == ["ep_persist"]

    store2.close()
    cand_store2.close()


def test_unknown_outcome_does_not_become_success(temp_db: Path, mock_provenance: ToolCallProvenance):
    """An Episode marked as 'unknown' stays 'unknown' and never automatically becomes 'success'."""
    store = EpisodeStore(temp_db)
    ep = Episode(
        episode_id="ep_unknown_stays",
        task_id="task_u",
        run_id="run_u",
        skill_name="u_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={"metric": "latency < 50ms"},
        outcome="unknown",
        outcome_reason="Latency could not be determined",
    )
    store.save_episode(ep)

    fetched = store.get_episode("ep_unknown_stays")
    assert fetched is not None
    assert fetched.outcome == "unknown"
    assert fetched.outcome != "success"

    success_episodes = store.list_episodes(outcome="success")
    assert not any(e.episode_id == "ep_unknown_stays" for e in success_episodes)

    unknown_episodes = store.list_episodes(outcome="unknown")
    assert any(e.episode_id == "ep_unknown_stays" for e in unknown_episodes)
    store.close()


def test_candidate_rejects_empty_source_episodes():
    """CandidateSkill rejects empty source_episode_ids."""
    with pytest.raises(ValueError, match="at least one valid source"):
        CandidateSkill(
            candidate_id="cand_empty_source",
            skill_name="test_skill",
            decision="create",
            source_episode_ids=[],
            meta=SkillMeta(
                name="test_skill",
                version="1.0.0",
                description="desc",
                use_when="when",
                trigger=Trigger(keywords=["t"]),
            ),
            body="body",
            rationale="rationale",
        )


def test_duplicate_candidate_id_cannot_tamper_with_invalid_sources(
    temp_db: Path,
    mock_provenance: ToolCallProvenance,
):
    """Re-saving an existing candidate ID cannot inject non-existent/invalid source episode IDs."""
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db, episode_store=ep_store)

    ep = Episode(
        episode_id="ep_valid_source_1",
        task_id="t1",
        run_id="r1",
        skill_name="tamper_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="unknown",
    )
    ep_store.save_episode(ep)

    cand = CandidateSkill(
        candidate_id="cand_tamper_test",
        skill_name="tamper_skill",
        decision="create",
        source_episode_ids=["ep_valid_source_1"],
        meta=SkillMeta(
            name="tamper_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["t"]),
        ),
        body="body",
        rationale="initial",
    )
    cand_store.save_candidate(cand)

    cand.source_episode_ids = ["ep_valid_source_1", "phantom_episode_999"]
    with pytest.raises(KeyError, match="does not exist in EpisodeStore"):
        cand_store.save_candidate(cand)

    loaded = cand_store.get_candidate("cand_tamper_test")
    assert loaded is not None
    assert loaded.source_episode_ids == ["ep_valid_source_1"]

    ep_store.close()
    cand_store.close()


def test_candidate_duplicate_id_conflict_rejection_and_isolation(
    temp_db: Path,
    mock_provenance: ToolCallProvenance,
):
    """Supervisor Acceptance Scenario:
    1. In temporary DB, save two valid Episodes: E1 and E2.
    2. Save candidate C1(source_episode_ids=[E1], body='original').
    3. Re-save with same candidate_id=C1, but valid source_episode_ids=[E2] and body='replacement'.
    4. Expected: Explicitly rejects conflict (ValueError).
    5. Close and reopen DB: C1 retains [E1] and 'original', E1 and E2 both exist unaffected.
    6. Normal revision using a new candidate_id (e.g. C2) succeeds.
    """
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db, episode_store=ep_store)

    # 1. Save two valid episodes E1 and E2
    e1 = Episode(
        episode_id="E1",
        task_id="task_e1",
        run_id="run_e1",
        skill_name="target_skill",
        skill_version="1.0.0",
        environment={"env": "prod_trace_1"},
        provenances=[mock_provenance],
        acceptance_criteria={"status": "ok"},
        outcome="unknown",
    )
    e2 = Episode(
        episode_id="E2",
        task_id="task_e2",
        run_id="run_e2",
        skill_name="target_skill",
        skill_version="1.0.0",
        environment={"env": "prod_trace_2"},
        provenances=[mock_provenance],
        acceptance_criteria={"status": "ok"},
        outcome="unknown",
    )
    ep_store.save_episode(e1)
    ep_store.save_episode(e2)

    # 2. Save candidate C1 with source [E1] and body 'original'
    c1 = CandidateSkill(
        candidate_id="C1",
        skill_name="target_skill",
        decision="create",
        source_episode_ids=["E1"],
        meta=SkillMeta(
            name="target_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["target"]),
        ),
        body="original",
        rationale="initial candidate from E1",
    )
    cand_store.save_candidate(c1)

    # 3. Attempt to save candidate with same candidate_id C1, but source [E2] and body 'replacement'
    c1_tamper = CandidateSkill(
        candidate_id="C1",
        skill_name="target_skill",
        decision="revise",
        source_episode_ids=["E2"],  # E2 is a valid existing episode!
        meta=SkillMeta(
            name="target_skill",
            version="1.0.1",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["target"]),
        ),
        body="replacement",
        rationale="tampered replacement candidate",
    )

    # 4. Expected: Explicitly reject conflict
    with pytest.raises(ValueError, match="already exists"):
        cand_store.save_candidate(c1_tamper)

    # 5. Close and reopen DB
    cand_store.close()
    ep_store.close()

    fresh_ep_store = EpisodeStore(temp_db)
    fresh_cand_store = CandidateStore(temp_db, episode_store=fresh_ep_store)

    # Verify C1 still retains [E1] and 'original'
    c1_loaded = fresh_cand_store.get_candidate("C1")
    assert c1_loaded is not None
    assert c1_loaded.candidate_id == "C1"
    assert c1_loaded.source_episode_ids == ["E1"]
    assert c1_loaded.body == "original"
    assert c1_loaded.rationale == "initial candidate from E1"

    # Verify both E1 and E2 remain unaffected
    assert fresh_ep_store.has_episode("E1")
    assert fresh_ep_store.has_episode("E2")

    # 6. Normal new revision using new candidate_id (e.g. C2) is supported
    c2 = CandidateSkill(
        candidate_id="C2",
        skill_name="target_skill",
        decision="revise",
        source_episode_ids=["E2"],
        meta=SkillMeta(
            name="target_skill",
            version="1.0.1",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["target"]),
        ),
        body="replacement",
        rationale="legitimate revision from E2 with new id",
    )
    fresh_cand_store.save_candidate(c2)
    c2_loaded = fresh_cand_store.get_candidate("C2")
    assert c2_loaded is not None
    assert c2_loaded.candidate_id == "C2"
    assert c2_loaded.source_episode_ids == ["E2"]
    assert c2_loaded.body == "replacement"

    fresh_cand_store.close()
    fresh_ep_store.close()


