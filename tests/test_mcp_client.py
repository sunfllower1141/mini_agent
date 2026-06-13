#!/usr/bin/env python3
"""Tests for MCP client (tools/mcp_client.py)."""

from __future__ import annotations

import sys
import unittest

from tools.mcp_client import (
    McpConnection,
    McpClientManager,
    McpError,
    McpConnectionError,
    get_mcp_manager,
    init_mcp_servers,
    shutdown_mcp,
)


# ---------------------------------------------------------------------------
# In-process mock MCP server (newline-delimited JSON-RPC over stdio)
# ---------------------------------------------------------------------------

def _mock_mcp_server_script() -> str:
    """Return a standalone Python script that acts as a minimal MCP server."""
    return r"""
import sys, json

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def read_msg():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)

# Handshake
req = read_msg()
assert req["method"] == "initialize"
send({"jsonrpc": "2.0", "id": req["id"], "result": {
    "protocolVersion": "2025-03-26",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "mock-server", "version": "1.0"},
}})

# Initialized notification
read_msg()  # initialized -- no response needed

# Main loop
while True:
    req = read_msg()
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [
                {"name": "echo", "description": "Echo back the input.",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "add", "description": "Add two numbers.",
                 "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
            ]
        }})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        if tool_name == "echo":
            text = tool_args.get("text", "")
            result = {"content": [{"type": "text", "text": f"ECHO: {text}"}]}
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif tool_name == "add":
            a = float(tool_args.get("a", 0))
            b = float(tool_args.get("b", 0))
            result = {"content": [{"type": "text", "text": str(a + b)}]}
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        break
"""


def mock_server_command():
    """Return a command that runs the mock MCP server as a subprocess."""
    return [sys.executable, "-c", _mock_mcp_server_script()]


# ---------------------------------------------------------------------------
# McpConnection tests
# ---------------------------------------------------------------------------

class TestMcpConnection(unittest.TestCase):
    """Unit tests for a single MCP connection."""

    def test_connect_and_discover(self):
        """Connect to the mock server and verify tools/list works."""
        cmd = mock_server_command()
        conn = McpConnection("mock", cmd)
        try:
            ok = conn.connect()
            self.assertTrue(ok)
            self.assertTrue(conn.is_connected)

            tools = conn.tools
            self.assertEqual(len(tools), 2)
            tool_names = [t["name"] for t in tools]
            self.assertIn("echo", tool_names)
            self.assertIn("add", tool_names)
        finally:
            conn.disconnect()

    def test_call_echo(self):
        """Call the echo tool and verify the result."""
        cmd = mock_server_command()
        conn = McpConnection("mock", cmd)
        try:
            conn.connect()
            result = conn.call_tool("echo", {"text": "hello world"})
            self.assertTrue(result.success)
            self.assertIn("ECHO: hello world", result.content)
        finally:
            conn.disconnect()

    def test_call_add(self):
        """Call the add tool and verify numeric result."""
        cmd = mock_server_command()
        conn = McpConnection("mock", cmd)
        try:
            conn.connect()
            result = conn.call_tool("add", {"a": 3, "b": 4})
            self.assertTrue(result.success)
            self.assertIn("7", result.content)  # "7" or "7.0"
        finally:
            conn.disconnect()

    def test_call_unknown_tool(self):
        """Calling a non-existent tool returns an error result."""
        cmd = mock_server_command()
        conn = McpConnection("mock", cmd)
        try:
            conn.connect()
            result = conn.call_tool("nonexistent", {})
            self.assertFalse(result.success)
            self.assertTrue(
                "Unknown tool" in result.content or "error" in result.content.lower()
            )
        finally:
            conn.disconnect()

    def test_disconnect_clears_state(self):
        """After disconnect, is_connected is False and tools list is empty."""
        cmd = mock_server_command()
        conn = McpConnection("mock", cmd)
        conn.connect()
        conn.disconnect()
        self.assertFalse(conn.is_connected)
        self.assertEqual(conn.tools, [])

    def test_connect_failure_bad_command(self):
        """Connecting to a non-existent command returns False."""
        conn = McpConnection("bad", ["/nonexistent/command_xyz_123"])
        ok = conn.connect()
        self.assertFalse(ok)
        self.assertFalse(conn.is_connected)


# ---------------------------------------------------------------------------
# McpClientManager tests
# ---------------------------------------------------------------------------

class TestMcpClientManager(unittest.TestCase):
    """Tests for the multi-server manager."""

    def setUp(self):
        """Create a manager configured with the mock server."""
        cmd = mock_server_command()
        self.mgr = McpClientManager({
            "mock": {"command": cmd},
        })

    def tearDown(self):
        self.mgr.shutdown()

    def test_discover(self):
        """Discover lists tools across all servers."""
        result = self.mgr.discover()
        self.assertTrue(result.success)
        self.assertIn("echo", result.content)
        self.assertIn("add", result.content)
        self.assertIn("mock", result.content)

    def test_call(self):
        """Call a tool by server and name."""
        result = self.mgr.call("mock", "echo", {"text": "test"})
        self.assertTrue(result.success)
        self.assertIn("ECHO: test", result.content)

    def test_call_bad_server(self):
        """Calling an unknown server returns an error."""
        result = self.mgr.call("nonexistent", "echo", {})
        self.assertFalse(result.success)
        self.assertTrue(
            "Unknown MCP server" in result.content or "nonexistent" in result.content
        )

    def test_call_bad_tool(self):
        """Calling an unknown tool returns an error."""
        result = self.mgr.call("mock", "nonexistent", {})
        self.assertFalse(result.success)
        self.assertIn("no tool", result.content.lower())

    def test_no_servers_configured(self):
        """Discover with no servers returns a helpful message."""
        mgr = McpClientManager({})
        result = mgr.discover()
        self.assertTrue(result.success)
        self.assertIn("No MCP servers configured", result.content)


# ---------------------------------------------------------------------------
# Module-level singleton tests
# ---------------------------------------------------------------------------

class TestModuleSingleton(unittest.TestCase):
    """Tests for get_mcp_manager / init_mcp_servers / shutdown_mcp."""

    def test_get_mcp_manager_returns_singleton(self):
        """Multiple calls return the same object."""
        mgr1 = get_mcp_manager()
        mgr2 = get_mcp_manager()
        self.assertIs(mgr1, mgr2)

    def test_init_mcp_servers_configures_manager(self):
        """init_mcp_servers configures the singleton manager."""
        try:
            cmd = mock_server_command()
            init_mcp_servers({"test_srv": {"command": cmd}})
            mgr = get_mcp_manager()
            result = mgr.discover()
            self.assertTrue(result.success)
            self.assertIn("echo", result.content)
        finally:
            shutdown_mcp()


# ---------------------------------------------------------------------------
# McpError / McpConnectionError tests
# ---------------------------------------------------------------------------

class TestMcpErrors(unittest.TestCase):
    """Tests for error types."""

    def test_mcp_error_str(self):
        """McpError renders code and message."""
        err = McpError({"code": -32601, "message": "Method not found"})
        self.assertIn("-32601", str(err))
        self.assertIn("Method not found", str(err))

    def test_mcp_connection_error_str(self):
        """McpConnectionError renders its message."""
        err = McpConnectionError("Server closed stdout")
        self.assertIn("Server closed stdout", str(err))
