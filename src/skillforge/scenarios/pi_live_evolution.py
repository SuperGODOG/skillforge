"""D5b: Prospective live Pi generation, revision, and partition benchmark scenario driver.

Invariants:
1. Strict protocol verification before any LLM/provider invocation (fail-closed on drift/tampering/lineage leak).
2. Shared PersistentLedger atomically syncing ledger.json before and after every provider call attempt.
3. Re-run prevention: refuses execution if report.json already exists or ledger.json contains existing records.
4. Generates V1 via generate_candidate_from_requirement with pure-document no-tools scope.
5. Loads V1 as experimental comparison baseline in isolated registry without forging release or validation pass.
6. Executes DEV A (bare) and DEV B (V1) with AgentRuntime.run_agent_with_pi(test_mode=False) and authoritative logistics oracle.
7. Triggers repair_skill_failure if and only if DEV B has skill-attributable failures (eval_cases strictly DEV only).
8. Freezes candidate hashes into freeze.json before executing any LOCKED_EVAL tasks.
9. Executes LOCKED_EVAL partition (strictly evaluation purpose, never leaking into generation or repair).
10. Generates comprehensive, dashboard-compatible report.json with full partitions, lineage, cases, and summaries.
11. Safe execution: no real money billing assumptions (cost=null if unbilled), no production modifications.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional
import uuid

import yaml

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator import SkillEvaluator
from skillforge.evaluator.llm_factory import BudgetExceededError, LLMCallRecord, LLMLedger
from skillforge.evolution_loop import compute_candidate_hash
from skillforge.models import CandidateSkill, EvolveBudget, SkillMeta
from skillforge.pi_evaluation import PiEvaluationBackend
from skillforge.pi_text_client import FORBIDDEN_CONFIG_KEYS, PiTextClient, _standard_usage
from skillforge.registry import SkillRegistry
from skillforge.repair import repair_skill_failure
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    MOCK_ORDERS, MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    get_refund_call_count,
    reset_refund_call_count,
    verify_logistics_fulfillment,
)
from skillforge.skill_generator import GenerationFailure, generate_candidate_from_requirement


class PersistentLedger(LLMLedger):
    """Subclass of LLMLedger that atomically persists ledger.json on every state change."""

    def __init__(self, budget: Optional[EvolveBudget] = None, ledger_path: Optional[Path] = None) -> None:
        super().__init__(budget=budget)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self._flush()

    def _flush(self) -> None:
        if not self.ledger_path:
            return
        data = {
            "start_time": self.start_time,
            "budget": {
                "max_calls": self.budget.max_calls,
                "max_tokens": self.budget.max_tokens,
                "deadline_seconds": self.budget.deadline_seconds,
            },
            "summary": self.as_dict(),
            "records": [dataclasses.asdict(r) for r in self.records],
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.ledger_path)

    def reserve_turn(self, role: str = "agent", call_id: Optional[str] = None) -> LLMCallRecord:
        rec = super().reserve_turn(role=role, call_id=call_id)
        self._flush()
        return rec

    def settle_turn(
        self,
        call_id: str,
        usage: Optional[dict[str, Any]] = None,
        latency_ms: float = 0.0,
        status: str = "success",
        error_type: Optional[str] = None,
    ) -> Optional[LLMCallRecord]:
        rec = super().settle_turn(
            call_id=call_id, usage=_standard_usage(usage), latency_ms=latency_ms, status=status, error_type=error_type
        )
        self._flush()
        return rec

    def cancel_pending(
        self,
        call_id: Optional[str] = None,
        error_type: str = "CANCELLED",
    ) -> list[LLMCallRecord]:
        recs = super().cancel_pending(call_id=call_id, error_type=error_type)
        self._flush()
        return recs


def verify_protocol(protocol_path: Path) -> tuple[dict[str, Any], str]:
    """Verify protocol file rawbytes hash, schema bounds, disjoint partitions, and oracle source hash."""
    protocol_path = Path(protocol_path).resolve()
    if not protocol_path.is_file():
        raise FileNotFoundError(f"Protocol file not found: {protocol_path}")

    raw_bytes = protocol_path.read_bytes()
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()

    sha256_path = protocol_path.with_name("protocol.sha256")
    if not sha256_path.is_file():
        sha256_path = protocol_path.parent / f"{protocol_path.name}.sha256"
        if not sha256_path.is_file():
            raise ValueError(f"Missing protocol sha256 file: {sha256_path}")

    expected_hash = sha256_path.read_text(encoding="utf-8").strip()
    if raw_hash != expected_hash:
        raise ValueError(
            f"Protocol rawbytes hash verification failed: actual {raw_hash} != expected {expected_hash}"
        )

    protocol = json.loads(raw_bytes.decode("utf-8"))
    if protocol.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema_version: {protocol.get('schema_version')}")

    budget_cfg = protocol.get("budget", {})
    max_calls = budget_cfg.get("max_provider_calls", 0)
    deadline_sec = budget_cfg.get("deadline_seconds", 0)
    if max_calls > 200:
        raise ValueError(f"Protocol max_provider_calls exceeds safety limit: {max_calls} > 200")
    if deadline_sec > 3600:
        raise ValueError(f"Protocol deadline_seconds exceeds safety limit: {deadline_sec} > 3600")
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
        raise ValueError("max_provider_calls must be a positive integer")
    if isinstance(deadline_sec, bool) or not isinstance(deadline_sec, (int, float)) or not 0 < deadline_sec <= 3600:
        raise ValueError("deadline_seconds must be positive and at most 3600")
    token_limit = budget_cfg.get("max_total_tokens")
    if isinstance(token_limit, bool) or not isinstance(token_limit, int) or not 0 < token_limit <= 400000:
        raise ValueError("max_total_tokens must be in [1, 400000]")

    partitions = protocol.get("partitions", {})
    dev_tasks = partitions.get("DEV", [])
    locked_tasks = partitions.get("LOCKED_EVAL", [])
    if not dev_tasks or not locked_tasks:
        raise ValueError("Protocol must have non-empty DEV and LOCKED_EVAL partitions")

    dev_task_ids = {str(t.get("task_id")) for t in dev_tasks if t.get("task_id")}
    locked_task_ids = {str(t.get("task_id")) for t in locked_tasks if t.get("task_id")}
    dev_source_ids = {str(t.get("source_task_id")) for t in dev_tasks if t.get("source_task_id")}
    locked_source_ids = {str(t.get("source_task_id")) for t in locked_tasks if t.get("source_task_id")}
    dev_root_families = {str(t.get("root_family")) for t in dev_tasks if t.get("root_family")}
    locked_root_families = {str(t.get("root_family")) for t in locked_tasks if t.get("root_family")}
    dev_derived = {str(d) for t in dev_tasks for d in t.get("derived_from", [])}
    locked_derived = {str(d) for t in locked_tasks for d in t.get("derived_from", [])}

    if dev_task_ids & locked_task_ids:
        raise ValueError(f"Cross-split task_id collision: {dev_task_ids & locked_task_ids}")
    if dev_source_ids & locked_source_ids:
        raise ValueError(f"Cross-split source_task_id collision: {dev_source_ids & locked_source_ids}")
    if dev_root_families & locked_root_families:
        raise ValueError(f"Cross-split root_family collision: {dev_root_families & locked_root_families}")
    if (dev_task_ids | dev_source_ids | dev_derived) & (locked_task_ids | locked_source_ids | locked_derived):
        raise ValueError(
            f"Cross-split derived lineage collision: {(dev_task_ids | dev_derived) & (locked_task_ids | locked_derived)}"
        )

    oracle_cfg = protocol.get("oracle", {})
    oracle_fn = oracle_cfg.get("function")
    expected_oracle_sha = oracle_cfg.get("source_sha256")
    if not oracle_fn or not expected_oracle_sha:
        raise ValueError("Missing oracle specification in protocol")

    mod_name, fn_name = oracle_fn.rsplit(".", 1)
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:
        raise ValueError(f"Failed to import oracle module '{mod_name}': {exc}")

    mod_file = Path(mod.__file__).resolve()
    actual_oracle_sha = hashlib.sha256(mod_file.read_bytes()).hexdigest()
    if actual_oracle_sha != expected_oracle_sha:
        raise ValueError(
            f"Oracle source hash drift: actual {actual_oracle_sha} != expected {expected_oracle_sha}"
        )
    if oracle_fn != "skillforge.scenarios.logistics.verify_logistics_fulfillment":
        raise ValueError("Only the frozen logistics business oracle is supported")
    if set(protocol.get("tool_allowlist", [])) != {"query_order_packages", "query_package_tracking"}:
        raise ValueError("D5 requires the two fixed read-only logistics tools")
    for task in dev_tasks + locked_tasks:
        if task.get("expected_tool_snapshot"):
            order = MOCK_ORDERS[task["order_id"]]
            actual = {"order": order, "packages": {p: MOCK_PACKAGES[p] for p in order["packages"]}}
            if actual != task["expected_tool_snapshot"]:
                raise ValueError("Synthetic tool snapshot drift")

    model_cfg = protocol.get("model_config", {})
    for k in model_cfg:
        if k.lower() in FORBIDDEN_CONFIG_KEYS:
            raise ValueError(f"Forbidden key in protocol model_config: {k}")
    PiTextClient(model_cfg)  # Validate public options without launching a runner.

    return protocol, raw_hash


def _serialize_tool_record(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        return t
    return {
        "call_id": getattr(t, "call_id", None),
        "tool_name": getattr(t, "tool_name", None),
        "parameters": getattr(t, "input_params", {}),
        "output_text": getattr(t, "output_text", None),
        "status": getattr(t, "status", None),
        "error_type": getattr(t, "error_type", None),
        "error_message": getattr(t, "error_message", None),
    }


def _run_single_task(
    runtime: AgentRuntime,
    task: dict[str, Any],
    candidate: Optional[CandidateSkill],
    group_label: str,
    partition_name: str,
    purpose: str,
    ledger: PersistentLedger,
    allowlist: list[str],
    system_prompt_header: str,
    model_config: dict[str, Any],
    timeout_s: float,
    test_mode: bool = False,
    extra_failing_packages: Optional[set[str]] = None,
    tracking_tool: Optional[QueryPackageTrackingTool] = None,
) -> tuple[dict[str, Any], dict[str, Any], Optional[Any]]:
    """Execute a single task run with AgentRuntime and verify with independent oracle."""
    run_id = f"run_{partition_name.lower()}_{group_label.lower()}_{task['task_id']}_{uuid.uuid4().hex[:8]}"
    ledger.check_budget(role="agent")

    if tracking_tool and extra_failing_packages is not None:
        tracking_tool.simulated_failing_packages = set(extra_failing_packages)

    skill_name = candidate.skill_name if candidate else None
    runtime.start_run(
        run_id=run_id,
        task_id=task["task_id"],
        skill_name=skill_name,
        candidate=candidate,
        purpose=purpose,
        budget_max=10,
        task_description=task["query"],
    )

    reserved_turn_ids: dict[tuple[str, int], str] = {}
    last_usage: Optional[dict[str, Any]] = None

    def admission_cb(r_id: str, t_idx: int) -> dict[str, Any]:
        if r_id != run_id:
            return {"allowed": False, "error_type": "CROSS_RUN_MISMATCH"}
        key = (r_id, t_idx)
        if key not in reserved_turn_ids:
            cid = f"{r_id}:{t_idx}"
            ledger.reserve_turn(role="agent", call_id=cid)
            reserved_turn_ids[key] = cid
        return {"allowed": True}

    def receipt_cb(frame: dict[str, Any]) -> None:
        nonlocal last_usage
        r_id = frame.get("run_id")
        t_idx = frame.get("turn_index")
        if r_id != run_id or t_idx is None:
            return
        key = (r_id, t_idx)
        cid = reserved_turn_ids.pop(key, None)
        if not cid:
            return
        usage = frame.get("usage")
        last_usage = usage
        status = frame.get("status", "success")
        error_type = frame.get("error")
        ledger.settle_turn(call_id=cid, usage=usage, status=status, error_type=error_type)

    t0 = time.perf_counter()
    out_text = ""
    infra_error: Optional[str] = None
    try:
        out_text = runtime.run_agent_with_pi(
            run_id=run_id,
            input_text=task["query"],
            candidate=candidate,
            tools=allowlist,
            system_prompt_header=system_prompt_header,
            model_config=model_config,
            test_mode=test_mode,
            timeout=timeout_s,
            model_turn_admission=admission_cb,
            model_turn_receipt=receipt_cb,
        )
    except Exception as exc:
        infra_error = f"{type(exc).__name__}: {exc}"
    finally:
        for cid in list(reserved_turn_ids.values()):
            ledger.cancel_pending(call_id=cid, error_type="RUN_TERMINATED_UNKNOWN")
        reserved_turn_ids.clear()
        if tracking_tool:
            tracking_tool.simulated_failing_packages = set()

    latency_ms = (time.perf_counter() - t0) * 1000

    calls = runtime.list_tool_calls(run_id)
    oracle = verify_logistics_fulfillment(
        model_output=out_text,
        order_id=task["order_id"],
        tool_records=calls,
        intent_constraint=task.get("intent_constraint"),
        expected_permission_denial=bool(task.get("expected_permission_denial", False)),
        expect_tool_failure=bool(task.get("expect_tool_failure", False)),
        failing_package_ids=task.get("failing_package_ids"),
        infra_error=infra_error,
    )

    run_rec, ep = runtime.finalize_run(
        run_id=run_id,
        model_output=out_text,
        verification_evidence=oracle,
        infra_error=infra_error,
    )

    records = [r for r in ledger.records if r.call_id and r.call_id.startswith(run_id + ":")]
    known_usage = records and all(not r.usage_unknown for r in records)
    usage = {"prompt_tokens": sum(r.prompt_tokens for r in records) if known_usage else None,
             "completion_tokens": sum(r.completion_tokens for r in records) if known_usage else None,
             "total_tokens": sum(r.total_tokens for r in records) if known_usage else None, "cost": None}
    task_group_entry = {
        "passed": oracle["independent_pass"],
        "output_text": out_text,
        "tool_calls_count": len(calls),
        "tools": [getattr(c, "tool_name", "") for c in calls],
        "latency_ms": round(latency_ms, 2),
        "usage": usage,
        "provider_calls": len(records),
        "failure_reason": oracle.get("failure_reason"),
        "classification": oracle.get("classification"),
        "run_id": run_id,
        "episode_id": ep.episode_id if ep else None,
        "skill_hash": compute_candidate_hash(candidate) if candidate else None,
    }

    run_data = {
        "run_id": run_id,
        "task_id": task["task_id"],
        "group": group_label,
        "partition": partition_name,
        "status": run_rec.status if run_rec else ("FAILED" if infra_error else "UNKNOWN"),
        "purpose": purpose,
        "skill_name": candidate.skill_name if candidate else None,
        "skill_version": getattr(candidate.meta, "version", None) if candidate else None,
        "candidate_id": candidate.candidate_id if candidate else None,
        "response_text": out_text,
        "content_hash": run_rec.content_hash if run_rec else None,
        "frozen_body": run_rec.frozen_body if run_rec else None,
        "episode_id": ep.episode_id if ep else None,
        "episode_outcome": ep.outcome if ep else None,
        "oracle": oracle,
        "tool_records": [_serialize_tool_record(t) for t in calls],
        "model": model_config.get("model_id"),
        "evidence": {
            "tier": "real_llm_synthetic_business",
            "model": model_config.get("model_id"),
            "provider": model_config.get("provider"),
            "data": "synthetic",
            "execution": "official_pi_runner_with_agent_runtime",
            "os_sandbox": "unverified",
            "cost": None,
        },
        "usage": usage,
        "provider_calls": [dataclasses.asdict(r) for r in records],
        "real_llm": not test_mode,
        "latency_ms": round(latency_ms, 2),
        "created_at": run_rec.created_at if run_rec else datetime.now(timezone.utc).isoformat(),
    }

    return run_data, task_group_entry, ep


def compute_partition_summary(tasks: list[dict[str, Any]], sample_size: int = 3) -> dict[str, Any]:
    """Compute standard A/B/C and paired B->C metric summary for a partition."""
    total = max(len(tasks), sample_size)
    summary: dict[str, Any] = {}
    for group in ("A", "B", "C"):
        recorded_bools = [
            t[group]["passed"]
            for t in tasks
            if t.get(group) and isinstance(t[group].get("passed"), bool)
        ]
        passed_count = sum(1 for p in recorded_bools if p is True)
        summary[group] = {
            "passed": passed_count,
            "recorded": len(recorded_bools),
            "total": total,
            "unknown": total - len(recorded_bools),
        }

    paired = {"improved": 0, "degraded": 0, "unchanged": 0, "unknown": 0}
    for t in tasks:
        b_p = t.get("B", {}).get("passed")
        c_p = t.get("C", {}).get("passed")
        if not isinstance(b_p, bool) or not isinstance(c_p, bool):
            paired["unknown"] += 1
        elif b_p == c_p:
            paired["unchanged"] += 1
        elif c_p and not b_p:
            paired["improved"] += 1
        else:
            paired["degraded"] += 1
    paired["unknown"] += max(0, total - len(tasks))
    summary["paired_b_to_c"] = paired
    return summary


def execute_pi_live_evolution(protocol_path: Path, test_mode: Optional[bool] = None) -> dict[str, Any]:
    """Execute live Pi forward-looking generation and evolution benchmark per protocol."""
    protocol_path = Path(protocol_path).resolve()
    exp_dir = protocol_path.parent.resolve()

    protocol, protocol_hash = verify_protocol(protocol_path)

    report_path = exp_dir / "report.json"
    ledger_path = exp_dir / "ledger.json"
    freeze_path = exp_dir / "freeze.json"

    if report_path.exists():
        raise RuntimeError(f"Experiment report already exists at {report_path}; refusing to re-run.")
    if ledger_path.exists():
        try:
            raw_l = json.loads(ledger_path.read_text(encoding="utf-8"))
            if raw_l.get("records") or raw_l.get("summary", {}).get("total_calls", 0) > 0:
                raise RuntimeError(f"Non-empty ledger already exists at {ledger_path}; refusing to re-run.")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f"Existing ledger at {ledger_path} is unreadable/corrupt; refusing to re-run.")

    budget_cfg = protocol["budget"]
    evolve_budget = EvolveBudget(
        max_calls=budget_cfg.get("max_provider_calls", 200),
        max_tokens=budget_cfg.get("max_total_tokens", 400000),
        deadline_seconds=budget_cfg.get("deadline_seconds", 3600),
    )
    ledger = PersistentLedger(budget=evolve_budget, ledger_path=ledger_path)

    runs_dir = exp_dir
    skills_dir = exp_dir / "skills"
    calls_dir = exp_dir / "calls"
    runs_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    calls_dir.mkdir(parents=True, exist_ok=True)

    runtime_db = runs_dir / "runtime.db"
    episodes_db = runs_dir / "episodes.db"
    candidates_db = runs_dir / "candidates.db"
    skills_db = runs_dir / "skills.db"

    registry = SkillRegistry(db_path=skills_db, skills_dir=skills_dir, repo_root=exp_dir)
    episode_store = EpisodeStore(db_path=episodes_db)
    candidate_store = CandidateStore(db_path=candidates_db, episode_store=episode_store)
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    allowlist_tools = list(protocol.get("tool_allowlist", ["query_order_packages", "query_package_tracking"]))
    broker = ToolBroker(application_allowlist=set(allowlist_tools))
    broker.register_tool(QueryOrderPackagesTool())
    tracking_tool = QueryPackageTrackingTool()
    broker.register_tool(tracking_tool)

    runtime = AgentRuntime(
        db_path=runtime_db,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
        candidate_store=candidate_store,
    )

    cases_report: list[dict[str, Any]] = []
    current_phase = "INIT"
    v1_candidate: Optional[CandidateSkill] = None
    v1_hash: Optional[str] = None
    c_candidate: Optional[CandidateSkill] = None
    repair_job_obj: Optional[Any] = None
    repair_status: Optional[str] = None
    freeze_time: Optional[str] = None
    dev_task_rows: list[dict[str, Any]] = []
    locked_task_rows: list[dict[str, Any]] = []

    model_config = protocol["model_config"]
    timeout_s = float(model_config.get("timeout_ms", 120000)) / 1000.0
    effective_test_mode = False if test_mode is None else bool(test_mode)

    def write_report_checkpoint(status: str, error_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        dev_summary = compute_partition_summary(dev_task_rows, sample_size=len(protocol["partitions"]["DEV"]))
        locked_summary = compute_partition_summary(
            locked_task_rows, sample_size=len(protocol["partitions"]["LOCKED_EVAL"])
        )
        infra_errors_count = sum(1 for c in cases_report if c.get("oracle", {}).get("is_infra_error"))

        report_payload = {
            "status": status,
            "phase": current_phase,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "experiment_name": "D5 · Pi真实生成与演进",
                "resolved_model": model_config.get("model_id"),
                "evidence_tier": "real_llm_synthetic_business",
                "protocol_hash": protocol_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            "partitions": {
                "DEV": {
                    "sample_size": len(protocol["partitions"]["DEV"]),
                    "tasks": dev_task_rows,
                    "summary": dev_summary,
                },
                "LOCKED_EVAL": {
                    "sample_size": len(protocol["partitions"]["LOCKED_EVAL"]),
                    "tasks": locked_task_rows,
                    "summary": locked_summary,
                },
            },
            "cases": cases_report,
            "skills": {
                "V1": {
                    "candidate_id": v1_candidate.candidate_id if v1_candidate else None,
                    "skill_name": v1_candidate.skill_name if v1_candidate else None,
                    "version": getattr(v1_candidate.meta, "version", "1.0.0") if v1_candidate else None,
                    "content_hash": v1_hash,
                    "body": v1_candidate.body if v1_candidate else None,
                    "source_requirement": v1_candidate.source_requirement if v1_candidate else None,
                    "source_episode_ids": v1_candidate.source_episode_ids if v1_candidate else [],
                }
                if v1_candidate
                else None,
                "C": {
                    "candidate_id": c_candidate.candidate_id,
                    "parent_candidate_id": c_candidate.parent_candidate_id,
                    "skill_name": c_candidate.skill_name,
                    "version": getattr(c_candidate.meta, "version", "1.0.1"),
                    "content_hash": compute_candidate_hash(c_candidate),
                    "body": c_candidate.body,
                    "source_requirement": c_candidate.source_requirement,
                    "source_episode_ids": c_candidate.source_episode_ids,
                    "ratchet_status": repair_job_obj.status if repair_job_obj else None,
                }
                if c_candidate
                else None,
            },
            "lineage": {
                "protocol_path": str(protocol_path),
                "protocol_sha256": protocol_hash,
                "v1_candidate_id": v1_candidate.candidate_id if v1_candidate else None,
                "v1_hash": v1_hash,
                "v1_generation_call_ids": [r.call_id for r in ledger.records if r.role == "generation"],
                "c_candidate_id": c_candidate.candidate_id if c_candidate else None,
                "c_hash": compute_candidate_hash(c_candidate) if c_candidate else None,
                "c_repair_job_id": getattr(repair_job_obj, "job_id", None) if repair_job_obj else None,
                "c_source_episode_ids": c_candidate.source_episode_ids if c_candidate else [],
                "c_repair_call_ids": [r.call_id for r in ledger.records if r.role == "repair"],
                "freeze_time": freeze_time,
                "experimental_baseline_candidate_id": v1_candidate.candidate_id if v1_candidate else None,
                "baseline_policy": "Generated V1 loaded only as a private comparison baseline; not validated promotion or production release",
            },
            "summary": {
                "total_calls": ledger.total_calls,
                "failed_calls": ledger.failed_calls,
                "total_tokens": ledger.total_tokens if not ledger.has_unknown_tokens else None,
                "tokens_by_role": ledger.tokens_by_role,
                "calls_by_role": ledger.calls_by_role,
                "cost": None,
                "dev_summary": dev_summary,
                "locked_eval_summary": locked_summary,
                "infra_errors_count": infra_errors_count,
            },
        }
        if error_info:
            report_payload["error"] = error_info

        tmp = report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(report_path)
        print(json.dumps({"phase": current_phase, "status": status,
                          "provider_calls": ledger.total_calls, "saved_runs": len(cases_report)}), flush=True)
        return report_payload

    try:
        # Phase 1: Generate V1 Candidate
        current_phase = "GENERATION_V1"
        write_report_checkpoint("RUNNING")

        gen_client = PiTextClient(
            model_config=model_config,
            ledger=ledger,
            role="generation",
            artifact_dir=calls_dir,
            timeout=timeout_s,
        )
        gen_budget = EvolveBudget(max_calls=3, max_tokens=30000, deadline_seconds=360)

        v1_result = generate_candidate_from_requirement(
            request=protocol["requirement"],
            candidate_store=candidate_store,
            repo_root=exp_dir,
            llm=gen_client,
            max_retries=1,
            budget=gen_budget,
            purpose="learning",
        )

        if isinstance(v1_result, GenerationFailure):
            return write_report_checkpoint(
                "FAILED",
                error_info={
                    "stage": "GENERATION_V1",
                    "reason": v1_result.reason,
                    "message": v1_result.message,
                    "details": v1_result.details,
                },
            )

        v1_candidate = v1_result
        v1_hash = compute_candidate_hash(v1_candidate)

        # Baseline registration in isolated comparison registry
        v1_dir = skills_dir / v1_candidate.skill_name
        v1_dir.mkdir(parents=True, exist_ok=True)
        frontmatter_dict = v1_candidate.meta.model_dump(exclude_none=True)
        frontmatter_dict["version"] = frontmatter_dict.get("version") or "1.0.0"
        skill_md = f"---\n{yaml.safe_dump(frontmatter_dict, sort_keys=False)}---\n{v1_candidate.body.strip()}\n"
        (v1_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        registry.load_skills_from_dir()

        # Phase 2: Execute DEV Partition (A and B)
        current_phase = "DEV_EXECUTION"
        write_report_checkpoint("RUNNING")

        dev_tasks = protocol["partitions"]["DEV"]
        dev_b_episodes = []

        for task in dev_tasks:
            t_row = {
                "task_id": task["task_id"],
                "task_family": task.get("task_family"),
                "root_family": task.get("root_family"),
                "query": task["query"],
                "A": {"passed": None},
                "B": {"passed": None},
                "C": {"passed": None},
            }

            dev_task_rows.append(t_row)
            # Run DEV A (bare, no skill)
            run_data_a, entry_a, ep_a = _run_single_task(
                runtime=runtime,
                task=task,
                candidate=None,
                group_label="A",
                partition_name="DEV",
                purpose="learning",
                ledger=ledger,
                allowlist=allowlist_tools,
                system_prompt_header=protocol["system_prompt_header"],
                model_config=model_config,
                timeout_s=timeout_s,
                test_mode=effective_test_mode,
                tracking_tool=tracking_tool,
            )
            cases_report.append(run_data_a)
            t_row["A"] = entry_a
            write_report_checkpoint("RUNNING")

            # Run DEV B (with V1)
            run_data_b, entry_b, ep_b = _run_single_task(
                runtime=runtime,
                task=task,
                candidate=v1_candidate,
                group_label="B",
                partition_name="DEV",
                purpose="learning",
                ledger=ledger,
                allowlist=allowlist_tools,
                system_prompt_header=protocol["system_prompt_header"],
                model_config=model_config,
                timeout_s=timeout_s,
                test_mode=effective_test_mode,
                tracking_tool=tracking_tool,
            )
            cases_report.append(run_data_b)
            t_row["B"] = entry_b
            write_report_checkpoint("RUNNING")
            if ep_b:
                dev_b_episodes.append(ep_b)

            write_report_checkpoint("RUNNING")

        # Phase 3: Repair Decision and Execution
        current_phase = "REPAIR_EVALUATION"
        write_report_checkpoint("RUNNING")

        failed_b_episodes = [
            episode_store.get_episode(ep.episode_id)
            for ep in dev_b_episodes
            if ep.outcome == "failure"
        ]
        failed_b_episodes = [ep for ep in failed_b_episodes if ep is not None]

        if not failed_b_episodes:
            repair_status = "NO_REPAIR_NEEDED"
            c_candidate = None
            repair_job_obj = None
        else:
            # DEV B had failures; execute repair
            repair_llm = PiTextClient(
                model_config=model_config,
                ledger=ledger,
                role="repair",
                artifact_dir=calls_dir,
                timeout=timeout_s,
            )
            judge_llm = PiTextClient(
                model_config=model_config,
                ledger=ledger,
                role="judge",
                artifact_dir=calls_dir,
                timeout=timeout_s,
            )
            backend = PiEvaluationBackend(
                runtime=runtime,
                tool_names=allowlist_tools,
                model_config=model_config,
                test_mode=effective_test_mode,
                budget_max=10,
                system_prompt_header=protocol["system_prompt_header"],
                ledger=ledger,
            )
            evaluator = SkillEvaluator(
                registry=registry,
                llm=repair_llm,
                judge_llm=judge_llm,
                execution_backend=backend,
                ledger=None,  # ledger=None to prevent double-counting with PiTextClient
            )
            eval_cases = [
                {
                    "id": t["task_id"],
                    "query": t["query"],
                    "oracle": "logistics",
                    "order_id": t["order_id"],
                    "skill": v1_candidate.skill_name,
                    "intent_constraint": t.get("intent_constraint"),
                }
                for t in dev_tasks
            ]
            repair_job_obj = repair_skill_failure(
                episodes=failed_b_episodes,
                skill_name=v1_candidate.skill_name,
                episode_store=episode_store,
                candidate_store=candidate_store,
                registry=registry,
                evaluator=evaluator,
                eval_cases=eval_cases,
                llm=repair_llm,
                diagnostic_llm=repair_llm,
                max_attempts=min(2, budget_cfg.get("max_repair_attempts", 2)),
            )
            repair_status = repair_job_obj.status
            c_candidate = repair_job_obj.latest_candidate

        # Phase 4: Candidate Hash Freeze before LOCKED tasks
        current_phase = "FREEZE_CANDIDATES"
        freeze_time = datetime.now(timezone.utc).isoformat()
        freeze_payload = {
            "protocol_sha256": protocol_hash,
            "v1_hash": v1_hash,
            "v1_candidate_id": v1_candidate.candidate_id if v1_candidate else None,
            "c_hash": compute_candidate_hash(c_candidate) if c_candidate else None,
            "c_candidate_id": c_candidate.candidate_id if c_candidate else None,
            "repair_status": repair_status,
            "generation_call_ids": [r.call_id for r in ledger.records if r.role == "generation"],
            "repair_call_ids": [r.call_id for r in ledger.records if r.role == "repair"],
            "frozen_at": freeze_time,
        }
        freeze_tmp = freeze_path.with_suffix(".tmp")
        freeze_tmp.write_text(json.dumps(freeze_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        freeze_tmp.replace(freeze_path)
        write_report_checkpoint("RUNNING")

        def check_frozen():
            if compute_candidate_hash(v1_candidate) != freeze_payload["v1_hash"]:
                raise ValueError("V1 content drift after freeze")
            if c_candidate and compute_candidate_hash(c_candidate) != freeze_payload["c_hash"]:
                raise ValueError("C content drift after freeze")
            _, current_hash = verify_protocol(protocol_path)
            if current_hash != protocol_hash:
                raise ValueError("Protocol drift after candidate freeze")

        # Phase 5: DEV C Execution (if C candidate exists)
        if c_candidate:
            current_phase = "DEV_C_EXECUTION"
            for t_row, task in zip(dev_task_rows, dev_tasks):
                check_frozen()
                run_data_c, entry_c, _ = _run_single_task(
                    runtime=runtime,
                    task=task,
                    candidate=c_candidate,
                    group_label="C",
                    partition_name="DEV",
                    purpose="learning",
                    ledger=ledger,
                    allowlist=allowlist_tools,
                    system_prompt_header=protocol["system_prompt_header"],
                    model_config=model_config,
                    timeout_s=timeout_s,
                    test_mode=effective_test_mode,
                    tracking_tool=tracking_tool,
                )
                cases_report.append(run_data_c)
                t_row["C"] = entry_c
                write_report_checkpoint("RUNNING")
                write_report_checkpoint("RUNNING")

        # Phase 6: LOCKED Partition Execution
        current_phase = "LOCKED_EXECUTION"
        write_report_checkpoint("RUNNING")

        locked_tasks = protocol["partitions"]["LOCKED_EVAL"]
        for task in locked_tasks:
            t_row = {
                "task_id": task["task_id"],
                "task_family": task.get("task_family"),
                "root_family": task.get("root_family"),
                "query": task["query"],
                "A": {"passed": None},
                "B": {"passed": None},
                "C": {"passed": None},
            }

            failing_pkgs = set(task.get("failing_package_ids", [])) if task.get("expect_tool_failure") else None
            reset_refund_call_count()

            locked_task_rows.append(t_row)
            check_frozen()
            # Run LOCKED A
            run_data_a, entry_a, _ = _run_single_task(
                runtime=runtime,
                task=task,
                candidate=None,
                group_label="A",
                partition_name="LOCKED_EVAL",
                purpose="evaluation",
                ledger=ledger,
                allowlist=allowlist_tools,
                system_prompt_header=protocol["system_prompt_header"],
                model_config=model_config,
                timeout_s=timeout_s,
                test_mode=effective_test_mode,
                extra_failing_packages=failing_pkgs,
                tracking_tool=tracking_tool,
            )
            cases_report.append(run_data_a)
            t_row["A"] = entry_a
            write_report_checkpoint("RUNNING")
            if task.get("expected_permission_denial"):
                if get_refund_call_count() > 0:
                    entry_a["passed"] = False
                    entry_a["failure_reason"] = "Refund write executed despite permission restrictions"

            check_frozen()
            # Run LOCKED B (with V1)
            reset_refund_call_count()
            run_data_b, entry_b, _ = _run_single_task(
                runtime=runtime,
                task=task,
                candidate=v1_candidate,
                group_label="B",
                partition_name="LOCKED_EVAL",
                purpose="evaluation",
                ledger=ledger,
                allowlist=allowlist_tools,
                system_prompt_header=protocol["system_prompt_header"],
                model_config=model_config,
                timeout_s=timeout_s,
                test_mode=effective_test_mode,
                extra_failing_packages=failing_pkgs,
                tracking_tool=tracking_tool,
            )
            cases_report.append(run_data_b)
            t_row["B"] = entry_b
            write_report_checkpoint("RUNNING")
            if task.get("expected_permission_denial"):
                if get_refund_call_count() > 0:
                    entry_b["passed"] = False
                    entry_b["failure_reason"] = "Refund write executed despite permission restrictions"

            # Run LOCKED C (if C candidate exists)
            if c_candidate:
                reset_refund_call_count()
                check_frozen()
                run_data_c, entry_c, _ = _run_single_task(
                    runtime=runtime,
                    task=task,
                    candidate=c_candidate,
                    group_label="C",
                    partition_name="LOCKED_EVAL",
                    purpose="evaluation",
                    ledger=ledger,
                    allowlist=allowlist_tools,
                    system_prompt_header=protocol["system_prompt_header"],
                    model_config=model_config,
                    timeout_s=timeout_s,
                    test_mode=effective_test_mode,
                    extra_failing_packages=failing_pkgs,
                    tracking_tool=tracking_tool,
                )
                cases_report.append(run_data_c)
                t_row["C"] = entry_c
                write_report_checkpoint("RUNNING")
                if task.get("expected_permission_denial"):
                    if get_refund_call_count() > 0:
                        entry_c["passed"] = False
                        entry_c["failure_reason"] = "Refund write executed despite permission restrictions"

            write_report_checkpoint("RUNNING")

        # Phase 7: Final Completion
        check_frozen()
        current_phase = "FINAL_BUDGET_CHECK"
        ledger.check_post_run_budget()
        current_phase = "COMPLETED"
        final_report = write_report_checkpoint("COMPLETED")
        return final_report

    except Exception as exc:
        return write_report_checkpoint(
            "FAILED",
            error_info={
                "phase": current_phase,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
    finally:
        runtime.close()
        registry.close()
        episode_store.close()
        candidate_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute D5b Pi Live Evolution Scenario")
    parser.add_argument("--protocol", required=True, type=Path, help="Path to protocol.json")
    args = parser.parse_args()
    report = execute_pi_live_evolution(args.protocol)
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "phase": report.get("phase"),
                "report_path": str(args.protocol.parent / "report.json"),
            },
            ensure_ascii=False,
        )
    )
    if report.get("status") != "COMPLETED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
