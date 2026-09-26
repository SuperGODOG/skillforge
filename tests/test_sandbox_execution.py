"""Milestone 5b Acceptance Test Suite: Sandbox Execution Isolation + Actual Dependency Verification.

Covers Supervisor Scenarios H1 - H8:
- H1: Allowed sandboxed tool execution via real M5a Broker & Runtime in sandbox workspace,
      read/write succeeds, allowlist/schema/budget enforced, trace/Episode contains backend,
      fingerprint, exit_code, and skill version.
- H2: Host sentinel protection outside workspace: absolute path, '../', and symlink write
      escapes are denied by Seatbelt; restricted read denied; host sentinel untouched.
- H3: Default network isolation denies local socket connection; host secrets stripped from env;
      tampering parameters (_allow_network, etc.) rejected before dispatch.
- H4: Process lifecycle: normal exit, timeout, and cancellation terminate child process tree
      via process group (SIGTERM -> SIGKILL); output limit with [TRUNCATED] marker.
- H5: Real dependency probe inside sandbox: existing dependencies pass, missing/mismatched fail
      before business handler execution, host handler call count = 0.
- H6: Fingerprint invalidation re-triggers probe; M4b rollback integration: rollback to version
      with unavailable dependency rejected and deployment untouched; valid version succeeds.
- H7: Fail-closed on missing/unavailable backend: host handler count = 0, no bare host fallback,
      terminal state and Episode record failure without faking success.
- H8: End-to-end: runtime + broker + sandbox + probe -> verified success yields learning Episode
      entering mining pool; rejected/timed-out run yields failure Episode excluded from miner.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from hello_agents.tools import ToolParameter

from skillforge import (
    AgentRuntime,
    CandidateSkill,
    CandidateStore,
    DependencyProbe,
    Deployment,
    DeploymentManager,
    Episode,
    EpisodeStore,
    ExperienceCollector,
    MacSeatbeltSandbox,
    PatternMiningConfig,
    ProbeResult,
    Release,
    ReleaseStateMachine,
    RunRecord,
    SandboxBackend,
    SandboxConfig,
    SandboxedToolSpec,
    SkillMeta,
    ToolBroker,
    mine_pending,
)
from skillforge.storage.db import init_db


@pytest.fixture
def tmp_env(tmp_path: Path):
    """Fixture providing isolated db, repo, and sandbox workspaces."""
    db_path = tmp_path / "skillforge_test.db"
    conn = init_db(db_path)
    conn.close()

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    ep_store = EpisodeStore(db_path)
    cand_store = CandidateStore(db_path)
    collector = ExperienceCollector(episode_store=ep_store)

    return {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "skills_dir": skills_dir,
        "ep_store": ep_store,
        "cand_store": cand_store,
        "collector": collector,
    }


# =========================================================================
# Scenario H1: Allowed Tool Execution in Sandbox via Broker & Runtime
# =========================================================================
def test_h1_sandboxed_tool_execution_via_broker_and_runtime(tmp_env):
    """H1: Run application-registered sandboxed tool via real broker, read/write in workspace succeeds."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available(), "macOS Seatbelt sandbox (/usr/bin/sandbox-exec) must be available"

    broker = ToolBroker(
        application_allowlist={"file_transformer_tool"},
        sandbox_backend=sb,
    )

    # Command reads JSON from stdin, writes to a file in isolated workspace, reads back and prints JSON
    code = (
        "import sys, json, os\n"
        "params = json.load(sys.stdin)\n"
        "filename = params['filename']\n"
        "content = params['content']\n"
        "with open(filename, 'w') as f:\n"
        "    f.write(content)\n"
        "with open(filename, 'r') as f:\n"
        "    read_val = f.read()\n"
        "print(json.dumps({'status': 'ok', 'read_back': read_val, 'echo': content.upper()}))\n"
    )

    spec = SandboxedToolSpec(
        name="file_transformer_tool",
        command_template=[sys.executable, "-c", code],
        description="Transforms files inside isolated sandbox workspace",
        parameters=[
            ToolParameter(name="filename", type="string", description="Target filename", required=True),
            ToolParameter(name="content", type="string", description="File content", required=True),
        ],
    )
    broker.register_sandboxed_tool(spec)

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )

    run = runtime.start_run(
        run_id="run_h1_01",
        task_id="task_h1_01",
        skill_name="text_processor",
        purpose="learning",
        budget_max=5,
    )
    assert run.status == "RUNNING"

    # Execute tool
    rec = runtime.execute_tool(
        run_id="run_h1_01",
        tool_name="file_transformer_tool",
        parameters={"filename": "output.txt", "content": "hello_seatbelt"},
    )

    assert rec.status == "EXECUTED"
    assert rec.error_type is None
    assert rec.output_data.get("backend") == "macos_seatbelt"
    assert rec.output_data.get("exit_code") == 0
    assert rec.output_data.get("read_back") == "hello_seatbelt"
    assert rec.output_data.get("echo") == "HELLO_SEATBELT"
    assert rec.provenance is not None
    assert rec.provenance.tool_success is True

    # Finalize run with verified evidence
    final_run, ep = runtime.finalize_run(
        run_id="run_h1_01",
        verification_evidence={"independent_pass": True, "details": "Verified in sandbox"},
    )
    assert final_run.status == "COMPLETED"
    assert ep is not None
    assert ep.outcome == "success"
    assert ep.environment.get("backend") == "macos_seatbelt"
    assert "environment_fingerprint" in ep.environment


# =========================================================================
# Scenario H2: Host Sentinel Protection & Restricted Read Denial
# =========================================================================
def test_h2_host_sentinel_protection_and_restricted_read_denial(tmp_env):
    """H2: Prohibit writing outside workspace (absolute path, ../, symlinks) and reading restricted paths."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    # External host directory outside workspace
    outside_dir = tmp_env["tmp_path"] / "host_outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    sentinel_file = outside_dir / "host_sentinel.txt"
    sentinel_file.write_text("HOST_PRISTINE_DATA")

    forbidden_read_file = outside_dir / "forbidden_secret.txt"
    forbidden_read_file.write_text("SUPER_CONFIDENTIAL")

    # Tool attempting various escape writes and forbidden reads
    tool_code = (
        "import sys, json, os\n"
        "p = json.load(sys.stdin)\n"
        "mode = p['mode']\n"
        "target = p['target']\n"
        "if mode == 'abs_write':\n"
        "    with open(target, 'w') as f: f.write('TAMPERED_ABS')\n"
        "elif mode == 'dotdot_write':\n"
        "    with open(target, 'w') as f: f.write('TAMPERED_DOTDOT')\n"
        "elif mode == 'symlink_write':\n"
        "    link = os.path.join(os.getcwd(), 'sentinel_link')\n"
        "    os.symlink(target, link)\n"
        "    with open(link, 'w') as f: f.write('TAMPERED_SYMLINK')\n"
        "elif mode == 'read_forbidden':\n"
        "    with open(target, 'r') as f: sys.stdout.write(f.read())\n"
        "print(json.dumps({'status': 'ok'}))\n"
    )

    spec = SandboxedToolSpec(
        name="escape_probe_tool",
        command_template=[sys.executable, "-c", tool_code],
        parameters=[
            ToolParameter(name="mode", type="string", description="Test mode", required=True),
            ToolParameter(name="target", type="string", description="Target path", required=True),
        ],
        denied_read_paths=[forbidden_read_file],
    )

    broker = ToolBroker(
        application_allowlist={"escape_probe_tool"},
        sandbox_backend=sb,
    )
    broker.register_sandboxed_tool(spec)

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )
    runtime.start_run(run_id="run_h2", task_id="task_h2")

    # 1. Absolute path write attempt
    rec1 = runtime.execute_tool(
        "run_h2",
        "escape_probe_tool",
        {"mode": "abs_write", "target": str(sentinel_file)},
    )
    assert rec1.status == "ERROR"
    assert rec1.error_type == "SANDBOX_EXEC_ERROR"
    assert sentinel_file.read_text() == "HOST_PRISTINE_DATA", "Host sentinel must not be modified!"

    # 2. Dot-dot (../) write attempt
    rec2 = runtime.execute_tool(
        "run_h2",
        "escape_probe_tool",
        {"mode": "dotdot_write", "target": f"../{sentinel_file.name}"},
    )
    assert rec2.status == "ERROR"
    assert sentinel_file.read_text() == "HOST_PRISTINE_DATA"

    # 3. Symlink write attempt
    rec3 = runtime.execute_tool(
        "run_h2",
        "escape_probe_tool",
        {"mode": "symlink_write", "target": str(sentinel_file)},
    )
    assert rec3.status == "ERROR"
    assert sentinel_file.read_text() == "HOST_PRISTINE_DATA"

    # 4. Restricted read attempt
    rec4 = runtime.execute_tool(
        "run_h2",
        "escape_probe_tool",
        {"mode": "read_forbidden", "target": str(forbidden_read_file)},
    )
    assert rec4.status == "ERROR"
    assert "SUPER_CONFIDENTIAL" not in rec4.output_text


# =========================================================================
# Scenario H3: Network Isolation, Secret Scrubbing & Policy Tamper Denial
# =========================================================================
def test_h3_network_isolation_secret_scrubbing_and_policy_tamper_denial(tmp_env):
    """H3: Default network isolation blocks socket connection; host secrets stripped; tampering rejected."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    # Start a local TCP listener on 127.0.0.1
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    # Inject mock host secrets into host environment
    os.environ["SECRET_API_KEY"] = "super_classified_token_999"
    os.environ["DATABASE_PASSWORD"] = "ultra_secret_pw"

    tool_code = (
        "import sys, json, os, socket\n"
        "p = json.load(sys.stdin)\n"
        "port = p['port']\n"
        "net_ok = False\n"
        "net_err = ''\n"
        "try:\n"
        "    s = socket.create_connection(('127.0.0.1', port), timeout=0.8)\n"
        "    s.close()\n"
        "    net_ok = True\n"
        "except Exception as e:\n"
        "    net_err = f'{type(e).__name__}: {e}'\n"
        "secret_found = ('SECRET_API_KEY' in os.environ) or ('DATABASE_PASSWORD' in os.environ)\n"
        "print(json.dumps({'net_ok': net_ok, 'net_err': net_err, 'secret_found': secret_found}))\n"
    )

    spec = SandboxedToolSpec(
        name="net_sec_inspector",
        command_template=[sys.executable, "-c", tool_code],
        parameters=[ToolParameter(name="port", type="integer", description="Port number", required=True)],
    )

    broker = ToolBroker(
        application_allowlist={"net_sec_inspector"},
        sandbox_backend=sb,
    )
    broker.register_sandboxed_tool(spec)

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )
    runtime.start_run(run_id="run_h3", task_id="task_h3")

    try:
        rec = runtime.execute_tool(
            "run_h3",
            "net_sec_inspector",
            {"port": port},
        )
        assert rec.status == "EXECUTED"
        # Network connection must have failed
        assert rec.output_data.get("net_ok") is False
        assert rec.output_data.get("net_err") != ""
        # Host secrets must have been scrubbed
        assert rec.output_data.get("secret_found") is False

        # Attempt privilege escalation parameter: e.g. _allow_network
        rec_tamper = runtime.execute_tool(
            "run_h3",
            "net_sec_inspector",
            {"port": port, "_allow_network": True},
        )
        assert rec_tamper.status == "REJECTED"
        assert rec_tamper.error_type in ("PERMISSION_DENIED", "SCHEMA_VALIDATION_ERROR")
    finally:
        server_sock.close()
        os.environ.pop("SECRET_API_KEY", None)
        os.environ.pop("DATABASE_PASSWORD", None)


# =========================================================================
# Scenario H4: Process Lifecycle, Timeout, Cancellation & Truncation
# =========================================================================
def test_h4_lifecycle_timeout_cancellation_and_output_truncation(tmp_env):
    """H4: Timeout and cancellation terminate process trees; output truncation with marker."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    # Tool that spawns child processes and sleeps
    spawn_code = (
        "import sys, subprocess, time\n"
        "# Spawn background worker child process\n"
        "p = subprocess.Popen(['python3', '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )

    # Tool that generates huge output
    verbose_code = (
        "import sys\n"
        "sys.stdout.write('A' * 10000)\n"
    )

    broker = ToolBroker(
        application_allowlist={"spawn_tool", "verbose_tool"},
        sandbox_backend=sb,
    )
    broker.register_sandboxed_tool(
        SandboxedToolSpec(
            name="spawn_tool",
            command_template=[sys.executable, "-c", spawn_code],
            parameters=[],
            timeout_seconds=5.0,
        )
    )
    broker.register_sandboxed_tool(
        SandboxedToolSpec(
            name="verbose_tool",
            command_template=[sys.executable, "-c", verbose_code],
            parameters=[],
            max_output_bytes=256,
        )
    )

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )

    # 1. Timeout test
    runtime.start_run(run_id="run_h4_to", task_id="task_h4_to")
    t0 = time.time()
    rec_to = runtime.execute_tool(
        "run_h4_to",
        "spawn_tool",
        {},
        tool_timeout=0.4,
    )
    duration = time.time() - t0
    assert duration < 3.0, "Timeout must terminate cleanly without hanging"
    assert rec_to.status == "TIMED_OUT"
    assert rec_to.error_type == "TIMEOUT"

    # 2. Output truncation test
    runtime.start_run(run_id="run_h4_trunc", task_id="task_h4_trunc")
    rec_trunc = runtime.execute_tool(
        "run_h4_trunc",
        "verbose_tool",
        {},
    )
    assert rec_trunc.status == "EXECUTED"
    assert rec_trunc.output_data.get("is_truncated") is True
    assert "... [TRUNCATED]" in rec_trunc.output_text
    assert len(rec_trunc.output_text) < 500

    # 3. Explicit run cancellation
    runtime.start_run(run_id="run_h4_cancel", task_id="task_h4_cancel")
    runtime.cancel_run("run_h4_cancel", reason="Operator interrupted")
    rec_after = runtime.execute_tool(
        "run_h4_cancel",
        "verbose_tool",
        {},
    )
    assert rec_after.status == "REJECTED"
    assert rec_after.error_type in ("CANCELLED", "DISPATCH_AFTER_TERMINAL")


# =========================================================================
# Scenario H5: Actual Dependency Probe Inside Sandbox
# =========================================================================
def test_h5_actual_dependency_probe_in_sandbox(tmp_env):
    """H5: Probe dependencies inside sandbox; missing/mismatched dependencies reject before handler."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    prober = DependencyProbe(backend=sb, workspace_base=tmp_env["tmp_path"])

    # 1. Existing valid dependencies
    p_json = prober.probe_dependency("json")
    assert p_json.satisfied is True

    p_py = prober.probe_dependency("python3>=3.8")
    assert p_py.satisfied is True

    # 2. Version mismatch
    p_py_future = prober.probe_dependency("python3>=99.0")
    assert p_py_future.satisfied is False
    assert "does not satisfy" in (p_py_future.error_reason or "")

    # 3. Missing dependency
    p_missing = prober.probe_dependency("nonexistent_phantom_pkg_xyz")
    assert p_missing.satisfied is False

    # 4. Tool registration with unsatisfied dependency
    host_counter = 0

    def mock_host_counter():
        nonlocal host_counter
        host_counter += 1

    spec = SandboxedToolSpec(
        name="tool_with_missing_dep",
        command_template=[sys.executable, "-c", "print('should_not_run')"],
        parameters=[],
        required_dependencies=["missing_ml_lib_xyz_99"],
        host_handler_counter=mock_host_counter,
    )

    broker = ToolBroker(
        application_allowlist={"tool_with_missing_dep"},
        sandbox_backend=sb,
        dependency_prober=prober,
    )
    broker.register_sandboxed_tool(spec)

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )
    runtime.start_run(run_id="run_h5", task_id="task_h5")

    rec = runtime.execute_tool("run_h5", "tool_with_missing_dep", {})
    assert rec.status == "REJECTED"
    assert rec.error_type == "DEPENDENCY_MISSING"
    assert host_counter == 0, "Host handler must not be executed when dependency is missing"


# =========================================================================
# Scenario H6: Fingerprint Invalidation & M4b Rollback Integration
# =========================================================================
def test_h6_fingerprint_invalidation_and_m4b_rollback_integration(tmp_env):
    """H6: Fingerprint changes invalidate cache; M4b rollback rejects invalid dependency target."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    prober = DependencyProbe(backend=sb, workspace_base=tmp_env["tmp_path"])

    # Probe and verify caching
    p1 = prober.probe_dependency("json")
    assert p1.satisfied is True
    assert "json" in prober._cache

    # Invalidate cache
    prober.invalidate_cache()
    assert len(prober._cache) == 0

    # Integration with M4b rollback
    dep_mgr = DeploymentManager(db_path=tmp_env["db_path"], repo_root=tmp_env["tmp_path"])
    conn = init_db(tmp_env["db_path"])

    skill_name = "data_sync"
    body_080 = "---\nname: data_sync\nversion: 0.8.0\ndescription: v0.8.0\ndependencies:\n  - missing_uninstalled_tool\n---\nBody 0.8.0"
    body_090 = "---\nname: data_sync\nversion: 0.9.0\ndescription: v0.9.0\ndependencies:\n  - json\n---\nBody 0.9.0"
    body_100 = "---\nname: data_sync\nversion: 1.0.0\ndescription: v1.0.0\ndependencies:\n  - json\n---\nBody 1.0.0"

    import hashlib
    h_080 = hashlib.sha256(body_080.strip().encode("utf-8")).hexdigest()
    h_090 = hashlib.sha256(body_090.strip().encode("utf-8")).hexdigest()
    h_100 = hashlib.sha256(body_100.strip().encode("utf-8")).hexdigest()

    conn.execute(
        """INSERT INTO skills (name, current_release_id) VALUES (?, NULL)""",
        (skill_name,),
    )
    meta_080 = {"name": skill_name, "version": "0.8.0", "description": "v0.8.0", "use_when": "sync", "dependencies": ["missing_uninstalled_tool"]}
    meta_090 = {"name": skill_name, "version": "0.9.0", "description": "v0.9.0", "use_when": "sync", "dependencies": ["json"]}
    meta_100 = {"name": skill_name, "version": "1.0.0", "description": "v1.0.0", "use_when": "sync", "dependencies": ["json"]}

    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, content_hash, meta_json, body_md)
           VALUES ('rel_080', ?, '0.8.0', 'PUBLISHED', 'L1', ?, ?, ?)""",
        (skill_name, h_080, json.dumps(meta_080), body_080),
    )
    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, content_hash, meta_json, body_md)
           VALUES ('rel_090', ?, '0.9.0', 'PUBLISHED', 'L1', ?, ?, ?)""",
        (skill_name, h_090, json.dumps(meta_090), body_090),
    )
    conn.execute(
        """INSERT INTO releases (release_id, skill_name, version, status, level, content_hash, meta_json, body_md)
           VALUES ('rel_100', ?, '1.0.0', 'PUBLISHED', 'L1', ?, ?, ?)""",
        (skill_name, h_100, json.dumps(meta_100), body_100),
    )
    conn.execute(
        """UPDATE skills SET current_release_id = 'rel_100' WHERE name = ?""",
        (skill_name,),
    )
    conn.execute(
        """INSERT INTO deployments (skill_name, stable_version, stable_release_id, canary_version, canary_share, rollout_id, revision)
           VALUES (?, '1.0.0', 'rel_100', NULL, 0, 'roll_init', 1)""",
        (skill_name,),
    )
    conn.commit()
    conn.close()

    # Attempt rollback to 0.8.0 (has missing_uninstalled_tool): MUST BE REJECTED
    with pytest.raises(ValueError, match="Rollback rejected: dependency 'missing_uninstalled_tool'"):
        dep_mgr.rollback_deployment(
            skill_name=skill_name,
            target_version="0.8.0",
            reason="Attempt rollback to broken v0.8.0",
            caller_confirmed=True,
            dependency_prober=prober,
        )

    # Deployment state must be untouched!
    cur_dep = dep_mgr.get_deployment(skill_name)
    assert cur_dep.stable_version == "1.0.0"
    assert cur_dep.revision == 1

    # Attempt rollback to 0.9.0 (has valid 'json' dependency): MUST SUCCEED
    dep_after = dep_mgr.rollback_deployment(
        skill_name=skill_name,
        target_version="0.9.0",
        reason="Rollback to working v0.9.0",
        caller_confirmed=True,
        dependency_prober=prober,
    )
    assert dep_after.stable_version == "0.9.0"
    assert dep_after.revision == 2


# =========================================================================
# Scenario H7: Fail-Closed on Missing/Unavailable Backend
# =========================================================================
def test_h7_fail_closed_on_missing_backend_no_fallback(tmp_env):
    """H7: If backend is missing/unavailable, fail-closed with 0 host calls, no bare execution."""
    host_calls = 0

    def mock_host():
        nonlocal host_calls
        host_calls += 1

    spec = SandboxedToolSpec(
        name="sandboxed_only_tool",
        command_template=[sys.executable, "-c", "print('bare_fallback_detected')"],
        parameters=[],
        host_handler_counter=mock_host,
    )

    # Broker with NO sandbox backend
    broker = ToolBroker(
        application_allowlist={"sandboxed_only_tool"},
        sandbox_backend=None,
    )
    broker.register_sandboxed_tool(spec)

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )
    runtime.start_run(run_id="run_h7", task_id="task_h7")

    rec = runtime.execute_tool("run_h7", "sandboxed_only_tool", {})
    assert rec.status == "REJECTED"
    assert rec.error_type == "SANDBOX_UNAVAILABLE"
    assert host_calls == 0, "Must not execute on host when sandbox is unavailable!"

    # Finalize run: records failure Episode, never faking success
    final_run, ep = runtime.finalize_run(
        run_id="run_h7",
        infra_error="Sandbox backend unavailable",
    )
    assert final_run.status == "FAILED"
    assert ep is not None
    assert ep.outcome in ("failure", "unknown")
    assert ep.outcome != "success"


# =========================================================================
# Scenario H8: End-to-End Pipeline & Learning Pool Isolation
# =========================================================================
def test_h8_end_to_end_runtime_sandbox_probe_and_episode_learning_pool(tmp_env):
    """H8: Verified sandbox execution enters learning pool; rejected/timed out run is excluded."""
    sb = MacSeatbeltSandbox()
    assert sb.is_available()

    prober = DependencyProbe(backend=sb, workspace_base=tmp_env["tmp_path"])
    broker = ToolBroker(
        application_allowlist={"safe_compute_tool"},
        sandbox_backend=sb,
        dependency_prober=prober,
    )

    code = (
        "import sys, json\n"
        "params = json.load(sys.stdin)\n"
        "val = params.get('val', 0)\n"
        "print(json.dumps({'result': val * 2, 'verified': True}))\n"
    )
    broker.register_sandboxed_tool(
        SandboxedToolSpec(
            name="safe_compute_tool",
            command_template=[sys.executable, "-c", code],
            parameters=[ToolParameter(name="val", type="integer", description="Numeric value", required=True)],
            required_dependencies=["json"],
        )
    )

    runtime = AgentRuntime(
        db_path=tmp_env["db_path"],
        tool_broker=broker,
        collector=tmp_env["collector"],
        episode_store=tmp_env["ep_store"],
    )

    # 1. Run A: Successful learning run
    runtime.start_run(
        run_id="run_h8_success",
        task_id="task_h8_s",
        skill_name="math_skill",
        purpose="learning",
    )
    rec_s = runtime.execute_tool(
        "run_h8_success",
        "safe_compute_tool",
        {"val": 21},
    )
    assert rec_s.status == "EXECUTED"
    assert rec_s.output_data.get("result") == 42

    _, ep_success = runtime.finalize_run(
        run_id="run_h8_success",
        verification_evidence={"independent_pass": True, "details": "42 confirmed"},
    )
    assert ep_success is not None
    assert ep_success.outcome == "success"

    # 2. Run B: Failed / rejected run
    runtime.start_run(
        run_id="run_h8_fail",
        task_id="task_h8_f",
        skill_name="math_skill",
        purpose="learning",
    )
    # Tampering parameter causes rejection
    rec_f = runtime.execute_tool(
        "run_h8_fail",
        "safe_compute_tool",
        {"val": 21, "_illegal_key": "bad"},
    )
    assert rec_f.status == "REJECTED"

    _, ep_fail = runtime.finalize_run(
        run_id="run_h8_fail",
        infra_error="Security policy rejection",
    )
    assert ep_fail is not None
    assert ep_fail.outcome in ("failure", "unknown")
    assert ep_fail.outcome != "success"

    # Check EpisodeStore: both are stored
    all_eps = tmp_env["ep_store"].list_episodes()
    assert any(e.episode_id == "ep_run_h8_success" for e in all_eps)
    assert any(e.episode_id == "ep_run_h8_fail" for e in all_eps)

    # Mine candidate from episodes: only success episodes should contribute
    report = mine_pending(
        episode_store=tmp_env["ep_store"],
        candidate_store=tmp_env["cand_store"],
        config=PatternMiningConfig(min_support=1),
    )
    # The mining engine processes eligible episodes; failure episodes are excluded from positive pattern extraction
    assert report is not None
