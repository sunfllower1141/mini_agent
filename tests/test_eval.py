#!/usr/bin/env python3
"""Tests for the Agent Evaluation Harness (eval module).

Covers: task YAML parsing, all 8 checker types, runner with trivial tasks,
metrics collection, and suite aggregation.
"""

import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# Ensure eval is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.scorer import CheckResult, run_checks
from eval.metrics import MetricsCollector
from eval.runner import (
    EvalTask,
    EvalResult,
    SuiteReport,
    parse_task_from_yaml,
    load_tasks,
    run_task,
)


# ---------------------------------------------------------------------------
# 1. Task YAML parsing
# ---------------------------------------------------------------------------


class TestTaskYAMLParsing:
    """Verify task YAML is parsed into EvalTask dataclasses correctly."""

    def test_parse_minimal_task(self):
        yaml_text = """
id: "test-task"
name: "Test Task"
description: "Do something."
category: "feature"
difficulty: "easy"
checks:
  - type: "file_exists"
    path: "foo.py"
"""
        task = parse_task_from_yaml(yaml_text)
        assert task.id == "test-task"
        assert task.name == "Test Task"
        assert task.description == "Do something."
        assert task.category == "feature"
        assert task.difficulty == "easy"
        assert len(task.checks) == 1
        assert task.checks[0]["type"] == "file_exists"
        assert task.checks[0]["path"] == "foo.py"
        assert task.workspace_fixture is None
        assert task.tags == []

    def test_parse_task_with_fixture_and_tags(self):
        yaml_text = """
id: "bugfix-task"
name: "Fix Bug"
description: "Fix the bug."
category: "bugfix"
difficulty: "medium"
workspace_fixture: "mini_repo"
checks:
  - type: "file_contains"
    path: "counter.py"
    pattern: "range\\\\(1, n \\\\+ 1\\\\)"
expected_tools:
  - "edit_file"
expected_turns_max: 8
tags: ["bugfix", "basics"]
"""
        task = parse_task_from_yaml(yaml_text)
        assert task.id == "bugfix-task"
        assert task.workspace_fixture == "mini_repo"
        assert task.expected_tools == ["edit_file"]
        assert task.expected_turns_max == 8
        assert task.tags == ["bugfix", "basics"]

    def test_load_tasks_from_directory(self):
        tasks_dir = os.path.join(os.path.dirname(__file__), "eval", "tasks")
        if not os.path.isdir(tasks_dir):
            pytest.skip("eval/tasks/ fixture directory not present")
        tasks = load_tasks(tasks_dir)
        assert len(tasks) >= 3
        task_ids = {t.id for t in tasks}
        assert "add-hello-world" in task_ids

    def test_load_tasks_filter_by_id(self):
        tasks_dir = os.path.join(os.path.dirname(__file__), "eval", "tasks")
        if not os.path.isdir(tasks_dir):
            pytest.skip("eval/tasks/ fixture directory not present")
        tasks = load_tasks(tasks_dir, task_id="add-hello-world")
        assert len(tasks) == 1
        assert tasks[0].id == "add-hello-world"

    def test_load_tasks_filter_by_tag(self):
        tasks_dir = os.path.join(os.path.dirname(__file__), "eval", "tasks")
        if not os.path.isdir(tasks_dir):
            pytest.skip("eval/tasks/ fixture directory not present")
        tasks = load_tasks(tasks_dir, tags=["bugfix"])
        assert len(tasks) >= 1
        for t in tasks:
            assert "bugfix" in t.tags

    def test_load_tasks_filter_by_difficulty(self):
        tasks_dir = os.path.join(os.path.dirname(__file__), "eval", "tasks")
        if not os.path.isdir(tasks_dir):
            pytest.skip("eval/tasks/ fixture directory not present")
        tasks = load_tasks(tasks_dir, difficulty="easy")
        assert len(tasks) >= 1
        for t in tasks:
            assert t.difficulty == "easy"


# ---------------------------------------------------------------------------
# 2. Checker types
# ---------------------------------------------------------------------------


class TestCheckerFileExists:
    """Test file_exists and file_not_exists checkers."""

    def test_file_exists_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "hello.py")
            with open(path, "w") as f:
                f.write("print('hi')")
            results = run_checks(
                [{"type": "file_exists", "path": "hello.py"}], ws
            )
            assert results[0].passed
            assert results[0].check_type == "file_exists"

    def test_file_exists_fail(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [{"type": "file_exists", "path": "nope.py"}], ws
            )
            assert not results[0].passed

    def test_file_not_exists_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [{"type": "file_not_exists", "path": "gone.txt"}], ws
            )
            assert results[0].passed

    def test_file_not_exists_fail(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "stay.txt")
            with open(path, "w") as f:
                f.write("here")
            results = run_checks(
                [{"type": "file_not_exists", "path": "stay.txt"}], ws
            )
            assert not results[0].passed


class TestCheckerFileContains:
    """Test file_contains and file_not_contains checkers."""

    def test_file_contains_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "mod.py")
            with open(path, "w") as f:
                f.write("def hello_world(name):\n    return f'Hello, {name}!'")
            results = run_checks(
                [{"type": "file_contains", "path": "mod.py", "pattern": "def hello_world"}],
                ws,
            )
            assert results[0].passed

    def test_file_contains_fail(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "mod.py")
            with open(path, "w") as f:
                f.write("def goodbye():\n    pass")
            results = run_checks(
                [{"type": "file_contains", "path": "mod.py", "pattern": "def hello_world"}],
                ws,
            )
            assert not results[0].passed

    def test_file_contains_file_missing(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [{"type": "file_contains", "path": "absent.py", "pattern": "x"}], ws
            )
            assert not results[0].passed
            assert "missing" in results[0].detail

    def test_file_not_contains_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "clean.py")
            with open(path, "w") as f:
                f.write("def foo():\n    pass")
            results = run_checks(
                [{"type": "file_not_contains", "path": "clean.py", "pattern": "TODO"}],
                ws,
            )
            assert results[0].passed

    def test_file_not_contains_fail(self):
        with tempfile.TemporaryDirectory() as ws:
            path = os.path.join(ws, "dirty.py")
            with open(path, "w") as f:
                f.write("# TODO: fix this")
            results = run_checks(
                [{"type": "file_not_contains", "path": "dirty.py", "pattern": "TODO"}],
                ws,
            )
            assert not results[0].passed


class TestCheckerTestPasses:
    """Test the test_passes checker."""

    def test_test_passes_success(self):
        with tempfile.TemporaryDirectory() as ws:
            # Create a test file that always passes
            test_path = os.path.join(ws, "test_simple.py")
            with open(test_path, "w") as f:
                f.write("def test_always_pass():\n    assert True\n")
            results = run_checks(
                [{"type": "test_passes", "path": "test_simple.py"}], ws
            )
            assert results[0].passed
            assert results[0].check_type == "test_passes"

    def test_test_passes_failure(self):
        with tempfile.TemporaryDirectory() as ws:
            test_path = os.path.join(ws, "test_fail.py")
            with open(test_path, "w") as f:
                f.write("def test_always_fail():\n    assert False\n")
            results = run_checks(
                [{"type": "test_passes", "path": "test_fail.py"}], ws
            )
            assert not results[0].passed


class TestCheckerDiff:
    """Test diff_contains and diff_not_contains checkers."""

    def test_diff_contains_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            _init_git(ws)
            path = os.path.join(ws, "file.txt")
            with open(path, "w") as f:
                f.write("new content")
            _git_add_commit(ws, "add file")
            # Make a change
            with open(path, "a") as f:
                f.write("\nmore content")
            results = run_checks(
                [{"type": "diff_contains", "fragment": "more content"}], ws
            )
            assert results[0].passed

    def test_diff_contains_fail(self):
        with tempfile.TemporaryDirectory() as ws:
            _init_git(ws)
            path = os.path.join(ws, "file.txt")
            with open(path, "w") as f:
                f.write("hello")
            _git_add_commit(ws, "add file")
            with open(path, "a") as f:
                f.write("world")
            results = run_checks(
                [{"type": "diff_contains", "fragment": "NOT_IN_DIFF_XYZ"}], ws
            )
            assert not results[0].passed

    def test_diff_not_contains_pass(self):
        with tempfile.TemporaryDirectory() as ws:
            _init_git(ws)
            path = os.path.join(ws, "file.txt")
            with open(path, "w") as f:
                f.write("hello")
            _git_add_commit(ws, "add file")
            results = run_checks(
                [{"type": "diff_not_contains", "fragment": "SHOULD_NOT_BE_HERE"}],
                ws,
            )
            assert results[0].passed


class TestCheckerShell:
    """Test the shell checker."""

    def test_shell_pass_returncode(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [{"type": "shell", "command": "echo hello"}], ws
            )
            assert results[0].passed

    def test_shell_pass_stdout_match(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [
                    {
                        "type": "shell",
                        "command": "echo hello_world",
                        "expected_stdout": "hello_world",
                    }
                ],
                ws,
            )
            assert results[0].passed

    def test_shell_fail_wrong_returncode(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [
                    {
                        "type": "shell",
                        "command": "exit 1",
                        "expected_returncode": 0,
                    }
                ],
                ws,
            )
            assert not results[0].passed

    def test_shell_fail_stdout_mismatch(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [
                    {
                        "type": "shell",
                        "command": "echo hi",
                        "expected_stdout": "NOT_HELLO",
                    }
                ],
                ws,
            )
            assert not results[0].passed

    def test_unknown_checker_type(self):
        with tempfile.TemporaryDirectory() as ws:
            results = run_checks(
                [{"type": "nonexistent_checker", "path": "x"}], ws
            )
            assert not results[0].passed
            assert "Unknown checker" in results[0].detail


# ---------------------------------------------------------------------------
# 3. Metrics collector
# ---------------------------------------------------------------------------


class TestMetricsCollector:
    """Test the MetricsCollector callbacks."""

    def test_on_tool_start_counts_tools(self):
        mc = MetricsCollector()
        mc.on_tool_start("read_file(utils.py)")
        mc.on_tool_start("read_file(config.py)")
        mc.on_tool_start("write_file(utils.py)")
        assert mc.tool_counts["read_file"] == 2
        assert mc.tool_counts["write_file"] == 1
        assert sum(mc.tool_counts.values()) == 3

    def test_on_tool_start_with_parallel_flag(self):
        mc = MetricsCollector()
        mc.on_tool_start("read_file(a.py)", parallel=True)
        mc.on_tool_start("write_file(b.py)", parallel=False)
        assert mc.tool_counts["read_file"] == 1
        assert mc.tool_counts["write_file"] == 1

    def test_mark_turn_increments(self):
        mc = MetricsCollector()
        assert mc.turn_count == 0
        mc.mark_turn()
        assert mc.turn_count == 1
        mc.mark_turn()
        assert mc.turn_count == 2

    def test_to_dict(self):
        mc = MetricsCollector()
        mc.on_tool_start("write_file(x.py)")
        mc.mark_turn()
        d = mc.to_dict()
        assert d["turns"] == 1
        assert d["tool_calls"] == {"write_file": 1}
        assert d["total_tool_calls"] == 1

    def test_tool_name_extraction_edge_cases(self):
        mc = MetricsCollector()
        # Tool name with no arguments
        mc.on_tool_start("list_directory")
        assert mc.tool_counts["list_directory"] == 1
        # Tool name with complex summary
        mc.on_tool_start("run_shell(git diff --stat HEAD~1)")
        assert mc.tool_counts["run_shell"] == 1


# ---------------------------------------------------------------------------
# 4. Runner with isolated workspace
# ---------------------------------------------------------------------------


class TestRunner:
    """Test the runner with mocked agent calls."""

    def _make_trivial_task(self, **overrides) -> EvalTask:
        """Create a minimal task for testing."""
        defaults = {
            "id": "trivial",
            "name": "Trivial task",
            "description": "Do nothing.",
            "category": "feature",
            "difficulty": "easy",
            "checks": [{"type": "shell", "command": "echo ok"}],
        }
        defaults.update(overrides)
        return EvalTask(**defaults)

    def test_run_task_trivial(self):
        """Run a trivial task that requires no file changes."""
        task = self._make_trivial_task()

        with tempfile.TemporaryDirectory() as ws:
            # Set up a minimal workspace
            _init_git(ws)

            # Mock run_agent_turn + init_session (imported locally in runner)
            with patch("llm.run_agent_turn") as mock_run, \
                 patch("config.init_session") as mock_init:
                mock_run.return_value = {"role": "assistant", "content": "Done."}
                mock_init.return_value = {
                    "config": MagicMock(),
                    "write_gate": MagicMock(),
                    "read_gate": MagicMock(),
                    "memory": MagicMock(),
                    "messages": [],
                    "session": MagicMock(),
                }
                result = run_task(task, workspace=ws)

            assert result.task_id == "trivial"
            assert result.success
            assert result.error is None
            assert len(result.checks) == 1
            assert result.checks[0].passed

    def test_run_task_with_mocked_tool_calls(self):
        """Runner collects tool call counts from on_tool_start callback."""
        task = self._make_trivial_task()

        with tempfile.TemporaryDirectory() as ws:
            _init_git(ws)

            def fake_run_agent_turn(**kwargs):
                on_tool_start = kwargs.get("on_tool_start")
                if on_tool_start:
                    on_tool_start("read_file(foo.py)")
                    on_tool_start("write_file(bar.py)")
                return {"role": "assistant", "content": "Done."}

            with patch("llm.run_agent_turn", side_effect=fake_run_agent_turn), \
                 patch("config.init_session") as mock_init:
                mock_init.return_value = {
                    "config": MagicMock(),
                    "write_gate": MagicMock(),
                    "read_gate": MagicMock(),
                    "memory": MagicMock(),
                    "messages": [],
                    "session": MagicMock(),
                }
                result = run_task(task, workspace=ws)

            assert result.tool_calls.get("read_file", 0) == 1
            assert result.tool_calls.get("write_file", 0) == 1

    def test_run_task_error_handling(self):
        """Runner catches exceptions and returns them in EvalResult.error."""
        task = self._make_trivial_task()

        with tempfile.TemporaryDirectory() as ws:
            _init_git(ws)

            with patch("llm.run_agent_turn", side_effect=RuntimeError("Boom")), \
                 patch("config.init_session") as mock_init:
                mock_init.return_value = {
                    "config": MagicMock(),
                    "write_gate": MagicMock(),
                    "read_gate": MagicMock(),
                    "memory": MagicMock(),
                    "messages": [],
                    "session": MagicMock(),
                }
                result = run_task(task, workspace=ws)

            assert not result.success
            assert result.error is not None
            assert "Boom" in result.error

    def test_run_task_with_fixture(self):
        """Runner can use a workspace fixture."""
        task = EvalTask(
            id="fixture-test",
            name="Fixture test",
            description="Check fixture exists.",
            category="feature",
            difficulty="easy",
            checks=[{"type": "file_exists", "path": "counter.py"}],
            workspace_fixture="mini_repo",
        )

        with patch("llm.run_agent_turn") as mock_run, \
             patch("config.init_session") as mock_init:
            mock_run.return_value = {"role": "assistant", "content": "Done."}
            mock_init.return_value = {
                "config": MagicMock(),
                "write_gate": MagicMock(),
                "read_gate": MagicMock(),
                "memory": MagicMock(),
                "messages": [],
                "session": MagicMock(),
            }
            result = run_task(task)

        assert result.success
        assert any(c.check_type == "file_exists" and c.passed for c in result.checks)


# ---------------------------------------------------------------------------
# 5. Suite aggregation
# ---------------------------------------------------------------------------


class TestSuiteReport:
    """Test SuiteReport aggregation across multiple tasks."""

    def test_empty_suite(self):
        report = SuiteReport()
        assert report.total == 0
        assert report.pass_rate == 0.0

    def test_aggregate_from_results(self):
        results = [
            EvalResult(
                task_id="t1",
                success=True,
                turns_used=3,
                tool_calls={"read_file": 2},
                tokens_consumed=500,
                wall_time_seconds=5.0,
            ),
            EvalResult(
                task_id="t2",
                success=False,
                turns_used=5,
                tool_calls={"read_file": 1, "write_file": 1},
                tokens_consumed=800,
                wall_time_seconds=8.0,
            ),
            EvalResult(
                task_id="t3",
                success=False,
                turns_used=0,
                tokens_consumed=0,
                wall_time_seconds=2.0,
                error="Connection timeout",
            ),
        ]
        report = run_suite_manual(results)

        assert report.total == 3
        assert report.passed == 1
        assert report.failed == 1
        assert report.errors == 1
        assert report.pass_rate == pytest.approx(1 / 3)
        assert report.avg_turns == pytest.approx(8 / 3)
        assert report.avg_tokens == pytest.approx(1300 / 3)
        assert report.avg_wall_time == pytest.approx(15.0 / 3)
        assert report.tool_usage["read_file"] == 3
        assert report.tool_usage["write_file"] == 1
        assert len(report.per_task) == 3


def run_suite_manual(results: list[EvalResult]) -> SuiteReport:
    """Helper that builds a SuiteReport from pre-computed EvalResults."""
    passed = sum(1 for r in results if r.success and not r.error)
    failed = sum(1 for r in results if not r.success and not r.error)
    errors = sum(1 for r in results if r.error)
    total = len(results)

    total_turns = sum(r.turns_used for r in results)
    total_tokens = sum(r.tokens_consumed for r in results)
    total_time = sum(r.wall_time_seconds for r in results)

    tool_usage: dict[str, int] = {}
    for r in results:
        for name, count in r.tool_calls.items():
            tool_usage[name] = tool_usage.get(name, 0) + count

    return SuiteReport(
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        pass_rate=passed / total if total > 0 else 0.0,
        avg_turns=total_turns / total if total > 0 else 0.0,
        avg_tokens=total_tokens / total if total > 0 else 0.0,
        avg_wall_time=total_time / total if total > 0 else 0.0,
        tool_usage=tool_usage,
        per_task=results,
    )


# ---------------------------------------------------------------------------
# 6. CheckResult dataclass
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_check_result_fields(self):
        cr = CheckResult("file_exists", True, "found file")
        assert cr.check_type == "file_exists"
        assert cr.passed is True
        assert cr.detail == "found file"

    def test_check_result_failure(self):
        cr = CheckResult("test_passes", False, "test failed: assert False")
        assert cr.passed is False
        assert "assert False" in cr.detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git(ws: str) -> None:
    """Initialize a git repo in a temp directory."""
    import subprocess

    subprocess.run(["git", "init"], cwd=ws, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=ws, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=ws, capture_output=True
    )


def _git_add_commit(ws: str, msg: str = "commit") -> None:
    """Stage all and commit in a git repo."""
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=ws, capture_output=True)
