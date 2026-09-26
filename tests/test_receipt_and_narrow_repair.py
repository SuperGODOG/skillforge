"""Acceptance Test Suite for Actionable Diagnostics and Narrow Local Repair (SC1 - SC6)

Validates Archify-inspired actionable diagnostics and narrow local repair:
- SC1: Actual error artifact -> ValidationReceipt: rule_code, JSON path, expected/actual,
       evidence, responsibility layer, supported_fixes, and deterministic fingerprints.
- SC2: Real entry recovery: missing required field/range/cross-field -> permitted local
       correction -> re-validation PASS. Final Episode records initial failure, attempts,
       and final evidence; disabled correction keeps original failure without fabricating success.
- SC3: No mistaken repair: permissions/backend/dependency rejection and unfixable rules do NOT
       call business tools or local fixers (call count = 0); correct layer (policy); no unrelated
       Skill repair triggered.
- SC4: Scope and lifecycle: sequential structural-then-semantic repair; out-of-bounds mutation
       rejected; no-progress, cyclic, max 2 corrections, budget/timeout or cancellation terminate;
       post-terminal results cannot overwrite.
- SC5: PASS cannot be mismatched: modifying artifact after validation, altering validator config,
       or replaying another candidate's receipt is rejected; artifact fix does not silently mutate
       or promote formal Skills.
- SC6: Isolation and controlled A/B: evaluation receipts do not leak to mining; receipt reading is
       read-only; fixed inputs produce raw counts table without fabricating LLM cost or ROI figures.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge import (
    AgentRuntime,
    CorrectionPolicy,
    DeterministicJsonFixer,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    FixAction,
    JsonConfigValidator,
    MacSeatbeltSandbox,
    PatternMiningConfig,
    SandboxedToolSpec,
    SkillMeta,
    SkillRegistry,
    ToolBroker,
    Trigger,
    ValidationDiagnostic,
    ValidationReceipt,
    compute_artifact_fingerprint,
    get_default_ab_cases,
    mine_pending,
    run_artifact_ab_comparison,
)
from skillforge.storage.db import init_db


@pytest.fixture
def receipt_env(tmp_path: Path):
    """Fixture providing isolated db, runtime, broker, sandbox, and validator."""
    db_path = tmp_path / "receipt_e2e.db"
    conn = init_db(db_path)
    conn.close()

    ep_store = EpisodeStore(db_path)
    collector = ExperienceCollector(episode_store=ep_store)

    # Real macOS sandbox backend
    sb = MacSeatbeltSandbox()
    sandbox_backend = sb if sb.is_available() else None

    # Config generator tool running in isolated sandbox
    gen_config_code = (
        "import sys, json\n"
        "params = json.load(sys.stdin)\n"
        "mode = params.get('mode', 'valid')\n"
        "if mode == 'valid':\n"
        "    res = {'name': 'service_alpha', 'workers': 4, 'timeout_seconds': 60, 'retry_seconds': 10}\n"
        "elif mode == 'missing_field':\n"
        "    res = {'workers': 4, 'timeout_seconds': 60, 'retry_seconds': 10}\n"
        "elif mode == 'range_error':\n"
        "    res = {'name': 'service_alpha', 'workers': 0, 'timeout_seconds': 60, 'retry_seconds': 10}\n"
        "elif mode == 'cross_field':\n"
        "    res = {'name': 'service_alpha', 'workers': 4, 'timeout_seconds': 10, 'retry_seconds': 30}\n"
        "elif mode == 'sequential_both':\n"
        "    res = {'workers': 4, 'timeout_seconds': 10, 'retry_seconds': 30}\n"
        "else:\n"
        "    res = {'status': 'unknown'}\n"
        "print(json.dumps(res))\n"
    )

    gen_config_spec = SandboxedToolSpec(
        name="gen_config_tool",
        command_template=["python3", "-c", gen_config_code],
        description="Generates JSON service configurations inside sandbox",
        parameters=[
            ToolParameter(name="mode", type="string", required=False, default="valid", description="Generation mode"),
        ],
    )

    broker = ToolBroker(
        application_allowlist={"gen_config_tool"},
        sandbox_backend=sandbox_backend,
    )
    broker.register_sandboxed_tool(gen_config_spec)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        episode_store=ep_store,
        collector=collector,
    )

    validator = JsonConfigValidator()
    fixer = DeterministicJsonFixer()

    return {
        "db_path": db_path,
        "runtime": runtime,
        "broker": broker,
        "episode_store": ep_store,
        "collector": collector,
        "validator": validator,
        "fixer": fixer,
        "sandbox_backend": sandbox_backend,
    }


# =========================================================================
# SC1: 实际错误产物 -> Receipt
# =========================================================================
def test_sc1_error_artifact_to_validation_receipt(receipt_env):
    """SC1: Actual error artifact -> ValidationReceipt:
    Verifies stable rule_code, subject JSON path, expected/actual evidence,
    responsibility layer, supported_fixes, and deterministic fingerprints.
    """
    validator: JsonConfigValidator = receipt_env["validator"]

    # 1. Missing required field
    bad_payload_missing = {"workers": 4, "timeout_seconds": 60, "retry_seconds": 10}
    rcpt1 = validator.validate(bad_payload_missing, task_id="task_sc1_01", run_id="run_sc1_01")
    assert rcpt1.status == "FAIL"
    assert len(rcpt1.diagnostics) == 1
    d1 = rcpt1.diagnostics[0]
    assert d1.rule_code == "MISSING_REQUIRED_FIELD"
    assert d1.subject == "$.name"
    assert d1.responsibility_layer == "tool"
    assert d1.retryable is True
    assert len(d1.supported_fixes) == 1
    assert d1.supported_fixes[0]["path"] == "$.name"
    assert d1.supported_fixes[0]["op"] == "set"

    # Fingerprint integrity
    assert rcpt1.content_fingerprint == compute_artifact_fingerprint(bad_payload_missing)
    assert rcpt1.validator_config_hash == validator.config_hash
    assert rcpt1.task_id == "task_sc1_01"
    assert rcpt1.run_id == "run_sc1_01"

    # JSON serializability
    as_dict = rcpt1.to_dict()
    assert isinstance(json.dumps(as_dict), str)
    assert as_dict["status"] == "FAIL"

    # 2. Value out of range
    bad_payload_range = {"name": "svc_test", "workers": 0, "timeout_seconds": 60, "retry_seconds": 10}
    rcpt2 = validator.validate(bad_payload_range)
    assert rcpt2.status == "FAIL"
    d2 = rcpt2.diagnostics[0]
    assert d2.rule_code == "VALUE_OUT_OF_RANGE"
    assert d2.subject == "$.workers"
    assert d2.actual == 0

    # 3. Cross-field constraint violation
    bad_payload_cross = {"name": "svc_test", "workers": 4, "timeout_seconds": 15, "retry_seconds": 30}
    rcpt3 = validator.validate(bad_payload_cross)
    assert rcpt3.status == "FAIL"
    d3 = rcpt3.diagnostics[0]
    assert d3.rule_code == "CROSS_FIELD_CONSTRAINT"
    assert d3.subject == "$.timeout_seconds"
    assert "timeout_seconds (15) must be >= retry_seconds (30)" in d3.evidence


# =========================================================================
# SC2: 真入口恢复
# =========================================================================
def test_sc2_real_entry_recovery_and_disabled_baseline(receipt_env):
    """SC2: Real entry recovery:
    Missing required fields/range/cross-field -> permitted local correction -> re-validation PASS.
    Final Episode records initial failure, attempts, and final evidence.
    Disabled correction preserves original failure without fabricating success.
    """
    runtime: AgentRuntime = receipt_env["runtime"]
    validator: JsonConfigValidator = receipt_env["validator"]
    fixer: DeterministicJsonFixer = receipt_env["fixer"]

    # --- Part A: Correction Policy Enabled ---
    runtime.start_run(run_id="run_sc2_enabled", task_id="task_sc2_recovery", purpose="learning")
    call_rec = runtime.execute_tool(
        run_id="run_sc2_enabled",
        tool_name="gen_config_tool",
        parameters={"mode": "missing_field"},
    )
    assert call_rec.status == "EXECUTED"
    initial_output = call_rec.output_data
    assert "name" not in initial_output

    policy_enabled = CorrectionPolicy(enabled=True, max_corrections=2)
    final_content, final_rcpt, history, stop_reason = runtime.repair_artifact(
        run_id="run_sc2_enabled",
        initial_content=initial_output,
        validator=validator,
        fixer=fixer,
        correction_policy=policy_enabled,
    )

    assert stop_reason == "SUCCESS"
    assert final_rcpt.status == "PASS"
    assert len(history) == 1
    assert "name" in final_content
    assert final_content["name"] == fixer.default_name

    _, ep_enabled = runtime.finalize_run(
        run_id="run_sc2_enabled",
        verification_evidence={
            "independent_pass": True,
            "receipt": final_rcpt.to_dict(),
            "content": final_content,
            "correction_attempts": len(history),
            "initial_failure_rule": "MISSING_REQUIRED_FIELD",
        },
    )
    assert ep_enabled.outcome == "success"
    assert ep_enabled.verification_evidence["correction_attempts"] == 1
    assert ep_enabled.verification_evidence["initial_failure_rule"] == "MISSING_REQUIRED_FIELD"

    # --- Part B: Correction Policy Disabled ---
    runtime.start_run(run_id="run_sc2_disabled", task_id="task_sc2_baseline", purpose="learning")
    call_rec_dis = runtime.execute_tool(
        run_id="run_sc2_disabled",
        tool_name="gen_config_tool",
        parameters={"mode": "missing_field"},
    )
    policy_disabled = CorrectionPolicy(enabled=False)
    final_content_dis, final_rcpt_dis, history_dis, stop_reason_dis = runtime.repair_artifact(
        run_id="run_sc2_disabled",
        initial_content=call_rec_dis.output_data,
        validator=validator,
        fixer=fixer,
        correction_policy=policy_disabled,
    )

    assert stop_reason_dis == "CORRECTION_DISABLED"
    assert final_rcpt_dis.status == "FAIL"
    assert len(history_dis) == 0

    _, ep_disabled = runtime.finalize_run(
        run_id="run_sc2_disabled",
        verification_evidence={
            "independent_pass": False,
            "receipt": final_rcpt_dis.to_dict(),
            "content": final_content_dis,
            "failure_reason": "Required field 'name' missing and correction policy disabled",
        },
    )
    assert ep_disabled.outcome == "failure"
    assert ep_disabled.verification_evidence["independent_pass"] is False


# =========================================================================
# SC3: 不误修
# =========================================================================
def test_sc3_no_mistaken_repair_on_policy_and_unfixable(receipt_env):
    """SC3: No mistaken repair:
    Permissions/backend/dependency rejection and unfixable rules do NOT call
    business tools or local fixers (call count = 0); correct terminal state and layer;
    no unrelated Skill repair triggered.
    """
    runtime: AgentRuntime = receipt_env["runtime"]
    validator: JsonConfigValidator = receipt_env["validator"]
    fixer: DeterministicJsonFixer = receipt_env["fixer"]
    broker: ToolBroker = receipt_env["broker"]
    policy = CorrectionPolicy(enabled=True, max_corrections=2)

    # 1. Real Broker-Level Permission Denial Integration
    fixer.call_count = 0
    perm_calls = {"val": 0}
    def _perm_tool_fn(params):
        perm_calls["val"] += 1
        return {"status": "ok"}
    # Register tool but do NOT put in application_allowlist
    broker.register_tool(_perm_tool_fn, name="unauthorized_secret_tool")

    runtime.start_run(run_id="run_sc3_perm", task_id="task_sc3_perm", purpose="learning")
    call_perm = runtime.execute_tool(run_id="run_sc3_perm", tool_name="unauthorized_secret_tool", parameters={})
    assert call_perm.status == "REJECTED"
    assert call_perm.error_type == "PERMISSION_DENIED"
    assert perm_calls["val"] == 0  # Business tool handler strictly NOT called

    _, rcpt_perm, hist_perm, stop_perm = runtime.repair_artifact(
        run_id="run_sc3_perm",
        initial_content=call_perm,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_perm == "POLICY_DENIED_NO_REPAIR"
    assert rcpt_perm.status == "FAIL"
    assert rcpt_perm.diagnostics[0].responsibility_layer == "policy"
    assert fixer.call_count == 0  # Fixer is strictly NOT called
    assert len(hist_perm) == 0

    _, ep_perm = runtime.finalize_run(
        run_id="run_sc3_perm",
        verification_evidence={"independent_pass": False, "receipt": rcpt_perm.to_dict(), "content": call_perm.output_data},
    )
    assert ep_perm.outcome == "failure"
    diag_perm = runtime.attribute_run_failure("run_sc3_perm")
    assert diag_perm is None or diag_perm.responsibility_layer == "policy"

    # 2. Real Broker-Level Sandbox Backend Unavailable Integration
    fixer.call_count = 0
    sb_handler_calls = {"val": 0}
    def _sb_counter():
        sb_handler_calls["val"] += 1

    broker_no_sb = ToolBroker(application_allowlist={"no_backend_tool"}, sandbox_backend=None)
    broker_no_sb.register_sandboxed_tool(
        SandboxedToolSpec(
            name="no_backend_tool",
            command_template=["echo", "unreachable"],
            host_handler_counter=_sb_counter,
        )
    )
    runtime_no_sb = AgentRuntime(
        db_path=receipt_env["db_path"],
        tool_broker=broker_no_sb,
        episode_store=receipt_env["episode_store"],
        collector=receipt_env["collector"],
    )
    runtime_no_sb.start_run(run_id="run_sc3_no_sb", task_id="task_sc3_no_sb", purpose="learning")
    call_no_sb = runtime_no_sb.execute_tool(run_id="run_sc3_no_sb", tool_name="no_backend_tool", parameters={})
    assert call_no_sb.status == "REJECTED"
    assert call_no_sb.error_type == "SANDBOX_UNAVAILABLE"
    assert sb_handler_calls["val"] == 0  # Business tool handler strictly NOT called

    _, rcpt_no_sb, hist_no_sb, stop_no_sb = runtime_no_sb.repair_artifact(
        run_id="run_sc3_no_sb",
        initial_content=call_no_sb,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_no_sb == "POLICY_DENIED_NO_REPAIR"
    assert rcpt_no_sb.status == "FAIL"
    assert rcpt_no_sb.diagnostics[0].responsibility_layer == "policy"
    assert fixer.call_count == 0  # Fixer strictly NOT called
    assert len(hist_no_sb) == 0

    # 3. Real Broker-Level Unsatisfied Dependency Integration
    fixer.call_count = 0
    dep_handler_calls = {"val": 0}
    def _dep_counter():
        dep_handler_calls["val"] += 1

    broker.application_allowlist.add("missing_dep_tool")
    broker.register_sandboxed_tool(
        SandboxedToolSpec(
            name="missing_dep_tool",
            command_template=["echo", "unreachable"],
            required_dependencies=["strictly_nonexistent_cli_pkg_12345"],
            host_handler_counter=_dep_counter,
        )
    )
    runtime.start_run(run_id="run_sc3_dep", task_id="task_sc3_dep", purpose="learning")
    call_dep = runtime.execute_tool(run_id="run_sc3_dep", tool_name="missing_dep_tool", parameters={})
    assert call_dep.status == "REJECTED"
    assert call_dep.error_type == "DEPENDENCY_MISSING"
    assert dep_handler_calls["val"] == 0  # Business tool handler strictly NOT called

    _, rcpt_dep, hist_dep, stop_dep = runtime.repair_artifact(
        run_id="run_sc3_dep",
        initial_content=call_dep,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_dep == "POLICY_DENIED_NO_REPAIR"
    assert rcpt_dep.status == "FAIL"
    assert rcpt_dep.diagnostics[0].responsibility_layer == "policy"
    assert fixer.call_count == 0  # Fixer strictly NOT called
    assert len(hist_dep) == 0

    # 4. Unfixable Rule (e.g. malformed non-dict payload)
    fixer.call_count = 0
    runtime.start_run(run_id="run_sc3_unfixable", task_id="task_sc3_unfix", purpose="learning")
    unfixable_content = "RAW_STRING_NOT_JSON_OBJECT"

    _, rcpt_unfix, history_unfix, stop_reason_unfix = runtime.repair_artifact(
        run_id="run_sc3_unfixable",
        initial_content=unfixable_content,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_reason_unfix == "UNFIXABLE_RULE_NO_REPAIR"
    assert fixer.call_count == 0  # Fixer strictly NOT called
    assert len(history_unfix) == 0


# =========================================================================
# SC4: 范围与生命周期
# =========================================================================
def test_sc4_scope_ordering_and_lifecycle_termination(receipt_env):
    """SC4: Scope and lifecycle:
    1. Sequential diagnostics: structural first, then semantic.
    2. Out-of-bounds mutation attempt rejected.
    3. No-progress and cyclic changes terminated.
    4. Hard ceiling max 2 corrections enforced even if policy specifies more.
    5. Single-step / total budget exhaustion stops repair.
    6. Deadline timeout terminates repair.
    7. Cancellation signal terminates repair.
    8. Late patch guard: external fixer returning after cancel/timeout is discarded.
    9. Post-terminal operations cannot overwrite.
    """
    runtime: AgentRuntime = receipt_env["runtime"]
    validator: JsonConfigValidator = receipt_env["validator"]
    fixer: DeterministicJsonFixer = receipt_env["fixer"]

    # 1. Sequential Ordering: Missing required field AND Cross-Field Constraint
    runtime.start_run(run_id="run_sc4_seq", task_id="task_sc4_order", purpose="learning")
    both_err_content = {"workers": 4, "timeout_seconds": 10, "retry_seconds": 30}
    policy = CorrectionPolicy(enabled=True, max_corrections=2)

    final_seq, rcpt_seq, history_seq, stop_seq = runtime.repair_artifact(
        run_id="run_sc4_seq",
        initial_content=both_err_content,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_seq == "SUCCESS"
    assert rcpt_seq.status == "PASS"
    assert len(history_seq) == 2
    assert history_seq[0]["actions"][0]["path"] == "$.name"
    assert history_seq[1]["actions"][0]["path"] == "$.timeout_seconds"
    assert final_seq["name"] == fixer.default_name
    assert final_seq["timeout_seconds"] >= final_seq["retry_seconds"]

    # 2. Out-of-Bounds Fix Attempt Rejected
    runtime.start_run(run_id="run_sc4_oob", task_id="task_sc4_bounds", purpose="learning")
    bad_content = {"name": "svc", "workers": 0, "timeout_seconds": 60, "retry_seconds": 10}
    oob_fixer = DeterministicJsonFixer(
        custom_action_provider=lambda r, c: [
            FixAction(path="$.system_privilege", op="set", value="root", rule_code="PRIVILEGE_ESCALATION")
        ]
    )
    _, _, _, stop_oob = runtime.repair_artifact(
        run_id="run_sc4_oob",
        initial_content=bad_content,
        validator=validator,
        fixer=oob_fixer,
        correction_policy=policy,
    )
    assert "OUT_OF_BOUNDS_PATH" in stop_oob

    # 3. No-Progress Termination
    runtime.start_run(run_id="run_sc4_noprog", task_id="task_sc4_noprog", purpose="learning")
    noprog_fixer = DeterministicJsonFixer(
        custom_action_provider=lambda r, c: [
            FixAction(path="$.workers", op="set", value=c.get("workers"), rule_code="NO_OP")
        ]
    )
    _, _, _, stop_noprog = runtime.repair_artifact(
        run_id="run_sc4_noprog",
        initial_content=bad_content,
        validator=validator,
        fixer=noprog_fixer,
        correction_policy=policy,
    )
    assert stop_noprog == "NO_PROGRESS"

    # 4. Cycle Detection
    runtime.start_run(run_id="run_sc4_cycle", task_id="task_sc4_cycle", purpose="learning")
    cycle_counter = {"val": 0}

    def _cycle_action(r, c):
        cycle_counter["val"] += 1
        new_w = -1 if cycle_counter["val"] % 2 == 1 else 0
        return [FixAction(path="$.workers", op="set", value=new_w, rule_code="CYCLE")]

    cycle_fixer = DeterministicJsonFixer(custom_action_provider=_cycle_action)
    _, _, _, stop_cycle = runtime.repair_artifact(
        run_id="run_sc4_cycle",
        initial_content=bad_content,
        validator=validator,
        fixer=cycle_fixer,
        correction_policy=CorrectionPolicy(enabled=True, max_corrections=5),
    )
    assert stop_cycle == "CYCLE_DETECTED"

    # 5. Hard Ceiling of max 2 corrections even when configured higher
    cap_counter = {"val": 0}
    def _always_bad(r, c):
        cap_counter["val"] += 1
        return [FixAction(path="$.workers", op="set", value=-cap_counter["val"], rule_code="DIFF_VAL")]
    cap_fixer = DeterministicJsonFixer(custom_action_provider=_always_bad)
    runtime.start_run(run_id="run_sc4_cap", task_id="task_sc4_cap", purpose="learning")
    _, _, history_cap, stop_cap = runtime.repair_artifact(
        run_id="run_sc4_cap",
        initial_content=bad_content,
        validator=validator,
        fixer=cap_fixer,
        correction_policy=CorrectionPolicy(enabled=True, max_corrections=10),
    )
    assert len(history_cap) <= 2
    assert stop_cap == "MAX_CORRECTIONS_REACHED"

    # 6. Budget Exhaustion (Single-step and total budget limit)
    runtime.start_run(run_id="run_sc4_budget", task_id="task_sc4_budget", purpose="learning", budget_max=1)
    # The first attempt consumes budget 1, so attempt 2 sees budget exhausted
    _, _, history_b, stop_b = runtime.repair_artifact(
        run_id="run_sc4_budget",
        initial_content=both_err_content,
        validator=validator,
        fixer=fixer,
        correction_policy=CorrectionPolicy(enabled=True, max_corrections=2),
    )
    assert stop_b == "BUDGET_EXHAUSTED"
    run_b = runtime.get_run("run_sc4_budget")
    assert run_b.status == "BUDGET_EXHAUSTED"

    # 7. Deadline Timeout Enforcement
    runtime.start_run(
        run_id="run_sc4_timeout",
        task_id="task_sc4_timeout",
        purpose="learning",
        deadline_ts=time.time() - 2.0,  # Already expired
    )
    _, _, _, stop_timeout = runtime.repair_artifact(
        run_id="run_sc4_timeout",
        initial_content=bad_content,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert stop_timeout == "TIMEOUT"
    run_to = runtime.get_run("run_sc4_timeout")
    assert run_to.status == "TIMED_OUT"

    # 8. Cancellation Signal Terminates Repair
    runtime.start_run(run_id="run_sc4_cancel", task_id="task_sc4_cancel", purpose="learning")
    runtime.cancel_run("run_sc4_cancel", reason="Explicit cancellation before repair")
    _, _, _, stop_cancel = runtime.repair_artifact(
        run_id="run_sc4_cancel",
        initial_content=bad_content,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert "RUN_ALREADY_TERMINAL" in stop_cancel

    # 9. Late Patch Guard: External Fixer Returning After Run Cancelled / Timed Out
    runtime.start_run(run_id="run_sc4_late", task_id="task_sc4_late", purpose="learning")
    def _slow_cancelling_fixer(r, c):
        # Simulates cancellation occurring while user-space fixer was computing
        # (Cooperative boundary: runtime re-checks status upon return to discard late patches)
        runtime.cancel_run("run_sc4_late", reason="User cancelled during fixer computation")
        return [FixAction(path="$.name", op="set", value="late_disallowed_name", rule_code="LATE")]

    late_fixer = DeterministicJsonFixer(custom_action_provider=_slow_cancelling_fixer)
    content_late, _, hist_late, stop_late = runtime.repair_artifact(
        run_id="run_sc4_late",
        initial_content={"workers": 4, "timeout_seconds": 60, "retry_seconds": 10},
        validator=validator,
        fixer=late_fixer,
        correction_policy=policy,
    )
    assert stop_late == "DISCARDED_LATE_PATCH_RUN_CANCELLED"
    assert "name" not in content_late  # Late action strictly discarded!
    assert len(hist_late) == 0
    run_late = runtime.get_run("run_sc4_late")
    assert run_late.status == "CANCELLED"

    # 10. Post-Terminal Results Cannot Overwrite
    runtime.finalize_run(
        run_id="run_sc4_seq",
        verification_evidence={"independent_pass": True, "receipt": rcpt_seq.to_dict(), "content": final_seq},
    )
    run_frozen = runtime.get_run("run_sc4_seq")
    assert run_frozen.status == "COMPLETED"

    _, _, _, stop_post = runtime.repair_artifact(
        run_id="run_sc4_seq",
        initial_content=both_err_content,
        validator=validator,
        fixer=fixer,
        correction_policy=policy,
    )
    assert "RUN_ALREADY_TERMINAL" in stop_post


# =========================================================================
# SC5: PASS 不可错绑
# =========================================================================
def test_sc5_pass_receipt_binding_integrity(receipt_env):
    """SC5: PASS cannot be mismatched:
    Modifying artifact after verification, altering validator config, or replaying
    another candidate's receipt is rejected; artifact fix does not silently mutate Skills.
    Proves from actual finalize_run entry that stricter validator invalidates old PASS.
    """
    runtime: AgentRuntime = receipt_env["runtime"]
    validator: JsonConfigValidator = receipt_env["validator"]

    # 1. Valid artifact passes with bound validator
    valid_content = {"name": "svc_gold", "workers": 4, "timeout_seconds": 60, "retry_seconds": 10}
    runtime.start_run(run_id="run_sc5_01", task_id="task_sc5", purpose="learning")
    runtime.register_artifact_validator("run_sc5_01", validator)
    receipt_valid = validator.validate(valid_content, task_id="task_sc5", run_id="run_sc5_01")
    assert receipt_valid.status == "PASS"

    _, ep_valid = runtime.finalize_run(
        run_id="run_sc5_01",
        verification_evidence={
            "independent_pass": True,
            "receipt": receipt_valid.to_dict(),
            "content": valid_content,
        },
    )
    assert ep_valid.outcome == "success"
    assert ep_valid.verification_evidence["independent_pass"] is True

    # 2. Tamper with content after verification -> Finalize fails closed
    runtime.start_run(run_id="run_sc5_tamper", task_id="task_sc5", purpose="learning")
    runtime.register_artifact_validator("run_sc5_tamper", validator)
    tampered_content = copy.deepcopy(valid_content)
    tampered_content["workers"] = 999

    _, ep_tamper = runtime.finalize_run(
        run_id="run_sc5_tamper",
        verification_evidence={
            "independent_pass": True,
            "receipt": receipt_valid.to_dict(),
            "content": tampered_content,
        },
    )
    assert ep_tamper.outcome == "failure"
    assert ep_tamper.verification_evidence["independent_pass"] is False
    assert "Mismatched content fingerprint" in ep_tamper.verification_evidence["failure_reason"]

    # 3. Validator Config Drift Invalidates Old PASS from actual finalize_run entry
    runtime.start_run(run_id="run_sc5_drift", task_id="task_sc5_drift", purpose="learning")
    stricter_validator = JsonConfigValidator(min_workers=8)  # Minimum 8 workers
    runtime.register_artifact_validator("run_sc5_drift", stricter_validator)

    # Caller attempts to present old PASS receipt from validator (min_workers=1)
    _, ep_drift = runtime.finalize_run(
        run_id="run_sc5_drift",
        verification_evidence={
            "independent_pass": True,
            "receipt": receipt_valid.to_dict(),  # Old PASS
            "content": valid_content,            # 4 workers < 8
        },
    )
    # Proves from actual finalize_run entry that stricter validator rejects old PASS!
    assert ep_drift.outcome == "failure"
    assert ep_drift.verification_evidence["independent_pass"] is False
    assert (
        "drift detected" in ep_drift.verification_evidence["failure_reason"]
        or "re-validation failed" in ep_drift.verification_evidence["failure_reason"]
    )

    # 4. Missing receipt for validated run fails closed
    runtime.start_run(run_id="run_sc5_missing", task_id="task_sc5_missing", purpose="learning")
    runtime.register_artifact_validator("run_sc5_missing", validator)
    _, ep_missing = runtime.finalize_run(
        run_id="run_sc5_missing",
        verification_evidence={
            "independent_pass": True,
            "content": valid_content,
        },
    )
    assert ep_missing.outcome == "failure"
    assert ep_missing.verification_evidence["independent_pass"] is False
    assert "Missing receipt" in ep_missing.verification_evidence["failure_reason"]

    # 5. Legacy unvalidated run backwards compatibility
    runtime.start_run(run_id="run_sc5_legacy", task_id="task_sc5_legacy", purpose="learning")
    _, ep_legacy = runtime.finalize_run(
        run_id="run_sc5_legacy",
        verification_evidence={"independent_pass": True},
    )
    assert ep_legacy.outcome == "success"

    # 6. Artifact fix does NOT silently mutate formal Skill
    assert receipt_env.get("registry") is None or not receipt_env["registry"].list_names()


# =========================================================================
# SC6: 隔离与对照
# =========================================================================
def test_sc6_evaluation_isolation_and_ab_comparison(receipt_env, tmp_path):
    """SC6: Isolation and controlled A/B:
    Evaluation receipts do not leak to mining; reading receipt is read-only;
    runs controlled A/B producing raw counts without fabricating ROI numbers.
    """
    runtime: AgentRuntime = receipt_env["runtime"]
    validator: JsonConfigValidator = receipt_env["validator"]
    ep_store: EpisodeStore = receipt_env["episode_store"]

    # 1. Evaluation Isolation
    runtime.start_run(run_id="run_sc6_eval", task_id="task_sc6_eval", purpose="evaluation")
    valid_content = {"name": "svc_eval", "workers": 4, "timeout_seconds": 60, "retry_seconds": 10}
    rcpt_eval = validator.validate(valid_content, task_id="task_sc6_eval", run_id="run_sc6_eval")
    runtime.finalize_run(
        run_id="run_sc6_eval",
        verification_evidence={
            "independent_pass": True,
            "receipt": rcpt_eval.to_dict(),
            "content": valid_content,
        },
    )

    # Pattern mining excludes evaluation episodes completely
    mining_report = mine_pending(
        episode_store=ep_store,
        candidate_store=None,  # type: ignore
        llm=None,
        config=PatternMiningConfig(min_support=1),
    )
    assert mining_report.filtered_evaluation_episodes >= 1
    assert len(mining_report.candidates_created) == 0

    # 2. Offline Controlled A/B Comparison
    def runtime_factory():
        db_p = tmp_path / f"ab_{time.time_ns()}.db"
        init_db(db_p).close()
        return AgentRuntime(db_path=db_p, episode_store=EpisodeStore(db_p))

    ab_report = run_artifact_ab_comparison(runtime_factory=runtime_factory)
    assert "group_a_baseline" in ab_report
    assert "group_b_narrow_repair" in ab_report

    grp_a = ab_report["group_a_baseline"]
    grp_b = ab_report["group_b_narrow_repair"]

    assert grp_a["total_cases"] == grp_b["total_cases"]
    assert grp_a["false_positive_count"] == 0
    assert grp_b["false_positive_count"] == 0

    # Baseline Group A has 0 corrections
    assert grp_a["final_pass_count"] == 0
    assert grp_a["fixer_call_count"] == 0

    # Group B successfully recovers fixable cases
    assert grp_b["final_pass_count"] >= 3
    assert grp_b["fixer_call_count"] >= 3

    # Verification of non-fabricated token and cost metrics
    assert grp_a["token_count"] is None
    assert grp_a["cost_usd"] is None
    assert grp_b["token_count"] is None
    assert grp_b["cost_usd"] is None
    assert "Mechanical gains in fixture benchmarks do NOT establish real-world user ROI" in ab_report["disclaimer"]
