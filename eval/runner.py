#!/usr/bin/env python3
"""Task runner for the Agent Evaluation Harness.

Provides run_task(), run_suite(), load_tasks(), and the CLI entry point
(``python -m eval.runner``).  Instruments the agent loop via existing
callbacks — zero changes to core modules.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from eval.scorer import CheckResult, run_checks
from eval.metrics import MetricsCollector

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EvalTask:
    """Parsed from a YAML task file."""

    id: str
    name: str
    description: str
    category: str
    difficulty: str
    checks: list[dict]
    workspace_fixture: str | None = None
    expected_tools: list[str] = field(default_factory=list)
    expected_turns_max: int | None = None
    expected_files_touched: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """Outcome of running a single eval task."""

    task_id: str
    success: bool
    checks: list[CheckResult] = field(default_factory=list)
    turns_used: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens_consumed: int = 0
    wall_time_seconds: float = 0.0
    error: str | None = None
    diff: str = ""


@dataclass
class SuiteReport:
    """Aggregated results across multiple tasks."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    pass_rate: float = 0.0
    avg_turns: float = 0.0
    avg_tokens: float = 0.0
    avg_wall_time: float = 0.0
    tool_usage: dict[str, int] = field(default_factory=dict)
    per_task: list[EvalResult] = field(default_factory=list)
    run_id: str = ""


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

_IGNORE_PATTERNS = (
    ".git",
    "__pycache__",
    ".pytest_cache",
    "venv",
    ".venv",
    "node_modules",
    "eval",
    ".mypy_cache",
    ".ruff_cache",
)


def _load_yaml() -> "callable":
    """Lazy-import YAML parser (yaml or tomli fallback)."""
    try:
        import yaml

        return yaml.safe_load
    except ImportError:
        # Fallback: treat YAML as simple key-value for tests
        # (real eval needs pyyaml installed)
        raise ImportError(
            "PyYAML is required for eval tasks. Install with: pip install pyyaml"
        )


def parse_task_from_yaml(yaml_text: str) -> EvalTask:
    """Parse a single task from a YAML string."""
    yaml_load = _load_yaml()
    data = yaml_load(yaml_text)
    return EvalTask(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        category=data.get("category", "feature"),
        difficulty=data.get("difficulty", "easy"),
        checks=data.get("checks", []),
        workspace_fixture=data.get("workspace_fixture"),
        expected_tools=data.get("expected_tools", []),
        expected_turns_max=data.get("expected_turns_max"),
        expected_files_touched=data.get("expected_files_touched", []),
        tags=data.get("tags", []),
    )


def load_tasks(
    tasks_dir: str | None = None,
    *,
    tags: list[str] | None = None,
    difficulty: str | None = None,
    task_id: str | None = None,
) -> list[EvalTask]:
    """Load tasks from the tasks directory, with optional filtering.

    Args:
        tasks_dir: Path to directory containing YAML task files.
                   Defaults to ``eval/tasks/`` relative to this file.
        tags: If set, only return tasks that have at least one matching tag.
        difficulty: If set, only return tasks matching this difficulty.
        task_id: If set, only return the task with this ID.
    """
    if tasks_dir is None:
        tasks_dir = os.path.join(os.path.dirname(__file__), "tasks")

    tasks: list[EvalTask] = []

    if not os.path.isdir(tasks_dir):
        return tasks

    for fname in sorted(os.listdir(tasks_dir)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        filepath = os.path.join(tasks_dir, fname)
        try:
            with open(filepath, "r") as f:
                task = parse_task_from_yaml(f.read())
            tasks.append(task)
        except Exception as exc:
            print(f"Warning: failed to parse {filepath}: {exc}", file=sys.stderr)

    # Apply filters
    if task_id:
        tasks = [t for t in tasks if t.id == task_id]
    if tags:
        tag_set = set(tags)
        tasks = [t for t in tasks if tag_set & set(t.tags)]
    if difficulty:
        tasks = [t for t in tasks if t.difficulty == difficulty]

    return tasks


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------


def run_task(
    task: EvalTask,
    *,
    timeout_seconds: int = 300,
    stream: bool = False,
    workspace: str | None = None,
) -> EvalResult:
    """Run a single eval task and return the result.

    Creates a temp copy of the workspace (or fixture), initializes the agent
    session, injects the task prompt, runs the agent loop with instrumentation,
    then runs all checks and returns a structured result.

    Args:
        task: Parsed EvalTask.
        timeout_seconds: Max wall-clock time per task.
        stream: If True, stream agent output to stderr.
        workspace: Optional pre-existing workspace to use instead of copying.
                   For testing only.

    Returns:
        EvalResult with success, checks, metrics, and diff.
    """
    own_workspace = workspace is None

    if own_workspace:
        if task.workspace_fixture:
            fixture_path = os.path.join(
                os.path.dirname(__file__), "fixtures", task.workspace_fixture
            )
            workspace = tempfile.mkdtemp(prefix=f"eval_{task.id}_")
            shutil.copytree(fixture_path, workspace, dirs_exist_ok=True)
        else:
            workspace = tempfile.mkdtemp(prefix=f"eval_{task.id}_")
            shutil.copytree(
                os.getcwd(),
                workspace,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_IGNORE_PATTERNS),
            )

    original_cwd = os.getcwd()
    os.chdir(workspace)

    # Initialize git repo in temp workspace (needed for diff)
    _ensure_git_repo(workspace)

    # Pre-assign before try block so except handler can compute elapsed
    start_time = time.monotonic()
    metrics = None

    try:
        # Initialize agent session
        from core.config import init_session

        session = init_session(workspace)
        config = session["config"]
        config.unrestricted = True
        config.allow_overwrites = True
        config.stream = stream
        config.verbose = False

        # Inject task prompt
        session["messages"].append(
            {
                "role": "user",
                "content": (
                    f"Your task: {task.name}\n\n{task.description}\n\n"
                    "When you are done, report what you changed and why."
                ),
            }
        )

        # Instrument with metrics collector
        metrics = MetricsCollector()
        cancel_event = threading.Event()

        # Timeout guard
        def _timeout():
            cancel_event.set()

        timer = threading.Timer(timeout_seconds, _timeout)
        timer.daemon = True
        timer.start()

        try:
            from core.llm import run_agent_turn

            result_msg = run_agent_turn(
                messages=session["messages"],
                config=config,
                write_gate=session["write_gate"],
                read_gate=session["read_gate"],
                on_tool_start=metrics.on_tool_start,
                on_tool_end=metrics.on_tool_end,
                cancel_event=cancel_event,
                max_turns=task.expected_turns_max if task.expected_turns_max is not None else 100,
                session=session["session"],
                memory_store=session["memory"],
            )
            # run_agent_turn stores _turn_count on the result message;
            # we use that for the actual turn count (metrics.mark_turn is a
            # coarse single increment for the top-level loop).
        finally:
            timer.cancel()

        elapsed = time.monotonic() - start_time

        # Collect git diff
        diff_result = subprocess.run(
            ["git", "diff"], capture_output=True, text=True, cwd=workspace, timeout=10
        )
        diff = diff_result.stdout

        # Run checks
        checks = run_checks(task.checks, workspace)

        # Estimate tokens
        from memory.memory import _total_tokens

        tokens = _total_tokens(session["messages"])

        return EvalResult(
            task_id=task.id,
            success=len(checks) > 0 and all(c.passed for c in checks),
            checks=checks,
            turns_used=result_msg.get("_turn_count", 1) if result_msg else 0,
            tool_calls=dict(metrics.tool_counts),
            tokens_consumed=tokens,
            wall_time_seconds=elapsed,
            diff=diff,
        )

    except Exception as exc:
        elapsed = time.monotonic() - start_time
        return EvalResult(
            task_id=task.id,
            success=False,
            checks=[],
            turns_used=0,
            tool_calls=dict(metrics.tool_counts) if metrics is not None else {},
            tokens_consumed=0,
            wall_time_seconds=elapsed,
            error=str(exc),
        )

    finally:
        os.chdir(original_cwd)
        if own_workspace and workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def run_suite(
    tasks: list[EvalTask],
    *,
    timeout_seconds: int = 300,
    stream: bool = False,
) -> SuiteReport:
    """Run a suite of eval tasks and return an aggregated report.

    Args:
        tasks: List of parsed EvalTask objects.
        timeout_seconds: Max wall-clock time per task.
        stream: If True, stream agent output to stderr.

    Returns:
        SuiteReport with aggregated metrics.
    """
    results: list[EvalResult] = []
    passed = 0
    failed = 0
    errors = 0
    total_turns = 0
    total_tokens = 0
    total_time = 0.0
    tool_usage: dict[str, int] = {}

    for task in tasks:
        result = run_task(task, timeout_seconds=timeout_seconds, stream=stream)
        results.append(result)

        if result.error:
            errors += 1
        elif result.success:
            passed += 1
        else:
            failed += 1

        total_turns += result.turns_used
        total_tokens += result.tokens_consumed
        total_time += result.wall_time_seconds
        for tool_name, count in result.tool_calls.items():
            tool_usage[tool_name] = tool_usage.get(tool_name, 0) + count

    total = len(results)
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
        run_id=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def _ensure_git_repo(workspace: str) -> None:
    """Ensure workspace is a git repo (init + initial commit if needed)."""
    dot_git = os.path.join(workspace, ".git")
    if os.path.isdir(dot_git):
        return

    def _run(cmd: list[str], timeout: int = 10) -> None:
        result = subprocess.run(
            cmd, cwd=workspace, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(cmd)!r} "
                f"(rc={result.returncode}) stderr: {result.stderr.strip()}"
            )

    _run(["git", "init"])
    _run(["git", "config", "user.email", "eval@mini.agent"], timeout=5)
    _run(["git", "config", "user.name", "Eval Runner"], timeout=5)
    _run(["git", "add", "-A"])
    _run(["git", "commit", "--allow-empty", "-m", "Initial state for eval"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> None:
    """Entry point for ``python -m eval.runner``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m eval.runner",
        description="Agent Evaluation Harness for mini_agent",
    )
    parser.add_argument(
        "--task", type=str, default=None, help="Run a specific task by ID."
    )
    parser.add_argument(
        "--suite",
        type=str,
        default="all",
        help="Suite filter: 'all', or comma-separated tags (default: all).",
    )
    parser.add_argument(
        "--difficulty", type=str, default=None, help="Filter by difficulty."
    )
    parser.add_argument(
        "--tags", type=str, default=None, help="Comma-separated tag filter."
    )
    parser.add_argument(
        "--output", type=str, default=None, help="Path for JSON report output."
    )
    parser.add_argument(
        "--stream", action="store_true", help="Stream agent output during eval."
    )
    parser.add_argument(
        "--timeout", type=int, default=300, help="Timeout per task in seconds."
    )
    parser.add_argument(
        "--tasks-dir",
        type=str,
        default=None,
        help="Custom tasks directory path.",
    )
    args = parser.parse_args()

    # Load tasks
    tag_list = args.tags.split(",") if args.tags else None
    suite = args.suite
    if suite == "all":
        tag_list = None  # Load everything

    tasks = load_tasks(
        tasks_dir=args.tasks_dir,
        task_id=args.task,
        tags=tag_list,
        difficulty=args.difficulty,
    )

    if not tasks:
        print("No tasks found matching the filters.", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(tasks)} task(s)...")

    report = run_suite(tasks, timeout_seconds=args.timeout, stream=args.stream)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Suite: {'all' if suite == 'all' else suite}")
    print(f"Total: {report.total}  Passed: {report.passed}  "
          f"Failed: {report.failed}  Errors: {report.errors}")
    print(f"Pass rate: {report.pass_rate:.1%}")
    print(f"Avg turns: {report.avg_turns:.1f}  "
          f"Avg tokens: {report.avg_tokens:.0f}  "
          f"Avg time: {report.avg_wall_time:.1f}s")
    print(f"{'='*60}")

    for r in report.per_task:
        status = "\u2713" if r.success else ("\u2717" if not r.error else "!")
        print(f"\n  [{status}] {r.task_id}  ({r.turns_used} turns, {r.tokens_consumed} tokens)")
        if r.error:
            print(f"       Error: {r.error}")
        for c in r.checks:
            mark = "\u2713" if c.passed else "\u2717"
            print(f"       [{mark}] {c.check_type}: {c.detail}")

    # Save report
    if args.output:
        output_path = args.output
    else:
        reports_dir = os.path.join(os.path.dirname(__file__), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"report_{report.run_id}.json")

    report_dict = {
        "run_id": report.run_id,
        "suite": suite,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "errors": report.errors,
        "pass_rate": report.pass_rate,
        "avg_turns": report.avg_turns,
        "avg_tokens": report.avg_tokens,
        "avg_wall_time": report.avg_wall_time,
        "tool_usage": report.tool_usage,
        "tasks": [
            {
                "task_id": r.task_id,
                "success": r.success,
                "turns_used": r.turns_used,
                "tool_calls": r.tool_calls,
                "tokens_consumed": r.tokens_consumed,
                "wall_time_seconds": r.wall_time_seconds,
                "error": r.error,
                "checks": [
                    {"check_type": c.check_type, "passed": c.passed, "detail": c.detail}
                    for c in r.checks
                ],
            }
            for r in report.per_task
        ],
    }
    with open(output_path, "w") as f:
        json.dump(report_dict, f, indent=2)
    print(f"\nReport saved to {output_path}")

    # Exit code
    sys.exit(0 if report.failed == 0 and report.errors == 0 else 1)


if __name__ == "__main__":
    _cli()
