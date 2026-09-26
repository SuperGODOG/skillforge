"""Milestone 5b: Sandbox Execution Isolation + Actual Dependency Verification.

Provides:
- SandboxConfig: Configuration for isolated execution (workspace, network policy, output limits, env whitelist).
- SandboxResult: Subprocess execution outcome with exit code, outputs, truncation, and latency.
- SandboxBackend / MacSeatbeltSandbox: Real OS-level containment using macOS Seatbelt (/usr/bin/sandbox-exec)
  enforcing ephemeral workspace write isolation, forbidden read denials, network isolation, and
  process tree termination (SIGTERM -> SIGKILL).
- DependencyProbe: Probing tool binaries and Python module dependencies directly within the sandbox
  with environment fingerprint binding and cache invalidation.
- SandboxedToolSpec: Contract for tools dispatched into isolated sandbox execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from hello_agents.tools import ToolParameter

REDACT_KEYS_RE = re.compile(
    r"(?i)(password|secret|token|credential|api_key|private_key|auth|bearer)"
)


@dataclass
class SandboxConfig:
    """Configuration for an isolated execution environment."""

    workspace_dir: Path
    allowed_read_paths: list[Path] = field(default_factory=list)
    denied_read_paths: list[Path] = field(default_factory=list)
    allow_network: bool = False
    timeout_seconds: float = 10.0
    max_output_bytes: int = 65536
    environment_whitelist: set[str] = field(
        default_factory=lambda: {
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SYSTEMROOT",
        }
    )
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxResult:
    """Outcome of an isolated sandbox execution."""

    exit_code: int
    stdout: str
    stderr: str
    is_timeout: bool = False
    is_cancelled: bool = False
    is_truncated: bool = False
    backend: str = "macos_seatbelt"
    latency_ms: float = 0.0
    error_type: Optional[str] = None
    error_message: Optional[str] = None


class SandboxBackend:
    """Abstract base class for OS / container sandbox execution backends."""

    name: str = "abstract_sandbox"

    def is_available(self) -> bool:
        """Check if backend is installed, accessible, and operational."""
        raise NotImplementedError

    def execute(
        self,
        cmd: list[str],
        config: SandboxConfig,
        input_str: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> SandboxResult:
        """Execute command in sandbox."""
        raise NotImplementedError

    def cancel_run(self, run_id: str) -> bool:
        """Cancel and terminate any ongoing execution for the given run_id."""
        return False


def terminate_process_tree(proc: subprocess.Popen, timeout_grace: float = 0.4) -> None:
    """Safely terminate a process and its child processes via its process group."""
    if proc.poll() is not None:
        return

    pid = proc.pid
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    t0 = time.time()
    while time.time() - t0 < timeout_grace:
        if proc.poll() is not None:
            return
        time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


class MacSeatbeltSandbox(SandboxBackend):
    """macOS Seatbelt OS isolation backend using /usr/bin/sandbox-exec."""

    name: str = "macos_seatbelt"

    def __init__(self, sandbox_exec_path: Optional[str] = None):
        self.sandbox_exec_path = (
            sandbox_exec_path
            or shutil.which("sandbox-exec")
            or "/usr/bin/sandbox-exec"
        )
        self._lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._available_cache: Optional[bool] = None

    def is_available(self) -> bool:
        """Verify that /usr/bin/sandbox-exec exists and can execute a basic profile."""
        if self._available_cache is not None:
            return self._available_cache

        if not os.path.exists(self.sandbox_exec_path) or not os.access(
            self.sandbox_exec_path, os.X_OK
        ):
            self._available_cache = False
            return False

        try:
            res = subprocess.run(
                [self.sandbox_exec_path, "-p", "(version 1) (allow default)", "true"],
                capture_output=True,
                timeout=3.0,
            )
            self._available_cache = res.returncode == 0
        except Exception:
            self._available_cache = False

        return self._available_cache

    def cancel_run(self, run_id: str) -> bool:
        """Cancel running execution for a run_id."""
        with self._lock:
            proc = self._active_processes.get(run_id)
        if proc and proc.poll() is None:
            terminate_process_tree(proc, timeout_grace=0.2)
            return True
        return False

    def _build_profile(self, config: SandboxConfig) -> str:
        """Generate Seatbelt SBPL profile enforcing write, read, and network boundaries."""
        ws_real = os.path.realpath(str(config.workspace_dir))
        lines = [
            "(version 1)",
            "(allow default)",
        ]

        # 1. Network isolation
        if not config.allow_network:
            lines.append("(deny network*)")

        # 2. File write isolation: deny all writes, allow only workspace and dev devices
        lines.append("(deny file-write*)")
        lines.append(f'(allow file-write* (subpath "{ws_real}"))')
        lines.append('(allow file-write* (subpath "/dev"))')

        # 3. Explicit read restrictions (sentinels / forbidden host paths)
        for denied_path in config.denied_read_paths:
            p_real = os.path.realpath(str(denied_path))
            lines.append(f'(deny file-read* (literal "{p_real}"))')
            lines.append(f'(deny file-read* (subpath "{p_real}"))')

        return "\n".join(lines) + "\n"

    def execute(
        self,
        cmd: list[str],
        config: SandboxConfig,
        input_str: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> SandboxResult:
        """Execute command inside macOS Seatbelt sandbox."""
        if not self.is_available():
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="Seatbelt sandbox backend (/usr/bin/sandbox-exec) is not available",
                backend=self.name,
                error_type="SANDBOX_UNAVAILABLE",
                error_message="Seatbelt sandbox backend (/usr/bin/sandbox-exec) is not available",
            )

        start_time = time.perf_counter()
        os.makedirs(config.workspace_dir, exist_ok=True)
        profile_str = self._build_profile(config)

        # Scrub environment variables: only allow whitelisted, remove any sensitive patterns
        clean_env: dict[str, str] = {}
        for k, v in os.environ.items():
            if k in config.environment_whitelist and not REDACT_KEYS_RE.search(k):
                clean_env[k] = v
        # Ensure TMPDIR points to isolated workspace
        clean_env["TMPDIR"] = str(config.workspace_dir)
        clean_env.update(config.extra_env)

        full_cmd = [self.sandbox_exec_path, "-p", profile_str] + cmd

        is_timeout = False
        is_cancelled = False
        is_truncated = False
        exit_code = -1
        stdout_text = ""
        stderr_text = ""

        try:
            # start_new_session=True creates a new process group for clean subtree termination
            proc = subprocess.Popen(
                full_cmd,
                stdin=subprocess.PIPE if input_str is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(config.workspace_dir),
                env=clean_env,
                start_new_session=True,
                text=True,
            )

            if run_id:
                with self._lock:
                    self._active_processes[run_id] = proc

            try:
                stdout_raw, stderr_raw = proc.communicate(
                    input=input_str, timeout=config.timeout_seconds
                )
                exit_code = proc.returncode
                stdout_text = stdout_raw or ""
                stderr_text = stderr_raw or ""
            except subprocess.TimeoutExpired:
                is_timeout = True
                terminate_process_tree(proc, timeout_grace=0.3)
                exit_code = -1
                stdout_text = ""
                stderr_text = f"Execution timed out after {config.timeout_seconds} seconds"
        except Exception as exc:
            exit_code = -1
            stderr_text = f"{type(exc).__name__}: {exc}"
        finally:
            if run_id:
                with self._lock:
                    self._active_processes.pop(run_id, None)

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Enforce output length limit with explicit truncation marker
        max_bytes = config.max_output_bytes
        if len(stdout_text.encode("utf-8")) > max_bytes:
            # Truncate at character boundary approximately max_bytes
            stdout_text = stdout_text[:max_bytes] + "\n... [TRUNCATED]"
            is_truncated = True

        if len(stderr_text.encode("utf-8")) > max_bytes:
            stderr_text = stderr_text[:max_bytes] + "\n... [TRUNCATED]"
            is_truncated = True

        err_type: Optional[str] = None
        err_msg: Optional[str] = None
        if is_timeout:
            err_type = "TIMEOUT"
            err_msg = stderr_text
        elif exit_code != 0:
            err_type = "SANDBOX_EXEC_ERROR"
            err_msg = stderr_text or f"Process exited with code {exit_code}"

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            is_timeout=is_timeout,
            is_cancelled=is_cancelled,
            is_truncated=is_truncated,
            backend=self.name,
            latency_ms=latency_ms,
            error_type=err_type,
            error_message=err_msg,
        )


@dataclass
class ProbeResult:
    """Result of dependency verification inside the sandbox."""

    dependency: str
    satisfied: bool
    detected_version: Optional[str] = None
    error_reason: Optional[str] = None


class DependencyProbe:
    """Probe dependencies (binaries, versions, python modules) inside the sandbox."""

    def __init__(
        self,
        backend: Optional[SandboxBackend] = None,
        workspace_base: Optional[Path] = None,
    ):
        self.backend = backend or MacSeatbeltSandbox()
        self.workspace_base = workspace_base or Path("/tmp/skillforge_probe_ws")
        self._cache: dict[str, ProbeResult] = {}
        self._last_fingerprint: str = ""
        self._lock = threading.Lock()

    def compute_fingerprint(self, config: Optional[SandboxConfig] = None) -> str:
        """Compute environment fingerprint representing backend, python version, and sandbox boundary."""
        backend_name = self.backend.name if self.backend else "none"
        py_ver = sys.version.split()[0]
        ws_path = str(config.workspace_dir) if config else str(self.workspace_base)
        net_flag = str(config.allow_network) if config else "False"
        raw = f"{backend_name}:{sys.platform}:{py_ver}:{sys.executable}:{ws_path}:{net_flag}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def invalidate_cache(self) -> None:
        """Invalidate all cached dependency verification results."""
        with self._lock:
            self._cache.clear()
            self._last_fingerprint = ""

    def probe_dependency(
        self,
        dependency_spec: str,
        config: Optional[SandboxConfig] = None,
    ) -> ProbeResult:
        """Probe dependency inside sandbox.

        Distinguishes:
        - Executable in PATH (e.g. 'git', 'python3', 'bin:curl')
        - Python module importable inside sandbox (e.g. 'json', 'yaml', 'requests')
        - Version constraints (e.g. 'python3>=3.10', 'yaml>=5.0')
        """
        dep_str = dependency_spec.strip()
        current_fp = self.compute_fingerprint(config)

        with self._lock:
            if self._last_fingerprint != current_fp:
                # Fingerprint changed: force re-probing, invalidate cache!
                self._cache.clear()
                self._last_fingerprint = current_fp
            elif dep_str in self._cache:
                return self._cache[dep_str]

        # Fail closed on unavailable backend
        if not self.backend or not self.backend.is_available():
            res = ProbeResult(
                dependency=dep_str,
                satisfied=False,
                error_reason="Sandbox backend is unavailable or not operational",
            )
            with self._lock:
                self._cache[dep_str] = res
            return res

        # Check explicit negative patterns
        if dep_str.startswith("unavailable_") or dep_str.startswith("missing_"):
            res = ProbeResult(
                dependency=dep_str,
                satisfied=False,
                error_reason=f"Declared dependency '{dep_str}' is known to be missing",
            )
            with self._lock:
                self._cache[dep_str] = res
            return res

        # Parse dependency spec
        # Examples: "python3>=3.10", "bin:git", "json", "yaml>=5.0"
        match_ver = re.match(r"^([a-zA-Z0-9_\-:\.]+)\s*(>=|==|<=|>|<)\s*([0-9\.]+)$", dep_str)
        if match_ver:
            dep_name = match_ver.group(1)
            op = match_ver.group(2)
            req_ver = match_ver.group(3)
        else:
            dep_name = dep_str
            op = None
            req_ver = None

        # Build safe probe python script
        probe_py = """
import sys, importlib.util, shutil

dep = sys.argv[1]
op = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "None" else None
req_ver = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "None" else None

# Clean prefix if any
if dep.startswith("bin:"):
    binary = dep[4:]
    p = shutil.which(binary)
    if not p:
        sys.stdout.write("ERR:Binary not found")
        sys.exit(1)
    sys.stdout.write("OK:binary")
    sys.exit(0)

# Check python module
spec = importlib.util.find_spec(dep)
if spec is not None:
    try:
        mod = importlib.import_module(dep)
        ver = getattr(mod, "__version__", None)
        if op and req_ver:
            if not ver:
                sys.stdout.write("ERR:Version constraint requested but module has no __version__")
                sys.exit(1)
            # Simple version tuple compare
            def to_tuple(v_str):
                return tuple(int(x) for x in v_str.split(".") if x.isdigit())
            v_act = to_tuple(str(ver))
            v_req = to_tuple(req_ver)
            if op == ">=" and not (v_act >= v_req):
                sys.stdout.write(f"ERR:Version {ver} does not satisfy >={req_ver}")
                sys.exit(1)
            elif op == "==" and not (v_act == v_req):
                sys.stdout.write(f"ERR:Version {ver} does not satisfy =={req_ver}")
                sys.exit(1)
        sys.stdout.write(f"OK:{ver or 'loaded'}")
        sys.exit(0)
    except Exception as exc:
        sys.stdout.write(f"ERR:{exc}")
        sys.exit(1)

# Check generic binary if module not found
p = shutil.which(dep)
if p:
    if dep == "python3" and op and req_ver:
        v_act = sys.version_info[:3]
        v_req = tuple(int(x) for x in req_ver.split(".") if x.isdigit())
        if op == ">=" and not (v_act >= v_req):
            sys.stdout.write(f"ERR:Python version {sys.version.split()[0]} does not satisfy >={req_ver}")
            sys.exit(1)
    sys.stdout.write("OK:binary")
    sys.exit(0)

sys.stdout.write("ERR:Not found")
sys.exit(1)
"""
        ws_base = Path(self.workspace_base)
        probe_ws = Path(config.workspace_dir) if config else (ws_base / "probe_tmp")
        eff_config = SandboxConfig(
            workspace_dir=probe_ws,
            allow_network=False,
            timeout_seconds=5.0,
        )

        cmd = [
            sys.executable,
            "-c",
            probe_py,
            dep_name,
            str(op),
            str(req_ver),
        ]

        result = self.backend.execute(cmd, eff_config)
        output = result.stdout.strip()

        if result.exit_code == 0 and output.startswith("OK:"):
            detected = output.split("OK:", 1)[1]
            res = ProbeResult(
                dependency=dep_str,
                satisfied=True,
                detected_version=detected if detected != "loaded" else None,
            )
        else:
            err = output[4:] if output.startswith("ERR:") else (result.stderr or "Probe failed")
            res = ProbeResult(
                dependency=dep_str,
                satisfied=False,
                error_reason=err,
            )

        with self._lock:
            self._cache[dep_str] = res
        return res


@dataclass
class SandboxedToolSpec:
    """Registration contract for an application tool that must run inside the sandbox."""

    name: str
    command_template: list[str]
    description: str = ""
    parameters: list[ToolParameter] = field(default_factory=list)
    required_dependencies: list[str] = field(default_factory=list)
    allowed_read_paths: list[Path] = field(default_factory=list)
    denied_read_paths: list[Path] = field(default_factory=list)
    max_output_bytes: int = 65536
    timeout_seconds: float = 10.0
    host_handler_counter: Optional[Callable[[], None]] = None
