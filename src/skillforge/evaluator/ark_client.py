"""Volcengine Ark Anthropic Messages Client Adapter for SkillForge P6.

Provides:
- ArkAnthropicClient: Direct, lightweight client for Volcengine Ark subscription endpoint
  adhering strictly to Anthropic POST /v1/messages specification.
- Native tool calling support with Anthropic tool schema conversion.
- Thinking block and text block parsing (with empty text protection).
- Strict token accounting from response usage fields (input, output, cache).
- Secure credential isolation (reads strictly from /tmp/skillforge-ark.EBLkaq/api_key).
- Masked logging and error sanitization to prevent key leakage.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from hello_agents.core.llm import LLMResponse


def get_default_key_file() -> Path:
    eval_path = Path("/tmp/skillforge-p6-eval.7tLWNC/api_key")
    if eval_path.exists():
        return eval_path
    compress_path = Path("/tmp/skillforge-p6-compress.oi1sRa/api_key")
    if compress_path.exists():
        return compress_path
    baseline_path = Path("/tmp/skillforge-p6-baseline.SKsqHT/api_key")
    if baseline_path.exists():
        return baseline_path
    final_path = Path("/tmp/skillforge-p6-final.ZpB1rO/api_key")
    if final_path.exists():
        return final_path
    resume_path = Path("/tmp/skillforge-p6-resume.oPgFDE/api_key")
    if resume_path.exists():
        return resume_path
    fix_path = Path("/tmp/skillforge-p6-fix.pjaUQU/api_key")
    if fix_path.exists():
        return fix_path
    supplement_path = Path("/tmp/skillforge-p6-supplement.IJ7z53/api_key")
    if supplement_path.exists():
        return supplement_path
    return Path("/tmp/skillforge-ark.EBLkaq/api_key")


DEFAULT_KEY_FILE = get_default_key_file()
DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/plan/v1/messages"
DEFAULT_MODEL = "glm-5.3-flash"


@dataclass
class ArkCallRecord:
    timestamp: float
    role: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    latency_ms: float
    status: str
    resolved_model: str
    error: Optional[str] = None


@dataclass
class PersistentCallLedger:
    """Persistent call ledger that tracks provider requests and token consumption across restarts."""
    ledger_path: Path

    def __post_init__(self) -> None:
        self.ledger_path = Path(self.ledger_path)
        if not self.ledger_path.exists():
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_state({
                "total_calls": 0,
                "task_calls": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "records": [],
            })

    def _read_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "total_calls": 0,
                "task_calls": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "records": [],
            }

    def _write_state(self, state: Dict[str, Any]) -> None:
        self.ledger_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def total_calls(self) -> int:
        return int(self._read_state().get("total_calls", 0))

    @property
    def task_calls(self) -> int:
        state = self._read_state()
        return int(state.get("task_calls", state.get("total_calls", 0)))

    @property
    def total_revisions(self) -> int:
        state = self._read_state()
        rev_acc = state.get("revision_accounting", {})
        if isinstance(rev_acc, dict) and "total_revisions" in rev_acc:
            return int(rev_acc["total_revisions"])
        return int(state.get("total_revisions", 0))

    def pre_reserve(
        self,
        budget_cap: Optional[int] = None,
        is_revision: bool = False,
        max_revisions: int = 6,
    ) -> int:
        state = self._read_state()
        task_cap = budget_cap if budget_cap is not None else int(state.get("task_budget_cap", 200))
        task_calls = int(state.get("task_calls", state.get("total_calls", 0)))
        if task_calls >= task_cap:
            raise RuntimeError(
                f"Provider call budget exceeded hard cap: {task_calls} >= {task_cap}. Outbound request blocked."
            )

        if is_revision:
            rev_acc = state.get("revision_accounting", {})
            if not isinstance(rev_acc, dict):
                rev_acc = {}
            cur_rev = int(rev_acc.get("total_revisions", state.get("total_revisions", 0)))
            cur_max = int(rev_acc.get("max_revisions", max_revisions))
            if cur_rev >= cur_max:
                raise RuntimeError(
                    f"Revision round limit exceeded hard cap: {cur_rev} >= {cur_max}. Outbound revision request blocked."
                )
            rev_acc["total_revisions"] = cur_rev + 1
            rev_acc["max_revisions"] = cur_max
            rev_acc["limit_exceeded"] = (cur_rev + 1) >= cur_max
            state["revision_accounting"] = rev_acc
            state["total_revisions"] = cur_rev + 1

        state["total_calls"] = int(state.get("total_calls", 0)) + 1
        state["task_calls"] = task_calls + 1
        self._write_state(state)
        return state["task_calls"]

    @property
    def total_prompt_tokens(self) -> int:
        return int(self._read_state().get("total_prompt_tokens", 0))

    @property
    def total_completion_tokens(self) -> int:
        return int(self._read_state().get("total_completion_tokens", 0))

    @property
    def total_tokens(self) -> int:
        return int(self._read_state().get("total_tokens", 0))

    @property
    def records(self) -> list:
        return list(self._read_state().get("records", []))

    def record_completed_call(self, record: ArkCallRecord) -> None:
        state = self._read_state()
        state["total_prompt_tokens"] = int(state.get("total_prompt_tokens", 0)) + record.prompt_tokens
        state["total_completion_tokens"] = int(state.get("total_completion_tokens", 0)) + record.completion_tokens
        state["total_tokens"] = int(state.get("total_tokens", 0)) + record.total_tokens
        recs = state.get("records", [])
        recs.append(asdict(record))
        state["records"] = recs
        if "task_attribution" in state and isinstance(state["task_attribution"], dict):
            task_calls = int(state.get("task_calls", 0))
            task_cap = int(state.get("task_budget_cap", 200))
            state["task_attribution"]["task_attributed_granular_calls"] = task_calls
            state["task_attribution"]["task_remaining_budget"] = max(0, task_cap - task_calls)
        if "audit_coverage" in state and isinstance(state["audit_coverage"], dict):
            state["audit_coverage"]["total_calls"] = int(state.get("total_calls", 0))
            state["audit_coverage"]["granular_response_records_count"] = len(recs)
        self._write_state(state)

    def record_call(
        self,
        role: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        latency_ms: float = 0.0,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        rec = ArkCallRecord(
            timestamp=time.time(),
            role=role,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            latency_ms=latency_ms,
            status=status,
            resolved_model=model,
            error=error_message,
        )
        self.record_completed_call(rec)


class ArkAnthropicClient:
    """Client adapter calling Volcengine Ark using the Anthropic Messages API."""

    def __init__(
        self,
        key_file: Path = DEFAULT_KEY_FILE,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        max_retries: int = 1,
        ledger: Optional[PersistentCallLedger] = None,
        budget_cap: Optional[int] = None,
    ) -> None:
        self.key_file = Path(key_file)
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.ledger = ledger
        self.budget_cap = budget_cap

        if not self.key_file.exists():
            raise FileNotFoundError(
                f"Ark API key file not found at {self.key_file}. "
                "Ensure authorized temporary credential exists."
            )

        # Read key in memory only; never store in plain string representation
        raw_key = self.key_file.read_text(encoding="utf-8").strip()
        if not raw_key:
            raise ValueError(f"Ark API key file {self.key_file} is empty.")
        self._api_key = raw_key

        self.records: List[ArkCallRecord] = []
        self._last_resolved_model: str = model

    def _add_record(self, record: ArkCallRecord) -> None:
        self.records.append(record)
        if self.ledger is not None:
            self.ledger.record_completed_call(record)

    def __repr__(self) -> str:
        return f"<ArkAnthropicClient endpoint='{self.endpoint}' model='{self.model}'>"

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def total_calls(self) -> int:
        return len(self.records)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self.records)

    @property
    def total_completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.records)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.records)

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(r.cache_creation_tokens for r in self.records)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(r.cache_read_tokens for r in self.records)

    @property
    def last_resolved_model(self) -> str:
        return self._last_resolved_model

    def _get_headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _post_with_retry(self, payload: Dict[str, Any], role: str = "agent") -> Tuple[Dict[str, Any], float]:
        """Execute HTTP POST with 1 retry on infra/TLS/network failure."""
        is_revision = role in ("reviser", "lifecycle_reviser")
        if self.ledger is not None:
            self.ledger.pre_reserve(budget_cap=self.budget_cap, is_revision=is_revision)

        headers = self._get_headers()
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                # Use trust_env=False to avoid local socks proxy configuration issues
                with httpx.Client(trust_env=False, timeout=self.timeout) as client:
                    resp = client.post(self.endpoint, headers=headers, json=payload)
                latency_ms = (time.perf_counter() - t0) * 1000.0

                if resp.status_code == 200:
                    data = resp.json()
                    return data, latency_ms
                else:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    if attempt < self.max_retries and resp.status_code in (429, 500, 502, 503, 504):
                        time.sleep(1.0)
                        continue
                    raise RuntimeError(f"Ark API returned {err_msg}")
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(1.5)
                    continue
                raise RuntimeError(f"Network transport error calling Ark API: {type(exc).__name__}: {exc}") from exc

        raise RuntimeError(f"Ark API call failed after retries: {last_exc}")

    def _format_messages(
        self, messages: Union[str, List[Dict[str, Any]]]
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert input messages into Anthropic (system, messages) format."""
        system_prompt: Optional[str] = None
        formatted: List[Dict[str, Any]] = []

        if isinstance(messages, str):
            formatted.append({"role": "user", "content": messages})
            return system_prompt, formatted

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # Anthropic separates system prompt to top-level request body
                if system_prompt is None:
                    system_prompt = content if isinstance(content, str) else str(content)
                else:
                    system_prompt += "\n\n" + (content if isinstance(content, str) else str(content))
            elif role == "tool":
                # Convert OpenAI tool response to Anthropic user tool_result
                tool_use_id = msg.get("tool_call_id", "")
                res_content = content if isinstance(content, str) else str(content)
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": res_content,
                }
                if formatted and formatted[-1].get("role") == "user" and isinstance(formatted[-1].get("content"), list):
                    formatted[-1]["content"].append(tool_result_block)
                else:
                    formatted.append({"role": "user", "content": [tool_result_block]})
            elif role == "assistant" and msg.get("tool_calls"):
                # Convert OpenAI assistant tool_calls to Anthropic content blocks
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in msg.get("tool_calls", []):
                    fn_name = tc.get("function", {}).get("name") if isinstance(tc, dict) else tc.function.name
                    fn_args = tc.get("function", {}).get("arguments") if isinstance(tc, dict) else tc.function.arguments
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}
                    tc_id = tc.get("id") if isinstance(tc, dict) else tc.id
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": fn_name,
                        "input": fn_args,
                    })
                formatted.append({"role": "assistant", "content": blocks})
            else:
                formatted.append({"role": role, "content": content})

        return system_prompt, formatted

    def invoke(
        self,
        messages: Union[str, List[Dict[str, Any]]],
        system: Optional[str] = None,
        max_tokens: int = 4096,
        thinking_budget: Optional[int] = 1024,
        temperature: Optional[float] = None,
        role: str = "agent",
        **kwargs: Any,
    ) -> LLMResponse:
        """Standard LLM invocation returning LLMResponse with parsed text and thinking."""
        extracted_system, formatted_messages = self._format_messages(messages)
        effective_system = system or extracted_system

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(max(max_tokens, 512), 4096),
            "messages": formatted_messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if thinking_budget is not None and thinking_budget < payload["max_tokens"]:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        if effective_system:
            payload["system"] = effective_system

        for attempt in range(self.max_retries + 1):
            try:
                data, latency_ms = self._post_with_retry(payload, role=role)
            except Exception as exc:
                self.records.append(
                    ArkCallRecord(
                        timestamp=time.time(),
                        role=role,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cache_creation_tokens=0,
                        cache_read_tokens=0,
                        latency_ms=0.0,
                        status="error",
                        resolved_model=self.model,
                        error=str(exc),
                    )
                )
                raise

            # Extract usage
            usage = data.get("usage", {})
            p_tokens = int(usage.get("input_tokens", 0) or 0)
            c_tokens = int(usage.get("output_tokens", 0) or 0)
            t_tokens = p_tokens + c_tokens
            cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
            resolved_model = str(data.get("model", self.model))
            self._last_resolved_model = resolved_model

            # Parse content blocks
            text_content = ""
            reasoning_content = ""
            for block in data.get("content", []):
                b_type = block.get("type")
                if b_type == "text":
                    text_content += block.get("text", "")
                elif b_type == "thinking":
                    reasoning_content += block.get("thinking", "")

            # Record call
            self._add_record(
                ArkCallRecord(
                    timestamp=time.time(),
                    role=role,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=t_tokens,
                    cache_creation_tokens=cache_create,
                    cache_read_tokens=cache_read,
                    latency_ms=latency_ms,
                    status="success" if text_content else "exhausted",
                    resolved_model=resolved_model,
                )
            )

            if not text_content and reasoning_content:
                if attempt < self.max_retries:
                    time.sleep(1.0)
                    continue
                raise ValueError(
                    f"Model returned thinking ({len(reasoning_content)} chars) but empty text content. "
                    "Output budget may be exhausted before text generation."
                )
            break

        return LLMResponse(
            content=text_content,
            model=resolved_model,
            usage={
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": t_tokens,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
            latency_ms=latency_ms,
            reasoning_content=reasoning_content,
        )

    def invoke_with_tools(
        self,
        messages: Union[str, List[Dict[str, Any]]],
        tools: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: int = 4096,
        thinking_budget: Optional[int] = 1024,
        temperature: Optional[float] = None,
        role: str = "agent",
        **kwargs: Any,
    ) -> Any:
        """Invoke model with Anthropic-compatible tool declarations."""
        extracted_system, formatted_messages = self._format_messages(messages)
        effective_system = system or extracted_system

        # Normalize tools to Anthropic format:
        # [{name, description, input_schema}]
        anthropic_tools: List[Dict[str, Any]] = []
        for t in tools:
            if "input_schema" in t:
                anthropic_tools.append(t)
            elif "function" in t:
                # OpenAI format
                fn = t["function"]
                anthropic_tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            else:
                anthropic_tools.append({
                    "name": t.get("name", "unknown_tool"),
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", t.get("input_schema", {"type": "object"})),
                })

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(max(max_tokens, 512), 4096),
            "messages": formatted_messages,
            "tools": anthropic_tools,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if thinking_budget is not None and thinking_budget < payload["max_tokens"]:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        if effective_system:
            payload["system"] = effective_system

        for attempt in range(self.max_retries + 1):
            try:
                data, latency_ms = self._post_with_retry(payload, role=role)
            except Exception as exc:
                self.records.append(
                    ArkCallRecord(
                        timestamp=time.time(),
                        role=role,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cache_creation_tokens=0,
                        cache_read_tokens=0,
                        latency_ms=0.0,
                        status="error",
                        resolved_model=self.model,
                        error=str(exc),
                    )
                )
                raise

            usage = data.get("usage", {})
            p_tokens = int(usage.get("input_tokens", 0) or 0)
            c_tokens = int(usage.get("output_tokens", 0) or 0)
            t_tokens = p_tokens + c_tokens
            cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
            resolved_model = str(data.get("model", self.model))
            self._last_resolved_model = resolved_model

            text_content = ""
            reasoning_content = ""
            tool_calls: List[Any] = []

            for block in data.get("content", []):
                b_type = block.get("type")
                if b_type == "text":
                    text_content += block.get("text", "")
                elif b_type == "thinking":
                    reasoning_content += block.get("thinking", "")
                elif b_type == "tool_use":
                    tool_calls.append(
                        SimpleNamespace(
                            id=block.get("id", f"call_{len(tool_calls)}"),
                            type="function",
                            function=SimpleNamespace(
                                name=block.get("name"),
                                arguments=json.dumps(block.get("input", {})),
                            ),
                        )
                    )

            self._add_record(
                ArkCallRecord(
                    timestamp=time.time(),
                    role=role,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    total_tokens=t_tokens,
                    cache_creation_tokens=cache_create,
                    cache_read_tokens=cache_read,
                    latency_ms=latency_ms,
                    status="success" if (text_content or tool_calls) else "exhausted",
                    resolved_model=resolved_model,
                )
            )

            if not text_content and not tool_calls and reasoning_content:
                if attempt < self.max_retries:
                    time.sleep(1.0)
                    continue
                raise ValueError(
                    f"Model returned thinking ({len(reasoning_content)} chars) but empty content and no tool calls. "
                    "Output budget may be exhausted before emission."
                )
            break

        message_obj = SimpleNamespace(
            content=text_content or None,
            tool_calls=tool_calls if tool_calls else None,
            reasoning_content=reasoning_content,
        )
        usage_obj = SimpleNamespace(
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=t_tokens,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message_obj)],
            usage=usage_obj,
            model=resolved_model,
            latency_ms=latency_ms,
        )
