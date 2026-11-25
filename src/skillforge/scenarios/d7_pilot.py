"""Prepare the small D7 logistics comparison from the local D4 evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from . import d7_oracle, logistics


def new_cases() -> list[dict[str, Any]]:
    """Six cases from five authored families, with new orders and labels."""
    def case(
        name: str,
        family: str,
        query: str,
        statuses: tuple[str, ...],
        *,
        kind: str = "transfer",
        scope: tuple[int, ...] | None = None,
        requires_order_lookup: bool = True,
        fail: tuple[int, ...] = (),
        order_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        order_id = f"D7-ORDER-{name}"
        package_ids = [f"D7-PKG-{name}-{i}" for i in range(1, len(statuses) + 1)]
        carriers = ["SF_EXPRESS" if i % 2 == 0 else "JD_LOGISTICS" for i in range(len(statuses))]
        row = {
            "task_id": f"D7-{name}",
            "source_task_id": f"d7-authored-{family}",
            "root_family": f"d7-{family}",
            "derived_from": [],
            "kind": kind,
            "query": f"订单 {order_id}。" + query.format(first=package_ids[0]),
            "order_id": order_id,
            "failing_package_ids": [package_ids[i] for i in fail],
            "tool_snapshot": {
                "order": {
                    "order_id": order_id,
                    "order_status": "SHIPPED",
                    "packages": package_ids,
                    "package_carriers": dict(zip(package_ids, carriers)),
                    **(order_fields or {}),
                },
                "packages": {
                    pid: {"package_id": pid, "status": status, "carrier": carrier}
                    for pid, status, carrier in zip(package_ids, statuses, carriers)
                },
            },
        }
        if scope is not None:
            row["scope_package_ids"] = [package_ids[i] for i in scope]
            row["requires_order_lookup"] = requires_order_lookup
        return row

    return [
        case("STALE", "merchant-summary", "商家页显示已完成。为结算核对订单的每件包裹当前物流状态。",
             ("DELIVERED", "DELIVERED", "IN_TRANSIT", "EXCEPTION"), order_fields={"order_status": "COMPLETED"}),
        case("REISSUE", "replacement-label", "有面单作废重打。核实当前有效面单的状态，作废的旧面单不计入。",
             ("DELIVERED", "DELIVERED", "DELIVERED"), order_fields={"voided_labels": ["D7-VOIDED-REISSUE"]}),
        case("HANDOFF", "cross-carrier-handoff", "两个承运商的交接批次已结案。核实全单每件实际状态，批次结案不等于包裹签收。",
             ("DELIVERED", "REJECTED_RETURN", "DELIVERED", "DELAYED", "DELIVERED"),
             order_fields={"handoff_batch": "CLOSED"}),
        case("ONE", "limited-tracking-scope", "授权范围只有包裹 {first}。只查询和返回这件的流水状态。",
             ("DELIVERED", "IN_TRANSIT", "EXCEPTION"), kind="boundary", scope=(0,), requires_order_lookup=False),
        case("CARRIER", "limited-tracking-scope", "仅查看顺丰包裹。按订单详情里的承运商映射过滤，其他承运商不要查。",
             ("DELAYED", "DELIVERED", "DELIVERED", "IN_TRANSIT"), kind="boundary", scope=(0, 2)),
        case("OUTAGE", "partial-carrier-availability", "核查全单。某个接口可能不可用，无法核实的包裹标为未知。",
             ("DELIVERED", "DELIVERED", "DELIVERED"), kind="boundary", fail=(1,)),
    ]


def make_protocol(d4_dir: Path, model_config: dict[str, Any]) -> dict[str, Any]:
    report_bytes = (d4_dir / "report.json").read_bytes()
    report = json.loads(report_bytes)
    if report.get("scenario") != "dashboard_evolution_demo" or report.get("status") != "SUCCESS":
        raise ValueError("Expected a successful D4 evidence directory")
    lineage = report["lineage"]
    skill_markdown = (d4_dir / "skills" / "logistics_query" / "SKILL.md").read_bytes()
    _, _, body = skill_markdown.decode().split("---", 2)
    if body.strip() != lineage["promoted_body"].strip():
        raise ValueError("D4 skill body differs from the recorded promotion")

    source_ids = lineage["source_episode_ids"]
    normal_run = "run_learning_normal_1"
    history = [c for c in report["cases"] if c["episode_id"] in source_ids or c["run_id"] == normal_run]
    if len(history) != len(source_ids) + 1 or any(c["purpose"] != "learning" for c in history):
        raise ValueError("D4 source Episode records are missing or ineligible")

    # Copy recorded business inputs/outputs and the Oracle verdict without injecting
    # either the old prompt's Skill body or any task from D7.
    fields = ("task_id", "run_id", "response_text", "tool_records", "oracle", "skill_version")
    episodes = []
    episode_db = (d4_dir / "episodes.db").resolve()
    with closing(sqlite3.connect(episode_db.as_uri() + "?mode=ro", uri=True)) as conn:
        for case in history:
            ep_id = case["episode_id"]
            row = conn.execute(
                "SELECT task_id, run_id, outcome, environment_json FROM episodes WHERE episode_id = ?",
                (ep_id,),
            ).fetchone()
            if row is None or row[:3] != (case["task_id"], case["run_id"], case["episode_outcome"]):
                raise ValueError(f"D4 report disagrees with the Episode store for {ep_id}")
            environment = json.loads(row[3])
            if environment.get("purpose") != "learning" or not environment.get("task_description"):
                raise ValueError(f"D4 Episode lacks a learning purpose or recorded task input: {ep_id}")
            episodes.append({
                "episode_id": ep_id, "source_task_id": case["task_id"],
                "root_family": "d4-whole-order-coverage", "derived_from": [],
                "purpose": "learning", "outcome": case["episode_outcome"],
                "trace": {"query": environment["task_description"], **{field: case[field] for field in fields}},
            })

    return {
        "schema_version": 1,
        "model_config": model_config,
        "system_prompt_header": "你是履约对账助手。本次任务的范围以用户要求为准。" + d7_oracle.ANSWER_FORMAT,
        "budget": {"max_provider_calls": 168, "max_total_tokens": 220_000,
                   "deadline_seconds": 3600, "per_run_steps": 18,
                   "memory_max_bytes": 20_000, "timeout_seconds": 120},
        "oracle_sha256": hashlib.sha256(Path(logistics.__file__).read_bytes()).hexdigest(),
        "grading_sha256": hashlib.sha256(Path(d7_oracle.__file__).read_bytes()).hexdigest(),
        "skill": {"name": "logistics_query", "version": lineage["promoted_version"],
                  "body": body.strip(), "source_episode_ids": source_ids},
        "episodes": episodes,
        "tasks": new_cases(),
        "source": {"scenario": "D4", "evidence": "scripted / synthetic",
                   "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
                   "skill_file_sha256": hashlib.sha256(skill_markdown).hexdigest(),
                   "release_id": lineage["release_id"]},
    }


def main() -> None:
    from .d7_protocol import freeze_protocol

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("d4_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    config = {"provider": args.provider, "model_id": args.model,
              "reasoning": "off", "temperature": 0.0, "max_tokens": 1536}
    protocol = make_protocol(args.d4_directory, config)
    print(freeze_protocol(args.output_directory, protocol))


if __name__ == "__main__":
    main()
