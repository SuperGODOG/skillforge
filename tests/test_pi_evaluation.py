"""Phase 3A Module-Specific Tests: SkillEvaluator with Pi Execution Backend.

Verifies:
1. Candidate validation using validate_candidate with PiEvaluationBackend:
   - Evaluator clones into isolated sandbox preserving execution_backend.
   - Baseline and candidate runs use real Pi runner with faux provider.
   - Provider inputs capture respective baseline and candidate bodies (isolated).
   - Real tool execution through ToolBroker (QueryOrderPackages, QueryPackageTracking).
   - Episodes created with purpose="evaluation".
   - Formal registry remains untouched.
   - Independent logistics oracle & gates PASS.
   - ValidationRecord binds candidate content_hash, config_hash, and dataset_version.
2. Incomplete or false delivery claim ("全部签收") blocked by logistics code oracle:
   - Model claims all delivered while PKG_102 is IN_TRANSIT (or omits PKG_102).
   - Even if Judge LLM returns PASS, logistics code oracle marks critical_fail.
   - Common ratchet gate DECLINES candidate and blocks publication.
3. Auxiliary assertions:
   - Pi evaluator config fingerprint differs from legacy evaluator.
   - Missing usage produces tokens=None and avg_tokens=None (never fake 0).
   - Production mode (test_mode=False) strictly rejects faux provider.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import CandidateStore, EpisodeStore
from skillforge.evaluator import EvaluatorOutputCache, SkillEvaluator
from skillforge.evolution_loop import compute_candidate_hash, validate_candidate
from skillforge.models import (
    CandidateSkill,
    SkillMeta,
    Trigger,
    ValidationRecord,
)
from skillforge.pi_evaluation import PiEvaluationBackend, PiEvaluationRunResult
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import (
    QueryOrderPackagesTool,
    QueryPackageTrackingTool,
)


def _judge_json(verdict: str = "tied") -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": ["OK_READABILITY"],
        "evidence_summary": "Both outputs are clear and readable.",
    })


class FakeLLM:
    """Deterministic mock LLM for testing evaluator without real API calls."""

    def __init__(self, contents: list[str] | None = None, role: str = "judge"):
        self.contents = list(contents or [])
        self.role = role
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.contents.pop(0) if self.contents else _judge_json("tied")
        if messages and any("findings" in str(m.get("content", "")) for m in messages if isinstance(m, dict)):
            if "findings" not in str(content):
                user_msg = ""
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        user_msg = str(m.get("content", ""))
                m_ans = re.search(r"<answer>\s*(.*?)\s*</answer>", user_msg, re.DOTALL)
                ev_str = m_ans.group(1).strip() if m_ans else ""
                if not ev_str or ev_str == "(空回答)":
                    m_q = re.search(r"<query>\s*(.*?)\s*</query>", user_msg, re.DOTALL)
                    ev_str = m_q.group(1).strip() if m_q else "query"
                quote_ev = f'"{ev_str[:30]}"'
                content = json.dumps({
                    "findings": [
                        {"rule_id": "TASK_GOAL_COMPLETE", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "TASK_CONSTRAINTS_FOLLOWED", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "ROBUST_EVIDENCE_FAITHFUL", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                        {"rule_id": "ROBUST_FAILURE_HANDLING", "status": "PASS", "evidence": quote_ev, "reason": "satisfied"},
                    ]
                })
        return SimpleNamespace(
            content=content,
            usage=None,
        )


@pytest.fixture
def eval_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sets up isolated SQLite runtime, ToolBroker, Collector, CandidateStore, and SkillRegistry."""
    from skillforge.scenarios.logistics import MOCK_PACKAGES

    # Ground truth: ORD_2026_0901 has PKG_101 (DELIVERED) and PKG_102 (IN_TRANSIT)
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "status", "IN_TRANSIT")
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "carrier", "Carrier_Beta")
    db_path = tmp_path / "runtime.db"
    episodes_db = tmp_path / "episodes.db"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    episode_store = EpisodeStore(db_path=episodes_db)
    cand_store = CandidateStore(db_path=tmp_path / "candidates.db", episode_store=episode_store)
    registry = SkillRegistry(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    collector = ExperienceCollector(episode_store=episode_store, registry=registry)

    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=episode_store,
        candidate_store=cand_store,
    )

    yield {
        "runtime": runtime,
        "broker": broker,
        "collector": collector,
        "episode_store": episode_store,
        "cand_store": cand_store,
        "registry": registry,
        "tmp_path": tmp_path,
    }

    runtime.close()


def test_1_validate_candidate_with_pi_backend_isolated_clone_and_oracle_pass(eval_env):
    """Scenario 1: Full validate_candidate with PiEvaluationBackend, sandbox cloning, isolated bodies, and pass."""
    runtime: AgentRuntime = eval_env["runtime"]
    registry: SkillRegistry = eval_env["registry"]
    cand_store: CandidateStore = eval_env["cand_store"]
    episode_store: EpisodeStore = eval_env["episode_store"]

    order_id = "ORD_2026_0901"

    # Register baseline skill in formal registry via SKILL.md
    baseline_body = "# 1. Overview\nBaseline Logistics Instructions: detailed step-by-step guidance for tracking all orders and packages.\n## Constraints\n- Only query status, do not modify.\n# 2. Instructions\nQuery all packages for order and verify all tracking states.\n# 3. Examples\nExample 1: query all packages."
    base_meta = SkillMeta(
        name="logistics_query",
        version="1.0.0",
        description="物流包裹状态查询工具",
        use_when="用户查询订单物流状态时使用",
        dependencies=["query_order_packages", "query_package_tracking"],
        trigger=Trigger(keywords=["物流", "包裹", "订单"], not_for=["退款"]),
        not_for=["退款"],
    )
    skill_dir = eval_env["tmp_path"] / "skills" / "logistics_query"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"""---
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
    - 包裹
    - 订单
  not_for:
    - 退款
not_for:
  - 退款
---
{baseline_body}
""", encoding="utf-8")
    registry.load_skills_from_dir()

    # Candidate with updated body
    candidate_body = "# 1. Overview\nUpgraded concise instructions.\n## Constraints\n- Only query status, do not modify.\n# 2. Instructions\nQuery order packages and tracking.\n# 3. Examples\nExample 1."
    cand_meta = SkillMeta(
        name="logistics_query",
        version="1.1.0",
        description="物流包裹状态查询工具升级版",
        use_when="用户查询订单物流状态时使用",
        dependencies=["query_order_packages", "query_package_tracking"],
        trigger=Trigger(keywords=["物流", "包裹", "订单"], not_for=["退款"]),
        not_for=["退款"],
    )
    candidate = CandidateSkill(
        candidate_id="cand_logistics_v110",
        skill_name="logistics_query",
        decision="revise",
        meta=cand_meta,
        body=candidate_body,
        task_spec_hash="spec_logistics_01",
        source_requirement="Upgrade logistics multi-package tracking verification",
    )
    cand_store.save_candidate(candidate)

    # Program standard multi-turn logistics query responses via faux provider
    logistics_responses = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_eval_1",
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
                    "id": "tc_eval_2",
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
                    "type": "toolCall",
                    "id": "tc_eval_3",
                    "name": "query_package_tracking",
                    "arguments": {"package_id": "PKG_102"},
                }
            ],
            "stopReason": "toolUse",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Order {order_id} has 2 packages: PKG_101 is DELIVERED via Carrier_Alpha, "
                        f"and PKG_102 is IN_TRANSIT via Carrier_Beta."
                    ),
                }
            ],
            "stopReason": "stop",
        },
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": logistics_responses},
        test_mode=True,
        budget_max=10,
    )

    # Wrap backend.run with a non-invasive spy to record calls without altering behavior
    spy_calls: list[dict[str, Any]] = []
    orig_run = backend.run

    def spy_run(*args, **kwargs):
        res = orig_run(*args, **kwargs)
        spy_calls.append({
            "args": args,
            "kwargs": kwargs,
            "result": res,
        })
        return res

    backend.run = spy_run

    evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(role="execution"),
        judge_llm=FakeLLM(role="judge"),
        execution_backend=backend,
        scoring_policy="criteria_v1",
    )

    # Fingerprint check: Pi evaluator must produce a different fingerprint and config hash than legacy
    legacy_evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(role="execution"),
        judge_llm=FakeLLM(role="judge"),
        scoring_policy="criteria_v1",
    )
    assert evaluator.get_config_fingerprint() != legacy_evaluator.get_config_fingerprint()
    assert evaluator.get_config_hash() != legacy_evaluator.get_config_hash()

    eval_cases = [
        {
            "id": "case_logistics_01",
            "skill": "logistics_query",
            "query": f"请查询订单 {order_id} 的全部包裹与物流跟踪状态",
            "order_id": order_id,
            "oracle": "logistics",
        }
    ]

    val_record = validate_candidate(
        candidate=candidate,
        evaluator=evaluator,
        registry=registry,
        eval_cases=eval_cases,
        candidate_store=cand_store,
    )

    # 1. Candidate validation verdict & binding assertions
    assert val_record.ratchet_decision == "PASS"
    assert val_record.ratchet_verdict.decision == "PASS"
    assert val_record.candidate_id == candidate.candidate_id
    assert val_record.content_hash == compute_candidate_hash(candidate)
    assert val_record.config_hash == evaluator.get_config_hash()
    assert val_record.dataset_version is not None
    assert val_record.eval_result is not None
    assert val_record.eval_result.valid is True
    assert val_record.eval_result.critical_fail is False

    # Validation channels marked as pi_runtime/broker
    assert "pi_runtime" in val_record.eval_result.validation_channels or "broker" in val_record.eval_result.validation_channels

    # 2. Formal Registry remains untouched (not mutated or published)
    assert registry.get_body("logistics_query") == baseline_body
    assert registry.get_meta("logistics_query").version == "1.0.0"

    # 3. Spy assertions: baseline and candidate Pi runs had isolated bodies in provider inputs
    # Calls include: baseline bare, baseline with_skill, candidate bare, candidate with_skill
    with_skill_calls = [
        c for c in spy_calls
        if c["kwargs"].get("body") or (len(c["args"]) > 1 and c["args"][1])
    ]
    assert len(with_skill_calls) >= 2

    baseline_call = next(
        c for c in with_skill_calls
        if (c["kwargs"].get("body") == baseline_body or (len(c["args"]) > 1 and c["args"][1] == baseline_body))
    )
    candidate_call = next(
        c for c in with_skill_calls
        if (c["kwargs"].get("body") == candidate_body or (len(c["args"]) > 1 and c["args"][1] == candidate_body))
    )

    # Verify captured provider inputs
    b_res: PiEvaluationRunResult = baseline_call["result"]
    c_res: PiEvaluationRunResult = candidate_call["result"]

    assert b_res.execution_result is not None
    assert c_res.execution_result is not None
    assert len(b_res.execution_result.captured_provider_inputs) > 0
    assert len(c_res.execution_result.captured_provider_inputs) > 0

    assert baseline_body in b_res.execution_result.captured_provider_inputs[0]["systemPrompt"]
    assert candidate_body not in b_res.execution_result.captured_provider_inputs[0]["systemPrompt"]

    assert candidate_body in c_res.execution_result.captured_provider_inputs[0]["systemPrompt"]
    assert baseline_body not in c_res.execution_result.captured_provider_inputs[0]["systemPrompt"]

    # 4. Runtime & Episode assertions
    assert len(c_res.provenances) == 3
    tool_names = [p.tool_name for p in c_res.provenances]
    assert tool_names == ["query_order_packages", "query_package_tracking", "query_package_tracking"]

    # Check child run Episodes
    child_ep = episode_store.get_episode(f"ep_{c_res.run_id}")
    assert child_ep is not None
    assert child_ep.environment.get("purpose") == "evaluation"
    # Child run finalized as unknown pending business oracle
    assert child_ep.outcome == "unknown"

    # 5. Missing usage reports null tokens, not pseudo 0
    assert c_res.metrics["tokens"] is None
    assert val_record.eval_result.objective_metrics.get("avg_tokens_skill") is None
    assert c_res.metrics["money"] is None


def test_2_false_claim_all_delivered_blocked_by_logistics_code_oracle(eval_env):
    """Scenario 2: Erroneous output claiming '全部签收' while PKG_102 is IN_TRANSIT.

    Even if Judge LLM is lenient / returns PASS, the existing logistics code oracle
    must place critical_fail on ROBUST_EVIDENCE_FAITHFUL, causing ratchet to DECLINE.
    """
    runtime: AgentRuntime = eval_env["runtime"]
    registry: SkillRegistry = eval_env["registry"]
    cand_store: CandidateStore = eval_env["cand_store"]

    order_id = "ORD_2026_0901"

    base_body = "# 1. Overview\nBaseline.\n## Constraints\n- Only query status, do not modify.\n# 2. Instructions\nQuery order.\n# 3. Examples\nExample."
    base_meta = SkillMeta(
        name="logistics_query",
        version="1.0.0",
        description="物流包裹状态查询工具",
        use_when="查询订单物流状态时使用",
        dependencies=["query_order_packages", "query_package_tracking"],
        trigger=Trigger(keywords=["物流", "包裹", "订单"], not_for=["退款"]),
        not_for=["退款"],
    )
    skill_dir = eval_env["tmp_path"] / "skills" / "logistics_query"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"""---
name: logistics_query
version: 1.0.0
description: 物流包裹状态查询工具
use_when: 查询订单物流状态时使用
dependencies:
  - query_order_packages
  - query_package_tracking
trigger:
  keywords:
    - 物流
    - 包裹
    - 订单
  not_for:
    - 退款
not_for:
  - 退款
---
{base_body}
""", encoding="utf-8")
    registry.load_skills_from_dir()

    cand_body = "# 1. Overview\nFaulty Candidate.\n## Constraints\n- Only query status, do not modify.\n# 2. Instructions\nQuery order.\n# 3. Examples\nExample."
    cand_meta = SkillMeta(
        name="logistics_query",
        version="1.1.0",
        description="物流包裹状态查询工具缺陷版",
        use_when="查询订单物流状态时使用",
        dependencies=["query_order_packages", "query_package_tracking"],
        trigger=Trigger(keywords=["物流", "包裹", "订单"], not_for=["退款"]),
        not_for=["退款"],
    )
    candidate = CandidateSkill(
        candidate_id="cand_logistics_faulty",
        skill_name="logistics_query",
        decision="revise",
        meta=cand_meta,
        body=cand_body,
        task_spec_hash="spec_logistics_faulty",
        source_requirement="Testing logistics false claim rejection",
    )
    cand_store.save_candidate(candidate)

    # Erroneous model output: claims all packages are delivered and skips querying PKG_102
    faulty_responses = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_f_1",
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
                    "id": "tc_f_2",
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
                    "text": f"订单 {order_id} 的全部包裹已全部签收，所有物品均已妥投完毕。",
                }
            ],
            "stopReason": "stop",
        },
    ]

    backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages", "query_package_tracking"],
        model_config={"provider": "faux", "responses": faulty_responses},
        test_mode=True,
        budget_max=10,
    )

    # Judge LLM is lenient (always returns PASS/tied)
    lenient_judge = FakeLLM(role="judge")

    evaluator = SkillEvaluator(
        registry=registry,
        llm=FakeLLM(role="execution"),
        judge_llm=lenient_judge,
        execution_backend=backend,
        scoring_policy="criteria_v1",
    )

    eval_cases = [
        {
            "id": "case_logistics_01",
            "skill": "logistics_query",
            "query": f"请查询订单 {order_id} 的物流状态",
            "order_id": order_id,
            "oracle": "logistics",
        }
    ]

    val_record = validate_candidate(
        candidate=candidate,
        evaluator=evaluator,
        registry=registry,
        eval_cases=eval_cases,
        candidate_store=cand_store,
    )

    # Logistics code oracle must detect false delivery claim and missing PKG_102
    assert val_record.ratchet_decision == "DECLINED"
    assert val_record.ratchet_verdict.decision == "DECLINED"
    assert val_record.eval_result is not None
    assert val_record.eval_result.critical_fail is True
    assert val_record.promoted is False

    # Formal registry must remain at baseline
    assert registry.get_meta("logistics_query").version == "1.0.0"


def test_3_production_mode_rejects_faux_and_scripts(eval_env):
    """Scenario 3: Production mode (test_mode=False) strictly rejects faux provider."""
    runtime: AgentRuntime = eval_env["runtime"]

    # Faux provider rejected in production mode
    with pytest.raises(ValueError, match="Faux provider is restricted to explicit test mode"):
        PiEvaluationBackend(
            runtime=runtime,
            model_config={"provider": "faux"},
            test_mode=False,
        )

    # Pre-written response scripts rejected in production mode
    with pytest.raises(ValueError, match="Pre-written response scripts are restricted to explicit test mode"):
        PiEvaluationBackend(
            runtime=runtime,
            model_config={"provider": "anthropic", "responses": [{"role": "assistant", "content": []}]},
            test_mode=False,
        )

    # Missing usage produces tokens=None, never pseudo 0
    missing_usage_backend = PiEvaluationBackend(
        runtime=runtime,
        model_config={
            "provider": "faux",
            "responses": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "missing usage text"}],
                    "usage": None,
                }
            ],
        },
        test_mode=True,
    )
    mu_res = missing_usage_backend.run(query="test missing usage")
    assert mu_res.metrics["tokens"] is None
    assert mu_res.metrics["money"] is None

    # Multi-turn usage aggregation: aggregates all assistant turns, not just the last one
    multi_usage_backend = PiEvaluationBackend(
        runtime=runtime,
        tool_names=["query_order_packages"],
        model_config={
            "provider": "faux",
            "responses": [
                {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "tc_u1", "name": "query_order_packages", "arguments": {"order_id": "ORD_2026_0901"}}],
                    "stopReason": "toolUse",
                    "usage": {"total_tokens": 120, "input": 100, "output": 20},
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                    "usage": {"total_tokens": 80, "input": 60, "output": 20},
                },
            ],
        },
        test_mode=True,
    )
    multi_res = multi_usage_backend.run(query="test multi usage")
    # Pi faux computes its own deterministic usage; aggregate the actual SDK
    # messages, including cache tokens, rather than assuming fixture usage wins.
    assistant_messages = [m for m in multi_res.execution_result.messages if m.get("role") == "assistant"]
    assert multi_res.metrics["tokens"] == sum(m["usage"]["totalTokens"] for m in assistant_messages)
    assert multi_res.metrics["tokens"] > assistant_messages[-1]["usage"]["totalTokens"]
    assert multi_res.metrics["turns"] == 2.0
    original_fp = multi_usage_backend.get_config_fingerprint()
    multi_usage_backend.system_prompt_header = "Status only; do not suggest actions."
    assert multi_usage_backend.get_config_fingerprint() != original_fp
    default_tools = PiEvaluationBackend(runtime=runtime, tool_names=None, model_config={"provider": "faux"}, test_mode=True)
    no_tools = PiEvaluationBackend(runtime=runtime, tool_names=[], model_config={"provider": "faux"}, test_mode=True)
    assert default_tools.get_config_fingerprint() != no_tools.get_config_fingerprint()
