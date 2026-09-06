"""Tests for SkillForge P2-D: LangGraph StateGraph Shadow Bypass & Equivalence Verification.

Covers:
1. Behavior & Outcome Equivalence (Diff tests vs evolver.py for-loop across N scenarios):
   - Scenario 1: L1 Auto-Publish (Success publish)
   - Scenario 2: Controlled Reflection Convergence (Round 1 DECLINED -> Round 2 convergence)
   - Scenario 3: Circuit Breaker Stop (Repeated fingerprint)
   - Scenario 4: Budget Hard Cap Stop (Validation BudgetExceededError)
   - Scenario 5: Baseline P0 Fail-Closed (Judge/infrastructure invalid stop)
   - Scenario 6: Dependency Diagnosis Exit (REVIEW diagnostic archive without generating patch)
   - Scenario 7: Round 1 REVIEW Candidate (Acceptable found -> no retry)
2. Durable Checkpoint Recovery:
   - SQLite checkpointer persistence across graph reload and resume.
3. Graph Introspection & Metadata:
   - Node count, edge count, conditional branch semantics verification.
"""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch
import pytest

from skillforge.models import (
    SkillMeta,
    Trigger,
    EvalResult,
    RatchetVerdict,
    Patch,
    EvolveBudget,
    BudgetExceededError,
)
from skillforge.evolver import SkillEvolver
from skillforge.langgraph_loop import (
    run_evolve_langgraph,
    build_evolve_state_graph,
    get_graph_metadata,
    SqliteCheckpointer,
    create_default_checkpointer,
    route_after_validation,
)


class CapturingFakeLLM:
    def __init__(self, responses: list[str], model: str = 'test-model'):
        self.responses = list(responses)
        self.model = model
        self.invocations: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.invocations.append(messages)
        content = self.responses.pop(0) if self.responses else '{}'
        return SimpleNamespace(content=content, usage={'total_tokens': 120})


def _make_skill_meta(name: str = 'weather_query') -> SkillMeta:
    return SkillMeta(
        name=name,
        version='1.0.0',
        description='天气查询',
        use_when='用户询问天气',
        not_for=['新闻'],
        dependencies=[],
        trigger=Trigger(keywords=['天气', '气温']),
        examples=['北京今天天气怎么样'],
    )


def _setup_test_repo(tmp_path: Path):
    repo_root = tmp_path / 'repo'
    repo_root.mkdir(parents=True, exist_ok=True)
    eval_dir = repo_root / 'evaluation_sets'
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / 'repair_set.json').write_text(
        json.dumps({'cases': [{'id': 'r1', 'query': '查询北京天气'}]}),
        encoding='utf-8',
    )
    (eval_dir / 'p0_cases.json').write_text(
        json.dumps({'p0_ids': ['r1']}),
        encoding='utf-8',
    )
    skills_dir = repo_root / 'skills' / 'weather_query'
    skills_dir.mkdir(parents=True, exist_ok=True)
    baseline_md = """---
name: weather_query
version: 1.0.0
description: 天气查询
use_when: 用户询问天气
not_for: [新闻]
dependencies: []
trigger:
  keywords: [天气, 气温]
examples: [北京今天天气怎么样]
---

## Overview
天气查询

## Instructions
查天气

## Examples
北京天气

## Constraints
只查天气"""
    (skills_dir / 'SKILL.md').write_text(baseline_md, encoding='utf-8')

    class MockRegistry:
        def __init__(self):
            self.repo_root = repo_root
            self.skills_dir = repo_root / 'skills'
            self._bodies = {'weather_query': """## Overview
天气查询

## Instructions
查天气

## Examples
北京天气

## Constraints
只查天气"""}

        def get_meta(self, name):
            return _make_skill_meta(name)

    base_eval = EvalResult(
        release_id='base_0',
        structure_score={'schema': 10.0},
        effect_score={'task': 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{'case_id': 'r1', 'task_completion': 'B_better'}],
        case_outputs=[{
            'case_id': 'r1',
            'query': '查询北京天气',
            'reference': '晴',
            'output_skill': '雨',
            'output_baseline': '晴',
        }],
        valid=True,
    )

    class MockEvaluator:
        def __init__(self):
            self.llm = None

        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    return repo_root, MockRegistry(), MockEvaluator(), baseline_md, base_eval


def _assert_strict_equivalence(outcome_for, outcome_graph):
    """Strictly compare the complete terminal blackboard and durable ledger.

    ``run_id``/trace paths are intentionally run-local.  Every other outcome
    field, every nested record/attempt/context field, every ledger record, and
    the graph's isolated side-effect manifest are checked.
    """
    def canonical(value):
        if is_dataclass(value):
            return canonical(asdict(value))
        if isinstance(value, dict):
            return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (list, tuple)):
            return [canonical(v) for v in value]
        if isinstance(value, set):
            return sorted(canonical(v) for v in value)
        return value

    def artifact_ref(value):
        name = Path(str(value)).name
        return re.sub(r'^\d{8}[-_]\d{6}(?:[-_]\d+)?(?:-[0-9a-f]{8,})?-?', '', name)

    def ledger_snapshot(ledger):
        if ledger is None:
            return {}
        return {
            'total_calls': ledger.total_calls,
            'failed_calls': ledger.failed_calls,
            'total_tokens': ledger.total_tokens,
            'prompt_tokens': ledger.prompt_tokens,
            'completion_tokens': ledger.completion_tokens,
            'calls_by_role': dict(ledger.calls_by_role),
            'tokens_by_role': dict(ledger.tokens_by_role),
            'records': [
                {
                    'role': r.role,
                    'prompt_tokens': r.prompt_tokens,
                    'completion_tokens': r.completion_tokens,
                    'total_tokens': r.total_tokens,
                    'status': r.status,
                    'error_type': r.error_type,
                }
                for r in ledger.records
            ],
        }

    def snapshot(outcome):
        return {
            'skill_name': outcome.skill_name,
            'baseline_score': outcome.baseline_score,
            'patches_generated': outcome.patches_generated,
            'rounds_executed': outcome.rounds_executed,
            'error': outcome.error,
            'patches_published': list(outcome.patches_published),
            'patches_review': [artifact_ref(v) for v in outcome.patches_review],
            'patches_declined': [artifact_ref(v) for v in outcome.patches_declined],
            'records': canonical(outcome.records),
            'attempts': canonical(outcome.attempts),
            'skipped_cases': canonical(outcome.skipped_cases),
            'context': canonical(outcome.context),
            'ledger': ledger_snapshot(outcome.ledger),
        }

    assert snapshot(outcome_for) == snapshot(outcome_graph)
    assert outcome_graph.shadow_root
    graph_root = Path(outcome_graph.shadow_root).resolve()
    assert Path(outcome_graph.trace_file).resolve().is_relative_to(graph_root)
    assert outcome_graph.side_effect_manifest
    assert all((graph_root / item['path']).resolve().is_relative_to(graph_root) for item in outcome_graph.side_effect_manifest)


# =========================================================================
# 场景 1: L1 自动发布等价性
# =========================================================================

def test_scenario_l1_publish_equivalence(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    patch_l1_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 天气与降水查询')
    l1_json = json.dumps([{
        'level': 'L1',
        'new_skill_md': patch_l1_md,
        'rationale': '补充描述范围',
    }])

    eval_pass = EvalResult(
        release_id='cand_1_pass',
        structure_score={'schema': 10.0},
        effect_score={'task': 15.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )

    budget = EvolveBudget(enable_reflection=False, shadow_mode=False, auto_publish_enabled=True)

    with mock_patch('skillforge.evolver._validate_patch') as mock_val,          mock_patch('skillforge.evolver._publish_patch') as mock_pub:

        mock_val.return_value = (eval_pass, RatchetVerdict(decision='PASS', reasons=['评分提升']))
        mock_pub.return_value = {'status': 'PUBLISHED', 'release_id': 'rel-12345', 'path': ''}

        # For 版
        llm_for = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '缺少说明'}}),
            l1_json,
        ])
        evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)
        outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        # Graph 版
        llm_graph = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '缺少说明'}}),
            l1_json,
        ])
        evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)
        outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert len(outcome_graph.patches_published) == 1
    assert outcome_graph.rounds_executed == 1


# =========================================================================
# 场景 2: 受控 2 轮反思收敛等价性
# =========================================================================

def test_scenario_reflection_converge_equivalence(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    patch_r1_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案一')
    patch_r2_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案二反思')

    eval_declined = EvalResult(
        release_id='cand_r1',
        structure_score={'schema': 10.0},
        effect_score={'task': 5.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )
    eval_review = EvalResult(
        release_id='cand_r2',
        structure_score={'schema': 10.0},
        effect_score={'task': 12.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )

    budget = EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True)

    with mock_patch('skillforge.evolver._validate_patch') as mock_val,          mock_patch('skillforge.evolver._publish_patch') as mock_pub:

        mock_val.side_effect = [
            (eval_declined, RatchetVerdict(decision='DECLINED', reasons=['得分不足'])),
            (eval_review, RatchetVerdict(decision='REVIEW', reasons=['指标平齐需人工评审'])),
        ]
        mock_pub.side_effect = [
            {'status': 'DECLINED', 'release_id': '', 'path': str(repo_root / 'runs/failures/r1.json')},
            {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'runs/reviews/r2.json')},
        ]

        llm_for = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r1_md, 'rationale': 'Round 1 尝试'}]),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r2_md, 'rationale': 'Round 2 反思尝试'}]),
        ])
        evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)
        outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        mock_val.side_effect = [
            (eval_declined, RatchetVerdict(decision='DECLINED', reasons=['得分不足'])),
            (eval_review, RatchetVerdict(decision='REVIEW', reasons=['指标平齐需人工评审'])),
        ]
        mock_pub.side_effect = [
            {'status': 'DECLINED', 'release_id': '', 'path': str(repo_root / 'runs/failures/r1.json')},
            {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'runs/reviews/r2.json')},
        ]

        llm_graph = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r1_md, 'rationale': 'Round 1 尝试'}]),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r2_md, 'rationale': 'Round 2 反思尝试'}]),
        ])
        evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)
        outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert outcome_graph.rounds_executed == 2
    assert outcome_graph.context.stop_reason == 'ROUNDS_EXHAUSTED'
    assert len(outcome_graph.attempts) == 2


# =========================================================================
# 场景 3: 熔断停止等价性 (重复指纹)
# =========================================================================

def test_scenario_circuit_breaker_repeated_fingerprint(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    identical_patch_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1')
    patch_identical_json = json.dumps([{
        'level': 'L2',
        'new_skill_md': identical_patch_md,
        'rationale': '仅 bump 版本',
    }])

    budget = EvolveBudget(enable_reflection=False, shadow_mode=True)

    llm_for = CapturingFakeLLM([
        json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
        patch_identical_json,
    ])
    evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)
    outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

    llm_graph = CapturingFakeLLM([
        json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
        patch_identical_json,
    ])
    evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)
    outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert len(outcome_graph.patches_declined) == 1
    assert any('REPEATED_FINGERPRINT' in r for r in outcome_graph.attempts[0].reason_codes)


# =========================================================================
# 场景 4: 预算耗尽停止等价性
# =========================================================================

def test_scenario_budget_exceeded_validation(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    patch_l2_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案X')
    l2_json = json.dumps([{
        'level': 'L2',
        'new_skill_md': patch_l2_md,
        'rationale': '尝试',
    }])

    budget = EvolveBudget(enable_reflection=False, shadow_mode=True)

    with mock_patch('skillforge.evolver._validate_patch') as mock_val:
        mock_val.side_effect = BudgetExceededError('LLM calls limit exceeded: 32 > 32', cap_type='call')

        llm_for = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            l2_json,
        ])
        evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)
        outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        llm_graph = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            l2_json,
        ])
        evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)
        outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert '沙箱验证预算硬帽超限' in outcome_graph.error


# =========================================================================
# 场景 5: Baseline P0 Fail-Closed 停止等价性
# =========================================================================

def test_scenario_baseline_p0_invalid_fail_closed(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    invalid_base_eval = EvalResult(
        release_id='base_bad',
        structure_score={'schema': 10.0},
        effect_score={'task': 0.0},
        objective_metrics={},
        p0_pass=False,
        case_verdicts=[{
            'case_id': 'r1',
            'task_completion': 'INVALID',
            'judge_audit': {'task_completion': {'reason_codes': ['JUDGE_CALL_FAILED']}},
        }],
        valid=False,
    )

    class InvalidEvaluator:
        def evaluate_skill(self, *args, **kwargs):
            return invalid_base_eval

    budget = EvolveBudget(enable_reflection=False, p0_fail_on_invalid=True)

    llm_for = CapturingFakeLLM([])
    evolver_for = SkillEvolver(registry=reg, evaluator=InvalidEvaluator(), llm=llm_for)
    outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

    llm_graph = CapturingFakeLLM([])
    evolver_graph = SkillEvolver(registry=reg, evaluator=InvalidEvaluator(), llm=llm_graph)
    outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert 'baseline 评估无效，P0 关键用例' in outcome_graph.error
    assert outcome_graph.patches_generated == 0


# =========================================================================
# 场景 6: 依赖诊断归档出口等价性 (A2 branch)
# =========================================================================

def test_scenario_dependency_issue_review(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    budget = EvolveBudget(enable_reflection=False, enable_a2=True, shadow_mode=True)

    llm_for = CapturingFakeLLM([
        json.dumps({'deps_broken': {'prob': 0.95, 'why': '外部天行 API 503 宕机'}}),
    ])
    evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)

    llm_graph = CapturingFakeLLM([
        json.dumps({'deps_broken': {'prob': 0.95, 'why': '外部天行 API 503 宕机'}}),
    ])
    evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)

    with mock_patch('skillforge.evolver.is_dependency_issue') as mock_dep:
        mock_dep.return_value = (True, '外部 API 503 网关超时故障')

        outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)
        outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert outcome_graph.patches_generated == 0
    assert len(outcome_graph.patches_review) == 1
    assert 'DEPENDENCY_ISSUE' in outcome_graph.records[0].bloat_reasons[0]


# =========================================================================
# 场景 7: Round 1 REVIEW 候选可接受不重试追分等价性
# =========================================================================

def test_scenario_round1_review_acceptable_no_retry(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)

    patch_l2_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 气象信息查询服务')
    l2_json = json.dumps([{
        'level': 'L2',
        'new_skill_md': patch_l2_md,
        'rationale': '扩展描述',
    }])

    eval_review = EvalResult(
        release_id='cand_rev_1',
        structure_score={'schema': 10.0},
        effect_score={'task': 12.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )

    budget = EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True)

    with mock_patch('skillforge.evolver._validate_patch') as mock_val,          mock_patch('skillforge.evolver._publish_patch') as mock_pub:

        mock_val.return_value = (eval_review, RatchetVerdict(decision='REVIEW', reasons=['改动触发 L2 需人工审核']))
        mock_pub.return_value = {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'runs/reviews/r1.json')}

        llm_for = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            l2_json,
        ])
        evolver_for = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_for)
        outcome_for = evolver_for.evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        llm_graph = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
            l2_json,
        ])
        evolver_graph = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm_graph)
        outcome_graph = run_evolve_langgraph(evolver_graph, 'weather_query', max_candidates=1, verbose=False, budget=budget)

    _assert_strict_equivalence(outcome_for, outcome_graph)
    assert outcome_graph.rounds_executed == 1
    assert outcome_graph.context.stop_reason == 'ACCEPTABLE_CANDIDATE_FOUND'
    assert len(outcome_graph.patches_review) == 1


# =========================================================================
# 8. Durable Checkpoint 断点恢复验证 (SQLite Checkpointer)
# =========================================================================

def test_sqlite_checkpointer_durable_resume(tmp_path: Path):
    db_file = tmp_path / 'checkpoints.sqlite'
    checkpointer_1 = SqliteCheckpointer(db_path=db_file)

    graph_paused = build_evolve_state_graph(
        checkpointer=checkpointer_1,
        interrupt_before=['validation'],
    )

    repo_root, reg, evaluator, baseline_md, base_eval = _setup_test_repo(tmp_path)
    patch_l2_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 断点演示方案')
    l2_json = json.dumps([{
        'level': 'L2',
        'new_skill_md': patch_l2_md,
        'rationale': '断点测试',
    }])

    llm = CapturingFakeLLM([
        json.dumps({'prompt_vague': {'prob': 0.8, 'why': '提示词不明确'}}),
        l2_json,
    ])
    evolver = SkillEvolver(registry=reg, evaluator=evaluator, llm=llm)

    thread_id = 'durable-test-thread'
    config = {
        'configurable': {
            'thread_id': thread_id,
            'evolver': evolver,
        }
    }
    initial_state = {
        'skill_name': 'weather_query',
        'max_candidates': 1,
        'eval_set_for_iter': 'repair_set',
        'verbose': False,
        'budget': EvolveBudget(enable_reflection=False, shadow_mode=True),
        'transition_history': [],
    }

    # 阶段 1: 运行并在 validation 节点前主动暂停
    graph_paused.invoke(initial_state, config)
    state_before_resume = graph_paused.get_state(config)
    assert 'validation' in state_before_resume.next
    assert state_before_resume.values['round_no'] == 1
    assert len(state_before_resume.values['pending_patches']) == 1

    # 阶段 2: 模拟进程重启，新建 Checkpointer 打开同一 SQLite 文件
    checkpointer_2 = SqliteCheckpointer(db_path=db_file)
    graph_resumed = build_evolve_state_graph(checkpointer=checkpointer_2)

    eval_pass = EvalResult(
        release_id='cand_resumed',
        structure_score={'schema': 10.0},
        effect_score={'task': 15.0},
        objective_metrics={},
        p0_pass=True,
        valid=True,
    )

    with mock_patch('skillforge.evolver._validate_patch') as mock_val,          mock_patch('skillforge.evolver._publish_patch') as mock_pub:

        mock_val.return_value = (eval_pass, RatchetVerdict(decision='PASS', reasons=['通过']))
        mock_pub.return_value = {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'runs/reviews/durable.json')}

        # 从 SQLite 断点继续无状态重跑
        final_state = graph_resumed.invoke(None, config)

    assert final_state['outcome'] is not None
    assert final_state['outcome'].context.stop_reason == 'ACCEPTABLE_CANDIDATE_FOUND'
    assert len(final_state['outcome'].attempts) == 1


def test_sqlite_resume_rebinds_durable_ledger_without_reset(tmp_path: Path):
    """A fresh evolver process must continue the persisted call ledger."""
    repo_root, reg, _, baseline_md, base_eval = _setup_test_repo(tmp_path)
    db_file = tmp_path / 'ledger-resume.sqlite'
    cp1 = SqliteCheckpointer(db_file)
    graph1 = build_evolve_state_graph(checkpointer=cp1, interrupt_before=['validation'])

    patch_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1')
    llm1 = CapturingFakeLLM([
        json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}),
        json.dumps([{'level': 'L2', 'new_skill_md': patch_md, 'rationale': '候选'}]),
    ])

    class Eval:
        def __init__(self):
            self.llm = None
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    budget = EvolveBudget(max_calls=10, enable_reflection=False, shadow_mode=True)
    e1 = SkillEvolver(registry=reg, evaluator=Eval(), llm=llm1)
    cfg1 = {'configurable': {'thread_id': 'ledger-resume-thread', 'evolver': e1}}
    graph1.invoke({
        'skill_name': 'weather_query', 'max_candidates': 1,
        'eval_set_for_iter': 'repair_set', 'verbose': False,
        'budget': budget, 'transition_history': [],
    }, cfg1)
    paused = graph1.get_state(cfg1)
    calls_before = paused.values['outcome'].ledger.total_calls
    assert calls_before == 2

    cp2 = SqliteCheckpointer(db_file)
    graph2 = build_evolve_state_graph(checkpointer=cp2)
    restart_llm = CapturingFakeLLM([])
    e2 = SkillEvolver(registry=reg, evaluator=Eval(), llm=restart_llm)
    cfg2 = {'configurable': {'thread_id': 'ledger-resume-thread', 'evolver': e2}}
    eval_pass = EvalResult(
        release_id='resume-pass', structure_score={'schema': 10.0},
        effect_score={'task': 15.0}, objective_metrics={}, p0_pass=True, valid=True,
    )
    with mock_patch('skillforge.evolver._validate_patch', return_value=(
        eval_pass, RatchetVerdict(decision='PASS', reasons=['通过'])
    )), mock_patch('skillforge.evolver._publish_patch', return_value={
        'status': 'REVIEW', 'release_id': '', 'path': str(tmp_path / 'review.json')
    }):
        final_state = graph2.invoke(None, cfg2)

    outcome = final_state['outcome']
    assert outcome.ledger.total_calls == calls_before
    assert e2.ledger is outcome.ledger
    assert e2.ledger.budget.max_calls == 10
    assert restart_llm.invocations == []


def test_sqlite_checkpointer_merge_and_corruption_are_fail_closed(tmp_path: Path):
    db_file = tmp_path / 'merge.sqlite'
    cp1 = SqliteCheckpointer(db_file)
    cp2 = SqliteCheckpointer(db_file)
    cp1.storage['thread-a']['']['checkpoint-a'] = {'value': 'a'}
    cp1._save_to_db()
    cp2._load_from_db()
    cp1.storage['thread-b']['']['checkpoint-b'] = {'value': 'b'}
    cp1._save_to_db()
    cp2.storage['thread-c']['']['checkpoint-c'] = {'value': 'c'}
    cp2._save_to_db()
    cp3 = SqliteCheckpointer(db_file)
    assert {'thread-a', 'thread-b', 'thread-c'} <= set(cp3.storage)

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            'UPDATE langgraph_checkpoints SET val = ? WHERE key = ?',
            (sqlite3.Binary(b'not-a-pickle'), 'state'),
        )
        conn.commit()
    with pytest.raises(RuntimeError, match='CHECKPOINT_RESTORE_FAILED'):
        SqliteCheckpointer(db_file)


def test_graph_shadow_root_isolated_and_run_id_uses_nonce(tmp_path: Path):
    repo_root, reg, evaluator, baseline_md, _ = _setup_test_repo(tmp_path)
    patch_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1')
    patch_json = json.dumps([{'level': 'L2', 'new_skill_md': patch_md, 'rationale': '隔离'}])
    budget = EvolveBudget(enable_reflection=False, shadow_mode=True)
    live_before = sorted(p.relative_to(repo_root) for p in repo_root.rglob('*') if p.is_file())

    with mock_patch('skillforge.evolver._validate_patch', return_value=(
        EvalResult('pass', {'schema': 10.0}, {'task': 15.0}, {}, p0_pass=True, valid=True),
        RatchetVerdict('PASS', ['通过']),
    )), mock_patch('skillforge.evolver._publish_patch', return_value={
        'status': 'REVIEW', 'release_id': '', 'path': str(tmp_path / 'review.json')
    }):
        e1 = SkillEvolver(reg, evaluator, CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), patch_json
        ]))
        e2 = SkillEvolver(reg, evaluator, CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), patch_json
        ]))
        o1 = run_evolve_langgraph(e1, 'weather_query', max_candidates=1, verbose=False, budget=budget,
                                  shadow_root=tmp_path / 'shadow-one')
        o2 = run_evolve_langgraph(e2, 'weather_query', max_candidates=1, verbose=False, budget=budget,
                                  shadow_root=tmp_path / 'shadow-two')

    assert o1.run_id != o2.run_id
    assert o1.shadow_root != o2.shadow_root
    assert Path(o1.trace_file).resolve().is_relative_to(Path(o1.shadow_root).resolve())
    assert Path(o2.trace_file).resolve().is_relative_to(Path(o2.shadow_root).resolve())
    live_after = sorted(p.relative_to(repo_root) for p in repo_root.rglob('*') if p.is_file())
    assert live_before == live_after


@pytest.mark.parametrize('status, reason', [
    ('exception', 'VALIDATION_EXCEPTION: boom-r2'),
    ('repeated_fingerprint', 'REPEATED_FINGERPRINT_STOP'),
    ('budget_exceeded', 'BUDGET_EXCEEDED: calls'),
])
def test_round2_terminal_reason_is_not_overwritten(status: str, reason: str):
    from skillforge.models import EvolveContext

    state = {
        'round_no': 2,
        'validation_status': status,
        'context': EvolveContext(skill_name='weather_query', stop_reason=reason),
        'transition_history': [],
    }
    assert route_after_validation(state) == '__end__'
    assert state['transition_history'][-1]['reason'].endswith(reason)


# =========================================================================
# 9. 图拓扑与元数据断言
# =========================================================================

def test_graph_metadata_and_topology():
    meta = get_graph_metadata()
    assert meta['node_count'] == 7
    assert '__start__' in meta['nodes']
    assert '__end__' in meta['nodes']
    assert 'failure_analysis' in meta['nodes']
    assert 'candidate_generation' in meta['nodes']
    assert 'validation' in meta['nodes']
    assert 'defense_adjudication' in meta['nodes']
    assert 'rounds_state_machine' in meta['nodes']
    assert meta['edge_count'] == 14
    assert meta['conditional_edge_count'] == 13
    assert 'graph TD;' in meta['mermaid']
