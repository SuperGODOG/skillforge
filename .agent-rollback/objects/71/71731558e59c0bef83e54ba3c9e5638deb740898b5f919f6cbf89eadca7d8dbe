"""ReleaseStateMachine：SQLite 发布状态机（唯一事实源，ADR-06）

写入协议四步固定顺序：
    Git commit → SQLite PREPARING → JSONL append → SQLite PUBLISHED

调用序列：
    release_id = sm.begin_release(name, version, level)
    commit_hash = sm.write_commit(release_id, patch)
    sm.append_evaluation(release_id, eval_result)     # 可选
    sm.commit_release(release_id)                     # 原子切换

任一步失败不推进；Watchdog 24h 清理孤儿 PREPARING → ABANDONED。
release_id UUID v4 幂等重放。

参见 ARCHITECTURE §4-C、§5.1、§7
"""
from __future__ import annotations
import json
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import EvalResult, Patch
from .storage.db import init_db
from .storage.git_ops import commit_skill


class ReleaseStateMachine:
    def __init__(self, db_path: Path, repo_root: Optional[Path] = None):
        self.db_path = db_path
        # repo_root 默认推断：db 通常在 runs/ 下，repo 根是 db 的祖父目录
        self.repo_root = repo_root or db_path.parent.parent
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -------- Step 1 --------
    def begin_release(self, skill_name: str, version: str, level: str) -> str:
        """插入 PREPARING 行，返回新 release_id (UUID v4)"""
        release_id = str(uuid.uuid4())
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO skills(name) VALUES (?)",
            (skill_name,),
        )
        conn.execute(
            """INSERT INTO releases
                 (release_id, skill_name, version, status, level, triggered_by)
               VALUES (?, ?, ?, 'PREPARING', ?, ?)""",
            (release_id, skill_name, version, level, "manual"),
        )
        conn.commit()
        return release_id

    # -------- Step 2 --------
    def write_commit(self, release_id: str, patch: Patch) -> str:
        """Git commit patch → 回写 commit_hash"""
        row = self._get_conn().execute(
            "SELECT skill_name FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"release_id 不存在：{release_id}")
        skill_name = row[0]

        skill_dir = self.repo_root / "skills" / skill_name
        short = release_id[:8]
        message = f"skill({skill_name}): {patch.level} patch {short} - {patch.rationale[:60]}"
        commit_hash = commit_skill(self.repo_root, skill_dir, message)

        conn = self._get_conn()
        conn.execute(
            "UPDATE releases SET commit_hash = ? WHERE release_id = ?",
            (commit_hash, release_id),
        )
        conn.commit()
        return commit_hash

    # -------- Step 3 --------
    def append_evaluation(self, release_id: str, result: EvalResult) -> None:
        """JSONL 追加 + SQLite 存 summary"""
        from .storage.jsonl import append
        summary = {
            "release_id": release_id,
            "structure_score": result.structure_score,
            "effect_score": result.effect_score,
            "objective_metrics": result.objective_metrics,
            "p0_pass": result.p0_pass,
        }
        append(self.repo_root / "runs" / "evaluations.jsonl", summary)
        conn = self._get_conn()
        conn.execute(
            "UPDATE releases SET eval_summary_json = ? WHERE release_id = ?",
            (json.dumps(summary, ensure_ascii=False), release_id),
        )
        conn.commit()

    # -------- Step 4 --------
    def commit_release(self, release_id: str) -> None:
        """原子切换 PREPARING → PUBLISHED + 更新 skills.current_release_id"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT skill_name, status FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"release_id 不存在：{release_id}")
        skill_name, status = row
        if status != "PREPARING":
            raise ValueError(
                f"release {release_id} 状态非 PREPARING（实际 = {status}），无法发布"
            )

        try:
            conn.execute("BEGIN")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE releases SET status = 'PUBLISHED', published_at = ? WHERE release_id = ?",
                (now, release_id),
            )
            conn.execute(
                "UPDATE skills SET current_release_id = ? WHERE name = ?",
                (release_id, skill_name),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -------- Watchdog --------
    def watchdog_sweep(self, threshold_hours: int = 24) -> int:
        """清理 threshold_hours 未推进的 PREPARING → ABANDONED，返回清理数量

        用 SQLite 内建 datetime('now', '-Nh')，避免 Python isoformat 带时区
        与 SQLite CURRENT_TIMESTAMP（YYYY-MM-DD HH:MM:SS 无时区）格式不匹配。
        用 <= 而非 <，让同秒创建的记录也能被极限阈值（如 threshold=0）扫到。
        """
        conn = self._get_conn()
        modifier = f"-{threshold_hours} hours"
        cur = conn.execute(
            """UPDATE releases
               SET status = 'ABANDONED'
               WHERE status = 'PREPARING'
                 AND datetime(created_at) <= datetime('now', ?)""",
            (modifier,),
        )
        conn.commit()
        return cur.rowcount

    # -------- 读取辅助（T6 使用）--------
    def get_release(self, release_id: str) -> Optional[dict]:
        row = self._get_conn().execute(
            """SELECT release_id, skill_name, version, commit_hash, status, level,
                      created_at, published_at
               FROM releases WHERE release_id = ?""",
            (release_id,),
        ).fetchone()
        if not row:
            return None
        keys = ["release_id", "skill_name", "version", "commit_hash",
                "status", "level", "created_at", "published_at"]
        return dict(zip(keys, row))

    def get_current_release_id(self, skill_name: str) -> Optional[str]:
        row = self._get_conn().execute(
            "SELECT current_release_id FROM skills WHERE name = ?",
            (skill_name,),
        ).fetchone()
        return row[0] if row and row[0] else None
