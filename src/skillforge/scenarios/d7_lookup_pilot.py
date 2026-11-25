"""C/D pilot: can source Episodes resolve a shipping-contract scope?"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from . import d7_oracle, d7_reuse_experiment, logistics
from .d7_protocol import freeze_protocol, load_protocol


GROUPS = ("C", "D")
SKILL = """## 核对尾程运费对账范围
1. 查本订单，读取包裹标识、`parcel_tags` 和当前对账协议。
2. 若订单提供 `current_approved_billing_scope`，直接使用这份已批准的当前范围。
3. 若缺少当前范围，按订单 `archive_refs` 按需回查已批准的历史协议（若具备回查工具）。
   只采纳 `profile` 和 `revision` 都匹配的规则，用其 `seller_paid_tags` 过滤订单包裹。
   包裹标签是内部编码，不能凭标签、承运商、旧版本或包裹顺序猜范围。
4. 仍无法确认范围时，不查任何单件，返回 {"packages":{},"all_delivered":null}。
5. 只查确认在范围内的包裹。逐件状态以本次跟踪工具为准，整单签收结论填 null。
"""


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_protocol() -> dict[str, Any]:
    # Historical onboarding records are synthetic fixtures, independent of the live orders below.
    policies = (
        ("EP-Q27", "PX-62", 3, ["U4"]),
        ("EP-H53", "PX-62", 2, ["K8"]),
        ("EP-E48", "PX-17", 1, ["R2", "R9"]),
        ("EP-S16", "PX-53", 5, ["M7"]),
        ("EP-K26", "PX-86", 2, ["N5"]),
    )
    episodes = [
        {
            "episode_id": episode_id,
            "source_task_id": f"onboarding-{profile}-{revision}",
            "root_family": "contract-onboarding",
            "derived_from": [],
            "purpose": "learning",
            "outcome": "success",
            "trace": {
                "query": f"记录对账主管批准的 {profile} 第 {revision} 版尾程运费标签规则。",
                "approval_state": "APPROVED",
                "approved_by_role": "settlement_lead",
                "profile": profile,
                "revision": revision,
                "seller_paid_tags": tags,
            },
        }
        for episode_id, profile, revision, tags in policies
    ]

    def case(
        name: str, profile: str, revision: int, tags: tuple[str, ...],
        expected_positions: tuple[int, ...], refs: tuple[str, ...], *, live_scope: bool = False,
    ) -> dict[str, Any]:
        order_id = f"D7L-{name}"
        package_ids = [f"{order_id}-{i}" for i in range(1, len(tags) + 1)]
        scope = [package_ids[i] for i in expected_positions]
        order = {
            "order_id": order_id,
            "order_status": "SHIPPED",
            "packages": package_ids,
            "billing_agreement": {"profile": profile, "revision": revision},
            "parcel_tags": dict(zip(package_ids, tags)),
            "archive_refs": list(refs),
        }
        if live_scope:
            order["current_approved_billing_scope"] = scope.copy()
        return {
            "task_id": order_id,
            "source_task_id": f"current-settlement-{name}",
            "root_family": "current-scope-published" if live_scope else "archive-only-scope",
            "derived_from": [],
            "kind": "boundary" if live_scope else "transfer",
            "query": f"订单 {order_id}。只核对本店承担尾程运费的包裹，报告逐件当前物流状态，别查询其他包裹。",
            "order_id": order_id,
            # Grading-only ground truth; neither order lookup nor tracking sends this field.
            "scope_package_ids": scope,
            "tool_snapshot": {
                "order": order,
                "packages": {
                    pid: {"package_id": pid, "carrier": "PARCEL_NETWORK", "status": status}
                    for pid, status in zip(package_ids, ("DELAYED", "DELIVERED", "IN_TRANSIT"))
                },
            },
        }

    tasks = [
        case("831", "PX-62", 3, ("K8", "U4", "K8"), (1,), ("EP-H53", "EP-Q27")),
        case("459", "PX-17", 1, ("R2", "H5", "R9"), (0, 2), ("EP-E48",)),
        case("674", "PX-53", 5, ("G3", "G3", "M7"), (2,), ("EP-S16",)),
        case("328", "PX-86", 2, ("N5", "T0", "T0"), (0,), ("EP-K26",)),
        case("992", "PX-62", 2, ("K8", "U4", "K8"), (0, 2), ("EP-H53",), live_scope=True),
        case("563", "PX-53", 6, ("M7", "B6", "M7"), (1,), ("EP-S16",), live_scope=True),
    ]
    return {
        "schema_version": 1,
        "model_config": {"provider": "cc-switch-deep-seek", "model_id": "deepseek-v4-flash",
                         "reasoning": "off", "temperature": 0.0, "max_tokens": 1536},
        "system_prompt_header": "你是履约对账助手。按授权范围查询此订单。" + d7_oracle.ANSWER_FORMAT,
        "budget": {"max_provider_calls": 72, "max_total_tokens": 120_000,
                   "deadline_seconds": 1200, "per_run_steps": 14,
                   "memory_max_bytes": 6000, "timeout_seconds": 90},
        "oracle_sha256": _sha(logistics.__file__),
        "grading_sha256": _sha(d7_oracle.__file__),
        "skill": {"name": "shipping_scope_verification", "version": "0.1.0", "body": SKILL,
                  "source_episode_ids": [episode["episode_id"] for episode in episodes]},
        "episodes": episodes,
        "tasks": tasks,
        "source": {
            "scenario": "d7_lookup_pilot",
            "historical_evidence": "authored synthetic onboarding Episode fixtures",
            "groups": list(GROUPS),
            "partition_labels": {"TRANSFER": "archive_required", "BOUNDARY": "live_scope_available"},
            "full_credit": "fresh order and tracking calls for exactly the pre-fixed scope; exact statuses; null aggregate",
            "safe_abstention": "successful current order lookup, no package query and empty-scope answer on archive-required task",
            "attribution": "matching approved historical profile/revision observed before first tracking call",
            "analysis_sha256": _sha(__file__),
            "runner_sha256": _sha(d7_reuse_experiment.__file__),
        },
    }


def outcome(task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    order = task["tool_snapshot"]["order"]
    calls = run["tool_records_oracle"]
    trackers = [call for call in calls if call["tool_name"] == "query_package_tracking"]
    order_confirmed = any(
        call["tool_name"] == "query_order_packages" and call["status"] == "EXECUTED"
        and (call.get("output_data") or {}).get("order_id") == order["order_id"] for call in calls
    )
    try:
        answer = json.loads(run["response_text"].strip().removeprefix("```json\n").removesuffix("\n```"))
    except json.JSONDecodeError:
        answer = None
    passed = run["verdict"]["passed"]
    category = "completed" if passed else "unknown" if passed is None else "other_failure"
    if (run["verdict"]["classification"] == "EVIDENCE_MISSING"
            and task["kind"] == "transfer" and not trackers and order_confirmed
            and answer == {"packages": {}, "all_delivered": None}):
        category = "safe_abstention"

    matching_sources = []
    for call in calls:
        if call["tool_name"] == "query_package_tracking":
            break
        if call["tool_name"] == "read_source_episode" and call["status"] == "EXECUTED":
            data = call["output_data"]
            trace = data["trace"]
            if (data["episode_id"] in order["archive_refs"] and trace["approval_state"] == "APPROVED"
                    and {field: trace[field] for field in ("profile", "revision")} == order["billing_agreement"]):
                matching_sources.append(data["episode_id"])
    return {"category": category, "matching_sources_before_tracking": matching_sources}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        print(freeze_protocol(args.directory, make_protocol()))
        return

    protocol, _ = load_protocol(args.directory)
    source = protocol["source"]
    if (source["scenario"] != "d7_lookup_pilot" or source["groups"] != list(GROUPS)
            or source["analysis_sha256"] != _sha(__file__)
            or source["runner_sha256"] != _sha(d7_reuse_experiment.__file__)):
        raise ValueError("Frozen lookup pilot code or arm selection differs")
    report = d7_reuse_experiment.execute_reuse_experiment(args.directory, groups=GROUPS)
    tasks = {task["task_id"]: task for task in protocol["tasks"]}
    review = {
        "report_sha256": _sha(args.directory / "report.json"),
        "runs": [{"task_id": run["task_id"], "group": run["group"], **outcome(tasks[run["task_id"]], run)}
                 for run in report["cases"]],
    }
    (args.directory / "lookup_review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, ensure_ascii=False))
    if report["status"] != "COMPLETED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
