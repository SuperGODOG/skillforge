"""D4a Scenario: End-to-end Pi execution chain evolution evidence generator for local dashboard.

Produces fully auditable trace from real official Pi runner, ToolBroker, Collector,
EpisodeStore, CandidateStore, SkillRegistry, and ReleaseStateMachine.

Invariants:
1. Isolated unique demo directory per run (dashboard_evolution_<uuid>). Never overwrites existing artifacts.
2. Explicit evidence labeling: model=faux/scripted, business=synthetic, execution=official_pi_runner,
   os_sandbox=unverified, cost=null. Never fakes real LLM calls.
3. Isolated fixture baseline skill 1.0.0.
4. Two learning failure tasks (omitted packages / unverified delivery claim) independently caught
   by verify_logistics_fulfillment code oracle, finalized into immutable failure episodes.
5. One normal learning task passing oracle, finalized into immutable success episode.
6. Real repair_skill_failure invocation with max_attempts=1, attributing failure, invoking scripted patcher,
   evaluating candidate via DynamicPiEvaluationBackend against normal + target cases, passing ratchet gate,
   recording job as READY, candidate binding exact source episode IDs.
7. Promotion gate: caller_confirmed=False rejected, caller_confirmed=True promoted to 1.0.1 via ReleaseStateMachine
   inside demo git repository.
8. Rebuilt runtime autonomous zero-config future reuse retrieving 1.0.1, verified frozen body,
   executing via real Pi runner into new success episode. Old runs remain frozen at 1.0.0.
9. Atomic report.json disk write with comprehensive lineage and evidence labels.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Optional
import uuid

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator import SkillEvaluator
from skillforge.evolution_loop import compute_candidate_hash
from skillforge.models import SkillMeta
from skillforge.pi_evaluation import PiEvaluationBackend, PiEvaluationRunResult
from skillforge.registry import SkillRegistry
from skillforge.repair import repair_skill_failure, promote_repaired_skill
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_ORDERS,
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)
from skillforge.state_machine import ReleaseStateMachine


BASELINE_BODY = """# 1. Overview
Baseline logistics query instructions for orders and packages.
## Constraints
- Only query status, do not modify.
# 2. Instructions
Query order packages and query package tracking status.
# 3. Examples
Example 1: query order and package status."""

PATCHED_BODY = """# 1. Overview
Baseline logistics query instructions for orders and packages.
## Constraints
- Only query status, do not modify.
- Never omit any package; verify all packages returned by order query.
# 2. Instructions
Query order packages and sequentially query tracking for all returned packages without omission.
# 3. Examples
Example 1: query order and package status."""


def _build_baseline_skill_text() -> str:
    return f"""---
name: logistics_query
version: 1.0.0
description: 物流包裹状态查询工具
use_when: 用户查询订单物流状态时使用
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords:
    - 物流
    - 订单
    - order
    - package
  not_for:
    - 退款
not_for:
  - 退款
---
{BASELINE_BODY}
"""


def _build_patched_skill_text() -> str:
    return f"""---
name: logistics_query
version: 1.0.1
description: 物流包裹状态查询工具
use_when: 用户查询订单物流状态时使用
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords:
    - 物流
    - 订单
    - order
    - package
  not_for:
    - 退款
not_for:
  - 退款
---
{PATCHED_BODY}
"""


def make_logistics_responses_for_order(order_id: str) -> list[dict[str, Any]]:
    """Synthesize complete, accurate tool responses and final text for an order."""
    order = MOCK_ORDERS.get(order_id)
    if not order:
        raise KeyError(f"Order '{order_id}' not found in MOCK_ORDERS")
    pkgs = order.get("packages", [])
    responses: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": f"tc_{order_id}_order",
                    "name": "query_order_packages",
                    "arguments": {"order_id": order_id},
                }
            ],
            "stopReason": "toolUse",
        }
    ]
    tracking_lines: list[str] = []
    for pkg_id in pkgs:
        pkg_data = MOCK_PACKAGES[pkg_id]
        status = pkg_data["status"]
        carrier = pkg_data["carrier"]
        tracking_lines.append(f"{pkg_id} ({carrier}): {status}")
        responses.append({
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": f"tc_{order_id}_{pkg_id}",
                    "name": "query_package_tracking",
                    "arguments": {"package_id": pkg_id},
                }
            ],
            "stopReason": "toolUse",
        })

    all_delivered = all(
        MOCK_PACKAGES.get(p, {}).get("status") == "DELIVERED" for p in pkgs
    )
    if all_delivered:
        summary_text = (
            f"订单 {order_id} 共有 {len(pkgs)} 个包裹：{', '.join(tracking_lines)}。"
            f"全部包裹已妥投。"
        )
    else:
        summary_text = (
            f"订单 {order_id} 共有 {len(pkgs)} 个包裹：{', '.join(tracking_lines)}。"
            f"订单未全部签收。"
        )

    responses.append({
        "role": "assistant",
        "content": [{"type": "text", "text": summary_text}],
        "stopReason": "stop",
    })
    return responses


class ScriptedPatcher:
    """Minimal scripted patcher returning full SKILL.md for version 1.0.1.

    Explicitly labeled as scripted fixture, not a real LLM.
    """

    def __init__(self, content: str):
        self.content = content
        self.calls: list[str] = []

    def invoke(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(str(prompt))
        return SimpleNamespace(content=self.content, usage=None)


class DeterministicJudgeLLM:
    """Deterministic judge adapter for criteria evaluation without external LLM calls.

    Explicitly labeled as scripted fixture, not a real LLM.
    """

    def __init__(self, contents: list[str] | None = None, role: str = "judge"):
        self.contents = list(contents or [])
        self.role = role
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.contents.pop(0) if self.contents else json.dumps({
            "verdict": "tied",
            "reason_codes": ["OK_READABILITY"],
            "evidence_summary": "Both outputs are clear and readable.",
        })
        if messages and any("findings" in str(m.get("content", "")) for m in messages if isinstance(m, dict)):
            if "findings" not in str(content):
                user_msg = ""
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_msg = str(m.get("content", ""))
                m_ans = re.search(r"<answer>\s*(.*?)\s*</answer>", user_msg, re.DOTALL)
                ev_str = m_ans.group(1).strip() if m_ans else ""
                if not ev_str or ev_str == "(空回答)":
                    m_q = re.search(r"<query>\s*(.*?)\s*</query>", user_msg, re.DOTALL)
                    ev_str = m_q.group(1).strip() if m_q else "query"
                quote_ev = f'"{ev_str[:30]}"'
                content = json.dumps({
                    "findings": [
                        {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                    ]
                })
        return SimpleNamespace(
            content=content,
            usage=None,
        )


class DynamicPiEvaluationBackend(PiEvaluationBackend):
    """PiEvaluationBackend routing scripted responses dynamically per query order_id."""

    def run(
        self,
        query: str,
        body: str = "",
        meta: Optional[SkillMeta] = None,
        skill_name: Optional[str] = None,
    ) -> PiEvaluationRunResult:
        m = re.search(r"ORD_\w+", query)
        if m is None:
            raise ValueError("Demo evaluation requires an explicit synthetic order ID")
        order_id = m.group(0)
        if self.model_config is not None:
            self.model_config["responses"] = make_logistics_responses_for_order(order_id)
        return super().run(query=query, body=body, meta=meta, skill_name=skill_name)


def _serialize_tool_record(rec: Any) -> dict[str, Any]:
    if hasattr(rec, "__dataclass_fields__"):
        d = asdict(rec)
    elif isinstance(rec, dict):
        d = dict(rec)
    else:
        d = {
            "call_id": getattr(rec, "call_id", ""),
            "run_id": getattr(rec, "run_id", ""),
            "tool_name": getattr(rec, "tool_name", ""),
            "status": getattr(rec, "status", ""),
            "input_params": getattr(rec, "input_params", {}),
            "output_text": getattr(rec, "output_text", ""),
            "error_type": getattr(rec, "error_type", None),
            "error_message": getattr(rec, "error_message", None),
            "latency_ms": getattr(rec, "latency_ms", None),
            "created_at": getattr(rec, "created_at", None),
        }
    return d


def _write_report(directory: Path, report: dict[str, Any]) -> None:
    temporary = directory / "report.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / "report.json")


def run_dashboard_evolution_demo(output_dir: Path | str) -> dict[str, Any]:
    """Execute end-to-end evolution demo and write atomic report.json in a unique subfolder."""
    parent_dir = Path(output_dir).resolve()
    parent_dir.mkdir(parents=True, exist_ok=True)

    demo_uuid = uuid.uuid4().hex[:12]
    demo_dir = parent_dir / f"dashboard_evolution_{demo_uuid}"
    demo_dir.mkdir(parents=True, exist_ok=False)

    runtime = registry = episode_store = cand_store = sm = None

    cases_report: list[dict[str, Any]] = []
    current_phase = "INIT"

    common_evidence = {
        "model": "scripted / faux",
        "data": "synthetic",
        "execution": "official_pi_runner_with_agent_runtime",
        "os_sandbox": "unverified",
        "cost": None,
    }

    def checkpoint(phase: str) -> None:
        nonlocal current_phase
        current_phase = phase
        _write_report(demo_dir, {
            "status": "RUNNING", "phase": phase,
            "scenario": "dashboard_evolution_demo", "demo_id": demo_uuid,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": "scripted / faux", "evidence": common_evidence,
            "cases": cases_report,
        })

    try:
        checkpoint("SETUP")
        skills_dir = demo_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        baseline_dir = skills_dir / "logistics_query"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        (baseline_dir / "SKILL.md").write_text(_build_baseline_skill_text(), encoding="utf-8")

        # Local temporary git repository inside demo_dir only
        subprocess.run(["git", "init"], cwd=demo_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "skills"], cwd=demo_dir, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=DemoRunner", "-c", "user.email=demo@example.com", "commit", "-m", "fixture baseline 1.0.0"], cwd=demo_dir, check=True, capture_output=True)

        db_path = demo_dir / "runtime.db"
        episodes_db = demo_dir / "episodes.db"
        candidates_db = demo_dir / "candidates.db"
        skills_db = demo_dir / "skills.db"

        registry = SkillRegistry(db_path=skills_db, skills_dir=skills_dir)
        registry.load_skills_from_dir()

        episode_store = EpisodeStore(db_path=episodes_db)
        cand_store = CandidateStore(db_path=candidates_db, episode_store=episode_store)
        broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
        broker.register_tool(QueryOrderPackagesTool())
        broker.register_tool(QueryPackageTrackingTool())
        collector = ExperienceCollector(episode_store=episode_store, registry=registry)

        runtime = AgentRuntime(
            db_path=db_path,
            tool_broker=broker,
            registry=registry,
            collector=collector,
            episode_store=episode_store,
            candidate_store=cand_store,
        )

        # Phase 1: Run Learning Failure 1 (omits PKG_102, claims all delivered)
        checkpoint("LEARNING_FAILURE_1")
        order_id_1 = "ORD_2026_0901"
        query_1 = f"Query order {order_id_1} logistics packages"
        runtime.start_run(
            run_id="run_learning_fail_1",
            task_id="task_learning_fail_1",
            skill_name="logistics_query",
            purpose="learning",
            budget_max=10,
            task_description=query_1,
        )
        fail_resp_1 = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_f1_order",
                        "name": "query_order_packages",
                        "arguments": {"order_id": order_id_1},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_f1_pkg101",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"订单 {order_id_1} 状态已核查：全部签收，所有包裹均已送达。",
                    }
                ],
                "stopReason": "stop",
            },
        ]
        out_1 = runtime.run_agent_with_pi(
            run_id="run_learning_fail_1",
            input_text=query_1,
            tools=["query_order_packages", "query_package_tracking"],
            test_mode=True,
            model_config={"provider": "faux", "responses": fail_resp_1},
        )
        calls_1 = runtime.list_tool_calls("run_learning_fail_1")
        oracle_1 = verify_logistics_fulfillment(model_output=out_1, order_id=order_id_1, tool_records=calls_1)
        assert oracle_1["independent_pass"] is False, "Expected learning fail 1 to fail oracle"
        run_rec_1, ep_1 = runtime.finalize_run(run_id="run_learning_fail_1", model_output=out_1, verification_evidence=oracle_1)
        assert ep_1 is not None and ep_1.outcome == "failure"

        cases_report.append({
            "run_id": "run_learning_fail_1",
            "task_id": "task_learning_fail_1",
            "status": run_rec_1.status,
            "purpose": "learning",
            "skill_name": "logistics_query",
            "skill_version": "1.0.0",
            "response_text": out_1,
            "content_hash": run_rec_1.content_hash,
            "frozen_body": run_rec_1.frozen_body,
            "episode_id": ep_1.episode_id,
            "episode_outcome": ep_1.outcome,
            "oracle": oracle_1,
            "tool_records": [_serialize_tool_record(t) for t in calls_1],
            "model": "scripted / faux",
            "evidence": common_evidence,
            "usage": None,
        })

        # Phase 2: Run Learning Failure 2 (omits PKG_202, claims all delivered)
        checkpoint("LEARNING_FAILURE_2")
        order_id_2 = "ORD_2026_0902"
        query_2 = f"Query order {order_id_2} logistics packages"
        runtime.start_run(
            run_id="run_learning_fail_2",
            task_id="task_learning_fail_2",
            skill_name="logistics_query",
            purpose="learning",
            budget_max=10,
            task_description=query_2,
        )
        fail_resp_2 = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_f2_order",
                        "name": "query_order_packages",
                        "arguments": {"order_id": order_id_2},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_f2_pkg201",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_201"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"订单 {order_id_2} 状态已核查：全部签收，所有包裹均已送达。",
                    }
                ],
                "stopReason": "stop",
            },
        ]
        out_2 = runtime.run_agent_with_pi(
            run_id="run_learning_fail_2",
            input_text=query_2,
            tools=["query_order_packages", "query_package_tracking"],
            test_mode=True,
            model_config={"provider": "faux", "responses": fail_resp_2},
        )
        calls_2 = runtime.list_tool_calls("run_learning_fail_2")
        oracle_2 = verify_logistics_fulfillment(model_output=out_2, order_id=order_id_2, tool_records=calls_2)
        assert oracle_2["independent_pass"] is False, "Expected learning fail 2 to fail oracle"
        run_rec_2, ep_2 = runtime.finalize_run(run_id="run_learning_fail_2", model_output=out_2, verification_evidence=oracle_2)
        assert ep_2 is not None and ep_2.outcome == "failure"

        cases_report.append({
            "run_id": "run_learning_fail_2",
            "task_id": "task_learning_fail_2",
            "status": run_rec_2.status,
            "purpose": "learning",
            "skill_name": "logistics_query",
            "skill_version": "1.0.0",
            "response_text": out_2,
            "content_hash": run_rec_2.content_hash,
            "frozen_body": run_rec_2.frozen_body,
            "episode_id": ep_2.episode_id,
            "episode_outcome": ep_2.outcome,
            "oracle": oracle_2,
            "tool_records": [_serialize_tool_record(t) for t in calls_2],
            "model": "scripted / faux",
            "evidence": common_evidence,
            "usage": None,
        })

        # Phase 3: Run Learning Normal Success (queries all packages accurately)
        checkpoint("LEARNING_NORMAL_SUCCESS")
        order_id_norm = "ORD_DEV_0101"
        query_norm = f"Query order {order_id_norm} logistics packages"
        runtime.start_run(
            run_id="run_learning_normal_1",
            task_id="task_learning_normal_1",
            skill_name="logistics_query",
            purpose="learning",
            budget_max=10,
            task_description=query_norm,
        )
        norm_resp = make_logistics_responses_for_order(order_id_norm)
        out_norm = runtime.run_agent_with_pi(
            run_id="run_learning_normal_1",
            input_text=query_norm,
            tools=["query_order_packages", "query_package_tracking"],
            test_mode=True,
            model_config={"provider": "faux", "responses": norm_resp},
        )
        calls_norm = runtime.list_tool_calls("run_learning_normal_1")
        oracle_norm = verify_logistics_fulfillment(model_output=out_norm, order_id=order_id_norm, tool_records=calls_norm)
        assert oracle_norm["independent_pass"] is True, "Expected normal learning to pass oracle"
        run_rec_norm, ep_norm = runtime.finalize_run(run_id="run_learning_normal_1", model_output=out_norm, verification_evidence=oracle_norm)
        assert ep_norm is not None and ep_norm.outcome == "success"

        cases_report.append({
            "run_id": "run_learning_normal_1",
            "task_id": "task_learning_normal_1",
            "status": run_rec_norm.status,
            "purpose": "learning",
            "skill_name": "logistics_query",
            "skill_version": "1.0.0",
            "response_text": out_norm,
            "content_hash": run_rec_norm.content_hash,
            "frozen_body": run_rec_norm.frozen_body,
            "episode_id": ep_norm.episode_id,
            "episode_outcome": ep_norm.outcome,
            "oracle": oracle_norm,
            "tool_records": [_serialize_tool_record(t) for t in calls_norm],
            "model": "scripted / faux",
            "evidence": common_evidence,
            "usage": None,
        })

        # Phase 4: Retrieve episodes and execute repair_skill_failure
        checkpoint("REPAIR_EXECUTION")
        failed_ep1 = episode_store.get_episode(ep_1.episode_id)
        failed_ep2 = episode_store.get_episode(ep_2.episode_id)
        assert failed_ep1 is not None and failed_ep2 is not None

        patcher = ScriptedPatcher(content=_build_patched_skill_text())
        backend = DynamicPiEvaluationBackend(
            runtime=runtime,
            tool_names=["query_order_packages", "query_package_tracking"],
            model_config={"provider": "faux", "responses": make_logistics_responses_for_order(order_id_1)},
            test_mode=True,
            budget_max=10,
        )
        evaluator = SkillEvaluator(
            registry=registry,
            llm=DeterministicJudgeLLM(role="execution"),
            judge_llm=DeterministicJudgeLLM(role="judge"),
            execution_backend=backend,
        )
        eval_cases = [
            {
                "id": "eval_regression_normal",
                "query": query_norm,
                "oracle": "logistics",
                "order_id": order_id_norm,
                "skill": "logistics_query",
                "reference": "Order ORD_DEV_0101 has 2 packages: PKG_D101 is DELIVERED and PKG_D102 is DELIVERED. 全部包裹已妥投。",
            },
            {
                "id": "eval_regression_fail_target",
                "query": query_1,
                "oracle": "logistics",
                "order_id": order_id_1,
                "skill": "logistics_query",
                "reference": "Order ORD_2026_0901 has 2 packages: PKG_101 is DELIVERED and PKG_102 is DELIVERED. 全部包裹已妥投。",
            },
            {
                "id": "eval_regression_partial_delivery",
                "query": query_2,
                "oracle": "logistics",
                "order_id": order_id_2,
                "skill": "logistics_query",
                "reference": "PKG_201 is DELIVERED; PKG_202 is IN_TRANSIT. 订单未全部签收。",
            },
        ]

        job = repair_skill_failure(
            episodes=[failed_ep1, failed_ep2],
            skill_name="logistics_query",
            episode_store=episode_store,
            candidate_store=cand_store,
            registry=registry,
            evaluator=evaluator,
            eval_cases=eval_cases,
            llm=patcher,
            max_attempts=1,
        )

        assert job.status == "READY", f"Expected job status READY, got {job.status}"
        assert job.latest_candidate is not None
        stored_candidate = cand_store.get_candidate(job.latest_candidate.candidate_id)
        assert stored_candidate is not None
        assert set(stored_candidate.source_episode_ids) == {ep_1.episode_id, ep_2.episode_id}
        val_rec = cand_store.get_validation_record(job.latest_candidate.candidate_id)
        assert val_rec is not None
        assert val_rec.ratchet_decision == "PASS"
        assert val_rec.content_hash == compute_candidate_hash(stored_candidate)
        assert val_rec.baseline_version == "1.0.0"
        assert val_rec.config_hash and val_rec.dataset_version
        assert len(patcher.calls) == 1
        assert all(ep.outcome_reason in patcher.calls[0] for ep in (failed_ep1, failed_ep2))

        # Phase 5: Gated Promotion: verify caller_confirmed=False rejected, then promote
        checkpoint("PROMOTION_GATE")
        sm = ReleaseStateMachine(db_path=registry.db_path, repo_root=demo_dir)
        rejection_reason = ""
        try:
            promote_repaired_skill(
                job=job,
                candidate_store=cand_store,
                registry=registry,
                state_machine=sm,
                caller_confirmed=False,
                evaluator=evaluator,
            )
            raise AssertionError("Expected ValueError when caller_confirmed=False")
        except ValueError as exc:
            rejection_reason = str(exc)

        assert registry.get_meta("logistics_query").version == "1.0.0"
        stored_val_pre = cand_store.get_validation_record(job.latest_candidate.candidate_id)
        assert stored_val_pre is not None and stored_val_pre.promoted is False

        release = promote_repaired_skill(
            job=job,
            candidate_store=cand_store,
            registry=registry,
            state_machine=sm,
            caller_confirmed=True,
            evaluator=evaluator,
        )
        assert release.version == "1.0.1"
        assert registry.get_meta("logistics_query").version == "1.0.1"
        assert job.status == "PROMOTED"
        assert job.release_id == release.release_id
        # Save promoted state to episode_store connection as well for dual-db sync
        job.save(episode_store._get_conn())

        # Phase 6: Rebuild Runtime and perform autonomous future reuse
        checkpoint("FUTURE_REUSE")
        runtime.close()

        new_collector = ExperienceCollector(episode_store=episode_store, registry=registry)
        new_runtime = AgentRuntime(
            db_path=db_path,
            tool_broker=broker,
            registry=registry,
            collector=new_collector,
            episode_store=episode_store,
            candidate_store=cand_store,
        )

        try:
            reuse_query = f"请查询订单 {order_id_1} 的物流包裹状态"
            run_rec_reuse = new_runtime.start_run(
                run_id="run_future_reuse_1",
                task_id="task_future_reuse_1",
                purpose="learning",
                enable_reuse=True,
                require_reuse=True,
                task_description=reuse_query,
                budget_max=10,
            )
            assert run_rec_reuse.skill_name == "logistics_query"
            assert run_rec_reuse.skill_version == "1.0.1"
            assert run_rec_reuse.frozen_body == PATCHED_BODY

            reuse_responses = make_logistics_responses_for_order(order_id_1)
            out_reuse = new_runtime.run_agent_with_pi(
                run_id="run_future_reuse_1",
                input_text=reuse_query,
                tools=["query_order_packages", "query_package_tracking"],
                test_mode=True,
                model_config={"provider": "faux", "responses": reuse_responses},
            )
            calls_reuse = new_runtime.list_tool_calls("run_future_reuse_1")
            oracle_reuse = verify_logistics_fulfillment(model_output=out_reuse, order_id=order_id_1, tool_records=calls_reuse)
            assert oracle_reuse["independent_pass"] is True, "Expected future reuse to pass oracle"
            final_run_rec_reuse, ep_reuse = new_runtime.finalize_run(
                run_id="run_future_reuse_1",
                model_output=out_reuse,
                verification_evidence=oracle_reuse,
            )
            assert ep_reuse is not None and ep_reuse.outcome == "success"
            assert ep_reuse.skill_version == "1.0.1"

            # Check old run frozen at 1.0.0
            old_run1 = new_runtime.get_run("run_learning_fail_1")
            assert old_run1 is not None and old_run1.skill_version == "1.0.0"
            assert old_run1.frozen_body == BASELINE_BODY

            cases_report.append({
                "run_id": "run_future_reuse_1",
                "task_id": "task_future_reuse_1",
                "status": final_run_rec_reuse.status,
                "purpose": "learning",
                "skill_name": "logistics_query",
                "skill_version": "1.0.1",
                "response_text": out_reuse,
                "content_hash": final_run_rec_reuse.content_hash,
                "frozen_body": final_run_rec_reuse.frozen_body,
                "episode_id": ep_reuse.episode_id,
                "episode_outcome": ep_reuse.outcome,
                "oracle": oracle_reuse,
                "tool_records": [_serialize_tool_record(t) for t in calls_reuse],
                "model": "scripted / faux",
                "evidence": common_evidence,
                "usage": None,
            })
        finally:
            new_runtime.close()

        # Phase 7: Atomic report.json assembly
        checkpoint("REPORT_GENERATION")
        report_data = {
            "status": "SUCCESS",
            "scenario": "dashboard_evolution_demo",
            "demo_id": demo_uuid,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "evidence": common_evidence,
            "model": "scripted / faux",
            "cases": cases_report,
            "lineage": {
                "baseline_version": "1.0.0",
                "baseline_body": BASELINE_BODY,
                "source_episode_ids": [ep_1.episode_id, ep_2.episode_id],
                "job_id": job.job_id,
                "candidate_id": job.latest_candidate.candidate_id,
                "validation_record_id": val_rec.record_id,
                "release_id": release.release_id,
                "future_reuse_run_id": "run_future_reuse_1",
                "future_reuse_episode_id": ep_reuse.episode_id,
                "promoted_version": "1.0.1",
                "promoted_body": PATCHED_BODY,
                "patcher": {
                    "mode": "scripted_fixed_text",
                    "call_count": len(patcher.calls),
                    "input_summaries": [c[:200] for c in patcher.calls],
                },
                "promotion_gate": {
                    "caller_confirmed_false_rejected": True,
                    "rejection_message": rejection_reason,
                    "caller_confirmed_true_promoted": True,
                    "target_release_id": release.release_id,
                    "scope_type": "isolated_demo_repository",
                },
                "regression_verification": {
                    "cases_evaluated": [c["id"] for c in eval_cases],
                    "ratchet_decision": val_rec.ratchet_decision,
                },
                "verification_boundary": {
                    "is_isolated_demo": True,
                    "not_a_production_release": True,
                    "git_commit_local_only": True,
                    "real_llm": False,
                    "os_sandbox_verified": False,
                    "scripted_outputs_do_not_measure_skill_effectiveness": True,
                    "baseline_is_seeded_fixture": True,
                },
            },
        }

        _write_report(demo_dir, report_data)

        return {
            "status": "SUCCESS",
            "demo_dir": demo_dir,
            "report_path": demo_dir / "report.json",
            "job_id": job.job_id,
            "candidate_id": job.latest_candidate.candidate_id,
            "validation_record_id": val_rec.record_id,
            "release_id": release.release_id,
            "source_episode_ids": [ep_1.episode_id, ep_2.episode_id],
            "normal_episode_id": ep_norm.episode_id,
            "future_reuse_episode_id": ep_reuse.episode_id,
            "future_reuse_run_id": "run_future_reuse_1",
            "report": report_data,
        }

    except Exception as exc:
        failed_report = {
            "status": "FAILED",
            "scenario": "dashboard_evolution_demo",
            "demo_id": demo_uuid,
            "failed_phase": current_phase,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "evidence": common_evidence,
            "cases": cases_report,
        }
        try:
            _write_report(demo_dir, failed_report)
        except Exception:
            pass
        raise
    finally:
        for resource in (runtime, sm, cand_store, episode_store, registry):
            if resource is not None:
                resource.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run D4a dashboard evolution demo scenario.")
    parser.add_argument("--output-dir", type=Path, default=Path("runs"), help="Parent directory for demo runs.")
    args = parser.parse_args()
    try:
        res = run_dashboard_evolution_demo(output_dir=args.output_dir)
        print(f"D4a evolution demo completed successfully: {res['demo_dir']}")
        sys.exit(0)
    except Exception as err:
        print(f"D4a evolution demo failed: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
