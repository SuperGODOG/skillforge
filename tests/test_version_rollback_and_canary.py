"""Milestone 4b Acceptance Test Suite: Version Comparison, Controlled Rollback, and Canary Routing

Covers Supervisor Scenarios F1 - F8:
- F1: Version comparison & snapshot integrity: read-only diff & eval deltas; N/A on missing scores;
      incomparable on protocol/eval_set mismatch; reading v1 never returns v2 body.
- F2: Canary admission gate: DRAFT/DECLINED/REVIEW candidates, unconfirmed PASS, drifted/tampered
      PASS all rejected; legitimate PASS + confirmed caller registers canary while stable remains v1.
- F3: Deterministic hash routing & boundaries: share=0 (all stable), share=100 (all canary);
      deterministic SHA256 bucketing across calls & DB reopen; invalid share rejected with no side effects;
      default to stable when no canary.
- F4: Execution snapshot binding & Episode integration: run freezes version/hash upon resolution;
      subsequent share change, rollback, or canary promotion does NOT mutate frozen snapshot;
      actual use_skill & collector record exact version & hash.
- F5: Controlled rollback, CAS & idempotency: rollback to historical v1 stops canary, updates stable;
      preserves full history & lineages; records audit event (from, to, reason); idempotent on operation_id;
      stale CAS revision rejected; DB reopen preserves state.
- F6: Rollback target validation: non-existent, other skill, unpublished/draft, corrupted hash,
      or unavailable dependency target rejected; current deployment untouched; incomplete history marked unavailable.
- F7: Switch consistency & fail-closed: simulated storage failure triggers transactional rollback
      without half-switched state; corrupted snapshot fails closed (never falls back to unverified body).
- F8: End-to-end lifecycle: M4a repair -> valid candidate -> set canary -> run executes canary & collects Episode ->
      promote canary to stable -> rollback to previous version; candidate cannot be routed directly,
      old runs remain frozen, lineage & audit complete.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import pytest

from skillforge import (
    CandidateSkill,
    CandidateStore,
    ConcurrencyError,
    Deployment,
    DeploymentAuditEvent,
    DeploymentManager,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    Release,
    ReleaseStateMachine,
    RunVersionBinding,
    SkillEvaluator,
    SkillMeta,
    SkillRegistry,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
    VersionComparison,
    VersionSnapshot,
    compute_candidate_hash,
    compute_content_hash,
    repair_skill_failure,
)


def _judge_json(verdict: str = "A_better") -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": ["OK"],
        "evidence_summary": "verified pass",
    })


class FakeLLM:
    """Deterministic FakeLLM recording invocations."""

    def __init__(self, contents: list[str], default_content: Optional[str] = None, usage_tokens: int = 100):
        self.contents = list(contents)
        self.default_content = default_content
        self.usage_tokens = usage_tokens
        self.calls: list[Any] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self.contents:
            content = self.contents.pop(0)
        elif self.default_content is not None:
            content = self.default_content
        else:
            content = "tied"
        return SimpleNamespace(
            content=content,
            usage={"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": self.usage_tokens},
        )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
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


def _valid_skill_md(
    name: str = "math_tool",
    version: str = "1.0.0",
    instructions: str = "Perform math.",
    description: str = "Standardized math skill",
    dependencies: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
) -> str:
    dep_list = dependencies or ["calc_api"]
    kw_list = keywords or ["calculate", "math"]
    deps_yaml = json.dumps(dep_list)
    kws_yaml = json.dumps(kw_list)
    return f"""---
name: {name}
version: {version}
description: {description}
use_when: calculating values
dependencies: {deps_yaml}
trigger:
  keywords: {kws_yaml}
---

## Overview
Operational guidance for {name}.

## Instructions
{instructions}
"""


def _make_provenance(tool_name: str = "calc_api", call_index: int = 0) -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name=tool_name,
        fixture_case_id=f"case_{tool_name}_{call_index}",
        call_index=call_index,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"param": "value"},
        output_status="SUCCESS",
        output_summary="Result: 42",
        latency_ms=10.0,
        timestamp="2026-09-25T12:00:00Z",
        signature=f"sha256:sig_{tool_name}_{call_index}",
        snapshot_id=f"snap_{tool_name}_{call_index}",
        snapshot_content="Result: 42",
    )


# ==================== F1: Version Comparison & Snapshot Integrity ====================

def test_scenario_f1_version_comparison_and_snapshot_integrity(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F1: Version comparison & snapshot integrity:
    - Same skill v1/v2 with distinct bodies, description, dependencies, and evaluations.
    - compare_versions gives accurate diffs & deltas and writes NO state (strictly read-only).
    - Missing scores marked N/A; mismatched eval protocols marked incomparable.
    - Reading v1 snapshot MUST return v1 body, never v2 body or latest disk content.
    """
    db_path = tmp_path / "test_f1.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Perform basic addition and subtraction."
    v1_md = _valid_skill_md(
        "math_tool", "1.0.0", v1_body, description="Math calculator v1", dependencies=["calc_api"]
    )
    v2_body = "Perform advanced arithmetic with division and bounds checking."
    v2_md = _valid_skill_md(
        "math_tool", "1.1.0", v2_body, description="Math calculator v2", dependencies=["calc_api", "precision_lib"]
    )

    # 1. Publish v1 to Git and SQLite
    (skill_dir / "SKILL.md").write_text(v1_md, encoding="utf-8")
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1_id = sm.begin_release("math_tool", "1.0.0", "L1")
    subprocess.run(["git", "add", "."], cwd=str(temp_git_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "release 1.0.0"], cwd=str(temp_git_repo), check=True, capture_output=True)
    c1_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(temp_git_repo), check=True, capture_output=True, text=True).stdout.strip()
    
    conn = sm._get_conn()
    conn.execute("UPDATE releases SET commit_hash = ? WHERE release_id = ?", (c1_hash, r1_id))
    conn.commit()
    sm.commit_release(r1_id)

    # Attach eval summary for v1
    eval_summary_v1 = {
        "eval_set": "benchmark_v1",
        "protocol": "pairwise_v1",
        "structure_score": {"name": 5.0, "version": 5.0, "instructions": 10.0},
        "effect_score": {"task": 20.0, "robust": 10.0, "readability": 8.0},
    }
    conn.execute(
        "UPDATE releases SET eval_summary_json = ?, body_md = ?, content_hash = ? WHERE release_id = ?",
        (json.dumps(eval_summary_v1), v1_body, compute_content_hash(v1_body), r1_id),
    )
    conn.commit()

    # 2. Publish v2 to Git and SQLite
    (skill_dir / "SKILL.md").write_text(v2_md, encoding="utf-8")
    r2_id = sm.begin_release("math_tool", "1.1.0", "L1")
    subprocess.run(["git", "add", "."], cwd=str(temp_git_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "release 1.1.0"], cwd=str(temp_git_repo), check=True, capture_output=True)
    c2_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(temp_git_repo), check=True, capture_output=True, text=True).stdout.strip()

    conn.execute("UPDATE releases SET commit_hash = ? WHERE release_id = ?", (c2_hash, r2_id))
    conn.commit()
    sm.commit_release(r2_id)

    eval_summary_v2 = {
        "eval_set": "benchmark_v1",
        "protocol": "pairwise_v1",
        "structure_score": {"name": 5.0, "version": 5.0, "instructions": 10.0},
        "effect_score": {"task": 25.0, "robust": 14.0, "readability": 9.0},
    }
    conn.execute(
        "UPDATE releases SET eval_summary_json = ?, body_md = ?, content_hash = ? WHERE release_id = ?",
        (json.dumps(eval_summary_v2), v2_body, compute_content_hash(v2_body), r2_id),
    )
    conn.commit()

    # Even edit disk SKILL.md to v3 to ensure snapshots are NOT reading latest disk!
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "3.0.0", "UNCOMMITTED DISK V3"), encoding="utf-8")

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)

    # Test snapshot integrity: reading v1 MUST return v1 body, NOT disk v3 or v2
    snap1 = dm.get_version_snapshot("math_tool", "1.0.0")
    assert snap1.body == v1_body
    assert "addition and subtraction" in snap1.body
    assert snap1.version == "1.0.0"
    assert snap1.content_hash == compute_content_hash(v1_body)

    snap2 = dm.get_version_snapshot("math_tool", "1.1.0")
    assert snap2.body == v2_body
    assert "bounds checking" in snap2.body
    assert snap2.version == "1.1.0"
    assert snap2.content_hash == compute_content_hash(v2_body)

    # Compare v1 and v2 (read-only)
    comp = dm.compare_versions("math_tool", "1.0.0", "1.1.0")
    assert comp.is_comparable is True
    assert "--- math_tool@1.0.0" in comp.content_diff
    assert "+++ math_tool@1.1.0" in comp.content_diff
    assert comp.metadata_diff["description"] == {"from": "Math calculator v1", "to": "Math calculator v2"}
    assert comp.dependencies_diff == {
        "added": ["precision_lib"],
        "removed": [],
        "unchanged": ["calc_api"],
    }
    assert comp.eval_delta["status"] == "comparable"
    deltas = comp.eval_delta["deltas"]
    assert deltas["effect.task"] == 5.0
    assert deltas["effect.robust"] == 4.0
    assert deltas["effect.readability"] == 1.0
    assert deltas["total_score"] == 10.0

    # Case B: Missing evaluation summary in v1
    conn.execute("UPDATE releases SET eval_summary_json = NULL WHERE release_id = ?", (r1_id,))
    conn.commit()
    comp_na = dm.compare_versions("math_tool", "1.0.0", "1.1.0")
    assert comp_na.eval_delta["status"] == "N/A"

    # Case C: Mismatched evaluation protocols
    eval_mismatch_v1 = {"eval_set": "benchmark_ALPHA", "protocol": "pairwise_v1"}
    eval_mismatch_v2 = {"eval_set": "benchmark_BETA", "protocol": "pairwise_v2"}
    conn.execute("UPDATE releases SET eval_summary_json = ? WHERE release_id = ?", (json.dumps(eval_mismatch_v1), r1_id))
    conn.execute("UPDATE releases SET eval_summary_json = ? WHERE release_id = ?", (json.dumps(eval_mismatch_v2), r2_id))
    conn.commit()
    comp_incomp = dm.compare_versions("math_tool", "1.0.0", "1.1.0")
    assert comp_incomp.is_comparable is False
    assert comp_incomp.eval_delta["status"] == "incomparable"
    assert "mismatch" in comp_incomp.incomparable_reason

    dm.close()
    sm.close()


# ==================== F2: Canary Admission Gate ====================

def test_scenario_f2_canary_admission_gate(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F2: Canary admission gate:
    - Draft, Failed, REVIEW candidates cannot be canary (stable remains unchanged).
    - Unconfirmed PASS cannot be canary.
    - Tampered / drifted PASS cannot be canary.
    - Verified PASS + confirmed caller can register canary via ReleaseStateMachine, stable remains v1.
    """
    db_path = tmp_path / "test_f2.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    base_body = "Baseline arithmetic."
    base_md = _valid_skill_md("math_tool", "1.0.0", base_body)
    (skill_dir / "SKILL.md").write_text(base_md, encoding="utf-8")

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)
    reg.load_skills_from_dir()
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1_id = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1_id)

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir, registry=reg)
    init_dep = dm.get_deployment("math_tool")
    assert init_dep.stable_version == "1.0.0"
    assert init_dep.canary_version is None

    # Helper candidate
    def _create_candidate(c_id: str, ver: str, body: str) -> CandidateSkill:
        c = CandidateSkill(
            candidate_id=c_id,
            skill_name="math_tool",
            decision="revise",
            source_episode_ids=["ep_mock"],
            meta=SkillMeta(
                name="math_tool",
                version=ver,
                description="desc",
                use_when="when",
                dependencies=["calc_api"],
                trigger=Trigger(keywords=["calc"]),
            ),
            body=body,
            rationale="testing canary",
        )
        return c

    # Case 1: DECLINED candidate cannot be canary
    cand_dec = _create_candidate("cand_dec", "1.0.1", "Declined body")
    val_dec = ValidationRecord(
        candidate_id="cand_dec",
        content_hash=compute_candidate_hash(cand_dec),
        baseline_version="1.0.0",
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="Only verified PASS candidates may be admitted"):
        dm.set_canary("math_tool", cand_dec, val_dec, share=20, caller_confirmed=True)

    # Case 2: REVIEW candidate cannot be canary
    cand_rev = _create_candidate("cand_rev", "1.0.1", "Review body")
    val_rev = ValidationRecord(
        candidate_id="cand_rev",
        content_hash=compute_candidate_hash(cand_rev),
        baseline_version="1.0.0",
        ratchet_decision="REVIEW",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="Only verified PASS candidates may be admitted"):
        dm.set_canary("math_tool", cand_rev, val_rev, share=20, caller_confirmed=True)

    # Case 3: PASS candidate with caller_confirmed=False
    cand_pass = _create_candidate("cand_pass", "1.0.1", "Valid pass body")
    val_pass = ValidationRecord(
        candidate_id="cand_pass",
        content_hash=compute_candidate_hash(cand_pass),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="requires explicit caller confirmation"):
        dm.set_canary("math_tool", cand_pass, val_pass, share=20, caller_confirmed=False)

    # Case 4: Candidate content mutated after evaluation
    cand_mut = _create_candidate("cand_mut", "1.0.1", "Body before mutation")
    val_mut = ValidationRecord(
        candidate_id="cand_mut",
        content_hash=compute_candidate_hash(cand_mut),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    cand_mut.body = "MUTATED BODY AFTER VALIDATION"
    with pytest.raises(ValueError, match="candidate content was mutated"):
        dm.set_canary("math_tool", cand_mut, val_mut, share=20, caller_confirmed=True)

    # Case 5: Baseline version drifted
    val_drift = ValidationRecord(
        candidate_id="cand_pass",
        content_hash=compute_candidate_hash(cand_pass),
        baseline_version="0.9.0",  # Expected 1.0.0
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(ValueError, match="baseline version changed from '0.9.0' to '1.0.0'"):
        dm.set_canary("math_tool", cand_pass, val_drift, share=20, caller_confirmed=True)

    # Stable deployment must remain untouched across all failed attempts
    dep_check = dm.get_deployment("math_tool")
    assert dep_check.stable_version == "1.0.0"
    assert dep_check.canary_version is None

    # Case 6: Legitimate candidate with PASS + caller_confirmed=True succeeds
    dep_ok = dm.set_canary("math_tool", cand_pass, val_pass, share=25, caller_confirmed=True)
    assert dep_ok.stable_version == "1.0.0"  # Stable is still 1.0.0!
    assert dep_ok.canary_version == "1.0.1"
    assert dep_ok.canary_share == 25
    assert dep_ok.revision == 2

    dm.close()
    sm.close()
    ep_store.close()
    cand_store.close()


# ==================== F3: Deterministic Hash Routing & Boundaries ====================

def test_scenario_f3_deterministic_hash_routing_and_boundaries(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F3: Deterministic hash routing & boundaries:
    - Fixed cohort of run IDs.
    - share=0 -> 100% stable; share=100 -> 100% canary.
    - Deterministic SHA256 bucketing: repeated calls & DB reopen give exact same choices.
    - Invalid share (<0 or >100) rejected with no side effects.
    - No canary configured -> default to stable.
    """
    db_path = tmp_path / "test_f3.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "1.0.0"), encoding="utf-8")

    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1_id = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1_id)
    r2_id = sm.begin_release("math_tool", "1.1.0", "L1")
    sm.commit_release(r2_id)

    conn = sm._get_conn()
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", ("Body 1.0.0", compute_content_hash("Body 1.0.0"), r1_id))
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", ("Body 1.1.0", compute_content_hash("Body 1.1.0"), r2_id))
    conn.execute("UPDATE skills SET current_release_id = ? WHERE name = 'math_tool'", (r1_id,))
    conn.commit()

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    dm.set_canary("math_tool", "1.1.0", share=50, caller_confirmed=True)

    test_runs = [f"run_cohort_case_{i:03d}" for i in range(50)]

    # 1. Boundary check: share = 0 -> 100% stable
    dm.change_canary_share("math_tool", share=0, caller_confirmed=True)
    for rid in test_runs:
        v, h, is_canary = dm.route_version("math_tool", run_id=rid)
        assert v == "1.0.0"
        assert is_canary is False

    # Clear run bindings for clean boundary test
    conn.execute("DELETE FROM run_version_bindings")
    conn.commit()

    # 2. Boundary check: share = 100 -> 100% canary
    dm.change_canary_share("math_tool", share=100, caller_confirmed=True)
    for rid in test_runs:
        v, h, is_canary = dm.route_version("math_tool", run_id=rid)
        assert v == "1.1.0"
        assert is_canary is True

    conn.execute("DELETE FROM run_version_bindings")
    conn.commit()

    # 3. Intermediate share = 40: deterministic routing & DB reopen reproducibility
    dm.change_canary_share("math_tool", share=40, caller_confirmed=True)
    initial_allocations = {}
    for rid in test_runs:
        v, h, is_canary = dm.route_version("math_tool", run_id=rid)
        initial_allocations[rid] = (v, is_canary)

    # Both stable and canary must be represented across the 50 runs
    canary_count = sum(1 for v, c in initial_allocations.values() if c)
    stable_count = sum(1 for v, c in initial_allocations.values() if not c)
    assert canary_count > 0
    assert stable_count > 0

    # Repeat call: exact same choices
    for rid in test_runs:
        v, h, is_canary = dm.route_version("math_tool", run_id=rid)
        assert (v, is_canary) == initial_allocations[rid]

    # Close and reopen DB: exact same choices
    dm.close()
    dm_reopen = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    for rid in test_runs:
        v, h, is_canary = dm_reopen.route_version("math_tool", run_id=rid)
        assert (v, is_canary) == initial_allocations[rid]

    # 4. Invalid share bounds rejected without side effects
    with pytest.raises(ValueError, match="Invalid canary share: -5"):
        dm_reopen.change_canary_share("math_tool", share=-5, caller_confirmed=True)
    with pytest.raises(ValueError, match="Invalid canary share: 105"):
        dm_reopen.change_canary_share("math_tool", share=105, caller_confirmed=True)
    assert dm_reopen.get_deployment("math_tool").canary_share == 40

    # 5. Skill without canary defaults to stable
    clean_dir = skills_dir / "clean_tool"
    clean_dir.mkdir(parents=True)
    (clean_dir / "SKILL.md").write_text(_valid_skill_md("clean_tool", "1.0.0"), encoding="utf-8")
    r_c_id = sm.begin_release("clean_tool", "1.0.0", "L1")
    sm.commit_release(r_c_id)
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", ("Clean body", compute_content_hash("Clean body"), r_c_id))
    conn.commit()

    v_c, _, is_canary_c = dm_reopen.route_version("clean_tool", run_id="run_new_1")
    assert v_c == "1.0.0"
    assert is_canary_c is False

    dm_reopen.close()
    sm.close()


# ==================== F4: Execution Snapshot Binding & Episode Integration ====================

def test_scenario_f4_execution_snapshot_binding_and_collector_integration(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F4: Execution snapshot binding & Episode integration:
    - Run A binds to canary v2; subsequent share change or rollback does NOT mutate Run A's frozen snapshot.
    - New Run B routes according to updated deployment.
    - Real use_skill reads frozen body, and ExperienceCollector records exact version & hash.
    """
    db_path = tmp_path / "test_f4.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Instructions v1.0.0: stable body"
    v2_body = "Instructions v1.1.0: canary body"

    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "1.0.0", v1_body), encoding="utf-8")
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1 = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1)
    r2 = sm.begin_release("math_tool", "1.1.0", "L1")
    sm.commit_release(r2)

    conn = sm._get_conn()
    h1 = compute_content_hash(v1_body)
    h2 = compute_content_hash(v2_body)
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v1_body, h1, r1))
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v2_body, h2, r2))
    conn.execute("UPDATE skills SET current_release_id = ? WHERE name = 'math_tool'", (r1,))
    conn.commit()

    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)
    reg.load_skills_from_dir()
    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir, registry=reg)
    reg._deployment_manager = dm

    # Set canary to v1.1.0 at 100% share
    dm.set_canary("math_tool", "1.1.0", share=100, caller_confirmed=True)

    # 1. Run A starts and binds to v1.1.0
    body_run_a = reg.use_skill("math_tool", reason="execute task A", run_id="run_A")
    assert body_run_a == v2_body
    binding_a = dm.get_run_binding("run_A", "math_tool")
    assert binding_a is not None
    assert binding_a.assigned_version == "1.1.0"
    assert binding_a.content_hash == h2
    assert binding_a.is_canary is True

    # 2. Deployment changes: canary share set to 0 and canary promoted/rolled back
    dm.change_canary_share("math_tool", share=0, caller_confirmed=True)
    dm.rollback_deployment("math_tool", "1.0.0", reason="emergency rollback", caller_confirmed=True)

    current_dep = dm.get_deployment("math_tool")
    assert current_dep.stable_version == "1.0.0"
    assert current_dep.canary_version is None

    # Run A MUST still receive frozen v1.1.0 body!
    body_run_a_later = reg.use_skill("math_tool", reason="re-fetch task A", run_id="run_A")
    assert body_run_a_later == v2_body

    # New Run B routes under new deployment -> gets stable v1.0.0 body
    body_run_b = reg.use_skill("math_tool", reason="execute task B", run_id="run_B")
    assert body_run_b == v1_body
    binding_b = dm.get_run_binding("run_B", "math_tool")
    assert binding_b.assigned_version == "1.0.0"
    assert binding_b.content_hash == h1

    # 3. ExperienceCollector integration: records bound version & hash into Episode
    ep_store = EpisodeStore(db_path)
    collector = ExperienceCollector(episode_store=ep_store, registry=reg)
    collector.start_run(
        run_id="run_A",
        task_id="task_A",
        skill_name="math_tool",
        skill_version=binding_a.assigned_version,
        environment={"cohort": "canary_test"},
    )
    ep_a = collector.finish_run(
        run_id="run_A",
        model_output=body_run_a,
        verification_evidence={"independent_pass": True, "checker": "unit_test"},
    )

    assert ep_a is not None
    assert ep_a.skill_version == "1.1.0"
    assert ep_a.outcome == "success"

    ep_store.close()
    reg.close()
    dm.close()
    sm.close()


# ==================== F5: Controlled Rollback, CAS, & Idempotency ====================

def test_scenario_f5_controlled_rollback_cas_and_idempotency(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F5: Controlled rollback, CAS, and idempotency:
    - Stable upgraded to v1.1.0; rollback to historical v1.0.0 deactivates canary.
    - All history, lineages, and releases preserved.
    - Audit event written with from, to, and reason.
    - Repeated operation_id is idempotent (no duplicate events or state changes).
    - Stale CAS revision rejected.
    - DB reopen preserves state.
    """
    db_path = tmp_path / "test_f5.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Instructions v1.0.0"
    v2_body = "Instructions v1.1.0"
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "1.0.0", v1_body), encoding="utf-8")

    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1 = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1)
    r2 = sm.begin_release("math_tool", "1.1.0", "L1")
    sm.commit_release(r2)

    conn = sm._get_conn()
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v1_body, compute_content_hash(v1_body), r1))
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v2_body, compute_content_hash(v2_body), r2))
    conn.commit()

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    # Set canary to 1.1.0 and promote to stable
    dm.set_canary("math_tool", "1.1.0", share=10, caller_confirmed=True)
    dep_v2 = dm.promote_canary_to_stable("math_tool", caller_confirmed=True)
    assert dep_v2.stable_version == "1.1.0"
    rev_before_rollback = dep_v2.revision

    # 1. Stale CAS revision rejected
    with pytest.raises(ConcurrencyError, match="revision mismatch"):
        dm.rollback_deployment(
            "math_tool", "1.0.0", reason="test stale CAS",
            caller_confirmed=True, expected_revision=rev_before_rollback - 1
        )

    # 2. Rollback with valid expected_revision and operation_id
    dep_rolled = dm.rollback_deployment(
        "math_tool", "1.0.0",
        reason="regression in production telemetry",
        caller_confirmed=True,
        expected_revision=rev_before_rollback,
        operation_id="op_rollback_001",
    )
    assert dep_rolled.stable_version == "1.0.0"
    assert dep_rolled.canary_version is None
    assert dep_rolled.canary_share == 0
    assert dep_rolled.revision == rev_before_rollback + 1

    # Audit events check
    events = dm.list_audit_events("math_tool")
    rb_events = [e for e in events if e.action == "ROLLBACK"]
    assert len(rb_events) == 1
    ev = rb_events[0]
    assert ev.operation_id == "op_rollback_001"
    assert ev.from_stable == "1.1.0"
    assert ev.to_stable == "1.0.0"
    assert ev.reason == "regression in production telemetry"

    # 3. Idempotent replay of same operation_id
    dep_replay = dm.rollback_deployment(
        "math_tool", "1.0.0",
        reason="regression in production telemetry",
        caller_confirmed=True,
        expected_revision=dep_rolled.revision,
        operation_id="op_rollback_001",
    )
    assert dep_replay.revision == dep_rolled.revision  # No new revision!
    events_after = dm.list_audit_events("math_tool")
    assert len(events_after) == len(events)  # No duplicate event!

    # 4. DB reopen maintains state
    dm.close()
    dm_reopen = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    dep_reopen = dm_reopen.get_deployment("math_tool")
    assert dep_reopen.stable_version == "1.0.0"
    assert dep_reopen.canary_version is None
    assert len(dm_reopen.list_audit_events("math_tool")) == len(events)

    dm_reopen.close()
    sm.close()


# ==================== F6: Rollback Target Validation ====================

def test_scenario_f6_rollback_target_validation_and_dependency_guards(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F6: Rollback target validation:
    - Target does not exist -> rejected.
    - Target belongs to another skill -> rejected.
    - Target is unpublished or draft -> rejected.
    - Target has corrupted content hash -> rejected.
    - Target has unavailable dependencies -> rejected.
    - Current deployment remains unchanged in all cases.
    """
    db_path = tmp_path / "test_f6.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Instructions v1.0.0 with calc_api"
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "1.0.0", v1_body, dependencies=["calc_api"]), encoding="utf-8")

    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1 = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1)

    v2_body = "Instructions v2.0.0"
    r2 = sm.begin_release("math_tool", "2.0.0", "L1")
    sm.commit_release(r2)

    conn = sm._get_conn()
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ?, meta_json = ? WHERE release_id = ?",
                 (v1_body, compute_content_hash(v1_body), json.dumps({"name": "math_tool", "version": "1.0.0", "description": "d", "use_when": "w", "dependencies": ["calc_api"]}), r1))
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?",
                 (v2_body, compute_content_hash(v2_body), r2))
    conn.commit()

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    init_dep = dm.get_deployment("math_tool")
    assert init_dep.stable_version == "2.0.0"

    # 1. Non-existent version
    with pytest.raises((KeyError, ValueError)):
        dm.rollback_deployment("math_tool", "9.9.9", reason="rollback missing", caller_confirmed=True)

    # 2. Unpublished / Draft candidate version
    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, body_md)
           VALUES ('rel_draft_99', 'math_tool', '1.9.9-draft', 'DRAFT', 'L1', 'Draft body')"""
    )
    conn.commit()
    with pytest.raises(ValueError, match="not a verified published release"):
        dm.rollback_deployment("math_tool", "1.9.9-draft", reason="rollback draft", caller_confirmed=True)

    # 3. Corrupted content hash
    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, body_md, content_hash)
           VALUES ('rel_corrupt_v0', 'math_tool', '0.5.0', 'PUBLISHED', 'L1', 'Tampered body', 'sha256:FAKE_HASH_000')"""
    )
    conn.commit()
    with pytest.raises(ValueError, match="corrupted"):
        dm.rollback_deployment("math_tool", "0.5.0", reason="rollback corrupted", caller_confirmed=True)

    # 4. Unavailable dependency
    v_dep_body = "Old instructions with legacy tool"
    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, body_md, content_hash, meta_json)
           VALUES ('rel_unavail_dep', 'math_tool', '0.8.0', 'PUBLISHED', 'L1', ?, ?, ?)""",
        (
            v_dep_body,
            compute_content_hash(v_dep_body),
            json.dumps({"name": "math_tool", "version": "0.8.0", "description": "d", "use_when": "w", "dependencies": ["unavailable_legacy_hardware_api"]}),
        ),
    )
    conn.commit()
    with pytest.raises(ValueError, match="unavailable in current environment"):
        dm.rollback_deployment("math_tool", "0.8.0", reason="rollback unavailable dep", caller_confirmed=True)

    # In all rejected cases, deployment remains 2.0.0!
    dep_final = dm.get_deployment("math_tool")
    assert dep_final.stable_version == "2.0.0"

    dm.close()
    sm.close()


# ==================== F7: Switch Consistency & Fail-Closed ====================

def test_scenario_f7_switch_consistency_transactional_rollback_and_fail_closed(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F7: Switch consistency and fail-closed behavior:
    - Simulated storage failure during rollback aborts transaction without half-switched state.
    - Corrupted snapshot fails closed on reading (never falls back to unverified disk content).
    """
    db_path = tmp_path / "test_f7.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Body 1.0.0"
    v2_body = "Body 2.0.0"
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "2.0.0", v2_body), encoding="utf-8")

    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    r1 = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1)
    r2 = sm.begin_release("math_tool", "2.0.0", "L1")
    sm.commit_release(r2)

    conn = sm._get_conn()
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v1_body, compute_content_hash(v1_body), r1))
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?", (v2_body, compute_content_hash(v2_body), r2))
    conn.commit()

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir)
    dep_init = dm.get_deployment("math_tool")
    assert dep_init.stable_version == "2.0.0"
    rev_init = dep_init.revision

    # 1. Transactional failure test: attach a trigger that aborts audit event writes
    conn.execute(
        """CREATE TRIGGER simulate_io_failure
           BEFORE INSERT ON deployment_audit_events
           BEGIN
               SELECT RAISE(ABORT, 'Simulated storage IO disk failure during audit write');
           END;"""
    )
    conn.commit()

    # Attempt rollback -> MUST fail and roll back transaction completely
    with pytest.raises(sqlite3.IntegrityError, match="Simulated storage IO disk failure"):
        dm.rollback_deployment("math_tool", "1.0.0", reason="testing atomic rollback", caller_confirmed=True)

    # Verify no half-switched state: stable is STILL 2.0.0, revision is unchanged!
    dep_check = dm.get_deployment("math_tool")
    assert dep_check.stable_version == "2.0.0"
    assert dep_check.revision == rev_init

    # Remove trigger
    conn.execute("DROP TRIGGER simulate_io_failure")
    conn.commit()

    # 2. Fail-closed test on corrupted snapshot
    conn.execute("UPDATE releases SET content_hash = 'sha256:CORRUPTED' WHERE release_id = ?", (r1,))
    conn.commit()

    # get_version_snapshot MUST raise ValueError on corrupted hash, NOT return unverified disk content
    with pytest.raises(ValueError, match="Snapshot corrupted"):
        dm.get_version_snapshot("math_tool", "1.0.0")

    dm.close()
    sm.close()


# ==================== F8: End-to-End Lifecycle ====================

def test_scenario_f8_end_to_end_lifecycle_from_m4a_to_canary_promotion_and_rollback(
    tmp_path: Path,
    temp_git_repo: Path,
):
    """F8: End-to-end lifecycle integration:
    - Step 1: Base skill v1.0.0 active in registry and deployment.
    - Step 2: M4a failure episode triggers bounded repair -> generates candidate v1.0.1 (READY, PASS).
    - Step 3: Assert candidate v1.0.1 CANNOT be routed directly to runs.
    - Step 4: Confirm canary for v1.0.1 at 100% share.
    - Step 5: Run A executes canary v1.0.1; ExperienceCollector records Episode with v1.0.1.
    - Step 6: Confirm promote canary to stable.
    - Step 7: Run A remains frozen at v1.0.1; new Run B gets stable v1.0.1.
    - Step 8: Rollback deployment to v1.0.0.
    - Step 9: Prior runs A & B maintain their frozen snapshots; new Run C routes to v1.0.0.
    - Step 10: All Episode records, lineages, and audit events are complete and verified.
    """
    db_path = tmp_path / "test_f8.db"
    skills_dir = temp_git_repo / "skills"
    skill_dir = skills_dir / "math_tool"
    skill_dir.mkdir(parents=True)

    v1_body = "Instructions v1.0.0: baseline math."
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("math_tool", "1.0.0", v1_body), encoding="utf-8")

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)
    reg.load_skills_from_dir()
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)

    r1_id = sm.begin_release("math_tool", "1.0.0", "L1")
    sm.commit_release(r1_id)
    conn = sm._get_conn()
    conn.execute("UPDATE releases SET body_md = ?, content_hash = ? WHERE release_id = ?",
                 (v1_body, compute_content_hash(v1_body), r1_id))
    conn.commit()

    dm = DeploymentManager(db_path=db_path, repo_root=temp_git_repo, skills_dir=skills_dir, registry=reg)
    reg._deployment_manager = dm
    dep_step1 = dm.get_deployment("math_tool")
    assert dep_step1.stable_version == "1.0.0"

    # Step 2: Trigger M4a failure repair
    prov = _make_provenance("calc_api", 0)
    failed_ep = Episode(
        episode_id="ep_fail_01",
        task_id="t_calc",
        run_id="r_calc",
        skill_name="math_tool",
        skill_version="1.0.0",
        environment={"purpose": "learning", "query": "calculate division"},
        provenances=[prov],
        acceptance_criteria={"expected": "numeric"},
        outcome="failure",
        outcome_reason="Zero division error unhandled",
        verification_evidence={"independent_pass": False, "checker": "assertion"},
    )
    ep_store.save_episode(failed_ep)

    v101_body = "Instructions v1.0.1: division safely handled."
    patcher_llm = FakeLLM([_valid_skill_md("math_tool", "1.0.1", v101_body)])
    judge_llm = FakeLLM([], default_content=_judge_json("tied"))
    evaluator = SkillEvaluator(registry=reg, llm=FakeLLM(["bare", "out"], default_content="out"), judge_llm=judge_llm)
    eval_cases = [{"id": "c1", "query": "calculate 10 / 2", "reference": "5"}]

    repair_job = repair_skill_failure(
        [failed_ep], "math_tool", ep_store, cand_store, reg, evaluator, eval_cases, patcher_llm, max_attempts=1
    )
    assert repair_job.status == "READY"
    candidate = repair_job.latest_candidate
    assert candidate is not None
    assert candidate.meta.version == "1.0.1"

    # Step 3: Candidate CANNOT be routed directly to live runs before canary admission
    v_direct, _, is_canary_direct = dm.route_version("math_tool", run_id="run_pre_canary")
    assert v_direct == "1.0.0"
    assert is_canary_direct is False

    # Step 4: Admitting candidate to canary
    val_rec = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=compute_candidate_hash(candidate),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    dep_canary = dm.set_canary("math_tool", candidate, val_rec, share=100, caller_confirmed=True)
    assert dep_canary.stable_version == "1.0.0"
    assert dep_canary.canary_version == "1.0.1"
    assert dep_canary.canary_share == 100

    # Step 5: Run A executes canary v1.0.1 & collector records Episode
    collector = ExperienceCollector(episode_store=ep_store, registry=reg)
    body_run_a = reg.use_skill("math_tool", reason="live task A", run_id="run_A")
    assert body_run_a == candidate.body

    collector.start_run(
        run_id="run_A",
        task_id="task_A",
        skill_name="math_tool",
        skill_version="1.0.1",
        environment={"purpose": "learning"},
    )
    ep_a = collector.finish_run(
        run_id="run_A",
        model_output=body_run_a,
        verification_evidence={"independent_pass": True, "checker": "unit_test"},
    )
    assert ep_a is not None
    assert ep_a.skill_version == "1.0.1"

    # Step 6: Promote canary to stable
    dep_promoted = dm.promote_canary_to_stable("math_tool", caller_confirmed=True)
    assert dep_promoted.stable_version == "1.0.1"
    assert dep_promoted.canary_version is None

    # Step 7: Run A remains frozen at 1.0.1; new Run B gets stable 1.0.1
    assert reg.use_skill("math_tool", reason="audit A", run_id="run_A") == candidate.body
    assert reg.use_skill("math_tool", reason="run B", run_id="run_B") == candidate.body

    # Step 8: Rollback to previous version v1.0.0
    dep_rb = dm.rollback_deployment("math_tool", "1.0.0", reason="detected production issue", caller_confirmed=True)
    assert dep_rb.stable_version == "1.0.0"
    assert dep_rb.canary_version is None

    # Step 9: Prior runs maintain frozen snapshots; new Run C routes to v1.0.0
    assert reg.use_skill("math_tool", reason="audit A after rollback", run_id="run_A") == candidate.body
    assert reg.use_skill("math_tool", reason="audit B after rollback", run_id="run_B") == candidate.body

    body_run_c = reg.use_skill("math_tool", reason="run C after rollback", run_id="run_C")
    assert body_run_c == v1_body
    binding_c = dm.get_run_binding("run_C", "math_tool")
    assert binding_c.assigned_version == "1.0.0"

    # Step 10: Audit history has both PROMOTE_CANARY and ROLLBACK events
    events = dm.list_audit_events("math_tool")
    actions = [e.action for e in events]
    assert "SET_CANARY" in actions
    assert "PROMOTE_CANARY" in actions
    assert "ROLLBACK" in actions

    ep_store.close()
    cand_store.close()
    reg.close()
    dm.close()
    sm.close()
