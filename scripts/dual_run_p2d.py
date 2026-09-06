#!/usr/bin/env python3
"""P2-D 双跑 Harness: 同一输入并跑 for 循环主链与 LangGraph 旁路状态机，输出终态字段级对比表。"""
from __future__ import annotations

import json
import hashlib
import re
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from skillforge.models import (
    SkillMeta, Trigger, EvalResult, RatchetVerdict, EvolveBudget, BudgetExceededError
)
from skillforge.evolver import SkillEvolver
from skillforge.langgraph_loop import run_evolve_langgraph as _raw_run_evolve_langgraph, get_graph_metadata


GRAPH_AUDITS: list[dict] = []


class CapturingFakeLLM:
    def __init__(self, responses: list[str], model: str = 'mock-dual-run'):
        self.responses = list(responses)
        self.model = model
        self.invocations: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.invocations.append(messages)
        content = self.responses.pop(0) if self.responses else '{}'
        return SimpleNamespace(content=content, usage={'total_tokens': 100})


def _setup_mock_env(tmp_path: Path):
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
            self.skills_dir = skills_dir.parent
            self._bodies = {'weather_query': """## Overview
天气查询

## Instructions
查天气

## Examples
北京天气

## Constraints
只查天气"""}
        def get_meta(self, name):
            return SkillMeta(
                name=name, version='1.0.0', description='天气查询',
                use_when='用户询问天气', not_for=['新闻'], dependencies=[],
                trigger=Trigger(keywords=['天气', '气温']), examples=['北京今天天气怎么样'],
            )

    base_eval = EvalResult(
        release_id='base_0',
        structure_score={'schema': 10.0},
        effect_score={'task': 10.0},
        objective_metrics={},
        p0_pass=True,
        case_verdicts=[{'case_id': 'r1', 'task_completion': 'B_better'}],
        case_outputs=[{'case_id': 'r1', 'query': '查询北京天气', 'reference': '晴', 'output_skill': '雨', 'output_baseline': '晴'}],
        valid=True,
    )

    class MockEvaluator:
        def evaluate_skill(self, *args, **kwargs):
            return base_eval

    return repo_root, MockRegistry(), MockEvaluator(), baseline_md, base_eval


def _reset_trace_flags(*results) -> None:
    """Keep the two isolated mock runs from sharing mutable EvalResult flags."""
    for result in results:
        if hasattr(result, '_eval_trace_written'):
            delattr(result, '_eval_trace_written')


def _canonical(value):
    """Canonicalize dataclasses/containers for strict semantic comparison."""
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, set):
        return sorted(_canonical(v) for v in value)
    if isinstance(value, Path):
        return value.name
    return value


def _normalize_artifact_ref(value: str) -> str:
    name = Path(str(value)).name
    # Timestamp/nonces belong to the isolated run, not to terminal semantics.
    return re.sub(r'^\d{8}[-_]\d{6}(?:[-_]\d+)?(?:-[0-9a-f]{8,})?-?', '', name)


def _ledger_snapshot(ledger) -> dict:
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


def _outcome_snapshot(outcome) -> dict:
    """All terminal fields except run-local identifiers and output paths."""
    context = _canonical(getattr(outcome, 'context', None))
    if isinstance(context, dict):
        # EvolveContext carries the complete baseline/attempt blackboard; no
        # field is dropped.  Only path-shaped strings in archive references are
        # run-local and normalized below.
        pass
    return {
        'skill_name': outcome.skill_name,
        'baseline_score': outcome.baseline_score,
        'patches_generated': outcome.patches_generated,
        'rounds_executed': outcome.rounds_executed,
        'error': outcome.error,
        'patches_published': list(outcome.patches_published),
        'patches_review': [_normalize_artifact_ref(v) for v in outcome.patches_review],
        'patches_declined': [_normalize_artifact_ref(v) for v in outcome.patches_declined],
        'records': _canonical(outcome.records),
        'attempts': _canonical(outcome.attempts),
        'skipped_cases': _canonical(outcome.skipped_cases),
        'context': context,
        'ledger': _ledger_snapshot(outcome.ledger),
    }


def _normalized_file_digest(path: Path) -> str:
    text = path.read_text(encoding='utf-8', errors='replace')
    if path.suffix == '.jsonl':
        normalized = []
        for line in text.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                normalized.append(line)
                continue

            def scrub(value):
                if isinstance(value, dict):
                    return {
                        k: scrub(v) for k, v in value.items()
                        if k not in {'ts', 'timestamp', 'trace_id', 'run_id', 'trace_file'}
                    }
                if isinstance(value, list):
                    return [scrub(v) for v in value]
                if isinstance(value, str):
                    return re.sub(r'evolve-[^/ ]+', 'evolve-RUN', value)
                return value

            normalized.append(json.dumps(scrub(data), ensure_ascii=False, sort_keys=True))
        text = '\n'.join(normalized)
    elif path.suffix in {'.md', '.json'}:
        text = re.sub(r'(?m)^- ts: .*$', '- ts: RUN', text)
        text = re.sub(r'evolve-[^/ ]+', 'evolve-RUN', text)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _side_effect_manifest(root: Path) -> list[tuple[str, str]]:
    """Compare semantic artifacts while excluding SQLite's run-local index."""
    runs = root / 'runs'
    if not runs.exists():
        return []
    result = []
    for path in sorted(
        p for p in runs.rglob('*')
        if p.is_file()
        and p.suffix not in {'.sqlite', '.db'}
        and not p.name.endswith(('.db-wal', '.db-shm'))
    ):
        rel = path.relative_to(runs)
        kind = rel.parts[0] if rel.parts else path.name
        result.append((kind, _normalized_file_digest(path)))
    return result


def _tree_snapshot(root: Path) -> list[tuple[str, str]]:
    if not root.exists():
        return []
    return [
        (str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(p for p in root.rglob('*') if p.is_file())
    ]


def _strict_outcome_equivalent(outcome_for, outcome_graph) -> bool:
    return _outcome_snapshot(outcome_for) == _outcome_snapshot(outcome_graph)


def _run_graph_isolated(evolver, skill_name: str, *args, repo_root: Path, shadow_root: Path, **kwargs):
    """Run the graph in an explicit sidecar and prove the live runs tree is unchanged."""
    graph_input_before = _tree_snapshot(repo_root / 'runs')
    # The harness creates ``tmp/repo`` for the for side and
    # ``tmp/graph-input/repo`` for the graph side.  Keep the lookup explicit so
    # a graph run cannot accidentally validate itself instead of the for side.
    live_repo = repo_root.parent.parent / 'repo'
    if not live_repo.exists():
        live_repo = repo_root
    live_before = _tree_snapshot(live_repo / 'runs')
    outcome = _raw_run_evolve_langgraph(
        evolver,
        skill_name,
        *args,
        shadow_root=shadow_root,
        **kwargs,
    )
    graph_input_after = _tree_snapshot(repo_root / 'runs')
    live_after = _tree_snapshot(live_repo / 'runs')
    outcome.main_repo_unchanged = live_before == live_after and graph_input_before == graph_input_after
    outcome.dual_side_effects = {
        'main_repo_unchanged': outcome.main_repo_unchanged,
        'for_manifest': _side_effect_manifest(live_repo),
        'graph_manifest': _side_effect_manifest(Path(outcome.shadow_root)),
        'graph_input_unchanged': graph_input_before == graph_input_after,
        'semantic_equal': _side_effect_manifest(live_repo) == _side_effect_manifest(Path(outcome.shadow_root)),
    }
    GRAPH_AUDITS.append(outcome.dual_side_effects)
    return outcome


def run_evolve_langgraph(evolver, skill_name: str, *args, **kwargs):
    """Script-local wrapper enforcing isolated graph output and live-tree audit."""
    repo_root = Path(evolver.repo_root).resolve()
    shadow_root = Path(kwargs.pop('shadow_root', repo_root.parent / 'graph-shadow')).resolve()
    return _run_graph_isolated(
        evolver,
        skill_name,
        *args,
        repo_root=repo_root,
        shadow_root=shadow_root,
        **kwargs,
    )


def run_dual_scenarios() -> list[dict]:
    GRAPH_AUDITS.clear()
    results = []

    # -------------------------------------------------------------
    # Scenario 1: L1 自动发布成功 (PUBLISHED)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        patch_l1_md = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 扩增天气描述')
        l1_json = json.dumps([{'level': 'L1', 'new_skill_md': patch_l1_md, 'rationale': '补充描述'}])
        eval_pass = EvalResult(
            release_id='cand_1_pass', structure_score={'schema': 10.0}, effect_score={'task': 15.0},
            objective_metrics={}, p0_pass=True, valid=True,
        )
        budget = EvolveBudget(enable_reflection=False, shadow_mode=False, auto_publish_enabled=True)

        with mock_patch('skillforge.evolver._validate_patch') as mock_val,              mock_patch('skillforge.evolver._publish_patch') as mock_pub:
            mock_val.return_value = (eval_pass, RatchetVerdict(decision='PASS', reasons=['指标提升']))
            mock_pub.return_value = {'status': 'PUBLISHED', 'release_id': 'rel-p2d-001', 'path': ''}

            llm_for = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '说明不详'}}), l1_json])
            outcome_for = SkillEvolver(reg, evaluator, llm_for).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

            _reset_trace_flags(eval_pass)
            llm_graph = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '说明不详'}}), l1_json])
            outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_graph), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '1. L1 成功发布 (PUBLISHED)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.context.stop_reason if outcome_for.context else outcome_for.error,
            'graph_stop': outcome_graph.context.stop_reason if outcome_graph.context else outcome_graph.error,
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                len(outcome_for.patches_published) == len(outcome_graph.patches_published) == 1 and
                outcome_for.rounds_executed == outcome_graph.rounds_executed == 1 and
                outcome_for.context.stop_reason == outcome_graph.context.stop_reason
            ),
        })

    # -------------------------------------------------------------
    # Scenario 2: 受控 2 轮反思收敛 (ROUNDS_EXHAUSTED)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        p_r1 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案1')
        p_r2 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案2反思')
        e_dec = EvalResult('c_r1', {'schema': 10.0}, {'task': 5.0}, {}, p0_pass=True, valid=True)
        e_rev = EvalResult('c_r2', {'schema': 10.0}, {'task': 12.0}, {}, p0_pass=True, valid=True)
        budget = EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True)

        with mock_patch('skillforge.evolver._validate_patch') as mock_val,              mock_patch('skillforge.evolver._publish_patch') as mock_pub:
            _reset_trace_flags(e_dec, e_rev)
            mock_val.side_effect = [
                (e_dec, RatchetVerdict('DECLINED', ['退步'])),
                (e_rev, RatchetVerdict('REVIEW', ['持平待审'])),
            ]
            mock_pub.side_effect = [
                {'status': 'DECLINED', 'release_id': '', 'path': str(repo_root / 'f1.json')},
                {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'r2.json')},
            ]
            llm_f = CapturingFakeLLM([
                json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}),
                json.dumps([{'level': 'L2', 'new_skill_md': p_r1, 'rationale': 'R1'}]),
                json.dumps([{'level': 'L2', 'new_skill_md': p_r2, 'rationale': 'R2'}]),
            ])
            outcome_for = SkillEvolver(reg, evaluator, llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

            _reset_trace_flags(e_dec, e_rev)
            mock_val.side_effect = [
                (e_dec, RatchetVerdict('DECLINED', ['退步'])),
                (e_rev, RatchetVerdict('REVIEW', ['持平待审'])),
            ]
            mock_pub.side_effect = [
                {'status': 'DECLINED', 'release_id': '', 'path': str(repo_root / 'f1.json')},
                {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'r2.json')},
            ]
            llm_g = CapturingFakeLLM([
                json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}),
                json.dumps([{'level': 'L2', 'new_skill_md': p_r1, 'rationale': 'R1'}]),
                json.dumps([{'level': 'L2', 'new_skill_md': p_r2, 'rationale': 'R2'}]),
            ])
            outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '2. 需修 2 轮反思收敛 (ROUNDS_EXHAUSTED)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.context.stop_reason,
            'graph_stop': outcome_graph.context.stop_reason,
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                outcome_for.rounds_executed == outcome_graph.rounds_executed == 2 and
                outcome_for.context.stop_reason == outcome_graph.context.stop_reason == 'ROUNDS_EXHAUSTED' and
                len(outcome_for.attempts) == len(outcome_graph.attempts) == 2
            ),
        })

    # -------------------------------------------------------------
    # Scenario 3: 重复指纹熔断停止 (REPEATED_FINGERPRINT)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        p_same = baseline_md.replace('version: 1.0.0', 'version: 1.0.1')
        p_json = json.dumps([{'level': 'L2', 'new_skill_md': p_same, 'rationale': '重复指纹'}])
        budget = EvolveBudget(enable_reflection=False, shadow_mode=True)

        llm_f = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), p_json])
        outcome_for = SkillEvolver(reg, evaluator, llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        llm_g = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), p_json])
        outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '3. 熔断停止 (REPEATED_FINGERPRINT)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.context.stop_reason,
            'graph_stop': outcome_graph.context.stop_reason,
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                len(outcome_for.patches_declined) == len(outcome_graph.patches_declined) == 1 and
                outcome_for.attempts[0].verdict == outcome_graph.attempts[0].verdict == 'DECLINED' and
                any('REPEATED_FINGERPRINT' in r for r in outcome_graph.attempts[0].reason_codes)
            ),
        })

    # -------------------------------------------------------------
    # Scenario 4: 预算耗尽停止 (BUDGET_EXCEEDED)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        p_l2 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 方案X')
        l2_json = json.dumps([{'level': 'L2', 'new_skill_md': p_l2, 'rationale': '尝试'}])
        budget = EvolveBudget(enable_reflection=False, shadow_mode=True)

        with mock_patch('skillforge.evolver._validate_patch') as mock_val:
            mock_val.side_effect = BudgetExceededError('LLM calls limit exceeded: 32 > 32', cap_type='call')
            llm_f = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), l2_json])
            outcome_for = SkillEvolver(reg, evaluator, llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

            llm_g = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), l2_json])
            outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '4. 预算耗尽停止 (BUDGET_EXCEEDED)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.error,
            'graph_stop': outcome_graph.error,
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                '沙箱验证预算硬帽超限' in outcome_for.error and
                '沙箱验证预算硬帽超限' in outcome_graph.error and
                outcome_for.context.stop_reason == outcome_graph.context.stop_reason
            ),
        })

    # -------------------------------------------------------------
    # Scenario 5: 基线 P0 Fail-Closed 停止 (BASELINE_INVALID)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, _, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, _, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        inv_eval = EvalResult('bad_0', {'schema': 10.0}, {'task': 0.0}, {}, p0_pass=False, case_verdicts=[{
            'case_id': 'r1', 'task_completion': 'INVALID', 'judge_audit': {'task_completion': {'reason_codes': ['JUDGE_TIMEOUT']}}
        }], valid=False)
        class BadEvaluator:
            def evaluate_skill(self, *a, **kw): return inv_eval

        budget = EvolveBudget(enable_reflection=False, p0_fail_on_invalid=True)
        llm_f = CapturingFakeLLM([])
        outcome_for = SkillEvolver(reg, BadEvaluator(), llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

        llm_g = CapturingFakeLLM([])
        outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, BadEvaluator(), llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '5. 基线 invalid 停止 (P0_FAIL_CLOSED)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.error[:30] + '...',
            'graph_stop': outcome_graph.error[:30] + '...',
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                'baseline 评估无效，P0 关键用例' in outcome_for.error and
                'baseline 评估无效，P0 关键用例' in outcome_graph.error and
                outcome_for.patches_generated == outcome_graph.patches_generated == 0
            ),
        })

    # -------------------------------------------------------------
    # Scenario 6: 外部依赖故障出口 (DEPENDENCY_ISSUE)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        budget = EvolveBudget(enable_reflection=False, enable_a2=True, shadow_mode=True)

        with mock_patch('skillforge.evolver.is_dependency_issue') as mock_dep:
            mock_dep.return_value = (True, '上游高德天气 API 网关宕机 503')
            llm_f = CapturingFakeLLM([json.dumps({'deps_broken': {'prob': 0.95, 'why': '503'}})])
            outcome_for = SkillEvolver(reg, evaluator, llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

            llm_g = CapturingFakeLLM([json.dumps({'deps_broken': {'prob': 0.95, 'why': '503'}})])
            outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '6. 依赖故障诊断归档出口 (DEPENDENCY_ISSUE)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': 'DEPENDENCY_ISSUE' in str(outcome_for.records[0].bloat_reasons[0]),
            'graph_stop': 'DEPENDENCY_ISSUE' in str(outcome_graph.records[0].bloat_reasons[0]),
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                len(outcome_for.patches_review) == len(outcome_graph.patches_review) == 1 and
                outcome_for.patches_generated == outcome_graph.patches_generated == 0
            ),
        })

    # -------------------------------------------------------------
    # Scenario 7: Round 1 REVIEW 候选可接受不重试 (ACCEPTABLE_FOUND)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo_root, reg, evaluator, baseline_md, _ = _setup_mock_env(Path(tmp))
        graph_repo_root, reg_graph, evaluator_graph, _, _ = _setup_mock_env(Path(tmp) / 'graph-input')
        p_l2 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 气象服务')
        l2_json = json.dumps([{'level': 'L2', 'new_skill_md': p_l2, 'rationale': '变更'}])
        e_rev = EvalResult('c_r1', {'schema': 10.0}, {'task': 12.0}, {}, p0_pass=True, valid=True)
        budget = EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True)

        with mock_patch('skillforge.evolver._validate_patch') as mock_val,              mock_patch('skillforge.evolver._publish_patch') as mock_pub:
            mock_val.return_value = (e_rev, RatchetVerdict('REVIEW', ['L2人工审']))
            mock_pub.return_value = {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'rev.json')}

            llm_f = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), l2_json])
            outcome_for = SkillEvolver(reg, evaluator, llm_f).evolve_full('weather_query', max_candidates=1, verbose=False, budget=budget)

            _reset_trace_flags(e_rev)
            llm_g = CapturingFakeLLM([json.dumps({'prompt_vague': {'prob': 0.8, 'why': '原因'}}), l2_json])
            outcome_graph = run_evolve_langgraph(SkillEvolver(reg_graph, evaluator_graph, llm_g), 'weather_query', max_candidates=1, verbose=False, budget=budget)

        results.append({
            'scenario': '7. REVIEW 可接受不重试 (ACCEPTABLE_FOUND)',
            'for_published': len(outcome_for.patches_published),
            'graph_published': len(outcome_graph.patches_published),
            'for_rounds': outcome_for.rounds_executed,
            'graph_rounds': outcome_graph.rounds_executed,
            'for_stop': outcome_for.context.stop_reason,
            'graph_stop': outcome_graph.context.stop_reason,
            'for_attempts': [a.verdict for a in outcome_for.attempts],
            'graph_attempts': [a.verdict for a in outcome_graph.attempts],
            'equivalent': _strict_outcome_equivalent(outcome_for, outcome_graph) and (
                len(outcome_for.patches_review) == len(outcome_graph.patches_review) == 1 and
                outcome_for.rounds_executed == outcome_graph.rounds_executed == 1 and
                outcome_for.context.stop_reason == outcome_graph.context.stop_reason == 'ACCEPTABLE_CANDIDATE_FOUND'
            ),
        })

    return results


def main():
    print('================================================================================')
    print('SkillForge P2-D: 受控回环 for 循环 vs LangGraph StateGraph 双跑行为等价性测试')
    print('================================================================================')

    meta = get_graph_metadata()
    print(f'LangGraph 拓扑图摘要: {meta["node_count"]} 节点 (含 START/END), {meta["edge_count"]} 边, {meta["conditional_edge_count"]} 条件分支')
    print(f'节点列表: {meta["nodes"]}')
    print('--------------------------------------------------------------------------------'); print()

    scenarios = run_dual_scenarios()
    all_ok = True

    header = f'| {"测试场景":<35} | {"for 版终态":<15} | {"LangGraph 终态":<15} | {"轮次":<6} | {"等价性":<6} |'
    print(header)
    print('|' + '-' * 37 + '|' + '-' * 17 + '|' + '-' * 17 + '|' + '-' * 8 + '|' + '-' * 8 + '|')

    for idx, sc in enumerate(scenarios):
        isolation_ok = idx < len(GRAPH_AUDITS) and GRAPH_AUDITS[idx].get('main_repo_unchanged', False)
        side_effects_ok = idx < len(GRAPH_AUDITS) and GRAPH_AUDITS[idx].get('semantic_equal', False)
        strict_ok = bool(sc['equivalent'] and isolation_ok and side_effects_ok)
        eq_label = '✅ 严格等价' if strict_ok else '❌ 差异不匹配'
        if not strict_ok:
            all_ok = False
        row = f"| {sc['scenario']:<33} | Pub:{sc['for_published']} Att:{len(sc['for_attempts']):<4} | Pub:{sc['graph_published']} Att:{len(sc['graph_attempts']):<4} | {sc['for_rounds']}/{sc['graph_rounds']:<4} | {eq_label} |"
        print(row)

    print(); print('--------------------------------------------------------------------------------')
    pass_cnt = sum(
        1 for idx, s in enumerate(scenarios)
        if s['equivalent']
        and idx < len(GRAPH_AUDITS)
        and GRAPH_AUDITS[idx].get('main_repo_unchanged', False)
        and GRAPH_AUDITS[idx].get('semantic_equal', False)
    )
    total_cnt = len(scenarios)
    print(f'双跑验证汇总: {pass_cnt}/{total_cnt} 场景完全等价 ({pass_cnt/total_cnt*100:.1f}%)')
    if all_ok:
        print('🎉 结论: LangGraph StateGraph 旁路 shadow 版在所有受控回环语义场景下与 for 循环主链逐字段行为 100% 等价！')
    else:
        print('⚠️ 注意: 存在部分场景终态不一致，请检查!')
        sys.exit(1)


if __name__ == '__main__':
    main()
