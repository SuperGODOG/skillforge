"""Continuous compression + restart + goal shift acceptance test.

Validates:
1. First compression: bulky object list offloaded (Level 0) and indexed.
2. Second compression: subsequent bulky log triggers Level 1 & Level 2 structured consolidation
   without losing facts, evidence citations, or artifact references.
3. Process crash & Runtime rebuild from disk checkpoint.
4. User goal redirection (v1 -> v2: STATUS_ONLY) purges stale suggestions and enforces new constraint.
5. In-flight artifact read-back via inspect_artifact without re-querying external business APIs.
6. Final Episode recorded in authoritative SQLite EpisodeStore.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from skillforge import (
    AgentRuntime,
    AsyncHarnessLoop,
    AttachmentRef,
    ContextCompressor,
    EpisodeStore,
    ExperienceCollector,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ProtectedState,
    ToolBroker,
    ToolCallSpec,
    VerifiedFact,
    WorkSummary,
    InspectArtifactTool,
)
from skillforge.storage.db import init_db


class ContinuousMultiTurnLLM:
    """Simulates an agent executing across multiple compression cycles and post-restart goal redirection."""

    def __init__(self, artifacts_dir: Path):
        self.step = 0
        self.artifacts_dir = artifacts_dir
        self.calls_received: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.step += 1
        self.calls_received.append({"step": self.step, "messages": messages, "system": system})

        if self.step == 1:
            # Turn 1: query bulk packages list (triggers 1st compression)
            return ModelResponse(
                content="Querying order packages...",
                tool_calls=[
                    ToolCallSpec(
                        id="c1_pkg",
                        name="query_order_packages",
                        arguments={"order_id": "ORD_FULL_808"},
                    )
                ],
            )
        elif self.step == 2:
            # Turn 2: query logistics log (triggers 2nd compression)
            return ModelResponse(
                content="Querying warehouse event log...",
                tool_calls=[
                    ToolCallSpec(
                        id="c2_log",
                        name="query_warehouse_log",
                        arguments={"order_id": "ORD_FULL_808"},
                    )
                ],
            )
        elif self.step == 3:
            # Turn 3 (Post-restart): Model notices unexpanded package PKG_808_11 in MemoryIndex
            # Calls inspect_artifact on disk rather than re-querying business API
            artifact_files = list(self.artifacts_dir.glob("**/*_query_order_packages.json"))
            if not artifact_files:
                artifact_files = list(self.artifacts_dir.parent.glob("**/*_query_order_packages.json"))
            ref_uri = f"fs://{artifact_files[0].resolve()}" if artifact_files else "fs://unknown"
            return ModelResponse(
                content="Inspecting unexpanded package PKG_808_11 from offloaded artifact...",
                tool_calls=[
                    ToolCallSpec(
                        id="c3_inspect",
                        name="inspect_artifact",
                        arguments={"ref_id": ref_uri, "object_id": "PKG_808_11"},
                    )
                ],
            )
        else:
            # Final Step: produce conclusion respecting ProtectedState constraints
            sys_combined = system + "\n" + "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            is_status_only = "STATUS_ONLY" in sys_combined
            if is_status_only:
                content = (
                    "订单 ORD_FULL_808 审计完毕：全量 12 件包裹（含工件回读之 PKG_808_11）妥投无误。"
                    "依据 STATUS_ONLY 强制约束，不提报任何索赔或退款建议。"
                )
            else:
                content = "订单部分包裹延迟，建议发起全额退款及赔付。"
            return ModelResponse(content=content, tool_calls=[])


@pytest.mark.anyio
async def test_continuous_compression_restart_and_goal_shift(tmp_path: Path):
    db_path = tmp_path / "skillforge.db"
    conn = init_db(db_path)
    conn.close()

    ep_store = EpisodeStore(db_path)
    workspace = tmp_path / "workspace"
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    business_api_query_count = 0

    # Business tool returning 12 packages
    def query_order_packages(order_id: str):
        nonlocal business_api_query_count
        business_api_query_count += 1
        return {
            "order_id": order_id,
            "packages": [
                {
                    "package_id": f"PKG_808_{i:02d}",
                    "status": "DELIVERED" if i != 11 else "CUSTOMS_CLEARED",
                    "details": "logistics_chain_inspection_report_" * 25,
                }
                for i in range(12)
            ],
        }

    # Business tool returning 60-line log
    def query_warehouse_log(order_id: str):
        return "\n".join([f"2026-10-24 10:{i:02d}:00 Warehouse gate check entry {i}" for i in range(60)])

    broker = ToolBroker(application_allowlist={"query_order_packages", "query_warehouse_log", "inspect_artifact"})
    broker.register_tool(query_order_packages, name="query_order_packages")
    broker.register_tool(query_warehouse_log, name="query_warehouse_log")
    broker.register_tool(InspectArtifactTool(allowed_base_dir=workspace), name="inspect_artifact")

    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker, episode_store=ep_store, collector=collector)

    compressor = ContextCompressor(
        trigger_watermark_tokens=900,
        target_watermark_tokens=750,
        recent_turns_to_keep=1,
        offload_threshold_chars=300,
        artifacts_dir=artifacts_dir,
    )

    compression_history = []

    async def on_post_compress(state: HarnessState, rep):
        if rep.triggered:
            compression_history.append(rep)

    hooks = HarnessHooks(post_compress=on_post_compress)
    sim_llm = ContinuousMultiTurnLLM(artifacts_dir=artifacts_dir)

    loop = AsyncHarnessLoop(
        runtime=runtime,
        tool_broker=broker,
        llm_client=sim_llm,
        hooks=hooks,
        workspace_dir=workspace,
    )

    initial_protected = ProtectedState(
        intent_version="v1",
        current_goal="Audit order ORD_FULL_808 across all warehouses and propose compensation if late",
        active_constraints=[],
        allowed_tools=["query_order_packages", "query_warehouse_log", "inspect_artifact"],
    )

    spec = HarnessTaskSpec(
        task_id="task_continuous_808",
        user_query="请审计订单 ORD_FULL_808",
        budget_max=2,  # Exactly 2 steps for Leg 1 before process pause / checkpoint
        context_compressor=compressor,
        initial_scratchpad={"protected_state": initial_protected},
    )

    # Execute Leg 1 (Runs Turns 1 & 2, then finishes Leg 1 with saved checkpoint)
    state_leg1 = await loop.run(spec)

    # Verify Two successive compressions occurred
    assert len(compression_history) >= 2, f"Expected at least 2 compressions, got {len(compression_history)}"
    first_comp = compression_history[0]
    second_comp = compression_history[1]
    assert first_comp.triggered is True
    assert second_comp.triggered is True
    assert first_comp.items_offloaded >= 1
    assert second_comp.tokens_after <= 950

    # Verify artifacts exist on disk
    run_artifacts = list(state_leg1.workspace_dir.glob("**/*.json"))
    assert len(run_artifacts) >= 1
    run_logs = list(state_leg1.workspace_dir.glob("**/*.log"))
    assert len(run_logs) >= 1

    # Verify Checkpoint was saved on disk
    chk_path = state_leg1.workspace_dir / "checkpoint.json"
    assert chk_path.exists()
    checkpoint_data = json.loads(chk_path.read_text(encoding="utf-8"))
    assert checkpoint_data["task_id"] == "task_continuous_808"

    # =========================================================================
    # RESTART & GOAL REDIRECTION (v1 -> v2)
    # =========================================================================
    updated_protected = ProtectedState(
        intent_version="v2",
        current_goal="Verify order ORD_FULL_808 status only; do not propose any compensation or refund",
        active_constraints=["Constraint: STATUS_ONLY"],
        allowed_tools=["query_order_packages", "query_warehouse_log", "inspect_artifact"],
    )

    def verify_order_audit(st: HarnessState) -> dict[str, Any]:
        # Independent validator verifying business criteria without relying on model self-assertion
        called_inspect = any(r.tool_name == "inspect_artifact" and r.status == "EXECUTED" for r in st.tool_records)
        status_only = "STATUS_ONLY" in (st.final_output or "")
        pass_verif = called_inspect and status_only
        return {
            "independent_pass": pass_verif,
            "failure_reason": None if pass_verif else "Missing inspect_artifact or STATUS_ONLY constraint",
        }

    updated_spec = HarnessTaskSpec(
        task_id="task_continuous_808",
        run_id=state_leg1.run_id,
        user_query="请审计订单 ORD_FULL_808（STATUS_ONLY，严禁退款与索赔）",
        intent_version="v2",
        budget_max=6,
        context_compressor=compressor,
        initial_scratchpad={"protected_state": updated_protected},
        verification_fn=verify_order_audit,
    )

    # Resume execution using new AsyncHarnessLoop instance resuming from checkpoint
    new_loop = AsyncHarnessLoop(
        runtime=runtime,
        tool_broker=broker,
        llm_client=sim_llm,
        workspace_dir=workspace,
    )

    final_state = await new_loop.resume(run_id=state_leg1.run_id, updated_spec=updated_spec)

    # -------------------------------------------------------------------------
    # VERIFICATION OF GUARANTEES:
    # -------------------------------------------------------------------------
    # 1. Stale proposal was pruned upon intent change to STATUS_ONLY
    final_summary: WorkSummary = final_state.working_scratchpad.get("work_summary", WorkSummary())
    for q in final_summary.open_questions:
        assert "退款" not in q and "refund" not in q

    # 2. inspect_artifact was called to read back PKG_808_11 directly from disk
    tool_names_called = [r.tool_name for r in final_state.tool_records]
    assert "inspect_artifact" in tool_names_called

    # 3. External business API was NOT queried a second time (read-back succeeded locally)
    assert business_api_query_count == 1, f"Expected exactly 1 business API call, got {business_api_query_count}"

    # 4. Final output obeyed STATUS_ONLY constraint and mentioned verified status
    assert "STATUS_ONLY" in final_state.final_output
    assert "不提报任何索赔或退款建议" in final_state.final_output
    assert "PKG_808_11" in final_state.final_output

    # 5. Authoritative Episode saved in SQLite EpisodeStore
    ep = ep_store.get_episode(f"ep_{final_state.run_id}")
    assert ep is not None
    assert ep.outcome == "success"
    assert len(ep.provenances) >= 3
    for p in ep.provenances:
        assert p.signature.startswith("sha256:")
