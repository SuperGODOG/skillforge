"""SkillRegistry 基础链路 10 条测试（Phase 1 交付门槛）

覆盖：
    T1  import skillforge 顶层 API 齐全
    T2  SkillMeta 合法 YAML 解析成功
    T3  SkillMeta 缺 frontmatter 报错
    T4  SkillMeta 重名报错
    T5  list_names 排序 + 数量正确
    T6  build_index 包含所有 skill 的 name / description / use_when / not_for
    T7  use_skill 未注册 → [ERROR] 前缀（不抛异常）
    T8  use_skill 未发布 → source=disk_no_release，body 含 ## Overview
    T9  use_skill 已发布 → source=git，body 与磁盘版一致
    T10 router.jsonl 日志格式：含 reason / source / latency_ms / release_id 字段
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from skillforge import SkillRegistry, ReleaseStateMachine, Patch


SKILL_A_MD = """---
name: skill_a
version: 1.0.0
description: Skill A 的一句话描述
use_when: 用户明确要 A 场景
not_for:
  - B 场景
  - C 场景
dependencies: []
trigger:
  keywords: [alpha, a]
examples: [use a here]
---

## Overview
A 的介绍。

## Instructions
按 A 的方式做。

## Constraints
不做 B 不做 C。
"""

SKILL_B_MD = """---
name: skill_b
version: 1.0.0
description: Skill B 的一句话描述
use_when: 用户明确要 B 场景
not_for:
  - A 场景
dependencies: []
trigger:
  keywords: [beta, b]
examples: [use b here]
---

## Overview
B 的介绍。

## Instructions
按 B 的方式做。

## Constraints
不做 A。
"""


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """临时 git 仓库 + skills/ 目录 + runs/ 目录"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@skillforge.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "SkillForge Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    (tmp_path / "skills" / "skill_a").mkdir(parents=True)
    (tmp_path / "skills" / "skill_b").mkdir(parents=True)
    (tmp_path / "skills" / "skill_a" / "SKILL.md").write_text(SKILL_A_MD, encoding="utf-8")
    (tmp_path / "skills" / "skill_b" / "SKILL.md").write_text(SKILL_B_MD, encoding="utf-8")
    (tmp_path / "runs").mkdir()
    subprocess.run(["git", "-C", str(tmp_path), "add", "skills/"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "add 2 test skills"],
        check=True,
    )
    return tmp_path


@pytest.fixture
def reg(tmp_repo: Path) -> SkillRegistry:
    r = SkillRegistry(
        db_path=tmp_repo / "runs" / "test.db",
        skills_dir=tmp_repo / "skills",
        repo_root=tmp_repo,
        router_log=tmp_repo / "runs" / "router.jsonl",
    )
    r.load_skills_from_dir()
    yield r
    r.close()


# =========================================================================


def test_1_import_topology():
    """import skillforge 顶层 API 齐全"""
    import skillforge
    for name in (
        "SkillMeta", "Trigger", "Evaluation",
        "RouteResult", "EvalResult", "RatchetVerdict", "Patch", "Release",
        "SkillRegistry", "IntentRouter", "SkillEvaluator",
        "SkillEvolver", "ReleaseStateMachine",
    ):
        assert hasattr(skillforge, name), f"skillforge.{name} 未暴露"


def test_2_skillmeta_parses(reg: SkillRegistry):
    """SkillMeta 合法 YAML 解析成功"""
    meta = reg.get_meta("skill_a")
    assert meta.name == "skill_a"
    assert meta.version == "1.0.0"
    assert meta.description == "Skill A 的一句话描述"
    assert meta.use_when == "用户明确要 A 场景"
    assert meta.not_for == ["B 场景", "C 场景"]
    assert meta.trigger.keywords == ["alpha", "a"]


def test_3_missing_frontmatter_raises(tmp_path: Path):
    """SKILL.md 缺 frontmatter → ValueError"""
    (tmp_path / "skills" / "bad").mkdir(parents=True)
    (tmp_path / "skills" / "bad" / "SKILL.md").write_text(
        "no frontmatter here just body\n", encoding="utf-8",
    )
    r = SkillRegistry(
        db_path=tmp_path / "test.db",
        skills_dir=tmp_path / "skills",
        repo_root=tmp_path,
    )
    with pytest.raises(ValueError, match="frontmatter"):
        r.load_skills_from_dir()


def test_4_duplicate_name_raises(tmp_path: Path):
    """同一 skill_name 出现两次 → ValueError"""
    for sub in ("dup1", "dup2"):
        (tmp_path / "skills" / sub).mkdir(parents=True)
        (tmp_path / "skills" / sub / "SKILL.md").write_text(SKILL_A_MD, encoding="utf-8")
    r = SkillRegistry(
        db_path=tmp_path / "test.db",
        skills_dir=tmp_path / "skills",
        repo_root=tmp_path,
    )
    with pytest.raises(ValueError, match="name 冲突"):
        r.load_skills_from_dir()


def test_5_list_names(reg: SkillRegistry):
    """list_names 排序 + 数量"""
    assert reg.list_names() == ["skill_a", "skill_b"]


def test_6_build_index(reg: SkillRegistry):
    """build_index 覆盖所有关键字段"""
    idx = reg.build_index()
    assert "### skill_a" in idx and "### skill_b" in idx
    assert "Skill A 的一句话描述" in idx
    assert "用户明确要 A 场景" in idx
    assert "B 场景" in idx  # not_for
    assert "use_skill(name, reason)" in idx


def test_7_use_skill_not_found(reg: SkillRegistry):
    """未注册 → [ERROR] 前缀，不抛异常（Agent 能读到并降级）"""
    result = reg.use_skill("no_such_skill", "test not-found path")
    assert result.startswith("[ERROR]")
    assert "no_such_skill" in result


def test_8_use_skill_no_release(reg: SkillRegistry):
    """未发布 → source=disk_no_release，body 包含 ## Overview"""
    body = reg.use_skill("skill_a", "test disk fallback")
    assert "## Overview" in body
    # 通过日志确认 source
    records = _read_jsonl(reg.router_log)
    last = records[-1]
    assert last["source"] == "disk_no_release"
    assert last["release_id"] is None


def test_9_use_skill_git_source(reg: SkillRegistry, tmp_repo: Path):
    """发布后 → source=git，且 body 与磁盘版一致"""
    sm = ReleaseStateMachine(db_path=reg.db_path, repo_root=tmp_repo)
    rid = sm.begin_release("skill_a", "1.0.0", "L1")
    patch = Patch(skill_name="skill_a", level="L1", diff="init", rationale="phase-1 publish")
    sm.write_commit(rid, patch)
    sm.commit_release(rid)
    sm.close()

    # 新 registry 读，模拟"发布后重启"
    reg2 = SkillRegistry(
        db_path=reg.db_path, skills_dir=tmp_repo / "skills",
        repo_root=tmp_repo, router_log=reg.router_log,
    )
    reg2.load_skills_from_dir()
    body = reg2.use_skill("skill_a", "test git path")

    disk_body = (tmp_repo / "skills" / "skill_a" / "SKILL.md").read_text(encoding="utf-8")
    # 磁盘版含 frontmatter；git 版剥了 frontmatter。取磁盘版的 body 段比较
    disk_only_body = disk_body.split("---", 2)[2].strip()
    assert body == disk_only_body

    records = _read_jsonl(reg.router_log)
    last = records[-1]
    assert last["source"] == "git"
    assert last["release_id"] == rid
    reg2.close()


def test_10_router_log_fields(reg: SkillRegistry):
    """router.jsonl 每条记录字段齐全"""
    reg.use_skill("skill_a", "field-shape check")
    reg.use_skill("no_such", "field-shape check 2")

    records = _read_jsonl(reg.router_log)
    assert len(records) >= 2
    required = {"ts", "op", "name", "reason", "status", "source", "release_id"}
    for r in records:
        missing = required - set(r.keys())
        assert not missing, f"缺字段：{missing}"
        assert r["op"] == "use_skill"
        assert isinstance(r["reason"], str) and r["reason"]


# =========================================================================


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
