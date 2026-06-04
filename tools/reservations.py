#!/usr/bin/env python3
"""
reservations.py — file reservation system to prevent sub-agent write collisions.

Extracted from tools/__init__.py to keep the dispatch module focused.
"""

from __future__ import annotations

import threading

# File reservation system — prevents sub-agent write collisions
# Maps file_path (relative to workspace) → task_id of owning agent
_FILE_RESERVATIONS: dict[str, str] = {}
_FILE_RESERVATIONS_LOCK = threading.Lock()


def reserve_file(path: str, task_id: str) -> tuple[bool, str]:
    """Try to reserve a file for writing. Returns (ok, message).

    Fails if the file is already reserved by another agent.
    Call this before write_file/edit_file to prevent collisions.
    """
    with _FILE_RESERVATIONS_LOCK:
        existing = _FILE_RESERVATIONS.get(path)
        if existing is not None and existing != task_id:
            return False, f"File '{path}' is reserved by agent '{existing[:8]}'"
        _FILE_RESERVATIONS[path] = task_id
    return True, ""


def release_file(path: str, task_id: str) -> None:
    """Release a file reservation. No-op if not reserved by this agent."""
    with _FILE_RESERVATIONS_LOCK:
        if _FILE_RESERVATIONS.get(path) == task_id:
            del _FILE_RESERVATIONS[path]


def release_all_files(task_id: str) -> None:
    """Release all file reservations held by an agent."""
    with _FILE_RESERVATIONS_LOCK:
        to_release = [p for p, t in _FILE_RESERVATIONS.items() if t == task_id]
        for path in to_release:
            del _FILE_RESERVATIONS[path]
