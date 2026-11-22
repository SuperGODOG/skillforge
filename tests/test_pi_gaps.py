"""Focused test suite for Gap A (reproducible deps & provider) and Gap B (authoritative status & budget).

Strictly verifies:
- Gap A:
  1. Minimum Node.js version (>=22.0.0) validation and PI_SOURCE_ROOT validation.
  2. Production mode strictly rejects faux provider and pre-written response scripts.
  3. Explicit normal Pi provider/model configuration entry point initializes native agent.
  4. Missing usage is strictly None/null; never falsified as {input: 0, output: 0}.
- Gap B:
  5. Small budget test: shared step/turn budget exhaustion stops handlers from continuing.
  6. Subprocess exception / abnormal exit settles as FAILED in runtime and never produces success Episode.
  7. Cancelled / aborted run settles as CANCELLED in runtime and never produces success Episode.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import EpisodeStore
from skillforge.pi_adapter import (
    MINIMUM_NODE_MAJOR,
    PiExecutionResult,
    PiRunnerClient,
    tool_parameters_to_json_schema,
    validate_node_environment,
)
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)


@pytest.fixture
def gap_env(tmp_path: Path):
    db_path = tmp_path / "test_runtime.db"
    episodes_db = tmp_path / "episodes.db"

    episode_store = EpisodeStore(db_path=episodes_db)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
    )

    yield {
        "runtime": runtime,
        "broker": broker,
        "collector": collector,
        "episode_store": episode_store,
        "tmp_path": tmp_path,
    }

    runtime.close()


def test_gap_a1_node_version_and_source_root_validation(monkeypatch, tmp_path):
    """(Gap A) Verify Node.js version >= 22.0.0 requirement and PI_SOURCE_ROOT validation."""
    # 1. Valid host node binary passes
    validate_node_environment("node")
    assert MINIMUM_NODE_MAJOR == 22

    # 2. Invalid PI_SOURCE_ROOT pointing to empty dir fails validation
    invalid_root = tmp_path / "invalid_pi_source"
    invalid_root.mkdir()
    monkeypatch.setenv("PI_SOURCE_ROOT", str(invalid_root))
    with pytest.raises(ValueError) as exc:
        validate_node_environment("node")
    assert "package.json" in str(exc.value)

    # 3. Valid PI_SOURCE_ROOT containing package.json passes
    (invalid_root / "package.json").write_text('{"name": "pi-monorepo", "version": "0.85.1"}')
    validate_node_environment("node")


def test_gap_a2_production_rejects_faux_and_scripts_without_test_mode(gap_env):
    """(Gap A) Production cannot default to faux or accept response scripts; faux requires explicit test mode."""
    runtime: AgentRuntime = gap_env["runtime"]
    run_id = "run_prod_faux_rejected"
    runtime.start_run(run_id=run_id, task_id="task_1", purpose="evaluation")

    # 1. Calling with faux in production mode (test_mode=False) is rejected
    with pytest.raises(ValueError) as exc1:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Hi",
            model_config={"provider": "faux"},
            test_mode=False,
        )
    assert "Faux provider is restricted to explicit test mode" in str(exc1.value)

    # 2. Calling with scripted responses in production mode is rejected
    with pytest.raises(ValueError) as exc2:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Hi",
            model_config={"provider": "anthropic", "model_id": "claude-haiku-4-5", "responses": ["hi"]},
            test_mode=False,
        )
    assert "Pre-written response scripts are restricted" in str(exc2.value)


def test_gap_a3_native_pi_provider_entrypoint_initialization():
    """(Gap A) Normal Pi provider/model configuration entrypoint reuses native Pi provider catalog."""
    async def _test():
        client = PiRunnerClient()
        await client.start()
        run_id = "run_native_provider_init"

        # Explicit normal Pi native provider and model configuration
        model_config = {
            "provider": "anthropic",
            "model_id": "claude-haiku-4-5",
        }

        # Runner initializes native Agent without error, without mock HTTP client
        await client.init_run(
            run_id=run_id,
            system_prompt="Test system prompt",
            tools=[],
            model_config=model_config,
            is_test=False,
        )
        assert client.is_running is True
        await client.close()

    asyncio.run(_test())


def test_gap_a4_missing_usage_reports_null_not_pseudo_zero(gap_env):
    """(Gap A) When usage is not reported by model, usage is None/null; never faked as 0."""
    async def _test():
        client = PiRunnerClient()
        await client.start()
        run_id = "run_usage_null_check"

        model_config = {
            "provider": "faux",
            "responses": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello world"}],
                    "stopReason": "stop",
                    # usage deliberately omitted
                }
            ],
        }

        await client.init_run(
            run_id=run_id,
            system_prompt="System prompt",
            tools=[],
            model_config=model_config,
            is_test=True,
        )

        res: PiExecutionResult = await client.prompt(run_id=run_id, text="Hello")
        assert res.text == "Hello world"

        # usage must be None, NOT {"input": 0, "output": 0}
        assert res.usage is None
        assert res.raw_response.get("usage") is None

        await client.close()

    asyncio.run(_test())


def test_gap_b1_shared_budget_exhaustion_stops_subsequent_handlers(gap_env):
    """(Gap B) Small budget test: shared step/turn budget exhaustion stops handlers from continuing."""
    runtime: AgentRuntime = gap_env["runtime"]
    broker: ToolBroker = gap_env["broker"]

    order_tool = QueryOrderPackagesTool()
    pkg_tool = QueryPackageTrackingTool()
    broker.register_tool(order_tool)
    broker.register_tool(pkg_tool)

    run_id = "run_budget_exhaust_stops_handler"
    task_id = "task_budget_01"

    # Strict small budget: max 2 tickets total shared between model turns and tool calls
    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=2,
    )

    handler_count = 0
    orig_order_run = order_tool.run
    orig_pkg_run = pkg_tool.run

    def tracked_order_run(params):
        nonlocal handler_count
        handler_count += 1
        return orig_order_run(params)

    def tracked_pkg_run(params):
        nonlocal handler_count
        handler_count += 1
        return orig_pkg_run(params)

    order_tool.run = tracked_order_run
    pkg_tool.run = tracked_pkg_run

    # Model attempts 2 tool calls in sequence
    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_b1",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_b2",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "stopReason": "stop",
            },
        ],
    }

    try:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Track order ORD_2026_0901",
            tools=["query_order_packages", "query_package_tracking"],
            model_config=model_config,
            test_mode=True,
            timeout=10.0,
        )
    except Exception:
        pass

    # Verification of authoritative budget & handler stop:
    # Turn 1 took ticket 1 -> Tool 1 took ticket 2 (handler ran, count=1).
    # Budget was exhausted at ticket 2.
    # Subsequent action was denied, and handler was NEVER called a 2nd time!
    assert handler_count == 1

    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status == "BUDGET_EXHAUSTED"
    assert run.budget_consumed >= 2
    # Denied model turns are not reported as actual provider requests.
    result = runtime.get_last_pi_result(run_id)
    assert result is not None
    assert len(result.captured_provider_inputs) == 1


def test_gap_b2_subprocess_failure_settles_and_never_success_episode(gap_env):
    """(Gap B) Exception / abnormal termination settles as FAILED and NEVER creates success Episode."""
    runtime: AgentRuntime = gap_env["runtime"]
    collector: ExperienceCollector = gap_env["collector"]

    run_id = "run_fail_settles_collector"
    task_id = "task_fail_01"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")
    collector.start_run(run_id=run_id, task_id=task_id, skill_name="test_skill")

    # Trigger error frame from runner via unsupported provider
    model_config = {
        "provider": "non_existent_unsupported_provider_xyz",
    }

    with pytest.raises(Exception):
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Hello",
            model_config=model_config,
            test_mode=True,
            timeout=5.0,
        )

    # Authoritative settlement in SQLite runtime_runs
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status in ("FAILED", "TIMED_OUT")
    assert run.terminal_at is not None

    # Authoritative settlement in ExperienceCollector
    episode_store: EpisodeStore = gap_env["episode_store"]
    episode = episode_store.get_episode(f"ep_{run_id}")
    assert episode is not None
    assert episode.outcome != "success"  # MUST NEVER be success!
    assert episode.outcome in ("failure", "unknown")


def test_gap_b3_aborted_run_settles_cancelled_and_never_success_episode(gap_env):
    """(Gap B) Abort/cancellation settles as CANCELLED and NEVER creates success Episode."""
    runtime: AgentRuntime = gap_env["runtime"]
    broker: ToolBroker = gap_env["broker"]
    collector: ExperienceCollector = gap_env["collector"]

    pkg_tool = QueryPackageTrackingTool()
    broker.register_tool(pkg_tool)

    run_id = "run_cancel_settles_collector"
    task_id = "task_cancel_02"

    runtime.start_run(run_id=run_id, task_id=task_id, purpose="evaluation")
    collector.start_run(run_id=run_id, task_id=task_id, skill_name="test_skill")

    # Hook inside tool execution that cancels the run
    orig_pkg_run = pkg_tool.run

    def cancelling_pkg_run(params):
        runtime.cancel_run(run_id, reason="User triggered mid-run abort")
        return orig_pkg_run(params)

    pkg_tool.run = cancelling_pkg_run

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_c1",
                        "name": "query_package_tracking",
                        "arguments": {"package_id": "PKG_101"},
                    }
                ],
                "stopReason": "toolUse",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Post-cancel text"}],
                "stopReason": "stop",
            },
        ],
    }

    try:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Track package PKG_101",
            tools=["query_package_tracking"],
            model_config=model_config,
            test_mode=True,
            timeout=5.0,
        )
    except (Exception, asyncio.CancelledError):
        pass

    # Status must be CANCELLED in runtime
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status == "CANCELLED"
    assert run.terminal_at is not None

    # Collector episode must NOT be success
    episode_store: EpisodeStore = gap_env["episode_store"]
    episode = episode_store.get_episode(f"ep_{run_id}")
    assert episode is not None
    assert episode.outcome != "success"


def test_gap_b4_real_deadline_expiration_stops_stream_and_handler_settles_timed_out(gap_env):
    """(Gap B) When deadline is expired, consume_turn_budget stops stream & handler, settles TIMED_OUT and non-success episode."""
    runtime: AgentRuntime = gap_env["runtime"]
    broker: ToolBroker = gap_env["broker"]
    collector: ExperienceCollector = gap_env["collector"]

    order_tool = QueryOrderPackagesTool()
    broker.register_tool(order_tool)

    run_id = "run_deadline_expired_stops_all"
    task_id = "task_deadline_01"

    # Start run with an already-expired deadline (in the past)
    past_deadline = time.time() - 5.0
    runtime.start_run(
        run_id=run_id,
        task_id=task_id,
        purpose="evaluation",
        budget_max=10,
        deadline_ts=past_deadline,
    )
    collector.start_run(run_id=run_id, task_id=task_id, skill_name="test_skill")

    handler_called = False
    orig_order_run = order_tool.run

    def tracked_run(params):
        nonlocal handler_called
        handler_called = True
        return orig_order_run(params)

    order_tool.run = tracked_run

    model_config = {
        "provider": "faux",
        "responses": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tc_dl_1",
                        "name": "query_order_packages",
                        "arguments": {"order_id": "ORD_2026_0901"},
                    }
                ],
                "stopReason": "toolUse",
            }
        ],
    }

    with pytest.raises(Exception) as exc_info:
        runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="Track order ORD_2026_0901",
            tools=["query_order_packages"],
            model_config=model_config,
            test_mode=True,
            timeout=5.0,
        )

    err_str = str(exc_info.value).lower()
    assert "deadline" in err_str or "timed_out" in err_str or "timeout" in err_str

    # Handler must NOT have been called
    assert handler_called is False

    # Status must be TIMED_OUT in runtime
    run = runtime.get_run(run_id)
    assert run is not None
    assert run.status == "TIMED_OUT"
    assert run.terminal_at is not None

    # Collector episode must NOT be success
    episode_store: EpisodeStore = gap_env["episode_store"]
    episode = episode_store.get_episode(f"ep_{run_id}")
    assert episode is not None
    assert episode.outcome != "success"

