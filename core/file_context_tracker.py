#!/usr/bin/env python3
"""
file_context_tracker.py -- DEPRECATED. File change tracking has been removed.
Stub kept to avoid import errors from stale references.
"""

from __future__ import annotations

from typing import Optional


class FileContextTracker:
    """No-op stub. File change tracking has been removed."""

    def __init__(self, task_id: str = "") -> None:
        pass

    def mark_file_read(self, path: str) -> None:
        pass

    def mark_file_edited(self, path: str) -> None:
        pass

    def is_stale(self, path: str) -> bool:
        return False

    def get_stale_warning(self, path: str) -> Optional[str]:
        return None

    def clear_state(self, path: str) -> None:
        pass

    def clear_all(self) -> None:
        pass


def get_tracker(task_id: str = "") -> FileContextTracker:
    """No-op stub."""
    return FileContextTracker(task_id)


def remove_tracker(task_id: str = "") -> None:
    """No-op stub."""
    pass
