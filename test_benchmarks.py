#!/usr/bin/env python3
"""
Benchmark tests for mini_agent.

These tests run the agent against the eval task suite and (optionally)
SWE-bench tasks. They are excluded from the default test run — use
``--run-benchmarks`` to include them:

    python -m pytest test_benchmarks.py --run-benchmarks -v
    make test-all   # same thing

The benchmarks are ordered last in collection to avoid interfering with
unit tests that may share global state (tool context, sub-agent threads).

Benchmark categories:

    test_eval_task_local[...]
        Run the built-in YAML eval tasks (add_hello_world, fix_off_by_one, etc.)
        These are fast (~10-60s each) and exercise core tool usage.

    test_swebench_smoke / test_swebench_lite[...]
        Run SWE-bench tasks. These require network access (repo cloning) and
        the ``datasets`` library. Use --swebench to opt in.

Usage examples:

    # Run only local eval tasks:
    python -m pytest test_benchmarks.py --run-benchmarks -v -k "local"

    # Run a specific task:
    python -m pytest test_benchmarks.py --run-benchmarks -v -k "hello_world"

    # Run SWE-bench smoke test (single task):
    python -m pytest test_benchmarks.py --run-benchmarks --swebench -v -k "smoke"

    # Run first 5 SWE-bench Lite tasks:
    python -m pytest test_benchmarks.py --run-benchmarks --swebench -v -k "lite"
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml_tasks() -> list:
    """Load all YAML tasks from eval/tasks/."""
    from eval.runner import load_tasks
    return load_tasks()


def _load_swebench_tasks(max_tasks: int = 5) -> list:
    """Load SWE-bench Lite tasks from HF."""
    try:
        from eval.swebench_runner import _load_dataset, parse_swebench_task
    except ImportError:
        pytest.skip("SWE-bench runner not available (install 'datasets' library)")

    raw = _load_dataset("princeton-nlp/SWE-bench_Lite", max_tasks=max_tasks)
    return [parse_swebench_task(r) for r in raw]


def _summarize_result(result) -> str:
    """Return a one-line summary of an EvalResult or SWEBenchResult."""
    from eval.runner import EvalResult
    from eval.swebench_runner import SWEBenchResult

    if isinstance(result, SWEBenchResult):
        return (
            f"turns={result.turns_used}, tokens={result.tokens_consumed}, "
            f"time={result.wall_time_seconds:.0f}s, "
            f"patch={len(result.model_patch)}B, error={result.error}"
        )
    elif isinstance(result, EvalResult):
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
    attempting SWE-bench.
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


# ---------------------------------------------------------------------------
# SWE-bench benchmarks
# ---------------------------------------------------------------------------


def _require_swebench(request):
    """Skip if --swebench flag not set."""
    if not request.config.getoption("--swebench", default=False):
        pytest.skip("SWE-bench benchmarks require --swebench flag")


def _require_network():
    """Skip if network unavailable."""
    import socket
    try:
        socket.create_connection(("github.com", 443), timeout=5)
    except (socket.timeout, OSError):
        pytest.skip("Network unavailable (cannot reach github.com)")


@pytest.mark.benchmark
@pytest.mark.slow
def test_swebench_smoke(request):
    """Smoke test: run a single SWE-bench Lite task end-to-end.

    This validates the full SWE-bench pipeline (dataset loading, repo cloning,
    agent execution, patch generation) without running a full suite.

    Uses a known-easy task from SWE-bench Lite to establish a baseline.
    """
    _require_swebench(request)
    _require_network()

    from eval.swebench_runner import run_swebench_task, parse_swebench_task, _load_dataset

    # Load just one task
    raw_tasks = _load_dataset("princeton-nlp/SWE-bench_Lite", max_tasks=1)
    assert len(raw_tasks) > 0, "No tasks loaded from dataset"

    task = parse_swebench_task(raw_tasks[0])

    print(f"\n--- SWE-bench Smoke Test: {task.instance_id} ---")
    print(f"Repo: {task.repo} @ {task.base_commit[:8]}")
    print(f"Issue: {task.problem_statement[:300]}...")

    start = time.monotonic()
    result = run_swebench_task(task, timeout_seconds=600)
    elapsed = time.monotonic() - start

    print(f"Result: {_summarize_result(result)}")

    # Smoke test assertions
    if result.error:
        pytest.fail(f"SWE-bench smoke test errored: {result.error}")

    # The agent must produce a patch
    assert result.model_patch, "Agent produced an empty patch"

    # Patch should be a valid git diff (starts with diff or has ---/+++ headers)
    patch_lines = result.model_patch.strip().split("\n")
    has_diff_header = any(
        line.startswith("diff --git") for line in patch_lines[:5]
    ) or any(
        "---" in line or "+++" in line for line in patch_lines[:10]
    )
    print(f"Patch has diff header: {has_diff_header}")
    print(f"Patch preview (first 500 chars):\n{result.model_patch[:500]}")

    # The agent should have made some tool calls
    assert sum(result.tool_calls.values()) > 0, "Agent made no tool calls"


@pytest.mark.benchmark
@pytest.mark.slow
@pytest.mark.parametrize(
    "task",
    [],  # populated dynamically in pytest_generate_tests
    ids=lambda t: t.instance_id if hasattr(t, "instance_id") else str(t),
)
def test_swebench_lite(request, task):
    """Run a SWE-bench Lite task.

    Parametrized dynamically based on --swebench-max-tasks.
    Each task clones its repo, runs the agent, and produces a patch.
    We don't assert resolution (that requires Docker evaluation), but
    we verify the patch is non-empty and the agent used tools.

    To score these predictions, run the official SWE-bench harness:
        python -m swebench.harness.run_evaluation \\
            --dataset_name princeton-nlp/SWE-bench_Lite \\
            --predictions_path predictions.jsonl \\
            --max_workers 4 --run_id mini_agent
    """
    _require_swebench(request)
    _require_network()

    from eval.swebench_runner import run_swebench_task

    print(f"\n--- SWE-bench Lite: {task.instance_id} ---")
    print(f"Repo: {task.repo} @ {task.base_commit[:8]}")

    start = time.monotonic()
    result = run_swebench_task(task, timeout_seconds=600)
    elapsed = time.monotonic() - start

    print(f"Result: {_summarize_result(result)}")

    if result.error:
        # Don't fail the whole parametrized suite on one error —
        # record it as a warning and continue
        pytest.fail(f"SWE-bench task {task.instance_id} errored: {result.error}")

    assert result.model_patch, f"Task {task.instance_id} produced an empty patch"
    assert sum(result.tool_calls.values()) > 0, (
        f"Task {task.instance_id}: agent made no tool calls"
    )

    # Save individual prediction alongside test results
    report_dir = os.path.join(os.path.dirname(__file__), "eval", "reports")
    os.makedirs(report_dir, exist_ok=True)

    prediction = {
        "instance_id": result.instance_id,
        "model_patch": result.model_patch,
        "model_name_or_path": "mini_agent",
        "turns_used": result.turns_used,
        "wall_time_seconds": result.wall_time_seconds,
    }
    pred_file = os.path.join(report_dir, f"pred_{result.instance_id}.json")
    with open(pred_file, "w") as f:
        json.dump(prediction, f, indent=2)


# ---------------------------------------------------------------------------
# Dynamic parametrization for SWE-bench
# ---------------------------------------------------------------------------


def pytest_generate_tests(metafunc):
    """Dynamically parametrize SWE-bench tests based on CLI flags."""
    if "task" in metafunc.fixturenames and metafunc.function.__name__ == "test_swebench_lite":
        # Only load SWE-bench tasks if --swebench is set
        if not metafunc.config.getoption("--swebench", default=False):
            metafunc.parametrize("task", [], ids=lambda t: "skipped")
            return

        max_tasks = metafunc.config.getoption("--swebench-max-tasks", default=5) or 5
        try:
            tasks = _load_swebench_tasks(max_tasks=max_tasks)
        except Exception as exc:
            print(f"Failed to load SWE-bench tasks: {exc}", file=sys.stderr)
            metafunc.parametrize("task", [], ids=lambda t: "load-failed")
            return

        metafunc.parametrize(
            "task",
            tasks,
            ids=lambda t: t.instance_id if hasattr(t, "instance_id") else str(t),
        )
