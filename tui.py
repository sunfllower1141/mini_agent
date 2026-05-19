#!/usr/bin/env python3
"""
tui.py — Textual TUI frontend for mini_agent.

Usage: python tui.py [--workspace PATH] [--quiet] [--stream] [--allow-overwrites] [--approve]

Themes stolen from Agents UI: Dawn, Sepia, Ember, Slate, Midnight, Cobalt, Neon, Forest.
Live status bar stolen from Agent Terminal.
Tool cards stolen from better-agent-terminal.
Attention pulse stolen from CodeGrid.
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
from queue import Queue, Empty
from dataclasses import dataclass


# NOTE: Per-token call_from_thread was burning CPU (one async dispatch per token,
# hundreds/sec).  Now the TUI drains the queue on a 60 fps timer instead,
# batching up to ~16 ms of tokens into one UI update.  Massive battery win.


# Escape user content for Rich markup.  rich.markup.escape() skips
# '[' that looks like a valid tag opener (e.g. '[/') — we can't
# trust it, so do our own simple escaping.
def _safe(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", r"\[")

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, HorizontalScroll
from textual.widgets import Header, Footer, RichLog, TextArea, Tree
from textual.binding import Binding

import requests

from config import AgentConfig, resolve_workspace, init_session, parse_args, build_startup_context
from api import APIError
from llm import run_agent_turn
from stream import THINKING_START, THINKING_END
from prompt import build_system_prompt
from safety import ReadSafetyGate, WriteSafetyGate
from memory import MemoryStore
from tools import set_context, build_symbol_index
from tools import _WS_AGENT_ID as _ws_agent_id
import ws_server


# ---------------------------------------------------------------------------
# Themes — stolen from Agents UI palette
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TuiTheme:
    name: str
    bg: str       # screen background
    surface: str  # header, footer, input area
    border: str   # scrollbar, separators
    accent: str   # header text, highlights
    text: str     # primary text
    dim: str      # secondary text, footer
    green: str    # success
    yellow: str   # warnings, tool calls
    red: str      # errors
    thinking: str # thinking block dim
    pulse: str    # attention/approval glow
    purple: str   # interjection queued messages

THEMES: dict[str, TuiTheme] = {
    "dawn": TuiTheme(
        name="Dawn",
        bg="#faf8f5", surface="#f0ede8", border="#d4cfc8",
        accent="#b8956a", text="#3d3a35", dim="#8a857d",
        green="#5a8a4a", yellow="#b89540", red="#c06050",
        thinking="#b0aaa0", pulse="#f0c060", purple="#a080c0",
    ),
    "sepia": TuiTheme(
        name="Sepia",
        bg="#f4f0e6", surface="#e8e0d0", border="#c8b898",
        accent="#b8893a", text="#4a3f30", dim="#8a7a60",
        green="#6a8a4a", yellow="#c0a040", red="#b85840",
        thinking="#b0a080", pulse="#e0b040", purple="#9a7ab0",
    ),
    "ember": TuiTheme(
        name="Ember",
        bg="#1e1814", surface="#2a221c", border="#3a3028",
        accent="#d4985a", text="#d0c8be", dim="#7a7064",
        green="#7ab860", yellow="#d4a040", red="#d47050",
        thinking="#5a5040", pulse="#e89840", purple="#c090d0",
    ),
    "slate": TuiTheme(
        name="Slate",
        bg="#111111", surface="#1b1b1b", border="#2a2a2a",
        accent="#8f8f8f", text="#b8b8b8", dim="#5a5a5a",
        green="#4f9f6f", yellow="#b89a4a", red="#a85a5a",
        thinking="#3a3a3a", pulse="#c0c040", purple="#8a7ab0",
    ),
    "midnight": TuiTheme(
        name="Midnight",
        bg="#090b0d", surface="#131619", border="#1e2226",
        accent="#8899aa", text="#b0c0d0", dim="#4a5560",
        green="#4a8a6a", yellow="#9a8a4a", red="#9a6060",
        thinking="#2a3040", pulse="#6a8acc", purple="#7a8ab0",
    ),
    "cobalt": TuiTheme(
        name="Cobalt",
        bg="#0a1220", surface="#101830", border="#1e2850",
        accent="#6090d0", text="#a0b8d8", dim="#4a6090",
        green="#5a9a6a", yellow="#a0a040", red="#b06060",
        thinking="#203050", pulse="#5090e0", purple="#8090d0",
    ),
    "neon": TuiTheme(
        name="Neon",
        bg="#0c0c0c", surface="#16161a", border="#303030",
        accent="#e040e0", text="#c0e0c0", dim="#506050",
        green="#00e060", yellow="#e0c000", red="#ff4060",
        thinking="#302040", pulse="#e040ff", purple="#c040ff",
    ),
    "forest": TuiTheme(
        name="Forest",
        bg="#0e1410", surface="#141c16", border="#1e2e22",
        accent="#60a870", text="#a0c0a8", dim="#4a6a50",
        green="#60d070", yellow="#b0b040", red="#c06050",
        thinking="#203028", pulse="#50d060", purple="#8090b0",
    ),
    "dracula": TuiTheme(
        name="Dracula",
        bg="#282a36", surface="#1e1f29", border="#44475a",
        accent="#bd93f9", text="#f8f8f2", dim="#6272a4",
        green="#50fa7b", yellow="#f1fa8c", red="#ff5555",
        thinking="#44475a", pulse="#ff79c6", purple="#bd93f9",
    ),
}

DEFAULT_THEME = "slate"
_AGENT_COLORS = ["green", "yellow", "accent", "pulse", "red"]


def _build_css(theme: TuiTheme) -> str:
    """Build the Textual CSS string from a Theme palette.

    Layout (top to bottom):
      Header
      #static-pane   — Horizontal: #tools-log (left) + #agent-tree (right)  (35%)
      #chat-pane     — user input, streaming assistant     (1fr)
      #input-area    — TextArea for user typing
      Footer
    """
    return f"""
Screen {{
    background: {theme.bg};
}}

Header {{
    background: {theme.surface};
    color: {theme.accent};
    text-style: bold;
}}

Footer {{
    background: {theme.bg};
    color: {theme.dim};
    transition: background 300ms;
}}

Footer.pulse {{
    background: {theme.pulse};
}}

#static-pane {{
    height: 35%;
    min-height: 5;
}}

#tools-log {{
    background: {theme.bg};
    color: {theme.text};
    border: none;
    border-bottom: solid {theme.border};
    padding: 0 1;
    overflow-y: auto;
    scrollbar-size: 0 0;
}}

#agent-tree {{
    display: block;
    background: {theme.bg};
    color: {theme.dim};
    border: none;
    border-left: solid {theme.border};
    border-bottom: solid {theme.border};
    padding: 0 1;
    overflow-y: auto;
    scrollbar-size: 0 0;
    min-width: 25;
}}

#subagent-pane {{
    background: {theme.bg};
    color: {theme.dim};
    border: none;
    border-top: solid {theme.border};
    padding: 0 1;
    height: auto;
    max-height: 12;
    min-height: 0;
    scrollbar-size: 0 0;
    display: none;
    layout: horizontal;
}}

#subagent-pane RichLog {{
    background: {theme.bg};
    width: 1fr;
    margin: 0 1;
    scrollbar-size: 0 0;
    border: solid {theme.border};
}}

#chat-pane {{
    background: {theme.bg};
    color: {theme.text};
    border: none;
    padding: 0 1;
    height: 1fr;
    scrollbar-size: 0 0;
}}

#input-area {{
    background: {theme.bg};
    border-top: solid {theme.dim};
    padding: 1 2;
    height: auto;
    min-height: 3;
    max-height: 12;
}}

#input {{
    background: {theme.bg};
    color: {theme.text};
    border: none;
    width: 100%;
    height: auto;
}}

#status-bar {{
    background: {theme.surface};
    color: {theme.dim};
    height: 1;
    padding: 0 2;
}}
"""


# ---------------------------------------------------------------------------
# Queue messages
# ---------------------------------------------------------------------------

@dataclass
class _TokenMsg:
    text: str

@dataclass
class _ToolStart:
    summary: str
    parallel: bool = False
    turn_id: int = 0

@dataclass
class _ToolEnd:
    ok: bool
    detail: str
    turn_id: int = 0
    diff_preview: str | None = None

@dataclass
class _SubAgentToken:
    """A token of streaming output from a sub-agent."""
    task_id: str
    text: str
    def __repr__(self):
        return f"_SubAgentToken(task_id={self.task_id!r}, text={self.text!r})"

@dataclass
class _ToolOutput:
    text: str
    turn_id: int = 0

@dataclass
class _Done:
    usage: dict | None = None
    turn_count: int = 0
    turn_id: int = 0

@dataclass
class _Error:
    msg: str


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class AgentWorker(threading.Thread):
    """Runs the agent loop in a background thread, pushing messages to a queue."""

    def __init__(self, messages, config, write_gate, read_gate, out: Queue, session, approve_callback=None, turn_id: int = 0):
        super().__init__(daemon=True)
        self.messages = messages
        self.config = config
        self.write_gate = write_gate
        self.read_gate = read_gate
        self.out = out
        self.cancel = threading.Event()
        self.session = session
        self.approve_callback = approve_callback
        self.turn_id = turn_id

    def run(self):
        config = self.config
        config.stream = True

        try:
            msg = run_agent_turn(
                self.messages, config,
                self.write_gate, self.read_gate,
                on_token=lambda t: self.out.put(_TokenMsg(t)),
                on_tool_start=lambda s, parallel=False: self.out.put(_ToolStart(s, parallel, turn_id=self.turn_id)),
                on_tool_end=lambda ok, d, diff_preview=None: self.out.put(_ToolEnd(ok, d, turn_id=self.turn_id, diff_preview=diff_preview)),
                on_tool_output=lambda line: self.out.put(_ToolOutput(line, turn_id=self.turn_id)),
                cancel_event=self.cancel,
                session=self.session,
                approve_callback=self.approve_callback,
            )
        except (APIError, requests.RequestException, RuntimeError, ValueError) as e:
            self.out.put(_Error(str(e)))
            self.out.put(_Done(turn_id=self.turn_id))
            return

        if msg is not None:
            self.out.put(_Done(
                usage=msg.get("_total_usage"),
                turn_count=msg.get("_turn_count", 0),
                turn_id=self.turn_id,
            ))
        # If msg is None, turn was cancelled — app's cancel handler cleans up


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class MiniAgentTUI(App):
    """Textual TUI for mini_agent."""

    CSS = _build_css(THEMES[DEFAULT_THEME])

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+z", "suspend_process", "Suspend", show=False),
        Binding("ctrl+shift+c", "copy", "Copy"),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="static-pane"):
            yield RichLog(id="tools-log", highlight=True, markup=True, wrap=True)
            yield Tree("agent", id="agent-tree")
        with HorizontalScroll(id="subagent-pane"):
            pass
        yield RichLog(id="chat-pane", highlight=True, markup=True, wrap=True)
        with Container(id="input-area"):
            yield TextArea("", id="input")
        yield Footer()

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Box-drawing helpers
    # ------------------------------------------------------------------

    def _box_open(self, log: RichLog, label: str, color: str) -> None:
        """Buffer the top border of a message box (rendered on next flush)."""
        self._write_to_log(log, f"[{color}]╭── {label} ──[/]")

    def _box_line(self, log: RichLog, text: str, color: str) -> None:
        """Buffer a single content line inside a message box."""
        self._write_to_log(log, f"[{color}]│ {text}[/]")

    def _box_empty(self, log: RichLog, color: str) -> None:
        """Buffer an empty line inside a message box (side border only)."""
        self._write_to_log(log, f"[{color}]│[/]")

    def _box_close(self, log: RichLog, color: str, label: str = "") -> None:
        """Buffer the bottom border of a message box (rendered on next flush)."""
        suffix = f" {label}" if label else ""
        self._write_to_log(log, f"[{color}]╰──[/]{suffix}")

    def _write_to_log(self, log: RichLog, text: str) -> None:
        """Buffer text for a given RichLog instead of writing immediately.

        During a drain cycle this accumulates text into per-pane buffers.
        At end of drain, _flush_logs() writes each buffer with a single
        RichLog.write() call to batch re-renders.
        """
        if log is self._chat:
            self._chat_buf += text + "\n"
        elif log is self._tools_log:
            self._tools_buf += text + "\n"
        else:
            # Sub-agent panes or other dynamic RichLog widgets — buffer by id.
            if not hasattr(self, "_log_bufs"):
                self._log_bufs: dict[int, str] = {}
            lid = id(log)
            self._log_bufs[lid] = self._log_bufs.get(lid, "") + text + "\n"
            if not hasattr(self, "_log_buf_objs"):
                self._log_buf_objs: dict[int, RichLog] = {}
            self._log_buf_objs[lid] = log

    def _flush_logs(self) -> None:
        """Write all buffered text to their RichLog widgets in one batch."""
        if self._chat_buf:
            self._chat.write(self._chat_buf.rstrip("\n"))
            self._chat_buf = ""
        if self._tools_buf:
            self._tools_log.write(self._tools_buf.rstrip("\n"))
            self._tools_buf = ""
        if hasattr(self, "_log_bufs"):
            for lid, text in self._log_bufs.items():
                if text:
                    log = self._log_buf_objs.get(lid)
                    if log:
                        log.write(text.rstrip("\n"))
            self._log_bufs.clear()
            if hasattr(self, "_log_buf_objs"):
                self._log_buf_objs.clear()

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        """Push theme colours to widget styles directly (Textual blocks dynamic CSS)."""
        t = self._tui_theme
        screen = self.screen
        screen.styles.background = t.bg
        try:
            footer = self.query_one(Footer)
            footer.styles.background = t.surface
            footer.styles.color = t.dim
        except Exception:
            pass
        try:
            header = self.query_one(Header)
            header.styles.background = t.surface
            header.styles.color = t.accent
        except Exception:
            pass
        for pane_id in ("static-pane", "chat-pane"):
            try:
                log = self.query_one(f"#{pane_id}", RichLog)
                log.styles.background = t.bg
                log.styles.color = t.text
            except Exception:
                pass
        try:
            input_area = self.query_one("#input-area", Container)
            input_area.styles.background = t.bg
        except Exception:
            pass
        try:
            inp = self.query_one("#input", TextArea)
            inp.styles.background = t.bg
            inp.styles.color = t.text
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Mount
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        theme_key = os.environ.get("MINI_AGENT_THEME", DEFAULT_THEME).lower()
        self._tui_theme = THEMES.get(theme_key, THEMES[DEFAULT_THEME])

        workspace = resolve_workspace()
        cli = parse_args()
        data = init_session(workspace, cli_args=cli)
        self.config = data["config"]
        self.config.verbose = "--quiet" not in sys.argv
        self.write_gate = data["write_gate"]
        self.read_gate = data["read_gate"]
        self.memory = data["memory"]
        self.messages = data["messages"]
        self.session = data["session"]

        # --- Start WebSocket server for Electron UI ---
        try:
            ws_server.start()
            _ws_agent_id = "orchestrator"
            ws_server.emit_graph_init(workspace)
        except Exception:
            pass  # WebSocket is optional

        t = self._tui_theme
        tools_log = self.query_one("#tools-log", RichLog)
        tools_log.write(f"[bold {t.accent}]mini_agent[/]  —  {self.config.model}")
        tools_log.write(f"[{t.dim}]Workspace: {_safe(workspace)}[/]")
        if saved := len(self.messages) - 2:
            tools_log.write(f"[{t.dim}]Restored {saved} messages from previous session[/]")
        tools_log.write(f"[{t.dim}]Theme: {t.name}  (/theme to switch)[/]")

        # Cache widget refs for fast drain-loop access (avoid query_one DOM walks)
        self._chat = self.query_one("#chat-pane", RichLog)
        self._tools_log = tools_log

        # Cache footer ref to avoid query_one DOM walk every 2s
        self._footer = self.query_one(Footer)

        # Flat task_id → tree node map for O(1) status updates (avoid recursive tree walks)
        self._tree_node_map: dict[str, object] = {}
        # Pending children whose parent hasn't arrived yet (race condition)
        self._pending_children: dict[str, list] = {}
        # Last assistant response for clipboard copy
        self._last_response: str = ""
        self.query_one("#input", TextArea).focus()
        self.queue: Queue = Queue()
        self.worker: AgentWorker | None = None
        self._buf = ""
        self._chat_buf = ""
        self._tools_buf = ""
        self._thinking_buf = ""
        self._thinking_flush_pos = 0
        self._in_thinking = False
        self._turn_finished = True
        self._history: list[str] = []
        self._history_pos: int = 0
        self._active_tool: str = ""
        self._total_turns: int = 0
        self._total_tokens: int = 0
        self._git_branch: str = ""
        self._git_dirty: bool = False
        self._approval_active: bool = False
        self._turn_id: int = 0

        # Wire TUI queue for sub-agent streaming
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT.__dict__["_tui_queue"] = self.queue

        self._apply_theme()
        self._refresh_git_status()
        self.set_interval(1/60, self._drain)          # 60 fps token drain (battery-friendly)
        self.set_interval(30.0, self._update_status_bar)
        self.set_interval(0.5, self._poll_ws_inbox)    # Check Electron UI messages

    # ------------------------------------------------------------------
    # Status bar — stolen from Agent Terminal
    # ------------------------------------------------------------------

    def _refresh_git_status(self) -> None:
        """Read git branch and dirty status for the workspace."""
        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.config.workspace, capture_output=True, text=True, timeout=3,
            )
            self._git_branch = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.workspace, capture_output=True, text=True, timeout=3,
            )
            self._git_dirty = bool(r2.stdout.strip())
        except Exception:
            self._git_branch = ""
            self._git_dirty = False

    def _poll_ws_inbox(self) -> None:
        """Check for messages from the Electron UI via WebSocket."""
        try:
            from ws_server import ui_inbox
            event_type, data = ui_inbox.get_nowait()
            if event_type == "ui.send_message":
                user_input = data.get("text", "").strip()
            elif event_type == "ui.click_node":
                file_path = data.get("file_path", "")
                user_input = f"Can you look at {file_path} and tell me what it does?"
            else:
                return

            if user_input:
                input_widget = self.query_one("#input", TextArea)
                input_widget.text = user_input
                self._submit()
        except Exception:
            pass  # Queue empty or other transient error

    def _update_status_bar(self) -> None:
        """Refresh the Footer with live metrics every 2 seconds."""
        footer = self._footer
        parts = []
        if self._git_branch:
            dirty = "*" if self._git_dirty else ""
            parts.append(f"⎇ {self._git_branch}{dirty}")
        if self._active_tool:
            parts.append(f"[tool] {self._active_tool}")
        if self._total_turns:
            parts.append(f"↻ turn {self._total_turns}")
        if self._total_tokens:
            tok = f"{self._total_tokens / 1000:.1f}k" if self._total_tokens >= 1000 else str(self._total_tokens)
            parts.append(f"⬡ {tok}")
        parts.append(self.config.model)
        label = " │ ".join(parts) if parts else self.config.model
        footer._label = label
        if self._approval_active:
            footer.add_class("pulse")
        else:
            footer.remove_class("pulse")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_shell(self) -> None:
        """Suspend the TUI and drop the user into their $SHELL.  Exit the shell
        (Ctrl+D or 'exit') to resume the TUI.

        Kills any running agent worker and its subprocesses first to release
        terminal control, then suspends cleanly so the shell has exclusive
        /dev/tty access."""
        import os as _os
        # Kill the hanging worker so its subprocesses release /dev/tty
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
        # Also kill the active subprocess directly (handles sudo /dev/tty reads)
        from tools import _TOOL_CONTEXT
        ap = getattr(_TOOL_CONTEXT, "_active_proc", None)
        if ap is not None:
            try:
                ap.kill()
            except Exception:
                pass
        shell = _os.environ.get("SHELL", "/bin/sh")
        with self.suspend():
            _os.system(shell)

    def action_suspend_process(self) -> None:
        """Kill worker + subprocess, then let Textual's native SIGTSTP
        handler do the terminal restore + SIGSTOP suspend."""
        import signal, os as _os
        # Kill the hanging worker so its subprocesses release /dev/tty
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
        # Also kill the active subprocess directly
        from tools import _TOOL_CONTEXT
        ap = getattr(_TOOL_CONTEXT, "_active_proc", None)
        if ap is not None:
            try:
                ap.kill()
            except Exception:
                pass
        # Let Textual's native SIGTSTP handler (suspend_application_mode
        # + SIGSTOP) restore the terminal and suspend cleanly.
        _os.kill(_os.getpid(), signal.SIGTSTP)

    def action_cancel(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
            self._turn_id += 1
            self.worker = None
            self._turn_finished = True
            self._flush_buf()
            self._thinking_buf = ""
            self._thinking_flush_pos = 0
            self._in_thinking = False
            self.messages = self.memory.save(self.messages)
            log = self.query_one("#chat-pane", RichLog)
            t = self._tui_theme
            log.write(f"[{t.yellow}]  ╼ Cancelled.[/]")
            self.query_one("#input", TextArea).focus()
            self._active_tool = ""
            self._approval_active = False

    def action_quit(self) -> None:
        """Save conversation before quitting (Ctrl+Q)."""
        self.messages = self.memory.save(self.messages)
        self.exit()

    def action_copy(self) -> None:
        """Copy last response to clipboard (Ctrl+Shift+C).
        
        RichLog panes don't support native mouse text selection, so
        this copies from a dedicated buffer that tracks the last
        assistant response.
        """
        try:
            import pyperclip
        except ImportError:
            self.notify(
                "pyperclip not installed — run: pip install pyperclip",
                severity="error",
                timeout=4,
            )
            return
        text = getattr(self, "_last_response", "")
        if text:
            pyperclip.copy(text)
            self.notify(f"Copied {len(text)} chars", timeout=1.5)
        else:
            self.notify("Nothing to copy yet", severity="warning", timeout=2)

    def action_submit(self) -> None:
        """Submit: Enter key — send TextArea content to agent."""
        focused = self.focused
        if isinstance(focused, TextArea) and focused.id == "input":
            self._submit()

    def on_key(self, event) -> None:
        """Handle Shift+Enter (newline) and Up/Down (history) in TextArea."""
        focused = self.focused
        if not isinstance(focused, TextArea) or focused.id != "input":
            return

        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            focused.insert("\n")

        elif event.key == "up" and not focused.text:
            event.stop()
            event.prevent_default()
            if self._history and self._history_pos > 0:
                self._history_pos -= 1
                focused.text = self._history[self._history_pos]

        elif event.key == "down" and not focused.text:
            event.stop()
            event.prevent_default()
            if self._history_pos < len(self._history) - 1:
                self._history_pos += 1
                focused.text = self._history[self._history_pos]
            else:
                self._history_pos = len(self._history)
                focused.text = ""

    def _approve(self, tool_name: str, args: dict) -> bool:
        """Auto-approve in TUI. User sees tool calls and can cancel with Ctrl+C."""
        log = self.query_one("#tools-log", RichLog)
        t = self._tui_theme
        brief = str(args)
        if len(brief) > 80:
            brief = brief[:80] + "..."
        log.write(f"[{t.yellow} italic]  ⏳ approved {tool_name}({_safe(brief)})[/]")
        return True

    def _export_to_file(self, path: str) -> None:
        """Write current conversation to a markdown file."""
        from memory import export_conversation_markdown
        md = export_conversation_markdown(self.messages)
        ok, reason = self.write_gate.check(path)
        if not ok:
            self.notify(f"Export blocked: {reason}", severity="error")
            return
        with open(path, "w") as f:
            f.write(md)

    def _submit(self) -> None:
        """Send user message to the agent."""
        # Guard against double-submit while agent is working
        # Instead of silently dropping the message, push it as an interjection
        # so the agent sees it at its next tool-call boundary.
        if self.worker is not None and self.worker.is_alive():
            input_widget = self.query_one("#input", TextArea)
            text = input_widget.text.strip()
            if text:
                from interject import push_interjection
                push_interjection(text)
                input_widget.clear()
                # Show confirmation to the user
                t = self._tui_theme
                chat = self.query_one("#chat-pane", RichLog)
                chat.write(f"[{t.purple} bold]  💬 queued:[/] [{t.purple}]{_safe(text[:120])}[/]")
            return

        # Defensive: clear any stale table buffer from a previous turn
        if hasattr(self, "_table_buf"):
            self._table_buf = []
        # Clear stale sub-agent panes and buffers
        if hasattr(self, "_sub_bufs"):
            self._sub_bufs.clear()
        if hasattr(self, "_sub_panes"):
            sap = self.query_one("#subagent-pane", HorizontalScroll)
            for child in sap.query(RichLog):
                child.remove()
            self._sub_panes.clear()
            self._sub_count = 0
            sap.styles.display = "none"

            # Clear agent tree for new conversation
            tree = self.query_one("#agent-tree", Tree)
            tree.clear()
            self._tree_node_map.clear()
            self._pending_children.clear()

        input_widget = self.query_one("#input", TextArea)
        text = input_widget.text.strip()
        if not text:
            return
        input_widget.clear()

        # Special commands
        if text.startswith("/"):
            self._handle_command(text)
            return

        self.messages.append({"role": "user", "content": text})
        self._history.append(text)
        self._history_pos = len(self._history)
        t = self._tui_theme
        chat = self.query_one("#chat-pane", RichLog)
        chat.write("")
        self._box_open(chat, "You", t.accent)
        self._box_line(chat, _safe(text), t.green)
        self._box_close(chat, t.accent)
        self._flush_logs()

        self._buf = ""
        self._thinking_buf = ""
        self._thinking_flush_pos = 0
        self._in_thinking = False
        self._turn_finished = False
        self._active_tool = ""

        self._turn_id += 1
        self.worker = AgentWorker(
            self.messages, self.config,
            self.write_gate, self.read_gate,
            self.queue,
            self.session,
            approve_callback=self._approve if self.config.approve_write_ops else None,
            turn_id=self._turn_id,
        )
        self.worker.start()

    def _handle_command(self, text: str) -> None:
        """Handle slash-commands typed in the input area."""
        cmd = text.lower().strip()
        t = self._tui_theme
        log = self.query_one("#tools-log", RichLog)

        if cmd == "/clear":
            self.messages = [
                {"role": "system", "content": build_system_prompt(self.config)},
                {"role": "system", "content": build_startup_context(self.config.workspace)},
            ]
            self.memory.clear()
            self._history = []
            self._history_pos = 0
            self._total_turns = 0
            self._total_tokens = 0
            log.write("")
            log.write(f"[{t.dim}]— conversation cleared —[/]")
            return

        if cmd == "/help":
            log.write("")
            log.write(f"[{t.dim}]Commands:[/]")
            log.write(f"[{t.dim}]  /clear     Reset conversation memory[/]")
            log.write(f"[{t.dim}]  /export    Write conversation to a markdown file[/]")
            log.write(f"[{t.dim}]  /help      Show this help[/]")
            log.write(f"[{t.dim}]  /init      Reinitialize .mini_agent.rules + .mini_agent.toml[/]")
            log.write(f"[{t.dim}]  /shell     Drop to a real shell (Ctrl+D/exit to return)[/]")
            log.write(f"[{t.dim}]  /theme     Switch theme (dawn, sepia, ember, slate, midnight, cobalt, neon, forest)[/]")
            log.write(f"[{t.dim}]  /session   Manage sessions (new | switch | delete | list)[/]")
            log.write(f"[{t.dim}]  /stats     Show session stats[/]")
            log.write(f"[{t.dim}]  /workspace Switch to a different workspace directory[/]")
            return

        if cmd == "/stats":
            log.write(f"[{t.dim}]Session: {len(self.messages)} msgs, {self._total_turns} turns, "
                      f"{self._total_tokens} tokens, {self.config.model}[/]")
            return

        if cmd.startswith("/session"):
            parts = cmd.split(maxsplit=2)
            sub = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""
            from config import list_sessions, switch_session, delete_session
            ws = self.config.workspace
            if sub == "list":
                sessions = list_sessions(ws)
                if sessions:
                    log.write(f"[{t.dim}]Sessions: {', '.join(sessions)}[/]")
                else:
                    log.write(f"[{t.dim}]No saved sessions found.[/]")
            elif sub == "new" and arg:
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                log.write(f"[{t.green}]Created and switched to session '{arg}'.[/]")
            elif sub == "switch" and arg:
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                log.write(f"[{t.green}]Switched to session '{arg}'.[/]")
            elif sub == "delete" and arg:
                ok, msg = delete_session(ws, arg)
                log.write(f"[{t.dim}]{msg}[/]")
            else:
                log.write(f"[{t.yellow}]Usage: /session new <name> | switch <name> | delete <name> | list[/]")
            self.query_one("#input", TextArea).focus()
            return

        if cmd == "/export":
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"conversation_{ts}.md"
            path = os.path.join(self.config.workspace, fname)
            self._export_to_file(path)
            log.write(f"[{t.dim}]Exported to {fname}[/]")
            self.query_one("#input", TextArea).focus()
            return

        if cmd.startswith("/theme"):
            parts = cmd.split(None, 1)
            theme_name = parts[1].strip().lower() if len(parts) > 1 else ""
            if theme_name in THEMES:
                self._tui_theme = THEMES[theme_name]
                self._apply_theme()
                log.write(f"[{t.green}]Theme switched to {self._tui_theme.name}[/]")
            else:
                names = ", ".join(THEMES.keys())
                log.write(f"[{t.yellow}]Available themes: {names}[/]")
                log.write(f"[{t.dim}]Usage: /theme <name>[/]")
            self.query_one("#input", TextArea).focus()
            return

        if cmd.startswith("/workspace"):
            parts = text.split(maxsplit=1)
            new_path = parts[1].strip() if len(parts) > 1 else ""
            if not new_path:
                log.write(f"[{t.yellow}]Usage: /workspace <path>[/]")
                self.query_one("#input", TextArea).focus()
                return
            new_workspace = os.path.abspath(new_path)
            if not os.path.isdir(new_workspace):
                log.write(f"[{t.yellow}]Not a directory: {new_workspace}[/]")
                self.query_one("#input", TextArea).focus()
                return
            # Save current session, then reinitialize at new workspace
            self.messages = self.memory.save(self.messages)
            self.memory.close()
            from config import init_session as _init_session
            try:
                new_data = _init_session(new_workspace)
            except Exception as exc:
                log.write(f"[{t.red}]Error switching workspace: {exc}[/]")
                self.query_one("#input", TextArea).focus()
                return
            self.config = new_data["config"]
            self.config.verbose = "--quiet" not in sys.argv
            self.write_gate = new_data["write_gate"]
            self.read_gate = new_data["read_gate"]
            self.memory = new_data["memory"]
            self.messages = new_data["messages"]
            self.session.close()
            self.session = new_data["session"]
            # Reset UI state
            self.worker = None
            self._buf = ""
            self._chat_buf = ""
            self._tools_buf = ""
            self._thinking_buf = ""
            self._thinking_flush_pos = 0
            self._in_thinking = False
            self._turn_finished = True
            self._active_tool = ""
            self._total_turns = 0
            self._total_tokens = 0
            self._turn_id += 1
            self._history = []
            self._history_pos = 0
            self.query_one("#input", TextArea).focus()
            self._refresh_git_status()
            log.write(f"[{t.green}]Workspace switched to: {_safe(new_workspace)}[/]")
            chat = self.query_one("#chat-pane", RichLog)
            chat.write(f"[{t.dim}]\u2500 workspace changed \u2500[/]")
            return

        if cmd == "/init":
            from tools.file_ops import _init_rules
            rg = ReadSafetyGate(self.config.workspace)
            result = _init_rules({}, None, rg)
            log.write(f"[{t.dim}]{_safe(result.content)}[/]")
            self.query_one("#input", TextArea).focus()
            return

        if cmd == "/shell":
            self.action_shell()
            self.query_one("#input", TextArea).focus()
            return

        log.write(f"[{t.yellow}]Unknown command: {text}[/]")

    # ------------------------------------------------------------------
    # Drain queue (called on-demand via _NotifyQueue.put)
    # ------------------------------------------------------------------

    def _drain(self) -> None:
        """Pull messages off the queue and route to the correct pane."""
        if self.queue.empty():
            return
        t = self._tui_theme
        chat = self._chat
        tools_log = self._tools_log
        try:
            while True:
                msg = self.queue.get_nowait()

                # Sub-agent streaming (checked first — tuples, not dataclass instances)
                if isinstance(msg, tuple) and len(msg) == 3 and msg[0] == "sub_token":
                    _tag, task_id, text = msg
                    if not hasattr(self, "_sub_panes"):
                        self._sub_panes = {}
                        self._sub_count = 0
                    if task_id not in self._sub_panes:
                        sap = self.query_one("#subagent-pane", HorizontalScroll)
                        sap.styles.display = "block"
                        self._sub_count += 1
                        rlog = RichLog(highlight=True, markup=True, wrap=True, max_lines=12)
                        color = _AGENT_COLORS[(self._sub_count - 1) % len(_AGENT_COLORS)]
                        ac = getattr(t, color)
                        if not hasattr(self, "_sub_colors"):
                            self._sub_colors = {}
                        self._sub_colors[task_id] = ac
                        # Agent name in top border
                        rlog.border_title = f"{color} Agent {self._sub_count} ({task_id[:8]}...)"
                        self._write_to_log(rlog, f"[{ac}]Agent {self._sub_count}  ({task_id})[/]")
                        sap.mount(rlog)
                        self._sub_panes[task_id] = rlog
                    sublog = self._sub_panes[task_id]
                    if not hasattr(self, "_sub_bufs"):
                        self._sub_bufs = {}
                    buf = self._sub_bufs.get(task_id, "")
                    buf += text
                    ac = self._sub_colors[task_id]
                    for line in buf.split("\n")[:-1]:
                        if line:
                            self._write_to_log(sublog, f"[{ac}][/] {_safe(line)}")
                    self._sub_bufs[task_id] = buf.split("\n")[-1]
                    continue

                # Sub-agent tree: spawn event
                if isinstance(msg, tuple) and len(msg) >= 5 and msg[0] == "sub_tree" and msg[1] == "spawn":
                    _tag, _action, _task_id, _parent_id = msg[0], msg[1], msg[2], msg[3]
                    _name = msg[4] if len(msg) > 4 else _task_id
                    _desc = msg[5] if len(msg) > 5 else ""
                    # Dedup: if this task_id already has a tree node, skip
                    if hasattr(self, "_tree_node_map") and _task_id in self._tree_node_map:
                        continue
                    tree = self.query_one("#agent-tree", Tree)
                    label = f"[RUN] {_name}"
                    parent_node = tree.root
                    if _parent_id and _parent_id in self._tree_node_map:
                        parent_node = self._tree_node_map[_parent_id]
                    elif _parent_id:
                        # Parent not yet in tree (race: child spawn msg arrived first)
                        self._pending_children.setdefault(_parent_id, []).append(
                            (_task_id, _name, _desc)
                        )
                        continue
                    node = parent_node.add(label)
                    node.data = {"id": _task_id, "label": _name, "desc": _desc}
                    self._tree_node_map[_task_id] = node
                    tree.root.expand()
                    parent_node.expand()
                    # Only show tree AFTER a real node is added
                    tree.styles.display = "block"
                    # Attach any children that arrived before this parent
                    for child_id, child_name, child_desc in self._pending_children.pop(_task_id, []):
                        child_node = node.add(f"[RUN] {child_name}")
                        child_node.data = {"id": child_id, "label": child_name, "desc": child_desc}
                        self._tree_node_map[child_id] = child_node
                        node.expand()
                    continue

                # Sub-agent tree: status update
                if isinstance(msg, tuple) and len(msg) >= 4 and msg[0] == "sub_tree" and msg[1] == "status":
                    _tag, _action, _task_id, _status = msg[0], msg[1], msg[2], msg[3]
                    tree = self.query_one("#agent-tree", Tree)
                    node = self._tree_node_map.get(_task_id)
                    if node is not None:
                        ol = str(node.label)
                        for old_tag in ("[RUN]", "[OK]", "[ERR]"):
                            if ol.startswith(old_tag + " "):
                                ol = ol[len(old_tag)+1:]
                                break
                        if _status == "running":
                            node.set_label(f"[RUN] {ol}")
                        elif _status == "completed":
                            node.set_label(f"[OK] {ol}")
                        else:
                            node.set_label(f"[ERR] {ol}")
                    # Keep tree visible even when all done (so user can see structure).
                    continue

                # Sub-agent tool streaming to tools-log
                if isinstance(msg, tuple) and len(msg) >= 3 and msg[0] == "sub_tool":
                    _tag, _action = msg[0], msg[1]
                    name = msg[2] if len(msg) > 2 else "?"
                    if _action == "start":
                        task_id = msg[3] if len(msg) > 3 else ""
                        # Dedup: skip if this sub-agent already has an open tool box
                        if not hasattr(self, "_active_sub_tools"):
                            self._active_sub_tools = set()
                        key = (task_id, name)
                        if key in self._active_sub_tools:
                            continue
                        self._active_sub_tools.add(key)
                        # Find color for this agent
                        ac = t.dim
                        if hasattr(self, "_sub_colors") and task_id in self._sub_colors:
                            ac = self._sub_colors[task_id]
                        label = task_id[:8] if task_id else "sub"
                        self._box_open(tools_log, _safe(f"[{label}] {name}"), ac)
                    elif _action == "end":
                        ok = msg[3] if len(msg) > 3 else True
                        detail = msg[4] if len(msg) > 4 else ""
                        task_id_e = msg[3] if len(msg) > 3 else ""
                        name_e = msg[2] if len(msg) > 2 else "?"
                        if hasattr(self, "_active_sub_tools"):
                            self._active_sub_tools.discard((task_id_e, name_e))
                        symbol = "✓" if ok else "✗"
                        color = t.green if ok else t.red
                        self._box_close(tools_log, color, f"{symbol} {_safe(detail[:60])}")
                    continue

                # Sub-agent done — hide its pane
                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "sub_done":
                    _tag, task_id = msg
                    if hasattr(self, "_sub_panes") and task_id in self._sub_panes:
                        sublog = self._sub_panes.pop(task_id)
                        sublog.remove()
                        self._sub_colors.pop(task_id, None)
                        self._sub_bufs.pop(task_id, None)
                    continue

                if isinstance(msg, _TokenMsg):
                    self._handle_token(msg, chat)
                elif isinstance(msg, _ToolStart):
                    if msg.turn_id != self._turn_id:
                        continue
                    self._flush_buf()
                    if self._in_thinking:
                        # Close thinking box before opening tool box
                        remaining = self._thinking_buf[self._thinking_flush_pos:].strip()
                        if remaining:
                            self._box_line(chat, f"[dim]{_safe(remaining)}[/]", f"dim {t.thinking}")
                        self._box_close(chat, f"dim {t.thinking}")
                        self._in_thinking = False
                        self._thinking_buf = ""
                        self._thinking_flush_pos = 0
                    self._close_agent_box()
                    self._active_tool = msg.summary.split("(")[0].strip() if "(" in msg.summary else msg.summary[:20]
                    self._box_open(tools_log, _safe(f"[tool] {msg.summary}"), t.yellow)
                elif isinstance(msg, _ToolEnd):
                    if msg.turn_id != self._turn_id:
                        continue
                    symbol = "✓" if msg.ok else "✗"
                    color = t.green if msg.ok else t.red
                    detail = _safe(msg.detail)
                    if len(detail) > 120:
                        detail = detail[:120] + "..."
                    self._box_close(tools_log, color, f"{symbol} {detail}")
                    self._active_tool = ""
                    # Render diff_preview if present
                    if msg.diff_preview:
                        self._box_open(tools_log, "diff", t.accent)
                        for line in msg.diff_preview.split("\n"):
                            self._box_line(tools_log, _safe(line), t.dim)
                        self._box_close(tools_log, t.accent)
                elif isinstance(msg, _ToolOutput):
                    if msg.turn_id != self._turn_id:
                        continue
                    pass  # Tool start/end already summarize; suppress raw output clutter
                elif isinstance(msg, _Error):
                    self._close_agent_box()
                    self._box_open(chat, "✗ Error", t.red)
                    self._box_line(chat, _safe(msg.msg), t.red)
                    self._box_close(chat, t.red)
                elif isinstance(msg, _Done):
                    # Ignore stale _Done from a cancelled/previous turn
                    if msg.turn_id != self._turn_id:
                        continue
                    self._finish_turn(usage=msg.usage, turn_count=msg.turn_count)
                    return

        except Empty:
            pass

        self._flush_logs()

        # Worker finished without sending Done (cancelled or crashed)
        if not self._turn_finished and (self.worker is None or not self.worker.is_alive()):
            self._finish_turn()

    def _close_agent_box(self) -> None:
        """Close the agent content box if it's currently open."""
        if getattr(self, "_agent_box_open", False):
            # Capture last response for clipboard
            acc = getattr(self, "_accumulated_content", [])
            if acc:
                self._last_response = "\n".join(acc)
                self._accumulated_content = []
            try:
                chat = self._chat
                t = self._tui_theme
                self._box_close(chat, t.accent)
            except Exception:
                pass
            self._agent_box_open = False

    def _handle_token(self, msg: _TokenMsg, log) -> None:
        """Process a single token: route to thinking or content buffer.

        Thinking is flushed on sentence boundaries (period-space, newlines)
        or every ~400 chars.  Content is flushed on newlines or every ~300
        chars at word boundaries.  Visual separators mark thinking blocks.
        """
        t = self._tui_theme
        text = msg.text

        if text.startswith(THINKING_START):
            self._flush_buf()  # safety: flush any pending content first
            self._close_agent_box()
            self._close_agent_box()
            self._in_thinking = True
            self._thinking_buf = ""
            self._thinking_flush_pos = 0
            self._box_open(log, "thinking", f"dim {t.thinking}")
            return

        if text == THINKING_END:
            self._in_thinking = False
            # Flush remaining thinking
            remaining = self._thinking_buf[self._thinking_flush_pos:].strip()
            if remaining:
                self._box_line(log, f"[dim]{_safe(remaining)}[/]", f"dim {t.thinking}")
            self._thinking_buf = ""
            self._thinking_flush_pos = 0
            self._box_close(log, f"dim {t.thinking}")
            return

        if self._in_thinking:
            self._thinking_buf += text
            # Flush on sentence boundaries or ~400-char chunks
            while True:
                buf = self._thinking_buf
                pos = self._thinking_flush_pos
                remaining = buf[pos:]

                # Find the best natural break: newline, then sentence end
                best = -1
                best_len = 0

                # Newline within 400 chars
                nl = remaining.find("\n")
                if nl != -1 and nl < 400:
                    best = nl + 1  # include the newline
                    best_len = nl
                else:
                    # Sentence ending within 400 chars: period/question/exclamation + space
                    for sep in (". ", "? ", "! ", ":\n"):
                        idx = remaining.find(sep)
                        if idx != -1 and idx < 400 and (best == -1 or idx < best_len):
                            # Skip ". " when preceded by a digit (e.g. "1. item")
                            if sep == ". " and idx > 0 and remaining[idx - 1].isdigit():
                                continue
                            best = idx + len(sep)
                            best_len = idx

                if best != -1:
                    chunk = remaining[:best].rstrip()
                    self._thinking_flush_pos = pos + best
                    if chunk:
                        self._box_line(log, _safe(chunk), f"dim {t.thinking}")
                    continue

                # No natural break — flush at ~400 chars on a space
                if len(remaining) >= 400:
                    cut = 400
                    space = remaining.rfind(" ", 250, 400)
                    if space != -1:
                        cut = space + 1
                    chunk = remaining[:cut].rstrip()
                    self._thinking_flush_pos = pos + cut
                    if chunk:
                        self._box_line(log, _safe(chunk), f"dim {t.thinking}")
                    continue

                break  # not enough to flush
            return

        # Content — open agent box if not yet open
        if not getattr(self, "_agent_box_open", False):
            self._box_open(log, "Agent", t.accent)
            self._agent_box_open = True

        # Content — accumulate, flush complete lines or ~300-char chunks
        self._buf += text
        # Buffer table rows to flush as monospace code block
        if not hasattr(self, "_table_buf"):
            self._table_buf: list[str] = []
        # Track accumulated content for final promotion to static pane
        if not hasattr(self, "_accumulated_content"):
            self._accumulated_content: list[str] = []
        while True:
            nl = self._buf.find("\n")
            if nl != -1 and nl < 300:
                line = self._buf[:nl].rstrip()
                self._buf = self._buf[nl + 1:]
                if line:
                    stripped = line.strip()
                    is_table = (stripped.startswith("|") and stripped.endswith("|")
                                and "|" in stripped[1:-1])
                    is_sep = (stripped.startswith("|") and all(c in "| -:" for c in stripped))
                    if is_table or is_sep:
                        self._table_buf.append(line)
                    else:
                        # Flush any buffered table first
                        if self._table_buf:
                            self._box_line(log, r"\[code\]\n" + "\n".join(_safe(l) for l in self._table_buf) + r"\n\[/code\]", t.accent)
                            self._table_buf = []
                        self._box_line(log, _safe(line), t.accent)
                        self._accumulated_content.append(line)
                        self._flush_logs()
                continue

            # No newline soon — flush at ~300 chars on a space boundary
            if len(self._buf) >= 300:
                cut = 300
                space = self._buf.rfind(" ", 200, 300)
                if space != -1:
                    cut = space + 1
                chunk = self._buf[:cut].rstrip()
                self._buf = self._buf[cut:]
                if chunk:
                    self._box_line(log, _safe(chunk), t.accent)
                    self._accumulated_content.append(chunk)
                    self._flush_logs()
                continue

            break

    def _finish_turn(self, usage: dict | None = None, turn_count: int = 0) -> None:
        """Commit buffers, close boxes, promote final response to static pane."""
        t = self._tui_theme
        chat = self._chat
        static = self._tools_log

        # Flush any remaining table buffer then regular buf
        if hasattr(self, "_table_buf") and self._table_buf:
            if not getattr(self, "_agent_box_open", False):
                self._box_open(chat, "Agent", t.accent)
                self._agent_box_open = True
            self._box_line(chat, r"\[code\]\n" + "\n".join(_safe(l) for l in self._table_buf) + r"\n\[/code\]", t.accent)
            self._table_buf = []

        self._flush_buf()
        self._in_thinking = False
        self._thinking_buf = ""
        self._thinking_flush_pos = 0
        self._buf = ""
        self._active_tool = ""
        self._approval_active = False

        # Close the agent box in chat pane
        self._close_agent_box()

        # Promote final content to static pane in a box
        accumulated = getattr(self, "_accumulated_content", [])
        if accumulated:
            self._box_open(self._tools_log, "Agent", t.accent)
            for line in accumulated:
                self._box_line(self._tools_log, _safe(line), t.accent)
            self._box_close(self._tools_log, t.accent)
        self._accumulated_content = []

        self.messages = self.memory.save(self.messages)
        self.worker = None
        self._turn_finished = True
        self.query_one("#input", TextArea).focus()
        if turn_count:
            self._total_turns = turn_count
        if usage and usage.get("total_tokens"):
            self._total_tokens += usage["total_tokens"]
        n = sum(1 for m in self.messages if m["role"] != "system")
        parts = [f"{n} msgs"]
        if self._total_tokens:
            tok = f"{self._total_tokens / 1000:.1f}k" if self._total_tokens >= 1000 else str(self._total_tokens)
            parts.append(f"{tok} tok")
        if turn_count > 1:
            parts.append(f"turn {turn_count}")
        parts.append(self.config.model)
        self._write_to_log(chat, f"[{t.dim}]— {' │ '.join(parts)}[/]")
        self._flush_logs()

    def _flush_buf(self) -> None:
        if self._buf.strip():
            chat = self._chat
            t = self._tui_theme
            # Ensure agent box is open
            if not getattr(self, "_agent_box_open", False):
                self._box_open(chat, "Agent", t.accent)
                self._agent_box_open = True
            line = self._buf.rstrip()
            self._box_line(chat, _safe(line), t.accent)
            # Track for promotion
            if not hasattr(self, "_accumulated_content"):
                self._accumulated_content: list[str] = []
            self._accumulated_content.append(line)
        self._buf = ""
        # Defensive: clear stale table buffer
        if hasattr(self, "_table_buf"):
            self._table_buf = []


if __name__ == "__main__":
    app = MiniAgentTUI()
    app.run()
