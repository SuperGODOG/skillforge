"""Acceptance Test Suite: Retrieval-to-Execution Loop (U1 - U6)

Validates the complete execution loop integrating Phase P3 memory retrieval
with the real AgentRuntime, ToolBroker, Sandbox isolation, and M4a failure attribution.

Scenarios:
- U1: Auto-retrieval and sandboxed execution success
      New task provides only task_description and enable_reuse=True; automatically
      selects verified version; executes Runtime -> Broker -> Sandbox; Episode records
      task, fixed version, retrieval reasons, backend, and success.
- U2: In-flight version binding immune to canary and rollback
      In-flight task binds version snapshot; subsequent canary switch or rollback
      does not mutate in-flight task; new task routes to updated active deployment;
      both Episodes record their respective bound versions.
- U3: Security and dependency boundary blocks unauthorized tool
      Keyword matches, but tool lacks permission or dependency; blocked before host
      execution (call count = 0); terminal rejected/failed status without faking
      success or triggering irrelevant code repair.
- U4: Retrieval miss or filtered clean fallback
      Retrieval returns no match or all filtered; falls back to standard execution
      without skill or explicit non-reusable terminal status; Episode records
      retrieval_hit=False; no forged skills created.
- U5: Formal skill failure routes to M4a attribution and bounded repair
      Formal skill hit, but business tool returns failure; Episode records failure evidence;
      routes to M4a failure attribution and creates bounded RepairJob without auto-publishing.
- U6: Evaluation purpose isolation and zero retrieval side-effects
      purpose='evaluation' executes and records evaluation status, strictly isolated
      from pattern mining; standalone retrieval has zero side-effects.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest
from hello_agents.tools import Tool, ToolParameter, ToolResponse

from skillforge import (
    AgentRuntime,
    CandidateSkill,
    CandidateStore,
    Deployment,
    DeploymentManager,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    FutureMemoryRetriever,
    FutureRetrievalResult,
    MacSeatbeltSandbox,
    PatternMiningConfig,
    RatchetVerdict,
    Release,
    ReleaseStateMachine,
    RetrievalContext,
    RunRecord,
    SandboxBackend,
    SandboxedToolSpec,
    SkillMeta,
    SkillRecommendation,
    SkillRegistry,
    ThreeTierMemoryManager,
    ToolBroker,
    ToolCallProvenance,
    Trigger,
    ValidationRecord,
    attribute_failure,
    compute_candidate_hash,
    mine_pending,
    promote_candidate,
)
from skillforge.storage.db import init_db


def init_git_repo(repo_root: Path) -> None:
    """Initialize a git repository for registry and release tracking."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "skills").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "runner@test.local"], cwd=str(repo_root), check=True, capture_output=True)
    readme = repo_root / "README.md"
    readme.write_text("# SkillForge Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_root), check=True, capture_output=True)


class FailingCalcTool(Tool):
    """Tool that deliberately produces a business error for failure diagnosis."""

    def __init__(self):
        super().__init__(name="failing_calc_tool", description="Calculates division with error on zero")
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="a", type="integer", required=True, description="Numerator"),
            ToolParameter(name="b", type="integer", required=True, description="Denominator"),
        ]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        b = parameters.get("b", 0)
        if b == 0:
            return ToolResponse.error(code="DIVISION_BY_ZERO", message="Division by zero error in calculator")
        a = parameters.get("a", 0)
        return ToolResponse.success(text=str(a // b), data={"result": a // b})


class MockRestrictedTool(Tool):
    """Tool requiring privileged policy authorization."""

    def __init__(self):
        super().__init__(name="restricted_shell_tool", description="Privileged execution tool")
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="cmd", type="string", required=True, description="Command to execute")]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        return ToolResponse.success(text="Executed privileged command")


@pytest.fixture
def test_setup(tmp_path: Path):
    """Create isolated database, git repo, registry, and deployment manager."""
    db_path = tmp_path / "skillforge.db"
    conn = init_db(db_path)
    conn.close()

    repo_root = tmp_path / "repo"
    init_git_repo(repo_root)

    registry = SkillRegistry(db_path=db_path, skills_dir=repo_root / "skills", repo_root=repo_root)
    state_machine = ReleaseStateMachine(db_path=db_path, repo_root=repo_root)
    deployment_mgr = DeploymentManager(db_path=db_path, repo_root=repo_root, registry=registry)
    episode_store = EpisodeStore(db_path)
    candidate_store = CandidateStore(db_path)
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)
    memory_mgr = ThreeTierMemoryManager(
        db_path=db_path,
        registry=registry,
        deployment_manager=deployment_mgr,
        episode_store=episode_store,
        candidate_store=candidate_store,
    )

    # Sandbox backend setup
    sb = MacSeatbeltSandbox()
    sandbox_backend = sb if sb.is_available() else None

    # Sandboxed tool command template (writes/reads json in isolated sandbox workspace)
    calc_code = (
        "import sys, json\n"
        "params = json.load(sys.stdin)\n"
        "res = params.get('a', 0) + params.get('b', 0)\n"
        "print(json.dumps({'status': 'ok', 'result': res, 'sum': res}))\n"
    )
    sandboxed_calc_spec = SandboxedToolSpec(
        name="sandboxed_calculator",
        command_template=[sys.executable, "-c", calc_code],
        description="Performs arithmetic in sandbox subprocess",
        parameters=[
            ToolParameter(name="a", type="integer", required=True, description="Operand A"),
            ToolParameter(name="b", type="integer", required=True, description="Operand B"),
        ],
    )

    failing_tool = FailingCalcTool()
    restricted_tool = MockRestrictedTool()

    broker = ToolBroker(
        application_allowlist={"sandboxed_calculator", "failing_calc_tool"},
        sandbox_backend=sandbox_backend,
    )
    broker.register_sandboxed_tool(sandboxed_calc_spec)
    broker.register_tool(failing_tool)
    broker.register_tool(restricted_tool)  # registered in broker, but NOT in application_allowlist

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        deployment_manager=deployment_mgr,
        episode_store=episode_store,
        collector=collector,
        memory_manager=memory_mgr,
    )

    return {
        "db_path": db_path,
        "repo_root": repo_root,
        "registry": registry,
        "state_machine": state_machine,
        "deployment_mgr": deployment_mgr,
        "episode_store": episode_store,
        "candidate_store": candidate_store,
        "collector": collector,
        "memory_mgr": memory_mgr,
        "broker": broker,
        "runtime": runtime,
        "sandbox_backend": sandbox_backend,
        "sandboxed_calc_spec": sandboxed_calc_spec,
        "failing_tool": failing_tool,
        "restricted_tool": restricted_tool,
    }


def _publish_verified_skill(
    env: dict[str, Any],
    skill_name: str,
    version: str = "1.0.0",
    dependencies: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
) -> None:
    """Helper to publish an active verified formal skill into registry and deployments."""
    seed_ep_id = f"ep_seed_{skill_name}_{version.replace('.', '_')}"
    prov = ToolCallProvenance(
        tool_name="sandboxed_calculator",
        fixture_case_id="fixture_seed",
        call_index=0,
        call_count=1,
        is_fixture=True,
        tool_required=True,
        tool_called=True,
        tool_success=True,
        authenticity_pass=True,
        input_params={"a": 1, "b": 1},
        output_status="SUCCESS",
        output_summary="2",
        latency_ms=5.0,
        timestamp="2026-09-25T12:00:00Z",
        signature="sha256:seed_signature",
    )
    seed_ep = Episode(
        episode_id=seed_ep_id,
        task_id=f"task_seed_{skill_name}",
        run_id=f"run_seed_{skill_name}",
        skill_name=skill_name,
        skill_version=version,
        environment={"purpose": "learning"},
        provenances=[prov],
        acceptance_criteria={"goal": "seed"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    env["episode_store"].save_episode(seed_ep)

    meta = SkillMeta(
        name=skill_name,
        version=version,
        description=f"Automated procedure for {skill_name}",
        use_when=f"When task involves {skill_name}",
        trigger=Trigger(keywords=keywords or [skill_name, "calculate", "math"]),
        dependencies=dependencies or ["sandboxed_calculator"],
    )
    cand = CandidateSkill(
        candidate_id=f"cand_{skill_name}_{version.replace('.', '_')}",
        skill_name=skill_name,
        decision="create",
        source_episode_ids=[seed_ep_id],
        meta=meta,
        body=f"## Instructions\nExecute {skill_name} using available tools.",
        status="DRAFT",
    )
    env["candidate_store"].save_candidate(cand)
    val = ValidationRecord(
        candidate_id=cand.candidate_id,
        content_hash=compute_candidate_hash(cand),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["verification passed"]),
        verification_episode_ids=[seed_ep_id],
    )
    env["memory_mgr"].promote_candidate(
        candidate_id=cand.candidate_id,
        validation_record=val,
        state_machine=env["state_machine"],
        caller_confirmed=True,
    )
    env["deployment_mgr"].get_deployment(skill_name)


# =========================================================================
# Scenario U1: Auto-retrieval and sandboxed execution success
# =========================================================================
def test_u1_auto_retrieval_and_sandboxed_execution_success(test_setup):
    """U1: Preset verified Skill & sandboxed tool; new task passes only task_description and enable_reuse=True;
    automatically selects qualified version; executes Runtime -> Broker -> Sandbox;
    Episode captures task, fixed skill version, retrieval reasons, backend, and success.
    """
    env = test_setup
    skill_name = "math_solver"
    _publish_verified_skill(env, skill_name=skill_name, version="1.0.0", keywords=["math", "solver", "arithmetic"])

    runtime: AgentRuntime = env["runtime"]

    # New task: Caller passes NO skill_name! Only task_description and enable_reuse=True
    run = runtime.start_run(
        run_id="run_u1_001",
        task_id="task_u1_solve",
        purpose="learning",
        enable_reuse=True,
        task_description="Solve arithmetic problem using math solver",
        budget_max=5,
    )

    # Automatically selected qualified version
    assert run.skill_name == skill_name
    assert run.skill_version == "1.0.0"
    assert run.status == "RUNNING"

    retrieval_res = runtime.get_retrieval_result("run_u1_001")
    assert retrieval_res is not None
    assert len(retrieval_res.skills) >= 1
    assert retrieval_res.skills[0].skill_name == skill_name

    # Execute tool via Runtime -> Broker -> Sandbox
    call_rec = runtime.execute_tool(
        run_id="run_u1_001",
        tool_name="sandboxed_calculator",
        parameters={"a": 15, "b": 27},
    )
    assert call_rec.status == "EXECUTED"
    assert call_rec.output_data.get("result") == 42
    if env["sandbox_backend"]:
        assert call_rec.output_data.get("backend") == env["sandbox_backend"].name

    # Finalize run with verified pass evidence
    run_final, ep = runtime.finalize_run(
        run_id="run_u1_001",
        verification_evidence={"independent_pass": True, "result": 42},
    )
    assert run_final.status == "COMPLETED"
    assert ep is not None
    assert ep.task_id == "task_u1_solve"
    assert ep.skill_name == skill_name
    assert ep.skill_version == "1.0.0"
    assert ep.outcome == "success"
    assert ep.environment.get("enable_reuse") is True
    assert ep.environment.get("retrieval_hit") is True
    assert isinstance(ep.environment.get("retrieval_reasons"), list)
    assert len(ep.environment["retrieval_reasons"]) > 0
    if env["sandbox_backend"]:
        assert ep.environment.get("backend") == env["sandbox_backend"].name


# =========================================================================
# Scenario U2: In-flight version binding immune to canary and rollback
# =========================================================================
def test_u2_in_flight_version_binding_immune_to_canary_and_rollback(test_setup):
    """U2: In-flight task binds version snapshot; subsequent canary switch or rollback
    does not mutate in-flight task; new task routes to updated active deployment;
    both Episodes record their respective bound versions.
    """
    env = test_setup
    skill_name = "versioned_processor"
    _publish_verified_skill(env, skill_name=skill_name, version="1.0.0", keywords=["processor", "compute"])

    # Create and publish version 2.0.0
    meta_v2 = SkillMeta(
        name=skill_name,
        version="2.0.0",
        description="Version 2 of processor",
        use_when="processor compute v2",
        trigger=Trigger(keywords=["processor", "compute"]),
        dependencies=["sandboxed_calculator"],
    )
    seed_v2_id = "ep_seed_proc_v2"
    seed_ep_v2 = Episode(
        episode_id=seed_v2_id,
        task_id="task_seed_proc_v2",
        run_id="run_seed_proc_v2",
        skill_name=skill_name,
        skill_version="2.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={"goal": "seed_v2"},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    env["episode_store"].save_episode(seed_ep_v2)

    cand_v2 = CandidateSkill(
        candidate_id="cand_proc_v2",
        skill_name=skill_name,
        decision="revise",
        source_episode_ids=[seed_v2_id],
        meta=meta_v2,
        body="## Instructions\nProcessor v2 optimized logic.",
        status="DRAFT",
    )
    env["candidate_store"].save_candidate(cand_v2)
    val_v2 = ValidationRecord(
        candidate_id=cand_v2.candidate_id,
        content_hash=compute_candidate_hash(cand_v2),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["v2 ok"]),
        verification_episode_ids=[seed_v2_id],
    )
    env["deployment_mgr"].set_canary(
        skill_name,
        candidate_or_version=cand_v2,
        validation_record=val_v2,
        share=100,
        caller_confirmed=True,
    )

    runtime: AgentRuntime = env["runtime"]

    # Task 1 starts: routes and binds to active canary version 2.0.0
    run1 = runtime.start_run(
        run_id="run_u2_inflight",
        task_id="task_u2_inflight",
        purpose="learning",
        enable_reuse=True,
        task_description="Run processor compute workload",
    )
    assert run1.skill_name == skill_name
    assert run1.skill_version == "2.0.0"

    # While Task 1 is still in-flight, a rollback occurs to stable version 1.0.0
    env["deployment_mgr"].rollback_deployment(
        skill_name=skill_name,
        target_version="1.0.0",
        reason="canary anomaly detected in monitoring",
        caller_confirmed=True,
    )
    active_dep = env["deployment_mgr"].get_deployment(skill_name)
    assert active_dep.stable_version == "1.0.0"
    assert active_dep.canary_version is None

    # In-flight Task 1 executes tool and finalizes
    call1 = runtime.execute_tool(
        run_id="run_u2_inflight",
        tool_name="sandboxed_calculator",
        parameters={"a": 1, "b": 2},
    )
    assert call1.status == "EXECUTED"
    _, ep1 = runtime.finalize_run(
        run_id="run_u2_inflight",
        verification_evidence={"independent_pass": True},
    )
    # Task 1 MUST retain bound version 2.0.0
    assert ep1.skill_version == "2.0.0"

    # Task 2 starts AFTER rollback: routes to current active stable version 1.0.0
    run2 = runtime.start_run(
        run_id="run_u2_post_rollback",
        task_id="task_u2_post_rollback",
        purpose="learning",
        enable_reuse=True,
        task_description="Run processor compute workload",
    )
    assert run2.skill_name == skill_name
    assert run2.skill_version == "1.0.0"

    call2 = runtime.execute_tool(
        run_id="run_u2_post_rollback",
        tool_name="sandboxed_calculator",
        parameters={"a": 3, "b": 4},
    )
    assert call2.status == "EXECUTED"
    _, ep2 = runtime.finalize_run(
        run_id="run_u2_post_rollback",
        verification_evidence={"independent_pass": True},
    )
    # Task 2 MUST record version 1.0.0
    assert ep2.skill_version == "1.0.0"


# =========================================================================
# Scenario U3: Security and dependency boundary blocks unauthorized tool
# =========================================================================
def test_u3_security_and_dependency_boundary_blocks_unauthorized_tool(test_setup):
    """U3: Keyword matches, but tool lacks permission or dependency; blocked before host
    execution (call count = 0); terminal rejected/failed status without faking
    success or triggering irrelevant code repair.
    """
    env = test_setup
    skill_name = "privileged_sysadmin_skill"
    # Skill declares dependency on restricted_shell_tool, which is NOT in broker application_allowlist
    _publish_verified_skill(
        env,
        skill_name=skill_name,
        version="1.0.0",
        dependencies=["restricted_shell_tool"],
        keywords=["sysadmin", "privileged", "terminal"],
    )

    runtime: AgentRuntime = env["runtime"]
    restricted_tool: MockRestrictedTool = env["restricted_tool"]
    initial_call_count = restricted_tool.call_count

    # Case A: require_reuse=True filters out skill during retrieval (or rejects dispatch)
    # Retrieval detects restricted_shell_tool not in broker's application_allowlist
    run_a = runtime.start_run(
        run_id="run_u3_strict",
        task_id="task_u3_strict",
        purpose="learning",
        enable_reuse=True,
        require_reuse=True,
        task_description="Execute sysadmin privileged tasks",
    )
    assert run_a.status == "FAILED"
    assert run_a.error_type in ("PERMISSION_DENIED", "NO_REUSABLE_SKILL")
    assert restricted_tool.call_count == initial_call_count  # 0 calls

    # Case B: Direct attempt to dispatch unauthorized tool via runtime
    run_b = runtime.start_run(
        run_id="run_u3_dispatch",
        task_id="task_u3_dispatch",
        purpose="learning",
        skill_name=skill_name,
        skill_required_tools={"restricted_shell_tool"},
    )
    assert run_b.status == "RUNNING"
    rec = runtime.execute_tool(
        run_id="run_u3_dispatch",
        tool_name="restricted_shell_tool",
        parameters={"cmd": "whoami"},
    )
    assert rec.status == "REJECTED"
    assert rec.error_type == "PERMISSION_DENIED"
    assert restricted_tool.call_count == initial_call_count  # Strictly 0 host executions

    # Finalize and verify failure attribution routes to 'policy', not code repair
    _, ep_b = runtime.finalize_run(
        run_id="run_u3_dispatch",
        verification_evidence={"independent_pass": False, "failure_reason": "Permission denied for restricted tool"},
    )
    assert ep_b.outcome == "failure"
    diag = attribute_failure([ep_b], skill_name=skill_name)
    assert diag.responsibility_layer == "policy"
    assert diag.strategy is None
    assert diag.structured_signal_override is True
    assert "policy" in (diag.handoff_info or "").lower()


# =========================================================================
# Scenario U4: Retrieval miss or filtered clean fallback
# =========================================================================
def test_u4_retrieval_miss_or_filtered_clean_fallback(test_setup):
    """U4: Retrieval returns no match or all filtered; falls back to standard execution
    without skill or explicit non-reusable terminal status; Episode records
    retrieval_hit=False; no forged skills created.
    """
    env = test_setup
    runtime: AgentRuntime = env["runtime"]
    candidate_store: CandidateStore = env["candidate_store"]
    registry: SkillRegistry = env["registry"]

    cands_before = len(candidate_store.list_candidates())
    skills_before = len(registry.list_names())

    # Case 4a: require_reuse=False -> fallback to standard execution without skill
    run_a = runtime.start_run(
        run_id="run_u4_fallback",
        task_id="task_u4_fallback",
        purpose="learning",
        enable_reuse=True,
        require_reuse=False,
        task_description="Completely unknown nonexistent query xyz_99999",
    )
    assert run_a.status == "RUNNING"
    assert run_a.skill_name is None
    assert run_a.skill_version is None

    # Executes standard generic tool
    call_a = runtime.execute_tool(
        run_id="run_u4_fallback",
        tool_name="sandboxed_calculator",
        parameters={"a": 5, "b": 5},
    )
    assert call_a.status == "EXECUTED"

    _, ep_a = runtime.finalize_run(
        run_id="run_u4_fallback",
        verification_evidence={"independent_pass": True},
    )
    assert ep_a.skill_name == ""
    assert ep_a.environment.get("enable_reuse") is True
    assert ep_a.environment.get("retrieval_hit") is False
    assert "No formal skills matched" in (ep_a.environment.get("retrieval_empty_reason") or "")

    # Case 4b: require_reuse=True -> immediate terminal FAILED status
    run_b = runtime.start_run(
        run_id="run_u4_terminal",
        task_id="task_u4_terminal",
        purpose="learning",
        enable_reuse=True,
        require_reuse=True,
        task_description="Completely unknown nonexistent query xyz_99999",
    )
    assert run_b.status == "FAILED"
    assert run_b.error_type == "NO_REUSABLE_SKILL"

    # Invariant: zero forged skills or silent candidate promotions!
    assert len(candidate_store.list_candidates()) == cands_before
    assert len(registry.list_names()) == skills_before


# =========================================================================
# Scenario U5: Formal skill failure routes to M4a attribution and bounded repair
# =========================================================================
def test_u5_formal_skill_failure_routes_to_m4a_attribution_and_bounded_repair(test_setup):
    """U5: Formal skill hit, but business tool returns failure; Episode records failure evidence;
    routes to M4a failure attribution and creates bounded RepairJob without auto-publishing.
    """
    env = test_setup
    skill_name = "division_operator"
    _publish_verified_skill(
        env,
        skill_name=skill_name,
        version="1.0.0",
        dependencies=["failing_calc_tool"],
        keywords=["division", "operator", "divide"],
    )

    runtime: AgentRuntime = env["runtime"]

    # Start run with reuse mode -> hits division_operator v1.0.0
    run = runtime.start_run(
        run_id="run_u5_fail",
        task_id="task_u5_div",
        purpose="learning",
        enable_reuse=True,
        task_description="Perform division operator task",
    )
    assert run.skill_name == skill_name
    assert run.skill_version == "1.0.0"

    # Business tool execution fails (division by zero)
    call_rec = runtime.execute_tool(
        run_id="run_u5_fail",
        tool_name="failing_calc_tool",
        parameters={"a": 10, "b": 0},
    )
    assert call_rec.status == "ERROR"
    assert call_rec.error_type == "DIVISION_BY_ZERO"

    # Finalize run with verified business failure
    _, ep = runtime.finalize_run(
        run_id="run_u5_fail",
        verification_evidence={"independent_pass": False, "failure_reason": "Arithmetic division by zero encountered"},
    )
    assert ep.outcome == "failure"
    assert ep.verification_evidence["independent_pass"] is False

    # Route failure to M4a attribution
    diag = runtime.attribute_run_failure("run_u5_fail")
    assert diag is not None
    assert diag.responsibility_layer in ("skill", "tool")
    assert len(diag.evidence_refs) > 0

    # Create bounded RepairJob
    job = runtime.create_repair_job_for_run("run_u5_fail", max_attempts=2)
    assert job is not None
    assert job.skill_name == skill_name
    assert job.baseline_version == "1.0.0"
    assert ep.episode_id in job.source_episode_ids
    assert job.status in ("PENDING", "BLOCKED")

    # Invariant: RepairJob creation does NOT auto-publish a new version
    meta_current = env["registry"].get_meta(skill_name)
    assert meta_current.version == "1.0.0"


# =========================================================================
# Scenario U6: Evaluation purpose isolation and zero retrieval side-effects
# =========================================================================
def test_u6_evaluation_purpose_isolation_and_zero_retrieval_side_effects(test_setup):
    """U6: purpose='evaluation' executes and records evaluation status, strictly isolated
    from pattern mining; standalone retrieval has zero side-effects.
    """
    env = test_setup
    skill_name = "eval_target_skill"
    _publish_verified_skill(env, skill_name=skill_name, version="1.0.0", keywords=["target", "evaluate"])

    runtime: AgentRuntime = env["runtime"]
    memory_mgr: ThreeTierMemoryManager = env["memory_mgr"]

    # 1. Evaluation task run
    run_eval = runtime.start_run(
        run_id="run_u6_eval_01",
        task_id="task_u6_eval",
        purpose="evaluation",
        enable_reuse=True,
        task_description="Execute target evaluate procedure",
    )
    assert run_eval.purpose == "evaluation"
    assert run_eval.skill_name == skill_name

    call_eval = runtime.execute_tool(
        run_id="run_u6_eval_01",
        tool_name="sandboxed_calculator",
        parameters={"a": 10, "b": 20},
    )
    assert call_eval.status == "EXECUTED"

    _, ep_eval = runtime.finalize_run(
        run_id="run_u6_eval_01",
        verification_evidence={"independent_pass": True},
    )
    assert ep_eval.environment.get("purpose") == "evaluation"

    # Evaluation purpose isolation from pattern mining
    mined_report = mine_pending(
        episode_store=runtime.episode_store,
        candidate_store=env["candidate_store"],
        llm=None,
        config=PatternMiningConfig(min_support=1),
    )
    # The evaluation episode MUST NOT be used to generate candidates (strictly filtered)
    assert len(mined_report.candidates_created) == 0
    assert mined_report.filtered_evaluation_episodes >= 1

    # Explicit attempt to synthesize candidate from evaluation episode MUST raise ValueError
    with pytest.raises(ValueError, match="purpose='evaluation'"):
        memory_mgr.create_candidate_from_episodes([ep_eval], target_skill_name=skill_name, llm=None)

    # 2. Standalone retrieval side-effects verification:
    conn = runtime._get_conn()
    counts_before = {
        "skills": conn.execute("SELECT count(*) FROM skills").fetchone()[0],
        "releases": conn.execute("SELECT count(*) FROM releases").fetchone()[0],
        "episodes": conn.execute("SELECT count(*) FROM episodes").fetchone()[0],
        "candidates": conn.execute("SELECT count(*) FROM candidate_skills").fetchone()[0],
        "repair_jobs": conn.execute("SELECT count(*) FROM repair_jobs").fetchone()[0],
    }

    # Execute read-only retrieval multiple times
    for _ in range(5):
        res = memory_mgr.retrieve("Execute target evaluate procedure")
        assert len(res.skills) >= 1

    counts_after = {
        "skills": conn.execute("SELECT count(*) FROM skills").fetchone()[0],
        "releases": conn.execute("SELECT count(*) FROM releases").fetchone()[0],
        "episodes": conn.execute("SELECT count(*) FROM episodes").fetchone()[0],
        "candidates": conn.execute("SELECT count(*) FROM candidate_skills").fetchone()[0],
        "repair_jobs": conn.execute("SELECT count(*) FROM repair_jobs").fetchone()[0],
    }

    # Zero writes or mutations
    assert counts_before == counts_after
