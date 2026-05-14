#!/usr/bin/env python3
"""Unit tests for tools/lsp.py.

No subprocess is spawned.  Instead we monkey-patch LspConnection._start_process
and _send_request to return fake inline responses.  All 4 LSP tools (definition,
references, hover, diagnostics) are tested with inline mock data.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.lsp import (
    LspConnection,
    LspClientManager,
    LspConnectionError,
    LspRpcError,
    detect_language,
    _uri_from_path,
    _severity_name,
    _location_to_line,
    uri_to_path,
    get_lsp_manager,
    shutdown_lsp,
)
from tools import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent

# Sentinel to distinguish "no override" from "override with None"
_UNSET = object()


class _FakeStdin:
    """Minimal file-like for disconnect() to write shutdown/exit messages."""
    def write(self, data: bytes) -> int:
        return len(data)
    def flush(self) -> None:
        pass
    def close(self) -> None:
        pass


class FakeProcess:
    """Minimal stand-in for subprocess.Popen so disconnect() doesn't crash."""
    stdin: _FakeStdin = _FakeStdin()
    stdout = None
    stderr = None

    @staticmethod
    def wait(timeout: float | None = None) -> int:
        return 0


def _make_fake_connection(
    language_id: str = "python",
    server_command: str = "fake-lsp",
    definition_result: object = _UNSET,
    references_result: object = _UNSET,
    hover_result: object = _UNSET,
    diagnostics: list[dict] | None = None,
    initialize_error: bool = False,
) -> LspConnection:
    """Create an LspConnection with all subprocess calls monkey-patched.

    Parameters
    ----------
    definition_result:
        Value returned by ``_send_request("textDocument/definition", ...)``.
        Pass ``_UNSET`` (default) to use the default mock Location dict.
    references_result:
        Value returned by ``_send_request("textDocument/references", ...)``.
        Pass ``_UNSET`` (default) to use two default mock locations.
    hover_result:
        Value returned by ``_send_request("textDocument/hover", ...)``.
        Pass ``_UNSET`` (default) to use default hover contents.
        Pass ``None`` to simulate a null hover response.
    diagnostics:
        List of diagnostic dicts injected into ``_diagnostics`` when
        ``textDocument/didOpen`` is sent.
    initialize_error:
        If True, ``_send_request("initialize", ...)`` raises LspRpcError.
    """
    conn = LspConnection(language_id, server_command, [])

    # -- _start_process: assign a fake process so process is not None ---------
    def fake_start_process() -> None:
        conn.process = FakeProcess()  # type: ignore[assignment]

    # -- Guard: mimic the real method's connected check -----------------------
    def _guard() -> None:
        if conn.process is None:
            raise LspConnectionError(
                f"LSP server '{conn.language_id}' is not connected"
            )

    # -- _send_request: canned responses based on method ----------------------
    def fake_send_request(method: str, params: dict | None = None) -> dict:
        _guard()
        if method == "initialize":
            if initialize_error:
                raise LspRpcError({"code": -32000, "message": "mock init failure"})
            return {"capabilities": {}}
        if method == "textDocument/definition":
            if definition_result is not _UNSET:
                return definition_result  # type: ignore[return-value]
            # Default: a single Location dict
            return {
                "uri": _uri_from_path("/fake/def_module.py"),
                "range": {
                    "start": {"line": 10, "character": 4},
                    "end": {"line": 10, "character": 12},
                },
            }
        if method == "textDocument/references":
            if references_result is not _UNSET:
                return references_result  # type: ignore[return-value]
            # Default: two locations
            return [
                {
                    "uri": _uri_from_path("/fake/ref_file.py"),
                    "range": {
                        "start": {"line": 1, "character": 0},
                        "end": {"line": 1, "character": 8},
                    },
                },
                {
                    "uri": _uri_from_path("/fake/ref_file2.py"),
                    "range": {
                        "start": {"line": 5, "character": 2},
                        "end": {"line": 5, "character": 10},
                    },
                },
            ]
        if method == "textDocument/hover":
            if hover_result is not _UNSET:
                return hover_result  # type: ignore[return-value]
            return {
                "contents": {
                    "kind": "markdown",
                    "value": "```python\ndef mock_function(x: int) -> str\n```\n\nMock docstring.",
                },
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 12},
                },
            }
        return {}

    # -- _send_notification: inject diagnostics on didOpen --------------------
    def fake_send_notification(method: str, params: dict | None = None) -> None:
        if not conn._connected:
            return
        if method == "textDocument/didOpen" and diagnostics is not None:
            params = params or {}
            td = params.get("textDocument", {})
            uri = td.get("uri", "")
            conn._diagnostics[uri] = diagnostics

    # -- _drain_notifications: no-op ------------------------------------------
    def fake_drain_notifications() -> None:
        pass

    conn._start_process = fake_start_process  # type: ignore[assignment]
    conn._send_request = fake_send_request  # type: ignore[assignment]
    conn._send_notification = fake_send_notification  # type: ignore[assignment]
    conn._drain_notifications = fake_drain_notifications  # type: ignore[assignment]

    return conn


# ====================================================================
# Test: _uri_from_path
# ====================================================================

class TestUriFromPath:
    def test_simple_path(self) -> None:
        uri = _uri_from_path("/home/user/test.py")
        assert uri.startswith("file://")
        assert "test.py" in uri

    def test_relative_path_converts_to_absolute(self) -> None:
        uri = _uri_from_path("test.py")
        assert uri.startswith("file://")
        assert os.path.abspath("test.py") in uri.replace("file://", "")

    def test_windows_style_path(self) -> None:
        uri = _uri_from_path("C:\\Users\\test.py")
        assert "test.py" in uri


# ====================================================================
# Test: detect_language
# ====================================================================

class TestDetectLanguage:
    def test_python_file(self) -> None:
        result = detect_language("foo.py")
        assert result is not None
        lang_id, command, args = result
        assert lang_id == "python"
        assert command == "pylsp"
        assert args == []

    def test_python_stub_file(self) -> None:
        result = detect_language("foo.pyi")
        assert result is not None
        assert result[0] == "python"

    def test_pythonx_file(self) -> None:
        result = detect_language("foo.pyx")
        assert result is not None
        assert result[0] == "python"

    def test_text_file_returns_none(self) -> None:
        assert detect_language("foo.txt") is None

    def test_no_extension_returns_none(self) -> None:
        assert detect_language("Makefile") is None

    def test_unknown_extension_returns_none(self) -> None:
        assert detect_language("foo.rs") is None

    def test_case_insensitive_extension(self) -> None:
        result = detect_language("foo.PY")
        assert result is not None
        assert result[0] == "python"


# ====================================================================
# Test: uri_to_path
# ====================================================================

class TestUriToPath:
    def test_file_uri_to_path(self) -> None:
        uri = _uri_from_path("/tmp/test.py")
        result = uri_to_path(uri)
        assert result.endswith("test.py")
        assert os.path.isabs(result)


# ====================================================================
# Test: _severity_name
# ====================================================================

class TestSeverityName:
    def test_error(self) -> None:
        assert "[ERROR]" in _severity_name(1)

    def test_warning(self) -> None:
        assert "[WARN]" in _severity_name(2)

    def test_info(self) -> None:
        assert "[INFO]" in _severity_name(3)

    def test_hint(self) -> None:
        assert "[HINT]" in _severity_name(4)

    def test_unknown(self) -> None:
        assert "[?]" in _severity_name(99)


# ====================================================================
# Test: _location_to_line
# ====================================================================

class TestLocationToLine:
    def test_standard_location(self) -> None:
        loc = {
            "uri": _uri_from_path("/tmp/test.py"),
            "range": {
                "start": {"line": 5, "character": 10},
                "end": {"line": 5, "character": 20},
            },
        }
        result = _location_to_line(loc)
        assert "test.py:5:10" in result

    def test_location_link(self) -> None:
        loc = {
            "targetUri": _uri_from_path("/tmp/test.py"),
            "targetRange": {
                "start": {"line": 3, "character": 0},
                "end": {"line": 3, "character": 8},
            },
            "targetSelectionRange": {
                "start": {"line": 3, "character": 0},
                "end": {"line": 3, "character": 8},
            },
        }
        result = _location_to_line(loc)
        assert "test.py:3:0" in result


# ====================================================================
# Test: LspConnection lifecycle
# ====================================================================

class TestLspConnectionLifecycle:
    def test_connect_success(self) -> None:
        conn = _make_fake_connection()
        assert conn.connect(_uri_from_path(str(HERE)))
        assert conn.is_connected

    def test_disconnect(self) -> None:
        conn = _make_fake_connection()
        conn.connect(_uri_from_path(str(HERE)))
        conn.disconnect()
        assert not conn.is_connected
        # double disconnect is safe
        conn.disconnect()
        assert not conn.is_connected

    def test_connect_when_already_connected(self) -> None:
        conn = _make_fake_connection()
        conn.connect(_uri_from_path(str(HERE)))
        assert conn.connect() is True

    def test_connect_fails_when_initialize_errors(self) -> None:
        conn = _make_fake_connection(initialize_error=True)
        assert conn.connect() is False
        assert not conn.is_connected

    def test_is_connected_initially_false(self) -> None:
        conn = _make_fake_connection()
        assert not conn.is_connected


# ====================================================================
# Test: LspConnection.definition
# ====================================================================

class TestLspConnectionDefinition:
    def test_definition_returns_location(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.definition("/fake/test.py", 5, 10)
        assert isinstance(result, ToolResult)
        assert result.success
        assert "def_module.py:10:4" in result.content

    def test_definition_list_result(self) -> None:
        """Definition returning a list of locations."""
        conn = _make_fake_connection(
            definition_result=[
                {
                    "uri": _uri_from_path("/a/foo.py"),
                    "range": {
                        "start": {"line": 1, "character": 0},
                        "end": {"line": 1, "character": 5},
                    },
                },
                {
                    "uri": _uri_from_path("/b/bar.py"),
                    "range": {
                        "start": {"line": 2, "character": 3},
                        "end": {"line": 2, "character": 8},
                    },
                },
            ]
        )
        conn.connect()
        result = conn.definition("/fake/test.py", 0, 0)
        assert result.success
        assert "foo.py:1:0" in result.content
        assert "bar.py:2:3" in result.content

    def test_definition_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        # Never connect
        result = conn.definition("/fake/test.py", 0, 0)
        assert isinstance(result, ToolResult)
        assert not result.success
        assert "not connected" in result.content.lower()


# ====================================================================
# Test: LspConnection.references
# ====================================================================

class TestLspConnectionReferences:
    def test_references_returns_locations(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.references("/fake/test.py", 3, 4)
        assert isinstance(result, ToolResult)
        assert result.success
        assert "ref_file.py:1:0" in result.content
        assert "ref_file2.py:5:2" in result.content

    def test_references_empty(self) -> None:
        conn = _make_fake_connection(references_result=[])
        conn.connect()
        result = conn.references("/fake/test.py", 0, 0)
        assert result.success
        assert "no references" in result.content.lower()

    def test_references_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        result = conn.references("/fake/test.py", 0, 0)
        assert not result.success
        assert "not connected" in result.content.lower()


# ====================================================================
# Test: LspConnection.hover
# ====================================================================

class TestLspConnectionHover:
    def test_hover_returns_contents(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.hover("/fake/test.py", 2, 6)
        assert isinstance(result, ToolResult)
        assert result.success
        assert "mock_function" in result.content

    def test_hover_null_result(self) -> None:
        """Hover returning null -> (no hover information)."""
        conn = _make_fake_connection(hover_result=None)
        conn.connect()
        result = conn.hover("/fake/test.py", 0, 0)
        assert result.success
        assert "no hover information" in result.content.lower()

    def test_hover_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        result = conn.hover("/fake/test.py", 0, 0)
        assert not result.success
        assert "not connected" in result.content.lower()


# ====================================================================
# Test: LspConnection.get_diagnostics
# ====================================================================

class TestLspConnectionDiagnostics:
    def test_get_diagnostics_with_data(self) -> None:
        diags = [
            {
                "range": {
                    "start": {"line": 10, "character": 0},
                    "end": {"line": 10, "character": 5},
                },
                "severity": 1,
                "message": "Undefined variable 'x'",
                "source": "pylint",
                "code": "E0602",
            },
            {
                "range": {
                    "start": {"line": 20, "character": 4},
                    "end": {"line": 20, "character": 8},
                },
                "severity": 2,
                "message": "Unused import 'os'",
            },
        ]
        conn = _make_fake_connection(diagnostics=diags)
        conn.connect()
        result = conn.get_diagnostics("/fake/test.py")
        assert result.success
        assert "Undefined variable 'x'" in result.content
        assert "Unused import 'os'" in result.content
        assert "[ERROR]" in result.content
        assert "[WARN]" in result.content

    def test_get_diagnostics_empty(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.get_diagnostics("/fake/test.py")
        assert result.success
        assert "No diagnostics" in result.content

    def test_get_diagnostics_returns_no_diagnostics_when_never_connected(self) -> None:
        """get_diagnostics doesn't check connection; just returns empty cache."""
        conn = _make_fake_connection()
        result = conn.get_diagnostics("/fake/test.py")
        assert result.success
        assert "No diagnostics" in result.content


# ====================================================================
# Test: LspClientManager
# ====================================================================

class TestLspClientManager:
    def test_get_connection_returns_none_for_unsupported_file(self) -> None:
        mgr = LspClientManager()
        assert mgr.get_connection("test.txt") is None

    def test_get_connection_caches(self) -> None:
        mgr = LspClientManager()
        fake = _make_fake_connection()
        fake._connected = True
        mgr._connections["python"] = fake
        mgr.set_root(str(HERE))
        conn = mgr.get_connection(str(HERE / "sample.py"))
        assert conn is fake

    def test_set_root(self) -> None:
        mgr = LspClientManager()
        mgr.set_root("/tmp/workspace")
        assert mgr._root_uri is not None
        assert "workspace" in mgr._root_uri

    def test_shutdown_all(self) -> None:
        mgr = LspClientManager()
        fake = _make_fake_connection()
        mgr._connections["python"] = fake
        mgr.shutdown_all()
        assert "python" not in mgr._connections

    def test_global_manager_singleton(self) -> None:
        # Reset global state first
        shutdown_lsp()
        m1 = get_lsp_manager()
        m2 = get_lsp_manager()
        assert m1 is m2

    def test_shutdown_lsp_clears_manager(self) -> None:
        m1 = get_lsp_manager()
        shutdown_lsp()
        m2 = get_lsp_manager()
        assert m1 is not m2


# ====================================================================
# Test: Error types
# ====================================================================

class TestErrorTypes:
    def test_lsp_rpc_error(self) -> None:
        err = LspRpcError({"code": -32601, "message": "Method not found"})
        assert err.code == -32601
        assert "Method not found" in str(err)

    def test_lsp_connection_error(self) -> None:
        err = LspConnectionError("Connection lost")
        assert "Connection lost" in str(err)
