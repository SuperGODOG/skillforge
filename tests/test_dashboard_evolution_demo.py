"""Tests for D4a: Pi execution chain evolution evidence generation and dashboard snapshot verification.

Verifies:
1. run_dashboard_evolution_demo runs via real Pi runner with faux/scripted provider.
2. Two distinct learning failure episodes independently caught by verify_logistics_fulfillment.
3. One normal learning episode passing oracle.
4. repair_skill_failure creates READY job, valid candidate binding exactly the 2 source episode IDs,
   ratchet PASS validation record, and no fabricated parent_candidate_id.
5. caller_confirmed=False rejected before caller_confirmed=True promotes 1.0.1 via ReleaseStateMachine.
6. Runtime rebuild performs autonomous future reuse retrieving 1.0.1 and executing into new success episode.
7. Old run remains frozen at 1.0.0.
8. Local dashboard build_snapshot successfully reads the generated demo directory:
   - Runs, Episodes, Candidates, Validations, Releases, and RepairJobs all present.
   - Canonical IDs link consistently across records.
   - Read is strictly read-only: SQLite SHA256 and mtime remain unchanged.
9. Repeat invocation creates a new unique directory without overwriting prior runs.
10. Explicit evidence labels never fake real LLM or production orders.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from skillforge.dashboard_data import build_snapshot
from skillforge.scenarios.dashboard_evolution_demo import (
    BASELINE_BODY,
    PATCHED_BODY,
    run_dashboard_evolution_demo,
)


def _file_hash_and_mtime(p: Path) -> tuple[str, float]:
    content = p.read_bytes()
    h = hashlib.sha256(content).hexdigest()
    m = p.stat().st_mtime
    return h, m


def test_dashboard_evolution_demo_end_to_end_and_snapshot(tmp_path: Path):
    """End-to-end verification of evolution evidence creation and local dashboard snapshot loading."""
    result = run_dashboard_evolution_demo(output_dir=tmp_path)

    assert result["status"] == "SUCCESS"
    demo_dir: Path = result["demo_dir"]
    assert demo_dir.is_dir()

    report_path = demo_dir / "report.json"
    assert report_path.is_file()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "SUCCESS"
    assert report["evidence"]["model"] == "scripted / faux"
    assert report["evidence"]["data"] == "synthetic"
    assert report["evidence"]["execution"] == "official_pi_runner_with_agent_runtime"
    assert report["evidence"]["os_sandbox"] == "unverified"
    assert report["evidence"]["cost"] is None

    lineage = report["lineage"]
    assert lineage["job_id"] == result["job_id"]
    assert lineage["candidate_id"] == result["candidate_id"]
    assert lineage["release_id"] == result["release_id"]
    assert lineage["source_episode_ids"] == result["source_episode_ids"]
    assert lineage["baseline_version"] == "1.0.0"
    assert lineage["baseline_body"] == BASELINE_BODY
    assert lineage["promoted_version"] == "1.0.1"
    assert lineage["promoted_body"] == PATCHED_BODY
    assert lineage["patcher"]["mode"] == "scripted_fixed_text"
    assert lineage["patcher"]["call_count"] >= 1
    assert lineage["promotion_gate"]["caller_confirmed_false_rejected"] is True
    assert lineage["promotion_gate"]["caller_confirmed_true_promoted"] is True
    assert lineage["verification_boundary"]["is_isolated_demo"] is True
    assert lineage["verification_boundary"]["real_llm"] is False
    assert lineage["verification_boundary"]["scripted_outputs_do_not_measure_skill_effectiveness"] is True
    assert lineage["patcher"]["call_count"] == 1
    assert len(lineage["regression_verification"]["cases_evaluated"]) == 3

    cases = report["cases"]
    assert len(cases) == 4
    case_run_ids = [c["run_id"] for c in cases]
    assert "run_learning_fail_1" in case_run_ids
    assert "run_learning_fail_2" in case_run_ids
    assert "run_learning_normal_1" in case_run_ids
    assert "run_future_reuse_1" in case_run_ids
    assert all(c["response_text"] and c["tool_records"] for c in cases)

    # 1. Verify SQLite files exist
    db_names = ["runtime.db", "episodes.db", "candidates.db", "skills.db"]
    for db_name in db_names:
        assert (demo_dir / db_name).is_file()

    # Record hashes and mtimes before dashboard read
    pre_meta = {name: _file_hash_and_mtime(demo_dir / name) for name in db_names}

    # 2. Build local dashboard snapshot via build_snapshot
    snapshot = build_snapshot(root=tmp_path, data_dirs=[demo_dir])

    # Verify database immutability: SHA256 and mtime must not change
    post_meta = {name: _file_hash_and_mtime(demo_dir / name) for name in db_names}
    for name in db_names:
        assert pre_meta[name][0] == post_meta[name][0], f"Database {name} hash changed upon snapshot read"
        assert pre_meta[name][1] == post_meta[name][1], f"Database {name} mtime changed upon snapshot read"

    # 3. Verify snapshot runs
    snap_runs = {r["run_id"]: r for r in snapshot["runs"]}
    assert "run_learning_fail_1" in snap_runs
    assert "run_learning_fail_2" in snap_runs
    assert "run_learning_normal_1" in snap_runs
    assert "run_future_reuse_1" in snap_runs

    expected_scope = demo_dir.relative_to(tmp_path).as_posix()
    assert snap_runs["run_learning_fail_1"]["scope"] == expected_scope
    assert snap_runs["run_learning_fail_1"]["skill_version"] == "1.0.0"
    assert snap_runs["run_future_reuse_1"]["skill_version"] == "1.0.1"
    assert snap_runs["run_future_reuse_1"]["frozen_body"] == PATCHED_BODY

    # 4. Verify snapshot episodes
    snap_eps = {e["episode_id"]: e for e in snapshot["episodes"]}
    assert result["source_episode_ids"][0] in snap_eps
    assert result["source_episode_ids"][1] in snap_eps
    assert result["normal_episode_id"] in snap_eps
    assert result["future_reuse_episode_id"] in snap_eps

    ep1 = snap_eps[result["source_episode_ids"][0]]
    ep2 = snap_eps[result["source_episode_ids"][1]]
    ep_norm = snap_eps[result["normal_episode_id"]]
    ep_reuse = snap_eps[result["future_reuse_episode_id"]]

    assert ep1["outcome"] == "failure"
    assert ep2["outcome"] == "failure"
    assert ep_norm["outcome"] == "success"
    assert ep_reuse["outcome"] == "success"
    assert ep_reuse["skill_version"] == "1.0.1"
    eval_eps = [e for e in snapshot["episodes"] if e["purpose"] == "evaluation"]
    assert eval_eps and all(e["outcome"] == "unknown" for e in eval_eps)
    assert not set(result["source_episode_ids"]) & {e["episode_id"] for e in eval_eps}

    # 5. Verify snapshot candidates
    cands = [c for c in snapshot["candidates"] if c.get("candidate_id") == result["candidate_id"]]
    assert len(cands) == 1, f"Expected exactly 1 candidate with id {result['candidate_id']}"
    cand = cands[0]
    assert set(cand["source_episode_ids"]) == set(result["source_episode_ids"])
    assert cand["scope"] == expected_scope
    assert cand["parent_candidate_id"] is None, "Should not fabricate parent_candidate_id"

    # 6. Verify snapshot validations
    vals = [v for v in snapshot["validations"] if v.get("candidate_id") == result["candidate_id"]]
    assert len(vals) == 1
    val = vals[0]
    assert val["ratchet_decision"] == "PASS"
    assert val["promoted"] in (True, 1)
    assert val["release_id"] == result["release_id"]
    assert val["scope"] == expected_scope
    assert val["baseline_version"] == "1.0.0"
    assert val["content_hash"] and val["config_hash"] and val["dataset_version"]

    # 7. Verify snapshot releases
    rels = [r for r in snapshot["releases"] if r.get("release_id") == result["release_id"]]
    assert len(rels) == 1
    rel = rels[0]
    assert rel["version"] == "1.0.1"
    assert rel["status"] == "PUBLISHED"
    assert rel["scope"] == expected_scope

    # 8. Verify snapshot repair_jobs
    jobs = [j for j in snapshot["repair_jobs"] if j.get("job_id") == result["job_id"]]
    assert len(jobs) >= 1
    job_rec = jobs[0]
    assert job_rec["status"] == "PROMOTED"
    assert job_rec["latest_candidate_id"] == result["candidate_id"]
    assert job_rec["release_id"] == result["release_id"]
    assert set(job_rec["source_episode_ids"]) == set(result["source_episode_ids"])
    assert job_rec["scope"] == expected_scope

    # 9. Verify SQLite reopenability and query execution
    conn = sqlite3.connect(f"file:{demo_dir / 'runtime.db'}?mode=ro", uri=True)
    try:
        r_count = conn.execute("SELECT count(*) FROM runtime_runs").fetchone()[0]
        assert r_count >= 4
    finally:
        conn.close()

    conn_ep = sqlite3.connect(f"file:{demo_dir / 'episodes.db'}?mode=ro", uri=True)
    try:
        ep_count = conn_ep.execute("SELECT count(*) FROM episodes").fetchone()[0]
        assert ep_count >= 4
    finally:
        conn_ep.close()


def test_dashboard_evolution_demo_unique_invocation(tmp_path: Path):
    """Verify that multiple invocations generate distinct subdirectories and do not overwrite."""
    result1 = run_dashboard_evolution_demo(output_dir=tmp_path)
    result2 = run_dashboard_evolution_demo(output_dir=tmp_path)

    assert result1["demo_dir"] != result2["demo_dir"]
    assert result1["demo_dir"].is_dir()
    assert result2["demo_dir"].is_dir()

    report1 = json.loads((result1["demo_dir"] / "report.json").read_text(encoding="utf-8"))
    report2 = json.loads((result2["demo_dir"] / "report.json").read_text(encoding="utf-8"))

    assert report1["demo_id"] != report2["demo_id"]
    assert report1["status"] == "SUCCESS"
    assert report2["status"] == "SUCCESS"


def test_setup_failure_is_recorded_without_starting_pi(tmp_path, monkeypatch):
    import skillforge.scenarios.dashboard_evolution_demo as module

    def fail_git(*args, **kwargs):
        raise OSError("fixture git unavailable")

    monkeypatch.setattr(module.subprocess, "run", fail_git)
    with pytest.raises(OSError, match="fixture git unavailable"):
        run_dashboard_evolution_demo(tmp_path)
    report_file = next(tmp_path.glob("dashboard_evolution_*/report.json"))
    report = json.loads(report_file.read_text())
    assert report["status"] == "FAILED"
    assert report["failed_phase"] == "SETUP"
    assert report["cases"] == []
    assert not list(tmp_path.glob("dashboard_evolution_*/*.db"))
