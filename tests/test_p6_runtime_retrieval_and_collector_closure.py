"""P6 Offline Behavioral Special Test Suite: Runtime Auto-Retrieval & Collector Closure.

Invariants verified:
1. AgentRuntime auto-retrieves formal skill (logistics_tracking v1.0.1) from natural language query
   without manual SkillID or body injection.
2. Freezes selected skill ID, version (1.0.1), and body SHA256 at run start.
3. ToolBroker gateway permission rejection: unauthorized refund_order call rejected before execution;
   underlying handler call count is strictly 0.
4. Authorized tools executed through Runtime -> ToolBroker, generating verified tool call records.
5. Independent business Oracle evaluates fulfillment under STATUS_ONLY constraint.
6. ExperienceCollector saves immutable Episode into EpisodeStore upon finalize_run.
7. Reopened database retrieves Episode by ID, confirming task intent, version, body hash,
   tool traces, and independent Oracle verdict.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.models import CandidateSkill, SkillMeta, Trigger
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
    RefundOrderTool,
    get_refund_call_count,
    reset_refund_call_count,
    verify_logistics_fulfillment,
)


V1_0_1_BODY = """---
name: logistics_tracking
version: 1.0.1
description: 电商多包裹物流客观核查助手（无建议模式）
use_when: 当用户需要仅核对多包裹物流客观状态、禁止提供建议时使用
not_for:
  - 金融支付交易
  - 提出后续建议
dependencies: []
trigger:
  keywords:
    - 物流
    - 包裹
    - 运单
    - 签收
    - 客观状态
examples:
  - 核实订单客观物流状态
evaluation:
  last_score: null
  last_release_id: null
---
## Overview
电商多包裹订单物流客观状态核查助手。支持严格客观状态核查，禁止任何后续建议或行动指引。

## Instructions
1. 调用 query_order_packages 查询订单包含的全部包裹单号列表。
2. 针对每个包裹，调用 query_package_tracking 查询其实时运输与签收状态。
3. 严格仅输出各包裹客观状态，严禁输出任何售后建议、确认收货建议、催促或行动指引！

## Examples
Q: 核实订单 ORD_DEV_0601 全部包裹状态，只列出状态，不提出任何后续建议。
A: 订单 ORD_DEV_0601 包裹状态：PKG_D601 已签收，PKG_D602 已签收。共 2 个包裹，全部签收。以上为全部客观状态。

## Constraints
- STATUS_ONLY 任务下严禁输出任何后续建议、确认收货建议、催促或行动指引。
- 仅通过已授权工具查询，不编造不存在的状态。"""


@pytest.fixture
def isolated_runtime_env():
    """Create isolated SQLite database, skills directory, and runtime environment."""
    reset_refund_call_count()
    tmp_path = Path(tempfile.mkdtemp(prefix="test_p6_closure_"))
    db_path = tmp_path / "eval.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Mount published 1.0.1 skill
    pkg_dir = skills_dir / "logistics_tracking"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "SKILL.md").write_text(V1_0_1_BODY, encoding="utf-8")

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=tmp_path)
    reg.load_skills_from_dir()

    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool("query_order_packages", QueryOrderPackagesTool())
    broker.register_tool("query_package_tracking", QueryPackageTrackingTool())
    broker.register_tool("refund_order", RefundOrderTool())

    collector = ExperienceCollector(episode_store=ep_store, registry=reg)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=reg,
        episode_store=ep_store,
        collector=collector,
    )

    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "skills_dir": skills_dir,
        "runtime": runtime,
        "broker": broker,
        "registry": reg,
        "ep_store": ep_store,
        "cand_store": cand_store,
        "collector": collector,
    }

    import shutil
    if tmp_path.exists():
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_runtime_auto_retrieval_freezes_version_and_body_sha(isolated_runtime_env):
    """Verify AgentRuntime auto-retrieves formal skill without manual ID and freezes version & SHA."""
    env = isolated_runtime_env
    runtime: AgentRuntime = env["runtime"]

    task_desc = "核对 ORD_DEV_0602 包裹配送情况，仅输出客观事实，不要给我任何建议或催促。"
    run_id = "run_closure_freeze_01"

    # Start run with natural language query only: no skill_name, no candidate
    run = runtime.start_run(
        run_id=run_id,
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description=task_desc,
        budget_max=10,
    )

    assert run.status == "RUNNING"
    assert run.skill_name == "logistics_tracking"
    assert run.skill_version == "1.0.1"
    assert run.content_hash is not None

    # Retrieve frozen body from runtime
    retrieved_body = runtime.get_run_body(run_id=run_id)
    assert "## Overview" in retrieved_body
    assert "STATUS_ONLY" in retrieved_body

    # Verify body matches frozen content_hash from run
    actual_sha = hashlib.sha256(retrieved_body.strip().encode("utf-8")).hexdigest()
    assert actual_sha == run.content_hash

    # Verify retrieval metadata recorded in runtime
    ret_res = runtime.get_retrieval_result(run_id)
    assert ret_res is not None
    assert len(ret_res.skills) >= 1
    assert ret_res.skills[0].skill_name == "logistics_tracking"
    assert ret_res.skills[0].version == "1.0.1"


def test_runtime_tool_broker_permission_rejection_handler_zero(isolated_runtime_env):
    """Verify ToolBroker permission rejection on refund_order and handler call count is strictly 0."""
    env = isolated_runtime_env
    runtime: AgentRuntime = env["runtime"]

    reset_refund_call_count()
    assert get_refund_call_count() == 0

    run = runtime.start_run(
        run_id="run_closure_perm_01",
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description="核对 ORD_DEV_0602 包裹配送情况，仅输出客观事实，不要给我任何建议或催促。",
    )

    # Attempt unauthorized high-privilege write tool
    rec_denied = runtime.execute_tool(
        run_id=run.run_id,
        tool_name="refund_order",
        parameters={"order_id": "ORD_DEV_0602", "amount": 99.0},
    )

    assert rec_denied.status == "REJECTED"
    assert rec_denied.error_type == "PERMISSION_DENIED"
    assert "not authorized" in rec_denied.error_message.lower() or "permission_denied" in rec_denied.error_message.lower()

    # Underlying handler must NOT have been called
    assert get_refund_call_count() == 0


def test_runtime_and_collector_episode_persistence_and_db_reopen(isolated_runtime_env):
    """Verify full auto-retrieval -> tool dispatch -> Oracle -> Collector -> DB reopen cycle."""
    env = isolated_runtime_env
    runtime: AgentRuntime = env["runtime"]
    db_path: Path = env["db_path"]

    run_id = "run_closure_e2e_01"
    run = runtime.start_run(
        run_id=run_id,
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description="核对 ORD_DEV_0602 包裹配送情况，仅输出客观事实，不要给我任何建议或催促。",
    )

    # Execute authorized tools through runtime
    t1 = runtime.execute_tool(run_id, "query_order_packages", {"order_id": "ORD_DEV_0602"})
    assert t1.status == "EXECUTED"
    assert "PKG_D603" in t1.output_text
    assert "PKG_D604" in t1.output_text

    t2 = runtime.execute_tool(run_id, "query_package_tracking", {"package_id": "PKG_D603"})
    assert t2.status == "EXECUTED"
    assert "DELIVERED" in t2.output_text

    t3 = runtime.execute_tool(run_id, "query_package_tracking", {"package_id": "PKG_D604"})
    assert t3.status == "EXECUTED"
    assert "IN_TRANSIT" in t3.output_text

    tool_calls = runtime.list_tool_calls(run_id)
    assert len(tool_calls) == 3

    # Model output obeying STATUS_ONLY (1 delivered, 1 in transit, 0 advice)
    model_output = (
        "订单 ORD_DEV_0602 包裹状态如下：\n"
        "- PKG_D603: 已签收（顺丰速运）\n"
        "- PKG_D604: 运输中（中通快递）\n\n"
        "共 2 个包裹，1 个已签收，1 个运输中。以上为全部客观状态。"
    )

    # Independent logistics Oracle verification
    verdict = verify_logistics_fulfillment(
        model_output=model_output,
        order_id="ORD_DEV_0602",
        tool_records=tool_calls,
        intent_constraint="STATUS_ONLY",
    )
    assert verdict["independent_pass"] is True
    assert verdict["classification"] == "TRUE_POSITIVE"
    assert verdict["all_delivered"] is False

    # Finalize run and verify Episode generation
    term_run, ep = runtime.finalize_run(
        run_id=run_id,
        model_output=model_output,
        verification_evidence=verdict,
        acceptance_criteria={"intent_constraint": "STATUS_ONLY", "order_id": "ORD_DEV_0602"},
    )
    assert term_run.status == "COMPLETED"
    assert ep is not None
    assert ep.episode_id == f"ep_{run_id}"
    assert ep.outcome == "success"
    assert ep.skill_name == "logistics_tracking"
    assert ep.skill_version == "1.0.1"
    assert len(ep.provenances) == 3

    # Reopen fresh EpisodeStore connection and verify round-trip
    reopened_store = EpisodeStore(db_path)
    loaded_ep = reopened_store.get_episode(f"ep_{run_id}")
    assert loaded_ep is not None
    assert loaded_ep.task_id == "DEV_GOAL_02"
    assert loaded_ep.skill_name == "logistics_tracking"
    assert loaded_ep.skill_version == "1.0.1"
    assert loaded_ep.outcome == "success"
    assert loaded_ep.verification_evidence["independent_pass"] is True
    assert loaded_ep.verification_evidence["classification"] == "TRUE_POSITIVE"
    assert len(loaded_ep.provenances) == 3
    assert loaded_ep.provenances[0].tool_name == "query_order_packages"
    assert loaded_ep.provenances[1].tool_name == "query_package_tracking"
    assert loaded_ep.provenances[2].tool_name == "query_package_tracking"


class ScriptedMultiTurnLLM:
    """Scripted LLM simulating multi-turn function calling for DEV_GOAL_02."""

    def __init__(self):
        self.model = "glm-5.3-flash"
        self.step = 0
        self.invocations: List[Any] = []

    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[Any], **kwargs: Any) -> Any:
        self.step += 1
        self.invocations.append(messages)

        if self.step == 1:
            # First turn: call query_order_packages
            tc = SimpleNamespace(
                id="call_pkg_query",
                type="function",
                function=SimpleNamespace(
                    name="query_order_packages",
                    arguments=json.dumps({"order_id": "ORD_DEV_0602"}),
                ),
            )
            msg = SimpleNamespace(content=None, tool_calls=[tc])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        elif self.step == 2:
            # Second turn: query individual package
            tc1 = SimpleNamespace(
                id="call_trk_603",
                type="function",
                function=SimpleNamespace(
                    name="query_package_tracking",
                    arguments=json.dumps({"package_id": "PKG_D603"}),
                ),
            )
            tc2 = SimpleNamespace(
                id="call_trk_604",
                type="function",
                function=SimpleNamespace(
                    name="query_package_tracking",
                    arguments=json.dumps({"package_id": "PKG_D604"}),
                ),
            )
            msg = SimpleNamespace(content=None, tool_calls=[tc1, tc2])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        else:
            # Final turn: answer
            final_text = (
                "订单 ORD_DEV_0602 包裹状态：\n"
                "- PKG_D603：已签收（顺丰速运）\n"
                "- PKG_D604：运输中（中通快递）\n\n"
                "共 2 个包裹，1 个已签收，1 个运输中。以上为全部客观状态。"
            )
            msg = SimpleNamespace(content=final_text, tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def test_runtime_run_agent_auto_retrieval_and_tool_execution(isolated_runtime_env):
    """Verify runtime.run_agent with Scripted LLM without passing candidate or skill ID."""
    env = isolated_runtime_env
    runtime: AgentRuntime = env["runtime"]

    run_id = "run_agent_auto_01"
    run = runtime.start_run(
        run_id=run_id,
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description="核对 ORD_DEV_0602 包裹配送情况，仅输出客观事实，不要给我任何建议或催促。",
    )
    assert run.skill_name == "logistics_tracking"
    assert run.skill_version == "1.0.1"

    llm = ScriptedMultiTurnLLM()

    # run_agent called without candidate: auto-resolves skill_name and frozen body from run_id!
    output = runtime.run_agent(
        run_id=run_id,
        input_text="核对 ORD_DEV_0602 包裹配送情况，仅输出客观事实，不要给我任何建议或催促。",
        llm=llm,
        system_prompt_header="你是一个电商多包裹物流核查助理。请严格遵循以下业务技能指南执行任务：",
        max_tool_iterations=5,
    )

    assert "PKG_D603" in output
    assert "PKG_D604" in output

    tool_calls = runtime.list_tool_calls(run_id)
    assert len(tool_calls) == 3
    assert tool_calls[0].tool_name == "query_order_packages"
    assert tool_calls[1].tool_name == "query_package_tracking"
    assert tool_calls[2].tool_name == "query_package_tracking"

    verdict = verify_logistics_fulfillment(
        model_output=output,
        order_id="ORD_DEV_0602",
        tool_records=tool_calls,
        intent_constraint="STATUS_ONLY",
    )
    assert verdict["independent_pass"] is True
    assert verdict["classification"] == "TRUE_POSITIVE"

    term_run, ep = runtime.finalize_run(
        run_id=run_id,
        model_output=output,
        verification_evidence=verdict,
        acceptance_criteria={"intent_constraint": "STATUS_ONLY", "order_id": "ORD_DEV_0602"},
    )
    assert term_run.status == "COMPLETED"
    assert ep.outcome == "success"
    assert ep.skill_version == "1.0.1"


def test_authoritative_snapshot_auto_retrieval_and_collector_closure(tmp_path):
    """Verify runtime auto-retrieval and collector persistence on authoritative DB snapshot."""
    auth_db = Path("/var/folders/2h/03vn62sn2bx9hn1j2hzy067w0000gn/T/sf_p6_eval_ktu64a61/eval.db")
    auth_repo = Path("/var/folders/2h/03vn62sn2bx9hn1j2hzy067w0000gn/T/sf_p6_eval_ktu64a61")
    if not auth_db.exists():
        pytest.skip("Authoritative DB not present")

    import shutil
    copy_dir = tmp_path / "auth_copy"
    shutil.copytree(auth_repo, copy_dir)
    db_path = copy_dir / "eval.db"
    skills_dir = copy_dir / "skills"

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path, episode_store=ep_store)
    reg = SkillRegistry(db_path=db_path, skills_dir=skills_dir, repo_root=copy_dir)
    reg.load_skills_from_dir()

    assert reg.has_skill("logistics_tracking")
    meta = reg.get_meta("logistics_tracking")
    assert meta.version == "1.0.1"

    val_rec = cand_store.get_validation_record("cand_lifecycle_v2_8e70d68c")
    assert val_rec is not None
    assert val_rec.record_id == "vrec_e1ff3e9f659a"
    assert val_rec.ratchet_decision == "PASS"
    assert val_rec.promoted is True

    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool("query_order_packages", QueryOrderPackagesTool())
    broker.register_tool("query_package_tracking", QueryPackageTrackingTool())
    broker.register_tool("refund_order", RefundOrderTool())

    collector = ExperienceCollector(episode_store=ep_store, registry=reg)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=reg,
        episode_store=ep_store,
        collector=collector,
    )

    run_id = "test_auth_snapshot_01"
    run = runtime.start_run(
        run_id=run_id,
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description="核实订单 ORD_DEV_0602 全部包裹状态，只列出状态，不提出任何后续建议。",
        budget_max=10,
    )
    assert run.skill_name == "logistics_tracking"
    assert run.skill_version == "1.0.1"
    assert run.content_hash is not None

    # Execute tools
    t1 = runtime.execute_tool(run_id, "query_order_packages", {"order_id": "ORD_DEV_0602"})
    assert t1.status == "EXECUTED"
    t2 = runtime.execute_tool(run_id, "query_package_tracking", {"package_id": "PKG_D603"})
    assert t2.status == "EXECUTED"
    t3 = runtime.execute_tool(run_id, "query_package_tracking", {"package_id": "PKG_D604"})
    assert t3.status == "EXECUTED"

    # Permission rejection on refund_order
    reset_refund_call_count()
    denied = runtime.execute_tool(run_id, "refund_order", {"order_id": "ORD_DEV_0602", "amount": 10.0})
    assert denied.status == "REJECTED"
    assert denied.error_type == "PERMISSION_DENIED"
    assert get_refund_call_count() == 0

    output = "订单 ORD_DEV_0602 包裹状态：PKG_D603 已签收，PKG_D604 运输中。共 2 个包裹，1 个已签收，1 个运输中。以上为全部客观状态。"
    verdict = verify_logistics_fulfillment(
        model_output=output,
        order_id="ORD_DEV_0602",
        tool_records=[t1, t2, t3],
        intent_constraint="STATUS_ONLY",
    )
    assert verdict["independent_pass"] is True
    assert verdict["classification"] == "TRUE_POSITIVE"

    term_run, ep = runtime.finalize_run(
        run_id=run_id,
        model_output=output,
        verification_evidence=verdict,
        acceptance_criteria={"intent_constraint": "STATUS_ONLY", "order_id": "ORD_DEV_0602"},
    )
    assert term_run.status == "COMPLETED"
    assert ep is not None

    # Reopen fresh EpisodeStore
    reopened = EpisodeStore(db_path)
    loaded_ep = reopened.get_episode(ep.episode_id)
    assert loaded_ep is not None
    assert loaded_ep.task_id == "DEV_GOAL_02"
    assert loaded_ep.skill_name == "logistics_tracking"
    assert loaded_ep.skill_version == "1.0.1"
    assert loaded_ep.outcome == "success"
    assert loaded_ep.verification_evidence["independent_pass"] is True


def test_runtime_snapshot_frozen_against_registry_mutation(isolated_runtime_env):
    """Verify that get_run_body and run_agent use the start_run frozen snapshot even if registry changes."""
    env = isolated_runtime_env
    runtime: AgentRuntime = env["runtime"]
    reg: SkillRegistry = env["registry"]
    skills_dir: Path = env["skills_dir"]

    run_id = "run_closure_mutation_01"
    run = runtime.start_run(
        run_id=run_id,
        task_id="DEV_GOAL_02",
        purpose="evaluation",
        enable_reuse=True,
        require_reuse=True,
        task_description="核实订单 ORD_DEV_0602 全部包裹状态，只列出状态，不提出任何后续建议。",
    )
    assert run.skill_name == "logistics_tracking"
    assert run.skill_version == "1.0.1"

    frozen_body = runtime.get_run_body(run_id=run_id)
    assert "Overview" in frozen_body
    assert "MUTATED_TAMPERED" not in frozen_body

    # Mutate the skill file on disk and reload the registry mid-run
    skill_file = skills_dir / "logistics_tracking" / "SKILL.md"
    mutated_content = "---\nname: logistics_tracking\nversion: 2.0.0\ndescription: MUTATED_TAMPERED\nuse_when: test\n---\n# MUTATED_TAMPERED\n"
    skill_file.write_text(mutated_content, encoding="utf-8")
    reg._metas.clear()
    reg._bodies.clear()
    reg.load_skills_from_dir()

    # Registry now returns mutated content
    assert "MUTATED_TAMPERED" in reg.get_body("logistics_tracking")

    # BUT runtime.get_run_body MUST return the start_run frozen snapshot!
    body_after_mutation = runtime.get_run_body(run_id=run_id)
    assert body_after_mutation == frozen_body
    assert "MUTATED_TAMPERED" not in body_after_mutation

    # And run_agent uses the frozen body, NOT the mutated registry
    class MockLLM:
        def __init__(self):
            self.model = "glm-5.3-flash"
            self.captured_system = ""
        def invoke_with_tools(self, messages, tools, **kwargs):
            if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
                self.captured_system = messages[0]["content"]
            else:
                self.captured_system = kwargs.get("system", "")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK", tool_calls=[]))])

    mock_llm = MockLLM()
    runtime.run_agent(run_id=run_id, input_text="test", llm=mock_llm)
    assert "MUTATED_TAMPERED" not in mock_llm.captured_system
    assert "Overview" in mock_llm.captured_system
