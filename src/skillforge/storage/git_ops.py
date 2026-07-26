"""Git 命令包装：commit / read-at-commit

skills/ 是 skillForge 主仓库的子目录，走同一份 git 历史。
"""
from __future__ import annotations
import subprocess
from pathlib import Path


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def commit_skill(repo_root: Path, skill_dir: Path, message: str) -> str:
    """git add <skill_dir> + git commit，返回 commit hash

    幂等：若无 staged 变更（比如重复调用），返回当前 HEAD hash 不新建 commit。
    """
    try:
        rel = skill_dir.relative_to(repo_root)
    except ValueError:
        rel = skill_dir  # 已经是相对路径

    _run_git(repo_root, "add", str(rel))

    # 判断是否有 staged 变更
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_root),
    )
    if diff.returncode == 0:
        return _run_git(repo_root, "rev-parse", "HEAD").strip()

    _run_git(repo_root, "commit", "-m", message)
    return _run_git(repo_root, "rev-parse", "HEAD").strip()


def read_file_at_commit(repo_root: Path, commit_hash: str, rel_path: str) -> str:
    """读指定 commit 版本的文件内容（use_skill 从历史版本读 Body 时用）"""
    return _run_git(repo_root, "show", f"{commit_hash}:{rel_path}")


def current_head(repo_root: Path) -> str:
    return _run_git(repo_root, "rev-parse", "HEAD").strip()
