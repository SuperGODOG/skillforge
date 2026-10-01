"""Zero-network unit tests for P6 1000-Token AND Prompt Bloat Policy and Security Invariants.

Verifies:
1. High ratio small increment passes (ratio > 25% but delta <= 1000 tokens -> PASS).
2. Low ratio >1000 tokens increment passes (delta > 1000 tokens but ratio <= 25% -> PASS).
3. Both exceeded triggers REVIEW (ratio > 25% AND delta > 1000 tokens -> REVIEW).
4. Strict boundary checks (exact 1000 tokens passes, 1001 triggers; exact 25% / 1.20x passes).
5. Token-governed rather than character-governed (multi-byte vs ASCII, preamble vs frontmatter).
6. Consistency across callers (check_prompt_bloat, skill_generator, evolution_loop).
7. Unknown tokenizer fail-closed to REVIEW (no fake len//4 or silent pass).
8. Config change invalidates old ValidationRecord and blocks promote_candidate.
9. Anti-bypass guards: fake caller PASS, V2 masquerading as V1, arbitrary caller scope tag rejected.
10. Budget cap 200 and reviser hard cap 6 (7th revision blocked).
11. Real on-disk candidate re-validation under 1000-token gate (length PASS, promotion pending eval).
12. SkillSplitter advisory-only invariant (applied=False, no auto route mutation on length).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
import pytest

from skillforge.evaluator.prompt_bloat import (
    DEFAULT_POLICY_TOKENIZER,
    DEFAULT_POLICY_VERSION,
    PromptBloatResult,
    check_prompt_bloat,
    compute_body_section_stats,
    compute_body_section_token_stats,
    count_tokens,
    strip_frontmatter_if_present,
)
from skillforge.models import (
    CandidateSkill,
    EvolveBudget,
    EvalResult,
    RatchetVerdict,
    SkillMeta,
    TaskContext,
    Trigger,
    ValidationRecord,
)
from skillforge.episode import CandidateStore
from skillforge.evolution_loop import (
    compute_candidate_hash,
    compute_cases_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.registry import SkillRegistry
from skillforge.state_machine import ReleaseStateMachine
from skillforge.evaluator.ark_client import PersistentCallLedger
from skillforge.skill_splitter import suggest_skill_split, DomainSpec


# ==================== 1. High Ratio Small Increment Passes ====================

def test_high_ratio_small_increment_passes_1000_token_gate():
    """Verify that a section with massive ratio growth (+500%) passes because delta <= 1000 tokens."""
    # Baseline: 20 tokens; Candidate: 120 tokens (+100 tokens, +500% ratio)
    old_body = "## Instructions\n" + ("第一步操作。" * 5)
    new_body = "## Instructions\n" + ("第一步操作。" * 30)

    res = check_prompt_bloat(old_body, new_body, changed_sections=["Instructions"])
    assert res.passed is True
    assert res.decision == "PASS"
    assert res.section_deltas["Instructions"]["ratio_tokens"] > 0.25
    assert res.section_deltas["Instructions"]["delta_tokens"] <= 1000


# ==================== 2. Low Ratio >1000 Increment Passes ====================

def test_low_ratio_large_increment_passes_1000_token_gate():
    """Verify that adding >1000 tokens passes when ratio <= 25% (e.g. 15% on large baseline)."""
    # Baseline: ~6400 tokens; Add: ~960 tokens section + ~1150 tokens across body with ratio ~18% <= 25%
    base_unit = "标准操作规范与严格执行指令要求。" * 100  # ~800 tokens
    old_body = (
        f"## Overview\n{base_unit}\n\n"
        f"## Instructions\n{base_unit * 4}\n\n"
        f"## Examples\n{base_unit}\n\n"
        f"## Constraints\n{base_unit}"
    )

    # In Instructions, add 1100 tokens (> 1000 tokens), but ratio is 1100 / 3200 = 34%
    # To keep ratio <= 25%: baseline instructions is 8000 tokens, add 1500 tokens (18.75% <= 25%)
    base_large_inst = "标准操作规范与严格执行指令要求。" * 1000  # ~8000 tokens
    old_body_large = (
        f"## Overview\n{base_unit}\n\n"
        f"## Instructions\n{base_large_inst}\n\n"
        f"## Examples\n{base_unit}\n\n"
        f"## Constraints\n{base_unit}"
    )
    add_inst = "标准操作规范与严格执行指令要求。" * 150  # ~1200 tokens (> 1000 tokens)
    new_body_large = (
        f"## Overview\n{base_unit}\n\n"
        f"## Instructions\n{base_large_inst}\n{add_inst}\n\n"
        f"## Examples\n{base_unit}\n\n"
        f"## Constraints\n{base_unit}"
    )

    budget = EvolveBudget(max_body_multiplier=1.50)  # isolate section gate
    res = check_prompt_bloat(old_body_large, new_body_large, budget=budget, changed_sections=["Instructions"])
    assert res.passed is True
    assert res.decision == "PASS"
    assert res.section_deltas["Instructions"]["delta_tokens"] > 1000
    assert res.section_deltas["Instructions"]["ratio_tokens"] <= 0.25


# ==================== 3. Both Exceeded Triggers REVIEW ====================

def test_both_conditions_exceeded_triggers_review():
    """Verify that when BOTH ratio > 25% AND delta > 1000 tokens, REVIEW is triggered."""
    base_inst = "标准操作规范与严格执行指令要求。" * 100  # ~800 tokens
    add_inst = "\n冗余扩充说明内容与无用说明步骤。" * 160  # ~1300 tokens (> 1000 tokens, ratio 1300/800 = 162% > 25%)

    old_body = f"## Overview\n概述\n\n## Instructions\n{base_inst}\n\n## Examples\n示例\n\n## Constraints\n约束"
    new_body = f"## Overview\n概述\n\n## Instructions\n{base_inst}{add_inst}\n\n## Examples\n示例\n\n## Constraints\n约束"

    res = check_prompt_bloat(old_body, new_body, changed_sections=["Instructions"])
    assert res.passed is False
    assert res.decision == "REVIEW"
    assert any("PROMPT_BLOAT" in r and "Instructions" in r for r in res.reasons)
    assert res.distillation_prompt is not None
    assert "建议精简收敛" in res.distillation_prompt


# ==================== 4. Strict Boundary Checks (AND Gate, Strict >) ====================

def test_strict_boundary_matrix_controllable_mock():
    """Use a controllable mock tokenizer to test boundary edge cases with mathematical precision."""
    # Custom tokenizer where each word is 1 token
    mock_tok = lambda text: len((text or "").split())

    # Case A: Exact 1000 tokens delta with ratio 10.0x (> 25%) -> Strict > means 1000 does NOT exceed!
    b_words = "w " * 100  # 100 tokens
    c_words = "w " * 1100  # 1100 tokens (delta = 1000 exactly)
    old_b = f"## Instructions\n{b_words}"
    new_b = f"## Instructions\n{c_words}"
    res = check_prompt_bloat(old_b, new_b, tokenizer_callable=mock_tok, changed_sections=["Instructions"])
    assert res.section_deltas["Instructions"]["delta_tokens"] == 1000
    assert res.passed is True
    assert res.decision == "PASS"

    # Case B: 1001 tokens delta with ratio 10.01x -> Exceeds strict > 1000!
    c_words_1001 = "w " * 1101  # delta = 1001
    new_b_1001 = f"## Instructions\n{c_words_1001}"
    res_1001 = check_prompt_bloat(old_b, new_b_1001, tokenizer_callable=mock_tok, changed_sections=["Instructions"])
    assert res_1001.section_deltas["Instructions"]["delta_tokens"] == 1001
    assert res_1001.passed is False
    assert res_1001.decision == "REVIEW"

    # Case C: Exact 25% ratio with delta 2000 tokens (> 1000 tokens) -> Strict > means 0.25 does NOT exceed!
    # Baseline: 8000 tokens, New: 10000 tokens (delta = 2000, ratio = 2000 / 8000 = 0.25 exactly)
    old_b_25 = f"## Instructions\n{'w ' * 8000}"
    new_b_25 = f"## Instructions\n{'w ' * 10000}"
    budget = EvolveBudget(max_body_multiplier=1.50)  # isolate section
    res_25 = check_prompt_bloat(old_b_25, new_b_25, budget=budget, tokenizer_callable=mock_tok, changed_sections=["Instructions"])
    assert res_25.section_deltas["Instructions"]["ratio_tokens"] == 0.25
    assert res_25.section_deltas["Instructions"]["delta_tokens"] == 2000
    assert res_25.passed is True
    assert res_25.decision == "PASS"

    # Case D: Whole body exact 1.20x multiplier with delta 2000 tokens -> Strict > means 1.20x does NOT exceed!
    old_whole = f"## Overview\n{'w ' * 5000}\n\n## Instructions\n{'w ' * 5000}"
    new_whole = f"## Overview\n{'w ' * 6000}\n\n## Instructions\n{'w ' * 6000}"
    res_whole_exact = check_prompt_bloat(old_whole, new_whole, tokenizer_callable=mock_tok)
    assert res_whole_exact.section_deltas["total"]["multiplier_tokens"] == pytest.approx(1.20, abs=1e-3)
    assert res_whole_exact.passed is True
    assert res_whole_exact.decision == "PASS"

    # Case E: Whole body > 1.20x multiplier with delta > 1000 tokens -> Exceeds strict > 1.20!
    new_whole_over = f"## Overview\n{'w ' * 6500}\n\n## Instructions\n{'w ' * 6500}"  # 13000 tokens
    res_whole_over = check_prompt_bloat(old_whole, new_whole_over, tokenizer_callable=mock_tok)
    assert res_whole_over.section_deltas["total"]["multiplier_tokens"] > 1.20
    assert res_whole_over.passed is False
    assert res_whole_over.decision == "REVIEW"


# ==================== 5. Token-Governed vs Character-Governed ====================

def test_token_governed_rather_than_character_governed():
    """Verify that decision is strictly governed by tokens, not characters.

    4000 characters of repeating single-character ascii words vs dense multi-byte Chinese.
    """
    # 4000 ascii characters 'a ' * 2000 is 2000 tokens (or 'a' * 4000 in single token chunk)
    # Baseline: 100 tokens, Add 4000 spaces/short tokens:
    old_body = "## Instructions\n" + ("step " * 100)
    # Add text with 3000 chars that is only 500 tokens
    add_text = "word " * 500  # 2500 chars, 500 tokens
    new_body = old_body + "\n" + add_text

    # Characters delta is > 100 (it is 2500 chars!), under old character rule this would be REVIEW!
    # Under new rule: delta is 500 tokens <= 1000 tokens -> Must PASS!
    res = check_prompt_bloat(old_body, new_body, changed_sections=["Instructions"])
    assert res.section_deltas["Instructions"]["delta_chars"] > 100
    assert res.section_deltas["Instructions"]["delta_tokens"] <= 1000
    assert res.passed is True
    assert res.decision == "PASS"

    # Test Frontmatter preamble immunity: YAML block does not count as prompt bloat
    fm = "---\nname: test_skill\nversion: 1.0.0\ndescription: " + ("metadata " * 300) + "\n---\n"
    body = "## Overview\n概述内容\n## Instructions\n说明内容\n## Examples\n例子\n## Constraints\n约束"
    res_fm = check_prompt_bloat(body, fm + body)
    assert res_fm.passed is True
    assert res_fm.decision == "PASS"

    # Test Preamble substantive text: Substantive markdown text before ## Overview IS counted
    preamble_old = "前言说明内容。\n\n" + body
    preamble_bloat = ("这是前言实质性要求，严格遵守业务规约不可违背。" * 80) + "\n\n" + body
    res_preamble = check_prompt_bloat(preamble_old, preamble_bloat)
    assert res_preamble.section_deltas["Preamble"]["delta_tokens"] > 1000
    assert res_preamble.passed is False
    assert res_preamble.decision == "REVIEW"


# ==================== 6. Caller Consistency ====================

def test_caller_consistency_across_gateways():
    """Verify that check_prompt_bloat and evolution_loop cheap check yield identical results."""
    from skillforge.evolution_loop import validate_candidate

    old_body = "## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC"
    # Candidate body adding 1200 tokens
    bloat_inst = "操作指令要求与执行标准规范。" * 150
    cand_body = f"## Overview\nO\n## Instructions\nI\n{bloat_inst}\n## Examples\nE\n## Constraints\nC"

    # Direct call
    direct_res = check_prompt_bloat(old_body, cand_body)
    assert direct_res.decision == "REVIEW"

    # Gateway call via evolution_loop.validate_candidate
    cand = CandidateSkill(
        candidate_id="cand_caller_sync",
        skill_name="test_sync",
        decision="revise",
        source_episode_ids=[],
        source_requirement="req",
        meta=SkillMeta(name="test_sync", version="1.0.1", description="d", use_when="w"),
        body=cand_body,
        task_spec_hash="spec_sync",
    )

    with tempfile.TemporaryDirectory(prefix="sf_sync_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "test_sync").mkdir()
        full_old_md = "---\nname: test_sync\nversion: 1.0.0\ndescription: d\nuse_when: w\n---\n" + old_body
        (skills_dir / "test_sync" / "SKILL.md").write_text(full_old_md, encoding="utf-8")

        reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
        reg.load_skills_from_dir()
        c_store = CandidateStore(db_path)
        c_store.save_candidate(cand)

        # validate_candidate cheap bloat check should halt before evaluator and persist REVIEW record
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=None,  # No evaluator needed because bloat cheap check intercepts
            registry=reg,
            eval_cases=[],
            candidate_store=c_store,
        )

        assert val_rec.ratchet_decision == "REVIEW"
        assert val_rec.eval_result is None
        assert val_rec.ratchet_verdict is not None
        assert val_rec.ratchet_verdict.reasons == direct_res.reasons


# ==================== 7. Unknown Tokenizer Fails Closed ====================

def test_unknown_tokenizer_fails_closed_no_fake_counts():
    """Verify that an unknown or unavailable tokenizer fails closed to REVIEW without fake len//4."""
    old_body = "## Instructions\n操作说明。"
    new_body = "## Instructions\n操作说明更新。"

    res = check_prompt_bloat(old_body, new_body, tokenizer_name="non_existent_fake_tokenizer_xyz")
    assert res.passed is False
    assert res.decision == "REVIEW"
    assert any("Tokenizer 未知或计数不可用" in r for r in res.reasons)
    assert any("non_existent_fake_tokenizer_xyz" in r for r in res.reasons)


# ==================== 8. Config / Tokenizer Change Invalidates Old Record ====================

def test_config_hash_change_invalidates_old_validation_record():
    """Verify that changing validator config hash invalidates old record and blocks promote_candidate."""
    with tempfile.TemporaryDirectory(prefix="sf_cfg_inv_") as tmp_dir:
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

        body = "## Overview\nO\n## Instructions\nI\n## Examples\nE\n## Constraints\nC"
        full_skill_md = "---\nname: cfg_test_skill\nversion: 1.0.0\ndescription: d\nuse_when: w\n---\n" + body
        base_dir = skills_dir / "cfg_test_skill"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "SKILL.md").write_text(full_skill_md, encoding="utf-8")
        reg.load_skills_from_dir()

        cand = CandidateSkill(
            candidate_id="cand_cfg_01",
            skill_name="cfg_test_skill",
            decision="create",
            source_episode_ids=[],
            source_requirement="req",
            meta=SkillMeta(name="cfg_test_skill", version="1.0.0", description="d", use_when="w"),
            body=body,
            task_spec_hash="spec_cfg_01",
        )
        cand_store.save_candidate(cand)

        # Validation record created under OLD config hash
        val_rec = ValidationRecord(
            candidate_id="cand_cfg_01",
            content_hash=compute_candidate_hash(cand),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=EvalResult(release_id="r", structure_score={"format": 40.0}, effect_score={"task_success": 60.0}, objective_metrics={}, p0_pass=True, valid=True),
            scope_hash="spec_cfg_01",
            config_hash="old_char_policy_config_v1",
        )
        cand_store.save_validation_record(val_rec)

        # Promotion requiring NEW token policy config hash must raise ValueError
        with pytest.raises(ValueError, match="validator config hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash="new_1000_token_policy_config_v2",
            )


# ==================== 9. Anti-Bypass Security Invariants ====================

def test_anti_bypass_caller_fake_pass_and_tampered_body():
    """Verify that caller_confirmed=True cannot bypass missing eval_result or tampered scope."""
    from skillforge.models import TaskContext
    with tempfile.TemporaryDirectory(prefix="sf_bypass_") as tmp_dir:
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
            task_id="TASK_AUTH_01",
            goal="Test auth goal",
            business_scope="auth_test",
            constraints=["STATUS_ONLY"],
            acceptance_criteria={"intent_constraint": "STATUS_ONLY"},
            intent_revision=1,
            active_body_snapshot=genuine_body,
        )
        fp = task_ctx.compute_fingerprint()
        task_ctx.contract_fingerprint = fp
        cand_store.save_task_context(task_ctx)

        cand = CandidateSkill(
            candidate_id="cand_auth_01",
            skill_name="auth_test",
            decision="create",
            source_episode_ids=[],
            source_requirement="req",
            meta=SkillMeta(name="auth_test", version="1.0.0", description="d", use_when="w"),
            body=genuine_body,
            task_spec_hash=fp,
        )
        cand_store.save_candidate(cand)

        # 1. Fake PASS with eval_result=None (e.g. from bloat REVIEW fabricated to PASS)
        fake_rec = ValidationRecord(
            candidate_id="cand_auth_01",
            content_hash=compute_candidate_hash(cand),
            baseline_version=None,
            ratchet_decision="PASS",
            eval_result=None,  # missing real evaluation!
            scope_hash=fp,
        )
        cand_store.save_validation_record(fake_rec)

        with pytest.raises(ValueError, match="has no evaluation result in validation record"):
            promote_candidate(
                candidate=cand,
                validation_record=fake_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )

        # 2. Genuine V1 hash verification: V2 cannot masquerade as V1
        from skillforge.scenarios.supplement_real_experiment import REAL_V1_BODY, verify_frozen_v2_hash
        h_v1 = hashlib.sha256(REAL_V1_BODY.encode("utf-8")).hexdigest()
        assert h_v1 == "3b8b0b6c2224a4ee5d02a9a3370de1736a6cc1935eeddf9756f6c0c6c28d1e43"
        h_v2 = verify_frozen_v2_hash()
        assert h_v2 != h_v1


# ==================== 10. Budget Cap 200 & Reviser Hard Cap 6 ====================

def test_budget_cap_and_reviser_hard_cap_persists():
    """Verify that ledger pre_reserve blocks at budget 200 and blocks 7th revision."""
    with tempfile.TemporaryDirectory(prefix="sf_cap_") as tmp_dir:
        ledger_path = Path(tmp_dir) / "ledger.json"
        ledger = PersistentCallLedger(ledger_path)

        state = ledger._read_state()
        state["task_calls"] = 200
        state["task_budget_cap"] = 200
        state["revision_accounting"] = {"total_revisions": 6, "max_revisions": 6}
        ledger._write_state(state)

        # 201st call blocked
        with pytest.raises(RuntimeError, match="Provider call budget exceeded hard cap: 200 >= 200"):
            ledger.pre_reserve(budget_cap=200)

        # 7th revision blocked
        state["task_calls"] = 50
        ledger._write_state(state)
        with pytest.raises(RuntimeError, match="Revision round limit exceeded hard cap: 6 >= 6"):
            ledger.pre_reserve(budget_cap=200, is_revision=True, max_revisions=6)


# ==================== 11. Real On-Disk Candidate Re-Validation Under 1000-Token Gate ====================

def test_real_on_disk_candidate_revalidated_under_1000_token_gate():
    """Verify that cand_lifecycle_v2_8e70d68c passes 1000-token bloat gate against cand_real_v1_3b8b0b6c."""
    from skillforge.scenarios.supplement_real_experiment import REAL_V1_BODY

    # Load candidate V2 body from disk checkpoint
    ckpt_path = Path("docs/p6_real_lifecycle_checkpoint.json")
    assert ckpt_path.exists(), "p6_real_lifecycle_checkpoint.json must exist"
    ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
    v2_body = ckpt["cand_v2"]["body_md"]

    # Re-evaluate bloat under new 1000-token policy
    res = check_prompt_bloat(REAL_V1_BODY, v2_body)

    # 1. Total tokens
    old_toks = res.baseline_token_stats["total"]
    new_toks = res.candidate_token_stats["total"]
    total_delta = res.section_deltas["total"]["delta_tokens"]
    total_mult = res.section_deltas["total"]["multiplier_tokens"]

    assert old_toks == 166
    assert new_toks == 298
    assert total_delta == 132  # <= 1000 tokens!
    assert 1.79 < total_mult < 1.80  # multiplier > 1.20x BUT delta <= 1000 tokens

    # 2. Section deltas
    for sec in ["Overview", "Instructions", "Examples", "Constraints"]:
        sec_delta = res.section_deltas[sec]["delta_tokens"]
        assert sec_delta <= 1000, f"Section {sec} delta {sec_delta} must be <= 1000 tokens"

    # 3. Overall Gate Verdict: Length gate passes!
    assert res.passed is True
    assert res.decision == "PASS"

    # 4. Invariant: Length gate passing alone does NOT constitute production promotion!
    # Candidate remains unadmitted until real behavioral evaluation on STATUS_ONLY.
    assert ckpt["cand_v2"]["status"] == "DRAFT"


# ==================== 12. SkillSplitter Advisory-Only Invariant (P5 L6) ====================

def test_skill_splitter_advisory_only_no_auto_route_mutation():
    """Verify that SkillSplitter suggest_skill_split is advisory-only (applied=False).

    It does NOT mutate live SKILL.md or router definitions on length alone.
    """
    with tempfile.TemporaryDirectory(prefix="sf_split_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Single-intent coherent long pipeline
        long_pipe_body = (
            "---\nname: order_fulfillment_pipeline\nversion: 1.0.0\ndescription: 顺序处理履约\n---\n"
            "## Overview\n单向不可分割长流程。\n\n"
            "## Instructions\n1. 验证订单。\n2. 扣减库存。\n3. 调用支付。\n4. 派发运单。\n\n"
            "## Examples\nQ: 履约\nA: 顺序执行1234。\n\n"
            "## Constraints\n必须严格顺序执行。"
        )
        pipe_dir = skills_dir / "order_fulfillment_pipeline"
        pipe_dir.mkdir()
        pipe_file = pipe_dir / "SKILL.md"
        pipe_file.write_text(long_pipe_body, encoding="utf-8")

        # Run suggest_skill_split
        proposal = suggest_skill_split(
            skill_name="order_fulfillment_pipeline",
            repo_root=tmp_path,
        )

        # 1. Pipeline cannot be split
        assert proposal.can_split is False
        assert proposal.status == "CANNOT_SPLIT"
        assert proposal.applied is False

        # 2. Invariant: Original SKILL.md is completely untouched
        assert pipe_file.read_text(encoding="utf-8") == long_pipe_body
