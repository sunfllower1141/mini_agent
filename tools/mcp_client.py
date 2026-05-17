#!/usr/bin/env python3
"""
mcp_client.py — lightweight synchronous MCP (Model Context Protocol) client.

Manages stdio subprocess connections to MCP-compliant servers, discovers
their tools on startup, and registers them into the global dispatch table
under ``mcp/<server_name>/<tool_name>`` namespaced names.

All I/O is synchronous (thread-based) to match mini_agent's existing
synchronous tool dispatch.  Streamable HTTP/SSE transport is deferred to v2.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import json

from tools import ToolResult, _TOOL_DISPATCH, _TOOL_SUMMARIES
from config import McpServerConfig  # noqa: F401  — re-exported for convenience


# ---------------------------------------------------------------------------
# MCP error types
# ---------------------------------------------------------------------------

class McpRpcError(Exception):
    """A JSON-RPC error returned by the MCP server."""
    def __init__(self, error: dict):
        self.code = error.get("code", -1)
        self.message = error.get("message", "unknown")
        super().__init__(f"MCP RPC error {self.code}: {self.message}")


class McpConnectionError(Exception):
    """Connection to the MCP server was lost or could not be established."""


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

# JSON Schema keywords unsupported by OpenAI function-calling parameters.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "$schema", "$defs", "$id", "$ref", "$anchor",
    "oneOf", "anyOf", "allOf", "not",
    "if", "then", "else",
    "definitions", "dependencies",
    "patternProperties", "additionalItems", "contains",
    "minContains", "maxContains", "unevaluatedItems", "unevaluatedProperties",
})


def convert_mcp_input_schema(input_schema: dict | None) -> dict:
    """Convert an MCP inputSchema (JSON Schema 2020-12) to OpenAI tool
    parameters schema by stripping unsupported keywords.

    Returns ``{"type": "object", "properties": {}}`` for missing input.
    """
    if not input_schema or not isinstance(input_schema, dict):
        return {"type": "object", "properties": {}}

    cleaned: dict = {}
    for key, value in input_schema.items():
        if key not in _UNSUPPORTED_SCHEMA_KEYS:
            cleaned[key] = value

    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


# ---------------------------------------------------------------------------
# McpConnection — one per MCP server
# ---------------------------------------------------------------------------

class McpConnection:
    """Manages one MCP server over stdio subprocess transport.

    Spawns the configured command, performs the initialize handshake,
    discovers tools, and exposes ``call_tool()`` for synchronous invocation.

    Thread-safety: a ``threading.Lock`` serialises stdin writes so
    concurrent calls (e.g. from ThreadPoolExecutor) don't interleave
    JSON-RPC messages.
    """

    def __init__(self, config: "McpServerConfig"):
        self.config = config
        self.process: subprocess.Popen | None = None
        self._request_id: int = 0
        self._lock = threading.Lock()
        self._tools: dict[str, dict] = {}       # name → MCP tool schema
        self._connected: bool = False
        self._tools_changed: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Spawn the subprocess, perform initialize handshake, and discover tools.

        Returns True on success, False on any failure (server is left dead).
        """
        if self._connected:
            return True

        try:
            self._start_process()
            if not self._initialize():
                self.disconnect()
                return False
            self._tools = self.discover_tools()
            self._connected = True
            return True
        except Exception as exc:
            print(
                f"Warning: MCP server '{self.config.name}' failed to connect: {exc}",
                file=sys.stderr,
            )
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Terminate the subprocess gracefully, then forcefully.

        Acquires the I/O lock to prevent races with in-flight _send_request
        calls.  Closes stdin, then waits briefly; if the process doesn't
        exit, kills it.
        """
        self._connected = False
        self._tools.clear()
        proc = self.process
        if proc is None:
            return
        self.process = None
        with self._lock:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except OSError:
                pass  # already dead

    def reconnect(self) -> bool:
        """Disconnect then connect.  Returns True if reconnect succeeds."""
        self.disconnect()
        return self.connect()

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def _start_process(self) -> None:
        """Launch the MCP server subprocess."""
        if not self.config.command:
            raise McpConnectionError(
                f"MCP server '{self.config.name}' has no command configured"
            )
        cmd = [self.config.command] + list(self.config.args)
        env = None
        if self.config.env:
            env = {**__import__("os").environ, **self.config.env}
        cwd = self.config.cwd or None

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            text=True,
        )
        # Drain stderr in a daemon thread to prevent pipe-buffer deadlock
        threading.Thread(
            target=self._drain_stderr, daemon=True, name=f"mcp-stderr-{self.config.name}"
        ).start()

    def _drain_stderr(self) -> None:
        """Read and discard stderr lines to prevent pipe buffer from filling."""
        try:
            if self.process and self.process.stderr:
                for _line in self.process.stderr:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC 2.0 request and return the result.

        Reads stdout lines until a response with matching id arrives.
        Notifications (responses without id or with different id) are
        processed via ``_handle_notification`` and then discarded.
        Raises ``McpRpcError`` on JSON-RPC error response.
        Raises ``McpConnectionError`` when stdout closes unexpectedly.
        """
        if self.process is None or self.process.stdin is None:
            raise McpConnectionError(
                f"MCP server '{self.config.name}' is not connected"
            )

        with self._lock:
            self._request_id += 1
            rid = self._request_id
            request = {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                "params": params or {},
            }
            line = json.dumps(request, ensure_ascii=False) + "\n"

            try:
                self.process.stdin.write(line)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._connected = False
                raise McpConnectionError(
                    f"MCP server '{self.config.name}' disconnected during write: {exc}"
                ) from exc

            # Read until we get the matching response
            while True:
                raw = self.process.stdout.readline()
                if not raw:
                    self._connected = False
                    raise McpConnectionError(
                        f"MCP server '{self.config.name}' closed stdout unexpectedly"
                    )

                try:
                    response = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # Skip junk lines
                    continue

                resp_id = response.get("id")
                if resp_id == rid:
                    if "error" in response:
                        raise McpRpcError(response["error"])
                    return response.get("result", {})
                elif resp_id is None and "method" in response:
                    # Notification — process it
                    self._handle_notification(response)
                # else: response for a different concurrent request —
                # handled in a future version with per-request queues.

    # ------------------------------------------------------------------
    # MCP protocol methods
    # ------------------------------------------------------------------

    def _initialize(self) -> bool:
        """Send the ``initialize`` request with client capabilities."""
        try:
            result = self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {
                        "name": "mini_agent",
                        "version": "2.0.0",
                    },
                },
            )
            # Send initialized notification (spec requirement)
            self._send_notification("notifications/initialized", {})
            return True
        except (McpRpcError, McpConnectionError) as exc:
            print(
                f"Warning: MCP server '{self.config.name}' initialize failed: {exc}",
                file=sys.stderr,
            )
            return False

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no id field)."""
        if self.process is None or self.process.stdin is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        with self._lock:
            try:
                line = json.dumps(notification, ensure_ascii=False) + "\n"
                self.process.stdin.write(line)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._connected = False

    def _handle_notification(self, msg: dict) -> None:
        """Process a notification from the server."""
        method = msg.get("method", "")
        if method == "notifications/tools/list_changed":
            self._tools_changed = True

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def discover_tools(self) -> dict[str, dict]:
        """Send ``tools/list``, paginate if needed, return ``{name: schema}``."""
        tools: dict[str, dict] = {}
        cursor: str | None = None

        while True:
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            try:
                result = self._send_request("tools/list", params)
            except (McpRpcError, McpConnectionError):
                break

            for tool_def in result.get("tools", []):
                name = tool_def.get("name", "")
                if name:
                    tools[name] = {
                        "description": tool_def.get("description", ""),
                        "inputSchema": tool_def.get("inputSchema", {}),
                    }

            cursor = result.get("nextCursor")
            if not cursor:
                break

        return tools

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """Invoke an MCP tool via ``tools/call``.

        On connection failure, attempts one reconnect before giving up.
        """
        if not self.is_connected:
            return ToolResult(
                success=False,
                content=f"MCP server '{self.config.name}' not connected.",
                hint=f"No process running for server '{self.config.name}'.",
            )
        try:
            result = self._send_request(
                "tools/call", {"name": name, "arguments": arguments}
            )
            return _result_to_tool_result(result, self.config.name, name)
        except McpRpcError as exc:
            return ToolResult(
                success=False,
                content=f"MCP tool '{name}' returned error: {exc.message}",
                hint=_build_mcp_hint(self.config.name, name, exc.message),
            )
        except (BrokenPipeError, ConnectionError, OSError, McpConnectionError) as exc:
            # Attempt one reconnect, then retry
            if self.reconnect():
                try:
                    result = self._send_request(
                        "tools/call", {"name": name, "arguments": arguments}
                    )
                    return _result_to_tool_result(result, self.config.name, name)
                except Exception as exc2:
                    return ToolResult(
                        success=False,
                        content=(
                            f"MCP server '{self.config.name}' unreachable "
                            f"after reconnect: {exc2}"
                        ),
                        hint=_build_mcp_hint(self.config.name, name, str(exc2)),
                    )
            return ToolResult(
                success=False,
                content=f"MCP server '{self.config.name}' disconnected: {exc}",
                hint=_build_mcp_hint(self.config.name, name, str(exc)),
            )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tool_schemas(self) -> dict[str, dict]:
        return dict(self._tools)


# ---------------------------------------------------------------------------
# McpClientManager — orchestrates all MCP connections
# ---------------------------------------------------------------------------

class McpClientManager:
    """Creates ``McpConnection`` instances from config, starts them all,
    registers their tools into the global dispatch table, and handles
    reconnection orchestration.
    """

    def __init__(self, servers: list["McpServerConfig"]):
        self._connections: dict[str, McpConnection] = {}
        for cfg in servers:
            if cfg.enabled:
                if not cfg.command:
                    print(
                        f"Warning: MCP server '{cfg.name}' has no command — skipping",
                        file=sys.stderr,
                    )
                    continue
                self._connections[cfg.name] = McpConnection(cfg)

    def start_all(self) -> list[str]:
        """Connect to all servers, discover tools, register them.

        Returns list of server names that connected successfully.
        """
        connected: list[str] = []
        for name, conn in self._connections.items():
            if conn.connect():
                connected.append(name)
                self._register_server_tools(name, conn)
        return connected

    def shutdown_all(self) -> None:
        """Disconnect all servers gracefully."""
        for conn in self._connections.values():
            conn.disconnect()

    def call_mcp_tool(self, full_name: str, arguments: dict) -> ToolResult:
        """Parse ``mcp/<server>/<tool>`` and dispatch to the right connection."""
        server_name, tool_name = _parse_full_name(full_name)
        conn = self._connections.get(server_name)
        if conn is None:
            known = sorted(self._connections.keys())
            return ToolResult(
                success=False,
                content=f"Unknown MCP server: {server_name}",
                hint=(
                    f"Server '{server_name}' not found. "
                    f"Available MCP servers: {', '.join(known)}. "
                    f"Please use an active server name."
                ),
            )
        if not conn.is_connected:
            return ToolResult(
                success=False,
                content=f"MCP server '{server_name}' is not connected",
                hint=(
                    f"MCP server '{server_name}' is down. "
                    f"The server may need to be restarted."
                ),
            )
        return conn.call_tool(tool_name, arguments)

    def refresh_tools_if_changed(self) -> None:
        """Check for ``tools/list_changed`` notifications and re-register."""
        for name, conn in self._connections.items():
            if conn._tools_changed:
                conn._tools_changed = False
                if conn.is_connected:
                    conn._tools = conn.discover_tools()
                    self._register_server_tools(name, conn)

    def _register_server_tools(self, server_name: str, conn: McpConnection) -> None:
        """Register one server's discovered tools into the global dispatch
        table and TOOLS schema list.

        Removes any previously-registered entries for this server first
        to prevent duplicates on re-registration.
        """
        from tools.schema import TOOLS

        # 0. Remove old registrations for this server
        prefix = f"mcp/{server_name}/"
        TOOLS[:] = [td for td in TOOLS if not td["function"]["name"].startswith(prefix)]
        for key in list(_TOOL_DISPATCH.keys()):
            if key.startswith(prefix):
                del _TOOL_DISPATCH[key]
        for key in list(_TOOL_SUMMARIES.keys()):
            if key.startswith(prefix):
                del _TOOL_SUMMARIES[key]

        for tool_name, tool_schema in conn.tool_schemas.items():
            full_name = f"mcp/{server_name}/{tool_name}"

            # Detect collision with a different server or duplicate registration.
            # Warn and skip so we don't silently shadow another server's tool.
            existing_dispatch = _TOOL_DISPATCH.get(full_name)
            if existing_dispatch is not None:
                import sys
                print(
                    f"Warning: MCP tool '{full_name}' already registered — "
                    f"skipping duplicate registration",
                    file=sys.stderr,
                )
                continue

            # 1. Add to TOOLS schema list
            TOOLS.append({
                "type": "function",
                "function": {
                    "name": full_name,
                    "description": tool_schema.get("description", ""),
                    "parameters": convert_mcp_input_schema(
                        tool_schema.get("inputSchema")
                    ),
                },
            })

            # 2. Register dispatch handler
            _TOOL_DISPATCH[full_name] = _make_mcp_dispatcher(self, full_name)

            # 3. Register summary
            _TOOL_SUMMARIES[full_name] = _make_mcp_summary(server_name, tool_name)

    def registered_tools(self) -> list[str]:
        """Return all fully-qualified MCP tool names currently registered."""
        out: list[str] = []
        for name, conn in self._connections.items():
            for tname in conn.tool_schemas:
                out.append(f"mcp/{name}/{tname}")
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_tool_result(
    mcp_result: dict, server_name: str, tool_name: str
) -> ToolResult:
    """Convert an MCP tools/call result dict to a ToolResult."""
    content_blocks = mcp_result.get("content", [])
    is_error = mcp_result.get("isError", False)

    if not content_blocks:
        return ToolResult(
            success=not is_error,
            content="(empty result)",
        )

    # Join text blocks with newlines; skip non-text blocks
    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "resource":
            parts.append(f"[resource: {block.get('resource', {})}]")

    content = "\n".join(parts) if parts else "(no text content)"
    hint = ""
    if is_error:
        hint = _build_mcp_hint(server_name, tool_name, content)

    return ToolResult(success=not is_error, content=content, hint=hint)


def _build_mcp_hint(server_name: str, tool_name: str, error: str) -> str:
    """Build an actionable hint for the LLM when an MCP tool fails."""
    return (
        f"MCP tool 'mcp/{server_name}/{tool_name}' failed: {error}\n"
        f"The tool is provided by an external MCP server. "
        f"Check that the server is running and the tool name is correct."
    )


def _parse_full_name(full_name: str) -> tuple[str, str]:
    """Parse ``mcp/<server>/<tool>`` into (server, tool).

    Raises ValueError on malformed names.
    """
    # Expected: mcp/<server>/<tool>
    parts = full_name.split("/")
    if len(parts) < 3 or parts[0] != "mcp":
        raise ValueError(
            f"Invalid MCP tool name '{full_name}'. "
            f"Expected format: mcp/<server>/<tool>"
        )
    server = parts[1]
    tool = "/".join(parts[2:])  # tool name may contain slashes
    if not server or not tool:
        raise ValueError(
            f"Invalid MCP tool name '{full_name}': server and tool names must be non-empty"
        )
    return server, tool


def _make_mcp_dispatcher(manager: McpClientManager, full_name: str):
    """Return a callable matching the native tool dispatch signature.

    Signature: ``fn(args: dict, write_gate, read_gate) -> ToolResult``
    """

    def _dispatch(args: dict, _write_gate, _read_gate) -> ToolResult:
        return manager.call_mcp_tool(full_name, args)

    return _dispatch


def _make_mcp_summary(server_name: str, tool_name: str):
    """Return a callable that produces a compact summary of an MCP tool call."""

    def _summary(args: dict) -> str:
        if args:
            first_key = next(iter(args))
            first_val = str(args[first_key])
            if len(first_val) > 40:
                first_val = first_val[:37] + "..."
            extra = f", +{len(args) - 1}" if len(args) > 1 else ""
            return f"mcp/{server_name}/{tool_name}({first_key}={first_val}{extra})"
        return f"mcp/{server_name}/{tool_name}()"

    return _summary


# ---------------------------------------------------------------------------
# Helpers (continued)
# ---------------------------------------------------------------------------


# ---- Registered MCP orchestration tools ----

from tools import _register, _summarize, _TOOL_CONTEXT, ToolResult


@_register("mcp_discover")
@_summarize("mcp_discover")
def _mcp_discover(args: dict, _write_gate, _read_gate) -> "ToolResult":
    """List all MCP tools discovered from connected servers."""
    manager = getattr(_TOOL_CONTEXT, "_mcp_manager", None)
    if manager is None:
        return ToolResult(success=False, content="No MCP manager configured.")
    tools_list = []
    for name, conn in manager._connections.items():
        state = "connected" if conn.is_connected else "disconnected"
        for tool in conn._tools:
            tools_list.append(
                f"  {tool.get('name','?')} [{name}/{state}] "
                f"- {tool.get('description','')[:120]}"
            )
    if not tools_list:
        return ToolResult(
            success=True,
            content="No MCP tools discovered. Configure MCP servers in .mini_agent.toml [agent.mcp_servers].",
        )
    return ToolResult(success=True, content="MCP tools:\n" + "\n".join(tools_list))


@_register("mcp_call")
@_summarize("mcp_call")
def _mcp_call(args: dict, _write_gate, _read_gate) -> "ToolResult":
    """Call an MCP tool on a connected server.

    Args:
        server: MCP server name
        tool: tool name on that server
        arguments: dict of arguments to pass
    """
    manager = getattr(_TOOL_CONTEXT, "_mcp_manager", None)
    if manager is None:
        return ToolResult(success=False, content="No MCP manager configured.")
    server = args.get("server", "")
    tool = args.get("tool", "")
    arguments = args.get("arguments", {})
    if not server or not tool:
        return ToolResult(
            success=False,
            content="Both 'server' and 'tool' are required. Use mcp_discover to find available tools.",
        )
    full_name = f"mcp/{server}/{tool}"
    return manager.call_mcp_tool(full_name, arguments)
