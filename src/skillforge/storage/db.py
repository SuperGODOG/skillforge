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

CREATE TABLE IF NOT EXISTS episodes (
    episode_id           TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    run_id               TEXT NOT NULL,
    skill_name           TEXT NOT NULL,
    skill_version        TEXT NOT NULL,
    environment_json     TEXT NOT NULL,
    provenances_json     TEXT NOT NULL,
    acceptance_json      TEXT NOT NULL,
    verification_json    TEXT,
    outcome              TEXT NOT NULL,
    outcome_reason       TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_episodes_skill   ON episodes(skill_name, outcome);
CREATE INDEX IF NOT EXISTS idx_episodes_task    ON episodes(task_id);

CREATE TABLE IF NOT EXISTS candidate_skills (
    candidate_id         TEXT PRIMARY KEY,
    skill_name           TEXT NOT NULL,
    decision             TEXT NOT NULL,
    source_episode_ids   TEXT NOT NULL,
    status               TEXT NOT NULL,
    meta_json            TEXT NOT NULL,
    body_md              TEXT NOT NULL,
    rationale            TEXT NOT NULL,
    source_doc_id        TEXT,
    source_doc_version   TEXT,
    source_snippet_ids   TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_candidates_skill  ON candidate_skills(skill_name, status);

CREATE TABLE IF NOT EXISTS mined_batches (
    batch_fingerprint    TEXT PRIMARY KEY,
    cluster_id           TEXT NOT NULL,
    target_skill_name    TEXT NOT NULL,
    baseline_version     TEXT NOT NULL,
    decision             TEXT NOT NULL,
    candidate_id         TEXT,
    abstain_reason       TEXT,
    source_episode_ids   TEXT NOT NULL,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mined_batches_target ON mined_batches(target_skill_name);

CREATE TABLE IF NOT EXISTS repair_jobs (
    job_id               TEXT PRIMARY KEY,
    fingerprint          TEXT UNIQUE NOT NULL,
    skill_name           TEXT NOT NULL,
    baseline_version     TEXT NOT NULL,
    source_episode_ids   TEXT NOT NULL,
    responsibility_layer TEXT NOT NULL,
    strategy             TEXT,
    status               TEXT NOT NULL,
    attempts_json        TEXT NOT NULL,
    max_attempts         INTEGER NOT NULL,
    current_attempt      INTEGER NOT NULL,
    diagnosis_json       TEXT NOT NULL,
    latest_candidate_id  TEXT,
    latest_content_hash  TEXT,
    stop_reason          TEXT,
    release_id           TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_repair_jobs_skill ON repair_jobs(skill_name, status);
CREATE INDEX IF NOT EXISTS idx_repair_jobs_fp    ON repair_jobs(fingerprint);

CREATE TABLE IF NOT EXISTS deployments (
    skill_name           TEXT PRIMARY KEY,
    stable_version       TEXT NOT NULL,
    stable_release_id    TEXT,
    canary_version       TEXT,
    canary_release_id    TEXT,
    canary_share         INTEGER NOT NULL DEFAULT 0,
    rollout_id           TEXT NOT NULL,
    revision             INTEGER NOT NULL DEFAULT 1,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deployments_skill ON deployments(skill_name);

CREATE TABLE IF NOT EXISTS deployment_audit_events (
    event_id             TEXT PRIMARY KEY,
    operation_id         TEXT UNIQUE,
    skill_name           TEXT NOT NULL,
    action               TEXT NOT NULL,
    from_stable          TEXT,
    to_stable            TEXT,
    from_canary          TEXT,
    to_canary            TEXT,
    from_share           INTEGER,
    to_share             INTEGER,
    reason               TEXT NOT NULL,
    revision_before      INTEGER NOT NULL,
    revision_after       INTEGER NOT NULL,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_events_skill ON deployment_audit_events(skill_name);
CREATE INDEX IF NOT EXISTS idx_audit_events_op    ON deployment_audit_events(operation_id);

CREATE TABLE IF NOT EXISTS run_version_bindings (
    run_id               TEXT PRIMARY KEY,
    skill_name           TEXT NOT NULL,
    assigned_version     TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    is_canary            INTEGER NOT NULL,
    frozen_body          TEXT NOT NULL,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_run_bindings_skill ON run_version_bindings(skill_name);

CREATE TABLE IF NOT EXISTS runtime_runs (
    run_id               TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    skill_name           TEXT,
    skill_version        TEXT,
    content_hash         TEXT,
    status               TEXT NOT NULL,
    purpose              TEXT NOT NULL DEFAULT 'evaluation',
    budget_max           INTEGER NOT NULL DEFAULT 10,
    budget_consumed      INTEGER NOT NULL DEFAULT 0,
    deadline_ts          REAL,
    error_type           TEXT,
    error_message        TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    terminal_at          TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runtime_runs_status ON runtime_runs(status);

CREATE TABLE IF NOT EXISTS runtime_tool_calls (
    call_id              TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    tool_name            TEXT NOT NULL,
    status               TEXT NOT NULL,
    input_params_json    TEXT NOT NULL,
    output_text          TEXT,
    output_data_json     TEXT,
    error_type           TEXT,
    error_message        TEXT,
    latency_ms           REAL NOT NULL DEFAULT 0.0,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_tool_calls_run ON runtime_tool_calls(run_id);

CREATE TABLE IF NOT EXISTS semantic_facts (
    fact_id             TEXT PRIMARY KEY,
    statement           TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    scope               TEXT NOT NULL,
    topic               TEXT NOT NULL DEFAULT 'general',
    is_universal        INTEGER NOT NULL DEFAULT 0,
    tags_json           TEXT NOT NULL DEFAULT '[]',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_semantic_facts_topic  ON semantic_facts(topic);
CREATE INDEX IF NOT EXISTS idx_semantic_facts_source ON semantic_facts(source_id);
CREATE INDEX IF NOT EXISTS idx_semantic_facts_scope  ON semantic_facts(scope);

CREATE TABLE IF NOT EXISTS document_sources (
    doc_id          TEXT NOT NULL,
    version         TEXT NOT NULL,
    title           TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    content         TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doc_id, version)
);

CREATE INDEX IF NOT EXISTS idx_doc_sources_id ON document_sources(doc_id);

CREATE TABLE IF NOT EXISTS document_snippets (
    snippet_id      TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    doc_version     TEXT NOT NULL,
    section_title   TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_snippets_doc ON document_snippets(doc_id, doc_version);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """打开或创建 SQLite，执行 Schema。调用方负责关闭连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)

    # Ensure optional columns exist in releases for pre-existing DBs
    cur = conn.execute("PRAGMA table_info(releases)")
    cols = [r[1] for r in cur.fetchall()]
    for col, col_type in [
        ("content_hash", "TEXT"),
        ("meta_json", "TEXT"),
        ("body_md", "TEXT"),
        ("source_lineage_json", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE releases ADD COLUMN {col} {col_type}")

    # Ensure document columns exist in candidate_skills for pre-existing DBs
    cur = conn.execute("PRAGMA table_info(candidate_skills)")
    cand_cols = [r[1] for r in cur.fetchall()]
    for col, col_type in [
        ("source_doc_id", "TEXT"),
        ("source_doc_version", "TEXT"),
        ("source_snippet_ids", "TEXT"),
    ]:
        if col not in cand_cols:
            conn.execute(f"ALTER TABLE candidate_skills ADD COLUMN {col} {col_type}")

    conn.commit()
    return conn
