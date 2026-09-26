"""Milestone 5c Acceptance Test Suite: Three-Tier Memory Architecture & Provenance Linking

Covers Supervisor Scenarios C1 - C5:
- C1: Semantic fact access & source lineage:
      Writes >= 2 facts with source_id & scope; rejects marking single-run observation as unconditional
      universal fact; filters by source_id/scope returning verified source; enforces fact_ prefix.
- C2: Experience immutability & success/failure separation:
      Generates 1 success and 1 failure Episode from runtime entry point (AgentRuntime + ToolBroker + ExperienceCollector);
      checks immutability (rejects overwrite) and fields (task, timestamp, version, outcome, provenances);
      failure records are strictly 'failure', never treated as facts or registered into formal skills.
- C3: Procedural knowledge generation & controlled promotion:
      Forms CandidateSkill from learning episodes; promotes only with PASS verification gate and explicit
      caller confirmation (caller_confirmed=True); blocks unverified or unconfirmed candidates from registry;
      retains linkage to supporting episodes.
- C4: Three-tier memory boundaries & provenance tracing:
      Constructs matching keyword across Semantic, Episodic, and Procedural memory; typed retrieval returns
      strictly segregated types and distinct ID prefixes (fact_, ep_, cand_); trace_lineage navigates from
      candidate back to supporting episodes and source facts.
- C5: Isolation & conflict preservation:
      Evaluation episodes attempting to flow back into candidate learning are rejected (ValueError);
      purely failed experiences cannot produce successful procedural skills; contradictory observations on the
      same topic/attribute coexist without silent overwriting or arbitrary arbitration, and are explicitly surfaced.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
import pytest

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge import (
    AgentRuntime,
    CandidateSkill,
    CandidateStore,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    MemoryLineage,
    Release,
    ReleaseStateMachine,
    SemanticConflict,
    SemanticFact,
    SemanticStore,
    SkillMeta,
    SkillRegistry,
    ThreeTierMemoryManager,
    ToolBroker,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
    compute_candidate_hash,
)
from skillforge.models import RatchetVerdict


class MockCalculatorTool(Tool):
    """Simple calculator tool for runtime invocation."""

    def __init__(self):
        super().__init__(name="calculator", description="Performs basic arithmetic")
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="a", type="integer", required=True, description="First operand"),
            ToolParameter(name="b", type="integer", required=True, description="Second operand"),
            ToolParameter(name="op", type="string", required=False, default="add", description="Operation"),
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        a = parameters.get("a", 0)
        b = parameters.get("b", 0)
        op = parameters.get("op", "add")
        res = a + b if op == "add" else a - b
        return ToolResponse.success(text=f"Result: {res}", data={"result": res})


class DeterministicFakeLLM:
    """Fake LLM returning pre-packaged SKILL.md responses."""

    def __init__(self, target_skill: str = "demo_skill"):
        self.target_skill = target_skill

    def invoke(self, prompt: str, **kwargs) -> Any:
        content = f"""---
name: {self.target_skill}
version: 1.0.0
description: Mined procedural skill for {self.target_skill}
use_when: When handling {self.target_skill} tasks
not_for:
  - Other tasks
dependencies: []
trigger:
  keywords:
    - {self.target_skill}
examples:
  - run {self.target_skill}
evaluation:
  last_score: null
  last_release_id: null
---
# {self.target_skill}

## Overview
Procedural steps for {self.target_skill}.

## Instructions
1. Step one.
2. Step two.
"""
        return SimpleNamespace(content=content)


# ==================== C1: Semantic Fact Access & Source Lineage ====================


def test_scenario_c1_semantic_fact_access_and_source_lineage(tmp_path: Path):
    """C1: Semantic fact access, source provenance tracking, and universal fact guardrails."""
    db_path = tmp_path / "skillforge.db"
    store = SemanticStore(db_path)

    # 1. Enforce ID prefix 'fact_'
    with pytest.raises(ValueError, match="fact_id must start with 'fact_'"):
        SemanticFact(
            fact_id="invalid_prefix_001",
            statement="Database connection limit is 50",
            source_id="run_101",
        )

    # 2. Invariant: Single execution observation CANNOT be marked as unconditional universal fact
    with pytest.raises(ValueError, match="cannot be marked as an unconditional universal fact"):
        SemanticFact(
            fact_id="fact_single_exec_universal",
            statement="Server port is 8080",
            source_id="run_101",  # starts with 'run_'
            is_universal=True,
        )

    with pytest.raises(ValueError, match="cannot be marked as an unconditional universal fact"):
        SemanticFact(
            fact_id="fact_ep_exec_universal",
            statement="Server port is 8080",
            source_id="ep_task_001",  # starts with 'ep_'
            is_universal=True,
        )

    with pytest.raises(ValueError, match="cannot be marked as an unconditional universal fact"):
        SemanticFact(
            fact_id="fact_tagged_single_exec",
            statement="Server port is 8080",
            source_id="probe_tool",
            tags=["single_execution"],
            is_universal=True,
        )

    # 3. Write >= 2 facts with source_id and scope
    fact1 = SemanticFact(
        fact_id="fact_east_latency",
        statement="East cluster p99 latency is 45ms",
        source_id="run_perf_east",
        scope="cluster_east",
        topic="latency",
        tags=["perf", "east"],
    )
    fact2 = SemanticFact(
        fact_id="fact_west_latency",
        statement="West cluster p99 latency is 85ms",
        source_id="run_perf_west",
        scope="cluster_west",
        topic="latency",
        tags=["perf", "west"],
    )

    store.save_fact(fact1)
    store.save_fact(fact2)

    # 4. Duplicate ID conflict check
    with pytest.raises(ValueError, match="already exists"):
        store.save_fact(fact1, on_conflict="error")

    # 5. Retrieve by ID
    retrieved = store.get_fact("fact_east_latency")
    assert retrieved is not None
    assert retrieved.statement == "East cluster p99 latency is 45ms"
    assert retrieved.source_id == "run_perf_east"
    assert retrieved.scope == "cluster_east"
    assert retrieved.is_universal is False

    # 6. Filter by source_id
    east_facts = store.list_facts(source_id="run_perf_east")
    assert len(east_facts) == 1
    assert east_facts[0].fact_id == "fact_east_latency"

    # 7. Filter by scope
    west_facts = store.list_facts(scope="cluster_west")
    assert len(west_facts) == 1
    assert west_facts[0].fact_id == "fact_west_latency"

    store.close()


# ==================== C2: Episodic Immutability & Success/Failure Separation ====================


def test_scenario_c2_episodic_immutability_and_success_failure_separation(tmp_path: Path):
    """C2: Generate 1 success and 1 failure Episode from runtime entry point, verify immutability & boundaries."""
    db_path = tmp_path / "skillforge.db"
    calc_tool = MockCalculatorTool()
    broker = ToolBroker(application_allowlist={"calculator"})
    broker.register_tool(calc_tool)

    ep_store = EpisodeStore(db_path)
    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        episode_store=ep_store,
        collector=collector,
    )

    # 1. Generate Success Episode from existing runtime entry point
    run_succ = runtime.start_run(
        run_id="run_c2_succ",
        task_id="task_c2_succ",
        skill_name="calculator_skill",
        purpose="learning",
    )
    rec1 = runtime.execute_tool(
        run_id=run_succ.run_id,
        tool_name="calculator",
        parameters={"a": 10, "b": 20, "op": "add"},
    )
    assert rec1.status == "EXECUTED"

    term_run1, ep_succ = runtime.finalize_run(
        run_id=run_succ.run_id,
        model_output="30",
        verification_evidence={"independent_pass": True, "evidence": "10+20=30 verified"},
        acceptance_criteria={"expected": 30},
    )
    assert ep_succ is not None
    assert ep_succ.outcome == "success"
    assert ep_succ.task_id == "task_c2_succ"
    assert ep_succ.skill_name == "calculator_skill"
    assert ep_succ.created_at is not None
    assert len(ep_succ.provenances) == 1
    assert ep_succ.provenances[0].tool_name == "calculator"

    # 2. Generate Failure Episode from existing runtime entry point
    run_fail = runtime.start_run(
        run_id="run_c2_fail",
        task_id="task_c2_fail",
        skill_name="calculator_skill",
        purpose="learning",
    )
    rec2 = runtime.execute_tool(
        run_id=run_fail.run_id,
        tool_name="calculator",
        parameters={"a": 5, "b": 3, "op": "sub"},
    )
    assert rec2.status == "EXECUTED"

    term_run2, ep_fail = runtime.finalize_run(
        run_id=run_fail.run_id,
        model_output="999",
        verification_evidence={"independent_pass": False, "failure_reason": "Expected 2, got 999"},
        acceptance_criteria={"expected": 2},
    )
    assert ep_fail is not None
    assert ep_fail.outcome == "failure"
    assert ep_fail.task_id == "task_c2_fail"
    assert "Expected 2" in ep_fail.outcome_reason

    # 3. Immutability verification: attempting to overwrite existing episode is strictly rejected
    with pytest.raises(ValueError, match="already exists"):
        ep_store.save_episode(ep_succ, on_conflict="error")

    with pytest.raises(ValueError, match="already exists"):
        ep_store.save_episode(ep_fail, on_conflict="error")

    # 4. Failure separation: failure is distinct, not treated as a semantic fact or formal skill
    sem_store = SemanticStore(db_path)
    assert sem_store.get_fact(ep_fail.episode_id) is None
    assert sem_store.has_fact(ep_fail.episode_id) is False

    runtime.close()
    sem_store.close()


# ==================== C3: Procedural Generation & Controlled Promotion ====================


def test_scenario_c3_procedural_generation_and_controlled_promotion(tmp_path: Path):
    """C3: Form CandidateSkill from learning episodes; enforce validation gate and explicit caller confirmation."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "skills").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), check=True, capture_output=True)
    readme = repo_root / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_root), check=True, capture_output=True)

    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)

    # 1. Create a verified learning episode
    prov = ToolCallProvenance(
        tool_name="calc_tool",
        fixture_case_id="case_01",
        call_index=1,
        call_count=1,
        is_fixture=False,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"x": 1},
        output_status="SUCCESS",
        output_summary="calc pass",
        latency_ms=12.0,
        timestamp="2026-09-25T12:00:00Z",
        signature="sig_test",
    )
    ep = Episode(
        episode_id="ep_learn_c3",
        task_id="task_calc_learn",
        run_id="run_c3_001",
        skill_name="math_calc",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={"goal": "calculate"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep)

    # 2. Mine CandidateSkill from learning episode
    fake_llm = DeterministicFakeLLM("math_calc")
    mining_res = mem_mgr.create_candidate_from_episodes(
        episodes=[ep],
        target_skill_name="math_calc",
        llm=fake_llm,
    )
    assert mining_res.decision == "create"
    candidate = mining_res.candidate
    assert candidate is not None
    assert candidate.candidate_id.startswith("cand_")
    assert candidate.status == "DRAFT"
    assert candidate.source_episode_ids == ["ep_learn_c3"]

    # Invariant: candidate is in CandidateStore, NOT yet in active SkillRegistry
    assert mem_mgr.candidate_store.has_candidate(candidate.candidate_id)
    assert not reg.has_skill("math_calc")

    c_hash = compute_candidate_hash(candidate)

    # 3. Case 3a: Ratchet PASS but caller_confirmed=False -> strictly BLOCKED
    val_pass = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=c_hash,
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["Ratchet passed"]),
    )
    with pytest.raises(ValueError, match="requires explicit caller confirmation"):
        mem_mgr.promote_candidate(
            candidate_id=candidate.candidate_id,
            validation_record=val_pass,
            state_machine=sm,
            caller_confirmed=False,
        )
    assert not reg.has_skill("math_calc")

    # 4. Case 3b: Ratchet DECLINED with caller_confirmed=True -> strictly BLOCKED
    val_declined = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=c_hash,
        baseline_version=None,
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="DECLINED", reasons=["Regression detected"]),
    )
    with pytest.raises(ValueError, match="Only PASS verdict may be promoted"):
        mem_mgr.promote_candidate(
            candidate_id=candidate.candidate_id,
            validation_record=val_declined,
            state_machine=sm,
            caller_confirmed=True,
        )
    assert not reg.has_skill("math_calc")

    # 5. Case 3c: PASS verdict + caller_confirmed=True -> promotion succeeds
    release = mem_mgr.promote_candidate(
        candidate_id=candidate.candidate_id,
        validation_record=val_pass,
        state_machine=sm,
        caller_confirmed=True,
    )
    assert release.status == "PUBLISHED"
    assert release.skill_name == "math_calc"
    assert reg.has_skill("math_calc")
    assert (repo_root / "skills" / "math_calc" / "SKILL.md").exists()

    # Linkage to supporting episodes remains intact in CandidateStore
    stored_cand = mem_mgr.candidate_store.get_candidate(candidate.candidate_id)
    assert stored_cand is not None
    assert stored_cand.source_episode_ids == ["ep_learn_c3"]

    mem_mgr.close()
    sm.close()


# ==================== C4: Three-Tier Memory Boundaries & Lineage Tracing ====================


def test_scenario_c4_three_tier_memory_boundaries_and_lineage_tracing(tmp_path: Path):
    """C4: Segregated memory tiers with distinct ID prefixes and cross-tier lineage tracing."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "skills").mkdir()

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)

    common_keyword = "auth_token"

    # 1. Semantic Memory tier: scoped fact with source_id
    fact = SemanticFact(
        fact_id="fact_auth_spec_01",
        statement=f"Service requires {common_keyword} formatted as Bearer JWT",
        source_id="run_auth_probe_1",
        scope="auth_service",
        topic="authentication",
        tags=["auth", "spec"],
    )
    mem_mgr.semantic_store.save_fact(fact)

    # 2. Episodic Memory tier: execution episode with matching keyword
    ep = Episode(
        episode_id="ep_auth_run_1",
        task_id="task_validate_auth_token",
        run_id="run_auth_probe_1",
        skill_name="auth_handler",
        skill_version="1.0.0",
        environment={"purpose": "learning", "query": f"Fetch {common_keyword}"},
        provenances=[],
        acceptance_criteria={"goal": "verify token"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep)

    # 3. Procedural Memory tier: candidate skill with matching keyword
    meta = SkillMeta(
        name="auth_handler",
        version="1.0.0",
        description=f"Skill for handling {common_keyword}",
        use_when=f"Tasks involving {common_keyword}",
        trigger=Trigger(keywords=[common_keyword]),
    )
    cand = CandidateSkill(
        candidate_id="cand_auth_handler_01",
        skill_name="auth_handler",
        decision="create",
        source_episode_ids=["ep_auth_run_1"],
        meta=meta,
        body=f"# Auth Handler\nProcess {common_keyword} securely.",
        rationale=f"Derived from {common_keyword} runs.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand)

    # 4. Typed Search Segregation: no type or ID confusion
    sem_res = mem_mgr.search(common_keyword, tier="semantic")
    assert isinstance(sem_res, list)
    assert len(sem_res) == 1
    assert isinstance(sem_res[0], SemanticFact)
    assert sem_res[0].fact_id == "fact_auth_spec_01"
    assert sem_res[0].fact_id.startswith("fact_")

    epi_res = mem_mgr.search(common_keyword, tier="episodic")
    assert isinstance(epi_res, list)
    assert len(epi_res) == 1
    assert isinstance(epi_res[0], Episode)
    assert epi_res[0].episode_id == "ep_auth_run_1"
    assert epi_res[0].episode_id.startswith("ep_")

    pro_res = mem_mgr.search(common_keyword, tier="procedural")
    assert isinstance(pro_res, list)
    assert len(pro_res) == 1
    assert isinstance(pro_res[0], CandidateSkill)
    assert pro_res[0].candidate_id == "cand_auth_handler_01"
    assert pro_res[0].candidate_id.startswith("cand_")

    # Combined search returns cleanly partitioned dictionary
    all_res = mem_mgr.search(common_keyword, tier=None)
    assert isinstance(all_res, dict)
    assert len(all_res["semantic"]) == 1
    assert len(all_res["episodic"]) == 1
    assert len(all_res["procedural"]) == 1

    # 5. Provenance Lineage Tracing: Candidate -> Supporting Episode -> Source Semantic Fact
    lineage = mem_mgr.trace_lineage("cand_auth_handler_01")
    assert isinstance(lineage, MemoryLineage)
    assert lineage.procedural_id == "cand_auth_handler_01"
    assert lineage.procedural_type == "candidate"
    assert len(lineage.supporting_episodes) == 1
    assert lineage.supporting_episodes[0].episode_id == "ep_auth_run_1"

    # Lineage accurately ties back to semantic fact through source_id ('run_auth_probe_1')
    assert len(lineage.source_facts) == 1
    assert lineage.source_facts[0].fact_id == "fact_auth_spec_01"
    assert lineage.source_facts[0].source_id == "run_auth_probe_1"

    mem_mgr.close()


# ==================== C5: Isolation & Conflict Preservation ====================


def test_scenario_c5_isolation_and_conflict_preservation(tmp_path: Path):
    """C5: Reject evaluation data feedback, prevent failure-induced procedures, and preserve conflicting facts."""
    db_path = tmp_path / "skillforge.db"
    mem_mgr = ThreeTierMemoryManager(db_path=db_path)

    # 1. Subcase 5a: Evaluation data feedback is strictly rejected (A8 boundary)
    eval_ep = Episode(
        episode_id="ep_eval_sentinel",
        task_id="task_eval_sec",
        run_id="run_eval_sec",
        skill_name="sec_skill",
        skill_version="1.0.0",
        environment={"purpose": "evaluation", "sentinel": "SECRET_BENCHMARK_KEY"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(eval_ep)

    fake_llm = DeterministicFakeLLM("sec_skill")
    with pytest.raises(ValueError, match="evaluation episodes cannot flow back into learning"):
        mem_mgr.create_candidate_from_episodes(
            episodes=[eval_ep],
            target_skill_name="sec_skill",
            llm=fake_llm,
        )

    # 2. Subcase 5b: Failed experiences cannot produce successful procedural skills
    fail_ep = Episode(
        episode_id="ep_fail_c5",
        task_id="task_fail_c5",
        run_id="run_fail_c5",
        skill_name="new_proc_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="failure",
        verification_evidence={"independent_pass": False, "failure_reason": "Process crashed"},
    )
    mem_mgr.episode_store.save_episode(fail_ep)

    with pytest.raises(ValueError, match="failed experiences cannot produce successful procedural skills"):
        mem_mgr.create_candidate_from_episodes(
            episodes=[fail_ep],
            target_skill_name="new_proc_skill",
            llm=fake_llm,
        )

    # 3. Subcase 5c: Preserving conflicting observations across sources (no silent overwrite or auto-arbitration)
    obs_source_a = SemanticFact(
        fact_id="fact_timeout_node_a",
        statement="Database connection timeout is 10 seconds",
        source_id="probe_node_alpha",
        scope="vpc_alpha",
        topic="db_connection_timeout",
        tags=["config", "alpha"],
    )
    obs_source_b = SemanticFact(
        fact_id="fact_timeout_node_b",
        statement="Database connection timeout is 30 seconds",
        source_id="probe_node_beta",
        scope="vpc_beta",
        topic="db_connection_timeout",
        tags=["config", "beta"],
    )

    mem_mgr.semantic_store.save_fact(obs_source_a)
    mem_mgr.semantic_store.save_fact(obs_source_b)

    # Both facts must coexist in storage without overwriting or deleting
    assert mem_mgr.semantic_store.has_fact("fact_timeout_node_a")
    assert mem_mgr.semantic_store.has_fact("fact_timeout_node_b")

    all_timeout_facts = mem_mgr.semantic_store.list_facts(topic="db_connection_timeout")
    assert len(all_timeout_facts) == 2
    sources = {f.source_id for f in all_timeout_facts}
    assert sources == {"probe_node_alpha", "probe_node_beta"}

    # Explicit conflict exposure: detect_conflicts surfaces both facts without silent arbitration
    conflicts = mem_mgr.detect_conflicts(topic="db_connection_timeout")
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.topic == "db_connection_timeout"
    assert len(conflict.facts) == 2
    assert "probe_node_alpha" in conflict.description
    assert "probe_node_beta" in conflict.description

    mem_mgr.close()
