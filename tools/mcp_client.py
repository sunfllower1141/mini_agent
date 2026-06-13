#!/usr/bin/env python3
"""
mcp_client.py -- Lightweight MCP (Model Context Protocol) client over stdio.

Connects to MCP servers via stdio JSON-RPC (newline-delimited),
discovers their tools, and exposes mcp_discover + mcp_call to the LLM.

Uses the same subprocess + drain_stderr pattern as tools/lsp.py.
No dependency on the `mcp` SDK -- raw JSON-RPC only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

from tools import ToolResult
from tools._json_rpc_shared import drain_stderr


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class McpError(Exception):
    """A JSON-RPC error returned by an MCP server."""
    def __init__(self, error: dict):
        self.code = error.get("code", -1)
        self.message = error.get("message", "unknown")
        super().__init__(f"MCP RPC error {self.code}: {self.message}")


class McpConnectionError(Exception):
    """Connection to an MCP server was lost or could not be established."""


# ---------------------------------------------------------------------------
# McpConnection -- one per server
# ---------------------------------------------------------------------------

class McpConnection:
    """Manages one MCP server over stdio JSON-RPC transport.

    MCP uses newline-delimited JSON (one JSON object per line), NOT
    Content-Length headers like LSP.  This makes the transport simpler.

    Protocol handshake (per spec):
        1. Client sends ``initialize`` request
        2. Server responds with capabilities (including tools)
        3. Client sends ``initialized`` notification
    """

    def __init__(self, name: str, command: list[str], env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.env = env
        self.process: subprocess.Popen | None = None
        self._request_id: int = 0
        self._lock = threading.Lock()
        self._connected: bool = False
        # Server capabilities from initialize response
        self._server_info: dict = {}
        # Cached tool list from tools/list
        self._tools: list[dict] = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Spawn the subprocess and perform MCP initialize handshake.

        Returns True on success, False on any failure.
        """
        if self._connected:
            return True
        try:
            self._start_process()
            if not self._initialize():
                self.disconnect()
                return False
            self._connected = True
            # Discover tools immediately
            self._discover_tools()
            return True
        except Exception:
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Terminate the subprocess gracefully, then forcefully."""
        self._connected = False
        self._tools.clear()
        self._server_info.clear()
        proc = self.process
        if proc is None:
            return
        self.process = None
        # Try graceful shutdown
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
                pass

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def _start_process(self) -> None:
        """Launch the MCP server subprocess."""
        merged_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if self.env:
            merged_env.update(self.env)

        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW

        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=creationflags or 0,
            env=merged_env,
        )
        drain_stderr(self.process, f"mcp-stderr-{self.name}")

    def _read_line(self, timeout: float = 15.0) -> bytes:
        """Read one line from stdout, with timeout."""
        if self.process is None or self.process.stdout is None:
            raise McpConnectionError(
                f"MCP server '{self.name}' is not connected"
            )

        # Use threading for timeout on readline
        result: list[bytes | None] = [None]
        exc: list[Exception | None] = [None]

        def _read():
            try:
                result[0] = self.process.stdout.readline()
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            self._connected = False
            raise McpConnectionError(
                f"MCP server '{self.name}' timed out waiting for response"
            )
        if exc[0] is not None:
            self._connected = False
            raise McpConnectionError(
                f"MCP server '{self.name}' read error: {exc[0]}"
            )
        raw = result[0]
        if not raw:
            self._connected = False
            raise McpConnectionError(
                f"MCP server '{self.name}' closed stdout unexpectedly"
            )
        return raw

    # ------------------------------------------------------------------
    # JSON-RPC (newline-delimited)
    # ------------------------------------------------------------------

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC 2.0 request and return the result.

        Reads stdout lines until a response with matching id arrives.
        Ignores notifications (messages without an id).
        """
        if self.process is None or self.process.stdin is None:
            raise McpConnectionError(
                f"MCP server '{self.name}' is not connected"
            )

        with self._lock:
            self._request_id += 1
            rid = self._request_id
            request: dict = {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                "params": params or {},
            }
            body = json.dumps(request) + "\n"
            try:
                self.process.stdin.write(body.encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._connected = False
                raise McpConnectionError(
                    f"MCP server '{self.name}' disconnected during write: {exc}"
                ) from exc

            while True:
                raw_line = self._read_line(15.0)
                try:
                    response = json.loads(raw_line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                resp_id = response.get("id")
                if resp_id == rid:
                    if "error" in response:
                        raise McpError(response["error"])
                    return response.get("result", {})
                elif resp_id is None and "method" in response:
                    # Notification -- ignore for now
                    pass
                # else: response for a different concurrent request -- ignore

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no id field)."""
        if self.process is None or self.process.stdin is None:
            return
        notification: dict = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        with self._lock:
            try:
                body = json.dumps(notification) + "\n"
                self.process.stdin.write(body.encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._connected = False

    # ------------------------------------------------------------------
    # MCP protocol methods
    # ------------------------------------------------------------------

    def _initialize(self) -> bool:
        """Send ``initialize``, check response, send ``initialized``."""
        try:
            result = self._send_request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "mini_agent",
                        "version": "1.0",
                    },
                },
            )
            self._server_info = result
            self._send_notification("initialized", {})
            return True
        except (McpError, McpConnectionError):
            return False

    def _discover_tools(self) -> None:
        """Call ``tools/list`` and cache the result."""
        try:
            result = self._send_request("tools/list", {})
            self._tools = result.get("tools", [])
        except (McpError, McpConnectionError):
            self._tools = []

    @property
    def tools(self) -> list[dict]:
        """Return cached tool list."""
        return self._tools

    @property
    def is_connected(self) -> bool:
        return self._connected

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        """Call a tool on the server via ``tools/call``."""
        try:
            result = self._send_request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": arguments or {},
                },
            )
            # MCP tools/call returns {"content": [...]} where each item
            # has {"type": "text", "text": "..."} or similar.
            content = result.get("content", [])
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif isinstance(item, dict):
                        text_parts.append(json.dumps(item))
                    elif isinstance(item, str):
                        text_parts.append(item)
                return ToolResult(success=True, content="\n".join(text_parts))
            elif isinstance(content, str):
                return ToolResult(success=True, content=content)
            else:
                # Fallback: return the whole result as JSON
                return ToolResult(
                    success=True,
                    content=json.dumps(result, indent=2),
                )
        except McpError as exc:
            return ToolResult(
                success=False,
                content=f"MCP tool '{tool_name}' failed: {exc.message}",
                hint=f"MCP server '{self.name}' returned error code {exc.code}.",
            )
        except McpConnectionError as exc:
            self._connected = False
            return ToolResult(
                success=False,
                content=f"MCP server '{self.name}' disconnected: {exc}",
            )


# ---------------------------------------------------------------------------
# McpClientManager -- orchestrates all MCP connections
# ---------------------------------------------------------------------------

class McpClientManager:
    """Manages multiple MCP server connections.

    Lazy-connects: servers are only started on first use (like LSP).
    Provides discover() to list tools across all servers and call()
    to invoke a specific tool on a specific server.
    """

    def __init__(self, server_configs: dict[str, dict] | None = None):
        """*server_configs* maps server name -> {command: [...], env: {...}}."""
        self._configs: dict[str, dict] = server_configs or {}
        self._connections: dict[str, McpConnection] = {}
        self._started: bool = False

    def configure(self, server_configs: dict[str, dict]) -> None:
        """Add or replace server configurations."""
        self._configs.update(server_configs)

    def _ensure_started(self) -> None:
        """Connect to all configured servers that aren't already connected."""
        for name, cfg in self._configs.items():
            if name in self._connections and self._connections[name].is_connected:
                continue
            cmd = cfg.get("command", [])
            if not cmd:
                continue
            env = cfg.get("env", {})
            conn = McpConnection(name=name, command=cmd, env=env)
            if conn.connect():
                self._connections[name] = conn
            else:
                # Store the failed connection so we don't retry endlessly
                self._connections[name] = conn
        self._started = True

    def discover(self) -> ToolResult:
        """Return a listing of all tools across all connected MCP servers.

        Connects to any unconnected servers first.
        """
        self._ensure_started()

        lines: list[str] = []
        total_tools = 0

        for name, conn in sorted(self._connections.items()):
            if conn.is_connected:
                tools = conn.tools
                total_tools += len(tools)
                lines.append(f"\n[{name}] -- {len(tools)} tools:")
                for tool in tools:
                    tname = tool.get("name", "?")
                    tdesc = tool.get("description", "")
                    # Truncate description for listing
                    if len(tdesc) > 120:
                        tdesc = tdesc[:117] + "..."
                    lines.append(f"  * {tname}: {tdesc}")
                if not tools:
                    lines.append("  (no tools exposed)")
            else:
                lines.append(f"\n[{name}] -- DISCONNECTED")

        if not lines:
            return ToolResult(
                success=True,
                content="No MCP servers configured. Add [agent.mcp_servers.<name>] to .mini_agent.toml.",
            )

        header = f"{total_tools} tools across {len(self._connections)} server(s):"
        return ToolResult(success=True, content=header + "\n" + "\n".join(lines))

    def call(self, server: str, tool: str, arguments: dict | None = None) -> ToolResult:
        """Call a tool on a specific MCP server.

        Connects to the server if not already connected.
        """
        self._ensure_started()

        conn = self._connections.get(server)
        if conn is None:
            # Try to start this server if configured
            cfg = self._configs.get(server)
            if cfg and cfg.get("command"):
                conn = McpConnection(
                    name=server,
                    command=cfg["command"],
                    env=cfg.get("env", {}),
                )
                if conn.connect():
                    self._connections[server] = conn
                else:
                    return ToolResult(
                        success=False,
                        content=f"MCP server '{server}' failed to connect.",
                        hint="Check the server command and ensure it's installed.",
                    )
            else:
                return ToolResult(
                    success=False,
                    content=f"Unknown MCP server: '{server}'.",
                    hint=f"Available servers: {list(self._configs.keys())}. "
                         f"Configure in .mini_agent.toml [agent.mcp_servers.{server}].",
                )

        if not conn.is_connected:
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' is not connected.",
                hint="The server may have crashed. Try restarting the session.",
            )

        # Check the tool exists
        tool_names = [t.get("name") for t in conn.tools]
        if tool not in tool_names:
            return ToolResult(
                success=False,
                content=f"MCP server '{server}' has no tool '{tool}'.",
                hint=f"Available tools on '{server}': {tool_names}. "
                     f"Use mcp_discover to see all tools.",
            )

        return conn.call_tool(tool, arguments)

    def shutdown(self) -> None:
        """Disconnect all servers."""
        for conn in self._connections.values():
            conn.disconnect()
        self._connections.clear()
        self._started = False


# ---------------------------------------------------------------------------
# Module-level singleton (lazy init)
# ---------------------------------------------------------------------------

_mcp_manager: McpClientManager | None = None


def get_mcp_manager() -> McpClientManager:
    """Return the module-level MCP client manager, creating it if needed."""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = McpClientManager()
    return _mcp_manager


def init_mcp_servers(server_configs: dict[str, dict]) -> None:
    """Configure MCP servers from AgentConfig.mcp_servers.

    Called once at session startup by init_session().
    """
    manager = get_mcp_manager()
    manager.configure(server_configs)


def shutdown_mcp() -> None:
    """Shutdown all MCP server connections."""
    global _mcp_manager
    if _mcp_manager is not None:
        _mcp_manager.shutdown()
        _mcp_manager = None
