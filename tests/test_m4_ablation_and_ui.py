"""Tests for M4: Ablation Benchmark Runner and Dual-Channel Terminal UI."""
import asyncio
from pathlib import Path
import pytest

from skillforge.scenarios.ablation_benchmark import (
    AblationBenchmarkRunner,
    SimulatedFulfillmentLLM,
)
from skillforge.cli_ui import DualChannelConsole, create_dual_channel_hooks
from skillforge.harness_loop import (
    AsyncHarnessLoop,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ToolCallSpec,
)
from skillforge.runtime import AgentRuntime, ToolBroker


def test_ablation_benchmark_execution(tmp_path: Path):
    async def _test():
        runner = AblationBenchmarkRunner(db_dir=tmp_path)

        tasks = [
            {
                "task_id": "T1_NORM",
                "order_id": "ORD_DEV_0102",
                "query": "核实订单 ORD_DEV_0102 中3个包裹的签收情况。",
                "constraint": None,
            },
            {
                "task_id": "T2_GOAL_SHIFT",
                "order_id": "ORD_DEV_0601",
                "query": "核实订单 ORD_DEV_0601 全部包裹状态，只列出状态，不提出任何后续建议。",
                "constraint": "STATUS_ONLY",
            },
        ]

        summaries = await runner.run_benchmark_suite(tasks=tasks)

        assert "GroupA_FullHistory" in summaries
        assert "GroupB_SlidingWindow" in summaries
        assert "GroupC_SkillForge" in summaries

        group_a = summaries["GroupA_FullHistory"]
        group_b = summaries["GroupB_SlidingWindow"]
        group_c = summaries["GroupC_SkillForge"]

        # Group C achieves full compliance and complete coverage
        assert group_c.success_rate_pct == 100.0
        assert group_c.total_violations == 0
        assert group_c.avg_evidence_coverage_pct == 100.0

        # Group B has redundant queries due to sliding window amnesia
        assert group_b.total_redundant_calls >= 1

        # Markdown report rendering check
        report_md = runner.render_markdown_report(summaries)
        assert "| **GroupA_FullHistory** |" in report_md
        assert "| **GroupB_SlidingWindow** |" in report_md
        assert "| **GroupC_SkillForge** |" in report_md
        assert "核心发现与架构归因" in report_md

    asyncio.run(_test())


def test_dual_channel_ui_integration(tmp_path: Path):
    async def _test():
        ui = DualChannelConsole()
        hooks = create_dual_channel_hooks(ui)

        db_path = tmp_path / "sf.db"
        broker = ToolBroker(application_allowlist={"mock_ping"})
        broker.register_tool(lambda: "pong", name="mock_ping")
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

        class QuickLLM:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, messages, tools, system=""):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(
                        content="Checking system health...",
                        tool_calls=[ToolCallSpec(id="c1", name="mock_ping", arguments={})],
                    )
                return ModelResponse(content="System healthy and verified.", tool_calls=[])

        loop = AsyncHarnessLoop(
            runtime=runtime,
            tool_broker=broker,
            llm_client=QuickLLM(),
            hooks=hooks,
            workspace_dir=tmp_path / "workspace",
        )

        spec = HarnessTaskSpec(
            task_id="task_ui_test",
            user_query="Ping system",
            budget_max=3,
        )

        final_state = await loop.run(spec)
        assert final_state.status == "COMPLETED"
        assert final_state.final_output == "System healthy and verified."

    asyncio.run(_test())
