#!/usr/bin/env python3
"""
Benchmark tests for mini_agent.

These tests run the agent against the eval task suite.
They are excluded from the default test run — use
``--run-benchmarks`` to include them:

    python -m pytest test_benchmarks.py --run-benchmarks -v
    make test-all   # same thing

The benchmarks are ordered last in collection to avoid interfering with
unit tests that may share global state (tool context, sub-agent threads).

Usage examples:

    # Run only local eval tasks:
    python -m pytest test_benchmarks.py --run-benchmarks -v -k "local"

    # Run a specific task:
    python -m pytest test_benchmarks.py --run-benchmarks -v -k "hello_world"
"""

from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml_tasks() -> list:
    """Load all YAML tasks from eval/tasks/."""
    from eval.runner import load_tasks
    return load_tasks()


def _summarize_result(result) -> str:
    """Return a one-line summary of an EvalResult."""
    from eval.runner import EvalResult

    if isinstance(result, EvalResult):
        return (
            f"turns={result.turns_used}, tokens={result.tokens_consumed}, "
            f"time={result.wall_time_seconds:.0f}s, "
            f"checks={len(result.checks)}, success={result.success}"
        )
    return str(result)


# ---------------------------------------------------------------------------
# Local eval task benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.parametrize("task", _load_yaml_tasks(), ids=lambda t: t.id)
def test_eval_task_local(task, request):
    """Run a single local YAML eval task.

    Each task exercises a specific tool or capability of the agent.
    These are the fastest benchmarks and should always pass before
    attempting larger evaluations.
    """
    from eval.runner import run_task

    # Dump task info for debugging failures
    print(f"\n--- Task: {task.id} ({task.difficulty}, {task.category}) ---")
    print(f"Description: {task.description[:300]}")
    print(f"Expected tools: {task.expected_tools}")
    print(f"Max turns: {task.expected_turns_max}")

    start = time.monotonic()
    result = run_task(task, timeout_seconds=120)
    elapsed = time.monotonic() - start

    print(f"Result: {_summarize_result(result)}")
    for c in result.checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"  [{mark}] {c.check_type}: {c.detail}")

    # Assertions
    if result.error:
        pytest.fail(f"Task errored: {result.error}")

    assert result.success, (
        f"Task {task.id} failed: "
        + "; ".join(f"{c.check_type}: {c.detail}" for c in result.checks if not c.passed)
    )

    # Verify expected tools were used
    for tool in task.expected_tools:
        assert tool in result.tool_calls, (
            f"Expected tool '{tool}' not used in task {task.id}. "
            f"Tools used: {list(result.tool_calls.keys())}"
        )

    # Verify turn budget
    if task.expected_turns_max:
        assert result.turns_used <= task.expected_turns_max, (
            f"Task {task.id} used {result.turns_used} turns "
            f"(max: {task.expected_turns_max})"
        )

    # Performance soft assertions (warn only, don't fail)
    if elapsed > 60:
        print(f"WARNING: Task {task.id} took {elapsed:.0f}s (>60s target)")