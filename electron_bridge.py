#!/usr/bin/env python3
"""JSON-RPC over stdio bridge for Electron/TUI frontends.

Reads JSON-RPC requests from stdin, dispatches to mini_agent's run_agent_turn(),
streams token-by-token output, and returns structured final results.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

from config import AgentConfig, init_session
from llm import run_agent_turn
from memory import MemoryStore
from safety import ReadSafetyGate, WriteSafetyGate

# ── module-level state (populated by 'init') ──────────────────────────
_config: AgentConfig | None = None
_write_gate: WriteSafetyGate | None = None
_read_gate: ReadSafetyGate | None = None
_memory: MemoryStore | None = None
_messages: list[dict] | None = None
_cancel_event: threading.Event = threading.Event()


def _write_line(obj: dict) -> None:
    """Write a JSON line to stdout and flush immediately."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _error(id_: Any, code: int, message: str) -> None:
    _write_line({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


# ── RPC method handlers ───────────────────────────────────────────────

def _handle_init(id_: Any, params: dict) -> None:
    global _config, _write_gate, _read_gate, _memory, _messages
    workspace = params.get("workspace", None)
    try:
        _config, _write_gate, _read_gate, _memory, _messages = init_session(workspace)
        _write_line({"jsonrpc": "2.0", "id": id_, "result": {"status": "ok"}})
    except Exception as exc:
        _error(id_, -1, f"init failed: {exc}")


def _handle_chat(id_: Any, params: dict) -> None:
    if _messages is None or _config is None:
        _error(id_, -2, "not initialized — call 'init' first")
        return

    user_content = params.get("message", "")
    if not user_content.strip():
        _error(id_, -3, "empty message")
        return

    _messages.append({"role": "user", "content": user_content})
    _cancel_event.clear()

    result_container: dict = {}

    def on_token(token: str) -> None:
        _write_line({"type": "token", "content": token})

    try:
        result = run_agent_turn(
            _messages,
            _config,
            _write_gate,
            _read_gate,
            on_token=on_token,
            cancel_event=_cancel_event,
            memory_store=_memory,
        )
    except Exception as exc:
        _error(id_, -4, f"turn error: {exc}")
        return

    if result is None:
        _write_line({"jsonrpc": "2.0", "id": id_, "result": {"cancelled": True}})
    else:
        content = result.get("content", "") if isinstance(result, dict) else str(result)
        _write_line({
            "jsonrpc": "2.0",
            "id": id_,
            "result": {"content": content, "role": "assistant"},
        })


def _handle_cancel(id_: Any, _params: dict) -> None:
    _cancel_event.set()
    _write_line({"jsonrpc": "2.0", "id": id_, "result": {"cancelled": True}})


# ── dispatch table ────────────────────────────────────────────────────

_METHODS = {
    "init":   _handle_init,
    "chat":   _handle_chat,
    "cancel": _handle_cancel,
}


# ── main loop ─────────────────────────────────────────────────────────

def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _error(None, -32700, "parse error")
            continue

        id_ = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        handler = _METHODS.get(method)
        if handler is None:
            _error(id_, -32601, f"method not found: {method}")
            continue

        handler(id_, params)


if __name__ == "__main__":
    main()
