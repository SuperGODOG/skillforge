"""D5a: Thin text-only adapter connecting Pi official SDK to synchronous llm.invoke interface.

Key Invariants:
1. Exposes synchronous invoke(messages: str | list[dict], **kwargs) -> SimpleNamespace(content, usage).
2. Uses dedicated PiRunnerClient Node.js official SDK runner under the hood (tools=[], is_test=False).
3. Text-only messages: strictly 'system', 'user', 'assistant' roles; rejects tool calls, non-text content (fail closed).
4. System prompt separated to init_run(system_prompt=...); previous dialogue passed as initial_messages; final user message sent via prompt().
5. Public model_config validated: rejects apiKey/baseUrl/headers/secrets, rejects faux/responses/is_test fixtures,
   enforces max_tokens <= 4096, max_retries == 0, timeout_ms <= 120000, reasoning='off'.
6. Independent run_id per invocation; asyncio.run with clean finally client.close() cleanup. Rejects running event loops.
7. Shared LLMLedger integration: reserve_turn before provider invocation, settle_turn upon turn_receipt. Never calls record_call or reset.
8. Single-turn failure / timeout / truncation: cancel_pending settles turn to unknown; subsequent token budgets fail closed.
9. Optional artifact_dir: atomic call_<uuid>.json containing sanitized request/response/usage/receipt/ledger summaries.
   Never stores hidden thinking, raw_response, or unredacted secrets.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Any, Optional
import uuid

from .evaluator.llm_factory import BudgetExceededError, LLMLedger
from .models import EvolveBudget
from .pi_adapter import PiExecutionResult, PiRunnerClient


FORBIDDEN_CONFIG_KEYS = {
    "apikey",
    "api_key",
    "baseurl",
    "base_url",
    "headers",
    "token",
    "secret",
    "password",
    "envheader",
    "env_header",
    "credential",
    "auth",
}

_SECRET_TEXT_RE = re.compile(
    r"\bBearer\s+[^\s\"']+|\b(?:sk-|ark-)[a-zA-Z0-9_-]{12,}",
    re.I,
)


def _redact_text(text: str) -> str:
    """Redact secret tokens from text strings."""
    if not isinstance(text, str):
        return ""
    return _SECRET_TEXT_RE.sub("[REDACTED]", text)


def _is_placeholder_or_missing_usage(raw_u: Any) -> bool:
    """Return True if raw_u is absent, malformed, marked missing, or SDK zero-placeholder."""
    if raw_u is None or not isinstance(raw_u, dict):
        return True
    if raw_u.get("_missingUsage") is True:
        return True

    # Check if every token count is 0 or None.
    # When provider emits no token telemetry, Pi SDK initializes all tokens and costs to 0.
    # Genuine completions cannot consume 0 tokens.
    input_val = raw_u.get("input") if raw_u.get("input") is not None else raw_u.get("prompt_tokens")
    output_val = raw_u.get("output") if raw_u.get("output") is not None else raw_u.get("completion_tokens")
    total_val = raw_u.get("totalTokens") if raw_u.get("totalTokens") is not None else raw_u.get("total_tokens")
    cache_read = raw_u.get("cacheRead") or 0
    cache_write = raw_u.get("cacheWrite") or 0

    tokens = [input_val, output_val, total_val]
    if all(v is None or v == 0 for v in tokens) and cache_read == 0 and cache_write == 0:
        return True

    return False


def _standard_usage(raw: Any) -> dict[str, Optional[int]]:
    result = dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens", "input", "output"))
    if _is_placeholder_or_missing_usage(raw):
        return result
    valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    output = raw.get("output", raw.get("completion_tokens"))
    total = raw.get("totalTokens", raw.get("total_tokens"))
    prompt = raw.get("input", raw.get("prompt_tokens"))
    if valid(total) and valid(output) and total >= output:
        prompt = total - output
    elif valid(prompt) and valid(output):
        cached, written = raw.get("cacheRead", 0), raw.get("cacheWrite", 0)
        if not valid(cached) or not valid(written):
            return result
        prompt += cached + written
        total = prompt + output
    else:
        return result
    return {"prompt_tokens": int(prompt), "completion_tokens": int(output),
            "total_tokens": int(total), "input": int(prompt), "output": int(output)}


def _write_call_artifact(
    artifact_dir: Path,
    run_id: str,
    role: str,
    model_config: dict[str, Any],
    system_prompt: str,
    messages: list[dict[str, Any]],
    content: str,
    usage: Optional[dict[str, Any]],
    latency_ms: float,
    stop_reason: Optional[str],
    error: Optional[str],
    receipt: Optional[dict[str, Any]],
    ledger: LLMLedger,
) -> None:
    """Atomically record call receipt and audit metadata, excluding thinking and secrets."""
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    sanitized_messages: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            sanitized_messages.append({
                "role": str(m.get("role", "")),
                "content": _redact_text(str(m.get("content", ""))),
            })

    receipt_proj: Optional[dict[str, Any]] = None
    if receipt and isinstance(receipt, dict):
        raw_rcpt_u = receipt.get("usage")
        clean_rcpt_u: Optional[dict[str, Any]] = None
        if not _is_placeholder_or_missing_usage(raw_rcpt_u) and isinstance(raw_rcpt_u, dict):
            clean_rcpt_u = _standard_usage(raw_rcpt_u)
            clean_rcpt_u["cost"] = None
        receipt_proj = {
            "receipt_id": receipt.get("receipt_id"),
            "turn_index": receipt.get("turn_index"),
            "status": receipt.get("status"),
            "stop_reason": receipt.get("stop_reason"),
            "usage": clean_rcpt_u,
        }

    ledger_summary = ledger.as_dict()

    art_usage: Optional[dict[str, Any]] = None
    if not _is_placeholder_or_missing_usage(usage) and isinstance(usage, dict):
        art_usage = dict(usage)
        if "cost" in art_usage:
            art_usage["cost"] = None

    artifact_data = {
        "run_id": run_id,
        "role": role,
        "model": {
            "provider": model_config.get("provider"),
            "model_id": model_config.get("model_id"),
            "max_tokens": model_config.get("max_tokens"),
            "temperature": model_config.get("temperature"),
            "reasoning": model_config.get("reasoning", "off"),
        },
        "request": {
            "system_prompt": _redact_text(system_prompt),
            "messages": sanitized_messages,
        },
        "content": _redact_text(content) if content else "",
        "usage": art_usage,
        "latency_ms": round(latency_ms, 2),
        "stop_reason": stop_reason,
        "error": _redact_text(str(error)) if error else None,
        "receipt": receipt_proj,
        "ledger_summary": ledger_summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    file_name = f"call_{uuid.uuid4().hex[:12]}.json"
    target_path = artifact_dir / file_name
    tmp_path = artifact_dir / f"{file_name}.tmp"
    tmp_path.write_text(json.dumps(artifact_data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, target_path)


class PiTextClient:
    """Synchronous thin adapter providing llm.invoke compatibility on top of official Pi SDK."""

    def __init__(
        self,
        model_config: dict[str, Any],
        ledger: Optional[LLMLedger] = None,
        role: str = "llm",
        artifact_dir: Optional[Path | str] = None,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(model_config, dict):
            raise TypeError("model_config must be a dictionary")

        # 1. Reject secrets and endpoints
        for k in model_config:
            if str(k).lower() in FORBIDDEN_CONFIG_KEYS:
                raise ValueError(
                    f"Secret or endpoint configuration '{k}' is prohibited in model_config. "
                    "Node reads local configuration via PI_AGENT_DIR."
                )

        # 2. Reject faux/test mocks
        if "faux" in str(model_config.get("provider", "")).lower():
            raise ValueError("Provider 'faux' is prohibited in PiTextClient; real provider routing required")
        if "responses" in model_config:
            raise ValueError("'responses' script is prohibited in PiTextClient")
        if "is_test" in model_config or "test_mode" in model_config:
            raise ValueError("'is_test' or 'test_mode' is prohibited in PiTextClient")

        # 3. Required provider and model
        provider = model_config.get("provider")
        if not provider or not isinstance(provider, str):
            raise ValueError("model_config must specify a non-empty string 'provider'")

        model_id = model_config.get("model_id") or model_config.get("model")
        if not model_id or not isinstance(model_id, str):
            raise ValueError("model_config must specify a non-empty string 'model_id'")

        # 4. Parameter boundaries
        max_tokens = model_config.get("max_tokens", 4096)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0 or max_tokens > 4096:
            raise ValueError("max_tokens must be an integer > 0 and <= 4096")

        temperature = model_config.get("temperature", 0.0)
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or temperature < 0:
            raise ValueError("temperature must be a non-negative number")

        max_retries = model_config.get("max_retries", 0)
        if not isinstance(max_retries, int) or max_retries != 0:
            raise ValueError("max_retries must be 0 in PiTextClient; retries are controlled by caller")

        timeout_ms = model_config.get("timeout_ms", 120000)
        if not isinstance(timeout_ms, int) or timeout_ms <= 0 or timeout_ms > 120000:
            raise ValueError("timeout_ms must be an integer > 0 and <= 120000")

        reasoning = model_config.get("reasoning", "off")
        if reasoning != "off":
            raise ValueError("reasoning must be 'off' in PiTextClient")

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > 120.0:
            raise ValueError("timeout must be a positive number <= 120.0")

        self.model_config: dict[str, Any] = {
            "provider": str(provider),
            "model_id": str(model_id),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "max_retries": int(max_retries),
            "timeout_ms": int(timeout_ms),
            "reasoning": "off",
        }

        if ledger is not None:
            self.ledger = ledger
        else:
            default_budget = EvolveBudget(max_calls=200, max_tokens=400000, deadline_seconds=3600)
            self.ledger = LLMLedger(budget=default_budget)

        self.role = role
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.timeout = float(timeout)

    def _parse_messages(
        self, messages: str | list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]], str]:
        """Validate messages, fail closed on non-text/tool items, and return (system, initial, prompt)."""
        if isinstance(messages, str):
            if not messages.strip():
                raise ValueError("Message string cannot be empty")
            return "", [], messages

        if not isinstance(messages, list):
            raise TypeError("messages must be a string or a list of dictionaries")

        if not messages:
            raise ValueError("messages list cannot be empty")

        allowed_roles = {"system", "user", "assistant"}
        system_chunks: list[str] = []
        dialogue: list[dict[str, Any]] = []

        for idx, m in enumerate(messages):
            if not isinstance(m, dict):
                raise TypeError(f"Message at index {idx} must be a dictionary")
            role = m.get("role")
            if role not in allowed_roles:
                raise ValueError(
                    f"Unsupported message role '{role}' at index {idx}; only 'system', 'user', 'assistant' permitted"
                )

            # Strictly reject tool calls or structured tool invocations (fail closed)
            if "tool_calls" in m or "tool_call_id" in m or "toolCallId" in m:
                raise ValueError(f"Message at index {idx} contains tool call fields; PiTextClient is text-only")

            content = m.get("content")
            if not isinstance(content, str):
                raise TypeError(f"Message content at index {idx} must be a string; got {type(content).__name__}")

            if role == "system":
                system_chunks.append(content)
            else:
                dialogue.append({"role": role, "content": content})

        if not dialogue:
            raise ValueError("No user message provided in conversation")

        if dialogue[-1]["role"] != "user":
            raise ValueError("Last message in conversation must be a user message")

        system_prompt = "\n\n".join(system_chunks)
        initial_messages = dialogue[:-1]
        prompt_text = dialogue[-1]["content"]

        return system_prompt, initial_messages, prompt_text

    def invoke(self, messages: str | list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        """Synchronously execute prompt via dedicated Pi runner subprocess."""
        # Fail closed on existing running event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            raise ValueError(
                "PiTextClient.invoke is synchronous and cannot be called from within an active asyncio event loop. "
                "Please call invoke outside an active event loop."
            )

        # Preflight budget check before spawning subprocess
        self.ledger.check_budget(role=self.role)

        run_id = f"pi_text_{uuid.uuid4().hex[:12]}"
        return asyncio.run(self._ainvoke(run_id=run_id, messages=messages, **kwargs))

    async def _ainvoke(
        self, run_id: str, messages: str | list[dict[str, Any]], **kwargs: Any
    ) -> SimpleNamespace:
        system_prompt, initial_messages, prompt_text = self._parse_messages(messages)

        client = PiRunnerClient()
        start_time = time.perf_counter()
        turn_index = 1
        call_id = f"{run_id}:{turn_index}"
        turn_receipt_received = False
        turn_receipt_frame: Optional[dict[str, Any]] = None
        turn_reserved = False

        async def turn_handler(r_id: str, t_idx: int, p_input: Optional[dict[str, Any]] = None) -> dict[str, Any]:
            nonlocal turn_reserved
            cid = f"{r_id}:{t_idx}"
            try:
                if r_id != run_id:
                    return {"allowed": False, "error_type": "CROSS_RUN_MISMATCH"}
                self.ledger.reserve_turn(role=self.role, call_id=cid)
                turn_reserved = True
                return {"allowed": True}
            except BudgetExceededError as exc:
                return {"allowed": False, "error_type": exc.cap_type or "BUDGET_EXCEEDED", "error": str(exc)}
            except Exception as exc:
                return {"allowed": False, "error_type": "BUDGET_ERROR", "error": str(exc)}

        async def receipt_handler(frame: dict[str, Any]) -> None:
            nonlocal turn_receipt_received, turn_receipt_frame
            turn_receipt_received = True
            turn_receipt_frame = frame
            cid = f"{run_id}:{frame.get('turn_index', 1)}"
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            st = frame.get("status", "success")
            err = frame.get("error")
            raw_u = frame.get("usage")
            if _is_placeholder_or_missing_usage(raw_u):
                clean_u = None
            else:
                clean_u = _standard_usage(raw_u)
                if clean_u:
                    clean_u["cost"] = None
            self.ledger.settle_turn(
                call_id=cid,
                usage=clean_u,
                latency_ms=elapsed_ms,
                status=st,
                error_type=err,
            )

        exec_res: Optional[PiExecutionResult] = None
        execution_err: Optional[Exception] = None

        try:
            await client.start()
            await client.init_run(
                run_id=run_id,
                system_prompt=system_prompt,
                tools=[],
                model_config=self.model_config,
                turn_handler=turn_handler,
                receipt_handler=receipt_handler,
                is_test=False,
                timeout=self.timeout,
                initial_messages=initial_messages,
            )
            exec_res = await client.prompt(
                run_id=run_id,
                text=prompt_text,
                timeout=self.timeout,
            )
            if exec_res.stop_reason != "stop":
                raise RuntimeError(f"Incomplete Pi text response: stop_reason={exec_res.stop_reason}")
            if not exec_res.text.strip():
                raise RuntimeError("Pi returned an empty text response")
        except Exception as exc:
            execution_err = exc
            if turn_reserved and not turn_receipt_received and call_id in self.ledger._pending_reservations:
                self.ledger.cancel_pending(call_id=call_id, error_type=type(exc).__name__)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            await client.close()

            if not turn_receipt_received and turn_reserved and call_id in self.ledger._pending_reservations:
                raw_u = exec_res.usage if exec_res else None
                clean_u = None if _is_placeholder_or_missing_usage(raw_u) else _standard_usage(raw_u)
                if clean_u:
                    clean_u["cost"] = None
                st = "success" if exec_res and exec_res.stop_reason != "error" else "error"
                err = None if exec_res and exec_res.stop_reason != "error" else "RUNNER_ERROR"
                self.ledger.settle_turn(
                    call_id=call_id,
                    usage=clean_u,
                    latency_ms=elapsed_ms,
                    status=st,
                    error_type=err,
                )

            if self.artifact_dir:
                all_msgs: list[dict[str, Any]] = []
                if isinstance(messages, str):
                    all_msgs = [{"role": "user", "content": messages}]
                elif isinstance(messages, list):
                    all_msgs = list(messages)
                raw_art_u = (turn_receipt_frame.get("usage") if turn_receipt_frame else None) or (exec_res.usage if exec_res else None)
                art_usage = None
                if not _is_placeholder_or_missing_usage(raw_art_u) and isinstance(raw_art_u, dict):
                    art_usage = _standard_usage(raw_art_u)
                _write_call_artifact(
                    artifact_dir=self.artifact_dir,
                    run_id=run_id,
                    role=self.role,
                    model_config=self.model_config,
                    system_prompt=system_prompt,
                    messages=all_msgs,
                    content=exec_res.text if exec_res else "",
                    usage=art_usage,
                    latency_ms=elapsed_ms,
                    stop_reason=exec_res.stop_reason if exec_res else "error",
                    error=str(execution_err) if execution_err else None,
                    receipt=turn_receipt_frame,
                    ledger=self.ledger,
                )

        if exec_res is None:
            raise RuntimeError("Pi runner finished without execution result")

        if exec_res.stop_reason == "error":
            raise RuntimeError(f"Pi execution returned stop_reason='error': {exec_res.text or 'Model call failed'}")

        if exec_res.text == "" and exec_res.stop_reason != "stop":
            raise RuntimeError(f"Pi execution returned empty text with stop_reason='{exec_res.stop_reason}'")

        if exec_res.stop_reason not in ("stop",):
            raise RuntimeError(f"Incomplete Pi text response: stop_reason={exec_res.stop_reason}")
        if not exec_res.text.strip():
            raise RuntimeError("Pi returned an empty text response")
        raw_u = turn_receipt_frame.get("usage") if turn_receipt_frame else exec_res.usage
        return SimpleNamespace(content=exec_res.text, usage=_standard_usage(raw_u), stop_reason=exec_res.stop_reason)
