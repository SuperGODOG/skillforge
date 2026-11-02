"""E1: 提炼规则溯源、失败机制与可追溯证据验证测试 (Rule Provenance & Failure Mechanisms).

Methodology source:
- Nuwa-Skill extraction framework (https://github.com/alchaincyf/nuwa-skill/blob/main/references/extraction-framework.md)

Tests:
1. Real production entrypoint call (repair_skill_failure / RepairJob) attaches traceable rule provenance.
2. Candidate store serialization & reopening retains exact rule provenance and links to canonical EpisodeStore.
3. Logistics multi-package scenario rule: complete package status checklist vs premature delivery claim.
4. Negative: Non-existent episode ID strictly rejected with KeyError.
5. Negative: Document / policy source masquerading as episode strictly rejected.
6. Negative: Document / policy source claiming 'verified' without execution trace rejected.
7. Negative: Stored failed episode claiming 'verified' positive behavior rejected.
8. Negative: promote_candidate fail-closed rejection on invalid / forged rule provenance.
9. PromotionCard integration: structured rule provenance and boundary warnings rendered cleanly.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import yaml

from skillforge.episode import CandidateStore, EpisodeStore, verify_candidate_rule_provenance
from skillforge.evolution_loop import (
    ValidationRecord,
    compute_candidate_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    RuleTraceEvidence,
    SkillMeta,
    Trigger,
)
from skillforge.promotion_card import build_promotion_card, render_promotion_card
from skillforge.registry import SkillRegistry
from skillforge.repair import RepairJob, repair_skill_failure
from skillforge.state_machine import ReleaseStateMachine


class ScriptedCriteriaLLM:
    """Deterministic Fake LLM returning scripted criteria findings."""

    def __init__(self, responses: Optional[list[str]] = None, default_response: Optional[str] = None):
        self.responses = list(responses) if responses else []
        self.default_response = default_response
        self.calls: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.responses:
            resp = self.responses.pop(0)
        elif self.default_response is not None:
            resp = self.default_response
        else:
            user_msg = str(messages)
            if "readability" in user_msg or "判断两个 Agent 回答" in user_msg:
                resp = json.dumps({"verdict": "tied", "reason_codes": ["OK"], "evidence_summary": "readability ok"})
            else:
                resp = json.dumps({
                    "findings": [
                        {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "verified", "reason": "ok"},
                        {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "verified", "reason": "ok"},
                        {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": "verified", "reason": "ok"},
                        {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "verified", "reason": "ok"},
                    ]
                })
        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        )


def _setup_repo(tmp_path: Path, skill_name: str = "logistics_checker") -> tuple[SkillRegistry, Path, Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "test@test.local"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True)

    skills_dir = repo_root / "skills"
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    meta = SkillMeta(
        name=skill_name,
        version="1.0.0",
        description="Logistics package status checker",
        use_when="When checking multi-package order status",
        not_for=["Financial refund processing"],
        dependencies=["query_orders", "query_tracking"],
        trigger=Trigger(keywords=["order", "package", "tracking"]),
    )
    body = (
        "## Overview\nLogistics multi-package fulfillment assistant.\n\n"
        "## Instructions\n1. Query all packages in the order.\n"
        "2. Query tracking status for each package.\n"
        "3. Collate delivery carrier details and report progress to user.\n\n"
        "## Constraints\n- Do not claim all delivered unless every package is confirmed delivered.\n"
    )
    (skill_dir / "SKILL.md").write_text(f"---\n{yaml.dump(meta.model_dump())}---\n\n{body}", encoding="utf-8")
    (repo_root / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    db_path = repo_root / "skillforge.db"
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()
    return reg, repo_root, db_path


# ==============================================================================
# 1. Real Production Entrypoint Call & Traceable Rule Extraction (E1)
# ==============================================================================

def test_repair_job_real_entrypoint_generates_and_verifies_rule_provenance():
    """Verify repair_skill_failure creates candidate with structured rule provenance,

    verifies against EpisodeStore, and persists across SQLite store reload.
    """
    with tempfile.TemporaryDirectory(prefix="sf_e1_repair_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, skill_name="logistics_checker")

        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        # 1. Create realistic failed learning episode in EpisodeStore
        failed_ep = Episode(
            episode_id="ep_learning_fail_01",
            task_id="task_log_dev_01",
            run_id="run_log_dev_01",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"query": "查询订单 ORD_DEV_0201 物流状态", "purpose": "learning"},
            provenances=[],
            acceptance_criteria={"require_all_packages_checked": True},
            outcome="failure",
            outcome_reason="Prematurely claimed all delivered when package PKG_D202 was in transit",
            verification_evidence={"independent_pass": False, "failure": "INV_2_VIOLATION"},
        )
        ep_store.save_episode(failed_ep)

        # 2. Setup regression cases and fake LLM evaluator
        from skillforge.evaluator import SkillEvaluator
        repair_yaml = (
            "---\n"
            "name: logistics_checker\n"
            "version: 1.0.1\n"
            "description: Logistics package status checker\n"
            "use_when: When checking multi-package order status\n"
            "not_for:\n"
            "  - Financial refund processing\n"
            "dependencies:\n"
            "  - query_orders\n"
            "  - query_tracking\n"
            "trigger:\n"
            "  keywords:\n"
            "    - order\n"
            "    - package\n"
            "    - tracking\n"
            "examples: []\n"
            "evaluation:\n"
            "  last_score: null\n"
            "  last_release_id: null\n"
            "rules:\n"
            "  - trigger_condition: Multi-package query with unverified packages\n"
            "    action: Require verification of all parcels before claiming delivery\n"
            "    failure_avoided: Prematurely claimed all delivered when package PKG_D202 was in transit\n"
            "    applicability_boundary: Multi-package fulfillment tasks\n"
            "    body_quote: Require verification of all parcels before claiming delivery.\n"
            "---\n\n"
            "## Overview\n"
            "Logistics multi-package fulfillment assistant.\n\n"
            "## Instructions\n"
            "1. Query all packages in the order.\n"
            "2. Query tracking status for each package.\n"
            "3. Require verification of all parcels before claiming delivery.\n\n"
            "## Constraints\n"
            "- Require verification of all parcels before claiming delivery.\n"
            "- Do not claim all delivered unless every package is confirmed delivered.\n"
        )
        patcher_llm = ScriptedCriteriaLLM(responses=[repair_yaml])
        eval_llm = ScriptedCriteriaLLM()
        judge_llm = ScriptedCriteriaLLM()
        evaluator = SkillEvaluator(registry=reg, llm=eval_llm, judge_llm=judge_llm)

        eval_cases = [
            {
                "id": "case_log_01",
                "query": "查询订单 ORD_DEV_0201 物流状态",
                "reference": "PKG_D201: 已送达; PKG_D202: 运输中. 未全部送达",
            }
        ]

        # 3. Call real production repair entry point
        repair_job = repair_skill_failure(
            episodes=[failed_ep],
            skill_name="logistics_checker",
            episode_store=ep_store,
            candidate_store=cand_store,
            registry=reg,
            evaluator=evaluator,
            eval_cases=eval_cases,
            llm=patcher_llm,
            max_attempts=1,
        )

        # Assert LLM prompt contained required rule extraction instructions and failure context
        assert len(patcher_llm.calls) == 1
        prompt_received = str(patcher_llm.calls[0]["messages"])
        assert "Observed Learning Failures" in prompt_received
        assert "rules" in prompt_received
        assert "trigger_condition" in prompt_received
        assert "body_quote" in prompt_received

        assert repair_job.status in ("READY", "AWAITING_REVIEW")
        cand = repair_job.latest_candidate
        assert cand is not None
        assert len(cand.rule_traces) == 1

        rule = cand.rule_traces[0]
        assert rule.rule_id == "RULE_REPAIR_logistics_checker_1_1"
        assert rule.source_ref == "ep_learning_fail_01"
        assert rule.source_type == "episode"
        assert rule.trigger_condition == "Multi-package query with unverified packages"
        assert rule.action == "Require verification of all parcels before claiming delivery"
        assert rule.failure_avoided == "Prematurely claimed all delivered when package PKG_D202 was in transit"
        assert rule.body_quote == "Require verification of all parcels before claiming delivery."
        assert rule.body_quote in cand.body
        assert rule.task_id == "task_log_dev_01"
        # Semantics inferred by repair to avoid failure; non-negotiable: does not forge verified claim
        assert rule.verification_status == "inferred"

        # 4. Store serialization & reload test
        cand_id = cand.candidate_id
        ep_store.close()
        cand_store.close()

        # Reopen from disk
        ep_store_reopen = EpisodeStore(db_path)
        cand_store_reopen = CandidateStore(db_path, episode_store=ep_store_reopen)

        loaded_cand = cand_store_reopen.get_candidate(cand_id)
        assert loaded_cand is not None
        assert len(loaded_cand.rule_traces) == 1
        loaded_rule = loaded_cand.rule_traces[0]
        assert loaded_rule.rule_id == rule.rule_id
        assert loaded_rule.source_ref == failed_ep.episode_id
        assert loaded_rule.verification_status == "inferred"

        # Check canonical episode exists in EpisodeStore and matches
        canonical_ep = ep_store_reopen.get_episode(loaded_rule.source_ref)
        assert canonical_ep is not None
        assert canonical_ep.episode_id == failed_ep.episode_id
        assert canonical_ep.task_id == "task_log_dev_01"

        # 5. Also assert repair response without rules leaves rules empty
        failed_ep_02 = Episode(
            episode_id="ep_learning_fail_02",
            task_id="task_log_dev_02",
            run_id="run_log_dev_02",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"query": "核实新运单状态", "purpose": "learning"},
            provenances=[],
            acceptance_criteria={"require_all_packages_checked": True},
            outcome="failure",
            outcome_reason="Failed parcel lookup",
            verification_evidence={"independent_pass": False},
        )
        ep_store_reopen.save_episode(failed_ep_02)

        no_rule_yaml = (
            "---\n"
            "name: logistics_checker\n"
            "version: 1.0.1\n"
            "description: Logistics package status checker\n"
            "use_when: When checking multi-package order status\n"
            "not_for:\n"
            "  - Financial refund processing\n"
            "dependencies:\n"
            "  - query_orders\n"
            "  - query_tracking\n"
            "trigger:\n"
            "  keywords: [order, package, tracking]\n"
            "examples: []\n"
            "evaluation: {last_score: null, last_release_id: null}\n"
            "---\n\n"
            "## Overview\nLogistics assistant.\n\n"
            "## Instructions\n1. Query all packages in the order.\n2. Query tracking status for each package.\n3. Return tracking status.\n\n"
            "## Constraints\n- Check all.\n"
        )
        no_rule_job = repair_skill_failure(
            episodes=[failed_ep_02],
            skill_name="logistics_checker",
            episode_store=ep_store_reopen,
            candidate_store=cand_store_reopen,
            registry=reg,
            evaluator=evaluator,
            eval_cases=eval_cases,
            llm=ScriptedCriteriaLLM(responses=[no_rule_yaml]),
            max_attempts=1,
        )
        cand_no_rule = no_rule_job.latest_candidate
        assert cand_no_rule is not None
        assert cand_no_rule.rule_traces == []
        assert cand_no_rule.meta.rules == []


# ==============================================================================
# 2. Logistics Scenario Rule: Multi-Package Checklist & Anti-Overclaim (E1)
# ==============================================================================

def test_logistics_rule_multi_package_complete_provenance():
    """Verify logistics multi-package fulfillment rule associates both failure

    counterexample and success verification episodes.
    """
    with tempfile.TemporaryDirectory(prefix="sf_e1_logistics_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, skill_name="logistics_checker")
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        # Counterexample failure episode
        fail_ep = Episode(
            episode_id="ep_log_fail_overclaim",
            task_id="task_pkg_check_01",
            run_id="run_pkg_01",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"query": "核查包裹清单及送达状态", "packages": ["PKG_101", "PKG_102"], "purpose": "learning"},
            provenances=[],
            acceptance_criteria={"all_signed_requires_100_percent": True},
            outcome="failure",
            outcome_reason="Overclaim: Only PKG_101 was DELIVERED while PKG_102 was UNKNOWN; output falsely claimed all delivered",
            verification_evidence={"independent_pass": False, "violation": "INV_2_PREMATURE_OVERCLAIM"},
        )
        ep_store.save_episode(fail_ep)

        # Verified success episode
        succ_ep = Episode(
            episode_id="ep_log_succ_verified",
            task_id="task_pkg_check_02",
            run_id="run_pkg_02",
            skill_name="logistics_checker",
            skill_version="1.1.0",
            environment={"query": "核查包裹清单及送达状态", "packages": ["PKG_101", "PKG_102"], "purpose": "learning"},
            provenances=[],
            acceptance_criteria={"all_signed_requires_100_percent": True},
            outcome="success",
            outcome_reason="Fully verified: both PKG_101 and PKG_102 confirmed DELIVERED via tools",
            verification_evidence={"independent_pass": True, "coverage": 1.0, "verified_rules": ["LOG_RULE_ALL_DELIVERED"]},
        )
        ep_store.save_episode(succ_ep)

        # Candidate with formal rule
        logistics_rule = RuleTraceEvidence(
            rule_id="LOG_RULE_ALL_DELIVERED",
            source_ref=succ_ep.episode_id,
            source_type="episode",
            trigger_condition="用户查询多包裹订单送达状态，包含 2 个及以上包裹",
            action="逐一核实每个包裹状态，核对全量送达凭单",
            failure_avoided="在部分包裹运输中或查询失败时过早断言全部签收(INV_2 幻觉/误报)",
            applicability_boundary="仅在 100% 包裹均确认为 DELIVERED 时方可断言全部送达；任一未知或失败状态必须保留未知并提示跟进",
            verification_status="verified",
            task_id="task_pkg_check_02",
            intent_revision=1,
            skill_version="1.1.0",
            body_quote="逐一核实每个包裹状态，核对全量送达凭单",
            notes="关联历史反例 ep_log_fail_overclaim 与证实正例 ep_log_succ_verified",
        )

        cand = CandidateSkill(
            candidate_id="cand_logistics_v110",
            skill_name="logistics_checker",
            decision="revise",
            source_episode_ids=[succ_ep.episode_id, fail_ep.episode_id],
            meta=SkillMeta(
                name="logistics_checker",
                version="1.1.0",
                description="Enhanced logistics checker",
                use_when="Multi-package logistics tracking",
                rules=[logistics_rule.to_dict()],
            ),
            body="## Overview\nLogistics assistant.\n\n## Instructions\n逐一核实每个包裹状态，核对全量送达凭单\n",
            task_spec_hash="spec_log_v110",
        )

        cand_id = cand_store.save_candidate(cand)
        assert cand_id == "cand_logistics_v110"

        # Verify saved rule
        saved = cand_store.get_candidate(cand_id)
        assert saved is not None
        assert len(saved.rule_traces) == 1
        assert saved.rule_traces[0].rule_id == "LOG_RULE_ALL_DELIVERED"
        assert saved.rule_traces[0].source_ref == "ep_log_succ_verified"
        assert saved.rule_traces[0].verification_status == "verified"


# ==============================================================================
# 3. Negative Tests: Fail-Closed Enforcement (E1)
# ==============================================================================

def test_negative_nonexistent_source_episode_rejected():
    """Verify CandidateStore.save_candidate strictly rejects rules citing non-existent episode IDs."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_neg1_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        forged_rule = RuleTraceEvidence(
            rule_id="RULE_FORGED_EP",
            source_ref="ep_nonexistent_ghost_999",
            source_type="episode",
            trigger_condition="Any query",
            action="Perform action",
            failure_avoided="Crash",
            applicability_boundary="General",
            verification_status="inferred",
        )

        cand = CandidateSkill(
            candidate_id="cand_forged_01",
            skill_name="logistics_checker",
            decision="create",
            source_requirement="req_01",
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[forged_rule.to_dict()]),
            body="body",
        )

        with pytest.raises(KeyError, match="does not exist in EpisodeStore"):
            cand_store.save_candidate(cand)


def test_negative_document_masquerading_as_episode():
    """Verify document/policy source using episode ID format is rejected."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_neg2_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        masquerade_rule = RuleTraceEvidence(
            rule_id="RULE_DOC_MASQUERADE",
            source_ref="ep_logistics_policy_v2",  # policy formatted as episode
            source_type="policy",
            trigger_condition="Any order",
            action="Do something",
            failure_avoided="Policy breach",
            applicability_boundary="Boundary",
            verification_status="inferred",
        )

        cand = CandidateSkill(
            candidate_id="cand_masq_01",
            skill_name="logistics_checker",
            decision="create",
            source_requirement="req_02",
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[masquerade_rule.to_dict()]),
            body="body",
        )

        with pytest.raises(ValueError, match="masquerades document/policy source"):
            cand_store.save_candidate(cand)


def test_negative_document_claiming_verified_without_execution():
    """Verify document/policy source cannot claim 'verified' status without an actual execution episode."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_neg3_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        unverified_doc_rule = RuleTraceEvidence(
            rule_id="RULE_UNVERIFIED_DOC",
            source_ref="doc_sop_fulfillment_v1",
            source_type="document",
            trigger_condition="Check packages",
            action="Query",
            failure_avoided="Avoid omission",
            applicability_boundary="Global",
            verification_status="verified",  # Documents cannot be 'verified' without execution trace!
        )

        cand = CandidateSkill(
            candidate_id="cand_unverified_doc_01",
            skill_name="logistics_checker",
            decision="create",
            source_requirement="req_03",
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[unverified_doc_rule.to_dict()]),
            body="body",
        )

        with pytest.raises(ValueError, match="cannot claim 'verified' status without execution episode trace"):
            cand_store.save_candidate(cand)


def test_negative_failed_episode_claiming_verified_positive_behavior():
    """Verify a rule citing a stored failed episode cannot claim 'verified' positive execution status."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_neg4_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        failed_ep = Episode(
            episode_id="ep_actual_failure_01",
            task_id="t1",
            run_id="r1",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"q": "check"},
            provenances=[],
            acceptance_criteria={},
            outcome="failure",
            outcome_reason="Timeout during package query",
            verification_evidence={"independent_pass": False},
        )
        ep_store.save_episode(failed_ep)

        fake_verified_rule = RuleTraceEvidence(
            rule_id="RULE_FAKE_VERIFIED",
            source_ref=failed_ep.episode_id,
            source_type="episode",
            trigger_condition="Query",
            action="Action",
            failure_avoided="Timeout",
            applicability_boundary="Boundary",
            verification_status="verified",  # Falsely claims verified when store says failure!
        )

        cand = CandidateSkill(
            candidate_id="cand_fake_ver_01",
            skill_name="logistics_checker",
            decision="create",
            source_episode_ids=[failed_ep.episode_id],
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[fake_verified_rule.to_dict()]),
            body="body",
        )

        with pytest.raises(ValueError, match="falsely claims 'verified' status but stored episode .* has outcome='failure'"):
            cand_store.save_candidate(cand)


def test_negative_promote_candidate_blocks_unverified_or_forged_rules():
    """Verify promote_candidate enforces rule provenance validation before promotion."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_neg5_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)

        valid_ep = Episode(
            episode_id="ep_valid_01",
            task_id="t1",
            run_id="r1",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"q": "ok"},
            provenances=[],
            acceptance_criteria={},
            outcome="success",
            outcome_reason="ok",
            verification_evidence={"independent_pass": True},
        )
        ep_store.save_episode(valid_ep)

        # Candidate initially saved with empty rules
        cand = CandidateSkill(
            candidate_id="cand_promote_neg_01",
            skill_name="logistics_checker",
            decision="revise",
            source_episode_ids=[valid_ep.episode_id],
            meta=SkillMeta(name="logistics_checker", version="1.1.0", description="d", use_when="w"),
            body="## Overview\nBody\n",
            task_spec_hash="spec_promote_neg_01",
        )
        cand_store.save_candidate(cand)

        # Create validation record
        val_rec = ValidationRecord(
            candidate_id=cand.candidate_id,
            content_hash=compute_candidate_hash(cand),
            baseline_version="1.0.0",
            ratchet_decision="PASS",
            eval_result=EvalResult(release_id="r", structure_score={"f": 40.0}, effect_score={"t": 60.0}, objective_metrics={}, p0_pass=True, valid=True),
            scope_hash="spec_promote_neg_01",
        )
        cand_store.save_validation_record(val_rec)

        # Tamper candidate rules with forged episode before promotion
        forged_rule = RuleTraceEvidence(
            rule_id="FORGED_PROV",
            source_ref="ep_not_in_store_404",
            source_type="episode",
            trigger_condition="c",
            action="a",
            failure_avoided="f",
            applicability_boundary="b",
            verification_status="verified",
        )
        cand.meta.rules = [forged_rule.to_dict()]
        cand.rule_traces = [forged_rule]
        cand_hash = compute_candidate_hash(cand)
        val_rec.content_hash = cand_hash
        cand_store.save_validation_record(val_rec)

        with pytest.raises(ValueError, match="Promotion rejected: Candidate 'cand_promote_neg_01' has invalid rule provenance"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
            )


# ==============================================================================
# 4. PromotionCard Integration (E1)
# ==============================================================================

def test_promotion_card_renders_rule_provenance_and_boundaries():
    """Verify PromotionCard displays structured rule provenance and extracts inferred rule boundaries."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_card_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        rule = RuleTraceEvidence(
            rule_id="LOG_DELIVERY_RULE",
            source_ref="ep_source_01",
            source_type="episode",
            trigger_condition="Multi-package tracking inquiry",
            action="Check all packages individually",
            failure_avoided="Premature delivery confirmation",
            applicability_boundary="Only DELIVERED count == total package count; otherwise unknown",
            verification_status="inferred",
        )

        cand = CandidateSkill(
            candidate_id="cand_card_01",
            skill_name="logistics_checker",
            decision="revise",
            source_requirement="req_card",
            meta=SkillMeta(
                name="logistics_checker",
                version="1.1.0",
                description="desc",
                use_when="when",
                rules=[rule.to_dict()],
            ),
            body="## Overview\nBody\n",
        )

        val_rec = ValidationRecord(
            candidate_id="cand_card_01",
            content_hash="h1",
            baseline_version="1.0.0",
            ratchet_decision="PASS",
        )

        card = build_promotion_card(
            candidate=cand,
            validation_record=val_rec,
            selection_total=10,
            selection_passed=10,
        )

        assert len(card.rule_provenance) == 1
        assert "[LOG_DELIVERY_RULE] INFERRED (source: ep_source_01, episode)" in card.rule_provenance[0]
        # Inferred boundary is automatically added to unverified_boundaries
        assert any("LOG_DELIVERY_RULE" in b for b in card.unverified_boundaries)

        rendered = render_promotion_card(card)
        assert "提炼规则溯源与失败机制 (Rule Provenance):" in rendered
        assert "LOG_DELIVERY_RULE" in rendered
        assert "Premature delivery confirmation" in rendered


# ==============================================================================
# 5. Targeted Gap Integration Tests (E1 Audit Remediation)
# ==============================================================================

def test_negative_heldout_purpose_episode_rejected_for_rule_evidence():
    """Verify episodes with purpose in ('heldout', 'evaluation', 'final_audit') are strictly rejected for rule evidence."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_heldout_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        heldout_ep = Episode(
            episode_id="ep_heldout_test_01",
            task_id="task_heldout_01",
            run_id="run_01",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"purpose": "heldout", "q": "test"},
            provenances=[],
            acceptance_criteria={},
            outcome="success",
            outcome_reason="ok",
            verification_evidence={"independent_pass": True, "verified_rules": ["RULE_HELDOUT_TEST"]},
        )
        ep_store.save_episode(heldout_ep)

        rule = RuleTraceEvidence(
            rule_id="RULE_HELDOUT_TEST",
            source_ref="ep_heldout_test_01",
            source_type="episode",
            trigger_condition="Any condition",
            action="Any action",
            failure_avoided="Any failure",
            applicability_boundary="Any boundary",
            verification_status="verified",
        )

        cand = CandidateSkill(
            candidate_id="cand_heldout_test",
            skill_name="logistics_checker",
            decision="create",
            source_episode_ids=[heldout_ep.episode_id],
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[rule.to_dict()]),
            body="body",
        )

        with pytest.raises(ValueError, match="disallowed purpose='heldout'; only 'learning' episodes are accepted"):
            cand_store.save_candidate(cand)


def test_negative_episode_success_alone_cannot_self_assert_verified_without_rule_level_evidence():
    """Verify episode success + independent_pass does NOT allow arbitrary rule to self-assert verified."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_succ_alone_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        # Episode succeeded overall, but has NO rule-level execution verification
        general_succ_ep = Episode(
            episode_id="ep_succ_no_rule_eval",
            task_id="task_gen_01",
            run_id="run_01",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria={},
            outcome="success",
            outcome_reason="Overall task passed",
            verification_evidence={"independent_pass": True},  # No verified_rules field!
        )
        ep_store.save_episode(general_succ_ep)

        rule = RuleTraceEvidence(
            rule_id="RULE_UNPROVEN_IN_EPISODE",
            source_ref=general_succ_ep.episode_id,
            source_type="episode",
            trigger_condition="Condition",
            action="Action",
            failure_avoided="Failure",
            applicability_boundary="Boundary",
            verification_status="verified",  # Falsely claims verified just because episode succeeded!
            task_id="task_gen_01",
        )

        cand = CandidateSkill(
            candidate_id="cand_unproven_rule",
            skill_name="logistics_checker",
            decision="create",
            source_episode_ids=[general_succ_ep.episode_id],
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[rule.to_dict()]),
            body="body",
        )

        with pytest.raises(ValueError, match="cannot claim 'verified' based merely on episode success/independent_pass without explicit rule-level execution verification evidence"):
            cand_store.save_candidate(cand)


def test_negative_source_drift_blocked_at_save_validate_promote():
    """Verify source drift (rule referencing episode outside candidate source_episode_ids or modified hash) is blocked."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_drift_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        ep1 = Episode(
            episode_id="ep_drift_01",
            task_id="task_01",
            run_id="r1",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria={},
            outcome="failure",
            outcome_reason="fail",
        )
        ep_store.save_episode(ep1)

        ep2 = Episode(
            episode_id="ep_other_02",
            task_id="task_02",
            run_id="r2",
            skill_name="logistics_checker",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria={},
            outcome="failure",
            outcome_reason="fail",
        )
        ep_store.save_episode(ep2)

        rule_drift = RuleTraceEvidence(
            rule_id="RULE_DRIFT",
            source_ref=ep1.episode_id,
            source_type="episode",
            trigger_condition="c",
            action="a",
            failure_avoided="f",
            applicability_boundary="b",
            verification_status="inferred",
            task_id=ep1.task_id,
        )

        # Candidate claims source_requirement="req_01" but rule references ep1 while candidate.source_episode_ids does NOT include ep1!
        cand = CandidateSkill(
            candidate_id="cand_drift_01",
            skill_name="logistics_checker",
            decision="create",
            source_requirement="req_01",
            source_episode_ids=["ep_other_02"],  # ep1 is NOT in source_episode_ids!
            meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[rule_drift.to_dict()]),
            body="body",
        )

        # Rejects on save
        with pytest.raises(ValueError, match="source_ref 'ep_drift_01' drifts: not present in candidate.source_episode_ids"):
            cand_store.save_candidate(cand)


def test_negative_unconnected_episode_store_fails_closed():
    """Verify candidate with episode rules cannot be verified if EpisodeStore is not connected."""
    rule = RuleTraceEvidence(
        rule_id="RULE_EP_NO_STORE",
        source_ref="ep_some_ep_01",
        source_type="episode",
        trigger_condition="c",
        action="a",
        failure_avoided="f",
        applicability_boundary="b",
        verification_status="inferred",
        body_quote="anchor_text",
    )
    cand = CandidateSkill(
        candidate_id="cand_no_store",
        skill_name="logistics_checker",
        decision="create",
        source_requirement="req_01",
        meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[rule.to_dict()]),
        body="body with anchor_text here",
    )
    errors = verify_candidate_rule_provenance(cand, episode_store=None)
    assert len(errors) == 1
    assert "cites episode 'ep_some_ep_01' but EpisodeStore is not connected (fail-closed)" in errors[0]


# ==============================================================================
# 6. Real Entrypoint Generation Tests (Requirement & Conversation)
# ==============================================================================

def test_generate_candidate_from_requirement_extracts_and_binds_rules():
    """Verify generate_candidate_from_requirement extracts rules from LLM response,

    verifies body_quote in body, and binds source_type='requirement' programmatically.
    """
    with tempfile.TemporaryDirectory(prefix="sf_e1_gen_req_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)

        gen_json = json.dumps({
            "name": "invoice_auditor",
            "version": "1.0.0",
            "description": "Invoice item receipt and VAT verification auditor",
            "use_when": "When validating invoice items and tax calculations",
            "not_for": ["Issuing bank transfers", "Modifying invoice records"],
            "keywords": ["invoice", "receipt", "audit", "tax"],
            "examples": ["Check VAT tax rate for INV_101", "Audit line item totals"],
            "rules": [
                {
                    "trigger_condition": "When invoice contains multiple tax line items",
                    "action": "Verify individual line item calculations before total sum",
                    "failure_avoided": "Rounding error propagation to overall invoice sum",
                    "applicability_boundary": "Only for multi-item invoices; abstains on fixed fee invoices",
                    "body_quote": "Verify individual line item calculations before total sum.",
                }
            ],
            "body": (
                "## Overview\nInvoice fulfillment assistant.\n\n"
                "## Instructions\n1. Read invoice header.\n"
                "2. Verify individual line item calculations before total sum.\n\n"
                "## Examples\n**Q**: Audit invoice\n**A**: Line items checked.\n\n"
                "## Constraints\n- Verify individual line item calculations before total sum.\n"
            ),
            "test_cases": [
                {"query": "Verify invoice INV_001 line items", "reference": "Line items breakdown"},
                {"query": "Audit line item tax rates", "reference": "Tax rate verified"},
                {"query": "Execute payment wire transfer", "reference": "Refuse: out of scope financial wire"},
            ],
        })

        scripted_llm = ScriptedCriteriaLLM(responses=[gen_json])

        from skillforge.skill_generator import generate_candidate_from_requirement
        res = generate_candidate_from_requirement(
            request="需要一个用于核查发票明细和税率计算的技能，规避尾差传递导致的审计异常",
            candidate_store=cand_store,
            llm=scripted_llm,
            repo_root=repo_root,
            registry=reg,
            task_id="task_req_test_01",
            similarity_threshold=0.95,
        )

        assert isinstance(res, CandidateSkill)
        # Assert LLM prompt contained required rule extraction instructions
        assert len(scripted_llm.calls) >= 1
        prompt_received = str(scripted_llm.calls[0]["messages"])
        assert "rules" in prompt_received
        assert "trigger_condition" in prompt_received
        assert "body_quote" in prompt_received

        # Assert candidate attributes
        assert res.source_type == "requirement"
        assert res.source_requirement is not None
        assert len(res.rule_traces) == 1

        rule = res.rule_traces[0]
        assert rule.rule_id == "RULE_invoice_auditor_1"
        assert rule.source_type == "requirement"
        assert rule.source_ref.startswith("req_")
        assert rule.trigger_condition == "When invoice contains multiple tax line items"
        assert rule.action == "Verify individual line item calculations before total sum"
        assert rule.failure_avoided == "Rounding error propagation to overall invoice sum"
        assert rule.body_quote == "Verify individual line item calculations before total sum."
        assert rule.body_quote in res.body
        assert rule.verification_status == "inferred"

        # Assert verification passes
        errors = verify_candidate_rule_provenance(res)
        assert errors == []


def test_generate_candidate_from_requirement_without_rules_leaves_empty():
    """Verify generate_candidate_from_requirement with no rules in LLM response leaves rules empty without fake mock rules."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_gen_empty_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)

        no_rules_json = json.dumps({
            "name": "invoice_auditor",
            "version": "1.0.0",
            "description": "Invoice item receipt and VAT verification auditor",
            "use_when": "When validating invoice items and tax calculations",
            "not_for": ["Issuing bank transfers", "Modifying invoice records"],
            "keywords": ["invoice", "receipt", "audit", "tax"],
            "examples": ["Check VAT tax rate for INV_101", "Audit line item totals"],
            "body": (
                "## Overview\nInvoice fulfillment assistant.\n\n"
                "## Instructions\n1. Read invoice header.\n\n"
                "## Examples\n**Q**: Audit invoice\n**A**: Line items checked.\n\n"
                "## Constraints\n- Check all.\n"
            ),
            "test_cases": [
                {"query": "Verify invoice INV_001 line items", "reference": "Line items breakdown"},
                {"query": "Audit line item tax rates", "reference": "Tax rate verified"},
                {"query": "Execute payment wire transfer", "reference": "Refuse: out of scope financial wire"},
            ],
        })

        scripted_llm = ScriptedCriteriaLLM(responses=[no_rules_json])

        from skillforge.skill_generator import generate_candidate_from_requirement
        res = generate_candidate_from_requirement(
            request="需要一个用于核查发票明细和税率计算的技能",
            candidate_store=cand_store,
            llm=scripted_llm,
            repo_root=repo_root,
            registry=reg,
            similarity_threshold=0.95,
        )

        assert isinstance(res, CandidateSkill)
        assert res.rule_traces == []
        assert res.meta.rules == []


def test_generate_candidate_from_conversation_preserves_session_and_messages():
    """Verify generate_candidate_from_conversation binds conversation source and preserves session and message IDs."""
    with tempfile.TemporaryDirectory(prefix="sf_e1_gen_conv_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        cand_store = CandidateStore(db_path)

        gen_json = json.dumps({
            "name": "invoice_auditor",
            "version": "1.0.0",
            "description": "Invoice item receipt and VAT verification auditor",
            "use_when": "When validating invoice items and tax calculations",
            "not_for": ["Issuing bank transfers", "Modifying invoice records"],
            "keywords": ["invoice", "receipt", "audit", "tax"],
            "examples": ["Check VAT tax rate for INV_101", "Audit line item totals"],
            "rules": [
                {
                    "trigger_condition": "When invoice contains multiple tax line items",
                    "action": "Verify individual line item calculations before total sum",
                    "failure_avoided": "Rounding error propagation to overall invoice sum",
                    "applicability_boundary": "Only for multi-item invoices; abstains on fixed fee invoices",
                    "body_quote": "Verify individual line item calculations before total sum.",
                }
            ],
            "body": (
                "## Overview\nInvoice fulfillment assistant.\n\n"
                "## Instructions\n1. Read invoice header.\n"
                "2. Verify individual line item calculations before total sum.\n\n"
                "## Examples\n**Q**: Audit invoice\n**A**: Line items checked.\n\n"
                "## Constraints\n- Verify individual line item calculations before total sum.\n"
            ),
            "test_cases": [
                {"query": "Verify invoice INV_001 line items", "reference": "Line items breakdown"},
                {"query": "Audit line item tax rates", "reference": "Tax rate verified"},
                {"query": "Execute payment wire transfer", "reference": "Refuse: out of scope financial wire"},
            ],
        })

        scripted_llm = ScriptedCriteriaLLM(responses=[gen_json])

        from skillforge.skill_generator import generate_candidate_from_conversation
        res = generate_candidate_from_conversation(
            session_id="sess_convo_dev_01",
            messages=[
                {"id": "msg_01", "role": "user", "content": "帮我查一下包裹"},
                {"id": "msg_02", "role": "assistant", "content": "需要逐一核对状态"},
            ],
            candidate_store=cand_store,
            llm=scripted_llm,
            repo_root=repo_root,
            registry=reg,
            message_ids=["msg_01", "msg_02"],
            similarity_threshold=0.95,
        )

        assert isinstance(res, CandidateSkill)
        assert res.source_type == "conversation"
        assert res.source_session_id == "sess_convo_dev_01"
        assert res.source_message_ids == ["msg_01", "msg_02"]

        rule = res.rule_traces[0]
        assert rule.source_type == "conversation"
        assert rule.source_ref == "sess_convo_dev_01"
        assert rule.body_quote in res.body

        errors = verify_candidate_rule_provenance(res)
        assert errors == []


def test_negative_body_quote_mismatch_rejected():
    """Verify rule whose body_quote is missing or does not match body is strictly rejected."""
    rule = RuleTraceEvidence(
        rule_id="RULE_MISMATCH",
        source_ref="req_test_01",
        source_type="requirement",
        trigger_condition="c",
        action="a",
        failure_avoided="f",
        applicability_boundary="b",
        verification_status="inferred",
        body_quote="Non-existent quote that is not anywhere in the body text",
    )
    cand = CandidateSkill(
        candidate_id="cand_mismatch_01",
        skill_name="logistics_checker",
        decision="create",
        source_requirement="req_01",
        meta=SkillMeta(name="logistics_checker", version="1.0.1", description="d", use_when="w", rules=[rule.to_dict()]),
        body="## Overview\nThis body does not contain that quote at all.\n",
    )
    errors = verify_candidate_rule_provenance(cand)
    assert len(errors) == 1
    assert "body_quote does not match candidate body" in errors[0]

