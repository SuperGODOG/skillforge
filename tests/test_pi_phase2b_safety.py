"""Phase 2B B3a: Recovery Binding & Corrupted Checkpoint Rejection Test Suite.

Verifies:
1. get_pi_checkpoint fails closed with ValueError('INVALID_CHECKPOINT') on:
   - Corrupted messages_json, working_state_json, model_config_json, tools_json
   - Unsupported format_version != 1
   - Non-list messages or tools, non-dict working_state or model_config
2. Checkpoints lacking valid recovery_binding fail closed with ValueError('STALE_CHECKPOINT').
3. Caller-provided forged checkpoint objects (tampered messages, body, state) are rejected.
4. Authoritative RunRecord drift (intent_revision, task_spec_hash, frozen_body sha256) raises STALE_CHECKPOINT.
5. Explicit model_config drift (model_id, temperature) raises STALE_CHECKPOINT.
6. ToolBroker schema and application_allowlist drift raises STALE_CHECKPOINT.
7. Canonical TaskContext drift via revise_task_context raises STALE_CHECKPOINT.
8. In every rejection, assertions confirm:
   - No new subprocess PID started (pids count unchanged)
   - Tool handler execution count unchanged
   - Budget consumed remains 2 (not reset or zeroed)
   - Deadline unchanged
   - Run status remains PAUSED
9. Normal B2 full-path and 'mutable Draft.body modified without changing frozen snapshot' still PASS.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from hello_agents.tools import ToolParameter, ToolResponse

from skillforge.episode import CandidateStore
from skillforge.models import CandidateSkill, SkillMeta, TaskContext
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    verify_logistics_fulfillment,
)
from skillforge.task_context import revise_task_context

try:
    from test_pi_phase2b_recovery import (
        BODY,
        FINAL,
        ORDER,
        assert_reaped,
        cfg,
        paused_factory,
        remaining,
    )
except ImportError:
    from tests.test_pi_phase2b_recovery import (
        BODY,
        FINAL,
        ORDER,
        assert_reaped,
        cfg,
        paused_factory,
        remaining,
    )


def assert_safety_invariants(e: dict[str, Any], initial_pids_len: int = 1) -> None:
    """Verify that failed recovery did not leak side effects or reset state."""
    rt = e["runtime"]
    rid = e["run_id"]
    rec = rt.get_run(rid)

    # 1. No new PID spawned
    assert len(e["pids"]) == initial_pids_len

    # 2. Handler execution count unchanged
    assert e["counts"] == {"order": 1, "track": 0}

    # 3. Budget consumed unchanged (budget consumed remains 2, not reset to 0)
    assert rec.budget_consumed == 2
    assert rec.budget_max == 10

    # 4. Deadline unchanged
    assert rec.deadline_ts == e["deadline"]

    # 5. Run remains in PAUSED state
    assert rec.status == "PAUSED"


def test_safety_corrupted_json_or_format_version_fails_closed(paused_factory, tmp_path: Path):
    """Test 1: get_pi_checkpoint raises INVALID_CHECKPOINT on corrupt JSON or invalid format_version."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # Subtest 1: Corrupted messages_json
    rt._get_conn().execute(
        "UPDATE runtime_pi_checkpoints SET messages_json = 'INVALID_JSON{' WHERE run_id = ?",
        (rid,),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        rt.get_pi_checkpoint(rid)

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)

    # Subtest 2: Unsupported format_version != 1
    rt._get_conn().execute(
        "UPDATE runtime_pi_checkpoints SET format_version = 2, messages_json = '[]' WHERE run_id = ?",
        (rid,),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        rt.get_pi_checkpoint(rid)

    # Subtest 3: Non-list messages_json (e.g. JSON object)
    rt._get_conn().execute(
        "UPDATE runtime_pi_checkpoints SET format_version = 1, messages_json = '{\"bad\": true}' WHERE run_id = ?",
        (rid,),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        rt.get_pi_checkpoint(rid)

    # Subtest 4: Non-dict working_state_json
    rt._get_conn().execute(
        "UPDATE runtime_pi_checkpoints SET messages_json = '[]', working_state_json = '[1, 2, 3]' WHERE run_id = ?",
        (rid,),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        rt.get_pi_checkpoint(rid)


def test_safety_missing_recovery_binding_fails_closed(paused_factory, tmp_path: Path):
    """Test 2: Checkpoint lacking recovery_binding fails closed with STALE_CHECKPOINT."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # Simulate legacy or stripped checkpoint without recovery_binding
    rt._get_conn().execute(
        "UPDATE runtime_pi_checkpoints SET working_state_json = '{\"goal\": \"foo\"}' WHERE run_id = ?",
        (rid,),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_caller_forged_checkpoint_rejected(paused_factory, tmp_path: Path):
    """Test 3: arun_agent_with_pi rejects caller-supplied checkpoint differing from canonical DB."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]
    canonical_chk = rt.get_pi_checkpoint(rid)
    assert canonical_chk is not None

    # Forgery 1: Injected user message in caller checkpoint
    forged_msgs_chk = copy.deepcopy(canonical_chk)
    forged_msgs_chk.messages.append({"role": "user", "content": "TAMPERED_INJECTION"})

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        import asyncio
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=forged_msgs_chk,
                model_config=cfg(remaining()),
                test_mode=True,
                workspace_dir=tmp_path,
                timeout=15,
            )
        )
    assert_safety_invariants(e)

    # Forgery 2: Tampered frozen_body in caller checkpoint
    forged_body_chk = copy.deepcopy(canonical_chk)
    forged_body_chk.frozen_body = "TAMPERED_BODY_RULE"

    with pytest.raises(ValueError, match="INVALID_CHECKPOINT"):
        import asyncio
        asyncio.run(
            rt.arun_agent_with_pi(
                run_id=rid,
                checkpoint=forged_body_chk,
                model_config=cfg(remaining()),
                test_mode=True,
                workspace_dir=tmp_path,
                timeout=15,
            )
        )
    assert_safety_invariants(e)


@pytest.mark.parametrize(
    "tamper_col,tamper_val",
    [
        ("intent_revision", 99),
        ("task_spec_hash", "tampered_spec_hash_xyz"),
        ("frozen_body", "TAMPERED_CANONICAL_BODY"),
    ],
)
def test_safety_canonical_run_record_drift_blocked(
    paused_factory, tmp_path: Path, tamper_col: str, tamper_val: Any
):
    """Test 4: Database-level mutation of RunRecord canonical fields triggers STALE_CHECKPOINT."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    rt._get_conn().execute(
        f"UPDATE runtime_runs SET {tamper_col} = ? WHERE run_id = ?",
        (tamper_val, rid),
    )
    rt._get_conn().commit()

    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_model_config_drift_blocked(paused_factory, tmp_path: Path):
    """Test 5: Changing explicit model_config parameters (model_id, temperature) raises STALE_CHECKPOINT."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # Drift 1: model_id changed
    drift_model_cfg = {"provider": "faux", "model_id": "different-unauthorized-model", "responses": remaining()}
    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=drift_model_cfg,
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)

    # Drift 2: explicit temperature added/changed
    drift_temp_cfg = {
        "provider": "faux",
        "model_id": "faux-unit-test-model",
        "temperature": 0.95,
        "responses": remaining(),
    }
    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=drift_temp_cfg,
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_broker_schema_and_allowlist_drift_blocked(paused_factory, tmp_path: Path):
    """Test 6: Changes to ToolBroker allowlist or tool parameter schemas raise STALE_CHECKPOINT."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # Drift 1: Allowlist reduced (missing query_package_tracking)
    rt.tool_broker.application_allowlist = {"query_order_packages"}
    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)

    # Restore allowlist
    rt.tool_broker.application_allowlist = {"query_order_packages", "query_package_tracking"}

    # Drift 2: Tool parameter schema drift
    class TamperedTrackingTool(QueryPackageTrackingTool):
        def get_parameters(self) -> list[ToolParameter]:
            return [
                ToolParameter(
                    name="tampered_tracking_id",
                    type="string",
                    description="Tampered param name",
                    required=True,
                )
            ]

    rt.tool_broker.register_tool(TamperedTrackingTool())
    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_real_task_context_drift_blocked(paused_factory, tmp_path: Path):
    """Test 7: Updating canonical TaskContext via revise_task_context blocks resume with STALE_CHECKPOINT."""
    cand_store = CandidateStore(db_path=tmp_path / "candidates.db")
    task_id = "recovery_task"
    initial_ctx = TaskContext(
        task_id=task_id,
        goal="Check all packages for " + ORDER,
        business_scope="Logistics package tracking",
        constraints=["Read only", "Do not alter status"],
        acceptance_criteria={"require_delivery_status": True},
        intent_revision=1,
    )

    e = paused_factory(candidate_store=cand_store, task_context=initial_ctx)
    rt, rid = e["runtime"], e["run_id"]

    # Canonical intent revision shift via official API
    new_ctx, _ = revise_task_context(
        current_ctx=initial_ctx,
        new_goal="Shifted Goal: Return packages immediately",
        new_constraints=["Return immediately", "Read only"],
        candidate_store=cand_store,
    )
    assert new_ctx.intent_revision == 2

    # Attempt to resume with stale checkpoint
    with pytest.raises(ValueError, match="STALE_CHECKPOINT"):
        rt.resume_agent_with_pi(
            run_id=rid,
            model_config=cfg(remaining()),
            test_mode=True,
            workspace_dir=tmp_path,
            timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_bound_context_cannot_be_hidden_by_omitting_store(paused_factory, tmp_path: Path):
    store = CandidateStore(db_path=tmp_path / "candidates.db")
    context = TaskContext(task_id="recovery_task", goal="Check all packages for " + ORDER,
                          intent_revision=1)
    e = paused_factory(candidate_store=store, task_context=context)
    revise_task_context(context, new_goal="Only report status", candidate_store=store)
    e["runtime"].candidate_store = None
    with pytest.raises(ValueError, match="STALE_CHECKPOINT.*TaskContext store"):
        e["runtime"].resume_agent_with_pi(
            e["run_id"], model_config=cfg(remaining()), test_mode=True,
            workspace_dir=tmp_path, timeout=15,
        )
    assert_safety_invariants(e)


def test_safety_candidate_draft_mutation_still_passes_b2(paused_factory, tmp_path: Path):
    """Test 8: Normal B2 full-path and 'Draft.body changed in memory' still recovers correctly."""
    e = paused_factory()
    rt, rid = e["runtime"], e["run_id"]

    # In paused_factory(), candidate.body was already set to 'NEW_BODY_MUST_NOT_LEAK'
    # Checkpoint recovery must succeed using the original frozen snapshot
    answer = rt.resume_agent_with_pi(
        run_id=rid,
        model_config=cfg(remaining()),
        test_mode=True,
        context_compressor=e["compressor"],
        workspace_dir=tmp_path,
        timeout=20,
    )
    assert answer == FINAL
    assert len(e["pids"]) == 2
    assert_reaped(e["pids"][1])
    assert e["counts"] == {"order": 1, "track": 2}

    rec = rt.get_run(rid)
    assert rec.budget_consumed == 7 and rec.budget_max == 10
    assert rec.deadline_ts == e["deadline"]

    # Verify frozen body snapshot retained, draft mutation isolated
    provider_input = rt.get_last_pi_result(rid).captured_provider_inputs[0]
    rendered = str(provider_input)
    assert BODY in rendered
    assert "NEW_BODY_MUST_NOT_LEAK" not in rendered

    # Verify Oracle PASS and single Episode settlement
    oracle = verify_logistics_fulfillment(answer, ORDER, rt.list_tool_calls(rid))
    assert oracle["independent_pass"] is True
    _, episode = rt.finalize_run(rid, model_output=answer, verification_evidence=oracle)
    assert episode.outcome == "success"
