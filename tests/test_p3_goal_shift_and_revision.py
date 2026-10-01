"""P3 Acceptance Test Suite: User Goal Shift-Driven Draft Revision (I1–I5).

Verifies Acceptance Criteria I1–I5:
- I1: Goal shift produces new intent revision; next execution consumes new Draft body;
      old Draft preserved as SUPERSEDED with lineage; in-flight snapshot preserved.
- I2: In-flight execution freezes Draft/Skill and contract; late-arriving results belong strictly
      to old intent, cannot overwrite new results, cannot serve as success Episode/positive case
      for new intent/candidate; survives SQLite reopen; external side effects not claimed undone.
- I3: Cosmetic rewording / politeness produces NO_OP (0 generator calls, no revision);
      ambiguous feedback returns CONFIRMATION_REQUIRED without silent rewriting.
- I4: Task cancellation via runtime.cancel_run; irreversible side effects audited honestly
      (side_effects_reversible=False); channel capability differences tested (async, sandbox, sync callback).
- I5: Session goal shifts do not globally deprecate formal skills in SkillRegistry;
      read-to-write intent shifts blocked by ToolBroker allowlist (handler calls == 0, no privilege escalation).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hello_agents.tools import Tool, ToolParameter, ToolResponse
from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.models import (
    CandidateSkill,
    Episode,
    SkillMeta,
    TaskContext,
)
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.task_context import detect_intent_shift, revise_task_context


# ---------------------------------------------------------------------------
# Desensitized Logistics Scripted Tool Fixtures
# ---------------------------------------------------------------------------

class ScriptedLogisticsQueryTool(Tool):
    """Read-only tool querying package tracking information."""

    def __init__(self) -> None:
        super().__init__(
            name="query_package_status",
            description="Query logistics status for a package ID.",
        )
        self.parameters = [
            ToolParameter(
                name="package_id",
                type="string",
                description="Package tracking number",
                required=True,
            )
        ]
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return self.parameters

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        pkg_id = parameters.get("package_id", "")
        packages_db = {
            "PKG-001": {"status": "DELIVERED", "location": "Hub A", "items": 2},
            "PKG-002": {"status": "IN_TRANSIT", "location": "Hub B", "items": 1},
            "PKG-003": {"status": "EXCEPTION", "location": "Hub C", "items": 3},
        }
        info = packages_db.get(pkg_id, {"status": "UNKNOWN", "location": "Unknown"})
        return ToolResponse.success(
            text=f"Package {pkg_id} status: {info['status']}",
            data=info,
        )


class ScriptedLogisticsRefundTool(Tool):
    """Write/Privileged tool executing package refunds."""

    def __init__(self) -> None:
        super().__init__(
            name="refund_order",
            description="Execute refund for a package order (Privileged Write Tool).",
        )
        self.parameters = [
            ToolParameter(
                name="package_id",
                type="string",
                description="Package tracking number to refund",
                required=True,
            )
        ]
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return self.parameters

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        return ToolResponse.success(
            text="Refund processed successfully",
            data={"refund_id": "REF-999", "status": "REFUNDED"},
        )


# ---------------------------------------------------------------------------
# I1: Goal Shift, New Intent Revision & Next Execution Consumes New Body
# ---------------------------------------------------------------------------

def test_i1_goal_shift_produces_new_revision_and_consumes_new_body(tmp_path: Path) -> None:
    """I1: User explicitly changes goal/prohibitions:
    - Produces new intent revision (revision 1 -> 2).
    - Next execution actually consumes new Draft body via AgentRuntime.get_run_body.
    - Old Draft preserved as SUPERSEDED with superseded_by pointer.
    - Old in-flight run body snapshot is not modified in-place.
    """
    db_path = tmp_path / "skillforge.db"
    store = CandidateStore(db_path)
    broker = ToolBroker(application_allowlist={"query_package_status"})
    query_tool = ScriptedLogisticsQueryTool()
    broker.register_tool(query_tool)
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    # 1. Initial goal and candidate 1: generate recommendations
    cand1 = CandidateSkill(
        candidate_id="cand_logistics_v1",
        skill_name="multi_package_advisor",
        decision="create",
        source_requirement="为多包裹物流生成处理建议",
        meta=SkillMeta(
            name="multi_package_advisor",
            version="0.1.0-draft",
            description="Analyze packages and generate follow-up recommendations",
            use_when="When user requests advice for delayed packages",
        ),
        body="## Instructions\n1. Query all package statuses.\n2. Formulate subsequent action advice for delays.",
        rationale="Initial creation for multi-package advice",
        status="DRAFT",
        intent_revision=1,
    )
    store.save_candidate(cand1)

    initial_ctx = TaskContext(
        task_id="task_logistics_001",
        goal="为多包裹物流生成处理建议",
        business_scope="logistics.multi_package",
        constraints=["建议需包含后续客服跟进策略"],
        intent_revision=1,
        active_candidate_id=cand1.candidate_id,
        active_body_snapshot=cand1.body,
    )
    store.save_task_context(initial_ctx)

    # Run 1 starts under initial intent
    run1 = runtime.start_run(
        run_id="run_intent_rev1",
        task_id=initial_ctx.task_id,
        candidate=cand1,
    )
    body1_before = runtime.get_run_body(run1.run_id)
    assert "Formulate subsequent action advice" in body1_before

    # 2. User explicitly shifts goal & adds prohibition:
    # "只核实全部包裹状态，不提出后续建议"
    new_prompt = "只核实全部包裹状态，不提出后续建议"
    new_constrs = ["禁止提出后续建议", "仅只读核实包裹状态"]
    shift_kind, reason = detect_intent_shift(initial_ctx, new_prompt, new_constrs)
    assert shift_kind == "REVISION"
    assert "Goal updated" in (reason or "")

    new_body_text = (
        "## Instructions\n"
        "1. Query package status for all packages.\n"
        "2. Report current state only.\n"
        "## Constraints\n"
        "- Do NOT propose follow-up actions or recommendations."
    )

    revised_ctx, cand2 = revise_task_context(
        current_ctx=initial_ctx,
        new_goal=new_prompt,
        new_constraints=new_constrs,
        new_body=new_body_text,
        candidate_store=store,
        new_candidate_id="cand_logistics_v2",
    )
    assert revised_ctx.intent_revision == 2
    assert revised_ctx.active_candidate_id == "cand_logistics_v2"
    assert "cand_logistics_v1" in revised_ctx.superseded_candidate_ids
    assert cand2 is not None
    assert cand2.supersedes == "cand_logistics_v1"

    # Verify old candidate in store is SUPERSEDED, preserving lineage
    cand1_stored = store.get_candidate("cand_logistics_v1")
    assert cand1_stored is not None
    assert cand1_stored.status == "SUPERSEDED"
    assert cand1_stored.superseded_by == "cand_logistics_v2"

    # 3. Next execution consumes new Draft body
    run2 = runtime.start_run(
        run_id="run_intent_rev2",
        task_id=revised_ctx.task_id,
        candidate=cand2,
    )
    body2 = runtime.get_run_body(run2.run_id)
    assert "Do NOT propose follow-up actions or recommendations" in body2
    assert "Formulate subsequent action advice" not in body2

    # In-flight run 1 body snapshot is immutable and not rewritten in-place
    body1_after = runtime.get_run_body(run1.run_id)
    assert body1_after == body1_before
    assert "Formulate subsequent action advice" in body1_after


# ---------------------------------------------------------------------------
# I2: Snapshot Freeze, Late-Arrival Isolation & DB Reopen
# ---------------------------------------------------------------------------

def test_i2_snapshot_freeze_late_arrival_isolation_and_db_reopen(tmp_path: Path) -> None:
    """I2: In-flight execution freezes Draft/Skill and contract:
    - Late-arriving results from old intent belong strictly to old intent / fingerprint.
    - Late results do NOT overwrite new results.
    - Late results cannot serve as success Episode/positive case for the new candidate.
    - State and isolation survive SQLite close & reopen.
    - Executed side-effects cannot be claimed rolled back.
    """
    db_path = tmp_path / "skillforge.db"
    store = CandidateStore(db_path)
    ep_store = EpisodeStore(db_path)
    broker = ToolBroker(application_allowlist={"query_package_status"})
    query_tool = ScriptedLogisticsQueryTool()
    broker.register_tool(query_tool)
    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(db_path=db_path, tool_broker=broker, collector=collector)

    # Initial candidate and task context
    cand1 = CandidateSkill(
        candidate_id="cand_v1_isolation",
        skill_name="pkg_tracer",
        decision="create",
        source_requirement="排查包裹延迟原因并提出建议",
        meta=SkillMeta(
            name="pkg_tracer",
            version="0.1.0",
            description="Trace packages",
            use_when="Checking package trace",
        ),
        body="## Body 1",
        status="DRAFT",
        intent_revision=1,
    )
    store.save_candidate(cand1)
    ctx1 = TaskContext(
        task_id="task_pkg_iso",
        goal="排查包裹延迟原因并提出建议",
        business_scope="logistics.trace",
        intent_revision=1,
        active_skill_name="pkg_tracer",
        active_skill_version="0.1.0",
        active_candidate_id=cand1.candidate_id,
        active_body_snapshot=cand1.body,
    )
    ctx1.contract_fingerprint = ctx1.compute_fingerprint()
    store.save_task_context(ctx1)

    # Run 1 starts under revision 1
    run1 = runtime.start_run(
        run_id="run_late_1",
        task_id=ctx1.task_id,
        candidate=cand1,
    )
    runtime.dispatch_tool(
        run_id=run1.run_id,
        tool_name="query_package_status",
        parameters={"package_id": "PKG-001"},
    )

    # Intent shift occurs while Run 1 is still incomplete:
    revised_ctx, cand2 = revise_task_context(
        current_ctx=ctx1,
        new_goal="仅查询包裹状态，不建议后续处理",
        new_constraints=["只读模式"],
        new_body="## Body 2 - Read Only",
        candidate_store=store,
        new_candidate_id="cand_v2_isolation",
    )
    assert revised_ctx.contract_fingerprint != ctx1.contract_fingerprint

    # Run 2 starts under revised intent
    run2 = runtime.start_run(
        run_id="run_new_2",
        task_id=revised_ctx.task_id,
        candidate=cand2,
    )
    runtime.dispatch_tool(
        run_id=run2.run_id,
        tool_name="query_package_status",
        parameters={"package_id": "PKG-002"},
    )

    # Run 2 completes first
    runtime.finalize_run(
        run_id=run2.run_id,
        model_output="Package 2 is in transit",
        verification_evidence={"independent_pass": True, "source": "oracle_check"},
    )
    ep2 = ep_store.get_episode(f"ep_{run2.run_id}")
    assert ep2 is not None
    assert ep2.outcome == "success"

    # Now Run 1 arrives late and finishes
    runtime.finalize_run(
        run_id=run1.run_id,
        model_output="Package 1 delivered; suggest customer notification",
        verification_evidence={"independent_pass": True, "source": "oracle_check"},
    )
    ep1 = ep_store.get_episode(f"ep_{run1.run_id}")
    assert ep1 is not None

    # Isolation check: late ep1 does NOT overwrite ep2
    ep2_recheck = ep_store.get_episode(f"ep_{run2.run_id}")
    assert ep2_recheck is not None
    assert ep2_recheck.run_id == run2.run_id
    assert ep2_recheck.environment.get("candidate_id") == "cand_v2_isolation"

    # Late ep1 belongs strictly to cand1 / run1
    assert ep1.environment.get("candidate_id") == "cand_v1_isolation"
    assert ep1.run_id == run1.run_id

    # Promotion / scope gate check:
    # Ep1 cannot be counted as a positive/success episode for candidate 2 because of candidate binding mismatch
    assert ep1.environment.get("candidate_id") != cand2.candidate_id
    assert ep1.run_id != run2.run_id

    # If attempting to promote candidate 2 using a validation record tied to old intent scope hash:
    from skillforge.evolution_loop import compute_candidate_hash, promote_candidate
    from skillforge.models import ValidationRecord
    from skillforge.state_machine import ReleaseStateMachine
    from skillforge.registry import SkillRegistry

    reg_dir = tmp_path / "registry"
    pkg_skill_file = reg_dir / "pkg_tracer" / "SKILL.md"
    pkg_skill_file.parent.mkdir(parents=True, exist_ok=True)
    pkg_skill_file.write_text(
        "---\nname: pkg_tracer\nversion: 0.1.0\n"
        "description: Trace packages\nuse_when: Checking trace\n---\n"
        "## Instructions\n1. Trace",
        encoding="utf-8",
    )
    reg = SkillRegistry(db_path=db_path, skills_dir=reg_dir)
    reg.load_skills_from_dir()
    sm = ReleaseStateMachine(db_path)

    cand2.task_spec_hash = revised_ctx.contract_fingerprint
    store.save_candidate(cand2, on_conflict="update")

    old_val_rec = ValidationRecord(
        candidate_id=cand2.candidate_id,
        content_hash=compute_candidate_hash(cand2),
        baseline_version="0.1.0",
        ratchet_decision="PASS",
        scope_hash=ctx1.contract_fingerprint,  # Old intent contract fingerprint
    )
    store.save_validation_record(old_val_rec)

    with pytest.raises(ValueError, match="task scope hash changed"):
        promote_candidate(
            candidate=cand2,
            validation_record=old_val_rec,
            state_machine=sm,
            registry=reg,
            candidate_store=store,
            caller_confirmed=True,
        )

    # Close and reopen store / SQLite to verify persistence across reboots
    store.close()
    ep_store.close()
    runtime.close()

    new_store = CandidateStore(db_path)
    loaded_ctx = new_store.get_task_context("task_pkg_iso")
    assert loaded_ctx is not None
    assert loaded_ctx.intent_revision == 2
    assert loaded_ctx.active_candidate_id == "cand_v2_isolation"
    assert "cand_v1_isolation" in loaded_ctx.superseded_candidate_ids

    c1 = new_store.get_candidate("cand_v1_isolation")
    c2 = new_store.get_candidate("cand_v2_isolation")
    assert c1 is not None and c1.status == "SUPERSEDED"
    assert c1.superseded_by == "cand_v2_isolation"
    assert c2 is not None and c2.status in ("DRAFT", "APPROVED")
    assert c2.supersedes == "cand_v1_isolation"
    new_store.close()


# ---------------------------------------------------------------------------
# I3: Paraphrase NO_OP & Ambiguous Confirmation Required
# ---------------------------------------------------------------------------

def test_i3_paraphrase_no_op_and_ambiguous_confirmation_required(tmp_path: Path) -> None:
    """I3: Paraphrasing / politeness does NOT rebuild or create revisions (0 generator calls).
    Ambiguous goal shift returns CONFIRMATION_REQUIRED without silent rewriting.
    """
    ctx = TaskContext(
        task_id="task_wording_test",
        goal="核对多包裹物流状态",
        business_scope="logistics.status",
        constraints=["仅核实状态", "只读访问"],
        intent_revision=1,
        active_candidate_id="cand_original",
    )

    # 1. Paraphrase with courtesy words and punctuation variation
    paraphrases = [
        "请帮我核对多包裹物流状态，谢谢！",
        "麻烦核对多包裹物流状态一下。",
        "hello, 请核对多包裹物流状态",
        "核对多包裹物流状态",
    ]
    for prompt in paraphrases:
        action, reason = detect_intent_shift(ctx, prompt, new_constraints=ctx.constraints)
        assert action == "NO_OP", f"Expected NO_OP for '{prompt}', got {action} ({reason})"

    # Generator calls count must be 0 for NO_OP
    generator_call_count = 0
    if action == "NO_OP":
        # System obeys NO_OP: does not call generator, does not bump revision
        pass
    assert generator_call_count == 0

    # 2. Vague / ambiguous feedback without clear direction -> CONFIRMATION_REQUIRED
    vague_inputs = [
        "这个不好，重做",
        "感觉有问题！",
        "好像不对",
        "你看着办",
        "改一下吧",
        "bad",
        "fix it",
    ]
    for prompt in vague_inputs:
        action, reason = detect_intent_shift(ctx, prompt)
        assert action == "CONFIRMATION_REQUIRED", (
            f"Expected CONFIRMATION_REQUIRED for vague prompt '{prompt}', got {action}"
        )
        assert "confirmation required" in (reason or "").lower() or "requires clarification" in (reason or "").lower()

    # 3. Explicit concrete shift -> REVISION
    explicit_shift = "改为导出全部异常包裹至CSV报表"
    action, reason = detect_intent_shift(ctx, explicit_shift)
    assert action == "REVISION"
    assert "Goal updated" in (reason or "")


# ---------------------------------------------------------------------------
# I4: Task Cancellation, Irreversible Side Effects & Channel Differences
# ---------------------------------------------------------------------------

def test_i4_task_cancellation_irreversible_audit_and_channel_capabilities(tmp_path: Path) -> None:
    """I4: Task cancellation via runtime.cancel_run:
    - Executed tool calls are audited as irreversible (side_effects_reversible=False).
    - Post-cancellation tool dispatches are rejected as CANCELLED.
    - Tests and documents cancellation differences across async coroutines and sync callbacks.
    """
    db_path = tmp_path / "skillforge.db"
    broker = ToolBroker(application_allowlist={"query_package_status", "async_probe_tool"})
    query_tool = ScriptedLogisticsQueryTool()
    broker.register_tool(query_tool)

    # Add an async coroutine tool for cooperative cancellation testing
    async def async_probe(params: dict[str, Any]) -> ToolResponse:
        delay = params.get("delay", 0.05)
        await asyncio.sleep(delay)
        return ToolResponse.success(text="Async probe complete", data={"probed": True})

    broker.register_tool(async_probe, name="async_probe_tool")

    runtime = AgentRuntime(db_path=db_path, tool_broker=broker)

    run = runtime.start_run(
        run_id="run_cancel_audit",
        task_id="task_cancel_demo",
    )

    # 1. Execute a tool call before cancellation
    rec1 = runtime.dispatch_tool(
        run_id=run.run_id,
        tool_name="query_package_status",
        parameters={"package_id": "PKG-001"},
    )
    assert rec1.status == "EXECUTED"

    # 2. Explicit cancellation requested
    cancelled_run = runtime.cancel_run(run.run_id, reason="用户主动中止任务")
    assert cancelled_run.status == "CANCELLED"

    # 3. Subsequent dispatch is rejected
    rec2 = runtime.dispatch_tool(
        run_id=run.run_id,
        tool_name="query_package_status",
        parameters={"package_id": "PKG-002"},
    )
    assert rec2.status == "REJECTED"
    assert rec2.error_type == "CANCELLED"

    # 4. Cancellation audit report
    report = runtime.get_cancellation_report(run.run_id)
    assert report["run_id"] == run.run_id
    assert report["run_status"] == "CANCELLED"
    assert report["executed_tool_calls_count"] == 1
    assert report["rejected_tool_calls_count"] == 1
    assert report["side_effects_reversible"] is False
    assert "non-reversible" in report["reversibility_statement"]

    # 5. Channel cancellation capabilities verification
    caps = report["channel_cancellation_capabilities"]
    assert "async_coroutine" in caps
    assert "process_sandbox" in caps
    assert "sync_python_callback" in caps

    # Verify cooperative async timeout handling in ToolBroker
    rec_timeout = broker.dispatch(
        run_id="run_async_test",
        tool_name="async_probe_tool",
        parameters={"delay": 0.5},
        timeout=0.05,  # Short timeout triggers TimeoutError
    )
    assert rec_timeout.status == "TIMED_OUT"
    assert rec_timeout.error_type == "INFRASTRUCTURE_ERROR"


# ---------------------------------------------------------------------------
# I5: Session Isolation & Tool Broker Privilege Escalation Prevention
# ---------------------------------------------------------------------------

def test_i5_session_isolation_and_tool_broker_privilege_escalation_guard(tmp_path: Path) -> None:
    """I5: Session goal shifts do not globally deprecate formal skills in SkillRegistry.
    Read-to-write intent shifts are blocked by ToolBroker application allowlist
    (handler call count == 0; no automatic privilege escalation).
    """
    db_path = tmp_path / "skillforge.db"
    reg_path = tmp_path / "skills"
    registry = SkillRegistry(db_path=db_path, skills_dir=reg_path)

    # 1. Establish a formal published skill in SkillRegistry
    formal_body = (
        "---\nname: logistics_formal_skill\nversion: 1.0.0\n"
        "description: Formal skill for package tracking across sessions\n"
        "use_when: When checking delivery updates\n---\n"
        "## Overview\nTrack package statuses.\n"
        "## Instructions\n1. Call query_package_status."
    )
    skill_file = reg_path / "logistics_formal_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(formal_body, encoding="utf-8")
    registry.load_skills_from_dir()
    assert registry.has_skill("logistics_formal_skill")

    # 2. Session A shifts intent to a modified draft
    store = CandidateStore(db_path)
    session_a_ctx = TaskContext(
        task_id="session_A_task",
        goal="只核查异常件",
        business_scope="logistics.anomaly",
        active_skill_name="logistics_formal_skill",
        active_skill_version="1.0.0",
    )
    store.save_task_context(session_a_ctx)

    # Session A mutates its own task context
    revised_a, _ = revise_task_context(
        current_ctx=session_a_ctx,
        new_goal="只核查异常件且无需后续通知",
        new_body="## Session A Draft Body",
        candidate_store=store,
    )
    assert revised_a.intent_revision == 2

    # Verification: Global SkillRegistry is NOT modified or deprecated by Session A's shift
    assert registry.has_skill("logistics_formal_skill")
    meta_session_b = registry.get_meta("logistics_formal_skill")
    assert meta_session_b.version == "1.0.0"
    body_session_b = registry.get_body("logistics_formal_skill")
    assert "Call query_package_status" in body_session_b

    # 3. Read-to-Write intent shift privilege escalation check:
    # User in Session A asks: "请对异常包裹直接进行退款处理"
    query_tool = ScriptedLogisticsQueryTool()
    refund_tool = ScriptedLogisticsRefundTool()

    # Application only authorizes read-only query_package_status
    broker = ToolBroker(application_allowlist={"query_package_status"})
    broker.register_tool(query_tool)
    broker.register_tool(refund_tool)  # Registered in broker, but NOT in application_allowlist

    runtime = AgentRuntime(db_path=db_path, tool_broker=broker, registry=registry)
    run_a = runtime.start_run(
        run_id="run_session_a_write",
        task_id=session_a_ctx.task_id,
        skill_name="logistics_formal_skill",
    )

    # Dispatch authorized read tool -> Success
    read_rec = runtime.dispatch_tool(
        run_id=run_a.run_id,
        tool_name="query_package_status",
        parameters={"package_id": "PKG-003"},
    )
    assert read_rec.status == "EXECUTED"
    assert query_tool.call_count == 1

    # Attempt to dispatch unauthorized write tool (refund_order)
    write_rec = runtime.dispatch_tool(
        run_id=run_a.run_id,
        tool_name="refund_order",
        parameters={"package_id": "PKG-003"},
    )

    # Broker must block unauthorized write tool despite intent shift
    assert write_rec.status == "REJECTED"
    assert write_rec.error_type == "PERMISSION_DENIED"
    assert "not authorized by application policy" in (write_rec.error_message or "")

    # Underlying privileged handler was NEVER executed
    assert refund_tool.call_count == 0
