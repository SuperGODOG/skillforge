"""Actual local HTTP checks; no model calls, execution APIs or writable stores."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
import sys
from threading import Thread

import pytest

from skillforge.dashboard import main, make_server, validate_roots


@contextmanager
def running(root):
    server = make_server(root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(server, path="/", method="GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_html_snapshot_headers_and_empty_root_do_not_create_data(tmp_path):
    with running(tmp_path) as server:
        status, headers, html = request(server)
        assert status == 200
        assert "证据工作台" in html.decode()
        assert html == files("skillforge").joinpath("dashboard.html").read_bytes()
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, headers, body = request(server, "/api/snapshot")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        data = json.loads(body)
        assert data["schema_version"] == 1
        assert data["runs"] == []
        assert data["benchmarks"] == []
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_write_methods_have_no_execution_route(tmp_path, method):
    with running(tmp_path) as server:
        status, _, _ = request(server, "/api/snapshot", method)
        assert status == 405
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", ["/../../etc/passwd", "/%2e%2e/.env", "/api/run", "/api/publish", "/dashboard.html", "/scratch/secret"])
def test_no_arbitrary_file_or_action_endpoints(tmp_path, route):
    with running(tmp_path) as server:
        assert request(server, route)[0] == 404


def test_foreign_host_and_origin_are_rejected(tmp_path):
    with running(tmp_path) as server:
        assert request(server, headers={"Host": "attacker.example"})[0] == 403
        assert request(server, "/api/snapshot", headers={"Origin": "https://attacker.example"})[0] == 403
        assert request(server, "/api/snapshot", headers={"Origin": "null"})[0] == 403
        assert request(server, "/api/snapshot", headers={"Origin": f"http://localhost:{server.server_port}"})[0] == 200
        assert request(server, "/favicon.ico")[0] == 204


def test_existing_db_stays_unchanged_across_requests(tmp_path):
    directory = tmp_path / "runs"
    directory.mkdir()
    path = directory / "runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime_runs (run_id TEXT, task_id TEXT, status TEXT, purpose TEXT)")
        conn.execute("INSERT INTO runtime_runs VALUES ('run-http', 'task-http', 'COMPLETED', 'evaluation')")
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    with running(tmp_path) as server:
        for _ in range(2):
            status, _, body = request(server, "/api/snapshot")
            assert status == 200
            assert any(r["run_id"] == "run-http" for r in json.loads(body)["runs"])
    after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert before == after


def test_roots_and_ports_are_checked_before_binding(tmp_path):
    with pytest.raises(FileNotFoundError):
        make_server(tmp_path / "absent", port=0)
    with pytest.raises(ValueError):
        make_server(tmp_path, port=0, data_dirs=[tmp_path.parent])
    link = tmp_path / "alias"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_roots(tmp_path, [link])
    for port in [-1, 65536, True]:
        with pytest.raises(ValueError):
            make_server(tmp_path, port=port)


def test_cli_help_and_dispatch(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--data-dir" in capsys.readouterr().out
    assert "--export-html" in capsys.readouterr().out or "--export-html" in files("skillforge").joinpath("dashboard.py").read_text(encoding="utf-8")
    from skillforge import cli, dashboard

    received = []
    monkeypatch.setattr(dashboard, "main", lambda argv: received.extend(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["skillforge", "--root", str(tmp_path), "dashboard", "--port", "8766", "--data-dir", "runs"])
    assert cli.main() == 0
    assert received == ["--root", str(tmp_path), "--port", "8766", "--data-dir", "runs"]


def test_cli_top_level_export_html_dispatch(monkeypatch, tmp_path):
    from skillforge import cli, dashboard

    received = []
    monkeypatch.setattr(dashboard, "main", lambda argv: received.extend(argv) or 0)
    monkeypatch.setattr(sys, "argv", [
        "skillforge", "--root", str(tmp_path), "dashboard", "--export-html", "dist/report.html", "--data-dir", "runs"
    ])
    assert cli.main() == 0
    assert received == ["--root", str(tmp_path), "--port", "8765", "--export-html", "dist/report.html", "--data-dir", "runs"]


def test_export_does_not_call_make_server(monkeypatch, tmp_path):
    from skillforge import dashboard

    def fail_if_called(*args, **kwargs):
        raise AssertionError("make_server should not be called in export mode")

    monkeypatch.setattr(dashboard, "make_server", fail_if_called)
    out_file = tmp_path / "export.html"
    code = dashboard.main(["--root", str(tmp_path), "--export-html", str(out_file)])
    assert code == 0
    assert out_file.is_file()


def test_export_snapshot_matches_build_snapshot_and_preserves_scores(tmp_path):
    from skillforge.dashboard import export_html
    from skillforge.dashboard_data import build_snapshot

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    db_path = runs_dir / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE runtime_runs (run_id TEXT, task_id TEXT, status TEXT, purpose TEXT)")
        conn.execute("INSERT INTO runtime_runs VALUES ('run-1', 'task-1', 'COMPLETED', 'eval')")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    p6_file = docs_dir / "p6_real_model_abc_raw_results.json"
    p6_content = {
        "metadata": {"experiment_name": "P6 Test"},
        "partitions": {
            "part1": {
                "tasks": [
                    {"task_id": "t1", "A": {"passed": True}, "B": {"passed": True}, "C": {"passed": None}},
                    {"task_id": "t2", "A": {"passed": False}, "B": {"passed": True}, "C": {"passed": False}},
                ]
            }
        },
    }
    p6_file.write_text(json.dumps(p6_content), encoding="utf-8")

    out_file = tmp_path / "report.html"
    res_path = export_html(tmp_path, out_file)
    assert res_path == out_file
    assert out_file.is_file()

    html_text = out_file.read_text(encoding="utf-8")
    prefix = '<script id="skillforge-snapshot" type="application/json">'
    suffix = '</script>'
    assert prefix in html_text
    start = html_text.index(prefix) + len(prefix)
    end = html_text.index(suffix, start)
    embedded_json = html_text[start:end]

    embedded_data = json.loads(embedded_json)
    direct_data = build_snapshot(tmp_path)

    assert embedded_data["schema_version"] == direct_data["schema_version"]
    assert embedded_data["project_name"] == direct_data["project_name"]
    assert len(embedded_data["runs"]) == len(direct_data["runs"])
    assert len(embedded_data["benchmarks"]) == len(direct_data["benchmarks"])

    bench = embedded_data["benchmarks"][0]
    p = bench["partitions"][0]
    # C unknown is preserved!
    assert p["computed_summary"]["C"]["unknown"] == 1
    assert p["computed_summary"]["B"]["passed"] == 2

    # Compare excluding generated_at
    d1 = dict(embedded_data)
    d2 = dict(direct_data)
    d1.pop("generated_at")
    d2.pop("generated_at")
    assert d1 == d2


def test_export_leaves_input_db_unchanged(tmp_path):
    from skillforge.dashboard import export_html

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    path = runs_dir / "evidence.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime_runs (run_id TEXT, task_id TEXT, status TEXT, purpose TEXT)")
        conn.execute("INSERT INTO runtime_runs VALUES ('r1', 't1', 'COMPLETED', 'test')")

    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)

    out_file = tmp_path / "snapshot.html"
    export_html(tmp_path, out_file)

    after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert before == after


def test_xss_script_breakout_and_redaction(tmp_path):
    import shutil
    import subprocess
    from skillforge.dashboard import export_html

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    report = runs_dir / "report.json"
    malicious_task = "</script><script>alert('pwned')</script> & <b>bold</b> \u2028 \u2029"
    report_data = {
        "run_id": "run-xss",
        "task_id": malicious_task,
        "status": "COMPLETED",
        "response_text": "Sensitive: Bearer secret-token-12345678901234 and sk-abcdef1234567890",
    }
    report.write_text(json.dumps(report_data, ensure_ascii=False), encoding="utf-8")

    out_file = tmp_path / "xss_test.html"
    export_html(tmp_path, out_file)

    html_content = out_file.read_text(encoding="utf-8")

    # Raw script breakout tag must NOT exist in the file
    assert "</script><script>" not in html_content
    # Secret tokens must be redacted
    assert "secret-token-12345678901234" not in html_content
    assert "sk-abcdef1234567890" not in html_content
    assert "[REDACTED]" in html_content

    # Extract JSON string and test with node if available
    prefix = '<script id="skillforge-snapshot" type="application/json">'
    suffix = '</script>'
    start = html_content.index(prefix) + len(prefix)
    end = html_content.index(suffix, start)
    embedded_json = html_content[start:end]

    if shutil.which("node"):
        res = subprocess.run(
            ["node", "-e", "const d = JSON.parse(process.argv[1]); process.stdout.write(d.runs[0].task_id);", embedded_json],
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.stdout == malicious_task


def test_export_rejects_existing_file_and_preserves_content(tmp_path):
    from skillforge.dashboard import export_html

    existing = tmp_path / "already.html"
    existing.write_text("ORIGINAL_CONTENT", encoding="utf-8")
    before_hash = hashlib.sha256(existing.read_bytes()).hexdigest()
    before_mtime = existing.stat().st_mtime_ns

    with pytest.raises(ValueError, match="目标文件已存在"):
        export_html(tmp_path, existing)

    assert existing.read_text(encoding="utf-8") == "ORIGINAL_CONTENT"
    assert hashlib.sha256(existing.read_bytes()).hexdigest() == before_hash
    assert existing.stat().st_mtime_ns == before_mtime


def test_export_rejects_symlink_and_traversal_and_home(tmp_path):
    from skillforge.dashboard import export_html

    # 1. Non-html extension
    with pytest.raises(ValueError, match="必须是 .html 结尾"):
        export_html(tmp_path, "out.txt")

    # 2. Out of bounds traversal
    with pytest.raises(ValueError, match="必须位于项目根目录内"):
        export_html(tmp_path, "../outside.html")

    # 3. Direct HOME root output
    with pytest.raises(ValueError, match="拒绝在用户 HOME 根目录下输出"):
        export_html(tmp_path, Path.home() / "forbidden.html")

    # 4. Target is symlink
    broken_link = tmp_path / "broken.html"
    broken_link.symlink_to(tmp_path / "nonexistent.html")
    with pytest.raises(ValueError, match="符号链接|已存在"):
        export_html(tmp_path, broken_link)

    # 5. Parent is symlink
    symlink_dir = tmp_path / "link_dir"
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        export_html(tmp_path, "link_dir/out.html")


def test_export_creates_parent_directory_and_cleans_temp_file(tmp_path):
    from skillforge.dashboard import export_html

    out_file = tmp_path / "deep" / "nested" / "reports" / "summary.html"
    assert not out_file.parent.exists()

    result = export_html(tmp_path, out_file)
    assert result == out_file
    assert out_file.is_file()
    # Check no temp files left
    temp_files = list(out_file.parent.glob(".tmp*"))
    assert temp_files == []


def test_corrupt_source_records_warnings_without_fabrication(tmp_path):
    from skillforge.dashboard import export_html

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    corrupt_db = runs_dir / "corrupted.db"
    corrupt_db.write_text("NOT A SQLITE DATABASE", encoding="utf-8")

    out_file = tmp_path / "warnings.html"
    export_html(tmp_path, out_file)

    html_text = out_file.read_text(encoding="utf-8")
    prefix = '<script id="skillforge-snapshot" type="application/json">'
    suffix = '</script>'
    start = html_text.index(prefix) + len(prefix)
    end = html_text.index(suffix, start)
    data = json.loads(html_text[start:end])

    assert len(data["warnings"]) > 0
    assert any("corrupted.db" in w for w in data["warnings"])
    assert data["runs"] == []


def test_offline_csp_and_online_compatibility(tmp_path):
    import shutil
    import subprocess
    from skillforge.dashboard import export_html

    out_file = tmp_path / "offline.html"
    export_html(tmp_path, out_file)
    html_text = out_file.read_text(encoding="utf-8")

    # Meta CSP in offline HTML
    assert '<meta http-equiv="Content-Security-Policy"' in html_text
    assert "connect-src 'none'" in html_text
    assert '<script id="skillforge-snapshot" type="application/json">' in html_text

    # Node execution check: verify offline mode sets offline=true, disables refresh, and does NOT fetch
    if shutil.which("node"):
        prefix_json = '<script id="skillforge-snapshot" type="application/json">'
        end_json = '</script>'
        start_j = html_text.index(prefix_json) + len(prefix_json)
        end_j = html_text.index(end_json, start_j)
        json_str = html_text[start_j:end_j]

        prefix_js = '<script>'
        start_js = html_text.rindex(prefix_js) + len(prefix_js)
        end_js = html_text.rindex('</script>')
        js_str = html_text[start_js:end_js]

        test_script = """
let fetchCalled = false;
global.fetch = () => { fetchCalled = true; throw new Error('Fetch must not be called'); };
const elements = {};
function mockElement(tag) {
  return {
    tagName: tag,
    children: [],
    textContent: '',
    className: '',
    hidden: false,
    disabled: false,
    title: '',
    append: function(...args) { this.children.push(...args); },
    replaceChildren: function(...args) { this.children = args; },
    setAttribute: function() {},
    removeAttribute: function() {},
    classList: { toggle: function() {} },
    addEventListener: function() {},
    querySelector: function(s) { return mockElement('div'); },
    querySelectorAll: function(s) { return []; }
  };
}
global.document = {
  querySelector: (s) => elements[s] || (elements[s] = mockElement('div')),
  querySelectorAll: (s) => [],
  getElementById: (id) => {
    if (id === 'skillforge-snapshot') {
      return { textContent: process.argv[1] };
    }
    return elements['#' + id] || (elements['#' + id] = mockElement('div'));
  },
  createElement: (tag) => mockElement(tag),
  createTextNode: (t) => ({ text: t })
};
global.window = global;

const js = process.argv[2];
eval(js + '; process.stdout.write(JSON.stringify({fetchCalled, offline: state.offline, schema: state.data.schema_version, disabled: document.getElementById("refresh").disabled, text: document.getElementById("refresh").textContent}));');
"""
        res = subprocess.run(
            ["node", "-e", test_script, json_str, js_str],
            capture_output=True,
            text=True,
            check=True,
        )
        verdict = json.loads(res.stdout)
        assert verdict["fetchCalled"] is False
        assert verdict["offline"] is True
        assert verdict["schema"] == 1
        assert verdict["disabled"] is True
        assert "不更新" in verdict["text"]

    # Online mode server test
    with running(tmp_path) as server:
        status, headers, online_html = request(server)
        assert status == 200
        # Online HTML does not block itself with connect-src 'none' meta tag
        assert "connect-src 'none'" not in online_html.decode()
        # Header permits same-origin connect-src
        assert "connect-src 'self'" in headers["Content-Security-Policy"]


def test_atomic_failure_cleanup(monkeypatch, tmp_path):
    import os
    from skillforge.dashboard import export_html

    def fake_link(src, dst):
        raise OSError("Simulated disk error during link")

    monkeypatch.setattr(os, "link", fake_link)

    out_file = tmp_path / "failed.html"
    with pytest.raises(OSError, match="Simulated disk error"):
        export_html(tmp_path, out_file)

    assert not out_file.exists()
    assert list(tmp_path.glob(".tmp*")) == []


def test_cli_export_error_exits_nonzero(tmp_path):
    from skillforge.dashboard import main as dashboard_main

    # 1. Non-existent root
    with pytest.raises(SystemExit) as exc:
        dashboard_main(["--root", str(tmp_path / "missing"), "--export-html", "out.html"])
    assert exc.value.code != 0

    # 2. Existing file
    existing = tmp_path / "exists.html"
    existing.write_text("OLD", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        dashboard_main(["--root", str(tmp_path), "--export-html", str(existing)])
    assert exc.value.code != 0


def test_export_rejects_symlink_above_existing_nested_parent(tmp_path):
    from skillforge.dashboard import export_html
    (tmp_path / "real" / "nested").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        export_html(tmp_path, "alias/nested/snapshot.html")
    assert not (tmp_path / "real" / "nested" / "snapshot.html").exists()
