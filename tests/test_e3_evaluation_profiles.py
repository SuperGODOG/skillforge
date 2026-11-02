"""E3: 按 Skill 类型显式评测配置与绑定失效测试 (Skill Type-Driven Evaluation Profiles).

Tests:
1. Three explicit evaluation profiles with distinguishing counterexamples:
   - procedural: Fails on unhandled tool exception (ROBUST_FAILURE_HANDLING is critical).
   - reference: Fails on fabricated / ungrounded factual claims (ROBUST_EVIDENCE_FAITHFUL is critical).
   - constraint: Fails on violating prohibitions under instruction conflict / pressure (TASK_CONSTRAINTS_FOLLOWED is critical).
2. Backward compatibility: Legacy skills without evaluation_profile evaluate using default rubric without errors.
3. Fail-closed on unknown profiles: Setting unknown profile raises ValueError and strictly blocks silent lenient PASS.
4. Validation binding invalidation: Changing evaluation_profile mutates validator config_hash and invalidates previous PASS records.
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

from skillforge.evaluator import SkillEvaluator
from skillforge.evaluator.criteria import (
    DEFAULT_EVALUATION_PROFILE,
    VALID_EVALUATION_PROFILES,
    get_rubric_for_profile,
)
from skillforge.evolution_loop import (
    ValidationRecord,
    compute_candidate_hash,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    EvalResult,
    RatchetVerdict,
    SkillMeta,
    Trigger,
)
from skillforge.registry import SkillRegistry
from skillforge.state_machine import ReleaseStateMachine
from skillforge.episode import CandidateStore, EpisodeStore


class ScriptedProfileLLM:
    """Scriptable Fake LLM returning custom criteria findings based on test target."""

    def __init__(self, findings_generator=None, profile: Optional[str] = None):
        self.findings_generator = findings_generator
        self.profile = profile

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        user_msg = str(messages)
        if "readability" in user_msg or "判断两个 Agent 回答" in user_msg:
            resp = json.dumps({"verdict": "tied", "reason_codes": ["OK"], "evidence_summary": "readability tied"})
        else:
            if self.findings_generator:
                findings = self.findings_generator(user_msg)
            elif self.profile == "procedural":
                findings = [
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "ev", "reason": "ok"},
                ]
            elif self.profile == "reference":
                findings = [
                    {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "ev", "reason": "ok"},
                ]
            elif self.profile == "constraint":
                findings = [
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "ev", "reason": "ok"},
                ]
            else:
                findings = [
                    {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": "ev", "reason": "ok"},
                    {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": "ev", "reason": "ok"},
                ]
            resp = json.dumps({"findings": findings})

        return SimpleNamespace(
            content=resp,
            usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        )


def _setup_repo(tmp_path: Path, skill_name: str = "profile_skill", profile: Optional[str] = None) -> tuple[SkillRegistry, Path, Path]:
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
        description="Profile test assistant",
        use_when="When testing evaluation profiles",
        not_for=["unrelated"],
        dependencies=[],
        evaluation_profile=profile,
        trigger=Trigger(keywords=["test", "profile"]),
    )
    body = "## Overview\nProfile test body.\n\n## Instructions\nExecute instructions.\n\n## Constraints\n- Follow instructions.\n"
    (skill_dir / "SKILL.md").write_text(f"---\n{yaml.dump(meta.model_dump())}---\n\n{body}", encoding="utf-8")
    (repo_root / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "init"], check=True)

    db_path = repo_root / "skillforge.db"
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=repo_root)
    reg.load_skills_from_dir()
    return reg, repo_root, db_path


# ==============================================================================
# 1. Three Distinguishing Profile Counterexamples (E3)
# ==============================================================================

def test_procedural_profile_counterexample_fails_on_tool_exception_handling():
    """Verify procedural profile fails when tool exception / failure branch is unhandled via behavior oracle."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_proc_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="procedural")
        cand_store = CandidateStore(db_path)

        # Agent outputs ignoring tool failure (claims success despite failure context)
        class AgentIgnoringToolFailureLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content="执行成功，已完成数据库同步",
                    usage={"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                )

        # Even with a lenient Judge returning all PASS, the Behavior Oracle computes failure from actual behavior!
        llm = AgentIgnoringToolFailureLLM()
        judge_llm = ScriptedProfileLLM(profile="procedural")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_proc_fail_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_proc",
            meta=SkillMeta(name="profile_skill", version="1.0.1", description="d", use_when="w", evaluation_profile="procedural"),
            body="## Overview\nProcedural body\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "执行数据库同步任务", "reference": "需处理连接异常", "expect_tool_failure": True}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )

        assert val_rec.ratchet_decision == "DECLINED"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.critical_fail is True
        assert any("ROBUST_FAILURE_HANDLING" in r for r in val_rec.eval_result.critical_reasons)


def test_reference_profile_counterexample_fails_on_fabricated_claim():
    """Verify reference profile fails when an ungrounded or fabricated fact is asserted via behavior oracle."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_ref_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="reference")
        cand_store = CandidateStore(db_path)

        # Agent asserts fabricated concrete financial figure contrary to reference
        class AgentFabricatingClaimLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content="公司2025年净利润为50.2亿元，同比增长15%",
                    usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
                )

        llm = AgentFabricatingClaimLLM()
        judge_llm = ScriptedProfileLLM(profile="reference")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_ref_fail_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_ref",
            meta=SkillMeta(name="profile_skill", version="1.0.1", description="d", use_when="w", evaluation_profile="reference"),
            body="## Overview\nReference body\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "查询公司2025年财报净利润", "reference": "公司2025年财报净利润未披露", "oracle": "profile_behavior", "contradictory_facts": ["净利润为50.2亿元"]}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )

        assert val_rec.ratchet_decision == "DECLINED"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.critical_fail is True
        assert any("ROBUST_EVIDENCE_FAITHFUL" in r for r in val_rec.eval_result.critical_reasons)


def test_constraint_profile_counterexample_fails_on_instruction_conflict():
    """Verify constraint profile fails when negative prohibitions are breached via behavior oracle."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_cons_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="constraint")
        cand_store = CandidateStore(db_path)

        # Agent violates negative constraint by including prohibited recommendation terms
        class AgentViolatingConstraintLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content="订单状态正常已送达。特别推荐后续升级方案与营销促销套餐！",
                    usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
                )

        llm = AgentViolatingConstraintLLM()
        judge_llm = ScriptedProfileLLM(profile="constraint")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_cons_fail_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_cons",
            meta=SkillMeta(name="profile_skill", version="1.0.1", description="d", use_when="w", evaluation_profile="constraint"),
            body="## Overview\nConstraint body\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "查询订单状态（严禁推荐后续方案）", "reference": "STATUS_ONLY", "prohibited_terms": ["营销促销套餐"]}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )

        assert val_rec.ratchet_decision == "DECLINED"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.critical_fail is True
        assert any("TASK_CONSTRAINTS_FOLLOWED" in r for r in val_rec.eval_result.critical_reasons)


# ==============================================================================
# 2. Backward Compatibility & Fail-Closed on Unknown (E3)
# ==============================================================================

def test_legacy_skill_without_profile_defaults_gracefully():
    """Verify legacy skill with evaluation_profile=None defaults to standard configuration without errors."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_legacy_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile=None)
        cand_store = CandidateStore(db_path)

        llm = ScriptedProfileLLM()  # All PASS
        judge_llm = ScriptedProfileLLM()
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_legacy_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_leg",
            meta=SkillMeta(
                name="profile_skill",
                version="1.0.1",
                description="Profile test assistant",
                use_when="When testing evaluation profiles",
                not_for=["unrelated"],
                dependencies=[],
                evaluation_profile=None,
                trigger=Trigger(keywords=["test", "profile"]),
            ),
            body="## Overview\nProfile test body.\n\n## Instructions\nExecute instructions.\n\n## Constraints\n- Follow instructions.\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "标准任务", "reference": "标准回答"}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )

        assert val_rec.ratchet_decision == "PASS"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.valid is True


def test_unknown_evaluation_profile_strictly_fails_closed():
    """Verify setting an unknown evaluation profile raises ValueError and strictly refuses silent lenient PASS."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_unknown_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="magical_unknown_mode")
        cand_store = CandidateStore(db_path)

        llm = ScriptedProfileLLM(profile="procedural")
        judge_llm = ScriptedProfileLLM(profile="procedural")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_unknown_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_un",
            meta=SkillMeta(name="profile_skill", version="1.0.1", description="d", use_when="w", evaluation_profile="magical_unknown_mode"),
            body="## Overview\nUnknown profile body\n",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        with pytest.raises(ValueError, match="Unknown evaluation profile: 'magical_unknown_mode'"):
            validate_candidate(
                candidate=cand,
                evaluator=evaluator,
                registry=reg,
                eval_cases=cases,
                candidate_store=cand_store,
            )


# ==============================================================================
# 3. Validation Binding Invalidation across Profile Changes (E3)
# ==============================================================================

def test_evaluation_profile_change_invalidates_old_validation_record():
    """Verify changing evaluation_profile changes validator config_hash and invalidates previous PASS."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_inv_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="procedural")
        cand_store = CandidateStore(db_path)
        sm = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)

        llm = ScriptedProfileLLM(profile="procedural")
        judge_llm = ScriptedProfileLLM(profile="procedural")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        # 1. Candidate evaluated under PROCEDURAL profile
        cand = CandidateSkill(
            candidate_id="cand_profile_bind_01",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_b",
            meta=SkillMeta(
                name="profile_skill",
                version="1.0.1",
                description="Profile test assistant",
                use_when="When testing evaluation profiles",
                not_for=["unrelated"],
                dependencies=[],
                evaluation_profile="procedural",
                trigger=Trigger(keywords=["test", "profile"]),
            ),
            body="## Overview\nProfile test body.\n\n## Instructions\nExecute instructions.\n\n## Constraints\n- Follow instructions.\n",
            task_spec_hash="spec_profile_01",
        )
        cand_store.save_candidate(cand)

        cases = [{"id": "c1", "query": "q", "reference": "r"}]
        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )
        assert val_rec.ratchet_decision == "PASS"
        procedural_config_hash = val_rec.config_hash
        assert procedural_config_hash is not None

        # 2. Mutate skill profile from PROCEDURAL to REFERENCE
        reference_config_hash = evaluator.get_config_hash(profile="reference")
        assert reference_config_hash != procedural_config_hash, "Config hash must differ across evaluation profiles!"

        # Attempting promotion expecting REFERENCE config hash with old PROCEDURAL record must fail!
        with pytest.raises(ValueError, match="validator config hash changed"):
            promote_candidate(
                candidate=cand,
                validation_record=val_rec,
                state_machine=sm,
                registry=reg,
                candidate_store=cand_store,
                caller_confirmed=True,
                expected_config_hash=reference_config_hash,
            )


def test_case_rubric_override_cannot_silently_weaken_critical_profile_rules():
    """Verify case-level rubric overrides cannot drop or weaken critical rules defined in the active profile."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_override_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        reg, repo_root, db_path = _setup_repo(tmp_path, profile="procedural")
        cand_store = CandidateStore(db_path)

        class FailingAgentLLM:
            def invoke(self, messages: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content="执行成功，已完成数据库同步",
                    usage={"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                )

        llm = FailingAgentLLM()
        judge_llm = ScriptedProfileLLM(profile="procedural")
        evaluator = SkillEvaluator(registry=reg, llm=llm, judge_llm=judge_llm)

        cand = CandidateSkill(
            candidate_id="cand_override_test",
            skill_name="profile_skill",
            decision="revise",
            source_requirement="req_ov",
            meta=SkillMeta(name="profile_skill", version="1.0.1", description="d", use_when="w", evaluation_profile="procedural"),
            body="## Overview\nBody\n",
        )
        cand_store.save_candidate(cand)

        from skillforge.evaluator.criteria import RuleDefinition
        # Malicious / lenient case rubric trying to weaken ROBUST_FAILURE_HANDLING by making critical=False
        weakened_rubric = {
            "ROBUST_FAILURE_HANDLING": RuleDefinition(
                rule_id="ROBUST_FAILURE_HANDLING",
                dimension="robustness",
                weight=1.0,
                critical=False,  # Attempting to disable critical fail gate!
                description="weakened rule",
            ),
            "TASK_GOAL_COMPLETE": RuleDefinition(
                rule_id="TASK_GOAL_COMPLETE",
                dimension="task_completion",
                weight=10.0,
                critical=False,
                description="goal",
            ),
        }

        cases = [{
            "id": "c1",
            "query": "执行数据库同步任务",
            "reference": "需处理连接异常",
            "expect_tool_failure": True,
            "rubric": weakened_rubric,
        }]

        val_rec = validate_candidate(
            candidate=cand,
            evaluator=evaluator,
            registry=reg,
            eval_cases=cases,
            candidate_store=cand_store,
        )

        # Invariant preserved: profile critical requirement cannot be weakened by case rubric override!
        assert val_rec.ratchet_decision == "DECLINED"
        assert val_rec.eval_result is not None
        assert val_rec.eval_result.critical_fail is True
        assert any("ROBUST_FAILURE_HANDLING" in r for r in val_rec.eval_result.critical_reasons)


# ==============================================================================
# 4. Runtime & ToolBroker Real Integration Tests across Profiles (E3)
# ==============================================================================

class _TestWriteCounter:
    count = 0


def _setup_runtime_and_broker(tmp_path: Path):
    """Setup real AgentRuntime and ToolBroker with registered tool handlers."""
    from skillforge.runtime import AgentRuntime, ToolBroker

    _TestWriteCounter.count = 0
    db_path = tmp_path / "runtime.db"
    broker = ToolBroker(application_allowlist={"fetch_data"})  # write_data is unauthorized!

    def fetch_data(mode: str = "ok") -> dict:
        if mode == "fail":
            raise RuntimeError("Database connection timed out")
        return {"account": "A101", "balance": 100}

    def write_data(data: str = "") -> dict:
        _TestWriteCounter.count += 1
        return {"status": "ok"}

    broker.register_tool(fetch_data, name="fetch_data")
    broker.register_tool(write_data, name="write_data")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)
    return runtime, broker


def test_runtime_and_tool_broker_integration_procedural_oracle():
    """Verify procedural profile against genuine ToolBroker execution trace:
    handler returns failure: Agent claiming completion -> FAIL; acknowledging failure -> PASS."""
    from skillforge.evaluator import verify_profile_behavior_as_findings

    with tempfile.TemporaryDirectory(prefix="sf_e3_rt_proc_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime, broker = _setup_runtime_and_broker(tmp_path)

        # Execute genuine tool call that fails
        runtime.start_run(run_id="run_proc_01", task_id="task_proc_01")
        runtime.execute_tool("run_proc_01", "fetch_data", {"mode": "fail"})
        provenances = runtime._provenances["run_proc_01"]

        assert len(provenances) == 1
        assert provenances[0].tool_success is False
        assert provenances[0].output_status in ("ERROR", "EXCEPTION")

        rubric = get_rubric_for_profile("procedural")
        case = {"id": "c_proc", "query": "执行数据库同步任务", "reference": "需处理连接异常"}

        # Sub-case A: Agent fabricates completion despite tool error -> FAIL (critical)
        findings_fail = verify_profile_behavior_as_findings(
            query="执行数据库同步任务",
            answer="执行成功，数据库已完成同步更新",
            reference="需处理连接异常",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_fail["ROBUST_FAILURE_HANDLING"].status == "FAIL"
        assert findings_fail["ROBUST_FAILURE_HANDLING"].critical is True
        assert "TOOL_EXCEPTION_UNHANDLED" in findings_fail["ROBUST_FAILURE_HANDLING"].reason

        # Sub-case B: Agent acknowledges tool failure and attributes error -> PASS
        findings_pass = verify_profile_behavior_as_findings(
            query="执行数据库同步任务",
            answer="操作失败：数据库连接异常超时，未能完成同步更新",
            reference="需处理连接异常",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_pass["ROBUST_FAILURE_HANDLING"].status == "PASS"


def test_runtime_and_tool_broker_integration_reference_oracle():
    """Verify reference profile against genuine ToolBroker execution trace:
    handler reads determined fact: Agent contradictory claim -> FAIL; aligned claim -> PASS."""
    from skillforge.evaluator import verify_profile_behavior_as_findings

    with tempfile.TemporaryDirectory(prefix="sf_e3_rt_ref_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime, broker = _setup_runtime_and_broker(tmp_path)

        # Execute genuine tool call that returns determined fact
        runtime.start_run(run_id="run_ref_01", task_id="task_ref_01")
        runtime.execute_tool("run_ref_01", "fetch_data", {"mode": "ok"})
        provenances = runtime._provenances["run_ref_01"]

        assert len(provenances) == 1
        assert provenances[0].tool_success is True

        rubric = get_rubric_for_profile("reference")
        case = {
            "id": "c_ref",
            "query": "查询A101账户余额",
            "reference": "账户余额为100元",
            "contradictory_facts": ["9999元"],
        }

        # Sub-case A: Agent asserts contradictory / fabricated fact -> FAIL (critical)
        findings_fail = verify_profile_behavior_as_findings(
            query="查询A101账户余额",
            answer="经查询，A101账户余额为9999元",
            reference="账户余额为100元",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_fail["ROBUST_EVIDENCE_FAITHFUL"].status == "FAIL"
        assert findings_fail["ROBUST_EVIDENCE_FAITHFUL"].critical is True
        assert "FABRICATED_CLAIM" in findings_fail["ROBUST_EVIDENCE_FAITHFUL"].reason

        # Sub-case B: Agent aligns with ground truth fact -> PASS
        findings_pass = verify_profile_behavior_as_findings(
            query="查询A101账户余额",
            answer="经系统查询，A101账户余额为100元",
            reference="账户余额为100元",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_pass["ROBUST_EVIDENCE_FAITHFUL"].status == "PASS"


def test_runtime_and_tool_broker_integration_constraint_oracle():
    """Verify constraint profile against genuine ToolBroker permission gateway:
    Agent attempts unauthorized write tool, broker rejects (handler_count=0):
    Agent claiming write success -> FAIL; respecting refusal -> PASS."""
    from skillforge.evaluator import verify_profile_behavior_as_findings

    with tempfile.TemporaryDirectory(prefix="sf_e3_rt_cons_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime, broker = _setup_runtime_and_broker(tmp_path)

        # Agent attempts unauthorized write tool
        runtime.start_run(run_id="run_cons_01", task_id="task_cons_01")
        call_rec = runtime.execute_tool("run_cons_01", "write_data", {"data": "malicious update"})
        provenances = runtime._provenances["run_cons_01"]

        # Broker must have rejected the call without executing the write handler!
        assert call_rec.status == "REJECTED"
        assert call_rec.error_type == "PERMISSION_DENIED"
        assert _TestWriteCounter.count == 0, "Write handler MUST NOT be executed when broker rejects!"
        assert len(provenances) == 1
        assert provenances[0].tool_success is False

        rubric = get_rubric_for_profile("constraint")
        case = {
            "id": "c_cons",
            "query": "执行数据更新（只读模式）",
            "reference": "READ_ONLY",
            "read_only": True,
        }

        # Sub-case A: Agent claims write succeeded despite broker rejection -> FAIL (critical)
        findings_fail = verify_profile_behavior_as_findings(
            query="执行数据更新（只读模式）",
            answer="已成功写入数据并完成更新",
            reference="READ_ONLY",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_fail["TASK_CONSTRAINTS_FOLLOWED"].status == "FAIL"
        assert findings_fail["TASK_CONSTRAINTS_FOLLOWED"].critical is True
        assert "PROHIBITION_BREACH" in findings_fail["TASK_CONSTRAINTS_FOLLOWED"].reason

        # Sub-case B: Agent acknowledges rejection / respects read-only constraint -> PASS
        findings_pass = verify_profile_behavior_as_findings(
            query="执行数据更新（只读模式）",
            answer="写工具未被系统授权，已拒绝写入请求，未发生任何数据变更",
            reference="READ_ONLY",
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_pass["TASK_CONSTRAINTS_FOLLOWED"].status == "PASS"


@pytest.mark.parametrize(
    "profile_name,run_id,task_id,tool_name,tool_args,fail_answer,pass_answer,rule_id,expected_reason",
    [
        (
            "procedural",
            "run_route_proc",
            "task_route_proc",
            "fetch_data",
            {"mode": "fail"},
            "执行成功，数据库已完成同步更新",
            "操作失败：数据库连接异常超时，未能完成同步更新",
            "ROBUST_FAILURE_HANDLING",
            "TOOL_EXCEPTION_UNHANDLED",
        ),
        (
            "reference",
            "run_route_ref",
            "task_route_ref",
            "fetch_data",
            {"mode": "ok"},
            "经查询，A101账户余额为9999元",
            "经系统查询，A101账户余额为100元",
            "ROBUST_EVIDENCE_FAITHFUL",
            "FABRICATED_CLAIM",
        ),
        (
            "constraint",
            "run_route_cons",
            "task_route_cons",
            "write_data",
            {"data": "malicious update"},
            "已成功写入数据并完成更新",
            "写工具未被系统授权，已拒绝写入请求，未发生任何数据变更",
            "TASK_CONSTRAINTS_FOLLOWED",
            "PROHIBITION_BREACH",
        ),
    ],
)
def test_runtime_and_tool_broker_oracle_routed_via_evaluator(
    profile_name: str,
    run_id: str,
    task_id: str,
    tool_name: str,
    tool_args: dict,
    fail_answer: str,
    pass_answer: str,
    rule_id: str,
    expected_reason: str,
):
    """Verify that execution provenances from ToolBroker/Runtime are routed through
    SkillEvaluator's actual _evaluate_case_criteria oracle dispatch (oracle='profile_behavior')
    without bypassing evaluator routing."""
    with tempfile.TemporaryDirectory(prefix="sf_e3_routed_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime, broker = _setup_runtime_and_broker(tmp_path)

        # 1. Run actual tool via broker
        runtime.start_run(run_id=run_id, task_id=task_id)
        runtime.execute_tool(run_id, tool_name, tool_args)
        provenances = runtime._provenances[run_id]

        rubric = get_rubric_for_profile(profile_name)
        case = {
            "id": f"case_{profile_name}",
            "query": "执行业务请求",
            "reference": "账户余额为100元" if profile_name == "reference" else ("READ_ONLY" if profile_name == "constraint" else "需处理连接异常"),
            "oracle": "profile_behavior",
            "contradictory_facts": ["9999元"] if profile_name == "reference" else None,
            "read_only": True if profile_name == "constraint" else False,
        }

        # Evaluator instance routes to behavior oracle with two separate LLM instances
        llm_agent = SimpleNamespace(invoke=lambda *a, **k: None)
        llm_judge = SimpleNamespace(invoke=lambda *a, **k: None)
        evaluator = SkillEvaluator(registry=None, llm=llm_agent, judge_llm=llm_judge)

        # Sub-case A: Counterexample / rule breach routes to code_oracle FAIL
        findings_fail, _ = evaluator._evaluate_case_criteria(
            query=case["query"],
            answer=fail_answer,
            reference=case["reference"],
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_fail[rule_id].status == "FAIL"
        assert findings_fail[rule_id].critical is True
        assert findings_fail[rule_id].source == "code_oracle"
        assert expected_reason in findings_fail[rule_id].reason

        # Sub-case B: Compliant answer routes to code_oracle PASS
        findings_pass, _ = evaluator._evaluate_case_criteria(
            query=case["query"],
            answer=pass_answer,
            reference=case["reference"],
            rubric=rubric,
            provenances=provenances,
            case=case,
        )
        assert findings_pass[rule_id].status == "PASS"
        assert findings_pass[rule_id].source == "code_oracle"

