#!/usr/bin/env python3
"""
llm.py — Agent turn orchestration for mini_agent.

Provides ``run_agent_turn()`` orchestrator, circuit breaker,
tool piping (Kahn's algorithm), and turn-summary persistence.
API communication (``call_deepseek()``) lives in ``api.py``;
retry logic in ``retry.py``; SSE parsing in ``stream.py``.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess as _sp
import sys
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests


from api import APIError, format_tool_detail, call_llm, call_deepseek, clear_api_cache

from config import AgentConfig
from tools import execute_tool, tool_summary, clear_tool_cache, _TOOL_CONTEXT, get_modified_files
from safety import ReadSafetyGate, WriteSafetyGate
from interject import poll_interjections


# ---------------------------------------------------------------------------
# Named constants (extracted from magic numbers)
# ---------------------------------------------------------------------------

# Display / truncation
TOOL_DETAIL_DISPLAY_LENGTH = 300   # max chars for tool result detail display

# Turn summary
TURN_SUMMARY_ASSISTANT_PREVIEW = 200  # max chars for assistant content in summary
TURN_SUMMARY_RESULT_PREVIEW = 150     # max chars for tool result content in summary
TURN_HISTORY_MAX_ENTRIES = 200        # cap on _turn_history entries

# Orchestration / context injection
SUB_AGENT_RESULT_PREVIEW = 120  # max chars for sub-agent result in context message

# Context injection intervals
PROGRESS_INTERVAL = 5               # turns between progress reminders
SCRATCHPAD_NUDGE_START_TURN = 5     # first turn to check scratchpad staleness
SCRATCHPAD_NUDGE_INTERVAL = 3       # interval for scratchpad staleness nudge
MODIFIED_FILES_CHECKPOINT_TURN = 2  # turn to show modified-files checkpoint

# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Circuit breaker — guards against repeated identical tool calls
# ---------------------------------------------------------------------------

_CIRCUIT_WINDOW: int = 6       # lookback window size
_CIRCUIT_THRESHOLD: int = 3    # trip after this many identical calls in the window


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
    """Return a warning message if the circuit is tripped, otherwise None.

    Trips when the same tool call appears *CIRCUIT_THRESHOLD* or more times
    within the last *CIRCUIT_WINDOW* calls.
    """
    if len(recent_keys) < _CIRCUIT_THRESHOLD:
        return None
    counts = Counter(recent_keys)
    for key, count in counts.items():
        if count >= _CIRCUIT_THRESHOLD:
            return (
                f"⚠️ Circuit breaker: you have called '{key}' {count} times "
                f"in the last {len(recent_keys)} tool calls. "
                "The same call keeps being made with identical arguments. "
                "Stop, diagnose why it isn't working, and try a different "
                "approach rather than repeating it."
            )
    return None


# ---------------------------------------------------------------------------
# Shared agent loop — used by both terminal REPL and TUI
# ---------------------------------------------------------------------------

def _save_turn_summary(
    turn: int,
    msg: dict,
    deferred_results: list[tuple[dict, "ToolResult"]],
    messages: list[dict],
) -> None:
    """Save a concise summary of this turn for later recall via recall_turn()."""
    from tools import ToolResult as TR

    parts: list[str] = []
    content = msg.get("content", "")
    if content:
        parts.append(f"Assistant: {content[:TURN_SUMMARY_ASSISTANT_PREVIEW]}")
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            parts.append(f"  Tool: {fn.get('name', '?')}({str(fn.get('arguments', ''))[:100]})")
    for tc, result in deferred_results:
        ok = "✓" if result.success else "✗"
        summary = result.content[:TURN_SUMMARY_RESULT_PREVIEW].replace("\n", " ")
        if len(result.content) > TURN_SUMMARY_RESULT_PREVIEW:
            summary += "…"
        parts.append(f"  Result: {ok} {summary}")
    _TOOL_CONTEXT._turn_history[turn] = "\n".join(parts)
    # Cap to last TURN_HISTORY_MAX_ENTRIES entries to prevent unbounded memory growth
    if not hasattr(_TOOL_CONTEXT, '_min_turn'):
        _TOOL_CONTEXT._min_turn = 0
    if len(_TOOL_CONTEXT._turn_history) > TURN_HISTORY_MAX_ENTRIES:
        oldest = _TOOL_CONTEXT._min_turn
        while oldest not in _TOOL_CONTEXT._turn_history:
            oldest += 1
        del _TOOL_CONTEXT._turn_history[oldest]
        _TOOL_CONTEXT._min_turn = oldest + 1


# Module-level flags for one-time context injections
_scratchpad_injected: bool = False
_git_diff_injected: bool = False


# ---------------------------------------------------------------------------
# Context injection helpers — each appends one kind of context message.
# ---------------------------------------------------------------------------


def _inject_scratchpad_context(
    messages: list[dict], *, memory_store: Any = None,
) -> None:
    """Inject current scratchpad content (one-time per session)."""
    global _scratchpad_injected
    if _scratchpad_injected or memory_store is None:
        return
    _scratchpad_injected = True
    scratchpad = memory_store.get_scratchpad()
    if scratchpad.strip():
        messages.append({
            "role": "user",
            "content": (
                "Your scratchpad (current working notes — use write_scratchpad "
                "to update):\n\n" + scratchpad
            ),
            "_transient": True,
        })


def _inject_git_diff(
    messages: list[dict], *, memory_store: Any = None,
    read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject recent git diff (one-time per session)."""
    global _git_diff_injected
    if _git_diff_injected or memory_store is None or read_gate is None:
        return
    _git_diff_injected = True
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ⚠ git diff failed: {exc}", file=sys.stderr, flush=True)


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
                status_snap = runtime.status_snapshots.get(tid, {})
                turns_budget = status_snap.get("turns_budget", 0)
                current_turn = status_snap.get("turn", 0)
                remaining = turns_budget - current_turn
                if remaining <= 3 and remaining > 0:
                    # Only extend if agent is making forward progress
                    last_action = status_snap.get("last_action", "")
                    if last_action and last_action != "idle":
                        runtime.extend_turns(tid, 10)
                        parts.append(f"  🔄 Auto-extended '{tid}' (+10 turns, {remaining} left)")
        if parts:
            messages.append({
                "role": "user",
                "content": "\n".join(parts),
                "_transient": True,
            })
    except (APIError, AttributeError, KeyError, ValueError, TypeError) as exc:
        print(f"  ⚠ orchestration context failed: {exc}", file=sys.stderr, flush=True)


def _inject_interjections(messages: list[dict]) -> None:
    """Inject any pending user interjections (every turn)."""
    interjections = poll_interjections()
    for msg_text in interjections:
        messages.append({
            "role": "user",
            "content": msg_text,
        })


def _inject_progress_check(messages: list[dict], *, turn_count: int) -> None:
    """Inject periodic progress reminder every PROGRESS_INTERVAL turns."""
    if turn_count <= 1 or turn_count % PROGRESS_INTERVAL != 0:
        return
    reminder = (
        f"You have been working for {turn_count} turns. "
        "Briefly assess your progress: are you making headway, "
        "stuck in a loop, or done? If you can wrap up now, "
        "give the final answer. If you truly need more turns, "
        "continue — but be specific about what remains."
    )
    messages.append({"role": "user", "content": reminder, "_transient": True})


def _inject_modified_files_checkpoint(
    messages: list[dict], *, read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject modified-files checkpoint (turn 2 only)."""
    if not get_modified_files():
        return
    mod_list = "\n".join(f"  - {f}" for f in get_modified_files())
    test_hint = ""
    for mf in get_modified_files():
        base = os.path.basename(mf)
        if base.startswith("test_") and base.endswith(".py"):
            test_hint += f"\n  Relevant test: {base}"
        elif base.endswith(".py") and not base.startswith("test_"):
            candidate = f"test_{base}"
            dp = os.path.dirname(mf)
            test_path = os.path.join(dp, candidate) if dp else candidate
            if os.path.isfile(os.path.join(read_gate.workspace_root, test_path)):
                test_hint += f"\n  Relevant test: {test_path}"
    ckpt = (
        f"Files modified this session:\n{mod_list}\n"
        f"Running `verify` or `run_tests`{test_hint if test_hint else ''} "
        f"after changes is recommended."
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
                "⚠️ Your scratchpad hasn't been updated in several turns. "
                "Consider using write_scratchpad to capture your current "
                "plan, progress, and decisions before continuing."
            ),
            "_transient": True,
        })
    _TOOL_CONTEXT._scratchpad_updated = False


def _inject_plan_status(messages: list[dict]) -> None:
    """Inject active plan status if a plan is in progress and no sub-agents are running."""
    plan_steps = _TOOL_CONTEXT._plan_steps
    if not plan_steps:
        return
    # Suppress plan when sub-agents are running — avoids confusion
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        if runtime.get_running_ids():
            return
    plan_done = _TOOL_CONTEXT._plan_done
    lines = [f"Active plan ({len(plan_done)}/{len(plan_steps)} done):"]
    for i, s in enumerate(plan_steps, 1):
        mark = "✓" if (i - 1) in plan_done else "○"
        lines.append(f"  [{mark}] {i}. {s}")
    lines.append("Use plan_status to mark steps complete as you finish them.")
    messages.append({
        "role": "user",
        "content": "\n".join(lines),
        "_transient": True,
    })


# ---------------------------------------------------------------------------
# Main context injector — orchestrates all helpers.
# ---------------------------------------------------------------------------



def _inject_strategy_hint(messages: list[dict]) -> None:
    """#5 Auto tool strategy hints — suggest optimal search tool."""
    try:
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
        if hint and not any(m["role"] == "system" and m["content"] == hint for m in messages):
            messages.insert(1, {"role": "system", "content": hint})
    except (KeyError, IndexError, TypeError, ValueError):
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
    # One-time injections (first turn only)
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
    _inject_scratchpad_nudge(messages, turn_count=turn_count)
    _inject_strategy_hint(messages)
    _inject_plan_status(messages)


def _apply_pipe(tc: dict, i: int,
                pipe_deps: dict, pipe_results: dict, _json: Any) -> None:
    """Substitute piped result into tc's arguments in-place."""
    if i not in pipe_deps:
        return
    src_idx, into_param = pipe_deps[i]
    src_result = pipe_results.get(src_idx)
    if src_result is None:
        return
    args_dict = _json.loads(tc["function"]["arguments"])
    if not into_param:
        for k, v in args_dict.items():
            if isinstance(v, str):
                into_param = k
                break
    if into_param and into_param in args_dict:
        args_dict[into_param] = src_result.content.strip()
        tc["function"]["arguments"] = _json.dumps(args_dict)


def _extract_pipe_deps(
    remaining: list[dict],
) -> tuple[dict[int, tuple[int, str]], dict[int, "ToolResult"]]:
    """Extract _pipe config from tool calls, returning (pipe_deps, pipe_results).

    pipe_deps maps target_idx -> (source_idx, into_param).
    pipe_results is pre-allocated empty dict for substitution results.
    Side effects: strips _pipe key from each tool call's arguments.
    """
    pipe_deps: dict[int, tuple[int, str]] = {}
    pipe_results: dict[int, "ToolResult"] = {}
    has_pipe = any(
        "_pipe" in tc["function"].get("arguments", "")
        for tc in remaining
    )
    if not has_pipe:
        return pipe_deps, pipe_results
    for i, tc in enumerate(remaining):
        raw = tc["function"].get("arguments", "{}")
        try:
            ad = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (APIError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        pipe_cfg = ad.pop("_pipe", None)
        if isinstance(pipe_cfg, dict) and "from" in pipe_cfg:
            pipe_deps[i] = (int(pipe_cfg["from"]), pipe_cfg.get("into", ""))
        tc["function"]["arguments"] = json.dumps(ad)
    return pipe_deps, pipe_results


def _execute_single_no_pipe(
    tc: dict,
    messages: list[dict],
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    on_tool_start: Callable[..., Any] | None,
    on_tool_end: Callable[..., Any] | None,
    on_tool_output: Callable[..., Any] | None,
    approve_callback: Callable[..., Any] | None,
    cancel_event: threading.Event | None,
    recent_tool_keys: deque[str] | None,
    tool_keys_lock: threading.Lock | None,
) -> list[tuple[dict, "ToolResult"]]:
    """Execute a single tool call with no piping dependencies."""
    if cancel_event is not None and cancel_event.is_set():
        _append_cancel_results([tc], messages, on_tool_end=on_tool_end,
                                recent_keys=recent_tool_keys, lock=tool_keys_lock)
        return []
    if on_tool_start is not None:
        on_tool_start(tool_summary(tc))
    result = execute_tool(tc, write_gate, read_gate,
                          on_output=on_tool_output,
                          approve_callback=approve_callback)
    _append_tool_result(messages, tc, result, on_tool_end,
                        recent_keys=recent_tool_keys,
                        lock=tool_keys_lock)
    return [(tc, result)]


def _execute_parallel_no_pipes(
    remaining: list[dict],
    messages: list[dict],
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    on_tool_start: Callable[..., Any] | None,
    on_tool_end: Callable[..., Any] | None,
    on_tool_output: Callable[..., Any] | None,
    approve_callback: Callable[..., Any] | None,
    cancel_event: threading.Event | None,
    recent_tool_keys: list[str] | None,
    tool_keys_lock: threading.Lock | None,
) -> list[tuple[dict, "ToolResult"]]:
    """Execute multiple independent tool calls in parallel."""
    if on_tool_start is not None:
        for tc in remaining:
            on_tool_start(tool_summary(tc), True)

    def _run_tool(tc: dict) -> tuple[dict, "ToolResult"]:
        return tc, execute_tool(tc, write_gate, read_gate,
                                on_output=on_tool_output,
                                approve_callback=approve_callback)

    parallel_results: list[tuple] = []
    with ThreadPoolExecutor(max_workers=len(remaining)) as pool:
        futures = {pool.submit(_run_tool, tc): tc for tc in remaining}
        for future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                # Append failure results for any tool calls not yet completed
                completed = {id(tc) for tc, _ in parallel_results}
                uncompleted = [tc for tc in remaining if id(tc) not in completed]
                _append_cancel_results(
                    uncompleted, messages, on_tool_end=on_tool_end,
                    recent_keys=recent_tool_keys, lock=tool_keys_lock,
                )
                return parallel_results
            tc, result = future.result()
            _append_tool_result(messages, tc, result, on_tool_end,
                                recent_keys=recent_tool_keys,
                                lock=tool_keys_lock)
            parallel_results.append((tc, result))
    return parallel_results


def _build_execution_groups(
    remaining: list[dict],
    pipe_deps: dict[int, tuple[int, str]],
) -> list[list[int]] | None:
    """Topological sort (Kahn's algorithm) into parallel-execution groups.

    Returns a list of groups (each group is a list of indices into *remaining*),
    or None if a cycle is detected.
    """
    n = len(remaining)
    children: dict[int, list[int]] = {i: [] for i in range(n)}
    indeg: dict[int, int] = {i: 0 for i in range(n)}
    for tgt, (src, _) in pipe_deps.items():
        children.setdefault(src, []).append(tgt)
        indeg[tgt] = indeg.get(tgt, 0) + 1

    queue = collections.deque([i for i in range(n) if indeg[i] == 0])
    groups: list[list[int]] = []
    seen = 0
    while queue:
        group = list(queue)
        groups.append(group)
        queue.clear()
        for node in group:
            seen += 1
            for child in children.get(node, []):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)

    if seen != n:
        return None  # cycle detected
    return groups


def _execute_groups(
    groups: list[list[int]],
    remaining: list[dict],
    messages: list[dict],
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    pipe_deps: dict[int, tuple[int, str]],
    pipe_results: dict[int, "ToolResult"],
    *,
    on_tool_start: Callable[..., Any] | None,
    on_tool_end: Callable[..., Any] | None,
    on_tool_output: Callable[..., Any] | None,
    approve_callback: Callable[..., Any] | None,
    cancel_event: threading.Event | None,
    recent_tool_keys: list[str] | None,
    tool_keys_lock: threading.Lock | None = None,
) -> list[tuple[dict, "ToolResult"]]:
    """Execute groups in order (parallel within group, sequential across groups)."""
    all_results: list[tuple] = []
    for group_idx, group in enumerate(groups):
        if on_tool_start is not None:
            for i in group:
                on_tool_start(tool_summary(remaining[i]),
                              parallel=len(group) > 1)

        if len(group) == 1:
            i = group[0]
            tc = remaining[i]
            _apply_pipe(tc, i, pipe_deps, pipe_results, json)
            if cancel_event is not None and cancel_event.is_set():
                # Append failure results for current + all future groups
                remaining_indices = {i} | {
                    idx for g in groups[group_idx + 1:] for idx in g
                }
                _append_cancel_results(
                    [remaining[idx] for idx in remaining_indices], messages,
                    on_tool_end=on_tool_end, recent_keys=recent_tool_keys,
                    lock=tool_keys_lock,
                )
                break
            result = execute_tool(tc, write_gate, read_gate,
                                  on_output=on_tool_output,
                                  approve_callback=approve_callback)
            pipe_results[i] = result
            _append_tool_result(messages, tc, result, on_tool_end,
                                recent_keys=recent_tool_keys)
            all_results.append((tc, result))
        else:
            results_lock = threading.Lock()

            def _run_piped(i: int) -> tuple[int, dict, "ToolResult"]:
                tc = remaining[i]
                _apply_pipe(tc, i, pipe_deps, pipe_results, json)
                return i, tc, execute_tool(tc, write_gate, read_gate,
                                            on_output=on_tool_output,
                                            approve_callback=approve_callback)

            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                futures = {pool.submit(_run_piped, i): i for i in group}
                completed_in_group: set[int] = set()
                for future in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        # Append failure results for incomplete in this group
                        # plus all future groups
                        incomplete = (set(group) - completed_in_group) | {
                            idx for g in groups[group_idx + 1:] for idx in g
                        }
                        _append_cancel_results(
                            [remaining[idx] for idx in incomplete], messages,
                            on_tool_end=on_tool_end,
                            recent_keys=recent_tool_keys,
                            lock=tool_keys_lock,
                        )
                        break
                    i, tc, result = future.result()
                    completed_in_group.add(i)
                    with results_lock:
                        pipe_results[i] = result
                    _append_tool_result(messages, tc, result, on_tool_end,
                                        recent_keys=recent_tool_keys,
                                        lock=tool_keys_lock)
                    all_results.append((tc, result))
    return all_results


def _execute_tools(
    remaining: list[dict],
    messages: list[dict],
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    on_tool_start: Callable[..., Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_tool_output: Callable[..., Any] | None = None,
    approve_callback: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
    recent_tool_keys: list[str] | None = None,
    tool_keys_lock: threading.Lock | None = None,
) -> list[tuple[dict, "ToolResult"]]:
    """Execute a list of tool calls, respecting _pipe dependencies.

    Uses Kahn's algorithm for topological sort when _pipe deps are present.
    Independent tools run in parallel via ThreadPoolExecutor.
    Returns a list of (tool_call_dict, ToolResult) tuples.
    """
    # --- Extract piping metadata ---
    pipe_deps, pipe_results = _extract_pipe_deps(remaining)

    # --- No piping: simple parallel or sequential execution ---
    if not pipe_deps:
        if len(remaining) == 1:
            return _execute_single_no_pipe(
                remaining[0], messages, write_gate, read_gate,
                on_tool_start=on_tool_start, on_tool_end=on_tool_end,
                on_tool_output=on_tool_output, approve_callback=approve_callback,
                cancel_event=cancel_event,
                recent_tool_keys=recent_tool_keys, tool_keys_lock=tool_keys_lock,
            )
        return _execute_parallel_no_pipes(
            remaining, messages, write_gate, read_gate,
            on_tool_start=on_tool_start, on_tool_end=on_tool_end,
            on_tool_output=on_tool_output, approve_callback=approve_callback,
            cancel_event=cancel_event,
            recent_tool_keys=recent_tool_keys, tool_keys_lock=tool_keys_lock,
        )

    # --- Piping: topological sort into execution groups ---
    groups = _build_execution_groups(remaining, pipe_deps)
    if groups is None:
        # Cycle detected — fall back to sequential execution
        if on_tool_start is not None:
            for tc in remaining:
                on_tool_start(tool_summary(tc))
        results: list[tuple[dict, "ToolResult"]] = []
        for i, tc in enumerate(remaining):
            if cancel_event is not None and cancel_event.is_set():
                _append_cancel_results(
                    remaining[i:], messages,
                    on_tool_end=on_tool_end,
                    recent_keys=recent_tool_keys,
                    lock=tool_keys_lock,
                )
                break
            result = execute_tool(tc, write_gate, read_gate,
                                  on_output=on_tool_output,
                                  approve_callback=approve_callback)
            pipe_results[i] = result
            _append_tool_result(messages, tc, result, on_tool_end,
                                recent_keys=recent_tool_keys)
            results.append((tc, result))
        return results

    # --- Execute groups: parallel within group, sequential across groups ---
    return _execute_groups(
        groups, remaining, messages, write_gate, read_gate,
        pipe_deps, pipe_results,
        on_tool_start=on_tool_start, on_tool_end=on_tool_end,
        on_tool_output=on_tool_output, approve_callback=approve_callback,
        cancel_event=cancel_event, recent_tool_keys=recent_tool_keys,
        tool_keys_lock=tool_keys_lock,
    )


# ---------------------------------------------------------------------------
# Helpers for run_agent_turn
# ---------------------------------------------------------------------------

def _accumulate_usage(total: dict[str, int], msg: dict) -> dict[str, int]:
    """Merge ``_usage`` from *msg* into the running *total* dict."""
    usage: dict[str, int] = msg.get("_usage", {})
    return {
        "prompt_tokens": total.get("prompt_tokens", 0) + usage.get("prompt_tokens", 0),
        "completion_tokens": total.get("completion_tokens", 0) + usage.get("completion_tokens", 0),
        "total_tokens": total.get("total_tokens", 0) + usage.get("total_tokens", 0),
    }


def _api_call_phase(
    messages: list[dict],
    config: AgentConfig,
    session: Any,
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    on_token: Callable[..., Any] | None = None,
    on_tool_start: Callable[..., Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_tool_output: Callable[..., Any] | None = None,
    approve_callback: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
    agent_id: str = "",
) -> tuple[dict, list[tuple], set[int]]:
    """Call the LLM, handling streaming tool execution during the call.

    Returns ``(msg, deferred_stream_results, executed_tool_indices)``.
    The caller must check *cancel_event* after this returns — the returned
    *msg* may be stale if cancellation was requested.
    """
    executed_tool_indices: set[int] = set()
    deferred_stream_results: list[tuple] = []  # (tc, result)

    # --- Wrap on_token to also emit stream tokens to WebSocket ---
    _outer_on_token = on_token

    def _on_tool_ready(tc: dict) -> None:
        """Execute a tool immediately when its args form valid JSON."""
        idx = tc.pop("_index", -1)
        if idx in executed_tool_indices:
            return
        if cancel_event is not None and cancel_event.is_set():
            return
        if on_tool_start is not None:
            on_tool_start(tool_summary(tc))
        try:
            result = execute_tool(tc, write_gate, read_gate,
                                  on_output=on_tool_output,
                                  approve_callback=approve_callback)
            executed_tool_indices.add(idx)
        except Exception as _exc:
            # NEVER leave a tool_call_id orphaned — a failure result
            # must be appended so the next API call doesn't get a 400
            # "insufficient tool messages following tool_calls" error.
            from tools import ToolResult as TR
            import sys as _sys
            tool_name = tc.get("function", {}).get("name", "?")
            _sys.stderr.write(
                f"{type(_exc).__name__}: {_exc}\n"
            )
            _sys.stderr.flush()
            result = TR(
                success=False,
                content=f"Tool '{tool_name}' failed during streaming: {_exc}",
            )
        deferred_stream_results.append((tc, result))
        # on_tool_end is deferred to _tool_execution_phase to avoid
        # double-firing for streaming tools.

    msg = call_llm(messages, config, on_token=_outer_on_token,
                        session=session, on_tool_ready=_on_tool_ready,
                        cancel_event=cancel_event)

    # Strip internal tracking fields and merge into executed set
    fired_indices: set[int] = set(msg.pop("_fired_indices", []))
    executed_tool_indices |= fired_indices

    return msg, deferred_stream_results, executed_tool_indices


def _tool_execution_phase(
    msg: dict,
    messages: list[dict],
    deferred_stream_results: list[tuple],
    executed_tool_indices: set[int],
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    turn_count: int,
    *,
    on_tool_start: Callable[..., Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_tool_output: Callable[..., Any] | None = None,
    approve_callback: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
    recent_tool_keys: list[str] | None = None,
    tool_keys_lock: threading.Lock | None = None,
) -> bool:
    """Execute remaining tool calls after the API response.

    Filters already-executed tools (streaming), flushes deferred results,
    executes the rest via ``_execute_tools``, and saves the turn summary.

    Returns ``True`` to continue the turn loop, ``False`` to break
    (all tools were already executed during streaming).
    """
    raw_tool_calls = msg["tool_calls"]
    remaining = [
        tc for i, tc in enumerate(raw_tool_calls)
        if i not in executed_tool_indices
    ]

    if not remaining:
        # All tools were already executed during streaming
        messages.append(msg)
        # on_tool_end already fired during streaming (via _on_tool_ready)
        for tc, result in deferred_stream_results:
            _append_tool_result(messages, tc, result, on_tool_end=None,
                                recent_keys=recent_tool_keys,
                                lock=tool_keys_lock)
        _save_turn_summary(turn_count, msg, deferred_stream_results, messages)
        return False  # continue the turn loop

    # Keep all tool_calls so deferred results have a reference
    msg["tool_calls"] = raw_tool_calls
    messages.append(msg)

    # Flush deferred tool results from streaming execution
    # on_tool_end already fired during streaming (via _on_tool_ready)
    for tc, result in deferred_stream_results:
        _append_tool_result(messages, tc, result, on_tool_end=None,
                            recent_keys=recent_tool_keys,
                            lock=tool_keys_lock)

    # Execute remaining tools with piping support
    tool_results = _execute_tools(
        remaining, messages, write_gate, read_gate,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        on_tool_output=on_tool_output,
        approve_callback=approve_callback,
        cancel_event=cancel_event,
        recent_tool_keys=recent_tool_keys,
        tool_keys_lock=tool_keys_lock,
    )
    _save_turn_summary(turn_count, msg, tool_results, messages)
    return True  # continue the turn loop


def run_agent_turn(
    messages: list[dict],
    config: AgentConfig,
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    on_token: Callable[[str], Any] | None = None,
    on_tool_start: Callable[..., Any] | None = None,
    on_tool_end: Callable[..., Any] | None = None,
    on_tool_output: Callable[..., Any] | None = None,
    approve_callback: Callable[..., Any] | None = None,
    cancel_event: threading.Event | None = None,
    max_turns: int = 100,
    session: requests.Session | None = None,
    memory_store: Any = None,
) -> dict | None:
    """Run one full agent turn — possibly multiple API calls if tools are used.

    Calls the LLM, executes any tool calls, feeds results back, and repeats
    until the model returns a plain text response or the turn is cancelled.

    *messages* is mutated in place: assistant and tool messages are appended.
    Returns the final assistant message dict, or ``None`` if cancelled.
    *max_turns* is a hard safety cap (default 100).

    If *memory_store* is provided, the scratchpad is read from it and
    injected as context at the start of the turn.

    Multiple independent tool calls are executed in parallel via a thread pool.
    If *session* is a requests.Session, it is reused across API calls for
    connection reuse. If None, the requests module is used (test-friendly).

    Every 5 tool-using turns, a system reminder is injected to keep the agent
    on track and let it decide whether to continue or wrap up.
    """
    global _scratchpad_injected, _git_diff_injected
    _scratchpad_injected = False
    _git_diff_injected = False

    # One-time cleanup / cache invalidation
    # Note: clear_api_cache is intentionally NOT called here — the incremental
    # message-cleaning cache in api.py survives across turns since the same
    # messages list is mutated. Clearing it every turn defeats the optimization.
    clear_tool_cache()

    total_usage: dict[str, int] = {}
    turn_count: int = 0
    recent_tool_keys: deque[str] = deque()  # circuit breaker tracking
    tool_keys_lock: threading.Lock = threading.Lock()

    _original_session = session  # track whether we own the session for cleanup
    if session is None:
        session = requests  # test-friendly: mockable via patch("llm.requests.post")

    try:
        for _ in range(max_turns):
            turn_count += 1
            if cancel_event is not None and cancel_event.is_set():
                return None

            # ----- phase 1: injection -----
            _inject_context(
                messages,
                turn_count=turn_count,
                memory_store=memory_store,
                read_gate=read_gate,
                recent_tool_keys=recent_tool_keys,
                cancel_event=cancel_event,
            )

            # ----- phase 2: API call -----
            msg, deferred_stream_results, executed_tool_indices = _api_call_phase(
                messages, config, session, write_gate, read_gate,
                on_token=on_token,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_tool_output=on_tool_output,
                approve_callback=approve_callback,
                cancel_event=cancel_event,
            )

            if cancel_event is not None and cancel_event.is_set():
                return None

            # Accumulate token usage across all API calls in this turn
            if "_usage" in msg:
                total_usage = _accumulate_usage(total_usage, msg)

            # Plain text response — turn is finished
            if not msg.get("tool_calls"):
                # Safety net: model returned empty tool_calls with no content —
                # it's choking on a big task. Inject a recovery nudge and retry.
                content = (msg.get("content") or "").strip()
                if not content:
                    recovery = (
                        "You responded with empty output. This usually means "
                        "the task was too large to process in one step. "
                        "Break it down: pick ONE small, concrete next action "
                        "(read a file, search for something, write one function) "
                        "and call exactly ONE tool. Do not try to do everything at once."
                    )
                    messages.append({"role": "user", "content": recovery})
                    continue  # retry the turn loop
                if total_usage:
                    msg["_total_usage"] = total_usage
                if turn_count > 1:
                    msg["_turn_count"] = turn_count
                messages.append(msg)
                _save_turn_summary(turn_count, msg, [], messages)
                return msg

            # ----- phase 3: tool execution -----
            continue_loop = _tool_execution_phase(
                msg, messages, deferred_stream_results, executed_tool_indices,
                write_gate, read_gate, turn_count,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                on_tool_output=on_tool_output,
                approve_callback=approve_callback,
                cancel_event=cancel_event,
                recent_tool_keys=recent_tool_keys,
                tool_keys_lock=tool_keys_lock,
            )
            # _tool_execution_phase returns False when all tools were
            # already streamed — just continue the loop.
            if not continue_loop:
                continue

        # Auto-extend when close to budget (like sub-agent auto-extend)
        if turn_count >= max_turns - 3 and turn_count < max_turns + 10:
            max_turns += 10

        # Exceeded max_turns — return last assistant message (still has tool_calls)
        if 'msg' not in locals():
            return None  # max_turns was 0, no API call made
        if total_usage:
            msg["_total_usage"] = total_usage
        if turn_count > 1:
            msg["_turn_count"] = turn_count
        return msg
    finally:
        # Only close the session if we created it; caller-managed sessions
        # (passed via the session parameter) are the caller's responsibility.
        if session is not _original_session and hasattr(session, "close"):
            session.close()


def _append_tool_result(
    messages: list[dict],
    tc: dict,
    result: "ToolResult",
    on_tool_end: Callable[..., Any] | None = None,
    recent_keys: list[str] | None = None,
    lock: threading.Lock | None = None,
) -> None:
    """Append a tool result message and fire the on_tool_end callback."""
    from tools import ToolResult as TR
    detail = format_tool_detail(result, max_len=TOOL_DETAIL_DISPLAY_LENGTH)
    tool_name = tc.get("function", {}).get("name", "?")
    tool_call_id = tc.get("id", "?")
    if on_tool_end is not None:
        on_tool_end(result.success, detail, diff_preview=result.diff_preview)
    messages.append({
        "role": "tool",
        "tool_call_id": tc["id"],
        "content": result.to_json(),
    })
    # Track for circuit breaker
    if recent_keys is not None:
        if lock is not None:
            with lock:
                recent_keys.append(_tool_call_key(tc))
                while len(recent_keys) > _CIRCUIT_WINDOW:
                    recent_keys.popleft()
        else:
            recent_keys.append(_tool_call_key(tc))
            while len(recent_keys) > _CIRCUIT_WINDOW:
                recent_keys.popleft()


def _append_cancel_results(
    tool_calls: list[dict],
    messages: list[dict],
    *,
    on_tool_end: Callable[..., Any] | None = None,
    recent_keys: list[str] | None = None,
    lock: threading.Lock | None = None,
) -> None:
    """Append failure ToolResults for cancelled tool calls.

    Ensures every tool_call_id has a matching tool message so the next
    API call doesn't get a 400 "insufficient tool messages" error.
    """
    from tools import ToolResult as TR
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "?")
        result = TR(
            success=False,
            content=f"Tool '{name}' cancelled.",
        )
        _append_tool_result(messages, tc, result, on_tool_end,
                            recent_keys=recent_keys, lock=lock)
