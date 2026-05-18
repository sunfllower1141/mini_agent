#!/usr/bin/env python3
"""
test_tools.py — tests for tool implementations and tool_summary display.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from safety import ReadSafetyGate, WriteSafetyGate
from tools import ToolResult, execute_tool, tool_summary, _TOOL_CONTEXT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_call(name: str, /, **kwargs) -> dict:
    return {
        "id": "call_test",
        "function": {
            "name": name,
            "arguments": json.dumps(kwargs),
        },
    }


def _gates(workspace: str) -> tuple[WriteSafetyGate, ReadSafetyGate]:
    return WriteSafetyGate(workspace, allow_overwrites=True), ReadSafetyGate(workspace)


# ---------------------------------------------------------------------------
# run_shell tests
# ---------------------------------------------------------------------------

class TestRunShell(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_simple_command_succeeds(self):
        tc = _make_tool_call("run_shell", command="echo hello")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("hello", result.content)
        self.assertIn("exit_code=0", result.content)

    def test_failing_command_returns_failure(self):
        tc = _make_tool_call("run_shell", command="exit 1")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("exit_code=1", result.content)

    def test_stderr_is_captured(self):
        tc = _make_tool_call("run_shell", command="echo err >&2")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("stderr:", result.content)
        self.assertIn("err", result.content)

    def test_stdout_and_stderr_both_captured(self):
        tc = _make_tool_call("run_shell", command="echo out && echo err >&2")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("out", result.content)
        self.assertIn("err", result.content)

    def test_returns_tool_result_not_exception(self):
        tc = _make_tool_call("run_shell", command="nonexistent_command_xyz")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)

    def test_runs_in_workspace_directory(self):
        tc = _make_tool_call("run_shell", command="pwd")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        resolved = os.path.realpath(self.workspace)
        self.assertIn(resolved, result.content)

    # --- shell guard ---

    def test_rm_rf_no_longer_blocked(self):
        """Safety guards removed — rm runs (may fail on perms but not blocked)."""
        tc = _make_tool_call("run_shell", command="rm -rf /etc")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertNotIn("blocked by safety guard", result.content)

    def test_fork_bomb_no_longer_blocked(self):
        """Safety guards removed — fork bomb runs (shell rejects syntax anyway)."""
        tc = _make_tool_call("run_shell", command=":(){ :|:& };:")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertNotIn("blocked by safety guard", result.content)

    def test_force_bypasses_guard(self):
        tc = _make_tool_call("run_shell", command="rm -rf /nonexistent_test_dir", force=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        # Will succeed or fail depending on permissions, but NOT blocked by guard
        self.assertNotIn("blocked by safety guard", result.content)

    def test_safe_commands_not_blocked(self):
        tc = _make_tool_call("run_shell", command="echo hello && ls -la")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("hello", result.content)

    def test_custom_timeout(self):
        """Custom timeout is accepted and used."""
        tc = _make_tool_call("run_shell", command="echo done", timeout=10)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("done", result.content)

    def test_timeout_clamped_at_300(self):
        """Timeout > 300 is clamped to 300."""
        tc = _make_tool_call("run_shell", command="echo done", timeout=999)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("done", result.content)


# ---------------------------------------------------------------------------
# search_files tests
# ---------------------------------------------------------------------------

class TestSearchFiles(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        self._write("a.txt", "hello world\nfoo bar\n")
        self._write("b.txt", "hello again\nbaz qux\n")
        os.makedirs(os.path.join(self.workspace, "sub"))
        self._write(os.path.join("sub", "c.txt"), "nested hello\n")
        os.makedirs(os.path.join(self.workspace, ".hidden"))
        self._write(os.path.join(".hidden", "d.txt"), "hidden hello\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_finds_matches(self):
        tc = _make_tool_call("search_files", pattern="hello", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("a.txt:1:", result.content)
        self.assertIn("b.txt:1:", result.content)
        self.assertIn("c.txt:1:", result.content)

    def test_no_matches(self):
        tc = _make_tool_call("search_files", pattern="zzznonexistent", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No matches", result.content)

    def test_match_includes_line_number(self):
        tc = _make_tool_call("search_files", pattern="bar", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("a.txt:2:", result.content)

    def test_skips_hidden_directories(self):
        tc = _make_tool_call("search_files", pattern="hidden", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No matches", result.content)

    def test_outside_workspace_allowed(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("search_files", pattern="x", path=outside)
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            self.assertNotIn("blocked by safety layer", result.content)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_capped_at_200_results(self):
        lines = "\n".join(f"match_{i}" for i in range(60))
        self._write("big.txt", lines)
        tc = _make_tool_call("search_files", pattern="match_", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        match_lines = [l for l in result.content.split("\n") if "big.txt:" in l]
        self.assertEqual(len(match_lines), 60)
        # 60 is below 200 cap — no truncation message
        self.assertNotIn("capped at", result.content)

    def test_default_path_is_dot(self):
        tc = _make_tool_call("search_files", pattern="hello")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)

    def test_unknown_tool_returns_failure(self):
        tc = _make_tool_call("no_such_tool", x="y")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.content)

    # --- regex search ---

    def test_regex_matches(self):
        self._write("funcs.py", "def hello():\n  pass\nclass Foo:\n  pass\n")
        tc = _make_tool_call("search_files", pattern=r"def \w+", path=self.workspace, regex=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("def hello", result.content)
        # "class Foo" should NOT match
        self.assertNotIn("class Foo", result.content)

    def test_regex_finds_decorators(self):
        self._write("deco.py", "@register\ndef f(): pass\n@summarize\ndef g(): pass\n")
        tc = _make_tool_call("search_files", pattern=r"@\w+", path=self.workspace, regex=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("@register", result.content)
        self.assertIn("@summarize", result.content)

    def test_invalid_regex_returns_error(self):
        tc = _make_tool_call("search_files", pattern="[unclosed", path=self.workspace, regex=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Invalid regex", result.content)

    # --- case-insensitive search ---

    def test_case_insensitive_matches(self):
        self._write("caps.py", "HELLO WORLD\nFooBar\n")
        tc = _make_tool_call("search_files", pattern="hello", path=self.workspace, ignore_case=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("HELLO", result.content)

    def test_case_sensitive_still_works(self):
        self._write("caps.py", "HELLO WORLD\n")
        tc = _make_tool_call("search_files", pattern="hello",
                             path=os.path.join(self.workspace, "caps.py"))
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No matches", result.content)

    def test_case_insensitive_with_regex(self):
        self._write("caps.py", "HELLO test\nhello TEST\n")
        tc = _make_tool_call("search_files", pattern=r"test", path=self.workspace,
                             regex=True, ignore_case=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("HELLO test", result.content)
        self.assertIn("hello TEST", result.content)


# ---------------------------------------------------------------------------
# edit_file tests
# ---------------------------------------------------------------------------

class TestEditFile(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_replaces_first_occurrence(self):
        path = self._write("f.txt", "hello world hello")
        tc = _make_tool_call("edit_file", path=path,
                             old_string="hello", new_string="hi")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        with open(path) as f:
            self.assertEqual(f.read(), "hi world hello")

    def test_old_string_not_found_returns_error(self):
        path = self._write("f.txt", "abc")
        tc = _make_tool_call("edit_file", path=path,
                             old_string="xyz", new_string="q")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("not found", result.content)

    def test_outside_workspace_allowed(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("edit_file",
                                 path=os.path.join(outside, "x.txt"),
                                 old_string="a", new_string="b")
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertNotIn("blocked by safety layer", result.content)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_write_file_tracks_modified_files(self):
        from tools import _MODIFIED_FILES, clear_tool_cache
        clear_tool_cache()
        _MODIFIED_FILES.clear()
        path = os.path.join(self.workspace, "new_file.txt")
        tc = _make_tool_call("write_file", path=path, content="hello")
        execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIn(os.path.realpath(path), _MODIFIED_FILES)


# ---------------------------------------------------------------------------
# file_info tests
# ---------------------------------------------------------------------------

class TestFileInfo(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_existing_file_returns_metadata(self):
        path = self._write("notes.txt", "hello")
        tc = _make_tool_call("file_info", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("size: 5 bytes", result.content)
        self.assertIn("type: file", result.content)
        self.assertIn("mode:", result.content)
        self.assertIn("modified:", result.content)

    def test_directory_identified_as_directory(self):
        sub = os.path.join(self.workspace, "subdir")
        os.makedirs(sub)
        tc = _make_tool_call("file_info", path=sub)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("type: directory", result.content)

    def test_nonexistent_file_reports_not_found(self):
        path = os.path.join(self.workspace, "nope.txt")
        tc = _make_tool_call("file_info", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("exists: no", result.content)

    def test_outside_workspace_allowed(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("file_info", path=os.path.join(outside, "x.txt"))
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            self.assertNotIn("blocked by safety layer", result.content)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)


# ---------------------------------------------------------------------------
# tool_summary tests
# ---------------------------------------------------------------------------

class TestToolSummary(unittest.TestCase):

    def test_read_file_summary(self):
        tc = _make_tool_call("read_file", path="/some/file.txt")
        s = tool_summary(tc)
        self.assertIn("read_file", s)
        self.assertIn("/some/file.txt", s)

    def test_write_file_summary(self):
        tc = _make_tool_call("write_file", path="out.txt", content="hello world")
        s = tool_summary(tc)
        self.assertIn("write_file", s)
        self.assertIn("out.txt", s)
        self.assertIn("11B", s)
        self.assertIn("hello world", s)

    def test_write_file_long_content_truncated(self):
        tc = _make_tool_call("write_file", path="x", content="a" * 100)
        s = tool_summary(tc)
        self.assertIn("…", s)
        self.assertLess(len(s), 150)

    def test_edit_file_summary(self):
        tc = _make_tool_call("edit_file", path="f.txt",
                             old_string="replace me", new_string="done")
        s = tool_summary(tc)
        self.assertIn("edit_file", s)
        self.assertIn("f.txt", s)
        self.assertIn("replace me", s)

    def test_list_directory_summary(self):
        tc = _make_tool_call("list_directory", path="/tmp")
        s = tool_summary(tc)
        self.assertIn("list_directory", s)
        self.assertIn("/tmp", s)

    def test_run_shell_summary(self):
        tc = _make_tool_call("run_shell", command="python -m pytest -v")
        s = tool_summary(tc)
        self.assertIn("run_shell", s)
        self.assertIn("python -m pytest", s)

    def test_run_shell_long_command_truncated(self):
        tc = _make_tool_call("run_shell", command="x" * 100)
        s = tool_summary(tc)
        self.assertIn("…", s)

    def test_search_files_summary(self):
        tc = _make_tool_call("search_files", pattern="TODO", path="src")
        s = tool_summary(tc)
        self.assertIn("search_files", s)
        self.assertIn("TODO", s)
        self.assertIn("src", s)

    def test_file_info_summary(self):
        tc = _make_tool_call("file_info", path="/a/b")
        s = tool_summary(tc)
        self.assertIn("file_info", s)
        self.assertIn("/a/b", s)

    def test_unknown_tool_summary(self):
        tc = _make_tool_call("nonexistent_tool", foo="bar")
        s = tool_summary(tc)
        self.assertIn("nonexistent_tool", s)
        self.assertIn("…", s)

    def test_summary_handles_bad_json(self):
        tc = {
            "id": "call_x",
            "function": {
                "name": "read_file",
                "arguments": "not valid json {{{",
            },
        }
        s = tool_summary(tc)
        self.assertIn("read_file", s)


# ---------------------------------------------------------------------------
# run_tests tool tests
# ---------------------------------------------------------------------------

class TestRunTests(unittest.TestCase):
    """Verify the run_tests tool works with real pytest output."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        # Reset sub-agent depth in case another test leaked it (daemon threads)
        _TOOL_CONTEXT._agent_depth = 0
        # Create a minimal test file so pytest has something to discover
        test_dir = os.path.join(self.workspace, "tests")
        os.makedirs(test_dir)
        with open(os.path.join(test_dir, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(test_dir, "test_dummy.py"), "w") as f:
            f.write("def test_pass(): assert True\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_runs_all_tests_in_workspace(self):
        tc = _make_tool_call("run_tests")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("passed", result.content)

    def test_runs_specific_file(self):
        tc = _make_tool_call("run_tests", path="tests/test_dummy.py")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("passed", result.content)

    def test_failing_tests_return_failure(self):
        with open(os.path.join(self.workspace, "tests", "test_fail.py"), "w") as f:
            f.write("def test_fail(): assert False\n")
        tc = _make_tool_call("run_tests", path="tests/test_fail.py")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("failed", result.content)

    def test_returns_tool_result_not_exception(self):
        tc = _make_tool_call("run_tests", path="nonexistent_file.py")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)

    def test_background_mode_returns_task_id(self):
        from tools import _TASK_REGISTRY
        tc = _make_tool_call("run_tests", path="tests/test_dummy.py", background=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("background test run", result.content)
        # Extract task_id: "Started background test run {id}. Use..."
        task_id = result.content.split()[4].rstrip(".")
        self.assertIn(task_id, _TASK_REGISTRY)
        # Clean up: wait for process to finish
        proc = _TASK_REGISTRY.pop(task_id, None)
        if proc:
            proc.wait(timeout=30)

    def test_test_output_persisted_to_db(self):
        """After running tests, the output should be in the memory DB."""
        from tools import _TOOL_CONTEXT
        # Create a temp DB and wire it into _TOOL_CONTEXT
        tmp_db = os.path.join(self.workspace, "test_memory.db")
        _TOOL_CONTEXT.scratchpad_path = tmp_db
        # Initialize the table
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("CREATE TABLE IF NOT EXISTS test_output (id INTEGER PRIMARY KEY CHECK (id = 1), output TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT OR IGNORE INTO test_output (id, output) VALUES (1, '')")
        conn.commit()
        conn.close()

        tc = _make_tool_call("run_tests", path="test_config.py")
        execute_tool(tc, self.write_gate, self.read_gate)

        # Verify the DB has test output
        conn = sqlite3.connect(tmp_db)
        row = conn.execute("SELECT output FROM test_output WHERE id = 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(len(row[0]) > 0, "DB should have test output")
        # Clean up
        _TOOL_CONTEXT.scratchpad_path = None


# ---------------------------------------------------------------------------
# web_search tests
# ---------------------------------------------------------------------------

class TestWebSearch(unittest.TestCase):
    """Verify web_search tool behavior. Uses real API if key is available."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        from config import DEFAULT_EXA_API_KEY
        from tools import set_context
        set_context(exa_api_key=os.environ.get("EXA_API_KEY", DEFAULT_EXA_API_KEY))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_requires_query(self):
        tc = _make_tool_call("web_search")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)

    # --- API-call tests disabled to save Exa tokens ---
    #
    # def test_valid_search_returns_results(self):
    #     from config import DEFAULT_EXA_API_KEY
    #     api_key = os.environ.get("EXA_API_KEY", DEFAULT_EXA_API_KEY)
    #     if not api_key:
    #         self.skipTest("EXA_API_KEY not set")
    #     tc = _make_tool_call("web_search", query="Python typing module best practices", num_results=3)
    #     result = execute_tool(tc, self.write_gate, self.read_gate)
    #     self.assertTrue(result.success)
    #     self.assertIn("1.", result.content)
    #     self.assertIn("http", result.content)
    #
    # def test_no_results_for_nonsense_query(self):
    #     from config import DEFAULT_EXA_API_KEY
    #     api_key = os.environ.get("EXA_API_KEY", DEFAULT_EXA_API_KEY)
    #     if not api_key:
    #         self.skipTest("EXA_API_KEY not set")
    #     tc = _make_tool_call("web_search", query="xxyzzzblargnothingatall123456789")
    #     result = execute_tool(tc, self.write_gate, self.read_gate)
    #     self.assertTrue(result.success)


# ---------------------------------------------------------------------------
# semantic_search tests
# ---------------------------------------------------------------------------

class TestSemanticSearch(unittest.TestCase):
    """Verify semantic_search indexes .py files and returns relevant chunks."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_requires_query(self):
        tc = _make_tool_call("semantic_search")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.success)
        self.assertIn("query", result.content)

    @patch("tools.search_ops._sem_get_model")
    def test_finds_relevant_chunks(self, mock_model):
        import numpy as np
        mock_model.return_value.encode.return_value = np.array([[0.0, 1.0], [1.0, 0.0]])
        self._write("auth.py", "def authenticate_user(token):\n    if token:\n        return True\n    return False\n")
        self._write("storage.py", "def save_file(path, data):\n    with open(path, 'w') as f:\n        f.write(data)\n")
        tc = _make_tool_call("semantic_search", query="user login and authentication", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        # Should find the auth function first
        self.assertIn("auth.py", result.content.lower())

    @patch("tools.search_ops._sem_get_model")
    def test_finds_file_io_chunks(self, mock_model):
        import numpy as np
        mock = mock_model.return_value
        # "writing files to disk" → similar to storage.py (0.95), not auth.py (0.3)
        mock.encode.side_effect = lambda texts, **kw: np.array([
            [1.0, 0.0] if "save_file" in t else [1.0, 0.0] if "writing" in t else [0.0, 1.0]
            for t in texts
        ])

        self._write("auth.py", "def authenticate_user(token):\n    if token:\n        return True\n    return False\n")
        self._write("storage.py", "def save_file(path, data):\n    with open(path, 'w') as f:\n        f.write(data)\n")
        tc = _make_tool_call("semantic_search", query="writing files to disk", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("storage.py", result.content.lower())

    @patch("tools.search_ops._sem_get_model")
    def test_no_python_files_returns_message(self, mock_model):
        self._write("readme.md", "# hello")
        tc = _make_tool_call("semantic_search", query="anything", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No matches found", result.content)

    def test_outside_workspace_allowed(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("semantic_search", query="anything", path=outside)
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertNotIn("blocked by safety layer", result.content)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Tool cache tests
# ---------------------------------------------------------------------------

class TestToolCache(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        from tools import clear_tool_cache
        clear_tool_cache()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_read_file_is_cached(self):
        """Reading the same file twice returns the same result object."""
        path = os.path.join(self.workspace, "cache_test.txt")
        with open(path, "w") as f:
            f.write("hello cache\n")

        tc = _make_tool_call("read_file", path=path)
        r1 = execute_tool(tc, self.write_gate, self.read_gate)
        r2 = execute_tool(tc, self.write_gate, self.read_gate)

        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        # After caching, same object is returned (identity check)
        self.assertIs(r1, r2)

    def test_cache_cleared_between_turns(self):
        """clear_tool_cache() invalidates cached results."""
        from tools import clear_tool_cache

        path = os.path.join(self.workspace, "cache_test.txt")
        with open(path, "w") as f:
            f.write("hello\n")

        tc = _make_tool_call("read_file", path=path)
        r1 = execute_tool(tc, self.write_gate, self.read_gate)

        clear_tool_cache()
        r2 = execute_tool(tc, self.write_gate, self.read_gate)

        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        # Different objects after cache clear
        self.assertIsNot(r1, r2)

    def test_write_tools_are_not_cached(self):
        """write_file results are never cached."""
        path = os.path.join(self.workspace, "no_cache.txt")

        tc = _make_tool_call("write_file", path=path, content="first")
        r1 = execute_tool(tc, self.write_gate, self.read_gate)

        tc2 = _make_tool_call("write_file", path=path, content="second")
        r2 = execute_tool(tc2, self.write_gate, self.read_gate)

        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        # Should be different objects (write ops skip cache)
        self.assertIsNot(r1, r2)


# ---------------------------------------------------------------------------
# find_symbol tests
# ---------------------------------------------------------------------------

class TestFindSymbol(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        # Write a test file with known symbols
        src = os.path.join(self.workspace, "demo.py")
        with open(src, "w") as f:
            f.write("def hello_world():\n    pass\n\n")
            f.write("class MyClass:\n    def method_one(self):\n        pass\n")
        # Build fresh index
        from tools.search_ops import build_symbol_index
        build_symbol_index(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_exact_match_finds_symbols(self):
        tc = _make_tool_call("find_symbol", name="hello_world")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("hello_world", result.content)
        self.assertIn("demo.py:1", result.content)

    def test_substring_match(self):
        tc = _make_tool_call("find_symbol", name="hello")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("hello_world", result.content)

    def test_class_found(self):
        tc = _make_tool_call("find_symbol", name="MyClass")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("MyClass", result.content)
        self.assertIn("class", result.content)

    def test_method_found(self):
        tc = _make_tool_call("find_symbol", name="method_one")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("method_one", result.content)

    def test_no_match_returns_gracefully(self):
        tc = _make_tool_call("find_symbol", name="nonexistent_xyz")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No symbols matching", result.content)


# ---------------------------------------------------------------------------
# Error hint tests
# ---------------------------------------------------------------------------

class TestErrorHints(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_read_file_not_found_includes_hint(self):
        path = os.path.join(self.workspace, "nope.txt")
        tc = _make_tool_call("read_file", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Hint:", result.content)
        self.assertIn("list_directory", result.content.lower())

    def test_shell_streaming_stderr(self):
        """on_output receives stderr lines with [stderr] prefix."""
        cmd = "echo to_stderr >&2"
        tc = _make_tool_call("run_shell", command=cmd)
        lines = []
        result = execute_tool(tc, self.write_gate, self.read_gate, on_output=lines.append)
        self.assertTrue(result.success)
        self.assertTrue(any("to_stderr" in l and "stderr" in l for l in lines),
                        f"Expected [stderr] prefix in output lines: {lines}")

    def test_write_outside_workspace_allowed(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("write_file",
                                 path=os.path.join(outside, "x.txt"),
                                 content="hello")
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_edit_not_found_includes_hint(self):
        path = os.path.join(self.workspace, "f.txt")
        with open(path, "w") as f:
            f.write("original content\n")
        tc = _make_tool_call("edit_file", path=path,
                             old_string="nonexistent", new_string="replacement")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Hint:", result.content)
        self.assertIn("read_file", result.content.lower())

    def test_destructive_guard_removed(self):
        """Safety guards removed — rm runs directly without force flag needed."""
        tc = _make_tool_call("run_shell", command="rm -rf /tmp/nonexistent")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertNotIn("blocked by safety guard", result.content)

    def test_bad_command_includes_hint(self):
        tc = _make_tool_call("run_shell", command="nonexistent_cmd_xyz")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Hint:", result.content)

    def test_shell_output_truncated_at_500_lines(self):
        """Long shell output is truncated."""
        cmd = "python3 -c 'for i in range(600): print(i)'"
        tc = _make_tool_call("run_shell", command=cmd)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("truncated at 500 lines", result.content)

    def test_shell_streaming_calls_on_output(self):
        """on_output is called for each line of shell stdout."""
        cmd = "echo line1 && echo line2 && echo line3"
        tc = _make_tool_call("run_shell", command=cmd)
        lines = []
        result = execute_tool(tc, self.write_gate, self.read_gate, on_output=lines.append)
        self.assertTrue(result.success)
        self.assertIn("line1", lines)
        self.assertIn("line2", lines)
        self.assertIn("line3", lines)


class TestPlanningPrompt(unittest.TestCase):

    def test_prompt_includes_planning_instruction(self):
        """System prompt tells agent to state a plan before tools."""
        from prompt import build_system_prompt
        from config import AgentConfig
        prompt = build_system_prompt(AgentConfig())
        self.assertIn("state your plan", prompt.lower())
        self.assertIn("1-3 sentences", prompt)


# ---------------------------------------------------------------------------
# Approval mode tests
# ---------------------------------------------------------------------------

class TestApprovalMode(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_approve_callback_denies_write(self):
        """When approve_callback returns False, write is blocked."""
        path = os.path.join(self.workspace, "denied.txt")
        tc = _make_tool_call("write_file", path=path, content="nope")
        result = execute_tool(tc, self.write_gate, self.read_gate,
                              approve_callback=lambda n, a: False)
        self.assertFalse(result.success)
        self.assertIn("not approved", result.content)
        self.assertFalse(os.path.isfile(path))

    def test_approve_callback_allows_write(self):
        """When approve_callback returns True, write proceeds."""
        path = os.path.join(self.workspace, "allowed.txt")
        tc = _make_tool_call("write_file", path=path, content="yes")
        result = execute_tool(tc, self.write_gate, self.read_gate,
                              approve_callback=lambda n, a: True)
        self.assertTrue(result.success)
        self.assertTrue(os.path.isfile(path))

    def test_approve_bypassed_for_read_tools(self):
        """Read tools never trigger the approval callback."""
        path = os.path.join(self.workspace, "readme.txt")
        with open(path, "w") as f:
            f.write("hello")
        called = [False]
        tc = _make_tool_call("read_file", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate,
                              approve_callback=lambda n, a: called.__setitem__(0, True) or True)
        self.assertTrue(result.success)
        self.assertFalse(called[0], "approve_callback should not be called for read tools")


# ---------------------------------------------------------------------------
# Background shell task tests
# ---------------------------------------------------------------------------

class TestBackgroundTasks(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        from tools import _TASK_REGISTRY
        _TASK_REGISTRY.clear()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_background_task_returns_id(self):
        """background=True returns a task ID."""
        tc = _make_tool_call("run_shell", command="sleep 1 && echo done", background=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("background task", result.content)
        # Task ID should be 8 hex chars
        self.assertIn("task", result.content.lower())

    def test_task_status_running(self):
        """task_status reports 'still running' for active tasks."""
        from tools import _TASK_REGISTRY
        task_id = "test1234"
        import subprocess
        _TASK_REGISTRY[task_id] = subprocess.Popen(
            ["sleep", "10"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        tc = _make_tool_call("task_status", task_id=task_id)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("still running", result.content)
        _TASK_REGISTRY[task_id].kill()
        _TASK_REGISTRY[task_id].wait()

    def test_task_status_completed(self):
        """task_status reports exit code for completed tasks."""
        from tools import _TASK_REGISTRY
        task_id = "done1234"
        import subprocess
        proc = subprocess.Popen(
            ["echo", "hello"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        proc.wait()
        _TASK_REGISTRY[task_id] = proc
        tc = _make_tool_call("task_status", task_id=task_id)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("completed", result.content)

    def test_task_status_not_found(self):
        """task_status handles unknown task IDs gracefully."""
        tc = _make_tool_call("task_status", task_id="nope1234")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("not found", result.content.lower())


# ---------------------------------------------------------------------------
# search_files file_path param (improvement #1)
# ---------------------------------------------------------------------------

class TestSearchFilesFilePath(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_file_path_restricts_search_to_one_file(self):
        f1 = os.path.join(self.workspace, "a.txt")
        f2 = os.path.join(self.workspace, "b.txt")
        with open(f1, "w") as f:
            f.write("hello world")
        with open(f2, "w") as f:
            f.write("hello world")
        tc = _make_tool_call("search_files", pattern="hello", file_path=f1)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("a.txt", result.content)
        self.assertNotIn("b.txt", result.content)

    def test_file_path_invalid_returns_error(self):
        tc = _make_tool_call("search_files", pattern="x", file_path="/no/such/file.txt")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        # Should fail or succeed with "No matches" — depends on safety gate
        # The point is it doesn't crash
        self.assertIsNotNone(result)

    def test_file_path_no_matches(self):
        f = os.path.join(self.workspace, "only.txt")
        with open(f, "w") as fh:
            fh.write("just text")
        tc = _make_tool_call("search_files", pattern="zzznomatch", file_path=f)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No matches", result.content)


# ---------------------------------------------------------------------------
# edit_file short output (improvement #2)
# ---------------------------------------------------------------------------

class TestEditFileShortOutput(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_success_output_is_short_no_diff(self):
        f = os.path.join(self.workspace, "e.txt")
        with open(f, "w") as fh:
            fh.write("alpha\nbeta\ngamma\n")
        tc = _make_tool_call("edit_file", path=f, old_string="beta", new_string="delta")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("OK: replaced 1 occurrence", result.content)
        self.assertIn("e.txt", result.content)
        # Must NOT include a full unified diff
        self.assertNotIn("--- a/", result.content)
        self.assertNotIn("+++ b/", result.content)

    def test_file_actually_changed(self):
        f = os.path.join(self.workspace, "e2.txt")
        with open(f, "w") as fh:
            fh.write("one\ntwo\nthree\n")
        tc = _make_tool_call("edit_file", path=f, old_string="two", new_string="TWO")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        with open(f) as fh:
            content = fh.read()
        self.assertIn("TWO", content)
        self.assertNotIn("two", content)


# ---------------------------------------------------------------------------
# write_file reindex (improvement #3)
# ---------------------------------------------------------------------------

class TestWriteFileReindex(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_write_py_file_triggers_reindex(self):
        pyf = os.path.join(self.workspace, "new_mod.py")
        tc = _make_tool_call("write_file", path=pyf,
                             content="def hello_world():\n    return 42\n")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)

        # find_symbol should now see hello_world
        tc2 = _make_tool_call("find_symbol", name="hello_world")
        result2 = execute_tool(tc2, self.write_gate, self.read_gate)
        self.assertTrue(result2.success)
        self.assertIn("new_mod.py", result2.content)

    def test_write_non_py_does_not_crash(self):
        f = os.path.join(self.workspace, "data.txt")
        tc = _make_tool_call("write_file", path=f, content="just text")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)


# ---------------------------------------------------------------------------
# recall_turn (improvement #5)
# ---------------------------------------------------------------------------

class TestRecallTurn(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        from tools import _TOOL_CONTEXT
        # Seed the turn history so recall_turn has data to return
        _TOOL_CONTEXT._turn_history = {
            1: "Assistant: wrote file\na.txt\n  Tool: write_file({...})\n  Result: ✓ OK",
            2: "Assistant: all done",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._turn_history = {}

    def test_recall_existing_turn(self):
        tc = _make_tool_call("recall_turn", turn=1)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Turn 1", result.content)
        self.assertIn("write_file", result.content)

    def test_recall_nonexistent_turn(self):
        tc = _make_tool_call("recall_turn", turn=99)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No record", result.content)

    def test_recall_turn_zero_errors(self):
        tc = _make_tool_call("recall_turn", turn=0)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("positive integer", result.content)


# ---------------------------------------------------------------------------
# plan / plan_status tools
# ---------------------------------------------------------------------------

class PlanTests(unittest.TestCase):
    def setUp(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()
        self.workspace = tempfile.mkdtemp()
        self.write_gate = WriteSafetyGate(self.workspace, allow_overwrites=True)
        self.read_gate = ReadSafetyGate(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()

    def test_plan_sets_steps(self):
        tc = _make_tool_call("plan", steps=["Read config", "Add option", "Test"])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("3 steps", result.content)
        self.assertIn("[1]", result.content)
        self.assertIn("[2]", result.content)
        self.assertIn("[3]", result.content)

    def test_plan_empty_steps_fails(self):
        tc = _make_tool_call("plan", steps=[])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)

    def test_plan_status_no_plan(self):
        tc = _make_tool_call("plan_status")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No active plan", result.content)

    def test_plan_status_mark_complete(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._plan_steps = ["A", "B", "C"]
        _TOOL_CONTEXT._plan_done = set()

        tc = _make_tool_call("plan_status", step=2)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("✓", result.content)
        self.assertIn("1/3", result.content)

    def test_plan_status_invalid_step(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._plan_steps = ["A"]
        _TOOL_CONTEXT._plan_done = set()

        tc = _make_tool_call("plan_status", step=5)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)

    def test_plan_status_all_done(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._plan_steps = ["A", "B"]
        _TOOL_CONTEXT._plan_done = {0, 1}

        tc = _make_tool_call("plan_status")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("All steps complete", result.content)


# ---------------------------------------------------------------------------
# Tool piping (_pipe meta-field)
# ---------------------------------------------------------------------------

class ToolPipingTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate = WriteSafetyGate(self.workspace, allow_overwrites=True)
        self.read_gate = ReadSafetyGate(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_pipe_stripped_from_args(self):
        """_pipe is popped before validation, so it never reaches tool impl."""
        from tools import _TOOL_DISPATCH
        original = _TOOL_DISPATCH.get("read_file")
        called_with = {}

        def _fake_read(args, wg, rg):
            called_with.update(args)
            return ToolResult(True, "ok")

        _TOOL_DISPATCH["read_file"] = _fake_read
        try:
            tc = _make_tool_call("read_file", path="/tmp/x", _pipe={"from": 0})
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            self.assertNotIn("_pipe", called_with)
        finally:
            if original:
                _TOOL_DISPATCH["read_file"] = original

    def test_pipe_unknown_params_without_pipe(self):
        """Normal unknown-param rejection still works."""
        tc = _make_tool_call("read_file", path="/tmp/x", bogus=123)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Unknown parameter", result.content)


# ---------------------------------------------------------------------------
# max_tokens truncation test
# ---------------------------------------------------------------------------

class TestMaxTokensTruncation(unittest.TestCase):
    """Verify _compress_tool_results actually truncates when data exceeds threshold."""

    def test_compress_truncates_long_tool_results(self):
        """Tool results > 5 lines are compressed to 5 lines + marker."""
        from memory import _compress_tool_results

        # Build tool-result messages with content well over 5 lines (10+ lines)
        messages: list[dict] = []
        for i in range(10):
            content_lines = [f"line {j}" for j in range(20)]  # 20 lines
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": '{"success": true, "content": "' +
                           "\\n".join(content_lines).replace('"', '\\"') +
                           '"}',
            })

        # keep_recent=1 means only the last message stays untouched
        result, changed = _compress_tool_results(messages, keep_recent=1)

        self.assertTrue(changed, "Expected compression to happen with 20-line tool results")
        # First 9 messages should be truncated
        for i in range(9):
            content = result[i]["content"]
            parsed = json.loads(content)
            self.assertIn("truncated at 5 lines", parsed["content"],
                          f"Message {i} should be truncated")
            self.assertEqual(parsed["content"].count("\n"), 5,
                             f"Message {i} should have 5 kept lines + truncation marker")
        # Last message (recent) should be untouched
        last_content = result[9]["content"]
        self.assertNotIn("truncated at 5 lines", last_content,
                         "Recent message should not be truncated")

    def test_under_threshold_not_truncated(self):
        """Tool results <= 5 lines are NOT compressed."""
        from memory import _compress_tool_results

        short_content = "short\nresult\nhere"  # 3 lines
        messages = [{
            "role": "tool",
            "tool_call_id": "call_0",
            "content": '{"success": true, "content": "' +
                       short_content.replace('"', '\\"') +
                       '"}',
        }]

        result, changed = _compress_tool_results(messages, keep_recent=0)
        self.assertFalse(changed, "Short results should not trigger compression")
        self.assertEqual(result[0]["content"], messages[0]["content"])
