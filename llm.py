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
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, TYPE_CHECKING

import requests

from api import APIError, format_tool_detail, call_llm, call_deepseek  # noqa: F401 call_deepseek re-exported for tests
from config import AgentConfig
from tools import execute_tool, tool_summary, clear_tool_cache, _TOOL_CONTEXT
from safety import ReadSafetyGate, WriteSafetyGate
from logging_setup import get_logger, log_error_trace

if TYPE_CHECKING:
    from tools import ToolResult

_log = get_logger("llm")


# ---------------------------------------------------------------------------
# Named constants (extracted from magic numbers)
# ---------------------------------------------------------------------------

# Display / truncation
TOOL_DETAIL_DISPLAY_LENGTH = 300   # max chars for tool result detail display

# Turn summary
TURN_SUMMARY_ASSISTANT_PREVIEW = 200  # max chars for assistant content in summary
TURN_SUMMARY_RESULT_PREVIEW = 150     # max chars for tool result content in summary
TURN_HISTORY_MAX_ENTRIES = 200        # cap on _turn_history entries

# Orchestration / context injection — constants now in context_inject.py
# (imported below after the circuit breaker section)

# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Circuit breaker — guards against repeated identical tool calls
# ---------------------------------------------------------------------------
# _CIRCUIT_WINDOW and _CIRCUIT_THRESHOLD defined in context_inject.py
# (single source of truth, shared with the circuit breaker implementation).
from context_inject import _CIRCUIT_WINDOW, _CIRCUIT_THRESHOLD


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


# Read-only nudge threshold (used by context_inject._inject_progress_check via _TOOL_CONTEXT)
_READ_ONLY_NUDGE_THRESHOLD: int = 3  # turns of pure reads before nudge

# ---------------------------------------------------------------------------
# Context injection — imported from context_inject.py
# (extracted to keep the orchestrator focused on the main loop).
# ---------------------------------------------------------------------------

from context_inject import (  # noqa: E402
    _inject_context,
    _inject_failure_pattern_warnings,
    _inject_pre_execution_context,
    _record_tool_sequence_to_graph,
    _check_circuit,
    _tool_call_key,
    _compress_stale_tool_results,
)


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
            tool_name = tc.get("function", {}).get("name", "?")
            log_error_trace("tool_execution_crash", f"{type(_exc).__name__}: {_exc}",
                            exc_info=True, extra={"tool_name": tool_name})
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
        # Fire on_tool_end for deferred streaming results (not fired during streaming)
        for tc, result in deferred_stream_results:
            _append_tool_result(messages, tc, result, on_tool_end=on_tool_end,
                                recent_keys=recent_tool_keys,
                                lock=tool_keys_lock)
        # Record streaming-executed tools to ToolGraph
        _record_tool_sequence_to_graph(deferred_stream_results)
        _save_turn_summary(turn_count, msg, deferred_stream_results, messages)
        return False  # continue the turn loop

    # Keep all tool_calls so deferred results have a reference
    msg["tool_calls"] = raw_tool_calls
    messages.append(msg)

    # Flush deferred tool results from streaming execution
    for tc, result in deferred_stream_results:
        _append_tool_result(messages, tc, result, on_tool_end=on_tool_end,
                            recent_keys=recent_tool_keys,
                            lock=tool_keys_lock)

    # --- Inject self-learning context before executing remaining tools ---
    _inject_pre_execution_context(messages, remaining, turn_count)

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

    # --- Record tool sequence to ToolGraph for future pattern learning ---
    # Include deferred results for complete turn picture
    all_results = deferred_stream_results + tool_results
    _record_tool_sequence_to_graph(all_results)

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
    # Reset one-time injection flags on AgentContext (no module-level globals)
    _TOOL_CONTEXT._scratchpad_injected = False
    _TOOL_CONTEXT._git_diff_injected = False
    _TOOL_CONTEXT._handoff_injected = False
    _TOOL_CONTEXT._state_txt_injected = False

    # Store provider on context for subsystem access (system reminder interval, etc.)
    _TOOL_CONTEXT._provider = config.api_provider

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
            # P4 fix: inject failure pattern warnings for the NEW tool calls
            # right before execution (was only pre-API-call before, missing
            # the current turn's tool choices).
            _inject_failure_pattern_warnings(msg, messages)
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

            # --- Track consecutive read-only turns ---
            _write_tools = {"write_file", "edit_file", "run_shell"}
            all_tool_calls = msg.get("tool_calls", [])
            if all_tool_calls:
                had_write = any(
                    tc.get("function", {}).get("name", "") in _write_tools
                    for tc in all_tool_calls
                )
                if had_write:
                    _TOOL_CONTEXT._consecutive_read_only_turns = 0
                else:
                    _TOOL_CONTEXT._consecutive_read_only_turns += 1

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
    detail = format_tool_detail(result, max_len=TOOL_DETAIL_DISPLAY_LENGTH)
    if on_tool_end is not None:
        on_tool_end(result.success, detail, diff_preview=result.diff_preview, content=result.content)
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
