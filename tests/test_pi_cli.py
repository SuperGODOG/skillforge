"""Phase 3C CLI Integration Test: Keyless Pi Execution & Logistics Oracle Demonstration.

Verifies end-to-end execution of `skillforge.cli pi-demo`:
- Subprocess invocation with cleaned environment (-u PI_SOURCE_ROOT, PYTHONPATH=src).
- Zero exit code and valid JSON output to stdout.
- Existing files in --output-dir (sentinel) are untouched.
- Generated `report.json` on disk matches stdout.
- Authoritative independent logistics Oracle passes and covers both packages (PKG_201 SF_EXPRESS, PKG_202 ZTO_EXPRESS).
- Real ToolBroker execution with 3 tool calls and 3 corresponding provenances.
- Episode recorded in SQLite episode store with matching ID and 'success' outcome.
- Shared runtime budget consumed count is exactly 7 (4 model turns + 3 tool calls).
- Pi subprocess reaped cleanly (os.kill with signal 0 raises ProcessLookupError).
- Honest evidence labels and null cost markers (tokens=None, money=None).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skillforge.episode import EpisodeStore


def test_pi_cli_demo_subprocess_e2e(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent

    # Place a sentinel file in output_dir before invocation
    sentinel_path = tmp_path / "sentinel.txt"
    sentinel_content = "sentinel_marker_data_preserved_12345"
    sentinel_path.write_text(sentinel_content, encoding="utf-8")

    env = dict(os.environ)
    env.pop("PI_SOURCE_ROOT", None)
    env["PYTHONPATH"] = "src"

    cmd = [
        sys.executable,
        "-m",
        "skillforge.cli",
        "pi-demo",
        "--output-dir",
        str(tmp_path),
    ]

    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        env=env,
        timeout=45,
        capture_output=True,
        text=True,
    )

    # 1. Exit code 0
    assert proc.returncode == 0, f"CLI exited with {proc.returncode}.\nStderr: {proc.stderr}\nStdout: {proc.stdout}"

    # 2. Sentinel file remains untouched
    assert sentinel_path.is_file()
    assert sentinel_path.read_text(encoding="utf-8") == sentinel_content

    # 3. Parse stdout JSON
    payload = json.loads(proc.stdout)
    assert isinstance(payload, dict)

    # 4. report.json exists on disk and content matches stdout
    report_file = Path(payload["report_path"])
    assert report_file.is_file()
    disk_payload = json.loads(report_file.read_text(encoding="utf-8"))
    assert disk_payload == payload

    # 5. Oracle verification passed with two packages queried
    oracle = payload["oracle"]
    assert oracle.get("verdict") == "PASS"
    assert oracle.get("independent_pass") is True
    assert oracle.get("all_delivered") is False
    assert sorted(oracle.get("queried_packages", [])) == ["PKG_201", "PKG_202"]

    # 6. Tool calls & provenances
    tool_records = payload["tool_records"]
    assert len(tool_records) == 3
    tool_names = [r["tool_name"] for r in tool_records]
    assert tool_names == ["query_order_packages", "query_package_tracking", "query_package_tracking"]

    provenances = payload["provenances"]
    assert len(provenances) == 3
    prov_tools = [p["tool_name"] for p in provenances]
    assert prov_tools == ["query_order_packages", "query_package_tracking", "query_package_tracking"]

    # 7. Episode and run metadata
    assert payload["purpose"] == "evaluation"
    assert payload["run_status"] == "COMPLETED"
    assert payload["episode_outcome"] == "success"
    assert payload["budget_max"] == 10
    assert payload["budget_consumed"] == 7  # 4 model turns + 3 tool calls

    # 8. Costs & Honest Evidence Labels
    assert payload["tokens"] is None
    assert payload["money"] is None
    assert payload["execution"] == "actual_pi_runtime_broker_collector"
    assert payload["model"] == "faux"
    assert payload["data"] == "synthetic"
    assert payload["os_sandbox"] == "not_exercised"
    labels = payload["evidence_labels"]
    assert labels["execution"] == "actual_pi_runtime_broker_collector"
    assert labels["model"] == "faux"
    assert labels["data"] == "synthetic"
    assert labels["os_sandbox"] == "not_exercised"

    # 9. SQLite Episode verified via EpisodeStore
    run_dir = Path(payload["run_dir"])
    episodes_db = run_dir / "episodes.db"
    assert episodes_db.is_file()
    ep_store = EpisodeStore(db_path=episodes_db)
    try:
        ep = ep_store.get_episode(payload["episode_id"])
        assert ep is not None
        assert ep.episode_id == payload["episode_id"]
        assert ep.outcome == "success"
        assert len(ep.provenances) == 3
    finally:
        ep_store.close()

    # 10. Reported Pi PID already reaped
    pi_pid = payload["pi_pid"]
    assert isinstance(pi_pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pi_pid, 0)
