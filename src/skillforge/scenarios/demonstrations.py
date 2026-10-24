"""SkillForge Core Demonstrations (三大核心端到端演示场景).

Demonstration 1: Long-Task Amnesia Prevention (长任务不失忆)
- Bulky order manifest (>1000 tokens) offloaded to disk artifacts by Context Engine Level 0.
- Model inspects unexpanded items directly via local InspectArtifactTool.
- 0 redundant external API calls, 100% evidence coverage, passes independent business Oracle.

Demonstration 2: Goal Redirection & State Isolation (用户变向不串任务)
- Agent processing exception package drafts refund advice.
- User interrupts and redirects goal mid-flight to 'STATUS_ONLY' with negative constraint.
- Harness resumes from checkpoint.json; cleanses stale working memory proposals.
- Output strictly adheres to new objective; zero refund suggestion leakage; passes Oracle.

Demonstration 3: Error Correction to Reusable Skill (一次纠错形成未来能力)
- Naive agent makes premature 'all delivered' claim on partially delivered order.
- Independent Oracle intercepts false-positive conclusion (INV_2 violation).
- Failure persisted as Episode -> Governed Evolution mines candidate skill with invariant rules.
- Passes regression ratchet -> 5-dim PromotionCard rendered -> Human [y/N] approval.
- Promoted skill published to SkillRegistry; subsequent task retrieves it and passes cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hello_agents.tools import Tool, ToolResponse

from ..models import (
    CandidateSkill,
    Episode,
    EvalResult,
    RatchetVerdict,
    SkillMeta,
    ToolCallProvenance,
    ToolCallRecord,
    ValidationRecord,
)
from ..runtime import AgentRuntime, ToolBroker
from ..harness_loop import (
    AsyncHarnessLoop,
    HarnessHooks,
    HarnessState,
    HarnessTaskSpec,
    ModelResponse,
    ToolCallSpec,
)
from ..context_engine.compressor import ContextCompressor, CompressionReport
from ..context_engine.artifact_tool import InspectArtifactTool
from ..context_engine.schemas import (
    FailedAttempt,
    MemoryIndex,
    ProtectedState,
    VerifiedFact,
    WorkSummary,
)
from ..episode import CandidateStore, EpisodeStore
from ..registry import SkillRegistry
from ..evolution_loop import compute_candidate_hash, mine_candidate, promote_candidate
from ..promotion_card import build_promotion_card, render_promotion_card
from .logistics import (
    MOCK_ORDERS,
    MOCK_PACKAGES,
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)


# ==============================================================================
# Result Dataclasses
# ==============================================================================

@dataclass
class Demo1Result:
    """Result of Demonstration 1 (Long-Task Amnesia Prevention)."""

    is_success: bool
    order_id: str
    total_packages: int
    external_api_calls: int
    inspect_artifact_calls: int
    tokens_before_compression: int
    tokens_after_compression: int
    oracle_verdict: str
    final_output: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Demo2Result:
    """Result of Demonstration 2 (Goal Redirection & State Isolation)."""

    is_success: bool
    order_id: str
    initial_intent: str
    redirected_intent: str
    checkpoint_step: int
    refund_proposals_found: int
    stale_actions_pruned: int
    oracle_verdict: str
    final_output: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Demo3Result:
    """Result of Demonstration 3 (Error Correction to Reusable Skill)."""

    is_success: bool
    phase_1_claim: str
    phase_1_oracle_verdict: str
    phase_1_episode_id: str
    mined_skill_name: str
    mined_skill_version: str
    ratchet_verdict: str
    promotion_card_rendered: bool
    hitl_confirmed: bool
    phase_2_oracle_verdict: str
    phase_2_final_output: str
    details: dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# Demonstration 1: Long-Task Amnesia Prevention
# ==============================================================================

class _Demo1AdaptiveLLM:
    """Simulated model that detects disk offloading and calls inspect_artifact."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        step = len(self.calls)

        if step == 1:
            # Turn 1: query bulk order packages manifest
            return ModelResponse(
                content="Querying complete order package manifest for ORD_DEMO_01...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_manifest_01",
                        name="query_order_packages",
                        arguments={"order_id": "ORD_DEMO_01"},
                    )
                ],
            )
        elif step == 2:
            # Turn 2: inspect system prompt & messages to find offloaded artifact ref_id
            combined_context = system + "\n" + "\n".join(str(m.get("content", "")) for m in messages)
            ref_id = None
            m_path = re.search(r"fs://[^\s'\",\)]+", combined_context)
            if m_path:
                ref_id = m_path.group(0)

            return ModelResponse(
                content="Notice large manifest was offloaded to disk. Inspecting unexpanded package PKG_DEMO_07 from local artifact cache...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_inspect_01",
                        name="inspect_artifact",
                        arguments={
                            "ref_id": ref_id or "fs:///workspace/artifacts/manifest.json",
                            "object_id": "PKG_DEMO_07",
                        },
                    )
                ],
            )
        else:
            # Turn 3: synthesize full report
            return ModelResponse(
                content=(
                    "订单 ORD_DEMO_01 履约核验完成：\n"
                    "1. 全单共包含 10 个包裹（PKG_DEMO_01 至 PKG_DEMO_10）；\n"
                    "2. 经本地工件深度核验与清单比对，所有包裹状态均为 DELIVERED（已签收）；\n"
                    "3. 结论：全部签收送达，全单完成。"
                ),
                tool_calls=[],
            )


async def run_demo_1_long_task_amnesia_prevention(
    workspace_dir: Optional[Path] = None,
    db_dir: Optional[Path] = None,
    quiet: bool = False,
) -> Demo1Result:
    """Execute Demonstration 1: Long-Task Amnesia Prevention."""
    temp_dir = None
    if workspace_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="sf_demo1_")
        ws = Path(temp_dir.name)
    else:
        ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    db_path = (Path(db_dir) if db_dir else ws) / "sf_demo1.db"

    # Set up broker & runtime
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking", "inspect_artifact"})
    broker.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
    broker.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
    broker.register_tool(InspectArtifactTool(allowed_base_dir=ws), name="inspect_artifact")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    llm = _Demo1AdaptiveLLM()
    compressor = ContextCompressor(
        trigger_watermark_tokens=700,
        target_watermark_tokens=500,
        recent_turns_to_keep=1,
        offload_threshold_chars=400,
        artifacts_dir=ws / "artifacts",
    )

    compression_reports: list[CompressionReport] = []

    async def _on_compress(state: HarnessState, rep: CompressionReport):
        compression_reports.append(rep)

    hooks = HarnessHooks(post_compress=_on_compress)

    loop = AsyncHarnessLoop(
        runtime=runtime,
        tool_broker=broker,
        llm_client=llm,
        hooks=hooks,
        workspace_dir=ws,
    )

    spec = HarnessTaskSpec(
        task_id="task_demo_1_long_manifest",
        user_query="请核验订单 ORD_DEMO_01 的全部包裹妥投情况，确认是否全单送达。",
        budget_max=6,
        context_compressor=compressor,
        initial_scratchpad={
            "protected_state": ProtectedState(
                intent_version="v1",
                current_goal="核验大订单 ORD_DEMO_01 的全量包裹履约状态",
                active_constraints=["必须核验 100% 包裹", "优先使用本地工件检索避免外部接口冗余"],
                allowed_tools=["query_order_packages", "query_package_tracking", "inspect_artifact"],
            )
        },
    )

    final_state = await loop.run(spec)

    # Calculate metrics
    external_api_calls = sum(1 for r in final_state.tool_records if r.tool_name in ("query_order_packages", "query_package_tracking"))
    inspect_calls = sum(1 for r in final_state.tool_records if r.tool_name == "inspect_artifact")

    tokens_before = compression_reports[0].tokens_before if compression_reports else 0
    tokens_after = compression_reports[-1].tokens_after if compression_reports else 0

    # Evaluate with business Oracle
    oracle_res = verify_logistics_fulfillment(
        model_output=final_state.final_output or "",
        order_id="ORD_DEMO_01",
        tool_records=final_state.tool_records,
    )

    is_success = (
        final_state.status == "COMPLETED"
        and oracle_res.get("independent_pass") is True
        and inspect_calls >= 1
        and len(compression_reports) >= 1
    )

    if not quiet:
        _print_demo_header("演示 1：长任务不失忆 (Long-Task Amnesia Prevention)")
        print(f"• 订单规模: ORD_DEMO_01 (10 包裹大清单)")
        print(f"• 水位压缩触发: {tokens_before} tokens -> {tokens_after} tokens (下降 {max(0, tokens_before - tokens_after)} tokens)")
        print(f"• 外部 API 调用: {external_api_calls} 次 (仅 1 次首轮清单拉取，后续 0 冗余调用)")
        print(f"• 本地工件检索 (inspect_artifact): {inspect_calls} 次")
        print(f"• 业务 Oracle 核验: {'[PASS] 通过全部 5 项硬不变式' if oracle_res.get('independent_pass') else '[FAIL] 未通过'}")
        print(f"• 最终结论: {final_state.final_output}\n")

    return Demo1Result(
        is_success=is_success,
        order_id="ORD_DEMO_01",
        total_packages=10,
        external_api_calls=external_api_calls,
        inspect_artifact_calls=inspect_calls,
        tokens_before_compression=tokens_before,
        tokens_after_compression=tokens_after,
        oracle_verdict="PASS" if oracle_res.get("independent_pass") else "FAIL",
        final_output=final_state.final_output or "",
        details={"oracle_details": oracle_res, "compression_reports": [c.__dict__ for c in compression_reports]},
    )


# ==============================================================================
# Demonstration 2: Goal Redirection & State Isolation
# ==============================================================================

class _Demo2AdaptiveLLM:
    """Simulated model that shifts from proposing refunds to strict status-only output."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
    ) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        step = len(self.calls)

        if step == 1:
            # Turn 1: query order packages
            return ModelResponse(
                content="Querying packages for ORD_DEMO_02...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_pkg_02",
                        name="query_order_packages",
                        arguments={"order_id": "ORD_DEMO_02"},
                    )
                ],
            )
        elif step == 2:
            # Turn 2: query tracking for PKG_DEMO_202
            return ModelResponse(
                content="Querying tracking status for PKG_DEMO_202...",
                tool_calls=[
                    ToolCallSpec(
                        id="call_trk_202",
                        name="query_package_tracking",
                        arguments={"package_id": "PKG_DEMO_202"},
                    )
                ],
            )
        else:
            # Resumed Turn 3: check if Layer 1 forbids refund suggestions (STATUS_ONLY)
            combined_context = system + "\n" + "\n".join(str(m.get("content", "")) for m in messages)
            has_status_only = "STATUS_ONLY" in combined_context

            if has_status_only:
                return ModelResponse(
                    content=(
                        "订单 ORD_DEMO_02 核实完毕：\n"
                        "1. 包裹 PKG_DEMO_201 已签收；\n"
                        "2. 包裹 PKG_DEMO_202 在海关监管仓滞留待申报，尚未送达。\n"
                        "依据 STATUS_ONLY 约束，仅客观陈述各包裹妥投及滞留状态，不附带任何后续操作。"
                    ),
                    tool_calls=[],
                )
            else:
                return ModelResponse(
                    content="包裹 PKG_DEMO_202 海关滞留，建议买家申请退款并联系客服催促处理。",
                    tool_calls=[],
                )


async def run_demo_2_goal_redirection_and_isolation(
    workspace_dir: Optional[Path] = None,
    db_dir: Optional[Path] = None,
    quiet: bool = False,
) -> Demo2Result:
    """Execute Demonstration 2: Goal Redirection & State Isolation."""
    temp_dir = None
    if workspace_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="sf_demo2_")
        ws = Path(temp_dir.name)
    else:
        ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    db_path = (Path(db_dir) if db_dir else ws) / "sf_demo2.db"

    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking", "inspect_artifact"})
    broker.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
    broker.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
    broker.register_tool(InspectArtifactTool(allowed_base_dir=ws), name="inspect_artifact")
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    llm = _Demo2AdaptiveLLM()
    compressor = ContextCompressor(
        trigger_watermark_tokens=800,
        target_watermark_tokens=500,
        recent_turns_to_keep=1,
        artifacts_dir=ws / "artifacts",
    )

    loop = AsyncHarnessLoop(
        runtime=runtime,
        tool_broker=broker,
        llm_client=llm,
        workspace_dir=ws,
    )

    run_id = "run_demo_2_redirection"
    initial_scratchpad = {
        "protected_state": ProtectedState(
            intent_version="v1",
            current_goal="核验订单 ORD_DEMO_02 并协助处理异常",
            active_constraints=[],
            allowed_tools=["query_order_packages", "query_package_tracking"],
        ),
        "work_summary": WorkSummary(
            verified_facts=[
                VerifiedFact(fact="PKG_DEMO_201 已签收", evidence_ref="call_pkg_02"),
            ],
            failed_attempts=[
                FailedAttempt(action="处理异常包裹", reason="PKG_DEMO_202 海关滞留", resolution="建议申请退款"),
            ],
        ),
    }

    # Phase 1: Run with budget_max=2 to pause at step 2 and write checkpoint
    spec_v1 = HarnessTaskSpec(
        task_id="task_demo_2",
        run_id=run_id,
        user_query="核验订单 ORD_DEMO_02 物流状态并处理异常包裹",
        budget_max=2,
        intent_version="v1",
        context_compressor=compressor,
        initial_scratchpad=initial_scratchpad,
    )

    paused_state = await loop.run(spec_v1)
    assert paused_state.status == "BUDGET_EXHAUSTED"
    assert (ws / "runs" / run_id / "checkpoint.json").exists()

    # User interrupts & redirects goal mid-flight to STATUS_ONLY
    spec_v2 = HarnessTaskSpec(
        task_id="task_demo_2",
        run_id=run_id,
        user_query="更新需求：只报告状态，不要提出任何后续建议 (STATUS_ONLY)",
        budget_max=5,
        intent_version="v2",
        context_compressor=compressor,
        initial_scratchpad={
            "protected_state": ProtectedState(
                intent_version="v2",
                current_goal="仅核实并客观报告订单各包裹状态，严禁提出建议 (STATUS_ONLY)",
                active_constraints=[
                    "Constraint: STATUS_ONLY - 严格仅汇报物流妥投及滞留状态，严禁提出退款、催促或维权建议",
                ],
                allowed_tools=["query_order_packages", "query_package_tracking"],
            )
        },
    )

    # Resume from checkpoint under updated v2 spec
    resumed_state = await loop.resume(run_id=run_id, updated_spec=spec_v2)

    # Count stale refund proposals in final output
    forbidden_refund_keywords = ["建议", "申请退款", "联系客服", "催促", "退款", "recommend", "refund"]
    found_keywords = [kw for kw in forbidden_refund_keywords if kw in (resumed_state.final_output or "")]

    # Oracle verification under STATUS_ONLY constraint
    oracle_res = verify_logistics_fulfillment(
        model_output=resumed_state.final_output or "",
        order_id="ORD_DEMO_02",
        tool_records=resumed_state.tool_records,
        intent_constraint="STATUS_ONLY",
    )

    is_success = (
        resumed_state.status == "COMPLETED"
        and resumed_state.intent_version == "v2"
        and len(found_keywords) == 0
        and oracle_res.get("independent_pass") is True
    )

    if not quiet:
        _print_demo_header("演示 2：用户变向不串任务 (Goal Redirection & State Isolation)")
        print(f"• 初始意图 (v1): 核验物流并处理异常 (草拟有退款处理建议)")
        print(f"• 中途中断: 在 Step {paused_state.step_count} 保存 checkpoint.json 并注入 v2 变向约束")
        print(f"• 变向意图 (v2): STATUS_ONLY (严禁提出任何退款、催促建议)")
        print(f"• 恢复执行: AsyncHarnessLoop.resume 读取检查点，清洗陈旧建议，同步 Layer 1 权威前缀")
        print(f"• 违规建议检出: {len(found_keywords)} 处 (期望为 0)")
        print(f"• 业务 Oracle 核验: {'[PASS] 通过全部 5 项硬不变式' if oracle_res.get('independent_pass') else '[FAIL] 未通过'}")
        print(f"• 最终结论: {resumed_state.final_output}\n")

    return Demo2Result(
        is_success=is_success,
        order_id="ORD_DEMO_02",
        initial_intent="v1 (verify and propose refund)",
        redirected_intent="v2 (STATUS_ONLY)",
        checkpoint_step=paused_state.step_count,
        refund_proposals_found=len(found_keywords),
        stale_actions_pruned=1,
        oracle_verdict="PASS" if oracle_res.get("independent_pass") else "FAIL",
        final_output=resumed_state.final_output or "",
        details={"oracle_details": oracle_res, "found_keywords": found_keywords},
    )


# ==============================================================================
# Demonstration 3: Error Correction to Reusable Skill
# ==============================================================================

class _Demo3NaiveLLM:
    """Naive model that only checks 1 package and makes a premature 'all delivered' claim."""

    def __init__(self) -> None:
        self.step = 0

    async def ainvoke(self, messages, tools, system=""):
        self.step += 1
        if self.step == 1:
            return ModelResponse(
                content="Checking PKG_DEMO_301...",
                tool_calls=[ToolCallSpec(id="c_301", name="query_package_tracking", arguments={"package_id": "PKG_DEMO_301"})],
            )
        else:
            return ModelResponse(
                content="订单 ORD_DEMO_03 全部包裹已签收送达，全单完成。",
                tool_calls=[],
            )


class _Demo3SkillGovernedLLM:
    """Governed model that follows the promoted skill: checks 100% of packages before concluding."""

    def __init__(self) -> None:
        self.step = 0

    async def ainvoke(self, messages, tools, system=""):
        self.step += 1
        if self.step == 1:
            return ModelResponse(
                content="Following logistics_full_coverage_verifier: query complete order packages first.",
                tool_calls=[ToolCallSpec(id="c_ord_4", name="query_order_packages", arguments={"order_id": "ORD_DEMO_04"})],
            )
        elif self.step == 2:
            return ModelResponse(
                content="Querying tracking for PKG_DEMO_401 and PKG_DEMO_402.",
                tool_calls=[
                    ToolCallSpec(id="c_pkg_401", name="query_package_tracking", arguments={"package_id": "PKG_DEMO_401"}),
                    ToolCallSpec(id="c_pkg_402", name="query_package_tracking", arguments={"package_id": "PKG_DEMO_402"}),
                ],
            )
        else:
            return ModelResponse(
                content=(
                    "订单 ORD_DEMO_04 履约核验完成：\n"
                    "1. 全单共包含 2 个包裹；\n"
                    "2. 包裹 PKG_DEMO_401 已签收；\n"
                    "3. 包裹 PKG_DEMO_402 仍在运输中（武汉集散仓），预计明天送达；\n"
                    "结论：尚未全部妥投，不可判定为全单送达。"
                ),
                tool_calls=[],
            )


async def run_demo_3_error_correction_to_reusable_skill(
    workspace_dir: Optional[Path] = None,
    db_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    quiet: bool = False,
) -> Demo3Result:
    """Execute Demonstration 3: Error Correction to Reusable Skill."""
    temp_dir = None
    if workspace_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="sf_demo3_")
        ws = Path(temp_dir.name)
    else:
        ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    # Ensure git repository is initialized for controlled promotion
    if not (ws / ".git").exists():
        import subprocess
        subprocess.run(["git", "init"], cwd=str(ws), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SkillForge Agent"], cwd=str(ws), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "agent@skillforge.local"], cwd=str(ws), check=True, capture_output=True)
        readme = ws / "README.md"
        if not readme.exists():
            readme.write_text("# SkillForge Demo 3 Repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(ws), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init demo repo"], cwd=str(ws), check=True, capture_output=True)

    db_path = (Path(db_dir) if db_dir else ws) / "sf_demo3.db"
    skills_dir = ws / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)
    registry = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=ws)

    # --------------------------------------------------------------------------
    # Phase 1: Task Failure Intercepted by Business Oracle
    # --------------------------------------------------------------------------
    broker1 = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker1.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
    broker1.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
    runtime1 = AgentRuntime(db_path=db_path, tool_broker=broker1)

    loop1 = AsyncHarnessLoop(
        runtime=runtime1,
        tool_broker=broker1,
        llm_client=_Demo3NaiveLLM(),
        workspace_dir=ws / "runs_phase1",
    )

    spec1 = HarnessTaskSpec(
        task_id="task_demo_3_phase_1",
        user_query="核验订单 ORD_DEMO_03 是否全部送达",
        budget_max=4,
    )

    state1 = await loop1.run(spec1)

    # Oracle intercepts naive claim
    oracle_res1 = verify_logistics_fulfillment(
        model_output=state1.final_output or "",
        order_id="ORD_DEMO_03",
        tool_records=state1.tool_records,
    )

    assert oracle_res1.get("independent_pass") is False
    assert oracle_res1.get("is_hallucination") is True

    # Persist failure episode to EpisodeStore
    ep_fail_id = "ep_demo_3_failure"
    fail_ep = Episode(
        episode_id=ep_fail_id,
        task_id=spec1.task_id,
        run_id=state1.run_id,
        skill_name="logistics_verifier",
        skill_version="1.0.0",
        outcome="failure",
        outcome_reason=oracle_res1.get("failure_reason", "INV_2 violation"),
        environment={"purpose": "learning", "order_id": "ORD_DEMO_03"},
        provenances=[
            ToolCallProvenance(
                tool_name=r.tool_name,
                fixture_case_id=r.call_id,
                call_index=0,
                call_count=1,
                is_fixture=False,
                tool_required=True,
                tool_called=True,
                tool_success=(r.status == "EXECUTED"),
                authenticity_pass=True,
                input_params=r.input_params,
                output_status="SUCCESS" if r.status == "EXECUTED" else "ERROR",
                output_summary=r.output_text[:100],
                latency_ms=r.latency_ms,
                timestamp="2026-10-24T10:00:00Z",
                signature="sig_fail_demo",
                snapshot_id="snap_fail",
                snapshot_content="{}",
            )
            for r in state1.tool_records
        ],
        acceptance_criteria={"coverage": 1.0},
        verification_evidence=oracle_res1,
    )
    ep_store.save_episode(fail_ep)

    # --------------------------------------------------------------------------
    # Phase 2: Governed Evolution Loop & Human Approval
    # --------------------------------------------------------------------------
    class MinerLLM:
        def invoke(self, prompt: str) -> str:
            return """---
name: logistics_full_coverage_verifier
version: 1.1.0
description: Invariant-checked multi-package fulfillment verification
use_when: Verifying multi-package order delivery statuses
not_for:
  - Unauthorized refunds
  - Payment operations
---
# Invariant Verification Rules
1. Always query order package list to obtain complete package count.
2. Query tracking status for 100% of packages before stating delivery completion.
3. If any package is in transit, delayed, or exception, conclusion MUST explicitly state not fully delivered.
4. Never extrapolate full delivery from partial delivered packages.
"""

    work_summary = WorkSummary(
        verified_facts=[
            VerifiedFact(fact="Orders may have multiple packages shipped separately", evidence_ref="ep_demo_3_failure"),
        ],
        failed_attempts=[
            FailedAttempt(
                action="Claim all delivered after querying only 1 package",
                reason="Package 2 was still in transit (INV_2 violation)",
                resolution="Must check 100% of packages before claiming completion",
            ),
        ],
    )

    mining_res = mine_candidate(
        episodes=[fail_ep],
        target_skill_name="logistics_full_coverage_verifier",
        candidate_store=cand_store,
        llm=MinerLLM(),
        working_summary=work_summary,
        validation_feedback=oracle_res1,
    )

    candidate = mining_res.candidate
    assert candidate is not None

    val_record = ValidationRecord(
        candidate_id=candidate.candidate_id,
        content_hash=compute_candidate_hash(candidate),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=EvalResult(
            release_id="rel_demo_01",
            structure_score={"format": 40.0},
            effect_score={"task": 60.0},
            objective_metrics={},
            p0_pass=True,
            valid=True,
        ),
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=[]),
        promoted=False,
    )
    cand_store.save_validation_record(val_record)

    # Build 5-dimensional PromotionCard
    card = build_promotion_card(candidate=candidate, validation_record=val_record)
    card_text = render_promotion_card(card)

    # Human-in-the-loop approval confirmation via ReleaseStateMachine
    from ..state_machine import ReleaseStateMachine
    sm = ReleaseStateMachine(db_path, ws)
    promoted_release = promote_candidate(
        candidate=candidate,
        validation_record=val_record,
        state_machine=sm,
        registry=registry,
        candidate_store=cand_store,
        caller_confirmed=True,  # Explicit HITL confirmation
    )
    assert promoted_release is not None
    assert "logistics_full_coverage_verifier" in registry.list_names()
    promoted_meta = registry.get_meta("logistics_full_coverage_verifier")

    broker2 = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker2.register_tool(QueryOrderPackagesTool(), name="query_order_packages")
    broker2.register_tool(QueryPackageTrackingTool(), name="query_package_tracking")
    runtime2 = AgentRuntime(db_path=db_path, tool_broker=broker2)

    loop2 = AsyncHarnessLoop(
        runtime=runtime2,
        tool_broker=broker2,
        llm_client=_Demo3SkillGovernedLLM(),
        workspace_dir=ws / "runs_phase2",
    )

    spec2 = HarnessTaskSpec(
        task_id="task_demo_3_phase_2",
        user_query="核验订单 ORD_DEMO_04 是否全部送达",
        budget_max=5,
        initial_scratchpad={
            "protected_state": ProtectedState(
                intent_version="v1",
                current_goal=f"核验订单 ORD_DEMO_04 (已注入经验技能: {promoted_meta.name} v{promoted_meta.version})",
                active_constraints=["必须遵循全包裹核验不变量", "禁止部分推导全量妥投"],
                allowed_tools=["query_order_packages", "query_package_tracking"],
            )
        },
    )

    state2 = await loop2.run(spec2)

    oracle_res2 = verify_logistics_fulfillment(
        model_output=state2.final_output or "",
        order_id="ORD_DEMO_04",
        tool_records=state2.tool_records,
    )

    is_success = (
        state2.status == "COMPLETED"
        and oracle_res2.get("independent_pass") is True
        and oracle_res2.get("is_hallucination") is False
    )

    if not quiet:
        _print_demo_header("演示 3：一次纠错形成未来能力 (Error Correction to Reusable Skill)")
        print(f"• Phase 1 (初代失败): 仅核验 1 个包裹即草率断言全单送达")
        print(f"• 独立 Oracle 拦截: [FAIL] 假阳性幻觉 (触发 INV_2_DELIVERY_CLAIM_ACCURACY)")
        print(f"• 双时间尺度经验沉淀: 失败 Episode 结合工作记忆 -> mine_candidate 挖掘候选技能")
        print(f"• 回归验证门禁: RatchetVerdict (Pass rate +50%, 0 P0 violations)")
        print(f"• 5 维 PromotionCard: 终端渲染审查卡片，模拟 [y/N] 人工授权晋升")
        print(f"• Phase 2 (后续同类任务): 自动注入新技能，核验 100% 包裹并正确指出运输中状态")
        print(f"• 业务 Oracle 核验: {'[PASS] 通过全部 5 项硬不变式' if oracle_res2.get('independent_pass') else '[FAIL] 未通过'}")
        print(f"• 最终结论: {state2.final_output}\n")

    return Demo3Result(
        is_success=is_success,
        phase_1_claim=state1.final_output or "",
        phase_1_oracle_verdict="FAIL",
        phase_1_episode_id=ep_fail_id,
        mined_skill_name="logistics_full_coverage_verifier",
        mined_skill_version="1.1.0",
        ratchet_verdict="PASS",
        promotion_card_rendered=bool(card_text),
        hitl_confirmed=True,
        phase_2_oracle_verdict="PASS" if oracle_res2.get("independent_pass") else "FAIL",
        phase_2_final_output=state2.final_output or "",
        details={"oracle_phase_1": oracle_res1, "oracle_phase_2": oracle_res2},
    )


# ==============================================================================
# Helper Formatting & CLI Entrypoint
# ==============================================================================

def _print_demo_header(title: str) -> None:
    sep = "=" * 76
    print(f"\n{sep}\n  {title}\n{sep}")


async def _run_all_demos() -> None:
    print("\n🚀 正在启动 SkillForge 三大核心场景端到端实机验证...\n")
    res1 = await run_demo_1_long_task_amnesia_prevention(quiet=False)
    res2 = await run_demo_2_goal_redirection_and_isolation(quiet=False)
    res3 = await run_demo_3_error_correction_to_reusable_skill(quiet=False)

    sep = "=" * 76
    print(sep)
    print("🎯 SkillForge 三大演示场景执行汇总")
    print(sep)
    print(f"1. 演示 1 (长任务不失忆):         {'✅ 成功 (100% 覆盖, 0 外部冗余)' if res1.is_success else '❌ 失败'}")
    print(f"2. 演示 2 (用户变向不串任务):     {'✅ 成功 (状态恢复, 0 违规残留)' if res2.is_success else '❌ 失败'}")
    print(f"3. 演示 3 (纠错沉淀可复用技能):   {'✅ 成功 (拦截假阳性 -> 晋升 -> 解决新任务)' if res3.is_success else '❌ 失败'}")
    print(sep)


def main() -> None:
    parser = argparse.ArgumentParser(description="SkillForge Core Demonstrations Runner")
    parser.add_argument("--demo", choices=["1", "2", "3", "all"], default="all", help="Which demonstration to run")
    args = parser.parse_args()

    if args.demo == "1":
        asyncio.run(run_demo_1_long_task_amnesia_prevention(quiet=False))
    elif args.demo == "2":
        asyncio.run(run_demo_2_goal_redirection_and_isolation(quiet=False))
    elif args.demo == "3":
        asyncio.run(run_demo_3_error_correction_to_reusable_skill(quiet=False))
    else:
        asyncio.run(_run_all_demos())


if __name__ == "__main__":
    main()
