#!/usr/bin/env python3
"""Unit tests for tools/lsp.py.

No subprocess is spawned.  Instead we monkey-patch LspConnection._start_process
and _send_request to return fake inline responses.  All 4 LSP tools (definition,
references, hover, diagnostics) are tested with inline mock data.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

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

class TestUriFromPath(unittest.TestCase):
    def test_simple_path(self) -> None:
        uri = _uri_from_path("/home/user/test.py")
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("test.py", uri)

    def test_relative_path_converts_to_absolute(self) -> None:
        uri = _uri_from_path("test.py")
        self.assertTrue(uri.startswith("file://"))
        # Path.as_uri() always uses forward slashes; normalize for comparison
        expected = os.path.abspath("test.py").replace("\\", "/")
        self.assertIn(expected, uri.replace("file://", ""))

    def test_windows_style_path(self) -> None:
        uri = _uri_from_path("C:\\Users\\test.py")
        self.assertIn("test.py", uri)


# ====================================================================
# Test: detect_language
# ====================================================================

class TestDetectLanguage(unittest.TestCase):
    def test_python_file(self) -> None:
        result = detect_language("foo.py")
        self.assertIsNotNone(result)
        lang_id, command, args = result
        self.assertEqual(lang_id, "python")
        self.assertEqual(command, "pylsp")
        self.assertEqual(args, [])

    def test_python_stub_file(self) -> None:
        result = detect_language("foo.pyi")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "python")

    def test_pythonx_file(self) -> None:
        result = detect_language("foo.pyx")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "python")

    def test_text_file_returns_none(self) -> None:
        self.assertIsNone(detect_language("foo.txt"))

    def test_no_extension_returns_none(self) -> None:
        self.assertIsNone(detect_language("Makefile"))

    def test_unknown_extension_returns_none(self) -> None:
        self.assertIsNone(detect_language("foo.rs"))

    def test_case_insensitive_extension(self) -> None:
        result = detect_language("foo.PY")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "python")


# ====================================================================
# Test: uri_to_path
# ====================================================================

class TestUriToPath(unittest.TestCase):
    def test_file_uri_to_path(self) -> None:
        uri = _uri_from_path("/tmp/test.py")
        result = uri_to_path(uri)
        self.assertTrue(result.endswith("test.py"))
        self.assertTrue(os.path.isabs(result))


# ====================================================================
# Test: _severity_name
# ====================================================================

class TestSeverityName(unittest.TestCase):
    def test_error(self) -> None:
        self.assertIn("[ERROR]", _severity_name(1))

    def test_warning(self) -> None:
        self.assertIn("[WARN]", _severity_name(2))

    def test_info(self) -> None:
        self.assertIn("[INFO]", _severity_name(3))

    def test_hint(self) -> None:
        self.assertIn("[HINT]", _severity_name(4))

    def test_unknown(self) -> None:
        self.assertIn("[?]", _severity_name(99))


# ====================================================================
# Test: _location_to_line
# ====================================================================

class TestLocationToLine(unittest.TestCase):
    def test_standard_location(self) -> None:
        loc = {
            "uri": _uri_from_path("/tmp/test.py"),
            "range": {
                "start": {"line": 5, "character": 10},
                "end": {"line": 5, "character": 20},
            },
        }
        result = _location_to_line(loc)
        self.assertIn("test.py:5:10", result)

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
        self.assertIn("test.py:3:0", result)


# ====================================================================
# Test: LspConnection lifecycle
# ====================================================================

class TestLspConnectionLifecycle(unittest.TestCase):
    def test_connect_success(self) -> None:
        conn = _make_fake_connection()
        self.assertTrue(conn.connect(_uri_from_path(str(HERE))))
        self.assertTrue(conn.is_connected)

    def test_disconnect(self) -> None:
        conn = _make_fake_connection()
        conn.connect(_uri_from_path(str(HERE)))
        conn.disconnect()
        self.assertFalse(conn.is_connected)
        # double disconnect is safe
        conn.disconnect()
        self.assertFalse(conn.is_connected)

    def test_connect_when_already_connected(self) -> None:
        conn = _make_fake_connection()
        conn.connect(_uri_from_path(str(HERE)))
        self.assertTrue(conn.connect())

    def test_connect_fails_when_initialize_errors(self) -> None:
        conn = _make_fake_connection(initialize_error=True)
        self.assertFalse(conn.connect())
        self.assertFalse(conn.is_connected)

    def test_is_connected_initially_false(self) -> None:
        conn = _make_fake_connection()
        self.assertFalse(conn.is_connected)


# ====================================================================
# Test: LspConnection.definition
# ====================================================================

class TestLspConnectionDefinition(unittest.TestCase):
    def test_definition_returns_location(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.definition("/fake/test.py", 5, 10)
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertIn("def_module.py:10:4", result.content)

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
        self.assertTrue(result.success)
        self.assertIn("foo.py:1:0", result.content)
        self.assertIn("bar.py:2:3", result.content)

    def test_definition_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        # Never connect
        result = conn.definition("/fake/test.py", 0, 0)
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.success)
        self.assertIn("not connected", result.content.lower())


# ====================================================================
# Test: LspConnection.references
# ====================================================================

class TestLspConnectionReferences(unittest.TestCase):
    def test_references_returns_locations(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.references("/fake/test.py", 3, 4)
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertIn("ref_file.py:1:0", result.content)
        self.assertIn("ref_file2.py:5:2", result.content)

    def test_references_empty(self) -> None:
        conn = _make_fake_connection(references_result=[])
        conn.connect()
        result = conn.references("/fake/test.py", 0, 0)
        self.assertTrue(result.success)
        self.assertIn("no references", result.content.lower())

    def test_references_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        result = conn.references("/fake/test.py", 0, 0)
        self.assertFalse(result.success)
        self.assertIn("not connected", result.content.lower())


# ====================================================================
# Test: LspConnection.hover
# ====================================================================

class TestLspConnectionHover(unittest.TestCase):
    def test_hover_returns_contents(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.hover("/fake/test.py", 2, 6)
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertIn("mock_function", result.content)

    def test_hover_null_result(self) -> None:
        """Hover returning null -> (no hover information)."""
        conn = _make_fake_connection(hover_result=None)
        conn.connect()
        result = conn.hover("/fake/test.py", 0, 0)
        self.assertTrue(result.success)
        self.assertIn("no hover information", result.content.lower())

    def test_hover_on_disconnected_returns_error(self) -> None:
        conn = _make_fake_connection()
        result = conn.hover("/fake/test.py", 0, 0)
        self.assertFalse(result.success)
        self.assertIn("not connected", result.content.lower())


# ====================================================================
# Test: LspConnection.get_diagnostics
# ====================================================================

class TestLspConnectionDiagnostics(unittest.TestCase):
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
        self.assertTrue(result.success)
        self.assertIn("Undefined variable 'x'", result.content)
        self.assertIn("Unused import 'os'", result.content)
        self.assertIn("[ERROR]", result.content)
        self.assertIn("[WARN]", result.content)

    def test_get_diagnostics_empty(self) -> None:
        conn = _make_fake_connection()
        conn.connect()
        result = conn.get_diagnostics("/fake/test.py")
        self.assertTrue(result.success)
        self.assertIn("No diagnostics", result.content)

    def test_get_diagnostics_returns_no_diagnostics_when_never_connected(self) -> None:
        """get_diagnostics doesn't check connection; just returns empty cache."""
        conn = _make_fake_connection()
        result = conn.get_diagnostics("/fake/test.py")
        self.assertTrue(result.success)
        self.assertIn("No diagnostics", result.content)


# ====================================================================
# Test: LspClientManager
# ====================================================================

class TestLspClientManager(unittest.TestCase):
    def test_get_connection_returns_none_for_unsupported_file(self) -> None:
        mgr = LspClientManager()
        self.assertIsNone(mgr.get_connection("test.txt"))

    def test_get_connection_caches(self) -> None:
        mgr = LspClientManager()
        fake = _make_fake_connection()
        fake._connected = True
        mgr._connections["python"] = fake
        mgr.set_root(str(HERE))
        conn = mgr.get_connection(str(HERE / "sample.py"))
        self.assertIs(conn, fake)

    def test_set_root(self) -> None:
        mgr = LspClientManager()
        mgr.set_root("/tmp/workspace")
        self.assertIsNotNone(mgr._root_uri)
        self.assertIn("workspace", mgr._root_uri)

    def test_shutdown_all(self) -> None:
        mgr = LspClientManager()
        fake = _make_fake_connection()
        mgr._connections["python"] = fake
        mgr.shutdown_all()
        self.assertNotIn("python", mgr._connections)

    def test_global_manager_singleton(self) -> None:
        # Reset global state first
        shutdown_lsp()
        m1 = get_lsp_manager()
        m2 = get_lsp_manager()
        self.assertIs(m1, m2)

    def test_shutdown_lsp_clears_manager(self) -> None:
        m1 = get_lsp_manager()
        shutdown_lsp()
        m2 = get_lsp_manager()
        self.assertIsNot(m1, m2)


# ====================================================================
# Test: Error types
# ====================================================================

class TestErrorTypes(unittest.TestCase):
    def test_lsp_rpc_error(self) -> None:
        err = LspRpcError({"code": -32601, "message": "Method not found"})
        self.assertEqual(err.code, -32601)
        self.assertIn("Method not found", str(err))

    def test_lsp_connection_error(self) -> None:
        err = LspConnectionError("Connection lost")
        self.assertIn("Connection lost", str(err))
