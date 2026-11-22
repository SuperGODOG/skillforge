"""Phase 4A2b Integration Tests: CLI evolve explicit Pi backend assembly, candidate execution, and shared ledger.

Verifies:
1. test_pi_cli_evolve_candidate_pi_execution_and_isolation:
   - Command invoked via actual parser and main() handler.
   - Real official Pi runner subprocess spawned with real ToolBroker and AgentRuntime.
   - Baseline evaluation runs on Pi and records failure; evolver generates candidate patch.
   - Sandbox candidate temporary clone preserves execution_backend, runtime, and tool_broker.
   - Candidate new text reaches Pi prompt during candidate evaluation.
   - Formal registry and skill file remain untouched and unmutated (shadow_mode=True).
   - Pi runner subprocesses cleanly reaped (ProcessLookupError on kill).
   - Console identifies backend=pi and synthetic scenario (logistics (synthetic)).
   - Exit code is 0.
2. test_pi_cli_evolve_shared_ledger_exhaustion_blocks_pi:
   - Top-level LLMLedger shared across Pi model turns, evolver patcher, and Judge.
   - Exceeded call budget causes subsequent Pi turn admission to be denied without invoking provider.
   - Budget exhaustion is recorded as an error and CLI returns non-zero (1), not replacing it with success.
3. test_pi_cli_evolve_legacy_default_and_upfront_rejections:
   - Default --execution-backend legacy (or omitted) does not spawn Pi runner.
   - Legacy passing Pi parameters fails with exit code 2.
   - Pi mode missing --pi-provider, --pi-model, or --pi-scenario fails with exit code 2 upfront.
   - Unsupported scenario fails with exit code 2 upfront.
   - Skill declaring unauthorized dependencies fails with exit code 2 upfront.
   - Negative or zero budget flags (--max-calls 0, --deadline-seconds 0) fail with exit code 2 upfront.
   - All upfront failures exit before runtime DB creation or model calls.
4. test_pi_cli_evolve_cleanup_and_fail_closed_instantiation:
   - Unexpected exception during evolution triggers cleanup of ReleaseStateMachine, registry, and Pi runner, exiting 1.
   - _instantiate_temp_evaluator with active execution_backend fails closed on instantiation error without fallback to legacy.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skillforge.cli import main
from skillforge.episode import EpisodeStore
from skillforge.evolver import _instantiate_temp_evaluator
from skillforge.models import EvolveBudget
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime
from skillforge.scenarios.logistics import MOCK_PACKAGES


def _judge_json(verdict: str = "tied") -> str:
    return json.dumps({
        "verdict": verdict,
        "reason_codes": ["OK_READABILITY"],
        "evidence_summary": f"Pairwise verdict is {verdict}.",
    })


class FakeEvolveLLM:
    """Mock LLM handling Judge evaluations, root cause analysis, and candidate patch generation."""

    def __init__(self, role: str = "patcher", model: str = "test-model"):
        self.role = role
        self.model = model
        self.calls: list[list[dict]] = []
        self.findings_calls: int = 0
        self.judge_calls: int = 0

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        content = _judge_json("tied")

        # 1. Patcher: Root Cause Analysis
        if messages and any("判定根因概率" in str(m.get("content", "")) for m in messages if isinstance(m, dict)):
            content = json.dumps({
                "prompt_vague": {
                    "prob": 0.85,
                    "why": "Instructions need explicit step to query tracking for each package",
                }
            })
            return SimpleNamespace(content=content, usage=None)

        # 2. Patcher: Candidate Patch Generation
        if messages and any("改动分级规则" in str(m.get("content", "")) or "你是 SkillForge 的元 Agent" in str(m.get("content", "")) for m in messages if isinstance(m, dict)):
            candidate_md = (
                "---\n"
                "name: logistics_query\n"
                "version: 1.0.1\n"
                "description: 物流包裹状态查询工具\n"
                "use_when: 用户查询订单物流状态时使用\n"
                "not_for:\n"
                "  - 退款\n"
                "dependencies:\n"
                "  - query_order_packages\n"
                "  - query_package_tracking\n"
                "trigger:\n"
                "  keywords:\n"
                "    - 物流\n"
                "    - 包裹\n"
                "    - 订单\n"
                "  not_for:\n"
                "    - 退款\n"
                "---\n"
                "# 1. Overview\n"
                "Candidate Logistics Instructions with marker: CANDIDATE_BODY_PI_MARKER_4A2B\n"
                "## Constraints\n"
                "- Only query status, do not modify.\n"
                "# 2. Instructions\n"
                "Query order packages and verify each package tracking state.\n"
                "# 3. Examples\n"
                "Example 1: query all packages."
            )
            content = json.dumps([
                {
                    "level": "L2",
                    "rationale": "Explicitly instruct to query each package tracking",
                    "new_skill_md": candidate_md,
                }
            ])
            return SimpleNamespace(content=content, usage=None)

        # 3. Judge: Criteria findings
        if messages and any("findings" in str(m.get("content", "")) for m in messages if isinstance(m, dict)):
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
            return SimpleNamespace(content=content, usage=None)

        # Readability stays neutral; the business Oracle creates the failure.
        self.judge_calls += 1
        return SimpleNamespace(content=_judge_json("tied"), usage=None)



@pytest.fixture
def cli_evolve_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sets up isolated repo environment with logistics skill and repair evaluation set."""
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "status", "IN_TRANSIT")
    monkeypatch.setitem(MOCK_PACKAGES["PKG_102"], "carrier", "Carrier_Beta")

    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "logistics_query"
    skill_dir.mkdir(parents=True, exist_ok=True)

    baseline_body = (
        "# 1. Overview\n"
        "Baseline Logistics Instructions: query order packages and track every package.\n"
        "## Constraints\n"
        "- Only query status, do not modify.\n"
        "# 2. Instructions\n"
        "Query order packages and verify each package tracking state.\n"
        "# 3. Examples\n"
        "Example 1: query all packages."
    )

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

    eval_sets_dir = tmp_path / "evaluation_sets"
    eval_sets_dir.mkdir(parents=True, exist_ok=True)
    eval_cases = {
        "meta": {"name": "test_logistics_repair", "total": 1},
        "cases": [
            {
                "id": "case_repair_01",
                "skill": "logistics_query",
                "query": "请查询订单 ORD_2026_0901 的全部包裹与物流跟踪状态",
                "order_id": "ORD_2026_0901",
                "oracle": "logistics",
            }
        ],
    }
    (eval_sets_dir / "repair_set.json").write_text(
        json.dumps(eval_cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (eval_sets_dir / "baseline_dev.json").write_text(
        json.dumps(eval_cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (eval_sets_dir / "p0_cases.json").write_text(
        json.dumps({"p0_ids": ["case_repair_01"]}, indent=2), encoding="utf-8"
    )
    (eval_sets_dir / "final_audit.json").write_text(
        json.dumps(eval_cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fake_patcher_llm = FakeEvolveLLM(role="patcher")
    fake_judge_llm = FakeEvolveLLM(role="judge")
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_llm_pair",
        lambda *a, **kw: (fake_patcher_llm, fake_judge_llm),
    )
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_judge_llm",
        lambda *a, **kw: fake_judge_llm,
    )
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_execution_llm",
        lambda *a, **kw: fake_patcher_llm,
    )

    yield {
        "repo_root": tmp_path,
        "baseline_body": baseline_body,
        "fake_patcher_llm": fake_patcher_llm,
        "fake_judge_llm": fake_judge_llm,
    }


def _get_standard_logistics_responses() -> list[dict[str, Any]]:
    order_id = "ORD_2026_0901"
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_evo_1",
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
                    "id": "tc_evo_2",
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
                    "id": "tc_evo_3",
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


def test_pi_cli_evolve_candidate_pi_execution_and_isolation(cli_evolve_env, monkeypatch, capsys):
    """Test 1: Candidate temporary clone executes real Pi runner with new body while formal skill remains untouched."""
    repo_root: Path = cli_evolve_env["repo_root"]
    baseline_body: str = cli_evolve_env["baseline_body"]

    responses = _get_standard_logistics_responses()
    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(responses)},
    )

    spawned_pids: list[int] = []
    candidate_bodies_seen: list[str] = []
    orig_start_run = AgentRuntime.run_agent_with_pi

    def track_pi_calls(self, *args, **kwargs):
        run_id = args[0] if args else kwargs.get("run_id")
        cbody = self._run_candidate_bodies.get(run_id, "")
        if cbody:
            candidate_bodies_seen.append(cbody)
        # Only the initial skill response is wrong. Bare and repaired runs retain
        # the same tools and return the correct package states.
        if cbody and "CANDIDATE_BODY_PI_MARKER_4A2B" not in cbody:
            kwargs["model_config"] = copy.deepcopy(kwargs["model_config"])
            kwargs["model_config"]["responses"][-1]["content"][0]["text"] = (
                "订单 ORD_2026_0901 的 PKG_101 和 PKG_102 全部签收。"
            )
        res = orig_start_run(self, *args, **kwargs)
        pid = self.get_last_pi_pid(run_id)
        if pid:
            spawned_pids.append(pid)
        return res

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", track_pi_calls)

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evolve",
        "--skill",
        "logistics_query",
        "--eval-set",
        "repair_set",
        "--execution-backend",
        "pi",
        "--pi-provider",
        "faux",
        "--pi-model",
        "test-haiku",
        "--pi-scenario",
        "logistics",
        "--max-candidates",
        "1",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    # 1. Exit code is 0
    assert exit_code == 0, f"Expected 0, got {exit_code}.\nStdout: {captured.out}\nStderr: {captured.err}"

    # 2. Console reports backend=pi and synthetic scenario
    assert "backend=pi" in captured.out
    assert "logistics (synthetic)" in captured.out
    assert "启动 Pi 执行后端" in captured.out

    # 3. Candidate clone executed real Pi runner with new body containing marker
    assert any("CANDIDATE_BODY_PI_MARKER_4A2B" in b for b in candidate_bodies_seen), (
        f"Candidate body marker not found in Pi runs. Seen: {candidate_bodies_seen}"
    )

    # 4. Formal registry and file remain completely unmutated (shadow_mode=True, auto_publish=False)
    reg = SkillRegistry(db_path=repo_root / "runs" / "skillforge.db", skills_dir=repo_root / "skills")
    try:
        reg.load_skills_from_dir()
        formal_body = reg.get_body("logistics_query")
        assert "CANDIDATE_BODY_PI_MARKER_4A2B" not in formal_body
        assert formal_body == baseline_body
        assert reg.get_meta("logistics_query").version == "1.0.0"
    finally:
        reg.close()

    skill_file = repo_root / "skills" / "logistics_query" / "SKILL.md"
    content = skill_file.read_text(encoding="utf-8")
    assert "CANDIDATE_BODY_PI_MARKER_4A2B" not in content
    assert "1.0.0" in content

    # 5. Spawned Pi runner processes were cleanly reaped
    assert len(spawned_pids) > 0
    for pid in spawned_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_pi_cli_evolve_shared_ledger_exhaustion_blocks_pi(cli_evolve_env, monkeypatch, capsys):
    """Test 2: Shared ledger across Pi, Patcher, and Judge rejects subsequent Pi turn without triggering provider."""
    repo_root: Path = cli_evolve_env["repo_root"]

    responses = _get_standard_logistics_responses()
    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(responses)},
    )

    from skillforge.evolver import SkillEvolver
    from skillforge.pi_evaluation import PiEvaluationBackend
    bound_ledgers = []
    pi_runs = []
    original_bind = SkillEvolver._bind_ledger
    original_run = PiEvaluationBackend.run

    def observe_bind(self, ledger):
        original_bind(self, ledger)
        assert self.llm.ledger is ledger
        assert self.evaluator.judge.llm.ledger is ledger
        assert self.evaluator.execution_backend.ledger is ledger
        bound_ledgers.append(ledger)

    def observe_run(self, *args, **kwargs):
        try:
            return original_run(self, *args, **kwargs)
        finally:
            pi_runs.extend(self.runtime._last_pi_results.values())

    monkeypatch.setattr(SkillEvolver, "_bind_ledger", observe_bind)
    monkeypatch.setattr(PiEvaluationBackend, "run", observe_run)

    # Tight budget: only 1 call allowed across all components
    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evolve",
        "--skill",
        "logistics_query",
        "--eval-set",
        "repair_set",
        "--execution-backend",
        "pi",
        "--pi-provider",
        "faux",
        "--pi-model",
        "test-haiku",
        "--pi-scenario",
        "logistics",
        "--max-calls",
        "1",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    # Budget exhaustion must cause non-zero exit (1), never substituted with success
    assert exit_code == 1, f"Expected 1 on budget exhaustion, got {exit_code}.\nStdout: {captured.out}"
    assert "超限" in captured.out or "BUDGET" in captured.out or "error:" in captured.out
    assert bound_ledgers and bound_ledgers[-1].total_calls == 1
    assert pi_runs
    assert sum(len(r.captured_provider_inputs) for r in pi_runs) == 1
    assert not cli_evolve_env["fake_patcher_llm"].calls
    assert not cli_evolve_env["fake_judge_llm"].calls


def test_pi_cli_evolve_legacy_default_and_upfront_rejections(cli_evolve_env, monkeypatch, capsys):
    """Test 3: Legacy default does not start Pi, and invalid/missing flags fail upfront before DB creation."""
    repo_root: Path = cli_evolve_env["repo_root"]
    runs_dir = repo_root / "runs"

    pi_called = []
    orig_start_run = AgentRuntime.run_agent_with_pi

    def track_pi(self, *a, **kw):
        pi_called.append(True)
        return orig_start_run(self, *a, **kw)

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", track_pi)

    # Subtest A: Legacy default does not launch Pi
    cmd_legacy = [
        "skillforge",
        "--root",
        str(repo_root),
        "evolve",
        "--skill",
        "logistics_query",
        "--eval-set",
        "repair_set",
    ]
    monkeypatch.setattr(sys, "argv", cmd_legacy)
    exit_code = main()
    captured = capsys.readouterr()

    assert "backend=legacy" in captured.out
    assert "backend=pi" not in captured.out
    assert len(pi_called) == 0

    # Subtest B: Legacy passing Pi parameters rejected upfront with exit 2
    monkeypatch.setattr(sys, "argv", cmd_legacy + ["--pi-provider", "anthropic"])
    assert main() == 2
    assert "Pi parameters require --execution-backend pi" in capsys.readouterr().out
    assert len(pi_called) == 0

    # Clean runs_dir for upfront check tests
    if runs_dir.exists():
        import shutil
        shutil.rmtree(runs_dir)

    # Subtest C1: Pi missing --pi-provider fails exit 2 before DB creation
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-model", "test-haiku", "--pi-scenario", "logistics",
    ])
    assert main() == 2
    assert "--pi-provider" in capsys.readouterr().out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest C2: Pi missing --pi-model fails exit 2 before DB creation
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-scenario", "logistics",
    ])
    assert main() == 2
    assert "--pi-model" in capsys.readouterr().out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest C3: Pi missing --pi-scenario fails exit 2 before DB creation
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
    ])
    assert main() == 2
    assert "--pi-scenario" in capsys.readouterr().out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest D: Unsupported scenario fails exit 2 before DB creation
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
        "--pi-scenario", "unsupported_scenario_xyz",
    ])
    assert main() == 2
    assert "Unsupported" in capsys.readouterr().out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest E: Incompatible skill dependencies fail exit 2 before DB creation
    bad_skill_dir = repo_root / "skills" / "unauthorized_skill"
    bad_skill_dir.mkdir(parents=True, exist_ok=True)
    (bad_skill_dir / "SKILL.md").write_text("""---
name: unauthorized_skill
version: 1.0.0
description: Skill with unauthorized tools
dependencies:
  - query_order_packages
  - unauthorized_payment_gateway
---
# Overview
Unauthorized tool test.
""", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "unauthorized_skill", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
        "--pi-scenario", "logistics",
    ])
    assert main() == 2
    cap_e = capsys.readouterr()
    assert "Incompatible dependencies" in cap_e.out
    assert "unauthorized_payment_gateway" in cap_e.out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest F: Non-positive budget flags fail upfront with exit 2
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
        "--pi-scenario", "logistics", "--max-calls", "0",
    ])
    assert main() == 2
    assert "--max-calls must be a positive integer" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evolve",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
        "--pi-scenario", "logistics", "--deadline-seconds", "-5",
    ])
    assert main() == 2
    assert "--deadline-seconds must be a positive number" in capsys.readouterr().out


def test_pi_cli_evolve_cleanup_and_fail_closed_instantiation(cli_evolve_env, monkeypatch, capsys):
    """Test 4: Exception cleanup frees resources and active Pi clone construction cannot silently fall back."""
    repo_root: Path = cli_evolve_env["repo_root"]

    responses = _get_standard_logistics_responses()
    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(responses)},
    )

    # Subtest A: Unexpected exception during evolution triggers clean exit 1
    from skillforge.evolver import SkillEvolver

    def broken_evolve_full(*a, **kw):
        raise RuntimeError("Simulated crash during evolution loop")

    monkeypatch.setattr(SkillEvolver, "evolve_full", broken_evolve_full)

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evolve",
        "--skill",
        "logistics_query",
        "--eval-set",
        "repair_set",
        "--execution-backend",
        "pi",
        "--pi-provider",
        "faux",
        "--pi-model",
        "test-haiku",
        "--pi-scenario",
        "logistics",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Pi 演进执行失败" in captured.out
    assert "Simulated crash during evolution loop" in captured.out

    # Subtest B: Active Pi evaluator instantiation MUST fail-closed on constructor error
    # Must NOT silently fall back to legacy constructor
    class MockEvaluator:
        def __init__(self, execution_backend=None, runtime=None, tool_broker=None, **kwargs):
            self.execution_backend = execution_backend or "active_pi_backend"
            self.runtime = runtime
            self.tool_broker = tool_broker
            self.output_cache = None
            self.router = None
            self.judge_llm = None
            self.llm = None

    class BrokenSkillEvaluator:
        def __init__(self, **kwargs):
            if "execution_backend" in kwargs:
                raise TypeError("Simulated active backend constructor error")

    monkeypatch.setattr("skillforge.evaluator.SkillEvaluator", BrokenSkillEvaluator)

    mock_eval = MockEvaluator()
    mock_reg = SimpleNamespace()

    with pytest.raises(RuntimeError) as exc_info:
        _instantiate_temp_evaluator(mock_reg, mock_eval)

    assert "Failed to instantiate isolated evaluator with active execution backend" in str(exc_info.value)
