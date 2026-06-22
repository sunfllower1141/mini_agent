#!/usr/bin/env python3
"""
env_tracker.py -- Environment context snapshot tracking.

Adopted from Dirac's src/core/context/context-tracking/EnvironmentContextTracker.ts.

Records OS, host, and version information at task start and on demand,
deduplicating when nothing changed.  Stored in task metadata for
checkpoint/restore, giving the agent awareness of its runtime context.

Also provides ``collect_environment_metadata()`` for a one-shot snapshot
usable in system prompts or startup context.
"""

from __future__ import annotations

import platform
import sys
import threading
import time
from typing import Any

from logging_setup import get_logger

_log = get_logger("env_tracker")


# ---------------------------------------------------------------------------
# Environment snapshot collection
# ---------------------------------------------------------------------------

def collect_environment_metadata() -> dict[str, str]:
    """Collect one-shot environment metadata usable in startup context.

    Returns a dict with:
        os_name, os_version, os_arch, host_name, host_version,
        agent_version, python_version, cwd
    """
    try:
        import os
        cwd = os.getcwd()
    except Exception:
        cwd = "unknown"

    return {
        "os_name": platform.system() or "unknown",
        "os_version": platform.release() or "unknown",
        "os_arch": platform.machine() or "unknown",
        "host_name": platform.node() or "unknown",
        "host_version": _get_agent_version(),
        "python_version": sys.version.split()[0] if hasattr(sys, "version") else "unknown",
        "cwd": cwd,
    }


def _get_agent_version() -> str:
    """Get mini_agent version from package metadata."""
    try:
        from importlib.metadata import version
        return version("mini_agent")
    except Exception:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return "unknown"


# ---------------------------------------------------------------------------
# Environment history tracking (per-session)
# ---------------------------------------------------------------------------

class EnvironmentHistoryTracker:
    """Tracks environment metadata snapshots across a session.

    Mirrors Dirac's EnvironmentContextTracker.ts:
    - Records snapshots at key moments (task start, before compaction)
    - Deduplicates: no-op when nothing changed since last snapshot
    - Persists to metadata store for checkpoint/restore
    """

    def __init__(self) -> None:
        self._history: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self) -> bool:
        """Take a snapshot and append if different from the last one.

        Returns True if a new snapshot was recorded.
        """
        current = collect_environment_metadata()
        current_with_ts = {"ts": time.time(), **current}

        with self._lock:
            if self._history:
                last = self._history[-1]
                if self._is_same_environment(last, current_with_ts):
                    return False  # no change, skip

            self._history.append(current_with_ts)
            _log.debug("Environment snapshot recorded: %s", current)
            return True

    def get_current(self) -> dict[str, str]:
        """Return the latest snapshot, or a fresh one if none recorded."""
        with self._lock:
            if self._history:
                return self._history[-1]
        return collect_environment_metadata()

    def get_history(self) -> list[dict[str, Any]]:
        """Return the full history of environment snapshots."""
        with self._lock:
            return list(self._history)

    def get_startup_context(self) -> str:
        """Return a formatted string for injection into the system/startup prompt."""
        env = self.get_current()
        return (
            f"OS: {env.get('os_name', '?')} {env.get('os_version', '?')} "
            f"({env.get('os_arch', '?')})\n"
            f"Host: {env.get('host_name', '?')}\n"
            f"Python: {env.get('python_version', '?')}\n"
            f"Agent: {env.get('host_version', '?')}\n"
            f"CWD: {env.get('cwd', '?')}"
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _is_same_environment(a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Compare key fields to check if the environment is unchanged."""
        keys = ("os_name", "os_version", "os_arch", "host_name", "host_version")
        return all(a.get(k) == b.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_env_tracker: EnvironmentHistoryTracker | None = None
_lock = threading.Lock()


def get_env_tracker() -> EnvironmentHistoryTracker:
    """Return the module-level EnvironmentHistoryTracker singleton."""
    global _env_tracker
    if _env_tracker is None:
        with _lock:
            if _env_tracker is None:
                _env_tracker = EnvironmentHistoryTracker()
    return _env_tracker


def reset_env_tracker() -> None:
    """Reset the module-level tracker (for testing)."""
    global _env_tracker
    with _lock:
        _env_tracker = None
