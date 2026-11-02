"""Generate authoritative business hooks samples for SkillForge.

Executes real AsyncHarnessLoop, AgentRuntime, ToolBroker, ExperienceCollector, and Oracles
to serialize verifiable execution artifacts for 5 canonical scenarios:
1. normal_logistics_fulfillment (Oracle pass, single success episode)
2. business_hallucination_denied (In-transit over-claim blocked, failure episode)
3. read_only_write_operation_denied (BeforeTool constraint denial, handler_count=0)
4. safe_view_offload_and_drilldown (AfterTool redaction, disk offload, inspect drilldown)
5. review_pause_and_full_reconstruction_resume (Pause, DB reload, resume without duplicate calls)
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from skillforge.collector import ExperienceCollector
from skillforge.context_engine.artifact_tool import InspectArtifactTool
from skillforge.context_engine.schemas import ProtectedState
from skillforge.episode import EpisodeStore
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    HookDecision,
    ModelResponse,
    ToolCallSpec,
    create_business_oracle_finalize_hook,
    create_read_only_constraint_hook,
    create_sensitive_data_redaction_hook,
)
from skillforge.models import ToolCallRecord
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)


class ScriptedMockLLM:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)

    async def ainvoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], system: str = "") -> ModelResponse:
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse(content="Final default output", tool_calls=[])


async def generate_samples() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        samples: dict[str, Any] = {}

        # ----------------------------------------------------------------------
        # 1. Normal Logistics Fulfillment
        # ----------------------------------------------------------------------
        db_1 = tmp_dir / "case1.db"
        ws_1 = tmp_dir / "ws1"
        broker_1 = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
        broker_1.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
        broker_1.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
        ep_store_1 = EpisodeStore(db_path=db_1)
        collector_1 = ExperienceCollector(episode_store=ep_store_1)
        runtime_1 = AgentRuntime(db_path=db_1, tool_broker=broker_1, collector=collector_1)

        finalize_hook_1 = create_business_oracle_finalize_hook(
            oracle_fn=verify_logistics_fulfillment,
            order_id="ORD_2026_0901",
            hook_id="logistics_fulfillment_oracle",
        )
        llm_1 = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_pkgs", name="query_order_packages", arguments={"order_id": "ORD_2026_0901"})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_p1", name="query_package_tracking", arguments={"package_id": "PKG_101"})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_p2", name="query_package_tracking", arguments={"package_id": "PKG_102"})]),
            ModelResponse(content="订单 ORD_2026_0901 的两个包裹 PKG_101 与 PKG_102 均已签收送达。", tool_calls=[]),
        ])
        loop_1 = AsyncHarnessLoop(runtime=runtime_1, tool_broker=broker_1, llm_client=llm_1, hooks=HarnessHooks(before_finalize=finalize_hook_1), workspace_dir=ws_1)
        state_1 = await loop_1.run(HarnessTaskSpec(task_id="task_case1_fulfillment", user_query="Query ORD_2026_0901"))
        ep_1 = ep_store_1.get_episode(f"ep_{state_1.run_id}")

        samples["normal_logistics_fulfillment"] = {
            "scenario": "Fully verified logistics query passed by Oracle",
            "run_id": state_1.run_id,
            "status": state_1.status,
            "final_output": state_1.final_output,
            "tool_records_count": len(state_1.tool_records),
            "hook_audits": state_1.hook_audits,
            "verification_evidence": state_1.working_scratchpad.get("verification_evidence"),
            "episode": asdict(ep_1) if ep_1 else None,
        }

        # ----------------------------------------------------------------------
        # 2. Business Hallucination Denied
        # ----------------------------------------------------------------------
        db_2 = tmp_dir / "case2.db"
        ws_2 = tmp_dir / "ws2"
        broker_2 = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
        broker_2.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
        broker_2.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
        ep_store_2 = EpisodeStore(db_path=db_2)
        collector_2 = ExperienceCollector(episode_store=ep_store_2)
        runtime_2 = AgentRuntime(db_path=db_2, tool_broker=broker_2, collector=collector_2)

        finalize_hook_2 = create_business_oracle_finalize_hook(
            oracle_fn=verify_logistics_fulfillment,
            order_id="ORD_2026_0902",
            hook_id="logistics_fulfillment_oracle",
        )
        llm_2 = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_pkgs2", name="query_order_packages", arguments={"order_id": "ORD_2026_0902"})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_p201", name="query_package_tracking", arguments={"package_id": "PKG_201"})]),
            ModelResponse(content="订单 ORD_2026_0902 全部包裹已签收送达，请放心。", tool_calls=[]),
        ])
        loop_2 = AsyncHarnessLoop(runtime=runtime_2, tool_broker=broker_2, llm_client=llm_2, hooks=HarnessHooks(before_finalize=finalize_hook_2), workspace_dir=ws_2)
        state_2 = await loop_2.run(HarnessTaskSpec(task_id="task_case2_hallucination", user_query="Query ORD_2026_0902"))
        ep_2 = ep_store_2.get_episode(f"ep_{state_2.run_id}")

        samples["business_hallucination_denied"] = {
            "scenario": "In-transit package falsely claimed as all-delivered blocked by Oracle",
            "run_id": state_2.run_id,
            "status": state_2.status,
            "final_output": state_2.final_output,
            "proposed_output_retained": state_2.working_scratchpad.get("proposed_output"),
            "hook_audits": state_2.hook_audits,
            "verification_evidence": state_2.working_scratchpad.get("verification_evidence"),
            "episode": asdict(ep_2) if ep_2 else None,
        }

        # ----------------------------------------------------------------------
        # 3. Read-Only Write Operation Denied
        # ----------------------------------------------------------------------
        db_3 = tmp_dir / "case3.db"
        ws_3 = tmp_dir / "ws3"
        broker_3 = ToolBroker(application_allowlist={"query_order", "refund_order"})
        broker_3.register_tool(lambda order_id: f"Order {order_id} active", name="query_order")
        broker_3.register_tool(lambda order_id, amount: f"Refunded {amount}", name="refund_order")
        runtime_3 = AgentRuntime(db_path=db_3, tool_broker=broker_3)

        read_only_hook = create_read_only_constraint_hook(
            tool_effects={"refund_order": "side_effect", "query_order": "read"},
            hook_id="security_read_only_gate",
        )
        llm_3 = ScriptedMockLLM([
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_q", name="query_order", arguments={"order_id": "ORD_1"})]),
            ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_ref", name="refund_order", arguments={"order_id": "ORD_1", "amount": 100.0})]),
            ModelResponse(content="Refund was denied by policy.", tool_calls=[]),
        ])
        loop_3 = AsyncHarnessLoop(runtime=runtime_3, tool_broker=broker_3, llm_client=llm_3, hooks=HarnessHooks(pre_tool_exec=read_only_hook), workspace_dir=ws_3)
        protected_3 = ProtectedState(intent_version="v1", current_goal="Audit", active_constraints=["READ_ONLY"])
        state_3 = await loop_3.run(HarnessTaskSpec(task_id="task_case3_readonly", user_query="Audit ORD_1", initial_scratchpad={"protected_state": protected_3}))

        samples["read_only_write_operation_denied"] = {
            "scenario": "Side-effect tool refund_order intercepted by BeforeTool under READ_ONLY constraint",
            "run_id": state_3.run_id,
            "status": state_3.status,
            "final_output": state_3.final_output,
            "tool_records": [asdict(r) for r in state_3.tool_records],
            "hook_audits": state_3.hook_audits,
            "sqlite_tool_calls": [asdict(c) for c in runtime_3.list_tool_calls(state_3.run_id)],
        }

        # ----------------------------------------------------------------------
        # 4. Safe View Offload and Drilldown
        # ----------------------------------------------------------------------
        db_4 = tmp_dir / "case4.db"
        ws_4 = tmp_dir / "ws4"
        broker_4 = ToolBroker(application_allowlist={"fetch_sensitive_customers", "inspect_artifact"})
        raw_custs = [
            {"id": f"CUST_{i:03d}", "name": f"User_{i}", "customer_email": f"user_{i}@synthetic-domain.com", "token": f"AUTH_KEY_{i:04d}", "padding": "x" * 80}
            for i in range(20)
        ]
        broker_4.register_tool(lambda: {"total": len(raw_custs), "customers": raw_custs}, name="fetch_sensitive_customers")
        broker_4.register_tool(InspectArtifactTool(allowed_base_dir=ws_4), name="inspect_artifact")
        runtime_4 = AgentRuntime(db_path=db_4, tool_broker=broker_4)

        redaction_hook = create_sensitive_data_redaction_hook(
            redact_patterns={r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+": "[REDACTED_EMAIL]", r"AUTH_KEY_\d+": "[REDACTED_TOKEN]"},
            filter_keys=["customer_email", "token"],
            hook_id="security_sanitizer",
            critical=True,
        )

        class InteractiveDrilldownLLM:
            def __init__(self):
                self.turn = 0
            async def ainvoke(self, messages, tools, system=""):
                self.turn += 1
                if self.turn == 1:
                    return ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_f", name="fetch_sensitive_customers", arguments={})])
                elif self.turn == 2:
                    last_msg = [m for m in messages if m.get("role") == "tool"][-1]["content"]
                    m_uri = re.search(r"fs://[^\s\n\"',]+", last_msg)
                    return ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_i", name="inspect_artifact", arguments={"ref_id": m_uri.group(0), "object_id": "CUST_001"})])
                return ModelResponse(content="Inspected customer safely", tool_calls=[])

        loop_4 = AsyncHarnessLoop(runtime=runtime_4, tool_broker=broker_4, llm_client=InteractiveDrilldownLLM(), hooks=HarnessHooks(post_tool_exec=redaction_hook), workspace_dir=ws_4)
        state_4 = await loop_4.run(HarnessTaskSpec(task_id="task_case4_safe_view", user_query="Audit customer tokens"))

        samples["safe_view_offload_and_drilldown"] = {
            "scenario": "Sensitive dataset sanitized in-flight, offloaded to artifact, drilled down via InspectArtifactTool",
            "run_id": state_4.run_id,
            "status": state_4.status,
            "messages": state_4.messages,
            "raw_record_retained_secret": any("user_1@synthetic-domain.com" in json.dumps(r.output_data) for r in state_4.tool_records if r.output_data),
            "hook_audits": state_4.hook_audits,
        }

        # ----------------------------------------------------------------------
        # 5. Review Pause and Reconstructed Resume
        # ----------------------------------------------------------------------
        db_5 = tmp_dir / "case5.db"
        ws_5 = tmp_dir / "ws5"
        run_id_5 = "run_case5_reconstruct"

        step1_calls = 0
        step2_calls = 0
        def s1():
            nonlocal step1_calls; step1_calls += 1; return "step1_ok"
        def s2():
            nonlocal step2_calls; step2_calls += 1; return "step2_ok"

        broker_5a = ToolBroker(application_allowlist={"s1", "s2"})
        broker_5a.register_tool(s1, name="s1")
        broker_5a.register_tool(s2, name="s2")
        ep_store_5a = EpisodeStore(db_path=db_5)
        collector_5a = ExperienceCollector(episode_store=ep_store_5a)
        runtime_5a = AgentRuntime(db_path=db_5, tool_broker=broker_5a, collector=collector_5a)

        loop_5a = AsyncHarnessLoop(
            runtime=runtime_5a,
            tool_broker=broker_5a,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_s1", name="s1", arguments={})]),
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_s2", name="s2", arguments={})]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.review("MANUAL_APPROVAL_REQUIRED") if tc.name == "s2" else HookDecision.allow()),
            workspace_dir=ws_5,
        )
        state_5a = await loop_5a.run(HarnessTaskSpec(task_id="task_case5_rev", user_query="Multi-step flow", run_id=run_id_5, budget_max=5))

        # Reconstruct runtime from disk
        ep_store_5b = EpisodeStore(db_path=db_5)
        collector_5b = ExperienceCollector(episode_store=ep_store_5b)
        broker_5b = ToolBroker(application_allowlist={"s1", "s2"})
        broker_5b.register_tool(s1, name="s1")
        broker_5b.register_tool(s2, name="s2")
        runtime_5b = AgentRuntime(db_path=db_5, tool_broker=broker_5b, collector=collector_5b)

        loop_5b = AsyncHarnessLoop(
            runtime=runtime_5b,
            tool_broker=broker_5b,
            llm_client=ScriptedMockLLM([
                ModelResponse(content="", tool_calls=[ToolCallSpec(id="c_s2_res", name="s2", arguments={})]),
                ModelResponse(content="Flow safely finished after resume", tool_calls=[]),
            ]),
            hooks=HarnessHooks(pre_tool_exec=lambda s, tc: HookDecision.allow()),
            workspace_dir=ws_5,
        )
        state_5b = await loop_5b.resume(run_id=run_id_5)
        ep_5b = ep_store_5b.get_episode(f"ep_{run_id_5}")

        samples["review_pause_and_full_reconstruction_resume"] = {
            "scenario": "Task paused for human review, Runtime rebuilt from SQLite, resumed without duplicate executions",
            "run_id": run_id_5,
            "status": state_5b.status,
            "paused_state": {"status": state_5a.status, "step1_calls": step1_calls, "step2_calls": 0},
            "resumed_state": {"status": state_5b.status, "final_output": state_5b.final_output, "step1_calls": step1_calls, "step2_calls": step2_calls},
            "hook_audits": state_5a.hook_audits + state_5b.hook_audits,
            "episode": asdict(ep_5b) if ep_5b else None,
        }

    return samples


if __name__ == "__main__":
    out_path = Path("docs/fixtures/hooks_business_samples.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = asyncio.run(generate_samples())
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {len(data)} canonical business hook samples to {out_path}")
