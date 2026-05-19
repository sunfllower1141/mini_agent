#!/usr/bin/env python3
"""Tests for MCP (Model Context Protocol) client integration."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

# Ensure workspace root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import AgentConfig, McpServerConfig, _apply_toml, _TOML_SCHEMA
from tools import (
    ToolResult,
    _TOOL_DISPATCH,
    _TOOL_SUMMARIES,
    clear_tool_cache,
    set_context,
)
from tools.mcp_client import (
    McpConnection,
    McpClientManager,
    McpConnectionError,
    McpRpcError,
    convert_mcp_input_schema,
    _parse_full_name,
    _result_to_tool_result,
    _build_mcp_hint,
    _make_mcp_dispatcher,
    _make_mcp_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ECHO_SERVER_SCRIPT = os.path.join(
    os.path.dirname(__file__), "tests", "mcp_echo_server.py"
)


def _echo_server_config(name: str = "test_echo") -> McpServerConfig:
    """Create a McpServerConfig pointing to the echo server fixture."""
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=[ECHO_SERVER_SCRIPT],
        env={},
        cwd="",
        enabled=True,
    )


# ---------------------------------------------------------------------------
# 1. Config parsing tests
# ---------------------------------------------------------------------------


class TestMcpServerConfig:
    """Tests for McpServerConfig parsing from TOML dicts."""

    def test_from_toml_dict_basic(self):
        """A minimal [[mcp_server]] block produces correct config."""
        entry = {
            "name": "filesystem",
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-server-filesystem", "/tmp"],
            "env": {},
            "cwd": "",
            "enabled": True,
        }
        cfg = McpServerConfig(
            name=entry["name"],
            command=entry["command"],
            args=entry["args"],
            env=entry.get("env", {}),
            cwd=entry.get("cwd", ""),
            enabled=entry.get("enabled", True),
        )
        assert cfg.name == "filesystem"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "@anthropic/mcp-server-filesystem", "/tmp"]
        assert cfg.enabled is True

    def test_from_toml_dict_defaults(self):
        """Missing optional fields get sensible defaults."""
        entry = {"name": "minimal", "command": "python"}
        cfg = McpServerConfig(
            name=entry["name"],
            command=entry["command"],
        )
        assert cfg.args == []
        assert cfg.env == {}
        assert cfg.cwd == ""
        assert cfg.enabled is True

    def test_disabled_server(self):
        """A server with enabled=False."""
        entry = {"name": "off", "command": "python", "enabled": False}
        cfg = McpServerConfig(
            name=entry["name"],
            command=entry["command"],
            enabled=entry.get("enabled", True),
        )
        assert cfg.enabled is False

    def test_toml_schema_includes_mcp_server(self):
        """_TOML_SCHEMA includes mcp_server key."""
        assert "mcp_server" in _TOML_SCHEMA
        assert _TOML_SCHEMA["mcp_server"] is list

    def test_apply_toml_parses_mcp_server_blocks(self):
        """_apply_toml correctly parses [[mcp_server]] TOML list."""
        config = AgentConfig()
        data = {
            "mcp_server": [
                {
                    "name": "echo1",
                    "command": "python",
                    "args": ["echo.py"],
                    "enabled": True,
                },
                {
                    "name": "echo2",
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"PORT": "3000"},
                    "enabled": False,
                },
            ]
        }
        _apply_toml(config, data)
        assert len(config.mcp_servers) == 2
        assert config.mcp_servers[0].name == "echo1"
        assert config.mcp_servers[0].command == "python"
        assert config.mcp_servers[0].args == ["echo.py"]
        assert config.mcp_servers[0].enabled is True
        assert config.mcp_servers[1].name == "echo2"
        assert config.mcp_servers[1].command == "node"
        assert config.mcp_servers[1].env == {"PORT": "3000"}
        assert config.mcp_servers[1].enabled is False

    def test_agent_config_has_mcp_servers_field(self):
        """AgentConfig has mcp_servers field with default empty list."""
        config = AgentConfig()
        assert config.mcp_servers == []


# ---------------------------------------------------------------------------
# 2. Schema conversion tests
# ---------------------------------------------------------------------------


class TestSchemaConversion:
    """Tests for MCP inputSchema → OpenAI parameters conversion."""

    def test_basic_pass_through(self):
        """A simple object schema passes through unchanged."""
        schema = {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        }
        result = convert_mcp_input_schema(schema)
        assert result == schema

    def test_strips_unsupported_keywords(self):
        """$schema, $defs, oneOf, anyOf, allOf, not are removed."""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "$defs": {"shared": {}},
            "oneOf": [],
            "anyOf": [],
            "allOf": [],
            "not": {},
        }
        result = convert_mcp_input_schema(schema)
        assert "$schema" not in result
        assert "$defs" not in result
        assert "oneOf" not in result
        assert "anyOf" not in result
        assert "allOf" not in result
        assert "not" not in result
        assert result["type"] == "object"
        assert "x" in result["properties"]

    def test_empty_input_schema(self):
        """None or empty dict produces default object schema."""
        assert convert_mcp_input_schema(None) == {
            "type": "object",
            "properties": {},
        }
        assert convert_mcp_input_schema({}) == {
            "type": "object",
            "properties": {},
        }

    def test_missing_type_defaults_to_object(self):
        """If type is missing, it's set to 'object'."""
        result = convert_mcp_input_schema({"properties": {"a": {"type": "string"}}})
        assert result["type"] == "object"

    def test_missing_properties_defaults_to_empty(self):
        """If properties is missing, default to empty dict."""
        result = convert_mcp_input_schema({"type": "object"})
        assert result["properties"] == {}


# ---------------------------------------------------------------------------
# 3. Connection lifecycle tests (integration with echo server)
# ---------------------------------------------------------------------------


class TestMcpConnectionLifecycle:
    """Tests that require the echo server subprocess."""

    def test_connect_and_initialize(self):
        """Connect spawns the process, initializes, and discovers tools."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            assert conn.connect() is True
            assert conn.is_connected is True
            assert conn.process is not None
            assert conn.process.poll() is None  # still running
        finally:
            conn.disconnect()

    def test_discover_tools(self):
        """After connect, tools are discovered."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            tools = conn.tool_schemas
            assert "echo" in tools
            assert "add" in tools
            assert "fail" in tools
            assert tools["echo"]["description"] == "Echo back the message"
            assert "inputSchema" in tools["echo"]
            assert "message" in tools["echo"]["inputSchema"]["properties"]
        finally:
            conn.disconnect()

    def test_call_tool_echo(self):
        """Call the echo tool and get result back."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            result = conn.call_tool("echo", {"message": "hello world"})
            assert result.success is True
            assert result.content == "hello world"
        finally:
            conn.disconnect()

    def test_call_tool_add(self):
        """Call the add tool."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            result = conn.call_tool("add", {"a": 3, "b": 7})
            assert result.success is True
            assert result.content == "10"
        finally:
            conn.disconnect()

    def test_call_tool_error_response(self):
        """When server returns isError=true, ToolResult.success=False."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            result = conn.call_tool("fail", {"reason": "test error"})
            assert result.success is False
            assert "Intentional failure" in result.content
            assert result.hint  # hint should be populated on failure
        finally:
            conn.disconnect()

    def test_call_unknown_tool(self):
        """Calling a tool the server doesn't know about returns error."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            result = conn.call_tool("nonexistent", {})
            assert result.success is False
        finally:
            conn.disconnect()

    def test_disconnect_stops_process(self):
        """After disconnect, process is not running."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        conn.connect()
        assert conn.process.poll() is None
        conn.disconnect()
        assert conn.is_connected is False
        assert conn.process is None
        # Process should be terminated

    def test_reconnect(self):
        """Reconnect after disconnect works."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            conn.connect()
            conn.disconnect()
            assert conn.reconnect() is True
            assert conn.is_connected is True
            result = conn.call_tool("echo", {"message": "after reconnect"})
            assert result.success is True
            assert result.content == "after reconnect"
        finally:
            conn.disconnect()

    def test_double_connect_is_idempotent(self):
        """Calling connect when already connected returns True immediately."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        try:
            assert conn.connect() is True
            assert conn.connect() is True  # second call
            assert conn.is_connected is True
        finally:
            conn.disconnect()


# ---------------------------------------------------------------------------
# 4. Manager + dispatch registration tests
# ---------------------------------------------------------------------------


class TestMcpClientManager:
    """Tests for McpClientManager orchestration."""

    def test_manager_creates_connections(self):
        """Manager creates connections from config."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        assert "test_echo" in manager._connections
        conn = manager._connections["test_echo"]
        assert isinstance(conn, McpConnection)
        assert conn.config.name == "test_echo"

    def test_manager_skips_disabled(self):
        """Disabled servers are not connected."""
        cfg = _echo_server_config()
        cfg.enabled = False
        manager = McpClientManager([cfg])
        assert "test_echo" not in manager._connections

    def test_manager_skips_missing_command(self):
        """Servers with no command are skipped with a warning."""
        cfg = McpServerConfig(name="bad", command="")
        manager = McpClientManager([cfg])
        assert "bad" not in manager._connections

    def test_start_all_and_registration(self):
        """start_all connects, discovers, and registers tools into dispatch."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        try:
            connected = manager.start_all()
            assert "test_echo" in connected

            # Tools should be in dispatch
            assert "mcp/test_echo/echo" in _TOOL_DISPATCH
            assert "mcp/test_echo/add" in _TOOL_DISPATCH
            assert "mcp/test_echo/fail" in _TOOL_DISPATCH

            # Tools should have summaries
            assert "mcp/test_echo/echo" in _TOOL_SUMMARIES
            assert "mcp/test_echo/add" in _TOOL_SUMMARIES
            assert "mcp/test_echo/fail" in _TOOL_SUMMARIES

            # Tools should be in TOOLS schema
            from tools.schema import TOOLS
            mcp_names = [
                t["function"]["name"]
                for t in TOOLS
                if t["function"]["name"].startswith("mcp/")
            ]
            assert "mcp/test_echo/echo" in mcp_names
            assert "mcp/test_echo/add" in mcp_names
            assert "mcp/test_echo/fail" in mcp_names
        finally:
            manager.shutdown_all()

    def test_call_via_manager(self):
        """MCP tool call via manager works."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        try:
            manager.start_all()
            result = manager.call_mcp_tool("mcp/test_echo/echo", {"message": "via manager"})
            assert result.success is True
            assert result.content == "via manager"
        finally:
            manager.shutdown_all()

    def test_call_via_dispatch(self):
        """Calling through _TOOL_DISPATCH works."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        try:
            manager.start_all()
            dispatcher = _TOOL_DISPATCH.get("mcp/test_echo/add")
            assert dispatcher is not None
            result = dispatcher({"a": 5, "b": 9}, None, None)
            assert result.success is True
            assert result.content == "14"
        finally:
            manager.shutdown_all()

    def test_call_unknown_server_via_manager(self):
        """Calling a non-existent server returns error."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        try:
            manager.start_all()
            result = manager.call_mcp_tool("mcp/nonexistent/tool", {})
            assert result.success is False
            assert "Unknown MCP server" in result.content
        finally:
            manager.shutdown_all()

    def test_registered_tools_list(self):
        """registered_tools() returns all fully-qualified names."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        try:
            manager.start_all()
            tools = manager.registered_tools()
            assert "mcp/test_echo/echo" in tools
            assert "mcp/test_echo/add" in tools
            assert "mcp/test_echo/fail" in tools
            assert len(tools) == 3
        finally:
            manager.shutdown_all()

    def test_shutdown_all(self):
        """shutdown_all disconnects all servers."""
        cfg = _echo_server_config()
        manager = McpClientManager([cfg])
        manager.start_all()
        assert manager._connections["test_echo"].is_connected
        manager.shutdown_all()
        assert not manager._connections["test_echo"].is_connected


# ---------------------------------------------------------------------------
# 5. Utility/helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for helper functions."""

    def test_parse_full_name_valid(self):
        """Valid mcp/server/tool names parse correctly."""
        server, tool = _parse_full_name("mcp/filesystem/read_file")
        assert server == "filesystem"
        assert tool == "read_file"

    def test_parse_full_name_with_slashes_in_tool(self):
        """Tool name can contain slashes."""
        server, tool = _parse_full_name("mcp/server/sub/tool")
        assert server == "server"
        assert tool == "sub/tool"

    def test_parse_full_name_invalid_prefix(self):
        """Name not starting with mcp/ raises ValueError."""
        with pytest.raises(ValueError, match="Invalid MCP tool name"):
            _parse_full_name("filesystem/read_file")

    def test_parse_full_name_too_short(self):
        """Name without enough parts raises ValueError."""
        with pytest.raises(ValueError, match="Invalid MCP tool name"):
            _parse_full_name("mcp/tool")

    def test_parse_full_name_empty_server(self):
        """Empty server name raises ValueError."""
        with pytest.raises(ValueError):
            _parse_full_name("mcp//tool")

    def test_result_to_tool_result_success(self):
        """MCP success result → ToolResult(success=True)."""
        mcp_result = {
            "content": [{"type": "text", "text": "hello"}],
            "isError": False,
        }
        result = _result_to_tool_result(mcp_result, "test", "echo")
        assert result.success is True
        assert result.content == "hello"
        assert result.hint == ""

    def test_result_to_tool_result_error(self):
        """MCP error result → ToolResult(success=False) with hint."""
        mcp_result = {
            "content": [{"type": "text", "text": "Something broke"}],
            "isError": True,
        }
        result = _result_to_tool_result(mcp_result, "test", "fail")
        assert result.success is False
        assert "Something broke" in result.content
        assert result.hint != ""

    def test_result_to_tool_result_multi_content(self):
        """Multiple content blocks are joined with newlines."""
        mcp_result = {
            "content": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ],
            "isError": False,
        }
        result = _result_to_tool_result(mcp_result, "test", "echo")
        assert result.content == "line1\nline2"

    def test_result_to_tool_result_empty(self):
        """Empty content → (empty result)."""
        mcp_result = {"content": [], "isError": False}
        result = _result_to_tool_result(mcp_result, "test", "echo")
        assert result.success is True
        assert result.content == "(empty result)"

    def test_build_mcp_hint(self):
        """Hint includes server, tool, and error."""
        hint = _build_mcp_hint("myserver", "mytool", "connection refused")
        assert "mcp/myserver/mytool" in hint
        assert "connection refused" in hint

    def test_make_mcp_dispatcher(self):
        """Dispatcher calls manager.call_mcp_tool with correct name."""
        class FakeManager:
            def __init__(self):
                self.called_with = None

            def call_mcp_tool(self, full_name, args):
                self.called_with = (full_name, args)
                return ToolResult(success=True, content="fake")

        mgr = FakeManager()
        dispatcher = _make_mcp_dispatcher(mgr, "mcp/test/tool")
        result = dispatcher({"x": 1}, None, None)
        assert result.success is True
        assert mgr.called_with == ("mcp/test/tool", {"x": 1})

    def test_make_mcp_summary(self):
        """Summary includes server and tool names."""
        summary = _make_mcp_summary("srv", "tool")
        result = summary({"key": "value"})
        assert "mcp/srv/tool" in result
        assert "key=value" in result

    def test_make_mcp_summary_long_value_truncated(self):
        """Long values are truncated at 40 chars."""
        summary = _make_mcp_summary("srv", "tool")
        long_val = "x" * 50
        result = summary({"key": long_val})
        assert "..." in result
        assert len(result) < 120  # not huge

    def test_make_mcp_summary_no_args(self):
        """Empty args shows just the tool name."""
        summary = _make_mcp_summary("srv", "tool")
        result = summary({})
        assert result == "mcp/srv/tool()"


# ---------------------------------------------------------------------------
# 6. Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error paths."""

    def test_connection_to_nonexistent_command(self):
        """Connecting to a non-existent executable fails gracefully."""
        cfg = McpServerConfig(
            name="bad",
            command="/nonexistent/path/xyzzy",
        )
        conn = McpConnection(cfg)
        assert conn.connect() is False
        assert conn.is_connected is False

    def test_call_tool_when_not_connected(self):
        """Calling call_tool on a disconnected connection returns failure result."""
        cfg = _echo_server_config()
        conn = McpConnection(cfg)
        result = conn.call_tool("echo", {})
        assert result.success is False

    def test_mcp_rpc_error(self):
        """McpRpcError carries code and message."""
        error = McpRpcError({"code": -32000, "message": "bad request"})
        assert error.code == -32000
        assert "bad request" in str(error)

    def test_mcp_connection_error(self):
        """McpConnectionError carries message."""
        error = McpConnectionError("server gone")
        assert "server gone" in str(error)


# ---------------------------------------------------------------------------
# 7. Edge-case / robustness tests
# ---------------------------------------------------------------------------


class TestMcpEdgeCases:
    """Edge-case and robustness tests for the MCP client."""

    # ------------------------------------------------------------------
    # Helpers for creating one-off server scripts
    # ------------------------------------------------------------------

    @staticmethod
    def _write_temp_server(script_text: str) -> str:
        """Write *script_text* to a temp .py file and return its path."""
        fd, path = tempfile.mkstemp(suffix=".py", prefix="mcp_test_")
        with os.fdopen(fd, "w") as fh:
            fh.write(script_text)
        return path

    @staticmethod
    def _slow_server_script() -> str:
        """Return a server script that sleeps 0.5 s before each tools/call."""
        return textwrap.dedent("""\
        import json, sys, time
        TOOLS = [{"name":"slow_echo","description":"Echo after delay",
                  "inputSchema":{"type":"object","properties":{"msg":{"type":"string"}},
                                 "required":["msg"]}}]
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try: req = json.loads(line)
            except json.JSONDecodeError: continue
            rid = req.get("id")
            if rid is None: continue
            method = req.get("method","")
            if method == "initialize":
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},
                              "serverInfo":{"name":"slow","version":"1.0"}}})+"\\n")
            elif method == "tools/list":
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"tools":TOOLS}})+"\\n")
            elif method == "tools/call":
                time.sleep(0.5)
                msg = req.get("params",{}).get("arguments",{}).get("msg","")
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"content":[{"type":"text","text":msg}],"isError":False}})+"\\n")
            else:
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "error":{"code":-32601,"message":"Not found"}})+"\\n")
            sys.stdout.flush()
        """)

    @staticmethod
    def _stderr_server_script() -> str:
        """Return a server script that writes diagnostic lines to stderr."""
        return textwrap.dedent("""\
        import json, sys
        TOOLS = [{"name":"echo","description":"Echo back","inputSchema":{
            "type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]}}]
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try: req = json.loads(line)
            except json.JSONDecodeError: continue
            rid = req.get("id")
            if rid is None: continue
            method = req.get("method","")
            # Write diagnostic noise to stderr before every response
            sys.stderr.write(f"DEBUG: handling {method}\\n")
            sys.stderr.flush()
            if method == "initialize":
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},
                              "serverInfo":{"name":"noisy","version":"1.0"}}})+"\\n")
            elif method == "tools/list":
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"tools":TOOLS}})+"\\n")
            elif method == "tools/call":
                msg = req.get("params",{}).get("arguments",{}).get("msg","")
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"content":[{"type":"text","text":msg}],"isError":False}})+"\\n")
            else:
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "error":{"code":-32601,"message":"Not found"}})+"\\n")
            sys.stdout.flush()
        """)

    @staticmethod
    def _junk_stdout_server_script() -> str:
        """Return a server script that writes a junk line to stdout before the
        real JSON-RPC response."""
        return textwrap.dedent("""\
        import json, sys
        TOOLS = [{"name":"echo","description":"Echo back","inputSchema":{
            "type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]}}]
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try: req = json.loads(line)
            except json.JSONDecodeError: continue
            rid = req.get("id")
            if rid is None: continue
            method = req.get("method","")
            if method == "initialize":
                sys.stdout.write("bootstrap v1.0 ready\\n")  # junk line
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},
                              "serverInfo":{"name":"junk","version":"1.0"}}})+"\\n")
            elif method == "tools/list":
                sys.stdout.write("# listing tools...\\n")  # junk line
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"tools":TOOLS}})+"\\n")
            elif method == "tools/call":
                msg = req.get("params",{}).get("arguments",{}).get("msg","")
                sys.stdout.write(f"tool_call({msg})\\n")  # junk line
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "result":{"content":[{"type":"text","text":msg}],"isError":False}})+"\\n")
            else:
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,
                    "error":{"code":-32601,"message":"Not found"}})+"\\n")
            sys.stdout.flush()
        """)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_slow_server_response(self):
        """MCP client handles a server that takes ~0.5 s to respond.

        Verifies that the client does not have an artificial timeout
        that cuts off slow-but-responsive servers.  The call should
        succeed and the elapsed wall-clock time should be >= the
        server's artificial delay.
        """
        script = self._slow_server_script()
        path = self._write_temp_server(script)
        try:
            cfg = McpServerConfig(name="slow", command=sys.executable, args=[path])
            conn = McpConnection(cfg)
            try:
                conn.connect()
                t0 = time.time()
                result = conn.call_tool("slow_echo", {"msg": "hello"})
                elapsed = time.time() - t0
                assert result.success is True
                assert result.content == "hello"
                # Must have waited at least the 0.5 s server sleep
                assert elapsed >= 0.4, f"Expected >= 0.4 s, got {elapsed:.3f}"
            finally:
                conn.disconnect()
        finally:
            os.unlink(path)

    def test_stderr_noise_does_not_break_stdout(self):
        """Server that writes diagnostic lines to stderr still communicates
        correctly over stdout."""
        script = self._stderr_server_script()
        path = self._write_temp_server(script)
        try:
            cfg = McpServerConfig(name="noisy", command=sys.executable, args=[path])
            conn = McpConnection(cfg)
            try:
                conn.connect()
                result = conn.call_tool("echo", {"msg": "hello"})
                assert result.success is True
                assert result.content == "hello"
            finally:
                conn.disconnect()
        finally:
            os.unlink(path)

    def test_junk_stdout_lines_skipped(self):
        """Client skips non-JSON junk lines on stdout before the valid
        JSON-RPC response arrives."""
        script = self._junk_stdout_server_script()
        path = self._write_temp_server(script)
        try:
            cfg = McpServerConfig(name="junk", command=sys.executable, args=[path])
            conn = McpConnection(cfg)
            try:
                conn.connect()
                result = conn.call_tool("echo", {"msg": "hello"})
                assert result.success is True
                assert result.content == "hello"
            finally:
                conn.disconnect()
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cleanup_mcp_tools():
    """Remove MCP-registered tools from global state after each test."""
    yield
    from tools.schema import TOOLS

    # Remove MCP tools from TOOLS list
    TOOLS[:] = [t for t in TOOLS if not t["function"]["name"].startswith("mcp/")]

    # Remove from dispatch
    for key in list(_TOOL_DISPATCH.keys()):
        if key.startswith("mcp/"):
            del _TOOL_DISPATCH[key]

    # Remove from summaries
    for key in list(_TOOL_SUMMARIES.keys()):
        if key.startswith("mcp/"):
            del _TOOL_SUMMARIES[key]
