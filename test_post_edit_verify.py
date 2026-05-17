"""Test post-edit auto-verification: _lsp_diagnostics is called after
successful write_file / edit_file, and NOT after failures or non-write tools.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from tools.__init__ import (
    ToolResult,
    _TOOL_CONTEXT,
    _TOOL_DISPATCH,
    execute_tool,
)


class TestPostEditVerify(unittest.TestCase):
    """Verify execute_tool calls _lsp_diagnostics only after successful writes."""

    def setUp(self):
        self._saved_dispatch = dict(_TOOL_DISPATCH)
        self._saved_context_state = dict(_TOOL_CONTEXT.__dict__)
        _TOOL_CONTEXT.__dict__["_failure_patterns"] = {}
        _TOOL_CONTEXT._memory_store = None
        self.write_gate = MagicMock()
        self.read_gate = MagicMock()

    def tearDown(self):
        _TOOL_DISPATCH.clear()
        _TOOL_DISPATCH.update(self._saved_dispatch)
        _TOOL_CONTEXT.__dict__.clear()
        _TOOL_CONTEXT.__dict__.update(self._saved_context_state)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _register_mock_tool(self, name, *, success=True, content="mock result"):
        def mock_tool(_args, _write_gate, _read_gate):
            return ToolResult(success=success, content=content)

        _TOOL_DISPATCH[name] = mock_tool

    @staticmethod
    def _write_file_call(path="/tmp/test.py", content="x"):
        return {
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": path, "content": content}),
            }
        }

    @staticmethod
    def _edit_file_call(path="/tmp/test.py", old="a", new="b"):
        return {
            "function": {
                "name": "edit_file",
                "arguments": json.dumps(
                    {"path": path, "old_string": old, "new_string": new}
                ),
            }
        }

    @staticmethod
    def _non_write_call(name, **extra):
        """Build a tool call payload whose required params match the schema."""
        # Minimal required args per tool schema
        required_map = {
            "read_file": {"path": "/tmp/test.py"},
            "run_shell": {"command": "echo hi"},
            "search_files": {"pattern": "test"},
            "find_symbol": {"name": "test"},
            "list_directory": {"path": "/tmp/test.py"},
        }
        args = dict(required_map.get(name, {}))
        args.update(extra)
        return {"function": {"name": name, "arguments": json.dumps(args)}}

    # ------------------------------------------------------------------
    # success cases: diagnostics called
    # ------------------------------------------------------------------

    def test_diagnostics_called_after_successful_write_file(self):
        self._register_mock_tool("write_file", success=True)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            mock_diag.return_value = ToolResult(success=True, content="LSP ok")
            result = execute_tool(
                self._write_file_call(), self.write_gate, self.read_gate
            )
            self.assertTrue(result.success)
            mock_diag.assert_called_once()

    def test_diagnostics_called_after_successful_edit_file(self):
        self._register_mock_tool("edit_file", success=True)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            mock_diag.return_value = ToolResult(success=True, content="LSP ok")
            result = execute_tool(
                self._edit_file_call(), self.write_gate, self.read_gate
            )
            self.assertTrue(result.success)
            mock_diag.assert_called_once()

    def test_diagnostics_result_appended_to_content(self):
        self._register_mock_tool("write_file", success=True)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            mock_diag.return_value = ToolResult(success=True, content="no issues")
            result = execute_tool(
                self._write_file_call(), self.write_gate, self.read_gate
            )
            self.assertTrue(result.success)
            self.assertIn("[auto-verify] LSP diagnostics:", result.content)
            self.assertIn("no issues", result.content)

    # ------------------------------------------------------------------
    # failure cases: diagnostics NOT called
    # ------------------------------------------------------------------

    def test_diagnostics_not_called_after_failed_write_file(self):
        self._register_mock_tool("write_file", success=False)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            result = execute_tool(
                self._write_file_call(), self.write_gate, self.read_gate
            )
            self.assertFalse(result.success)
            mock_diag.assert_not_called()

    def test_diagnostics_not_called_after_failed_edit_file(self):
        self._register_mock_tool("edit_file", success=False)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            result = execute_tool(
                self._edit_file_call(), self.write_gate, self.read_gate
            )
            self.assertFalse(result.success)
            mock_diag.assert_not_called()

    def test_diagnostics_not_called_for_non_write_tools(self):
        non_write_tools = ["read_file", "run_shell", "search_files", "find_symbol"]
        for tool_name in non_write_tools:
            with self.subTest(tool=tool_name):
                self._register_mock_tool(tool_name, success=True)
                with patch("tools.lsp._lsp_diagnostics") as mock_diag:
                    result = execute_tool(
                        self._non_write_call(tool_name),
                        self.write_gate,
                        self.read_gate,
                    )
                    self.assertTrue(result.success)
                    mock_diag.assert_not_called()

    def test_diagnostics_not_called_when_path_empty(self):
        self._register_mock_tool("write_file", success=True)
        empty_path_call = {
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "", "content": "x"}),
            }
        }
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            mock_diag.return_value = ToolResult(success=True, content="LSP ok")
            result = execute_tool(empty_path_call, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            mock_diag.assert_not_called()

    # ------------------------------------------------------------------
    # resilience
    # ------------------------------------------------------------------

    def test_diagnostics_exception_does_not_crash_execute_tool(self):
        self._register_mock_tool("write_file", success=True)
        with patch("tools.lsp._lsp_diagnostics") as mock_diag:
            mock_diag.side_effect = RuntimeError("LSP server gone")
            result = execute_tool(
                self._write_file_call(), self.write_gate, self.read_gate
            )
            self.assertTrue(result.success)
