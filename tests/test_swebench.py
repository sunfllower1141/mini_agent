#!/usr/bin/env python3
"""Tests for the SWE-bench evaluation components.

Covers: task parsing, prompt building, predictions I/O, report generation,
and the agent script entry point. Network-heavy operations (dataset loading,
repo cloning) are mocked.
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock


# Ensure eval is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.swebench_runner import (
    SWEBenchTask,
    SWEBenchResult,
    SWEBenchReport,
    parse_swebench_task,
    save_predictions,
    load_predictions,
    _build_prompt,
    _repo_cache_path,
)


# ---------------------------------------------------------------------------
# 1. Task parsing
# ---------------------------------------------------------------------------


class TestSWEBenchTaskParsing:
    """Verify raw dataset rows are parsed into SWEBenchTask correctly."""

    def test_parse_minimal_task(self):
        raw = {
            "instance_id": "django__django-11049",
            "repo": "django/django",
            "base_commit": "abc123def456",
            "problem_statement": "Fix the login bug.",
        }
        task = parse_swebench_task(raw)
        assert task.instance_id == "django__django-11049"
        assert task.repo == "django/django"
        assert task.base_commit == "abc123def456"
        assert task.problem_statement == "Fix the login bug."
        assert task.patch == ""
        assert task.fail_to_pass == []
        assert task.pass_to_pass == []

    def test_parse_task_with_tests(self):
        raw = {
            "instance_id": "test__task-1",
            "repo": "test/repo",
            "base_commit": "deadbeef",
            "problem_statement": "Something is broken.",
            "FAIL_TO_PASS": '["test_feature", "test_edge_case"]',
            "PASS_TO_PASS": '["test_baseline", "test_regression"]',
            "test_patch": "diff --git ...",
            "hints_text": "Look at the utils module.",
        }
        task = parse_swebench_task(raw)
        assert task.fail_to_pass == ["test_feature", "test_edge_case"]
        assert task.pass_to_pass == ["test_baseline", "test_regression"]
        assert task.test_patch == "diff --git ..."
        assert task.hints_text == "Look at the utils module."

    def test_parse_task_with_malformed_json_tests(self):
        """Malformed JSON in FAIL_TO_PASS should be handled gracefully."""
        raw = {
            "instance_id": "bad__task",
            "repo": "bad/repo",
            "base_commit": "0000000",
            "problem_statement": "x",
            "FAIL_TO_PASS": "not valid json {{{",
            "PASS_TO_PASS": "also bad",
        }
        task = parse_swebench_task(raw)
        assert task.fail_to_pass == []
        assert task.pass_to_pass == []

    def test_parse_task_defaults(self):
        """Missing optional fields should use defaults."""
        raw = {
            "instance_id": "minimal",
            "repo": "a/b",
            "base_commit": "123",
            "problem_statement": "do stuff",
        }
        task = parse_swebench_task(raw)
        assert task.patch == ""
        assert task.test_patch == ""
        assert task.hints_text == ""
        assert task.created_at == ""
        assert task.version == ""


# ---------------------------------------------------------------------------
# 2. Prompt building
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    """Verify agent prompts are constructed from SWE-bench tasks."""

    def test_basic_prompt(self):
        task = SWEBenchTask(
            instance_id="test-1",
            repo="a/b",
            base_commit="abc",
            problem_statement="The widget is broken.",
        )
        prompt = _build_prompt(task)
        assert "The widget is broken." in prompt
        assert "GitHub Issue" in prompt
        assert "Instructions" in prompt
        assert "Explore the codebase" in prompt

    def test_prompt_with_hints(self):
        task = SWEBenchTask(
            instance_id="test-2",
            repo="a/b",
            base_commit="abc",
            problem_statement="Fix it.",
            hints_text="Check the auth module.",
        )
        prompt = _build_prompt(task)
        assert "Fix it." in prompt
        assert "Check the auth module." in prompt
        assert "Hints" in prompt


# ---------------------------------------------------------------------------
# 3. Predictions I/O
# ---------------------------------------------------------------------------


class TestPredictionsIO:
    """Verify predictions can be saved and loaded in SWE-bench format."""

    def test_save_and_load_predictions(self):
        results = [
            SWEBenchResult(
                instance_id="django__django-11049",
                repo="django/django",
                model_patch="diff --git a/foo.py b/foo.py\n+fixed",
                turns_used=5,
                tokens_consumed=12000,
            ),
            SWEBenchResult(
                instance_id="flask__flask-2000",
                repo="pallets/flask",
                model_patch="diff --git a/app.py b/app.py\n-patched",
                turns_used=3,
                tokens_consumed=8000,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "predictions.jsonl")
            save_predictions(results, path)

            # Verify file format (one JSON object per line)
            with open(path, "r") as f:
                lines = [line.strip() for line in f if line.strip()]
                assert len(lines) == 2

                obj1 = json.loads(lines[0])
                assert obj1["instance_id"] == "django__django-11049"
                assert obj1["model_name_or_path"] == "mini_agent"
                assert "+fixed" in obj1["model_patch"]

                obj2 = json.loads(lines[1])
                assert obj2["instance_id"] == "flask__flask-2000"
                assert "-patched" in obj2["model_patch"]

            # Load back
            loaded = load_predictions(path)
            assert len(loaded) == 2
            assert loaded["django__django-11049"] == "diff --git a/foo.py b/foo.py\n+fixed"
            assert loaded["flask__flask-2000"] == "diff --git a/app.py b/app.py\n-patched"

    def test_load_empty_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "nonexistent.jsonl")
            loaded = load_predictions(path)
            assert loaded == {}

    def test_load_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bad.jsonl")
            with open(path, "w") as f:
                f.write("this is not json\n")
                f.write('{"instance_id": "good", "model_patch": "ok"}\n')
                f.write("\n")  # blank line
                f.write("also bad\n")
            loaded = load_predictions(path)
            assert len(loaded) == 1
            assert loaded["good"] == "ok"

    def test_save_predictions_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "subdir", "nested", "preds.jsonl")
            results = [SWEBenchResult(instance_id="test", repo="a/b", model_patch="patch")]
            save_predictions(results, path)
            assert os.path.isfile(path)


# ---------------------------------------------------------------------------
# 4. Results & reports
# ---------------------------------------------------------------------------


class TestSWEBenchResult:
    """Verify SWEBenchResult dataclass."""

    def test_result_defaults(self):
        result = SWEBenchResult(instance_id="test-1", repo="a/b")
        assert result.instance_id == "test-1"
        assert result.model_patch == ""
        assert result.turns_used == 0
        assert result.error is None
        assert result.resolved is False

    def test_result_with_patch(self):
        result = SWEBenchResult(
            instance_id="test-2",
            repo="c/d",
            model_patch="diff --git ...",
            turns_used=10,
            tool_calls={"edit_file": 3, "run_shell": 5},
            tokens_consumed=25000,
            wall_time_seconds=45.6,
            resolved=True,
            fail_to_pass_passed=3,
            fail_to_pass_total=3,
        )
        assert result.model_patch == "diff --git ..."
        assert result.tool_calls == {"edit_file": 3, "run_shell": 5}
        assert result.resolved is True


class TestSWEBenchReport:
    """Verify SWEBenchReport aggregation."""

    def test_empty_report(self):
        report = SWEBenchReport()
        assert report.total == 0
        assert report.resolution_rate == 0.0
        assert report.per_task == []

    def test_report_with_results(self):
        report = SWEBenchReport(
            total=10,
            completed=8,
            errors=2,
            resolved=3,
            resolution_rate=0.375,
            avg_turns=12.5,
            avg_tokens=30000.0,
            avg_wall_time=120.0,
            per_task=[
                SWEBenchResult(instance_id="a", repo="x/y", resolved=True),
                SWEBenchResult(instance_id="b", repo="x/z", resolved=False),
            ],
        )
        assert report.total == 10
        assert report.resolved == 3
        assert report.resolution_rate == 0.375
        assert len(report.per_task) == 2


# ---------------------------------------------------------------------------
# 5. Repo cache path
# ---------------------------------------------------------------------------


class TestRepoCachePath:
    """Verify repo cache path generation."""

    def test_repo_cache_path(self):
        path = _repo_cache_path("django/django")
        assert path.name == "django__django"
        assert ".cache" in str(path)
        assert "mini_agent" in str(path)
        assert "swebench_repos" in str(path)

    def test_repo_cache_path_nested(self):
        path = _repo_cache_path("pallets/flask")
        assert path.name == "pallets__flask"


# ---------------------------------------------------------------------------
# 6. Agent script (swebench_agent.py)
# ---------------------------------------------------------------------------


class TestSWEBenchAgentScript:
    """Verify the agent script parses environment variables and builds prompts."""

    def test_build_prompt_from_env_style(self):
        """Simulate what the agent script does with SWE_TASK env var."""
        from eval.swebench_agent import _build_prompt as agent_build_prompt

        prompt = agent_build_prompt(
            "The API returns 500 on empty input.",
            hints_text="Check the validation layer.",
        )
        assert "The API returns 500 on empty input." in prompt
        assert "Check the validation layer." in prompt
        assert "GitHub Issue" in prompt

    def test_agent_script_imports(self):
        """Verify the agent script can be imported without side effects."""
        import eval.swebench_agent
        assert hasattr(eval.swebench_agent, "main")


# ---------------------------------------------------------------------------
# 7. Integration: runner with mocked agent
# ---------------------------------------------------------------------------


class TestSWEBenchRunnerIntegration:
    """Light integration tests with mocked agent and network."""

    @patch("eval.swebench_runner._get_repo")
    @patch("config.init_session")
    @patch("llm.run_agent_turn")
    @patch("memory._total_tokens", return_value=5000)
    def test_run_swebench_task_mocked(
        self, mock_tokens, mock_run_agent, mock_init_session, mock_get_repo
    ):
        """Run a SWE-bench task with all heavy dependencies mocked."""
        from eval.swebench_runner import run_swebench_task

        # Setup mocks
        mock_get_repo.return_value = tempfile.mkdtemp(prefix="swebench_test_")

        # Mock session
        mock_session = MagicMock()
        mock_session.__getitem__.side_effect = lambda k: {
            "config": MagicMock(),
            "write_gate": MagicMock(),
            "read_gate": MagicMock(),
            "messages": [],
            "memory": MagicMock(),
            "session": MagicMock(),
        }[k]
        mock_init_session.return_value = mock_session

        # Mock agent turn
        mock_run_agent.return_value = {"_turn_count": 3}

        task = SWEBenchTask(
            instance_id="test__mock-1",
            repo="test/mock",
            base_commit="abc123def456",
            problem_statement="Fix the off-by-one error.",
        )

        result = run_swebench_task(task, timeout_seconds=60)

        # Minimal assertions
        assert result.instance_id == "test__mock-1"
        assert result.repo == "test/mock"
        # The agent should have been called
        assert mock_init_session.called
        assert mock_run_agent.called

    @patch("eval.swebench_runner._get_repo")
    @patch("config.init_session")
    @patch("llm.run_agent_turn")
    def test_run_swebench_task_error_handling(
        self, mock_run_agent, mock_init_session, mock_get_repo
    ):
        """Task should return a result with error, not raise, on failure."""
        from eval.swebench_runner import run_swebench_task

        mock_get_repo.side_effect = RuntimeError("Clone failed: network error")

        task = SWEBenchTask(
            instance_id="test__error-1",
            repo="test/error",
            base_commit="deadbeef",
            problem_statement="x",
        )

        result = run_swebench_task(task, timeout_seconds=60)

        assert result.instance_id == "test__error-1"
        assert result.error is not None
        assert "Clone failed" in result.error
        assert result.model_patch == ""
        assert result.turns_used == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git(workspace: str) -> None:
    """Initialize a git repo in workspace with an initial commit."""
    import subprocess
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=workspace,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=workspace,
        capture_output=True, text=True,
    )
