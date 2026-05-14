#!/usr/bin/env python3
"""Public API for the Agent Evaluation Harness.

Usage::

    from eval import run_task, run_suite, load_tasks, EvalTask, EvalResult, SuiteReport

    tasks = load_tasks()
    result = run_task(tasks[0])
    print(result.success)
"""

from eval.runner import (
    EvalTask,
    EvalResult,
    SuiteReport,
    run_task,
    run_suite,
    load_tasks,
    parse_task_from_yaml,
)
from eval.scorer import CheckResult, run_checks
from eval.metrics import MetricsCollector

__all__ = [
    "EvalTask",
    "EvalResult",
    "SuiteReport",
    "CheckResult",
    "run_task",
    "run_suite",
    "load_tasks",
    "parse_task_from_yaml",
    "run_checks",
    "MetricsCollector",
]
