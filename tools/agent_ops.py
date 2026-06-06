#!/usr/bin/env python3
"""
agent_ops.py — agent lifecycle tools for mini_agent.

Tools: spawn_agent, agent_status, collect_agent, collect_any,
       agent_extend, agent_cancel, diff, restore_file,
       session_stats, recall_turn, remember, read_image,
       wait_for_agent

spawn_agent launches a sub-agent in a background thread and returns
a task_id immediately (never blocks the parent).  agent_status polls
for completion.  collect_agent blocks until the sub-agent finishes
and returns the full result.  collect_any returns the first finishing
sub-agent from a set.
"""
from __future__ import annotations

import threading
import time
import uuid

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from agents.agent_runtime import AgentRuntime, SubAgentResult


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------

_MAX_CONCURRENT = 10         # hard cap on concurrent sub-agents
_DEFAULT_MAX_TURNS = 15      # default turn budget per sub-agent (soft — sub-agent loop self-governs via hung/error detection)


def _parse_max_turns(raw) -> "int | ToolResult":
    """Parse max_turns from args (soft cap — loop self-governs)."""
    try:
        mt = int(raw)
    except (TypeError, ValueError):
        return ToolResult(
            success=False,
            content=f"max_turns must be an integer, got: {raw}",
        )
    return max(1, mt)


def _spawn_one(
    task: str,
    config,
    runtime: AgentRuntime,
    wg: WriteSafetyGate,
    rg: ReadSafetyGate,
    max_turns: int,
    *,
    cancel_event: threading.Event | None = None,
    visible: bool = False,
    shared_context: str = "",
    subscriptions: list[str] | None = None,
    reserved_files: list[str] | None = None,
    parent_depth: int = 0,
    max_depth: int = 3,
    parent_task_id: str = "",
    subagent_callback: callable | None = None,
) -> str:
    """Spawn a single sub-agent thread. Returns the task_id."""
    from tools import _TOOL_CONTEXT
    from agents.sub_agent import run_sub_agent

    task_id = str(uuid.uuid4())[:8]
    if cancel_event is None:
        cancel_event = threading.Event()

    # Generate a short human-readable name from the task text
    words = task.strip().split()
    short_name = "_".join(w for w in words[:3] if w.isalnum() or w in ("-",))
    if not short_name:
        short_name = "agent"
    short_name = short_name.lower()[:24]

    # Capture the sub-agent callback NOW (before the thread starts) to avoid
    # a race condition: the parent clears _TOOL_CONTEXT._subagent_callback
    # after run_agent_turn returns, which may happen before the sub-agent
    # thread gets to read it.  Capturing here in the parent thread guarantees
    # we have the right value.
    if subagent_callback is None:
        subagent_callback = getattr(_TOOL_CONTEXT, "_subagent_callback", None)
    # DEBUG: log whether the callback was captured
    import sys as _sys_debug
    _sys_debug.__stderr__.write(f"[DEBUG _spawn_one] task={short_name} callback={'SET' if subagent_callback else 'NONE'}\n")

    def _runner() -> None:
        import sys as _sys
        sub_cb = subagent_callback  # captured in parent thread, no race
        # ---- Redirect stderr to buffer to prevent UI corruption ----
        from io import StringIO as _StringIO
        _stderr_buf = _StringIO()
        _saved_stderr = _sys.stderr
        _sys.stderr = _stderr_buf
        # ---- end stderr redirect ----
        # Set depth context for tools called by this sub-agent
        current_depth = parent_depth + 1
        _TOOL_CONTEXT._agent_depth = current_depth
        _TOOL_CONTEXT._agent_max_depth = max_depth
        _TOOL_CONTEXT._agent_task_id = task_id
        original_stream = config.stream
        # Notify Electron via sub-agent callback (if wired)
        if sub_cb:
            sub_cb("start", {"task_id": task_id, "parent_id": parent_task_id, "name": short_name, "desc": task})
        try:
            if visible:
                config.stream = True
                # Log to file for debugging
                import os as _os
                _os.makedirs("logs", exist_ok=True)
                _log_path = f"logs/sub_agent_{task_id}.log"
                with open(_log_path, "a", encoding="utf-8") as _lf:
                    _lf.write(f"\n--- [sub {task_id}] START: {task[:200]} ---\n")
            else:
                config.stream = False  # suppress raw token streaming for invisible sub-agents
            result = run_sub_agent(
                task=task,
                config=config,
                write_gate=wg,
                read_gate=rg,
                max_turns=max_turns,
                cancel_event=cancel_event,
                parent_depth=parent_depth,
                max_depth=max_depth,
                shared_context=shared_context,
                task_id=task_id,
                parent_task_id=parent_task_id,
                subagent_callback=sub_cb,
            )
            runtime.store_result(task_id, result)
            # Notify Electron via sub-agent callback (if wired)
            if sub_cb:
                sub_cb("end", {"task_id": task_id, "ok": result.success, "content": result.content[:500]})
        finally:
            config.stream = original_stream
            # ---- Restore stderr + flush buffer to disk only if non-empty ----
            _sys.stderr = _saved_stderr
            _stderr_text = _stderr_buf.getvalue()
            _stderr_buf.close()
            if _stderr_text:
                import os as _os
                _os.makedirs("logs", exist_ok=True)
                with open(f"logs/sub_agent_{task_id}_stderr.log", "w", encoding="utf-8") as _lf:
                    _lf.write(_stderr_text)
            # ---- end restore stderr ----
            # Restore agent context so parent isn't polluted
            _TOOL_CONTEXT._agent_depth = parent_depth
            _TOOL_CONTEXT._agent_task_id = parent_task_id
            # Always release file reservations, even on crash.
            # store_result (called in try) would normally do this, but if
            # run_sub_agent raises an unhandled exception, files are leaked.
            from tools import release_all_files
            release_all_files(task_id)

    thread = threading.Thread(target=_runner, daemon=True, name=f"subagent-{task_id}")
    # Determine parent task_id (passed explicitly by caller)
    parent_id = parent_task_id
    runtime.register(task_id, thread, cancel_event, max_turns,
                     label=short_name, parent_task_id=parent_id)
    # Reserve files if specified
    if reserved_files:
        from tools import _FILE_RESERVATIONS, _FILE_RESERVATIONS_LOCK
        with _FILE_RESERVATIONS_LOCK:
            for f in reserved_files:
                _FILE_RESERVATIONS[f] = task_id
    # Set subscriptions if provided
    if subscriptions is not None:
        runtime.set_subscriptions(task_id, subscriptions)
    thread.start()
    return task_id


@_register("spawn_agent")
def _spawn_agent(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Spawn a sub-agent to work on a task in the background.

    Returns a task_id immediately.  Use agent_status to poll or
    collect_agent to block until completion.

    Supports batch spawn via 'tasks' (list of task strings) in addition
    to single 'task' spawn.

    Set 'synchronous'=true to block until the sub-agent completes and
    return its result directly (agent-as-tool pattern).
    """
    from tools import _TOOL_CONTEXT

    # --- batch spawn (tasks=list) ---
    tasks_list = args.get("tasks", None)
    if tasks_list is not None:
        if not isinstance(tasks_list, list):
            return ToolResult(
                success=False,
                content="'tasks' must be a list of task description strings.",
            )
        valid_tasks = [t for t in tasks_list if isinstance(t, str) and t.strip()]
        if not valid_tasks:
            return ToolResult(
                success=False,
                content="No sub-agents could be spawned: 'tasks' must be a non-empty list.",
            )

        shared_context = args.get("shared_context", "")
        max_turns = _parse_max_turns(args.get("max_turns", _DEFAULT_MAX_TURNS))
        if isinstance(max_turns, ToolResult):
            return max_turns
        visible = args.get("visible", False)
        subscriptions = args.get("subscriptions", None)

        runtime: AgentRuntime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            return ToolResult(
                success=False,
                content="Agent runtime not initialized. Multi-agent support is unavailable.",
            )

        config = getattr(_TOOL_CONTEXT, "_agent_config", None)
        if config is None:
            return ToolResult(
                success=False,
                content="Agent config not available in tool context.",
            )

        parent_task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")
        synchronous = args.get("synchronous", False)
        task_ids = []
        _max_concurrent = getattr(getattr(_TOOL_CONTEXT, "_agent_config", None), "sub_agent_max_concurrent", _MAX_CONCURRENT)
        for task in valid_tasks:
            if runtime.active_count >= _max_concurrent:
                break
            tid = _spawn_one(task, config, runtime, wg, rg, max_turns,
                             cancel_event=None, visible=visible,
                             shared_context=shared_context,
                             subscriptions=subscriptions,
                             parent_depth=getattr(_TOOL_CONTEXT, "_agent_depth", 0),
                             max_depth=getattr(_TOOL_CONTEXT, "_agent_max_depth", 3),
                             parent_task_id=parent_task_id)
            task_ids.append(tid)

        if not task_ids:
            return ToolResult(
                success=False,
                content=f"Too many sub-agents running ({runtime.active_count} active, "
                        f"max {_MAX_CONCURRENT}). Wait for some to complete before spawning more.",
            )

        # --- synchronous mode: block until all complete (agent-as-tool pattern) ---
        if synchronous:
            results = []
            for tid in task_ids:
                def _ready(tid=tid):
                    return runtime.get_status(tid) != "running"
                with runtime._condition:
                    runtime._condition.wait_for(_ready, timeout=_COLLECT_TIMEOUT)
                status = runtime.get_status(tid)
                if status == "completed":
                    result = runtime.get_result(tid)
                    if result is not None:
                        runtime._collected.add(tid)
                        results.append(result)
            if not results:
                return ToolResult(
                    success=False,
                    content="Synchronous spawn: no agents completed in time.",
                )
            lines = [f"Synchronous batch: {len(results)}/{len(task_ids)} completed:"]
            for r in results:
                lines.append(f"  - [{r.success}] {r.content[:200]}")
            return ToolResult(success=True, content="\n".join(lines))

        return ToolResult(
            success=True,
            content=(
                f"Spawned {len(task_ids)} sub-agent(s): {', '.join(task_ids)}.\n"
                f"Use agent_status or collect_agent to check results.\n"
                f"Use collect_any() to grab the fastest completion."
            ),
        )

    # --- single task spawn ---
    task = args.get("task", "")
    if not task.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task' (the task description for the sub-agent).",
        )

    max_turns = _parse_max_turns(args.get("max_turns", _DEFAULT_MAX_TURNS))
    if isinstance(max_turns, ToolResult):
        return max_turns
    visible = args.get("visible", False)
    shared_context = args.get("shared_context", "")
    subscriptions = args.get("subscriptions", None)

    runtime: AgentRuntime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized. Multi-agent support is unavailable.",
        )

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    _max_concurrent = getattr(config, "sub_agent_max_concurrent", _MAX_CONCURRENT)
    if runtime.active_count >= _max_concurrent:
        return ToolResult(
            success=False,
            content=(
                f"Too many sub-agents running ({runtime.active_count} active, "
                f"max {_MAX_CONCURRENT}). Wait for some to complete with "
                f"agent_status or collect_agent before spawning more."
            ),
        )

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    if config is None:
        return ToolResult(
            success=False,
            content="Agent config not available in tool context.",
        )

    parent_task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")
    synchronous = args.get("synchronous", False)
    task_id = _spawn_one(task, config, runtime, wg, rg, max_turns,
                         cancel_event=None, visible=visible,
                         shared_context=shared_context,
                         subscriptions=subscriptions,
                         parent_depth=getattr(_TOOL_CONTEXT, "_agent_depth", 0),
                         max_depth=getattr(_TOOL_CONTEXT, "_agent_max_depth", 3),
                         parent_task_id=parent_task_id)

    # --- synchronous mode: block until complete (agent-as-tool pattern) ---
    if synchronous:
        def _ready():
            return runtime.get_status(task_id) != "running"
        with runtime._condition:
            runtime._condition.wait_for(_ready, timeout=_COLLECT_TIMEOUT)
        status = runtime.get_status(task_id)
        if status == "completed":
            result = runtime.get_result(task_id)
            if result is not None:
                runtime._collected.add(task_id)
                return ToolResult(
                    success=result.success,
                    content=(
                        f"Synchronous agent '{task_id}' completed:\n"
                        f"  Success: {result.success}\n"
                        f"  Turns: {result.turns_used}\n"
                        f"  Content:\n{result.content}"
                    ),
                )
        return ToolResult(
            success=False,
            content=f"Synchronous agent '{task_id}' did not complete in time.",
        )

    return ToolResult(
        success=True,
        content=(
            f"Spawned sub-agent '{task_id}' with {max_turns} turn budget.\n"
            f"Task: {task[:200]}{'...' if len(task) > 200 else ''}\n"
            f"Use agent_status('{task_id}') to poll or "
            f"collect_agent('{task_id}') to block until done."
        ),
    )


@_summarize("spawn_agent")
def _spawn_agent_summary(args: dict) -> str:
    tasks_list = args.get("tasks")
    if tasks_list and isinstance(tasks_list, list):
        return f"spawn_agent(tasks=[{len(tasks_list)} items])"
    task = args.get("task", "?")
    preview = task[:60]
    if len(task) > 60:
        preview += "\u2026"
    return f"spawn_agent(\"{preview}\")"


# ---------------------------------------------------------------------------
# agent_status
# ---------------------------------------------------------------------------

@_register("agent_status")
def _agent_status(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Check the status of a sub-agent without blocking.

    Returns 'running', 'completed' with a result summary, or 'not_found'.
    """
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(success=False, content="Missing required parameter: 'task_id'.")

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    status = runtime.get_status(task_id)
    if status == "not_found":
        return ToolResult(
            success=True,
            content=f"Sub-agent '{task_id}' not found (may have completed or never existed).",
        )

    if status == "running":
        active = runtime.active_count
        content = f"Sub-agent '{task_id}' is still running. ({active} total active)"
        # Include auto-snapshot if available
        snap = runtime.get_snapshot(task_id)
        if snap is not None:
            age = ""  # snapshot timestamps are monotonic; just show turn info
            content += (
                f"\n  Turn: {snap['turn']}/{snap['turns_budget']}"
                f" | Tools called: {snap['tool_calls_made']}"
                f"\n  Last tool: {snap['last_tool'] or '(none)'}"
            )
            if snap["last_tool_summary"]:
                summary = snap["last_tool_summary"]
                if len(summary) > 120:
                    summary = summary[:120] + "..."
                content += f"\n  Result: {summary}"
            if snap.get("last_action") in ("thinking", "calling_llm"):
                content += f"\n  Status: {snap['last_action']}"
                if snap.get("streamed_tokens"):
                    content += f" ({snap['streamed_tokens']} tokens streamed)"
                if snap.get("thought_snippet"):
                    content += f"\n  Thought: …{snap['thought_snippet'][-120:]}"
            if snap["last_error"]:
                content += f"\n  ⚠ Last error: {snap['last_error'][:120]}"
            if snap["scratchpad_snippet"]:
                snippet = snap["scratchpad_snippet"]
                if len(snippet) > 150:
                    snippet = snippet[-150:]
                content += f"\n  Scratchpad: {snippet}"
            # If last heartbeat was a while ago, nudge
            import time as _time
            age_s = _time.monotonic() - snap["timestamp"]
            if age_s > 60:
                content += f"\n  ⚠ Last snapshot {age_s:.0f}s ago — agent may be stuck or between turns."
        else:
            content += "\n  (no snapshot yet — agent may not have completed a turn)"
        return ToolResult(success=True, content=content)

    # Completed
    result = runtime.get_result(task_id)
    runtime._collected.add(task_id)
    if result is None:
        return ToolResult(success=True, content=f"Sub-agent '{task_id}' completed but result not found.")

    summary = (
        f"Sub-agent '{task_id}': completed.\n"
        f"  Success: {result.success}\n"
        f"  Turns used: {result.turns_used}\n"
        f"  Tool calls: {result.tool_calls_made}\n"
        f"  Summary: {result.content[:500]}{'...' if len(result.content) > 500 else ''}"
    )
    if result.error:
        summary += f"\n  Error: {result.error}"

    return ToolResult(success=True, content=summary)


@_summarize("agent_status")
def _agent_status_summary(args: dict) -> str:
    return f"agent_status({args.get('task_id', '?')})"


# ---------------------------------------------------------------------------
# collect_agent
# ---------------------------------------------------------------------------

_COLLECT_TIMEOUT = 30  # seconds to wait for sub-agent completion (kept moderate
                       # so parent agent can check for user interjections between
                       # polls — use agent_extend + collect_agent again if needed)


@_register("collect_agent")
def _collect_agent(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Block until a sub-agent completes, then return its full result."""
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(success=False, content="Missing required parameter: 'task_id'.")

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    status = runtime.get_status(task_id)
    if status == "not_found":
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )

    if status == "running":
        # Wait for completion using condition.wait_for with predicate,
        # which atomically checks status under the lock to avoid lost wakeups.
        def _completed():
            return runtime.get_status(task_id) != "running"
        with runtime._condition:
            runtime._condition.wait_for(_completed, timeout=_COLLECT_TIMEOUT)

        # If still running after timeout, report back — don't cancel.
        # The parent can extend turns or collect again later.
        final_status = runtime.get_status(task_id)
        if final_status == "running":
            return ToolResult(
                success=False,
                content=(
                    f"Sub-agent '{task_id}' is still running after {_COLLECT_TIMEOUT}s. "
                    "It continues in the background. Use agent_status, agent_extend, "
                    "or collect_agent again to wait longer."
                ),
            )

    result = runtime.get_result(task_id)
    runtime._collected.add(task_id)
    if result is None:
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' completed but no result was stored.",
        )

    content = (
        f"Sub-agent '{task_id}' result:\n"
        f"  Success: {result.success}\n"
        f"  Turns used: {result.turns_used}\n"
        f"  Tool calls: {result.tool_calls_made}\n"
        f"  Content:\n{result.content}\n"
    )
    if result.scratchpad:
        content += f"  Scratchpad (final):\n{result.scratchpad[:500]}\n"
    if result.error:
        content += f"  Error: {result.error}\n"

    return ToolResult(
        success=result.success,
        content=content,
    )


@_summarize("collect_agent")
def _collect_agent_summary(args: dict) -> str:
    return f"collect_agent({args.get('task_id', '?')})"


# ---------------------------------------------------------------------------
# collect_any
# ---------------------------------------------------------------------------

_COLLECT_ANY_TIMEOUT = 60   # seconds to wait for any sub-agent (was 10s; 60s
                            # reduces orchestrator polling frequency and context
                            # bloat from repeated orchestration injections)


@_register("collect_any")
def _collect_any(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Collect the first sub-agent that finishes.

    If any have already completed, returns immediately.
    Otherwise polls until one completes or timeout.
    """
    from tools import _TOOL_CONTEXT

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    task_ids = args.get("task_ids", None)
    if task_ids is not None and not isinstance(task_ids, list):
        return ToolResult(
            success=False,
            content="'task_ids' must be a list of task ID strings.",
        )

    # Determine which tasks to check
    if task_ids:
        candidates = task_ids
    else:
        with runtime._lock:
            # Include both running tasks and completed results
            candidates = set(runtime.tasks.keys()) | set(runtime.results.keys())
            # Exclude already-collected results
            candidates -= runtime._collected

    if not candidates:
        return ToolResult(
            success=False,
            content="No sub-agents to collect.",
        )

    # Check for already-completed
    candidates = list(candidates)  # materialize for iteration
    for tid in candidates:
        status = runtime.get_status(tid)
        if status == "completed":
            result = runtime.get_result(tid)
            if result is not None:
                runtime._collected.add(tid)
                return _format_collect_any(tid, result)

    # Wait for any completion using condition.wait_for with predicate,
    # which atomically checks status under the lock to avoid lost wakeups.
    def _any_completed():
        for tid in candidates:
            if runtime.get_status(tid) == "completed":
                return True
        return False
    with runtime._condition:
        runtime._condition.wait_for(_any_completed, timeout=_COLLECT_ANY_TIMEOUT)

    for tid in candidates:
        status = runtime.get_status(tid)
        if status == "completed":
            result = runtime.get_result(tid)
            if result is not None:
                runtime._collected.add(tid)
                return _format_collect_any(tid, result)

    # Report which sub-agents are still running so the parent can retry
    still_running = [tid for tid in candidates if runtime.get_status(tid) == "running"]
    return ToolResult(
        success=False,
        content=(
            f"No sub-agent completed within {_COLLECT_ANY_TIMEOUT}s. "
            f"Still running: {still_running if still_running else 'none'}. "
            "Use collect_any again to retry."
        ),
    )


def _format_collect_any(task_id: str, result: SubAgentResult) -> ToolResult:
    """Format a collected sub-agent result."""
    content = (
        f"Sub-agent '{task_id}' finished first:\n"
        f"  Success: {result.success}\n"
        f"  Turns used: {result.turns_used}\n"
        f"  Tool calls: {result.tool_calls_made}\n"
        f"  Content:\n{result.content}\n"
    )
    if result.error:
        content += f"  Error: {result.error}\n"
    return ToolResult(success=result.success, content=content)


@_summarize("collect_any")
def _collect_any_summary(args: dict) -> str:
    tids = args.get("task_ids")
    if tids:
        return f"collect_any([{len(tids)} ids])"
    return "collect_any()"

# ---------------------------------------------------------------------------
# agent_extend
# ---------------------------------------------------------------------------

@_register("agent_extend")
def _agent_extend(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Extend the turn budget of a running sub-agent."""
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task_id'.",
        )

    additional = args.get("additional", 10)
    try:
        additional = int(additional)
    except (TypeError, ValueError):
        return ToolResult(
            success=False,
            content=f"'additional' must be an integer, got: {additional}",
        )
    if additional < 1:
        return ToolResult(
            success=False,
            content="'additional' must be a positive integer.",
        )

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    status = runtime.get_status(task_id)
    if status == "not_found":
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )

    if status == "completed":
        return ToolResult(
            success=True,
            content=f"Sub-agent '{task_id}' has already completed.",
        )

    ok = runtime.extend_turns(task_id, additional)
    if not ok:
        return ToolResult(
            success=False,
            content=f"Failed to extend turns for '{task_id}'.",
        )

    new_max = runtime.get_max_turns(task_id)
    return ToolResult(
        success=True,
        content=f"Extended sub-agent '{task_id}' by +{additional} turns "
                f"(new max: {new_max}).",
    )


@_summarize("agent_extend")
def _agent_extend_summary(args: dict) -> str:
    return f"agent_extend({args.get('task_id', '?')}, +{args.get('additional', 10)})"


# ---------------------------------------------------------------------------
# agent_cancel
# ---------------------------------------------------------------------------

@_register("agent_cancel")
def _agent_cancel(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Cancel a running sub-agent by setting its cancel event."""
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task_id'.",
        )

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    status = runtime.get_status(task_id)
    if status == "not_found":
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )

    if status == "completed":
        return ToolResult(
            success=True,
            content=f"Sub-agent '{task_id}' has already completed.",
        )

    ok = runtime.cancel(task_id)
    if not ok:
        return ToolResult(
            success=False,
            content=f"Failed to cancel sub-agent '{task_id}'.",
        )

    return ToolResult(
        success=True,
        content=f"Sub-agent '{task_id}' cancellation requested.",
    )


@_summarize("agent_cancel")
def _agent_cancel_summary(args: dict) -> str:
    return f"agent_cancel({args.get('task_id', '?')})"

# ---------------------------------------------------------------------------
# diff tool — show unstaged changes via git
# ---------------------------------------------------------------------------

@_register("diff")
def _diff(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Show unstaged changes (git diff) for the workspace or a specific file."""
    import subprocess
    path = args.get("path", "")
    cmd = ["git", "-C", rg.workspace_root, "diff"]
    if path:
        cmd.append(path)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ToolResult(success=False, content=r.stderr or "git diff failed")
        if not r.stdout.strip():
            return ToolResult(success=True, content="No unstaged changes.")
        return ToolResult(success=True, content=r.stdout.rstrip())
    except FileNotFoundError:
        return ToolResult(success=False, content="git not found")
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, content="diff timed out")
    except Exception as e:
        return ToolResult(success=False, content=f"Error running diff: {e}")


@_summarize("diff")
def _diff_summary(args: dict) -> str:
    path = args.get("path", "")
    if path:
        return f"diff({path})"
    return "diff()"


# ---------------------------------------------------------------------------
# restore_file — session undo
# ---------------------------------------------------------------------------

# _BACKUPS lives in file_ops.py (shared with _backup_before_write)
from tools.file_ops import _BACKUPS
import shutil
import os as _os_restore


@_register("restore_file")
def _restore_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Restore a file from its session backup (undo the last write/edit)."""
    path = args["path"]
    safety_result = wg.check(path)
    if not safety_result.allowed:
        return ToolResult(
            success=False,
            content=f"Restore blocked by safety layer: {safety_result.reason}",
        )
    resolved = safety_result.resolved_path

    if resolved not in _BACKUPS:
        return ToolResult(
            success=False,
            content=f"No backup available for '{resolved}'. Only files modified this session can be restored.",
            hint="No backup exists. Either the file hasn't been modified this session, or it was already restored.",
        )

    backup_path = _BACKUPS[resolved]
    try:
        shutil.copy2(backup_path, resolved)
        del _BACKUPS[resolved]
        from tools import _MODIFIED_FILES, _MODIFIED_FILES_LOCK
        with _MODIFIED_FILES_LOCK:
            _MODIFIED_FILES.discard(safety_result.resolved_path)
        return ToolResult(
            success=True,
            content=f"Restored '{resolved}' from backup ({_os_restore.path.basename(backup_path)}).",
        )
    except Exception as e:
        return ToolResult(
            success=False,
            content=f"Error restoring '{resolved}': {e}",
        )


@_summarize("restore_file")
def _restore_file_summary(args: dict) -> str:
    return f"restore_file({args.get('path', '?')})"


# ---------------------------------------------------------------------------
# recall_turn — retrieve a summary of a past turn
# ---------------------------------------------------------------------------

@_register("session_stats")
def _session_stats(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Show session statistics: turns, tokens, context usage, active sub-agents."""
    turn_history = getattr(_TOOL_CONTEXT, "_turn_history", None) or {}
    turns_used = len(turn_history)
    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    token_count = 0
    if memory_store is not None:
        token_count = memory_store.token_count
    CONTEXT_BUDGET = 800_000
    pct_used = (token_count / CONTEXT_BUDGET * 100) if CONTEXT_BUDGET else 0

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    active_agents = len(runtime.get_running_ids()) if runtime else 0
    completed_agents = len(runtime.results) if runtime else 0

    lines = [
        f"Turns used:    {turns_used}",
        f"Context tokens: {token_count:} / {CONTEXT_BUDGET:} ({pct_used:.1f}% used)",
        f"Sub-agents:     {active_agents} active, {completed_agents} completed",
    ]
    plan = getattr(_TOOL_CONTEXT, "_plan_steps", [])
    plan_done = getattr(_TOOL_CONTEXT, "_plan_done", set())
    if plan:
        done_count = len(plan_done)  # plan_done stores 0-based indices, just count
        lines.append(f"Plan:           {done_count}/{len(plan)} steps done")

    return ToolResult(success=True, content="\\n".join(lines))


@_summarize("session_stats")
def _session_stats_summary(args: dict) -> str:
    return "session_stats"


@_register("recall_turn")
def _recall_turn(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Return a summary of what happened on a given turn number."""
    turn = args.get("turn", 0)
    if not isinstance(turn, int) or turn < 1:
        return ToolResult(success=False, content="turn must be a positive integer")

    history = _TOOL_CONTEXT._turn_history
    if turn not in history:
        available = sorted(history.keys()) if history else []
        return ToolResult(
            success=True,
            content=(
                f"No record of turn {turn}. "
                + (f"Available turns: {available}" if available else "No turns recorded yet.")
            ),
        )

    return ToolResult(success=True, content=f"Turn {turn}:\n{history[turn]}")


@_summarize("recall_turn")
def _recall_turn_summary(args: dict) -> str:
    return f"recall_turn({args.get('turn', '?')})"


# ---------------------------------------------------------------------------
# remember — store project knowledge in the persistent knowledge base
# ---------------------------------------------------------------------------


@_register("remember")
def _remember(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Store a project-level learning that persists across sessions.

    Saved to the ``project_knowledge`` table in the session SQLite DB.
    Auto-categorizes the learning if no category is provided.
    Returns a summary of what was stored.
    """
    topic = args.get("topic", "")
    detail = args.get("detail", "")
    category = args.get("category", "")
    if not topic.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'topic' (short topic label for this learning).",
        )

    # Auto-categorize if no category provided
    if not category:
        try:
            from tools.failure_learning import suggest_category, KNOWLEDGE_CATEGORIES
            category = suggest_category(topic, detail)
            if category not in KNOWLEDGE_CATEGORIES:
                category = "general"
        except ImportError:
            category = "general"

    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    topic_preview = topic[:200] + ("..." if len(topic) > 200 else "")
    detail_preview = detail[:200] + ("..." if len(detail) > 200 else "")
    if memory_store is not None:
        try:
            conn = memory_store._get_conn()
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",
                (category, topic, detail),
            )
            conn.commit()
        except Exception as e:
            return ToolResult(
                success=True,
                content=f"Remember noted, but DB insert failed: {e}",
            )
        return ToolResult(
            success=True,
            content=(
                f"Stored in project knowledge [{category}]:\\n"
                f"  Topic: {topic_preview}\\n"
                f"  Detail: {detail_preview}"
            ),
        )

    # Fallback: try SQLite directly via scratchpad_path
    db_path = getattr(_TOOL_CONTEXT, "scratchpad_path", None)
    if db_path:
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",
                (category, topic, detail),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            return ToolResult(
                success=True,
                content=f"Remember noted, but DB fallback failed: {e}",
            )
        return ToolResult(
            success=True,
            content=(
                f"Stored in project knowledge (DB fallback) [{category}]:\\n"
                f"  Topic: {topic_preview}\\n"
                f"  Detail: {detail_preview}"
            ),
        )

    return ToolResult(
        success=True,
        content=(
            f"Remember noted (no persistent storage available) [{category}]:\\n"
            f"  Topic: {topic_preview}"
        ),
    )


@_summarize("remember")
def _remember_summary(args: dict) -> str:
    topic = args.get("topic", "?")
    preview = topic[:60]
    if len(topic) > 60:
        preview += "…"
    return f"remember(\"{preview}\")"


# ---------------------------------------------------------------------------
# read_image — describe an image using GPT-4o
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"})


def _guess_mime_type(path: str) -> str:
    """Guess MIME type from file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
    }.get(ext, "image/png")


@_register("read_image")
def _read_image(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Read an image file, send it to GPT-4o, and return a text description."""
    import base64
    import requests

    path = args.get("path", "")
    if not path:
        return ToolResult(success=False, content="Missing required parameter: 'path'.")

    # Safety check: ensure the file is within the workspace
    sr = rg.check(path)
    if not sr.allowed:
        return ToolResult(success=False, content=f"Read blocked: {sr.reason}")

    # Validate file exists and is an image
    import os as _os
    resolved = sr.resolved_path
    if not _os.path.isfile(resolved):
        return ToolResult(success=False, content=f"File not found: {path}")

    ext = _os.path.splitext(resolved)[1].lower()
    if ext not in _IMAGE_EXTENSIONS:
        return ToolResult(
            success=False,
            content=f"Unsupported image format: {ext}. Supported: {sorted(_IMAGE_EXTENSIONS)}",
        )

    mime_type = _guess_mime_type(resolved)

    # Read and base64-encode the image
    try:
        with open(resolved, "rb") as f:
            image_bytes = f.read()
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
    except Exception as e:
        return ToolResult(success=False, content=f"Failed to read image: {e}")

    # Get API key from tool context
    openai_api_key = _TOOL_CONTEXT.openai_api_key or ""
    if not openai_api_key:
        return ToolResult(
            success=False,
            content="OpenAI API key not configured. Set OPENAI_API_KEY env var or openai_api_key in .mini_agent.toml.",
        )

    # Build the GPT-4o request
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe this image in detail. What do you see?",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64_data}",
                            "detail": "auto",
                        },
                    },
                ],
            }
        ],
        "max_tokens": 1000,
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            return ToolResult(
                success=False,
                content=f"OpenAI API error ({resp.status_code}): {resp.text[:500]}",
            )
        data = resp.json()
        description = data["choices"][0]["message"]["content"]
        return ToolResult(success=True, content=description)
    except requests.exceptions.Timeout:
        return ToolResult(success=False, content="OpenAI API request timed out (60s).")
    except Exception as e:
        return ToolResult(success=False, content=f"OpenAI API request failed: {e}")


# ---------------------------------------------------------------------------
# wait_for_agent
# ---------------------------------------------------------------------------

@_register("wait_for_agent")
def _wait_for_agent(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Block until any sub-agent completes, needs extension, or timeout expires.

    Wakes on three events:
    1. Any agent completes (returns result immediately)
    2. Any agent exhausts its turn budget (returns so orchestrator can extend)
    3. New messages arrive in the orchestrator's inbox

    Sleeps with exponential backoff (1s→2s→4s…→30s) between polls
    to minimize LLM token burn.
    """
    from tools import _TOOL_CONTEXT

    task_ids = args.get("task_ids", [])
    timeout = args.get("timeout", 120)

    if not task_ids:
        return ToolResult(success=False, content="Missing required parameter: 'task_ids'.")

    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    last_inbox_count = len(runtime.messages)  # track new messages

    def _check_once() -> str | None:
        """Return reason to wake, or None if still waiting."""
        nonlocal last_inbox_count
        # Check for completed agents
        for tid in task_ids:
            s = runtime.get_status(tid)
            if s == "completed":
                result = runtime.get_result(tid)
                if result is not None:
                    runtime._collected.add(tid)
                    return "completed:" + tid
            # Check for hung/error agents via snapshot timestamps
            snap = runtime.get_snapshot(tid)
            if snap and s == "running":
                age_s = time.monotonic() - snap["timestamp"]
                if age_s > 300:  # 5 min since last snapshot → likely hung
                    return "hung:" + tid
                if snap.get("last_error") and snap["turn"] > 3:
                    return "error:" + tid
        # Check for new inbox messages
        if len(runtime.messages) > last_inbox_count:
            last_inbox_count = len(runtime.messages)
            return "new_message"
        return None

    # Check immediately
    reason = _check_once()
    if reason is not None:
        if reason.startswith("completed:"):
            tid = reason.split(":", 1)[1]
            return _format_collect_any(tid, runtime.get_result(tid))
        if reason.startswith("hung:") or reason.startswith("error:"):
            tid = reason.split(":", 1)[1]
            return ToolResult(
                success=False,
                content=f"Agent '{tid}' may be stuck ({reason.split(':',1)[0]}). Check agent_status for details. Use agent_extend or agent_cancel.",
            )
        return ToolResult(
            success=False,
            content=f"Agent needs attention: {reason}. Use agent_status to check.",
        )

    deadline = time.time() + timeout
    delay = 1.0

    while time.time() < deadline:
        reason = _check_once()
        if reason is not None:
            if reason.startswith("completed:"):
                tid = reason.split(":", 1)[1]
                return _format_collect_any(tid, runtime.get_result(tid))
            if reason in ("new_message",):
                return ToolResult(
                    success=False,
                    content="New message(s) in inbox. Check agent_inbox.",
                )
            tid = reason.split(":", 1)[1] if ":" in reason else ""
            return ToolResult(
                success=False,
                content=f"Agent '{tid}' needs attention: {reason}. Use agent_status to check.",
            )

        time.sleep(min(delay, deadline - time.time()))
        delay = min(delay * 2, 30.0)

    still_running = [tid for tid in task_ids if runtime.get_status(tid) == "running"]
    return ToolResult(
        success=False,
        content=(
            f"Timeout after {timeout}s. Still running: {still_running}. "
            "Use agent_extend then retry."
        ),
    )


@_summarize("wait_for_agent")
def _wait_for_agent_summary(args: dict) -> str:
    tids = args.get("task_ids", [])
    timeout = args.get("timeout", 120)
    return f"wait_for_agent([{len(tids)} ids], {timeout}s)"

@_summarize("read_image")
def _read_image_summary(args: dict) -> str:
    path = args.get("path", "?")
    return f"read_image({path})"
