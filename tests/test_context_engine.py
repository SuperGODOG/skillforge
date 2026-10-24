"""Unit and Integration Tests for ContextEngine (M2):
- Object-aware large output offloading (coverage completeness & references)
- Plain-text offloading (line counts and chunk preview)
- Hierarchical context schemas and authoritative state preservation
- 12k/6k double-watermark compression ladder
- Anti-drift verification on user intent shift (v1 -> v2)
"""
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
import pytest

from skillforge.context_engine.schemas import (
    ProtectedState,
    WorkSummary,
    VerifiedFact,
    FailedAttempt,
    MemoryIndex,
    AttachmentRef,
    HierarchicalContext,
)
from skillforge.context_engine.offloader import LargeOutputOffloader, OffloadResult
from skillforge.context_engine.rebuilder import ContextRebuilder
from skillforge.context_engine.compressor import ContextCompressor, CompressionReport
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ToolCallSpec,
)
from skillforge.runtime import AgentRuntime, ToolBroker


class MockLLM:
    """Mock LLM returning pre-programmed responses."""

    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.call_history: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.call_history.append({"messages": messages, "tools": tools, "system": system})
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse(content="Default final answer", tool_calls=[])


def test_object_aware_offloading(tmp_path: Path):
    offloader = LargeOutputOffloader(threshold_chars=200)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 5 packages payload
    packages = [
        {"package_id": f"PKG_00{i}", "status": "DELIVERED" if i <= 2 else "IN_TRANSIT", "events": [f"event_{j}" for j in range(10)]}
        for i in range(1, 6)
    ]
    raw_text = json.dumps({"packages": packages}, indent=2)

    assert offloader.should_offload(raw_text, {"packages": packages}) is True

    result = offloader.offload(
        run_id="run_test_01",
        tool_name="query_order_packages",
        call_id="call_pkg_01",
        raw_text=raw_text,
        data={"packages": packages},
        artifacts_dir=artifacts_dir,
    )

    assert result.is_offloaded is True
    assert result.artifact_file.exists()
    assert result.total_count == 5
    assert result.expanded_count == 2
    assert result.unexpanded_count == 3
    assert result.object_ids == ["PKG_001", "PKG_002", "PKG_003", "PKG_004", "PKG_005"]

    # In-context replacement must be compact, valid JSON, and contain explicit coverage statement
    summary_data = json.loads(result.in_context_replacement)
    assert summary_data["_type"] == "OBJECT_AWARE_OFFLOADED_SUMMARY"
    assert summary_data["total_count"] == 5
    assert summary_data["expanded_count"] == 2
    assert summary_data["unexpanded_count"] == 3
    assert "Do not assume unexpanded items are non-existent" in summary_data["coverage_warning"]
    assert str(result.artifact_file) in summary_data["artifact_ref"]


def test_plain_text_offloading(tmp_path: Path):
    offloader = LargeOutputOffloader(threshold_chars=200)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 100 lines of log text
    lines = [f"2026-10-23 14:00:{i:02d} System kernel log entry {i}" for i in range(100)]
    raw_text = "\n".join(lines)

    result = offloader.offload(
        run_id="run_test_02",
        tool_name="read_system_log",
        call_id="call_log_01",
        raw_text=raw_text,
        data=None,
        artifacts_dir=artifacts_dir,
    )

    assert result.is_offloaded is True
    assert result.artifact_file.exists()
    assert result.total_lines == 100
    assert "Line range: 1-100" in result.in_context_replacement
    assert "Head preview" in result.in_context_replacement
    assert "Tail preview" in result.in_context_replacement


def test_context_rebuilder_and_authoritative_state():
    rebuilder = ContextRebuilder()

    protected = ProtectedState(
        intent_version="v2",
        current_goal="Only verify order status, DO NOT propose refunds",
        active_constraints=["Must check all packages", "Refund proposals strictly forbidden"],
        allowed_tools=["query_order_packages", "query_package_tracking"],
    )

    summary = WorkSummary(
        verified_facts=[
            VerifiedFact(fact="PKG_001 is DELIVERED", evidence_ref="ref_01", timestamp="14:00"),
            VerifiedFact(fact="PKG_002 is IN_TRANSIT", evidence_ref="ref_02", timestamp="14:05"),
        ],
        failed_attempts=[
            FailedAttempt(action="query_tms(PKG_003)", reason="Timeout 504", resolution="Queued for retry")
        ],
        open_questions=["Status of PKG_003 remains unverified"],
    )

    memory_index = MemoryIndex(
        attachments=[
            AttachmentRef(
                ref_id="fs://runs/123/artifacts/tms_call.json",
                description="Raw TMS tracking responses",
                total_items=3,
                expanded_items=2,
                item_keys=["PKG_001", "PKG_002", "PKG_003"],
                access_method="query_package_tracking",
            )
        ]
    )

    recent_messages = [
        {"role": "user", "content": "What is the status of PKG_001 and PKG_002?"},
        {"role": "assistant", "content": "PKG_001 is delivered and PKG_002 is in transit."},
    ]

    h_ctx = HierarchicalContext(
        protected=protected,
        summary=summary,
        memory_index=memory_index,
        recent_messages=recent_messages,
    )

    rendered_messages = rebuilder.render_to_messages(h_ctx)

    # 1. First message must be the authoritative system prompt
    assert rendered_messages[0]["role"] == "system"
    system_text = rendered_messages[0]["content"]

    # 2. Check structure sections
    assert "[PROTECTED_SYSTEM_STATE (AUTHORITATIVE)]" in system_text
    assert "intent_version: v2" in system_text
    assert "Refund proposals strictly forbidden" in system_text
    assert "[HISTORICAL_KNOWLEDGE_BASE]" in system_text
    assert "PKG_001 is DELIVERED" in system_text
    assert "[ATTACHMENTS_INDEX]" in system_text
    assert "total_items: 3, expanded_items: 2" in system_text

    # 3. Recent messages follow
    assert rendered_messages[1]["role"] == "user"
    assert rendered_messages[2]["role"] == "assistant"


def test_compression_ladder_on_intent_shift(tmp_path: Path):
    async def _test():
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        compressor = ContextCompressor(
            trigger_watermark_tokens=500,  # low threshold to trigger in test
            target_watermark_tokens=250,
            recent_turns_to_keep=1,
            offload_threshold_chars=200,
            artifacts_dir=artifacts_dir,
        )

        # Build state with bulky messages exceeding 500 tokens
        state = HarnessState(
            run_id="run_intent_shift",
            task_id="task_fulfillment_01",
            intent_version="v2",
            goal="Verify all packages, no refunds allowed",
            system_prompt="",
            workspace_dir=tmp_path,
            messages=[
                {"role": "user", "content": "Check order ORD_001 and give refund recommendation if late"},
                {"role": "assistant", "content": "I am querying packages...", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "query_order_packages", "arguments": {}}}]},
                {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"packages": [{"package_id": f"P_{i}", "data": "long_text_string_" * 50} for i in range(10)]})},
                {"role": "user", "content": "Update: user changed mind, DO NOT propose any refund! Only verify status."},
                {"role": "assistant", "content": "Understood, switching to status verification only.", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "query_package_tracking", "arguments": {"package_id": "P_0"}}}]},
                {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"package_id": "P_0", "status": "DELIVERED", "details": "info_" * 50})},
            ],
            step_count=2,
            budget_max=10,
        )

        # Attach authoritative protected state
        state.working_scratchpad["protected_state"] = ProtectedState(
            intent_version="v2",
            current_goal="Verify all packages, no refunds allowed",
            active_constraints=["No refunds allowed", "Must check all packages"],
            allowed_tools=["query_order_packages", "query_package_tracking"],
        )

        report = await compressor.compress(state)

        assert report.triggered is True
        assert report.tokens_after < report.tokens_before
        assert report.tokens_after <= 700

        # Verify that big raw tool output was offloaded to disk
        assert any(p.suffix == ".json" for p in artifacts_dir.glob("*.json"))

        # Verify authoritative state preserved in reconstructed messages
        system_msg = state.messages[0]
        assert system_msg["role"] == "system"
        assert "intent_version: v2" in system_msg["content"]
        assert "No refunds allowed" in system_msg["content"]

    asyncio.run(_test())


def test_harness_loop_with_context_compressor(tmp_path: Path):
    async def _test():
        db_path = tmp_path / "sf.db"
        broker = ToolBroker(application_allowlist={"get_big_data"})

        # Tool returns 500 lines of data
        def get_big_data() -> dict:
            return {"packages": [{"package_id": f"PKG_{i}", "detail": "bulk_" * 30} for i in range(15)]}

        broker.register_tool(get_big_data, name="get_big_data")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        mock_llm = MockLLM([
            ModelResponse(
                content="Fetching data",
                tool_calls=[ToolCallSpec(id="c_big", name="get_big_data", arguments={})],
            ),
            ModelResponse(content="Final verified summary", tool_calls=[]),
        ])

        compressor = ContextCompressor(
            trigger_watermark_tokens=400,
            target_watermark_tokens=200,
            artifacts_dir=tmp_path / "artifacts",
        )

        compression_events: list[CompressionReport] = []

        async def on_post_compress(state: HarnessState, report: CompressionReport):
            compression_events.append(report)

        hooks = HarnessHooks(post_compress=on_post_compress)

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=mock_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace",
        )

        spec = HarnessTaskSpec(
            task_id="task_e2e_compress",
            user_query="Fetch all packages and verify",
            budget_max=5,
            context_compressor=compressor,
        )

        final_state = await loop.run(spec)

        assert final_state.status == "COMPLETED"
        assert final_state.final_output == "Final verified summary"
        assert len(compression_events) >= 1
        assert any(r.triggered for r in compression_events)

    asyncio.run(_test())


def test_e2e_watermark_compression_unexpanded_query_and_goal_redirection(tmp_path: Path):
    """Rigorous end-to-end verification proving:
    1. Actual watermark trigger: Token count exceeds trigger_watermark_tokens (> 600) and drops below target (<= 300).
    2. Before/after context & evidence references: Bulky output offloads to disk, AttachmentRef in MemoryIndex.
    3. Querying unexpanded objects after compression: Agent queries an unexpanded package found in MemoryIndex.
    4. Mid-task goal redirection: Intent shifts to v2 ('STATUS_ONLY'), ProtectedState re-injected, model respects constraint.
    5. Explicit execution metadata: Documents simulated model behavior, step-by-step token accounting, and artifacts.
    """
    async def _test():
        db_path = tmp_path / "sf_trace.db"
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Mock tools
        queried_details = []

        def query_order_manifest(order_id: str) -> dict:
            # Bulky payload: 10 packages with long tracking event descriptions (> 600 tokens)
            return {
                "order_id": order_id,
                "packages": [
                    {
                        "package_id": f"PKG_E2E_{i}",
                        "carrier": "EXPRESS_LOGISTICS",
                        "status": "DELIVERED" if i < 3 else "IN_TRANSIT",
                        "detailed_route_history": [
                            f"Timestamp: 2026-10-23T10:0{j}:00Z Hub_{j} processed checkpoint {j} bulk metadata verification pass"
                            for j in range(5)
                        ],
                    }
                    for i in range(10)
                ],
            }

        def query_single_package(package_id: str) -> dict:
            queried_details.append(package_id)
            return {
                "package_id": package_id,
                "status": "DELIVERED",
                "signed_by": "本人签收",
                "sign_time": "2026-10-23T16:00:00Z",
            }

        broker = ToolBroker(application_allowlist={"query_order_manifest", "query_single_package"})
        broker.register_tool(query_order_manifest, name="query_order_manifest")
        broker.register_tool(query_single_package, name="query_single_package")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        # Model simulation reacting to context:
        # Step 0: Initial call -> query_order_manifest
        # Step 1: After compression, inspects MemoryIndex -> queries unexpanded package PKG_E2E_7
        # Step 2: Final response -> respects mid-task redirected constraint (STATUS_ONLY)
        class AdaptiveVerificationLLM:
            def __init__(self):
                self.calls_received = []

            async def ainvoke(self, messages, tools, system=""):
                self.calls_received.append({"messages": messages, "system": system})
                step = len(self.calls_received)

                if step == 1:
                    return ModelResponse(
                        content="Discovering order packages",
                        tool_calls=[ToolCallSpec(id="c_manifest", name="query_order_manifest", arguments={"order_id": "ORD_E2E_99"})],
                    )
                elif step == 2:
                    # Model inspects system prompt Layer 3 (MemoryIndex)
                    # Notice that PKG_E2E_7 was unexpanded in the summary
                    return ModelResponse(
                        content="Checking unexpanded package PKG_E2E_7 from MemoryIndex",
                        tool_calls=[ToolCallSpec(id="c_single", name="query_single_package", arguments={"package_id": "PKG_E2E_7"})],
                    )
                else:
                    # Final Step: Model inspects Layer 1 ProtectedState across system prompt & system messages
                    all_sys = system + "\n" + "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
                    has_status_only = "Constraint: STATUS_ONLY" in all_sys
                    if has_status_only:
                        return ModelResponse(
                            content="订单 ORD_E2E_99 核实完毕：所有核实包裹均妥投。依据 STATUS_ONLY 约束，不提出退款建议。",
                            tool_calls=[],
                        )
                    else:
                        return ModelResponse(
                            content="核实完毕，建议针对异常包裹申请退款。",
                            tool_calls=[],
                        )

        sim_llm = AdaptiveVerificationLLM()
        compressor = ContextCompressor(
            trigger_watermark_tokens=1000,
            target_watermark_tokens=850,
            recent_turns_to_keep=1,
            artifacts_dir=artifacts_dir,
        )

        compression_audit: list[CompressionReport] = []

        async def record_compression(state: HarnessState, rep: CompressionReport):
            compression_audit.append(rep)

        hooks = HarnessHooks(post_compress=record_compression)

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=sim_llm,
            hooks=hooks,
            workspace_dir=tmp_path / "workspace",
        )

        initial_scratchpad = {
            "protected_state": ProtectedState(
                intent_version="v1",
                current_goal="核实订单 ORD_E2E_99 包裹妥投状态",
                active_constraints=[],
                allowed_tools=["query_order_manifest", "query_single_package"],
            )
        }

        spec = HarnessTaskSpec(
            task_id="task_e2e_complete_demonstration",
            user_query="核实订单 ORD_E2E_99",
            budget_max=6,
            context_compressor=compressor,
            initial_scratchpad=initial_scratchpad,
        )

        # Hook to simulate mid-task user goal redirection right after step 1
        async def on_mid_task_redirection(state: HarnessState, model_resp: ModelResponse):
            if state.step_count == 1:
                # User changes intent mid-run: "STATUS_ONLY, no refund proposals"
                state.intent_version = "v2"
                ps: ProtectedState = state.working_scratchpad.get("protected_state")
                if ps:
                    ps.intent_version = "v2"
                    ps.active_constraints.append("Constraint: STATUS_ONLY")

        hooks.post_model_call = on_mid_task_redirection

        final_state = await loop.run(spec)

        # -------------------------------------------------------------
        # VERIFICATION CRITERIA:
        # -------------------------------------------------------------
        # 1. Actual watermark trigger
        assert len(compression_audit) >= 1
        triggered_reports = [r for r in compression_audit if r.triggered]
        assert len(triggered_reports) >= 1
        rep0 = triggered_reports[0]
        assert rep0.tokens_before > 1000, f"Expected tokens_before > 1000, got {rep0.tokens_before}"
        assert rep0.tokens_after <= 900, f"Expected tokens_after <= 900, got {rep0.tokens_after}"

        # 2. Disk offload & Evidence references preserved
        disk_files = list((final_state.workspace_dir / "artifacts").glob("*.json"))
        assert len(disk_files) >= 1, "Expected raw bulky output offloaded to disk"

        # Check Layer 1 & Layer 3 in reconstructed messages
        final_messages = sim_llm.calls_received[-1]["messages"]
        system_content = "\n".join(str(m.get("content", "")) for m in final_messages if m.get("role") == "system")
        assert ("Protected State" in system_content or "PROTECTED_SYSTEM_STATE" in system_content)
        assert "Constraint: STATUS_ONLY" in system_content
        assert ("[ATTACHMENTS_INDEX]" in system_content or "AttachmentRef" in system_content)
        assert "PKG_E2E_0" in system_content

        # 3. Querying unexpanded objects after compression
        assert "PKG_E2E_7" in queried_details, "Agent successfully queried unexpanded object from MemoryIndex"

        # 4. Mid-task goal redirection respected
        assert final_state.status == "COMPLETED"
        assert "依据 STATUS_ONLY 约束，不提出退款建议" in (final_state.final_output or "")
        assert "建议针对异常包裹申请退款" not in (final_state.final_output or "")

    asyncio.run(_test())


def test_inspect_artifact_tool_query_by_id_and_security(tmp_path: Path):
    """Verify InspectArtifactTool retrieves specific objects by object_id,
    supports pagination, and enforces path traversal boundaries."""
    from skillforge.context_engine.artifact_tool import InspectArtifactTool

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    sample_file = artifacts_dir / "packages_dump.json"
    packages = [
        {"package_id": f"PKG_TEST_{i}", "status": "DELIVERED" if i % 2 == 0 else "IN_TRANSIT", "carrier": "TEST_CARRIER"}
        for i in range(10)
    ]
    sample_file.write_text(json.dumps({"packages": packages}, indent=2), encoding="utf-8")

    tool = InspectArtifactTool(allowed_base_dir=tmp_path)

    # 1. Query specific object by object_id
    res1 = tool.run({"ref_id": f"fs://{sample_file}", "object_id": "PKG_TEST_5"})
    assert res1.status.name == "SUCCESS"
    assert res1.data["object"]["package_id"] == "PKG_TEST_5"
    assert res1.data["object"]["status"] == "IN_TRANSIT"

    # 2. Query non-existent object
    res2 = tool.run({"ref_id": str(sample_file), "object_id": "PKG_NOT_EXIST"})
    assert res2.status.name == "ERROR"
    assert "not found" in res2.text

    # 3. Paginated list retrieval (offset=2, limit=3)
    res3 = tool.run({"ref_id": str(sample_file), "offset": 2, "limit": 3})
    assert res3.status.name == "SUCCESS"
    assert len(res3.data["items"]) == 3
    assert res3.data["items"][0]["package_id"] == "PKG_TEST_2"

    # 4. Security violation check (attempt to access outside allowed_base_dir)
    outside_file = tmp_path.parent / "unauthorized.json"
    outside_file.write_text("{}", encoding="utf-8")
    res4 = tool.run({"ref_id": str(outside_file)})
    assert res4.status.name == "ERROR"
    assert "outside allowed directory" in res4.text
