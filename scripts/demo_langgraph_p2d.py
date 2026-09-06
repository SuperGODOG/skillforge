#!/usr/bin/env python3
"""P2-D 面试演示资产: LangGraph 受控回环状态机小输入演示 + 状态转移序列 + Durable 断点恢复。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from skillforge.models import (
    SkillMeta, Trigger, EvalResult, RatchetVerdict, EvolveBudget
)
from skillforge.evolver import SkillEvolver
from skillforge.langgraph_loop import (
    run_evolve_langgraph,
    build_evolve_state_graph,
    get_graph_metadata,
    SqliteCheckpointer,
)


class CapturingFakeLLM:
    def __init__(self, responses: list[str], model: str = 'interview-demo-llm'):
        self.responses = list(responses)
        self.model = model
        self.invocations: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.invocations.append(messages)
        content = self.responses.pop(0) if self.responses else '{}'
        return SimpleNamespace(content=content, usage={'total_tokens': 150})


def main():
    print('================================================================================')
    print('       SkillForge P2-D: LangGraph StateGraph 受控回环面试技术展示')
    print('================================================================================')

    # 1. 图拓扑与结构摘要
    meta = get_graph_metadata()
    print('【1. 状态图拓扑架构摘要】')
    print(f'· 节点总数: {meta["node_count"]} (含 START 与 END)')
    print(f'· 边总数: {meta["edge_count"]} 条 (其中条件分支边: {meta["conditional_edge_count"]} 条)')
    print('· 核心节点序列:')
    for node in meta['nodes']:
        if node not in ('__start__', '__end__'):
            print(f'  - [{node}]')
    print()
    print('· Mermaid 流程图描述 (可直接贴入 Obsidian/Markdown 汇报):')
    print('```mermaid')
    print(meta['mermaid'].strip())
    print('```')
    print()

    # 2. 状态转移序列实测演示 (2 轮反思收敛场景)
    print('--------------------------------------------------------------------------------')
    print('【2. 小输入实测：状态转移序列与受控回环执行轨迹】')
    print('模拟场景: Round 1 候选退步 DECLINED -> 触发定向反思 -> Round 2 修复达标 REVIEW 归档')

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_root = Path(tmp_dir)
        skills_dir = repo_root / 'skills' / 'weather_query'
        skills_dir.mkdir(parents=True, exist_ok=True)
        eval_dir = repo_root / 'evaluation_sets'
        eval_dir.mkdir(parents=True, exist_ok=True)
        (eval_dir / 'repair_set.json').write_text(json.dumps({'cases': [{'id': 'r1', 'query': '查天气'}]}))
        (eval_dir / 'p0_cases.json').write_text(json.dumps({'p0_ids': ['r1']}))

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
                return SkillMeta(name=name, version='1.0.0', description='天气查询', use_when='查天气', trigger=Trigger(keywords=['天气']))

        base_eval = EvalResult('base_0', {'schema': 10.0}, {'task': 10.0}, {}, p0_pass=True, case_verdicts=[{'case_id': 'r1', 'task_completion': 'B_better'}], valid=True)

        class MockEvaluator:
            def evaluate_skill(self, *a, **kw): return base_eval

        patch_r1 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 粗糙修补方案')
        patch_r2 = baseline_md.replace('version: 1.0.0', 'version: 1.0.1').replace('description: 天气查询', 'description: 定向反思精细优化方案')

        e_r1 = EvalResult('c_r1', {'schema': 10.0}, {'task': 6.0}, {}, p0_pass=True, valid=True)
        e_r2 = EvalResult('c_r2', {'schema': 10.0}, {'task': 13.0}, {}, p0_pass=True, valid=True)

        budget = EvolveBudget(max_rounds=2, enable_reflection=True, shadow_mode=True)

        llm = CapturingFakeLLM([
            json.dumps({'prompt_vague': {'prob': 0.85, 'why': '规则边界定义不严密'}}),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r1, 'rationale': 'Round 1 初始尝试'}]),
            json.dumps([{'level': 'L2', 'new_skill_md': patch_r2, 'rationale': 'Round 2 定向反思修复'}]),
        ])

        evolver = SkillEvolver(registry=MockRegistry(), evaluator=MockEvaluator(), llm=llm)

        with mock_patch('skillforge.evolver._validate_patch') as mock_val,              mock_patch('skillforge.evolver._publish_patch') as mock_pub:
            mock_val.side_effect = [
                (e_r1, RatchetVerdict('DECLINED', ['总分由 20 降至 16，发生退步'])),
                (e_r2, RatchetVerdict('REVIEW', ['总分由 20 提升至 23，L2 变更转入审核'])),
            ]
            mock_pub.side_effect = [
                {'status': 'DECLINED', 'release_id': '', 'path': str(repo_root / 'r1_fail.json')},
                {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root / 'r2_review.json')},
            ]

            outcome = run_evolve_langgraph(
                evolver=evolver,
                skill_name='weather_query',
                max_candidates=1,
                verbose=False,
                budget=budget,
            )

    transitions = getattr(outcome, 'graph_transitions', [])
    assert outcome.rounds_executed == 2, 'demo 必须覆盖两轮受控回环'
    assert len(outcome.patches_declined) == 1 and len(outcome.patches_review) == 1
    assert outcome.context is not None and outcome.context.stop_reason == 'ROUNDS_EXHAUSTED'
    assert len(transitions) == 9, 'demo 状态转移序列漂移'
    assert outcome.ledger is not None and outcome.ledger.total_calls == 3
    assert outcome.shadow_root and Path(outcome.trace_file).resolve().is_relative_to(Path(outcome.shadow_root).resolve())
    print(f'· 执行总轮次: {outcome.rounds_executed} 轮')
    print(f'· 终态裁决: 成功产出 REVIEW 审核件 {len(outcome.patches_review)} 份，未通过 {len(outcome.patches_declined)} 份')
    print(f'· 终止原因码: {outcome.context.stop_reason}')
    print(f'· 状态转移序列 (共 {len(transitions)} 次流转):')
    for idx, t in enumerate(transitions, 1):
        print(f"  [{idx:02d}] R{t['round_no']} | {t['from_node']:<22} -> {t['to_node']:<22} | 转移原因: {t['reason']}")

    # 3. Durable 检查点断点恢复演示
    print('\n--------------------------------------------------------------------------------')
    print('【3. Durable 检查点持久化与断点续跑演示 (SQLite Checkpointer)】')
    with tempfile.NamedTemporaryFile(suffix='.sqlite') as tmp_sqlite:
        db_path = tmp_sqlite.name
        print(f'· 步骤 A: 挂载 SQLite Durable Checkpointer ({Path(db_path).name})，在 validation 节点前注入主动断点')
        cp1 = SqliteCheckpointer(db_path=db_path)
        graph_paused = build_evolve_state_graph(checkpointer=cp1, interrupt_before=['validation'])

        with tempfile.TemporaryDirectory() as tmp_env:
            repo_root2 = Path(tmp_env)
            skills_dir2 = repo_root2 / 'skills' / 'weather_query'
            skills_dir2.mkdir(parents=True, exist_ok=True)
            eval_dir2 = repo_root2 / 'evaluation_sets'
            eval_dir2.mkdir(parents=True, exist_ok=True)
            (eval_dir2 / 'repair_set.json').write_text(json.dumps({'cases': [{'id': 'r1', 'query': '查天气'}]}))
            (skills_dir2 / 'SKILL.md').write_text(baseline_md, encoding='utf-8')

            class MockReg2:
                def __init__(self):
                    self.repo_root = repo_root2
                    self.skills_dir = skills_dir2.parent
                    self._bodies = {'weather_query': """## Overview
天气查询

## Instructions
查天气

## Examples
北京天气

## Constraints
只查天气"""}
                def get_meta(self, name): return SkillMeta(name=name, version='1.0.0', description='天气查询', use_when='查天气')

            llm_durable = CapturingFakeLLM([
                json.dumps({'prompt_vague': {'prob': 0.8, 'why': '断点原因'}}),
                json.dumps([{'level': 'L2', 'new_skill_md': patch_r1, 'rationale': '断点测试候选'}]),
            ])
            evolver2 = SkillEvolver(MockReg2(), MockEvaluator(), llm_durable)

            cfg = {'configurable': {'thread_id': 'demo-thread-durable', 'evolver': evolver2}}
            init_st = {
                'skill_name': 'weather_query',
                'max_candidates': 1,
                'eval_set_for_iter': 'repair_set',
                'verbose': False,
                'budget': EvolveBudget(enable_reflection=False, shadow_mode=True),
                'transition_history': [],
            }
            graph_paused.invoke(init_st, cfg)
            snapshot = graph_paused.get_state(cfg)
            print(f'  ✓ 进程 1 中断暂停成功！下一待执行节点: {snapshot.next}, 待验 Patch 数: {len(snapshot.values.get("pending_patches", []))}')

            print('· 步骤 B: 模拟主进程 Crash / 重启，新建全新 Checkpointer 打开该 SQLite 文件')
            cp2 = SqliteCheckpointer(db_path=db_path)
            graph_resumed = build_evolve_state_graph(checkpointer=cp2)
            # The second process gets a fresh SkillEvolver/LLM.  The graph must
            # rebind the ledger deserialized from SQLite before resuming.
            restart_llm = CapturingFakeLLM([])
            evolver3 = SkillEvolver(MockReg2(), MockEvaluator(), restart_llm)
            cfg_restart = {'configurable': {'thread_id': 'demo-thread-durable', 'evolver': evolver3}}
            restored_snapshot = graph_resumed.get_state(cfg_restart)
            print(f'  ✓ 进程 2 从磁盘冷加载状态成功！识别到断点节点: {restored_snapshot.next}')

            print('· 步骤 C: 从 SQLite Checkpoint 继续执行 (Resume)')
            with mock_patch('skillforge.evolver._validate_patch') as mock_val2,                  mock_patch('skillforge.evolver._publish_patch') as mock_pub2:
                mock_val2.return_value = (e_r2, RatchetVerdict('REVIEW', ['恢复通过']))
                mock_pub2.return_value = {'status': 'REVIEW', 'release_id': '', 'path': str(repo_root2 / 'rev.json')}
                final_state = graph_resumed.invoke(None, cfg_restart)

            print(f'  ✓ 恢复运行完成！终态 stop_reason: {final_state["outcome"].context.stop_reason}')
            assert final_state['outcome'].ledger.total_calls == 2
            assert restart_llm.invocations == [], 'resume 不应重置预算或额外调用生成 LLM'
            print('  ✓ Durable 状态机断点恢复全链路验证成功！')

    print('\n================================================================================')
    print('                      演示资产生成完毕，可完整汇报')
    print('================================================================================')


if __name__ == '__main__':
    main()
