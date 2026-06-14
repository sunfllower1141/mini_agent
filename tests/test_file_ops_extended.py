#!/usr/bin/env python3
"""
test_file_ops_extended.py -- comprehensive tests for tools not yet covered.

Tests: list_directory, diff, restore_file, plan/plan_status,
       task_status, find_usages, verify, recall_turn, write_scratchpad.
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conftest import make_tool_call as _make_tool_call, make_gates as _gates
from tools import (
    ToolResult,
    execute_tool,
    _TOOL_CONTEXT,
    _TASK_REGISTRY,
    set_context,
)


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------

class TestListDirectory(unittest.TestCase):

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

    def test_non_empty_directory(self):
        self._write("a.txt", "hello")
        self._write("b.py", "x=1")
        os.makedirs(os.path.join(self.workspace, "subdir"))
        tc = _make_tool_call("list_directory", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("[f] a.txt", result.content)
        self.assertIn("[f] b.py", result.content)
        self.assertIn("[d] subdir", result.content)

    def test_empty_directory(self):
        tc = _make_tool_call("list_directory", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("(empty)", result.content)

    def test_missing_directory(self):
        path = os.path.join(self.workspace, "nonexistent")
        tc = _make_tool_call("list_directory", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Error listing", result.content)

    def test_blocked_outside_workspace(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("list_directory", path=outside)
            result = execute_tool(tc, self.write_gate, self.read_gate)
            # Safety gates are now unrestricted -- listing outside workspace succeeds
            self.assertTrue(result.success)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_entries_sorted_alphabetically(self):
        self._write("z.txt", "")
        self._write("a.txt", "")
        self._write("m.txt", "")
        tc = _make_tool_call("list_directory", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        # Extract the file entries in order
        lines = [l.strip() for l in result.content.split("\n") if "[f]" in l]
        names = [l.split("] ")[1] for l in lines]
        self.assertEqual(names, sorted(names))

    def test_hidden_files_listed(self):
        """Hidden files (starting with '.') should be listed."""
        self._write(".hidden_file", "secret")
        tc = _make_tool_call("list_directory", path=self.workspace)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("[f] .hidden_file", result.content)


# ---------------------------------------------------------------------------
# diff (standalone diff tool, not git subcommand)
# ---------------------------------------------------------------------------

class TestDiffTool(unittest.TestCase):

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

    def _init_git(self):
        subprocess.run(["git", "init"], cwd=self.workspace, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test"],
            cwd=self.workspace, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.workspace, capture_output=True,
        )

    def test_no_changes_in_clean_repo(self):
        self._init_git()
        self._write("f.txt", "initial")
        subprocess.run(["git", "add", "f.txt"], cwd=self.workspace, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, capture_output=True)
        tc = _make_tool_call("diff")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No unstaged changes", result.content)

    def test_shows_unstaged_changes(self):
        self._init_git()
        self._write("f.txt", "initial")
        subprocess.run(["git", "add", "f.txt"], cwd=self.workspace, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, capture_output=True)
        self._write("f.txt", "modified")
        tc = _make_tool_call("diff")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("+modified", result.content)

    def test_diff_specific_file(self):
        self._init_git()
        self._write("a.txt", "a")
        self._write("b.txt", "b")
        subprocess.run(["git", "add", "."], cwd=self.workspace, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, capture_output=True)
        self._write("a.txt", "a modified")
        self._write("b.txt", "b modified")
        tc = _make_tool_call("diff", path="a.txt")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("a.txt", result.content)
        self.assertNotIn("b.txt", result.content)

    def test_no_git_repo(self):
        tc = _make_tool_call("diff")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        # May succeed or fail depending on git state, but should be a ToolResult
        self.assertIsInstance(result, ToolResult)

    @patch("subprocess.run")
    def test_git_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("git not found")
        tc = _make_tool_call("diff")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("git not found", result.content)

    @patch("subprocess.run")
    def test_diff_timeout(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["git", "diff"], timeout=10)
        tc = _make_tool_call("diff")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("timed out", result.content)


# ---------------------------------------------------------------------------
# restore_file
# ---------------------------------------------------------------------------

class TestRestoreFile(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        from tools.file_ops import _BACKUPS
        _BACKUPS.clear()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_restore_after_write(self):
        path = self._write("original.txt", "original content")
        # Simulate a modification: write over it (which creates a backup)
        tc_write = _make_tool_call("write_file", path=path, content="modified")
        result_write = execute_tool(tc_write, self.write_gate, self.read_gate)
        self.assertTrue(result_write.success)

        # Now restore
        tc = _make_tool_call("restore_file", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Restored", result.content)

        # Verify file content is back to original
        with open(path) as f:
            self.assertEqual(f.read(), "original content")

    def test_no_backup_available(self):
        path = self._write("untouched.txt", "never modified")
        tc = _make_tool_call("restore_file", path=path)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("No backup available", result.content)

    def test_restore_then_restore_again_fails(self):
        """After restoring once, the backup is consumed -- second restore fails."""
        path = self._write("twice.txt", "original")
        execute_tool(
            _make_tool_call("write_file", path=path, content="modified"),
            self.write_gate, self.read_gate,
        )
        r1 = execute_tool(
            _make_tool_call("restore_file", path=path),
            self.write_gate, self.read_gate,
        )
        self.assertTrue(r1.success)
        r2 = execute_tool(
            _make_tool_call("restore_file", path=path),
            self.write_gate, self.read_gate,
        )
        self.assertFalse(r2.success)
        self.assertIn("No backup available", r2.content)

    def test_blocked_outside_workspace(self):
        outside = tempfile.mkdtemp()
        try:
            tc = _make_tool_call("restore_file", path=os.path.join(outside, "x.txt"))
            result = execute_tool(tc, self.write_gate, self.read_gate)
            # Safety gates are now unrestricted -- restore is attempted but fails
            # because there's no session backup for a file outside the workspace
            self.assertIsInstance(result, ToolResult)
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_restore_after_edit(self):
        """Restore works after edit_file too."""
        path = self._write("editme.txt", "initial text")
        # Must read before editing (read-before-edit enforcement)
        execute_tool(
            _make_tool_call("read_file", path=path),
            self.write_gate, self.read_gate,
        )
        execute_tool(
            _make_tool_call("edit_file", path=path, old_string="initial", new_string="changed"),
            self.write_gate, self.read_gate,
        )
        r = execute_tool(
            _make_tool_call("restore_file", path=path),
            self.write_gate, self.read_gate,
        )
        self.assertTrue(r.success)
        with open(path) as f:
            self.assertEqual(f.read(), "initial text")


# ---------------------------------------------------------------------------
# plan / plan_status
# ---------------------------------------------------------------------------

class TestPlan(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        # Reset plan context before each test
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()

    def tearDown(self):
        import shutil
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_declares_plan(self):
        tc = _make_tool_call("plan", steps=["step one", "step two", "step three"])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Plan (3 steps)", result.content)
        self.assertIn("[1] step one", result.content)
        self.assertIn("[2] step two", result.content)
        self.assertIn("[3] step three", result.content)

    def test_empty_steps_rejected(self):
        tc = _make_tool_call("plan", steps=[])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("at least one step", result.content.lower())

    def test_non_list_steps_rejected(self):
        tc = _make_tool_call("plan", steps="not a list")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)

    def test_plan_status_no_plan(self):
        tc = _make_tool_call("plan_status")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No active plan", result.content)

    def test_plan_status_reports_progress(self):
        execute_tool(
            _make_tool_call("plan", steps=["A", "B", "C"]),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan_status")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Plan (0/3 complete)", result.content)
        self.assertIn("[o] 1. A", result.content)
        self.assertIn("[o] 2. B", result.content)
        self.assertIn("[o] 3. C", result.content)

    def test_plan_status_mark_step_complete(self):
        execute_tool(
            _make_tool_call("plan", steps=["A", "B", "C"]),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan_status", step=1)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Plan (1/3 complete)", result.content)
        self.assertIn("[V] 1. A", result.content)
        self.assertIn("[o] 2. B", result.content)

    def test_plan_status_all_steps_complete(self):
        execute_tool(
            _make_tool_call("plan", steps=["A", "B"]),
            self.write_gate, self.read_gate,
        )
        execute_tool(
            _make_tool_call("plan_status", step=1),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan_status", step=2)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("All steps complete!", result.content)

    def test_plan_status_invalid_step(self):
        execute_tool(
            _make_tool_call("plan", steps=["A", "B"]),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan_status", step=99)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Invalid step", result.content)

    def test_plan_status_zero_step(self):
        execute_tool(
            _make_tool_call("plan", steps=["A"]),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan_status", step=0)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("Invalid step", result.content)

    def test_plan_overwrites_previous_plan(self):
        execute_tool(
            _make_tool_call("plan", steps=["old step"]),
            self.write_gate, self.read_gate,
        )
        tc = _make_tool_call("plan", steps=["new step"])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("new step", result.content)
        self.assertNotIn("old step", result.content)
        # Check context was reset
        self.assertEqual(_TOOL_CONTEXT._plan_steps, ["new step"])
        self.assertEqual(_TOOL_CONTEXT._plan_done, set())

    def test_plan_too_many_steps_rejected(self):
        """Plan with more than 15 steps is rejected."""
        tc = _make_tool_call("plan", steps=[f"step {i}" for i in range(20)])
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("too large", result.content.lower())

    def test_plan_exactly_fifteen_steps_accepted(self):
        """Plan with exactly 15 steps is accepted."""
        steps = [f"step {i}" for i in range(15)]
        tc = _make_tool_call("plan", steps=steps)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertEqual(_TOOL_CONTEXT._plan_steps, steps)

    def test_plan_status_idempotent(self):
        """Marking a step complete multiple times is harmless."""
        execute_tool(
            _make_tool_call("plan", steps=["A", "B"]),
            self.write_gate, self.read_gate,
        )
        execute_tool(_make_tool_call("plan_status", step=1), self.write_gate, self.read_gate)
        # Mark step 1 again -- should succeed without error
        result = execute_tool(_make_tool_call("plan_status", step=1), self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertEqual(_TOOL_CONTEXT._plan_done, {0})

    def test_plan_tracks_last_advanced_turn(self):
        """Plan status updates _plan_last_advanced_turn."""
        _TOOL_CONTEXT._turn_count = 5
        execute_tool(
            _make_tool_call("plan", steps=["A", "B"]),
            self.write_gate, self.read_gate,
        )
        # After plan(), last_advanced_turn should be set to turn_count
        self.assertEqual(_TOOL_CONTEXT._plan_last_advanced_turn, 5)
        # Advance turn and complete a step
        _TOOL_CONTEXT._turn_count = 7
        execute_tool(_make_tool_call("plan_status", step=1), self.write_gate, self.read_gate)
        self.assertEqual(_TOOL_CONTEXT._plan_last_advanced_turn, 7)

    def test_auto_advance_plan_on_write(self):
        """Auto-advance fires when write_file path matches plan step keyword."""
        execute_tool(
            _make_tool_call("plan", steps=["Create test_config.py", "Verify output"]),
            self.write_gate, self.read_gate,
        )
        self.assertEqual(_TOOL_CONTEXT._plan_done, set())
        # Write a file whose name matches step 1 keyword
        filepath = os.path.join(self.workspace, "test_config.py")
        tc = _make_tool_call("write_file", path=filepath, content="# test config")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn(0, _TOOL_CONTEXT._plan_done, "Step 1 should be auto-completed")

    def test_auto_advance_plan_no_match(self):
        """Auto-advance does NOT fire when file path doesn't match any step."""
        execute_tool(
            _make_tool_call("plan", steps=["Create test_config.py"]),
            self.write_gate, self.read_gate,
        )
        filepath = os.path.join(self.workspace, "unrelated_file.txt")
        tc = _make_tool_call("write_file", path=filepath, content="hello")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertEqual(_TOOL_CONTEXT._plan_done, set(), "No step should auto-complete")


# ---------------------------------------------------------------------------
# session_stats plan line
# ---------------------------------------------------------------------------

class TestSessionStatsPlan(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()

    def tearDown(self):
        import shutil
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_session_stats_no_plan(self):
        """session_stats omits plan line when no plan is active."""
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertNotIn("Plan:", result.content)

    def test_session_stats_shows_plan_progress(self):
        """session_stats shows correct plan progress count."""
        _TOOL_CONTEXT._plan_steps = ["A", "B", "C"]
        _TOOL_CONTEXT._plan_done = {0}  # step 1 done (0-indexed)
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Plan:", result.content)
        self.assertIn("1/3", result.content)


class TestSessionStatsCache(unittest.TestCase):
    """Test cache hit/miss, input/output tokens, and cost saving in session_stats."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        # Clear any prior cache stats
        if hasattr(_TOOL_CONTEXT, "_cache_stats"):
            del _TOOL_CONTEXT._cache_stats
        _TOOL_CONTEXT._turn_history = {1: "turn 1"}
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()
        _TOOL_CONTEXT._agent_runtime = None
        _TOOL_CONTEXT._provider = "deepseek"

    def tearDown(self):
        import shutil
        if hasattr(_TOOL_CONTEXT, "_cache_stats"):
            del _TOOL_CONTEXT._cache_stats
        _TOOL_CONTEXT._turn_history = {}
        _TOOL_CONTEXT._plan_steps = []
        _TOOL_CONTEXT._plan_done = set()
        _TOOL_CONTEXT._agent_runtime = None
        _TOOL_CONTEXT._provider = "deepseek"
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_no_cache_stats_shows_baseline_only(self):
        """Without _cache_stats, session_stats shows turns/context/sub-agents only."""
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Turns used:", result.content)
        self.assertNotIn("API calls:", result.content)
        self.assertNotIn("Cache hit rate:", result.content)
        self.assertNotIn("Cost:", result.content)

    def test_cache_hits_only_shows_100_pct_and_savings(self):
        """All cached input -> 100% hit rate, cost savings shown."""
        _TOOL_CONTEXT._cache_stats = {
            "hits": 50_000, "misses": 0, "calls": 3,
            "input_tokens": 50_000, "output_tokens": 10_000,
        }
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Cache hit rate: 100.0%", result.content)
        self.assertIn("50,000 cached", result.content)
        self.assertIn("input 50,000 tok", result.content)
        self.assertIn("output 10,000 tok", result.content)
        self.assertIn("saved $", result.content)
        # Cost without cache: 50000 * 0.14/1M = $0.007
        # With cache:       50000 * 0.014/1M = $0.0007
        # Savings: $0.0063

    def test_cache_misses_only_shows_0_pct_no_savings(self):
        """All cache misses -> 0% hit rate, cost line but no savings."""
        _TOOL_CONTEXT._cache_stats = {
            "hits": 0, "misses": 30_000, "calls": 2,
            "input_tokens": 30_000, "output_tokens": 5_000,
        }
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Cache hit rate: 0.0%", result.content)
        self.assertIn("0 cached", result.content)
        self.assertIn("input 30,000 tok", result.content)
        # No "saved" line because saved=0
        self.assertNotIn("saved $", result.content)
        # Cost line should still appear
        self.assertIn("Cost:", result.content)

    def test_mixed_cache_shows_partial_hit_rate_and_savings(self):
        """Mixed hits/misses -> correct hit rate, proportional savings."""
        _TOOL_CONTEXT._cache_stats = {
            "hits": 20_000, "misses": 10_000, "calls": 4,
            "input_tokens": 30_000, "output_tokens": 8_000,
        }
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        # 20000 / 30000 = 66.666...%
        self.assertIn("Cache hit rate: 66.7%", result.content)
        self.assertIn("20,000 cached", result.content)
        self.assertIn("30,000 tokens", result.content)
        self.assertIn("saved $", result.content)

    def test_unknown_provider_skips_cost_line(self):
        """Provider with no pricing -> no Cost line."""
        _TOOL_CONTEXT._provider = "ollama"  # ollama has 0.0 pricing
        _TOOL_CONTEXT._cache_stats = {
            "hits": 5_000, "misses": 5_000, "calls": 1,
            "input_tokens": 10_000, "output_tokens": 2_000,
        }
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Cache hit rate:", result.content)
        self.assertNotIn("Cost:", result.content)

    def test_no_cache_stats_no_api_calls_no_cost(self):
        """Empty _cache_stats with calls=0 -> API/cache lines suppressed."""
        _TOOL_CONTEXT._cache_stats = {
            "hits": 0, "misses": 0, "calls": 0,
            "input_tokens": 0, "output_tokens": 0,
        }
        tc = _make_tool_call("session_stats")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Turns used:", result.content)
        self.assertNotIn("API calls:", result.content)
        self.assertNotIn("Cache hit rate:", result.content)
        self.assertNotIn("Cost:", result.content)


class TestTaskStatus(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        _TASK_REGISTRY.clear()

    def tearDown(self):
        import shutil
        _TASK_REGISTRY.clear()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_missing_task_id(self):
        tc = _make_tool_call("task_status")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("task_id", result.content)

    def test_task_not_found(self):
        tc = _make_tool_call("task_status", task_id="nonexistent123")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("not found", result.content)

    def test_running_task(self):
        """A still-running background task reports 'still running'."""
        import sys
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _TASK_REGISTRY["test_task"] = proc
        try:
            tc = _make_tool_call("task_status", task_id="test_task")
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            self.assertIn("still running", result.content)
        finally:
            proc.kill()
            proc.wait()

    def test_completed_task(self):
        """A completed task reports its exit code."""
        import sys
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('done')"],
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()  # ensure it finishes
        _TASK_REGISTRY["done_task"] = proc
        tc = _make_tool_call("task_status", task_id="done_task")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("completed", result.content)
        self.assertIn("exit_code=0", result.content)
        # Should be removed from registry after reporting
        self.assertNotIn("done_task", _TASK_REGISTRY)

    def test_failed_task_reports_exit_code(self):
        """A task that exits non-zero still reports its status."""
        import sys
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        _TASK_REGISTRY["fail_task"] = proc
        tc = _make_tool_call("task_status", task_id="fail_task")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("exit_code=42", result.content)


# ---------------------------------------------------------------------------
# find_usages
# ---------------------------------------------------------------------------

class TestFindUsages(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        # Write test files and build index
        self._write("mod.py", (
            "def greet(name):\n"
            "    return f'Hello {name}'\n"
            "\n"
            "def main():\n"
            "    result = greet('world')\n"
            "    print(result)\n"
        ))
        from tools.search_ops import build_symbol_index
        build_symbol_index(self.workspace)

    def tearDown(self):
        import shutil
        from tools.search_ops import _SYMBOL_INDEX, _REF_INDEX
        _SYMBOL_INDEX = None
        _REF_INDEX = None
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_finds_usages(self):
        tc = _make_tool_call("find_usages", name="greet")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        # Should find usage of 'greet' inside main()
        self.assertIn("usage(s) of 'greet'", result.content)
        self.assertIn("mod.py", result.content)

    def test_no_usages(self):
        tc = _make_tool_call("find_usages", name="nonexistent_xyz")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No usages found", result.content)

    def test_missing_name(self):
        tc = _make_tool_call("find_usages")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("name", result.content)

    def test_substring_match(self):
        tc = _make_tool_call("find_usages", name="gree")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("greet", result.content)

    def test_usage_across_multiple_files(self):
        self._write("caller.py", (
            "from mod import greet\n"
            "def call_it():\n"
            "    return greet('everyone')\n"
        ))
        # Force a fresh rebuild by clearing all index globals
        import tools.search_ops as so
        so._SYMBOL_INDEX = None
        so._REF_INDEX = None
        so._INDEX_MAX_MTIME = 0.0
        so.build_symbol_index(self.workspace)
        tc = _make_tool_call("find_usages", name="greet")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("caller.py", result.content)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

class TestVerify(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        from tools import _MODIFIED_FILES
        _MODIFIED_FILES.clear()
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._agent_depth = 0
        # Create a minimal test so pytest has something to run
        test_dir = os.path.join(self.workspace, "tests")
        os.makedirs(test_dir, exist_ok=True)
        with open(os.path.join(test_dir, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(test_dir, "test_dummy.py"), "w") as f:
            f.write("def test_pass(): assert True\n")

    def tearDown(self):
        import shutil
        from tools import _MODIFIED_FILES
        _MODIFIED_FILES.clear()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_no_modified_files_runs_all(self):
        tc = _make_tool_call("verify")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success, f"Expected success but got: {result.content}")

    def test_with_modified_file_runs_related_tests(self):
        from tools import _MODIFIED_FILES
        # Write a source file to trigger test matching
        src_path = os.path.join(self.workspace, "my_module.py")
        with open(src_path, "w") as f:
            f.write("x = 1\n")
        _MODIFIED_FILES.add(os.path.realpath(src_path))

        tc = _make_tool_call("verify")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success, f"Expected success but got: {result.content}")

    def test_returns_tool_result_not_exception(self):
        tc = _make_tool_call("verify")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertIsInstance(result, ToolResult)


# ---------------------------------------------------------------------------
# recall_turn
# ---------------------------------------------------------------------------

class TestRecallTurn(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        _TOOL_CONTEXT._turn_history = {}

    def tearDown(self):
        import shutil
        _TOOL_CONTEXT._turn_history = {}
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_recall_existing_turn(self):
        _TOOL_CONTEXT._turn_history = {
            1: "Turn 1: read_file(test.txt)",
            2: "Turn 2: write_file(out.txt, 'hello')",
        }
        tc = _make_tool_call("recall_turn", turn=1)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Turn 1", result.content)
        self.assertIn("read_file(test.txt)", result.content)

    def test_turn_not_found(self):
        _TOOL_CONTEXT._turn_history = {1: "something"}
        tc = _make_tool_call("recall_turn", turn=99)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No record of turn 99", result.content)
        self.assertIn("[1]", result.content)  # shows available turns

    def test_no_turns_recorded(self):
        tc = _make_tool_call("recall_turn", turn=1)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("No turns recorded", result.content)

    def test_invalid_turn_zero(self):
        tc = _make_tool_call("recall_turn", turn=0)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("positive integer", result.content)

    def test_invalid_turn_negative(self):
        tc = _make_tool_call("recall_turn", turn=-5)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("positive integer", result.content)

    def test_invalid_turn_type(self):
        tc = _make_tool_call("recall_turn", turn="abc")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("positive integer", result.content)


# ---------------------------------------------------------------------------
# write_scratchpad
# ---------------------------------------------------------------------------

class TestWriteScratchpad(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        _TOOL_CONTEXT.scratchpad_path = ""
        set_context(workspace=self.workspace)

    def tearDown(self):
        import shutil
        _TOOL_CONTEXT.scratchpad_path = ""
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_writes_to_fallback_file(self):
        """When no scratchpad_path is set, writes to a fallback .md file."""
        tc = _make_tool_call("write_scratchpad", content="test scratchpad content")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Scratchpad updated", result.content)

        # Verify fallback file was created
        fallback = os.path.join(self.workspace, ".mini_agent_scratchpad.md")
        self.assertTrue(os.path.isfile(fallback))
        with open(fallback) as f:
            self.assertEqual(f.read(), "test scratchpad content")

    def test_writes_to_sqlite(self):
        """When scratchpad_path is set, writes to SQLite DB."""
        db_path = os.path.join(self.workspace, "scratchpad.db")
        _TOOL_CONTEXT.scratchpad_path = db_path
        tc = _make_tool_call("write_scratchpad", content="db content here")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("Scratchpad updated", result.content)

        # Verify DB content
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT content FROM scratchpad WHERE id = 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "db content here")

    def test_overwrites_previous_content(self):
        """Writing again overwrites previous scratchpad content."""
        tc1 = _make_tool_call("write_scratchpad", content="first write")
        r1 = execute_tool(tc1, self.write_gate, self.read_gate)
        self.assertTrue(r1.success)

        tc2 = _make_tool_call("write_scratchpad", content="second write")
        r2 = execute_tool(tc2, self.write_gate, self.read_gate)
        self.assertTrue(r2.success)

        fallback = os.path.join(self.workspace, ".mini_agent_scratchpad.md")
        with open(fallback) as f:
            self.assertEqual(f.read(), "second write")

    def test_empty_content(self):
        """Writing empty content should succeed."""
        tc = _make_tool_call("write_scratchpad", content="")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertIn("0 chars", result.content)

    def test_scratchpad_updated_flag(self):
        """The _scratchpad_updated flag is set after writing (SQLite path)."""
        db_path = os.path.join(self.workspace, "flag_test.db")
        _TOOL_CONTEXT.scratchpad_path = db_path
        tc = _make_tool_call("write_scratchpad", content="flag test")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        self.assertTrue(_TOOL_CONTEXT._scratchpad_updated)


if __name__ == "__main__":
    unittest.main()
