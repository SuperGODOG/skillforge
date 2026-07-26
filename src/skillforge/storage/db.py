"""SQLite 封装：Schema 定义 + 打开/初始化

参见 ARCHITECTURE §5.1
skills.current_release_id 必须指向 status='PUBLISHED' 的 releases 行。
同一 skill 同时最多 1 条 PREPARING（Watchdog 兜底）。
"""
from __future__ import annotations
from pathlib import Path
import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skills (
    name                 TEXT PRIMARY KEY,
    current_release_id   TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (current_release_id) REFERENCES releases(release_id)
);

CREATE TABLE IF NOT EXISTS releases (
    release_id           TEXT PRIMARY KEY,
    skill_name           TEXT NOT NULL,
    version              TEXT NOT NULL,
    commit_hash          TEXT,
    status               TEXT NOT NULL,
    level                TEXT,
    triggered_by         TEXT,
    eval_summary_json    TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at         TIMESTAMP,
    FOREIGN KEY (skill_name) REFERENCES skills(name)
);

CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status);
CREATE INDEX IF NOT EXISTS idx_releases_skill  ON releases(skill_name, status);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """打开或创建 SQLite，执行 Schema。调用方负责关闭连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
