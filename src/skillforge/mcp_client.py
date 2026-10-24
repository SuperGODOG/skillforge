"""Model Context Protocol (MCP) Local stdio Client and Broker Adapter.

Implements the official Model Context Protocol (2024-11-05 spec) over local stdio
JSON-RPC 2.0 transport for tool discovery and execution within SkillForge ToolBroker.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from hello_agents.tools import Tool, ToolParameter, ToolResponse

logger = logging.getLogger(__name__)


def _json_schema_to_tool_parameters(schema: Optional[dict[str, Any]]) -> list[ToolParameter]:
    """Convert an MCP inputSchema (JSON Schema object) into hello_agents ToolParameters."""
    if not schema or not isinstance(schema, dict):
        return []
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    params: list[ToolParameter] = []

    type_mapping = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
    }

    for prop_name, prop_spec in properties.items():
        if not isinstance(prop_spec, dict):
            continue
        raw_type = prop_spec.get("type", "string")
        param_type = type_mapping.get(raw_type, "string")
        description = prop_spec.get("description", "")
        required = prop_name in required_fields
        default = prop_spec.get("default", None)

        params.append(
            ToolParameter(
                name=prop_name,
                type=param_type,
                required=required,
                default=default,
                description=description,
            )
        )
    return params


class McpStdioClient:
    """Lightweight, robust stdio JSON-RPC 2.0 client for local MCP servers."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[dict[str, str]] = None,
        timeout: float = 10.0,
    ):
        self.command = list(command)
        self.cwd = Path(cwd) if cwd else None
        self.env = env
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_counter = 0
        self._is_initialized = False
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.protocol_version: str = "2024-11-05"

    def start(self) -> McpStdioClient:
        """Start the MCP server subprocess and perform standard initialize handshake."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self

            run_env = os.environ.copy()
            if self.env:
                run_env.update(self.env)

            try:
                self._proc = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(self.cwd) if self.cwd else None,
                    env=run_env,
                    text=True,
                    bufsize=1,  # Line buffered
                )
            except Exception as e:
                raise RuntimeError(f"Failed to spawn MCP server subprocess {self.command}: {e}") from e

            # 1. Send initialize request
            init_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "skillforge-mcp-client",
                        "version": "1.0.0",
                    },
                },
            }
            init_res = self._send_request_locked(init_req)
            if "error" in init_res:
                self.close()
                raise RuntimeError(f"MCP server initialize rejected: {init_res['error']}")

            result = init_res.get("result", {})
            self.server_info = result.get("serverInfo", {})
            self.server_capabilities = result.get("capabilities", {})
            self.protocol_version = result.get("protocolVersion", self.protocol_version)

            # 2. Send initialized notification
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            self._send_notification_locked(initialized_notification)
            self._is_initialized = True
            return self

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    def _send_notification_locked(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("MCP server subprocess is not running")
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Failed writing notification to MCP server stdio: {e}") from e

    def _send_request_locked(self, msg: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("MCP server subprocess is not running")

        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Failed writing request to MCP server stdio: {e}") from e

        # Read JSON-RPC response line
        # Wait for a matching response id (ignoring unhandled notifications or empty lines)
        target_id = msg.get("id")
        while True:
            resp_line = self._proc.stdout.readline()
            if not resp_line:
                stderr_output = ""
                if self._proc.stderr:
                    try:
                        stderr_output = self._proc.stderr.read()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"MCP server subprocess closed stdio prematurely. Stderr: {stderr_output.strip()}"
                )
            resp_line = resp_line.strip()
            if not resp_line:
                continue
            try:
                resp_json = json.loads(resp_line)
            except Exception:
                continue  # Ignore non-JSON diagnostic lines on stdout
            if resp_json.get("id") == target_id:
                return resp_json

    def list_tools(self) -> list[dict[str, Any]]:
        """Query server for supported tools using `tools/list`."""
        with self._lock:
            if not self._is_initialized:
                self.start()
            req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            }
            res = self._send_request_locked(req)
            if "error" in res:
                raise RuntimeError(f"MCP tools/list failed: {res['error']}")
            return res.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Invoke a tool on the MCP server using `tools/call`."""
        with self._lock:
            if not self._is_initialized:
                self.start()
            req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments or {},
                },
            }
            res = self._send_request_locked(req)
            if "error" in res:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": str(res["error"])}],
                }
            return res.get("result", {})

    def close(self) -> None:
        """Terminate the server process and clean up pipes."""
        with self._lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin and not self._proc.stdin.closed:
                        self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
            self._is_initialized = False

    def __enter__(self) -> McpStdioClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class McpTool(Tool):
    """hello_agents.tools.Tool wrapper around an MCP tool served over stdio."""

    def __init__(self, client: McpStdioClient, tool_def: dict[str, Any]):
        tool_name = tool_def["name"]
        description = tool_def.get("description", "")
        super().__init__(name=tool_name, description=description)
        self.client = client
        self.tool_def = tool_def
        self._parameters = _json_schema_to_tool_parameters(tool_def.get("inputSchema", {}))

    def get_parameters(self) -> list[ToolParameter]:
        return self._parameters

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        try:
            raw_res = self.client.call_tool(self.name, parameters)
            is_error = raw_res.get("isError", False)
            content_items = raw_res.get("content", [])
            text_parts = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
                else:
                    text_parts.append(json.dumps(item, ensure_ascii=False))

            combined_text = "\n".join(text_parts) if text_parts else json.dumps(raw_res, ensure_ascii=False)
            if is_error:
                return ToolResponse.fail(error=combined_text or f"MCP tool '{self.name}' returned error")
            return ToolResponse.success(text=combined_text)
        except Exception as e:
            return ToolResponse.fail(error=f"MCP call failed: {e}")
