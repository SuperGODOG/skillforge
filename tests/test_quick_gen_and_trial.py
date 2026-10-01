"""P1 Acceptance Test Suite: Quick Generation & Controlled Trial Execution.

Verifies Acceptance Criteria G1–G6:
- G1: Requirement -> Draft Candidate without prior episodes -> Executed in AgentRuntime with frozen snapshot.
      Invariant: In-memory mutation of Candidate after start_run does not alter the frozen run body.
      Candidate dynamically reports is_trial_tested == True once an episode is saved.
- G2: Unpromoted Draft Candidate is filtered out from formal skill retrieval for unrelated tasks.
- G3: Repeated generation with same requirement does not overwrite or register into formal skills.
- G4: Unauthorized tool (refund_order) rejected before execution by ToolBroker with PERMISSION_DENIED;
      tool handler call count is strictly 0.
- G5: Execution without authoritative verification oracle records outcome='unknown';
      model self-assertions of success are ignored; draft remains in DRAFT and editable.
- G6: generate_skill(register=False) is 100% backward compatible; register_skill requires explicit caller_confirmed=True.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hello_agents.tools import ToolParameter
from skillforge.collector import ExperienceCollector
from skillforge.deployments import DeploymentManager
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.memory import ThreeTierMemoryManager
from skillforge.models import CandidateSkill, RetrievalContext, SkillMeta
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
from skillforge.evolution_loop import ValidationRecord
from skillforge.skill_generator import (
    GeneratedSkill,
    GenerationFailure,
    RegistrationError,
    compute_generated_hash,
    generate_candidate_from_requirement,
    generate_skill,
    register_skill,
)


class MockLLM:
    """Predictable mock LLM returning valid skill generation payload."""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.invocations: list[Any] = []

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.invocations.append(messages)
        return SimpleNamespace(content=self.response_text)


class ScriptedLogisticsLLM:
    """Scripted LLM for G1 trial: verifies frozen prompt consumption and drives logistics tools."""

    def __init__(self, expected_frozen_substring: str, forbidden_substring: str = "TAMPERED"):
        self.model = "scripted-logistics-model"
        self.expected_frozen_substring = expected_frozen_substring
        self.forbidden_substring = forbidden_substring
        self.saw_frozen_prompt = False
        self.step = 0

    def invoke_with_tools(self, messages: list[dict[str, Any]], tools: list[Any], **kwargs: Any) -> Any:
        self.step += 1
        system_content = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        if self.expected_frozen_substring in system_content and self.forbidden_substring not in system_content:
            self.saw_frozen_prompt = True

        if self.step == 1:
            tc = SimpleNamespace(
                id="call_pkg_list",
                type="function",
                function=SimpleNamespace(
                    name="query_order_packages",
                    arguments=json.dumps({"order_id": "ORD_2026_0901"}),
                ),
            )
            msg = SimpleNamespace(content=None, tool_calls=[tc])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        elif self.step == 2:
            tc1 = SimpleNamespace(
                id="call_trk_101",
                type="function",
                function=SimpleNamespace(
                    name="query_package_tracking",
                    arguments=json.dumps({"package_id": "PKG_101"}),
                ),
            )
            tc2 = SimpleNamespace(
                id="call_trk_102",
                type="function",
                function=SimpleNamespace(
                    name="query_package_tracking",
                    arguments=json.dumps({"package_id": "PKG_102"}),
                ),
            )
            msg = SimpleNamespace(content=None, tool_calls=[tc1, tc2])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        else:
            final_text = "订单 ORD_2026_0901 包含两个包裹 PKG_101 与 PKG_102，均已签收送达，订单全部签收。"
            msg = SimpleNamespace(content=final_text, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def _sample_llm_json(name: str = "logistics_tracker") -> str:
    return json.dumps({
        "name": name,
        "version": "1.0.0",
        "description": "电商多包裹物流状态核查助手",
        "use_when": "用户需要查询订单中的所有包裹物流状态及是否全部签收",
        "not_for": ["退款退货申请", "修改收货地址"],
        "keywords": ["物流查询", "包裹追踪", "签收状态"],
        "examples": ["帮我查一下订单 ORD_2026_0901 的所有包裹到了没？"],
        "body": "## Overview\n电商多包裹物流查询技能正文。\n## Instructions\n1. 查询订单包裹。\n2. 逐一核查状态。\n## Examples\n示例1\n## Constraints\n- 必须全量包裹签收方可答复全部签收。",
        "test_cases": [
            {"query": "查询 ORD_2026_0901", "reference": "全部包裹均已签收"},
            {"query": "查询 ORD_2026_0902", "reference": "部分包裹仍在运输中"},
            {"query": "查询不存在订单", "reference": "提示订单不存在并核实"},
        ],
    })


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "skillforge_test.db"


@pytest.fixture
def isolated_stores(temp_db: Path) -> tuple[EpisodeStore, CandidateStore]:
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db)
    return ep_store, cand_store


def test_g1_quick_generation_and_trial_execution(temp_db: Path, tmp_path: Path):
    """G1: 无历史 Episode，一条明确需求产生草稿并在 AgentRuntime 中受控试用。

    - 需求 -> Draft Candidate (无前置 Episode 历史，source_episode_ids 为空)
    - 运行时注入 candidate 执行，冻结 body 快照
    - 验证内存中修改 Candidate 对象不影响运行时冻结的 body
    - 记录 Episode 后，is_trial_tested 动态返回 True
    """
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db)
    mock_llm = MockLLM(_sample_llm_json("logistics_checker"))

    requirement = "请生成一个电商多包裹物流核查技能，用于根据订单查询所有包裹配送状态"

    candidate = generate_candidate_from_requirement(
        request=requirement,
        candidate_store=cand_store,
        llm=mock_llm,
        repo_root=tmp_path,
        conflict_method="embedding",
    )

    assert isinstance(candidate, CandidateSkill)
    assert candidate.skill_name == "logistics_checker"
    assert candidate.status == "DRAFT"
    assert candidate.source_episode_ids == []
    assert candidate.source_requirement == requirement
    assert candidate.source_type == "requirement"
    assert candidate.task_spec_hash is not None
    assert len(candidate.task_spec_hash) > 0
    assert not candidate.is_trial_tested(ep_store)

    # 启动 AgentRuntime 并进行候选试用
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())

    collector = ExperienceCollector(episode_store=ep_store)
    runtime = AgentRuntime(db_path=temp_db, tool_broker=broker, collector=collector)

    run_rec = runtime.start_run(
        run_id="run_trial_001",
        task_id="task_order_check_01",
        candidate=candidate,
        purpose="learning",
    )
    assert run_rec.status == "RUNNING"
    assert run_rec.skill_name == "logistics_checker"

    # 验证运行时获取到冻结 body
    frozen_body = runtime.get_run_body(skill_name="logistics_checker", run_id="run_trial_001")
    assert "电商多包裹物流查询技能正文" in frozen_body

    # 不变量验证：修改内存中的 candidate 对象，运行时的 frozen_body 保持不变
    original_candidate_body = candidate.body
    candidate.body = "TAMPERED_IN_MEMORY_CONTENT"
    retrieved_body_after_tamper = runtime.get_run_body(skill_name="logistics_checker", run_id="run_trial_001")
    assert retrieved_body_after_tamper == original_candidate_body
    assert "TAMPERED" not in retrieved_body_after_tamper

    # 执行 Agent 真实试用：验证冻结的正文进入 Agent 的 prompt 输入，通过 BrokeredTool 派发物流工具
    scripted_llm = ScriptedLogisticsLLM(
        expected_frozen_substring="电商多包裹物流查询技能正文",
        forbidden_substring="TAMPERED",
    )
    model_output = runtime.run_agent(
        run_id="run_trial_001",
        input_text="帮我查一下订单 ORD_2026_0901 的所有包裹到了没？",
        llm=scripted_llm,
        candidate=candidate,
    )
    assert scripted_llm.saw_frozen_prompt is True

    # 验证工具调用已通过 Broker 真实执行并记录到运行时与 Collector
    tool_calls = runtime.list_tool_calls("run_trial_001")
    assert len(tool_calls) == 3
    executed_tool_names = [c.tool_name for c in tool_calls]
    assert executed_tool_names == ["query_order_packages", "query_package_tracking", "query_package_tracking"]
    assert all(c.status == "EXECUTED" for c in tool_calls)

    # 业务端通过独立 Oracle 进行核验
    evidence = verify_logistics_fulfillment(model_output, "ORD_2026_0901", tool_calls)
    assert evidence["independent_pass"] is True

    term_run, ep = runtime.finalize_run(
        run_id="run_trial_001",
        model_output=model_output,
        verification_evidence=evidence,
    )
    assert term_run.status == "COMPLETED"
    assert ep.outcome == "success"
    assert ep.environment["candidate_id"] == candidate.candidate_id
    assert ep.environment["task_spec_hash"] == candidate.task_spec_hash

    # 验证 Candidate 动态计算 is_trial_tested == True 且状态未被篡改
    assert candidate.is_trial_tested(ep_store) is True
    assert candidate.status == "DRAFT"


def test_g2_draft_candidate_never_retrieved_by_unrelated_tasks(temp_db: Path, tmp_path: Path):
    """G2: 未晋升草稿绝不会被另一无关任务的正式检索选中。"""
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db)
    reg = SkillRegistry(db_path=temp_db, skills_dir=tmp_path / "skills")
    dm = DeploymentManager(temp_db, registry=reg)

    # 存入一个未晋升的 Draft Candidate
    candidate = CandidateSkill(
        candidate_id="cand_draft_logistic_99",
        skill_name="logistics_helper",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(
            name="logistics_helper",
            version="1.0.0",
            description="电商物流查询与追踪",
            use_when="用户需要查询订单物流",
        ),
        body="## Overview\n查询订单物流配送进度",
        status="DRAFT",
        source_requirement="查询电商物流",
        source_type="requirement",
    )
    cand_store.save_candidate(candidate)

    mm = ThreeTierMemoryManager(
        db_path=temp_db,
        registry=reg,
        deployment_manager=dm,
        episode_store=ep_store,
        candidate_store=cand_store,
    )

    # 无关任务发起正式检索
    ctx = RetrievalContext(task_id="task_weather_lookup")
    res = mm.retrieve(query="查询订单物流配送进度", context=ctx)

    # 核心断言：未晋升候选绝不出现在 formal skills 列表里
    formal_names = [s.skill_name for s in res.skills]
    assert "logistics_helper" not in formal_names

    # 验证被显式过滤并记录原因
    filtered_cand_entries = [f for f in res.filtered_out if f.get("item_id") == "cand_draft_logistic_99"]
    assert len(filtered_cand_entries) == 1
    assert filtered_cand_entries[0]["filter_type"] == "unpromoted_candidate"

    # 在严格复用模式下启动无关任务，应该因为无正式技能而直接拒绝，而不是偷用草稿
    runtime = AgentRuntime(db_path=temp_db, registry=reg, deployment_manager=dm, memory_manager=mm)
    run_rec = runtime.start_run(
        run_id="run_unrelated_task",
        task_id="task_weather_lookup",
        enable_reuse=True,
        require_reuse=True,
        task_description="查询订单物流配送进度",
    )
    assert run_rec.status == "FAILED"
    assert run_rec.error_type == "NO_REUSABLE_SKILL"


def test_g3_duplicate_requests_do_not_overwrite_formal_registry(temp_db: Path, tmp_path: Path):
    """G3: 相同请求重复触发时复用/修订草稿而非派生无限制新 ID，不覆盖正式技能库，重启 DB 后一致。"""
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    cand_store = CandidateStore(temp_db)
    mock_llm = MockLLM(_sample_llm_json("multi_pkg_checker"))
    req = "请生成电商多包裹物流查询工具"

    # 第一次生成
    cand1 = generate_candidate_from_requirement(
        request=req,
        candidate_store=cand_store,
        llm=mock_llm,
        repo_root=tmp_path,
    )
    assert isinstance(cand1, CandidateSkill)
    assert cand1.status == "DRAFT"

    # 第二次相同需求生成：识别 task_spec_hash 复用并更新草稿，不派生无限新 ID
    cand2 = generate_candidate_from_requirement(
        request=req,
        candidate_store=cand_store,
        llm=mock_llm,
        repo_root=tmp_path,
    )
    assert isinstance(cand2, CandidateSkill)
    assert cand2.status == "DRAFT"
    assert cand2.candidate_id == cand1.candidate_id
    assert cand2.task_spec_hash == cand1.task_spec_hash
    assert len(cand_store.list_candidates()) == 1

    # 重启验证：关闭 CandidateStore 并重新连接 DB，数据保持一致
    cand_store.close()
    reopened_store = CandidateStore(temp_db)
    reopened_cand = reopened_store.get_candidate(cand1.candidate_id)
    assert reopened_cand is not None
    assert reopened_cand.candidate_id == cand1.candidate_id
    assert reopened_cand.task_spec_hash == cand1.task_spec_hash
    assert reopened_cand.skill_name == "multi_pkg_checker"
    assert reopened_cand.status == "DRAFT"
    assert len(reopened_store.list_candidates()) == 1
    reopened_store.close()

    # skills/ 目录中不应被静默写入
    formal_skill_dir = tmp_path / "skills" / "multi_pkg_checker"
    assert not formal_skill_dir.exists()

    # 尝试使用旧 register=True 但不带确认，被安全拦截
    res = generate_skill(request=req, llm=mock_llm, repo_root=tmp_path, register=True, caller_confirmed=False)
    assert isinstance(res, GenerationFailure)
    assert res.reason == "REGISTER_UNCONFIRMED"
    assert not formal_skill_dir.exists()

    # 带 caller_confirmed=True 但无 validation_record，同样被拦截
    res2 = generate_skill(request=req, llm=mock_llm, repo_root=tmp_path, register=True, caller_confirmed=True)
    assert isinstance(res2, GenerationFailure)
    assert res2.reason == "REGISTER_UNVALIDATED"
    assert not formal_skill_dir.exists()


def test_g4_unauthorized_tool_rejected_by_broker(temp_db: Path):
    """G4: 声明未授权工具时在执行前被拒绝；不能通过草稿试用路径绕过 Broker。

    - 侧效应退款工具 refund_order 不在 allowlist
    - Broker 拒绝执行，返回 PERMISSION_DENIED
    - 底层 handler 调用次数严格为 0
    """
    reset_refund_call_count()

    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())
    broker.register_tool(RefundOrderTool())  # 注册了但未在 application_allowlist 授权

    runtime = AgentRuntime(db_path=temp_db, tool_broker=broker)

    cand = CandidateSkill(
        candidate_id="cand_with_privilege_attempt",
        skill_name="refund_hacker",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(
            name="refund_hacker",
            version="1.0.0",
            description="试图在草稿中调用未授权退款",
            use_when="退款",
            dependencies=["refund_order"],
        ),
        body="## Overview\nRefund Draft",
        status="DRAFT",
        source_requirement="我想退款",
        source_type="requirement",
    )

    runtime.start_run(run_id="run_priv_test", task_id="task_priv", candidate=cand)

    # 派发未授权工具
    rec = runtime.execute_tool(
        run_id="run_priv_test",
        tool_name="refund_order",
        parameters={"order_id": "ORD_2026_0901", "amount": 100.0},
    )

    assert rec.status == "REJECTED"
    assert rec.error_type == "PERMISSION_DENIED"
    assert "not authorized" in rec.error_message or "not in application allowlist" in rec.error_message
    assert get_refund_call_count() == 0


def test_g5_missing_oracle_yields_unknown_and_preserves_draft(temp_db: Path):
    """G5: 结果证据不足保留 unknown，草稿仍可修订；不伪造成功 Episode。

    - 模型自我声称“全部签收成功”
    - 没有权威验证证据（verification_evidence 为 None）
    - Episode.outcome 严格为 unknown，理由明确标为无独立核验证据
    - 草稿维持 DRAFT 状态，仍可被读取和修订
    """
    ep_store = EpisodeStore(temp_db)
    cand_store = CandidateStore(temp_db)
    collector = ExperienceCollector(episode_store=ep_store)

    broker = ToolBroker(application_allowlist={"query_order_packages"})
    broker.register_tool(QueryOrderPackagesTool())

    runtime = AgentRuntime(db_path=temp_db, tool_broker=broker, collector=collector)

    cand = CandidateSkill(
        candidate_id="cand_no_oracle_01",
        skill_name="logistics_unverified",
        decision="create",
        source_episode_ids=[],
        meta=SkillMeta(name="logistics_unverified", version="1.0.0", description="desc", use_when="when"),
        body="## Overview\nDraft",
        status="DRAFT",
        source_requirement="查询物流",
        source_type="requirement",
    )
    cand_store.save_candidate(cand)

    runtime.start_run(run_id="run_no_oracle", task_id="task_no_oracle", candidate=cand)

    runtime.execute_tool(
        run_id="run_no_oracle",
        tool_name="query_order_packages",
        parameters={"order_id": "ORD_2026_0901"},
    )

    # 假模型输出自我吹嘘成功，但无独立证据
    model_output = "【大功告成】我已经确认全部包裹已送达签收！100%成功！"
    _, ep = runtime.finalize_run(
        run_id="run_no_oracle",
        model_output=model_output,
        verification_evidence=None,  # 关键：缺少权威验证
    )

    assert ep.outcome == "unknown"
    assert "No valid independent verification evidence" in ep.outcome_reason

    # 草稿未被篡改为已验证/已晋升，依旧处于 DRAFT
    saved_cand = cand_store.get_candidate("cand_no_oracle_01")
    assert saved_cand is not None
    assert saved_cand.status == "DRAFT"
    # 但试用经历已记录，动态 is_trial_tested 为 True
    assert saved_cand.is_trial_tested(ep_store) is True


def test_g6_backward_compatibility_and_explicit_registration_gate(tmp_path: Path):
    """G6: 旧 generate_skill 默认返回行为保持兼容；显式注册路径不能绕过新的正式准入。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    eval_dir = tmp_path / "evaluation_sets"
    eval_dir.mkdir()
    repair_file = eval_dir / "repair_set.json"
    repair_file.write_text(
        json.dumps({
            "meta": {"total": 9, "auto_case_ids": ["old_auto_01"], "auto_case_count": 1},
            "cases": [
                {"id": "old_auto_01", "skill": "old_skill", "query": "q", "reference": "r", "trace_id": "t"},
                *[{"id": f"base_{i}", "skill": "old_skill", "query": f"b{i}", "reference": f"r{i}"} for i in range(1, 9)],
            ],
        }),
        encoding="utf-8",
    )
    router_file = eval_dir / "router_negatives.json"
    router_file.write_text(json.dumps({"meta": {}, "cases": []}), encoding="utf-8")

    mock_llm = MockLLM(_sample_llm_json("legacy_test_skill"))

    # 1. 旧用法：generate_skill(register=False) 保持 100% 兼容，返回 GeneratedSkill
    gen_result = generate_skill(
        request="测试向后兼容技能",
        llm=mock_llm,
        repo_root=tmp_path,
        register=False,
    )
    assert isinstance(gen_result, GeneratedSkill)
    assert gen_result.success is True
    assert gen_result.name == "legacy_test_skill"
    assert not (skills_dir / "legacy_test_skill").exists()

    # 2. 试图直接调用 register_skill 但不传确认 -> 拒绝并给出明确指引
    with pytest.raises(RegistrationError, match="直接向正式技能库落盘注册已受控"):
        register_skill(
            gen_result,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=False,
        )
    assert not (skills_dir / "legacy_test_skill").exists()

    # 3. 显式确认但未传门禁验证记录 -> 拒绝落盘
    with pytest.raises(RegistrationError, match="无法直接落盘：候选必须先通过共同门禁验证"):
        register_skill(
            gen_result,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=None,
        )
    assert not (skills_dir / "legacy_test_skill").exists()

    # 4. generate_skill(register=True, caller_confirmed=True) 无验证记录 -> 返回 REGISTER_UNVALIDATED 失败
    gen_fail_unval = generate_skill(
        request="测试向后兼容技能",
        llm=mock_llm,
        repo_root=tmp_path,
        register=True,
        caller_confirmed=True,
        validation_record=None,
    )
    assert isinstance(gen_fail_unval, GenerationFailure)
    assert gen_fail_unval.reason == "REGISTER_UNVALIDATED"
    assert not (skills_dir / "legacy_test_skill").exists()

    # 5. 未持久化于 CandidateStore 的伪造验证记录 -> 拒绝落盘
    cand_store = CandidateStore(tmp_path / "skillforge.db")
    cand_id = "cand_legacy_test_skill"
    cand_store.save_candidate(
        CandidateSkill(
            candidate_id=cand_id,
            skill_name=gen_result.name,
            decision="create",
            source_episode_ids=[],
            meta=gen_result.meta,
            body=gen_result.body_raw or "",
            rationale="Legacy registration test candidate",
            status="DRAFT",
            source_requirement="测试向后兼容技能",
            source_type="requirement",
        )
    )

    unpersisted_record = ValidationRecord(
        candidate_id="cand_unpersisted_forged",
        content_hash=compute_generated_hash(gen_result),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    with pytest.raises(RegistrationError, match="未经过共同门禁权威验证或验证记录不存在（伪造验证被拒绝）"):
        register_skill(
            gen_result,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=unpersisted_record,
            candidate_store=cand_store,
        )
    assert not (skills_dir / "legacy_test_skill").exists()

    # 6. 持久化记录决策非 PASS (如 DECLINED) -> 拒绝落盘
    declined_record = ValidationRecord(
        candidate_id=cand_id,
        content_hash=compute_generated_hash(gen_result),
        baseline_version="1.0.0",
        ratchet_decision="DECLINED",
        eval_result=None,
        ratchet_verdict=None,
    )
    cand_store.save_validation_record(declined_record)
    with pytest.raises(RegistrationError, match="共同门禁验证未通过"):
        register_skill(
            gen_result,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=declined_record,
            candidate_store=cand_store,
        )
    assert not (skills_dir / "legacy_test_skill").exists()

    # 7. 验证后候选正文发生变更 (哈希不匹配) -> 拒绝落盘
    tampered_record = ValidationRecord(
        candidate_id=cand_id,
        content_hash="tampered_unmatched_content_hash_12345",
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
    )
    cand_store.save_validation_record(tampered_record)
    with pytest.raises(RegistrationError, match="验证记录已失效：候选正文在验证后发生变更"):
        register_skill(
            gen_result,
            repo_root=tmp_path,
            repair_set_path=repair_file,
            router_negatives_path=router_file,
            caller_confirmed=True,
            validation_record=tampered_record,
            candidate_store=cand_store,
        )
    assert not (skills_dir / "legacy_test_skill").exists()

    # 8. 持有权威 PASS 验证记录且正文哈希一致 + 显式确认 -> 允许完成注册落盘并标记 PROMOTED
    valid_pass_record = ValidationRecord(
        candidate_id=cand_id,
        content_hash=compute_generated_hash(gen_result),
        baseline_version="1.0.0",
        ratchet_decision="PASS",
        eval_result=None,
        ratchet_verdict=None,
        promoted=False,
    )
    cand_store.save_validation_record(valid_pass_record)
    skill_file = register_skill(
        gen_result,
        repo_root=tmp_path,
        repair_set_path=repair_file,
        router_negatives_path=router_file,
        caller_confirmed=True,
        validation_record=valid_pass_record,
        candidate_store=cand_store,
    )
    assert skill_file.exists()
    assert (skills_dir / "legacy_test_skill" / "SKILL.md").exists()
    promoted_val = cand_store.get_validation_record(cand_id)
    assert promoted_val is not None and promoted_val.promoted is True
