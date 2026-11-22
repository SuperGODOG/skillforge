"""Phase 5A: Pi Local Model Live Smoke Scenario Runner.

Executes live LLM evaluation runs using the local Pi agent configuration
(~/.pi/agent/models.json) against authoritative logistics multi-package scenarios:
1. ORD_2026_0901: Normal full delivery (2 packages DELIVERED)
2. ORD_2026_0902: Partial delivery (PKG_201 DELIVERED, PKG_202 IN_TRANSIT)
3. ORD_2026_0902: Fault injection on PKG_202 via QueryPackageTrackingTool

Enforces:
- Shared admission gate (<= 16 requests total, overall deadline <= 900s).
- Usage thresholds stop later requests after receipts; they are not a preflight input-token guarantee.
- Independent business Oracle (verify_logistics_fulfillment) with STATUS_ONLY intent constraint.
- Strict secret confidentiality: apiKey is NEVER logged, printed, or persisted in records/JSON.
- Isolated temporary SQLite and repository directories.
- Atomic per-case persistence and comprehensive report.json / report.jsonl.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..collector import ExperienceCollector
from ..episode import EpisodeStore
from ..models import CandidateSkill, SkillMeta
from ..registry import SkillRegistry
from ..runtime import AgentRuntime, ToolBroker
from .logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)


def redact_secrets(val: Any) -> Any:
    """Sanitize strings, dictionaries, or lists to guarantee secrets are never recorded."""
    if isinstance(val, str):
        v = re.sub(r"sk-[a-zA-Z0-9_\-]{16,}", "[REDACTED_SECRET]", val)
        v = re.sub(r"Bearer\s+[a-zA-Z0-9_\-\.]{16,}", "Bearer [REDACTED_SECRET]", v)
        return v
    elif isinstance(val, dict):
        sanitized: dict[str, Any] = {}
        for k, v in val.items():
            if str(k).lower() in ("apikey", "api_key", "authorization", "secret", "password"):
                sanitized[k] = "[REDACTED_SECRET]"
            else:
                sanitized[k] = redact_secrets(v)
        return sanitized
    elif isinstance(val, list):
        return [redact_secrets(item) for item in val]
    return val


def write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(redact_secrets(value), indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def aggregate_usage(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum every turn, preserving unknowns; Pi input excludes cacheRead/cacheWrite."""
    result: dict[str, Any] = {"cost": None}
    for field in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        values = [(r.get("usage") or {}).get(field) for r in receipts]
        result[field] = (
            sum(values) if values and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v >= 0 for v in values
            ) else None
        )
    total, output = result["totalTokens"], result["output"]
    result["input_including_cache"] = total - output if total is not None and output is not None else None
    return result


class SharedSmokeAdmissionGate:
    """Shared authoritative admission gate across all smoke runs in the scenario."""

    def __init__(
        self,
        max_requests: int = 16,
        deadline_ts: float = 0.0,
        max_total_input: int = 128000,
        max_total_output: int = 32768,
    ) -> None:
        self.max_requests = max_requests
        self.deadline_ts = deadline_ts
        self.max_total_input = max_total_input
        self.max_total_output = max_total_output
        self.request_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.turn_history: list[dict[str, Any]] = []
        self.usage_unknown = False
        self._receipt_ids: set[str] = set()
        self._pending: set[tuple[str, int]] = set()

    def admit(self, run_id: str, turn_index: int) -> dict[str, Any]:
        if self.usage_unknown or self._pending:
            return {"allowed": False, "error_type": "USAGE_UNKNOWN", "error": "A prior attempt has no complete usage receipt"}
        now = time.time()
        if self.deadline_ts > 0 and now > self.deadline_ts:
            return {
                "allowed": False,
                "error_type": "DEADLINE_EXCEEDED",
                "error": f"Smoke scenario absolute deadline exceeded ({now:.1f} > {self.deadline_ts:.1f})",
            }
        if self.request_count >= self.max_requests:
            return {
                "allowed": False,
                "error_type": "MAX_REQUESTS_EXCEEDED",
                "error": f"Scenario request limit ({self.max_requests}) reached",
            }
        if self.total_input_tokens >= self.max_total_input:
            return {
                "allowed": False,
                "error_type": "INPUT_TOKEN_LIMIT_EXCEEDED",
                "error": f"Max input tokens ({self.max_total_input}) reached",
            }
        if self.total_output_tokens >= self.max_total_output:
            return {
                "allowed": False,
                "error_type": "OUTPUT_TOKEN_LIMIT_EXCEEDED",
                "error": f"Max output tokens ({self.max_total_output}) reached",
            }

        self.request_count += 1
        self._pending.add((run_id, turn_index))
        return {"allowed": True}

    def on_receipt(self, frame: dict[str, Any]) -> None:
        receipt_id = frame.get("receipt_id")
        if receipt_id and receipt_id in self._receipt_ids:
            return
        if receipt_id:
            self._receipt_ids.add(receipt_id)
        self._pending.discard((frame.get("run_id"), frame.get("turn_index")))
        # Anonymous receipts are supported only by offline gate unit tests.
        if "run_id" not in frame:
            self._pending.clear()
        usage = frame.get("usage")
        if usage and isinstance(usage, dict):
            inp = usage.get("input")
            outp = usage.get("output")
            total = usage.get("totalTokens")
            cached = usage.get("cacheRead", 0)
            written = usage.get("cacheWrite", 0)
            valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
            if valid(total) and valid(outp) and total >= outp:
                self.total_input_tokens += int(total - outp)
            elif all(valid(v) for v in (inp, outp, cached, written)):
                self.total_input_tokens += int(inp + cached + written)
            else:
                self.usage_unknown = True
            if valid(outp):
                self.total_output_tokens += int(outp)
            else:
                self.usage_unknown = True
        else:
            self.usage_unknown = True
        self.turn_history.append(redact_secrets(frame))


def load_local_model_metadata(
    provider_name: str = "cc-switch-deep-seek",
    model_id: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Inspect local ~/.pi/agent/models.json and return public metadata (no credentials)."""
    agent_dir = os.environ.get("PI_AGENT_DIR") or os.path.expanduser("~/.pi/agent")
    models_path = Path(agent_dir) / "models.json"

    if not models_path.exists():
        raise FileNotFoundError(f"Local Pi models configuration not found at {models_path}")

    with open(models_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    provider_entry = data.get("providers", {}).get(provider_name)
    if not provider_entry:
        raise KeyError(f"Provider '{provider_name}' not found in {models_path}")

    base_url = provider_entry.get("baseUrl", "")
    parsed_url = urlparse(base_url)
    host = parsed_url.hostname or base_url

    models_list = provider_entry.get("models", [])
    model_meta: Optional[dict[str, Any]] = None
    for m in models_list:
        if m.get("id") == model_id:
            model_meta = m
            break

    if not model_meta:
        raise KeyError(f"Model '{model_id}' not found under provider '{provider_name}' in {models_path}")

    return {
        "provider": provider_name,
        "model_id": model_id,
        "api": provider_entry.get("api", "openai-completions"),
        "baseUrl": base_url,
        "host": host,
        "contextWindow": model_meta.get("contextWindow", 128000),
        "maxTokens": model_meta.get("maxTokens", 4096),
        "reasoning": model_meta.get("reasoning", False),
    }


def run_single_smoke_case(
    case_index: int,
    case_name: str,
    order_id: str,
    output_dir: Path,
    model_config: dict[str, Any],
    gate: SharedSmokeAdmissionGate,
    expect_tool_failure: bool = False,
    failing_package_ids: Optional[list[str]] = None,
    per_request_timeout: float = 120.0,
) -> dict[str, Any]:
    """Execute a single locked logistics test case through AgentRuntime -> Pi -> Broker -> Collector."""
    case_start_ts = time.time()
    run_uuid = uuid.uuid4().hex[:8]
    run_id = f"run_smoke_{case_index}_{run_uuid}"
    task_id = f"task_smoke_{case_index}_{order_id}"

    db_path = output_dir / f"runtime_case_{case_index}.db"
    ep_db = output_dir / f"episodes_case_{case_index}.db"
    sk_db = output_dir / f"skills_case_{case_index}.db"
    sk_dir = output_dir / f"skills_case_{case_index}"
    sk_dir.mkdir(parents=True, exist_ok=True)

    ep_store = EpisodeStore(db_path=ep_db)
    registry = SkillRegistry(db_path=sk_db, skills_dir=sk_dir)

    # ToolBroker setup
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool(QueryOrderPackagesTool())
    if expect_tool_failure and failing_package_ids:
        broker.register_tool(QueryPackageTrackingTool(simulated_failing_packages=set(failing_package_ids)))
    else:
        broker.register_tool(QueryPackageTrackingTool())

    collector = ExperienceCollector(episode_store=ep_store, registry=registry)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=ep_store,
    )

    case_record: dict[str, Any] = {
        "case_index": case_index,
        "case_name": case_name,
        "order_id": order_id,
        "run_id": run_id,
        "task_id": task_id,
        "expect_tool_failure": expect_tool_failure,
        "failing_package_ids": failing_package_ids or [],
        "real_llm": True,
        "synthetic_data": True,
        "execution": "actual_pi_runtime_broker_collector",
        "started_at": case_start_ts,
    }

    atomic_file = output_dir / f"case_{case_index}_{order_id}.json"
    attempts_before = gate.request_count

    try:
        draft = CandidateSkill(
            candidate_id=f"cand_smoke_{case_index}_{run_uuid}",
            skill_name="logistics_live_verifier",
            decision="create",
            meta=SkillMeta(
                name="logistics_live_verifier",
                version="0.1.0-smoke",
                description="电商物流多包裹核查真机冒烟验证候选",
                use_when="核验订单物流各包裹最新状态",
                dependencies=["query_order_packages", "query_package_tracking"],
            ),
            body=(
                "核验物流订单步骤：\n"
                "1. 首先调用 query_order_packages 查询订单所含的所有包裹ID；\n"
                "2. 针对每个包裹ID，逐一调用 query_package_tracking 查询其实际物流状态；\n"
                "3. 严格遵循 STATUS_ONLY 约束汇总输出每个包裹的当前最新状态（可保留DELIVERED/IN_TRANSIT等状态枚举）；\n"
                "4. 若某个包裹查询失败或状态暂不可用，如实报告查询异常，严禁虚构签收事实；\n"
                "5. 严禁提供任何后续行动建议、催促建议、退款建议或联系客服建议。"
            ),
            source_requirement="Phase 5A 本地真实模型物流核查最小冒烟运行",
            status="DRAFT",
        )

        budget_max = 8
        run_timeout = min(240.0, gate.deadline_ts - time.time())
        if run_timeout <= 0:
            raise TimeoutError("Smoke scenario deadline expired before dispatch")
        deadline_ts = min(gate.deadline_ts, case_start_ts + run_timeout)

        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            candidate=draft,
            purpose="evaluation",
            budget_max=budget_max,
            deadline_ts=deadline_ts,
        )

        user_prompt = (
            f"请核验订单 {order_id} 下全部包裹的最新物流状态。\n"
            "约束要求：严格遵循 STATUS_ONLY，仅逐个包裹陈述当前状态，严禁给出任何建议（如催促、退款、联系客服等）。"
        )

        final_text = runtime.run_agent_with_pi(
            run_id=run_id,
            input_text=user_prompt,
            candidate=draft,
            tools=["query_order_packages", "query_package_tracking"],
            model_config=model_config,
            test_mode=False,
            timeout=run_timeout,
            model_turn_admission=gate.admit,
            model_turn_receipt=gate.on_receipt,
        )

        provider_attempts = gate.request_count - attempts_before

        tool_records = runtime.list_tool_calls(run_id)

        oracle = verify_logistics_fulfillment(
            model_output=final_text,
            order_id=order_id,
            tool_records=tool_records,
            intent_constraint="STATUS_ONLY",
            expect_tool_failure=expect_tool_failure,
            failing_package_ids=failing_package_ids,
        )

        run_rec, episode = runtime.finalize_run(
            run_id=run_id,
            model_output=final_text,
            verification_evidence=oracle,
        )

        pi_pid = runtime.get_last_pi_pid(run_id)
        duration = time.time() - case_start_ts

        case_record.update({
            "status": "COMPLETED",
            "prompt": user_prompt,
            "response_text": final_text,
            "tool_records": [asdict(r) for r in tool_records],
            "oracle": oracle,
            "run_status": run_rec.status if run_rec else "UNKNOWN",
            "budget": {
                "budget_max": run_rec.budget_max if run_rec else budget_max,
                "budget_consumed": run_rec.budget_consumed if run_rec else 0,
                "deadline_ts": run_rec.deadline_ts if run_rec else deadline_ts,
            },
            "episode": {
                "episode_id": episode.episode_id if episode else None,
                "outcome": episode.outcome if episode else "unknown",
                "purpose": run_rec.purpose if run_rec else "evaluation",
            },
            "duration_seconds": round(duration, 3),
            "provider_attempts": provider_attempts,
            "error": None,
            "pid_cleanup": {
                "pi_pid": pi_pid,
                "reaped": False,
            },
        })

    except Exception as exc:
        duration = time.time() - case_start_ts
        err_msg = str(exc)
        sanitized_err = redact_secrets(err_msg)
        tool_records = runtime.list_tool_calls(run_id)

        oracle = verify_logistics_fulfillment(
            model_output="",
            order_id=order_id,
            tool_records=tool_records,
            intent_constraint="STATUS_ONLY",
            expect_tool_failure=expect_tool_failure,
            failing_package_ids=failing_package_ids,
            infra_error=sanitized_err,
        )

        run_rec = runtime.get_run(run_id)
        pi_pid = runtime.get_last_pi_pid(run_id)

        case_record.update({
            "status": "FAILED",
            "prompt": f"请核验订单 {order_id} 下全部包裹的最新物流状态。",
            "response_text": "",
            "tool_records": [asdict(r) for r in tool_records],
            "oracle": oracle,
            "run_status": run_rec.status if run_rec else "FAILED",
            "budget": {
                "budget_max": run_rec.budget_max if run_rec else 8,
                "budget_consumed": run_rec.budget_consumed if run_rec else 0,
                "deadline_ts": run_rec.deadline_ts if run_rec else 0.0,
            },
            "episode": {
                "episode_id": None,
                "outcome": "failed",
                "purpose": "evaluation",
            },
            "usage": {"input": None, "output": None, "totalTokens": None, "cost": None},
            "duration_seconds": round(duration, 3),
            "provider_attempts": gate.request_count - attempts_before,
            "error": {
                "type": type(exc).__name__,
                "message": sanitized_err,
            },
            "pid_cleanup": {
                "pi_pid": pi_pid,
                "reaped": False,
            },
        })

    finally:
        runtime.close()
        registry.close()
        ep_store.close()
        receipts = [r for r in gate.turn_history if r.get("run_id") == run_id]
        case_record["turn_receipts"] = receipts
        case_record["usage"] = aggregate_usage(receipts)
        case_record["provider_attempts"] = gate.request_count - attempts_before
        if len(receipts) != case_record["provider_attempts"]:
            case_record["usage"] = {k: None for k in case_record["usage"]}
        # Check actual process absence instead of inferring it from close().
        if "pid_cleanup" in case_record:
            pid = case_record["pid_cleanup"].get("pi_pid")
            reaped = pid is None
            if pid is not None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    reaped = True
            case_record["pid_cleanup"]["reaped"] = reaped

        # Atomic persistence of case record
        write_json_atomic(atomic_file, case_record)

    return case_record


def execute_pi_live_smoke(
    output_dir: Path,
    provider_name: str = "cc-switch-deep-seek",
    model_id: str = "deepseek-v4-flash",
    per_request_timeout: float = 120.0,
    overall_deadline_sec: float = 900.0,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Execute complete Phase 5A live smoke suite across 3 locked cases."""
    if not (math.isfinite(per_request_timeout) and 0 < per_request_timeout <= 120):
        raise ValueError("per_request_timeout must be in (0, 120]")
    if not (math.isfinite(overall_deadline_sec) and 0 < overall_deadline_sec <= 900):
        raise ValueError("overall_deadline_sec must be in (0, 900]")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 0 < max_tokens <= 2048:
        raise ValueError("max_tokens must be an integer in [1, 2048]")
    start_ts = time.time()
    smoke_uuid = uuid.uuid4().hex[:8]
    run_dir = output_dir / f"pi_live_smoke_{smoke_uuid}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load local model metadata
    model_meta = load_local_model_metadata(provider_name=provider_name, model_id=model_id)

    model_config = {
        "provider": provider_name,
        "model_id": model_id,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "max_retries": 0,
        "timeout_ms": int(per_request_timeout * 1000),
        "reasoning": "off",
    }

    # 2. Shared admission gate (16 requests max, 900s deadline max)
    overall_deadline = start_ts + overall_deadline_sec
    gate = SharedSmokeAdmissionGate(
        max_requests=16,
        deadline_ts=overall_deadline,
        max_total_input=128000,
        max_total_output=32768,
    )

    cases_spec = [
        {
            "case_index": 1,
            "case_name": "normal_all_delivered",
            "order_id": "ORD_2026_0901",
            "expect_tool_failure": False,
            "failing_package_ids": None,
        },
        {
            "case_index": 2,
            "case_name": "partial_in_transit",
            "order_id": "ORD_2026_0902",
            "expect_tool_failure": False,
            "failing_package_ids": None,
        },
        {
            "case_index": 3,
            "case_name": "tool_failure_injection",
            "order_id": "ORD_2026_0902",
            "expect_tool_failure": True,
            "failing_package_ids": ["PKG_202"],
        },
    ]

    case_results: list[dict[str, Any]] = []
    jsonl_path = run_dir / "report.jsonl"

    print(f"================================================================================")
    print(f"SkillForge Phase 5A: Pi Native Local Model Live Smoke Suite")
    print(f"Provider: {provider_name} | Model: {model_id} | Host: {model_meta['host']}")
    print(f"Output Directory: {run_dir}")
    print(f"Max Requests: {gate.max_requests} | Overall Deadline: {overall_deadline_sec}s")
    print(f"================================================================================")

    with open(jsonl_path, "a", encoding="utf-8") as f_jsonl:
        for spec in cases_spec:
            idx = spec["case_index"]
            c_name = spec["case_name"]
            ord_id = spec["order_id"]
            print(f"\n[Case {idx}/3] Starting {c_name} (Order: {ord_id})...")

            res = run_single_smoke_case(
                case_index=idx,
                case_name=c_name,
                order_id=ord_id,
                output_dir=run_dir,
                model_config=model_config,
                gate=gate,
                expect_tool_failure=spec["expect_tool_failure"],
                failing_package_ids=spec["failing_package_ids"],
                per_request_timeout=per_request_timeout,
            )
            case_results.append(res)
            f_jsonl.write(json.dumps(res, ensure_ascii=False) + "\n")
            f_jsonl.flush()

            oracle_res = res.get("oracle", {})
            oracle_verdict = oracle_res.get("verdict", "FAIL")
            oracle_pass = oracle_res.get("independent_pass", False)
            print(f"[Case {idx}/3] Completed in {res.get('duration_seconds')}s. Status: {res.get('status')}. Oracle: {oracle_verdict} (pass={oracle_pass})")
            if not oracle_pass:
                print(f"           Failure reason: {oracle_res.get('failure_reason')}")

    total_duration = time.time() - start_ts
    total_passed = sum(1 for c in case_results if c.get("oracle", {}).get("independent_pass") is True)

    report_summary: dict[str, Any] = {
        "suite_name": "Pi_Local_Model_Live_Smoke_Phase5A",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ts)),
        "model": {
            "model_id": model_id,
            "provider": provider_name,
            "api": model_meta["api"],
            "baseUrl": model_meta["baseUrl"],
            "host": model_meta["host"],
        },
        "evidence_labels": {
            "real_llm": True,
            "synthetic_data": True,
            "execution": "actual_pi_runtime_broker_collector",
            "os_sandbox": "not_exercised",
        },
        "metrics": {
            "total_cases": len(cases_spec),
            "passed_cases": total_passed,
            "failed_cases": len(cases_spec) - total_passed,
            "pass_rate": round(total_passed / len(cases_spec), 4),
            "total_provider_calls": gate.request_count,
            "total_input_tokens": None if gate.usage_unknown else gate.total_input_tokens,
            "total_output_tokens": None if gate.usage_unknown else gate.total_output_tokens,
            "total_tokens": None if gate.usage_unknown else gate.total_input_tokens + gate.total_output_tokens,
            "total_cost": None,  # Strictly None: no fake billing
            "overall_duration_seconds": round(total_duration, 3),
        },
        "gate_limits": {
            "max_requests": gate.max_requests,
            "requests_used": gate.request_count,
            "max_total_input": gate.max_total_input,
            "max_total_output": gate.max_total_output,
            "overall_deadline_sec": overall_deadline_sec,
        },
        "cases": case_results,
        "run_dir": str(run_dir),
        "artifacts": {
            "report_json": str(run_dir / "report.json"),
            "report_jsonl": str(jsonl_path),
        },
    }

    report_json_path = run_dir / "report.json"
    write_json_atomic(report_json_path, report_summary)

    print(f"\n================================================================================")
    print(f"Phase 5A Live Smoke Completed:")
    print(f"Passed: {total_passed}/{len(cases_spec)} ({report_summary['metrics']['pass_rate']*100:.1f}%)")
    print(f"Total Provider Requests: {gate.request_count}/{gate.max_requests}")
    print(f"Total Usage Tokens: {report_summary['metrics']['total_tokens']} (In: {gate.total_input_tokens}, Out: {gate.total_output_tokens})")
    print(f"Duration: {total_duration:.2f}s")
    print(f"Report: {report_json_path}")
    print(f"================================================================================")

    return report_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="SkillForge Pi Local Model Live Smoke Runner")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("scratch") / f"pi_live_smoke_{int(time.time())}",
        help="Target output directory for smoke artifacts",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="cc-switch-deep-seek",
        help="Provider name in ~/.pi/agent/models.json",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-v4-flash",
        help="Model ID under provider in ~/.pi/agent/models.json",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Max tokens per response",
    )
    parser.add_argument(
        "--overall-deadline",
        type=float,
        default=900.0,
        help="Overall deadline in seconds for entire smoke suite",
    )

    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report = execute_pi_live_smoke(
        output_dir=output_dir,
        provider_name=args.provider,
        model_id=args.model,
        per_request_timeout=args.timeout,
        overall_deadline_sec=args.overall_deadline,
        max_tokens=args.max_tokens,
    )
    if report["metrics"]["passed_cases"] != report["metrics"]["total_cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
