#!/usr/bin/env python3
"""
context_inject.py -- per-turn context injection for the agent orchestrator.

Injects scratchpad, git diff, orchestration status, progress reminders,
circuit breaker warnings, self-critique, and strategy hints into the
message list before each LLM API call.

Extracted from llm.py to keep the orchestrator focused on the main loop.
"""

from __future__ import annotations

import os
import subprocess as _sp
import sys
import threading
from collections import deque
from typing import Any

from api import APIError
from .safety import ReadSafetyGate
from tools import _TOOL_CONTEXT, get_modified_files
from logging_setup import get_logger
from interject import poll_interjections

_log = get_logger("context_inject")

# ---------------------------------------------------------------------------
# Named constants for context injection intervals
# ---------------------------------------------------------------------------


import json
import fnmatch
from collections import Counter

# Circuit breaker constants and helpers (shared with llm.py)
_CIRCUIT_WINDOW: int = 6
_CIRCUIT_THRESHOLD: int = 3

# Dead-tool pruning: after this many turns, deactivate skills whose tools
# have never been used.  Reduces API payload by ~500-2000 tokens and
# stabilizes the KV-cache prefix (tool definitions stop changing).
_DEAD_TOOL_PRUNE_TURN: int = 5
_MIN_PRUNE_COUNT: int = 3  # must have at least this many unused tools to prune

def _tool_call_key(tc: dict) -> str:
    """Stable hash key for a tool call: name + normalized args."""
    fn = tc["function"]
    name = fn["name"]
    try:
        args_normalized = json.dumps(
            json.loads(fn["arguments"]), sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        args_normalized = fn["arguments"]
    return f"{name}:{args_normalized}"

def _check_circuit(recent_keys: list[str]) -> str | None:
    """Return a warning message if the circuit is tripped, otherwise None."""
    if len(recent_keys) < _CIRCUIT_THRESHOLD:
        return None
    counts = Counter(recent_keys)
    for key, count in counts.items():
        if count >= _CIRCUIT_THRESHOLD:
            return (
                f"WARNING: Circuit breaker: you have called '{key}' {count} times "
                f"in the last {len(recent_keys)} tool calls. "
                "The same call keeps being made with identical arguments. "
                "Stop, diagnose why it isn't working, and try a different "
                "approach rather than repeating it."
            )
    return None

SUB_AGENT_RESULT_PREVIEW = 120  # max chars for sub-agent result in context message

# Context injection intervals
PROGRESS_INTERVAL = 5               # turns between progress reminders
SCRATCHPAD_NUDGE_START_TURN = 5     # first turn to check scratchpad staleness
SCRATCHPAD_NUDGE_INTERVAL = 3       # interval for scratchpad staleness nudge
MODIFIED_FILES_CHECKPOINT_TURN = 2  # turn to show modified-files checkpoint



# One-time context injection flags are stored on _TOOL_CONTEXT
# (_scratchpad_injected, _git_diff_injected) -- no module-level globals.


# ---------------------------------------------------------------------------
# Context injection helpers -- each appends one kind of context message.
# ---------------------------------------------------------------------------


def _inject_handoff_context(
    messages: list[dict], *, workspace_root: str = "",
) -> None:
    """Inject HANDOFF.md content at session start (one-time per session)."""
    if _TOOL_CONTEXT._handoff_injected or not workspace_root:
        return
    _TOOL_CONTEXT._handoff_injected = True
    handoff_path = os.path.join(workspace_root, "HANDOFF.md")
    if not os.path.isfile(handoff_path):
        return
    try:
        with open(handoff_path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return
    if not content:
        return
    messages.append({
        "role": "user",
        "content": (
            "Session handoff from your previous session "
            "(you wrote this at the end of last session):\n\n"
            + content
        ),
        "_transient": True,
    })


def _inject_state_context(
    messages: list[dict], *, workspace_root: str = "",
) -> None:
    """Inject STATE.txt content at session start (one-time per session)."""
    if _TOOL_CONTEXT._state_txt_injected or not workspace_root:
        return
    _TOOL_CONTEXT._state_txt_injected = True
    state_path = os.path.join(workspace_root, "STATE.txt")
    if not os.path.isfile(state_path):
        return
    try:
        with open(state_path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return
    if not content:
        return
    messages.append({
        "role": "user",
        "content": (
            "Architecture state from your last session "
            "(you maintain this as your map of the codebase):\n\n"
            + content
        ),
        "_transient": True,
    })


def _inject_tasks_context(
    messages: list[dict], *, workspace_root: str = "",
) -> None:
    """Inject TASKS.md task-to-file index at session start (one-time)."""
    if getattr(_TOOL_CONTEXT, "_tasks_injected", False) or not workspace_root:
        return
    _TOOL_CONTEXT._tasks_injected = True
    tasks_path = os.path.join(workspace_root, "TASKS.md")
    if not os.path.isfile(tasks_path):
        return
    try:
        with open(tasks_path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return
    if not content:
        return
    messages.append({
        "role": "user",
        "content": (
            "Task-to-file index for this codebase (read to orient yourself "
            "when starting a new task):\n\n"
            + content
        ),
        "_transient": True,
    })


def _inject_core_memory_context(
    messages: list[dict], *, memory_store: Any = None,
) -> None:
    """Inject core memory at session start (one-time, frozen snapshot).

    Core memory is a bounded, consolidated summary of what the agent
    has learned across sessions. Injected ONCE at session start so the
    agent benefits from past learning without burning tokens every turn.
    """
    if memory_store is None:
        return
    if getattr(_TOOL_CONTEXT, "_core_memory_injected", False):
        return
    _TOOL_CONTEXT._core_memory_injected = True
    try:
        core_content = memory_store.get_core_memory()
        if not core_content or not core_content.strip():
            return
        messages.append({
            "role": "user",
            "content": (
                "[CORE MEMORY -- persistent learnings from past sessions]\n"
                "You wrote this in a previous session to help your future self. "
                "Use it to avoid re-discovering things:\n\n"
                + core_content.strip()
            ),
            "_transient": True,
        })
    except Exception:
        pass  # best-effort; never break the session over memory


def _inject_scratchpad_context(
    messages: list[dict], *, memory_store: Any = None,
) -> None:
    """Inject current scratchpad content (one-time per session)."""
    if _TOOL_CONTEXT._scratchpad_injected or memory_store is None:
        return
    _TOOL_CONTEXT._scratchpad_injected = True
    scratchpad = memory_store.get_scratchpad()
    if scratchpad.strip():
        messages.append({
            "role": "user",
            "content": (
                "Your scratchpad (current working notes -- use write_scratchpad "
                "to update):\n\n" + scratchpad
            ),
            "_transient": True,
        })


def _inject_git_diff(
    messages: list[dict], *, memory_store: Any = None,
    read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject recent git diff (one-time per session)."""
    if _TOOL_CONTEXT._git_diff_injected or memory_store is None or read_gate is None:
        return
    _TOOL_CONTEXT._git_diff_injected = True
    try:
        result = _sp.run(
            ["git", "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=5,
            cwd=read_gate.workspace_root,
        )
        if result.stdout.strip():
            messages.append({
                "role": "user",
                "content": (
                    "Recent git changes since last commit:\n\n"
                    + result.stdout.strip()
                    + "\n\nFocus on these files first when making changes."
                ),
                "_transient": True,
            })
    except (OSError, _sp.TimeoutExpired) as exc:
        print(f"  WARNING: git diff failed: {exc}", file=sys.stderr, flush=True)


def _inject_orchestration_context(messages: list[dict]) -> None:
    """Inject sub-agent orchestration status ONLY when state changes.

    Suppresses injection when nothing new happened since last turn:
    - No new completions (pending_results unchanged)
    - No new broadcast messages
    - No auto-extensions performed
    - Running set unchanged

    This prevents ~500-1000 tokens of orchestration overhead per turn
    when sub-agents are steadily working but nothing has completed yet.
    """
    try:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            return
        running_ids = runtime.get_running_ids()
        pending = runtime.get_pending_results()
        if not running_ids and not pending:
            return

        # --- Compute a state fingerprint to detect changes ---
        pending_fp = tuple(
            (tid, r.success, r.content[:80])
            for tid, r in pending
        )
        running_fp = tuple(sorted(running_ids))
        all_msgs = getattr(runtime, "messages", None)
        msg_fp = len(all_msgs) if all_msgs is not None else 0

        # Check if anything changed since last injection
        last_state = getattr(_TOOL_CONTEXT, "_last_orch_state", None)
        current_state = (pending_fp, running_fp, msg_fp)
        if last_state == current_state and not (
            # Always inject if running_ids is non-empty on first turn or if
            # there are pending results we haven't reported yet
            pending and getattr(_TOOL_CONTEXT, "_last_pending_reported", 0) < len(pending)
        ):
            return
        _TOOL_CONTEXT._last_orch_state = current_state
        _TOOL_CONTEXT._last_pending_reported = len(pending)

        parts: list[str] = []
        if pending:
            parts.append("Sub-agent(s) COMPLETED since your last turn:")
            for tid, result in pending:
                status = "OK" if result.success else "FAILED"
                parts.append(
                    f"  - {tid}: [{status}] {result.content[:SUB_AGENT_RESULT_PREVIEW]}"
                    f"{'...' if len(result.content) > SUB_AGENT_RESULT_PREVIEW else ''}"
                )
            parts.append("")
        if running_ids:
            parts.append(
                f"{len(running_ids)} sub-agent(s) still RUNNING: "
                f"{', '.join(running_ids)}"
            )
            parts.append(
                "Use agent_status() to check each or collect_any() to grab "
                "the first result. Do NOT redo their work."
            )
        # --- Inject new broadcast messages from sub-agents ---
        if all_msgs is not None:
            msg_count = len(all_msgs)
            last_seen = getattr(_TOOL_CONTEXT, "_last_msg_count", 0)
            if msg_count > last_seen:
                new_msgs = all_msgs[last_seen:]
                _TOOL_CONTEXT._last_msg_count = msg_count
                parts.append("New message(s) from sub-agents:")
                for m in new_msgs[-5:]:  # cap at 5 most recent
                    text = m.get('text', '')
                    sender = m.get('from', '')
                    parts.append(f"  [{sender}] {text[:200]}")
                if len(new_msgs) > 5:
                    parts.append(f"  ... ({len(new_msgs)-5} more)")
                parts.append("Use agent_inbox or agent_read to view full messages.")
        # --- Auto-extend productive sub-agents running low on turns ---
        if running_ids:
            for tid in running_ids:
                status_snap = runtime.get_snapshot(tid) or {}
                turns_budget = status_snap.get("turns_budget", 0)
                current_turn = status_snap.get("turn", 0)
                remaining = turns_budget - current_turn
                if remaining <= 3 and remaining > 0:
                    # Only extend if agent is making forward progress
                    last_action = status_snap.get("last_action", "")
                    if last_action and last_action != "idle":
                        runtime.extend_turns(tid, 10)
                        parts.append(f"  [LOOP] Auto-extended '{tid}' (+10 turns, {remaining} left)")
        if parts:
            messages.append({
                "role": "user",
                "content": "\n".join(parts),
                "_transient": True,
            })
    except (APIError, AttributeError, KeyError, ValueError, TypeError) as exc:
        _log.warning("orchestration context failed: %s", exc)


def _inject_interjections(messages: list[dict]) -> None:
    """Inject any pending user interjections (every turn)."""
    interjections = poll_interjections()
    for msg_text in interjections:
        messages.append({
            "role": "user",
            "content": msg_text,
        })


# Consecutive read-only turn counter lives on _TOOL_CONTEXT (no module-level global).
_READ_ONLY_NUDGE_THRESHOLD: int = 3  # turns of pure reads before nudge


def _inject_progress_check(messages: list[dict], *, turn_count: int) -> None:
    """Inject periodic progress reminder every PROGRESS_INTERVAL turns.

    Also injects a sufficiency nudge when the agent has spent several
    consecutive turns only reading (no writes / shell executions).
    """
    if turn_count <= 1:
        return

    # --- Read-only sufficiency nudge (every turn, once threshold reached) ---
    if _TOOL_CONTEXT._consecutive_read_only_turns >= _READ_ONLY_NUDGE_THRESHOLD:
        messages.append({
            "role": "user",
            "content": (
                f"You've spent {_TOOL_CONTEXT._consecutive_read_only_turns} turns reading "
                "code without making changes. If you have enough context to "
                "answer the user, do so NOW. If you need more, state what "
                "SPECIFIC information you're still missing -- don't just keep "
                "reading files broadly."
            ),
            "_transient": True,
        })

    if turn_count % PROGRESS_INTERVAL != 0:
        return
    reminder = (
        f"You have been working for {turn_count} turns. "
        "Briefly assess your progress: are you making headway, "
        "stuck in a loop, or done? If you can wrap up now, "
        "give the final answer. If you truly need more turns, "
        "continue -- but be specific about what remains."
    )
    messages.append({"role": "user", "content": reminder, "_transient": True})


def _inject_modified_files_checkpoint(
    messages: list[dict], *, read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject modified-files checkpoint (turn 2 only)."""
    modified = get_modified_files()
    if not modified:
        return
    mod_list = "\n".join(f"  - {f}" for f in modified)
    test_hint = ""
    for mf in modified:
        base = os.path.basename(mf)
        if base.startswith("test_") and base.endswith(".py"):
            test_hint += f"\n  Relevant test: {base}"
        elif base.endswith(".py") and not base.startswith("test_"):
            candidate = f"test_{base}"
            dp = os.path.dirname(mf)
            test_path = os.path.join(dp, candidate) if dp else candidate
            if read_gate and os.path.isfile(os.path.join(read_gate.workspace_root, test_path)):
                test_hint += f"\n  Relevant test: {test_path}"

    # Knowledge graph caller analysis for modified files
    kg_hint = ""
    workspace = read_gate.workspace_root if read_gate else ""
    if workspace:
        try:
            from core.knowledge_graph import ensure_graph_built, find_callers_of_file
            if ensure_graph_built(workspace):
                caller_summaries: list[str] = []
                for mf in modified[:5]:  # max 5 files
                    if not mf.endswith(".py"):
                        continue
                    callers = find_callers_of_file(mf)
                    if callers:
                        # Show top 2 caller files
                        cf_parts = []
                        for cf in callers[:2]:
                            names = cf["callers"][:2]
                            names_str = ", ".join(names)
                            if len(cf["callers"]) > 2:
                                names_str += f" (+{len(cf['callers']) - 2})"
                            cf_parts.append(f"{cf['file']}({names_str})")
                        caller_summaries.append(f"  {mf} -> called by: {', '.join(cf_parts)}")
                if caller_summaries:
                    kg_hint = "\nCaller impact analysis:\n" + "\n".join(caller_summaries)
        except Exception:
            pass

    ckpt = (
        f"Files modified this session:\n{mod_list}\n"
        f"Running `verify` or `run_tests`{test_hint if test_hint else ''} "
        f"after changes is recommended."
        f"{kg_hint}"
    )
    messages.append({"role": "user", "content": ckpt, "_transient": True})


def _inject_circuit_breaker(
    messages: list[dict], *, recent_tool_keys: deque[str] | None = None,
) -> None:
    """Inject circuit breaker warning if recent calls are repetitive."""
    if recent_tool_keys is None:
        return
    warning = _check_circuit(recent_tool_keys)
    if warning:
        messages.append({"role": "user", "content": warning, "_transient": True})


def _inject_cache_degradation_alert(messages: list[dict]) -> None:
    """Inject a warning if DeepSeek prompt cache hit rate has dropped sharply.

    The alert is only injected when degradation is detected (most turns:
    nothing happens, zero cost).  Uses the per-turn cache tracking in
    api.py:_report_cache_hit.
    """
    try:
        from api import _check_cache_degradation
        alert = _check_cache_degradation()
        if alert:
            messages.append({"role": "user", "content": alert, "_transient": True})
    except Exception:
        pass  # never block the turn on cache monitoring failure


def _inject_post_edit_verification(messages: list[dict]) -> None:
    """Inject post-edit verification suggestions when files have been modified.

    Uses knowledge graph to find callers that may need re-verification
    after changes.  Runs every turn after the modified-files checkpoint
    turn, but only when there are modified files.
    """
    modified = get_modified_files()
    if not modified:
        return

    workspace = None
    read_gate = getattr(_TOOL_CONTEXT, "_read_gate", None)
    if read_gate and hasattr(read_gate, "workspace_root"):
        workspace = read_gate.workspace_root
    if not workspace:
        return

    # Only inject if there are non-test Python files modified
    py_files = [f for f in modified if f.endswith(".py") and not os.path.basename(f).startswith("test_")]
    if not py_files:
        return

    # Inject more frequently: every 6 turns OR whenever there's a new edit
    # since last verification check.
    turn = getattr(_TOOL_CONTEXT, "_turn_count", 0)
    last_verified = getattr(_TOOL_CONTEXT, "_last_verification_turn", 0)
    edits_since_last = turn > last_verified and modified != getattr(
        _TOOL_CONTEXT, "_last_verified_modified", set()
    )

    if turn <= MODIFIED_FILES_CHECKPOINT_TURN:
        return
    if (turn - MODIFIED_FILES_CHECKPOINT_TURN) % 6 != 0 and not edits_since_last:
        return

    # Update tracking
    _TOOL_CONTEXT._last_verification_turn = turn
    _TOOL_CONTEXT._last_verified_modified = set(modified)

    lines: list[str] = []
    lines.append("Post-edit verification -- files modified this session:")

    # Test coverage: show which modified files have/haven't test coverage
    for mf in py_files[:5]:
        base = os.path.basename(mf)
        candidate = f"test_{base}"
        dp = os.path.dirname(mf)
        test_path = os.path.join(dp, candidate) if dp else candidate
        has_test = os.path.isfile(os.path.join(workspace, test_path))
        if not has_test:
            alt_path = os.path.join("tests", candidate)
            has_test = os.path.isfile(os.path.join(workspace, alt_path))
        status = "[OK] has test" if has_test else "WARNING: no test found"
        lines.append(f"  {mf} -- {status}")

    # Knowledge graph: show affected callers across all modified files
    try:
        from core.knowledge_graph import ensure_graph_built, find_callers_of_file
        if ensure_graph_built(workspace):
            all_callers: dict[str, list[str]] = {}
            for mf in py_files[:5]:
                callers = find_callers_of_file(mf)
                for cf in callers:
                    f = cf["file"]
                    if f not in all_callers:
                        all_callers[f] = []
                    all_callers[f].extend(cf["callers"])
            if all_callers:
                # Deduplicate and sort
                unique_callers = {f: sorted(set(names)) for f, names in all_callers.items()}
                lines.append("\nAffected callers (may need verification):")
                for f, names in sorted(unique_callers.items())[:5]:
                    names_str = ", ".join(names[:3])
                    if len(names) > 3:
                        names_str += f" (+{len(names) - 3})"
                    lines.append(f"  {f}: {names_str}")
    except Exception:
        pass

    lines.append("\nConsider running `verify` or `run_tests` for affected files.")
    messages.append({
        "role": "user",
        "content": "\n".join(lines),
        "_transient": True,
    })


# ---------------------------------------------------------------------------
# Pre-edit risk briefing
# ---------------------------------------------------------------------------

# How many recent commits to check for co-change analysis
_EDIT_RISK_GIT_DEPTH = 10
# Max co-changed files to show
_EDIT_RISK_MAX_COCHANGES = 5


def _inject_edit_risk_context(messages: list[dict]) -> None:
    """Inject a risk briefing when the agent is about to edit files.

    Scans the most recent assistant message for edit_file / write_file
    tool calls and checks git history for:
      - Recent modification frequency (hotspot detection)
      - Co-changed files (files that change in the same commits)
      - Missing test coverage
      - Knowledge graph caller analysis (which callers may break)
      - Git blame (who last touched these lines)

    Only injects when there are pending edits -- zero tokens otherwise.
    """
    # Find the most recent assistant message with tool calls
    edit_targets: list[str] = []
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls", [])
        if not tool_calls:
            continue
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name in ("edit_file", "write_file"):
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    continue
                path = args.get("path", "")
                if path:
                    edit_targets.append(path)
        break  # Only scan the most recent assistant message

    if not edit_targets:
        return

    workspace = None
    read_gate = getattr(_TOOL_CONTEXT, "_read_gate", None)
    if read_gate and hasattr(read_gate, "workspace_root"):
        workspace = read_gate.workspace_root
    if not workspace:
        return

    # Lazy-build knowledge graph for caller analysis
    _kg_available = False
    try:
        from core.knowledge_graph import ensure_graph_built, find_callers_of_file
        _kg_available = ensure_graph_built(workspace)
    except Exception:
        _kg_available = False

    lines: list[str] = []
    for target in edit_targets[:3]:  # max 3 files to avoid bloat
        full_path = os.path.join(workspace, target) if not os.path.isabs(target) else target
        if not os.path.isfile(full_path):
            continue

        risk_items: list[str] = []

        # 1. Recent git changes to this file
        try:
            r = _sp.run(
                ["git", "-C", workspace, "log", f"-{_EDIT_RISK_GIT_DEPTH}",
                "--oneline", "--", target],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                commit_lines = r.stdout.strip().split("\n")
                count = len(commit_lines)
                if count >= 3:
                    risk_items.append(f"modified in {count}/{_EDIT_RISK_GIT_DEPTH} recent commits WARNING:")
                elif count > 0:
                    risk_items.append(f"modified in {count} recent commit(s)")
        except (OSError, _sp.TimeoutExpired):
            pass

        # 2. Co-change analysis: files modified in same commits
        try:
            r = _sp.run(
                ["git", "-C", workspace, "log", f"-{_EDIT_RISK_GIT_DEPTH}",
                "--format=", "--name-only", "--", target],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                all_files: set[str] = set()
                for line in r.stdout.strip().split("\n"):
                    line = line.strip()
                    if line and line != target and "/" in line:
                        all_files.add(line)
                if all_files:
                    shown = sorted(all_files)[:_EDIT_RISK_MAX_COCHANGES]
                    cochange = ", ".join(shown)
                    if len(all_files) > _EDIT_RISK_MAX_COCHANGES:
                        cochange += f" (+{len(all_files) - _EDIT_RISK_MAX_COCHANGES} more)"
                    risk_items.append(f"co-changes with: {cochange}")
        except (OSError, _sp.TimeoutExpired):
            pass

        # 3. Test coverage check
        base = os.path.basename(target)
        if base.endswith(".py") and not base.startswith("test_"):
            candidate = f"test_{base}"
            dp = os.path.dirname(target)
            test_path = os.path.join(dp, candidate) if dp else candidate
            if not os.path.isfile(os.path.join(workspace, test_path)):
                # Also check tests/ directory
                alt_path = os.path.join("tests", candidate)
                if not os.path.isfile(os.path.join(workspace, alt_path)):
                    risk_items.append("no test file found WARNING:")

        # 4. Knowledge graph caller analysis (if graph is available)
        if _kg_available and target.endswith(".py"):
            try:
                callers = find_callers_of_file(target)
                if callers:
                    # Cap to 3 caller files to keep context tight
                    caller_parts: list[str] = []
                    for cf in callers[:3]:
                        names = cf["callers"][:3]
                        names_str = ", ".join(names)
                        if len(cf["callers"]) > 3:
                            names_str += f" (+{len(cf['callers']) - 3} more)"
                        caller_parts.append(f"{cf['file']} ({names_str})")
                    caller_summary = "; ".join(caller_parts)
                    if len(callers) > 3:
                        caller_summary += f" (+{len(callers) - 3} more files)"
                    risk_items.append(f"callers: {caller_summary} -- verify after changes")
            except Exception:
                pass

        # 5. Git blame -- who last modified this file
        try:
            r = _sp.run(
                ["git", "-C", workspace, "blame", "--line-porcelain", target],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0 and r.stdout.strip():
                # Parse porcelain blame: extract author lines
                authors: dict[str, int] = {}
                for line in r.stdout.split("\n"):
                    if line.startswith("author "):
                        author = line[7:].strip()
                        authors[author] = authors.get(author, 0) + 1
                if authors:
                    # Show top authors by line count
                    top = sorted(authors.items(), key=lambda x: -x[1])[:3]
                    author_parts = [f"{a} ({c} lines)" for a, c in top]
                    risk_items.append(f"authors: {', '.join(author_parts)}")
        except (OSError, _sp.TimeoutExpired):
            pass

        if risk_items:
            lines.append(f"  {target}: {'; '.join(risk_items)}")

    if lines:
        messages.append({
            "role": "user",
            "content": (
                "Pre-edit risk briefing for files you're about to modify:\n"
                + "\n".join(lines)
                + "\n\nProceed with appropriate caution."
            ),
            "_transient": True,
        })


# ---------------------------------------------------------------------------
# File-pattern conditional rules
# ---------------------------------------------------------------------------

# Cache for loaded pattern rules: list of (pattern, instruction, rule_name)
_PATTERN_RULES: list[tuple[str, str, str]] | None = None
# Set of pattern names already injected this session (avoid repeats)
_PATTERN_RULES_INJECTED: set[str] = set()


def _reset_pattern_rules() -> None:
    """Reset pattern rules state for a new session."""
    _PATTERN_RULES_INJECTED.clear()
    global _PATTERN_RULES
    _PATTERN_RULES = None  # force re-load on next session


def _load_pattern_rules(workspace_root: str) -> list[tuple[str, str]]:
    """Load file-pattern conditional rules from .mini_agent/rules.toml.

    Returns list of (pattern, instruction) tuples. Cached globally.
    Patterns use fnmatch glob syntax.
    """
    global _PATTERN_RULES
    if _PATTERN_RULES is not None:
        return _PATTERN_RULES

    _PATTERN_RULES = []
    rules_path = os.path.join(workspace_root, ".mini_agent", "rules.toml")
    if not os.path.isfile(rules_path):
        return _PATTERN_RULES

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return _PATTERN_RULES

    try:
        with open(rules_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        _log.debug("Failed to parse pattern rules: %s", rules_path)
        return _PATTERN_RULES

    rules_table = data.get("rules", {})
    for rule_name, rule_def in rules_table.items():
        pattern = rule_def.get("pattern", "")
        instruction = rule_def.get("instruction", "").strip()
        if pattern and instruction:
            _PATTERN_RULES.append((pattern, instruction, rule_name))

    _log.debug("Loaded %d pattern rules from %s", len(_PATTERN_RULES), rules_path)
    return _PATTERN_RULES


def _inject_pattern_rules(messages: list[dict]) -> None:
    """Inject pattern-conditional rules when relevant files are being touched.

    Scans recent assistant messages for file operations and matches
    target files against pattern rules from .mini_agent/rules.toml.
    Only injects rules that haven't been injected this session.
    """
    global _PATTERN_RULES_INJECTED

    if not _PATTERN_RULES:
        workspace = None
        read_gate = getattr(_TOOL_CONTEXT, "_read_gate", None)
        if read_gate and hasattr(read_gate, "workspace_root"):
            workspace = read_gate.workspace_root
        if not workspace:
            return
        _load_pattern_rules(workspace)
        if not _PATTERN_RULES:
            return

    # Collect file paths from recent tool calls
    file_paths: set[str] = set()
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name not in ("read_file", "edit_file", "write_file",
                            "find_symbol", "search_files", "file_info",
                            "list_directory", "run_shell", "run_tests"):
                continue
            try:
                if "arguments" not in fn:
                    continue
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                continue
            # Collect path, file_path, or target args
            for key in ("path", "file_path", "target"):
                val = args.get(key, "")
                if val and isinstance(val, str):
                    file_paths.add(val)

    if not file_paths:
        return

    # Find matching rules that haven't been injected yet
    to_inject: list[str] = []
    for pattern, instruction, rule_name in _PATTERN_RULES:
        if rule_name in _PATTERN_RULES_INJECTED:
            continue
        # Check if any file path matches the pattern
        matched = False
        for fp in file_paths:
            if fnmatch.fnmatch(fp, pattern):
                matched = True
                break
        if matched:
            to_inject.append(instruction)
            _PATTERN_RULES_INJECTED.add(rule_name)

    if to_inject:
        joined = "\n\n".join(to_inject)
        messages.append({
            "role": "user",
            "content": f"Relevant project rules for your current work:\n\n{joined}",
            "_transient": True,
        })


def _inject_scratchpad_nudge(messages: list[dict], *, turn_count: int) -> None:
    """Inject scratchpad staleness nudge every SCRATCHPAD_NUDGE_INTERVAL turns
    after SCRATCHPAD_NUDGE_START_TURN."""
    if turn_count < SCRATCHPAD_NUDGE_START_TURN:
        return
    if (turn_count - 1) % SCRATCHPAD_NUDGE_INTERVAL != 0:
        return
    if not _TOOL_CONTEXT._scratchpad_updated:
        messages.append({
            "role": "user",
            "content": (
                "WARNING: Your scratchpad hasn't been updated in several turns. "
                "Consider using write_scratchpad to capture your current "
                "plan, progress, and decisions before continuing.\n\n"
                "Good scratchpad format:\n"
                "  GOAL: [1 line -- what the user wants]\n"
                "  DONE: [what you've accomplished so far]\n"
                "  NEXT: [exactly what you'll do next turn -- be specific]\n"
                "  QUESTIONS: [anything you're uncertain about]\n"
                "Keep it short -- this is for YOUR memory, not the user."
            ),
            "_transient": True,
        })
    _TOOL_CONTEXT._scratchpad_updated = False


def _inject_plan_status(messages: list[dict]) -> None:
    """Inject active plan status if a plan is in progress and no sub-agents are running."""
    plan_steps = _TOOL_CONTEXT._plan_steps
    if not plan_steps:
        return
    # Suppress plan when sub-agents are running -- avoids confusion
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        if runtime.get_running_ids():
            return
    plan_done = _TOOL_CONTEXT._plan_done
    lines = [f"Active plan ({len(plan_done)}/{len(plan_steps)} done):"]
    for i, s in enumerate(plan_steps, 1):
        mark = "V" if (i - 1) in plan_done else "o"
        lines.append(f"  [{mark}] {i}. {s}")
    lines.append("Use plan_status to mark steps complete as you finish them.")

    # --- Plan staleness detection ---
    turn_count = getattr(_TOOL_CONTEXT, "_turn_count", 0)
    last_advanced = getattr(_TOOL_CONTEXT, "_plan_last_advanced_turn", 0)
    _PLAN_STALE_TURNS = 3
    stale_turns = turn_count - last_advanced
    if len(plan_done) < len(plan_steps) and stale_turns >= _PLAN_STALE_TURNS:
        lines.append(
            f"\nWARNING: PLAN STALLED: No step advanced in {stale_turns} turns. "
            "Either you're stuck on the current step, or you forgot to call "
            "plan_status when you finished it. Re-evaluate: are you making "
            "progress? If stuck, read relevant files, try a different approach, "
            "or mark the step as done and move on."
        )

    messages.append({
        "role": "user",
        "content": "\n".join(lines),
        "_transient": True,
    })


# ---------------------------------------------------------------------------
# Main context injector -- orchestrates all helpers.
# ---------------------------------------------------------------------------



def _inject_system_reminder(messages: list[dict], *, turn_count: int) -> None:
    """Re-inject critical rules near end of long contexts to fight
    'instruction centrifugation' -- the system prompt fading as context grows.

    Triggers when message count > 20 (~6-7 turns), repeats every 8 messages
    for DeepSeek (prone to tool-call loops), 12 for other providers.
    """
    if len(messages) <= 20:
        return
    # Provider-specific interval: DeepSeek benefits from more frequent reminders
    provider = getattr(_TOOL_CONTEXT, "_provider", None) or "deepseek"
    _REMINDER_INTERVAL = 8 if provider == "deepseek" else 12
    if not hasattr(_TOOL_CONTEXT, '_system_reminder_last_msg_count'):
        _TOOL_CONTEXT._system_reminder_last_msg_count = 0
    if len(messages) - _TOOL_CONTEXT._system_reminder_last_msg_count < _REMINDER_INTERVAL:
        return
    _TOOL_CONTEXT._system_reminder_last_msg_count = len(messages)

    reminder = (
        "WARNING: SYSTEM REMINDER (context is long -- critical rules):\n\n"
        "LOOP PREVENTION:\n"
        "- Same tool + same args 2x = STUCK. Switch approach immediately.\n"
        "- edit_file MUST be preceded by read_file in the same batch.\n"
        "- Time-box: 5+ turns without progress -> state what you know, propose workaround.\n"
        "- Context grows stale -- trust write_scratchpad and plan over old tool results.\n\n"
        "EFFICIENCY:\n"
        "- Batch ALL independent tool calls in ONE response (parallel execution).\n"
        "- Update write_scratchpad every 3 turns.\n"
        "- Long commands: background=True, poll task_status once.\n\n"
        "CURRENT TURN: {turn_count}. Remember: stale context is worse than no context."
    ).format(turn_count=turn_count)

    messages.append({"role": "user", "content": reminder, "_transient": True})


def _compress_stale_tool_results(messages: list[dict]) -> None:
    """Compress tool results older than 12 messages behind the tail.

    Uses the content-aware compression from ``memory_prune`` (same
    algorithm used during persistence), so tool results are compressed
    consistently throughout the turn loop -- not just on save.

    Only tool results outside the *keep_recent* window are compressed;
    recent results stay intact for the model to reference.
    """
    from memory.memory_prune import _compress_tool_results

    _compress_tool_results(messages, keep_recent=12)


def _inject_failure_pattern_warnings(
    msg: dict, messages: list[dict],
) -> None:
    """Inject failure pattern warnings for an assistant message's tool calls.

    Called between API call and tool execution so warnings target the
    CURRENT turn's tool choices.  (M7: deduplicated -- removed the
    pre-API-call variant that re-warned about the previous turn's
    tool_calls that had already been warned post-API.)
    """
    try:
        fps = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
        if fps is None:
            return
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return
        from tools.failure_learning import build_self_learning_context
        warning = build_self_learning_context(fps, tool_calls)
        if warning:
            messages.append({
                "role": "user",
                "content": warning,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_self_critique(messages: list[dict], *, turn_count: int) -> None:
    """Inject self-critique context when failure clusters are detected.

    Uses the SelfCritique instance to analyze recent tool results and
    generate corrective guidance when the agent appears stuck.

    Also detects consecutive same-tool failures (e.g. edit_file failing
    3+ times with 'not found') and injects a targeted nudge.
    """
    try:
        # --- Consecutive same-tool failure detection ---
        _CONSECUTIVE_FAILURE_THRESHOLD = 3
        _CRITICAL_FAILURE_TOOLS = {"edit_file", "write_file", "run_shell"}
        recent_failures: list[tuple[str, bool]] = []  # (tool_name, success)
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                for prev_msg in messages:
                    if prev_msg.get("role") == "assistant":
                        for tc in prev_msg.get("tool_calls", []):
                            if tc.get("id") == tcid:
                                name = tc.get("function", {}).get("name", "")
                                try:
                                    import json as _json
                                    data = _json.loads(msg.get("content", "{}"))
                                    success = data.get("success", True)
                                except Exception:
                                    success = True
                                recent_failures.append((name, success))
                                break
            if len(recent_failures) >= 8:
                break
        # Check for consecutive same-tool failures
        if len(recent_failures) >= _CONSECUTIVE_FAILURE_THRESHOLD:
            first_name, _ = recent_failures[0]
            if all(name == first_name and not success for name, success in recent_failures[:_CONSECUTIVE_FAILURE_THRESHOLD]):
                if first_name in _CRITICAL_FAILURE_TOOLS:
                    tool_hints = {
                        "edit_file": "STOP using edit_file. Use read_file FIRST to see the exact text, then copy-paste the exact old_string. You're editing blind.",
                        "write_file": "STOP using write_file repeatedly. Verify the file path and content before retrying.",
                        "run_shell": "STOP retrying the same shell command. It's failing consistently. Try a different approach or diagnose the error output.",
                    }
                    hint = tool_hints.get(first_name, f"STOP retrying {first_name}. It has failed {_CONSECUTIVE_FAILURE_THRESHOLD}+ times. Switch approach.")
                    messages.append({
                        "role": "user",
                        "content": f"WARNING: {hint}",
                        "_transient": True,
                    })
                    return  # Don't also inject the general self-critique

        # --- Original self-critique logic ---
        sc = getattr(_TOOL_CONTEXT, "_self_critique", None)
        if sc is None:
            return
        # Collect recent tool results from messages
        recent_results: list[tuple[dict, object]] = []
        from tools import ToolResult as TR
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                # Find the matching tool call
                for prev_msg in messages:
                    if prev_msg.get("role") == "assistant":
                        for tc in prev_msg.get("tool_calls", []):
                            if tc.get("id") == tcid:
                                try:
                                    import json
                                    data = json.loads(msg.get("content", "{}"))
                                    result = TR(
                                        success=data.get("success", False),
                                        content=data.get("content", ""),
                                        hint=data.get("hint", ""),
                                    )
                                    recent_results.append((tc, result))
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                break
            if len(recent_results) >= 30:  # Scan last 30 tool results (was 10 -- P6 fix)
                break

        if recent_results:
            critique_msg = sc.assess_turn_results(recent_results, turn_count)
            if critique_msg:
                messages.append({
                    "role": "user",
                    "content": critique_msg,
                    "_transient": True,
                })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


# ---------------------------------------------------------------------------
# Confidence-based web_search nudge
# ---------------------------------------------------------------------------

# Thresholds for the confidence nudge
_CONFIDENCE_NO_RESULT_THRESHOLD = 3      # consecutive search misses before nudging
_CONFIDENCE_FAILURE_THRESHOLD = 2        # consecutive tool failures before nudging
_CONFIDENCE_NUDGE_COOLDOWN_TURNS = 4     # turns between repeat nudges
# Tools whose "no results" indicate low knowledge confidence (not just bad queries)
_CONFIDENCE_SEARCH_TOOLS = {"find_symbol", "search_files", "find_usages",
                            "semantic_search", "lsp_definition", "lsp_references"}
# Tools whose repeated failure suggests external knowledge gap
_CONFIDENCE_FAILURE_TOOLS = {"edit_file", "write_file", "run_shell", "run_tests"}

def _inject_confidence_web_search_nudge(
    messages: list[dict], *, turn_count: int,
) -> None:
    """Inject a web_search nudge when the agent shows signs of low confidence.

    Detects patterns that suggest the agent doesn't know the answer and is
    flailing with local codebase tools instead of looking things up:
      - 3+ consecutive search misses (find_symbol, search_files returning nothing)
      - 2+ consecutive tool failures on edit/write/shell/test
      - 3+ turns of reading code without progress (complement to read-only nudge)

    When triggered, injects a brief nudge to use web_search/use_skill('web').
    Has a cooldown to avoid nagging.
    """
    # --- Cooldown check ---
    last_nudge_turn = getattr(_TOOL_CONTEXT, "_confidence_nudge_last_turn", 0)
    if turn_count - last_nudge_turn < _CONFIDENCE_NUDGE_COOLDOWN_TURNS:
        return

    # --- Scan recent tool results for difficulty patterns ---
    consecutive_misses = 0
    max_consecutive_misses = 0
    consecutive_failures = 0
    max_consecutive_failures = 0
    total_read_only_turns = 0
    found_any_result = False

    # Walk messages in reverse (most recent first) to count consecutive patterns
    _stopped_read_only = False
    for msg in reversed(messages):
        role = msg.get("role", "")

        if role == "tool":
            # Parse the tool result
            content = msg.get("content", "")
            data = None
            try:
                import json as _json
                data = _json.loads(content)
                success = data.get("success", True)
                result_content = data.get("content", "")
            except Exception:
                success = True
                result_content = content
                data = {}  # ensure data is defined for isinstance check below

            # Find matching tool call
            tcid = msg.get("tool_call_id", "")
            tool_name = ""
            for prev_msg in messages:
                if prev_msg.get("role") == "assistant":
                    for tc in prev_msg.get("tool_calls", []):
                        if tc.get("id") == tcid:
                            tool_name = tc.get("function", {}).get("name", "")
                            break
                    if tool_name:
                        break

            # Track search misses
            if tool_name in _CONFIDENCE_SEARCH_TOOLS:
                is_miss = (
                    "no match" in str(result_content).lower()
                    or "not found" in str(result_content).lower()
                    or "no results" in str(result_content).lower()
                    or (isinstance(data, dict) and not data.get("content", "").strip())
                )
                if is_miss:
                    consecutive_misses += 1
                    max_consecutive_misses = max(max_consecutive_misses, consecutive_misses)
                else:
                    consecutive_misses = 0
                    found_any_result = True

            # Track tool failures
            elif tool_name in _CONFIDENCE_FAILURE_TOOLS:
                if not success:
                    consecutive_failures += 1
                    max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
                else:
                    consecutive_failures = 0

        elif role == "assistant":
            # Track read-only turns (no tool calls that write/execute)
            # Once we encounter a productive turn, stop incrementing read-only
            # counter, but keep iterating for tool failure/miss tracking.
            tool_calls = msg.get("tool_calls", [])
            if not _stopped_read_only:
                if tool_calls:
                    all_reads = all(
                        tc.get("function", {}).get("name", "") in
                        ("read_file", "find_symbol", "search_files", "list_directory",
                        "file_info", "find_usages", "lsp_definition", "lsp_references",
                        "lsp_diagnostics", "lsp_hover", "semantic_search", "todo_read",
                        "plan_status", "session_stats", "recall_turn", "agent_status",
                        "memory_core", "session_search", "todo_write", "write_scratchpad",
                        "plan")
                        for tc in tool_calls
                    )
                    if all_reads:
                        total_read_only_turns += 1
                    else:
                        _stopped_read_only = True  # found productive turn, stop counting reads
                else:
                    total_read_only_turns += 1

    # --- Determine if a nudge is warranted ---
    nudge = ""
    if max_consecutive_misses >= _CONFIDENCE_NO_RESULT_THRESHOLD and not found_any_result:
        nudge = (
            f"WARNING: CONFIDENCE CHECK: Your last {max_consecutive_misses} codebase "
            f"searches returned no results. Your knowledge confidence appears LOW "
            f"(\u22643/10). Before searching further locally, use "
            f"web_search to look up documentation for the relevant library, API, "
            f"or concept. Then return with the right terminology to search effectively."
        )
    elif max_consecutive_failures >= _CONFIDENCE_FAILURE_THRESHOLD:
        nudge = (
            f"WARNING: CONFIDENCE CHECK: Your last {max_consecutive_failures} "
            f"tool calls failed. Your approach may be based on incorrect assumptions. "
            f"Consider web_search to verify the correct API, syntax, or pattern "
            f"before retrying."
        )
    elif total_read_only_turns >= 6:
        nudge = (
            f"WARNING: CONFIDENCE CHECK: You've spent {total_read_only_turns} "
            f"turns reading code without writing. If you're unsure how to proceed, "
            f"use web_search to find the relevant documentation or examples."
        )

    if nudge:
        _TOOL_CONTEXT._confidence_nudge_last_turn = turn_count
        messages.append({
            "role": "user",
            "content": nudge,
            "_transient": True,
        })


def _inject_strategy_hint(messages: list[dict]) -> None:
    """#5 Auto tool strategy hints -- suggest optimal search tool.

    Also detects when the agent is using search_files for symbol-like
    patterns (single CamelCase/snake_case identifiers) and nudges
    toward find_symbol instead.
    """
    try:
        # --- Part A: keyword-based hints from latest user message ---
        for msg in reversed(messages):
            if msg["role"] == "user":
                last = msg["content"]
                break
        else:
            return
        hint = ""
        if any(kw in last.lower() for kw in ("find where", "locate", "where is", "find_symbol", "function ", "class ", "def ")):
            hint = "[Hint: Use find_symbol for fast symbol lookup. Use search_files for text patterns.]"
        elif any(kw in last.lower() for kw in ("refactor", "all callers", "who uses", "references")):
            hint = "[Hint: Use find_usages to find all callers, then edit_file for targeted changes.]"
        elif any(kw in last.lower() for kw in ("semantic", "similar", "feels like", "find code that")):
            hint = "[Hint: Use semantic_search for meaning-based code search.]"

        # --- Part B: detect search_files being used for symbol search ---
        if not hint:
            import re as _re
            _symbol_pattern = _re.compile(r"^(search_files|find_symbol|read_file)$")
            text_patterns_seen = 0
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        if name == "search_files":
                            args_raw = fn.get("arguments", "{}")
                            try:
                                import json as _json
                                args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                            except Exception:
                                args = {}
                            pattern = args.get("pattern", "")
                            # Symbol-like: no spaces, contains _ or mixed case
                            if pattern and " " not in pattern and (
                                "_" in pattern or (pattern != pattern.lower() and pattern != pattern.upper())
                            ):
                                text_patterns_seen += 1
            if text_patterns_seen >= 2:
                hint = "[Hint: You've been using search_files for patterns that look like symbol names. Try find_symbol -- it's ~10x faster and gives exact line numbers.]"

        if hint:
            # Track injected hints in a set for O(1) dedup
            if not hasattr(_inject_strategy_hint, '_injected'):
                _inject_strategy_hint._injected = set()
            if hint in _inject_strategy_hint._injected:
                return
            _inject_strategy_hint._injected.add(hint)
            # Insert after the last system message, or at index 1 if none found
            insert_at = 1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    insert_at = i + 1
                    break
            # Avoid inserting beyond list bounds
            if insert_at <= len(messages):
                messages.insert(insert_at, {"role": "system", "content": hint})
    except (KeyError, IndexError, TypeError, ValueError):
        pass


def _inject_tool_graph_context(messages: list[dict]) -> None:
    """Inject tool sequencing hints from the ToolGraph.

    Analyzes recent tool usage and suggests optimal sequencing patterns
    learned from past sessions (e.g., "after read_file, most agents follow
    with edit_file").
    """
    try:
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is None:
            return

        # Collect recent tool names from the conversation
        recent_tools: list[str] = []
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        recent_tools.insert(0, name)  # chronological order
            if len(recent_tools) >= 10:
                break

        context_msg = tg.get_tool_context_hints(recent_tools)
        if context_msg:
            messages.append({
                "role": "user",
                "content": context_msg,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_experience_context(
    messages: list[dict],
    memory_store=None,
) -> None:
    """Inject relevant past experiences from project_knowledge.

    Searches project_knowledge for entries relevant to the current
    conversation context and injects matching learnings.
    """
    if memory_store is None:
        return
    try:
        from tools.failure_learning import build_experience_context_from_text

        # Extract context from the last user message
        search_context = ""
        for msg in reversed(messages):
            if msg.get("role") == "user" and not msg.get("_transient"):
                search_context = msg.get("content", "")[:200]
                break

        if not search_context:
            return

        ctx_msg = build_experience_context_from_text(
            memory_store,
            text=search_context,
        )
        if ctx_msg:
            messages.append({
                "role": "user",
                "content": ctx_msg,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_dead_tool_pruning(
    messages: list[dict],
    turn_count: int,
) -> None:
    """Prune unused tools after the dead-tool threshold turn.

    After _DEAD_TOOL_PRUNE_TURN turns, any skill whose tools have never
    been used is deactivated.  This shrinks the API payload (fewer tool
    definitions) and stabilizes the KV-cache prefix (tool definitions
    stop changing mid-session).

    A transient message is injected so the agent is aware of the change.
    Only runs once per session (at exactly the threshold turn).
    """
    if turn_count != _DEAD_TOOL_PRUNE_TURN:
        return

    try:
        from tools import get_unused_tools
        from tools.skills import prune_unused_skills, active_skills
        unused = get_unused_tools(min_turns=_DEAD_TOOL_PRUNE_TURN)
        if len(unused) < _MIN_PRUNE_COUNT:
            return
        pruned = prune_unused_skills(unused)
        if pruned > 0:
            remaining = active_skills()
            msg = (
                f"TOOL PRUNING: After {turn_count} turns, {pruned} skill(s) were "
                f"deactivated because none of their tools were used. "
                f"Remaining active skills: {', '.join(sorted(remaining)) if remaining else 'none'}. "
                f"This reduces API payload and stabilizes the cache prefix."
            )
            messages.append({
                "role": "user",
                "content": msg,
                "_transient": True,
            })
            _log.info(
                "dead_tool_pruning turn=%d pruned=%d unused_count=%d remaining=%d",
                turn_count, pruned, len(unused), len(remaining),
            )
    except Exception:
        _log.warning("dead_tool_pruning failed", exc_info=True)


def _inject_pre_execution_context(
    messages: list[dict],
    pending_tool_calls: list[dict],
    turn_count: int,
) -> None:
    """Inject self-learning context before executing tool calls.

    Runs AFTER the API response, when we know which tools will be called.
    Injects:
      - Failure pattern warnings (from FailurePatternStore)
      - Mistake notebook entries (generalized fixes)
      - Tool graph read-before-write detection

    All injected messages are marked _transient so they aren't persisted.
    """
    try:
        # --- 1. Failure pattern warnings ---
        fps = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
        if fps is not None:
            from tools.failure_learning import build_self_learning_context
            warning = build_self_learning_context(fps, pending_tool_calls)
            if warning:
                messages.append({
                    "role": "user",
                    "content": warning,
                    "_transient": True,
                })

        # --- 2. Mistake notebook entries ---
        mn = getattr(_TOOL_CONTEXT, "_mistake_notebook", None)
        if mn is not None:
            mn.distill(turn_count)  # Trigger distillation if cooldown elapsed
            notebook_ctx = mn.build_notebook_context(
                pending_tool_calls, turn_count=turn_count,
            )
            if notebook_ctx:
                messages.append({
                    "role": "user",
                    "content": notebook_ctx,
                    "_transient": True,
                })

        # --- 3. Tool graph: read-before-write detection ---
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is not None:
            # Collect recent tools for context
            recent_tools: list[str] = []
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        name = tc.get("function", {}).get("name", "")
                        if name:
                            recent_tools.insert(0, name)
                if len(recent_tools) >= 10:
                    break

            gap_warning = tg.detect_read_before_write_gap(
                pending_tool_calls, recent_tools,
            )
            if gap_warning:
                messages.append({
                    "role": "user",
                    "content": gap_warning,
                    "_transient": True,
                })

    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _record_tool_sequence_to_graph(
    tool_results: list[tuple[dict, object]],
) -> None:
    """Record a turn's tool execution sequence to the ToolGraph."""
    try:
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is None:
            return

        tool_names = [
            tc.get("function", {}).get("name", "")
            for tc, _ in tool_results
            if tc.get("function", {}).get("name", "")
        ]
        if len(tool_names) >= 2:
            tg.record_turn_tool_sequence(tool_names)
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_context(
    messages: list[dict],
    *,
    turn_count: int,
    memory_store: Any = None,
    read_gate: ReadSafetyGate | None = None,
    recent_tool_keys: deque[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Inject all context messages for the current turn.

    Delegates to smaller helpers for one-time and per-turn injections.
    """
    # Compaction must run FIRST -- before injecting new context messages --
    # so fresh context isn't immediately pruned.
    _compact_if_needed(messages)

    # Build knowledge graph at startup (one-time, lazy -- no-op if already built)
    workspace = read_gate.workspace_root if read_gate else ""
    if workspace and turn_count == 1:
        try:
            from core.knowledge_graph import ensure_graph_built
            ensure_graph_built(workspace)
        except Exception:
            pass

    # One-time injections (first turn only)
    _inject_handoff_context(
        messages,
        workspace_root=read_gate.workspace_root if read_gate else "",
    )
    _inject_state_context(
        messages,
        workspace_root=read_gate.workspace_root if read_gate else "",
    )
    _inject_tasks_context(
        messages,
        workspace_root=read_gate.workspace_root if read_gate else "",
    )
    _inject_core_memory_context(messages, memory_store=memory_store)
    _inject_scratchpad_context(messages, memory_store=memory_store)
    _inject_git_diff(messages, memory_store=memory_store, read_gate=read_gate)

    # Per-turn injections
    _inject_orchestration_context(messages)
    _inject_interjections(messages)


    _inject_progress_check(messages, turn_count=turn_count)

    if turn_count == MODIFIED_FILES_CHECKPOINT_TURN and (
        hasattr(read_gate, "workspace_root") if read_gate else False
    ):
        _inject_modified_files_checkpoint(messages, read_gate=read_gate)

    _inject_circuit_breaker(messages, recent_tool_keys=recent_tool_keys)
    _inject_cache_degradation_alert(messages)
    _inject_edit_risk_context(messages)
    _inject_pattern_rules(messages)
    _inject_scratchpad_nudge(messages, turn_count=turn_count)
    _inject_strategy_hint(messages)
    _inject_plan_status(messages)
    _inject_self_critique(messages, turn_count=turn_count)
    _inject_confidence_web_search_nudge(messages, turn_count=turn_count)
    _inject_tool_graph_context(messages)
    _inject_experience_context(messages, memory_store=memory_store)
    _inject_dead_tool_pruning(messages, turn_count=turn_count)
    _inject_post_edit_verification(messages)

    # Context-quality defences (research-backed: 25% fill degrades quality)
    _compress_stale_tool_results(messages)
    _inject_system_reminder(messages, turn_count=turn_count)


# ---------------------------------------------------------------------------
# Mid-session conversation compaction
# ---------------------------------------------------------------------------

# Compaction threshold: fraction of context window at which we compact
_COMPACTION_THRESHOLD = 0.80
# Target fraction after compaction (leave room for turn growth)
_COMPACTION_TARGET = 0.70
# Minimum number of messages at the tail to keep intact (preserve recent context)
_COMPACTION_KEEP_RECENT = 20


def _compact_if_needed(messages: list[dict]) -> None:
    """Compact conversation when approaching the context window limit.

    Uses the existing _prune_by_tokens to drop oldest messages and
    _summarize_pruned_rules to inject a summary, keeping the model
    aware of earlier context.  Preserves system prompt + startup
    context (first 2 messages) and the most recent messages.

    Called before context injection each turn to ensure fresh context
    messages aren't immediately pruned.
    """
    from memory.memory_prune import _total_tokens, _prune_by_tokens, _summarize_pruned

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    if config is None:
        return
    context_window = getattr(config, "context_window", 200_000)
    if context_window <= 0:
        return

    # Only compact if over threshold
    current_tokens = _total_tokens(messages)
    threshold = int(context_window * _COMPACTION_THRESHOLD)
    if current_tokens <= threshold:
        return

    # Preserve system prompt + startup context (first 2 messages)
    if len(messages) <= _COMPACTION_KEEP_RECENT + 2:
        return  # Not enough to compact meaningfully

    system_msgs = messages[:2]
    conversation = messages[2:]

    # Prune to target fraction of context window
    target = int(context_window * _COMPACTION_TARGET)
    kept, pruned = _prune_by_tokens(
        conversation, target, max_messages=len(conversation),
    )

    if not pruned:
        return

    # Build summary of what was pruned
    summary = _summarize_pruned(pruned)

    # Rebuild: system + summary + kept conversation
    messages.clear()
    messages.extend(system_msgs)
    if summary:
        messages.append({
            "role": "user",
            "content": summary,
            "_transient": True,
        })
    messages.extend(kept)

    _log.info(
        "Compacted conversation: %d -> %d messages (%d pruned)",
        len(system_msgs) + len(conversation),
        len(messages),
        len(pruned),
    )

