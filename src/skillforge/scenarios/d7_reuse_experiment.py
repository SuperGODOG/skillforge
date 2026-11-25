"""D7b: Four-group (A/B/C/D) Pi reuse pilot runner and paired comparison report.

Executes a verified frozen D7 protocol across four treatments per task with
starting-group rotation:
  A: No history (bare baseline)
  B: Deterministic lexical retrieval of 1 frozen structured Episode
  C: Frozen Skill body bound via AgentRuntime CandidateSnapshot
  D: Same frozen Skill body + read-only read_source_episode(episode_id) tool

All business tools read strictly from the current task's isolated tool_snapshot.
All new Episodes are archived with purpose='evaluation' and never feed back
into B or D.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import uuid

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator.llm_factory import BudgetExceededError
from skillforge.evolution_loop import compute_candidate_hash
from skillforge.models import CandidateSkill, EvolveBudget, SkillMeta
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.d7_oracle import grade_logistics_answer
from skillforge.scenarios.d7_protocol import load_protocol
from skillforge.scenarios.pi_live_evolution import PersistentLedger, _serialize_tool_record


GROUPS: tuple[str, ...] = ("A", "B", "C", "D")
BASE_TOOLS: tuple[str, ...] = ("query_order_packages", "query_package_tracking")
GROUP_D_TOOLS: tuple[str, ...] = ("query_order_packages", "query_package_tracking", "read_source_episode")

GROUP_LABELS: dict[str, str] = {
    "A": "无经验基线",
    "B": "原始 Episode",
    "C": "固定 Skill",
    "D": "Skill＋来源回查",
}

COMPARISON_PAIRS: list[dict[str, str]] = [
    {"from": "B", "to": "C", "label": "B->C"},
    {"from": "C", "to": "D", "label": "C->D"},
]

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")

_HISTORICAL_WARNING = (
    "注意：以下为历史学习 Episode 的原始冻结记录，仅作流程参考。"
    "其中的订单号、包裹号与物流状态等历史事实不得作为当前任务的证据，"
    "当前状态必须以本次任务调用业务工具获得的实时返回为准。"
)


def _lexical_tokens(text: str) -> set[str]:
    """Extract Latin words and CJK characters/bigrams for deterministic lexical matching."""
    lowered = text.lower()
    atoms = _TOKEN_RE.findall(lowered)
    tokens = set(atoms)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
    for run in cjk_runs:
        for idx in range(len(run) - 1):
            tokens.add(run[idx : idx + 2])
    return tokens


def rank_lexical_episodes(query: str, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank frozen episodes by lexical overlap, preferring verified success on ties."""
    q_tokens = _lexical_tokens(query)

    def _score(ep: dict[str, Any]) -> tuple[int, int, str]:
        ep_text = " ".join([
            str(ep.get("root_family", "")),
            str(ep.get("source_task_id", "")),
            str(ep.get("outcome", "")),
            json.dumps(ep.get("trace", {}), ensure_ascii=False, sort_keys=True),
        ])
        ep_tokens = _lexical_tokens(ep_text)
        overlap = len(q_tokens & ep_tokens)
        return (-overlap, 0 if ep.get("outcome") == "success" else 1, ep["episode_id"])

    return sorted(episodes, key=_score)


def _format_group_b_input(
    query: str, episodes: list[dict[str, Any]], memory_max_bytes: int,
) -> tuple[str, list[str]]:
    """Include whole raw traces that fit the history budget, in retrieval order."""
    parts = [_HISTORICAL_WARNING]
    selected = []
    remaining = memory_max_bytes - len(_HISTORICAL_WARNING.encode("utf-8"))
    for episode in rank_lexical_episodes(query, episodes):
        trace = json.dumps(episode["trace"], ensure_ascii=False, separators=(",", ":"))
        text = f"\n[历史参考 Episode: {episode['episode_id']} | outcome={episode['outcome']}]\n{trace}\n"
        size = len(text.encode("utf-8"))
        if size <= remaining:
            parts.append(text)
            selected.append(episode["episode_id"])
            remaining -= size
    if not selected:
        raise ValueError("No complete Episode fits the Group B history budget")
    return "".join(parts) + f"\n\n[当前任务]\n{query}", selected


class _SnapshotContext:
    """Mutable per-case snapshot holder bound strictly to the active task."""

    def __init__(self) -> None:
        self.task: Optional[dict[str, Any]] = None

    def bind(self, task: dict[str, Any]) -> None:
        self.task = task

    def clear(self) -> None:
        self.task = None


class SnapshotQueryOrderPackagesTool(Tool):
    """Isolated query_order_packages reading strictly from the active task's tool_snapshot."""

    def __init__(self, ctx: _SnapshotContext) -> None:
        super().__init__(
            name="query_order_packages",
            description="Query all package tracking IDs associated with a customer order.",
        )
        self._ctx = ctx

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="order_id",
                type="string",
                description="The order identifier to look up, e.g. 'ORD_2026_0901'",
                required=True,
            )
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        task = self._ctx.task
        if task is None:
            return ToolResponse.error(code="NO_ACTIVE_SNAPSHOT", message="No active task snapshot bound.")
        order_id = str(parameters.get("order_id", "")).strip()
        snap_order = task["tool_snapshot"]["order"]
        if order_id != snap_order["order_id"]:
            return ToolResponse.error(
                code="ORDER_NOT_FOUND",
                message=f"Order '{order_id}' not found in current isolated task snapshot.",
                context={"order_id": order_id},
            )
        order_data = copy.deepcopy(snap_order)
        if "package_carriers" not in order_data:
            order_data["package_carriers"] = {
                pid: str(pkg.get("carrier", ""))
                for pid, pkg in task["tool_snapshot"]["packages"].items()
            }
        return ToolResponse.success(
            # Pi sends tool text to the provider and retains data as local details.
            text=json.dumps(order_data, ensure_ascii=False, separators=(",", ":")),
            data=order_data,
        )


class SnapshotQueryPackageTrackingTool(Tool):
    """Isolated query_package_tracking reading strictly from the active task's tool_snapshot."""

    def __init__(self, ctx: _SnapshotContext) -> None:
        super().__init__(
            name="query_package_tracking",
            description="Query the real-time shipping and delivery status of an individual package.",
        )
        self._ctx = ctx

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="package_id",
                type="string",
                description="The package tracking identifier, e.g. 'PKG_101'",
                required=True,
            )
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        task = self._ctx.task
        if task is None:
            return ToolResponse.error(code="NO_ACTIVE_SNAPSHOT", message="No active task snapshot bound.")
        package_id = str(parameters.get("package_id", "")).strip()
        failing_ids = set(task.get("failing_package_ids") or [])
        if package_id in failing_ids:
            return ToolResponse.error(
                code="LOGISTICS_SERVICE_UNAVAILABLE",
                message=f"Logistics query service temporarily unavailable for package '{package_id}'.",
                context={"package_id": package_id},
            )
        packages = task["tool_snapshot"]["packages"]
        if package_id not in packages:
            return ToolResponse.error(
                code="PACKAGE_NOT_FOUND",
                message=f"Package '{package_id}' not found in current isolated task snapshot.",
                context={"package_id": package_id},
            )
        pkg = copy.deepcopy(packages[package_id])
        if pkg.get("status") == "TOOL_ERROR":
            return ToolResponse.error(
                code="CARRIER_API_ERROR",
                message=f"Upstream carrier API error for package '{package_id}': {pkg.get('error_reason')}",
                context={"package_id": package_id, "detail": pkg.get("error_reason")},
            )
        return ToolResponse.success(
            text=f"Package {package_id} status is {pkg['status']} via {pkg['carrier']}",
            data=pkg,
        )


class ReadSourceEpisodeTool(Tool):
    """Read-only Group D broker tool returning frozen skill source episodes within memory_max_bytes."""

    def __init__(
        self,
        episodes_by_id: dict[str, dict[str, Any]],
        allowed_episode_ids: set[str],
        memory_max_bytes: int,
    ) -> None:
        super().__init__(
            name="read_source_episode",
            description=(
                "Read a frozen historical source episode referenced by the active skill "
                "(historical learning trace only, not current task evidence). "
                f"Available episode IDs: {', '.join(sorted(allowed_episode_ids))}. "
                "Use only if you need to resolve uncertainty about the procedure."
            ),
        )
        self._episodes_by_id = episodes_by_id
        self._allowed_ids = set(allowed_episode_ids)
        self._memory_max_bytes = int(memory_max_bytes)
        self._active: bool = False
        self.reset_run_stats()

    def reset_run_stats(self, active: bool = False) -> None:
        self._active = active
        self.cumulative_bytes: int = 0
        self.attempted_ids: list[str] = []
        self.succeeded_ids: list[str] = []
        self.rejected: list[dict[str, str]] = []

    def snapshot_stats(self) -> dict[str, Any]:
        return {
            "used": len(self.succeeded_ids) > 0,
            "attempted": list(self.attempted_ids),
            "succeeded": list(self.succeeded_ids),
            "rejected": list(self.rejected),
            "bytes_read": self.cumulative_bytes,
        }

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="episode_id",
                type="string",
                description="The frozen source episode identifier listed in skill.source_episode_ids.",
                required=True,
            )
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        if not self._active:
            return ToolResponse.error(
                code="SOURCE_LOOKUP_DISABLED",
                message="read_source_episode is only enabled during Group D runs.",
            )
        episode_id = str(parameters.get("episode_id", "")).strip()
        self.attempted_ids.append(episode_id)

        if not episode_id or episode_id not in self._allowed_ids:
            self.rejected.append({"episode_id": episode_id, "reason": "SOURCE_EPISODE_NOT_ALLOWED"})
            return ToolResponse.error(
                code="SOURCE_EPISODE_NOT_ALLOWED",
                message=f"Episode '{episode_id}' is not in skill.source_episode_ids.",
                context={"episode_id": episode_id},
            )

        ep = self._episodes_by_id.get(episode_id)
        if ep is None:
            self.rejected.append({"episode_id": episode_id, "reason": "EPISODE_NOT_FOUND"})
            return ToolResponse.error(
                code="EPISODE_NOT_FOUND",
                message=f"Frozen episode '{episode_id}' not found.",
                context={"episode_id": episode_id},
            )

        payload_obj = {
            "episode_id": ep["episode_id"],
            "source_task_id": ep["source_task_id"],
            "root_family": ep["root_family"],
            "outcome": ep["outcome"],
            "trace": ep["trace"],
            "warning": "HISTORICAL_TRACE_NOT_CURRENT_EVIDENCE",
        }
        payload_text = json.dumps(payload_obj, ensure_ascii=False)
        payload_bytes = len(payload_text.encode("utf-8"))

        if self.cumulative_bytes + payload_bytes > self._memory_max_bytes:
            self.rejected.append({"episode_id": episode_id, "reason": "MEMORY_BUDGET_EXCEEDED"})
            return ToolResponse.error(
                code="MEMORY_BUDGET_EXCEEDED",
                message=(
                    f"Reading episode '{episode_id}' ({payload_bytes} bytes) exceeds "
                    f"memory_max_bytes ({self.cumulative_bytes + payload_bytes} > {self._memory_max_bytes})."
                ),
                context={
                    "episode_id": episode_id,
                    "cumulative_bytes": self.cumulative_bytes,
                    "payload_bytes": payload_bytes,
                    "memory_max_bytes": self._memory_max_bytes,
                },
            )

        self.cumulative_bytes += payload_bytes
        self.succeeded_ids.append(episode_id)
        payload_obj["bytes_read"] = payload_bytes
        payload_obj["cumulative_bytes"] = self.cumulative_bytes
        return ToolResponse.success(text=payload_text, data=payload_obj)


def _compute_paired_delta(tasks: list[dict[str, Any]], src_group: str, dst_group: str) -> dict[str, int]:
    """Compute paired improved/degraded/unchanged/unknown counts retaining unpaired rows as unknown."""
    counts = {"improved": 0, "degraded": 0, "unchanged": 0, "unknown": 0}
    for row in tasks:
        src_p = (row.get(src_group) or {}).get("passed")
        dst_p = (row.get(dst_group) or {}).get("passed")
        if not isinstance(src_p, bool) or not isinstance(dst_p, bool):
            counts["unknown"] += 1
        elif dst_p and not src_p:
            counts["improved"] += 1
        elif src_p and not dst_p:
            counts["degraded"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def compute_d7_summary(
    tasks: list[dict[str, Any]], groups: tuple[str, ...] = GROUPS,
) -> dict[str, Any]:
    """Compute counts and paired metrics for the groups that actually ran."""
    total = len(tasks)
    summary: dict[str, Any] = {}
    for group in groups:
        passed = sum(1 for row in tasks if (row.get(group) or {}).get("passed") is True)
        failed = sum(1 for row in tasks if (row.get(group) or {}).get("passed") is False)
        recorded = passed + failed
        summary[group] = {
            "passed": passed,
            "failed": failed,
            "recorded": recorded,
            "unknown": total - recorded,
            "total": total,
        }
    if "B" in groups:
        summary["paired_b_to_c"] = _compute_paired_delta(tasks, "B", "C")
    summary["paired_c_to_d"] = _compute_paired_delta(tasks, "C", "D")

    d_runs = [row.get("D") or {} for row in tasks]
    d_lookups = [d.get("source_lookups") or {} for d in d_runs if d.get("status") != "UNKNOWN"]
    summary["d_source_lookup_summary"] = {
        "evaluated_runs": len(d_lookups),
        "runs_with_lookup": sum(1 for lk in d_lookups if lk.get("used")),
        "total_succeeded_lookups": sum(len(lk.get("succeeded", [])) for lk in d_lookups),
        "total_rejected_lookups": sum(len(lk.get("rejected", [])) for lk in d_lookups),
        "total_bytes_read": sum(int(lk.get("bytes_read", 0)) for lk in d_lookups),
    }
    return summary


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _acquire_exclusive_lock(exp_dir: Path) -> Path:
    """Create an exclusive run lock and fail closed if report.json, non-empty ledger.json, or lock exists."""
    report_path = exp_dir / "report.json"
    ledger_path = exp_dir / "ledger.json"
    lock_path = exp_dir / ".d7_run.lock"

    if report_path.exists():
        raise RuntimeError(f"Experiment report already exists at {report_path}; refusing to re-run.")
    if ledger_path.exists():
        try:
            raw_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Existing ledger at {ledger_path} is unreadable; refusing to re-run.") from exc
        if raw_ledger.get("records") or raw_ledger.get("summary", {}).get("total_calls", 0) > 0:
            raise RuntimeError(f"Non-empty ledger already exists at {ledger_path}; refusing to re-run.")

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"Exclusive run lock already exists at {lock_path}; refusing concurrent or re-run.") from exc

    try:
        payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()})
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return lock_path


def _classify_run_error(
    exc: Optional[BaseException],
    pre_finalize_status: Optional[str],
    pre_finalize_error_type: Optional[str],
    pre_finalize_error_msg: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Distinguish task timeout, task budget exhaustion, and infrastructure exceptions."""
    if exc is not None:
        msg = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, BudgetExceededError) or "budget" in str(exc).lower() or pre_finalize_status == "BUDGET_EXHAUSTED":
            return f"TASK_BUDGET_EXCEEDED: {msg}", "TASK_BUDGET"
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower() or "deadline" in str(exc).lower() or pre_finalize_status == "TIMED_OUT":
            return f"TASK_TIMEOUT: {msg}", "TASK_TIMEOUT"
        return f"INFRA_ERROR: {msg}", "INFRASTRUCTURE_EXCEPTION"

    if pre_finalize_status == "TIMED_OUT" or pre_finalize_error_type == "TIMEOUT":
        return f"TASK_TIMEOUT: {pre_finalize_error_msg or 'Task execution deadline exceeded'}", "TASK_TIMEOUT"
    if pre_finalize_status == "BUDGET_EXHAUSTED" or pre_finalize_error_type == "BUDGET_EXHAUSTED":
        return f"TASK_BUDGET_EXCEEDED: {pre_finalize_error_msg or 'Task step/turn budget exhausted'}", "TASK_BUDGET"
    if pre_finalize_status in ("FAILED", "INTERRUPTED"):
        return f"INFRA_ERROR: {pre_finalize_error_msg or pre_finalize_status}", "INFRASTRUCTURE_EXCEPTION"
    return None, None


def _build_frozen_candidate(protocol: dict[str, Any]) -> CandidateSkill:
    skill_cfg = protocol["skill"]
    return CandidateSkill(
        candidate_id=f"cand_d7_{skill_cfg['name']}_{skill_cfg['version']}",
        skill_name=skill_cfg["name"],
        decision="create",
        source_episode_ids=list(skill_cfg["source_episode_ids"]),
        meta=SkillMeta(
            name=skill_cfg["name"],
            version=skill_cfg["version"],
            description="D7 frozen pilot skill (experimental baseline only; not formally promoted)",
            use_when="When verifying multi-package logistics orders under D7 protocol.",
            dependencies=[],
        ),
        body=skill_cfg["body"],
        rationale="Frozen protocol skill snapshot for D7 C/D comparison",
        status="DRAFT",
        source_type="episode",
    )


def _run_group_case(
    *,
    runtime: AgentRuntime,
    task: dict[str, Any],
    group: str,
    candidate: Optional[CandidateSkill],
    skill_hash: Optional[str],
    frozen_episodes: list[dict[str, Any]],
    snapshot_ctx: _SnapshotContext,
    source_tool: ReadSourceEpisodeTool,
    ledger: PersistentLedger,
    system_prompt_header: str,
    model_config: dict[str, Any],
    budget_cfg: dict[str, Any],
    exp_deadline_mono: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute one (task, group) run via AgentRuntime + Broker + Collector and return (case_record, group_entry)."""
    # Global budget & deadline guard before launching another run.
    ledger.check_budget(role="agent")
    if time.monotonic() > exp_deadline_mono:
        raise BudgetExceededError(
            "DEADLINE_EXCEEDED: experiment global deadline_seconds exceeded",
            cap_type="deadline",
            limit=float(budget_cfg["deadline_seconds"]),
        )

    run_id = f"run_d7_{group.lower()}_{task['task_id']}_{uuid.uuid4().hex[:8]}"
    is_group_d = group == "D"
    tools = list(GROUP_D_TOOLS if is_group_d else BASE_TOOLS)
    skill_required = set(tools)

    selected_ep_ids: list[str] = []
    if group == "B":
        input_text, selected_ep_ids = _format_group_b_input(
            task["query"], frozen_episodes, int(budget_cfg["memory_max_bytes"]),
        )
    else:
        input_text = task["query"]

    runtime.start_run(
        run_id=run_id,
        task_id=task["task_id"],
        skill_name=candidate.skill_name if candidate else None,
        candidate=candidate,
        purpose="evaluation",
        budget_max=int(budget_cfg["per_run_steps"]),
        deadline_ts=time.time() + float(budget_cfg["timeout_seconds"]),
        skill_required_tools=skill_required,
        task_description=task["query"],
    )

    reserved_turn_ids: dict[tuple[str, int], str] = {}

    def admission_cb(r_id: str, t_idx: int) -> dict[str, Any]:
        if r_id != run_id:
            return {"allowed": False, "error_type": "CROSS_RUN_MISMATCH"}
        if time.monotonic() > exp_deadline_mono:
            return {"allowed": False, "error_type": "BUDGET_EXCEEDED", "error": "Global deadline_seconds exceeded"}
        key = (r_id, t_idx)
        if key not in reserved_turn_ids:
            cid = f"{r_id}:{t_idx}"
            ledger.reserve_turn(role="agent", call_id=cid)
            reserved_turn_ids[key] = cid
        return {"allowed": True}

    def receipt_cb(frame: dict[str, Any]) -> None:
        r_id = frame.get("run_id")
        t_idx = frame.get("turn_index")
        if r_id != run_id or t_idx is None:
            return
        cid = reserved_turn_ids.pop((r_id, t_idx), None)
        if not cid:
            return
        ledger.settle_turn(
            call_id=cid,
            usage=frame.get("usage"),
            status=frame.get("status", "success"),
            error_type=frame.get("error"),
        )

    snapshot_ctx.bind(task)
    source_tool.reset_run_stats(active=is_group_d)

    t0 = time.perf_counter()
    out_text = ""
    caught_exc: Optional[BaseException] = None
    try:
        out_text = runtime.run_agent_with_pi(
            run_id=run_id,
            input_text=input_text,
            candidate=candidate,
            tools=tools,
            system_prompt_header=system_prompt_header,
            model_config=model_config,
            test_mode=False,
            timeout=float(budget_cfg["timeout_seconds"]),
            model_turn_admission=admission_cb,
            model_turn_receipt=receipt_cb,
        )
    except Exception as exc:
        caught_exc = exc
    finally:
        for cid in list(reserved_turn_ids.values()):
            ledger.cancel_pending(call_id=cid, error_type="RUN_TERMINATED_UNKNOWN")
        reserved_turn_ids.clear()
        source_stats = source_tool.snapshot_stats()
        source_tool.reset_run_stats(active=False)
        snapshot_ctx.clear()

    latency_ms = (time.perf_counter() - t0) * 1000.0
    pre_run = runtime.get_run(run_id)
    infra_error, error_category = _classify_run_error(
        caught_exc,
        pre_run.status if pre_run else None,
        pre_run.error_type if pre_run else None,
        pre_run.error_message if pre_run else None,
    )

    calls = runtime.list_tool_calls(run_id)
    oracle_records = [dataclasses.asdict(c) for c in calls]
    verdict = grade_logistics_answer(
        task=task,
        answer=out_text,
        tool_records=oracle_records,
        infra_error=infra_error,
    )
    if error_category is None and verdict["passed"] is False:
        error_category = "BUSINESS_FAILURE"

    verification_evidence = {
        **verdict,
        "independent_pass": verdict["passed"],
        "is_infra_error": infra_error is not None or verdict["classification"] == "INFRA_ERROR",
        "error_category": error_category,
    }
    run_rec, ep = runtime.finalize_run(
        run_id=run_id,
        model_output=out_text,
        verification_evidence=verification_evidence,
        infra_error=infra_error,
    )

    records = [r for r in ledger.records if r.call_id and r.call_id.startswith(run_id + ":")]
    known_usage = bool(records) and all(not r.usage_unknown for r in records)
    usage = {
        "prompt_tokens": sum(r.prompt_tokens for r in records) if known_usage else None,
        "completion_tokens": sum(r.completion_tokens for r in records) if known_usage else None,
        "total_tokens": sum(r.total_tokens for r in records) if known_usage else None,
        "cost": None,
    }
    serialized_tools = [_serialize_tool_record(c) for c in calls]

    group_entry = {
        "passed": verdict["passed"],
        "status": run_rec.status if run_rec else ("FAILED" if infra_error else "UNKNOWN"),
        "verdict": verdict,
        "classification": verdict["classification"],
        "failure_reason": verdict["failure_reason"],
        "error_category": error_category,
        "output_text": out_text,
        "tool_calls_count": len(calls),
        "tools": [c.tool_name for c in calls],
        "tool_records": serialized_tools,
        "latency_ms": round(latency_ms, 2),
        "usage": usage,
        "provider_calls": len(records),
        "run_id": run_id,
        "episode_id": ep.episode_id if ep else None,
        "episode_outcome": ep.outcome if ep else None,
        "selected_episode_ids": selected_ep_ids,
        "source_lookups": source_stats,
        "source_lookup_ids": source_stats["succeeded"],
        "skill_hash": skill_hash if candidate else None,
        "content_hash": run_rec.content_hash if run_rec else None,
    }

    case_record = {
        "run_id": run_id,
        "task_id": task["task_id"],
        "kind": task["kind"],
        "partition": task["kind"].upper(),
        "root_family": task["root_family"],
        "group": group,
        "status": group_entry["status"],
        "purpose": "evaluation",
        "skill_name": candidate.skill_name if candidate else None,
        "skill_version": getattr(candidate.meta, "version", None) if candidate else None,
        "candidate_id": candidate.candidate_id if candidate else None,
        "skill_hash": skill_hash if candidate else None,
        "content_hash": run_rec.content_hash if run_rec else None,
        "frozen_body": run_rec.frozen_body if run_rec else None,
        "response_text": out_text,
        "episode_id": ep.episode_id if ep else None,
        "episode_outcome": ep.outcome if ep else None,
        "oracle": verification_evidence,
        "verdict": verdict,
        "error_category": error_category,
        "tool_records": serialized_tools,
        "tool_records_oracle": oracle_records,
        "selected_episode_ids": selected_ep_ids,
        "source_lookups": source_stats,
        "usage": usage,
        "provider_calls": [dataclasses.asdict(r) for r in records],
        "latency_ms": round(latency_ms, 2),
        "model": model_config.get("model_id"),
        "evidence": {
            "mode": "real_model",
            "data": "synthetic",
            "execution": "official_pi_runner_with_agent_runtime",
            "cost": None,
        },
        "created_at": run_rec.created_at if run_rec else datetime.now(timezone.utc).isoformat(),
    }
    return case_record, group_entry


def execute_reuse_experiment(
    directory: str | Path, *, groups: tuple[str, ...] = GROUPS,
) -> dict[str, Any]:
    """Execute the frozen D7 protocol with all four arms or just the source-lookup pair."""
    if groups not in (GROUPS, ("C", "D")):
        raise ValueError("Expected the four-arm pilot or the C/D source-lookup pair")
    exp_dir = Path(directory).resolve()
    protocol, protocol_hash = load_protocol(exp_dir)
    if "B" in groups:
        for task in protocol["tasks"]:
            _format_group_b_input(task["query"], protocol["episodes"], protocol["budget"]["memory_max_bytes"])
    runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _acquire_exclusive_lock(exp_dir)

    budget_cfg = protocol["budget"]
    model_config = protocol["model_config"]
    evolve_budget = EvolveBudget(
        max_calls=int(budget_cfg["max_provider_calls"]),
        max_tokens=int(budget_cfg["max_total_tokens"]),
        deadline_seconds=float(budget_cfg["deadline_seconds"]),
    )
    ledger = PersistentLedger(budget=evolve_budget, ledger_path=exp_dir / "ledger.json")
    exp_deadline_mono = time.monotonic() + float(budget_cfg["deadline_seconds"])

    skills_dir = exp_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(db_path=exp_dir / "skills.db", skills_dir=skills_dir, repo_root=exp_dir)
    episode_store = EpisodeStore(db_path=exp_dir / "episodes.db")
    candidate_store = CandidateStore(db_path=exp_dir / "candidates.db", episode_store=episode_store)
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    snapshot_ctx = _SnapshotContext()
    episodes_by_id = {ep["episode_id"]: ep for ep in protocol["episodes"]}
    source_tool = ReadSourceEpisodeTool(
        episodes_by_id=episodes_by_id,
        allowed_episode_ids=set(protocol["skill"]["source_episode_ids"]),
        memory_max_bytes=int(budget_cfg["memory_max_bytes"]),
    )
    broker = ToolBroker(application_allowlist=set(GROUP_D_TOOLS))
    broker.register_tool(SnapshotQueryOrderPackagesTool(snapshot_ctx))
    broker.register_tool(SnapshotQueryPackageTrackingTool(snapshot_ctx))
    broker.register_tool(source_tool)

    runtime = AgentRuntime(
        db_path=exp_dir / "runtime.db",
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
        candidate_store=candidate_store,
    )

    frozen_candidate = _build_frozen_candidate(protocol)
    frozen_candidate_hash = compute_candidate_hash(frozen_candidate)
    frozen_body_sha256 = hashlib.sha256(protocol["skill"]["body"].encode("utf-8")).hexdigest()

    transfer_rows: list[dict[str, Any]] = []
    boundary_rows: list[dict[str, Any]] = []
    cases_report: list[dict[str, Any]] = []
    report_path = exp_dir / "report.json"

    for idx, task in enumerate(protocol["tasks"]):
        shift = idx % len(groups)
        group_order = list(groups[shift:] + groups[:shift])
        row: dict[str, Any] = {
            "task_id": task["task_id"],
            "root_family": task["root_family"],
            "kind": task["kind"],
            "order_id": task["order_id"],
            "query": task["query"],
            "group_order": group_order,
        }
        for grp in groups:
            row[grp] = {
                "passed": None,
                "status": "UNKNOWN",
                "classification": "NOT_RUN",
                "failure_reason": "Not yet executed",
            }
        if task["kind"] == "transfer":
            transfer_rows.append(row)
        else:
            boundary_rows.append(row)

    def _write_checkpoint(status: str, error_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        all_rows = transfer_rows + boundary_rows
        payload: dict[str, Any] = {
            "scenario": "d7_reuse_experiment",
            "status": status,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "experiment_name": (
                    "D7 · 跨任务复用四组对照试点" if groups == GROUPS else "D7 · 来源回查对照"
                ),
                "mode": "real_model",
                "evidence": "synthetic",
                "bounded_pilot": True,
                "source": copy.deepcopy(protocol.get("source")),
                "protocol_hash": protocol_hash,
                "oracle_sha256": protocol["oracle_sha256"],
                "grading_sha256": protocol["grading_sha256"],
                "runner_sha256": runner_hash,
                "resolved_model": model_config.get("model_id"),
                "provider": model_config.get("provider"),
                "group_labels": {group: GROUP_LABELS[group] for group in groups},
                "comparison_pairs": [pair for pair in COMPARISON_PAIRS if pair["from"] in groups],
            },
            "skills": {
                "frozen_skill": {
                    "name": protocol["skill"]["name"],
                    "version": protocol["skill"]["version"],
                    "candidate_id": frozen_candidate.candidate_id,
                    "body_sha256": frozen_body_sha256,
                    "candidate_hash": frozen_candidate_hash,
                    "source_episode_ids": list(protocol["skill"]["source_episode_ids"]),
                    "promotion_status": "experimental_frozen_snapshot_not_formally_promoted",
                }
            },
            "partitions": {
                "TRANSFER": {
                    "sample_size": len(transfer_rows),
                    "tasks": transfer_rows,
                    "summary": compute_d7_summary(transfer_rows, groups),
                },
                "BOUNDARY": {
                    "sample_size": len(boundary_rows),
                    "tasks": boundary_rows,
                    "summary": compute_d7_summary(boundary_rows, groups),
                },
            },
            "cases": cases_report,
            "summary": {
                **compute_d7_summary(all_rows, groups),
                "total_calls": ledger.total_calls,
                "failed_calls": ledger.failed_calls,
                "total_tokens": ledger.total_tokens if not ledger.has_unknown_tokens else None,
                "tokens_by_role": ledger.tokens_by_role,
                "calls_by_role": ledger.calls_by_role,
                "cost": None,
            },
        }
        if error_info is not None:
            payload["error"] = error_info
        _atomic_write_json(report_path, payload)
        return payload

    all_task_rows = transfer_rows + boundary_rows
    row_by_task_id = {r["task_id"]: r for r in all_task_rows}

    try:
        _write_checkpoint("RUNNING")
        budget_stopped = False
        budget_stop_reason: Optional[str] = None

        for task in protocol["tasks"]:
            if budget_stopped:
                break
            row = row_by_task_id[task["task_id"]]
            for group in row["group_order"]:
                # Re-verify frozen protocol raw-bytes before every run to fail closed on drift.
                _, current_hash = load_protocol(exp_dir)
                if current_hash != protocol_hash:
                    raise ValueError(f"Frozen protocol hash drifted mid-experiment: {current_hash} != {protocol_hash}")

                cand = frozen_candidate if group in ("C", "D") else None
                try:
                    case_rec, group_entry = _run_group_case(
                        runtime=runtime,
                        task=task,
                        group=group,
                        candidate=cand,
                        skill_hash=frozen_body_sha256 if cand else None,
                        frozen_episodes=protocol["episodes"],
                        snapshot_ctx=snapshot_ctx,
                        source_tool=source_tool,
                        ledger=ledger,
                        system_prompt_header=protocol["system_prompt_header"],
                        model_config=model_config,
                        budget_cfg=budget_cfg,
                        exp_deadline_mono=exp_deadline_mono,
                    )
                    cases_report.append(case_rec)
                    row[group] = group_entry
                    _write_checkpoint("RUNNING")
                except BudgetExceededError as exc:
                    budget_stopped = True
                    budget_stop_reason = f"{getattr(exc, 'cap_type', 'budget')}: {exc}"
                    row[group] = {
                        "passed": None,
                        "status": "UNKNOWN",
                        "verdict": {"passed": None, "classification": "BUDGET_EXHAUSTED", "failure_reason": budget_stop_reason},
                        "classification": "BUDGET_EXHAUSTED",
                        "failure_reason": budget_stop_reason,
                        "error_category": "GLOBAL_BUDGET_EXHAUSTED",
                    }
                    _write_checkpoint("RUNNING")
                    break

        if budget_stopped:
            for r in all_task_rows:
                for grp in groups:
                    if r[grp].get("classification") == "NOT_RUN":
                        r[grp]["failure_reason"] = f"Skipped after global budget exhaustion: {budget_stop_reason}"
            return _write_checkpoint(
                "BUDGET_EXHAUSTED",
                error_info={"category": "GLOBAL_BUDGET_EXHAUSTED", "message": budget_stop_reason},
            )
        return _write_checkpoint("COMPLETED")
    except Exception as exc:
        return _write_checkpoint(
            "FAILED",
            error_info={"category": "INFRASTRUCTURE_EXCEPTION", "message": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        runtime.close()
        episode_store.close()
        candidate_store.close()
        registry.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Frozen D7 protocol directory containing protocol.json and protocol.sha256")
    args = parser.parse_args()
    report = execute_reuse_experiment(args.directory)
    print(
        json.dumps(
            {
                "status": report["status"],
                "protocol_hash": report["metadata"]["protocol_hash"],
                "total_calls": report["summary"]["total_calls"],
                "summary": {k: report["summary"][k] for k in (*GROUPS, "paired_b_to_c", "paired_c_to_d")},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
