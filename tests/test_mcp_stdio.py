"""Tests for Model Context Protocol (MCP) local stdio client and ToolBroker adapter."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from skillforge import AgentRuntime, McpStdioClient, McpTool, ToolBroker
from skillforge.storage.db import init_db

# Minimal Python script implementing a valid stdio MCP server (2024-11-05 spec)
MOCK_MCP_SERVER_CODE = r"""
import sys, json

def send_response(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue

    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "mock-mcp-inventory-server",
                    "version": "1.2.0"
                }
            }
        })
    elif method == "notifications/initialized":
        # Notification, no response required
        pass
    elif method == "tools/list":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "mcp_query_inventory",
                        "description": "Look up warehouse inventory for SKU",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "sku": {"type": "string", "description": "Stock keeping unit"},
                                "warehouse_id": {"type": "string", "description": "Target warehouse code"}
                            },
                            "required": ["sku"]
                        }
                    },
                    {
                        "name": "mcp_reserve_stock",
                        "description": "Reserve quantity of items for an order",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "sku": {"type": "string", "description": "Stock keeping unit"},
                                "quantity": {"type": "integer", "description": "Number of units to reserve"}
                            },
                            "required": ["sku", "quantity"]
                        }
                    }
                ]
            }
        })
    elif method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "mcp_query_inventory":
            sku = args.get("sku")
            wh = args.get("warehouse_id", "WH-MAIN")
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"sku": sku, "warehouse": wh, "available_qty": 42, "status": "IN_STOCK"})
                        }
                    ],
                    "isError": False
                }
            })
        elif tool_name == "mcp_reserve_stock":
            sku = args.get("sku")
            qty = args.get("quantity", 0)
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"reservation_id": "RES-8891", "sku": sku, "reserved_qty": qty, "status": "CONFIRMED"})
                        }
                    ],
                    "isError": False
                }
            })
        else:
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {tool_name}"
                }
            })
"""


@pytest.fixture
def mcp_server_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "mock_mcp_server.py"
    script_path.write_text(MOCK_MCP_SERVER_CODE, encoding="utf-8")
    return script_path


def test_mcp_stdio_client_lifecycle_and_handshake(mcp_server_script: Path):
    """Verify McpStdioClient starts subprocess, completes initialize handshake, lists tools, calls tool, and terminates cleanly."""
    cmd = [sys.executable, str(mcp_server_script)]
    client = McpStdioClient(command=cmd)

    try:
        client.start()
        assert client.server_info.get("name") == "mock-mcp-inventory-server"
        assert client.server_info.get("version") == "1.2.0"
        assert client.protocol_version == "2024-11-05"

        tools = client.list_tools()
        assert len(tools) == 2
        tool_names = [t["name"] for t in tools]
        assert "mcp_query_inventory" in tool_names
        assert "mcp_reserve_stock" in tool_names

        # Direct call
        call_res = client.call_tool("mcp_query_inventory", {"sku": "SKU-900", "warehouse_id": "WH-EAST"})
        assert call_res.get("isError") is False
        content_items = call_res.get("content", [])
        assert len(content_items) == 1
        data = json.loads(content_items[0]["text"])
        assert data["sku"] == "SKU-900"
        assert data["warehouse"] == "WH-EAST"
        assert data["available_qty"] == 42
    finally:
        client.close()


def test_mcp_tools_integrated_into_tool_broker_and_runtime(mcp_server_script: Path, tmp_path: Path):
    """Verify ToolBroker registers MCP stdio tools, enforces schemas & allowlists, and captures provenance."""
    db_path = tmp_path / "mcp_test.db"
    conn = init_db(db_path)
    conn.close()

    cmd = [sys.executable, str(mcp_server_script)]
    broker = ToolBroker()
    client, registered = broker.register_mcp_server(command=cmd, auto_allow=True)

    try:
        assert len(registered) == 2
        assert "mcp_query_inventory" in registered
        assert "mcp_reserve_stock" in registered
        assert "mcp_query_inventory" in broker.application_allowlist
        assert "mcp_reserve_stock" in broker.application_allowlist

        # Verify schema conversion
        params = broker.get_parameters("mcp_reserve_stock")
        param_names = {p.name: p for p in params}
        assert "sku" in param_names
        assert param_names["sku"].required is True
        assert param_names["sku"].type == "string"
        assert "quantity" in param_names
        assert param_names["quantity"].required is True
        assert param_names["quantity"].type == "integer"

        # Initialize AgentRuntime and execute tool call
        runtime = AgentRuntime(db_path=db_path, tool_broker=broker)
        runtime.start_run(run_id="run_mcp_01", task_id="task_mcp_01")

        # 1. Valid execution
        rec1 = runtime.execute_tool(
            run_id="run_mcp_01",
            tool_name="mcp_query_inventory",
            parameters={"sku": "SKU-ABC-123"},
        )
        assert rec1.status == "EXECUTED"
        assert "SKU-ABC-123" in rec1.output_text
        parsed = json.loads(rec1.output_text)
        assert parsed["available_qty"] == 42
        assert rec1.provenance is not None
        assert rec1.provenance.tool_name == "mcp_query_inventory"
        assert rec1.provenance.signature is not None
        assert rec1.provenance.signature.startswith("sha256:")
        assert len(rec1.provenance.signature.split(":", 1)[1]) == 64  # SHA-256 hex length

        # 2. Schema validation error on missing required field
        rec_bad = runtime.execute_tool(
            run_id="run_mcp_01",
            tool_name="mcp_reserve_stock",
            parameters={"sku": "SKU-ABC-123"},  # missing quantity
        )
        assert rec_bad.status == "REJECTED"
        assert rec_bad.error_type == "SCHEMA_VALIDATION_ERROR"

        # 3. Disallow tool from allowlist -> permission denied
        broker.application_allowlist.remove("mcp_reserve_stock")
        rec_denied = runtime.execute_tool(
            run_id="run_mcp_01",
            tool_name="mcp_reserve_stock",
            parameters={"sku": "SKU-ABC-123", "quantity": 5},
        )
        assert rec_denied.status == "REJECTED"
        assert rec_denied.error_type == "PERMISSION_DENIED"
    finally:
        client.close()
