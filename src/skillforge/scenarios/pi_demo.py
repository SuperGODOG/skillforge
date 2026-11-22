"""Phase 3C Logistics Demo with Pi Agent SDK Execution Backend.

Provides a keyless offline demonstration of:
- CandidateSkill draft trial execution within isolated sandbox and bounded budget.
- Official Pi runner subprocess execution with faux model provider (test_mode=True).
- ToolBroker dispatch for read-only logistics tools (QueryOrderPackages, QueryPackageTracking).
- Authoritative independent business verification Oracle (verify_logistics_fulfillment).
- Episode generation and persistence via ExperienceCollector and EpisodeStore.
- Clean process reaping without zombie leftovers.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

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


def run_pi_demo(output_dir: Path) -> dict[str, Any]:
    """Execute keyless Pi CLI demo and persist artifacts under output_dir.

    Creates an isolated unique run subdirectory (`pi_demo_<uuid>`) to guarantee
    existing files are never overwritten. Returns report dictionary with full
    audit metadata.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_uuid = uuid.uuid4().hex
    run_dir = output_dir / f"pi_demo_{run_uuid}"
    run_dir.mkdir(parents=True, exist_ok=False)

    db_path = run_dir / "runtime.db"
    episodes_db = run_dir / "episodes.db"
    skills_dir = run_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"

    episode_store = EpisodeStore(db_path=episodes_db)
    registry = SkillRegistry(db_path=run_dir / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(
        application_allowlist={"query_order_packages", "query_package_tracking"}
    )
    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())

    collector = ExperienceCollector(episode_store=episode_store, registry=registry)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
    )

    try:
        skill_name = "logistics_tracking_demo"
        candidate_id = f"cand_demo_{run_uuid[:8]}"
        draft = CandidateSkill(
            candidate_id=candidate_id,
            skill_name=skill_name,
            decision="create",
            meta=SkillMeta(
                name=skill_name,
                version="0.1.0-draft",
                description="物流多包裹核验 Demo 草稿技能",
                use_when="核验订单物流状态与各包裹签收情况",
                dependencies=["query_order_packages", "query_package_tracking"],
            ),
            body="先列全包裹逐个核验，不能以部分签收声称全部",
            source_requirement="演示未上线的物流多包裹核验草稿在独立沙箱与受限预算下的试运行",
            status="DRAFT",
        )

        run_id = f"run_{run_uuid[:8]}"
        task_id = f"task_{run_uuid[:8]}"
        budget_max = 10
        deadline_ts = time.time() + 30.0

        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            candidate=draft,
            purpose="evaluation",
            budget_max=budget_max,
            deadline_ts=deadline_ts,
        )

        order_id = "ORD_2026_0902"
        call_prefix = f"call_{run_uuid[:8]}"

        model_config = {
            "provider": "faux",
            "responses": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": f"{call_prefix}_1",
                            "name": "query_order_packages",
                            "arguments": {"order_id": order_id},
                        }
                    ],
                    "stopReason": "toolUse",
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": f"{call_prefix}_2",
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
                            "type": "toolCall",
                            "id": f"{call_prefix}_3",
                            "name": "query_package_tracking",
                            "arguments": {"package_id": "PKG_202"},
                        }
                    ],
                    "stopReason": "toolUse",
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"订单 {order_id} 包含2个包裹：包裹 PKG_201 由 SF_EXPRESS 承运，状态为 DELIVERED（已签收）；"
                                f"包裹 PKG_202 由 ZTO_EXPRESS 承运，状态为 IN_TRANSIT（运输中）。订单尚未全部签收。"
                            ),
                        }
                    ],
                    "stopReason": "stop",
                },
            ],
        }

        final_text = runtime.run_agent_with_pi(
            run_id=run_id,
            input_text=f"核验订单 {order_id} 下全部包裹的物流状态",
            candidate=draft,
            tools=["query_order_packages", "query_package_tracking"],
            model_config=model_config,
            test_mode=True,
            timeout=25.0,
        )

        tool_records = runtime.list_tool_calls(run_id)
        oracle = verify_logistics_fulfillment(
            model_output=final_text,
            order_id=order_id,
            tool_records=tool_records,
        )

        run_rec, episode = runtime.finalize_run(
            run_id=run_id,
            model_output=final_text,
            verification_evidence=oracle,
        )

        provenances = runtime.get_provenances(run_id)
        pi_pid = runtime.get_last_pi_pid(run_id)

        report: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "candidate_id": draft.candidate_id,
            "frozen_body_hash": run_rec.content_hash if run_rec else None,
            "run_status": run_rec.status if run_rec else "UNKNOWN",
            "purpose": run_rec.purpose if run_rec else "evaluation",
            "budget_max": run_rec.budget_max if run_rec else budget_max,
            "budget_consumed": run_rec.budget_consumed if run_rec else 0,
            "deadline_ts": run_rec.deadline_ts if run_rec else deadline_ts,
            "text": final_text,
            "tool_records": [asdict(r) for r in tool_records],
            "provenances": [asdict(p) for p in provenances],
            "oracle": oracle,
            "episode_id": episode.episode_id if episode else None,
            "episode_outcome": episode.outcome if episode else "unknown",
            "pi_pid": pi_pid,
            "tokens": None,
            "money": None,
            "execution": "actual_pi_runtime_broker_collector",
            "model": "faux",
            "data": "synthetic",
            "os_sandbox": "not_exercised",
            "evidence_labels": {
                "execution": "actual_pi_runtime_broker_collector",
                "model": "faux",
                "data": "synthetic",
                "os_sandbox": "not_exercised",
            },
            "report_path": str(report_path),
            "run_dir": str(run_dir),
            "artifacts": {
                "report_json": str(report_path),
                "runtime_db": str(db_path),
                "episodes_db": str(episodes_db),
            },
        }

        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return report
    finally:
        runtime.close()
        registry.close()
        episode_store.close()
