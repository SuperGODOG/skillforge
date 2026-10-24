"""Minimal Rich Terminal UI & Event Consumer for SkillForge Harness (M4).

Provides:
- Dual-Channel Event Stream:
  1. Answer Channel: User-facing conclusions, explanations, and final proposals.
  2. Execution Channel: Live status panel (Intent version, Step budget, Tool dispatch state, Token gauge).
- Pure event consumer: Zero interference with core runtime business logic.
- 3 Repeatable Interview Demo Walkthroughs:
  - Demo 1: Multi-package long task with object-aware offloading.
  - Demo 2: Mid-flight goal shift with authoritative state reinjection.
  - Demo 3: Failure attribution to governed skill promotion and reuse.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

from .harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ToolCallSpec,
)
from .context_engine import ContextCompressor, CompressionReport


class DualChannelConsole:
    """Renders user answer channel and execution channel side-by-side or stacked in terminal."""

    def __init__(self) -> None:
        if _HAS_RICH:
            self.console = Console()
        else:
            self.console = None

    def print_answer_chunk(self, text: str) -> None:
        """Stream or print user-facing conclusion."""
        if _HAS_RICH and self.console:
            self.console.print(f"[bold green]💬 [User Answer Channel][/bold green] {text}")
        else:
            print(f"[Answer] {text}")

    def render_execution_status(
        self,
        state: HarnessState,
        active_tool: Optional[str] = None,
        tool_status: str = "IDLE",
    ) -> None:
        """Render live execution state panel."""
        if not _HAS_RICH or not self.console:
            print(
                f"[Execution] Step {state.step_count}/{state.budget_max} | "
                f"Intent: {state.intent_version} | Status: {state.status} | "
                f"Tool: {active_tool or 'none'} ({tool_status})"
            )
            return

        table = Table(title="⚙️ Harness Execution Dashboard", show_header=True, header_style="bold cyan")
        table.add_column("Property", style="dim", width=20)
        table.add_column("Value", style="bold")

        table.add_row("Run ID", state.run_id)
        table.add_row("Task ID", state.task_id)
        table.add_row("Intent Version", f"[yellow]{state.intent_version}[/yellow]")
        table.add_row("Step Budget", f"{state.step_count} / {state.budget_max}")
        table.add_row("Run Status", f"[{'green' if state.status == 'COMPLETED' else 'cyan'}]{state.status}[/]")
        table.add_row("Active Tool", f"{active_tool or 'None'} ({tool_status})")
        table.add_row("Tools Executed", str(len(state.tool_records)))

        panel = Panel(table, border_style="blue", title="SkillForge Control Plane")
        self.console.print(panel)


def create_dual_channel_hooks(ui: DualChannelConsole) -> HarnessHooks:
    """Create HarnessHooks wired to the dual-channel terminal interface."""
    async def post_model(state: HarnessState, resp: ModelResponse):
        if resp.content:
            ui.print_answer_chunk(resp.content)
        ui.render_execution_status(state, active_tool="None", tool_status="INFERENCE_DONE")

    async def pre_tool(state: HarnessState, tool_spec: ToolCallSpec):
        ui.render_execution_status(state, active_tool=tool_spec.name, tool_status="RUNNING")

    async def post_tool(state: HarnessState, record: Any):
        ui.render_execution_status(state, active_tool=record.tool_name, tool_status=record.status)

    async def post_compress(state: HarnessState, report: Any):
        if getattr(report, "triggered", False):
            ui.print_answer_chunk(
                f"[Context Compressed: {report.tokens_before} -> {report.tokens_after} tokens "
                f"via {report.level}]"
            )

    async def task_end(state: HarnessState):
        ui.render_execution_status(state, active_tool="None", tool_status="TERMINAL")
        if state.final_output:
            ui.print_answer_chunk(f"[Final Output] {state.final_output}")

    return HarnessHooks(
        post_model_call=post_model,
        pre_tool_exec=pre_tool,
        post_tool_exec=post_tool,
        post_compress=post_compress,
        task_end=task_end,
    )
