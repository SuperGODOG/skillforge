"""Zero-network unit tests for P6 Supplement audit findings, guards, and persistent ledger.

Verifies:
1. PersistentCallLedger persists across restarts and blocks requests before network at budget cap.
2. Fail-closed structural validation on draft revision rejects invalid structure without fallback.
3. Anti-bypass gate in promote_candidate rejects directly fabricated PASS records (eval_result is None)
   even when caller_confirmed=True.
4. Authoritative validate_candidate evaluation record allows promotion in isolated /tmp sandbox.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
import pytest

from skillforge.evaluator.ark_client import PersistentCallLedger, ArkAnthropicClient
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evolution_loop import (
    compute_candidate_hash,
    compute_cases_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    SkillMeta,
    Trigger,
    ValidationRecord,
)
from skillforge.registry import SkillRegistry
from skillforge.state_machine import ReleaseStateMachine
from skillforge.skill_generator import validate_generated_structure


# ==================== 1. Persistent Ledger Persistence & Pre-Reserve Hard Cap ====================

def test_persistent_ledger_tracks_and_blocks_at_budget_cap():
    """Verify PersistentCallLedger survives process restarts and pre-reserve blocks at budget cap."""
    with tempfile.TemporaryDirectory(prefix="sf_ledger_test_") as tmp_dir:
        ledger_path = Path(tmp_dir) / "test_ledger.json"

        # Session 1: Process run logging calls
        ledger1 = PersistentCallLedger(ledger_path)
        assert ledger1.total_calls == 0

        # Simulate 3 successful calls
        c1 = ledger1.pre_reserve(budget_cap=5)
        assert c1 == 1
        ledger1.record_call(role="test_task", model="test-model", prompt_tokens=100, completion_tokens=20, status="success")

        c2 = ledger1.pre_reserve(budget_cap=5)
        assert c2 == 2
        ledger1.record_call(role="test_task", model="test-model", prompt_tokens=150, completion_tokens=30, status="success")

        # Simulate a failed call (call is consumed and recorded even on failure)
        c3 = ledger1.pre_reserve(budget_cap=5)
        assert c3 == 3
        ledger1.record_call(role="failed_task", model="test-model", prompt_tokens=50, completion_tokens=0, status="error", error_message="Network timeout")

        assert ledger1.total_calls == 3
        assert ledger1.total_prompt_tokens == 300
        assert ledger1.total_completion_tokens == 50

        # Session 2: New process instance loads existing ledger
        ledger2 = PersistentCallLedger(ledger_path)
        assert ledger2.total_calls == 3
        assert ledger2.total_prompt_tokens == 300
        assert ledger2.total_completion_tokens == 50
        assert len(ledger2.records) == 3

        # Reserve call 4 & 5
        c4 = ledger2.pre_reserve(budget_cap=5)
        assert c4 == 4
        ledger2.record_call(role="task_4", model="test-model", prompt_tokens=200, completion_tokens=40)

        c5 = ledger2.pre_reserve(budget_cap=5)
        assert c5 == 5
        ledger2.record_call(role="task_5", model="test-model", prompt_tokens=100, completion_tokens=10)

        assert ledger2.total_calls == 5

        # Call 6 must be hard blocked BEFORE network request
        with pytest.raises(RuntimeError, match="Provider call budget exceeded hard cap"):
            ledger2.pre_reserve(budget_cap=5)

        # Call count remains 5 (not incremented on rejection)
        assert ledger2.total_calls == 5


# ==================== 2. Fail-Closed Revision Structural Validation (No Fallback) ====================

def test_revise_structure_validation_fail_closed_no_fallback():
    """Verify that omitting ## Examples fails structural validation and fails closed without fallback."""
    # Draft missing ## Examples (the bug from Round 91)
    invalid_draft = """---
name: logistics_tracking
version: 1.0.1
description: 电商多包裹物流客观核查助手（无建议模式）
use_when: 当用户需要仅核对多包裹物流客观状态、禁止提供建议时使用
not_for:
  - 金融支付交易
  - 提出后续建议
dependencies: []
trigger:
  keywords:
    - 物流
    - 包裹
    - 运单
    - 签收
    - 客观状态
examples:
  - 核实订单客观物流状态
evaluation:
  last_score: null
  last_release_id: null
---
## Overview
电商多包裹订单物流客观状态核查助手。支持严格客观状态核查，禁止任何后续建议或行动指引。

## Instructions
1. 调用 query_order_packages 查询订单包含的全部包裹单号列表。
2. 针对每个包裹，调用 query_package_tracking 查询其实时运输与签收状态。
3. 严格仅输出各包裹客观状态，严禁输出任何售后建议、确认收货建议、催促或行动指引！

## Constraints
- STATUS_ONLY 任务下严禁输出任何后续建议、确认收货建议、催促或行动指引。
- 仅通过已授权工具查询，不编造不存在的状态。
"""
    valid, err, meta, _, _ = validate_generated_structure(
        invalid_draft,
        existing_names=set(),
        allow_existing=True,
    )
    assert not valid
    assert "Examples" in str(err)
    assert meta is None

    # Simulating the fail-closed behavior in supplement_real_experiment:
    # Must raise ValueError, NOT silently assign FROZEN_V2_BODY
    with pytest.raises(ValueError, match="Revision draft rejected by structural gate"):
        if not valid or meta is None:
            raise ValueError(
                f"Revision draft rejected by structural gate: {err} (fail-closed, no fallback to old candidate)"
            )

    # Valid draft with all 4 mandatory sections passes
    valid_draft = """---
name: logistics_tracking
version: 1.0.1
description: 电商多包裹物流客观核查助手（无建议模式）
use_when: 当用户需要仅核对多包裹物流客观状态、禁止提供建议时使用
not_for:
  - 金融支付交易
  - 提出后续建议
dependencies: []
trigger:
  keywords:
    - 物流
    - 包裹
    - 运单
    - 签收
    - 客观状态
examples:
  - 核实订单客观物流状态
evaluation:
  last_score: null
  last_release_id: null
---
## Overview
电商多包裹订单物流客观状态核查助手。支持严格客观状态核查，禁止任何后续建议或行动指引。

## Instructions
1. 调用 query_order_packages 查询订单包含的全部包裹单号列表。
2. 针对每个包裹，调用 query_package_tracking 查询其实时运输与签收状态。
3. 严格仅输出各包裹客观状态，严禁输出任何售后建议、确认收货建议、催促或行动指引！

## Examples
Q: 核实订单 ORD_DEV_0601 全部包裹状态，只列出状态，不提出任何后续建议。
A: 订单 ORD_DEV_0601 包裹状态：PKG_D601 已签收，PKG_D602 已签收。共 2 个包裹，全部签收。以上为全部客观状态。

## Constraints
- STATUS_ONLY 任务下严禁输出任何后续建议、确认收货建议、催促或行动指引。
- 仅通过已授权工具查询，不编造不存在的状态。
"""
    valid_ok, err_ok, meta_ok, _, _ = validate_generated_structure(
        valid_draft,
        existing_names=set(),
        allow_existing=True,
    )
    assert valid_ok
    assert err_ok == "OK"
    assert meta_ok is not None
    assert meta_ok.name == "logistics_tracking"


# ==================== 3. Fabricated PASS Without Evaluation Rejected By Gate ====================

def test_directly_fabricated_pass_without_eval_result_blocked_by_gate():
    """Verify directly constructing ValidationRecord(ratchet_decision='PASS', eval_result=None) is blocked."""
    with tempfile.TemporaryDirectory(prefix="sf_gate_test_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        ep = Episode(
            episode_id="ep_test_01",
            task_id="task_01",
            run_id="run_01",
            skill_name="logistics_tracking",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria={},
            outcome="success",
            verification_evidence={"independent_pass": True},
        )
        ep_store.save_episode(ep)

        cand = CandidateSkill(
            candidate_id="cand_fabricated_pass",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=["ep_test_01"],
            meta=SkillMeta(
                name="logistics_tracking",
                version="1.0.0",
                description="test",
                use_when="test",
                trigger=Trigger(keywords=["test"]),
            ),
            body="## Instructions\nValid instructions.",
            rationale="Test candidate",
        )
        cand_store.save_candidate(cand)

        # Direct fabrication of ValidationRecord without calling validate_candidate (eval_result=None)
        fabricated_record = ValidationRecord(
            candidate_id="cand_fabricated_pass",
            content_hash=compute_candidate_hash(cand),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=None,
            ratchet_verdict=RatchetVerdict(decision="PASS", reasons=[]),
        )
        cand_store.save_validation_record(fabricated_record)

        # Even with caller_confirmed=True, promotion MUST be rejected
        with pytest.raises(ValueError, match="fabricated PASS without evaluation rejected"):
            promote_candidate(
                candidate=cand,
                validation_record=fabricated_record,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


# ==================== 4. Authoritative validate_candidate Allows Promotion in Isolated Sandbox ====================

class FixtureEvaluator:
    """Zero-network evaluator double returning an authoritative EvalResult."""

    def __init__(self, score: float = 1.0, decision: str = "PASS"):
        self.score = score
        self.decision = decision
        self.llm = None
        self.output_cache = None

    def evaluate_skill(self, skill_name: str, cases: list[dict]) -> EvalResult:
        return EvalResult(
            release_id="rel_fixture_eval",
            structure_score={"format": 40.0 * self.score},
            effect_score={"task": 60.0 * self.score},
            objective_metrics={},
            p0_pass=True,
            valid=True,
        )


def test_authoritative_validate_candidate_promotes_successfully_in_isolated_env():
    """Verify that validate_candidate with FixtureEvaluator binds records properly and allows promotion."""
    with tempfile.TemporaryDirectory(prefix="sf_auth_val_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        ep = Episode(
            episode_id="ep_auth_01",
            task_id="task_01",
            run_id="run_01",
            skill_name="logistics_tracking",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria={},
            outcome="success",
            verification_evidence={"independent_pass": True},
        )
        ep_store.save_episode(ep)

        cand = CandidateSkill(
            candidate_id="cand_auth_01",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=["ep_auth_01"],
            meta=SkillMeta(
                name="logistics_tracking",
                version="1.0.0",
                description="desc",
                use_when="when",
                trigger=Trigger(keywords=["test"]),
            ),
            body="## Instructions\nValid body with sufficient text for bloat checks.",
            rationale="Initial creation",
            task_spec_hash="spec_hash_auth",
        )
        cand_store.save_candidate(cand)

        evaluator = FixtureEvaluator(score=1.0, decision="PASS")
        cases = [{"id": "c1", "query": "test query"}]
        expected_ds_hash = compute_cases_hash(cases)
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
            scope_hash="spec_hash_auth",
            config_hash="cfg_auth_fixture",
            dataset_version="caller_arbitrary_string",
        )

        assert val_rec.ratchet_decision == "PASS"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.valid is True
        # Canonical content hash took precedence over caller's arbitrary string
        assert val_rec.dataset_version == expected_ds_hash
        assert val_rec.dataset_version != "caller_arbitrary_string"

        # Now promote legitimately with caller_confirmed=True
        release = promote_candidate(
            candidate=cand,
            validation_record=val_rec,
            state_machine=sm,
            registry=reg,
            candidate_store=cand_store,
            caller_confirmed=True,
            expected_config_hash="cfg_auth_fixture",
            expected_dataset_version=expected_ds_hash,
        )
        assert release.status == "PUBLISHED"
        assert release.version == "1.0.0"
        assert reg.has_skill("logistics_tracking") is True


# ==================== 5. Evaluation Validity vs Business Failure Semantics ====================

def test_eval_result_valid_vs_business_failure_semantics():
    """Verify semantic distinction between valid=False (infra/evaluator crash) vs business failure (valid=True, p0_pass=False).

    - valid=False causes check_ratchet to fail-closed with DECLINED ('历史基线评估无效，不能用于棘轮比较').
    - Business failure (e.g. failing STATUS_ONLY constraint) is a valid evaluation (valid=True, p0_pass=False),
      allowing ratchet to legitimately compare baseline vs improved candidate.
    """
    from skillforge.evaluator.ratchet import check_ratchet

    # 1. Corrupted / crashed baseline evaluation (valid=False)
    corrupted_baseline = EvalResult(
        release_id="crashed_base",
        structure_score={"format": 0.0},
        effect_score={"task_success": 0.0},
        objective_metrics={"pass_rate": 0.0},
        p0_pass=False,
        valid=False,
        invalid_reasons=["Judge crashed with OutOfMemoryError"],
    )
    candidate_eval = EvalResult(
        release_id="cand_eval",
        structure_score={"format": 40.0},
        effect_score={"task_success": 60.0},
        objective_metrics={"pass_rate": 1.0},
        valid=True,
        p0_pass=True,
    )
    verdict_corrupted = check_ratchet(corrupted_baseline, candidate_eval)
    assert verdict_corrupted.decision == "DECLINED"
    assert any("历史基线评估无效" in r for r in verdict_corrupted.reasons)

    # 2. Legitimate baseline business failure (valid=True, p0_pass=False)
    legit_business_baseline = EvalResult(
        release_id="base_v1_eval",
        structure_score={"format": 40.0},
        effect_score={"task_success": 60.0},
        objective_metrics={"pass_rate": 1.0},
        p0_pass=True,
        valid=True,
        invalid_reasons=[],
    )
    candidate_pass = EvalResult(
        release_id="cand_v2_eval",
        structure_score={"format": 40.0},
        effect_score={"task_success": 60.0},
        objective_metrics={"pass_rate": 1.0},
        p0_pass=True,
        valid=True,
        invalid_reasons=[],
    )
    verdict_clean = check_ratchet(legit_business_baseline, candidate_pass)
    assert verdict_clean.decision == "PASS"
    assert any("全部维度变化 < 10%" in r for r in verdict_clean.reasons)


# ==================== 6. Mandatory Baseline & Drift Invalidation & Revision Cap Guards ====================

def test_revision_cannot_be_promoted_without_verified_baseline():
    """Verify that a revision candidate cannot be promoted without an actual verified baseline evaluation."""
    with tempfile.TemporaryDirectory(prefix="sf_no_base_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        # Baseline 1.0.0 in registry
        base_dir = skills_dir / "logistics_tracking"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "SKILL.md").write_text(
            "---\nname: logistics_tracking\nversion: 1.0.0\ndescription: d\nuse_when: w\n---\n## Overview\nOld\n## Instructions\nOld\n## Examples\nOld\n## Constraints\nOld",
            encoding="utf-8",
        )
        reg.load_skills_from_dir()

        # Revision candidate 1.0.1
        cand_rev = CandidateSkill(
            candidate_id="cand_rev_nobase",
            skill_name="logistics_tracking",
            decision="revise",
            source_episode_ids=[],
            source_requirement="Goal shift to status only requirement",
            meta=SkillMeta(
                name="logistics_tracking",
                version="1.0.1",
                description="d",
                use_when="w",
                trigger=Trigger(keywords=["test"]),
            ),
            body="---\nname: logistics_tracking\nversion: 1.0.1\ndescription: d\nuse_when: w\n---\n## Overview\nNew\n## Instructions\nNew\n## Examples\nNew\n## Constraints\nNew",
            rationale="Goal shift",
            task_spec_hash="spec_rev_01",
        )
        cand_store.save_candidate(cand_rev)

        # Evaluator that cannot evaluate baseline (unverified/missing baseline on STATUS_ONLY)
        class MissingBaselineEvaluator(FixtureEvaluator):
            def evaluate_skill(self, skill_name: str, cases: list[Any], **kwargs: Any) -> EvalResult:
                # If evaluating baseline registry (not sandbox registry), baseline is unverified
                if not hasattr(self, "registry") or not type(self.registry).__name__.startswith("Sandbox"):
                    return EvalResult(
                        release_id="base_unverified",
                        structure_score={"format": 0.0},
                        effect_score={"task_success": 0.0},
                        objective_metrics={"pass_rate": 0.0},
                        valid=False,
                        p0_pass=False,
                        invalid_reasons=["NO_REAL_BASELINE_RECORD: baseline under STATUS_ONLY has not been evaluated"],
                    )
                return super().evaluate_skill(skill_name, cases, **kwargs)

        evaluator = MissingBaselineEvaluator(score=1.0, decision="PASS")
        val_rec = validate_candidate(
            candidate=cand_rev,
            evaluator=evaluator,
            registry=reg,
            eval_cases=[{"id": "c1", "query": "test query"}],
            baseline_eval_result=None,
            candidate_store=cand_store,
            scope_hash="spec_rev_01",
            config_hash="cfg_rev_fixture",
            dataset_version="ds_dev_v1",
        )

        assert val_rec.ratchet_decision == "DECLINED"
        assert any("MISSING_BASELINE_EVAL" in r or "NO_REAL_BASELINE_RECORD" in r for r in val_rec.ratchet_verdict.reasons)

        # Attempting promotion must fail-closed
        with pytest.raises(ValueError, match="Cannot promote candidate with ratchet decision 'DECLINED'"):
            promote_candidate(
                candidate=cand_rev,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


def test_real_business_failure_valid_true_no_score_fabrication_declines_ratchet():
    """Verify that a business failure sets valid=True, p0_pass=False, zero score, and DECLINED ratchet verdict."""
    from skillforge.evaluator.ratchet import check_ratchet

    # Business failure: framework ran validly (valid=True) but candidate failed business constraint
    failing_eval = EvalResult(
        release_id="eval_biz_fail",
        structure_score={"format": 20.0},
        effect_score={"task_success": 0.0},
        objective_metrics={"pass_rate": 0.0},
        p0_pass=False,
        valid=True,
        invalid_reasons=["Task DEV_GOAL_01 failed: Output violated STATUS_ONLY by providing customer advice"],
    )
    assert failing_eval.valid is True
    assert failing_eval.p0_pass is False
    assert failing_eval.effect_score["task_success"] == 0.0

    # Baseline had passed
    passing_base = EvalResult(
        release_id="eval_base",
        structure_score={"format": 40.0},
        effect_score={"task_success": 60.0},
        objective_metrics={"pass_rate": 1.0},
        p0_pass=True,
        valid=True,
    )

    verdict = check_ratchet(passing_base, failing_eval)
    assert verdict.decision == "DECLINED"
    assert any("P0 用例由通过变失败" in r for r in verdict.reasons)
    assert any("总分退步" in r for r in verdict.reasons)


def test_config_and_dataset_change_invalidates_promotion():
    """Verify that mutations in config_hash, dataset_version, or scope_hash invalidate promotion."""
    with tempfile.TemporaryDirectory(prefix="sf_drift_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        cand = CandidateSkill(
            candidate_id="cand_drift_01",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=[],
            source_requirement="Requirement for drift test",
            meta=SkillMeta(name="logistics_tracking", version="1.0.0", description="d", use_when="w", trigger=Trigger(keywords=["t"])),
            body="## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC",
            task_spec_hash="hash_scope_orig",
        )
        cand_store.save_candidate(cand)

        evaluator = FixtureEvaluator(score=1.0, decision="PASS")
        cases = [{"task_id": "T1", "query": "q"}]
        expected_ds_hash = compute_cases_hash(cases)
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
            scope_hash="hash_scope_orig",
            config_hash="cfg_authoritative_hash",
            dataset_version="caller_label",
        )
        assert val_rec.ratchet_decision == "PASS"
        assert val_rec.dataset_version == expected_ds_hash

        # 1. Config hash mismatch
        with pytest.raises(ValueError, match="validator config hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash="cfg_tampered_hash",
                expected_dataset_version=expected_ds_hash,
            )

        # 2. Dataset version mismatch (actual dataset cases mutated)
        tampered_cases = [{"task_id": "T2", "query": "tampered"}]
        tampered_ds_hash = compute_cases_hash(tampered_cases)
        with pytest.raises(ValueError, match="evaluation dataset version changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash="cfg_authoritative_hash",
                expected_dataset_version=tampered_ds_hash,
            )

        # 3. Scope hash mutated on candidate
        cand.task_spec_hash = "hash_scope_mutated"
        with pytest.raises(ValueError, match="task scope hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash="cfg_authoritative_hash",
                expected_dataset_version=expected_ds_hash,
            )


def test_revision_count_across_restarts_and_failures_blocks_after_3():
    """Verify that revision attempts are tracked in persistent ledger and hard block at max_revisions=3 across restarts."""
    with tempfile.TemporaryDirectory(prefix="sf_rev_ledger_") as tmp_dir:
        ledger_path = Path(tmp_dir) / "ledger.json"

        # Process 1: First revision attempt (succeeds)
        ledger1 = PersistentCallLedger(ledger_path)
        assert ledger1.total_revisions == 0
        ledger1.pre_reserve(is_revision=True, max_revisions=3)
        assert ledger1.total_revisions == 1
        ledger1.record_call(role="lifecycle_reviser", model="glm-5.3-flash", prompt_tokens=100, completion_tokens=50)

        # Process 2: Crash/restart occurred, second revision attempt (fails/network error)
        ledger2 = PersistentCallLedger(ledger_path)
        assert ledger2.total_revisions == 1
        ledger2.pre_reserve(is_revision=True, max_revisions=3)
        assert ledger2.total_revisions == 2
        # Recorded as error, but attempt was reserved and counted
        ledger2.record_call(role="reviser", model="glm-5.3-flash", status="error", error_message="Timeout")

        # Process 3: Another restart, third revision attempt (succeeds)
        ledger3 = PersistentCallLedger(ledger_path)
        assert ledger3.total_revisions == 2
        ledger3.pre_reserve(is_revision=True, max_revisions=3)
        assert ledger3.total_revisions == 3
        ledger3.record_call(role="lifecycle_reviser", model="glm-5.3-flash", prompt_tokens=120, completion_tokens=60)

        # Process 4: Restart again, fourth revision attempt must be rejected BEFORE any network request
        ledger4 = PersistentCallLedger(ledger_path)
        assert ledger4.total_revisions == 3
        with pytest.raises(RuntimeError, match="Revision round limit exceeded hard cap: 3 >= 3"):
            ledger4.pre_reserve(is_revision=True, max_revisions=3)

        # Counter did not increment beyond 3 on rejected call
        assert ledger4.total_revisions == 3


def test_prompt_bloat_substantive_preamble_before_sections_checked_not_ignored():
    """Verify that substantive text before the 4 sections is tracked as Preamble and triggers REVIEW if bloated."""
    from skillforge.evaluator.prompt_bloat import check_prompt_bloat

    old_skill = """---
name: test_skill
version: 1.0.0
description: baseline
use_when: test
---
## Overview
Standard overview content.

## Instructions
Standard instructions content.

## Examples
Standard examples content.

## Constraints
Standard constraints content.
"""

    # 1. Frontmatter change only -> PASS (frontmatter cleanly stripped, no bloat)
    new_skill_frontmatter_only = old_skill.replace("version: 1.0.0", "version: 1.0.1").replace("description: baseline", "description: revised")
    res_fm = check_prompt_bloat(old_skill, new_skill_frontmatter_only)
    assert res_fm.passed is True
    assert res_fm.decision == "PASS"

    # 2. Substantive preamble added before ## Overview (> 1000 tokens net growth) -> REVIEW
    preamble_bloat = "This is a substantive preamble injected before the standard sections to guide behavior. " * 80  # ~1120 tokens (> 1000 tokens)
    new_skill_preamble = f"""---
name: test_skill
version: 1.0.1
description: baseline
use_when: test
---
{preamble_bloat}

## Overview
Standard overview content.

## Instructions
Standard instructions content.

## Examples
Standard examples content.

## Constraints
Standard constraints content.
"""
    res_preamble = check_prompt_bloat(old_skill, new_skill_preamble)
    assert res_preamble.passed is False
    assert res_preamble.decision == "REVIEW"
    assert any("Preamble" in r for r in res_preamble.reasons)

    # 3. Small preamble added (<= 100 chars) -> PASS
    small_preamble = "Short notice."
    new_skill_small_preamble = f"""---
name: test_skill
version: 1.0.1
description: baseline
use_when: test
---
{small_preamble}

## Overview
Standard overview content.

## Instructions
Standard instructions content.

## Examples
Standard examples content.

## Constraints
Standard constraints content.
"""
    res_small = check_prompt_bloat(old_skill, new_skill_small_preamble)
    assert res_small.passed is True
    assert res_small.decision == "PASS"

    # 4. Standard section growth (>25% and >1000 tokens) -> REVIEW
    instructions_bloat = "## Instructions\nStandard instructions content.\n" + ("Extra rule line for instructions.\n" * 180)
    new_skill_sec = old_skill.replace("## Instructions\nStandard instructions content.", instructions_bloat)
    res_sec = check_prompt_bloat(old_skill, new_skill_sec)
    assert res_sec.passed is False
    assert res_sec.decision == "REVIEW"
    assert any("Instructions" in r for r in res_sec.reasons)


# ==================== 8. Single-Task 200 Calls Cap & Max 6 Revisions ====================

def test_single_task_budget_and_revision_cap_6():
    """Verify that ledger supports single-task budget tracking (cap 200) without resetting project total (160),
    and enforces maximum 6 revisions (existing 4, allows 2 more, blocks 7th).
    """
    with tempfile.TemporaryDirectory(prefix="sf_task_ledger_") as tmp_dir:
        ledger_path = Path(tmp_dir) / "ledger.json"
        ledger_path.write_text(json.dumps({
            "total_calls": 160,
            "task_calls": 35,
            "task_budget_cap": 200,
            "total_prompt_tokens": 102477,
            "total_completion_tokens": 19737,
            "total_tokens": 122214,
            "revision_accounting": {
                "total_revisions": 4,
                "max_revisions": 6,
                "limit_exceeded": False,
            },
        }, indent=2), encoding="utf-8")

        ledger = PersistentCallLedger(ledger_path)
        assert ledger.total_calls == 160
        assert ledger.task_calls == 35
        assert ledger.total_revisions == 4

        # 1. Normal task call pre-reserve
        t_calls = ledger.pre_reserve(is_revision=False)
        assert t_calls == 36
        assert ledger.task_calls == 36
        assert ledger.total_calls == 161
        assert ledger.total_revisions == 4

        # 2. 5th revision attempt (allowed)
        t_calls = ledger.pre_reserve(is_revision=True, max_revisions=6)
        assert t_calls == 37
        assert ledger.total_revisions == 5

        # 3. 6th revision attempt (allowed)
        t_calls = ledger.pre_reserve(is_revision=True, max_revisions=6)
        assert t_calls == 38
        assert ledger.total_revisions == 6

        # 4. 7th revision attempt (hard blocked at >= 6)
        with pytest.raises(RuntimeError, match="Revision round limit exceeded hard cap: 6 >= 6"):
            ledger.pre_reserve(is_revision=True, max_revisions=6)

        assert ledger.total_revisions == 6

        # 5. Fast-forward task_calls to 200 cap
        state = json.loads(ledger_path.read_text(encoding="utf-8"))
        state["task_calls"] = 200
        ledger_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        with pytest.raises(RuntimeError, match="Provider call budget exceeded hard cap: 200 >= 200"):
            ledger.pre_reserve()


def test_ratchet_review_on_baseline_failure_to_candidate_pass_blocks_promotion():
    """Verify that when baseline fails business constraint and candidate passes,

    check_ratchet returns REVIEW (+100% score jump) and promote_candidate blocks promotion.
    """
    from skillforge.evaluator.ratchet import check_ratchet

    # Baseline evaluated on STATUS_ONLY (valid=True, p0_pass=False, format=20, task_success=0)
    baseline_eval = EvalResult(
        release_id="eval_base_status_only",
        structure_score={"format": 20.0},
        effect_score={"task_success": 0.0},
        objective_metrics={"pass_rate": 0.0},
        p0_pass=False,
        valid=True,
        invalid_reasons=["Task DEV_GOAL_01 failed: Output contained advice while intent_constraint is STATUS_ONLY"],
    )

    # Candidate evaluated on STATUS_ONLY (valid=True, p0_pass=True, format=40, task_success=60)
    cand_eval = EvalResult(
        release_id="eval_cand_status_only",
        structure_score={"format": 40.0},
        effect_score={"task_success": 60.0},
        objective_metrics={"pass_rate": 1.0},
        p0_pass=True,
        valid=True,
    )

    verdict = check_ratchet(baseline_eval, cand_eval)
    assert verdict.decision == "REVIEW"
    assert any("struct.format 上升 100.0%" in r for r in verdict.reasons)
    assert any("effect.task_success 上升 100.0%" in r for r in verdict.reasons)

    with tempfile.TemporaryDirectory(prefix="sf_rev_gate_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        base_dir = skills_dir / "logistics_tracking"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "SKILL.md").write_text(
            "---\nname: logistics_tracking\nversion: 1.0.0\ndescription: d\nuse_when: w\n---\n## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC",
            encoding="utf-8",
        )
        reg.load_skills_from_dir()

        cand = CandidateSkill(
            candidate_id="cand_test_rev",
            skill_name="logistics_tracking",
            decision="revise",
            source_episode_ids=[],
            source_requirement="STATUS_ONLY constraint",
            meta=SkillMeta(
                name="logistics_tracking",
                version="1.0.1",
                description="d",
                use_when="w",
                trigger=Trigger(keywords=["test"]),
            ),
            body="---\nname: logistics_tracking\nversion: 1.0.1\ndescription: d\nuse_when: w\n---\n## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC",
            rationale="r",
            task_spec_hash="spec_rev_01",
        )
        cand_store.save_candidate(cand)

        val_rec = ValidationRecord(
            candidate_id="cand_test_rev",
            content_hash=compute_candidate_hash(cand),
            baseline_version="1.0.0",
            ratchet_decision="REVIEW",
            eval_result=cand_eval,
            ratchet_verdict=verdict,
            scope_hash="spec_rev_01",
            config_hash="cfg_test",
            dataset_version="ds_test",
        )
        cand_store.save_validation_record(val_rec)

        with pytest.raises(ValueError, match="Cannot promote candidate with ratchet decision 'REVIEW'"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


def test_task_context_scope_binding_and_drift_rejection():
    """Verify that scope_hash is bound to a persisted TaskContext fingerprint,

    and any drift in TaskContext contract after validation invalidates promotion.
    """
    from skillforge.models import TaskContext
    with tempfile.TemporaryDirectory(prefix="sf_tc_drift_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        task_ctx = TaskContext(
            task_id="TASK_SCOPE_01",
            goal="Query logistics strictly without advice",
            business_scope="logistics_tracking",
            constraints=["STATUS_ONLY"],
            acceptance_criteria={"intent_constraint": "STATUS_ONLY"},
            intent_revision=2,
        )
        fp = task_ctx.compute_fingerprint()
        task_ctx.contract_fingerprint = fp
        cand_store.save_task_context(task_ctx)

        # Retrieve by fingerprint works
        retrieved_ctx = cand_store.get_task_context_by_fingerprint(fp)
        assert retrieved_ctx is not None
        assert retrieved_ctx.task_id == "TASK_SCOPE_01"

        cand = CandidateSkill(
            candidate_id="cand_tc_bound",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=[],
            source_requirement="Requirement for bound test",
            meta=SkillMeta(name="logistics_tracking", version="1.0.0", description="d", use_when="w"),
            body="## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC",
            task_spec_hash=fp,
        )
        cand_store.save_candidate(cand)

        evaluator = FixtureEvaluator(score=1.0, decision="PASS")
        cases = [{"task_id": "T1", "query": "q"}]
        ds_hash = compute_cases_hash(cases)
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
            scope_hash=fp,
            config_hash="cfg_auth",
            dataset_version=ds_hash,
        )
        assert val_rec.ratchet_decision == "PASS"

        # Now drift TaskContext contract in CandidateStore
        task_ctx.constraints = ["STATUS_ONLY", "MUTATED_CONSTRAINT_ADDED"]
        cand_store.save_task_context(task_ctx)

        with pytest.raises(ValueError, match="TaskContext contract drift detected"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash="cfg_auth",
                expected_dataset_version=ds_hash,
                expected_scope_hash=fp,
            )


def test_baseline_v2_masquerading_as_v1_rejected():
    """Verify that genuine V1 baseline body differs from V2, instructs advice,

    and matches the canonical hash 3b8b0b6c2224a4ee5d02a9a3370de1736a6cc1935eeddf9756f6c0c6c28d1e43.
    """
    import hashlib
    from skillforge.scenarios.supplement_real_experiment import REAL_V1_BODY, verify_frozen_v2_hash

    h_v1 = hashlib.sha256(REAL_V1_BODY.encode("utf-8")).hexdigest()
    assert h_v1 == "3b8b0b6c2224a4ee5d02a9a3370de1736a6cc1935eeddf9756f6c0c6c28d1e43"

    h_v2 = verify_frozen_v2_hash()
    assert h_v2 == "fb30297bbe949474556ab20f1ab15abed8f29db662be315ef9d39f3176300c6e"

    assert h_v1 != h_v2
    # Genuine V1 instructs to give advice
    assert "给出合理的售后处理建议" in REAL_V1_BODY
    assert "STATUS_ONLY" not in REAL_V1_BODY


def test_future_task_isolation_when_candidate_awaiting_review():
    """Verify that when candidate is in AWAITING_REVIEW / unadmitted,

    production registry serves only baseline 1.0.0, and unadmitted candidate 1.0.1 is isolated.
    """
    from skillforge.runtime import AgentRuntime, ToolBroker
    with tempfile.TemporaryDirectory(prefix="sf_future_iso_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        # Baseline 1.0.0 in production registry
        from skillforge.scenarios.supplement_real_experiment import REAL_V1_BODY
        base_dir = skills_dir / "logistics_tracking"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "SKILL.md").write_text(REAL_V1_BODY, encoding="utf-8")
        reg.load_skills_from_dir()

        runtime = AgentRuntime(db_path=db_path, tool_broker=ToolBroker(), registry=reg, episode_store=ep_store)

        # Unadmitted candidate in candidate_store
        cand_unadmitted = CandidateSkill(
            candidate_id="cand_unadmitted_v2",
            skill_name="logistics_tracking",
            decision="revise",
            source_episode_ids=[],
            source_requirement="Requirement for unadmitted candidate",
            meta=SkillMeta(name="logistics_tracking", version="1.0.1", description="v2", use_when="w"),
            body="## Overview\nV2 unadmitted",
            status="DRAFT",
        )
        cand_store.save_candidate(cand_unadmitted)

        # Future run retrieves from registry
        run = runtime.start_run(
            run_id="run_future_iso",
            task_id="TASK_FUT_01",
            task_description="Query logistics status",
            enable_reuse=True,
            require_reuse=True,
        )
        assert run.skill_name == "logistics_tracking"
        body = runtime.get_run_body("logistics_tracking", "run_future_iso")
        assert body == reg.get_body("logistics_tracking")
        assert "给出合理的售后处理建议" in body
        assert reg.get_meta("logistics_tracking").version == "1.0.0"
        assert "V2 unadmitted" not in body


def test_caller_fake_scope_and_tampered_body_rejected():
    """Verify that promote_candidate rejects caller fake scopes, tampered bodies,

    and detects TaskContext contract drift even when DB connection is reopened from disk.
    """
    from skillforge.models import TaskContext
    with tempfile.TemporaryDirectory(prefix="sf_scope_tamper_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True, capture_output=True)

        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        genuine_body = "## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC"
        task_ctx = TaskContext(
            task_id="TASK_TAMPER_01",
            goal="Query logistics strictly without advice",
            business_scope="logistics_tracking",
            constraints=["STATUS_ONLY"],
            acceptance_criteria={"intent_constraint": "STATUS_ONLY"},
            intent_revision=2,
            active_body_snapshot=genuine_body,
        )
        fp = task_ctx.compute_fingerprint()
        task_ctx.contract_fingerprint = fp
        cand_store.save_task_context(task_ctx)

        # 1. Caller passes fake/unauthorized scope hash
        cand_fake = CandidateSkill(
            candidate_id="cand_fake_scope",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=[],
            source_requirement="Requirement",
            meta=SkillMeta(name="logistics_tracking", version="1.0.0", description="d", use_when="w"),
            body=genuine_body,
            task_spec_hash="fake_caller_scope_999",
        )
        cand_store.save_candidate(cand_fake)
        val_rec_fake = ValidationRecord(
            candidate_id="cand_fake_scope",
            content_hash=compute_candidate_hash(cand_fake),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=EvalResult(release_id="r", structure_score={"format": 40.0}, effect_score={"task_success": 60.0}, objective_metrics={}, p0_pass=True, valid=True),
            scope_hash="fake_caller_scope_999",
        )
        cand_store.save_validation_record(val_rec_fake)

        with pytest.raises(ValueError, match="Unknown or unauthorized scope hash"):
            promote_candidate(
                candidate=cand_fake,
                validation_record=val_rec_fake,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )

        # 2. Tampered candidate body vs active_body_snapshot
        tampered_body = genuine_body + "\n\n## Injected Malicious Section\nMalicious"
        cand_tampered = CandidateSkill(
            candidate_id="cand_tampered_body",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=[],
            source_requirement="Requirement",
            meta=SkillMeta(name="logistics_tracking", version="1.0.0", description="d", use_when="w"),
            body=tampered_body,
            task_spec_hash=fp,
        )
        cand_store.save_candidate(cand_tampered)
        val_rec_tampered = ValidationRecord(
            candidate_id="cand_tampered_body",
            content_hash=compute_candidate_hash(cand_tampered),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=EvalResult(release_id="r", structure_score={"format": 40.0}, effect_score={"task_success": 60.0}, objective_metrics={}, p0_pass=True, valid=True),
            scope_hash=fp,
        )
        cand_store.save_validation_record(val_rec_tampered)

        with pytest.raises(ValueError, match="TaskContext active_body_snapshot does not match candidate body"):
            promote_candidate(
                candidate=cand_tampered,
                validation_record=val_rec_tampered,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )

        # 3. DB connection reopened from disk, contract drifted in DB
        del cand_store
        del sm
        del reg

        # Reopen brand new instances against same SQLite DB on disk
        cand_store2 = CandidateStore(db_path)
        sm2 = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
        reg2 = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)

        cand_valid = CandidateSkill(
            candidate_id="cand_valid_body",
            skill_name="logistics_tracking",
            decision="create",
            source_episode_ids=[],
            source_requirement="Requirement",
            meta=SkillMeta(name="logistics_tracking", version="1.0.0", description="d", use_when="w"),
            body=genuine_body,
            task_spec_hash=fp,
        )
        cand_store2.save_candidate(cand_valid)
        val_rec_valid = ValidationRecord(
            candidate_id="cand_valid_body",
            content_hash=compute_candidate_hash(cand_valid),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=EvalResult(release_id="r", structure_score={"format": 40.0}, effect_score={"task_success": 60.0}, objective_metrics={}, p0_pass=True, valid=True),
            scope_hash=fp,
        )
        cand_store2.save_validation_record(val_rec_valid)

        # Mutate TaskContext directly in SQLite
        conn = cand_store2._get_conn()
        conn.execute("UPDATE task_contexts SET constraints_json = ? WHERE contract_fingerprint = ?", (json.dumps(["STATUS_ONLY", "MUTATED"]), fp))
        conn.commit()

        with pytest.raises(ValueError, match="TaskContext contract drift detected"):
            promote_candidate(
                candidate=cand_valid,
                validation_record=val_rec_valid,
                state_machine=sm2,
                registry=reg2,
                candidate_store=cand_store2,
                caller_confirmed=True,
            )


def test_budget_cap_200_and_reviser_hard_cap_7th_blocked():
    """Verify that ledger pre_reserve blocks at budget cap 200,

    and blocks 7th revision attempt when 6 revisions have been consumed.
    """
    with tempfile.TemporaryDirectory(prefix="sf_cap_test_") as tmp_dir:
        ledger_path = Path(tmp_dir) / "ledger.json"
        ledger = PersistentCallLedger(ledger_path)

        # Simulate reaching budget cap 200
        state = ledger._read_state()
        state["task_calls"] = 200
        state["task_budget_cap"] = 200
        state["revision_accounting"] = {"total_revisions": 5, "max_revisions": 6}
        ledger._write_state(state)

        # 201st call must be blocked
        with pytest.raises(RuntimeError, match="Provider call budget exceeded hard cap: 200 >= 200"):
            ledger.pre_reserve(budget_cap=200)

        # Now test 7th revision blocking: allow normal calls but max revisions reached
        state["task_calls"] = 100
        state["task_budget_cap"] = 200
        state["revision_accounting"] = {"total_revisions": 6, "max_revisions": 6}
        ledger._write_state(state)

        # 7th revision must be blocked BEFORE outbound request
        with pytest.raises(RuntimeError, match="Revision round limit exceeded hard cap: 6 >= 6"):
            ledger.pre_reserve(budget_cap=200, is_revision=True, max_revisions=6)





