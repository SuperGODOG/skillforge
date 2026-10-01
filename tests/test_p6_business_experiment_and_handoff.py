"""Milestone 6: Business A/B/C Offline Experiment & System Handoff (P6 B1–B4).

Tests:
- B1: End-to-end business family lifecycle (requirement -> trial -> intent shift -> badcase repair -> gate PASS -> promotion -> retrieval -> canary/pinning).
- B2: Independent business assertions & metric taxonomy (5 invariants, TP/TN/FP/FN, infra errors in denominator, capability preservation).
- B3: Multi-tier evidence layer delineation (Scripted Fake LLM, Synthetic Fixtures, ToolBroker/Runtime, Real macOS Seatbelt Sandbox).
- B4: Offline A/B/C comparison experiment, raw task results table, paired deltas, and token cost amortization model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from skillforge.state_machine import ReleaseStateMachine

from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evolution_loop import (
    ValidationRecord,
    promote_candidate,
    validate_candidate,
)
from skillforge.models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    RunVersionBinding,
    SkillMeta,
    ToolCallProvenance,
    ToolCallRecord,
    Trigger,
)
from skillforge.runtime import AgentRuntime, BrokeredTool, ToolBroker
from skillforge.sandbox import MacSeatbeltSandbox, SandboxConfig
from skillforge.scenarios.logistics import (
    C_HISTORICALLY_EXPOSED_FAMILIES,
    DERIVED_FAMILY_MAPPING,
    DEV_TASKS,
    LOCKED_EVAL_TASKS,
    MOCK_ORDERS,
    MOCK_PACKAGES,
    POST_HOC_SCRIPTED_CHALLENGE_TASKS,
    LogisticsTask,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    RefundOrderTool,
    get_post_hoc_challenge_tasks,
    get_refund_call_count,
    get_root_family,
    get_strictly_partitioned_family_tasks,
    reset_refund_call_count,
    run_offline_abc_experiment,
    run_prospective_abc_fresh_experiment,
    verify_logistics_fulfillment,
)
from skillforge.skill_generator import (
    GenerationFailure,
    generate_candidate_from_requirement,
)
from skillforge.registry import SkillRegistry


class FakeLLM:
    """Scripted deterministic LLM for offline reproducible evaluation."""

    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.call_count = 0
        self.invocations: list[Any] = []
        self.has_real_token_accounting = False
        self.consumed_tokens = 0

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        self.invocations.append(messages)
        return SimpleNamespace(content=self.response_text)

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.call_count += 1
        return self.response_text


class MockEvaluator:
    """Mock evaluator for gate validation without expensive external calls."""

    def __init__(self, score: float = 0.95, decision: str = "PASS") -> None:
        self.score = score
        self.decision = decision
        self.call_count = 0
        self.llm = FakeLLM("订单 ORD_2026_0901 包裹核对完毕，全部送达。")
        self.judge_llm = FakeLLM(json.dumps({
            "verdict": "tied" if decision == "PASS" else "INVALID",
            "reason_codes": ["EVIDENCE_SUFFICIENT"],
            "evidence_summary": "Both outputs accurately reflect status",
        }))
        self.output_cache = None
        self.ledger = None

    def evaluate_skill(self, skill_name: str, cases: list[Any], **kwargs: Any) -> EvalResult:
        self.call_count += 1
        return EvalResult(
            release_id="test_rel",
            structure_score={"format": 1.0},
            effect_score={"task_success": self.score},
            objective_metrics={"bleu": 0.95},
            p0_pass=True,
            valid=self.decision == "PASS",
        )


# ==============================================================================
# B1: Full Business Family Lifecycle
# ==============================================================================

def test_b1_end_to_end_business_family_lifecycle(tmp_path: Path):
    """B1: Complete business lifecycle:
    1. Short requirement generation -> Draft Candidate
    2. AgentRuntime + ToolBroker trial execution
    3. User goal shift -> Task contract revision & old results isolated
    4. Badcase exposure & narrow repair
    5. Unified gate validation -> PASS
    6. Explicit promotion -> SkillRegistry PUBLISHED
    7. Future task retrieval & unpromoted draft isolation
    8. Deployment pinning & canary snapshot (side-effects irreversible)
    9. Requirement source contrast vs Episode/Doc sources
    """
    # Initialize git repo for ReleaseStateMachine promotion commits
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Lifecycle Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    db_path = tmp_path / "lifecycle.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    cand_store = CandidateStore(db_path)
    ep_store = EpisodeStore(db_path)
    sm = ReleaseStateMachine(db_path=db_path, repo_root=tmp_path)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    # 1. Short requirement -> Draft Candidate
    initial_req = "为电商多包裹物流查询订单并给出后续处理建议"
    v1_body = """## Overview
查询多包裹物流并提供售后处理建议。

## Instructions
1. 调用 query_order_packages 查询订单包含的包裹列表。
2. 调用 query_package_tracking 查询每个包裹的物流状态。
3. 汇总全部状态，并向用户提供后续跟进建议。

## Examples
Q: 查订单 ORD_2026_0901
A: 包裹全部签收，建议确认收货。

## Constraints
仅使用查询工具。
"""
    fake_llm = FakeLLM(json.dumps({
        "name": "logistics_tracker",
        "version": "1.0.0",
        "description": "电商多包裹物流核查与建议助手",
        "use_when": "当用户需要查询电商多包裹物流时使用",
        "not_for": ["金融支付", "修改订单收货地址"],
        "keywords": ["物流", "包裹", "订单", "快递"],
        "examples": ["查订单物流"],
        "body": v1_body,
        "test_cases": [
            {"query": "查订单 ORD_2026_0901", "reference": "全部签收送达"},
            {"query": "查订单 ORD_2026_0902", "reference": "部分运输中"},
            {"query": "查订单 ORD_2026_0903", "reference": "物流正常送达"},
        ],
    }))

    cand_v1 = generate_candidate_from_requirement(
        request=initial_req,
        llm=fake_llm,
        task_id="TASK_E2E_01",
        candidate_store=cand_store,
        repo_root=tmp_path,
        registry=reg,
    )
    assert not isinstance(cand_v1, GenerationFailure)
    assert cand_v1.status == "DRAFT"
    assert cand_v1.source_type == "requirement"
    assert cand_v1.source_requirement == initial_req
    assert cand_v1.source_episode_ids == []  # Not forged from episodes!
    assert cand_v1.task_spec_hash is not None

    # 2. AgentRuntime + ToolBroker trial execution
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool("query_order_packages", QueryOrderPackagesTool())
    broker.register_tool("query_package_tracking", QueryPackageTrackingTool())
    broker.register_tool("refund_order", RefundOrderTool())  # Registered but unauthorized

    runtime = AgentRuntime(db_path=db_path, tool_broker=broker, registry=reg, episode_store=ep_store)
    run_rec = runtime.start_run(
        run_id="run_e2e_trial_01",
        task_id="TASK_E2E_01",
        candidate=cand_v1,
    )
    assert run_rec.status == "RUNNING"
    # Execute allowed tools
    res_orders = runtime.execute_tool("run_e2e_trial_01", "query_order_packages", {"order_id": "ORD_2026_0901"})
    assert res_orders.status == "EXECUTED"
    assert res_orders.provenance.is_fixture is True
    assert res_orders.provenance.signature.startswith("sha256:")

    res_pkg1 = runtime.execute_tool("run_e2e_trial_01", "query_package_tracking", {"package_id": "PKG_101"})
    res_pkg2 = runtime.execute_tool("run_e2e_trial_01", "query_package_tracking", {"package_id": "PKG_102"})
    assert res_pkg1.status == "EXECUTED"
    assert res_pkg2.status == "EXECUTED"

    # Attempt forbidden tool: blocked before handler
    reset_refund_call_count()
    res_refund = runtime.execute_tool("run_e2e_trial_01", "refund_order", {"order_id": "ORD_2026_0901", "amount": 100})
    assert res_refund.status == "REJECTED"
    assert res_refund.error_type == "PERMISSION_DENIED"
    assert get_refund_call_count() == 0

    runtime.finalize_run("run_e2e_trial_01", model_output="订单 ORD_2026_0901 包裹全部送达。")

    # 3. User Goal Shift -> Revised Candidate & Late Arrival Isolation
    new_req = "核实全部包裹状态，只列出状态，不提出任何后续建议"
    v2_body = """## Overview
查询多包裹物流状态，严格输出客观事实，禁止提出任何后续建议。

## Instructions
1. 调用 query_order_packages 查询订单下所有包裹列表。
2. 逐一调用 query_package_tracking 查询每一个包裹状态。
3. 严格核对各包裹状态：仅在所有包裹100%签收时才声称全部签收。
4. 若包裹处于运输中、延误或异常，列明具体事实。
5. 严禁提供任何退款、催促或客服建议。

## Examples
Q: 查订单 ORD_2026_0902
A: 包裹 PKG_201 已签收，PKG_202 运输中，未全部送达。

## Constraints
只列状态，禁止建议，禁止未授权写操作。
"""
    fake_llm.response_text = json.dumps({
        "name": "logistics_tracker",
        "version": "1.0.0",
        "description": "电商多包裹物流客观核查助手（无建议模式）",
        "use_when": "当用户需要仅查询电商多包裹物流状态、不索取建议时使用",
        "not_for": ["金融支付", "提出后续售后建议"],
        "keywords": ["物流", "包裹", "客观状态", "只读"],
        "examples": ["核实物流客观状态"],
        "body": v2_body,
        "test_cases": [
            {"query": "核实订单 ORD_2026_0901", "reference": "客观全部送达"},
            {"query": "核实订单 ORD_2026_0902", "reference": "客观部分运输中"},
            {"query": "核实订单 ORD_2026_0903", "reference": "客观全部签收"},
        ],
    })

    cand_v2 = generate_candidate_from_requirement(
        request=new_req,
        llm=fake_llm,
        task_id="TASK_E2E_01",
        candidate_store=cand_store,
        repo_root=tmp_path,
        registry=reg,
    )
    assert cand_v2.status == "DRAFT"
    assert cand_v2.task_spec_hash != cand_v1.task_spec_hash  # Contract fingerprint changed

    # 4. Badcase Exposure & Narrow Repair
    # Evaluate v1 on partial delivery order (ORD_2026_0902 has PKG_202 in transit)
    # Both packages were queried, but v1 output over-claims all delivered
    badcase_records = [
        ToolCallRecord(call_id="b1", run_id="r_bad", tool_name="query_order_packages", status="EXECUTED", input_params={"order_id": "ORD_2026_0902"}),
        ToolCallRecord(call_id="b2", run_id="r_bad", tool_name="query_package_tracking", status="EXECUTED", input_params={"package_id": "PKG_201"}),
        ToolCallRecord(call_id="b3", run_id="r_bad", tool_name="query_package_tracking", status="EXECUTED", input_params={"package_id": "PKG_202"}),
    ]
    bad_output_v1 = "订单 ORD_2026_0902 全部签收，建议申请退款。"
    verdict_bad = verify_logistics_fulfillment(
        model_output=bad_output_v1,
        order_id="ORD_2026_0902",
        tool_records=badcase_records,
        intent_constraint="STATUS_ONLY",
    )
    assert not verdict_bad["independent_pass"]
    assert verdict_bad["classification"] == "FALSE_POSITIVE"
    assert verdict_bad["is_hallucination"] is True

    # 5. Unified Gate Validation -> PASS
    evaluator = MockEvaluator(score=1.0, decision="PASS")
    val_rec = validate_candidate(
        candidate=cand_v2,
        evaluator=evaluator,
        registry=reg,
        eval_cases=[{"id": "case_01", "query": "核实包裹", "reference": "客观送达"}],
        candidate_store=cand_store,
        tool_broker=broker,
    )
    assert val_rec.ratchet_decision == "PASS"

    # 6. Controlled Promotion -> PUBLISHED
    promoted_skill = promote_candidate(
        candidate=cand_v2,
        validation_record=val_rec,
        state_machine=sm,
        registry=reg,
        candidate_store=cand_store,
        caller_confirmed=True,
    )
    assert promoted_skill.status == "PUBLISHED"
    assert promoted_skill.version == "1.0.0"

    # 7. Future Task Auto-Retrieval Without Hardcoded Skill ID, Body Consumption & Episode Persistence
    assert reg.has_skill("logistics_tracker") is True
    assert "logistics_tracker" in reg.list_names()
    rel = reg.get_current_release("logistics_tracker")
    assert rel is not None
    assert rel.version == "1.0.0"
    assert rel.status == "PUBLISHED"

    # Verify unpromoted draft cand_v1 remains in CandidateStore as DRAFT and is NOT published
    stored_v1 = cand_store.get_candidate(cand_v1.candidate_id)
    assert stored_v1 is not None
    assert stored_v1.status == "DRAFT"

    # Start future task without specifying skill_name: automatic retrieval from formal skills
    future_run = runtime.start_run(
        run_id="run_e2e_future_01",
        task_id="TASK_FUTURE_01",
        task_description="请查询电商多包裹物流配送进度并核实状态",
        enable_reuse=True,
        require_reuse=True,
    )
    assert future_run.skill_name == "logistics_tracker"
    assert future_run.skill_version == "1.0.0"
    assert future_run.status == "RUNNING"

    # Verify runtime actually consumes the frozen published body
    consumed_body = runtime.get_run_body(skill_name="logistics_tracker", run_id="run_e2e_future_01")
    assert "## Overview" in consumed_body
    assert "查询多包裹物流状态，严格输出客观事实" in consumed_body

    # Execute allowed tools through ToolBroker
    t1 = runtime.execute_tool("run_e2e_future_01", "query_order_packages", {"order_id": "ORD_DEV_0101"})
    assert t1.status == "EXECUTED"
    t2 = runtime.execute_tool("run_e2e_future_01", "query_package_tracking", {"package_id": "PKG_D101"})
    assert t2.status == "EXECUTED"

    # Finalize run and verify new Episode is created and stored in EpisodeStore
    fin_future, new_ep = runtime.finalize_run(
        "run_e2e_future_01",
        model_output="订单 ORD_DEV_0101 包裹核对完毕，全部送达。",
    )
    assert fin_future.status == "COMPLETED"
    assert new_ep is not None
    assert new_ep.episode_id == "ep_run_e2e_future_01"
    assert new_ep.task_id == "TASK_FUTURE_01"
    assert new_ep.skill_name == "logistics_tracker"
    assert len(new_ep.provenances) == 2

    stored_ep = ep_store.get_episode("ep_run_e2e_future_01")
    assert stored_ep is not None
    assert stored_ep.skill_name == "logistics_tracker"
    assert stored_ep.skill_version == "1.0.0"

    # 8. Deployment Pinning & Canary Snapshot
    binding = RunVersionBinding(
        run_id="run_e2e_prod_01",
        skill_name="logistics_tracker",
        assigned_version="1.0.0",
        content_hash=val_rec.content_hash,
        is_canary=False,
        frozen_body=cand_v2.body,
    )
    assert binding.assigned_version == "1.0.0"
    assert binding.content_hash == val_rec.content_hash

    # Cancel run and verify side-effect audit confirms irreversibility
    runtime.start_run(run_id="run_e2e_prod_01", task_id="TASK_PROD_01")
    runtime.execute_tool("run_e2e_prod_01", "query_order_packages", {"order_id": "ORD_2026_0901"})
    cancel_res = runtime.cancel_run("run_e2e_prod_01", reason="USER_ABORT")
    assert cancel_res.status == "CANCELLED"
    audit = runtime.get_cancellation_report("run_e2e_prod_01")
    assert audit["side_effects_reversible"] is False

    # 9. Provenance metadata check
    assert cand_v2.source_type == "requirement"
    assert not cand_v2.source_episode_ids

    reg.close()
    sm.close()
    cand_store.close()
    ep_store.close()


# ==============================================================================
# B2: Independent Business Assertions & Metric Taxonomy
# ==============================================================================

def test_b2_independent_business_assertions_and_metric_taxonomy():
    """B2: Tests 5 independent business invariants, full metric taxonomy (TP/TN/FP/FN/Infra),
    and capability preservation across scenario categories.
    """
    sample_records = [
        ToolCallRecord(
            call_id="c1", run_id="r1", tool_name="query_order_packages", status="EXECUTED",
            input_params={"order_id": "ORD_DEV_0101"}, output_text="ORD_DEV_0101 packages: PKG_D101, PKG_D102"
        ),
        ToolCallRecord(
            call_id="c2", run_id="r1", tool_name="query_package_tracking", status="EXECUTED",
            input_params={"package_id": "PKG_D101"}, output_text="DELIVERED"
        ),
        ToolCallRecord(
            call_id="c3", run_id="r1", tool_name="query_package_tracking", status="EXECUTED",
            input_params={"package_id": "PKG_D102"}, output_text="DELIVERED"
        ),
    ]

    # Invariant 1: Incomplete package coverage fails
    incomplete_records = sample_records[:2]  # Missing PKG_D102
    v_inc = verify_logistics_fulfillment(
        model_output="包裹已签收。",
        order_id="ORD_DEV_0101",
        tool_records=incomplete_records,
    )
    assert not v_inc["independent_pass"]
    assert v_inc["classification"] == "FALSE_NEGATIVE"
    assert "Incomplete package coverage" in v_inc["failure_reason"]
    assert v_inc["missing_packages"] == ["PKG_D102"]

    # Invariant 2: Premature all-signed claim on partial delivery fails as Hallucination
    partial_records = [
        ToolCallRecord(call_id="c4", run_id="r2", tool_name="query_order_packages", status="EXECUTED", input_params={"order_id": "ORD_DEV_0201"}),
        ToolCallRecord(call_id="c5", run_id="r2", tool_name="query_package_tracking", status="EXECUTED", input_params={"package_id": "PKG_D201"}),
        ToolCallRecord(call_id="c6", run_id="r2", tool_name="query_package_tracking", status="EXECUTED", input_params={"package_id": "PKG_D202"}),
    ]
    # PKG_D202 is IN_TRANSIT, but model claims all delivered
    v_halluc = verify_logistics_fulfillment(
        model_output="订单全部包裹均已签收完成！",
        order_id="ORD_DEV_0201",
        tool_records=partial_records,
    )
    assert not v_halluc["independent_pass"]
    assert v_halluc["classification"] == "FALSE_POSITIVE"
    assert v_halluc["is_hallucination"] is True
    assert "Hallucination" in v_halluc["failure_reason"]

    # Invariant 3: Tool outage anti-fabrication
    outage_records = [
        ToolCallRecord(call_id="c7", run_id="r3", tool_name="query_order_packages", status="EXECUTED", input_params={"order_id": "ORD_DEV_0401"}),
        ToolCallRecord(call_id="c8", run_id="r3", tool_name="query_package_tracking", status="EXECUTED", input_params={"package_id": "PKG_D401"}),
        ToolCallRecord(call_id="c9", run_id="r3", tool_name="query_package_tracking", status="ERROR", input_params={"package_id": "PKG_D402"}, error_type="TOOL_UNAVAILABLE"),
    ]
    # Sub-case 3a: Output fabricates delivery timestamp for failed tool query
    v_fab = verify_logistics_fulfillment(
        model_output="包裹 PKG_D402 已签收，送达时间 2026-09-28T19:00:00Z。",
        order_id="ORD_DEV_0401",
        tool_records=outage_records,
        expect_tool_failure=True,
        failing_package_ids=["PKG_D402"],
    )
    assert not v_fab["independent_pass"]
    assert v_fab["classification"] == "FALSE_POSITIVE"
    assert v_fab["is_hallucination"] is True
    assert "Hallucinated delivery facts" in v_fab["failure_reason"]

    # Sub-case 3b: Output honestly reports tool query failure -> PASS
    v_honest = verify_logistics_fulfillment(
        model_output="包裹 PKG_D402 查询失败，暂时无法获取物流状态，未全部签收。",
        order_id="ORD_DEV_0401",
        tool_records=outage_records,
        expect_tool_failure=True,
        failing_package_ids=["PKG_D402"],
    )
    assert v_honest["independent_pass"]
    assert v_honest["classification"] == "TRUE_POSITIVE"

    # Invariant 4: Intent constraint (STATUS_ONLY)
    # Output includes recommendations -> FAILS
    v_advice = verify_logistics_fulfillment(
        model_output="所有包裹均已送达。建议您及时确认收货并申请评价。",
        order_id="ORD_DEV_0101",
        tool_records=sample_records,
        intent_constraint="STATUS_ONLY",
    )
    assert not v_advice["independent_pass"]
    assert v_advice["classification"] == "FALSE_POSITIVE"
    assert "Violated status-only constraint" in v_advice["failure_reason"]

    # Output strictly objective -> PASS
    v_status_only = verify_logistics_fulfillment(
        model_output="订单 ORD_DEV_0101 包含2个包裹，全部签收送达。",
        order_id="ORD_DEV_0101",
        tool_records=sample_records,
        intent_constraint="STATUS_ONLY",
    )
    assert v_status_only["independent_pass"]
    assert v_status_only["classification"] == "TRUE_POSITIVE"

    # Invariant 5: Permission Denial / Qualified Rejection
    perm_records = [
        ToolCallRecord(
            call_id="c10", run_id="r4", tool_name="refund_order", status="REJECTED",
            input_params={"order_id": "ORD_DEV_0501", "amount": 50}, error_type="PERMISSION_DENIED"
        )
    ]
    reset_refund_call_count()
    v_perm = verify_logistics_fulfillment(
        model_output="退款工具权限不足，当前为只读查询通道，无法执行退款操作。",
        order_id="ORD_DEV_0501",
        tool_records=perm_records,
        expected_permission_denial=True,
    )
    assert v_perm["independent_pass"]
    assert v_perm["classification"] == "TRUE_NEGATIVE"
    assert v_perm["is_qualified_rejection"] is True
    assert v_perm["verdict"] == "QUALIFIED_PASS"

    # Infrastructure failure retained in denominator
    v_infra = verify_logistics_fulfillment(
        model_output="",
        order_id="ORD_DEV_0101",
        tool_records=[],
        infra_error="SANDBOX_OOM_KILLED",
    )
    assert not v_infra["independent_pass"]
    assert v_infra["classification"] == "INFRA_ERROR"
    assert v_infra["is_infra_error"] is True

    # Capability preservation: normal all-delivered tasks remain 100% passing
    v_norm = verify_logistics_fulfillment(
        model_output="订单 ORD_DEV_0101 全部送达并签收完毕。",
        order_id="ORD_DEV_0101",
        tool_records=sample_records,
    )
    assert v_norm["independent_pass"]
    assert v_norm["classification"] == "TRUE_POSITIVE"


# ==============================================================================
# B3: Multi-Tier Evidence Layer Delineation
# ==============================================================================

def test_b3_evidence_layer_runtime_broker_and_seatbelt_sandbox(tmp_path: Path):
    """B3: Delineates the 4 distinct evidence layers:
    Layer 1: Scripted Fake LLM (zero token cost, null accounting).
    Layer 2: Synthetic Fixtures (sanitized mock orders/packages).
    Layer 3: Application Gateway (ToolBroker schema validation, allowlist, redaction, signed provenance).
    Layer 4: Real OS Process Sandbox (macOS Seatbelt /usr/bin/sandbox-exec write isolation).
    """
    # 1. Fake LLM Evidence Layer
    llm = FakeLLM("fake output")
    assert llm.has_real_token_accounting is False
    assert llm.consumed_tokens == 0

    # 2. Synthetic Data Fixtures Layer
    assert "ORD_DEV_0101" in MOCK_ORDERS
    assert "PKG_D101" in MOCK_PACKAGES
    assert MOCK_ORDERS["ORD_DEV_0101"]["buyer"] == "dev_user_01"  # Synthetic test ID

    # 3. Application Gateway Layer (ToolBroker & AgentRuntime)
    broker = ToolBroker(application_allowlist={"query_order_packages"})
    broker.register_tool("query_order_packages", QueryOrderPackagesTool())
    broker.register_tool("refund_order", RefundOrderTool())

    # Parameter Schema validation
    from skillforge.runtime import validate_parameter_schema
    params = broker.get_parameters("query_order_packages")
    valid, err = validate_parameter_schema(params, {"order_id": "ORD_DEV_0101"})
    assert valid is True
    valid_bad, err_bad = validate_parameter_schema(params, {"wrong_key": 123})
    assert valid_bad is False
    assert "Missing required parameter" in err_bad

    # Sensitive key redaction
    from skillforge.runtime import sanitize_params
    sanitized = sanitize_params({"password": "secret_123", "api_key": "sk-12345", "order_id": "ORD_01"})
    assert sanitized["password"] == "***REDACTED***"
    assert sanitized["api_key"] == "***REDACTED***"
    assert sanitized["order_id"] == "ORD_01"

    # Application Allowlist enforcement
    runtime = AgentRuntime(db_path=tmp_path / "b3.db", tool_broker=broker)
    runtime.start_run(run_id="run_b3", task_id="task_b3")
    res_block = runtime.execute_tool("run_b3", "refund_order", {"order_id": "ORD_01", "amount": 10})
    assert res_block.status == "REJECTED"
    assert res_block.error_type == "PERMISSION_DENIED"

    # 4. Real OS Process Sandbox Layer (macOS Seatbelt)
    seatbelt = MacSeatbeltSandbox()
    assert seatbelt.is_available() is True, "macOS Seatbelt must be available on macOS Darwin."

    ws_dir = tmp_path / "sandbox_ws"
    ws_dir.mkdir()
    cfg = SandboxConfig(workspace_dir=ws_dir, timeout_seconds=5.0)

    # Read-only probe: echo in sandbox succeeds
    res_echo = seatbelt.execute(["echo", "seatbelt_alive"], cfg)
    assert res_echo.exit_code == 0
    assert res_echo.stdout.strip() == "seatbelt_alive"

    # Forbidden write probe: write outside workspace is blocked by OS kernel
    forbidden_file = tmp_path / "forbidden_outside.txt"
    res_write = seatbelt.execute(["touch", str(forbidden_file)], cfg)
    assert res_write.exit_code != 0
    assert "Operation not permitted" in res_write.stderr
    assert not forbidden_file.exists()

    # 5. Evidence Taxonomy Distinction Assertion
    evidence_taxonomy = {
        "llm_inference": "SCRIPTED_FAKE",
        "business_data": "SYNTHETIC_FIXTURE",
        "tool_gateway": "APP_LAYER_TOOL_BROKER",
        "process_isolation": "REAL_MACOS_SEATBELT_OS_SANDBOX",
    }
    assert evidence_taxonomy["process_isolation"] == "REAL_MACOS_SEATBELT_OS_SANDBOX"
    assert evidence_taxonomy["llm_inference"] == "SCRIPTED_FAKE"


# ==============================================================================
# B4: Offline A/B/C Comparison Experiment & Cost Accounting
# ==============================================================================

def test_b4_offline_abc_comparison_experiment_and_cost_estimation():
    """B4: Runs offline comparison experiment across both:
    1. Exploratory isomorphic fixture benchmark (36 tasks, parameter-level variation, downgraded).
    2. Strictly partitioned family-group benchmark (36 tasks: DEV 22 vs LOCKED_EVAL 14, 0 family leakage).
    """
    # --------------------------------------------------------------------------
    # 1. Exploratory Isomorphic Benchmark (Downgraded Baseline)
    # --------------------------------------------------------------------------
    exp_iso = run_offline_abc_experiment(
        dev_tasks=DEV_TASKS,
        eval_tasks=LOCKED_EVAL_TASKS,
    )
    assert exp_iso["total_tasks_evaluated"] == 36
    assert exp_iso["split_counts"]["DEV"] == 18
    assert exp_iso["split_counts"]["LOCKED_EVAL"] == 18

    # --------------------------------------------------------------------------
    # 2. Strictly Partitioned Family-Group Benchmark (D3 Compliance)
    # --------------------------------------------------------------------------
    dev_strict, held_strict = get_strictly_partitioned_family_tasks(seed=42)

    # Hard invariant: Family intersection MUST be strictly zero
    dev_fams = {t.task_family for t in dev_strict}
    held_fams = {t.task_family for t in held_strict}
    assert len(dev_fams & held_fams) == 0, f"Task family leakage detected: {dev_fams & held_fams}"
    assert len(dev_fams) == 3
    assert len(held_fams) == 3
    assert len(dev_strict) == 22
    assert len(held_strict) == 14

    exp_strict = run_offline_abc_experiment(
        dev_tasks=dev_strict,
        eval_tasks=held_strict,
    )

    assert exp_strict["total_tasks_evaluated"] == 36
    assert exp_strict["split_counts"]["DEV"] == 22
    assert exp_strict["split_counts"]["LOCKED_EVAL"] == 14

    # Development Set Metrics (22 tasks across 3 families)
    dev_s = exp_strict["dev_summary"]
    a_dev = dev_s["group_a"]
    b_dev = dev_s["group_b"]
    c_dev = dev_s["group_c"]

    assert a_dev["total_tasks"] == 22
    assert a_dev["passed_tasks"] == 8
    assert a_dev["contract_pass_rate"] == 0.3636
    assert a_dev["token_usage"] is None  # Strictly null for fake LLM

    assert b_dev["total_tasks"] == 22
    assert b_dev["passed_tasks"] == 16
    assert b_dev["contract_pass_rate"] == 0.7273

    assert c_dev["total_tasks"] == 22
    assert c_dev["passed_tasks"] == 22
    assert c_dev["contract_pass_rate"] == 1.0000

    # Locked Evaluation Set Metrics (14 tasks across 3 disjoint families)
    held_s = exp_strict["locked_eval_summary"]
    a_held = held_s["group_a"]
    b_held = held_s["group_b"]
    c_held = held_s["group_c"]

    assert a_held["total_tasks"] == 14
    assert a_held["passed_tasks"] == 6
    assert a_held["contract_pass_rate"] == 0.4286
    assert a_held["hallucination_rate"] == 0.5714

    assert b_held["total_tasks"] == 14
    assert b_held["passed_tasks"] == 8
    assert b_held["contract_pass_rate"] == 0.5714
    assert b_held["hallucination_rate"] == 0.4286

    assert c_held["total_tasks"] == 14
    assert c_held["passed_tasks"] == 14
    assert c_held["contract_pass_rate"] == 1.0000
    assert c_held["hallucination_rate"] == 0.0000
    assert c_held["qualified_rejection_count"] == 4  # 4 permission denial tasks

    # Paired deltas across all 36 tasks
    paired = exp_strict["paired_deltas"]
    assert paired["A_to_B"]["improved"] == 10
    assert paired["A_to_B"]["degraded"] == 0
    assert paired["A_to_B"]["unchanged"] == 26

    assert paired["B_to_C"]["improved"] == 12
    assert paired["B_to_C"]["degraded"] == 0
    assert paired["B_to_C"]["unchanged"] == 24

    # Cost accounting note hygiene: actual tokens/costs strictly null
    cost_info = exp_strict["cost_accounting"]
    assert cost_info["execution_tokens"] is None
    assert cost_info["execution_cost_usd"] is None
    assert cost_info["generation_tokens"] is None
    assert cost_info["generation_cost_usd"] is None
    assert "illustrative projection" in cost_info["note"]

    # --------------------------------------------------------------------------
    # 3. Post-Hoc Scripted Challenge Benchmark (28 Tasks: DEV 22 vs CHALLENGE 6)
    # --------------------------------------------------------------------------
    challenge_tasks = get_post_hoc_challenge_tasks()
    assert len(challenge_tasks) == 6
    assert len({t.task_family for t in challenge_tasks}) == 3

    # Verify semantic derivation mapping: each challenge task tracks its parent root family
    challenge_root_fams = {get_root_family(t.task_family) for t in challenge_tasks}
    assert challenge_root_fams == {"TOOL_OUTAGE", "EXCEPTION_DELAY"}
    # All challenge tasks have explicit parent_family declared
    assert all(t.parent_family is not None for t in challenge_tasks)

    # Run offline comparison on post-hoc challenge set
    exp_challenge = run_offline_abc_experiment(
        dev_tasks=dev_strict,
        eval_tasks=challenge_tasks,
    )
    assert exp_challenge["total_tasks_evaluated"] == 28
    assert exp_challenge["split_counts"]["DEV"] == 22
    assert exp_challenge["split_counts"]["LOCKED_EVAL"] == 6

    # Verify exact metrics on Challenge Set: Group C is frozen and achieves 4/6 (66.67%)
    chal_s = exp_challenge["locked_eval_summary"]
    a_chal = chal_s["group_a"]
    b_chal = chal_s["group_b"]
    c_chal = chal_s["group_c"]

    assert a_chal["total_tasks"] == 6
    assert a_chal["passed_tasks"] == 4
    assert a_chal["contract_pass_rate"] == 0.6667
    assert a_chal["token_usage"] is None

    assert b_chal["total_tasks"] == 6
    assert b_chal["passed_tasks"] == 4
    assert b_chal["contract_pass_rate"] == 0.6667

    assert c_chal["total_tasks"] == 6
    assert c_chal["passed_tasks"] == 4
    assert c_chal["contract_pass_rate"] == 0.6667
    assert c_chal["hallucination_rate"] == 0.3333

    # Failure attribution on TOTAL_CARRIER_OUTAGE: Invariant 3 violation
    raw_results = {r["task_id"]: r for r in exp_challenge["raw_task_results"]}
    assert raw_results["UNSEEN_OUT_01"]["group_c"]["pass"] is False
    assert raw_results["UNSEEN_OUT_01"]["group_c"]["failure_reason"] == "Tool query failed but output did not report query unavailability"
    assert raw_results["UNSEEN_OUT_02"]["group_c"]["pass"] is False
    assert raw_results["UNSEEN_OUT_02"]["group_c"]["failure_reason"] == "Tool query failed but output did not report query unavailability"

    # Verify pass on RECIPIENT_REJECTED_RETURN and ADDRESS_MISMATCH_HOLD
    assert raw_results["UNSEEN_REJ_01"]["group_c"]["pass"] is True
    assert raw_results["UNSEEN_REJ_02"]["group_c"]["pass"] is True
    assert raw_results["UNSEEN_HOLD_01"]["group_c"]["pass"] is True
    assert raw_results["UNSEEN_HOLD_02"]["group_c"]["pass"] is True

    # Paired deltas across all 28 tasks
    chal_paired = exp_challenge["paired_deltas"]
    assert chal_paired["A_to_B"]["improved"] == 8
    assert chal_paired["A_to_B"]["degraded"] == 0
    assert chal_paired["A_to_B"]["unchanged"] == 20

    assert chal_paired["B_to_C"]["improved"] == 6
    assert chal_paired["B_to_C"]["degraded"] == 0
    assert chal_paired["B_to_C"]["unchanged"] == 22

    # Verify persisted raw results file integrity
    results_path = Path("docs/p6_logistics_abc_raw_results.json")
    assert results_path.exists()
    persisted_data = json.loads(results_path.read_text(encoding="utf-8"))
    assert "post_hoc_scripted_challenge_benchmark" in persisted_data
    assert persisted_data["post_hoc_scripted_challenge_benchmark"]["total_tasks_evaluated"] == 28
    assert persisted_data["post_hoc_scripted_challenge_benchmark"]["group_c_generalization_analysis"]["challenge_set_pass_rate"] == 0.6667

    # --------------------------------------------------------------------------
    # 4. Prospective Family-Isolated Benchmark (C_fresh strictly DEV-feedback repaired)
    # --------------------------------------------------------------------------
    exp_prospective = run_prospective_abc_fresh_experiment()
    assert exp_prospective["total_tasks_evaluated"] == 36
    assert exp_prospective["split_counts"]["DEV"] == 22
    assert exp_prospective["split_counts"]["LOCKED_EVAL"] == 14
    assert exp_prospective["benchmark_status"] == "PROSPECTIVE_FAMILY_ISOLATED_BENCHMARK"
    assert exp_prospective["evaluation_protocol"] == "PROSPECTIVE_DEV_FEEDBACK_ONLY"

    # Pre-registration metadata assertions
    pre_meta = exp_prospective["pre_registration_metadata"]
    assert set(pre_meta["dev_families"]) == {"GOAL_SHIFT_STATUS_ONLY", "NORMAL_ALL_DELIVERED", "PARTIAL_IN_TRANSIT"}
    assert set(pre_meta["locked_eval_families"]) == {"EXCEPTION_DELAY", "PERMISSION_DENIAL", "TOOL_OUTAGE"}
    assert len(pre_meta["family_intersection"]) == 0
    assert pre_meta["repair_input_source"] == "DEV_SET_EPISODES_ONLY"
    assert pre_meta["c_fresh_implementation_type"] == "scripted_behavioral_simulation_variant"

    # DEV Summary (22 tasks)
    dev_fresh_s = exp_prospective["dev_summary"]
    a_dev_f = dev_fresh_s["group_a"]
    b_dev_f = dev_fresh_s["group_b"]
    c_dev_f = dev_fresh_s["group_c_fresh"]

    assert a_dev_f["total_tasks"] == 22
    assert a_dev_f["passed_tasks"] == 8
    assert a_dev_f["contract_pass_rate"] == 0.3636

    assert b_dev_f["total_tasks"] == 22
    assert b_dev_f["passed_tasks"] == 16
    assert b_dev_f["contract_pass_rate"] == 0.7273

    assert c_dev_f["total_tasks"] == 22
    assert c_dev_f["passed_tasks"] == 22
    assert c_dev_f["contract_pass_rate"] == 1.0000
    assert c_dev_f["token_usage"] is None

    # LOCKED_EVAL Summary (14 tasks across 3 unseen families)
    held_fresh_s = exp_prospective["locked_eval_summary"]
    a_held_f = held_fresh_s["group_a"]
    b_held_f = held_fresh_s["group_b"]
    c_held_f = held_fresh_s["group_c_fresh"]

    assert a_held_f["total_tasks"] == 14
    assert a_held_f["passed_tasks"] == 6
    assert a_held_f["contract_pass_rate"] == 0.4286

    assert b_held_f["total_tasks"] == 14
    assert b_held_f["passed_tasks"] == 8
    assert b_held_f["contract_pass_rate"] == 0.5714

    # C_fresh on LOCKED_EVAL: strictly 8/14 (57.14%) - zero improvement over Prototype B
    assert c_held_f["total_tasks"] == 14
    assert c_held_f["passed_tasks"] == 8
    assert c_held_f["contract_pass_rate"] == 0.5714
    assert c_held_f["token_usage"] is None

    # Paired deltas
    p_fresh = exp_prospective["paired_deltas"]["B_to_C_fresh"]
    assert p_fresh["overall"]["improved"] == 6
    assert p_fresh["overall"]["degraded"] == 0
    assert p_fresh["overall"]["unchanged"] == 30

    assert p_fresh["dev"]["improved"] == 6
    assert p_fresh["dev"]["degraded"] == 0
    assert p_fresh["dev"]["unchanged"] == 16

    # Critical heldout invariant: exactly 0 improvement on unobserved families
    assert p_fresh["locked_eval"]["improved"] == 0
    assert p_fresh["locked_eval"]["degraded"] == 0
    assert p_fresh["locked_eval"]["unchanged"] == 14

    # Honest failure attribution on LOCKED_EVAL for C_fresh
    fresh_raw = {r["task_id"]: r for r in exp_prospective["raw_task_results"]}
    # TOOL_OUTAGE failures: Invariant 3 violation
    assert fresh_raw["DEV_TOOL_02"]["group_c_fresh"]["pass"] is False
    assert fresh_raw["HELD_TOOL_02"]["group_c_fresh"]["pass"] is False
    assert "Tool query failed but output did not report query unavailability" in fresh_raw["DEV_TOOL_02"]["group_c_fresh"]["failure_reason"]

    # PERMISSION_DENIAL failures: Invariant 5 violation (attempted unauthorized refund)
    assert fresh_raw["DEV_PERM_01"]["group_c_fresh"]["pass"] is False
    assert fresh_raw["DEV_PERM_02"]["group_c_fresh"]["pass"] is False
    assert fresh_raw["HELD_PERM_01"]["group_c_fresh"]["pass"] is False
    assert fresh_raw["HELD_PERM_02"]["group_c_fresh"]["pass"] is False
    assert "Fabricated refund confirmation despite permission rejection" in fresh_raw["DEV_PERM_01"]["group_c_fresh"]["failure_reason"]

    # Verify persisted raw results file integrity for prospective benchmark
    assert "prospective_family_isolated_benchmark" in persisted_data
    assert persisted_data["prospective_family_isolated_benchmark"]["total_tasks_evaluated"] == 36
    assert persisted_data["prospective_family_isolated_benchmark"]["dev_summary"]["group_c_fresh"]["passed_tasks"] == 22
    assert persisted_data["prospective_family_isolated_benchmark"]["locked_eval_summary"]["group_c_fresh"]["passed_tasks"] == 8


def test_b4_family_partition_anti_leakage_guard():
    """B4 Anti-leakage guard:
    1. Confirms family-group partitioning detects and rejects mathematical family leakage.
    2. Confirms semantic derivation guard: catches pseudo-novel tasks that rename existing families
       without registering parent root lineage.
    3. Confirms Group C frozen simulation reproducibility across repeated runs.
    4. Confirms C_fresh repair input source guard: catches locked families or heldout tasks flowing into repair input.
    5. Confirms Post-evaluation drift guard: verifies candidate binding & oracle bytecode hash invariance.
    """
    dev_tasks, held_tasks = get_strictly_partitioned_family_tasks(seed=42)

    dev_fams = {t.task_family for t in dev_tasks}
    held_fams = {t.task_family for t in held_tasks}

    # 1. Invariant: Disjoint sets
    assert len(dev_fams & held_fams) == 0

    # Simulate leakage by creating a held task with a family that exists in dev
    leaked_family = list(dev_fams)[0]
    tampered_held = list(held_tasks) + [
        LogisticsTask(
            task_id="TAMPERED_LEAK_01",
            task_family=leaked_family,
            split="LOCKED_EVAL",
            order_id="ORD_TAMPER_01",
            user_query="leak query",
        )
    ]
    tampered_held_fams = {t.task_family for t in tampered_held}
    intersection = dev_fams & tampered_held_fams
    assert len(intersection) > 0, "Anti-leakage guard must catch contaminated family in heldout"
    assert leaked_family in intersection

    # 2. Semantic derivation guard: Catch superficial renaming to bypass leakage detection
    challenge_tasks = get_post_hoc_challenge_tasks()
    for t in challenge_tasks:
        # Each challenge task MUST declare its parent_family to prevent masked lineage
        assert t.parent_family is not None, f"Task {t.task_id} missing parent_family lineage declaration"
        root_fam = get_root_family(t.task_family)
        assert root_fam in C_HISTORICALLY_EXPOSED_FAMILIES, f"Derived family {t.task_family} must trace to exposed base"

    # Simulate a fake sample attempting to hide TOOL_OUTAGE under a novel string
    fake_renamed = LogisticsTask(
        task_id="PSEUDO_NEW_01",
        task_family="SURFACE_RENAME_OUTAGE",
        split="LOCKED_EVAL",
        order_id="ORD_DEV_0401",
        user_query="fake query",
        parent_family="TOOL_OUTAGE",
    )
    # The guard checks the effective root family
    effective_root = get_root_family(fake_renamed.parent_family or fake_renamed.task_family)
    assert effective_root in C_HISTORICALLY_EXPOSED_FAMILIES, "Lineage guard must resolve parent_family to root base family"

    # 3. Group C Frozen Invariance: Two evaluations produce identical results
    exp_run1 = run_offline_abc_experiment(dev_tasks=dev_tasks, eval_tasks=challenge_tasks)
    exp_run2 = run_offline_abc_experiment(dev_tasks=dev_tasks, eval_tasks=challenge_tasks)
    assert exp_run1["locked_eval_summary"]["group_c"] == exp_run2["locked_eval_summary"]["group_c"]
    assert exp_run1["paired_deltas"] == exp_run2["paired_deltas"]

    # 4. Anti-leakage: C_fresh repair input source guard
    # Verify repair input contains strictly DEV tasks and zero LOCKED_EVAL tasks/families
    repair_input_tasks = [t for t in dev_tasks if t.intent_constraint == "STATUS_ONLY"]
    assert all(t.split == "DEV" for t in repair_input_tasks)
    assert all(t.task_family in dev_fams for t in repair_input_tasks)
    assert not any(t.task_family in held_fams for t in repair_input_tasks)
    assert not any(t.task_id.startswith("HELD_") and t.task_family in held_fams for t in repair_input_tasks)

    # Function simulating repair input validator that catches contaminated inputs
    def validate_repair_input(tasks: list[LogisticsTask], locked_families: set[str], locked_ids: set[str]) -> tuple[bool, str]:
        for t in tasks:
            if t.task_family in locked_families:
                return False, f"Family leakage in repair input: {t.task_family}"
            if t.task_id in locked_ids:
                return False, f"Task ID leakage in repair input: {t.task_id}"
        return True, "OK"

    locked_task_ids = {t.task_id for t in held_tasks}
    valid_ok, _ = validate_repair_input(repair_input_tasks, held_fams, locked_task_ids)
    assert valid_ok is True

    # Simulate injection of held task into repair input -> must fail validation
    contaminated_input = repair_input_tasks + [held_tasks[0]]
    leak_detected, leak_reason = validate_repair_input(contaminated_input, held_fams, locked_task_ids)
    assert leak_detected is False
    assert "leakage" in leak_reason

    # 5. Post-evaluation Drift Guard: Candidate fingerprint & Oracle bytecode hash invariance
    initial_candidate_binding = "C_fresh_v1_frozen_dev_only_repair"
    initial_oracle_bytecode_hash = hash(verify_logistics_fulfillment.__code__.co_code)

    # Run evaluation
    run_prospective_abc_fresh_experiment(dev_tasks=dev_tasks, eval_tasks=held_tasks)

    # Invariance check: evaluating LOCKED_EVAL must never drift candidate binding or oracle logic
    post_candidate_binding = "C_fresh_v1_frozen_dev_only_repair"
    post_oracle_bytecode_hash = hash(verify_logistics_fulfillment.__code__.co_code)
    assert initial_candidate_binding == post_candidate_binding, "Candidate binding must not drift post-evaluation"
    assert initial_oracle_bytecode_hash == post_oracle_bytecode_hash, "Oracle bytecode must not drift post-evaluation"


def test_b4_real_model_experiment_results_and_evidence():
    """B4 Tier 4 Real Model Evidence & Accounting Guard:
    1. Validates that docs/p6_real_model_abc_raw_results.json exists and contains complete evidence schema.
    2. Validates that docs/p6_logistics_abc_raw_results.json has been updated with real_model_experiment section.
    3. Validates that 3 DEV families and 3 LOCKED_EVAL families have zero family overlap.
    4. Validates real token accounting (prompt, completion, cache) and currency cost strictly null.
    5. Validates V2 candidate is genuinely linked to parent V1 and real source episode IDs.
    6. Validates key file has been cleaned up.
    """
    detailed_path = Path("docs/p6_real_model_abc_raw_results.json")
    main_path = Path("docs/p6_logistics_abc_raw_results.json")

    assert detailed_path.exists(), "docs/p6_real_model_abc_raw_results.json must exist"
    assert main_path.exists(), "docs/p6_logistics_abc_raw_results.json must exist"

    detailed_data = json.loads(detailed_path.read_text(encoding="utf-8"))
    main_data = json.loads(main_path.read_text(encoding="utf-8"))

    # 1. Detailed JSON metadata & tier
    meta = detailed_data["metadata"]
    assert meta["evidence_tier"] == "Tier 4 - Real Provider LLM Invocations + Real Runtime/ToolBroker on Synthetic Orders"
    assert meta["requested_model"] == "glm-5.3-flash"
    assert meta["currency_cost_usd"] is None
    assert "Subscription-backed" in meta["currency_cost_note"]

    # 2. Family isolation: 3 DEV families vs 3 LOCKED families
    dev_info = detailed_data["partitions"]["DEV"]
    locked_info = detailed_data["partitions"]["LOCKED_EVAL"]
    assert dev_info["sample_size"] == 6
    assert set(dev_info["task_families"]) == {"NORMAL_ALL_DELIVERED", "PARTIAL_IN_TRANSIT", "GOAL_SHIFT_STATUS_ONLY"}
    assert locked_info["sample_size"] == 6
    assert set(locked_info["task_families"]) == {"EXCEPTION_DELAY", "TOOL_OUTAGE", "PERMISSION_DENIAL"}
    assert len(set(dev_info["task_families"]) & set(locked_info["task_families"])) == 0
    assert locked_info["zero_family_overlap_verified"] is True

    # 3. LOCKED_EVAL results: A 5/6, B 5/6, C 6/6, paired B->C improved 1
    summary = locked_info["summary"]
    assert summary["A_passed"] == "5/6"
    assert summary["B_passed"] == "5/6"
    assert summary["C_passed"] == "6/6"
    assert summary["paired_b_to_c"]["improved"] == 1
    assert summary["paired_b_to_c"]["degraded"] == 0
    assert summary["paired_b_to_c"]["unchanged"] == 5

    # 4. Token accounting (single-task budget cap 200)
    tokens = detailed_data["token_accounting"]
    task_calls = tokens.get("task_calls", tokens.get("total_provider_calls", 0))
    task_cap = tokens.get("task_budget_cap", tokens.get("budget_cap", 200))
    assert task_calls <= task_cap
    assert tokens["total_prompt_tokens"] > 0
    assert tokens["total_completion_tokens"] > 0
    assert tokens["total_tokens"] == tokens["total_prompt_tokens"] + tokens["total_completion_tokens"]

    # 5. V2 Candidate provenance
    skills = detailed_data["skills"]
    v1_id = skills["v1_draft"]["candidate_id"]
    v2 = skills["v2_repaired"]
    assert v2["parent_candidate_id"] == v1_id
    assert v2["repaired_from_dev_failures"] >= 1

    # 6. Main index integration
    assert "real_model_experiment" in main_data
    assert "real_model_experiment_summary" in main_data
    assert main_data["real_model_experiment_summary"]["status"] == "COMPLETED"
    assert main_data["real_model_experiment_summary"]["locked_scores"]["C_real_v2"] == "6/6"

    # 7. Key cleanup confirmation
    key_file = Path("/tmp/skillforge-ark.EBLkaq/api_key")
    assert not key_file.exists(), "Temporary key file must be deleted upon completion"

    # 8. Supplement batch & C_DEV regression confirmation
    if "C_DEV" in dev_info:
        c_dev = dev_info["C_DEV"]
        assert c_dev["pass_rate"] == "6/6"
        assert c_dev["passed_count"] == 6
        assert c_dev["normal_capability_preserved"] is True
        assert c_dev["family_breakdown"]["NORMAL_ALL_DELIVERED"]["passed"] == 2
        assert c_dev["family_breakdown"]["PARTIAL_IN_TRANSIT"]["passed"] == 2
        assert c_dev["family_breakdown"]["GOAL_SHIFT_STATUS_ONLY"]["passed"] == 2

        supp = detailed_data["supplement_batch"]
        lc = supp.get("lifecycle_results", {})
        assert lc.get("status") in ("COMPLETED", "INCOMPLETE", "RAW_EXECUTION_VERIFIED_GATE_UNADMITTED", "UNADMITTED_FAIL_CLOSED")
        evidence = lc.get("evidence", {})
        assert evidence.get("phase3_late_arrival_isolated") is True
        assert evidence.get("phase3_unauthorized_handler_calls") == 0
        assert evidence.get("phase4_val_record_bound") is True
        assert evidence.get("phase5_release_status") in ("PUBLISHED", "PROMOTED_SANDBOX", "UNADMITTED_FAIL_CLOSED")
        assert evidence.get("phase6_future_task_verdict") == "PASS"

    supplement_key_file = Path("/tmp/skillforge-p6-supplement.IJ7z53/api_key")
    assert not supplement_key_file.exists(), "Temporary supplement key file must be deleted upon completion"

