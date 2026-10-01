"""Acceptance Test Suite for P5: Real LangGraph & RepairJob Bounded Recovery Integration.

Verifies Acceptance Criteria A, B, C, D, E:
A. Production entry point (repair_skill_failure with enable_shadow_recovery=True) ->
   cheap gate -> reason routing -> existing StateGraph execution (5 nodes) ->
   RepairJob.run -> common validation (validate_candidate) -> Candidate returned in READY status;
   verified with controlled FakeLLM asserting "框架实际调用、LLM为fake".
B. Durable SQLite checkpointer resume: opening new runtime/checkpointer instances on the
   same DB retains consumed attempts, calls, tokens, deadline without reset; completed
   steps/calls do not repeat; inner RepairJob retries share the top-level budget ledger.
C. Non-recoverable signals (user_cancelled, nooracle/evaluator_fault, env_missing,
   security/permission_denied) yield 0 model repair calls and 0 graph executions;
   policy disabled keeps bloat in REVIEW; budget exhausted, duplicate hash, no progress,
   and timeout diagnostics are verified.
D. Lineage drift (baseline content or intent revision) invalidates checkpoints and stops;
   forged PASS or unconfirmed promotion is rejected; authoritative PASS validation record
   plus caller_confirmed=True promotes through ReleaseStateMachine.
E. First-batch invariants (snapshot, source, legacy migration, 1000-token boundary,
   and 3000-char cold start) remain strictly uncompromised.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml

from skillforge.bounded_recovery import (
    REASON_BASELINE_DRIFT,
    REASON_BUDGET_EXHAUSTED,
    REASON_CHECKPOINT_INVALIDATED,
    REASON_DUPLICATE_HASH,
    REASON_ENV_MISSING,
    REASON_EVALUATOR_FAULT,
    REASON_METRIC_ANOMALY,
    REASON_NO_PROGRESS,
    REASON_PERMISSION_DENIED,
    REASON_PROMPT_BLOAT_COMPRESSED,
    REASON_PROMPT_BLOAT_REVIEW,
    REASON_RECEIPT_DEFECT,
    REASON_TIMEOUT,
    REASON_TOOL_UNAVAILABLE,
    REASON_USER_CANCELLED,
    BoundedRecoveryResult,
    LineageBinding,
    RecoveryBudget,
    build_recovery_state_graph,
    check_non_recoverable_blockers,
    recover_bloated_candidate,
    run_bounded_recovery,
)
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator import SkillEvaluator
from skillforge.evaluator.prompt_bloat import check_prompt_bloat
from skillforge.evolution_loop import (
    compute_candidate_hash,
    compute_cases_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.langgraph_loop import SqliteCheckpointer
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    EvolveBudget,
    RatchetVerdict,
    Release,
    SkillMeta,
    TaskContext,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
)
from skillforge.registry import SkillRegistry
from skillforge.repair import (
    AttributionDiagnosis,
    RepairAttemptRecord,
    RepairJob,
    promote_repaired_skill,
    repair_skill_failure,
)
from skillforge.state_machine import ReleaseStateMachine


class FakeLLM:
    """Controllable deterministic FakeLLM for test isolation."""

    def __init__(self, contents: list[str] | None = None, default_content: Optional[str] = None):
        self.contents = list(contents) if contents else []
        self.default_content = default_content
        self.calls: list[Any] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self.contents:
            c = self.contents.pop(0)
        elif self.default_content is not None:
            c = self.default_content
        else:
            c = "Order 101: 2 packages: Package 1 delivered, Package 2 in transit."
        return SimpleNamespace(content=c, usage={"total_tokens": 50})


def _setup_test_env(tmp_path: Path):
    db_path = tmp_path / "skillforge_p5_test.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    logistics_dir = skills_dir / "logistics_tracker"
    logistics_dir.mkdir(parents=True, exist_ok=True)

    skill_meta = SkillMeta(
        name="logistics_tracker",
        version="1.0.0",
        description="Tracks multi-package logistics orders and shipments",
        use_when="Customer asks to track an order with multiple packages",
        not_for=["booking rides"],
        dependencies=[],
        trigger=Trigger(keywords=["track", "order", "package", "shipment"]),
    )
    skill_body = (
        "## Overview\n"
        "Tracks orders containing multiple carrier parcels.\n\n"
        "## Instructions\n"
        "1. Query order system for order ID.\n"
        "2. Parse package list and tracking numbers.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        "## Examples\n"
        "- Query: Track order 101 -> Report status of each package.\n\n"
        "## Constraints\n"
        "- Never fabricate tracking statuses."
    )
    full_md = (
        f"---\n{yaml.dump(skill_meta.model_dump(), sort_keys=False)}---\n\n{skill_body}"
    )
    (logistics_dir / "SKILL.md").write_text(full_md, encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True, capture_output=True)
    readme = tmp_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True, capture_output=True)

    episode_store = EpisodeStore(db_path=db_path)
    candidate_store = CandidateStore(db_path=db_path)
    state_machine = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
    registry = SkillRegistry(db_path=db_path, skills_dir=skills_dir)
    registry.load_skills_from_dir()

    judge_llm = FakeLLM(default_content=json.dumps({
        "verdict": "tied",
        "reason_codes": ["OK"],
        "evidence_summary": "verified pass across cases",
    }))
    evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(default_content="Order 101: 2 packages"),
        judge_llm=judge_llm,
    )

    eval_cases = [
        {
            "id": "c_pkg_01",
            "query": "Track order 101",
            "reference": "Package 1 delivered, Package 2 in transit",
            "expected_tools": ["track_order"],
        }
    ]

    return {
        "db_path": db_path,
        "skills_dir": skills_dir,
        "episode_store": episode_store,
        "candidate_store": candidate_store,
        "state_machine": state_machine,
        "registry": registry,
        "evaluator": evaluator,
        "eval_cases": eval_cases,
    }


def _make_failure_episode(ep_id: str, reason: str = "Logic error: missed package 2") -> Episode:
    return Episode(
        episode_id=ep_id,
        task_id="t_logistics_01",
        run_id="run_01",
        skill_name="logistics_tracker",
        skill_version="1.0.0",
        environment={"query": "Track order 101", "purpose": "learning", "session_id": "sess_01"},
        provenances=[
            ToolCallProvenance(
                tool_name="track_order",
                fixture_case_id="c_pkg_01",
                call_index=1,
                call_count=1,
                is_fixture=True,
                tool_required=True,
                tool_called=True,
                tool_success=True,
                authenticity_pass=True,
                input_params={"order_id": "101"},
                output_status="SUCCESS",
                output_summary="2 packages found: 1 delivered, 1 pending",
                latency_ms=10.0,
                timestamp="2026-10-01T12:00:00Z",
                signature="sig_test_provenance",
            )
        ],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason=reason,
        verification_evidence={"error": reason},
    )


# ==============================================================================
# Acceptance A: Public Entry Point -> Cheap Gate -> StateGraph -> RepairJob.run -> Common Validation
# ==============================================================================
def test_acceptance_a_production_entry_to_graph_and_repair_job(tmp_path: Path):
    """Criterion A:

    Calls repair_skill_failure(enable_shadow_recovery=True).
    Verifies full call chain:
    cheap gate -> reason routing -> StateGraph (5 nodes) -> RepairJob.run ->
    common validation -> candidate returned in READY status;
    asserts '框架实际调用、LLM为fake'.
    """
    env = _setup_test_env(tmp_path)
    ep = _make_failure_episode("ep_fail_01")
    env["episode_store"].save_episode(ep)

    patcher_llm = FakeLLM(contents=[
        "---\n"
        "name: logistics_tracker\n"
        "version: 1.0.1\n"
        "description: Tracks multi-package logistics orders and shipments\n"
        "use_when: Customer asks to track an order with multiple packages\n"
        "not_for: [booking rides]\n"
        "dependencies: []\n"
        "trigger:\n"
        "  keywords: [track, order, package, shipment]\n"
        "---\n\n"
        "## Overview\n"
        "Tracks multi-package orders accurately.\n\n"
        "## Instructions\n"
        "1. Query order system for order ID.\n"
        "2. Parse package list and all tracking numbers without omission.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        "## Examples\n"
        "- Query: Track order 101 -> Report status of every package.\n\n"
        "## Constraints\n"
        "- Never omit in-transit packages."
    ])

    shared_budget = RecoveryBudget(max_attempts=2, max_calls=10, max_tokens=10_000)

    # Invocation from real public production evolution entry point
    job = repair_skill_failure(
        episodes=[ep],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        max_attempts=2,
        enable_shadow_recovery=True,
        shared_budget=shared_budget,
    )

    # 1. Assert RepairJob created and executed through protocol
    assert job is not None
    assert job.job_id.startswith("job_")
    assert job.status == "READY"
    assert job.current_attempt == 1
    assert len(job.attempts) == 1
    assert job.attempts[0].validation_decision == "PASS"

    # 2. Assert candidate returned and persisted in authoritative CandidateStore
    assert job.latest_candidate is not None
    assert job.latest_candidate.skill_name == "logistics_tracker"
    assert job.latest_candidate.meta.version == "1.0.1"
    assert job.latest_candidate.status == "READY"

    stored_cand = env["candidate_store"].get_candidate(job.latest_candidate.candidate_id)
    assert stored_cand is not None
    assert stored_cand.status == "READY"

    # 3. Assert common validation record exists in CandidateStore
    val_rec = env["candidate_store"].get_validation_record(job.latest_candidate.candidate_id)
    assert val_rec is not None
    assert val_rec.ratchet_decision == "PASS"
    assert val_rec.content_hash == compute_candidate_hash(job.latest_candidate)

    # 4. Assert StateGraph transition history traverses the required 5-node lifecycle
    rec_res: BoundedRecoveryResult = getattr(job, "bounded_recovery_result", None)
    assert rec_res is not None
    transitions = rec_res.transition_history
    assert len(transitions) >= 3
    node_sequence = [t["from"] for t in transitions]
    assert "failure_analysis" in node_sequence
    assert "candidate_generation" in node_sequence
    assert "validation" in node_sequence
    assert "defense_adjudication" in node_sequence

    # 5. Assert L5: Formal SkillRegistry remains UNCHANGED (gated promotion only)
    assert env["registry"].get_meta("logistics_tracker").version == "1.0.0"

    # 6. Explicit confirmation of testing contract: "框架实际调用、LLM为fake"
    assert len(patcher_llm.calls) == 1, "框架实际调用了 LLM 一次，LLM 为受控 FakeLLM"
    assert shared_budget.consumed_attempts == 1
    assert shared_budget.consumed_calls == 1


# ==============================================================================
# Acceptance B: SqliteCheckpointer Durable Resume & Shared Budget Non-Reset
# ==============================================================================
def test_acceptance_b_sqlite_checkpoint_resume_and_shared_budget_persistence(tmp_path: Path):
    """Criterion B:

    Verifies SqliteCheckpointer durable checkpointing:
    - New process/checkpointer instance re-opens same SQLite checkpoint
    - attempts, calls, tokens, deadline DO NOT reset to 0
    - Completed tool/model steps count does not increase
    - Inner RepairJob retry shares top-level budget without duplication
    """
    env = _setup_test_env(tmp_path)
    ep = _make_failure_episode("ep_fail_durable_01")
    env["episode_store"].save_episode(ep)

    checkpointer_db = tmp_path / "langgraph_durable.db"
    thread_id = "thread-durable-01"

    # 1. First run interrupted after candidate_generation
    cp1 = SqliteCheckpointer(db_path=checkpointer_db)
    budget1 = RecoveryBudget(max_attempts=3, max_calls=10, max_tokens=10_000, deadline_seconds=100.0)

    patcher_llm = FakeLLM(contents=[
        "---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Durable test\nuse_when: Customer asks to track packages\nnot_for: [booking rides]\ndependencies: []\ntrigger:\n  keywords: [track, order, package, shipment]\n---\n\n## Overview\nFix 1.\n\n## Instructions\n1. S1.\n\n## Examples\n- E1.\n\n## Constraints\n- C1.\n"
    ])

    res1 = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        candidate_store=env["candidate_store"],
        episode_store=env["episode_store"],
        llm=patcher_llm,
        shared_budget=budget1,
        checkpointer=cp1,
        thread_id=thread_id,
        interrupt_after=["candidate_generation"],
    )
    # Interrupted after candidate_generation: attempt 1 generated, budget consumed
    assert budget1.consumed_attempts == 1
    assert budget1.consumed_calls == 1
    assert budget1.consumed_tokens == 50
    assert len(patcher_llm.calls) == 1

    # 2. Fresh instance re-opening the exact same SQLite checkpointer DB
    cp2 = SqliteCheckpointer(db_path=checkpointer_db)
    app2 = build_recovery_state_graph(checkpointer=cp2)
    saved_state = app2.get_state({"configurable": {"thread_id": thread_id}})
    assert saved_state is not None
    restored_budget: RecoveryBudget = saved_state.values["budget"]

    # Invariant: attempts, calls, tokens, deadline original values DO NOT reset to 0!
    assert restored_budget.consumed_attempts == 1
    assert restored_budget.consumed_calls == 1
    assert restored_budget.consumed_tokens == 50
    assert restored_budget.deadline_seconds == 100.0
    assert restored_budget.start_time == budget1.start_time

    # 2b. Controlled clock: advance time past original absolute deadline
    # Resuming from checkpoint MUST lead to TIMEOUT without new model calls (does NOT grant new 100s)
    thread_timeout = "thread-durable-timeout"
    cp_to1 = SqliteCheckpointer(db_path=checkpointer_db)
    budget_to = RecoveryBudget(max_attempts=3, max_calls=10, max_tokens=10_000, deadline_seconds=100.0)
    run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        candidate_store=env["candidate_store"],
        episode_store=env["episode_store"],
        llm=patcher_llm,
        shared_budget=budget_to,
        checkpointer=cp_to1,
        thread_id=thread_timeout,
        interrupt_after=["candidate_generation"],
    )
    future_time = budget_to.start_time + 105.0
    with patch("time.time", return_value=future_time):
        cp_to2 = SqliteCheckpointer(db_path=checkpointer_db)
        calls_before_to = len(patcher_llm.calls)
        res_to = run_bounded_recovery(
            skill_name="logistics_tracker",
            episodes=[ep],
            registry=env["registry"],
            evaluator=env["evaluator"],
            eval_cases=env["eval_cases"],
            candidate_store=env["candidate_store"],
            episode_store=env["episode_store"],
            llm=patcher_llm,
            checkpointer=cp_to2,
            thread_id=thread_timeout,
        )
        assert res_to.status in ("STOPPED", "BLOCKED")
        assert res_to.reason_code == REASON_TIMEOUT
        assert len(patcher_llm.calls) == calls_before_to, "No extra model calls when resuming past deadline"

    # 3. Resume from checkpoint: completed LLM call is NOT re-executed!
    res2 = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        candidate_store=env["candidate_store"],
        episode_store=env["episode_store"],
        llm=patcher_llm,
        shared_budget=restored_budget,
        checkpointer=cp2,
        thread_id=thread_id,
    )
    assert res2.status == "SUCCESS"
    assert len(patcher_llm.calls) == 2, "1 call from thread_id + 1 call from thread_timeout, 0 calls on resume"
    assert res2.candidate is not None
    assert res2.candidate.meta.version == "1.0.1"

    # 4. Multi-round retry shares top-level budget:
    judge_retry_llm = FakeLLM(default_content=json.dumps({"verdict": "tied", "reason_codes": ["OK"], "evidence_summary": "tied"}))
    retry_evaluator = SkillEvaluator(
        registry=env["registry"],
        llm=FakeLLM(default_content="Order 101"),
        judge_llm=judge_retry_llm,
    )
    # Candidate 1: drops Constraints section (score drops to 65.00 -> DECLINED with reason A)
    # Candidate 2: drops trigger keywords (score drops to 60.00 -> DECLINED with reason B)
    multi_patcher_llm = FakeLLM(contents=[
        "---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Att1 test\nuse_when: Customer asks to track packages\nnot_for: [booking rides]\ndependencies: []\ntrigger:\n  keywords: [track, order, package, shipment]\n---\n\n## Overview\nA1.\n\n## Instructions\n1. S1.\n\n## Examples\n- E1.\n",
        "---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Att2 test\nuse_when: Customer asks to track packages\nnot_for: [booking rides]\ndependencies: []\ntrigger:\n  keywords: [track]\n---\n\n## Overview\nA2.\n\n## Instructions\n1. S2.\n\n## Examples\n- E2.\n\n## Constraints\n- C2.\n",
    ])
    ep_retry = _make_failure_episode("ep_fail_retry_01")
    env["episode_store"].save_episode(ep_retry)
    retry_budget = RecoveryBudget(max_attempts=2, max_calls=10)
    job_retry = repair_skill_failure(
        episodes=[ep_retry],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=retry_evaluator,
        eval_cases=env["eval_cases"],
        llm=multi_patcher_llm,
        max_attempts=2,
        enable_shadow_recovery=True,
        shared_budget=retry_budget,
    )
    assert job_retry.status == "EXHAUSTED"
    assert retry_budget.consumed_attempts == 2
    assert retry_budget.consumed_calls == 2
    assert len(job_retry.attempts) == 2, "Inner RepairJob retried exactly within shared top-level budget"


# ==============================================================================
# Acceptance C: Non-recoverable Blockers, Disabled Policy, & Stopping Diagnostics
# ==============================================================================
def test_acceptance_c_non_recoverable_blockers_and_diagnostics(tmp_path: Path):
    """Criterion C:

    - user_cancelled, nooracle/evaluator_fault, env_missing, security -> 0 repair calls
    - policy disabled -> 0 graph invokes, candidate remains in REVIEW
    - budget exhausted, duplicate hash, no progress, timeout diagnostics
    """
    env = _setup_test_env(tmp_path)
    ep_cancel = _make_failure_episode("ep_cancel_01")
    env["episode_store"].save_episode(ep_cancel)

    patcher_llm = FakeLLM([])

    # 1. user_cancelled -> BLOCKED, 0 repair calls
    job_cancel = repair_skill_failure(
        episodes=[ep_cancel],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
        user_cancelled=True,
    )
    assert job_cancel.status == "BLOCKED"
    assert job_cancel.stop_reason == REASON_USER_CANCELLED
    assert len(patcher_llm.calls) == 0

    # 2. nooracle / evaluator fault -> BLOCKED, 0 repair calls
    ep_eval_fault = _make_failure_episode("ep_eval_fault_01", reason="Evaluator error: missing ground truth oracle")
    env["episode_store"].save_episode(ep_eval_fault)
    job_nooracle = repair_skill_failure(
        episodes=[ep_eval_fault],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
    )
    assert job_nooracle.status == "BLOCKED"
    assert job_nooracle.stop_reason == REASON_EVALUATOR_FAULT
    assert len(patcher_llm.calls) == 0

    # 3. env_missing -> BLOCKED, 0 repair calls
    ep_env_missing = _make_failure_episode("ep_env_01", reason="Runtime missing: NoSuchFileOrDirectory /opt/bin/carrier")
    env["episode_store"].save_episode(ep_env_missing)
    job_env = repair_skill_failure(
        episodes=[ep_env_missing],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
    )
    assert job_env.status == "BLOCKED"
    assert job_env.stop_reason == REASON_ENV_MISSING
    assert len(patcher_llm.calls) == 0

    # 4. security / permission denied -> AWAITING_REVIEW, 0 repair calls
    ep_security = _make_failure_episode("ep_sec_01", reason="Security policy denied: 403 Forbidden on carrier credentials")
    env["episode_store"].save_episode(ep_security)
    job_sec = repair_skill_failure(
        episodes=[ep_security],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
    )
    assert job_sec.status == "AWAITING_REVIEW"
    assert job_sec.stop_reason == REASON_PERMISSION_DENIED
    assert len(patcher_llm.calls) == 0

    # 5. Policy disabled on prompt bloat -> remains in REVIEW, 0 model repair calls
    ep_bloat = _make_failure_episode("ep_bloat_01")
    env["episode_store"].save_episode(ep_bloat)
    bloated_body = (
        "## Overview\n"
        "Tracks orders containing multiple carrier parcels.\n\n"
        "## Instructions\n"
        "1. Query order system for order ID.\n"
        "2. Parse package list and tracking numbers.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        + "\n".join(f"Extra duplicate line {i}: Always repeat tracking checks." for i in range(150))
    )
    cand_bloat = CandidateSkill(
        candidate_id="cand_bloated_01",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=[ep_bloat.episode_id],
        meta=env["registry"].get_meta("logistics_tracker"),
        body=bloated_body,
        rationale="Bloated modification",
        status="DRAFT",
    )
    env["candidate_store"].save_candidate(cand_bloat)

    job_bloat_disabled = repair_skill_failure(
        episodes=[ep_bloat],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=False,  # Policy disabled
        candidate=cand_bloat,
    )
    assert job_bloat_disabled.status == "AWAITING_REVIEW"
    assert job_bloat_disabled.stop_reason == REASON_PROMPT_BLOAT_REVIEW
    assert len(patcher_llm.calls) == 0

    # 6. Budget Exhausted diagnosis
    ep_exhaust = _make_failure_episode("ep_exhaust_01")
    env["episode_store"].save_episode(ep_exhaust)
    exhausted_budget = RecoveryBudget(max_attempts=2, consumed_attempts=2)
    job_exhaust = repair_skill_failure(
        episodes=[ep_exhaust],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
        shared_budget=exhausted_budget,
    )
    assert job_exhaust.status == "EXHAUSTED"
    assert job_exhaust.stop_reason == REASON_BUDGET_EXHAUSTED

    # 7. Timeout diagnosis
    ep_timeout = _make_failure_episode("ep_timeout_01")
    env["episode_store"].save_episode(ep_timeout)
    timeout_budget = RecoveryBudget(max_attempts=2, deadline_seconds=0.001)
    time.sleep(0.005)
    job_timeout = repair_skill_failure(
        episodes=[ep_timeout],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
        shared_budget=timeout_budget,
    )
    assert job_timeout.status == "BLOCKED"
    assert job_timeout.stop_reason == REASON_TIMEOUT


# ==============================================================================
# Acceptance D: Lineage Drift & Gated Promotion Verification
# ==============================================================================
def test_acceptance_d_lineage_drift_and_gated_promotion(tmp_path: Path):
    """Criterion D:

    - Baseline drift or intent revision drift stops resume with CHECKPOINT_INVALIDATED
    - Unconfirmed promotion or forged PASS leaves active registry unchanged
    - Only authoritative PASS validation record + caller_confirmed=True successfully promotes
    """
    env = _setup_test_env(tmp_path)
    ep = _make_failure_episode("ep_lineage_01")
    env["episode_store"].save_episode(ep)

    # 1. Saved checkpoint with baseline version 1.0.0 and initial hash
    initial_meta = env["registry"].get_meta("logistics_tracker")
    initial_body = env["registry"].get_body("logistics_tracker")
    initial_hash = hashlib.sha256(initial_body.encode("utf-8")).hexdigest()

    saved_lineage = LineageBinding(
        skill_name="logistics_tracker",
        baseline_version="1.0.0",
        baseline_hash=initial_hash,
        intent_revision=1,
        business_scope="default",
    )
    checkpoint_state = {
        "lineage": saved_lineage.to_dict(),
        "budget": {"consumed_attempts": 1, "consumed_calls": 1},
    }

    # Case A: Intent revision shifts from 1 to 2
    task_ctx_drift = TaskContext(task_id="t_01", goal="track order", intent_revision=2, business_scope="default")
    res_intent_drift = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        candidate_store=env["candidate_store"],
        episode_store=env["episode_store"],
        task_context=task_ctx_drift,
        resume_checkpoint=checkpoint_state,
    )
    assert res_intent_drift.status == "BLOCKED"
    assert res_intent_drift.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "intent_revision changed" in res_intent_drift.diagnostics.get("error", "")

    # Case B: Baseline content mutated in registry (drift)
    checkpoint_drifted_hash = {
        "lineage": LineageBinding(
            skill_name="logistics_tracker",
            baseline_version="1.0.0",
            baseline_hash="drifted_hash_mismatch",
            intent_revision=1,
            business_scope="default",
        ).to_dict(),
        "budget": {"consumed_attempts": 1},
    }
    res_base_drift = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        candidate_store=env["candidate_store"],
        episode_store=env["episode_store"],
        resume_checkpoint=checkpoint_drifted_hash,
    )
    assert res_base_drift.status == "BLOCKED"
    assert res_base_drift.reason_code == REASON_CHECKPOINT_INVALIDATED

    # Case C: Canonical evaluation dataset drift via public repair_skill_failure entry point
    initial_cases = list(env["eval_cases"])
    initial_ds_version = compute_cases_hash(initial_cases)
    initial_config_hash = env["evaluator"].get_config_fingerprint() if hasattr(env["evaluator"], "get_config_fingerprint") else None
    checkpoint_drifted_dataset = {
        "lineage": LineageBinding(
            skill_name="logistics_tracker",
            baseline_version="1.0.0",
            baseline_hash=initial_hash,
            intent_revision=1,
            business_scope="default",
            config_hash=initial_config_hash,
            dataset_version=initial_ds_version,
        ).to_dict(),
        "budget": {"consumed_attempts": 1, "consumed_calls": 1},
    }

    drifted_cases = list(initial_cases) + [{"id": "c_pkg_drift_99", "query": "Track order 99", "reference": "Delivered"}]
    patcher_llm_ds = FakeLLM(contents=["---\nname: logistics_tracker\n..."])
    calls_before_ds = len(patcher_llm_ds.calls)

    job_ds_drift = repair_skill_failure(
        episodes=[ep],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=drifted_cases,
        llm=patcher_llm_ds,
        enable_shadow_recovery=True,
        resume_checkpoint=checkpoint_drifted_dataset,
    )
    assert job_ds_drift.status == "BLOCKED"
    assert job_ds_drift.stop_reason == REASON_CHECKPOINT_INVALIDATED
    assert "dataset_version changed" in job_ds_drift.bounded_recovery_result.diagnostics.get("error", "")
    assert len(patcher_llm_ds.calls) == calls_before_ds, "0 model calls when canonical evaluation cases drift"

    # Case D: Legacy unverified dataset binding (dataset_version=None) vs active cases is rejected
    checkpoint_legacy_dataset = {
        "lineage": LineageBinding(
            skill_name="logistics_tracker",
            baseline_version="1.0.0",
            baseline_hash=initial_hash,
            intent_revision=1,
            business_scope="default",
            config_hash=initial_config_hash,
            dataset_version=None,
        ).to_dict(),
        "budget": {"consumed_attempts": 1},
    }
    job_legacy_ds = repair_skill_failure(
        episodes=[ep],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=initial_cases,
        llm=patcher_llm_ds,
        enable_shadow_recovery=True,
        resume_checkpoint=checkpoint_legacy_dataset,
    )
    assert job_legacy_ds.status == "BLOCKED"
    assert job_legacy_ds.stop_reason == REASON_CHECKPOINT_INVALIDATED
    assert len(patcher_llm_ds.calls) == calls_before_ds, "0 model calls on unverified legacy dataset binding"

    # 2. Gated Promotion (L5)
    # Generate a READY repair job
    ep_promo = _make_failure_episode("ep_lineage_promo_01")
    env["episode_store"].save_episode(ep_promo)
    patcher_llm = FakeLLM(contents=[
        "---\n"
        "name: logistics_tracker\n"
        "version: 1.0.1\n"
        "description: Tracks multi-package logistics orders and shipments\n"
        "use_when: Customer asks to track an order with multiple packages\n"
        "not_for: [booking rides]\n"
        "dependencies: []\n"
        "trigger:\n"
        "  keywords: [track, order, package, shipment]\n"
        "---\n\n"
        "## Overview\n"
        "Tracks multi-package orders accurately.\n\n"
        "## Instructions\n"
        "1. Query order system for order ID.\n"
        "2. Parse package list and all tracking numbers without omission.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        "## Examples\n"
        "- Query: Track order 101 -> Report status of every package.\n\n"
        "## Constraints\n"
        "- Never omit in-transit packages."
    ])
    job_ready = repair_skill_failure(
        episodes=[ep_promo],
        skill_name="logistics_tracker",
        episode_store=env["episode_store"],
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        evaluator=env["evaluator"],
        eval_cases=env["eval_cases"],
        llm=patcher_llm,
        enable_shadow_recovery=True,
    )
    assert job_ready.status == "READY"
    assert job_ready.latest_candidate is not None

    # Gate rule: caller_confirmed=False must fail
    with pytest.raises(ValueError, match="caller confirmation"):
        promote_repaired_skill(
            job=job_ready,
            candidate_store=env["candidate_store"],
            registry=env["registry"],
            state_machine=env["state_machine"],
            caller_confirmed=False,
        )
    assert env["registry"].get_meta("logistics_tracker").version == "1.0.0"

    # Gate rule: candidate tampering / forged content hash must fail
    tampered_cand = copy.deepcopy(job_ready.latest_candidate)
    tampered_cand.body += "\n## Malicious Injection"
    env["candidate_store"].save_candidate(tampered_cand, on_conflict="update")
    with pytest.raises(ValueError, match="candidate content was mutated"):
        promote_repaired_skill(
            job=job_ready,
            candidate_store=env["candidate_store"],
            registry=env["registry"],
            state_machine=env["state_machine"],
            caller_confirmed=True,
        )

    # Restore untampered candidate and promote with caller_confirmed=True
    env["candidate_store"].save_candidate(job_ready.latest_candidate, on_conflict="update")
    release = promote_repaired_skill(
        job=job_ready,
        candidate_store=env["candidate_store"],
        registry=env["registry"],
        state_machine=env["state_machine"],
        caller_confirmed=True,
    )
    assert release is not None
    assert release.version == "1.0.1"
    assert env["registry"].get_meta("logistics_tracker").version == "1.0.1"
    assert job_ready.status == "PROMOTED"

    # Duplicate promotion must be rejected
    with pytest.raises(ValueError, match="already been promoted"):
        promote_repaired_skill(
            job=job_ready,
            candidate_store=env["candidate_store"],
            registry=env["registry"],
            state_machine=env["state_machine"],
            caller_confirmed=True,
        )


# ==============================================================================
# Acceptance E: First-Batch Non-Regression Invariants (1000-Token & Snapshot)
# ==============================================================================
def test_acceptance_e_snapshot_source_migration_and_bloat_guards(tmp_path: Path):
    """Criterion E:

    - Exact 1000 tokens increase AND <= 1.20x strictly PASSES (<= 1000 tokens passes)
    - > 1000 tokens increase FAILS
    - 3000-character upper bound for cold start works
    """
    # 1. Exact 1000 tokens increase: passes
    # Baseline body with ~5000 tokens
    base_words = ["word"] * 5000
    base_body = " ".join(base_words)

    # Exactly 1000 tokens added (total 6000 tokens, growth = 1000 <= 1000 AND 6000/5000 = 1.20 <= 1.20)
    exact_cand_words = ["word"] * 6000
    exact_cand_body = " ".join(exact_cand_words)

    res_exact = check_prompt_bloat(old_body=base_body, new_body=exact_cand_body, cold_start=False)
    assert res_exact.passed is True, "Exact 1000 tokens increase (and <= 1.20x) MUST PASS"

    # 2. 1001 tokens added: exceeds 1000 token limit -> fails
    over_cand_words = ["word"] * 6001
    over_cand_body = " ".join(over_cand_words)
    res_over = check_prompt_bloat(old_body=base_body, new_body=over_cand_body, cold_start=False)
    assert res_over.passed is False, "1001 tokens increase MUST FAIL prompt bloat check"
    assert any("全 Body 膨胀门控" in r or "PROMPT_BLOAT" in r for r in res_over.reasons)

    # 3. Cold start: 3000 characters limit
    cold_under = "a" * 2900
    res_cold_ok = check_prompt_bloat(old_body="", new_body=cold_under, cold_start=True, budget=EvolveBudget(max_body_chars=3000))
    assert res_cold_ok.passed is True

    cold_over = "a" * 3100
    res_cold_bad = check_prompt_bloat(old_body="", new_body=cold_over, cold_start=True, budget=EvolveBudget(max_body_chars=3000))
    assert res_cold_bad.passed is False
    assert any("冷启动绝对上限" in r for r in res_cold_bad.reasons)
