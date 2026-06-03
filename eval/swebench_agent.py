#!/usr/bin/env python3
"""
SWE-bench Agent Script for mini_agent.

This script implements the standard SWE-bench agent interface for use with
the official SWE-bench evaluation harness (Docker-based).

When the official harness runs, it:
  1. Creates a Docker container with the repo checked out at base_commit
  2. Sets environment variables with task data
  3. Runs this script
  4. Captures stdout (the agent's patch) for evaluation

Environment variables provided by the harness:
  - SWE_TASK_ID: instance_id (e.g. "django__django-11049")
  - SWE_TASK: JSON string with full task data
     (problem_statement, repo, base_commit, FAIL_TO_PASS, PASS_TO_PASS, etc.)

Usage (manual testing):
  SWE_TASK_ID="test__task-1" \\
  SWE_TASK='{"problem_statement":"Fix the off-by-one bug in counter.py","repo":"test/test","base_commit":"abc123"}' \\
  python eval/swebench_agent.py

Output:
  Prints the git diff (patch) to stdout.
  Exit code 0 on success, non-zero on error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time


def main() -> None:
    """Main entry point for SWE-bench harness."""
    # Read task from environment
    task_id = os.environ.get("SWE_TASK_ID", "unknown")
    task_json = os.environ.get("SWE_TASK", "{}")

    try:
        task_data = json.loads(task_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse SWE_TASK JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    problem_statement = task_data.get("problem_statement", "")
    repo = task_data.get("repo", "")
    base_commit = task_data.get("base_commit", "")

    print(f"[{task_id}] Starting mini_agent on {repo} @ {base_commit[:8]}...",
          file=sys.stderr)

    # Working directory should already be the checked-out repo
    workspace = os.getcwd()

    # Ensure it's a git repo with initial commit
    _ensure_git_repo(workspace)

    # Initialize agent session
    try:
        from core.config import init_session
        from core.llm import run_agent_turn
        from memory.memory import _total_tokens
    except ImportError as exc:
        print(f"ERROR: Failed to import mini_agent modules: {exc}", file=sys.stderr)
        sys.exit(1)

    session = init_session(workspace)
    config = session["config"]
    config.unrestricted = True
    config.allow_overwrites = True
    config.stream = False
    config.verbose = False

    # Build prompt
    prompt = _build_prompt(problem_statement, task_data.get("hints_text", ""))
    session["messages"].append({
        "role": "user",
        "content": prompt,
    })

    # Run agent
    cancel_event = threading.Event()
    start_time = time.monotonic()

    try:
        result_msg = run_agent_turn(
            messages=session["messages"],
            config=config,
            write_gate=session["write_gate"],
            read_gate=session["read_gate"],
            cancel_event=cancel_event,
            max_turns=100,
            session=session["session"],
            memory_store=session["memory"],
        )
    except Exception as exc:
        print(f"[{task_id}] ERROR during agent run: {exc}", file=sys.stderr)
        # Even if agent fails, output whatever diff we have
        pass

    elapsed = time.monotonic() - start_time
    turns = result_msg.get("_turn_count", 0) if result_msg else 0
    tokens = _total_tokens(session["messages"])

    # Collect the patch
    diff_result = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True, text=True,
        cwd=workspace, timeout=30,
    )
    patch = diff_result.stdout

    print(f"[{task_id}] Done in {elapsed:.0f}s, {turns} turns, {tokens} tokens, "
          f"patch: {len(patch)} bytes",
          file=sys.stderr)

    # Output the patch to stdout (SWE-bench harness captures this)
    print(patch)


def _build_prompt(problem_statement: str, hints_text: str = "") -> str:
    """Build the agent prompt from SWE-bench task data."""
    parts = [
        "You are an expert software engineer fixing a real bug in a codebase.",
        "",
        "## GitHub Issue",
        problem_statement,
        "",
        "## Instructions",
        "1. Explore the codebase to understand the relevant code",
        "2. Find the root cause of the issue described above",
        "3. Fix the bug by editing the appropriate files",
        "4. If possible, run relevant tests to verify your fix",
        "5. When done, summarize your changes clearly",
        "",
        "Work carefully and methodically. Read files before editing them.",
        "Use the available tools to search, read, write, and test.",
    ]
    if hints_text:
        parts.extend(["", "## Hints", hints_text])
    return "\n".join(parts)


def _ensure_git_repo(workspace: str) -> None:
    """Ensure workspace is a git repo with an initial commit."""
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
    _run(["git", "config", "user.name", "SWE-bench Agent"], timeout=5)
    _run(["git", "add", "-A"])
    _run(["git", "commit", "--allow-empty", "-m", "Initial state for SWE-bench eval"])


if __name__ == "__main__":
    main()
