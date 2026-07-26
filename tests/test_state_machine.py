"""ReleaseStateMachine 单元 + 集成测试（Phase 3 补 pytest 版）

覆盖：
    - begin_release: UUID + PREPARING 行
    - write_commit:  Git commit 幂等 + 回写 hash
    - append_evaluation: JSONL 追加 + summary
    - commit_release: 原子切换 + skills.current_release_id 更新
    - 重复 commit 拒绝
    - watchdog_sweep 清理 + 幂等
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

from skillforge import ReleaseStateMachine
from skillforge.models import Patch, EvalResult


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """临时 git 仓库 + skills/ 子目录（write_commit 需要）"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    (tmp_path / "skills" / "s1").mkdir(parents=True)
    (tmp_path / "skills" / "s1" / "SKILL.md").write_text("stub", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    subprocess.run(["git", "-C", str(tmp_path), "add", "skills/"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "add s1"], check=True)
    return tmp_path


@pytest.fixture
def sm(tmp_repo: Path) -> ReleaseStateMachine:
    m = ReleaseStateMachine(db_path=tmp_repo / "runs" / "test.db", repo_root=tmp_repo)
    yield m
    m.close()


def test_begin_release_creates_preparing_row(sm: ReleaseStateMachine):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    assert len(rid) == 36  # UUID v4
    row = sm.get_release(rid)
    assert row and row["status"] == "PREPARING"
    assert row["commit_hash"] is None


def test_begin_release_ids_unique(sm: ReleaseStateMachine):
    ids = {sm.begin_release("s1", f"1.0.{i}", "L1") for i in range(5)}
    assert len(ids) == 5


def test_write_commit_idempotent_on_no_change(sm: ReleaseStateMachine, tmp_repo: Path):
    """无 staged 变更时 write_commit 返回 HEAD hash 不新建 commit"""
    rid = sm.begin_release("s1", "1.0.0", "L1")
    patch = Patch(skill_name="s1", level="L1", diff="no-change", rationale="test")
    h1 = sm.write_commit(rid, patch)
    h2 = sm.write_commit(rid, patch)
    assert h1 == h2, "重复调用应返回同一 commit（无实际变更）"


def test_append_evaluation_writes_jsonl(sm: ReleaseStateMachine, tmp_repo: Path):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    eval_result = EvalResult(
        release_id=rid,
        structure_score={"schema": 15, "trigger": 10, "prompt": 10, "deps": 5},
        effect_score={"task": 20, "robust": 12, "efficiency": 8, "readability": 9},
        objective_metrics={"turns": 1, "tokens": 800},
        p0_pass=True,
    )
    sm.append_evaluation(rid, eval_result)

    jsonl = tmp_repo / "runs" / "evaluations.jsonl"
    assert jsonl.exists()
    line = jsonl.read_text(encoding="utf-8").strip()
    assert rid in line
    assert '"p0_pass": true' in line


def test_commit_release_flips_to_published(sm: ReleaseStateMachine):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    sm.commit_release(rid)
    row = sm.get_release(rid)
    assert row["status"] == "PUBLISHED"
    assert row["published_at"] is not None
    # skills.current_release_id 应被更新
    assert sm.get_current_release_id("s1") == rid


def test_double_commit_release_rejects(sm: ReleaseStateMachine):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    sm.commit_release(rid)
    with pytest.raises(ValueError, match="状态非 PREPARING"):
        sm.commit_release(rid)


def test_watchdog_sweep_abandons(sm: ReleaseStateMachine):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    cleaned = sm.watchdog_sweep(threshold_hours=0)  # 立即清理
    assert cleaned >= 1
    row = sm.get_release(rid)
    assert row["status"] == "ABANDONED"


def test_watchdog_sweep_idempotent(sm: ReleaseStateMachine):
    sm.begin_release("s1", "1.0.0", "L1")
    sm.watchdog_sweep(threshold_hours=0)
    second = sm.watchdog_sweep(threshold_hours=0)
    assert second == 0, "二次 sweep 不应重复清理"


def test_watchdog_leaves_published_untouched(sm: ReleaseStateMachine):
    rid = sm.begin_release("s1", "1.0.0", "L1")
    sm.commit_release(rid)
    sm.watchdog_sweep(threshold_hours=0)
    row = sm.get_release(rid)
    assert row["status"] == "PUBLISHED", "Watchdog 不应触碰 PUBLISHED"
