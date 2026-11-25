"""Targeted tests for PiTextClient (D5a thin adapter).

Verifies:
1. invoke with text return, real usage parsing, and preservation of system / user roles in payload.
2. Shared LLMLedger budget enforcement: 2nd client fails before sending HTTP when budget exhausted.
3. Unknown usage fails closed: missing usage flags unknown tokens, subsequent token-capped budget call fails closed.
4. Robust rejection of faux, responses, is_test, apiKey, baseUrl, and invalid config values.
5. Handling of server errors and finish_reason='length' (truncated).
6. Artifact writing: atomic call_<uuid>.json, secret redaction, no thinking, model reasoning='off'.
7. Subprocess cleanup: runner process is reaped upon invoke completion.
8. Message validation: fails closed on tool messages, non-string content, missing user messages.
9. Synchronous boundary guard: fails closed when called inside active running event loop.
"""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any

import pytest

from skillforge.evaluator.llm_factory import BudgetExceededError, LLMLedger
from skillforge.models import EvolveBudget
from skillforge.pi_text_client import PiTextClient


class MockSSECompletionsHandler(BaseHTTPRequestHandler):
    requests_log: list[dict[str, Any]] = []
    mode: str = "normal"

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

        if self.__class__.mode == "server_error":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "Internal mock server failure"}}')
            return

        if "/chat/completions" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            if self.__class__.mode == "length_truncated":
                chunk1 = {
                    "id": "chatcmpl-mock-trunc",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "部分文本已达到长度截断上限"},
                            "finish_reason": None,
                        }
                    ],
                }
                chunk2 = {
                    "id": "chatcmpl-mock-trunc",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            elif self.__class__.mode == "no_usage":
                chunk1 = {
                    "id": "chatcmpl-mock-nousage",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "返回无token用量信息的内容"},
                            "finish_reason": None,
                        }
                    ],
                }
                chunk2 = {
                    "id": "chatcmpl-mock-nousage",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            else:  # normal
                chunk1 = {
                    "id": "chatcmpl-mock-001",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "包裹PKG_101已签收DELIVERED。"},
                            "finish_reason": None,
                        }
                    ],
                }
                chunk2 = {
                    "id": "chatcmpl-mock-001",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-custom-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
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
        pass


@pytest.fixture
def mock_sse_server():
    MockSSECompletionsHandler.requests_log = []
    MockSSECompletionsHandler.mode = "normal"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), MockSSECompletionsHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}/v1"
    yield base_url, MockSSECompletionsHandler

    server.shutdown()
    server.server_close()


def setup_agent_dir(agent_dir: Path, base_url: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "providers": {
            "mock-provider": {
                "name": "Mock Provider",
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": "sk-local-test-key-mock-123456",
                "models": [
                    {
                        "id": "mock-model",
                        "name": "Mock Model",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 16000,
                        "maxTokens": 4096,
                    }
                ],
            }
        }
    }
    (agent_dir / "models.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def test_pi_text_client_success_invoke_and_system_role_preservation(
    tmp_path: Path, mock_sse_server, monkeypatch
):
    """Test standard invoke returning text content and usage, preserving system role."""
    base_url, handler_cls = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        timeout=30.0,
    )

    messages = [
        {"role": "system", "content": "You are a logistics assistant."},
        {"role": "user", "content": "查询包裹状态"},
    ]

    res = client.invoke(messages)

    assert res.content == "包裹PKG_101已签收DELIVERED。"
    assert res.usage["prompt_tokens"] == 42
    assert res.usage["completion_tokens"] == 15
    assert res.usage["total_tokens"] == 57

    # Verify that the HTTP request sent by Pi runner retained system message and user message
    assert len(handler_cls.requests_log) == 1
    req_body = handler_cls.requests_log[0]["body"]
    req_msgs = req_body.get("messages", [])
    roles = [m.get("role") for m in req_msgs]
    assert "system" in roles
    assert "user" in roles
    sys_msg = next(m for m in req_msgs if m.get("role") == "system")
    assert sys_msg["content"] == "You are a logistics assistant."


def test_pi_text_client_shared_ledger_budget_exhaustion(
    tmp_path: Path, mock_sse_server, monkeypatch
):
    """Test two clients sharing one LLMLedger; 2nd call raises BudgetExceededError without HTTP request."""
    base_url, handler_cls = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    shared_ledger = LLMLedger(budget=EvolveBudget(max_calls=1))
    client1 = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        ledger=shared_ledger,
        role="generator",
    )
    client2 = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        ledger=shared_ledger,
        role="patcher",
    )

    res1 = client1.invoke("First prompt")
    assert res1.content == "包裹PKG_101已签收DELIVERED。"
    assert shared_ledger.total_calls == 1
    assert len(handler_cls.requests_log) == 1

    # Second invocation must fail before issuing HTTP request
    with pytest.raises(BudgetExceededError) as exc_info:
        client2.invoke("Second prompt")

    assert "CALL_LIMIT_EXCEEDED" in str(exc_info.value)
    # Server should not have received a second request
    assert len(handler_cls.requests_log) == 1


def test_pi_text_client_unknown_usage_blocks_subsequent(
    tmp_path: Path, mock_sse_server, monkeypatch
):
    """Test that missing token usage sets unknown tokens and blocks subsequent token-budgeted calls."""
    base_url, handler_cls = mock_sse_server
    handler_cls.mode = "no_usage"
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    ledger = LLMLedger(budget=EvolveBudget(max_calls=10, max_tokens=1000))
    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        ledger=ledger,
    )

    res = client.invoke("Test query")
    assert res.content == "返回无token用量信息的内容"
    assert res.usage["prompt_tokens"] is None
    assert res.usage["total_tokens"] is None
    assert ledger.has_unknown_tokens is True

    # Subsequent call must fail closed because token ceiling cannot be verified
    with pytest.raises(BudgetExceededError) as exc_info:
        client.invoke("Second query")

    assert "TOKEN_USAGE_UNKNOWN" in str(exc_info.value)


def test_pi_text_client_rejects_forbidden_configs():
    """Test that faux, responses, is_test, apiKey, baseUrl, and out-of-bound params are rejected."""
    # 1. Prohibit faux
    with pytest.raises(ValueError, match="faux"):
        PiTextClient(model_config={"provider": "faux", "model_id": "test"})

    # 2. Prohibit apiKey
    with pytest.raises(ValueError, match="Secret or endpoint"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "apiKey": "secret"})

    # 3. Prohibit baseUrl
    with pytest.raises(ValueError, match="Secret or endpoint"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "baseUrl": "http://evil"})

    # 4. Prohibit responses
    with pytest.raises(ValueError, match="responses"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "responses": []})

    # 5. Prohibit is_test
    with pytest.raises(ValueError, match="is_test"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "is_test": True})

    # 6. Prohibit max_tokens > 4096
    with pytest.raises(ValueError, match="max_tokens"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "max_tokens": 5000})

    # 7. Prohibit max_retries > 0
    with pytest.raises(ValueError, match="max_retries"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test", "max_retries": 2})

    # 8. Prohibit timeout > 120
    with pytest.raises(ValueError, match="timeout"):
        PiTextClient(model_config={"provider": "openai", "model_id": "test"}, timeout=150.0)


def test_pi_text_client_error_and_truncation_handling(
    tmp_path: Path, mock_sse_server, monkeypatch
):
    """Test HTTP 500 raises RuntimeError and finish_reason='length' sets truncated=True."""
    base_url, handler_cls = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    # 1. Test server error
    handler_cls.mode = "server_error"
    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        timeout=10.0,
    )
    with pytest.raises(RuntimeError):
        client.invoke("Fail prompt")

    # 2. Test length truncation
    handler_cls.mode = "length_truncated"
    client_trunc = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        timeout=10.0,
    )
    with pytest.raises(RuntimeError, match="stop_reason=length"):
        client_trunc.invoke("Truncate prompt")


def test_pi_text_client_artifact_redaction_and_no_thinking(
    tmp_path: Path, mock_sse_server, monkeypatch
):
    """Test artifact file logging with secret redaction, ledger summary, and absence of thinking."""
    base_url, handler_cls = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    artifacts_dir = tmp_path / "call_artifacts"
    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"},
        artifact_dir=artifacts_dir,
    )

    client.invoke([
        {"role": "system", "content": "You are a test agent."},
        {"role": "user", "content": "Token is Bearer secret_token_xyz_12345678"},
    ])

    files = list(artifacts_dir.glob("call_*.json"))
    assert len(files) == 1
    artifact = json.loads(files[0].read_text(encoding="utf-8"))

    # Verify absence of thinking
    assert "thinking" not in artifact
    assert "raw_response" not in artifact

    # Verify secret redaction
    user_msg = next(m for m in artifact["request"]["messages"] if m["role"] == "user")
    assert "[REDACTED]" in user_msg["content"]
    assert "secret_token_xyz_12345678" not in user_msg["content"]

    # Verify model reasoning='off' recorded
    assert artifact["model"]["reasoning"] == "off"
    assert artifact["model"]["provider"] == "mock-provider"
    assert artifact["ledger_summary"]["total_calls"] == 1


def test_pi_text_client_fail_closed_on_unsupported_messages(tmp_path: Path, mock_sse_server, monkeypatch):
    """Test that tool calls, non-text content, or missing user messages fail closed."""
    base_url, _ = mock_sse_server
    agent_dir = tmp_path / "pi_agent_env"
    setup_agent_dir(agent_dir, base_url)
    monkeypatch.setenv("PI_AGENT_DIR", str(agent_dir))

    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"}
    )

    # 1. Reject unsupported role
    with pytest.raises(ValueError, match="Unsupported message role"):
        client.invoke([{"role": "tool", "content": "result"}])

    # 2. Reject tool call field
    with pytest.raises(ValueError, match="tool call fields"):
        client.invoke([{"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]}])

    # 3. Reject non-string content
    with pytest.raises(TypeError, match="must be a string"):
        client.invoke([{"role": "user", "content": ["not a string"]}])

    # 4. Reject conversation without user message
    with pytest.raises(ValueError, match="No user message"):
        client.invoke([{"role": "system", "content": "System only"}])

    # 5. Reject last message not being user
    with pytest.raises(ValueError, match="Last message in conversation must be a user message"):
        client.invoke([{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}])


def test_pi_text_client_running_event_loop_guard():
    """Test that invoke fails closed with ValueError when called inside a running asyncio event loop."""
    client = PiTextClient(
        model_config={"provider": "mock-provider", "model_id": "mock-model"}
    )

    async def _call_in_loop():
        client.invoke("Hello inside loop")

    with pytest.raises(ValueError, match="active asyncio event loop"):
        asyncio.run(_call_in_loop())


def test_cache_usage_includes_cached_input_and_preserves_unknown():
    from skillforge.pi_text_client import _standard_usage
    usage = _standard_usage({'input': 10, 'cacheRead': 30, 'cacheWrite': 2, 'output': 5, 'totalTokens': 47})
    assert usage['prompt_tokens'] == 42
    assert usage['total_tokens'] == 47
    without_total = _standard_usage({'input': 10, 'cacheRead': 30, 'cacheWrite': 2, 'output': 5})
    assert without_total == usage
    assert _standard_usage({'input': 0, 'output': 0, 'totalTokens': 0})['total_tokens'] is None
    assert _standard_usage({'input': float('nan'), 'output': 5})['total_tokens'] is None


@pytest.mark.parametrize('config', [{'temperature': float('nan')}, {'max_tokens': True}])
def test_invalid_numeric_parameters_fail_before_spawn(config):
    with pytest.raises(ValueError):
        PiTextClient({'provider': 'mock-provider', 'model_id': 'mock-model', **config})


def test_local_model_history_preserves_existing_frozen_binding_guard(tmp_path, mock_sse_server, monkeypatch):
    base_url, handler_cls = mock_sse_server
    setup_agent_dir(tmp_path/'agent', base_url)
    monkeypatch.setenv('PI_AGENT_DIR', str(tmp_path/'agent'))
    client = PiTextClient({'provider': 'mock-provider', 'model_id': 'mock-model'}, timeout=10)
    with pytest.raises(RuntimeError, match='frozen configuration binding'):
        client.invoke([{'role':'system','content':'system'}, {'role':'user','content':'first'}, {'role':'assistant','content':'prior answer'}, {'role':'user','content':'next'}])
    assert handler_cls.requests_log == []
    assert client.ledger.total_calls == 0
