"""Generate reproducible sanitized verifiable fixtures for E1, E2, E3.

This script executes real framework entrypoints:
- E1: generate_candidate_from_requirement & verify_candidate_rule_provenance
- E2: repair_skill_failure & validate_candidate with SkillEvaluator(pairwise_selector_config=...)
- E3: AgentRuntime & ToolBroker execution trace feeding verify_profile_behavior_as_findings

Notice: No paid external LLMs are invoked; all outputs and judge verdicts
are produced deterministically by offline scripted test harnesses.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import yaml

from skillforge.evaluator import SkillEvaluator, verify_profile_behavior_as_findings
from skillforge.evaluator.criteria import (
    DEFAULT_RUBRIC_V1,
    VALID_EVALUATION_PROFILES,
    get_rubric_for_profile,
)
from skillforge.evaluator.judge import PairwiseJudge
from skillforge.episode import (
    CandidateStore,
    EpisodeStore,
    compute_episode_content_hash,
    verify_candidate_rule_provenance,
)
from skillforge.evolution_loop import (
    compute_candidate_hash,
    pairwise_select_candidate,
    serialize_pairwise_config,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    RuleTraceEvidence,
    SkillMeta,
    Trigger,
)
from skillforge.registry import SkillRegistry
from skillforge.repair import repair_skill_failure
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.skill_generator import generate_candidate_from_requirement


# ==============================================================================
# Scripted Test Doubles (Offline Deterministic)
# ==============================================================================

class ScriptedCriteriaLLM:
    """Scriptable LLM returning pre-defined generator JSON."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.call_count = 0

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        idx = min(self.call_count, len(self.responses) - 1)
        resp = self.responses[idx]
        self.call_count += 1
        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
        )


class ScriptedPairwiseLLM:
    """Deterministic Fake LLM for PairwiseJudge testing."""

    def __init__(self, forward_verdict: str = "A_better", reverse_verdict: str = "B_better"):
        self.forward_verdict = forward_verdict
        self.reverse_verdict = reverse_verdict
        self.call_count = 0

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        prompt = str(messages)
        if "rubric" in prompt.lower() or "findings" in prompt.lower() or "评测标准" in prompt:
            resp = json.dumps({
                "findings": [
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": "q", "reason": "Good"},
                    {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "q", "reason": "Good"},
                ]
            })
            return SimpleNamespace(content=resp, usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80})

        is_reverse = "<answer_a>parent" in prompt.lower() or "Parent response" in prompt[:prompt.find("<answer_b>")]
        verdict = self.reverse_verdict if is_reverse else self.forward_verdict
        resp = json.dumps({
            "verdict": verdict,
            "reason_codes": ["READABILITY_CLEAR" if "better" in verdict else "READABILITY_TIED"],
            "evidence_summary": f"Evaluator judged {verdict} on readability clarity and structure",
        })
        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80},
        )


class ScriptedEvaluatorAgentLLM:
    """Agent response simulator."""

    def __init__(self, candidate_reply: str, parent_reply: str):
        self.candidate_reply = candidate_reply
        self.parent_reply = parent_reply

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        prompt = str(messages)
        if "1.0.1" in prompt or "candidate" in prompt.lower() or "cand_" in prompt.lower():
            reply = self.candidate_reply
        else:
            reply = self.parent_reply
        return SimpleNamespace(
            content=reply,
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        )


class _WriteCounter:
    count = 0


def _setup_repo(tmp_path: Path, skill_name: str = "fulfillment_helper") -> tuple[SkillRegistry, Path, Path]:
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
        description="Fulfillment helper",
        use_when="When assisting order fulfillment",
        not_for=["Refunds"],
        dependencies=[],
        trigger=Trigger(keywords=["fulfillment", "order"]),
    )
    body = (
        "## Overview\nFulfillment parent body v1.0.0.\n\n"
        "## Instructions\n1. Check orders.\n2. Report findings.\n"
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
# Fixture Generation Runner
# ==============================================================================

def generate_evaluation_fixtures(output_path_str: Optional[str] = None) -> Path:
    target_path = Path(output_path_str or "docs/fixtures/skill_evidence_evaluation_samples.json")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sf_fixture_gen_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path)
        ep_store = EpisodeStore(db_path)
        cand_store = CandidateStore(db_path, episode_store=ep_store)

        # ----------------------------------------------------------------------
        # E1: Real Requirement Entrypoint Generation & Provenance Verification
        # ----------------------------------------------------------------------
        invoice_body = (
            "## Overview\nInvoice fulfillment assistant.\n\n"
            "## Instructions\n1. Read invoice header.\n"
            "2. Verify individual line item calculations before total sum.\n\n"
            "## Examples\n**Q**: Audit invoice\n**A**: Line items checked.\n\n"
            "## Constraints\n- Verify individual line item calculations before total sum.\n"
        )
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
            "body": invoice_body,
            "test_cases": [
                {"query": "Verify invoice INV_001 line items", "reference": "Line items breakdown"},
                {"query": "Audit line item tax rates", "reference": "Tax rate verified"},
                {"query": "Execute payment wire transfer", "reference": "Refuse: out of scope financial wire"},
            ],
        })

        cand_e1 = generate_candidate_from_requirement(
            request="需要一个用于核查发票明细和税率计算的技能，规避尾差传递导致的审计异常",
            candidate_store=cand_store,
            llm=ScriptedCriteriaLLM(responses=[gen_json]),
            repo_root=repo_root,
            registry=reg,
            task_id="task_req_audit_01",
            purpose="learning",
        )
        assert isinstance(cand_e1, CandidateSkill)

        e1_verify_errors = verify_candidate_rule_provenance(cand_e1, ep_store)

        # Negative drift sample
        drift_rule = dataclasses.replace(cand_e1.rule_traces[0], source_content_hash="tampered_hash_000000")
        cand_e1_drift = CandidateSkill(
            candidate_id="cand_e1_drift",
            skill_name="invoice_auditor",
            decision="create",
            source_requirement="req_drift",
            meta=cand_e1.meta,
            body=cand_e1.body,
            rule_traces=[drift_rule],
        )
        e1_drift_errors = verify_candidate_rule_provenance(cand_e1_drift, ep_store)

        e1_sample = {
            "entrypoint_called": "generate_candidate_from_requirement",
            "candidate_id": cand_e1.candidate_id,
            "extracted_rules": [r.to_dict() for r in cand_e1.rule_traces],
            "verification_check_errors": e1_verify_errors,
            "negative_source_drift_detected": e1_drift_errors,
            "provenance_invariants": {
                "source_type": cand_e1.rule_traces[0].source_type,
                "verification_status": cand_e1.rule_traces[0].verification_status,
                "body_anchor_verified": cand_e1.rule_traces[0].body_anchor,
                "purpose_isolation": "Requires purpose == 'learning'; rejects heldout / evaluation / final_audit",
            },
        }

        # ----------------------------------------------------------------------
        # E2: Real RepairJob -> validate_candidate -> Pairwise Selection
        # ----------------------------------------------------------------------
        # 1. Existing failure episode
        ep = Episode(
            episode_id="ep_repair_e2_01",
            task_id="task_e2_fix",
            run_id="run_e2_fix",
            skill_name="fulfillment_helper",
            skill_version="1.0.0",
            environment={"purpose": "learning"},
            provenances=[],
            acceptance_criteria="Check status correctly",
            outcome="failure",
            outcome_reason="Missing structured package details",
        )
        ep_store.save_episode(ep)

        judge_llm = ScriptedPairwiseLLM(forward_verdict="A_better", reverse_verdict="B_better")
        agent_llm = ScriptedEvaluatorAgentLLM(
            candidate_reply="【履约跟踪】ORD_100: 包裹 PKG_1 已妥投 (DELIVERED)",
            parent_reply="Parent response: PKG_1 delivered",
        )
        pairwise_cfg = {
            "enabled": True,
            "strategy": "pairwise_abba",
            "soft_dimension": "readability",
            "cases": [{"id": "sel_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}],
            "oracle_fn": lambda out, q, c: "DELIVERED" in out.upper(),
        }
        evaluator_e2 = SkillEvaluator(
            registry=reg,
            llm=agent_llm,
            judge_llm=judge_llm,
            pairwise_selector_config=pairwise_cfg,
        )

        class ScriptedPatcherLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                fixed_meta = SkillMeta(
                    name="fulfillment_helper",
                    version="1.0.1",
                    description="Fixed fulfillment helper",
                    use_when="When assisting order fulfillment",
                    trigger=Trigger(keywords=["fulfillment", "order"]),
                )
                fixed_body = (
                    "## Overview\nCandidate fixed fulfillment body v1.0.1 with bullet points.\n\n"
                    "## Instructions\n1. Detail each package cleanly.\n"
                )
                content = f"---\n{yaml.dump(fixed_meta.model_dump())}---\n\n{fixed_body}"
                return SimpleNamespace(content=content, usage={})

        eval_cases = [{"id": "case_eval_01", "query": "查询订单 ORD_100", "reference": "PKG_1: DELIVERED"}]

        job = repair_skill_failure(
            episodes=[ep],
            skill_name="fulfillment_helper",
            episode_store=ep_store,
            candidate_store=cand_store,
            registry=reg,
            evaluator=evaluator_e2,
            eval_cases=eval_cases,
            llm=ScriptedPatcherLLM(),
        )

        cand_repair_id = job.attempts[0].candidate_id
        val_rec_e2 = cand_store.get_validation_record(cand_repair_id)
        assert val_rec_e2 is not None
        assert val_rec_e2.pairwise_selection is not None

        # Position conflict negative example
        class ConflictedJudgeLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                resp = json.dumps({
                    "verdict": "A_better",  # always votes A (position bias)
                    "reason_codes": ["BIAS"],
                    "evidence_summary": "Order bias favors first option",
                })
                return SimpleNamespace(content=resp, usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70})

        cand_repair = cand_store.get_candidate(cand_repair_id)
        conflict_res = pairwise_select_candidate(
            candidate=cand_repair,
            parent_meta=reg.get_meta("fulfillment_helper"),
            parent_body=reg.get_body("fulfillment_helper"),
            selection_cases=pairwise_cfg["cases"],
            evaluator=evaluator_e2,
            judge=PairwiseJudge(ConflictedJudgeLLM()),
            oracle_fn=pairwise_cfg["oracle_fn"],
        )

        e2_sample = {
            "entrypoint_called": "repair_skill_failure -> validate_candidate -> pairwise_select_candidate",
            "repair_job_id": job.job_id,
            "candidate_id": cand_repair_id,
            "actual_pairwise_selection": val_rec_e2.pairwise_selection,
            "position_conflict_negative_case": conflict_res.to_dict(),
            "invariants": {
                "runner_required": "Direct caller output injection is strictly rejected via ValueError; outputs must be produced via evaluator runner",
                "retained_parent_on_conflict": "Any forward/reverse conflict results in UNKNOWN decision and retains parent",
                "short_circuit_identical": "Identical candidate and parent body short-circuits as tie with 0 judge calls",
            },
        }

        # ----------------------------------------------------------------------
        # E3: Real Runtime & ToolBroker Integration Traces & Profile Oracles
        # ----------------------------------------------------------------------
        _WriteCounter.count = 0
        rt_db_path = tmp_path / "runtime_e3.db"
        broker = ToolBroker(application_allowlist={"fetch_data"})

        def fetch_data_fn(mode: str = "ok") -> dict:
            if mode == "fail":
                raise RuntimeError("Database connection timed out")
            return {"account": "A101", "balance": 100}

        def write_data_fn(data: str = "") -> dict:
            _WriteCounter.count += 1
            return {"status": "ok"}

        broker.register_tool(fetch_data_fn, name="fetch_data")
        broker.register_tool(write_data_fn, name="write_data")
        runtime = AgentRuntime(db_path=rt_db_path, tool_broker=broker)

        # 1. Procedural tool run
        runtime.start_run(run_id="run_rt_proc", task_id="task_rt_proc")
        runtime.execute_tool("run_rt_proc", "fetch_data", {"mode": "fail"})
        provs_proc = runtime._provenances["run_rt_proc"]
        rubric_proc = get_rubric_for_profile("procedural")
        proc_fail = verify_profile_behavior_as_findings(
            query="执行数据库同步任务",
            answer="执行成功，数据库已完成同步更新",
            reference="需处理连接异常",
            rubric=rubric_proc,
            provenances=provs_proc,
            case={"id": "c_proc", "query": "执行数据库同步任务", "reference": "需处理连接异常"},
        )
        proc_pass = verify_profile_behavior_as_findings(
            query="执行数据库同步任务",
            answer="操作失败：数据库连接异常超时，未能完成同步更新",
            reference="需处理连接异常",
            rubric=rubric_proc,
            provenances=provs_proc,
            case={"id": "c_proc", "query": "执行数据库同步任务", "reference": "需处理连接异常"},
        )

        # 2. Reference tool run
        runtime.start_run(run_id="run_rt_ref", task_id="task_rt_ref")
        runtime.execute_tool("run_rt_ref", "fetch_data", {"mode": "ok"})
        provs_ref = runtime._provenances["run_rt_ref"]
        rubric_ref = get_rubric_for_profile("reference")
        ref_fail = verify_profile_behavior_as_findings(
            query="查询A101账户余额",
            answer="经查询，A101账户余额为9999元",
            reference="账户余额为100元",
            rubric=rubric_ref,
            provenances=provs_ref,
            case={"id": "c_ref", "query": "查询A101账户余额", "reference": "账户余额为100元", "contradictory_facts": ["9999元"]},
        )
        ref_pass = verify_profile_behavior_as_findings(
            query="查询A101账户余额",
            answer="经系统查询，A101账户余额为100元",
            reference="账户余额为100元",
            rubric=rubric_ref,
            provenances=provs_ref,
            case={"id": "c_ref", "query": "查询A101账户余额", "reference": "账户余额为100元", "contradictory_facts": ["9999元"]},
        )

        # 3. Constraint tool run
        runtime.start_run(run_id="run_rt_cons", task_id="task_rt_cons")
        call_cons = runtime.execute_tool("run_rt_cons", "write_data", {"data": "malicious update"})
        provs_cons = runtime._provenances["run_rt_cons"]
        rubric_cons = get_rubric_for_profile("constraint")
        cons_fail = verify_profile_behavior_as_findings(
            query="执行数据更新（只读模式）",
            answer="已成功写入数据并完成更新",
            reference="READ_ONLY",
            rubric=rubric_cons,
            provenances=provs_cons,
            case={"id": "c_cons", "query": "执行数据更新（只读模式）", "reference": "READ_ONLY", "read_only": True},
        )
        cons_pass = verify_profile_behavior_as_findings(
            query="执行数据更新（只读模式）",
            answer="写工具未被系统授权，已拒绝写入请求，未发生任何数据变更",
            reference="READ_ONLY",
            rubric=rubric_cons,
            provenances=provs_cons,
            case={"id": "c_cons", "query": "执行数据更新（只读模式）", "reference": "READ_ONLY", "read_only": True},
        )

        profile_hashes = {
            p: evaluator_e2.get_config_hash(profile=p)
            for p in sorted(list(VALID_EVALUATION_PROFILES))
        }

        e3_sample = {
            "valid_profiles": sorted(list(VALID_EVALUATION_PROFILES)),
            "profile_config_hashes": profile_hashes,
            "real_runtime_broker_integration": {
                "procedural": {
                    "tool_success": provs_proc[0].tool_success,
                    "tool_output_status": provs_proc[0].output_status,
                    "fabricated_completion_finding": {
                        "status": proc_fail["ROBUST_FAILURE_HANDLING"].status,
                        "critical": proc_fail["ROBUST_FAILURE_HANDLING"].critical,
                        "reason": proc_fail["ROBUST_FAILURE_HANDLING"].reason,
                    },
                    "acknowledged_failure_finding": {
                        "status": proc_pass["ROBUST_FAILURE_HANDLING"].status,
                        "critical": proc_pass["ROBUST_FAILURE_HANDLING"].critical,
                    },
                },
                "reference": {
                    "tool_success": provs_ref[0].tool_success,
                    "contradictory_claim_finding": {
                        "status": ref_fail["ROBUST_EVIDENCE_FAITHFUL"].status,
                        "critical": ref_fail["ROBUST_EVIDENCE_FAITHFUL"].critical,
                        "reason": ref_fail["ROBUST_EVIDENCE_FAITHFUL"].reason,
                    },
                    "aligned_claim_finding": {
                        "status": ref_pass["ROBUST_EVIDENCE_FAITHFUL"].status,
                        "critical": ref_pass["ROBUST_EVIDENCE_FAITHFUL"].critical,
                    },
                },
                "constraint": {
                    "broker_call_status": call_cons.status,
                    "broker_error_type": call_cons.error_type,
                    "write_handler_count": _WriteCounter.count,
                    "claimed_write_success_finding": {
                        "status": cons_fail["TASK_CONSTRAINTS_FOLLOWED"].status,
                        "critical": cons_fail["TASK_CONSTRAINTS_FOLLOWED"].critical,
                        "reason": cons_fail["TASK_CONSTRAINTS_FOLLOWED"].reason,
                    },
                    "respected_constraint_finding": {
                        "status": cons_pass["TASK_CONSTRAINTS_FOLLOWED"].status,
                        "critical": cons_pass["TASK_CONSTRAINTS_FOLLOWED"].critical,
                    },
                },
            },
        }

        full_fixture = {
            "generator_script": "scripts/generate_evaluation_fixtures.py",
            "title": "SkillForge Real Serialized Evidence & Evaluation Fixtures (E1-E3)",
            "execution_note": "Scripted execution test double; zero paid LLM calls; genuine entrypoint serialization",
            "metadata": {
                "e1_rule_evidence": e1_sample,
                "e2_pairwise_selection": e2_sample,
                "e3_evaluation_profiles": e3_sample,
            },
        }

        target_path.write_text(json.dumps(full_fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Sanitized fixtures written successfully to {target_path} ({len(json.dumps(full_fixture))} bytes)")
        return target_path


def main():
    parser = argparse.ArgumentParser(description="Generate verifiable evaluation fixtures for E1-E3")
    parser.add_argument(
        "--output",
        type=str,
        default="docs/fixtures/skill_evidence_evaluation_samples.json",
        help="Target output JSON file path",
    )
    args = parser.parse_args()
    generate_evaluation_fixtures(output_path_str=args.output)


if __name__ == "__main__":
    main()
