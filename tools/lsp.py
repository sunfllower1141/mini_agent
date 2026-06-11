#!/usr/bin/env python3
"""
lsp.py — Lightweight LSP (Language Server Protocol) client over stdio.

Manages stdio subprocess connections to language servers (pylsp for Python),
sends initialize/initialized, and exposes textDocument/definition,
textDocument/references, textDocument/hover, and collects published diagnostics.

All I/O is synchronous (thread-based) to match mini_agent's existing
synchronous tool dispatch.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import json
import queue
from pathlib import Path

from tools import ToolResult, _register, _summarize
from tools._json_rpc_shared import drain_stderr

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class LspRpcError(Exception):
    """A JSON-RPC error returned by the LSP server."""
    def __init__(self, error: dict):
        self.code = error.get("code", -1)
        self.message = error.get("message", "unknown")
        super().__init__(f"LSP RPC error {self.code}: {self.message}")


class LspConnectionError(Exception):
    """Connection to the LSP server was lost or could not be established."""


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

# Map file extension → (language_id, server_command, server_args)
_LANGUAGE_CONFIG: dict[str, tuple[str, str, list[str]]] = {
    ".py": ("python", "pylsp", []),
    ".pyi": ("python", "pylsp", []),
    ".pyx": ("python", "pylsp", []),
    ".js": ("javascript", "typescript-language-server", ["--stdio"]),
    ".jsx": ("javascriptreact", "typescript-language-server", ["--stdio"]),
    ".ts": ("typescript", "typescript-language-server", ["--stdio"]),
    ".tsx": ("typescriptreact", "typescript-language-server", ["--stdio"]),
    ".mjs": ("javascript", "typescript-language-server", ["--stdio"]),
    ".cjs": ("javascript", "typescript-language-server", ["--stdio"]),
}


def detect_language(file_path: str) -> tuple[str, str, list[str]] | None:
    """Detect language from file extension.

    Returns (language_id, server_command, server_args) or None
    if the file extension is not supported.
    """
    ext = Path(file_path).suffix.lower()
    return _LANGUAGE_CONFIG.get(ext)


def _uri_from_path(file_path: str) -> str:
    """Convert a file path to a URI."""
    abs_path = os.path.abspath(file_path)
    return Path(abs_path).as_uri()


# ---------------------------------------------------------------------------
# LspConnection — one per language server
# ---------------------------------------------------------------------------

class LspConnection:
    """Manages one LSP server over stdio subprocess transport.

    Spawns the configured command, performs the initialize handshake,
    and exposes ``query()`` for synchronous LSP method invocation.
    Diagnostics are collected from ``textDocument/publishDiagnostics``
    notifications and cached per-URI.

    Thread safety: a ``threading.Lock`` serialises stdin writes.

    On Windows, ``select.select`` cannot be used with pipes, so a
    background reader thread feeds a ``queue.Queue``.  On Unix,
    ``select.select`` is used directly for efficiency.
    """

    def __init__(self, language_id: str, server_command: str,
                 server_args: list[str] | None = None):
        self.language_id = language_id
        self.server_command = server_command
        self.server_args = server_args or []
        self.process: subprocess.Popen | None = None
        self._request_id: int = 0
        self._lock = threading.Lock()
        self._connected: bool = False
        # Cache diagnostics per URI
        self._diagnostics: dict[str, list[dict]] = {}
        # Windows stdout reader (None on Unix, threading.Thread on Windows)
        self._stdout_queue: "queue.Queue | None" = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, root_uri: str | None = None) -> bool:
        """Spawn the subprocess, perform initialize handshake.

        Returns True on success, False on any failure.
        """
        if self._connected:
            return True
        try:
            self._start_process()
            if not self._initialize(root_uri):
                self.disconnect()
                return False
            self._connected = True
            return True
        except Exception:
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Terminate the subprocess gracefully, then forcefully."""
        self._connected = False
        self._diagnostics.clear()
        proc = self.process
        if proc is None:
            return
        self.process = None
        # Send shutdown request, then exit notification
        with self._lock:
            try:
                if proc.stdin:
                    shutdown = json.dumps({
                        "jsonrpc": "2.0", "id": self._request_id + 1,
                        "method": "shutdown", "params": {},
                    }) + "\n"
                    proc.stdin.write(shutdown.encode("utf-8"))
                    proc.stdin.flush()
                    exit_notif = json.dumps({
                        "jsonrpc": "2.0",
                        "method": "exit", "params": {},
                    }) + "\n"
                    proc.stdin.write(exit_notif.encode("utf-8"))
                    proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        with self._lock:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                # Kill entire process group — critical for Node-based
                # servers (typescript-language-server) that spawn child
                # processes like tsserver.  proc.kill() alone leaves
                # orphans that accumulate and cause system thrashing.
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    def _os_readline(self, timeout: float = 15.0) -> bytes:
        """Read one line from stdout, platform-aware.

        On Unix, blocks with ``select.select`` + ``readline``.
        On Windows, reads from the background queue (``select`` doesn't
        work with pipes on Windows).
        """
        if self._stdout_queue is not None:
            # Windows — background thread feeds the queue
            try:
                data = self._stdout_queue.get(timeout=timeout)
                if data is None:
                    self._connected = False
                    raise LspConnectionError(
                        f"LSP server '{self.language_id}' closed stdout unexpectedly"
                    )
                return data
            except queue.Empty:
                self._connected = False
                raise LspConnectionError(
                    f"LSP server '{self.language_id}' timed out waiting for response"
                )
        else:
            # Unix — select + readline
            import select
            ready, _, _ = select.select([self.process.stdout], [], [], timeout)
            if not ready:
                self._connected = False
                raise LspConnectionError(
                    f"LSP server '{self.language_id}' timed out waiting for response"
                )
            raw = self.process.stdout.readline()
            if not raw:
                self._connected = False
                raise LspConnectionError(
                    f"LSP server '{self.language_id}' closed stdout unexpectedly"
                )
            return raw

    def _stdout_reader_thread(self) -> None:
        """Background thread: read stdout lines into _stdout_queue (Windows)."""
        try:
            for raw_line in iter(self.process.stdout.readline, b''):
                self._stdout_queue.put(raw_line)
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            self._stdout_queue.put(None)  # EOF sentinel

    def _start_process(self) -> None:
        """Launch the LSP server subprocess."""
        cmd = [self.server_command] + list(self.server_args)
        creationflags = 0
        if os.name == 'nt':
            # Prevent conhost.exe spam on Windows
            creationflags = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            creationflags=creationflags or 0,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        # On Windows, start a background reader thread since select doesn't work on pipes
        if os.name == 'nt':
            self._stdout_queue = queue.Queue()
            threading.Thread(
                target=self._stdout_reader_thread, daemon=True,
                name=f"lsp-stdout-{self.language_id}"
            ).start()
        drain_stderr(self.process, f"lsp-stderr-{self.language_id}")

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC 2.0 request and return the result.

        Reads stdout lines until a response with matching id arrives.
        Processes notifications via ``_handle_notification``.
        """
        if self.process is None or self.process.stdin is None:
            raise LspConnectionError(
                f"LSP server '{self.language_id}' is not connected"
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
            body = json.dumps(request)
            header = f"Content-Length: {len(body)}\r\n\r\n"
            try:
                self.process.stdin.write((header + body).encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._connected = False
                raise LspConnectionError(
                    f"LSP server '{self.language_id}' disconnected during write: {exc}"
                ) from exc

            while True:
                raw_line = self._os_readline(15.0)
                raw = raw_line.decode("utf-8")
                if raw.startswith("Content-Length:"):
                    clen = int(raw.split(":")[1].strip())
                    # Skip all header lines until blank \r\n separator
                    while True:
                        header_line = self._os_readline(5.0)
                        if header_line == b"\r\n" or header_line == b"\n":
                            break
                    # Read exactly clen bytes
                    raw_bytes = b""
                    while len(raw_bytes) < clen:
                        # Use _os_readline to get remaining bytes if on Windows queue,
                        # or read directly from stdout on Unix
                        if self._stdout_queue is not None:
                            chunk = self._os_readline(5.0)
                            raw_bytes += chunk
                        else:
                            chunk = self.process.stdout.read(clen - len(raw_bytes))
                            if not chunk:
                                break
                            raw_bytes += chunk
                    raw = raw_bytes.decode("utf-8")
                if not raw:
                    self._connected = False
                    raise LspConnectionError(
                        f"LSP server '{self.language_id}' closed stdout unexpectedly"
                    )
                try:
                    response = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                resp_id = response.get("id")
                if resp_id == rid:
                    if "error" in response:
                        raise LspRpcError(response["error"])
                    return response.get("result", {})
                elif resp_id is None and "method" in response:
                    self._handle_notification(response)
                # else: response for a different concurrent request — ignore

    def _drain_notifications(self) -> None:
        """Read any pending server notifications from stdout.

        After sending a notification (e.g. ``textDocument/didOpen``), the
        server may respond with its own notifications (e.g.
        ``textDocument/publishDiagnostics``).  This drains them so they
        are cached before the next tool call checks state.

        Reads Content-Length framed messages until stdout has no more data
        immediately available (polled with a 0.05 s per-message timeout).
        """
        if self.process is None or self.process.stdout is None:
            return
        while True:
            try:
                raw_line = self._os_readline(0.05)
            except LspConnectionError:
                break  # timeout or EOF — no more notifications
            raw = raw_line.decode("utf-8")
            if not raw:
                self._connected = False
                break
            if raw.startswith("Content-Length:"):
                clen = int(raw.split(":")[1].strip())
                self.process.stdout.readline()  # skip empty (bytes, discard) \r\n line
                raw_bytes = b""
                while len(raw_bytes) < clen:
                    chunk = self.process.stdout.read(clen - len(raw_bytes))
                    if not chunk:
                        break
                    raw_bytes += chunk
                raw = raw_bytes.decode("utf-8")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") is None and "method" in msg:
                self._handle_notification(msg)

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no id field).

        After writing, drains stdout for any server-initiated notifications
        (e.g. ``textDocument/publishDiagnostics``) so they are available
        immediately for subsequent calls like ``get_diagnostics``.
        """
        if self.process is None or self.process.stdin is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        with self._lock:
            try:
                body = json.dumps(notification)
                header = f"Content-Length: {len(body)}\r\n\r\n"
                self.process.stdin.write((header + body).encode("utf-8"))
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._connected = False
                return
            # Drain any notification responses (e.g. publishDiagnostics)
            self._drain_notifications()

    def _handle_notification(self, msg: dict) -> None:
        """Process a notification from the server."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self._diagnostics[uri] = diagnostics

    # ------------------------------------------------------------------
    # LSP protocol methods
    # ------------------------------------------------------------------

    def _initialize(self, root_uri: str | None = None) -> bool:
        """Send the ``initialize`` request with client capabilities."""
        try:
            result = self._send_request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root_uri,
                    "capabilities": {
                        "textDocument": {
                            "definition": {"dynamicRegistration": False},
                            "references": {"dynamicRegistration": False},
                            "hover": {
                                "dynamicRegistration": False,
                                "contentFormat": ["markdown", "plaintext"],
                            },
                        },
                    },
                    "workspaceFolders": (
                        [{"uri": root_uri, "name": "workspace"}] if root_uri else None
                    ),
                },
            )
            self._send_notification("initialized", {})
            return True
        except (LspRpcError, LspConnectionError):
            return False

    def _ensure_document_open(self, uri: str) -> None:
        """Send ``textDocument/didOpen`` to make the server aware of a file.

        Reads the file contents if the file exists on disk; otherwise sends
        an empty string so the server can still respond with diagnostics.
        """
        try:
            with open(uri_to_path(uri), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, FileNotFoundError):
            text = ""
        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.language_id,
                    "version": 1,
                    "text": text,
                },
            },
        )

    def definition(self, file_path: str, line: int, character: int) -> ToolResult:
        """Get the definition location of a symbol at position."""
        uri = _uri_from_path(file_path)
        self._ensure_document_open(uri)
        try:
            result = self._send_request(
                "textDocument/definition",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                },
            )
            return _definition_to_tool_result(result, self.language_id)
        except LspRpcError as exc:
            return ToolResult(
                success=False,
                content=f"LSP definition query failed: {exc.message}",
                hint=f"Could not find definition at {file_path}:{line}:{character}.",
            )
        except LspConnectionError as exc:
            self._connected = False
            return ToolResult(
                success=False,
                content=f"LSP server disconnected: {exc}",
            )

    def references(self, file_path: str, line: int, character: int,
                   include_declaration: bool = True) -> ToolResult:
        """Find all references to the symbol at position."""
        uri = _uri_from_path(file_path)
        self._ensure_document_open(uri)
        try:
            result = self._send_request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                    "context": {"includeDeclaration": include_declaration},
                },
            )
            return _locations_to_tool_result(result, "references")
        except LspRpcError as exc:
            return ToolResult(
                success=False,
                content=f"LSP references query failed: {exc.message}",
                hint=f"Could not find references at {file_path}:{line}:{character}.",
            )
        except LspConnectionError as exc:
            self._connected = False
            return ToolResult(
                success=False,
                content=f"LSP server disconnected: {exc}",
            )

    def hover(self, file_path: str, line: int, character: int) -> ToolResult:
        """Get hover information for the symbol at position."""
        uri = _uri_from_path(file_path)
        self._ensure_document_open(uri)
        try:
            result = self._send_request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                },
            )
            if result is None:
                return ToolResult(
                    success=True,
                    content="(no hover information)",
                )
            contents = result.get("contents", {})
            if isinstance(contents, dict):
                text = contents.get("value", contents.get("kind", str(contents)))
            elif isinstance(contents, list):
                parts = []
                for item in contents:
                    if isinstance(item, dict):
                        parts.append(item.get("value", str(item)))
                    else:
                        parts.append(str(item))
                text = "\n".join(parts)
            else:
                text = str(contents) if contents else "(no hover information)"

            # Include range if present
            range_info = result.get("range")
            prefix = ""
            if range_info:
                start = range_info.get("start", {})
                prefix = (
                    f"Lines {start.get('line', '?')}:{start.get('character', '?')} "
                    f"— "
                )
            return ToolResult(success=True, content=f"{prefix}{text}")
        except LspRpcError as exc:
            return ToolResult(
                success=False,
                content=f"LSP hover query failed: {exc.message}",
                hint=f"Could not get hover info at {file_path}:{line}:{character}.",
            )
        except LspConnectionError as exc:
            self._connected = False
            return ToolResult(
                success=False,
                content=f"LSP server disconnected: {exc}",
            )

    def get_diagnostics(self, file_path: str) -> ToolResult:
        """Return cached diagnostics for a file.

        Diagnostics are collected from ``textDocument/publishDiagnostics``
        notifications sent by the server after a document is opened.
        """
        uri = _uri_from_path(file_path)
        self._ensure_document_open(uri)
        diagnostics = self._diagnostics.get(uri, [])

        if not diagnostics:
            return ToolResult(
                success=True,
                content=f"No diagnostics for {file_path}",
            )

        lines: list[str] = []
        for diag in diagnostics:
            start = diag.get("range", {}).get("start", {})
            line_num = start.get("line", "?")
            col = start.get("character", "?")
            severity = _severity_name(diag.get("severity", 0))
            message = diag.get("message", "")
            source = diag.get("source", "")
            code = diag.get("code", "")
            source_str = f"[{source}] " if source else ""
            code_str = f" ({code})" if code else ""
            lines.append(
                f"{severity}{source_str}{file_path}:{line_num}:{col}: "
                f"{message}{code_str}"
            )
        return ToolResult(success=True, content="\n".join(lines))

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# LspClientManager — orchestrates all LSP connections
# ---------------------------------------------------------------------------

class LspClientManager:
    """Manages LSP server connections per language.

    Lazily starts servers on first use. One connection per language_id.
    """

    def __init__(self):
        self._connections: dict[str, LspConnection] = {}
        self._root_uri: str | None = None

    def set_root(self, workspace_path: str) -> None:
        """Set the workspace root URI for initialize requests."""
        self._root_uri = Path(os.path.abspath(workspace_path)).as_uri()

    def get_connection(self, file_path: str) -> LspConnection | None:
        """Get or create an LSP connection for the language of the given file."""
        lang_info = detect_language(file_path)
        if lang_info is None:
            return None
        language_id, command, args = lang_info

        if language_id not in self._connections:
            conn = LspConnection(language_id, command, args)
            self._connections[language_id] = conn
            if not conn.connect(self._root_uri):
                return None
        else:
            conn = self._connections[language_id]
            if not conn.is_connected:
                # Clean up the old subprocess tree before reconnecting.
                # Without this, every reconnect leaks orphaned children
                # (e.g. tsserver processes spawned by typescript-language-server).
                conn.disconnect()
                if not conn.connect(self._root_uri):
                    return None
        return conn

    def shutdown_all(self) -> None:
        """Disconnect all language servers gracefully."""
        for conn in self._connections.values():
            conn.disconnect()
        self._connections.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a platform-native path."""
    from urllib.parse import urlparse, unquote
    parsed = urlparse(uri)
    return os.path.abspath(unquote(parsed.path))


def _severity_name(severity: int) -> str:
    """Convert LSP diagnostic severity number to a short label."""
    names = {1: "[ERROR] ", 2: "[WARN] ", 3: "[INFO] ", 4: "[HINT] "}
    return names.get(severity, "[?] ")


def _location_to_line(loc: dict) -> str:
    """Format a single LSP Location or LocationLink to a readable line."""
    if "targetUri" in loc:
        # LocationLink
        uri = loc.get("targetUri", "")
        range_info = loc.get("targetSelectionRange", loc.get("targetRange", {}))
    else:
        uri = loc.get("uri", "")
        range_info = loc.get("range", {})

    path = uri_to_path(uri)
    start = range_info.get("start", {})
    line = start.get("line", "?")
    col = start.get("character", "?")
    return f"{path}:{line}:{col}"


def _definition_to_tool_result(result: dict | list | None,
                                language_id: str) -> ToolResult:
    """Convert a textDocument/definition result to ToolResult."""
    if result is None:
        return ToolResult(success=True, content="(no definition found)")

    if isinstance(result, list):
        if not result:
            return ToolResult(success=True, content="(no definition found)")
        lines = [_location_to_line(loc) for loc in result]
        return ToolResult(success=True, content="\n".join(lines))
    elif isinstance(result, dict):
        return ToolResult(success=True, content=_location_to_line(result))

    return ToolResult(success=True, content=str(result))


def _locations_to_tool_result(locations: list | None, label: str) -> ToolResult:
    """Convert a list of LSP Locations to ToolResult."""
    if locations is None or not locations:
        return ToolResult(success=True, content=f"(no {label} found)")
    lines = [_location_to_line(loc) for loc in locations]
    return ToolResult(success=True, content="\n".join(lines))


# ---------------------------------------------------------------------------
# Global manager instance (set by init_session)
# ---------------------------------------------------------------------------

_LSP_MANAGER: LspClientManager | None = None


def get_lsp_manager() -> LspClientManager:
    """Return the global LSP client manager, creating it if needed."""
    global _LSP_MANAGER
    if _LSP_MANAGER is None:
        _LSP_MANAGER = LspClientManager()
    return _LSP_MANAGER


def set_lsp_root(workspace_path: str) -> None:
    """Set the workspace root on the global LSP manager."""
    mgr = get_lsp_manager()
    mgr.set_root(workspace_path)


def shutdown_lsp() -> None:
    """Shutdown all LSP connections."""
    global _LSP_MANAGER
    if _LSP_MANAGER is not None:
        _LSP_MANAGER.shutdown_all()
        _LSP_MANAGER = None


# ---------------------------------------------------------------------------
# Tool implementations — registered via @_register in __init__.py
# ---------------------------------------------------------------------------

@_register("lsp_definition")
def _lsp_definition(args: dict, _write_gate, _read_gate) -> ToolResult:
    """Go to definition via LSP."""
    file_path = args.get("file_path", "")
    line = args.get("line", 0)
    character = args.get("character", 0)

    if not file_path:
        return ToolResult(success=False, content="file_path is required")

    manager = get_lsp_manager()
    conn = manager.get_connection(file_path)
    if conn is None:
        return ToolResult(
            success=False,
            content=f"No LSP server available for file: {file_path}",
            hint="Supported: .py/.pyi/.pyx (pylsp), .js/.jsx/.ts/.tsx/.mjs/.cjs (typescript-language-server).",
        )
    return conn.definition(file_path, line, character)


@_register("lsp_references")
def _lsp_references(args: dict, _write_gate, _read_gate) -> ToolResult:
    """Find references via LSP."""
    file_path = args.get("file_path", "")
    line = args.get("line", 0)
    character = args.get("character", 0)
    include_declaration = args.get("include_declaration", True)

    if not file_path:
        return ToolResult(success=False, content="file_path is required")

    manager = get_lsp_manager()
    conn = manager.get_connection(file_path)
    if conn is None:
        return ToolResult(
            success=False,
            content=f"No LSP server available for file: {file_path}",
            hint="Supported: .py/.pyi/.pyx (pylsp), .js/.jsx/.ts/.tsx/.mjs/.cjs (typescript-language-server).",
        )
    return conn.references(file_path, line, character, include_declaration)


@_register("lsp_hover")
def _lsp_hover(args: dict, _write_gate, _read_gate) -> ToolResult:
    """Get hover information via LSP."""
    file_path = args.get("file_path", "")
    line = args.get("line", 0)
    character = args.get("character", 0)

    if not file_path:
        return ToolResult(success=False, content="file_path is required")

    manager = get_lsp_manager()
    conn = manager.get_connection(file_path)
    if conn is None:
        return ToolResult(
            success=False,
            content=f"No LSP server available for file: {file_path}",
            hint="Supported: .py/.pyi/.pyx (pylsp), .js/.jsx/.ts/.tsx/.mjs/.cjs (typescript-language-server).",
        )
    return conn.hover(file_path, line, character)


@_register("lsp_diagnostics")
def _lsp_diagnostics(args: dict, _write_gate, _read_gate) -> ToolResult:
    """Get diagnostics for a file via LSP."""
    file_path = args.get("file_path", "")

    if not file_path:
        return ToolResult(success=False, content="file_path is required")

    manager = get_lsp_manager()
    conn = manager.get_connection(file_path)
    if conn is None:
        return ToolResult(
            success=False,
            content=f"No LSP server available for file: {file_path}",
            hint="Supported: .py/.pyi/.pyx (pylsp), .js/.jsx/.ts/.tsx/.mjs/.cjs (typescript-language-server).",
        )
    return conn.get_diagnostics(file_path)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

@_summarize("lsp_definition")
def _lsp_definition_summary(args: dict) -> str:
    fp = args.get("file_path", "?")
    l = args.get("line", 0)
    c = args.get("character", 0)
    return f"lsp_definition({fp}, {l}:{c})"


@_summarize("lsp_references")
def _lsp_references_summary(args: dict) -> str:
    fp = args.get("file_path", "?")
    l = args.get("line", 0)
    c = args.get("character", 0)
    return f"lsp_references({fp}, {l}:{c})"


@_summarize("lsp_hover")
def _lsp_hover_summary(args: dict) -> str:
    fp = args.get("file_path", "?")
    l = args.get("line", 0)
    c = args.get("character", 0)
    return f"lsp_hover({fp}, {l}:{c})"


@_summarize("lsp_diagnostics")
def _lsp_diagnostics_summary(args: dict) -> str:
    fp = args.get("file_path", "?")
    return f"lsp_diagnostics({fp})"
