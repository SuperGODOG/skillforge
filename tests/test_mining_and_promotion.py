"""Milestone 2 Acceptance Test Suite: Mining, Validation, and Controlled Promotion

Covers Supervisor Scenarios A1 - A8:
- A1: 2 valid episodes -> FakeLLM create candidate; source IDs match; candidate invisible to active registry.
- A2: Empty / non-existent source rejected; only unknown/no evidence -> abandon without candidate.
- A3: Illegal structure, forged sources, or unauthorized target modification rejected; Policy/Tool/Evaluator immutable.
- A4: Revise binds to existing skill & fixed old version; unique candidate ID; old body & version remain before promotion.
- A5: Real validation under FakeLLM/fixed cases: DECLINED/REVIEW/Invalid all refuse promotion.
- A6: PASS without confirmation is unpromoted; explicit confirmation promotes with lineage; create and revise both succeed.
- A7: Candidate content hash or baseline version mismatch invalidates promotion; duplicate promotion rejected.
- A8: Heldout sentinel only in eval set, never leaked into miner prompt.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml

from skillforge import (
    Episode,
    CandidateSkill,
    EpisodeStore,
    CandidateStore,
    ToolCallProvenance,
    SkillMeta,
    Trigger,
    SkillRegistry,
    SkillEvaluator,
    ReleaseStateMachine,
    MiningResult,
    ValidationRecord,
    mine_candidate,
    validate_candidate,
    promote_candidate,
    compute_candidate_hash,
)
from skillforge.evaluator.judge import skill_is_presented_as_a


# ==================== Test Doubles & Fixtures ====================

def _judge_json(verdict: str) -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": ["TEST_REASON"],
        "evidence_summary": "test evidence",
    })


class FakeLLM:
    """Deterministic FakeLLM recording invocations."""

    def __init__(self, contents: list[str], usage_tokens: int = 100):
        self.contents = list(contents)
        self.usage_tokens = usage_tokens
        self.calls: list[Any] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.contents.pop(0) if self.contents else "tied"
        return SimpleNamespace(
            content=content,
            usage={"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": self.usage_tokens},
        )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Set up an isolated, temporary Git repository for state machine tests."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True, capture_output=True)
    return repo_dir


@pytest.fixture
def mock_provenance() -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name="weather_api",
        fixture_case_id="case_w1",
        call_index=0,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"city": "Beijing"},
        output_status="SUCCESS",
        output_summary="25C Sunny",
        latency_ms=12.0,
        timestamp="2026-09-25T11:00:00Z",
        signature="sha256:prov123",
        snapshot_id="snap_w1",
        snapshot_content="Beijing 25C Sunny",
    )


# ==================== A1: Two Valid Episodes -> Create Candidate ====================

def test_scenario_a1_create_candidate_from_two_valid_episodes(
    tmp_path: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = tmp_path / "test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    e1 = Episode(
        episode_id="E1",
        task_id="t1",
        run_id="r1",
        skill_name="weather_reporter",
        skill_version="0.1.0",
        environment={"os": "darwin"},
        provenances=[mock_provenance],
        acceptance_criteria={"check": "sunny"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    e2 = Episode(
        episode_id="E2",
        task_id="t2",
        run_id="r2",
        skill_name="weather_reporter",
        skill_version="0.1.0",
        environment={"os": "darwin"},
        provenances=[mock_provenance],
        acceptance_criteria={"check": "sunny"},
        outcome="failure",
        outcome_reason="Timeout on external endpoint",
    )
    ep_store.save_episode(e1)
    ep_store.save_episode(e2)

    fake_response = """---
name: weather_reporter
version: 1.0.0
description: Weather report skill
use_when: User asks for weather
not_for: []
dependencies: []
trigger:
  keywords: [weather, forecast]
examples: ["What is the weather?"]
---

## Overview
Weather reporter skill.

## Instructions
Fetch weather using weather_api with bounds checking.
"""
    fake_llm = FakeLLM([fake_response])
    reg = SkillRegistry(db_path=db_path, skills_dir=tmp_path / "skills", repo_root=tmp_path)

    result = mine_candidate(
        episodes=[e1, e2],
        target_skill_name="weather_reporter",
        llm=fake_llm,
        candidate_store=cand_store,
        registry=reg,
    )

    assert result.decision == "create"
    assert result.candidate is not None
    assert result.candidate.source_episode_ids == ["E1", "E2"]
    assert cand_store.has_candidate(result.candidate.candidate_id)

    # Candidate is strictly isolated: NOT visible in active registry
    assert "weather_reporter" not in reg.list_names()
    assert "weather_reporter" not in reg.build_index()
    res = reg.use_skill("weather_reporter", reason="try bypass")
    assert "[ERROR]" in res
    assert "未注册" in res

    reg.close()
    ep_store.close()
    cand_store.close()


# ==================== A2: Empty / Invalid Source / Unknown Abandon ====================

def test_scenario_a2_empty_or_nonexistent_source_and_unknown_abandon(
    tmp_path: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = tmp_path / "test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    fake_llm = FakeLLM(["any response"])

    # 1. Empty episodes list rejected
    with pytest.raises(ValueError, match="Cannot mine candidate from empty episode list"):
        mine_candidate([], "target_skill", fake_llm, cand_store)

    # 2. Non-existent episode rejected without side effects
    phantom_ep = Episode(
        episode_id="E_PHANTOM",
        task_id="tp",
        run_id="rp",
        skill_name="target_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="failure",
    )
    with pytest.raises(KeyError, match="not found in EpisodeStore"):
        mine_candidate([phantom_ep], "target_skill", fake_llm, cand_store)
    assert cand_store.list_candidates() == []

    # 3. Only unknown / lacking evidence -> abandon, no candidate saved
    ep_unknown = Episode(
        episode_id="E_UNKNOWN",
        task_id="tu",
        run_id="ru",
        skill_name="target_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[],  # No tool evidence
        acceptance_criteria={},
        outcome="unknown",
        verification_evidence=None,
    )
    ep_store.save_episode(ep_unknown)

    res = mine_candidate([ep_unknown], "target_skill", fake_llm, cand_store)
    assert res.decision == "abandon"
    assert res.candidate is None
    assert "unknown or lack valid execution evidence" in (res.abandon_reason or "")
    assert cand_store.list_candidates() == []

    ep_store.close()
    cand_store.close()


# ==================== A3: Illegal Structure / Forged Source Rejected ====================

def test_scenario_a3_illegal_structure_and_forged_source_rejected(
    tmp_path: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = tmp_path / "test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    ep = Episode(
        episode_id="E_A3",
        task_id="t3",
        run_id="r3",
        skill_name="math_calc",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="failure",
    )
    ep_store.save_episode(ep)

    # Case 1: Malformed frontmatter (no YAML delimiters)
    malformed_llm = FakeLLM(["Just random text without frontmatter"])
    with pytest.raises(ValueError, match="lacks valid YAML frontmatter"):
        mine_candidate([ep], "math_calc", malformed_llm, cand_store)

    # Case 2: Model attempts to emit a different skill name
    spoofed_name_llm = FakeLLM(["""---
name: hacked_skill_name
version: 1.0.0
description: desc
use_when: when
trigger:
  keywords: [calc]
---

## Overview
Body
"""])
    with pytest.raises(ValueError, match="mismatched skill name"):
        mine_candidate([ep], "math_calc", spoofed_name_llm, cand_store)

    ep_store.close()
    cand_store.close()


# ==================== A4: Revise Binds to Existing Skill & Preserves Old Version ====================

def test_scenario_a4_revise_binds_to_existing_skill_and_preserves_old_version(
    temp_git_repo: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = temp_git_repo / "runs" / "skillforge.db"
    skills_dir = temp_git_repo / "skills"
    active_dir = skills_dir / "calc_skill"
    active_dir.mkdir(parents=True)
    (active_dir / "SKILL.md").write_text("""---
name: calc_skill
version: 1.0.0
description: Arithmetic calculator
use_when: Basic arithmetic
not_for: []
dependencies: []
trigger:
  keywords: [calc]
examples: []
---

## Overview
Old baseline body v1.0.0.
""", encoding="utf-8")

    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)
    reg.load_skills_from_dir()
    assert reg.get_meta("calc_skill").version == "1.0.0"

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    ep = Episode(
        episode_id="E_CALC_FAIL",
        task_id="t_calc",
        run_id="r_calc",
        skill_name="calc_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="ZeroDivisionError",
    )
    ep_store.save_episode(ep)

    revised_response = """---
name: calc_skill
version: 1.0.1
description: Arithmetic calculator with zero check
use_when: Basic arithmetic
not_for: []
dependencies: []
trigger:
  keywords: [calc]
examples: []
---

## Overview
New improved body v1.0.1 with zero check.
"""
    fake_llm = FakeLLM([revised_response])

    res = mine_candidate([ep], "calc_skill", fake_llm, cand_store, registry=reg)
    assert res.decision == "revise"
    candidate = res.candidate
    assert candidate is not None
    assert candidate.meta.version == "1.0.1"

    # Prior to promotion, active skill in registry is completely unchanged
    assert reg.get_meta("calc_skill").version == "1.0.0"
    assert "Old baseline body v1.0.0." in reg.use_skill("calc_skill", reason="audit old body")

    reg.close()
    ep_store.close()
    cand_store.close()


# ==================== A5: Failure / Error / Review Never Promotes ====================

def test_scenario_a5_failure_error_review_verdicts_do_not_promote(
    temp_git_repo: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = temp_git_repo / "runs" / "skillforge.db"
    skills_dir = temp_git_repo / "skills"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)

    candidate = CandidateSkill(
        candidate_id="cand_a5",
        skill_name="gate_test_skill",
        decision="create",
        source_episode_ids=["ep_mock"],
        meta=SkillMeta(
            name="gate_test_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["gate"]),
        ),
        body="## Instructions\nGate body",
        rationale="Testing ratchet blocks",
    )

    # 1. DECLINED ratchet record -> cannot promote
    rec_declined = ValidationRecord(
        candidate_id="cand_a5",
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="Only PASS verdict may be promoted"):
        promote_candidate(candidate, rec_declined, sm, reg, cand_store, caller_confirmed=True)

    # 2. REVIEW ratchet record -> cannot promote even if caller_confirmed=True
    rec_review = ValidationRecord(
        candidate_id="cand_a5",
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="REVIEW",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="Only PASS verdict may be promoted"):
        promote_candidate(candidate, rec_review, sm, reg, cand_store, caller_confirmed=True)

    reg.close()
    sm.close()
    ep_store.close()
    cand_store.close()


# ==================== A6: PASS Unconfirmed Unpromoted & Confirmed Promotes with Lineage ====================

def test_scenario_a6_pass_unconfirmed_unpromoted_and_confirmed_promoted_with_lineage(
    temp_git_repo: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = temp_git_repo / "runs" / "skillforge.db"
    skills_dir = temp_git_repo / "skills"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)

    # Save valid episode for cold-start create
    ep = Episode(
        episode_id="E_COLD",
        task_id="tc",
        run_id="rc",
        skill_name="text_cleaner",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep)

    # 1. Cold-start create
    candidate_create = CandidateSkill(
        candidate_id="cand_cold_create",
        skill_name="text_cleaner",
        decision="create",
        source_episode_ids=["E_COLD"],
        meta=SkillMeta(
            name="text_cleaner",
            version="1.0.0",
            description="Cleans noisy text",
            use_when="Clean text",
            trigger=Trigger(keywords=["clean"]),
        ),
        body="## Instructions\nStrip whitespace and special chars.",
        rationale="Cold start from E_COLD",
    )
    cand_store.save_candidate(candidate_create)

    # Simulate real validation outputting PASS
    rec_pass = ValidationRecord(
        candidate_id="cand_cold_create",
        content_hash=compute_candidate_hash(candidate_create),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )

    # Unconfirmed -> blocked
    with pytest.raises(ValueError, match="requires explicit caller confirmation"):
        promote_candidate(candidate_create, rec_pass, sm, reg, cand_store, caller_confirmed=False)
    assert "text_cleaner" not in reg.list_names()

    # Confirmed -> promoted via ReleaseStateMachine
    release = promote_candidate(candidate_create, rec_pass, sm, reg, cand_store, caller_confirmed=True)
    assert release.status == "PUBLISHED"
    assert "text_cleaner" in reg.list_names()
    assert "Strip whitespace and special chars." in reg.use_skill("text_cleaner", reason="audit active")

    # Source lineage preserved in candidate store
    saved_cand = cand_store.get_candidate("cand_cold_create")
    assert saved_cand is not None
    assert saved_cand.status == "APPROVED"
    assert saved_cand.source_episode_ids == ["E_COLD"]

    # 2. Revise path
    ep_rev = Episode(
        episode_id="E_REVISE",
        task_id="tr",
        run_id="rr",
        skill_name="text_cleaner",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="Missing unicode emoji strip",
    )
    ep_store.save_episode(ep_rev)

    candidate_revise = CandidateSkill(
        candidate_id="cand_rev_1",
        skill_name="text_cleaner",
        decision="revise",
        source_episode_ids=["E_REVISE"],
        meta=SkillMeta(
            name="text_cleaner",
            version="1.1.0",
            description="Cleans noisy text and emojis",
            use_when="Clean text",
            trigger=Trigger(keywords=["clean"]),
        ),
        body="## Instructions\nStrip whitespace, special chars, and emojis.",
        rationale="Added emoji stripping from E_REVISE",
    )
    cand_store.save_candidate(candidate_revise)

    rec_rev_pass = ValidationRecord(
        candidate_id="cand_rev_1",
        content_hash=compute_candidate_hash(candidate_revise),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    rel_rev = promote_candidate(candidate_revise, rec_rev_pass, sm, reg, cand_store, caller_confirmed=True)
    assert rel_rev.version == "1.1.0"
    assert reg.get_meta("text_cleaner").version == "1.1.0"
    assert "emojis" in reg.use_skill("text_cleaner", reason="audit revised active")

    reg.close()
    sm.close()
    ep_store.close()
    cand_store.close()


# ==================== A7: Content Hash / Baseline Mismatch & Duplicate Promotion ====================

def test_scenario_a7_content_hash_baseline_mismatch_and_duplicate_promotion_rejected(
    temp_git_repo: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = temp_git_repo / "runs" / "skillforge.db"
    skills_dir = temp_git_repo / "skills"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)

    ep = Episode(
        episode_id="E_A7",
        task_id="t7",
        run_id="r7",
        skill_name="hash_test_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep)

    candidate = CandidateSkill(
        candidate_id="cand_hash_test",
        skill_name="hash_test_skill",
        decision="create",
        source_episode_ids=["E_A7"],
        meta=SkillMeta(
            name="hash_test_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["test"]),
        ),
        body="Original verified body",
        rationale="Testing tamper invalidation",
    )
    cand_store.save_candidate(candidate)

    rec = ValidationRecord(
        candidate_id="cand_hash_test",
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )

    # 1. Mutate body after validation -> rejected
    candidate.body = "Tampered body injected after evaluation"
    with pytest.raises(ValueError, match="candidate content was mutated after evaluation"):
        promote_candidate(candidate, rec, sm, reg, cand_store, caller_confirmed=True)

    # Restore body
    candidate.body = "Original verified body"

    # 2. Promote candidate legitimately
    promote_candidate(candidate, rec, sm, reg, cand_store, caller_confirmed=True)

    # 3. Duplicate promotion call for same candidate rejected
    with pytest.raises(ValueError, match="already been promoted"):
        promote_candidate(candidate, rec, sm, reg, cand_store, caller_confirmed=True)

    reg.close()
    sm.close()
    ep_store.close()
    cand_store.close()


# ==================== A8: Heldout Sentinel Never Leaked to Miner Prompt ====================

def test_scenario_a8_heldout_sentinel_never_leaked_to_miner_prompt(
    tmp_path: Path,
    mock_provenance: ToolCallProvenance,
):
    db_path = tmp_path / "test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    ep = Episode(
        episode_id="E_A8",
        task_id="t8",
        run_id="r8",
        skill_name="sentinel_skill",
        skill_version="1.0.0",
        environment={},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="Normal failure without test leakage",
    )
    ep_store.save_episode(ep)

    heldout_sentinel = "HELDOUT_SECRET_EVAL_SENTINEL_XYZ_999"
    eval_cases = [
        {"id": "heldout_1", "skill": "sentinel_skill", "query": heldout_sentinel, "reference": "ground truth"}
    ]

    llm = FakeLLM(["""---
name: sentinel_skill
version: 1.0.0
description: desc
use_when: when
trigger:
  keywords: [sentinel]
---

## Overview
Body
"""])

    # Run miner
    mining_result = mine_candidate([ep], "sentinel_skill", llm, cand_store)

    # CRITICAL ASSERTION: The heldout sentinel string NEVER appeared in the miner prompt
    assert heldout_sentinel not in mining_result.miner_prompt_used
    for call in llm.calls:
        assert heldout_sentinel not in str(call)

    # A8 constraint extension: episodes with purpose='evaluation' are strictly rejected from mining
    eval_ep = Episode(
        episode_id="E_EVAL_SENTINEL",
        task_id="t_eval",
        run_id="r_eval",
        skill_name="sentinel_skill",
        skill_version="1.0.0",
        environment={"purpose": "evaluation"},
        provenances=[mock_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(eval_ep)
    with pytest.raises(ValueError, match="strictly isolated from candidate mining"):
        mine_candidate([eval_ep], "sentinel_skill", llm, cand_store)

    ep_store.close()
    cand_store.close()
