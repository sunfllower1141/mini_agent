#!/usr/bin/env python3
"""
file_context_tracker.py -- Proactive file change detection for mini_agent.

Dirac-equivalent of FileContextTracker.ts.  Tracks which files the agent
has read or edited, and detects external modifications (e.g., the user
editing a file outside the agent) so the agent can be warned before
making stale edits.

Two mechanisms:
  1. Timestamp tracking: record read & edit times per file
  2. Polling check: stat the file before each edit to verify it hasn't
     changed since the last read

This prevents the class of bugs where an LLM edits a file based on
stale context, creating diff conflicts or incorrect merges.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Per-task singleton: task_id -> FileContextTracker
_TRACKERS: dict[str, "FileContextTracker"] = {}
_lock = threading.Lock()


class FileContextTracker:
    """Per-task file context tracker for a single agent session."""

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        # path -> mtime at read time
        self._read_mtimes: dict[str, float] = {}
        # path -> mtime after edit
        self._edit_mtimes: dict[str, float] = {}
        # paths that were read (for stale checking)
        self._read_paths: set[str] = set()
        # paths with pending stale warnings
        self._stale_warnings: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_file_read(self, path: str) -> None:
        """
        Record that a file was read by the agent.
        Called after every successful read_file operation.
        """
        resolved = os.path.realpath(path)
        try:
            mtime = os.path.getmtime(resolved)
            self._read_mtimes[resolved] = mtime
            self._read_paths.add(resolved)
            self._stale_warnings.discard(resolved)
        except OSError:
            pass

    def mark_file_edited(self, path: str) -> None:
        """
        Record that a file was edited by the agent.
        Called after every successful edit_file / write_file.
        """
        resolved = os.path.realpath(path)
        try:
            mtime = os.path.getmtime(resolved)
            self._read_mtimes[resolved] = mtime
            self._edit_mtimes[resolved] = mtime
            self._stale_warnings.discard(resolved)
        except OSError:
            pass

    def is_stale(self, path: str) -> bool:
        """
        Return True if the file has been modified externally since
        the agent last read (or edited) it.
        """
        resolved = os.path.realpath(path)
        if resolved not in self._read_paths:
            return False  # Never read, can't be stale
        try:
            current_mtime = os.path.getmtime(resolved)
            last_known = max(
                self._read_mtimes.get(resolved, 0),
                self._edit_mtimes.get(resolved, 0),
            )
            return current_mtime > last_known + 0.001  # Tolerance for FS rounding
        except OSError:
            return False

    def get_stale_warning(self, path: str) -> Optional[str]:
        """
        Return a warning message if the file appears to have been
        modified externally.  Returns None if the file is fresh.
        """
        if not self.is_stale(path):
            return None
        resolved = os.path.realpath(path)
        if resolved in self._stale_warnings:
            return None  # Already warned once
        self._stale_warnings.add(resolved)
        return (
            f"Warning: '{path}' may have been modified externally "
            f"since the agent last read it.  Consider re-reading the "
            f"file before editing to avoid stale context."
        )

    def clear_state(self, path: str) -> None:
        """Clear tracking for a specific path."""
        resolved = os.path.realpath(path)
        self._read_mtimes.pop(resolved, None)
        self._edit_mtimes.pop(resolved, None)
        self._read_paths.discard(resolved)
        self._stale_warnings.discard(resolved)

    def clear_all(self) -> None:
        """Clear all tracking state."""
        self._read_mtimes.clear()
        self._edit_mtimes.clear()
        self._read_paths.clear()
        self._stale_warnings.clear()


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------


def get_tracker(task_id: str = "") -> FileContextTracker:
    """Get or create the FileContextTracker for a task."""
    key = task_id or "__default__"
    with _lock:
        if key not in _TRACKERS:
            _TRACKERS[key] = FileContextTracker(task_id)
        return _TRACKERS[key]


def remove_tracker(task_id: str = "") -> None:
    """Remove a tracker when a task completes."""
    key = task_id or "__default__"
    with _lock:
        _TRACKERS.pop(key, None)
