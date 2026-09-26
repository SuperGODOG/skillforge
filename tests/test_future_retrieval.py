"""Phase P3 Acceptance Test Suite: Future Retrieval & Memory Context Retrieval

Covers Supervisor Scenarios R1 - R6:
- R1: Tiered and hybrid retrieval boundary:
      Keywords placed across Semantic, Episodic, and Procedural memory;
      querying by specific tier returns only that tier; hybrid query preserves
      distinct types and ID prefixes; Episode is never treated as a formal Skill.
- R2: Provenance chains and conflict preservation:
      Retrieves Episode -> Candidate -> Formal Skill and
      DocumentSnippet -> Candidate -> Verification Episode -> Formal Skill chains;
      returns exact source IDs, document version/snippets, verification status;
      broken lineages are explicitly flagged; conflicting facts retain sources and surface as conflicts.
- R3: Contextual version binding, rollback, and permission/dependency filtering:
      Same-name skill with versions and rollback; skill requiring unauthorized tool or missing dependency;
      retrieval with task version and permissions context recommends only the applicable, verified,
      active version; blocks unauthorized or stale/rolled-back versions.
- R4: Evaluation and failure isolation & unpromoted candidate guard:
      learning vs evaluation episodes matching same term; evaluation excluded from positive evidence;
      failed episodes excluded from positive success evidence; unpromoted candidates cannot appear as formal skills.
- R5: Deterministic multi-word scoring, ranking, and explainable empty reasons:
      Multi-word token scoring with stable tie-breaking; explainable match and filter reasons;
      bounded limits; explicit empty reasons when no match or filtered out.
- R6: Read-only and zero side-effects guarantee:
      Retrieval executes 0 tools, performs 0 candidate promotions, 0 registry mutations,
      0 version switches, and creates 0 new episodes.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
import pytest

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge import (
    AgentRuntime,
    CandidateSkill,
    CandidateStore,
    DeploymentManager,
    DocumentSnippet,
    DocumentSource,
    DocumentStore,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    FutureMemoryRetriever,
    FutureRetrievalResult,
    MemoryLineage,
    Release,
    ReleaseStateMachine,
    RetrievalContext,
    SemanticConflict,
    SemanticFact,
    SemanticStore,
    SkillMeta,
    SkillRecommendation,
    SkillRegistry,
    ThreeTierMemoryManager,
    ToolBroker,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
    compute_candidate_hash,
    extract_candidate_from_document,
    parse_markdown_snippets,
    promote_candidate,
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


def init_git_repo(repo_root: Path) -> None:
    """Initialize a bare git repository for releases."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "skills").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), check=True, capture_output=True)
    readme = repo_root / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_root), check=True, capture_output=True)


# ==================== R1: Tiered and Hybrid Retrieval Boundary ====================


def test_scenario_r1_tiered_and_hybrid_retrieval_boundary(tmp_path: Path):
    """R1: Keywords across 3 tiers; tier query returns only that tier; hybrid query preserves types & ID prefixes; Episode never treated as formal Skill."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)
    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg)

    kw = "cluster_manager"

    # 1. Semantic Memory entry
    fact = SemanticFact(
        fact_id="fact_cluster_1",
        statement="cluster_manager nodes limit is 100",
        source_id="run_config_probe",
        scope="prod",
        topic="cluster_manager",
    )
    mem_mgr.semantic_store.save_fact(fact)

    # 2. Episodic Memory entry
    prov = ToolCallProvenance(
        tool_name="calculator",
        fixture_case_id="case_1",
        call_index=1,
        call_count=1,
        is_fixture=False,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"a": 1, "b": 2},
        output_status="SUCCESS",
        output_summary="3",
        latency_ms=5.0,
        timestamp="2026-09-25T12:00:00Z",
        signature="sig_1",
    )
    ep = Episode(
        episode_id="ep_cluster_1",
        task_id="task_cluster_1",
        run_id="run_cluster_1",
        skill_name="cluster_manager",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={"goal": "manage cluster"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep)

    # 3. Procedural Memory: a DRAFT candidate
    meta_cand = SkillMeta(
        name="cluster_manager_draft",
        version="0.1.0",
        description="Draft cluster_manager steps",
        use_when="When clustering",
        trigger=Trigger(keywords=["cluster_manager"]),
    )
    cand = CandidateSkill(
        candidate_id="cand_cluster_1",
        skill_name="cluster_manager_draft",
        decision="create",
        source_episode_ids=["ep_cluster_1"],
        meta=meta_cand,
        body="## Instructions\n1. Manage cluster.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand)

    # 4. Procedural Memory: a Formal Published Skill
    meta_skill = SkillMeta(
        name="cluster_manager",
        version="1.0.0",
        description="Formal cluster_manager procedure",
        use_when="When managing production clusters",
        trigger=Trigger(keywords=["cluster_manager"]),
    )
    cand_formal = CandidateSkill(
        candidate_id="cand_cluster_formal",
        skill_name="cluster_manager",
        decision="create",
        source_episode_ids=["ep_cluster_1"],
        meta=meta_skill,
        body="## Instructions\n1. Run cluster_manager operations.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_formal)
    val_rec = ValidationRecord(
        candidate_id=cand_formal.candidate_id,
        content_hash=compute_candidate_hash(cand_formal),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_cluster_1"],
    )
    mem_mgr.promote_candidate(
        candidate_id=cand_formal.candidate_id,
        validation_record=val_rec,
        state_machine=sm,
        caller_confirmed=True,
    )
    assert reg.has_skill("cluster_manager")

    # ==================== Verification ====================

    # Case 1: Query tier='semantic' returns ONLY SemanticFact
    res_sem = retriever.retrieve(kw, tier="semantic")
    assert len(res_sem.evidence_facts) == 1
    assert res_sem.evidence_facts[0].fact_id == "fact_cluster_1"
    assert res_sem.skills == []
    assert res_sem.evidence_episodes == []
    assert res_sem.candidates == []

    # Case 2: Query tier='episodic' returns ONLY Episode
    res_epi = retriever.retrieve(kw, tier="episodic")
    assert len(res_epi.evidence_episodes) == 1
    assert res_epi.evidence_episodes[0].episode_id == "ep_cluster_1"
    assert res_epi.skills == []
    assert res_epi.evidence_facts == []
    assert res_epi.candidates == []

    # Case 3: Query tier='procedural' returns formal skills and candidates
    res_proc = retriever.retrieve(kw, tier="procedural")
    assert len(res_proc.skills) == 1
    assert res_proc.skills[0].skill_name == "cluster_manager"
    assert res_proc.skills[0].version == "1.0.0"
    assert any(c.candidate_id == "cand_cluster_1" for c in res_proc.candidates)
    assert res_proc.evidence_facts == []
    assert res_proc.evidence_episodes == []

    # Case 4: Hybrid query (tier=None) preserves distinct types & ID prefixes
    res_hybrid = retriever.retrieve(kw, tier=None)
    # Semantic facts
    assert len(res_hybrid.evidence_facts) == 1
    assert res_hybrid.evidence_facts[0].fact_id.startswith("fact_")
    # Evidence episodes
    assert len(res_hybrid.evidence_episodes) == 1
    assert res_hybrid.evidence_episodes[0].episode_id.startswith("ep_")
    # Candidates
    assert any(c.candidate_id.startswith("cand_") for c in res_hybrid.candidates)
    # Formal skills
    assert len(res_hybrid.skills) == 1
    assert isinstance(res_hybrid.skills[0], SkillRecommendation)
    assert res_hybrid.skills[0].skill_name == "cluster_manager"

    # Invariant: Episode is NEVER treated as a formal Skill!
    for rec in res_hybrid.skills:
        assert not rec.skill_name.startswith("ep_")
        assert not isinstance(rec, Episode)

    mem_mgr.close()
    sm.close()


# ==================== R2: Provenance Chains & Conflict Preservation ====================


def test_scenario_r2_provenance_chains_and_conflict_preservation(tmp_path: Path):
    """R2: Retrieve Episode-mined and Document-derived chains; surface broken lineages and fact conflicts."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)
    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg)

    # 1. Chain 1: Episode -> Candidate -> Formal Skill
    prov1 = ToolCallProvenance(
        tool_name="calculator",
        fixture_case_id="case_mined",
        call_index=1,
        call_count=1,
        is_fixture=False,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"a": 5, "b": 5},
        output_status="SUCCESS",
        output_summary="10",
        latency_ms=8.0,
        timestamp="2026-09-25T12:00:00Z",
        signature="sig_mined",
    )
    ep_mined = Episode(
        episode_id="ep_mined_101",
        task_id="task_mined_101",
        run_id="run_mined_101",
        skill_name="mined_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov1],
        acceptance_criteria={"goal": "addition"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep_mined)

    meta_mined = SkillMeta(
        name="mined_skill",
        version="1.0.0",
        description="Mined skill procedure",
        use_when="When calculating",
        trigger=Trigger(keywords=["mined_skill"]),
    )
    cand_mined = CandidateSkill(
        candidate_id="cand_mined_101",
        skill_name="mined_skill",
        decision="create",
        source_episode_ids=["ep_mined_101"],
        meta=meta_mined,
        body="## Instructions\n1. Add numbers.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_mined)
    val_mined = ValidationRecord(
        candidate_id=cand_mined.candidate_id,
        content_hash=compute_candidate_hash(cand_mined),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_mined_101"],
    )
    mem_mgr.promote_candidate(
        candidate_id=cand_mined.candidate_id,
        validation_record=val_mined,
        state_machine=sm,
        caller_confirmed=True,
    )

    # 2. Chain 2: DocumentSnippet -> Candidate -> Verification Episode -> Formal Skill
    doc_text = """# Doc Skill Guide
Official document for document skill.

## Instructions
1. Call tool calculator with a=2 and b=3.
2. Confirm output is 5.
"""
    doc_id = "doc_guide_202"
    doc_ver = "1.0.0"
    snippets = parse_markdown_snippets(doc_text, doc_id=doc_id, doc_version=doc_ver)
    doc_source = DocumentSource(
        doc_id=doc_id,
        title="Doc Skill Guide",
        version=doc_ver,
        content=doc_text,
        content_hash=hashlib.sha256(doc_text.encode("utf-8")).hexdigest(),
        snippets=snippets,
    )
    mem_mgr.ingest_document(doc_source)

    extract_res = mem_mgr.extract_candidate_from_document(doc_source, target_skill_name="doc_skill")
    cand_doc = extract_res.candidate
    assert cand_doc is not None

    # Real verification episode
    ep_verify = Episode(
        episode_id="ep_verify_202",
        task_id="task_verify_202",
        run_id="run_verify_202",
        skill_name="doc_skill",
        skill_version="1.0.0",
        environment={"purpose": "verification"},
        provenances=[prov1],
        acceptance_criteria={"goal": "verify doc skill"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep_verify)

    val_doc = ValidationRecord(
        candidate_id=cand_doc.candidate_id,
        content_hash=compute_candidate_hash(cand_doc),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_verify_202"],
    )
    mem_mgr.promote_candidate(
        candidate_id=cand_doc.candidate_id,
        validation_record=val_doc,
        state_machine=sm,
        caller_confirmed=True,
    )

    # 3. Chain 3: Broken lineage
    meta_broken = SkillMeta(
        name="broken_skill",
        version="1.0.0",
        description="Skill with missing source episode",
        use_when="When testing broken chains",
        trigger=Trigger(keywords=["broken_skill"]),
    )
    ep_to_delete = Episode(
        episode_id="ep_to_delete_303",
        task_id="task_delete_303",
        run_id="run_delete_303",
        skill_name="broken_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov1],
        acceptance_criteria={"goal": "broken"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep_to_delete)

    cand_broken = CandidateSkill(
        candidate_id="cand_broken_303",
        skill_name="broken_skill",
        decision="create",
        source_episode_ids=["ep_to_delete_303"],
        meta=meta_broken,
        body="## Instructions\n1. Do something.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_broken)
    val_broken = ValidationRecord(
        candidate_id=cand_broken.candidate_id,
        content_hash=compute_candidate_hash(cand_broken),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_to_delete_303"],
    )
    mem_mgr.promote_candidate(
        candidate_id=cand_broken.candidate_id,
        validation_record=val_broken,
        state_machine=sm,
        caller_confirmed=True,
    )

    # Now simulate episode loss in underlying store to cause broken lineage
    conn = mem_mgr.episode_store._get_conn()
    conn.execute("DELETE FROM episodes WHERE episode_id = 'ep_to_delete_303'")
    conn.commit()

    # 4. Fact conflicts on topic 'cache_ttl'
    fact_a = SemanticFact(
        fact_id="fact_ttl_a",
        statement="cache_ttl is 30 seconds",
        source_id="probe_run_a",
        scope="cluster_east",
        topic="cache_ttl",
    )
    fact_b = SemanticFact(
        fact_id="fact_ttl_b",
        statement="cache_ttl is 60 seconds",
        source_id="config_file_b",
        scope="cluster_east",
        topic="cache_ttl",
    )
    mem_mgr.semantic_store.save_fact(fact_a)
    mem_mgr.semantic_store.save_fact(fact_b)

    # ==================== Verification ====================

    # Verify Chain 1 (Episode-mined)
    res_mined = retriever.retrieve("mined_skill")
    assert len(res_mined.skills) == 1
    rec_m = res_mined.skills[0]
    assert rec_m.source_type == "episode_mined"
    assert rec_m.is_verified is True
    assert rec_m.lineage is not None
    assert any(e.episode_id == "ep_mined_101" for e in rec_m.lineage.supporting_episodes)
    assert rec_m.lineage_broken is False

    # Verify Chain 2 (Document-derived)
    res_doc = retriever.retrieve("doc_skill")
    assert len(res_doc.skills) == 1
    rec_d = res_doc.skills[0]
    assert rec_d.source_type == "document_derived"
    assert rec_d.is_verified is True
    assert rec_d.lineage is not None
    assert rec_d.lineage.source_document is not None
    assert rec_d.lineage.source_document.doc_id == "doc_guide_202"
    assert len(rec_d.lineage.source_snippets) > 0
    assert any(e.episode_id == "ep_verify_202" for e in rec_d.verification_episodes)
    assert rec_d.lineage_broken is False

    # Verify Chain 3 (Broken lineage flagged)
    res_broken = retriever.retrieve("broken_skill")
    assert len(res_broken.skills) == 1
    rec_b = res_broken.skills[0]
    assert rec_b.lineage_broken is True
    assert any("Missing supporting episode" in r for r in rec_b.broken_reasons)

    # Verify Fact Conflicts preserved and surfaced
    res_conflict = retriever.retrieve("cache_ttl", tier="semantic")
    assert len(res_conflict.evidence_facts) == 2
    assert len(res_conflict.conflicts) > 0
    conflict = res_conflict.conflicts[0]
    assert conflict.topic == "cache_ttl"
    assert len(conflict.facts) == 2
    sources = {f.source_id for f in conflict.facts}
    assert sources == {"probe_run_a", "config_file_b"}

    mem_mgr.close()
    sm.close()


# ==================== R3: Contextual Version Binding & Guards ====================


def test_scenario_r3_version_binding_rollback_and_permission_dependency_guards(tmp_path: Path):
    """R3: Contextual task version binding; rollback inactive versions; permission & dependency filtering."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    dm = DeploymentManager(db_path=db_path, repo_root=repo_root, skills_dir=repo_root / "skills", registry=reg)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg, deployment_manager=dm)
    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg, deployment_manager=dm)

    # 1. Setup multi-version skill: calc_service (v1.0.0 stable, v1.1.0 canary, then rolled back)
    meta_v1 = SkillMeta(
        name="calc_service",
        version="1.0.0",
        description="Version 1.0.0 of calculator service",
        use_when="When calculating v1",
        trigger=Trigger(keywords=["calc_service"]),
    )
    cand_v1 = CandidateSkill(
        candidate_id="cand_calc_v1",
        skill_name="calc_service",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_calc",
        meta=meta_v1,
        body="## Instructions\n1. Calc v1.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_v1)
    val_v1 = ValidationRecord(
        candidate_id=cand_v1.candidate_id,
        content_hash=compute_candidate_hash(cand_v1),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_v1.candidate_id, val_v1, sm, caller_confirmed=True)

    meta_v2 = SkillMeta(
        name="calc_service",
        version="1.1.0",
        description="Version 1.1.0 of calculator service",
        use_when="When calculating v2",
        trigger=Trigger(keywords=["calc_service"]),
    )
    cand_v2 = CandidateSkill(
        candidate_id="cand_calc_v2",
        skill_name="calc_service",
        decision="revise",
        source_episode_ids=[],
        source_doc_id="doc_calc",
        meta=meta_v2,
        body="## Instructions\n1. Calc v1.1.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_v2)
    val_v2 = ValidationRecord(
        candidate_id=cand_v2.candidate_id,
        content_hash=compute_candidate_hash(cand_v2),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_v2.candidate_id, val_v2, sm, caller_confirmed=True)

    # Initialize deployment and set canary 30%
    dm.get_deployment("calc_service")
    dm.set_canary("calc_service", candidate_or_version="1.1.0", share=30, caller_confirmed=True)

    # Freeze run_bound to 1.0.0 in run_version_bindings
    conn = dm._get_conn()
    conn.execute(
        """INSERT INTO run_version_bindings (run_id, skill_name, assigned_version, content_hash, is_canary, frozen_body)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("run_bound_100", "calc_service", "1.0.0", "hash_100", 0, "body_v1"),
    )
    conn.commit()

    # Query with run_id='run_bound_100' MUST recommend assigned_version '1.0.0'
    ctx_bound = RetrievalContext(run_id="run_bound_100")
    res_bound = retriever.retrieve("calc_service", context=ctx_bound)
    assert len(res_bound.skills) == 1
    assert res_bound.skills[0].version == "1.0.0"

    # Now simulate rollback of calc_service back to 1.0.0
    dm.rollback_deployment("calc_service", target_version="1.0.0", reason="Canary regression", caller_confirmed=True)
    dep_after_rollback = dm.get_deployment("calc_service")
    assert dep_after_rollback.stable_version == "1.0.0"
    assert dep_after_rollback.canary_version is None

    # New task retrieval without bindings MUST recommend active stable version 1.0.0, NOT 1.1.0
    res_active = retriever.retrieve("calc_service", context=RetrievalContext())
    assert len(res_active.skills) == 1
    assert res_active.skills[0].version == "1.0.0"

    # 2. Setup skill with unauthorized tool dependency
    meta_admin = SkillMeta(
        name="admin_tool_skill",
        version="1.0.0",
        description="Requires admin tool",
        use_when="Admin operations",
        dependencies=["restricted_shell"],
        trigger=Trigger(keywords=["admin_tool_skill"]),
    )
    cand_admin = CandidateSkill(
        candidate_id="cand_admin_01",
        skill_name="admin_tool_skill",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_admin",
        meta=meta_admin,
        body="## Instructions\n1. Run admin shell.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_admin)
    val_admin = ValidationRecord(
        candidate_id=cand_admin.candidate_id,
        content_hash=compute_candidate_hash(cand_admin),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_admin.candidate_id, val_admin, sm, caller_confirmed=True)

    # Context without 'restricted_shell' MUST filter out admin_tool_skill
    ctx_unauth = RetrievalContext(allowed_tools={"calculator"})
    res_unauth = retriever.retrieve("admin_tool_skill", context=ctx_unauth)
    assert res_unauth.skills == []
    assert any(
        f.get("skill_name") == "admin_tool_skill" and "restricted_shell" in f.get("reason", "")
        for f in res_unauth.filtered_out
    )

    # Context with 'restricted_shell' allowed CAN recommend it
    ctx_auth = RetrievalContext(allowed_tools={"calculator", "restricted_shell"})
    res_auth = retriever.retrieve("admin_tool_skill", context=ctx_auth)
    assert len(res_auth.skills) == 1
    assert res_auth.skills[0].skill_name == "admin_tool_skill"

    # 3. Setup skill with missing physical dependency
    meta_gpu = SkillMeta(
        name="gpu_math_skill",
        version="1.0.0",
        description="Requires GPU acceleration",
        use_when="GPU operations",
        dependencies=["cuda_runtime"],
        trigger=Trigger(keywords=["gpu_math_skill"]),
    )
    cand_gpu = CandidateSkill(
        candidate_id="cand_gpu_01",
        skill_name="gpu_math_skill",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_gpu",
        meta=meta_gpu,
        body="## Instructions\n1. Run CUDA.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_gpu)
    val_gpu = ValidationRecord(
        candidate_id=cand_gpu.candidate_id,
        content_hash=compute_candidate_hash(cand_gpu),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_gpu.candidate_id, val_gpu, sm, caller_confirmed=True)

    # Available dependencies missing cuda_runtime
    ctx_cpu_only = RetrievalContext(available_dependencies={"cpu", "python"})
    res_gpu = retriever.retrieve("gpu_math_skill", context=ctx_cpu_only)
    assert res_gpu.skills == []
    assert any(
        f.get("skill_name") == "gpu_math_skill" and "cuda_runtime" in f.get("reason", "")
        for f in res_gpu.filtered_out
    )

    mem_mgr.close()
    dm.close()
    sm.close()


# ==================== R4: Evaluation & Failure Isolation ====================


def test_scenario_r4_evaluation_and_failure_isolation_and_unpromoted_candidates(tmp_path: Path):
    """R4: Evaluation episodes cannot serve as positive evidence; failures excluded from success evidence; unpromoted candidates cannot appear as formal skills."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)
    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg)

    kw = "payment_processor"

    prov = ToolCallProvenance(
        tool_name="calculator",
        fixture_case_id="case_p",
        call_index=1,
        call_count=1,
        is_fixture=False,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"amount": 100},
        output_status="SUCCESS",
        output_summary="charged",
        latency_ms=10.0,
        timestamp="2026-09-25T12:00:00Z",
        signature="sig_p",
    )

    # 1. Valid learning success episode
    ep_learn_succ = Episode(
        episode_id="ep_pay_learn_succ",
        task_id="task_pay_1",
        run_id="run_pay_1",
        skill_name="payment_processor",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={"goal": "charge"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep_learn_succ)

    # 2. Evaluation episode (must NOT be used as positive learning evidence)
    ep_eval_succ = Episode(
        episode_id="ep_pay_eval_succ",
        task_id="task_pay_2",
        run_id="run_pay_2",
        skill_name="payment_processor",
        skill_version="1.0.0",
        environment={"purpose": "evaluation"},
        provenances=[prov],
        acceptance_criteria={"goal": "charge"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    mem_mgr.episode_store.save_episode(ep_eval_succ)

    # 3. Learning failure episode (must NOT be counted as success evidence)
    ep_learn_fail = Episode(
        episode_id="ep_pay_learn_fail",
        task_id="task_pay_3",
        run_id="run_pay_3",
        skill_name="payment_processor",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={"goal": "charge"},
        outcome="failure",
        outcome_reason="Network timeout during charge",
    )
    mem_mgr.episode_store.save_episode(ep_learn_fail)

    # 4. Unpromoted DRAFT candidate
    meta_cand = SkillMeta(
        name="payment_processor_draft",
        version="0.1.0",
        description="Draft payment flow",
        use_when="Payment draft",
        trigger=Trigger(keywords=["payment_processor"]),
    )
    cand_draft = CandidateSkill(
        candidate_id="cand_pay_draft_01",
        skill_name="payment_processor_draft",
        decision="create",
        source_episode_ids=["ep_pay_learn_succ"],
        meta=meta_cand,
        body="## Instructions\n1. Process payment.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_draft)

    # 5. Formal published skill
    meta_formal = SkillMeta(
        name="payment_processor",
        version="1.0.0",
        description="Formal verified payment processing",
        use_when="When charging payments",
        trigger=Trigger(keywords=["payment_processor"]),
    )
    cand_formal = CandidateSkill(
        candidate_id="cand_pay_formal_01",
        skill_name="payment_processor",
        decision="create",
        source_episode_ids=["ep_pay_learn_succ"],
        meta=meta_formal,
        body="## Instructions\n1. Verified payment.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_formal)
    val_formal = ValidationRecord(
        candidate_id=cand_formal.candidate_id,
        content_hash=compute_candidate_hash(cand_formal),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_pay_learn_succ"],
    )
    mem_mgr.promote_candidate(cand_formal.candidate_id, val_formal, sm, caller_confirmed=True)

    # ==================== Verification ====================

    res = retriever.retrieve(kw)

    # 1. Formal skills contains ONLY payment_processor (cand_pay_draft_01 must NOT appear!)
    assert len(res.skills) == 1
    assert res.skills[0].skill_name == "payment_processor"
    assert not any(s.skill_name == "payment_processor_draft" for s in res.skills)

    # 2. Candidate appears in filtered_out
    assert any(
        f.get("item_id") == "cand_pay_draft_01" and "DRAFT" in f.get("reason", "")
        for f in res.filtered_out
    )

    # 3. Evidence episodes contains ONLY ep_pay_learn_succ
    # ep_pay_eval_succ (evaluation) and ep_pay_learn_fail (failure) are EXCLUDED
    ep_ids = [e.episode_id for e in res.evidence_episodes]
    assert "ep_pay_learn_succ" in ep_ids
    assert "ep_pay_eval_succ" not in ep_ids
    assert "ep_pay_learn_fail" not in ep_ids

    # 4. Evaluation and failure episodes are logged in filtered_out
    assert any(
        f.get("item_id") == "ep_pay_eval_succ" and "Evaluation episode" in f.get("reason", "")
        for f in res.filtered_out
    )
    assert any(
        f.get("item_id") == "ep_pay_learn_fail" and "cannot count as positive success" in f.get("reason", "")
        for f in res.filtered_out
    )

    mem_mgr.close()
    sm.close()


# ==================== R5: Deterministic Scoring, Ranking & Empty Reasons ====================


def test_scenario_r5_deterministic_scoring_ranking_and_empty_reasons(tmp_path: Path):
    """R5: Multi-word query, deterministic scoring, stable tie-breaking, bounded limits, explainable empty reasons."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg)
    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg)

    # Setup 3 formal skills:
    # Skill A: fast_math_calculator (name matches fast & calculator, desc matches arithmetic)
    meta_a = SkillMeta(
        name="fast_math_calculator",
        version="1.0.0",
        description="Rapid arithmetic calculations and math operations",
        use_when="Fast calculations",
        trigger=Trigger(keywords=["calculator", "fast"]),
    )
    cand_a = CandidateSkill(
        candidate_id="cand_a",
        skill_name="fast_math_calculator",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_a",
        meta=meta_a,
        body="## Instructions\n1. Fast compute.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_a)
    val_a = ValidationRecord(
        candidate_id=cand_a.candidate_id,
        content_hash=compute_candidate_hash(cand_a),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_a.candidate_id, val_a, sm, caller_confirmed=True)

    # Skill B: slow_math_calculator (desc matches arithmetic, name does not match fast)
    meta_b = SkillMeta(
        name="slow_math_calculator",
        version="1.0.0",
        description="High precision arithmetic calculations and math operations",
        use_when="Precise calculations",
        trigger=Trigger(keywords=["calculator", "slow"]),
    )
    cand_b = CandidateSkill(
        candidate_id="cand_b",
        skill_name="slow_math_calculator",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_b",
        meta=meta_b,
        body="## Instructions\n1. Slow compute.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_b)
    val_b = ValidationRecord(
        candidate_id=cand_b.candidate_id,
        content_hash=compute_candidate_hash(cand_b),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_b.candidate_id, val_b, sm, caller_confirmed=True)

    # Skill C: text_formatter (unrelated to math/calculator)
    meta_c = SkillMeta(
        name="text_formatter",
        version="1.0.0",
        description="String and text formatting tools",
        use_when="Formatting text",
        trigger=Trigger(keywords=["format", "text"]),
    )
    cand_c = CandidateSkill(
        candidate_id="cand_c",
        skill_name="text_formatter",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_c",
        meta=meta_c,
        body="## Instructions\n1. Format text.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand_c)
    val_c = ValidationRecord(
        candidate_id=cand_c.candidate_id,
        content_hash=compute_candidate_hash(cand_c),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand_c.candidate_id, val_c, sm, caller_confirmed=True)

    # ==================== Verification ====================

    # 1. Multi-word query: "fast arithmetic operations"
    res = retriever.retrieve("fast arithmetic operations")
    assert len(res.skills) >= 2
    # Skill A should score significantly higher than Skill B
    assert res.skills[0].skill_name == "fast_math_calculator"
    assert res.skills[1].skill_name == "slow_math_calculator"
    assert res.skills[0].relevance_score > res.skills[1].relevance_score
    assert len(res.skills[0].match_reasons) > 0
    assert any("Name contains 'fast'" in r for r in res.skills[0].match_reasons)

    # 2. Limit enforcement
    res_limited = retriever.retrieve("fast arithmetic operations", context=RetrievalContext(limit=1))
    assert len(res_limited.skills) == 1
    assert res_limited.skills[0].skill_name == "fast_math_calculator"

    # 3. Explainable empty reason on no match
    res_empty = retriever.retrieve("quantum_warp_drive")
    assert res_empty.skills == []
    assert res_empty.empty_reason is not None
    assert "No formal skills matched query 'quantum_warp_drive'" in res_empty.empty_reason

    mem_mgr.close()
    sm.close()


# ==================== R6: Read-Only & Zero Side-Effects Guarantee ====================


def test_scenario_r6_read_only_and_zero_side_effects_guarantee(tmp_path: Path):
    """R6: Retrieval executes 0 tools, performs 0 candidate promotions, 0 registry mutations, 0 version switches, and 0 new episodes."""
    db_path = tmp_path / "skillforge.db"
    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    reg = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    dm = DeploymentManager(db_path=db_path, repo_root=repo_root, skills_dir=repo_root / "skills", registry=reg)
    mem_mgr = ThreeTierMemoryManager(db_path=db_path, registry=reg, deployment_manager=dm)

    calc_tool = MockCalculatorTool()
    broker = ToolBroker(application_allowlist={"calculator"})
    broker.register_tool(calc_tool)

    # Setup initial skill
    meta = SkillMeta(
        name="readonly_test_skill",
        version="1.0.0",
        description="Testing read-only invariants",
        use_when="Read-only test",
        trigger=Trigger(keywords=["readonly_test_skill"]),
    )
    cand = CandidateSkill(
        candidate_id="cand_ro_01",
        skill_name="readonly_test_skill",
        decision="create",
        source_episode_ids=[],
        source_doc_id="doc_ro",
        meta=meta,
        body="## Instructions\n1. Readonly test.",
        status="DRAFT",
    )
    mem_mgr.candidate_store.save_candidate(cand)
    val = ValidationRecord(
        candidate_id=cand.candidate_id,
        content_hash=compute_candidate_hash(cand),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
    )
    mem_mgr.promote_candidate(cand.candidate_id, val, sm, caller_confirmed=True)
    dm.get_deployment("readonly_test_skill")

    # Record initial states
    initial_tool_calls = calc_tool.call_count
    initial_episodes = len(mem_mgr.episode_store.list_episodes())
    initial_candidates = len(mem_mgr.candidate_store.list_candidates())
    initial_cand_statuses = {c.candidate_id: c.status for c in mem_mgr.candidate_store.list_candidates()}
    initial_skills = reg.list_names()
    initial_deployments = {
        name: dm.get_deployment(name).stable_version for name in initial_skills
    }

    retriever = FutureMemoryRetriever(memory_manager=mem_mgr, registry=reg, deployment_manager=dm)

    # Perform multiple retrievals with varying queries and contexts
    res1 = retriever.retrieve("readonly_test_skill")
    assert len(res1.skills) == 1
    res2 = retriever.retrieve("calculator", context=RetrievalContext(limit=5))
    res3 = retriever.retrieve("nonexistent_task", tier="episodic")
    res4 = retriever.retrieve("readonly_test_skill", tier="semantic")

    # Assert ZERO side-effects across all dimensions
    assert calc_tool.call_count == initial_tool_calls  # 0 tool invocations
    assert len(mem_mgr.episode_store.list_episodes()) == initial_episodes  # 0 new episodes
    assert len(mem_mgr.candidate_store.list_candidates()) == initial_candidates  # 0 candidates added
    assert {c.candidate_id: c.status for c in mem_mgr.candidate_store.list_candidates()} == initial_cand_statuses  # 0 status mutations
    assert reg.list_names() == initial_skills  # 0 registry modifications
    current_deployments = {
        name: dm.get_deployment(name).stable_version for name in initial_skills
    }
    assert current_deployments == initial_deployments  # 0 version switches

    mem_mgr.close()
    dm.close()
    sm.close()
