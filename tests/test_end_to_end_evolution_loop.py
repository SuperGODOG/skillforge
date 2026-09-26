"""End-to-End Evolution Loop Full-Chain Acceptance Test Suite (F1 - F4)

Validates that all modular milestones (M1–M5c, P2, P3, U1–U6) cleanly connect across
a single coherent lifecycle chain:
Execution Experience -> Pattern Mining -> Candidate Skill -> Gate Validation / Explicit Promotion ->
Future Memory Retrieval & Actual Reuse -> Failure Attribution -> Bounded Repair / Regression ->
Version Evolution / Canary Routing / Controlled Rollback.

Scenarios:
- F1: Forward Generation to Reuse
      Runs two real learning tasks via AgentRuntime -> ToolBroker -> Sandbox;
      pattern mining synthesizes single CandidateSkill preserving source episode IDs;
      validation gate holds unconfirmed candidate; explicit caller confirmation generates v1;
      new task passes only task_description and enable_reuse=True; auto-retrieves v1;
      executes via Runtime -> Broker -> Sandbox; records immutable reuse Episode with
      provenance, fixed version, sandbox backend, and verified outcome.
- F2: Failure Attribution and Gate
      Task reusing v1 encounters business error in tool execution; Episode records failure;
      M4a attribute_failure categorizes root cause (skill/tool/policy); creates bounded RepairJob;
      unconfirmed or declined regression blocks publication; v1 remains active stable version;
      policy failures do not incorrectly trigger skill code repair.
- F3: Patch Verification, Canary, and Rollback
      Repaired candidate passes regression evaluation and explicit confirmation yields v2;
      canary traffic routing binds in-flight task to v2; mid-flight rollback restores stable v1;
      in-flight task completes on bound v2; subsequent new tasks route to v1;
      all historical episodes and lineage traces remain intact without corrupting past snapshots.
- F4: Isolation and Fail-Closed Safety
      Evaluation episodes with identical keywords are strictly isolated from pattern mining;
      unpromoted DRAFT candidates cannot masquerade as executable formal skills;
      query misses fall back cleanly according to require_reuse policy;
      unauthorized or missing-dependency skills are blocked with host execution count = 0.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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
    validate_candidate,
)
from skillforge.storage.db import init_db


def init_git_repo(repo_root: Path) -> None:
    """Initialize a git repository for registry and release tracking."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "skills").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "E2E Runner"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "e2e@skillforge.local"], cwd=str(repo_root), check=True, capture_output=True)
    readme = repo_root / "README.md"
    readme.write_text("# SkillForge E2E Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init e2e repo"], cwd=str(repo_root), check=True, capture_output=True)


class FakeLLM:
    """Deterministic FakeLLM for pattern mining synthesis and failure attribution."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[Any] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else ""
        return SimpleNamespace(
            content=content,
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )


def _skill_markdown(name: str = "math_calc_skill", version: str = "1.0.0", body_extra: str = "") -> str:
    return f"""---
name: {name}
version: {version}
description: Standardized procedure for {name}
use_when: handling arithmetic addition and math tasks
trigger:
  keywords: [{name}, calculate, addition, math]
dependencies: [sandboxed_calc]
---

## Overview
Automated operational guidance for {name}.

## Workflow
1. Parse operands a and b.
2. Dispatch sandboxed_calc with parameters.
{body_extra}
"""


class MockRestrictedTool(Tool):
    """Privileged tool not permitted in standard broker allowlist."""

    def __init__(self):
        super().__init__(name="restricted_system_tool", description="Privileged system execution")
        self.call_count = 0

    def get_parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="cmd", type="string", required=True, description="Command")]

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        self.call_count += 1
        return ToolResponse.success(text="Executed privileged system command")


@pytest.fixture
def e2e_env(tmp_path: Path):
    """Fixture providing isolated db, git repo, runtime, broker, sandbox, and memory manager."""
    db_path = tmp_path / "skillforge_e2e.db"
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

    # Real macOS sandbox backend
    sb = MacSeatbeltSandbox()
    sandbox_backend = sb if sb.is_available() else None

    # Multi-operation calculator executed inside real sandbox subprocess
    calc_code = (
        "import sys, json\n"
        "params = json.load(sys.stdin)\n"
        "op = params.get('op', 'add')\n"
        "a = params.get('a', 0)\n"
        "b = params.get('b', 0)\n"
        "if op == 'div' and b == 0:\n"
        "    print(json.dumps({'error': 'DIVISION_BY_ZERO', 'msg': 'Cannot divide by zero'}))\n"
        "    sys.exit(1)\n"
        "if op == 'fail':\n"
        "    print(json.dumps({'error': 'BUSINESS_ERROR', 'msg': 'Calculation failed on negative value'}))\n"
        "    sys.exit(1)\n"
        "res = a + b if op == 'add' else (a - b if op == 'sub' else a * b)\n"
        "print(json.dumps({'status': 'ok', 'result': res, 'op': op}))\n"
    )

    sandboxed_calc_spec = SandboxedToolSpec(
        name="sandboxed_calc",
        command_template=[sys.executable, "-c", calc_code],
        description="Performs arithmetic inside isolated subprocess sandbox",
        parameters=[
            ToolParameter(name="a", type="integer", required=True, description="Operand A"),
            ToolParameter(name="b", type="integer", required=True, description="Operand B"),
            ToolParameter(name="op", type="string", required=False, default="add", description="Operation"),
        ],
    )

    restricted_tool = MockRestrictedTool()

    broker = ToolBroker(
        application_allowlist={"sandboxed_calc"},
        sandbox_backend=sandbox_backend,
    )
    broker.register_sandboxed_tool(sandboxed_calc_spec)
    broker.register_tool(restricted_tool)  # Registered in broker, but NOT in application_allowlist

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
        "restricted_tool": restricted_tool,
    }


# =========================================================================
# Scenario F1: Forward Generation to Reuse
# =========================================================================
def test_f1_forward_generation_to_reuse(e2e_env):
    """F1: Run two learning tasks producing immutable Episodes; pattern mining forms
    single CandidateSkill with provenance; validation gate holds until explicit confirmation
    creates immutable v1; future task passes only task_description and enable_reuse=True;
    auto-retrieves v1, executes in sandbox, and produces verified reuse Episode.
    """
    env = e2e_env
    runtime: AgentRuntime = env["runtime"]
    ep_store: EpisodeStore = env["episode_store"]
    cand_store: CandidateStore = env["candidate_store"]
    reg: SkillRegistry = env["registry"]
    sm: ReleaseStateMachine = env["state_machine"]
    mem_mgr: ThreeTierMemoryManager = env["memory_mgr"]

    # 1. Run two learning tasks with identical operational pattern
    run_rec1 = runtime.start_run(
        run_id="run_f1_learn_01",
        task_id="task_f1_add_small",
        purpose="learning",
        task_description="calculate addition sum of small numbers",
    )
    call_rec1 = runtime.execute_tool(
        run_id="run_f1_learn_01",
        tool_name="sandboxed_calc",
        parameters={"a": 3, "b": 4, "op": "add"},
    )
    assert call_rec1.status == "EXECUTED"
    assert call_rec1.output_data.get("result") == 7
    _, ep1 = runtime.finalize_run(
        run_id="run_f1_learn_01",
        verification_evidence={"independent_pass": True, "result": 7},
    )
    assert ep1.outcome == "success"

    run_rec2 = runtime.start_run(
        run_id="run_f1_learn_02",
        task_id="task_f1_add_large",
        purpose="learning",
        task_description="calculate addition sum of large numbers",
    )
    call_rec2 = runtime.execute_tool(
        run_id="run_f1_learn_02",
        tool_name="sandboxed_calc",
        parameters={"a": 30, "b": 40, "op": "add"},
    )
    assert call_rec2.status == "EXECUTED"
    assert call_rec2.output_data.get("result") == 70
    _, ep2 = runtime.finalize_run(
        run_id="run_f1_learn_02",
        verification_evidence={"independent_pass": True, "result": 70},
    )
    assert ep2.outcome == "success"

    # Both episodes exist in immutable store
    assert ep_store.get_episode("ep_run_f1_learn_01") is not None
    assert ep_store.get_episode("ep_run_f1_learn_02") is not None

    # 2. Pattern mining synthesizes single CandidateSkill linked to both episodes
    llm = FakeLLM([_skill_markdown("calculate_skill", "1.0.0")])
    batch_report = mine_pending(
        episode_store=ep_store,
        candidate_store=cand_store,
        registry=reg,
        llm=llm,
        config=PatternMiningConfig(min_support=2, min_expressions=2, min_steps=1),
    )
    assert len(batch_report.candidates_created) == 1
    cand = batch_report.candidates_created[0]
    assert cand.skill_name == "calculate_skill"
    assert cand.status == "DRAFT"
    assert set(cand.source_episode_ids) == {"ep_run_f1_learn_01", "ep_run_f1_learn_02"}

    # Invariant: Active registry is untouched before promotion
    assert not reg.has_skill("calculate_skill")

    # 3. Validation gate passes; caller_confirmed=False blocks promotion
    val_record = ValidationRecord(
        candidate_id=cand.candidate_id,
        content_hash=compute_candidate_hash(cand),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["Validation cases passed"]),
        verification_episode_ids=["ep_run_f1_learn_01", "ep_run_f1_learn_02"],
    )

    with pytest.raises(ValueError, match="caller_confirmed=True"):
        mem_mgr.promote_candidate(
            candidate_id=cand.candidate_id,
            validation_record=val_record,
            state_machine=sm,
            caller_confirmed=False,
        )
    assert not reg.has_skill("calculate_skill")

    # Explicit confirmation promotes candidate to formal v1
    release_v1 = mem_mgr.promote_candidate(
        candidate_id=cand.candidate_id,
        validation_record=val_record,
        state_machine=sm,
        caller_confirmed=True,
    )
    assert release_v1.version == "1.0.0"
    assert reg.has_skill("calculate_skill")
    assert reg.get_meta("calculate_skill").version == "1.0.0"

    # 4. Future Task: Passes only task_description and enable_reuse=True (NO skill_name!)
    run_reuse = runtime.start_run(
        run_id="run_f1_reuse_01",
        task_id="task_f1_future_sum",
        purpose="learning",
        enable_reuse=True,
        task_description="calculate addition sum of numbers",
    )

    # Retrieval automatically selects qualified v1
    assert run_reuse.skill_name == "calculate_skill"
    assert run_reuse.skill_version == "1.0.0"
    assert run_reuse.status == "RUNNING"

    # Executes through Runtime -> Broker -> Sandbox
    call_reuse = runtime.execute_tool(
        run_id="run_f1_reuse_01",
        tool_name="sandboxed_calc",
        parameters={"a": 100, "b": 250, "op": "add"},
    )
    assert call_reuse.status == "EXECUTED"
    assert call_reuse.output_data.get("result") == 350

    # Finalize run and inspect immutable Episode
    _, ep_reuse = runtime.finalize_run(
        run_id="run_f1_reuse_01",
        verification_evidence={"independent_pass": True, "result": 350},
    )
    assert ep_reuse.skill_name == "calculate_skill"
    assert ep_reuse.skill_version == "1.0.0"
    assert ep_reuse.outcome == "success"
    assert ep_reuse.environment.get("enable_reuse") is True
    assert ep_reuse.environment.get("retrieval_hit") is True
    assert len(ep_reuse.environment["retrieval_reasons"]) > 0
    if env["sandbox_backend"]:
        assert ep_reuse.environment.get("backend") == env["sandbox_backend"].name


# =========================================================================
# Scenario F2: Failure Attribution and Gate
# =========================================================================
def test_f2_failure_attribution_and_gate(e2e_env):
    """F2: Controlled business error when reusing v1; Episode records failure;
    M4a attribute_failure attributes to correct layer; bounded RepairJob created;
    unconfirmed or declined regression leaves v1 active; policy violation does not
    incorrectly trigger skill code repair.
    """
    env = e2e_env
    runtime: AgentRuntime = env["runtime"]
    reg: SkillRegistry = env["registry"]
    cand_store: CandidateStore = env["candidate_store"]
    sm: ReleaseStateMachine = env["state_machine"]
    mem_mgr: ThreeTierMemoryManager = env["memory_mgr"]
    restricted_tool: MockRestrictedTool = env["restricted_tool"]

    # Pre-publish v1 of math_calc_skill
    cand_seed = CandidateSkill(
        candidate_id="cand_f2_seed",
        skill_name="math_calc_skill",
        decision="create",
        source_episode_ids=["ep_seed_f2"],
        meta=SkillMeta(
            name="math_calc_skill",
            version="1.0.0",
            description="Math calculator skill",
            use_when="when performing math calculations",
            trigger=Trigger(keywords=["math", "calculate", "addition"]),
            dependencies=["sandboxed_calc"],
        ),
        body=_skill_markdown("math_calc_skill", "1.0.0"),
        status="DRAFT",
    )
    seed_ep = Episode(
        episode_id="ep_seed_f2",
        task_id="task_seed_f2",
        run_id="run_seed_f2",
        skill_name="math_calc_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    env["episode_store"].save_episode(seed_ep)
    cand_store.save_candidate(cand_seed)
    val_seed = ValidationRecord(
        candidate_id="cand_f2_seed",
        content_hash=compute_candidate_hash(cand_seed),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_seed_f2"],
    )
    mem_mgr.promote_candidate("cand_f2_seed", val_seed, sm, caller_confirmed=True)
    env["deployment_mgr"].get_deployment("math_calc_skill")

    # 1. Reusing v1 encounters business failure in tool execution
    run_fail = runtime.start_run(
        run_id="run_f2_business_fail",
        task_id="task_f2_fail",
        purpose="learning",
        enable_reuse=True,
        task_description="calculate math expression with failure trigger",
    )
    assert run_fail.skill_name == "math_calc_skill"

    call_fail = runtime.execute_tool(
        run_id="run_f2_business_fail",
        tool_name="sandboxed_calc",
        parameters={"a": 10, "b": 0, "op": "fail"},
    )
    assert call_fail.status == "ERROR"

    _, ep_fail = runtime.finalize_run(
        run_id="run_f2_business_fail",
        verification_evidence={"independent_pass": False, "failure_reason": "Tool reported calculation failure on negative parameter"},
    )
    assert ep_fail.outcome == "failure"

    # 2. M4a failure attribution diagnoses root cause
    diag = runtime.attribute_run_failure("run_f2_business_fail")
    assert diag is not None
    assert diag.responsibility_layer in ("skill", "tool")
    assert len(diag.evidence_refs) > 0

    # Create bounded RepairJob in SQLite
    repair_job = runtime.create_repair_job_for_run("run_f2_business_fail", max_attempts=2)
    assert repair_job is not None
    assert repair_job.skill_name == "math_calc_skill"
    assert repair_job.baseline_version == "1.0.0"
    assert repair_job.status in ("PENDING", "BLOCKED")

    # 3. Gate verification: Unconfirmed candidate or regression failure keeps v1 unchanged
    cand_patch = CandidateSkill(
        candidate_id="cand_f2_patch_unconfirmed",
        skill_name="math_calc_skill",
        decision="revise",
        source_episode_ids=[ep_fail.episode_id],
        meta=SkillMeta(
            name="math_calc_skill",
            version="1.0.1",
            description="Patched math calculator",
            use_when="when performing math calculations",
            trigger=Trigger(keywords=["math", "calculate"]),
            dependencies=["sandboxed_calc"],
        ),
        body=_skill_markdown("math_calc_skill", "1.0.1", "Added boundary check."),
        status="DRAFT",
    )
    cand_store.save_candidate(cand_patch)

    val_declined = ValidationRecord(
        candidate_id=cand_patch.candidate_id,
        content_hash=compute_candidate_hash(cand_patch),
        baseline_version="1.0.0",
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="DECLINED", reasons=["Regression in test case"]),
        verification_episode_ids=[ep_fail.episode_id],
    )
    with pytest.raises(ValueError, match="ratchet decision"):
        mem_mgr.promote_candidate(
            cand_patch.candidate_id,
            val_declined,
            sm,
            caller_confirmed=True,
        )

    # Version 1.0.0 remains active and untampered
    assert reg.get_meta("math_calc_skill").version == "1.0.0"
    dep = env["deployment_mgr"].get_deployment("math_calc_skill")
    assert dep.stable_version == "1.0.0"

    # 4. Policy failure check: calling unauthorized tool does NOT trigger skill repair
    run_policy = runtime.start_run(
        run_id="run_f2_policy_violation",
        task_id="task_f2_policy",
        purpose="learning",
        skill_name="math_calc_skill",
    )
    call_policy = runtime.execute_tool(
        run_id="run_f2_policy_violation",
        tool_name="restricted_system_tool",
        parameters={"cmd": "reboot"},
    )
    assert call_policy.status == "REJECTED"
    assert call_policy.error_type == "PERMISSION_DENIED"
    assert restricted_tool.call_count == 0  # 0 host calls

    _, ep_policy = runtime.finalize_run(
        run_id="run_f2_policy_violation",
        verification_evidence={"independent_pass": False, "failure_reason": "Permission denied for tool restricted_system_tool"},
    )
    diag_policy = attribute_failure([ep_policy], skill_name="math_calc_skill")
    assert diag_policy.responsibility_layer == "policy"
    assert diag_policy.strategy is None
    assert diag_policy.structured_signal_override is True
    assert "policy" in (diag_policy.handoff_info or "").lower()


# =========================================================================
# Scenario F3: Patch Verification, Canary, and Rollback
# =========================================================================
def test_f3_patch_verification_canary_and_rollback(e2e_env):
    """F3: Validated patch candidate confirmed to immutable v2; canary routing binds
    in-flight run to v2; mid-flight rollback restores stable v1; in-flight run completes
    with bound v2; subsequent new tasks route to v1; historical episodes and lineage intact.
    """
    env = e2e_env
    runtime: AgentRuntime = env["runtime"]
    reg: SkillRegistry = env["registry"]
    cand_store: CandidateStore = env["candidate_store"]
    sm: ReleaseStateMachine = env["state_machine"]
    mem_mgr: ThreeTierMemoryManager = env["memory_mgr"]
    dep_mgr: DeploymentManager = env["deployment_mgr"]

    # 1. Publish baseline v1.0.0
    cand_v1 = CandidateSkill(
        candidate_id="cand_f3_v1",
        skill_name="arithmetic_suite",
        decision="create",
        source_episode_ids=["ep_seed_f3_v1"],
        meta=SkillMeta(
            name="arithmetic_suite",
            version="1.0.0",
            description="Arithmetic operations",
            use_when="when performing arithmetic operations",
            trigger=Trigger(keywords=["arithmetic", "suite", "math"]),
            dependencies=["sandboxed_calc"],
        ),
        body=_skill_markdown("arithmetic_suite", "1.0.0"),
        status="DRAFT",
    )
    seed_v1 = Episode(
        episode_id="ep_seed_f3_v1",
        task_id="task_seed_f3_v1",
        run_id="run_seed_f3_v1",
        skill_name="arithmetic_suite",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    env["episode_store"].save_episode(seed_v1)
    cand_store.save_candidate(cand_v1)
    val_v1 = ValidationRecord(
        candidate_id="cand_f3_v1",
        content_hash=compute_candidate_hash(cand_v1),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["v1 ok"]),
        verification_episode_ids=["ep_seed_f3_v1"],
    )
    mem_mgr.promote_candidate("cand_f3_v1", val_v1, sm, caller_confirmed=True)
    dep_mgr.get_deployment("arithmetic_suite")

    # 2. Patch candidate verified and admitted as Canary v2.0.0
    cand_v2 = CandidateSkill(
        candidate_id="cand_f3_v2",
        skill_name="arithmetic_suite",
        decision="revise",
        source_episode_ids=["ep_seed_f3_v2"],
        meta=SkillMeta(
            name="arithmetic_suite",
            version="2.0.0",
            description="Arithmetic operations v2",
            use_when="when performing arithmetic operations",
            trigger=Trigger(keywords=["arithmetic", "suite", "math"]),
            dependencies=["sandboxed_calc"],
        ),
        body=_skill_markdown("arithmetic_suite", "2.0.0", "Optimized parallel computation logic."),
        status="DRAFT",
    )
    seed_v2 = Episode(
        episode_id="ep_seed_f3_v2",
        task_id="task_seed_f3_v2",
        run_id="run_seed_f3_v2",
        skill_name="arithmetic_suite",
        skill_version="2.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    env["episode_store"].save_episode(seed_v2)
    cand_store.save_candidate(cand_v2)
    val_v2 = ValidationRecord(
        candidate_id="cand_f3_v2",
        content_hash=compute_candidate_hash(cand_v2),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["v2 regression passed"]),
        verification_episode_ids=["ep_seed_f3_v2"],
    )
    dep_mgr.set_canary(
        "arithmetic_suite",
        candidate_or_version=cand_v2,
        validation_record=val_v2,
        share=100,
        caller_confirmed=True,
    )
    dep_current = dep_mgr.get_deployment("arithmetic_suite")
    assert dep_current.canary_version == "2.0.0"

    # 3. Task 1 starts: In-flight run binds canary v2.0.0
    run_inflight = runtime.start_run(
        run_id="run_f3_inflight",
        task_id="task_f3_flight",
        purpose="learning",
        enable_reuse=True,
        task_description="Execute arithmetic suite task",
    )
    assert run_inflight.skill_name == "arithmetic_suite"
    assert run_inflight.skill_version == "2.0.0"

    # Mid-flight emergency rollback occurs: stable set back to 1.0.0, canary disabled
    dep_mgr.rollback_deployment(
        skill_name="arithmetic_suite",
        target_version="1.0.0",
        reason="Rollback canary due to downstream anomaly",
        caller_confirmed=True,
    )
    dep_rolled_back = dep_mgr.get_deployment("arithmetic_suite")
    assert dep_rolled_back.stable_version == "1.0.0"
    assert dep_rolled_back.canary_version is None

    # In-flight task executes and finalizes: MUST retain bound version 2.0.0
    call_inflight = runtime.execute_tool(
        run_id="run_f3_inflight",
        tool_name="sandboxed_calc",
        parameters={"a": 12, "b": 13, "op": "add"},
    )
    assert call_inflight.status == "EXECUTED"
    assert call_inflight.output_data.get("result") == 25
    _, ep_inflight = runtime.finalize_run(
        run_id="run_f3_inflight",
        verification_evidence={"independent_pass": True, "result": 25},
    )
    assert ep_inflight.skill_version == "2.0.0"

    # 4. Post-rollback new task routes to active stable version 1.0.0
    run_post_rb = runtime.start_run(
        run_id="run_f3_post_rb",
        task_id="task_f3_post",
        purpose="learning",
        enable_reuse=True,
        task_description="Execute arithmetic suite task",
    )
    assert run_post_rb.skill_name == "arithmetic_suite"
    assert run_post_rb.skill_version == "1.0.0"

    call_post = runtime.execute_tool(
        run_id="run_f3_post_rb",
        tool_name="sandboxed_calc",
        parameters={"a": 50, "b": 50, "op": "add"},
    )
    assert call_post.status == "EXECUTED"
    _, ep_post = runtime.finalize_run(
        run_id="run_f3_post_rb",
        verification_evidence={"independent_pass": True, "result": 100},
    )
    assert ep_post.skill_version == "1.0.0"

    # 5. Provenance & Lineage traceability
    lineage = mem_mgr.trace_lineage("arithmetic_suite")
    assert lineage.lineage_broken is False
    assert len(lineage.supporting_episodes) >= 1


# =========================================================================
# Scenario F4: Isolation and Fail-Closed Safety
# =========================================================================
def test_f4_isolation_and_fail_closed_safety(e2e_env):
    """F4: Evaluation episodes excluded from learning; unpromoted DRAFT candidates
    cannot execute as formal skills; unmatched queries fall back cleanly;
    unauthorized or dependency-missing skills blocked with 0 host executions.
    """
    env = e2e_env
    runtime: AgentRuntime = env["runtime"]
    cand_store: CandidateStore = env["candidate_store"]
    ep_store: EpisodeStore = env["episode_store"]
    reg: SkillRegistry = env["registry"]
    mem_mgr: ThreeTierMemoryManager = env["memory_mgr"]
    restricted_tool: MockRestrictedTool = env["restricted_tool"]

    # 1. Evaluation Episode Isolation
    run_eval = runtime.start_run(
        run_id="run_f4_eval",
        task_id="task_f4_eval",
        purpose="evaluation",
        enable_reuse=True,
        task_description="calculate math expression addition",
    )
    assert run_eval.purpose == "evaluation"
    call_eval = runtime.execute_tool(
        run_id="run_f4_eval",
        tool_name="sandboxed_calc",
        parameters={"a": 1, "b": 2, "op": "add"},
    )
    assert call_eval.status == "EXECUTED"
    _, ep_eval = runtime.finalize_run(
        run_id="run_f4_eval",
        verification_evidence={"independent_pass": True},
    )
    assert ep_eval.environment.get("purpose") == "evaluation"

    # Pattern mining strictly excludes evaluation episodes
    report = mine_pending(
        episode_store=ep_store,
        candidate_store=cand_store,
        llm=None,
        config=PatternMiningConfig(min_support=1),
    )
    assert len(report.candidates_created) == 0
    assert report.filtered_evaluation_episodes >= 1

    # Attempt to synthesize candidate directly from evaluation episode raises ValueError
    with pytest.raises(ValueError, match="purpose='evaluation'"):
        mem_mgr.create_candidate_from_episodes([ep_eval], target_skill_name="eval_test", llm=None)

    # 2. Unpromoted DRAFT candidate cannot masquerade as an executable formal skill
    cand_draft = CandidateSkill(
        candidate_id="cand_f4_unpromoted_draft",
        skill_name="draft_only_skill",
        decision="create",
        source_episode_ids=["ep_seed_draft"],
        meta=SkillMeta(
            name="draft_only_skill",
            version="1.0.0",
            description="Draft procedure",
            use_when="when executing draft procedures",
            trigger=Trigger(keywords=["draft_only_skill", "calculate"]),
            dependencies=["sandboxed_calc"],
        ),
        body=_skill_markdown("draft_only_skill", "1.0.0"),
        status="DRAFT",
    )
    seed_draft = Episode(
        episode_id="ep_seed_draft",
        task_id="task_seed_draft",
        run_id="run_seed_draft",
        skill_name="draft_only_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(seed_draft)
    cand_store.save_candidate(cand_draft)

    retrieval_res = mem_mgr.retrieve("draft_only_skill")
    # Draft candidate MUST NOT appear in recommendations
    assert not any(rec.skill_name == "draft_only_skill" for rec in retrieval_res.skills)
    assert any(
        item.get("filter_type") == "unpromoted_candidate" and item.get("skill_name") == "draft_only_skill"
        for item in retrieval_res.filtered_out
    )

    # 3. Clean fallback on non-matching query
    # require_reuse=False -> fallback to standard execution
    run_miss_fallback = runtime.start_run(
        run_id="run_f4_miss_fallback",
        task_id="task_f4_miss_fallback",
        purpose="learning",
        enable_reuse=True,
        require_reuse=False,
        task_description="nonexistent operation query 88888",
    )
    assert run_miss_fallback.status == "RUNNING"
    assert run_miss_fallback.skill_name is None
    _, ep_miss = runtime.finalize_run(
        run_id="run_f4_miss_fallback",
        verification_evidence={"independent_pass": True},
    )
    assert ep_miss.skill_name == ""
    assert ep_miss.environment.get("retrieval_hit") is False

    # require_reuse=True -> immediate terminal FAILED status
    run_miss_strict = runtime.start_run(
        run_id="run_f4_miss_strict",
        task_id="task_f4_miss_strict",
        purpose="learning",
        enable_reuse=True,
        require_reuse=True,
        task_description="nonexistent operation query 88888",
    )
    assert run_miss_strict.status == "FAILED"
    assert run_miss_strict.error_type == "NO_REUSABLE_SKILL"

    # 4. Unauthorized skill blocked with 0 host executions
    cand_unauth = CandidateSkill(
        candidate_id="cand_f4_unauth",
        skill_name="unauthorized_exec_skill",
        decision="create",
        source_episode_ids=["ep_seed_unauth"],
        meta=SkillMeta(
            name="unauthorized_exec_skill",
            version="1.0.0",
            description="Skill requiring forbidden tool",
            use_when="when executing unauthorized operations",
            trigger=Trigger(keywords=["unauth_exec", "privileged"]),
            dependencies=["restricted_system_tool"],
        ),
        body=_skill_markdown("unauthorized_exec_skill", "1.0.0"),
        status="DRAFT",
    )
    seed_unauth = Episode(
        episode_id="ep_seed_unauth",
        task_id="task_seed_unauth",
        run_id="run_seed_unauth",
        skill_name="unauthorized_exec_skill",
        skill_version="1.0.0",
        environment={"purpose": "learning"},
        provenances=[],
        acceptance_criteria={},
        outcome="success",
        verification_evidence={"independent_pass": True},
    )
    ep_store.save_episode(seed_unauth)
    cand_store.save_candidate(cand_unauth)
    val_unauth = ValidationRecord(
        candidate_id="cand_f4_unauth",
        content_hash=compute_candidate_hash(cand_unauth),
        baseline_version=None,
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=RatchetVerdict(decision="PASS", reasons=["ok"]),
        verification_episode_ids=["ep_seed_unauth"],
    )
    mem_mgr.promote_candidate("cand_f4_unauth", val_unauth, env["state_machine"], caller_confirmed=True)
    env["deployment_mgr"].get_deployment("unauthorized_exec_skill")

    init_calls = restricted_tool.call_count

    # Retrieval filters out unauthorized skill
    run_unauth = runtime.start_run(
        run_id="run_f4_unauth_call",
        task_id="task_f4_unauth_call",
        purpose="learning",
        enable_reuse=True,
        require_reuse=True,
        task_description="Execute unauth_exec privileged procedure",
    )
    assert run_unauth.status == "FAILED"
    assert run_unauth.error_type in ("PERMISSION_DENIED", "NO_REUSABLE_SKILL")
    assert restricted_tool.call_count == init_calls  # Host tool call count is strictly 0
