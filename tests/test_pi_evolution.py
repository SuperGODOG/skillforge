"""Phase 3B1: End-to-end Pi Failure Learning Episode to Real RepairJob Validation.

Verifies:
1. Actual Pi failure learning Episode created via real AgentRuntime & faux provider:
   - Logistics fulfillment oracle marks independent_pass is False.
   - Immutable Episode produced with purpose='learning', outcome='failure', and 2 real tool provenances.
   - EpisodeStore retrieval matches canonical episode.
2. Real RepairJob execution with PiEvaluationBackend co-evaluation:
   - ScriptedPatcher receives prompt containing actual outcome_reason.
   - Bounded repair loop evaluates candidate against baseline using real Pi execution backend.
   - ValidationRecord passes ratchet gate, resulting in RepairJob status READY.
   - Latest candidate content_hash is bound and candidate saved in CandidateStore.
   - Active formal registry baseline remains intact (version 1.0.0).
   - Evaluation child episodes have purpose='evaluation' and outcome='unknown' with real tool calls.
3. Purpose Isolation invariant:
   - Evaluation episodes from backend.run cannot trigger repair_skill_failure (raises ValueError, 0 patcher calls).
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

# Ensure tests directory is on sys.path for sibling imports
_tests_dir = str(Path(__file__).resolve().parent)
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import pytest

from test_pi_evaluation import FakeLLM, eval_env
from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, Episode, EpisodeStore
from skillforge.evaluator import SkillEvaluator
from skillforge.evolution_loop import compute_candidate_hash
from skillforge.pi_evaluation import PiEvaluationBackend
from skillforge.registry import SkillRegistry
from skillforge.repair import RepairJob, repair_skill_failure, promote_repaired_skill
from skillforge.runtime import AgentRuntime
from skillforge.scenarios.logistics import verify_logistics_fulfillment
from skillforge.state_machine import ReleaseStateMachine


def make_logistics_responses(order_id: str) -> list[dict[str, Any]]:
    """15-line response function: order tool, track 101, track 102, correct text."""
    final_text = (
        f"Order {order_id} has 2 packages: PKG_101 is DELIVERED via Carrier_Alpha, "
        f"and PKG_102 is IN_TRANSIT via Carrier_Beta."
    )
    return [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "tc_order", "name": "query_order_packages", "arguments": {"order_id": order_id}}],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "tc_pkg_101", "name": "query_package_tracking", "arguments": {"package_id": "PKG_101"}}],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "tc_pkg_102", "name": "query_package_tracking", "arguments": {"package_id": "PKG_102"}}],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": final_text}],
            "stopReason": "stop",
        },
    ]


class ScriptedPatcher:
    """Minimal scripted patcher returning full SKILL.md for version 1.0.1."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[str] = []

    def invoke(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(str(prompt))
        return SimpleNamespace(content=self.content, usage=None)


@pytest.fixture
def pi_repair_env(eval_env):
    """Fixture orchestrating actual Pi learning failure, backend setup, and RepairJob execution."""
    runtime: AgentRuntime = eval_env["runtime"]
    registry: SkillRegistry = eval_env["registry"]
    episode_store: EpisodeStore = eval_env["episode_store"]
    cand_store: CandidateStore = eval_env["cand_store"]
    tmp_path: Path = eval_env["tmp_path"]

    order_id = "ORD_2026_0901"
    query = f"Query order {order_id} logistics packages"

    # Step 1: Write temporary baseline SKILL.md (v1.0.0, body 200-500 chars)
    baseline_body = (
        "# 1. Overview\n"
        "Baseline logistics query instructions for orders and packages.\n"
        "## Constraints\n"
        "- Only query status, do not modify.\n"
        "# 2. Instructions\n"
        "Query order packages and query package tracking status.\n"
        "# 3. Examples\n"
        "Example 1: query order and package status."
    )
    baseline_skill_dir = tmp_path / "skills" / "logistics_query"
    baseline_skill_dir.mkdir(parents=True, exist_ok=True)
    baseline_skill_file = baseline_skill_dir / "SKILL.md"
    baseline_skill_file.write_text(
        f"""---
name: logistics_query
version: 1.0.0
description: 物流包裹状态查询工具
use_when: 用户查询订单物流状态时使用
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords:
    - 物流
    - 订单
    - order
    - package
  not_for:
    - 退款
not_for:
  - 退款
---
{baseline_body}
""",
        encoding="utf-8",
    )
    registry.load_skills_from_dir()

    # Step 2: Run actual Pi failure learning episode
    runtime.start_run(
        run_id="pi_learning_failure",
        task_id="task_pi_failure",
        skill_name="logistics_query",
        purpose="learning",
        budget_max=10,
        task_description=query,
    )
    failure_responses = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_fail_order",
                    "name": "query_order_packages",
                    "arguments": {"order_id": order_id},
                }
            ],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_fail_track101",
                    "name": "query_package_tracking",
                    "arguments": {"package_id": "PKG_101"},
                }
            ],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"Order {order_id}: all packages delivered.",
                }
            ],
            "stopReason": "stop",
        },
    ]
    output = runtime.run_agent_with_pi(
        run_id="pi_learning_failure",
        input_text=query,
        tools=["query_order_packages", "query_package_tracking"],
        test_mode=True,
        model_config={"provider": "faux", "responses": failure_responses},
    )
    tool_records = runtime.list_tool_calls("pi_learning_failure")
    oracle = verify_logistics_fulfillment(
        model_output=output,
        order_id=order_id,
        tool_records=tool_records,
    )
    assert oracle["independent_pass"] is False

    run_rec, canonical_ep = runtime.finalize_run(
        run_id="pi_learning_failure",
        model_output=output,
        verification_evidence=oracle,
    )
    assert canonical_ep is not None
    assert canonical_ep.outcome == "failure"
    assert canonical_ep.environment.get("purpose") == "learning"
    assert "INV_5_PERMISSION_QUALIFIED_REJECTION" in canonical_ep.verification_evidence["checked_invariants"]
    assert len(canonical_ep.provenances) == 2

    # EpisodeStore comparison
    stored_ep = episode_store.get_episode(canonical_ep.episode_id)
    assert stored_ep is not None
    assert stored_ep.episode_id == canonical_ep.episode_id
    assert stored_ep.outcome == "failure"
    assert stored_ep.environment.get("purpose") == "learning"

    # Step 3: Construct PiEvaluationBackend & SkillEvaluator
    correct_responses = make_logistics_responses(order_id)
    final_text = correct_responses[-1]["content"][0]["text"]
    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": correct_responses},
        test_mode=True,
        budget_max=10,
    )
    evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(role="execution"),
        judge_llm=FakeLLM(role="judge"),
        execution_backend=backend,
    )
    case = {
        "id": "pi_pkg",
        "query": query,
        "oracle": "logistics",
        "order_id": order_id,
        "reference": final_text,
        "skill": "logistics_query",
    }

    # Step 4: ScriptedPatcher with v1.0.1 content (with full package verification rules)
    patched_body = (
        "# 1. Overview\n"
        "Baseline logistics query instructions for orders and packages.\n"
        "## Constraints\n"
        "- Only query status, do not modify.\n"
        "- Never omit any package; verify all packages returned by order query.\n"
        "# 2. Instructions\n"
        "Query order packages and sequentially query tracking for all returned packages without omission.\n"
        "# 3. Examples\n"
        "Example 1: query order and package status."
    )
    patched_skill_text = f"""---
name: logistics_query
version: 1.0.1
description: 物流包裹状态查询工具
use_when: 用户查询订单物流状态时使用
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords:
    - 物流
    - 订单
    - order
    - package
  not_for:
    - 退款
not_for:
  - 退款
---
{patched_body}
"""
    patcher = ScriptedPatcher(content=patched_skill_text)

    # Step 5: Execute repair_skill_failure without shadow recovery
    job = repair_skill_failure(
        episodes=[episode_store.get_episode(canonical_ep.episode_id)],
        skill_name="logistics_query",
        episode_store=episode_store,
        candidate_store=cand_store,
        registry=registry,
        evaluator=evaluator,
        eval_cases=[case],
        llm=patcher,
        max_attempts=1,
    )

    yield {
        "job": job,
        "patcher": patcher,
        "evaluator": evaluator,
        "backend": backend,
        "eval_env": eval_env,
        "canonical_ep": canonical_ep,
        "case": case,
        "baseline_body": baseline_body,
        "patched_body": patched_body,
        "order_id": order_id,
    }


def test_pi_failure_to_repair_candidate(pi_repair_env):
    """Test actual Pi failure learning episode -> real RepairJob -> Pi validation -> Candidate."""
    job: RepairJob = pi_repair_env["job"]
    patcher: ScriptedPatcher = pi_repair_env["patcher"]
    eval_env = pi_repair_env["eval_env"]
    canonical_ep: Episode = pi_repair_env["canonical_ep"]
    baseline_body: str = pi_repair_env["baseline_body"]

    registry: SkillRegistry = eval_env["registry"]
    cand_store: CandidateStore = eval_env["cand_store"]
    episode_store: EpisodeStore = eval_env["episode_store"]

    # Assert RepairJob completed with status READY and validation PASS
    assert job.status == "READY"
    assert job.diagnosis.responsibility_layer == "skill"
    from skillforge.bounded_recovery import check_non_recoverable_blockers
    assert check_non_recoverable_blockers(episodes=[canonical_ep])[0] is False
    assert len(job.attempts) == 1
    attempt = job.attempts[0]
    assert attempt.validation_decision == "PASS"

    # Assert Candidate generated and content_hash bound
    candidate = job.latest_candidate
    assert candidate is not None
    assert candidate.meta.version == "1.0.1"
    expected_hash = compute_candidate_hash(candidate)
    assert job.latest_content_hash == expected_hash
    stored_cand = cand_store.get_candidate(candidate.candidate_id)
    assert stored_cand is not None

    # Assert formal registry baseline remains intact (version 1.0.0)
    meta = registry.get_meta("logistics_query")
    assert meta.version == "1.0.0"
    current_body = registry.get_body("logistics_query") if hasattr(registry, "get_body") else registry._bodies.get("logistics_query")
    assert current_body == baseline_body

    # Assert patcher invocation: called exactly once and actual ep.outcome_reason is present in prompt
    assert len(patcher.calls) == 1
    assert canonical_ep.outcome_reason in patcher.calls[0]

    # Assert evaluation child episodes: purpose='evaluation', outcome='unknown', with actual Pi tool calls
    all_episodes = episode_store.list_episodes()
    eval_episodes = [
        ep for ep in all_episodes
        if isinstance(ep.environment, dict) and ep.environment.get("purpose") == "evaluation"
    ]
    assert len(eval_episodes) >= 1
    for ep in eval_episodes:
        assert ep.outcome == "unknown"
        assert ep.environment.get("purpose") == "evaluation"
        tool_names = [p.tool_name for p in ep.provenances]
        assert "query_order_packages" in tool_names
        assert "query_package_tracking" in tool_names


def test_pi_evaluation_episode_cannot_be_repaired(pi_repair_env):
    """Test purpose isolation: evaluation episodes from backend.run cannot trigger repair_skill_failure."""
    eval_env = pi_repair_env["eval_env"]
    episode_store: EpisodeStore = eval_env["episode_store"]
    cand_store: CandidateStore = eval_env["cand_store"]
    registry: SkillRegistry = eval_env["registry"]
    evaluator: SkillEvaluator = pi_repair_env["evaluator"]
    case = pi_repair_env["case"]

    all_episodes = episode_store.list_episodes()
    eval_ep = next(
        (ep for ep in all_episodes if isinstance(ep.environment, dict) and ep.environment.get("purpose") == "evaluation"),
        None,
    )
    assert eval_ep is not None
    assert eval_ep.outcome == "unknown"

    new_patcher = ScriptedPatcher(content="dummy")
    with pytest.raises(ValueError) as exc_info:
        repair_skill_failure(
            episodes=[eval_ep],
            skill_name="logistics_query",
            episode_store=episode_store,
            candidate_store=cand_store,
            registry=registry,
            evaluator=evaluator,
            eval_cases=[case],
            llm=new_patcher,
            max_attempts=1,
        )

    # Must fail because outcome is 'unknown' or purpose is not 'learning'
    err_msg = str(exc_info.value)
    assert "only failed episodes may trigger repair" in err_msg or "No valid learning episodes found" in err_msg
    # Ensure new patcher was never called
    assert len(new_patcher.calls) == 0


def test_pi_explicit_promotion_and_future_reuse(pi_repair_env):
    """Phase 3B2: Explicit human promotion of repair candidate and zero-config future reuse."""
    job: RepairJob = pi_repair_env["job"]
    eval_env = pi_repair_env["eval_env"]
    registry: SkillRegistry = eval_env["registry"]
    cand_store: CandidateStore = eval_env["cand_store"]
    evaluator: SkillEvaluator = pi_repair_env["evaluator"]
    baseline_body: str = pi_repair_env["baseline_body"]
    patched_body: str = pi_repair_env["patched_body"]
    order_id: str = pi_repair_env["order_id"]
    query = f"Query order {order_id} logistics packages"
    tmp_path: Path = eval_env["tmp_path"]

    # Step 1: Initialize temporary Git repository in tmp_path
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "skills"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture baseline",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Step 2: ReleaseStateMachine and caller_confirmed=False gate check
    sm = ReleaseStateMachine(db_path=registry.db_path, repo_root=tmp_path)
    with pytest.raises(ValueError) as exc_info:
        promote_repaired_skill(
            job=job,
            candidate_store=cand_store,
            registry=registry,
            state_machine=sm,
            caller_confirmed=False,
            evaluator=evaluator,
        )
    assert "caller_confirmed" in str(exc_info.value)

    # Assert baseline 1.0.0 and body remain unchanged
    meta = registry.get_meta("logistics_query")
    assert meta.version == "1.0.0"
    current_body = (
        registry.get_body("logistics_query")
        if hasattr(registry, "get_body")
        else registry._bodies.get("logistics_query")
    )
    assert current_body == baseline_body

    # Assert stored validation record promoted is False
    stored_rec = cand_store.get_validation_record(job.latest_candidate.candidate_id)
    assert stored_rec is not None
    assert stored_rec.promoted is False

    # Step 3: Explicit promotion with caller_confirmed=True
    release = promote_repaired_skill(
        job=job,
        candidate_store=cand_store,
        registry=registry,
        state_machine=sm,
        caller_confirmed=True,
        evaluator=evaluator,
    )
    assert release is not None
    assert release.version == "1.0.1"

    # Assert registry updated to 1.0.1 and patched_body
    meta_promoted = registry.get_meta("logistics_query")
    assert meta_promoted.version == "1.0.1"
    promoted_body = (
        registry.get_body("logistics_query")
        if hasattr(registry, "get_body")
        else registry._bodies.get("logistics_query")
    )
    assert promoted_body == patched_body

    # Assert stored record promoted is True and release_id matches
    stored_rec_promoted = cand_store.get_validation_record(job.latest_candidate.candidate_id)
    assert stored_rec_promoted is not None
    assert stored_rec_promoted.promoted is True
    assert stored_rec_promoted.release_id == release.release_id

    # Assert job status is PROMOTED and release_id is bound
    assert job.status == "PROMOTED"
    assert job.release_id == release.release_id

    # Repeated promotion is rejected (already promoted / non-READY)
    with pytest.raises(ValueError) as exc_re:
        promote_repaired_skill(
            job=job,
            candidate_store=cand_store,
            registry=registry,
            state_machine=sm,
            caller_confirmed=True,
            evaluator=evaluator,
        )
    assert "already been promoted" in str(exc_re.value) or "Only READY jobs" in str(exc_re.value)

    # Step 4: Close old runtime, instantiate fresh ExperienceCollector and AgentRuntime
    eval_env["runtime"].close()

    new_collector = ExperienceCollector(
        episode_store=eval_env["episode_store"],
        registry=registry,
    )
    newrt = AgentRuntime(
        db_path=eval_env["runtime"].db_path,
        tool_broker=eval_env["broker"],
        registry=registry,
        collector=new_collector,
        episode_store=eval_env["episode_store"],
        candidate_store=eval_env["cand_store"],
    )

    try:
        # Step 5: Autonomous future reuse without passing skill_name/candidate
        rec = newrt.start_run(
            run_id="pi_future_reuse",
            task_id="pi_future_task",
            purpose="learning",
            enable_reuse=True,
            require_reuse=True,
            task_description=query,
            budget_max=10,
        )

        assert rec.skill_name == "logistics_query"
        assert rec.skill_version == "1.0.1"
        assert rec.frozen_body == patched_body
        assert newrt.get_run_body(run_id="pi_future_reuse") == patched_body

        retrieval_res = newrt.get_retrieval_result("pi_future_reuse")
        assert retrieval_res is not None
        assert len(retrieval_res.skills) > 0
        assert retrieval_res.skills[0].skill_name == "logistics_query"
        assert retrieval_res.skills[0].version == "1.0.1"

        # Step 6: Execute via Pi without candidate argument
        out = newrt.run_agent_with_pi(
            run_id="pi_future_reuse",
            input_text=query,
            tools=["query_order_packages", "query_package_tracking"],
            test_mode=True,
            model_config={"provider": "faux", "responses": make_logistics_responses(order_id)},
        )

        # Assert system prompt contained patched_body and not whole baseline_body
        last_pi_res = newrt._last_pi_results["pi_future_reuse"]
        sys_prompt = last_pi_res.captured_provider_inputs[0]["systemPrompt"]
        assert patched_body in sys_prompt
        assert baseline_body not in sys_prompt

        # Assert tool calls and provenances
        tool_calls = newrt.list_tool_calls("pi_future_reuse")
        assert len(tool_calls) == 3
        provenances = newrt.get_provenances("pi_future_reuse")
        assert len(provenances) == 3

        # Logistics fulfillment oracle validation
        oracle = verify_logistics_fulfillment(
            model_output=out,
            order_id=order_id,
            tool_records=tool_calls,
        )
        assert oracle["independent_pass"] is True
        assert "checked_invariants" in oracle and len(oracle["checked_invariants"]) > 0

        # Finalize run -> immutable success Episode
        run_rec, canonical_ep = newrt.finalize_run(
            run_id="pi_future_reuse",
            model_output=out,
            verification_evidence=oracle,
        )
        assert canonical_ep is not None
        assert canonical_ep.outcome == "success"
        assert canonical_ep.run_id == "pi_future_reuse"
        assert canonical_ep.task_id == "pi_future_task"
        assert canonical_ep.skill_version == "1.0.1"
        assert canonical_ep.environment.get("purpose") == "learning"
        assert canonical_ep.episode_id != pi_repair_env["canonical_ep"].episode_id

        # Persistent storage check
        stored_ep = eval_env["episode_store"].get_episode(canonical_ep.episode_id)
        assert stored_ep is not None
        assert stored_ep.outcome == "success"
        assert stored_ep.skill_version == "1.0.1"

        # Idempotent finalization check
        ep_count_before = len(eval_env["episode_store"].list_episodes())
        run_rec_idem, canonical_ep_idem = newrt.finalize_run(
            run_id="pi_future_reuse",
            model_output=out,
            verification_evidence=oracle,
        )
        ep_count_after = len(eval_env["episode_store"].list_episodes())
        assert ep_count_before == ep_count_after
        assert canonical_ep_idem.episode_id == canonical_ep.episode_id
    finally:
        newrt.close()

