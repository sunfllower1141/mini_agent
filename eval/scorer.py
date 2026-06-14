#!/usr/bin/env python3
"""Scoring module for the Agent Evaluation Harness.

Provides all 8 checker types and the run_checks() orchestrator.
Each checker runs against a workspace directory after the agent finishes.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class CheckResult:
    """Outcome of a single check."""

    check_type: str
    passed: bool
    detail: str


_CHECKERS: dict[str, callable] = {}


def _register(name: str):
    """Decorator: register a checker function in the dispatch table."""

    def dec(fn):
        _CHECKERS[name] = fn
        return fn

    return dec


def run_checks(checks: list[dict], workspace: str) -> list[CheckResult]:
    """Run all checkers against the workspace. Order is preserved.

    Args:
        checks: List of check dicts from task YAML (each has 'type' + params).
        workspace: Absolute path to the workspace directory.

    Returns:
        List of CheckResult, one per check.
    """
    results: list[CheckResult] = []
    for check in checks:
        check_type = check.get("type", "unknown")
        checker = _CHECKERS.get(check_type)
        if checker is None:
            results.append(
                CheckResult(check_type, False, f"Unknown checker type: {check_type}")
            )
            continue
        try:
            results.append(checker(check, workspace))
        except Exception as exc:
            results.append(
                CheckResult(check_type, False, f"Checker error: {exc}")
            )
    return results


# ---------------------------------------------------------------------------
# Checker implementations
# ---------------------------------------------------------------------------


@_register("file_exists")
def _check_file_exists(params: dict, workspace: str) -> CheckResult:
    path = os.path.join(workspace, params["path"])
    ok = os.path.isfile(path)
    return CheckResult(
        "file_exists", ok, f"{params['path']} {'exists' if ok else 'missing'}"
    )


@_register("file_not_exists")
def _check_file_not_exists(params: dict, workspace: str) -> CheckResult:
    path = os.path.join(workspace, params["path"])
    ok = not os.path.isfile(path)
    return CheckResult(
        "file_not_exists",
        ok,
        f"{params['path']} {'absent' if ok else 'exists (should not)'}",
    )


@_register("file_contains")
def _check_file_contains(params: dict, workspace: str) -> CheckResult:
    path = os.path.join(workspace, params["path"])
    if not os.path.isfile(path):
        return CheckResult("file_contains", False, f"{params['path']} missing")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return CheckResult("file_contains", False, f"Cannot read {params['path']}: {exc}")
    match = re.search(params["pattern"], content, re.MULTILINE)
    return CheckResult(
        "file_contains",
        bool(match),
        f"pattern '{params['pattern']}' {'found' if match else 'not found'} in {params['path']}",
    )


@_register("file_not_contains")
def _check_file_not_contains(params: dict, workspace: str) -> CheckResult:
    path = os.path.join(workspace, params["path"])
    if not os.path.isfile(path):
        return CheckResult("file_not_contains", False, f"{params['path']} missing")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return CheckResult("file_not_contains", False, f"Cannot read {params['path']}: {exc}")
    match = re.search(params["pattern"], content, re.MULTILINE)
    ok = not match
    return CheckResult(
        "file_not_contains",
        ok,
        f"pattern '{params['pattern']}' {'absent (good)' if ok else 'found (should not be)'} in {params['path']}",
    )


@_register("test_passes")
def _check_test_passes(params: dict, workspace: str) -> CheckResult:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", params["path"], "-q", "--tb=short"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    ok = result.returncode == 0
    detail = result.stdout[-500:] or result.stderr[-500:]
    return CheckResult("test_passes", ok, detail.strip() or f"rc={result.returncode}")


@_register("diff_contains")
def _check_diff_contains(params: dict, workspace: str) -> CheckResult:
    result = subprocess.run(
        ["git", "diff"], capture_output=True, text=True, cwd=workspace, timeout=10
    )
    fragment = params["fragment"]
    ok = fragment in result.stdout
    return CheckResult(
        "diff_contains", ok, "fragment found in diff" if ok else "fragment not in diff"
    )


@_register("diff_not_contains")
def _check_diff_not_contains(params: dict, workspace: str) -> CheckResult:
    result = subprocess.run(
        ["git", "diff"], capture_output=True, text=True, cwd=workspace, timeout=10
    )
    fragment = params["fragment"]
    ok = fragment not in result.stdout
    return CheckResult(
        "diff_not_contains",
        ok,
        "fragment absent from diff (good)" if ok else "fragment found in diff (should not be)",
    )


@_register("shell")
def _check_shell(params: dict, workspace: str) -> CheckResult:
    # Security: tokenize the command and run with shell=False to avoid
    # shell injection from YAML-defined commands.
    try:
        cmd = shlex.split(params["command"])
    except ValueError as exc:
        return CheckResult("shell", False, f"Invalid shell command syntax: {exc}")
    result = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        cwd=workspace,
        timeout=60,
    )
    expected_rc = params.get("expected_returncode", 0)
    rc_ok = result.returncode == expected_rc
    stdout_ok = True
    if "expected_stdout" in params:
        stdout_ok = bool(re.search(params["expected_stdout"], result.stdout))
    ok = rc_ok and stdout_ok
    detail = f"rc={result.returncode} (expected {expected_rc})"
    if not stdout_ok:
        detail += f"; stdout did not match '{params['expected_stdout']}'"
    return CheckResult("shell", ok, detail)
