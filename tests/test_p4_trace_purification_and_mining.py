"""P4 Acceptance Test Suite: Trace Purification and Scope-Aware Mining (D1–D5).

Verifies Acceptance Criteria D1–D5:
- D1: Business failure attributable to Skill produces structured, reproducible test case
      proposals with independent expectation, contract version, and tool snapshots;
      missing expectation or self-reinforcing model failure output is strictly rejected/routed.
- D2: Representative normal/success forms regression cases; unknown, infra_error, and
      policy denials are diverted without triggering skill evolution; legitimate tool denials
      verified by oracle pass as acceptable policy compliance.
- D3: Grouped partition by task family & intent revision prevents heldout leakage;
      anti-dilution duplicate detection blocks inflating the denominator; persistence survives DB reopen.
- D4: Scope-aware clustering groups FIRST by business scope, intent revision, and tool contracts;
      similar wording with different scopes/tools are never falsely merged; sub-scenario failures
      cannot be masked by majority success; support count enforces independent task IDs.
- D5: Purpose isolation penetrates low-level entry points (generator, repair, purifier);
      heldout cases cannot enter dev prompts without explicit demotion invalidating benchmark numbers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.data_partition import (
    group_and_partition_cases,
    validate_repair_set_composition,
)
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.models import (
    CandidateSkill,
    Episode,
    SkillMeta,
    TaskContext,
    TestCaseProposal,
    ToolCallProvenance,
)
from skillforge.pattern_mining import (
    ClusterReport,
    MiningBatchReport,
    PatternMiningConfig,
    extract_scope_key,
    mine_pending,
)
from skillforge.registry import SkillRegistry
from skillforge.repair import repair_skill_failure
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_ORDERS,
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    RefundOrderTool,
    verify_logistics_fulfillment,
)
from skillforge.skill_generator import generate_candidate_from_requirement
from skillforge.trace_purification import (
    demote_heldout_to_dev,
    purify_trace_to_proposal,
)


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

class FakePatcherLLM:
    """Deterministic Fake LLM for repair and generation testing."""

    def __init__(self, patched_body: str = "Patched skill body"):
        self.patched_body = patched_body
        self.invocations: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.invocations.append(prompt)
        class _Resp:
            def __init__(self, content: str):
                self.content = content
        yaml_content = f"""---
name: logistics_skill
version: 1.0.1
description: Patched logistics query skill
use_when: Query order logistics
---
{self.patched_body}"""
        return _Resp(yaml_content)


def _make_provenance(
    tool_name: str,
    output_status: str = "SUCCESS",
    input_params: Optional[dict[str, Any]] = None,
    output_summary: str = "OK",
) -> ToolCallProvenance:
    return ToolCallProvenance(
        tool_name=tool_name,
        fixture_case_id="logistics_fix_01",
        call_index=1,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=(output_status == "SUCCESS"),
        authenticity_pass=True,
        input_params=input_params or {},
        output_status=output_status,
        output_summary=output_summary,
        latency_ms=12.5,
        timestamp="2026-09-30T10:00:00Z",
        signature="sig_test_prov",
        snapshot_id="snap_101",
        snapshot_content=json.dumps({"status": output_status}),
    )


# ---------------------------------------------------------------------------
# D1 Tests: Business Failure Purification & Expectation Safeguards
# ---------------------------------------------------------------------------

def test_d1_purify_trace_to_proposal_with_business_expectation_and_rejection_rules(tmp_path: Path):
    """D1: Business failure produces structured, reproducible test case proposal;
    missing expectations remain PENDING; model failed answer as expectation is strictly rejected.
    """
    db_path = tmp_path / "skillforge.db"
    store = CandidateStore(db_path)

    # 1. Create a business failure episode in the multi-package logistics scenario
    # ORD_2026_0902 has PKG_201 DELIVERED, PKG_202 IN_TRANSIT.
    # Model hallucinated that all packages were delivered.
    model_failed_answer = "订单 ORD_2026_0902 中所有包裹均已送达签收，谢谢！"
    ep_fail = Episode(
        episode_id="ep_logistics_fail_01",
        task_id="task_ord_0902_status",
        run_id="run_0902_fail",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={
            "query": "查询订单 ORD_2026_0902 的物流进展并核实是否全部送达",
            "business_scope": "logistics_readonly",
            "intent_revision": 1,
            "contract_fingerprint": "fp_ord_0902_rev1",
            "purpose": "learning",
        },
        provenances=[
            _make_provenance("query_order_packages", input_params={"order_id": "ORD_2026_0902"}),
            _make_provenance("query_package_tracking", input_params={"package_id": "PKG_201"}),
            _make_provenance("query_package_tracking", input_params={"package_id": "PKG_202"}),
        ],
        acceptance_criteria={
            "query": "查询订单 ORD_2026_0902 的物流进展并核实是否全部送达",
            "expected_all_delivered": False,
        },
        outcome="failure",
        outcome_reason="Hallucination: claimed all delivered when PKG_202 is in transit",
        verification_evidence={
            "source": "independent_oracle",
            "actual_output": model_failed_answer,
            "ground_truth_all_delivered": False,
        },
    )

    # Case A: Authoritative business expectation provided from oracle/rule -> APPROVED
    independent_business_expectation = {
        "all_delivered": False,
        "summary": "PKG_201 DELIVERED, PKG_202 IN_TRANSIT",
        "expected_status": "SHIPPED",
    }
    res_a = purify_trace_to_proposal(
        source=ep_fail,
        business_expectation=independent_business_expectation,
        expectation_source="business_rule",
        failure_attribution="skill",
        tool_contract_version="1.0",
        store=store,
    )
    assert res_a.category == "business_failure"
    assert res_a.can_trigger_skill_evolution is True
    assert res_a.proposal is not None
    prop_a = res_a.proposal
    assert prop_a.status == "APPROVED"
    assert prop_a.skill_name == "logistics_skill"
    assert prop_a.source_task_id == "task_ord_0902_status"
    assert prop_a.intent_revision == 1
    assert prop_a.expected_output == independent_business_expectation
    assert prop_a.expectation_source == "business_rule"
    assert len(prop_a.tool_snapshots) == 3
    assert prop_a.tool_snapshots[0]["contract_version"] == "1.0"
    assert prop_a.actual_output == model_failed_answer

    # Verify saved in SQLite store
    saved_prop = store.get_proposal(prop_a.proposal_id)
    assert saved_prop is not None
    assert saved_prop.status == "APPROVED"

    # Case B: Missing expectation -> PENDING_APPROVAL, cannot auto-approve or trigger evolution
    res_b = purify_trace_to_proposal(
        source=ep_fail,
        business_expectation=None,
        failure_attribution="skill",
        store=store,
    )
    assert res_b.category == "business_failure"
    assert res_b.can_trigger_skill_evolution is False
    assert res_b.proposal is not None
    assert res_b.proposal.status == "PENDING_APPROVAL"
    assert res_b.proposal.expectation_source == "missing"

    # Case C: Model's own failed answer passed as expectation -> STRICT ERROR
    with pytest.raises(ValueError, match="cannot use model's failed output as independent business expectation"):
        purify_trace_to_proposal(
            source=ep_fail,
            business_expectation=model_failed_answer,
            expectation_source="business_rule",
            failure_attribution="skill",
        )

    # Case D: Model-drafted proposal without human confirmation -> PENDING_APPROVAL
    res_d = purify_trace_to_proposal(
        source=ep_fail,
        business_expectation="Draft expectation suggested by model",
        expectation_source="draft_proposal",
        failure_attribution="skill",
    )
    assert res_d.proposal.status == "PENDING_APPROVAL"
    assert res_d.can_trigger_skill_evolution is False
    assert "requires human confirmation" in (res_d.proposal.rejection_reason or "")

    # Case E: Sanitization impaired tool snapshot -> PENDING_APPROVAL (no fabricated data)
    res_e = purify_trace_to_proposal(
        source=ep_fail,
        business_expectation=independent_business_expectation,
        expectation_source="business_rule",
        failure_attribution="skill",
        sanitization_impaired=True,
    )
    assert res_e.proposal.status == "PENDING_APPROVAL"
    assert res_e.can_trigger_skill_evolution is False
    assert "routed to human review without fabricating data" in (res_e.proposal.rejection_reason or "")


# ---------------------------------------------------------------------------
# D2 Tests: Representative Regression Cases & Multi-Stream Diversion
# ---------------------------------------------------------------------------

def test_d2_representative_regression_cases_and_multi_stream_diversion(tmp_path: Path):
    """D2: Verified normal/success forms regression cases; unknown, infra_error,
    and policy denials are diverted without triggering skill evolution; legitimate tool denials pass.
    """
    db_path = tmp_path / "skillforge.db"
    store = CandidateStore(db_path)

    # Case A: Representative verified success converted to regression case with deduplication
    ep_succ = Episode(
        episode_id="ep_succ_01",
        task_id="task_ord_0901_status",
        run_id="run_0901_succ",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={
            "query": "查询 ORD_2026_0901 物流状态",
            "business_scope": "logistics_readonly",
            "intent_revision": 1,
            "purpose": "learning",
        },
        provenances=[
            _make_provenance("query_order_packages", input_params={"order_id": "ORD_2026_0901"}),
            _make_provenance("query_package_tracking", input_params={"package_id": "PKG_101"}),
            _make_provenance("query_package_tracking", input_params={"package_id": "PKG_102"}),
        ],
        acceptance_criteria={"expected_all_delivered": True},
        outcome="success",
        verification_evidence={"source": "independent_oracle", "independent_pass": True},
    )

    res_succ = purify_trace_to_proposal(
        source=ep_succ,
        business_expectation="ORD_2026_0901 全部签收 (PKG_101, PKG_102)",
        expectation_source="oracle",
        store=store,
    )
    assert res_succ.category == "regression_success"
    assert res_succ.can_trigger_skill_evolution is False
    assert res_succ.proposal is not None
    assert res_succ.proposal.is_regression_case is True
    assert res_succ.proposal.status == "APPROVED"

    # Deduplication test: re-purifying the same task does NOT create duplicate proposal
    res_succ_dup = purify_trace_to_proposal(
        source=ep_succ,
        business_expectation="ORD_2026_0901 全部签收",
        expectation_source="oracle",
        store=store,
    )
    assert res_succ_dup.diagnosis.get("deduplicated") is True
    assert res_succ_dup.proposal.proposal_id == res_succ.proposal.proposal_id
    proposals = store.list_proposals(skill_name="logistics_skill")
    assert len(proposals) == 1  # Only 1 unique regression proposal stored

    # Case B: Unknown outcome diverted to diagnosis_only
    ep_unknown = Episode(
        episode_id="ep_unknown_01",
        task_id="task_ord_unknown",
        run_id="run_unknown",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria="vague query",
        outcome="unknown",
        outcome_reason="Missing tracking number, judge inconclusive",
    )
    res_unknown = purify_trace_to_proposal(source=ep_unknown, store=store)
    assert res_unknown.category == "diagnosis_only"
    assert res_unknown.proposal is None
    assert res_unknown.can_trigger_skill_evolution is False

    # Case C: Infrastructure / Judge timeout diverted to infrastructure_report
    ep_infra = Episode(
        episode_id="ep_infra_01",
        task_id="task_ord_infra",
        run_id="run_infra",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="INFRASTRUCTURE_ERROR: sandbox timeout after 30s",
    )
    res_infra = purify_trace_to_proposal(
        source=ep_infra,
        failure_attribution="infrastructure",
        store=store,
    )
    assert res_infra.category == "infrastructure_report"
    assert res_infra.proposal is None
    assert res_infra.can_trigger_skill_evolution is False

    # Case D: Policy Denial handled appropriately -> PASS, not mechanical skill failure
    ep_policy = Episode(
        episode_id="ep_policy_01",
        task_id="task_ord_policy",
        run_id="run_policy",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={
            "query": "查询 ORD_2026_0901 并申请退款",
            "business_scope": "logistics_readonly",
            "purpose": "learning",
        },
        provenances=[
            _make_provenance("query_order_packages", input_params={"order_id": "ORD_2026_0901"}),
            _make_provenance("refund_order", output_status="PERMISSION_DENIED", output_summary="Blocked by ToolBroker"),
        ],
        acceptance_criteria="status check",
        outcome="failure",
        outcome_reason="PERMISSION_DENIED: refund_order not permitted",
    )

    # Oracle verifier confirms that unauthorized refund was properly blocked and not executed
    def mock_policy_oracle():
        return {"independent_pass": True, "reason": "Unauthorized tool call successfully prevented"}

    res_policy = purify_trace_to_proposal(
        source=ep_policy,
        failure_attribution="policy",
        oracle_verifier=mock_policy_oracle,
        store=store,
    )
    assert res_policy.category == "policy_compliance"
    assert res_policy.proposal is None
    assert res_policy.can_trigger_skill_evolution is False
    assert res_policy.diagnosis["policy_enforced"] is True


# ---------------------------------------------------------------------------
# D3 Tests: Grouped Partition Anti-Leakage & Anti-Dilution Rules
# ---------------------------------------------------------------------------

def test_d3_grouped_partition_anti_leakage_and_dilution_prevention(tmp_path: Path):
    """D3: Grouped partition by task family ensures same family cannot cross into heldout;
    anti-dilution duplicate detection blocks inflating the denominator; persistence survives DB reopen.
    """
    # 1. Grouped Partition Anti-Leakage
    # Create 12 cases across 3 distinct task families: alpha, beta, gamma
    cases = [
        {"id": "wq_alpha_01", "source_task_id": "task_alpha", "variant_family": "fam_alpha", "query": "查北京今天天气", "intent_revision": 1},
        {"id": "wq_alpha_02", "source_task_id": "task_alpha", "variant_family": "fam_alpha", "query": "北京今天有雨吗", "intent_revision": 1},
        {"id": "wq_alpha_03", "source_task_id": "task_alpha", "variant_family": "fam_alpha", "query": "看下北京气温", "intent_revision": 1},

        {"id": "wq_beta_01", "source_task_id": "task_beta", "variant_family": "fam_beta", "query": "查上海今天天气", "intent_revision": 1},
        {"id": "wq_beta_02", "source_task_id": "task_beta", "variant_family": "fam_beta", "query": "上海明天下雨吗", "intent_revision": 1},
        {"id": "wq_beta_03", "source_task_id": "task_beta", "variant_family": "fam_beta", "query": "看下上海气温", "intent_revision": 1},

        {"id": "wq_gamma_01", "source_task_id": "task_gamma", "variant_family": "fam_gamma", "query": "查广州今天天气", "intent_revision": 1},
        {"id": "wq_gamma_02", "source_task_id": "task_gamma", "variant_family": "fam_gamma", "query": "广州降水情况", "intent_revision": 1},
    ]

    partition = group_and_partition_cases(cases, dev_ratio=0.5, seed=42)
    repair_fams = set(c["variant_family"] for c in partition["repair"])
    holdout_fams = set(c["variant_family"] for c in partition["experiment_holdout"])

    # Strict isolation invariant: no family can be in both repair and holdout!
    overlap = repair_fams & holdout_fams
    assert len(overlap) == 0, f"Data leakage detected! Families present in both sets: {overlap}"
    assert len(partition["repair"]) + len(partition["experiment_holdout"]) == len(cases)

    # 2. Anti-Dilution Auto-Case Ratio Enforcement
    # Baseline: 5 auto cases and 5 distinct human cases (ratio = 5/10 = 50%) -> VALID
    valid_set = [
        {"id": f"wq_auto_{i}", "is_auto": True, "query": f"auto query {i}"} for i in range(1, 6)
    ] + [
        {"id": f"wq_h_{i}", "is_auto": False, "query": f"human query {i}"} for i in range(1, 6)
    ]
    report_valid = validate_repair_set_composition(valid_set, max_auto_ratio=0.50)
    assert report_valid["valid"] is True
    assert report_valid["effective_auto_ratio"] == 0.50

    # Malicious dilution attempt: 6 auto cases (should be 6/(6+5)=54.5% > 50%),
    # but attacker adds 20 duplicate copies of human query 1 to try to claim 6/(6+25)=19.3%
    dilution_set = [
        {"id": f"wq_auto_{i}", "is_auto": True, "query": f"auto query {i}"} for i in range(1, 7)
    ] + [
        {"id": f"wq_h_{i}", "is_auto": False, "query": f"human query {i}"} for i in range(1, 6)
    ] + [
        {"id": f"wq_h_dup_{i}", "is_auto": False, "query": "human query 1"} for i in range(1, 21)
    ]

    with pytest.raises(ValueError, match="Dilution attempt rejected"):
        validate_repair_set_composition(dilution_set, max_auto_ratio=0.50)

    # 3. Persistence and DB Reopen
    db_path = tmp_path / "partition_test.db"
    store1 = CandidateStore(db_path)
    prop = TestCaseProposal(
        proposal_id="prop_persist_01",
        skill_name="weather_query",
        source_task_id="task_bj_01",
        intent_revision=2,
        contract_fingerprint="fp_bj_rev2",
        query="北京天气如何",
        tool_snapshots=[{"tool_name": "query_weather", "contract_version": "1.0"}],
        expected_output="晴天 25°C",
        expectation_source="business_rule",
        status="APPROVED",
        failure_attribution="skill",
        partition_tier="repair",
        variant_family="fam_bj",
    )
    store1.save_proposal(prop)
    store1.close()

    # Reopen connection
    store2 = CandidateStore(db_path)
    loaded_prop = store2.get_proposal("prop_persist_01")
    assert loaded_prop is not None
    assert loaded_prop.intent_revision == 2
    assert loaded_prop.partition_tier == "repair"
    assert loaded_prop.status == "APPROVED"
    assert loaded_prop.expected_output == "晴天 25°C"
    store2.close()


# ---------------------------------------------------------------------------
# D4 Tests: Scope-Aware Pattern Mining & Sub-Scenario Failure Guard
# ---------------------------------------------------------------------------

def test_d4_scope_aware_pattern_mining_and_subscenario_failure_guard(tmp_path: Path):
    """D4: Scope-aware clustering groups FIRST by scope, intent revision, and tool contracts;
    similar wording with different scopes/tools are never falsely merged; sub-scenario failures
    cannot be masked by majority success; support count enforces independent task IDs.
    """
    db_path = tmp_path / "mining_test.db"
    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)

    # Case A: Two tasks with nearly identical wording but different scopes & tools
    # Task 1: "查询订单状态及包裹详情", scope="logistics_readonly", tools=("query_order_packages", "query_package_tracking")
    # Task 2: "查询订单状态及申请包裹退款", scope="logistics_refund", tools=("query_order_packages", "refund_order")
    ep_readonly = Episode(
        episode_id="ep_ro_01",
        task_id="task_ro_01",
        run_id="run_ro_01",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={
            "query": "查询订单状态及包裹详情",
            "business_scope": "logistics_readonly",
            "intent_revision": 1,
            "purpose": "learning",
        },
        provenances=[
            _make_provenance("query_order_packages"),
            _make_provenance("query_package_tracking"),
        ],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )

    ep_refund = Episode(
        episode_id="ep_rf_01",
        task_id="task_rf_01",
        run_id="run_rf_01",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={
            "query": "查询订单状态及包裹详情并退款",
            "business_scope": "logistics_refund",
            "intent_revision": 1,
            "purpose": "learning",
        },
        provenances=[
            _make_provenance("query_order_packages"),
            _make_provenance("refund_order"),
        ],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )

    # Verify extract_scope_key distinguishes them
    scope_ro = extract_scope_key(ep_readonly)
    scope_rf = extract_scope_key(ep_refund)
    assert scope_ro != scope_rf
    assert scope_ro[0] == "logistics_readonly"
    assert scope_rf[0] == "logistics_refund"
    assert scope_ro[2] == ("query_order_packages", "query_package_tracking")
    assert scope_rf[2] == ("query_order_packages", "refund_order")

    # Case B: Support count requires independent task_ids
    # Insert 3 runs of the exact same task_id ("task_ro_01")
    ep_store.save_episode(ep_readonly)
    ep_ro_run2 = Episode(
        episode_id="ep_ro_02",
        task_id="task_ro_01",  # Same task_id
        run_id="run_ro_02",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly"},
        provenances=[_make_provenance("query_order_packages")],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )
    ep_ro_run3 = Episode(
        episode_id="ep_ro_03",
        task_id="task_ro_01",  # Same task_id
        run_id="run_ro_03",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly"},
        provenances=[_make_provenance("query_order_packages")],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )
    ep_store.save_episode(ep_ro_run2)
    ep_store.save_episode(ep_ro_run3)

    # Mining with min_support=3 should ABSTAIN because there is only 1 distinct task_id
    cfg = PatternMiningConfig(min_support=3)
    batch_rep = mine_pending(ep_store, cand_store, config=cfg)
    assert batch_rep.unique_tasks == 1
    assert len(batch_rep.clusters) == 1
    assert batch_rep.clusters[0].decision == "abstain"
    assert "Insufficient independent task support: got 1, required >= 3" in batch_rep.clusters[0].abstain_reasons[0]
    assert batch_rep.embedder_used in ("bow_fallback", "embed_layer")

    # Case C: Sub-Scenario Failure Guard (正反例参与范围分析)
    # Populate a cluster with 4 successful tasks and 1 distinct sub-scenario task that always fails
    # Overall success rate would be 4/5 = 80%, but sub-scenario 'split_warehouse' fails 100%
    ep_succ_a = Episode(
        episode_id="ep_s1", task_id="task_s1", run_id="r1", skill_name="logistics_skill", skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly", "sub_intent": "standard_order"},
        provenances=[_make_provenance("query_order_packages"), _make_provenance("query_package_tracking")],
        acceptance_criteria={"sub_intent": "standard_order"}, outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )
    ep_succ_b = Episode(
        episode_id="ep_s2", task_id="task_s2", run_id="r2", skill_name="logistics_skill", skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly", "sub_intent": "standard_order"},
        provenances=[_make_provenance("query_order_packages"), _make_provenance("query_package_tracking")],
        acceptance_criteria={"sub_intent": "standard_order"}, outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )
    ep_succ_c = Episode(
        episode_id="ep_s3", task_id="task_s3", run_id="r3", skill_name="logistics_skill", skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly", "sub_intent": "standard_order"},
        provenances=[_make_provenance("query_order_packages"), _make_provenance("query_package_tracking")],
        acceptance_criteria={"sub_intent": "standard_order"}, outcome="success",
        verification_evidence={"source": "oracle", "independent_pass": True},
    )
    ep_fail_sub = Episode(
        episode_id="ep_f_split", task_id="task_split_01", run_id="r_split", skill_name="logistics_skill", skill_version="1.0.0",
        environment={"purpose": "learning", "business_scope": "logistics_readonly", "sub_intent": "split_warehouse"},
        provenances=[_make_provenance("query_order_packages"), _make_provenance("query_package_tracking")],
        acceptance_criteria={"sub_intent": "split_warehouse"}, outcome="failure",
        outcome_reason="Failed to aggregate packages from multiple warehouses",
    )
    ep_store.save_episode(ep_succ_a)
    ep_store.save_episode(ep_succ_b)
    ep_store.save_episode(ep_succ_c)
    ep_store.save_episode(ep_fail_sub)

    cfg2 = PatternMiningConfig(min_support=3, min_success_rate=0.75)
    batch_rep2 = mine_pending(ep_store, cand_store, config=cfg2)
    # The cluster with split_warehouse must be abstained due to sub-scenario failure guard
    ro_cluster = next((c for c in batch_rep2.clusters if "logistics_readonly" in (c.scope_key or "")), None)
    assert ro_cluster is not None
    assert ro_cluster.decision == "abstain"
    assert any("Stable sub-scenario failure detected for 'split_warehouse'" in r for r in ro_cluster.abstain_reasons)


# ---------------------------------------------------------------------------
# D5 Tests: Purpose Isolation Penetrates Low-Level Entry Points
# ---------------------------------------------------------------------------

def test_d5_purpose_isolation_penetration_at_low_level_entry_points(tmp_path: Path):
    """D5: Locked evaluation inputs and expectations cannot leak into candidate generation,
    pattern mining, repair prompts, or dev case proposals; demotion invalidates prior benchmark numbers.
    """
    db_path = tmp_path / "skillforge.db"
    store = CandidateStore(db_path)

    # 1. Entry Point 1: generate_candidate_from_requirement low-level rejection
    with pytest.raises(ValueError, match="Purpose isolation violation: cannot generate candidate skills from locked evaluation"):
        generate_candidate_from_requirement(
            request="查询订单状态",
            purpose="evaluation",
        )

    with pytest.raises(ValueError, match="Purpose isolation violation"):
        generate_candidate_from_requirement(
            request="查询订单状态",
            is_heldout=True,
        )

    # 2. Entry Point 2: repair_skill_failure low-level rejection when heldout case passed
    ep_store = EpisodeStore(db_path)
    failed_ep = Episode(
        episode_id="ep_repair_fail",
        task_id="task_fail",
        run_id="run_fail",
        skill_name="logistics_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[_make_provenance("query_order_packages")],
        acceptance_criteria={},
        outcome="failure",
        outcome_reason="Format error",
    )
    ep_store.save_episode(failed_ep)

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(db_path=db_path, skills_dir=skills_dir)
    fake_llm = FakePatcherLLM()

    # Passing heldout case to repair regression must be strictly rejected
    heldout_eval_case = {
        "id": "wq_h01",
        "skill": "logistics_skill",
        "query": "秘密评测输入",
        "reference": "秘密评测预期",
        "layer": "experiment_holdout",
        "is_heldout": True,
    }
    with pytest.raises(ValueError, match="Purpose isolation violation: case 'wq_h01' is from locked tier 'experiment_holdout'"):
        repair_skill_failure(
            episodes=[failed_ep],
            skill_name="logistics_skill",
            episode_store=ep_store,
            candidate_store=store,
            registry=registry,
            evaluator=None,
            eval_cases=[heldout_eval_case],
            llm=fake_llm,
        )

    # 3. Entry Point 3: purify_trace_to_proposal low-level rejection & demotion
    heldout_trace = {
        "case_id": "case_heldout_99",
        "skill": "logistics_skill",
        "query": "锁定测试集的查询",
        "purpose": "experiment_holdout",
        "is_heldout": True,
        "evaluand_answer": "wrong answer",
        "judge_verdict": {"verdict": "VALID_FAILURE"},
    }

    # Reject without explicit demotion
    with pytest.raises(ValueError, match="Purpose isolation violation: trace 'case_heldout_99' has locked purpose"):
        purify_trace_to_proposal(
            source=heldout_trace,
            business_expectation="correct answer",
            expectation_source="business_rule",
            allow_heldout_demotion=False,
        )

    # Allow with explicit demotion
    res_demoted = purify_trace_to_proposal(
        source=heldout_trace,
        business_expectation="correct answer",
        expectation_source="business_rule",
        allow_heldout_demotion=True,
        store=store,
    )
    assert res_demoted.category == "business_failure"
    assert res_demoted.proposal.partition_tier == "repair"
    assert res_demoted.diagnosis["demotion_applied"] is True

    # 4. Demotion Audit Record Check
    audit_record = demote_heldout_to_dev(res_demoted.proposal.proposal_id, store=store)
    assert audit_record["benchmark_invalidated"] is True
    assert "All previous evaluation scores on this case can no longer claim heldout status" in audit_record["warning"]
