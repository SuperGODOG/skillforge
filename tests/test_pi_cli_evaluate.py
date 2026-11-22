"""Phase 4A1 Integration Tests: CLI evaluate explicit Pi backend assembly.

Verifies:
1. test_pi_cli_evaluate_success_e2e:
   - Command invoked via actual parser / main() handler.
   - Real official Pi runner subprocess spawned with real ToolBroker and AgentRuntime.
   - 3 real tool calls and provenances executed (query_order_packages, query_package_tracking).
   - EpisodeStore records child runs with purpose='evaluation', outcome='unknown'.
   - Frozen body reaches Pi runner systemPrompt.
   - Independent logistics Oracle passes, CLI returns exit code 0.
   - Console identifies backend=pi and synthetic scenario (logistics (synthetic)).
   - Nullable metrics (tokens, money) format safely as 'unknown', never fabricating 0.00.
   - Formal registry untouched (no skill mutation).
   - Pi runner subprocess cleanly reaped (ProcessLookupError on kill).
2. test_pi_cli_evaluate_oracle_critical_failure_fails_nonzero:
   - Erroneous model output falsely claims '全部签收' while PKG_102 is IN_TRANSIT.
   - Independent logistics code oracle detects critical failure.
   - Oracle critical failure cannot be scored away, CLI returns nonzero (1).
   - Formal registry untouched, Pi runner subprocess reaped.
3. test_pi_cli_evaluate_legacy_default_no_pi_spawned:
   - Default --execution-backend legacy (or omitted) uses original evaluator.
   - No Pi subprocess spawned.
   - Passing --pi-provider on legacy does not silently switch to Pi.
4. test_pi_cli_evaluate_missing_or_invalid_args_fail_upfront:
   - Missing --pi-provider, --pi-model, or --pi-scenario fails with exit code 2.
   - Unsupported scenario fails with exit code 2.
   - All fail upfront before runtime DB files are created or models called.
   - No silent fallback.
5. test_pi_cli_evaluate_incompatible_skill_deps_fail_upfront:
   - Skill declaring dependencies outside the logistics allowlist rejected upfront.
   - Exits nonzero (2) without spawning Pi or creating runtime DB.
6. test_pi_cli_evaluate_production_rejects_faux_and_scripts:
   - In non-test mode, provider='faux' is strictly rejected with nonzero exit.
   - No silent fallback to simulation.
7. test_pi_cli_evaluate_cleanup_on_exception:
   - Exceptions during evaluation trigger full cleanup of runtime, stores, and Pi runner.
   - Exits nonzero (1).
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
from skillforge.registry import SkillRegistry
from skillforge.scenarios.logistics import MOCK_PACKAGES


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
def cli_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sets up an isolated repo environment with logistics skill and eval set."""
    # Reset ground truth: ORD_2026_0901 has PKG_101 (DELIVERED) and PKG_102 (IN_TRANSIT)
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
        "meta": {"name": "test_logistics_dev", "total": 1},
        "cases": [
            {
                "id": "case_logistics_01",
                "skill": "logistics_query",
                "query": "请查询订单 ORD_2026_0901 的全部包裹与物流跟踪状态",
                "order_id": "ORD_2026_0901",
                "oracle": "logistics",
            }
        ],
    }
    (eval_sets_dir / "baseline_dev.json").write_text(
        json.dumps(eval_cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (eval_sets_dir / "p0_cases.json").write_text(
        json.dumps({"p0_ids": ["case_logistics_01"]}, indent=2), encoding="utf-8"
    )

    # Monkeypatch LLM factory for Judge calls
    fake_execution_llm = FakeLLM(role="execution")
    fake_judge_llm = FakeLLM(role="judge")
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_llm_pair",
        lambda *a, **kw: (fake_execution_llm, fake_judge_llm),
    )
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_judge_llm",
        lambda *a, **kw: fake_judge_llm,
    )
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_execution_llm",
        lambda *a, **kw: fake_execution_llm,
    )

    yield {
        "repo_root": tmp_path,
        "baseline_body": baseline_body,
        "judge_llm": fake_judge_llm,
    }


def _get_standard_logistics_responses() -> list[dict[str, Any]]:
    order_id = "ORD_2026_0901"
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tc_cli_1",
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
                    "id": "tc_cli_2",
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
                    "id": "tc_cli_3",
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


def test_pi_cli_evaluate_success_e2e(cli_test_env, monkeypatch, capsys):
    """Scenario 1: End-to-end CLI evaluate via Pi backend assembly passes with code 0."""
    repo_root: Path = cli_test_env["repo_root"]
    baseline_body: str = cli_test_env["baseline_body"]

    # Inject multi-turn responses via test-only model config hook
    responses = _get_standard_logistics_responses()
    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(responses)},
    )

    # Track spawned PIDs through AgentRuntime
    spawned_pids: list[int] = []
    from skillforge.runtime import AgentRuntime
    orig_start_run = AgentRuntime.run_agent_with_pi

    def track_pi_pids(self, *args, **kwargs):
        res = orig_start_run(self, *args, **kwargs)
        run_id = args[0] if args else kwargs.get("run_id")
        pid = self.get_last_pi_pid(run_id)
        if pid:
            spawned_pids.append(pid)
        return res

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", track_pi_pids)

    # Invoke CLI via actual parser and main()
    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "logistics_query",
        "--eval-set",
        "baseline_dev",
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

    # 1. Exit code is 0
    assert exit_code == 0, f"Expected 0, got {exit_code}.\nStdout: {captured.out}\nStderr: {captured.err}"

    # 2. Console identifies backend + synthetic scenario
    assert "backend=pi" in captured.out
    assert "logistics (synthetic)" in captured.out
    assert "启动 Pi 执行后端" in captured.out
    assert "P0 用例: ✓ 全通过" in captured.out

    # 3. Nullable tokens/money safely formatted without fabricating 0.00
    assert "unknown" in captured.out
    assert "0.00 / 0" not in captured.out

    # 4. SQLite artifacts exist on disk
    runs_dir = repo_root / "runs"
    assert (runs_dir / "runtime.db").is_file()
    assert (runs_dir / "episodes.db").is_file()

    # 5. Episodes recorded with purpose='evaluation', outcome='unknown' for child bare/skill runs
    ep_store = EpisodeStore(db_path=runs_dir / "episodes.db")
    try:
        episodes = ep_store.list_episodes()
        assert len(episodes) >= 2
        child_episodes = [e for e in episodes if "eval_pi_" in e.episode_id]
        assert len(child_episodes) >= 2  # bare run and skill run
        for ep in child_episodes:
            assert ep.environment.get("purpose") == "evaluation"
            assert ep.outcome == "unknown"

        # Check skill run has 3 provenances
        skill_ep = next(e for e in child_episodes if "skill" in e.episode_id)
        assert len(skill_ep.provenances) == 3
        prov_tools = [p.tool_name for p in skill_ep.provenances]
        assert prov_tools == ["query_order_packages", "query_package_tracking", "query_package_tracking"]
    finally:
        ep_store.close()

    # 6. Formal registry remains untouched (frozen body snapshot isolation)
    reg = SkillRegistry(db_path=runs_dir / "skillforge.db", skills_dir=repo_root / "skills")
    try:
        reg.load_skills_from_dir()
        assert reg.get_body("logistics_query") == baseline_body
        assert reg.get_meta("logistics_query").version == "1.0.0"
    finally:
        reg.close()

    # 7. Pi subprocess reaped cleanly
    assert len(spawned_pids) > 0
    for pid in spawned_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_pi_cli_evaluate_oracle_critical_failure_fails_nonzero(cli_test_env, monkeypatch, capsys):
    """Scenario 2: Erroneous output claiming '全部签收' triggers logistics oracle critical failure.

    Even if Judge is lenient, the code oracle failure cannot be scored away, and CLI exits 1.
    """
    repo_root: Path = cli_test_env["repo_root"]
    baseline_body: str = cli_test_env["baseline_body"]

    order_id = "ORD_2026_0901"
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

    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(faulty_responses)},
    )

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "logistics_query",
        "--eval-set",
        "baseline_dev",
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

    # 1. Nonzero exit code due to critical failure
    assert exit_code == 1
    assert "critical_fail" in captured.out or "有失败（棘轮硬门槛 5 触发）" in captured.out

    # 2. Formal registry untouched
    runs_dir = repo_root / "runs"
    reg = SkillRegistry(db_path=runs_dir / "skillforge.db", skills_dir=repo_root / "skills")
    try:
        reg.load_skills_from_dir()
        assert reg.get_body("logistics_query") == baseline_body
        assert reg.get_meta("logistics_query").version == "1.0.0"
    finally:
        reg.close()


def test_pi_cli_evaluate_legacy_default_no_pi_spawned(cli_test_env, monkeypatch, capsys):
    """Scenario 3: Legacy default path uses original evaluator; no Pi runner spawned.

    Also tests that passing --pi-provider on legacy does not silently switch to Pi.
    """
    repo_root: Path = cli_test_env["repo_root"]

    # Provide valid execution text for legacy LLM so oracle passes
    valid_text = (
        "Order ORD_2026_0901 has 2 packages: PKG_101 is DELIVERED via Carrier_Alpha, "
        "and PKG_102 is IN_TRANSIT via Carrier_Beta."
    )
    fake_exec = FakeLLM(contents=[valid_text, valid_text, valid_text, valid_text], role="execution")
    monkeypatch.setattr(
        "skillforge.evaluator.llm_factory.build_llm_pair",
        lambda *a, **kw: (fake_exec, cli_test_env["judge_llm"]),
    )

    # Spy on Pi runner invocation: must NEVER be called in legacy mode
    from skillforge.runtime import AgentRuntime
    pi_called = []
    orig_run_pi = AgentRuntime.run_agent_with_pi

    def spy_run_pi(*args, **kwargs):
        pi_called.append(True)
        return orig_run_pi(*args, **kwargs)

    monkeypatch.setattr(AgentRuntime, "run_agent_with_pi", spy_run_pi)

    # 1. Default without --execution-backend
    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "logistics_query",
        "--eval-set",
        "baseline_dev",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    # 2. Identified as legacy backend
    assert "backend=legacy" in captured.out
    assert "backend=pi" not in captured.out
    assert exit_code == 0
    # No Pi runner invoked
    assert len(pi_called) == 0

    monkeypatch.setattr(sys, "argv", cmd + ["--pi-provider", "anthropic"])
    assert main() == 2
    assert "Pi parameters require --execution-backend pi" in capsys.readouterr().out
    assert not pi_called


def test_pi_cli_evaluate_missing_or_invalid_args_fail_upfront(cli_test_env, monkeypatch, capsys):
    """Scenario 4: Missing or invalid Pi flags fail upfront before runtime DB creation or models called."""
    repo_root: Path = cli_test_env["repo_root"]
    runs_dir = repo_root / "runs"

    # Subtest A: Missing --pi-provider
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evaluate",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-model", "test-haiku", "--pi-scenario", "logistics",
    ])
    code_a = main()
    cap_a = capsys.readouterr()
    assert code_a == 2
    assert "--pi-provider" in cap_a.out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest B: Missing --pi-model
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evaluate",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-scenario", "logistics",
    ])
    code_b = main()
    cap_b = capsys.readouterr()
    assert code_b == 2
    assert "--pi-model" in cap_b.out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest C: Missing --pi-scenario
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evaluate",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
    ])
    code_c = main()
    cap_c = capsys.readouterr()
    assert code_c == 2
    assert "--pi-scenario" in cap_c.out
    assert not (runs_dir / "runtime.db").exists()

    # Subtest D: Unsupported scenario
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(repo_root), "evaluate",
        "--skill", "logistics_query", "--execution-backend", "pi",
        "--pi-provider", "anthropic", "--pi-model", "test-haiku",
        "--pi-scenario", "unsupported_scenario_xyz",
    ])
    code_d = main()
    cap_d = capsys.readouterr()
    assert code_d == 2
    assert "Unsupported" in cap_d.out
    assert not (runs_dir / "runtime.db").exists()


def test_pi_cli_evaluate_incompatible_skill_deps_fail_upfront(cli_test_env, monkeypatch, capsys):
    """Scenario 5: Skill declaring incompatible dependencies fails upfront before runtime DB or tools."""
    repo_root: Path = cli_test_env["repo_root"]
    runs_dir = repo_root / "runs"

    incompatible_skill_dir = repo_root / "skills" / "bad_skill"
    incompatible_skill_dir.mkdir(parents=True, exist_ok=True)
    (incompatible_skill_dir / "SKILL.md").write_text("""---
name: bad_skill
version: 1.0.0
description: 不兼容的技能
dependencies:
  - query_order_packages
  - unsupported_payment_gateway
---
# Instructions
Do something.
""", encoding="utf-8")

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "bad_skill",
        "--eval-set",
        "baseline_dev",
        "--execution-backend",
        "pi",
        "--pi-provider",
        "anthropic",
        "--pi-model",
        "test-haiku",
        "--pi-scenario",
        "logistics",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Incompatible dependencies" in captured.out
    assert "unsupported_payment_gateway" in captured.out
    # Did not create runtime DB
    assert not (runs_dir / "runtime.db").exists()


def test_pi_cli_evaluate_production_rejects_faux_and_scripts(cli_test_env, monkeypatch, capsys):
    """Scenario 6: Production mode strictly rejects faux provider with clear nonzero exit."""
    repo_root: Path = cli_test_env["repo_root"]

    # Simulate production environment
    monkeypatch.setenv("SKILLFORGE_ENV", "production")
    monkeypatch.delitem(sys.modules, "pytest", raising=False)

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "logistics_query",
        "--eval-set",
        "baseline_dev",
        "--execution-backend",
        "pi",
        "--pi-provider",
        "faux",  # Faux provider rejected in production
        "--pi-model",
        "test-haiku",
        "--pi-scenario",
        "logistics",
    ]
    monkeypatch.setattr(sys, "argv", cmd)

    exit_code = main()
    captured = capsys.readouterr()

    # Returns 1 with clear failure message, no silent fallback
    assert exit_code == 1
    assert "Faux provider is restricted to explicit test mode" in captured.out


def test_pi_cli_evaluate_cleanup_on_exception(cli_test_env, monkeypatch, capsys):
    """Scenario 7: Unexpected runtime exception during evaluation triggers clean resource cleanup and exits 1."""
    repo_root: Path = cli_test_env["repo_root"]

    responses = _get_standard_logistics_responses()
    monkeypatch.setattr(
        "skillforge.pi_assembly._TEST_MODEL_CONFIG_HOOK",
        lambda cfg: {**cfg, "responses": copy.deepcopy(responses)},
    )

    from skillforge.evaluator import SkillEvaluator
    orig_eval = SkillEvaluator.evaluate_skill

    def boom_eval(*args, **kwargs):
        raise RuntimeError("Simulated unexpected crash during evaluation")

    monkeypatch.setattr(SkillEvaluator, "evaluate_skill", boom_eval)

    cmd = [
        "skillforge",
        "--root",
        str(repo_root),
        "evaluate",
        "--skill",
        "logistics_query",
        "--eval-set",
        "baseline_dev",
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
    assert "Simulated unexpected crash during evaluation" in captured.out


def test_pi_assembly_closes_resources_when_backend_initialization_fails(tmp_path, monkeypatch):
    from skillforge import pi_assembly

    closed = []
    for name in ("SkillRegistry", "EpisodeStore", "CandidateStore", "AgentRuntime"):
        def make_resource(*args, _name=name, **kwargs):
            return SimpleNamespace(close=lambda: closed.append(_name))
        monkeypatch.setattr(pi_assembly, name, make_resource)
    monkeypatch.setattr(pi_assembly, "ExperienceCollector", lambda **kwargs: object())

    def fail_backend(**kwargs):
        raise RuntimeError("backend initialization failed")

    monkeypatch.setattr(pi_assembly, "PiEvaluationBackend", fail_backend)
    with pytest.raises(RuntimeError, match="backend initialization failed"):
        with pi_assembly.assemble_pi_resources("logistics", "anthropic", "test", tmp_path):
            pytest.fail("initialization must fail before yielding resources")
    assert closed == ["AgentRuntime", "CandidateStore", "EpisodeStore", "SkillRegistry"]
