#!/usr/bin/env python3
"""session.py -- session management for mini_agent.

Session DBs are SQLite databases stored as .mini_agent_memory_session_<name>.db
in the workspace root.  The default session uses .mini_agent_memory.db directly.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.config import MEMORY_FILENAME
from core.prompt import build_startup_context, build_session_header

if TYPE_CHECKING:
    from core.config import AgentConfig
    from memory.memory import MemoryStore


def _session_db_path(workspace: str, session_name: str | None = None) -> str:
    """Return the memory DB path for a given session name."""
    if session_name:
        base = MEMORY_FILENAME.replace(".db", "")
        return os.path.join(workspace, f"{base}_session_{session_name}.db")
    return os.path.join(workspace, MEMORY_FILENAME)


def list_sessions(workspace: str) -> list[str]:
    """Return list of available session names in the workspace."""
    sessions: list[str] = []
    prefix = MEMORY_FILENAME.replace(".db", "_session_")
    for fname in os.listdir(workspace):
        if fname.startswith(prefix) and fname.endswith(".db"):
            name = fname[len(prefix):-len(".db")]
            sessions.append(name)
    # Also check if default session DB exists
    default_path = os.path.join(workspace, MEMORY_FILENAME)
    if os.path.isfile(default_path) and "default" not in sessions:
        sessions.insert(0, "default")
    return sessions


def switch_session(
    workspace: str,
    session_name: str,
    current_memory: "MemoryStore | None",
    current_config: "AgentConfig",
) -> dict:
    """Save current session and load a new one. Returns new session dict."""
    from memory.memory import MemoryStore
    from core.prompt import build_system_prompt

    # Save current session
    if current_memory is not None:
        current_memory.close()

    db_path = _session_db_path(workspace, session_name)
    memory = MemoryStore(db_path, max_messages=current_config.max_messages,
                         max_tokens=current_config.context_window)
    saved = memory.load()
    if saved:
        from memory.memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned
        saved, _ = _compress_tool_results(saved, keep_recent=20)
        saved, pruned = _prune_by_tokens(saved, current_config.context_window, current_config.max_messages)
        if pruned:
            summary = _summarize_pruned(pruned)
            if summary:
                saved.insert(0, {"role": "user", "content": summary})

    knowledge = memory.get_top_knowledge(limit=15) if not memory._skip_load else []
    startup_ctx = build_startup_context(workspace, knowledge=knowledge)
    session_header = build_session_header(current_config)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(current_config)},
        {"role": "user", "content": session_header},
        {"role": "user", "content": startup_ctx},
    ]
    if saved:
        messages.extend(saved)

    return {"memory": memory, "messages": messages}


def delete_session(workspace: str, session_name: str) -> tuple[bool, str]:
    """Delete a session's memory DB. Returns (ok, message)."""
    if session_name == "default":
        return False, "Cannot delete the default session."
    db_path = _session_db_path(workspace, session_name)
    if not os.path.isfile(db_path):
        return False, f"Session '{session_name}' not found."
    os.remove(db_path)
    return True, f"Deleted session '{session_name}'."
