"""Milestone 3b Acceptance Test Suite: Pattern Mining and Episode Pool Filtering

Covers Supervisor Scenarios D1 - D8:
- D1: 3 distinct learning tasks with >= 2 steps, high similarity, >= 2 expressions ->
      1 candidate created in CandidateStore (DRAFT), sources match, active registry untouched.
- D2: Conservative threshold abstain:
      D2a: < 3 independent tasks;
      D2b: repeated same task (deduplicated < 3);
      D2c: < 2 distinct expressions;
      D2d: < 2 observable steps -> all abstain, 0 LLM calls, 0 candidates.
- D3: Success rate & coverage filtering:
      D3a: 4 success, 1 failure (0.8 rate, 1.0 coverage) passes, failure kept as counter-example;
      D3b: 3 success, 2 failure (0.6 rate < 0.8) abstains;
      D3c: 2 success, 3 unknown (0.4 coverage < 0.8) abstains.
- D4: Semantic separation & document workflow:
      D4a: 2 distinct semantic task families split into 2 clusters using embedder;
      D4b: document workflow with explicit action steps satisfies complexity without tools.
- D5: Strict A8 purpose isolation:
      Evaluation / heldout episodes with sentinels and unknown-purpose episodes strictly
      filtered before clustering/embedding/LLM.
- D6: Batch idempotency & persistence:
      Repeated runs reuse SQLite mined_batches ledger; DB close/reopen preserves cache;
      adding a new valid task allows re-evaluation without corrupting old candidate.
- D7: Revise existing skill & target version drift:
      D7a: existing skill triggers 'revise' decision and binds baseline version;
      D7b: LLM malformed output records 'abandon' without crashing;
      D7c: target skill version bump in registry updates fingerprint and invalidates stale cache.
- D8: End-to-end integration:
      Real SkillEvaluator.evaluate_skill with purpose='learning' and collector auto-persists
      episodes into SQLite; mine_pending discovers and synthesizes isolated draft candidate.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import pytest

from skillforge import (
    Episode,
    CandidateSkill,
    EpisodeStore,
    CandidateStore,
    SkillRegistry,
    SkillEvaluator,
    ExperienceCollector,
    ToolCallProvenance,
    SkillMeta,
    Trigger,
    PatternMiningConfig,
    ClusterReport,
    MiningBatchReport,
    mine_pending,
)


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


def _valid_skill_md(name: str = "report_skill", version: str = "1.0.0") -> str:
    return f"""---
name: {name}
version: {version}
description: Standardized skill for {name}
use_when: handling {name} tasks
trigger:
  keywords: [{name}]
---

## Overview
Automated operational guidance for {name}.

## Workflow
1. Read input parameters.
2. Execute target action.
"""


def _make_provenance(tool_name: str, call_index: int = 0) -> ToolCallProvenance:
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
        output_summary=f"Output for {tool_name}",
        latency_ms=10.0,
        timestamp="2026-09-25T12:00:00Z",
        signature=f"sha256:sig_{tool_name}_{call_index}",
        snapshot_id=f"snap_{tool_name}_{call_index}",
        snapshot_content=f"Content for {tool_name}",
    )


def _make_episode(
    task_id: str,
    run_id: str,
    skill_name: str = "report_skill",
    skill_version: str = "1.0.0",
    outcome: str = "success",
    purpose: str = "learning",
    query: str = "fetch and format report",
    provenances: Optional[list[ToolCallProvenance]] = None,
    environment_steps: Optional[list[str]] = None,
    created_at: Optional[str] = None,
) -> Episode:
    provs = provenances
    if provs is None and environment_steps is None:
        provs = [
            _make_provenance("fetch_tool", 0),
            _make_provenance("format_tool", 1),
        ]
    elif provs is None:
        provs = []

    env: dict[str, Any] = {"purpose": purpose, "query": query}
    if environment_steps is not None:
        env["steps"] = environment_steps

    verification: Optional[dict[str, Any]] = None
    if outcome == "success":
        verification = {
            "source": "fixture_test",
            "passed": True,
            "independent_pass": True,
        }
    elif outcome == "failure":
        verification = {
            "source": "fixture_test",
            "passed": False,
            "independent_pass": False,
        }

    return Episode(
        episode_id=f"ep_{run_id}",
        task_id=task_id,
        run_id=run_id,
        skill_name=skill_name,
        skill_version=skill_version,
        environment=env,
        provenances=provs,
        acceptance_criteria={"query": query, "expected": "valid_output"},
        outcome=outcome,
        verification_evidence=verification,
        outcome_reason="Independent fixture verification pass" if outcome == "success" else "Test outcome",
        created_at=created_at or "2026-09-25T12:00:00Z",
    )


# ==================== D1: Auto Cluster & Candidate Creation ====================

def test_scenario_d1_auto_cluster_and_candidate_creation(tmp_path: Path):
    """D1: 3 distinct learning tasks with >= 2 steps, high similarity, >= 2 expressions ->

    1 candidate created, sources match, active registry untouched.
    """
    db_path = tmp_path / "test_d1.db"
    skills_dir = tmp_path / "skills"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

    # 3 distinct tasks, 2 steps each, diverse expressions
    ep1 = _make_episode("task_sales", "run_1", query="generate sales report for Q1")
    ep2 = _make_episode("task_revenue", "run_2", query="generate revenue report for Q2")
    ep3 = _make_episode("task_profit", "run_3", query="generate profit report for Q3")

    for ep in (ep1, ep2, ep3):
        ep_store.save_episode(ep)

    llm = FakeLLM([_valid_skill_md("report_skill")])

    report: MiningBatchReport = mine_pending(
        episode_store=ep_store,
        candidate_store=cand_store,
        registry=reg,
        llm=llm,
    )

    assert report.total_episodes_scanned == 3
    assert report.learning_episodes_count == 3
    assert report.filtered_evaluation_episodes == 0
    assert report.unique_tasks == 3
    assert len(report.candidates_created) == 1
    assert len(report.abstained_clusters) == 0

    cand = report.candidates_created[0]
    assert cand.skill_name == "report_skill"
    assert cand.decision == "create"
    assert cand.status == "DRAFT"
    assert set(cand.source_episode_ids) == {"ep_run_1", "ep_run_2", "ep_run_3"}

    # Verified stored in CandidateStore
    stored = cand_store.get_candidate(cand.candidate_id)
    assert stored is not None
    assert stored.candidate_id == cand.candidate_id

    # Active registry is strictly untouched
    assert "report_skill" not in reg.list_names()


# ==================== D2: Conservative Threshold Abstain ====================

def test_scenario_d2_conservative_threshold_abstain(tmp_path: Path):
    """D2: Conservative threshold checks abstain before calling LLM:

    - D2a: < 3 independent tasks
    - D2b: repeated runs of same task (< 3 unique tasks after dedup)
    - D2c: < 2 distinct normalized expressions
    - D2d: < 2 observable steps
    """
    # D2a: Insufficient support count (< 3 tasks)
    db_a = tmp_path / "test_d2a.db"
    ep_a = EpisodeStore(db_a)
    cand_a = CandidateStore(db_a, episode_store=ep_a)
    ep_a.save_episode(_make_episode("task_1", "run_1", query="task 1 action"))
    ep_a.save_episode(_make_episode("task_2", "run_2", query="task 2 action"))

    llm_a = FakeLLM([])
    rep_a = mine_pending(ep_a, cand_a, llm=llm_a)
    assert len(rep_a.candidates_created) == 0
    assert len(rep_a.abstained_clusters) == 1
    assert any("Insufficient independent task support" in r for r in rep_a.abstained_clusters[0].abstain_reasons)
    assert len(llm_a.calls) == 0
    assert len(cand_a.list_candidates()) == 0

    # D2b: Repeated same task: 3 runs of task_dup -> deduplicated to 1 task (< 3)
    db_b = tmp_path / "test_d2b.db"
    ep_b = EpisodeStore(db_b)
    cand_b = CandidateStore(db_b, episode_store=ep_b)
    ep_b.save_episode(_make_episode("task_dup", "run_1", query="task dup run 1", created_at="2026-09-25T10:00:00Z"))
    ep_b.save_episode(_make_episode("task_dup", "run_2", query="task dup run 2", created_at="2026-09-25T11:00:00Z"))
    ep_b.save_episode(_make_episode("task_dup", "run_3", query="task dup run 3", created_at="2026-09-25T12:00:00Z"))

    llm_b = FakeLLM([])
    rep_b = mine_pending(ep_b, cand_b, llm=llm_b)
    assert rep_b.unique_tasks == 1
    assert len(rep_b.candidates_created) == 0
    assert any("Insufficient independent task support" in r for r in rep_b.abstained_clusters[0].abstain_reasons)
    assert len(llm_b.calls) == 0

    # D2c: < 2 distinct expressions: 3 distinct tasks with identical normalized expression
    db_c = tmp_path / "test_d2c.db"
    ep_c = EpisodeStore(db_c)
    cand_c = CandidateStore(db_c, episode_store=ep_c)
    ep_c.save_episode(_make_episode("task_1", "run_1", query="identical query text"))
    ep_c.save_episode(_make_episode("task_2", "run_2", query="IDENTICAL QUERY TEXT"))
    ep_c.save_episode(_make_episode("task_3", "run_3", query="  identical query text  "))

    llm_c = FakeLLM([])
    rep_c = mine_pending(ep_c, cand_c, llm=llm_c)
    assert len(rep_c.candidates_created) == 0
    assert any("Insufficient task expression diversity" in r for r in rep_c.abstained_clusters[0].abstain_reasons)
    assert len(llm_c.calls) == 0

    # D2d: < 2 observable steps: 3 tasks with only 1 step each
    db_d = tmp_path / "test_d2d.db"
    ep_d = EpisodeStore(db_d)
    cand_d = CandidateStore(db_d, episode_store=ep_d)
    ep_d.save_episode(_make_episode("task_1", "run_1", query="task one action", provenances=[_make_provenance("single_tool", 0)]))
    ep_d.save_episode(_make_episode("task_2", "run_2", query="task two action", provenances=[_make_provenance("single_tool", 0)]))
    ep_d.save_episode(_make_episode("task_3", "run_3", query="task three action", provenances=[_make_provenance("single_tool", 0)]))

    llm_d = FakeLLM([])
    rep_d = mine_pending(ep_d, cand_d, llm=llm_d)
    assert len(rep_d.candidates_created) == 0
    assert any("Insufficient observable step complexity" in r for r in rep_d.abstained_clusters[0].abstain_reasons)
    assert len(llm_d.calls) == 0


# ==================== D3: Success Rate & Coverage Filtering ====================

def test_scenario_d3_success_rate_and_coverage_filtering(tmp_path: Path):
    """D3: Outcome metrics filtering:

    - D3a: 4 success, 1 failure (0.8 success_rate, 1.0 coverage) -> passes, failure in counter_examples
    - D3b: 3 success, 2 failure (0.6 success_rate < 0.8) -> abstains
    - D3c: 2 success, 3 unknown (0.4 coverage < 0.8) -> abstains
    """
    # D3a: 4 success, 1 failure -> passes
    db_a = tmp_path / "test_d3a.db"
    ep_a = EpisodeStore(db_a)
    cand_a = CandidateStore(db_a, episode_store=ep_a)
    for i in range(1, 5):
        ep_a.save_episode(_make_episode(f"task_{i}", f"run_{i}", outcome="success", query=f"fetch metrics {i}"))
    ep_a.save_episode(_make_episode("task_5", "run_5", outcome="failure", query="fetch metrics 5"))

    llm_a = FakeLLM([_valid_skill_md("report_skill")])
    rep_a = mine_pending(ep_a, cand_a, llm=llm_a)
    assert len(rep_a.candidates_created) == 1
    cluster_a = rep_a.clusters[0]
    assert cluster_a.success_count == 4
    assert cluster_a.failure_count == 1
    assert cluster_a.success_rate == 0.8
    assert cluster_a.coverage == 1.0
    assert cluster_a.counter_example_episode_ids == ["ep_run_5"]
    assert len(cluster_a.source_episode_ids) == 5

    # D3b: 3 success, 2 failure -> 3/5 = 0.6 < 0.8 -> abstains
    db_b = tmp_path / "test_d3b.db"
    ep_b = EpisodeStore(db_b)
    cand_b = CandidateStore(db_b, episode_store=ep_b)
    for i in range(1, 4):
        ep_b.save_episode(_make_episode(f"task_{i}", f"run_{i}", outcome="success", query=f"sync report {i}"))
    for i in range(4, 6):
        ep_b.save_episode(_make_episode(f"task_{i}", f"run_{i}", outcome="failure", query=f"sync report {i}"))

    llm_b = FakeLLM([])
    rep_b = mine_pending(ep_b, cand_b, llm=llm_b)
    assert len(rep_b.candidates_created) == 0
    assert len(rep_b.abstained_clusters) == 1
    assert any("Success rate too low" in r for r in rep_b.abstained_clusters[0].abstain_reasons)
    assert len(llm_b.calls) == 0

    # D3c: 2 success, 3 unknown -> 2/5 = 0.4 coverage < 0.8 -> abstains
    db_c = tmp_path / "test_d3c.db"
    ep_c = EpisodeStore(db_c)
    cand_c = CandidateStore(db_c, episode_store=ep_c)
    for i in range(1, 3):
        ep_c.save_episode(_make_episode(f"task_{i}", f"run_{i}", outcome="success", query=f"parse log {i}"))
    for i in range(3, 6):
        ep_c.save_episode(_make_episode(f"task_{i}", f"run_{i}", outcome="unknown", query=f"parse log {i}"))

    llm_c = FakeLLM([])
    rep_c = mine_pending(ep_c, cand_c, llm=llm_c)
    assert len(rep_c.candidates_created) == 0
    assert len(rep_c.abstained_clusters) == 1
    assert any("Known outcome coverage too low" in r for r in rep_c.abstained_clusters[0].abstain_reasons)
    assert len(llm_c.calls) == 0


# ==================== D4: Semantic Separation & Document Workflow ====================

def test_scenario_d4_semantic_separation_and_document_workflow(tmp_path: Path):
    """D4: Semantic clustering and tool-free document workflow:

    - D4a: 2 semantic groups sharing tools split into 2 separate clusters
    - D4b: document workflow with explicit action steps satisfies step complexity without tools
    """
    # D4a: Semantic separation with mock embedder
    db_a = tmp_path / "test_d4a.db"
    ep_a = EpisodeStore(db_a)
    cand_a = CandidateStore(db_a, episode_store=ep_a)

    # 3 code review tasks + 3 sql optimization tasks
    shared_provs = [_make_provenance("file_tool", 0), _make_provenance("editor_tool", 1)]
    for i in range(1, 4):
        ep_a.save_episode(_make_episode(f"task_cr_{i}", f"run_cr_{i}", skill_name="code_reviewer", query=f"code review python module {i}", provenances=shared_provs))
    for i in range(1, 4):
        ep_a.save_episode(_make_episode(f"task_sql_{i}", f"run_sql_{i}", skill_name="sql_optimizer", query=f"sql query performance tuning {i}", provenances=shared_provs))

    def mock_embedder(texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            if "code review" in t:
                vecs.append([1.0, 0.0])
            else:
                vecs.append([0.0, 1.0])
        return vecs

    llm_a = FakeLLM([
        _valid_skill_md("code_reviewer"),
        _valid_skill_md("sql_optimizer"),
    ])
    rep_a = mine_pending(ep_a, cand_a, embedder=mock_embedder, llm=llm_a)
    assert len(rep_a.clusters) == 2
    assert len(rep_a.candidates_created) == 2
    skill_names = {c.skill_name for c in rep_a.candidates_created}
    assert skill_names == {"code_reviewer", "sql_optimizer"}

    # D4b: Document workflow without tools (environment steps)
    db_b = tmp_path / "test_d4b.db"
    ep_b = EpisodeStore(db_b)
    cand_b = CandidateStore(db_b, episode_store=ep_b)

    for i in range(1, 4):
        ep_b.save_episode(_make_episode(
            task_id=f"task_doc_{i}",
            run_id=f"run_doc_{i}",
            skill_name="doc_drafter",
            query=f"draft executive summary chapter {i}",
            provenances=[],
            environment_steps=["outline_key_points", "draft_section", "review_compliance"],
        ))

    llm_b = FakeLLM([_valid_skill_md("doc_drafter")])
    rep_b = mine_pending(ep_b, cand_b, llm=llm_b)
    assert len(rep_b.candidates_created) == 1
    assert rep_b.clusters[0].complexity_steps == 3
    assert rep_b.candidates_created[0].skill_name == "doc_drafter"


# ==================== D5: Evaluation / Heldout Isolation ====================

def test_scenario_d5_evaluation_heldout_isolation(tmp_path: Path):
    """D5: Strict A8 Purpose Isolation:

    Evaluation / heldout episodes and unverified purposes are filtered before grouping/LLM.
    Sentinels never reach the miner prompt.
    """
    db_path = tmp_path / "test_d5.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    # 3 valid learning episodes
    for i in range(1, 4):
        ep_store.save_episode(_make_episode(
            f"task_learn_{i}", f"run_learn_{i}",
            query=f"standard production task {i}",
            purpose="learning",
        ))

    # 2 evaluation / heldout episodes with secret sentinel string
    sentinel = "CLASSIFIED_EVAL_SECRET_KEY_998877"
    ep_store.save_episode(_make_episode(
        "task_eval_1", "run_eval_1",
        query=f"evaluate sentinel {sentinel} A",
        purpose="evaluation",
    ))
    ep_store.save_episode(_make_episode(
        "task_heldout_2", "run_heldout_2",
        query=f"heldout secret {sentinel} B",
        purpose="heldout",
    ))

    # 1 unknown / empty purpose episode
    ep_store.save_episode(_make_episode(
        "task_unverified", "run_unverified",
        query="unverified purpose run",
        purpose="",
    ))

    llm = FakeLLM([_valid_skill_md("report_skill")])
    report = mine_pending(ep_store, cand_store, llm=llm)

    assert report.total_episodes_scanned == 6
    assert report.learning_episodes_count == 3
    assert report.filtered_evaluation_episodes == 3
    assert report.unique_tasks == 3
    assert len(report.candidates_created) == 1

    cand = report.candidates_created[0]
    assert set(cand.source_episode_ids) == {"ep_run_learn_1", "ep_run_learn_2", "ep_run_learn_3"}

    # Sentinel check: never leaked into LLM prompts
    assert len(llm.calls) == 1
    miner_prompt = llm.calls[0]
    assert sentinel not in miner_prompt


# ==================== D6: Batch Idempotency & Persistence ====================

def test_scenario_d6_batch_idempotency_and_persistence(tmp_path: Path):
    """D6: Batch idempotency & persistence:

    - Repeated identical run reuses mined_batches ledger (0 new LLM calls)
    - Reopening DB from disk preserves cached ledger result
    - Adding a new valid task changes fingerprint and synthesizes new candidate without corruption
    """
    db_path = tmp_path / "test_d6.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    for i in range(1, 4):
        ep_store.save_episode(_make_episode(f"task_{i}", f"run_{i}", query=f"batch action {i}"))

    llm = FakeLLM([
        _valid_skill_md("report_skill"),
        _valid_skill_md("report_skill"),  # backup for second valid synthesis
    ])

    # Run 1: fresh synthesis
    rep1 = mine_pending(ep_store, cand_store, llm=llm)
    assert len(rep1.candidates_created) == 1
    assert rep1.clusters[0].is_cached is False
    assert len(llm.calls) == 1
    cand1 = rep1.candidates_created[0]

    # Run 2: identical input -> reuses cache
    rep2 = mine_pending(ep_store, cand_store, llm=llm)
    assert len(rep2.candidates_created) == 1
    assert rep2.clusters[0].is_cached is True
    assert rep2.clusters[0].candidate_id == cand1.candidate_id
    assert len(llm.calls) == 1  # No duplicate LLM call
    assert len(cand_store.list_candidates()) == 1

    # Run 3: reopen DB connection and verify persistence
    ep_store.close()
    ep_store_reopen = EpisodeStore(db_path)
    cand_store_reopen = CandidateStore(db_path, episode_store=ep_store_reopen)

    rep3 = mine_pending(ep_store_reopen, cand_store_reopen, llm=llm)
    assert len(rep3.candidates_created) == 1
    assert rep3.clusters[0].is_cached is True
    assert rep3.clusters[0].candidate_id == cand1.candidate_id
    assert len(llm.calls) == 1

    # Run 4: add a 4th valid task -> changes batch fingerprint -> synthesizes new candidate
    ep_store_reopen.save_episode(_make_episode("task_4", "run_4", query="batch action 4"))
    rep4 = mine_pending(ep_store_reopen, cand_store_reopen, llm=llm)
    assert len(rep4.candidates_created) == 1
    assert rep4.clusters[0].is_cached is False
    assert len(llm.calls) == 2  # New LLM call
    cand2 = rep4.candidates_created[0]
    assert cand2.candidate_id != cand1.candidate_id
    assert len(cand_store_reopen.list_candidates()) == 2


# ==================== D7: Revise Existing Skill & Target Version Drift ====================

def test_scenario_d7_revise_existing_skill_and_target_version_drift(tmp_path: Path):
    """D7: Revising existing skill and target version drift:

    - D7a: existing skill in registry sets decision to 'revise' and binds baseline version
    - D7b: LLM malformed output records 'abandon' without crashing
    - D7c: target skill version bump in registry invalidates old cached fingerprint
    """
    db_path = tmp_path / "test_d7.db"
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "target_tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("target_tool", "1.0.0"), encoding="utf-8")

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    # D7a: Existing skill triggers revise
    for i in range(1, 4):
        ep_store.save_episode(_make_episode(f"task_{i}", f"run_{i}", skill_name="target_tool", query=f"target action {i}"))

    llm_revise = FakeLLM([_valid_skill_md("target_tool", "1.1.0")])
    rep_a = mine_pending(ep_store, cand_store, registry=reg, llm=llm_revise)
    assert len(rep_a.candidates_revised) == 1
    assert rep_a.clusters[0].decision == "revise"
    cand = rep_a.candidates_revised[0]
    assert cand.decision == "revise"
    assert cand.skill_name == "target_tool"

    # D7b: LLM malformed output gracefully records abandon
    db_b = tmp_path / "test_d7b.db"
    ep_b = EpisodeStore(db_b)
    cand_b = CandidateStore(db_b, episode_store=ep_b)
    for i in range(1, 4):
        ep_b.save_episode(_make_episode(f"task_{i}", f"run_{i}", query=f"action {i}"))

    llm_malformed = FakeLLM(["NOT_A_VALID_SKILL_MD_OUTPUT"])
    rep_b = mine_pending(ep_b, cand_b, llm=llm_malformed)
    assert len(rep_b.candidates_created) == 0
    assert len(rep_b.abstained_clusters) == 1
    assert rep_b.abstained_clusters[0].decision == "abandon"
    assert len(cand_b.list_candidates()) == 0

    # D7c: Target skill version bump invalidates old batch fingerprint
    # Update target_tool to 2.0.0 in registry
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("target_tool", "2.0.0"), encoding="utf-8")
    reg_v2 = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg_v2.load_skills_from_dir()
    assert reg_v2.get_meta("target_tool").version == "2.0.0"

    llm_drift = FakeLLM([_valid_skill_md("target_tool", "2.1.0")])
    rep_c = mine_pending(ep_store, cand_store, registry=reg_v2, llm=llm_drift)
    assert rep_c.clusters[0].is_cached is False  # Fingerprint changed because baseline_version is 2.0.0
    assert len(llm_drift.calls) == 1


# ==================== D8: E2E M3a Collector to M3b Mining ====================

def test_scenario_d8_e2e_m3a_collector_to_m3b_mining(tmp_path: Path):
    """D8: End-to-end integration:

    SkillEvaluator.evaluate_skill with purpose='learning' and collector auto-persists
    episodes in SQLite -> mine_pending discovers and synthesizes isolated draft candidate.
    """
    db_path = tmp_path / "test_d8.db"
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "pipeline_tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_valid_skill_md("pipeline_tool", "1.0.0"), encoding="utf-8")

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    collector = ExperienceCollector(episode_store=ep_store, registry=reg)

    execution_llm = FakeLLM([
        "bare_output_1", "Pipeline step 1 finished",
        "bare_output_2", "Pipeline step 2 finished",
        "bare_output_3", "Pipeline step 3 finished",
    ])
    judge_llm = FakeLLM([
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
        json.dumps({"verdict": "A_better", "reason_codes": ["OK"], "evidence_summary": "good"}),
    ])

    evaluator = SkillEvaluator(registry=reg, llm=execution_llm, judge_llm=judge_llm)
    evaluator._injected_provenances = [
        _make_provenance("etl_extract", 0),
        _make_provenance("etl_load", 1),
    ]

    # Auto-collect 3 learning episodes through production evaluate_skill entry point
    tasks = [
        ("task_etl_1", "run_etl_1", "execute etl daily batch"),
        ("task_etl_2", "run_etl_2", "execute etl hourly delta"),
        ("task_etl_3", "run_etl_3", "execute etl historical sync"),
    ]
    for tid, rid, q in tasks:
        res = evaluator.evaluate_skill(
            "pipeline_tool",
            eval_set="baseline_dev",
            cases=[{"id": f"c_{rid}", "query": q, "reference": "success"}],
            p0_ids=[],
            collector=collector,
            run_id=rid,
            task_id=tid,
            purpose="learning",
        )
        assert res.valid is True
        assert res.p0_pass is True

    # Verify 3 episodes persisted automatically in SQLite
    persisted_eps = ep_store.list_episodes()
    assert len(persisted_eps) == 3
    assert all(ep.outcome == "success" for ep in persisted_eps)
    assert all(ep.environment.get("purpose") == "learning" for ep in persisted_eps)

    # Now run M3b mine_pending over the auto-collected pool
    miner_llm = FakeLLM([_valid_skill_md("pipeline_tool", "1.1.0")])
    batch_report = mine_pending(
        episode_store=ep_store,
        candidate_store=cand_store,
        registry=reg,
        llm=miner_llm,
    )

    assert batch_report.total_episodes_scanned == 3
    assert batch_report.learning_episodes_count == 3
    assert batch_report.unique_tasks == 3
    assert len(batch_report.candidates_revised) == 1
    assert len(batch_report.abstained_clusters) == 0

    cand = batch_report.candidates_revised[0]
    assert cand.skill_name == "pipeline_tool"
    assert cand.decision == "revise"
    assert cand.status == "DRAFT"
    assert set(cand.source_episode_ids) == {"ep_run_etl_1", "ep_run_etl_2", "ep_run_etl_3"}

    # Registry remains untouched at 1.0.0
    meta = reg.get_meta("pipeline_tool")
    assert meta.version == "1.0.0"
