#!/usr/bin/env python3
"""
server.py — JSON-lines backend server for the mini_agent Electron app.

Communicates with the Electron main process via stdin/stdout using
JSON-lines protocol. Each line is a complete JSON object.

Protocol (Electron → Python):
  {"type": "submit",    "text": "user message"}
  {"type": "command",   "command": "/clear"}
  {"type": "cancel"}
  {"type": "get_status"}
  {"type": "shutdown"}

Protocol (Python → Electron):
  {"type": "ready",     "model": "...", "workspace": "...", ...}
  {"type": "token",     "text": "..."}
  {"type": "thinking_start"}
  {"type": "thinking_end"}
  {"type": "tool_start","summary": "...", "parallel": bool}
  {"type": "tool_end",  "ok": bool, "detail": "..."}
  {"type": "tool_output","line": "..."}
  {"type": "turn_complete","usage": {...}, "turn_count": N}
  {"type": "error",     "message": "..."}
  {"type": "status",    "model": "...", "git_branch": "...", ...}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

# Ensure the parent mini_agent package is importable.
# main.js spawns us with cwd = mini_agent root, so cwd is the right path.
_cwd = os.getcwd()
if _cwd not in sys.path:
    sys.path.insert(0, _cwd)
# Also try relative to this file (belt-and-suspenders)
_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from config import (
    resolve_workspace, init_session, parse_args,
    build_startup_context,
)
from llm import run_agent_turn
from stream import THINKING_START, THINKING_END
from safety import ReadSafetyGate, WriteSafetyGate
from prompt import build_system_prompt
from api import clear_api_cache
from emoji_svg import clean_text


# ---------------------------------------------------------------------------
# JSON-lines transport
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()

def send_msg(msg: dict) -> None:
    """Write a JSON message to stdout followed by newline, then flush.

    Thread-safe: multiple sub-agent threads may call this concurrently
    via the subagent_callback.  The lock prevents interleaved writes.
    """
    line = json.dumps(msg, ensure_ascii=False, default=str)
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def read_msg() -> dict | None:
    """Read one JSON message from stdin. Returns None on EOF/error."""
    try:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        return json.loads(line)
    except (json.JSONDecodeError, EOFError, IOError) as e:
        # Log parse errors to stderr instead of flooding the UI.
        # Most common cause: concurrent stdin writes from Electron's
        # flushPending() and an IPC handler producing an interleaved line.
        # Show the raw line (truncated) so we can diagnose.
        import sys as _sys
        raw_preview = repr(line)[:120]
        print(f"[server] Ignoring stdin parse error ({raw_preview}): {e}", file=_sys.stderr, flush=True)
        return None


# ---------------------------------------------------------------------------
# Callbacks for run_agent_turn
# ---------------------------------------------------------------------------

class StreamCallbacks:
    """Callbacks that stream agent output to Electron via JSON messages."""

    def __init__(self):
        self._in_thinking = False

    def on_token(self, text: str) -> None:
        if text == THINKING_START:
            self._in_thinking = True
            send_msg({"type": "thinking_start"})
            return
        if text == THINKING_END:
            self._in_thinking = False
            send_msg({"type": "thinking_end"})
            return
        send_msg({"type": "token", "text": clean_text(text)})

    def on_tool_start(self, summary: str, parallel: bool = False) -> None:
        send_msg({"type": "tool_start", "summary": clean_text(summary), "parallel": parallel})

    def on_tool_end(self, ok: bool, detail: str, turn_id: int = 0, diff_preview=None, content: str = "") -> None:
        send_msg({"type": "tool_end", "ok": ok, "detail": clean_text(detail), "content": clean_text(content)})

    def on_tool_output(self, line: str, turn_id: int = 0) -> None:
        send_msg({"type": "tool_output", "line": clean_text(line)})

    # -- sub-agent events (wired to _TOOL_CONTEXT._subagent_callback) --

    def on_subagent_start(self, task_id: str, parent_id: str, name: str, desc: str) -> None:
        send_msg({"type": "subagent_start", "task_id": task_id, "parent_id": parent_id, "name": name, "desc": clean_text(desc)})

    def on_subagent_output(self, task_id: str, line: str) -> None:
        send_msg({"type": "subagent_output", "task_id": task_id, "line": clean_text(line)})

    def on_subagent_end(self, task_id: str, ok: bool, content: str) -> None:
        send_msg({"type": "subagent_end", "task_id": task_id, "ok": ok, "content": clean_text(content[:500])})

    def on_subagent_tool_start(self, task_id: str, tool_name: str, tool_args: str) -> None:
        send_msg({"type": "subagent_tool_start", "task_id": task_id, "tool_name": tool_name, "tool_args": tool_args})

    def on_subagent_tool_end(self, task_id: str, tool_name: str, ok: bool, content: str) -> None:
        send_msg({"type": "subagent_tool_end", "task_id": task_id, "tool_name": tool_name, "ok": ok, "content": clean_text(content[:500])})

    def on_subagent_thought(self, task_id: str, text: str) -> None:
        send_msg({"type": "subagent_thought", "task_id": task_id, "text": clean_text(text)})


# ---------------------------------------------------------------------------
# Agent runner — runs in a background thread so the main thread can accept
# cancel messages and new input while a turn is in progress.
# ---------------------------------------------------------------------------

class AgentRunner:
    def __init__(self):
        # Bootstrap the agent session
        workspace = os.environ.get("MINI_AGENT_WORKSPACE") or resolve_workspace()
        cli = parse_args()
        data = init_session(workspace, cli_args=cli)
        self.config = data["config"]
        self.config.stream = True
        self.write_gate: WriteSafetyGate = data["write_gate"]
        self.read_gate: ReadSafetyGate = data["read_gate"]
        self.memory = data["memory"]
        self.messages: list[dict] = data["messages"]
        self.session = data["session"]
        self.workspace = workspace

        self._cancel_event = threading.Event()
        self._turn_thread: threading.Thread | None = None
        self._total_turns = 0
        self._total_tokens = 0
        self._input_queue: list[str] = []
        self._input_lock = threading.Lock()
        self._callbacks = StreamCallbacks()

        # Sub-agent auto-report tracking:
        #   _pending_subagents  – set of task_ids spawned during the current turn.
        #   _auto_report_flag   – prevents double-queuing a synthesis prompt.
        # Reset at the start of each turn in _run_turn.
        self._pending_subagents: set[str] = set()
        self._auto_report_flag: bool = False

        # Git status
        self._git_branch = ""
        self._git_dirty = False
        self._refresh_git_status()

    # -- status ---------------------------------------------------------

    def send_status(self) -> None:
        """Send current status to Electron."""
        # Derive session name from memory db path
        session_name = "default"
        db_path = getattr(self.memory, '_db_path', '')
        if db_path:
            import re
            m = re.search(r'_session_(.+)\.db$', db_path)
            if m:
                session_name = m.group(1)
        status = {
            "type": "status",
            "model": self.config.model,
            "workspace": self.workspace,
            "session_name": session_name,
            "git_branch": self._git_branch,
            "git_dirty": self._git_dirty,
        }
        status["restored_count"] = max(0, len(self.messages) - 2)
        send_msg(status)

    def _refresh_git_status(self) -> None:
        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.config.workspace,
                capture_output=True, text=True, encoding="utf-8", timeout=3,
            )
            self._git_branch = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.workspace,
                capture_output=True, text=True, encoding="utf-8", timeout=3,
            )
            self._git_dirty = bool(r2.stdout.strip())
        except Exception:
            self._git_branch = ""
            self._git_dirty = False

    # -- turn execution -------------------------------------------------

    def submit(self, text: str) -> None:
        """Queue user input and start a turn if not already running."""
        with self._input_lock:
            self._input_queue.append(text)

        if self._turn_thread is None or not self._turn_thread.is_alive():
            self._start_turn()

    def _start_turn(self) -> None:
        """Start the sequential turn-processing loop in a background thread.

        The thread loops until the input queue is drained, running one
        turn at a time.  This avoids the race condition where a second
        turn's thread could call run_agent_turn concurrently with the
        first, corrupting self.messages.
        """
        self._turn_thread = threading.Thread(
            target=self._turn_loop, daemon=True
        )
        self._turn_thread.start()

    def _turn_loop(self) -> None:
        """Sequential turn-processing loop: drain queue, run turn, repeat."""
        while True:
            with self._input_lock:
                if not self._input_queue:
                    return  # all queued messages processed
                texts = list(self._input_queue)
                self._input_queue.clear()

            text = "\n\n".join(texts)
            self._cancel_event.clear()
            self._run_turn(text)

    def _run_turn(self, text: str) -> None:
        """Execute a single agent turn."""
        # Belt-and-suspenders: sub-agents may mutate config.stream when they
        # share the same config object.  Force it back to True for the
        # orchestrator so streaming always works.
        self.config.stream = True
        self.messages.append({"role": "user", "content": text})

        # Reset sub-agent auto-report tracking for this turn
        self._pending_subagents.clear()
        self._auto_report_flag = False

        # Wire sub-agent events to Electron via a callback on the tool context.
        # The callback is called from _spawn_one (agent_ops.py) on sub-agent
        # lifecycle events (start, output, end).
        #
        # IMPORTANT: We set this once during init() and NEVER clear it after
        # run_agent_turn returns.  If we clear it, sub-agents spawned by other
        # sub-agents (grandchildren) won't find a callback because the parent
        # turn may have already finished and cleared it.  The callback closure
        # captures `self` (AgentRunner) which lives for the whole session, so
        # it's safe to keep permanently.
        from tools import _TOOL_CONTEXT
        if getattr(_TOOL_CONTEXT, "_subagent_callback", None) is None:
            def _sub_cb(event_type: str, data: dict) -> None:
                if event_type == "start":
                    task_id = data.get("task_id", "")
                    self._pending_subagents.add(task_id)
                    self._callbacks.on_subagent_start(
                        task_id, data.get("parent_id", ""),
                        data.get("name", ""), data.get("desc", ""))
                elif event_type == "output":
                    self._callbacks.on_subagent_output(
                        data.get("task_id", ""), data.get("line", ""))
                elif event_type == "end":
                    task_id = data.get("task_id", "")
                    self._pending_subagents.discard(task_id)
                    self._callbacks.on_subagent_end(
                        task_id, data.get("ok", False),
                        data.get("content", ""))
                    # Auto-report: if all sub-agents from this turn
                    # have finished, queue a synthesis prompt so the
                    # orchestrator processes and reports their results.
                    if not self._pending_subagents and not self._auto_report_flag:
                        self._auto_report_flag = True
                        self.submit(
                            "[Report: All sub-agents have completed. "
                            "Synthesize their results and report to the user.]"
                        )
                elif event_type == "tool_start":
                    self._callbacks.on_subagent_tool_start(
                        data.get("task_id", ""), data.get("tool_name", ""),
                        data.get("tool_args", ""))
                elif event_type == "tool_end":
                    self._callbacks.on_subagent_tool_end(
                        data.get("task_id", ""), data.get("tool_name", ""),
                        data.get("ok", False), data.get("content", ""))
                elif event_type == "thought":
                    self._callbacks.on_subagent_thought(
                        data.get("task_id", ""), data.get("text", ""))
            _TOOL_CONTEXT._subagent_callback = _sub_cb

        try:
            msg = run_agent_turn(
                self.messages, self.config,
                self.write_gate, self.read_gate,
                on_token=self._callbacks.on_token,
                on_tool_start=self._callbacks.on_tool_start,
                on_tool_end=self._callbacks.on_tool_end,
                on_tool_output=self._callbacks.on_tool_output,
                cancel_event=self._cancel_event,
                session=self.session,
            )
        except Exception as e:
            # Safety: reset thinking flag so a stuck marker doesn't persist
            self._callbacks._in_thinking = False
            if not self._cancel_event.is_set():
                send_msg({"type": "error", "message": clean_text(str(e))})
            # Always send turn_complete so the renderer resets its loading state
            send_msg({
                "type": "turn_complete",
                "usage": {"total_tokens": self._total_tokens, "prompt_tokens": 0, "completion_tokens": 0},
                "turn_count": self._total_turns,
            })
            return
        # Safety: reset thinking flag so a stuck marker doesn't persist across turns
        self._callbacks._in_thinking = False

        if self._cancel_event.is_set():
            send_msg({
                "type": "turn_complete",
                "usage": {"total_tokens": self._total_tokens, "prompt_tokens": 0, "completion_tokens": 0},
                "turn_count": self._total_turns,
                "cancelled": True,
            })
            return

        if msg is not None:
            self._total_turns += msg.get("_turn_count", 0)
            usage = msg.get("_total_usage") or {}
            self._total_tokens += usage.get("total_tokens", 0)

        # Persist
        self.messages = self.memory.save(self.messages)

        # Notify Electron
        send_msg({
            "type": "turn_complete",
            "usage": {"total_tokens": self._total_tokens, "prompt_tokens": 0, "completion_tokens": 0},
            "turn_count": self._total_turns,
        })

    # -- commands -------------------------------------------------------

    def handle_command(self, command: str) -> None:
        """Handle /slash commands."""
        cmd = command.lower().strip()

        if cmd == "/clear":
            self._cancel_event.set()
            self.messages = [
                {"role": "system", "content": build_system_prompt(self.config)},
                {"role": "system", "content": build_startup_context(self.config.workspace)},
            ]
            clear_api_cache()
            self.memory.clear()
            self._total_turns = 0
            self._total_tokens = 0
            send_msg({"type": "response", "lines": ["--- conversation cleared ---"]})
            return

        if cmd == "/stats":
            send_msg({
                "type": "response",
                "lines": [
                    f"Session: {len(self.messages)} msgs, {self._total_turns} turns, "
                    f"{self._total_tokens} tokens, {self.config.model}"
                ]
            })
            return

        if cmd.startswith("/session"):
            parts = cmd.split(maxsplit=2)
            sub = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""
            if sub == "list":
                from config import list_sessions
                sessions = list_sessions(self.workspace)
                send_msg({
                    "type": "response",
                    "lines": [f"Sessions: {', '.join(sessions) if sessions else 'none'}"]
                })
            elif sub == "new" and arg:
                from config import switch_session
                sd = switch_session(self.workspace, arg, self.memory, self.config)
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                self.memory = sd["memory"]
                self.messages = sd["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                send_msg({"type": "response", "lines": [f"Created session '{arg}'."]})
            elif sub == "switch" and arg:
                from config import switch_session
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                sd = switch_session(self.workspace, arg, self.memory, self.config)
                self.memory = sd["memory"]
                self.messages = sd["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                send_msg({"type": "response", "lines": [f"Switched to '{arg}'."]})
            elif sub == "delete" and arg:
                from config import delete_session
                ok, msg = delete_session(self.workspace, arg)
                send_msg({"type": "response", "lines": [msg]})
            else:
                send_msg({
                    "type": "response",
                    "lines": ["Usage: /session new <name> | switch <name> | delete <name> | list"]
                })
            return

        if cmd == "/export":
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"conversation_{ts}.md"
            path = os.path.join(self.config.workspace, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write("# mini_agent Conversation\n\n")
                for msg in self.messages:
                    role = msg["role"].upper()
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        f.write(f"## {role}\n\n{content}\n\n")
            send_msg({"type": "response", "lines": [f"Exported to {fname}"]})
            return

        if cmd == "/test-svg":
            lines = [
                "✅ SVG icon test — check-circle",
                "❌ SVG icon test — x-circle",
                "⚠️ SVG icon test — warning",
                "💡 SVG icon test — lightbulb",
                "📁 SVG icon test — folder",
                "🔧 SVG icon test — wrench",
                "🚀 SVG icon test — rocket",
                "⭐ SVG icon test — star",
                "🐛 SVG icon test — bug",
                "🔥 SVG icon test — fire",
                "💥 SVG icon test — burst",
            ]
            send_msg({
                "type": "response",
                "lines": [clean_text(l) for l in lines],
            })
            return

        if cmd == "/init":
            from tools.file_ops import _init_rules
            rg = ReadSafetyGate(self.config.workspace)
            from tools import ToolResult
            result = _init_rules({}, None, rg)
            lines = str(result.content).split("\n") if result.content else []
            send_msg({"type": "response", "lines": lines})
            return

        if cmd.startswith("/workspace"):
            parts = command.split(maxsplit=1)
            new_path = parts[1].strip() if len(parts) > 1 else ""
            if not new_path:
                send_msg({"type": "response", "lines": ["Usage: /workspace <path>"]})
                return
            new_workspace = os.path.abspath(new_path)
            if not os.path.isdir(new_workspace):
                send_msg({"type": "response", "lines": [f"Not a directory: {new_workspace}"]})
                return
            self.messages = self.memory.save(self.messages)
            self.memory.close()
            self.workspace = new_workspace
            try:
                new_data = init_session(new_workspace)
            except Exception as exc:
                send_msg({"type": "error", "message": clean_text(str(exc))})
                return
            self.config = new_data["config"]
            self.config.stream = True
            self.write_gate = new_data["write_gate"]
            self.read_gate = new_data["read_gate"]
            self.memory = new_data["memory"]
            self.messages = new_data["messages"]
            self.session.close()
            self.session = new_data["session"]
            self._total_turns = 0
            self._total_tokens = 0
            self._refresh_git_status()
            self.send_status()
            return

        send_msg({"type": "response", "lines": [f"Unknown command: {command}"]})

    def cancel(self) -> None:
        """Cancel the current turn."""
        self._cancel_event.set()


# ---------------------------------------------------------------------------
# Main — JSON-lines event loop
# ---------------------------------------------------------------------------

def main() -> None:
    # Disable Python buffering on stdout
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

    runner = AgentRunner()

    # Send initial ready + status
    send_msg({"type": "ready", "model": runner.config.model})
    runner.send_status()

    # Event loop: read messages from stdin, dispatch
    while True:
        msg = read_msg()
        if msg is None:
            # EOF — Electron closed stdin
            break

        msg_type = msg.get("type", "")

        if msg_type == "submit":
            runner.submit(msg.get("text", ""))

        elif msg_type == "command":
            runner.handle_command(msg.get("command", ""))

        elif msg_type == "cancel":
            runner.cancel()

        elif msg_type == "get_status":
            runner.send_status()

        elif msg_type == "session_list":
            from config import list_sessions
            sessions = list_sessions(runner.workspace)
            current = ""
            db_path = getattr(runner.memory, '_db_path', '')
            if db_path:
                import re
                m = re.search(r'_session_(.+)\.db$', db_path)
                current = m.group(1) if m else "default"
            else:
                current = "default"
            send_msg({"type": "session_list_result", "sessions": sessions, "current": current})

        elif msg_type == "session_switch":
            from config import switch_session
            name = msg.get("name", "")
            if not name:
                send_msg({"type": "session_list_result", "error": "Session name required."})
            else:
                runner.messages = runner.memory.save(runner.messages)
                runner.memory.close()
                sd = switch_session(runner.workspace, name, runner.memory, runner.config)
                runner.memory = sd["memory"]
                runner.messages = sd["messages"]
                runner._total_turns = 0
                runner._total_tokens = 0
                runner.send_status()

        elif msg_type == "session_new":
            from config import switch_session
            name = msg.get("name", "")
            if not name:
                send_msg({"type": "session_list_result", "error": "Session name required."})
            else:
                # switch_session creates a new session if it doesn't exist
                sd = switch_session(runner.workspace, name, runner.memory, runner.config)
                runner.messages = runner.memory.save(runner.messages)
                runner.memory.close()
                runner.memory = sd["memory"]
                runner.messages = sd["messages"]
                runner._total_turns = 0
                runner._total_tokens = 0
                runner.send_status()

        elif msg_type == "session_delete":
            from config import delete_session
            name = msg.get("name", "")
            if not name:
                send_msg({"type": "session_list_result", "error": "Session name required."})
            else:
                ok, msg_text = delete_session(runner.workspace, name)
                if ok and name == getattr(runner, '_session_name', None):
                    # Deleted the current session — switch to default
                    from config import switch_session
                    sd = switch_session(runner.workspace, "default", runner.memory, runner.config)
                    runner.memory = sd["memory"]
                    runner.messages = sd["messages"]
                    runner._total_turns = 0
                    runner._total_tokens = 0
                send_msg({"type": "session_delete_result", "ok": ok, "message": msg_text})
                runner.send_status()

        elif msg_type == "shutdown":
            break

        else:
            send_msg({"type": "error", "message": clean_text(f"Unknown message type: {msg_type}")})

    # Cleanup
    try:
        runner.messages = runner.memory.save(runner.messages)
        runner.memory.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
