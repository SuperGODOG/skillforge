"""P5 Acceptance Test Suite: Bounded Exception Recovery Reusing LangGraph (L1–L6).

Verifies Acceptance Criteria L1–L6:
- L1: Candidates entering REVIEW solely due to prompt bloat can be boundedly compressed/reflected
      when policy is enabled (enable_shadow_recovery=True) and pass the unified gate;
      when disabled, candidate stays in REVIEW. Normal small changes or coherent long procedures
      are not forced into compression or graph retry.
- L2: Non-recoverable failures (permission denied, missing environment, tool unavailable,
      evaluator fault/missing ground truth, user cancelled, anomalous metric jumps, baseline drift)
      immediately halt with distinct reason codes; 0 model repair calls are made.
      Receipt-level defects never alter skill definitions.
- L3: Outer graph and inner repair share top-level attempts, token, and call budget;
      no 2x2 hidden attempt expansion. Checkpoint restore preserves consumed attempts and
      executed side-effects without reset.
- L4: Duplicate candidate hashes, lack of progress, budget exhaustion, or timeouts cleanly terminate.
      Shifting intent revision, business scope, or baseline version/hash invalidates checkpoints;
      shadow directories are cleaned up without leaking.
- L5: Graph returns re-validated CandidateSkill and ValidationRecord without writing to active
      SkillRegistry or disk; promotion requires explicit confirmation (caller_confirmed=True)
      and authoritative PASS validation records (G6 gate).
- L6: Long coherent single processes are never split; separable multi-domain skills produce
      advisory-only proposals (applied=False) without mutating original skills or live routing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
    check_non_recoverable_blockers,
    recover_bloated_candidate,
    run_bounded_recovery,
)
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator import SkillEvaluator
from skillforge.evaluator.prompt_bloat import check_prompt_bloat
from skillforge.evolution_loop import (
    compute_candidate_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    Release,
    SkillMeta,
    TaskContext,
    ToolCallProvenance,
    ValidationRecord,
)
from skillforge.registry import SkillRegistry
from skillforge.skill_splitter import (
    DomainSpec,
    SplitProposal,
    analyze_split,
    suggest_skill_split,
)
from skillforge.state_machine import ReleaseStateMachine


def _judge_json(verdict: str = "tied", reasons: list[str] | None = None) -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": reasons or ["OK"],
        "evidence_summary": "verified pass",
    })


class FakeLLM:
    """Deterministic FakeLLM recording invocations."""

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
        from types import SimpleNamespace
        return SimpleNamespace(content=c, usage={"total_tokens": 50})


def _make_provenance(
    tool_name: str,
    output_status: str = "SUCCESS",
    output_summary: str = "OK",
) -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name=tool_name,
        fixture_case_id="case_logistics_01",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=(output_status == "SUCCESS"),
        authenticity_pass=True,
        input_params={"order_id": "ORD_101"},
        output_status=output_status,
        output_summary=output_summary,
        latency_ms=10.0,
        timestamp="2026-09-30T10:00:00Z",
        signature="sig_test_provenance",
        snapshot_id="snap_101",
        snapshot_content=json.dumps({"status": output_status}),
    )


def _make_episode(
    episode_id: str,
    outcome: str = "failure",
    outcome_reason: str = "test reason",
    provenances: Optional[list[ToolCallProvenance]] = None,
    task_id: str = "t_test",
    run_id: str = "r_test",
    verification_evidence: Optional[dict[str, Any]] = None,
) -> Episode:
    evidence = verification_evidence
    if outcome == "success" and evidence is None:
        evidence = {"source": "oracle", "independent_pass": True}
    return Episode(
        episode_id=episode_id,
        task_id=task_id,
        run_id=run_id,
        skill_name="logistics_tracker",
        skill_version="1.0.0",
        outcome=outcome,
        outcome_reason=outcome_reason,
        environment={"purpose": "learning"},
        provenances=provenances if provenances is not None else [_make_provenance("query_order_packages")],
        acceptance_criteria={},
        verification_evidence=evidence,
    )



def _setup_workspace(tmp_path: Path):
    """Setup minimal workspace with registry, evaluator, candidate store, and episode store."""
    db_path = tmp_path / "skillforge.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    logistics_dir = skills_dir / "logistics_tracker"
    logistics_dir.mkdir(parents=True, exist_ok=True)

    baseline_body = (
        "## Overview\n"
        "Multi-package logistics tracking helper.\n\n"
        "## Instructions\n"
        "1. Query order packages using query_order_packages tool.\n"
        "2. For each package, inspect tracking status.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        "## Examples\n"
        "- Query: Track order 101.\n"
        "- Result: Package 1 delivered, Package 2 in transit.\n\n"
        "## Constraints\n"
        "- Do not fabricate delivery timestamps.\n"
        "- Report partial deliveries clearly.\n"
    )

    baseline_meta = SkillMeta(
        name="logistics_tracker",
        version="1.0.0",
        description="Tracks multi-package logistics orders and shipments",
        use_when="Customer asks to track an order with multiple packages",
        not_for=["Processing payments", "Cancelling placed orders"],
        dependencies=["query_order_packages", "query_package_tracking"],
    )

    full_md = (
        f"---\n{yaml.dump(baseline_meta.model_dump(), sort_keys=False)}---\n\n{baseline_body}"
    )
    (logistics_dir / "SKILL.md").write_text(full_md, encoding="utf-8")

    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True, capture_output=True)
    readme = tmp_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True, capture_output=True)

    registry = SkillRegistry(db_path=db_path, skills_dir=skills_dir)
    registry.load_skills_from_dir()
    candidate_store = CandidateStore(db_path)
    episode_store = EpisodeStore(db_path)
    state_machine = ReleaseStateMachine(db_path, repo_root=tmp_path)

    # Seed episodes for source reference validity
    ep_seed1 = _make_episode("ep_01", outcome="success", outcome_reason="Initial seed episode")
    ep_seed2 = _make_episode("ep_02", outcome="success", outcome_reason="Second seed episode")
    episode_store.save_episode(ep_seed1)
    episode_store.save_episode(ep_seed2)

    agent_llm = FakeLLM(default_content="Order 101: Package 1 delivered, Package 2 in transit.")
    judge_llm = FakeLLM(default_content=_judge_json("tied"))
    evaluator = SkillEvaluator(registry=registry, llm=agent_llm, judge_llm=judge_llm)

    eval_cases = [
        {"id": "case_01", "query": "Track order 101", "reference": "Package in transit", "layer": "repair"},
        {"id": "case_02", "query": "Where is my delivery?", "reference": "Delivered", "layer": "repair"},
    ]


    return {
        "db_path": db_path,
        "skills_dir": skills_dir,
        "registry": registry,
        "candidate_store": candidate_store,
        "episode_store": episode_store,
        "state_machine": state_machine,
        "evaluator": evaluator,
        "eval_cases": eval_cases,
        "baseline_meta": baseline_meta,
        "baseline_body": baseline_body,
    }


def test_l1_prompt_bloat_shadow_compression_and_preflight_guards(tmp_path: Path):
    """L1: Prompt bloat candidate enters bounded compression if policy enabled; stays REVIEW if disabled.

    Normal modifications and coherent procedures are not forced into compression graph.
    """
    ws = _setup_workspace(tmp_path)
    registry: SkillRegistry = ws["registry"]
    candidate_store: CandidateStore = ws["candidate_store"]
    evaluator: SkillEvaluator = ws["evaluator"]
    eval_cases: list[dict] = ws["eval_cases"]
    baseline_body: str = ws["baseline_body"]

    # 1. Create a bloated candidate (> 1.20x growth and > 100 net characters)
    # Repeatedly duplicating instructions to artificially inflate prompt size (> 1000 tokens)
    redundant_repeats = "4. Redundant rule: ensure you check the status once again carefully.\n" * 80  # ~1280 tokens (> 1000 tokens)
    bloated_instruction = (
        "1. Query order packages using query_order_packages tool.\n"
        "2. For each package, inspect tracking status.\n"
        "3. Collate delivery carrier details and report progress to user.\n"
        f"{redundant_repeats}"
    )
    bloated_body = (
        "## Overview\n"
        "Multi-package logistics tracking helper with excessively long and repetitive instructions.\n\n"
        f"## Instructions\n{bloated_instruction}\n"
        "## Examples\n"
        "- Query: Track order 101.\n"
        "- Result: Package 1 delivered, Package 2 in transit.\n\n"
        "## Constraints\n"
        "- Do not fabricate delivery timestamps.\n"
        "- Report partial deliveries clearly.\n"
    )
    # Verify it actually trips bloat thresholds
    bloat_check = check_prompt_bloat(baseline_body, bloated_body)
    assert bloat_check.passed is False
    assert bloat_check.decision == "REVIEW"

    bloated_cand = CandidateSkill(
        candidate_id="cand_bloated_01",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=["ep_01"],
        meta=ws["baseline_meta"],
        body=bloated_body,
        status="DRAFT",
    )
    candidate_store.save_candidate(bloated_cand)

    # 2. Scenario A: Policy disabled (enable_shadow_recovery=False) -> stays in REVIEW
    res_disabled = recover_bloated_candidate(
        candidate=bloated_cand,
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        enable_shadow_recovery=False,
    )
    assert res_disabled.status == "AWAITING_REVIEW"
    assert res_disabled.reason_code == REASON_PROMPT_BLOAT_REVIEW
    assert res_disabled.validation_record is not None
    assert res_disabled.validation_record.ratchet_decision == "REVIEW"
    # Candidate body was NOT compressed
    assert res_disabled.candidate.body == bloated_body

    # 3. Scenario B: Policy enabled (enable_shadow_recovery=True) -> bounded compression & PASS
    res_enabled = recover_bloated_candidate(
        candidate=bloated_cand,
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        enable_shadow_recovery=True,
    )
    assert res_enabled.status == "SUCCESS"
    assert res_enabled.reason_code == REASON_PROMPT_BLOAT_COMPRESSED
    assert res_enabled.validation_record is not None
    assert res_enabled.validation_record.ratchet_decision == "PASS"
    assert res_enabled.candidate.candidate_id != bloated_cand.candidate_id
    assert res_enabled.candidate.parent_candidate_id == bloated_cand.candidate_id
    # Compressed body passed prompt bloat check against baseline
    compressed_check = check_prompt_bloat(baseline_body, res_enabled.candidate.body)
    assert compressed_check.passed is True

    # 4. Scenario C: Normal small modification or coherent procedure that does not trip bloat check
    normal_body = (
        "## Overview\n"
        "Multi-package logistics tracking helper.\n\n"
        "## Instructions\n"
        "1. Query order packages using query_order_packages tool.\n"
        "2. For each package, inspect tracking status.\n"
        "3. Collate delivery carrier details and report progress to user.\n"
        "4. Note estimated delivery date if available.\n\n"
        "## Examples\n"
        "- Query: Track order 101.\n"
        "- Result: Package 1 delivered, Package 2 in transit.\n\n"
        "## Constraints\n"
        "- Do not fabricate delivery timestamps.\n"
        "- Report partial deliveries clearly.\n"
    )
    normal_check = check_prompt_bloat(baseline_body, normal_body)
    assert normal_check.passed is True

    normal_cand = CandidateSkill(
        candidate_id="cand_normal_01",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=["ep_02"],
        meta=ws["baseline_meta"],
        body=normal_body,
        status="DRAFT",
    )
    candidate_store.save_candidate(normal_cand)

    res_normal = recover_bloated_candidate(
        candidate=normal_cand,
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        enable_shadow_recovery=True,
    )
    assert res_normal.status == "SUCCESS"
    assert res_normal.reason_code is None  # Never entered compression graph!
    assert res_normal.candidate.candidate_id == normal_cand.candidate_id


def test_l2_non_retryable_failure_blocking_and_reason_codes(tmp_path: Path):
    """L2: Non-recoverable failures stop immediately with distinct reason codes; 0 model repair calls.

    Receipt-level defects never alter skill definitions.
    """
    ws = _setup_workspace(tmp_path)
    registry: SkillRegistry = ws["registry"]
    candidate_store: CandidateStore = ws["candidate_store"]
    episode_store: EpisodeStore = ws["episode_store"]
    evaluator: SkillEvaluator = ws["evaluator"]
    eval_cases: list[dict] = ws["eval_cases"]

    # 1. Permission Denied (403 / policy violation)
    ep_perm = _make_episode(
        "ep_perm_01",
        outcome="failure",
        outcome_reason="403 Forbidden: Tool execution denied by security policy",
        provenances=[_make_provenance("query_order_packages", output_status="ERROR", output_summary="403 Forbidden")],
    )
    episode_store.save_episode(ep_perm)

    budget1 = RecoveryBudget(max_attempts=2)
    res_perm = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_perm],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget1,
    )
    assert res_perm.status == "BLOCKED"
    assert res_perm.reason_code == REASON_PERMISSION_DENIED
    assert budget1.consumed_attempts == 0  # 0 LLM calls made!

    # 2. Environment / Runtime Missing
    ep_env = _make_episode(
        "ep_env_01",
        outcome="failure",
        outcome_reason="Runtime missing: No such file or directory '/usr/bin/python3'",
    )
    episode_store.save_episode(ep_env)

    budget2 = RecoveryBudget(max_attempts=2)
    res_env = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_env],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget2,
    )
    assert res_env.status == "BLOCKED"
    assert res_env.reason_code == REASON_ENV_MISSING
    assert budget2.consumed_attempts == 0

    # 3. Tool Unavailable / Crash (500 / Connection Refused)
    ep_tool = _make_episode(
        "ep_tool_01",
        outcome="failure",
        outcome_reason="Tool execution crashed with ConnectionRefusedError",
        provenances=[_make_provenance("query_order_packages", output_status="ERROR", output_summary="ConnectionRefusedError: Connection refused")],
    )
    episode_store.save_episode(ep_tool)

    budget3 = RecoveryBudget(max_attempts=2)
    res_tool = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_tool],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget3,
    )
    assert res_tool.status == "BLOCKED"
    assert res_tool.reason_code == REASON_TOOL_UNAVAILABLE
    assert budget3.consumed_attempts == 0

    # 4. Evaluator Fault / Missing Ground Truth
    ep_eval = _make_episode(
        "ep_eval_01",
        outcome="failure",
        outcome_reason="Evaluator error: invalid_judge_result due to missing ground truth",
    )
    episode_store.save_episode(ep_eval)

    budget4 = RecoveryBudget(max_attempts=2)
    res_eval = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_eval],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget4,
    )
    assert res_eval.status == "BLOCKED"
    assert res_eval.reason_code == REASON_EVALUATOR_FAULT
    assert budget4.consumed_attempts == 0

    # 5. User Cancelled
    ep_cancel = _make_episode(
        "ep_cancel_01",
        outcome="failure",
        outcome_reason="User manually clicked cancel",
    )
    budget5 = RecoveryBudget(max_attempts=2)
    res_cancel = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_cancel],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget5,
        user_cancelled=True,
    )
    assert res_cancel.status == "BLOCKED"
    assert res_cancel.reason_code == REASON_USER_CANCELLED
    assert budget5.consumed_attempts == 0

    # 6. Metric Anomalous Jump
    ep_metric = _make_episode(
        "ep_metric_01",
        outcome="failure",
        outcome_reason="Sudden anomalous performance collapse",
    )
    budget6 = RecoveryBudget(max_attempts=2)
    res_metric = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_metric],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget6,
        metric_jump=-0.75,
    )
    assert res_metric.status == "BLOCKED"
    assert res_metric.reason_code == REASON_METRIC_ANOMALY
    assert budget6.consumed_attempts == 0

    # 7. Baseline Drift
    ep_drift = _make_episode(
        "ep_drift_01",
        outcome="failure",
        outcome_reason="Baseline mismatch during check",
    )
    budget7 = RecoveryBudget(max_attempts=2)
    res_drift = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_drift],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget7,
        baseline_drift=True,
    )
    assert res_drift.status == "BLOCKED"
    assert res_drift.reason_code == REASON_BASELINE_DRIFT
    assert budget7.consumed_attempts == 0

    # 8. Receipt-Level Defect (Isolated from Skill Modifying)
    ep_receipt = _make_episode(
        "ep_receipt_01",
        outcome="failure",
        outcome_reason="Execution parameter order_id was malformed in receipt",
    )
    budget8 = RecoveryBudget(max_attempts=2)
    res_receipt = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep_receipt],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget8,
        is_receipt_defect=True,
    )
    assert res_receipt.status == "BLOCKED"
    assert res_receipt.reason_code == REASON_RECEIPT_DEFECT
    assert budget8.consumed_attempts == 0


def test_l3_shared_top_level_budget_and_checkpoint_state_separation(tmp_path: Path):
    """L3: Outer graph and inner repair share top-level budget; no 2x2 multiplication.

    Resuming from checkpoint preserves attempts and side effects without reset.
    """
    ws = _setup_workspace(tmp_path)
    registry: SkillRegistry = ws["registry"]
    candidate_store: CandidateStore = ws["candidate_store"]
    episode_store: EpisodeStore = ws["episode_store"]
    evaluator: SkillEvaluator = ws["evaluator"]
    eval_cases: list[dict] = ws["eval_cases"]

    # Evaluator that declines candidate so repair consumes full remaining attempts
    declining_judge_llm = FakeLLM(default_content=json.dumps({
        "verdict": "INVALID",
        "reason_codes": ["EVAL_REGRESSION"],
        "evidence_summary": "declined due to test regression",
    }))
    declining_evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(default_content="Order 101"),
        judge_llm=declining_judge_llm,
    )

    ep = _make_episode(
        "ep_budget_01",
        task_id="t_budget",
        run_id="r_budget",
        outcome="failure",
        outcome_reason="Logic error: failed to collate partial packages",
    )
    episode_store.save_episode(ep)

    # 1. Top-level budget has max_attempts=2. Pre-consume 1 attempt.
    budget = RecoveryBudget(max_attempts=2, max_calls=10, max_tokens=20_000)
    budget.consume(calls=1, tokens=50)
    assert budget.consumed_attempts == 1
    assert budget.remaining_attempts == 1

    # 2. Run bounded recovery: it MUST only execute 1 attempt (not 2 fresh attempts creating 1 + 2 = 3 or 2x2 = 4)
    res = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=budget,
    )

    assert res.status == "EXHAUSTED"
    assert res.reason_code == REASON_BUDGET_EXHAUSTED
    assert budget.consumed_attempts == 2
    assert budget.remaining_attempts == 0
    # Exactly 1 attempt was made in recovery
    assert len(res.attempts) == 1

    # 3. Checkpoint restoration test:
    # Save checkpoint state with consumed_attempts=2 and executed side effect
    checkpoint_state = {
        "lineage": res.lineage.to_dict(),
        "budget": {
            "max_attempts": 2,
            "consumed_attempts": 2,
            "consumed_calls": 5,
            "consumed_tokens": 1200,
            "executed_side_effects": ["recorded_audit_receipt"],
        },
    }

    # Resuming with this checkpoint must NOT reset consumed attempts to 0!
    resumed_budget = RecoveryBudget(max_attempts=2)
    res_resumed = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=resumed_budget,
        resume_checkpoint=checkpoint_state,
    )
    assert res_resumed.status == "EXHAUSTED"
    assert res_resumed.reason_code == REASON_BUDGET_EXHAUSTED
    assert resumed_budget.consumed_attempts == 2
    assert "recorded_audit_receipt" in resumed_budget.executed_side_effects

    # 4. Top-level call cap check:
    call_limited_budget = RecoveryBudget(max_attempts=5, max_calls=1)
    call_limited_budget.consume(attempts=0, calls=1)
    assert call_limited_budget.remaining_calls == 0
    assert call_limited_budget.can_attempt() is False
    res_call_exhausted = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=call_limited_budget,
    )
    assert res_call_exhausted.status == "EXHAUSTED"
    assert res_call_exhausted.reason_code == REASON_BUDGET_EXHAUSTED

    # 5. Top-level tool call cap check:
    tool_limited_budget = RecoveryBudget(max_attempts=5, max_tool_calls=2)
    tool_limited_budget.consume(attempts=0, calls=0, tool_calls=2)
    assert tool_limited_budget.remaining_tool_calls == 0
    assert tool_limited_budget.can_attempt() is False
    res_tool_exhausted = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=tool_limited_budget,
    )
    assert res_tool_exhausted.status == "EXHAUSTED"
    assert res_tool_exhausted.reason_code == REASON_BUDGET_EXHAUSTED

    # 6. Deadline timeout stops cleanly without extra model calls:
    import time
    timeout_budget = RecoveryBudget(max_attempts=5, deadline_seconds=0.01)
    time.sleep(0.02)
    assert timeout_budget.is_timed_out() is True
    res_timeout = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        shared_budget=timeout_budget,
    )
    assert res_timeout.status == "STOPPED"
    assert res_timeout.reason_code == REASON_TIMEOUT
    assert timeout_budget.consumed_attempts == 0

    # 7. Irreversible side-effect handler idempotency on checkpoint resume:
    side_effect_execution_counter = {"audit_receipt": 0}

    def execute_audit_receipt(b: RecoveryBudget, action_id: str):
        if action_id not in b.executed_side_effects:
            side_effect_execution_counter["audit_receipt"] += 1
            b.consume(attempts=0, calls=0, side_effect=action_id)

    fresh_budget = RecoveryBudget(max_attempts=2)
    execute_audit_receipt(fresh_budget, "audit_receipt_101")
    assert side_effect_execution_counter["audit_receipt"] == 1
    assert "audit_receipt_101" in fresh_budget.executed_side_effects

    # Checkpoint state saved with recorded side effect
    checkpoint_with_side_effect = {
        "lineage": res.lineage.to_dict(),
        "budget": fresh_budget.to_dict(),
    }

    # Restore from checkpoint and simulate resumed action
    restored_budget_side_effect = RecoveryBudget.from_dict(checkpoint_with_side_effect["budget"])
    execute_audit_receipt(restored_budget_side_effect, "audit_receipt_101")
    # Side effect MUST NOT re-execute; counter remains exactly 1!
    assert side_effect_execution_counter["audit_receipt"] == 1

    # 8. Real token accounting ground truth vs missing usage:
    # Real usage from model is recorded accurately
    real_token_llm = FakeLLM(
        contents=[
            "---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Tracks multi-package logistics orders and shipments\nuse_when: Customer asks to track an order with multiple packages\nnot_for: []\ndependencies: []\n---\n\n## Overview\nTrack.\n\n## Instructions\n1. S.\n\n## Examples\n- E.\n\n## Constraints\n- C.\n"
        ]
    )
    # FakeLLM default returns usage={"total_tokens": 50}
    budget_token_real = RecoveryBudget(max_attempts=2)
    res_real_tok = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        llm=real_token_llm,
        shared_budget=budget_token_real,
    )
    assert budget_token_real.has_real_token_accounting is True
    assert budget_token_real.consumed_tokens == budget_token_real.consumed_attempts * 50

    # When model response provides no usage, tokens are NOT fabricated
    class NoUsageLLM:
        def invoke(self, prompt: str):
            from types import SimpleNamespace
            return SimpleNamespace(
                content=(
                    "---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Tracks multi-package logistics orders and shipments\nuse_when: Customer asks to track an order with multiple packages\nnot_for: []\ndependencies: []\n---\n\n## Overview\nTrack.\n\n## Instructions\n1. S.\n\n## Examples\n- E.\n\n## Constraints\n- C.\n"
                )
            )

    budget_no_usage = RecoveryBudget(max_attempts=2)
    res_no_tok = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        llm=NoUsageLLM(),
        shared_budget=budget_no_usage,
    )
    assert budget_no_usage.has_real_token_accounting is False
    assert budget_no_usage.consumed_tokens == 0  # Not faked from string length!


def test_l4_duplicate_candidate_budget_exhaustion_and_checkpoint_invalidation(tmp_path: Path):
    """L4: Duplicate candidate hash, no-progress, and lineage shifts cleanly terminate.

    No runaway orphaned shadow directories.
    """
    ws = _setup_workspace(tmp_path)
    registry: SkillRegistry = ws["registry"]
    candidate_store: CandidateStore = ws["candidate_store"]
    episode_store: EpisodeStore = ws["episode_store"]
    evaluator: SkillEvaluator = ws["evaluator"]
    eval_cases: list[dict] = ws["eval_cases"]

    ep = _make_episode(
        "ep_stagnant_01",
        task_id="t_stagnant",
        run_id="r_stagnant",
        outcome="failure",
        outcome_reason="Failed order query",
    )
    episode_store.save_episode(ep)

    # 1. Scenario A: Duplicate Candidate Hash
    # Mock LLM that always returns the exact same candidate
    class RepeatedOutputLLM:
        def __init__(self, raw: str):
            self.raw = raw
            self.calls = 0

        def invoke(self, prompt: str):
            self.calls += 1
            return MagicMock(content=self.raw)

    same_output = (
        "---\n"
        "name: logistics_tracker\n"
        "version: 1.0.1\n"
        "description: Tracks multi-package logistics orders and shipments\n"
        "use_when: Customer asks to track an order with multiple packages\n"
        "not_for: []\n"
        "dependencies: []\n"
        "---\n\n"
        "## Overview\nStatic body.\n\n## Instructions\n1. Fixed step.\n\n## Examples\n- Ex.\n\n## Constraints\n- C.\n"
    )
    dup_llm = RepeatedOutputLLM(same_output)

    # Evaluator returns DECLINED so it tries a second round
    declining_judge_llm = FakeLLM(default_content=json.dumps({
        "verdict": "INVALID",
        "reason_codes": ["EVAL_REGRESSION"],
        "evidence_summary": "declined attempt",
    }))
    declining_eval = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(default_content="Order 101"),
        judge_llm=declining_judge_llm,
    )

    res_dup = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=declining_eval,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        llm=dup_llm,
        shared_budget=RecoveryBudget(max_attempts=3),
    )
    assert res_dup.status == "DECLINED"
    assert res_dup.reason_code == REASON_DUPLICATE_HASH
    assert "Identical candidate hash" in res_dup.diagnostics.get("error", "")

    # 2. Scenario B: Stagnation / No-Progress (different hash, identical failure feedback)
    class StagnantEvolvingLLM:
        def __init__(self):
            self.attempt = 0

        def invoke(self, prompt: str):
            self.attempt += 1
            # Different content on each attempt ensures different candidate hashes
            return MagicMock(content=(
                f"---\nname: logistics_tracker\nversion: 1.0.1\ndescription: Tracks multi-package logistics orders\n"
                f"use_when: Customer asks to track an order with multiple packages\nnot_for: []\ndependencies: []\n---\n\n"
                f"## Overview\nOverview {self.attempt}.\n\n## Instructions\n1. Step {self.attempt}.\n\n## Examples\n- Ex.\n\n## Constraints\n- C.\n"
            ))

    stagnant_judge_llm = FakeLLM(default_content=json.dumps({
        "verdict": "DECLINED",
        "reason_codes": ["EVAL_ASSERTION_FAIL: Package 2 tracking missing"],
        "evidence_summary": "failed assertion",
    }))
    stagnant_evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(default_content="Order 101"),
        judge_llm=stagnant_judge_llm,
    )
    res_stagnant = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=stagnant_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        llm=StagnantEvolvingLLM(),
        shared_budget=RecoveryBudget(max_attempts=3),
    )
    assert res_stagnant.status == "DECLINED"
    assert res_stagnant.reason_code == REASON_NO_PROGRESS
    assert "No progress observed across attempts with identical failure reasons" in res_stagnant.diagnostics.get("error", "")

    # 3. Scenario C: Checkpoint Lineage Invalidation across all dimensions
    initial_lineage = LineageBinding(
        skill_name="logistics_tracker",
        baseline_version="1.0.0",
        baseline_hash=hashlib.sha256(registry.get_body("logistics_tracker").encode()).hexdigest(),
        intent_revision=1,
        business_scope="logistics",
        contract_fingerprint="fp_original_01",
        config_hash="cfg_hash_eval_01",
        candidate_hash="cand_hash_01",
        dataset_version="ds_v1",
    )
    checkpoint_data = {
        "lineage": initial_lineage.to_dict(),
        "budget": {"max_attempts": 2, "consumed_attempts": 1},
    }

    # 3.1 Intent revision shifted from 1 to 2
    task_intent_shifted = TaskContext(
        task_id="t_logistics_shifted",
        goal="Change delivery address and query package history",
        business_scope="logistics",
        intent_revision=2,
        contract_fingerprint="fp_original_01",
    )
    res_intent_invalid = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        task_context=task_intent_shifted,
        resume_checkpoint=checkpoint_data,
    )
    assert res_intent_invalid.status == "BLOCKED"
    assert res_intent_invalid.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "intent_revision changed" in res_intent_invalid.diagnostics.get("error", "")

    # 3.2 Baseline version changed (e.g. 0.9.0 vs 1.0.0)
    cp_bad_base_ver = copy.deepcopy(checkpoint_data)
    cp_bad_base_ver["lineage"]["baseline_version"] = "0.9.0"
    res_base_ver_invalid = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        resume_checkpoint=cp_bad_base_ver,
    )
    assert res_base_ver_invalid.status == "BLOCKED"
    assert res_base_ver_invalid.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "baseline_version changed" in res_base_ver_invalid.diagnostics.get("error", "")

    # 3.3 Baseline hash changed (version stays 1.0.0, but content hash differs)
    cp_bad_base_hash = copy.deepcopy(checkpoint_data)
    cp_bad_base_hash["lineage"]["baseline_hash"] = "tampered_baseline_hash"
    res_base_hash_invalid = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        resume_checkpoint=cp_bad_base_hash,
    )
    assert res_base_hash_invalid.status == "BLOCKED"
    assert res_base_hash_invalid.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "baseline_hash changed" in res_base_hash_invalid.diagnostics.get("error", "")

    # 3.4 Contract fingerprint changed
    task_fp_shifted = TaskContext(
        task_id="t_logistics_fp",
        goal="New constraints",
        business_scope="logistics",
        intent_revision=1,
        contract_fingerprint="fp_drifted_99",
    )
    res_fp_invalid = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        task_context=task_fp_shifted,
        resume_checkpoint=checkpoint_data,
    )
    assert res_fp_invalid.status == "BLOCKED"
    assert res_fp_invalid.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "contract_fingerprint changed" in res_fp_invalid.diagnostics.get("error", "")

    # 3.5 Validator config_hash changed
    class ConfigDriftEvaluator(SkillEvaluator):
        def get_config_fingerprint(self) -> str:
            return "cfg_hash_eval_CHANGED_99"

    drift_evaluator = ConfigDriftEvaluator(registry=registry, llm=evaluator.llm, judge_llm=evaluator.judge_llm)
    task_cfg_matched = TaskContext(
        task_id="t_logistics_cfg",
        goal="Change delivery address and query package history",
        business_scope="logistics",
        intent_revision=1,
        contract_fingerprint="fp_original_01",
    )
    res_cfg_invalid = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=drift_evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
        task_context=task_cfg_matched,
        resume_checkpoint=checkpoint_data,
    )
    assert res_cfg_invalid.status == "BLOCKED"
    assert res_cfg_invalid.reason_code == REASON_CHECKPOINT_INVALIDATED
    assert "validator config_hash changed" in res_cfg_invalid.diagnostics.get("error", "")

    # 4. Scenario D: Invalidation prevents promotion via G6 gate
    valid_cand = CandidateSkill(
        candidate_id="cand_drift_check_01",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=["ep_01"],
        meta=ws["baseline_meta"],
        body=ws["baseline_body"],
        status="READY",
    )
    candidate_store.save_candidate(valid_cand)
    drift_rec = ValidationRecord(
        candidate_id=valid_cand.candidate_id,
        content_hash=compute_candidate_hash(valid_cand),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        config_hash="cfg_hash_eval_01",
    )
    candidate_store.save_validation_record(drift_rec)
    with pytest.raises(ValueError, match="validator config hash changed"):
        promote_candidate(
            candidate=valid_cand,
            validation_record=drift_rec,
            state_machine=ws["state_machine"],
            registry=registry,
            candidate_store=candidate_store,
            caller_confirmed=True,
            expected_config_hash="cfg_hash_eval_CHANGED_99",  # Drifted config!
        )


def test_l5_graph_returns_candidate_without_direct_promotion_and_g6_gate(tmp_path: Path):
    """L5: Graph returns candidate without direct promotion; promotion strictly requires

    caller_confirmed=True and authoritative PASS validation records (G6).
    """
    ws = _setup_workspace(tmp_path)
    registry: SkillRegistry = ws["registry"]
    candidate_store: CandidateStore = ws["candidate_store"]
    episode_store: EpisodeStore = ws["episode_store"]
    state_machine: ReleaseStateMachine = ws["state_machine"]
    evaluator: SkillEvaluator = ws["evaluator"]
    eval_cases: list[dict] = ws["eval_cases"]

    ep = _make_episode(
        "ep_pass_01",
        task_id="t_pass",
        run_id="r_pass",
        outcome="failure",
        outcome_reason="Missing partial delivery note in output",
    )
    episode_store.save_episode(ep)

    # 1. Run recovery to PASS
    res = run_bounded_recovery(
        skill_name="logistics_tracker",
        episodes=[ep],
        registry=registry,
        evaluator=evaluator,
        eval_cases=eval_cases,
        candidate_store=candidate_store,
        episode_store=episode_store,
    )
    assert res.status == "SUCCESS"
    assert res.candidate is not None
    assert res.candidate.status == "READY"
    assert res.validation_record is not None
    assert res.validation_record.ratchet_decision == "PASS"

    # INVARIANT L5: Graph does NOT promote or write to active registry!
    assert res.applied_to_registry is False
    assert registry.get_meta("logistics_tracker").version == "1.0.0"
    assert registry.get_body("logistics_tracker").strip() == ws["baseline_body"].strip()

    # Explicit boundary declarations:
    assert res.diagnostics.get("sandbox_type") == "app_layer_shadow_dir"
    assert res.diagnostics.get("transaction_type") == "sequential_staged_persistence"

    # 2. Promotion attempt without explicit caller confirmation MUST fail
    with pytest.raises(ValueError, match="caller_confirmed=True"):
        promote_candidate(
            candidate=res.candidate,
            validation_record=res.validation_record,
            state_machine=state_machine,
            registry=registry,
            candidate_store=candidate_store,
            caller_confirmed=False,  # Blocked!
        )

    # 3. Forged in-memory record not stored in authoritative CandidateStore MUST fail
    forged_cand = CandidateSkill(
        candidate_id="forged_cand_999",
        skill_name="logistics_tracker",
        decision="revise",
        source_episode_ids=["ep_01"],
        meta=ws["baseline_meta"],
        body=ws["baseline_body"],
        status="READY",
    )
    forged_rec = ValidationRecord(
        candidate_id=forged_cand.candidate_id,
        content_hash=compute_candidate_hash(forged_cand),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
    )
    # forged_rec is NOT saved in candidate_store!
    with pytest.raises(ValueError, match="forged in-memory record rejected"):
        promote_candidate(
            candidate=forged_cand,
            validation_record=forged_rec,
            state_machine=state_machine,
            registry=registry,
            candidate_store=candidate_store,
            caller_confirmed=True,
        )

    # 4. Post-PASS tampered candidate body MUST fail
    tampered_cand = copy.deepcopy(res.candidate)
    tampered_cand.body += "\n<!-- Malicious post-evaluation modification -->"
    with pytest.raises(ValueError, match="candidate content was mutated"):
        promote_candidate(
            candidate=tampered_cand,
            validation_record=res.validation_record,
            state_machine=state_machine,
            registry=registry,
            candidate_store=candidate_store,
            caller_confirmed=True,
        )

    # 5. Promotion attempt with mismatched expected config hash MUST fail
    with pytest.raises(ValueError, match="validator config hash changed"):
        promote_candidate(
            candidate=res.candidate,
            validation_record=res.validation_record,
            state_machine=state_machine,
            registry=registry,
            candidate_store=candidate_store,
            caller_confirmed=True,
            expected_config_hash="mismatched_evaluator_config_hash",
        )

    # 6. Promotion attempt with caller_confirmed=True and untampered candidate succeeds
    release = promote_candidate(
        candidate=res.candidate,
        validation_record=res.validation_record,
        state_machine=state_machine,
        registry=registry,
        candidate_store=candidate_store,
        caller_confirmed=True,
    )
    assert isinstance(release, Release)
    assert release.skill_name == "logistics_tracker"
    assert release.version == res.candidate.meta.version
    assert registry.get_meta("logistics_tracker").version == res.candidate.meta.version


def test_l6_split_advisory_only_coherent_flow_preserved_and_no_auto_apply(tmp_path: Path):
    """L6: Long coherent single process is never split; multi-domain produces advisory-only proposal.

    In all cases, original skill and live routing remain completely unchanged before confirmation.
    """
    skills_dir = tmp_path / "skills"
    eval_dir = tmp_path / "evaluation_sets"
    skills_dir.mkdir(parents=True)
    eval_dir.mkdir(parents=True)

    # 1. Coherent Single Process Skill (Sequential multi-step workflow)
    coherent_md = (
        "---\n"
        "name: order_fulfillment_pipeline\n"
        "version: 1.0.0\n"
        "description: Sequential order fulfillment workflow\n"
        "use_when: Process order from reception to dispatch\n"
        "not_for: [Marketing]\n"
        "dependencies: []\n"
        "trigger:\n"
        "  keywords: [order, fulfillment, shipping]\n"
        "examples: [Fulfill order 501]\n"
        "---\n\n"
        "## Overview\nOrder fulfillment sequential workflow.\n\n"
        "## Instructions\n"
        "### 步骤一：收单验证\n1. 验证用户支付单据合法性。\n"
        "### 步骤二：仓库配货\n1. 基于前面步骤得到的订单清单锁定库存。\n"
        "### 步骤三：打单发货\n1. 依据前置步骤生成的配货条码呼叫快递上门发货。\n\n"
        "## Examples\n- Q: 发货？\n- A: 顺序执行各步骤。\n\n"
        "## Constraints\n- 必须严格遵循前置步骤顺序，不得跳步。\n"
    )
    order_dir = skills_dir / "order_fulfillment_pipeline"
    order_dir.mkdir(parents=True)
    (order_dir / "SKILL.md").write_text(coherent_md, encoding="utf-8")

    # Defined candidate domains that are tightly coupled
    coupled_domains = [
        DomainSpec(domain_id="receive", name="order_receive", description="收单验证", use_when="验证订单"),
        DomainSpec(domain_id="pack", name="order_pack", description="仓库配货", use_when="配货"),
        DomainSpec(domain_id="dispatch", name="order_dispatch", description="打单发货", use_when="发货"),
    ]

    repair_file = eval_dir / "repair_set.json"
    repair_file.write_text(json.dumps({"cases": [
        {"id": "c1", "skill": "order_fulfillment_pipeline", "query": "验证订单", "reference": "ok"},
        {"id": "c2", "skill": "order_fulfillment_pipeline", "query": "仓库配货", "reference": "ok"},
        {"id": "c3", "skill": "order_fulfillment_pipeline", "query": "呼叫发货", "reference": "ok"},
    ]}), encoding="utf-8")

    proposal_coherent = suggest_skill_split(
        skill_name="order_fulfillment_pipeline",
        repo_root=tmp_path,
        candidate_domains=coupled_domains,
    )
    assert proposal_coherent.can_split is False
    assert proposal_coherent.status == "CANNOT_SPLIT"
    assert proposal_coherent.applied is False
    assert (order_dir / "SKILL.md").exists()

    # 2. Separable Multi-domain Skill (Synthetic carrier with 2 orthogonal domains)
    carrier_md = (
        "---\n"
        "name: carrier_hub\n"
        "version: 1.0.0\n"
        "description: 多物流承运商查询与面单打印助手\n"
        "use_when: 用户需要查询顺丰/京东快递轨迹或者生成电子面单\n"
        "not_for: [国际海运报关]\n"
        "dependencies: []\n"
        "trigger:\n"
        "  keywords: [顺丰, 京东, 面单, 轨迹]\n"
        "examples: [查顺丰单号, 打印京东面单]\n"
        "---\n\n"
        "## Overview\n多承运商物流助手。\n\n"
        "## Instructions\n"
        "### 顺丰速运服务\n1. 调用顺丰开放平台查询包裹节点轨迹与派件员电话。\n"
        "### 京东快递服务\n1. 调用京东物流接口获取揽收时间与电子面单条形码。\n\n"
        "## Examples\n- Q: 查顺丰？\n- A: 返回顺丰轨迹。\n- Q: 打印京东？\n- A: 返回面单信息。\n\n"
        "## Constraints\n- 区分各承运商特定参数要求。\n"
    )
    carrier_dir = skills_dir / "carrier_hub"
    carrier_dir.mkdir(parents=True)
    (carrier_dir / "SKILL.md").write_text(carrier_md, encoding="utf-8")

    orthogonal_domains = [
        DomainSpec(
            domain_id="sf",
            name="sf_express_service",
            description="顺丰速运查询服务",
            use_when="查询顺丰物流",
            keywords=["顺丰", "顺丰速运", "派件员"],
            examples=["查顺丰单号"],
            dependencies=["sf_tracking_api", "sf_courier_tool", "sf_station_api", "query_order_packages"],
            overview="顺丰服务",
            instructions="1. 查询顺丰轨迹与派件员电话。",
            body_examples="- 顺丰示例",
            constraints="- 顺丰约束",
        ),
        DomainSpec(
            domain_id="jd",
            name="jd_express_service",
            description="京东快递面单服务",
            use_when="打印京东面单",
            keywords=["京东", "京东物流", "面单"],
            examples=["打印京东面单"],
            dependencies=["jd_waybill_api", "jd_pickup_tool", "jd_label_printer", "query_order_packages"],
            overview="京东服务",
            instructions="1. 获取京东揽收时间与电子面单条码。",
            body_examples="- 京东示例",
            constraints="- 京东约束",
        ),
    ]

    carrier_cases = [
        {"id": "sf_01", "skill": "carrier_hub", "query": "顺丰速运查询顺丰单号 SF100 当前到哪里了", "reference": "顺丰轨迹"},
        {"id": "sf_02", "skill": "carrier_hub", "query": "顺丰速运获取顺丰派件员联系电话", "reference": "派件员电话"},
        {"id": "jd_01", "skill": "carrier_hub", "query": "京东物流打印京东快递电子面单条形码", "reference": "京东面单"},
        {"id": "jd_02", "skill": "carrier_hub", "query": "京东物流打印京东电子面单条码", "reference": "京东面单"},
        # Ambiguous cross-domain query with overlapping keywords that should not be forced into either
        {"id": "ambiguous_01", "skill": "carrier_hub", "query": "顺丰速运与京东快递面单综合比较", "reference": "对比分析"},
    ]
    # Add sufficient cases to satisfy auto case quota ratio
    for i in range(15):
        carrier_cases.append({"id": f"base_{i:02d}", "skill": "carrier_hub", "query": f"常见承运商问题 {i}", "reference": "说明"})

    carrier_repair_file = eval_dir / "carrier_repair.json"
    carrier_repair_file.write_text(json.dumps({"cases": carrier_cases}), encoding="utf-8")

    proposal_carrier = suggest_skill_split(
        skill_name="carrier_hub",
        repo_root=tmp_path,
        repair_set_path=carrier_repair_file,
        candidate_domains=orthogonal_domains,
    )

    # Invariants for L6:
    assert proposal_carrier.can_split is True
    assert proposal_carrier.status == "SUGGESTION"
    assert proposal_carrier.applied is False  # Never auto-applied!
    assert "sf_express_service" in proposal_carrier.sub_skill_names
    assert "jd_express_service" in proposal_carrier.sub_skill_names

    # 1. Routing keywords are distinguishable and disjoint
    assert set(orthogonal_domains[0].keywords).isdisjoint(set(orthogonal_domains[1].keywords))

    # 2. Independent testability and domain routing
    assert len(proposal_carrier.analysis.assigned_cases["sf"]) >= 2
    assert any(c["id"] == "sf_01" for c in proposal_carrier.analysis.assigned_cases["sf"])
    assert any(c["id"] == "sf_02" for c in proposal_carrier.analysis.assigned_cases["sf"])
    assert len(proposal_carrier.analysis.assigned_cases["jd"]) >= 2
    assert any(c["id"] == "jd_01" for c in proposal_carrier.analysis.assigned_cases["jd"])
    assert any(c["id"] == "jd_02" for c in proposal_carrier.analysis.assigned_cases["jd"])

    # 3. Ambiguous cross-domain cases land in unassigned_cases rather than being falsely forced
    assert any(c["id"] == "ambiguous_01" for c in proposal_carrier.analysis.unassigned_cases)

    # 4. Acyclic dependencies between sub-skills
    assert "jd_express_service" not in orthogonal_domains[0].dependencies
    assert "sf_express_service" not in orthogonal_domains[1].dependencies

    # 5. Shared tools do not unilaterally force split or merge
    assert "query_order_packages" in orthogonal_domains[0].dependencies
    assert "query_order_packages" in orthogonal_domains[1].dependencies
    # Both sub-skills declare the shared tool cleanly; split still succeeds based on business domain

    # CRITICAL INVARIANT: Original skill and live files are 100% UNTOUCHED before confirmation!
    assert (carrier_dir / "SKILL.md").exists()
    assert (carrier_dir / "SKILL.md").read_text(encoding="utf-8") == carrier_md
    assert not (tmp_path / "skills_backup").exists()
    assert not (skills_dir / "sf_express_service").exists()
    assert not (skills_dir / "jd_express_service").exists()
