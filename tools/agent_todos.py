#!/usr/bin/env python3
"""
agent_todos.py — task tracking and scratchpad tools for mini_agent.

Tools: todo_write, todo_read, plan, plan_status, write_scratchpad

Extracted from agent_ops.py to keep that module focused on agent lifecycle.
"""

from __future__ import annotations

import threading

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT


# ---------------------------------------------------------------------------
# Todo tracking: in-memory list, survives across turns
# ---------------------------------------------------------------------------

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
            from memory.memory import MemoryStore
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
        preview += "\u2026"
    return f"write_scratchpad(\u2026{len(content)} chars \u2192 \"{preview}\")"


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
    return f"plan({len(steps)} steps: {steps[0][:40] if steps else '?'}\u2026)"


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
        mark = "\u2713" if (i - 1) in done else "\u25cb"
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
