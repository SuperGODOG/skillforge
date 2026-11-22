"""Targeted offline unit test for Pi native SDK custom model routing, options, and security boundaries.

Verifies:
1. Native Pi official SDK custom API routing to local HTTP stub with SSE streaming.
2. Parameter propagation: max_tokens, temperature.
3. Secret key confinement: passed in closure / options, never leaked into captured provider inputs or SQLite.
4. Robust rejection: missing model, shell command '!' in apiKey, unknown API protocol, invalid params (NaN/negative).
5. Smoke scenario admission gate: max 16 requests, deadlines, token limits.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from skillforge.collector import ExperienceCollector
from skillforge.episode import EpisodeStore
from skillforge.models import CandidateSkill, SkillMeta
from skillforge.registry import SkillRegistry
from skillforge.runtime import AgentRuntime, ToolBroker
from skillforge.scenarios.logistics import QueryOrderPackagesTool, QueryPackageTrackingTool
from skillforge.scenarios.pi_live_smoke import SharedSmokeAdmissionGate, redact_secrets


class MockSSECompletionsHandler(BaseHTTPRequestHandler):
    """Local HTTP mock server handler for OpenAI-compatible chat completions."""

    requests_log: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        parsed_body: dict[str, Any] = {}
        try:
            parsed_body = json.loads(raw_body)
        except Exception:
            parsed_body = {"raw": raw_body}

        req_record = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": parsed_body,
        }
        self.__class__.requests_log.append(req_record)

        if "/chat/completions" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            # SSE chunk 1: content delta
            chunk1 = {
                "id": "chatcmpl-mock-001",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "custom-test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "包裹PKG_101已签收DELIVERED。"},
                        "finish_reason": None,
                    }
                ],
            }
            # SSE chunk 2: stop & usage
            chunk2 = {
                "id": "chatcmpl-mock-001",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "custom-test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 15,
                    "total_tokens": 57,
                },
            }

            self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode("utf-8"))
            self.wfile.flush()
            self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode("utf-8"))
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging to keep test output clean
        pass


@pytest.fixture
def mock_sse_server():
    """Start in-process HTTP mock server on random free port."""
    MockSSECompletionsHandler.requests_log = []
    # Find free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), MockSSECompletionsHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}/v1"
    yield base_url, MockSSECompletionsHandler.requests_log

    server.shutdown()
    server.server_close()


def create_agent_config(
    agent_dir: Path,
    provider: str,
    model_id: str,
    base_url: str,
    api_key: str,
    api: str = "openai-completions",
    headers: dict[str, str] | None = None,
    models: list[dict[str, Any]] | None = None,
) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "providers": {
            provider: {
                "name": "Mock Provider",
                "baseUrl": base_url,
                "api": api,
                "apiKey": api_key,
                "headers": headers or {},
                "models": models or [
                    {
                        "id": model_id,
                        "name": "Custom Test Model",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 32000,
                        "maxTokens": 4096,
                    }
                ],
            }
        }
    }
    (agent_dir / "models.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def test_custom_provider_route_and_stream_options(tmp_path: Path, mock_sse_server, monkeypatch):
    """Test custom model from models.json routes to HTTP stub, verifies headers and options."""
    base_url, req_log = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    secret_key = "sk-test-super-secret-token-abcdef12345"

    create_agent_config(
        agent_dir=agent_dir,
        provider="mock-custom-provider",
        model_id="mock-custom-model",
        base_url=base_url,
        api_key=secret_key,
        headers={"X-Custom-Client": "SkillForgeTest"},
    )
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    db_path = tmp_path / "runtime.db"
    ep_db = tmp_path / "episodes.db"
    sk_db = tmp_path / "skills.db"
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir(parents=True, exist_ok=True)

    ep_store = EpisodeStore(db_path=ep_db)
    registry = SkillRegistry(db_path=sk_db, skills_dir=sk_dir)
    broker = ToolBroker(application_allowlist={"query_order_packages", "query_package_tracking"})
    broker.register_tool(QueryOrderPackagesTool())
    broker.register_tool(QueryPackageTrackingTool())

    collector = ExperienceCollector(episode_store=ep_store, registry=registry)
    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=broker,
        registry=registry,
        collector=collector,
        episode_store=ep_store,
    )

    try:
        run_id = "run_stub_test_01"
        task_id = "task_stub_test_01"
        draft = CandidateSkill(
            candidate_id="cand_stub_01",
            skill_name="logistics_stub_test",
            decision="create",
            meta=SkillMeta(
                name="logistics_stub_test",
                version="0.1.0",
                description="Test candidate",
                use_when="Test",
                dependencies=[],
            ),
            body="核验物流状态",
            source_requirement="测试自定义本地模型选路",
            status="DRAFT",
        )
        runtime.start_run(
            run_id=run_id,
            task_id=task_id,
            candidate=draft,
            purpose="evaluation",
            budget_max=10,
            deadline_ts=time.time() + 60.0,
        )

        model_cfg = {
            "provider": "mock-custom-provider",
            "model_id": "mock-custom-model",
            "max_tokens": 1024,
            "temperature": 0.25,
            "timeout_ms": 15000,
            "max_retries": 0,
            "reasoning": "off",
        }

        resp_text = runtime.run_agent_with_pi(
            run_id=run_id,
            input_text="请报告订单状态",
            candidate=draft,
            tools=[],
            model_config=model_cfg,
            test_mode=False,
            timeout=20.0,
        )

        assert "DELIVERED" in resp_text
        assert len(req_log) >= 1
        last_req = req_log[-1]
        assert "/chat/completions" in last_req["path"]

        # Check authorization header delivered to HTTP stub
        auth_hdr = last_req["headers"].get("authorization") or last_req["headers"].get("Authorization")
        assert auth_hdr == f"Bearer {secret_key}"

        # Check custom header delivered
        assert last_req["headers"].get("x-custom-client") == "SkillForgeTest"

        # Check parameter propagation
        body = last_req["body"]
        assert body.get("max_tokens") == 1024 or body.get("max_completion_tokens") == 1024
        assert body.get("temperature") == 0.25

        # Check secret containment: NEVER in captured inputs or SQLite DB
        run_rec = runtime.get_run(run_id)
        assert run_rec is not None
        assert secret_key not in str(run_rec)

        # Inspect SQLite directly for secret leakage
        with open(db_path, "rb") as f:
            db_bytes = f.read()
        assert secret_key.encode("utf-8") not in db_bytes

    finally:
        runtime.close()
        registry.close()
        ep_store.close()


def test_missing_model_in_models_json_fails(tmp_path: Path, monkeypatch):
    """Test requesting a model not present in models.json fails explicitly with no silent fallback."""
    agent_dir = tmp_path / "pi_agent_env"
    create_agent_config(
        agent_dir=agent_dir,
        provider="mock-provider",
        model_id="known-model",
        base_url="http://127.0.0.1:9999/v1",
        api_key="sk-test-key",
    )
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    db_path = tmp_path / "runtime.db"
    ep_db = tmp_path / "episodes.db"
    sk_db = tmp_path / "skills.db"
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir(parents=True, exist_ok=True)

    runtime = AgentRuntime(
        db_path=db_path,
        tool_broker=ToolBroker(),
        registry=SkillRegistry(db_path=sk_db, skills_dir=sk_dir),
        collector=ExperienceCollector(episode_store=EpisodeStore(db_path=ep_db), registry=None),
        episode_store=EpisodeStore(db_path=ep_db),
    )

    try:
        run_id = "run_missing_model"
        runtime.start_run(run_id=run_id, task_id="t1", purpose="evaluation", budget_max=5)

        with pytest.raises(Exception) as exc_info:
            runtime.run_agent_with_pi(
                run_id=run_id,
                input_text="hello",
                model_config={"provider": "mock-provider", "model_id": "nonexistent-model"},
                test_mode=False,
                timeout=10.0,
            )
        assert "not found" in str(exc_info.value).lower()
    finally:
        runtime.close()


def test_shell_command_in_api_key_rejected(tmp_path: Path, monkeypatch):
    """Test shell command reference ('!') in apiKey is explicitly rejected."""
    agent_dir = tmp_path / "pi_agent_env"
    create_agent_config(
        agent_dir=agent_dir,
        provider="mock-provider",
        model_id="m1",
        base_url="http://127.0.0.1:9999/v1",
        api_key="!cat /etc/passwd",
    )
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    runtime = AgentRuntime(
        db_path=tmp_path / "runtime.db",
        tool_broker=ToolBroker(),
        registry=SkillRegistry(db_path=tmp_path / "sk.db", skills_dir=tmp_path / "sks"),
        collector=ExperienceCollector(episode_store=EpisodeStore(db_path=tmp_path / "ep.db"), registry=None),
        episode_store=EpisodeStore(db_path=tmp_path / "ep.db"),
    )

    try:
        run_id = "run_shell_key"
        runtime.start_run(run_id=run_id, task_id="t1", purpose="evaluation", budget_max=5)

        with pytest.raises(Exception) as exc_info:
            runtime.run_agent_with_pi(
                run_id=run_id,
                input_text="hello",
                model_config={"provider": "mock-provider", "model_id": "m1"},
                test_mode=False,
                timeout=10.0,
            )
        assert "!" in str(exc_info.value) or "shell" in str(exc_info.value).lower()
    finally:
        runtime.close()


def test_unknown_api_protocol_rejected(tmp_path: Path, monkeypatch):
    """Test unknown API protocol string is explicitly rejected and does not masquerade."""
    agent_dir = tmp_path / "pi_agent_env"
    create_agent_config(
        agent_dir=agent_dir,
        provider="mock-provider",
        model_id="m1",
        base_url="http://127.0.0.1:9999/v1",
        api_key="sk-test",
        api="nonexistent-magic-api-protocol",
    )
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    runtime = AgentRuntime(
        db_path=tmp_path / "runtime.db",
        tool_broker=ToolBroker(),
        registry=SkillRegistry(db_path=tmp_path / "sk.db", skills_dir=tmp_path / "sks"),
        collector=ExperienceCollector(episode_store=EpisodeStore(db_path=tmp_path / "ep.db"), registry=None),
        episode_store=EpisodeStore(db_path=tmp_path / "ep.db"),
    )

    try:
        run_id = "run_unknown_api"
        runtime.start_run(run_id=run_id, task_id="t1", purpose="evaluation", budget_max=5)

        with pytest.raises(Exception) as exc_info:
            runtime.run_agent_with_pi(
                run_id=run_id,
                input_text="hello",
                model_config={"provider": "mock-provider", "model_id": "m1"},
                test_mode=False,
                timeout=10.0,
            )
        assert "protocol" in str(exc_info.value).lower() or "unsupported" in str(exc_info.value).lower()
    finally:
        runtime.close()


def test_invalid_parameters_rejected_before_network(tmp_path: Path, monkeypatch):
    """Test NaN / negative parameters are rejected before network stream."""
    agent_dir = tmp_path / "pi_agent_env"
    create_agent_config(
        agent_dir=agent_dir,
        provider="mock-provider",
        model_id="m1",
        base_url="http://127.0.0.1:9999/v1",
        api_key="sk-test",
    )
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    runtime = AgentRuntime(
        db_path=tmp_path / "runtime.db",
        tool_broker=ToolBroker(),
        registry=SkillRegistry(db_path=tmp_path / "sk.db", skills_dir=tmp_path / "sks"),
        collector=ExperienceCollector(episode_store=EpisodeStore(db_path=tmp_path / "ep.db"), registry=None),
        episode_store=EpisodeStore(db_path=tmp_path / "ep.db"),
    )

    try:
        run_id = "run_neg_tokens"
        runtime.start_run(run_id=run_id, task_id="t1", purpose="evaluation", budget_max=5)

        with pytest.raises(Exception) as exc_info:
            runtime.run_agent_with_pi(
                run_id=run_id,
                input_text="hello",
                model_config={"provider": "mock-provider", "model_id": "m1", "max_tokens": -50},
                test_mode=False,
                timeout=10.0,
            )
        assert "max_tokens" in str(exc_info.value).lower()
    finally:
        runtime.close()


def test_smoke_scenario_shared_admission_gate():
    """Verify SharedSmokeAdmissionGate enforcement of 16-request cap, token caps, and deadlines."""
    gate = SharedSmokeAdmissionGate(
        max_requests=3,
        deadline_ts=time.time() + 10.0,
        max_total_input=100,
        max_total_output=50,
    )

    # First 3 requests admitted
    for run_id, turn in (("r1", 1), ("r1", 2), ("r2", 1)):
        assert gate.admit(run_id, turn)["allowed"] is True
        gate.on_receipt({"run_id": run_id, "turn_index": turn, "usage": {"input": 0, "output": 0}})

    # 4th request rejected by request cap
    res = gate.admit("r2", 2)
    assert res["allowed"] is False
    assert res["error_type"] == "MAX_REQUESTS_EXCEEDED"

    # Token usage aggregation
    gate2 = SharedSmokeAdmissionGate(
        max_requests=10,
        deadline_ts=time.time() + 10.0,
        max_total_input=100,
        max_total_output=50,
    )
    assert gate2.admit("r1", 1)["allowed"] is True
    gate2.on_receipt({"usage": {"input": 80, "output": 20}})
    assert gate2.total_input_tokens == 80
    assert gate2.total_output_tokens == 20

    # Next request pushes input over limit
    gate2.on_receipt({"usage": {"input": 30, "output": 10}})
    assert gate2.total_input_tokens == 110
    res2 = gate2.admit("r1", 2)
    assert res2["allowed"] is False
    assert res2["error_type"] == "INPUT_TOKEN_LIMIT_EXCEEDED"

    # Deadline expiration check
    gate3 = SharedSmokeAdmissionGate(
        max_requests=10,
        deadline_ts=time.time() - 1.0,
    )
    res3 = gate3.admit("r1", 1)
    assert res3["allowed"] is False
    assert res3["error_type"] == "DEADLINE_EXCEEDED"


def test_secret_redaction_utility():
    """Test secret redaction regex helper."""
    text_with_sk = "Error calling API with key sk-1234567890abcdef1234567890: status 401"
    clean = redact_secrets(text_with_sk)
    assert "sk-" not in clean
    assert "[REDACTED_SECRET]" in clean

    bearer = "Authorization: Bearer secret-auth-token-1234567890abcdef"
    clean_bearer = redact_secrets(bearer)
    assert "secret-auth-token" not in clean_bearer
    assert "Bearer [REDACTED_SECRET]" in clean_bearer


def test_all_turn_usage_includes_cache_and_preserves_unknowns():
    from skillforge.scenarios.pi_live_smoke import aggregate_usage

    receipts = [
        {"usage": {"input": 10, "output": 5, "cacheRead": 80, "cacheWrite": 0, "totalTokens": 95}},
        {"usage": {"input": 20, "output": 6, "cacheRead": 90, "cacheWrite": 0, "totalTokens": 116}},
    ]
    total = aggregate_usage(receipts)
    assert total == {"input": 30, "output": 11, "cacheRead": 170, "cacheWrite": 0,
                     "totalTokens": 211, "input_including_cache": 200, "cost": None}
    assert aggregate_usage(receipts + [{"usage": None}])["totalTokens"] is None

    gate = SharedSmokeAdmissionGate(max_requests=5, deadline_ts=time.time() + 10)
    assert gate.admit("r", 1)["allowed"]
    assert gate.admit("r", 2)["error_type"] == "USAGE_UNKNOWN"
    frame = {"run_id": "r", "turn_index": 1, "receipt_id": "r1", **receipts[0]}
    gate.on_receipt(frame)
    gate.on_receipt(frame)
    assert gate.total_input_tokens == 90
    assert gate.total_output_tokens == 5
    assert gate.admit("r", 2)["allowed"]
    gate.on_receipt({"run_id": "r", "turn_index": 2, "usage": None})
    assert gate.admit("r", 3)["error_type"] == "USAGE_UNKNOWN"


def test_logistics_failure_is_structured_service_error_not_handler_type_error():
    result = QueryPackageTrackingTool(simulated_failing_packages={"PKG_202"}).run({"package_id": "PKG_202"})
    assert result.error_info["code"] == "LOGISTICS_SERVICE_UNAVAILABLE"
    assert "temporarily unavailable" in result.text
    assert result.context["package_id"] == "PKG_202"
    missing = QueryPackageTrackingTool().run({"package_id": "missing"})
    assert missing.error_info["code"] == "PACKAGE_NOT_FOUND"
    missing_order = QueryOrderPackagesTool().run({"order_id": "missing"})
    assert missing_order.error_info["code"] == "ORDER_NOT_FOUND"


def test_mutable_local_model_checkpoint_resume_fails_closed(tmp_path, monkeypatch):
    import asyncio
    from skillforge.pi_adapter import PiRunnerClient

    create_agent_config(tmp_path / "agent", "local", "model", "http://127.0.0.1:1/v1", "test-token")
    monkeypatch.setenv("PI_AGENT_DIR", str(tmp_path / "agent"))

    async def check():
        client = PiRunnerClient()
        await client.start()
        try:
            with pytest.raises(Exception, match="frozen configuration binding"):
                await client.init_run(
                    run_id="local_resume", system_prompt="test", tools=[],
                    model_config={"provider": "local", "model_id": "model"}, is_test=False,
                    initial_messages=[{"role": "user", "content": "history"}],
                )
        finally:
            await client.close()
    asyncio.run(check())
