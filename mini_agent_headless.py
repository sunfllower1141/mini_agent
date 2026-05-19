#!/usr/bin/env python3
"""
mini_agent_headless.py -- JSON-line IPC backend for the Ink CLI.

Spawned by the Node UI as a child process.  Communicates via
newline-delimited JSON on stdin/stdout.  Protocol: see headless_ipc.py.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from typing import Any

import requests

from api import APIError
from config import (
    AgentConfig, resolve_workspace,
    init_session, parse_args, list_sessions, switch_session, delete_session,
)
from llm import run_agent_turn
from prompt import build_system_prompt
import ws_server

from headless_ipc import (
    StdoutEmitter, StdinReader, Command, make_callbacks,
    EVT_READY, EVT_TURN_DONE, EVT_ERROR, EVT_STATUS, EVT_LOG,
    EVT_APPROVE_REQ,
    CMD_USER_MESSAGE, CMD_USER_CANCEL, CMD_USER_APPROVE,
    CMD_USER_COMMAND, CMD_USER_QUIT,
)

# ---------------------------------------------------------------------------
# Approval bridge
# ---------------------------------------------------------------------------

class ApprovalBridge:
    def __init__(self, emitter: StdoutEmitter, timeout: float = 300.0) -> None:
        self._emitter = emitter
        self._timeout = timeout
        self._lock = threading.Lock()
        self._counter = 0
        self._pending: dict[int, tuple[threading.Event, list]] = {}

    def request(self, tool_name: str, args: dict) -> bool:
        with self._lock:
            self._counter += 1
            req_id = self._counter
            event = threading.Event()
            box: list = [False]
            self._pending[req_id] = (event, box)
        brief = json.dumps(args, default=str)
        if len(brief) > 200:
            brief = brief[:200] + "..."
        self._emitter.emit(EVT_APPROVE_REQ, {
            "id": req_id, "tool_name": tool_name, "args_brief": brief,
        })
        if not event.wait(self._timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            return False
        with self._lock:
            self._pending.pop(req_id, None)
        return bool(box[0])

    def resolve(self, req_id: int, allow: bool) -> None:
        with self._lock:
            entry = self._pending.get(req_id)
        if entry is None:
            return
        event, box = entry
        box[0] = bool(allow)
        event.set()


# ---------------------------------------------------------------------------
# Agent worker thread
# ---------------------------------------------------------------------------

class AgentWorker(threading.Thread):
    def __init__(self, messages, config, write_gate, read_gate, callbacks,
                 session, approve_callback, memory_store,
                 done_queue: queue.Queue, emitter: StdoutEmitter):
        super().__init__(daemon=True, name="agent-worker")
        self.messages = messages
        self.config = config
        self.write_gate = write_gate
        self.read_gate = read_gate
        self.callbacks = callbacks
        self.session = session
        self.approve_callback = approve_callback
        self.memory_store = memory_store
        self.done_queue = done_queue
        self.emitter = emitter
        self.cancel = threading.Event()
        self.result: dict | None = None
        self.error: str | None = None

    def run(self) -> None:
        self.config.stream = True
        self.emitter.emit(EVT_LOG, {"level": "info", "msg": "agent turn starting..."})
        try:
            self.result = run_agent_turn(
                self.messages, self.config,
                self.write_gate, self.read_gate,
                cancel_event=self.cancel,
                session=self.session,
                memory_store=self.memory_store,
                approve_callback=self.approve_callback,
                **self.callbacks,
            )
            self.emitter.emit(EVT_LOG, {"level": "info", "msg": "agent turn completed"})
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.emitter.emit(EVT_LOG, {"level": "error", "msg": f"agent turn failed: {self.error}"})
        finally:
            self.done_queue.put(self)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def _export_conversation(messages: list[dict], workspace: str) -> str:
    import datetime
    from memory import export_conversation_markdown
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"conversation_{ts}.md"
    path = os.path.join(workspace, fname)
    md = export_conversation_markdown(messages)
    md = md.replace("mini_agent conversation", f"mini_agent conversation -- {ts}", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def _handle_command(cmd_name: str, cmd_args: dict, *, state: dict, emitter: StdoutEmitter) -> str:
    workspace = state["workspace"]
    messages = state["messages"]
    config   = state["config"]
    memory   = state["memory"]
    cli      = state["cli"]

    if cmd_name == "init":
        from tools.file_ops import _init_rules
        from safety import ReadSafetyGate
        rg = ReadSafetyGate(workspace)
        result = _init_rules({}, None, rg)
        return str(result.content)
    if cmd_name == "clear":
        new = [{"role": "system", "content": build_system_prompt(config)}]
        messages.clear(); messages.extend(new)
        memory.clear()
        state["stats"] = {"turns": 0, "tool_calls": 0}
        return "Memory cleared."
    if cmd_name == "export":
        return f"Exported to {_export_conversation(messages, workspace)}"
    if cmd_name == "stats":
        return f"Turns: {state['stats']['turns']}  Tool calls: {state['stats']['tool_calls']}  Messages: {len(messages)}"
    if cmd_name == "session":
        sub = (cmd_args.get("sub") or "").lower()
        name = cmd_args.get("name") or ""
        if sub == "list":
            sessions = list_sessions(workspace)
            return f"Sessions: {', '.join(sessions)}" if sessions else "No saved sessions."
        if sub in ("new", "switch") and name:
            memory.save(messages)
            data = switch_session(workspace, name, memory, config)
            state["memory"] = data["memory"]
            state["messages"] = data["messages"]
            state["stats"] = {"turns": 0, "tool_calls": 0}
            return f"Switched to session '{name}'."
        if sub == "delete" and name:
            _, msg = delete_session(workspace, name)
            return msg
        return "Usage: /session new <name> | switch <name> | delete <name> | list"
    if cmd_name == "workspace":
        new_path = cmd_args.get("path", "").strip()
        if not new_path:
            return "Usage: /workspace <path>"
        new_workspace = os.path.abspath(new_path)
        if not os.path.isdir(new_workspace):
            return f"Not a directory: {new_workspace}"
        memory.save(messages)
        new_data = init_session(new_workspace, cli_args=cli)
        state["config"] = new_data["config"]
        state["write_gate"] = new_data["write_gate"]
        state["read_gate"] = new_data["read_gate"]
        state["memory"] = new_data["memory"]
        state["messages"] = new_data["messages"]
        state["session"].close()
        state["session"] = new_data["session"]
        state["workspace"] = new_workspace
        state["stats"] = {"turns": 0, "tool_calls": 0}
        return f"Workspace switched to: {new_workspace}"
    return f"Unknown command: /{cmd_name}"


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def _emit_status(emitter: StdoutEmitter, state: dict) -> None:
    import subprocess
    workspace = state["workspace"]
    git_branch = ""
    git_dirty = False
    try:
        r = subprocess.run(["git", "branch", "--show-current"], cwd=workspace,
                           capture_output=True, text=True, timeout=2)
        git_branch = r.stdout.strip()
        r2 = subprocess.run(["git", "status", "--porcelain"], cwd=workspace,
                            capture_output=True, text=True, timeout=2)
        git_dirty = bool(r2.stdout.strip())
    except Exception:
        pass
    emitter.emit(EVT_STATUS, {
        "model": state["config"].model,
        "workspace": workspace,
        "git_branch": git_branch,
        "git_dirty": git_dirty,
        "total_turns": state["stats"]["turns"],
        "total_tool_calls": state["stats"]["tool_calls"],
    })


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    cli = parse_args()
    workspace = resolve_workspace(override=getattr(cli, "workspace", None))
    data = init_session(workspace, cli_args=cli)
    config: AgentConfig = data["config"]
    config.stream = True

    emitter = StdoutEmitter()
    inbox: queue.Queue = queue.Queue()
    reader = StdinReader(
        out=inbox,
        on_error=lambda m: emitter.emit(EVT_LOG, {"level": "warn", "msg": f"stdin: {m}"}),
    )
    reader.start()

    approval = ApprovalBridge(emitter)

    state: dict = {
        "workspace":  workspace,
        "config":     config,
        "write_gate": data["write_gate"],
        "read_gate":  data["read_gate"],
        "memory":     data["memory"],
        "messages":   data["messages"],
        "session":    data["session"],
        "stats":      {"turns": 0, "tool_calls": 0},
        "cli":        cli,
    }

    # WebSocket server -- best-effort
    try:
        ws_server.start()
        ws_server.emit_graph_init(workspace)
    except Exception:
        pass

    try:
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT.__dict__["_ipc_emitter"] = emitter
    except Exception:
        pass

    emitter.emit(EVT_READY, {
        "model": config.model,
        "workspace": workspace,
        "restored_messages": max(0, len(state["messages"]) - 2),
    })
    emitter.emit(EVT_LOG, {"level": "info",
        "msg": f"headless ready — python={sys.executable} model={config.model} msgs={len(state['messages'])}"})
    _emit_status(emitter, state)

    worker: AgentWorker | None = None
    done_q: queue.Queue = queue.Queue()

    try:
        while True:
            # 1. Check worker completion
            if worker is not None:
                try:
                    finished = done_q.get_nowait()
                except queue.Empty:
                    finished = None
                if finished is not None:
                    if finished.error:
                        emitter.emit(EVT_ERROR, {"msg": finished.error})
                    msg = finished.result
                    usage = (msg or {}).get("_total_usage") if msg else None
                    emitter.emit(EVT_TURN_DONE, {
                        "usage": usage,
                        "turn_count": (msg or {}).get("_turn_count", 0) if msg else 0,
                        "cancelled": msg is None and not finished.error,
                    })
                    state["stats"]["turns"] += 1
                    state["messages"] = state["memory"].save(state["messages"])
                    _emit_status(emitter, state)
                    worker = None

            # 2. Drain commands
            try:
                cmd = inbox.get(timeout=0.1)
            except queue.Empty:
                continue

            if cmd is None:
                if worker is not None:
                    worker.cancel.set()
                break

            assert isinstance(cmd, Command)

            if cmd.type == CMD_USER_QUIT:
                if worker is not None:
                    worker.cancel.set()
                state["memory"].save(state["messages"])
                break

            if cmd.type == CMD_USER_CANCEL and worker is not None:
                worker.cancel.set()
                continue

            if cmd.type == CMD_USER_APPROVE:
                approval.resolve(int(cmd.data.get("id", 0)),
                                 bool(cmd.data.get("allow", False)))
                continue

            if cmd.type == CMD_USER_COMMAND:
                name = cmd.data.get("name", "")
                args = cmd.data.get("args", {}) or {}
                text = _handle_command(name, args, state=state, emitter=emitter)
                if text:
                    emitter.emit(EVT_LOG, {"level": "info", "msg": text})
                _emit_status(emitter, state)
                continue

            if cmd.type == CMD_USER_MESSAGE:
                if worker is not None:
                    emitter.emit(EVT_LOG, {"level": "warn", "msg": "turn already in progress"})
                    continue
                text = (cmd.data.get("text") or "").strip()
                if not text:
                    continue

                emitter.emit(EVT_LOG, {"level": "info", "msg": f"received: {text[:80]}"})
                state["messages"].append({"role": "user", "content": text})
                callbacks = make_callbacks(emitter, agent_id="orchestrator")

                _orig_start = callbacks["on_tool_start"]
                def _counted_start(s: str, parallel: bool = False, _o=_orig_start) -> None:
                    state["stats"]["tool_calls"] += 1
                    _o(s, parallel)
                callbacks["on_tool_start"] = _counted_start

                approve_cb = (
                    (lambda name, args: approval.request(name, args))
                    if config.approve_write_ops else None
                )

                worker = AgentWorker(
                    messages=state["messages"],
                    config=state["config"],
                    write_gate=state["write_gate"],
                    read_gate=state["read_gate"],
                    callbacks=callbacks,
                    session=state["session"],
                    approve_callback=approve_cb,
                    memory_store=state["memory"],
                    done_queue=done_q,
                    emitter=emitter,
                )
                worker.start()
                continue

    finally:
        reader.stop()
        emitter.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
