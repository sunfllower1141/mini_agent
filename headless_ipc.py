#!/usr/bin/env python3
"""
headless_ipc.py â JSON-line IPC layer for the Ink CLI frontend.

This module replaces ``tui.py``'s in-process Textual integration with a
language-agnostic newline-delimited JSON protocol over stdin/stdout.  The
Ink CLI (Node) spawns ``mini_agent_headless.py`` as a child process; the two
sides exchange events through:

    Python â UI:   one JSON object per stdout line (events)
    UI â Python:   one JSON object per stdin line  (commands)

The event schema reuses the existing ``ws_server.py`` types verbatim so the
Electron UI keeps working unchanged. The only difference is transport.

Design notes
------------
* ``StdoutEmitter`` is **thread-safe** â the agent runs in a worker thread
  and emits tokens at hundreds/sec; we need a lock so two threads can't
  interleave one JSON line.
* ``StdinReader`` runs on its own daemon thread, pushing parsed messages
  into a ``queue.Queue`` for the main loop to drain non-blockingly.
* All emit/read functions are pure (no globals) so they're easy to unit-test.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any, Callable


# ---------------------------------------------------------------------------
# Event types â keep in sync with ws_server.py and ui/src/ipc.ts
# ---------------------------------------------------------------------------

# Server â UI
EVT_READY          = "ready"
EVT_STREAM_TOKEN   = "stream.token"
EVT_STREAM_THINK   = "stream.thinking"
EVT_TOOL_START     = "tool.start"
EVT_TOOL_END       = "tool.end"
EVT_TOOL_OUTPUT    = "tool.output"
EVT_SUBAGENT_SPAWN = "subagent.spawn"
EVT_SUBAGENT_TOKEN = "subagent.token"
EVT_SUBAGENT_DONE  = "subagent.done"
EVT_TURN_DONE      = "turn.done"
EVT_APPROVE_REQ    = "approve.request"
EVT_ERROR          = "error"
EVT_STATUS         = "status"
EVT_LOG            = "log"

# UI â Server
CMD_USER_MESSAGE   = "user.message"
CMD_USER_CANCEL    = "user.cancel"
CMD_USER_APPROVE   = "user.approve"
CMD_USER_COMMAND   = "user.command"   # slash commands: /theme, /stats, etc.
CMD_USER_QUIT      = "user.quit"


# ---------------------------------------------------------------------------
# StdoutEmitter â thread-safe newline-delimited JSON emitter
# ---------------------------------------------------------------------------

class StdoutEmitter:
    """Emit JSON events to a stream, one per line, with a write lock.

    All public methods are thread-safe.  The default stream is sys.stdout
    but tests pass an in-memory io.StringIO.

    Each line is a single JSON object with these keys:
        type:  event type string (see EVT_* constants)
        data:  event-specific dict payload
        ts:    server timestamp (seconds since epoch, float)
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Write a single JSON line.  Safe to call from any thread."""
        if self._closed:
            return
        payload = {
            "type": event_type,
            "data": data or {},
            "ts": time.time(),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self._closed:
                return
            try:
                self._stream.write(line)
                self._stream.write("\n")
                self._stream.flush()
            except (BrokenPipeError, ValueError):
                # UI closed stdin or stream is gone â stop emitting silently.
                self._closed = True

    def close(self) -> None:
        with self._lock:
            self._closed = True


# ---------------------------------------------------------------------------
# StdinReader â background thread reading JSON commands from a stream
# ---------------------------------------------------------------------------

@dataclass
class Command:
    """A parsed UI â Python command."""
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class StdinReader(threading.Thread):
    """Read JSON-line commands from a stream into a queue.

    On EOF or stream error, pushes ``None`` to signal shutdown.  Malformed
    JSON lines are reported via ``on_error`` (if given) and skipped.
    """

    def __init__(
        self,
        stream: IO[str] | None = None,
        out: queue.Queue | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(daemon=True, name="ipc-stdin-reader")
        self._stream = stream if stream is not None else sys.stdin
        self.queue: queue.Queue = out if out is not None else queue.Queue()
        self._on_error = on_error
        # NB: do not name this ``_stop`` ÃÂ¢ that shadows
        # ``threading.Thread._stop()`` and breaks thread teardown.
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                line = self._stream.readline()
            except (ValueError, OSError):
                self.queue.put(None)
                return
            if not line:                # EOF
                self.queue.put(None)
                return
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                if self._on_error is not None:
                    self._on_error(f"bad JSON from UI: {exc}: {line[:120]}")
                continue
            if not isinstance(obj, dict):
                if self._on_error is not None:
                    self._on_error(f"non-object message from UI: {line[:120]}")
                continue
            cmd_type = obj.get("type", "")
            cmd_data = obj.get("data", {}) or {}
            if not isinstance(cmd_data, dict):
                cmd_data = {}
            self.queue.put(Command(type=cmd_type, data=cmd_data))

    def stop(self) -> None:
        """Signal the reader to exit at next loop iteration."""
        self._stop_evt.set()


# ---------------------------------------------------------------------------
# Convenience helpers for the agent code path
# ---------------------------------------------------------------------------

def make_callbacks(emitter: StdoutEmitter, agent_id: str = "orchestrator") -> dict:
    """Build the ``on_token``/``on_tool_*`` callbacks that ``run_agent_turn``
    expects.  Returns a dict you can ``**``-splat into the call.

    The callbacks emit IPC events but do not maintain any state â the UI is
    responsible for assembling tokens into messages and grouping tool
    start/end pairs by sequence.

    Each ``on_tool_start`` invocation increments an internal counter that's
    embedded in the event as ``seq``; ``on_tool_end`` uses the same counter
    so the UI can pair the events without ambiguity.
    """
    from stream import THINKING_START, THINKING_END

    state = {"tool_seq": 0, "active_seq": None, "in_thinking": False}
    lock = threading.Lock()
    think_lock = threading.Lock()

    def on_token(tok: str) -> None:
        # Detect thinking-block boundaries injected by stream.py
        if tok == THINKING_START:
            with think_lock:
                state["in_thinking"] = True
            return  # don't emit the marker itself
        if tok == THINKING_END:
            with think_lock:
                state["in_thinking"] = False
            return  # don't emit the marker itself
        with think_lock:
            in_think = state["in_thinking"]
        if in_think:
            emitter.emit(EVT_STREAM_THINK, {"token": tok, "agent_id": agent_id})
        else:
            emitter.emit(EVT_STREAM_TOKEN, {"token": tok, "agent_id": agent_id})

    def on_tool_start(summary: str, parallel: bool = False) -> None:
        with lock:
            state["tool_seq"] += 1
            seq = state["tool_seq"]
            state["active_seq"] = seq
        emitter.emit(EVT_TOOL_START, {
            "seq": seq,
            "summary": summary,
            "parallel": parallel,
            "agent_id": agent_id,
        })

    def on_tool_end(ok: bool, detail: str, diff_preview: str | None = None) -> None:
        with lock:
            seq = state["active_seq"] or state["tool_seq"]
            state["active_seq"] = None
        emitter.emit(EVT_TOOL_END, {
            "seq": seq,
            "ok": bool(ok),
            "detail": detail,
            "diff_preview": diff_preview,
            "agent_id": agent_id,
        })

    def on_tool_output(line: str) -> None:
        with lock:
            seq = state["active_seq"] or state["tool_seq"]
        emitter.emit(EVT_TOOL_OUTPUT, {
            "seq": seq,
            "line": line,
            "agent_id": agent_id,
        })

    return {
        "on_token": on_token,
        "on_tool_start": on_tool_start,
        "on_tool_end": on_tool_end,
        "on_tool_output": on_tool_output,
    }


__all__ = [
    # event types
    "EVT_READY", "EVT_STREAM_TOKEN", "EVT_STREAM_THINK",
    "EVT_TOOL_START", "EVT_TOOL_END", "EVT_TOOL_OUTPUT",
    "EVT_SUBAGENT_SPAWN", "EVT_SUBAGENT_TOKEN", "EVT_SUBAGENT_DONE",
    "EVT_TURN_DONE", "EVT_APPROVE_REQ", "EVT_ERROR", "EVT_STATUS", "EVT_LOG",
    # command types
    "CMD_USER_MESSAGE", "CMD_USER_CANCEL", "CMD_USER_APPROVE",
    "CMD_USER_COMMAND", "CMD_USER_QUIT",
    # classes / helpers
    "StdoutEmitter", "StdinReader", "Command", "make_callbacks",
]
