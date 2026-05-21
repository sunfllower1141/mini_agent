#!/usr/bin/env python3
"""
agent_ops.py — multi-agent tools for mini_agent.

Tools: spawn_agent, agent_status, collect_agent, collect_any,
       agent_message, agent_read, agent_handoff, agent_inbox,
       agent_subscribe, agent_extend, agent_cancel

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

from safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from agent_runtime import AgentRuntime, SubAgentResult
from tools.agent_messages import (
    AgentMessage,
    MSG_TYPE_REGISTRY,
    _route_message,
    register_message_type,
)


# ---------------------------------------------------------------------------
# Shared state for inter-agent messaging
# ---------------------------------------------------------------------------

_AGENT_MSGS: list[dict] = []
_AGENT_MSGS_MAX = 1000        # ring-buffer cap: keep last N messages
_AGENT_MSGS_LOCK = threading.Lock()

# --- Todo tracking: in-memory list, survives across turns ---
_AGENT_TODOS: list[dict] = []  # [{"id": str, "content": str, "status": "pending"|"done"}]
_AGENT_TODOS_LOCK = threading.Lock()


@_register("todo_write")
def _todo_write(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Create or update a todo item. Set content to empty string to delete."""
    global _AGENT_TODOS
    todo_id = args.get("id", "")
    content = args.get("content", "")
    status = args.get("status", "pending")
    with _AGENT_TODOS_LOCK:
        if not content and todo_id:
            _AGENT_TODOS = [t for t in _AGENT_TODOS if t["id"] != todo_id]
            return ToolResult(success=True, content=f"Todo '{todo_id}' deleted.")
        if todo_id:
            for t in _AGENT_TODOS:
                if t["id"] == todo_id:
                    t["content"] = content
                    if status:
                        t["status"] = status
                    return ToolResult(success=True, content=f"Todo '{todo_id}' updated.")
        new_id = todo_id or str(len(_AGENT_TODOS) + 1)
        _AGENT_TODOS.append({"id": new_id, "content": content, "status": status})
        return ToolResult(success=True, content=f"Todo '{new_id}' created.")


@_register("todo_read")
def _todo_read(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read all todos or filter by status/id."""
    todo_id = args.get("id", "")
    status_filter = args.get("status", "")
    with _AGENT_TODOS_LOCK:
        items = _AGENT_TODOS
        if todo_id:
            items = [t for t in items if t["id"] == todo_id]
        if status_filter:
            items = [t for t in items if t["status"] == status_filter]
        if not items:
            return ToolResult(success=True, content="No todos found.")
        lines = [f"{'[x]' if t['status'] == 'done' else '[ ]'} {t['id']}: {t['content']}" for t in items]
        return ToolResult(success=True, content="\n".join(lines))


@_summarize("todo_write")
def _todo_write_summary(args: dict) -> str:
    return f"todo_write({args.get('id', '?')})"


@_summarize("todo_read")
def _todo_read_summary(args: dict) -> str:
    return f"todo_read({args.get('id', args.get('status', 'all'))})"


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
) -> str:
    """Spawn a single sub-agent thread. Returns the task_id."""
    from tools import _TOOL_CONTEXT
    from sub_agent import run_sub_agent

    task_id = str(uuid.uuid4())[:8]
    if cancel_event is None:
        cancel_event = threading.Event()

    # Generate a short human-readable name from the task text
    words = task.strip().split()
    short_name = "_".join(w for w in words[:3] if w.isalnum() or w in ("-",))
    if not short_name:
        short_name = "agent"
    short_name = short_name.lower()[:24]

    def _runner() -> None:
        import sys as _sys
        tui_queue = getattr(_TOOL_CONTEXT, "_tui_queue", None)
        # ---- Redirect stderr to log file to prevent TUI corruption ----
        # Any print(..., file=sys.stderr) from sub-agents or the tools they
        # invoke will break the prompt_toolkit alternate-screen layout.
        # Capture stderr to a per-task log file instead.
        import os as _os
        _os.makedirs("logs", exist_ok=True)
        _stderr_log_path = f"logs/sub_agent_{task_id}_stderr.log"
        _stderr_log = open(_stderr_log_path, "a", encoding="utf-8")
        _saved_stderr = _sys.stderr
        _sys.stderr = _stderr_log
        # ---- end stderr redirect ----
        # Set depth context for tools called by this sub-agent
        current_depth = parent_depth + 1
        _TOOL_CONTEXT._agent_depth = current_depth
        _TOOL_CONTEXT._agent_max_depth = max_depth
        _TOOL_CONTEXT._agent_task_id = task_id
        original_stream = config.stream
        try:
            if visible:
                config.stream = True
                if tui_queue is not None:
                    tui_queue.put(("sub_token", task_id, f"[sub {task_id}] START: {task[:80]}\n"))
                else:
                    # Log to file instead of stderr to avoid breaking TUI layout
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
                tui_queue=tui_queue,
                tui_task_id=task_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
            )
            runtime.store_result(task_id, result)
            # Signal TUI to hide sub-agent streaming pane
            if tui_queue is not None:
                tui_queue.put(("sub_done", task_id))
                status = "completed" if result.success else "error"
                tui_queue.put(("sub_tree", "status", task_id, status))
        finally:
            config.stream = original_stream
            # ---- Restore stderr ----
            _sys.stderr = _saved_stderr
            _stderr_log.close()
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
    # Push tree spawn message via the context queue
    from tools import _TOOL_CONTEXT
    tui_queue = getattr(_TOOL_CONTEXT, "_tui_queue", None)
    if tui_queue is not None:
        desc = task[:60].replace("\n", " ")
        tui_queue.put(("sub_tree", "spawn", task_id, parent_id, short_name, desc))
    thread.start()
    return task_id


@_register("spawn_agent")
def _spawn_agent(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:
    """Spawn a sub-agent to work on a task in the background.

    Returns a task_id immediately.  Use agent_status to poll or
    collect_agent to block until completion.

    Supports batch spawn via 'tasks' (list of task strings) in addition
    to single 'task' spawn.
    """
    from tools import _TOOL_CONTEXT
    from agent_runtime import AgentRuntime

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
    task_id = _spawn_one(task, config, runtime, wg, rg, max_turns,
                         cancel_event=None, visible=visible,
                         shared_context=shared_context,
                         subscriptions=subscriptions,
                         parent_depth=getattr(_TOOL_CONTEXT, "_agent_depth", 0),
                         max_depth=getattr(_TOOL_CONTEXT, "_agent_max_depth", 3),
                         parent_task_id=parent_task_id)

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
    from agent_runtime import AgentRuntime

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
    from agent_runtime import AgentRuntime

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

_COLLECT_ANY_TIMEOUT = 10   # seconds to wait for any sub-agent (kept short so
                            # parent agent can check for user interjections between
                            # polls — the parent's natural turn cycle handles this)


@_register("collect_any")
def _collect_any(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Collect the first sub-agent that finishes.

    If any have already completed, returns immediately.
    Otherwise polls until one completes or timeout.
    """
    from tools import _TOOL_CONTEXT
    from agent_runtime import AgentRuntime

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
# agent_message
# ---------------------------------------------------------------------------

@_register("agent_message")
def _agent_message(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Broadcast a message visible to parent and sibling sub-agents."""
    from tools import _TOOL_CONTEXT

    text = args.get("text", "")
    if not text:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'text'.",
        )
    sender = args.get("from", "")

    # Create typed AgentMessage for routing
    try:
        msg = AgentMessage(
            type="text",
            sender=sender,
            payload={"body": text},
        )
    except ValueError as exc:
        return ToolResult(
            success=False,
            content=f"Invalid message: {exc}",
        )

    # Append to legacy flat list (backward compat)
    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.append(msg.to_legacy_dict())
        if len(_AGENT_MSGS) > _AGENT_MSGS_MAX:
            _AGENT_MSGS[:] = _AGENT_MSGS[-_AGENT_MSGS_MAX:]
        count = len(_AGENT_MSGS)

    # Route to subscribed inboxes
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        _route_message(
            msg,
            runtime.inboxes,
            runtime.subscriptions,
            runtime._lock,
            target=None,
        )

    return ToolResult(
        success=True,
        content=f"Message broadcast. ({count} total messages)",
    )


@_summarize("agent_message")
def _agent_message_summary(args: dict) -> str:
    text = args.get("text", "?")
    preview = text[:50]
    if len(text) > 50:
        preview += "\u2026"
    return f"agent_message(\"{preview}\")"


# ---------------------------------------------------------------------------
# agent_read
# ---------------------------------------------------------------------------

@_register("agent_read")
def _agent_read(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read broadcast messages from other sub-agents and the parent.

    Returns messages in chronological order.  Use 'since' to only
    get messages with index >= that value (for polling).
    """
    since = args.get("since", None)
    if since is not None:
        try:
            since = int(since)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                content="'since' must be an integer index.",
            )

    with _AGENT_MSGS_LOCK:
        if since is not None:
            msgs = _AGENT_MSGS[since:]
        else:
            msgs = list(_AGENT_MSGS)

    if not msgs:
        return ToolResult(
            success=True,
            content="No new messages.",
        )

    lines = []
    base_idx = since if since is not None else 0
    for i, m in enumerate(msgs):
        idx = base_idx + i
        sender = f" from={m['from']}" if m.get("from") else ""
        lines.append(f"[{idx}]{sender} {m['text']}")

    return ToolResult(
        success=True,
        content="\n".join(lines),
    )


@_summarize("agent_read")
def _agent_read_summary(args: dict) -> str:
    since = args.get("since")
    if since is not None:
        return f"agent_read(since={since})"
    return "agent_read()"


# ---------------------------------------------------------------------------
# agent_handoff
# ---------------------------------------------------------------------------

@_register("agent_handoff")
def _agent_handoff(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Produce a typed result and route it to subscribed agents.

    Parameters:
        type: str              — message type (default \"handoff.result\")
        result: dict           — structured result payload
        correlation_id: str    — optional correlation ID
        target: str | None     — if set, deliver only to this task_id
    """
    from tools import _TOOL_CONTEXT

    msg_type = args.get("type", "handoff.result")
    if msg_type not in MSG_TYPE_REGISTRY:
        return ToolResult(
            success=False,
            content=f"Unknown handoff message type: {msg_type!r}. "
                    f"Use a registered type like 'handoff.result', 'handoff.ack', etc.",
        )

    result_payload = args.get("result", None)
    if result_payload is None:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'result'.",
        )
    if not isinstance(result_payload, dict):
        return ToolResult(
            success=False,
            content="'result' must be a dict.",
        )

    correlation_id = args.get("correlation_id", None)
    target = args.get("target", None)
    sender = args.get("from", "")

    # Build the payload according to type
    payload = {}
    if msg_type == "handoff.result":
        payload = {"result": result_payload, "task": str(result_payload)}
    elif msg_type == "handoff.request":
        payload = {"task": str(result_payload), "input_schema": result_payload}
    elif msg_type == "handoff.ack":
        payload = {"accepted": bool(result_payload.get("accepted", True)),
                    "reason": str(result_payload.get("reason", ""))}
    elif msg_type == "status.heartbeat":
        payload = {"progress": str(result_payload.get("progress", "")),
                    "pct": float(result_payload.get("pct", 0))}
    elif msg_type == "status.error":
        payload = {"error": str(result_payload.get("error", "")),
                    "phase": str(result_payload.get("phase", ""))}
    elif msg_type == "coord.fan_out":
        payload = {"items": result_payload, "worker_type": str(result_payload.get("worker_type", ""))}
    elif msg_type == "coord.fan_in":
        payload = {"results": result_payload, "worker_count": int(result_payload.get("worker_count", 0))}
    elif msg_type == "coord.sync":
        payload = {"barrier": str(result_payload.get("barrier", "")),
                    "arrived": int(result_payload.get("arrived", 1)),
                    "total": int(result_payload.get("total", 1))}

    try:
        msg = AgentMessage(
            type=msg_type,
            sender=sender,
            payload=payload,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        return ToolResult(
            success=False,
            content=f"Invalid handoff message: {exc}",
        )

    # Append to legacy flat list (backward compat)
    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.append(msg.to_legacy_dict())
        if len(_AGENT_MSGS) > _AGENT_MSGS_MAX:
            _AGENT_MSGS[:] = _AGENT_MSGS[-_AGENT_MSGS_MAX:]
        count = len(_AGENT_MSGS)

    # Route to subscribed inboxes
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        _route_message(
            msg,
            runtime.inboxes,
            runtime.subscriptions,
            runtime._lock,
            target=target,
        )
        # Also append to runtime.messages for orchestrator visibility
        runtime.messages.append(msg.to_legacy_dict())

    target_info = f" to '{target}'" if target else ""
    return ToolResult(
        success=True,
        content=f"Handoff {msg_type!r} sent{target_info}. ({count} total messages)",
    )


@_summarize("agent_handoff")
def _agent_handoff_summary(args: dict) -> str:
    msg_type = args.get("type", "handoff.result")
    return f"agent_handoff(type=\"{msg_type}\")"


# ---------------------------------------------------------------------------
# agent_inbox
# ---------------------------------------------------------------------------

@_register("agent_inbox")
def _agent_inbox(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read the typed inbox for a specific agent (task_id).

    Use 'since' to only get messages with index >= that value (for polling).
    """
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        # Default to the caller's own task_id (supports the parent orchestrator
        # checking its own inbox without needing to know its ID).
        task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")
        if not task_id:
            return ToolResult(
                success=False,
                content="Missing required parameter: 'task_id'.",
            )

    since = args.get("since", None)
    if since is not None:
        try:
            since = int(since)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                content="'since' must be an integer index.",
            )

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    inbox = runtime.get_inbox(task_id)
    if inbox is None:
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )
    if since is not None:
        inbox = inbox[since:]

    if not inbox:
        return ToolResult(
            success=True,
            content="No new messages in inbox.",
        )

    lines = []
    for i, msg in enumerate(inbox):
        idx = (since if since is not None else 0) + i
        lines.append(f"[{idx}] [{msg.type}] from={msg.sender}: {msg.payload}")

    return ToolResult(
        success=True,
        content="\n".join(lines),
    )


@_summarize("agent_inbox")
def _agent_inbox_summary(args: dict) -> str:
    since = args.get("since")
    if since is not None:
        return f"agent_inbox({args.get('task_id', '?')}, since={since})"
    return f"agent_inbox({args.get('task_id', '?')})"


# ---------------------------------------------------------------------------
# agent_subscribe
# ---------------------------------------------------------------------------

@_register("agent_subscribe")
def _agent_subscribe(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Declare or update message type subscriptions for an agent at runtime.

    Parameters:
        task_id: str       — the agent to update
        types: list[str]   — message types to subscribe to (empty = all)
    """
    from tools import _TOOL_CONTEXT

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task_id'.",
        )

    types = args.get("types", None)
    if types is not None and not isinstance(types, list):
        return ToolResult(
            success=False,
            content="'types' must be a list of message type strings.",
        )

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    if runtime.get_status(task_id) == "not_found":
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )

    if types is None:
        # Default: subscribe to all (clear subscriptions)
        runtime.set_subscriptions(task_id, [])
        return ToolResult(
            success=True,
            content=f"Sub-agent '{task_id}' now receives all message types (default).",
        )

    # Validate types
    unknown = [t for t in types if t not in MSG_TYPE_REGISTRY]
    if unknown:
        return ToolResult(
            success=False,
            content=f"Unknown message type(s): {unknown}. "
                    f"Known types: {sorted(MSG_TYPE_REGISTRY.keys())}",
        )

    runtime.set_subscriptions(task_id, types)
    return ToolResult(
        success=True,
        content=f"Sub-agent '{task_id}' subscribed to: {types}",
    )


@_summarize("agent_subscribe")
def _agent_subscribe_summary(args: dict) -> str:
    types = args.get("types", [])
    return f"agent_subscribe({args.get('task_id', '?')}, types={types})"


# ---------------------------------------------------------------------------
# agent_extend
# ---------------------------------------------------------------------------

@_register("agent_extend")
def _agent_extend(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Extend the turn budget of a running sub-agent."""
    from tools import _TOOL_CONTEXT
    from agent_runtime import AgentRuntime

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
    from agent_runtime import AgentRuntime

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
# write_scratchpad
# ---------------------------------------------------------------------------

@_register("write_scratchpad")
def _write_scratchpad(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Write content to the agent's persistent working scratchpad."""
    content_text = args["content"]

    # Use the shared MemoryStore connection to avoid SQLite "database is locked"
    # errors caused by opening a second connection to the same WAL-mode file.
    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)

    # If no shared store but scratchpad_path is configured, create one on the fly.
    if memory_store is None:
        scratchpad_path = getattr(_TOOL_CONTEXT, "scratchpad_path", None)
        if scratchpad_path:
            from memory import MemoryStore
            memory_store = MemoryStore(scratchpad_path)

    if memory_store is not None:
        try:
            memory_store.set_scratchpad(content_text)
            _TOOL_CONTEXT._scratchpad_updated = True
            return ToolResult(
                success=True,
                content=f"Scratchpad updated ({len(content_text)} chars).",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Failed to update scratchpad: {e}",
            )

    # Fallback: file-based scratchpad (no MemoryStore available)
    import os as _os
    fallback = _os.path.join(
        _TOOL_CONTEXT.workspace or ".", ".mini_agent_scratchpad.md"
    )
    sr = _wg.check(fallback)
    if not sr.allowed:
        return ToolResult(success=False, content=f"Scratchpad blocked: {sr.reason}")
    try:
        with open(fallback, "w", encoding="utf-8") as f:
            f.write(content_text)
        return ToolResult(
            success=True,
            content=f"Scratchpad updated ({len(content_text)} chars).",
        )
    except Exception as e:
        return ToolResult(
            success=False,
            content=f"Failed to update scratchpad: {e}",
        )


@_summarize("write_scratchpad")
def _write_scratchpad_summary(args: dict) -> str:
    content = args.get("content", "")
    preview = content[:60].replace("\n", " ")
    if len(content) > 60:
        preview += "…"
    return f"write_scratchpad(…{len(content)} chars → \"{preview}\")"


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
# plan / plan_status tools — structured task tracking
# ---------------------------------------------------------------------------

@_register("plan")
def _plan(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Declare a structured task plan."""
    steps = args["steps"]
    if not isinstance(steps, list) or not steps:
        return ToolResult(
            success=False,
            content="Plan must have at least one step.",
            hint="Provide a non-empty array of step descriptions.",
        )
    _TOOL_CONTEXT._plan_steps = steps
    _TOOL_CONTEXT._plan_done = set()
    lines = [f"Plan ({len(steps)} steps):"]
    for i, step in enumerate(steps, 1):
        lines.append(f"  [{i}] {step}")
    return ToolResult(success=True, content="\n".join(lines))


@_summarize("plan")
def _plan_summary(args: dict) -> str:
    steps = args.get("steps", [])
    return f"plan({len(steps)} steps: {steps[0][:40] if steps else '?'}…)"


@_register("plan_status")
def _plan_status(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Mark a step complete or report status."""
    step = args.get("step")
    steps = _TOOL_CONTEXT._plan_steps
    done = _TOOL_CONTEXT._plan_done

    if not steps:
        return ToolResult(success=True, content="No active plan.")

    if step is not None:
        idx = step - 1  # 1-indexed → 0-indexed
        if idx < 0 or idx >= len(steps):
            return ToolResult(
                success=False,
                content=f"Invalid step {step}. Plan has {len(steps)} steps.",
                hint=f"Step must be between 1 and {len(steps)}.",
            )
        done.add(idx)
        _TOOL_CONTEXT._plan_done = done

    lines = [f"Plan ({len(done)}/{len(steps)} complete):"]
    for i, s in enumerate(steps, 1):
        mark = "✓" if (i - 1) in done else "○"
        lines.append(f"  [{mark}] {i}. {s}")
    all_done = len(done) == len(steps)
    if all_done:
        lines.append("  All steps complete!")
    return ToolResult(success=True, content="\n".join(lines))


@_summarize("plan_status")
def _plan_status_summary(args: dict) -> str:
    step = args.get("step")
    if step is not None:
        return f"plan_status(complete step {step})"
    return "plan_status()"


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
        done_count = len(plan_done & set(range(1, len(plan) + 1)))
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
    Returns a summary of what was stored.
    """
    topic = args.get("topic", "")
    detail = args.get("detail", "")
    if not topic.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'topic' (short topic label for this learning).",
        )

    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    topic_preview = topic[:200] + ("..." if len(topic) > 200 else "")
    detail_preview = detail[:200] + ("..." if len(detail) > 200 else "")
    if memory_store is not None:
        try:
            conn = memory_store._get_conn()
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",
                (topic, topic, detail),
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
                f"Stored in project knowledge:\\n"
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
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",
                (topic, topic, detail),
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
                f"Stored in project knowledge (DB fallback):\\n"
                f"  Topic: {topic_preview}\\n"
                f"  Detail: {detail_preview}"
            ),
        )

    return ToolResult(
        success=True,
        content=(
            f"Remember noted (no persistent storage available):\\n"
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
    import time
    from tools import _TOOL_CONTEXT
    from agent_runtime import AgentRuntime

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
