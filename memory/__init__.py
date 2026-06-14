"""Persistent memory: SQLite store, pruning, session management.

This __init__.py is intentionally minimal to avoid circular imports.
Import specific submodules directly (e.g. ``from memory.memory import MemoryStore``).
"""

from __future__ import annotations


def __getattr__(name):
    """Lazy import to avoid circular dependencies at package-init time."""
    if name == "MemoryStore":
        from memory.memory import MemoryStore
        return MemoryStore
    if name == "_session_db_path":
        from memory.session import _session_db_path
        return _session_db_path
    if name == "list_sessions":
        from memory.session import list_sessions
        return list_sessions
    if name == "switch_session":
        from memory.session import switch_session
        return switch_session
    if name == "delete_session":
        from memory.session import delete_session
        return delete_session
    if name == "_total_tokens":
        from memory.memory_prune import _total_tokens
        return _total_tokens
    if name == "_prune_by_tokens":
        from memory.memory_prune import _prune_by_tokens
        return _prune_by_tokens
    if name == "_estimate_tokens":
        from memory.memory_prune import _estimate_tokens
        return _estimate_tokens
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
