#!/usr/bin/env python3
"""interject.py -- thread-safe user interjection queue for mini_agent.

Allows the user to type messages while the agent is working.  Messages are
queued and injected into the conversation at the next tool-call boundary
by ``run_agent_turn()``.
"""
from __future__ import annotations

import threading
from collections import deque

_INTERJECTIONS: deque[str] = deque()
_LOCK = threading.Lock()


def push_interjection(text: str) -> None:
    """Push a user message onto the interjection queue.  Thread-safe."""
    with _LOCK:
        _INTERJECTIONS.append(text)


def poll_interjections() -> list[str]:
    """Return all pending interjections and clear the queue.  Thread-safe."""
    with _LOCK:
        if not _INTERJECTIONS:
            return []
        items = list(_INTERJECTIONS)
        _INTERJECTIONS.clear()
        return items


def has_interjections() -> bool:
    """Return True if there are pending interjections.  Thread-safe."""
    with _LOCK:
        return len(_INTERJECTIONS) > 0
