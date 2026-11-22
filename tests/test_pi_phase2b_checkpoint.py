"""Phase 2B B1: Pi Checkpoint & Cooperative Pause Integration Test Suite.

Verifies:
1. Real npm Pi + faux provider + actual Broker tool cooperative pause at next context checkpoint.
   - Completed toolCall and toolResult are preserved in checkpoint messages.
   - Tool executed strictly 1 time.
   - captured_provider_inputs has only 1 item (no 2nd model invocation).
   - Run status is PAUSED (terminal_at is NULL).
   - ExperienceCollector run context is not completed, no final Episode generated.
   - Pi runner process PID is cleanly reclaimed.
   - New AgentRuntime opening the same DB reads checkpoint via get_pi_checkpoint:
     matches frozen_body, messages, epoch > 1, working_scratchpad, budget_consumed == 2,
     and identical original deadline_ts.
2. request_pause_run repeated calls are idempotent.
3. request_pause_run explicitly rejects non-existent and terminal runs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge.collector import ExperienceCollector
from skillforge.context_engine.compressor import ContextCompressor
from skillforge.episode import EpisodeStore
from skillforge.models import CandidateSkill, SkillMeta
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import QueryOrderPackagesTool


def test_phase2b_b1_checkpoint_on_cooperative_pause(tmp_path: Path):
    """Scenario 1: Main cooperative pause flow at context boundary with real compression and checkpoint."""
    db_path = tmp_path / "test_runtime.db"
    episodes_db = tmp_path / "episodes.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    episode_store = EpisodeStore(db_path=episodes_db)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
    )

    run_id = "run_p2b_b1_pause_001"
    task_id = "task_p2b_b1_001"
    deadline = time.time() + 120.0

    # 1. Authoritative candidate and frozen body snapshot
    frozen_rule_content = "AUTHORITATIVE_FROZEN_LOGISTICS_TASK_RULE_V1"
    candidate = CandidateSkill(
        candidate_id="cand_b1_001",
        skill_name="package_tracker",
        decision="create",
        source_requirement="Track order packages under cooperative pause",
        body=frozen_rule_content,
        meta=SkillMeta(
            name="package_tracker",
            version="0.1.0",
            description="Logistics package tracking skill",
            use_when="Querying order packages",
            dependencies=["query_order_packages"],
        ),
    )

    # 2. Broker tool wrapper that counts execution and requests pause upon completion
    tool_call_count = 0

    class PausingOrderTool(QueryOrderPackagesTool):
        def run(self, *args, **kwargs) -> ToolResponse:
            nonlocal tool_call_count
            tool_call_count += 1
            res = super().run(*args, **kwargs)
            # Cooperatively request pause for this active run
            pause_accepted = runtime.request_pause_run(run_id)
            assert pause_accepted is True
            return res

    broker.register_tool(PausingOrderTool())

    # 3. Start run binding candidate and collector
    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        candidate=candidate,
        purpose="evaluation",
        budget_max=10,
        deadline_ts=deadline,
    )
    collector.start_run(
        run_id=run_id,
        task_id=task_id,
        skill_name=candidate.skill_name,
        skill_version="0.1.0",
        environment={"purpose": "evaluation"},
    )

    # 4. ContextCompressor configured to trigger real compression (epoch 1 -> 2)
    # on order packages output (>200 chars)
    compressor = ContextCompressor(
        trigger_watermark_tokens=20,
        target_watermark_tokens=10,
        hard_capacity_tokens=5000,
        offload_threshold_chars=200,
        artifacts_dir=tmp_path / "artifacts",
    )

    # 5. Faux provider: Turn 1 calls tool; Turn 2 should never execute due to pause
    model_config = {
        "provider": "faux",
        "model_id": "faux-unit-test-model",
        "test_mode": True,
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_b1_packages_01",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_DEV_0102"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "UNEXPECTED_TURN_2_SHOULD_BE_PREVENTED"}],
                "stopReason": "stop",
            },
        ],
    }

    # 6. Execute agent with Pi
    final_text = runtime.run_agent_with_pi(
        run_id=run_id,
        input_text="Track order packages for ORD_DEV_0102",
        tools=["query_order_packages"],
        model_config=model_config,
        test_mode=True,
        context_compressor=compressor,
        workspace_dir=tmp_path,
        timeout=15.0,
    )

    # 7. Verification of live execution outcome
    # (a) Tool executed strictly 1 time
    assert tool_call_count == 1

    # (b) Pi result stop_reason is "paused"
    last_res = runtime.get_last_pi_result(run_id)
    assert last_res is not None
    assert last_res.stop_reason == "paused"

    # (c) Captured provider inputs has only 1 item (no Turn 2 model call)
    assert len(last_res.captured_provider_inputs) == 1

    # (d) Run status in DB is PAUSED, terminal_at is None
    run_rec = runtime.get_run(run_id)
    assert run_rec is not None
    assert run_rec.status == "PAUSED"
    assert run_rec.terminal_at is None

    # (e) Collector run context is not completed, no final Episode generated
    assert run_id in collector._runs
    collector_ctx = collector._runs[run_id]
    assert not getattr(collector_ctx, "completed", False)
    assert episode_store.get_episode(f"ep_{run_id}") is None

    # (f) Old Pi client subprocess is cleanly reaped and closed
    assert run_id not in runtime._active_pi_clients

    runtime.close()

    # 8. Re-open database with a brand-new AgentRuntime instance and load checkpoint
    new_runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
    )
    try:
        chk = new_runtime.get_pi_checkpoint(run_id)
        assert chk is not None
        assert chk.run_id == run_id
        assert chk.format_version == 1

        # Frozen body snapshot matches initial candidate
        assert chk.frozen_body == frozen_rule_content

        # Epoch > 1 due to actual compression before checkpoint
        assert chk.epoch > 1

        # Completed toolCall and toolResult are preserved in messages
        tc_found = False
        tr_found = False
        for m in chk.messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls", []):
                    if tc.get("id") == "tc_b1_packages_01":
                        tc_found = True
            elif m.get("role") in ("tool", "toolResult"):
                if m.get("tool_call_id") == "tc_b1_packages_01" or m.get("toolCallId") == "tc_b1_packages_01":
                    tr_found = True
        assert tc_found, "tc_b1_packages_01 missing from checkpoint messages"
        assert tr_found, "toolResult for tc_b1_packages_01 missing from checkpoint messages"

        # Working scratchpad preserved
        assert "working_scratchpad" in chk.working_state
        assert "protected_state" in chk.working_state["working_scratchpad"]

        # Budget consumed strictly 2 (1 model turn + 1 tool execution; Turn 2 was prevented)
        assert chk.budget_consumed == 2

        # Original deadline preserved
        assert chk.deadline_ts is not None
        assert chk.deadline_ts == deadline

        # Run status is PAUSED
        assert chk.run_status == "PAUSED"

        # Model config whitelist preserved without secret leakage
        assert chk.model_config.get("provider") == "faux"
        assert chk.model_config.get("model_id") == "faux-unit-test-model"
        assert "responses" not in chk.model_config

        # Tools list preserved
        assert "query_order_packages" in chk.tools
    finally:
        new_runtime.close()


def test_phase2b_b1_request_pause_repeated_is_idempotent(tmp_path: Path):
    """Scenario 2: request_pause_run is idempotent for active runs."""
    db_path = tmp_path / "test_runtime.db"
    runtime = AgentRuntime(db_path=db_path)
    try:
        run_id = "run_p2b_idempotent_001"
        runtime.start_run(
            run_id=run_id,
            task_id="task_idempotent",
            purpose="evaluation",
            budget_max=5,
        )

        # First request adds flag
        assert runtime.request_pause_run(run_id) is True
        assert run_id in runtime._pause_requested_runs

        # Repeated request succeeds idempotently
        assert runtime.request_pause_run(run_id) is True
        assert len(runtime._pause_requested_runs) == 1
    finally:
        runtime.close()


def test_phase2b_b1_request_pause_rejects_nonexistent_and_terminal(tmp_path: Path):
    """Scenario 3: request_pause_run explicitly rejects non-existent and terminal runs."""
    db_path = tmp_path / "test_runtime.db"
    runtime = AgentRuntime(db_path=db_path)
    try:
        # Non-existent run rejected with KeyError
        with pytest.raises(KeyError, match="not found"):
            runtime.request_pause_run("non_existent_run_9999")

        # Create run and transition to terminal CANCELLED
        term_run_id = "run_p2b_term_001"
        runtime.start_run(
            run_id=term_run_id,
            task_id="task_term",
            purpose="evaluation",
            budget_max=5,
        )
        runtime.cancel_run(term_run_id, reason="Test termination")

        # Terminal run rejected with ValueError
        with pytest.raises(ValueError, match="terminal"):
            runtime.request_pause_run(term_run_id)
    finally:
        runtime.close()
