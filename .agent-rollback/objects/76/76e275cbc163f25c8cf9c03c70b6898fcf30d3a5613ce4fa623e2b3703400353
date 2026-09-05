"""SkillEvolver 六步单元测试（Phase 4）

覆盖：
    - Failure / RootCause dataclass 正确
    - _collect_failures 从 EvalResult 挑 B_better
    - _analyze_root_cause 正确解析 LLM JSON / 坏 JSON 降级
    - _generate_patches 正确解析 / 过滤非法 level / 空数组
    - _strip_code_fence 去代码块
    - _reconstruct_skill_md 拼回
    - _archive_failure / _archive_suggestion 落盘
"""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillforge.models import EvalResult, RatchetVerdict, SkillMeta, Trigger, Patch
from skillforge.evolver import (
    Failure, RootCause,
    _collect_failures, _analyze_root_cause, _generate_patches,
    _strip_code_fence, _reconstruct_skill_md,
    _archive_failure, _archive_suggestion,
)


class FakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)

    def invoke(self, messages, **kw):
        c = self.contents.pop(0) if self.contents else ""
        return SimpleNamespace(content=c, usage={"total_tokens": 100})


def _mk_meta(**over):
    d = dict(name="s1", version="1.0.0", description="做事",
             use_when="用户要做事", not_for=["其他"], dependencies=[],
             trigger=Trigger(keywords=["a", "b"]), examples=["ex1"])
    d.update(over)
    return SkillMeta(**d)


# ============ dataclass ============

def test_failure_dataclass():
    f = Failure(case_id="c1", query="q", reference="r", output_skill="s", output_baseline="b")
    assert f.case_id == "c1"
    assert f.losing_dims == []


def test_root_cause_dataclass():
    rc = RootCause(label="trigger_inaccurate", prob=0.5, why="reason")
    assert rc.prob == 0.5


# ============ _collect_failures ============

def test_collect_failures_picks_b_better():
    result = EvalResult(
        release_id="r-x",
        structure_score={"schema": 15}, effect_score={"task": 20},
        objective_metrics={}, p0_pass=True,
        case_verdicts=[
            {"case_id": "c1", "query": "q1",
             "task_completion": "A_better", "robustness": "A_better", "readability": "A_better"},
            {"case_id": "c2", "query": "q2",
             "task_completion": "B_better", "robustness": "tied", "readability": "A_better"},
            {"case_id": "c3", "query": "q3",
             "task_completion": "tied", "robustness": "B_better", "readability": "B_better"},
        ],
        case_outputs=[
            {"case_id": "c1", "query": "q1", "reference": "r1", "output_skill": "s1", "output_baseline": "b1"},
            {"case_id": "c2", "query": "q2", "reference": "r2", "output_skill": "s2", "output_baseline": "b2"},
            {"case_id": "c3", "query": "q3", "reference": "r3", "output_skill": "s3", "output_baseline": "b3"},
        ],
    )
    fails = _collect_failures(result)
    assert len(fails) == 2, "只 c2/c3 有 B_better"
    ids = [f.case_id for f in fails]
    assert "c2" in ids and "c3" in ids and "c1" not in ids

    c3 = next(f for f in fails if f.case_id == "c3")
    assert set(c3.losing_dims) == {"robustness", "readability"}


def test_collect_failures_empty_verdicts():
    result = EvalResult(
        release_id="r", structure_score={}, effect_score={},
        objective_metrics={}, p0_pass=True,
        case_verdicts=[], case_outputs=[],
    )
    assert _collect_failures(result) == []


# ============ _analyze_root_cause ============

def test_analyze_root_cause_parses_valid_json():
    meta = _mk_meta()
    fails = [Failure(case_id="c1", query="q", reference="", output_skill="", output_baseline="")]
    llm = FakeLLM(['{"trigger_inaccurate": {"prob": 0.6, "why": "kw off"}, '
                   '"prompt_vague": {"prob": 0.2, "why": ""}, '
                   '"deps_broken": {"prob": 0.0, "why": ""}, '
                   '"boundary_missing": {"prob": 0.1, "why": ""}}'])
    causes = _analyze_root_cause(llm, meta, "body", fails)
    assert len(causes) == 4
    # 应按 prob 降序
    assert causes[0].label == "trigger_inaccurate"
    assert causes[0].prob == 0.6


def test_analyze_root_cause_bad_json_returns_empty():
    meta = _mk_meta()
    llm = FakeLLM(["not a json at all"])
    causes = _analyze_root_cause(llm, meta, "body", [])
    assert causes == []


# ============ _generate_patches ============

def test_generate_patches_parses_valid_json():
    import json as _json
    meta = _mk_meta()
    valid_md = "---\nname: s1\nversion: 1.0.1\ndescription: X\nuse_when: Y\ntrigger:\n  keywords: [a]\n---\n\n## Overview\n..."
    llm = FakeLLM([_json.dumps([
        {"level": "L1", "rationale": "add examples", "new_skill_md": valid_md},
        {"level": "L2", "rationale": "tweak trigger", "new_skill_md": valid_md},
    ])])
    patches = _generate_patches(llm, meta, "body", [], [], max_candidates=2)
    assert len(patches) == 2
    assert patches[0].level == "L1"
    assert patches[1].level == "L2"
    assert patches[0].computed_level == "L2"
    assert patches[0].downgrade_attempt is True
    assert patches[0].unified_diff.startswith("--- old/SKILL.md")
    assert patches[0].changed_frontmatter
    assert patches[0].changed_body_sections == ["__full_body__", "Overview"]
    assert "add examples" in patches[0].rationale


def test_generate_patches_filters_invalid_level():
    import json as _json
    meta = _mk_meta()
    valid_md = _reconstruct_skill_md(meta, "body").replace(
        "version: 1.0.0", "version: 1.0.1", 1
    )
    llm = FakeLLM([_json.dumps([
        {"level": "L4", "rationale": "x", "new_skill_md": valid_md},
        {"level": "L1", "rationale": "ok", "new_skill_md": valid_md},
    ])])
    patches = _generate_patches(llm, meta, "body", [], [], max_candidates=2)
    assert len(patches) == 1
    assert patches[0].level == "L1"


def test_generate_patches_bad_json_returns_empty():
    meta = _mk_meta()
    llm = FakeLLM(["nonsense output"])
    assert _generate_patches(llm, meta, "body", [], [], max_candidates=3) == []


def test_generate_patches_skips_non_mapping_and_non_string_payloads():
    import json as _json
    meta = _mk_meta()
    llm = FakeLLM([_json.dumps([None, {"level": 1}, {"level": "L1", "new_skill_md": 3}])])

    assert _generate_patches(llm, meta, "body", [], [], max_candidates=3) == []


# ============ _strip_code_fence ============

def test_strip_code_fence_json_block():
    text = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_markdown_block():
    text = '```markdown\nhello\n```'
    assert _strip_code_fence(text) == 'hello'


def test_strip_code_fence_no_fence_unchanged():
    text = '{"a": 1}'
    assert _strip_code_fence(text) == '{"a": 1}'


# ============ _reconstruct_skill_md ============

def test_reconstruct_skill_md_contains_all_fields():
    meta = _mk_meta()
    md = _reconstruct_skill_md(meta, "## Overview\ntest")
    assert md.startswith("---\n")
    assert "name: s1" in md
    assert "use_when:" in md
    assert "## Overview" in md


# ============ 归档 ============

def test_archive_failure_writes_md(tmp_path: Path):
    patch = Patch(
        skill_name="s1",
        level="L1",
        diff="---\nname: s1\n---\n\nbody",
        rationale="test",
        computed_level="L2",
        unified_diff="--- old/SKILL.md\n+++ new/SKILL.md",
        downgrade_attempt=True,
    )
    verdict = RatchetVerdict(decision="DECLINED", reasons=["总分退步"])
    path = _archive_failure(tmp_path, "s1", patch, verdict, None, None)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "DECLINED" in text
    assert "L1" in text
    assert "总分退步" in text
    assert "```markdown" in text
    assert "declared_level: L1" in text
    assert "computed_level: L2" in text
    assert "downgrade_attempt: true" in text
    assert "## 差异对比 (Unified Diff)" in text
    assert "已阻断自动发布" in text


def test_archive_suggestion_writes_md(tmp_path: Path):
    patch = Patch(skill_name="s1", level="L2", diff="---\nname: s1\n---\n\nbody", rationale="tweak")
    verdict = RatchetVerdict(decision="REVIEW", reasons=["软门槛触发 15%"])
    result = EvalResult(
        release_id="r", structure_score={"schema": 15},
        effect_score={"task": 20}, objective_metrics={}, p0_pass=True,
    )
    path = _archive_suggestion(tmp_path, "s1", patch, verdict, result)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "[L2 建议]" in text
    assert "35.00 / 100" in text
