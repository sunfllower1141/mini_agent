#!/usr/bin/env python3
"""idempotency.py -- idempotency key system for write tools in mini_agent.

2026 best practice: "Idempotency keys required for every write tool."

Every destructive tool call (write_file, edit_file, run_shell with side effects)
is assigned an idempotency key derived from the logical operation.  If the same
operation is retried (e.g. after a timeout), the stored result is returned
instead of re-executing.

Architecture
------------
- ``_IDEMPOTENCY_STORE``: in-memory dict of key -> (expiry_ts, ToolResult)
- ``idempotency_key_for(tool_name, args)``: derive a stable key from args
- ``check_idempotent(tool_name, args)``: return cached result or None
- ``store_idempotent(tool_name, args, result)``: cache a result
- ``clear_idempotent()``: flush at session start

Keys are derived from:
  - write_file:   sha256(path + content[:256])
  - edit_file:    sha256(path + old_string + new_string)
  - run_shell:    sha256(command + cwd)  (only for destructive commands)

TTL: 300 seconds (5 min) -- long enough for retry, short enough to prevent
stale-result poisoning.  Capped at 64 entries.

Thread-safe via a reentrant lock.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.result import ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IDEMPOTENCY_TTL: float = 300.0     # 5 minutes
_IDEMPOTENCY_MAX_ENTRIES: int = 64  # cap to prevent unbounded growth

# Write tools that support idempotency
_IDEMPOTENT_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "edit_lines",
    "run_shell",
})

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_IDEMPOTENCY_STORE: dict[str, tuple[float, "ToolResult"]] = {}
_IDEMPOTENCY_LOCK = threading.RLock()


def idempotency_key_for(tool_name: str, args: dict) -> str:
    """Derive a stable idempotency key from tool name and args.

    The key captures the logical operation identity, not the exact byte
    representation.  For example, ``write_file`` keys on path + content
    so two writes to the same path with the same content produce the
    same key.
    """
    if tool_name == "write_file":
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))[:256]
        material = f"wf:{path}:{content}"
    elif tool_name == "edit_file":
        path = str(args.get("path", ""))
        old = str(args.get("old_string", ""))[:256]
        new = str(args.get("new_string", ""))[:256]
        material = f"ef:{path}:{old}:{new}"
    elif tool_name == "edit_lines":
        path = str(args.get("path", ""))
        # edit_lines uses an "edits" array; hash the whole thing
        edits = repr(args.get("edits", []))[:512]
        material = f"el:{path}:{edits}"
    elif tool_name == "run_shell":
        command = str(args.get("command", ""))
        cwd = str(args.get("cwd", ""))
        material = f"sh:{command}:{cwd}"
    elif tool_name == "restore_file":
        path = str(args.get("path", ""))
        material = f"rf:{path}"
    else:
        # Generic fallback: hash all sorted args
        material = f"{tool_name}:" + str(sorted(args.items()))[:256]

    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]


def check_idempotent(tool_name: str, args: dict) -> "ToolResult | None":
    """Return a cached result if this exact operation was already performed.

    Returns None if no cached result exists or it expired.
    """
    if tool_name not in _IDEMPOTENT_TOOLS:
        return None

    key = idempotency_key_for(tool_name, args)
    with _IDEMPOTENCY_LOCK:
        entry = _IDEMPOTENCY_STORE.get(key)
        if entry is None:
            return None
        expiry, result = entry
        if time.monotonic() > expiry:
            del _IDEMPOTENCY_STORE[key]
            return None
        return result


def store_idempotent(tool_name: str, args: dict, result: "ToolResult") -> None:
    """Cache a tool result keyed by the idempotency key of its operation."""
    if tool_name not in _IDEMPOTENT_TOOLS:
        return

    key = idempotency_key_for(tool_name, args)
    expiry = time.monotonic() + _IDEMPOTENCY_TTL

    # Annotate the result with its idempotency key so the LLM can see it
    result.idempotency_key = key

    with _IDEMPOTENCY_LOCK:
        # Evict oldest entry if at capacity
        if len(_IDEMPOTENCY_STORE) >= _IDEMPOTENCY_MAX_ENTRIES:
            oldest_key = min(_IDEMPOTENCY_STORE, key=lambda k: _IDEMPOTENCY_STORE[k][0])
            del _IDEMPOTENCY_STORE[oldest_key]

        _IDEMPOTENCY_STORE[key] = (expiry, result)


def clear_idempotent() -> None:
    """Flush all idempotency entries (called at session start)."""
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY_STORE.clear()
