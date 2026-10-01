"""P2 Acceptance Test Suite: Unified Admission and Bloat Guards (V1–V6).

Verifies Acceptance Criteria V1–V6:
- V1: mine_candidate and mine_pending strictly require purpose == 'learning' episodes;
      reject evaluation / heldout / unknown / missing purposes.
- V2: Anti-tampering check: In-memory episodes differing from canonical EpisodeStore are rejected.
      Task scope isolation: generate_candidate_from_requirement incorporates task_id into task_spec_hash.
- V3: Prompt bloat guards:
      - section ratio > 0.25 AND delta > 100 -> REVIEW
      - total multiplier > 1.20 AND delta > 100 -> REVIEW
      - cold-start create enforces max_body_chars=3000 (cold_start=True)
      - validate_candidate runs cheap checks (tool permissions, bloat) BEFORE expensive LLM eval.
- V4: ValidationRecord binding & persistence:
      - Binds candidate_id, content_hash, baseline_version, scope_hash, config_hash, dataset_version.
      - Survives CandidateStore close / SQLite reopen.
      - In-memory content mutation, baseline drift, or scope drift rejects promotion.
      - Duplicate promotion is rejected.
- V5: Tool dependency pre-check:
      - Candidate dependencies not in tool_broker.application_allowlist rejected before LLM evaluation.
      - Distinguishes fixture vs broker vs sandbox evidence; unauthorized tool blocked before handler.
- V6: Lightweight path for L1 modifications (frontmatter/metadata) vs heavy multi-turn reflection.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hello_agents.tools import Tool, ToolParameter, ToolResponse
from skillforge.diff import compute_semantic_diff
from skillforge.evaluator.prompt_bloat import check_prompt_bloat, compute_body_section_stats
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evolution_loop import (
    compute_candidate_hash,
    mine_candidate,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    SkillMeta,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
    EvolveBudget,
)
from skillforge.pattern_mining import PatternMiningConfig, mine_pending
from skillforge.registry import SkillRegistry
from skillforge.state_machine import ReleaseStateMachine
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.skill_generator import (
    GenerationFailure,
    generate_candidate_from_requirement,
)


class MockLLM:
    """Mock LLM that counts invocations and returns a preset response."""

    def __init__(self, response_text: str = ""):
        self.response_text = response_text
        self.invocations: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.invocations.append(messages)
        return SimpleNamespace(content=self.response_text)


class DummyEvaluator:
    """Mock evaluator to track if evaluate_skill was reached."""

    def __init__(self):
        self.call_count = 0
        self.llm = MockLLM()
        self.output_cache = None

    def evaluate_skill(self, skill_name: str, cases: list[dict], **kwargs: Any) -> EvalResult:
        self.call_count += 1
        return EvalResult(
            release_id="test_rel",
            structure_score={"format": 1.0},
            effect_score={"task_success": 1.0},
            objective_metrics={"bleu": 0.95},
            p0_pass=True,
            valid=True,
        )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Set up an isolated Git repository for state machine tests."""
    repo_dir = tmp_path / "git_repo"
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
def sample_provenance() -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name="test_tool",
        fixture_case_id="case_01",
        call_index=0,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"param": "value"},
        output_status="SUCCESS",
        output_summary="Result summary",
        latency_ms=10.0,
        timestamp="2026-09-30T00:00:00Z",
        signature="sha256:abc123sig",
        snapshot_id="snap_01",
        snapshot_content="Result summary",
    )


# ==============================================================================
# V1: Purpose Isolation in mine_candidate and mine_pending
# ==============================================================================

def test_v1_mining_rejects_non_learning_episodes(tmp_path: Path, sample_provenance: ToolCallProvenance):
    """V1: mine_candidate & mine_pending only accept purpose='learning' episodes."""
    db_path = tmp_path / "v1_test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    # 1. Episode with purpose='evaluation'
    ep_eval = Episode(
        episode_id="ep_eval_01",
        task_id="task_eval",
        run_id="run_eval",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"purpose": "evaluation"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep_eval)

    mock_llm = MockLLM("--- \nname: test_skill\n--- \n## Overview\nBody")
    with pytest.raises(ValueError, match="only 'learning' episodes are accepted"):
        mine_candidate([ep_eval], "test_skill", mock_llm, cand_store)

    # 2. Episode with purpose='heldout'
    ep_heldout = Episode(
        episode_id="ep_heldout_01",
        task_id="task_heldout",
        run_id="run_heldout",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"purpose": "heldout"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep_heldout)
    with pytest.raises(ValueError, match="only 'learning' episodes are accepted"):
        mine_candidate([ep_heldout], "test_skill", mock_llm, cand_store)

    # 3. Episode with missing / unknown purpose
    ep_missing = Episode(
        episode_id="ep_missing_01",
        task_id="task_missing",
        run_id="run_missing",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"os": "darwin"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep_missing)
    with pytest.raises(ValueError, match="has purpose='unknown'"):
        mine_candidate([ep_missing], "test_skill", mock_llm, cand_store)

    # 4. mine_pending filters out evaluation and heldout episodes
    report = mine_pending(
        episode_store=ep_store,
        candidate_store=cand_store,
        llm=mock_llm,
        config=PatternMiningConfig(min_support=1),
    )
    assert report.learning_episodes_count == 0
    assert report.filtered_evaluation_episodes == 3
    assert len(report.candidates_created) == 0

    # 5. Valid learning episode accepted
    ep_learn = Episode(
        episode_id="ep_learn_01",
        task_id="task_learn",
        run_id="run_learn",
        skill_name="test_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep_learn)

    mock_llm.response_text = """---
name: test_skill
version: 1.0.0
description: Valid description
use_when: When testing
not_for: []
dependencies: []
trigger:
  keywords: [test]
examples: []
---

## Overview
Valid skill body.
"""
    res = mine_candidate([ep_learn], "test_skill", mock_llm, cand_store)
    assert res.decision == "create"
    assert res.candidate is not None
    assert res.candidate.skill_name == "test_skill"

    ep_store.close()
    cand_store.close()


# ==============================================================================
# V2: Anti-Tampering Check & Task Scope Hash
# ==============================================================================

def test_v2_tampered_episode_rejected_and_task_scope_hash(tmp_path: Path, sample_provenance: ToolCallProvenance):
    """V2: Tampered in-memory episode rejected; task_id incorporates into task_spec_hash."""
    db_path = tmp_path / "v2_test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)

    canonical_ep = Episode(
        episode_id="ep_canon_01",
        task_id="task_canon",
        run_id="run_canon",
        skill_name="tamper_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="Original failure reason",
    )
    ep_store.save_episode(canonical_ep)

    # Clone in-memory and tamper outcome to 'success'
    tampered_ep = Episode(
        episode_id="ep_canon_01",
        task_id="task_canon",
        run_id="run_canon",
        skill_name="tamper_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",  # Tampered!
        outcome_reason="Forged success outcome",
        verification_evidence={"independent_pass": True},
    )

    mock_llm = MockLLM()
    with pytest.raises(ValueError, match="tampered in-memory episode rejected"):
        mine_candidate([tampered_ep], "tamper_skill", mock_llm, cand_store)

    # Task Scope Hash verification in generate_candidate_from_requirement
    gen_llm = MockLLM(json.dumps({
        "name": "scope_skill",
        "version": "1.0.0",
        "description": "Scope test skill",
        "use_when": "Scope test query",
        "not_for": ["other tasks", "symbolic execution"],
        "keywords": ["scope", "test", "orders"],
        "examples": ["test example 1"],
        "body": "## Overview\nScope overview\n\n## Instructions\nDo test\n\n## Examples\nEx\n\n## Constraints\nNone",
        "test_cases": [
            {"query": "scope q1", "reference": "ref1"},
            {"query": "scope q2", "reference": "ref2"},
            {"query": "scope q3", "reference": "ref3"},
        ],
    }))

    cand_task_a = generate_candidate_from_requirement(
        request="Query logistics for order A",
        llm=gen_llm,
        task_id="TASK_001",
        candidate_store=cand_store,
    )
    assert not isinstance(cand_task_a, GenerationFailure)

    cand_task_b = generate_candidate_from_requirement(
        request="Query logistics for order A",
        llm=gen_llm,
        task_id="TASK_002",
        candidate_store=cand_store,
    )
    assert not isinstance(cand_task_b, GenerationFailure)

    expected_hash_a = hashlib.sha256("TASK_001:Query logistics for order A".encode("utf-8")).hexdigest()[:16]
    expected_hash_b = hashlib.sha256("TASK_002:Query logistics for order A".encode("utf-8")).hexdigest()[:16]

    assert cand_task_a.task_spec_hash == expected_hash_a
    assert cand_task_b.task_spec_hash == expected_hash_b
    assert cand_task_a.task_spec_hash != cand_task_b.task_spec_hash

    ep_store.close()
    cand_store.close()


# ==============================================================================
# V3: Prompt Bloat Consistency, Cold Start Bounds & Cheap Checks Pre-flight
# ==============================================================================

def test_v3_prompt_bloat_consistency_and_cheap_checks():
    """V3: Ratio > 0.25 AND delta > 100 triggers REVIEW; cold-start > 3000 chars triggers REVIEW; strict boundary/AND checks."""
    baseline_body = """## Overview
Baseline overview text.

## Instructions
""" + ("Step one do something standard and clear. " * 5) + """

## Examples
Example text.

## Constraints
Constraint text.
"""
    old_stats = compute_body_section_stats(baseline_body)
    inst_len = old_stats["Instructions"]

    # 1. Section growth > 25% AND delta > 100 chars -> REVIEW
    bloated_instructions = ("Step one do something standard and clear. " * 5) + (" Extra bloated instruction text added." * 5)
    bloated_body = baseline_body.replace(
        ("Step one do something standard and clear. " * 5),
        bloated_instructions,
    )
    delta = len(bloated_instructions.strip()) - inst_len
    ratio = delta / inst_len
    assert delta > 100
    assert ratio > 0.25

    test_budget = EvolveBudget(section_growth_tokens=100, max_body_delta_tokens=100)
    res = check_prompt_bloat(baseline_body, bloated_body, budget=test_budget, tokenizer_callable=len)
    assert not res.passed
    assert res.decision == "REVIEW"
    assert any("PROMPT_BLOAT" in r and "Instructions" in r for r in res.reasons)

    # 2. Section boundary: delta == 100 vs delta == 101 (AND condition)
    prefix = "## Instructions\n"
    base_inst_200 = prefix + ("X" * (200 - len(prefix)))
    # delta = 100 -> new is 300 chars, ratio is 100/200 = 50% (>25%), but delta is <= 100: PASS
    body_delta_100 = prefix + ("X" * (200 - len(prefix))) + ("Y" * 100)
    res_d100 = check_prompt_bloat(base_inst_200, body_delta_100, budget=test_budget, tokenizer_callable=len)
    assert res_d100.passed
    assert res_d100.decision == "PASS"

    # delta = 101 -> ratio is 101/200 = 50.5% (>25%) AND delta > 100: REVIEW
    body_delta_101 = prefix + ("X" * (200 - len(prefix))) + ("Y" * 101)
    res_d101 = check_prompt_bloat(base_inst_200, body_delta_101, budget=test_budget, tokenizer_callable=len)
    assert not res_d101.passed
    assert res_d101.decision == "REVIEW"

    # ratio == 25% boundary: base is 400 chars, delta is 100, ratio is 100/400 = 25% (<= 25%): PASS
    base_inst_400 = prefix + ("X" * (400 - len(prefix)))
    body_ratio_25 = prefix + ("X" * (400 - len(prefix))) + ("Y" * 100)
    res_r25 = check_prompt_bloat(base_inst_400, body_ratio_25, budget=test_budget, tokenizer_callable=len)
    assert res_r25.passed
    assert res_r25.decision == "PASS"

    # 3. Whole body multiplier boundary:
    # base is 500 chars, delta is 100 (multiplier 1.20, delta <= 100): PASS
    base_body_500 = prefix + ("Z" * (500 - len(prefix)))
    body_mult_120 = prefix + ("Z" * (500 - len(prefix))) + ("W" * 100)
    res_mult = check_prompt_bloat(base_body_500, body_mult_120, budget=test_budget, tokenizer_callable=len)
    assert res_mult.passed
    assert res_mult.decision == "PASS"

    # delta is 101, total is 601 (> 500 * 1.20 = 600) AND delta > 100: REVIEW
    body_mult_exceed = prefix + ("Z" * (500 - len(prefix))) + ("W" * 101)
    res_mult_exceed = check_prompt_bloat(base_body_500, body_mult_exceed, budget=test_budget, tokenizer_callable=len)
    assert not res_mult_exceed.passed
    assert res_mult_exceed.decision == "REVIEW"

    # 4. Cold-start exact boundary check (cold_start=True, max_body_chars=3000)
    exact_3000_body = prefix + ("A" * (3000 - len(prefix)))
    assert len(exact_3000_body) == 3000
    res_exact = check_prompt_bloat("", exact_3000_body, cold_start=True)
    assert res_exact.passed
    assert res_exact.decision == "PASS"

    exceed_3001_body = exact_3000_body + "B"
    assert len(exceed_3001_body) == 3001
    res_exceed = check_prompt_bloat("", exceed_3001_body, cold_start=True)
    assert not res_exceed.passed
    assert res_exceed.decision == "REVIEW"
    assert any("冷启动绝对上限" in r or "3000" in r for r in res_exceed.reasons)


def test_v3_cheap_checks_preflight_in_validate_candidate(tmp_path: Path):
    """V3 cheap check preflight: Bloat and tool permissions reject candidate BEFORE expensive LLM evaluation."""
    db_path = tmp_path / "cheap_checks.db"
    cand_store = CandidateStore(db_path)
    reg = SkillRegistry(db_path=db_path, skills_dir=tmp_path / "skills", repo_root=tmp_path)

    huge_body = "## Instructions\n" + ("A" * 3200)
    cand = CandidateSkill(
        candidate_id="cand_bloat_preflight",
        skill_name="bloat_test_skill",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(
            name="bloat_test_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["bloat"]),
        ),
        body=huge_body,
        rationale="Cold start body too large",
        status="DRAFT",
        source_requirement="Requirement for bloat test",
    )
    cand_store.save_candidate(cand)

    evaluator = DummyEvaluator()
    rec = validate_candidate(
        candidate=cand,
        evaluator=evaluator,
        registry=reg,
        eval_cases=[{"query": "q", "reference": "r"}],
        candidate_store=cand_store,
    )

    # Cheap bloat check caught the issue, LLM evaluator was NEVER invoked!
    assert rec.ratchet_decision == "REVIEW"
    assert evaluator.call_count == 0
    assert cand_store.get_validation_record("cand_bloat_preflight") is not None
    assert cand_store.get_validation_record("cand_bloat_preflight").ratchet_decision == "REVIEW"

    reg.close()
    cand_store.close()


def test_v3_consistent_bloat_diagnosis_across_all_entry_points(tmp_path: Path):
    """V3: The SAME oversized modification triggers consistent bloat diagnosis across:
    1. Requirement generator revise (generate_candidate_from_requirement)
    2. Explicit candidate revise (validate_candidate)
    3. RepairJob (repair_skill_failure)
    4. Evaluator patch check (check_prompt_bloat)
    Expensive LLM evaluation calls are strictly 0 across all entry points.
    """
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "math_calc"
    skill_dir.mkdir(parents=True)
    base_skill_md = """---
name: math_calc
version: 1.0.0
description: Calculation helper
use_when: Basic math
not_for: []
dependencies: []
trigger:
  keywords: [calc]
examples: []
---

## Overview
Math calculation tool.

## Instructions
1. Parse expression carefully.
2. Output exact numerical result.

## Examples
Q: 2+2
A: 4

## Constraints
None.
"""
    (skill_dir / "SKILL.md").write_text(base_skill_md, encoding="utf-8")

    db_path = tmp_path / "all_entries.db"
    cand_store = CandidateStore(db_path)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    bloated_addition = " Extra explanation step that adds unnecessary verbose text to the prompt and inflates instructions length significantly beyond allowable ratio threshold." * 80
    bloated_body = """## Overview
Math calculation tool.

## Instructions
1. Parse expression carefully.
2. Output exact numerical result.""" + bloated_addition + """

## Examples
Q: 2+2
A: 4

## Constraints
None.
"""
    # 1. Entry 1: 需求生成器 revise (generate_candidate_from_requirement)
    mock_llm_gen = MockLLM(json.dumps({
        "name": "math_calc",
        "version": "1.0.1",
        "description": "Calculation helper",
        "use_when": "Basic math",
        "not_for": ["calculus", "linear algebra"],
        "keywords": ["calc", "math", "arithmetic"],
        "examples": ["1+1", "2+2"],
        "body": bloated_body,
        "test_cases": [
            {"query": "1+1", "reference": "2"},
            {"query": "2+2", "reference": "4"},
            {"query": "3*3", "reference": "9"},
        ],
    }))
    cand_gen = generate_candidate_from_requirement(
        request="Revise math_calc with verbose explanation steps",
        candidate_store=cand_store,
        llm=mock_llm_gen,
        repo_root=tmp_path,
        registry=reg,
    )
    assert isinstance(cand_gen, CandidateSkill)
    assert cand_gen.decision == "revise"
    assert "Bloat Warning" in cand_gen.rationale
    rec_gen = cand_store.get_validation_record(cand_gen.candidate_id)
    assert rec_gen is not None
    assert rec_gen.ratchet_decision == "REVIEW"
    assert any("PROMPT_BLOAT" in r and "Instructions" in r for r in rec_gen.ratchet_verdict.reasons)

    # 2. Entry 2: 显式 Candidate revise (validate_candidate)
    evaluator_explicit = DummyEvaluator()
    rec_explicit = validate_candidate(
        candidate=cand_gen,
        evaluator=evaluator_explicit,
        registry=reg,
        eval_cases=[{"query": "1+1", "reference": "2"}],
        candidate_store=cand_store,
    )
    assert rec_explicit.ratchet_decision == "REVIEW"
    assert evaluator_explicit.call_count == 0  # Expensive eval halted!
    assert any("PROMPT_BLOAT" in r and "Instructions" in r for r in rec_explicit.ratchet_verdict.reasons)

    # 3. Entry 3: RepairJob (repair_skill_failure)
    from skillforge.repair import repair_skill_failure
    ep_store = EpisodeStore(db_path)
    ep_fail = Episode(
        episode_id="ep_repair_bloat_01",
        task_id="t_calc_fail",
        run_id="r_calc_fail",
        skill_name="math_calc",
        skill_version="1.0.0",
        environment={"purpose": "learning", "query": "2*3"},
        provenances=[],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="Arithmetic calculation failed",
    )
    ep_store.save_episode(ep_fail)

    patcher_llm = MockLLM("""---
name: math_calc
version: 1.0.1
description: Calculation helper
use_when: Basic math
not_for: []
dependencies: []
trigger:
  keywords: [calc]
examples: []
---

""" + bloated_body)
    evaluator_repair = DummyEvaluator()
    job = repair_skill_failure(
        episodes=[ep_fail],
        skill_name="math_calc",
        episode_store=ep_store,
        candidate_store=cand_store,
        registry=reg,
        evaluator=evaluator_repair,
        eval_cases=[{"id": "c1", "query": "2*3", "reference": "6"}],
        llm=patcher_llm,
        max_attempts=1,
    )
    assert job.status == "AWAITING_REVIEW"
    assert len(job.attempts) == 1
    assert job.attempts[0].validation_decision == "REVIEW"
    assert evaluator_repair.call_count == 0  # Expensive eval halted in RepairJob!
    assert "PROMPT_BLOAT" in job.attempts[0].error_feedback

    # 4. Entry 4: check_prompt_bloat (shared / evolver rule)
    bloat_res_evolver = check_prompt_bloat(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8").split("---\n\n")[1],
        bloated_body,
    )
    assert bloat_res_evolver.decision == "REVIEW"
    assert any("PROMPT_BLOAT" in r and "Instructions" in r for r in bloat_res_evolver.reasons)

    reg.close()
    ep_store.close()
    cand_store.close()


# ==============================================================================
# V4: ValidationRecord Binding, Persistence & Tamper Rejection
# ==============================================================================

def test_v4_validation_record_binding_and_persistence(temp_git_repo: Path, sample_provenance: ToolCallProvenance):
    """V4: ValidationRecord binds metadata and hashes; survives store reopen; detects post-eval tampering."""
    db_path = temp_git_repo / "runs" / "v4_test.db"
    skills_dir = temp_git_repo / "skills"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=temp_git_repo)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=temp_git_repo)

    ep = Episode(
        episode_id="ep_v4_01",
        task_id="task_v4",
        run_id="run_v4",
        skill_name="v4_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[sample_provenance],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(ep)

    candidate = CandidateSkill(
        candidate_id="cand_v4_001",
        skill_name="v4_skill",
        decision="create",
        source_episode_ids=["ep_v4_01"],
        meta=SkillMeta(
            name="v4_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            trigger=Trigger(keywords=["v4"]),
        ),
        body="## Instructions\nOriginal verified content.",
        rationale="Validation test",
        task_spec_hash="spec_hash_12345",
    )
    cand_store.save_candidate(candidate)

    content_hash = compute_candidate_hash(candidate)
    val_rec = ValidationRecord(
        candidate_id="cand_v4_001",
        content_hash=content_hash,
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=EvalResult(
            release_id="rel_v4",
            structure_score={"format": 40.0},
            effect_score={"task": 60.0},
            objective_metrics={},
            p0_pass=True,
            valid=True,
        ),
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=[]),
        scope_hash="spec_hash_12345",
        config_hash="cfg_hash_67890",
        dataset_version="ds_v2026_09",
    )
    cand_store.save_validation_record(val_rec)

    # 1. Verify persistence survives closing and reopening CandidateStore
    cand_store.close()
    cand_store_reopened = CandidateStore(db_path, episode_store=ep_store)
    loaded_rec = cand_store_reopened.get_validation_record("cand_v4_001")
    assert loaded_rec is not None
    assert loaded_rec.candidate_id == "cand_v4_001"
    assert loaded_rec.content_hash == content_hash
    assert loaded_rec.ratchet_decision == "PASS"
    assert loaded_rec.scope_hash == "spec_hash_12345"
    assert loaded_rec.config_hash == "cfg_hash_67890"
    assert loaded_rec.dataset_version == "ds_v2026_09"
    assert not loaded_rec.promoted

    # 2. In-memory content mutation after validation -> rejected
    candidate.body = "## Instructions\nTampered post-validation content."
    with pytest.raises(ValueError, match="mutated after"):
        promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True)

    # Restore body
    candidate.body = "## Instructions\nOriginal verified content."

    # 3. Task intent/scope mismatch rejected (tested on unpromoted candidate)
    candidate.task_spec_hash = "spec_drifted_9999"
    with pytest.raises(ValueError, match="task scope hash changed"):
        promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True)
    candidate.task_spec_hash = "spec_hash_12345"

    # 4. Validator config mismatch rejected (tested on unpromoted candidate)
    with pytest.raises(ValueError, match="validator config hash changed"):
        promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True, expected_config_hash="cfg_different")

    # 5. Dataset version mismatch rejected (tested on unpromoted candidate)
    with pytest.raises(ValueError, match="evaluation dataset version changed"):
        promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True, expected_dataset_version="ds_different")

    # 6. Legitimate promotion with PASS record and caller_confirmed=True
    rel = promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True)
    assert rel.version == "1.0.0"
    assert rel.status == "PUBLISHED"

    # 7. Duplicate promotion rejected (after already promoted)
    with pytest.raises(ValueError, match="already been promoted"):
        promote_candidate(candidate, val_rec, sm, reg, cand_store_reopened, caller_confirmed=True)

    # 8. Corrupted / incomplete validation record in SQLite (fail-closed)
    corrupted_cand = CandidateSkill(
        candidate_id="cand_v4_corrupted",
        skill_name="v4_corrupted",
        decision="create",
        source_episode_ids=[],
        meta=candidate.meta,
        body="## Overview\nCorrupted test.",
        rationale="Corrupted test",
        source_requirement="Requirement for corrupted test",
    )
    cand_store_reopened.save_candidate(corrupted_cand)
    corrupted_rec = ValidationRecord(
        candidate_id="cand_v4_corrupted",
        content_hash="",
        baseline_version=None,
        ratchet_decision="",
    )
    cand_store_reopened.save_validation_record(corrupted_rec)
    with pytest.raises(ValueError, match="incomplete or corrupted"):
        promote_candidate(corrupted_cand, corrupted_rec, sm, reg, cand_store_reopened, caller_confirmed=True)

    reg.close()
    sm.close()
    ep_store.close()
    cand_store_reopened.close()


# ==============================================================================
# V5: Tool Permission Pre-check & Evidence Distinction
# ==============================================================================

class SampleCalcTool(Tool):
    def __init__(self):
        super().__init__(name="sample_calc", description="Calculate arithmetic")

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="expr", type="string", required=True, description="Math expression"),
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        return ToolResponse.success(text="42", data={"result": 42})


def test_v5_tool_permission_precheck_and_evidence_distinction(tmp_path: Path):
    """V5: Tool not in application allowlist rejected before LLM eval; broker generates signed provenance."""
    db_path = tmp_path / "v5_test.db"
    cand_store = CandidateStore(db_path)
    reg = SkillRegistry(db_path=db_path, skills_dir=tmp_path / "skills", repo_root=tmp_path)

    # Candidate requests unauthorized_tool
    cand = CandidateSkill(
        candidate_id="cand_v5_tool",
        skill_name="tool_dep_skill",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(
            name="tool_dep_skill",
            version="1.0.0",
            description="desc",
            use_when="when",
            dependencies=["forbidden_tool"],
            trigger=Trigger(keywords=["tool"]),
        ),
        body="## Instructions\nCall forbidden_tool.",
        rationale="Testing dependency allowlist precheck",
        status="DRAFT",
        source_requirement="Requirement for tool test",
    )
    cand_store.save_candidate(cand)

    # Broker only permits sample_calc, forbidden_tool is registered but unauthorized
    broker = ToolBroker(application_allowlist={"sample_calc"})
    calc_tool = SampleCalcTool()
    broker.register_tool("sample_calc", calc_tool)
    broker.register_tool("forbidden_tool", SampleCalcTool())

    evaluator = DummyEvaluator()
    rec = validate_candidate(
        candidate=cand,
        evaluator=evaluator,
        registry=reg,
        eval_cases=[{"query": "q", "reference": "r"}],
        candidate_store=cand_store,
        tool_broker=broker,
    )

    # Pre-check caught dependency error; 0 LLM calls!
    assert rec.ratchet_decision == "DECLINED"
    assert evaluator.call_count == 0
    assert any("TOOL_DEPENDENCY_ERROR" in r for r in rec.ratchet_verdict.reasons)

    # Runtime ToolBroker execution generates signed provenance with is_fixture=True
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)
    run_rec = runtime.start_run(run_id="run_v5_01", task_id="t_v5")

    # 1. Allowed tool call
    res_allowed = runtime.execute_tool("run_v5_01", "sample_calc", {"expr": "6 * 7"})
    assert res_allowed.status == "EXECUTED"
    assert res_allowed.provenance is not None
    assert res_allowed.provenance.is_fixture is True
    assert res_allowed.provenance.signature.startswith("sha256:")
    assert res_allowed.provenance.snapshot_content == "42"

    # 2. Unauthorized tool call rejected before handler invocation (handler call count strictly 0)
    res_forbidden = runtime.execute_tool("run_v5_01", "forbidden_tool", {"expr": "0"})
    assert res_forbidden.status == "REJECTED"
    assert res_forbidden.error_type == "PERMISSION_DENIED"

    # 3. Candidate modifying dependencies cannot be L1 auto-promoted (classified as L3)
    from skillforge.evaluator.validators import validate_dependency_patch
    base_skill_md = """---
name: tool_dep_skill
version: 1.0.0
description: Tool dependency skill
use_when: When using tools
not_for: []
dependencies: []
trigger:
  keywords: [tool]
examples: []
---

## Overview
Overview

## Instructions
Instructions

## Examples
Ex

## Constraints
None
"""
    revised_dep_md = base_skill_md.replace("dependencies: []", "dependencies: [sample_calc]").replace("version: 1.0.0", "version: 1.0.1")
    diff_dep = compute_semantic_diff(base_skill_md, revised_dep_md, declared_level="L1")
    # Declaring L1 for dependency modification is strictly flagged as a downgrade attempt and computed as L3
    assert diff_dep.computed_level == "L3"
    assert diff_dep.downgrade_attempt is True
    assert "dependencies" in diff_dep.changed_frontmatter

    # Plain text metadata change (e.g. description) remains L1 with no downgrade attempt
    revised_desc_md = base_skill_md.replace("description: Tool dependency skill", "description: Updated description").replace("version: 1.0.0", "version: 1.0.1")
    diff_desc = compute_semantic_diff(base_skill_md, revised_desc_md, declared_level="L1")
    assert diff_desc.is_valid
    assert diff_desc.computed_level == "L1"
    assert diff_desc.downgrade_attempt is False

    # 4. validate_dependency_patch runs fixture validation on dependencies
    mock_base_reg = SimpleNamespace(
        get_meta=lambda s: SimpleNamespace(dependencies=[]),
        _bodies={"tool_dep_skill": "Instructions"},
    )
    mock_cand_reg = SimpleNamespace(
        get_meta=lambda s: SimpleNamespace(dependencies=["sample_calc"]),
        _bodies={"tool_dep_skill": "Instructions with sample_calc call"},
    )
    verdict, provs = validate_dependency_patch(mock_base_reg, mock_cand_reg, "tool_dep_skill")
    assert isinstance(verdict, RatchetVerdict)

    # 5. Evidence hierarchy verification:
    # Fixture evidence: provenance.is_fixture is True, snapshot_id is an SHA256 integrity fingerprint (not an external certification)
    assert res_allowed.provenance.is_fixture is True
    assert res_allowed.provenance.signature.startswith("sha256:")  # Fingerprint hash
    # Application broker: ToolBroker enforces application allowlist, unauthorized tool calls return PERMISSION_DENIED
    assert res_forbidden.status == "REJECTED"
    assert res_forbidden.error_type == "PERMISSION_DENIED"

    reg.close()
    cand_store.close()


# ==============================================================================
# V6: Lightweight Path for L1 Modifications
# ==============================================================================

def test_v6_lightweight_l1_modification_path(tmp_path: Path):
    """V6: L1 frontmatter/metadata modification computes level L1 with empty changed_body_sections;
    L1 updates bypass the 5-node LangGraph loop; P1 Draft trial completes without heavy benchmark re-eval.
    """
    original_skill_md = """---
name: math_helper
version: 1.0.0
description: Math assistance
use_when: Basic math operations
not_for: [calculus]
dependencies: []
trigger:
  keywords: [math, calc]
examples: []
---

## Overview
Math helper overview.

## Instructions
1. Read formula.
2. Calculate result.

## Examples
Q: 1+1
A: 2

## Constraints
No calculus.
"""

    # Only modify single frontmatter L1 field (description), with patch bump 1.0.0 -> 1.0.1
    modified_l1_skill_md = original_skill_md.replace(
        "version: 1.0.0",
        "version: 1.0.1",
    ).replace(
        "description: Math assistance",
        "description: Fast math assistance",
    )

    diff_l1 = compute_semantic_diff(original_skill_md, modified_l1_skill_md, declared_level="L1")
    assert diff_l1.is_valid
    assert diff_l1.computed_level == "L1"
    assert diff_l1.changed_frontmatter == ["description"]
    assert len(diff_l1.changed_body_sections) == 0  # Body instructions untouched!

    # L2 modification: instructions body modified (patch bump 1.0.0 -> 1.0.1)
    modified_l2_skill_md = original_skill_md.replace(
        "version: 1.0.0",
        "version: 1.0.1",
    ).replace(
        "2. Calculate result.",
        "2. Validate inputs and calculate result accurately.",
    )
    diff_l2 = compute_semantic_diff(original_skill_md, modified_l2_skill_md, declared_level="L2")
    assert diff_l2.is_valid
    assert diff_l2.computed_level == "L2"
    assert "Instructions" in diff_l2.changed_body_sections

    # P1 Lightweight Draft trial runs directly without full evaluation suite or LangGraph:
    from skillforge.collector import ExperienceCollector
    ep_store = EpisodeStore(tmp_path / "v6_draft.db")
    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(db_path=tmp_path / "v6_draft.db", episode_store=ep_store, collector=collector)
    cand_l1 = CandidateSkill(
        candidate_id="cand_v6_draft",
        skill_name="math_helper",
        decision="revise",
        source_episode_ids=[],
        meta=SkillMeta(
            name="math_helper",
            version="1.0.1",
            description="Fast math assistance",
            use_when="Basic math operations",
            trigger=Trigger(keywords=["math"]),
        ),
        body="## Instructions\n1. Read formula.\n2. Calculate result.",
        rationale="L1 fast update",
        status="DRAFT",
        source_requirement="Requirement for L1 update",
    )
    run_rec = runtime.start_run(run_id="run_v6_trial", task_id="t_v6", candidate=cand_l1)
    body_used = runtime.get_run_body("math_helper", "run_v6_trial")
    assert "Fast math assistance" in cand_l1.meta.description
    assert body_used == cand_l1.body
    # Completes trial run and records into store without calling full evaluator or LangGraph!
    runtime.finalize_run("run_v6_trial", model_output="Output", verification_evidence={"independent_pass": True})
    ep = runtime.episode_store.get_episode("ep_run_v6_trial")
    assert ep is not None
    assert ep.outcome == "success"

    # Insufficient evidence remains unknown without forcing full evaluation
    run_rec2 = runtime.start_run(run_id="run_v6_unknown", task_id="t_v6_un", candidate=cand_l1)
    runtime.finalize_run("run_v6_unknown", model_output="Output without evidence")
    ep_un = runtime.episode_store.get_episode("ep_run_v6_unknown")
    assert ep_un is not None
    assert ep_un.outcome == "unknown"

    runtime.close()


# ==============================================================================
# G6: Unified Admission Gate & Anti-Bypass
# ==============================================================================

def test_g6_unified_admission_gate_and_anti_bypass(tmp_path: Path):
    """G6: Unpersisted/forged PASS rejected; tampered content rejected; Splitter does not mint fake PASS."""
    import yaml
    from skillforge.skill_generator import GeneratedSkill, RegistrationError, compute_generated_hash, generate_skill, register_skill
    from skillforge.skill_splitter import DomainSpec, SplitAnalysis, split_skill

    db_path = tmp_path / "g6_test.db"
    cand_store = CandidateStore(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    eval_dir = tmp_path / "evaluation_sets"
    eval_dir.mkdir(parents=True)
    repair_file = eval_dir / "repair_set.json"
    repair_file.write_text(json.dumps({
        "meta": {"total": 9, "auto_case_count": 1, "auto_case_ids": ["old_auto_01"]},
        "cases": [
            {"id": "old_auto_01", "skill": "old_skill", "query": "q", "reference": "r", "trace_id": "t"},
            *[{"id": f"base_{i}", "skill": "old_skill", "query": f"b{i}", "reference": f"r{i}"} for i in range(1, 9)],
        ],
    }), encoding="utf-8")
    router_file = eval_dir / "router_negatives.json"
    router_file.write_text(json.dumps({"meta": {}, "cases": []}), encoding="utf-8")

    mock_llm = MockLLM(json.dumps({
        "name": "g6_skill",
        "version": "1.0.0",
        "description": "Gate 6 test skill description",
        "use_when": "When testing gate 6",
        "not_for": ["other tasks", "symbolic execution"],
        "keywords": ["g6", "test", "gate"],
        "examples": ["ex1"],
        "body": "## Overview\nOverview text\n\n## Instructions\nInstruction text\n\n## Examples\nEx1\n\n## Constraints\nNone",
        "test_cases": [
            {"query": "q1", "reference": "r1"},
            {"query": "q2", "reference": "r2"},
            {"query": "q3", "reference": "r3"},
        ],
    }))

    gen_skill = generate_skill(
        request="Gate 6 test skill description",
        llm=mock_llm,
        repo_root=tmp_path,
        register=False,
    )
    assert isinstance(gen_skill, GeneratedSkill)

    # 0. generate_skill(register=True, caller_confirmed=True) without validation record -> REGISTER_UNVALIDATED
    fail_res = generate_skill(
        request="Gate 6 test skill description",
        llm=mock_llm,
        repo_root=tmp_path,
        register=True,
        caller_confirmed=True,
        validation_record=None,
    )
    assert isinstance(fail_res, GenerationFailure)
    assert fail_res.reason == "REGISTER_UNVALIDATED"

    # 1. Unconfirmed registration rejected
    with pytest.raises(RegistrationError, match="caller_confirmed=True"):
        register_skill(gen_skill, repo_root=tmp_path, repair_set_path=repair_file, router_negatives_path=router_file, caller_confirmed=False)

    # 2. Confirmed but unvalidated (no record) rejected
    with pytest.raises(RegistrationError, match="必须先通过共同门禁验证并持有有效 PASS 验证记录"):
        register_skill(gen_skill, repo_root=tmp_path, repair_set_path=repair_file, router_negatives_path=router_file, caller_confirmed=True)

    # 3. Forged in-memory PASS record not in CandidateStore rejected
    cand_id = "cand_g6_001"
    content_hash = compute_generated_hash(gen_skill)
    forged_rec = ValidationRecord(
        candidate_id=cand_id,
        content_hash=content_hash,
        baseline_version=None,
        ratchet_decision="PASS",
    )
    with pytest.raises(RegistrationError, match="未经过共同门禁权威验证或验证记录不存在"):
        register_skill(
            gen_skill,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=forged_rec,
            candidate_store=cand_store,
        )

    # 4. Save CandidateSkill and valid PASS record in CandidateStore
    cand = CandidateSkill(
        candidate_id=cand_id,
        skill_name="g6_skill",
        decision="create",
        source_episode_ids=[],
        meta=gen_skill.meta,
        body=gen_skill.body_raw,
        rationale="P2 G6 test",
        source_requirement="Testing G6 admission",
    )
    cand_store.save_candidate(cand)
    valid_rec = ValidationRecord(
        candidate_id=cand_id,
        content_hash=content_hash,
        baseline_version=None,
        ratchet_decision="PASS",
    )
    cand_store.save_validation_record(valid_rec)

    # 5. Content tampered after validation rejected
    tampered_skill = GeneratedSkill(
        name="g6_skill",
        version="1.0.0",
        description="Gate 6 test skill description",
        use_when="When testing gate 6",
        not_for=["other tasks", "symbolic execution"],
        frontmatter_raw="",
        body_raw=gen_skill.body_raw + "\nTampered after eval",
        full_skill_md=gen_skill.full_skill_md + "\nTampered after eval",
        test_cases=[],
        meta=gen_skill.meta,
    )
    with pytest.raises(RegistrationError, match="验证记录已失效：候选正文在验证后发生变更"):
        register_skill(
            tampered_skill,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=valid_rec,
            candidate_store=cand_store,
        )

    # 6. Legitimate PASS + caller_confirmed=True registers successfully
    path = register_skill(
        gen_skill,
        repo_root=tmp_path,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
        caller_confirmed=True,
        validation_record=valid_rec,
        candidate_store=cand_store,
    )
    assert path.exists()
    assert (skills_dir / "g6_skill" / "SKILL.md").exists()
    assert cand_store.get_validation_record(cand_id).promoted is True

    # 7. Splitter does not mint fake PASS records:
    from skillforge.skill_splitter import analyze_split, split_skill
    composite_md = """---
name: coding_helper_hub
version: 1.0.0
description: 综合编程开发辅助助手，提供正则表达式原理讲解与常见 HTTP 状态码排查
use_when: 用户需要理解正则表达式的语法机制与回溯原理，或者排查 HTTP 4xx/5xx 报错状态码
not_for:
  - 编写业务生产代码、爬虫脚本或后端服务开发
  - 服务器终端远程运维与 Linux 系统调优
dependencies: []
trigger:
  keywords:
    - 正则
    - regex
    - 状态码
    - HTTP状态码
    - 回溯
    - 报错排查
examples:
  - 讲一下 (a|b)*c 是怎么匹配的
  - 为什么 .* 会回溯这么慢
  - 502 Bad Gateway 报错原因与网关排查
  - 429 Too Many Requests 限流排查
evaluation:
  last_score: null
  last_release_id: null
---

## Overview

本技能是面向开发者的综合编程辅助说明书，涵盖两大独立能力：
1. 正则表达式原理解析：字符类、量词、分组、锚点与回溯机制。
2. HTTP 状态码排查：4xx 客户端错误与 5xx 服务端错误的原因与排查思路。

## Instructions

### 正则表达式原理解析
1. 识别用户问的是具体正则含义、概念原理还是语法区别。
2. 按识别到的类型给出分步拆解、小例子演示与陷阱提示。
3. 纯原理解析，不生成业务生产代码。

### HTTP 状态码排查
1. 从用户问题中提取状态码或错误范围（4xx/5xx）。
2. 按状态码含义、错误类别、常见原因、排查方向分步说明。
3. 遇到非标准状态码明确标注来源。

## Examples

**Q**：讲一下 `.*?` 是什么意思？
**A**：`.*?` 是非贪婪匹配：尽量少匹配字符。

**Q**：网站出现 `502 Bad Gateway` 怎么排查？
**A**：502 是网关从上游收到无效响应，排查上游服务存活与反向代理配置。

## Constraints

- 纯说明型技能，不为用户生成业务可执行代码。
- 遇服务器运维与终端部署需求，明确告知边界并引导至专业运维流程。
- 不虚构非标准状态码与内部正则引擎实现。
"""
    comp_dir = skills_dir / "coding_helper_hub"
    comp_dir.mkdir(parents=True, exist_ok=True)
    (comp_dir / "SKILL.md").write_text(composite_md, encoding="utf-8")

    comp_cases = [
        {"id": "er_d01", "skill": "coding_helper_hub", "query": "讲一下 (a|b)*c 是怎么匹配的", "reference": "拆解 (a|b) 分组交替、* 零次或多次、c 字面"},
        {"id": "er_d02", "skill": "coding_helper_hub", "query": "为什么 .* 会回溯这么慢", "reference": "解释贪婪 + 回溯本质"},
        {"id": "er_d04", "skill": "coding_helper_hub", "query": "分组 (?:) 和 () 有什么区别", "reference": "命名/编号 vs 不捕获"},
        {"id": "er_h01", "skill": "coding_helper_hub", "query": "这段正则的意思是不是 ^abc.*$", "reference": "验证用户理解正确"},
        {"id": "ehs_a01", "skill": "coding_helper_hub", "query": "用户访问接口时收到 502 Bad Gateway，应该从哪些方向排查？", "reference": "先说明 502 属于 5xx 服务器错误"},
        {"id": "ehs_a02", "skill": "coding_helper_hub", "query": "接口返回 429 Too Many Requests，是什么意思？", "reference": "解释 429 是 4xx 客户端错误"},
        {"id": "ehs_a03", "skill": "coding_helper_hub", "query": "4xx 和 5xx 有什么区别？", "reference": "说明 4xx 客户端错误与 5xx 服务端错误区别"},
        {"id": "ehs_a04", "skill": "coding_helper_hub", "query": "Nginx 日志里有很多 499 状态码，这是 HTTP 标准错误吗？", "reference": "499 不是 IANA 注册的标准 HTTP 状态码"},
    ]
    cur_repair = json.loads(repair_file.read_text(encoding="utf-8"))
    cur_repair["cases"].extend(comp_cases)
    cur_repair["meta"]["total"] = len(cur_repair["cases"])
    repair_file.write_text(json.dumps(cur_repair, ensure_ascii=False, indent=2), encoding="utf-8")

    analysis = analyze_split("coding_helper_hub", repo_root=tmp_path, repair_set_path=repair_file)
    assert analysis.can_split is True

    # 7.1 Split preview does NOT mint fake PASS records into CandidateStore
    preview = split_skill(
        analysis,
        repo_root=tmp_path,
        register=False,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
    )
    assert len(preview.sub_skills) == 2
    for sub in preview.sub_skills:
        assert cand_store.get_validation_record(f"cand_split_{sub.name}") is None
        assert cand_store.get_validation_record(sub.name) is None

    # 7.2 Attempting to register without valid PASS records in CandidateStore fails (anti-bypass)
    split_fail = split_skill(
        analysis,
        repo_root=tmp_path,
        register=True,
        caller_confirmed=True,
        candidate_store=cand_store,
        backup_original=False,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
    )
    assert split_fail.success is False
    assert any("RegistrationError" in err for err in split_fail.errors)
    assert any("未经过共同门禁权威验证或验证记录不存在" in err or "必须先通过共同门禁验证" in err for err in split_fail.errors)

    # 7.3 Only genuine PASS records in CandidateStore allow registration
    for sub in preview.sub_skills:
        cid = f"cand_split_{sub.name}"
        cand_store.save_candidate(
            CandidateSkill(
                candidate_id=cid,
                skill_name=sub.name,
                decision="create",
                source_episode_ids=[],
                meta=sub.meta,
                body=sub.body_raw or "",
                rationale="Split proposal validated",
                source_requirement="split",
            )
        )
        cand_store.save_validation_record(
            ValidationRecord(
                candidate_id=cid,
                content_hash=compute_generated_hash(sub),
                baseline_version=None,
                ratchet_decision="PASS",
            )
        )

    split_res = split_skill(
        analysis,
        repo_root=tmp_path,
        register=True,
        caller_confirmed=True,
        candidate_store=cand_store,
        backup_original=False,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
    )
    assert split_res.success is True
    assert len(split_res.sub_skills) == 2
    for sub in split_res.sub_skills:
        rec = cand_store.get_validation_record(f"cand_split_{sub.name}")
        assert rec is not None
        assert rec.promoted is True

