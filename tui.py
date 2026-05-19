#!/usr/bin/env python3
"""
tui.py — Textual TUI frontend for mini_agent.

Usage: python tui.py [--workspace PATH] [--quiet] [--stream] [--allow-overwrites] [--approve]

Layout (left to right):
  #left-pane (45%)        #right-pane (55%)
    tools-log (RichLog)     chat-view (VerticalScroll)
    agent-tree (Tree)         └─ user messages (Markdown, tinted bg)
    subagent-pane              └─ assistant messages (Markdown, green border)
    response-md (Markdown)
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
from queue import Queue, Empty
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.widgets import Header, Footer, RichLog, TextArea, Tree, Markdown, Static

# Re-exported for test compatibility (CSS handles most styling now, but RichLog
# tool output still uses markup escaping for user content)
def _safe(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", r"\[")

from textual.binding import Binding
from textual import on
from textual.message import Message

import requests

from config import AgentConfig, resolve_workspace, init_session, parse_args, build_startup_context
from api import APIError
from llm import run_agent_turn
from stream import THINKING_START, THINKING_END
from prompt import build_system_prompt
from safety import ReadSafetyGate, WriteSafetyGate
from memory import MemoryStore
from tools import set_context, build_symbol_index


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TuiTheme:
    name: str
    bg: str
    surface: str
    border: str
    accent: str
    text: str
    dim: str
    green: str
    yellow: str
    red: str
    thinking: str
    pulse: str
    purple: str

THEMES: dict[str, TuiTheme] = {
    "dawn": TuiTheme("Dawn",
        bg="#faf8f5", surface="#f0ede8", border="#d4cfc8",
        accent="#b8956a", text="#3d3a35", dim="#8a857d",
        green="#5a8a4a", yellow="#b89540", red="#c06050",
        thinking="#b0aaa0", pulse="#f0c060", purple="#a080c0"),
    "sepia": TuiTheme("Sepia",
        bg="#f4f0e6", surface="#e8e0d0", border="#c8b898",
        accent="#b8893a", text="#4a3f30", dim="#8a7a60",
        green="#6a8a4a", yellow="#c0a040", red="#b85840",
        thinking="#b0a080", pulse="#e0b040", purple="#9a7ab0"),
    "ember": TuiTheme("Ember",
        bg="#1e1814", surface="#2a221c", border="#3a3028",
        accent="#d4985a", text="#d0c8be", dim="#7a7064",
        green="#7ab860", yellow="#d4a040", red="#d47050",
        thinking="#5a5040", pulse="#e89840", purple="#c090d0"),
    "slate": TuiTheme("Slate",
        bg="#111111", surface="#1b1b1b", border="#2a2a2a",
        accent="#8f8f8f", text="#b8b8b8", dim="#5a5a5a",
        green="#4f9f6f", yellow="#b89a4a", red="#a85a5a",
        thinking="#3a3a3a", pulse="#c0c040", purple="#8a7ab0"),
    "midnight": TuiTheme("Midnight",
        bg="#090b0d", surface="#131619", border="#1e2226",
        accent="#8899aa", text="#b0c0d0", dim="#4a5560",
        green="#4a8a6a", yellow="#9a8a4a", red="#9a6060",
        thinking="#2a3040", pulse="#6a8acc", purple="#7a8ab0"),
    "cobalt": TuiTheme("Cobalt",
        bg="#0a1220", surface="#101830", border="#1e2850",
        accent="#6090d0", text="#a0b8d8", dim="#4a6090",
        green="#5a9a6a", yellow="#a0a040", red="#b06060",
        thinking="#203050", pulse="#5090e0", purple="#8090d0"),
    "neon": TuiTheme("Neon",
        bg="#0c0c0c", surface="#16161a", border="#303030",
        accent="#e040e0", text="#c0e0c0", dim="#506050",
        green="#00e060", yellow="#e0c000", red="#ff4060",
        thinking="#302040", pulse="#e040ff", purple="#c040ff"),
    "forest": TuiTheme("Forest",
        bg="#0e1410", surface="#141c16", border="#1e2e22",
        accent="#60a870", text="#a0c0a8", dim="#4a6a50",
        green="#60d070", yellow="#b0b040", red="#c06050",
        thinking="#203028", pulse="#50d060", purple="#8090b0"),
    "dracula": TuiTheme("Dracula",
        bg="#282a36", surface="#1e1f29", border="#44475a",
        accent="#bd93f9", text="#f8f8f2", dim="#6272a4",
        green="#50fa7b", yellow="#f1fa8c", red="#ff5555",
        thinking="#44475a", pulse="#ff79c6", purple="#bd93f9"),
}

DEFAULT_THEME = "slate"
_AGENT_COLORS = ["green", "yellow", "accent", "pulse", "red"]


def _build_css(theme: TuiTheme) -> str:
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

#main-area {{
    height: 1fr;
    min-height: 6;
}}

#left-pane {{
    width: 42%;
    background: {theme.bg};
    border-right: solid {theme.border};
}}

#right-pane {{
    width: 1fr;
    background: {theme.bg};
}}

#tools-log {{
    background: {theme.bg};
    color: {theme.text};
    border: none;
    padding: 0 1;
    height: 1fr;
    overflow-y: auto;
    scrollbar-size: 0 0;
}}

#agent-tree {{
    display: block;
    background: {theme.bg};
    color: {theme.dim};
    border: none;
    border-top: solid {theme.border};
    padding: 0 1;
    height: auto;
    max-height: 10;
    min-height: 0;
    overflow-y: auto;
    scrollbar-size: 0 0;
}}

#subagent-pane {{
    background: {theme.bg};
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

#chat-view {{
    background: {theme.bg};
    padding: 1 2 0 2;
    scrollbar-size: 0 0;
}}

MsgUser {{
    background: {theme.text} 5%;
    color: {theme.text};
    margin: 0 0 1 8;
    padding: 0 2;
    border-left: solid {theme.accent};
}}

MsgAgent {{
    background: {theme.green} 5%;
    color: {theme.text};
    margin: 0 8 1 0;
    padding: 0 2;
    border-left: solid {theme.green};
}}

MsgThinking {{
    color: {theme.thinking};
    margin: 0 8 1 0;
    padding: 0 2;
}}

MsgError {{
    background: {theme.red} 10%;
    color: {theme.text};
    margin: 0 8 1 0;
    padding: 0 2;
    border-left: solid {theme.red};
}}

#response-md {{
    background: {theme.bg};
    color: {theme.text};
    border: none;
    border-top: solid {theme.border};
    padding: 0 1;
    height: auto;
    max-height: 50%;
    overflow-y: auto;
    scrollbar-size: 0 0;
    display: none;
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
"""


# ---------------------------------------------------------------------------
# Custom message widgets for chat-view
# ---------------------------------------------------------------------------

class MsgUser(Markdown):
    """User message with accent left-border, tinted background."""

class MsgAgent(Markdown):
    """Assistant message with green left-border, tinted background."""

class MsgThinking(Static):
    """Dim thinking text block."""

class MsgError(Markdown):
    """Error message with red left-border."""


# ---------------------------------------------------------------------------
# Queue messages (unchanged protocol)
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
    """A token of streaming output from a sub-agent. (Kept for test compat.)"""
    task_id: str
    text: str

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

    def __init__(self, messages, config, write_gate, read_gate, out: Queue, session,
                 approve_callback=None, turn_id: int = 0):
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
                on_tool_start=lambda s, parallel=False: self.out.put(
                    _ToolStart(s, parallel, turn_id=self.turn_id)),
                on_tool_end=lambda ok, d, diff_preview=None: self.out.put(
                    _ToolEnd(ok, d, turn_id=self.turn_id, diff_preview=diff_preview)),
                on_tool_output=lambda line: self.out.put(
                    _ToolOutput(line, turn_id=self.turn_id)),
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
        Binding("ctrl+l", "clear_pane", "Clear Chat"),
        Binding("ctrl+h", "help_overlay", "Help"),
        Binding("question_mark", "help_overlay", "Help", show=False),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="left-pane"):
                yield RichLog(id="tools-log", highlight=True, markup=True, wrap=True)
                yield Tree("agent", id="agent-tree")
                with HorizontalScroll(id="subagent-pane"):
                    pass
            with Vertical(id="right-pane"):
                yield VerticalScroll(id="chat-view")
                yield Markdown("", id="response-md")
        with Container(id="input-area"):
            yield TextArea("", id="input")
        yield Footer()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        """Rebuild CSS with new theme and refresh."""
        self.CSS = _build_css(self._tui_theme)
        self.refresh_css()

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

        t = self._tui_theme
        tools_log = self.query_one("#tools-log", RichLog)
        tools_log.write(f"[bold {t.accent}]mini_agent[/]  \u2014  {self.config.model}")
        tools_log.write(f"[{t.dim}]Workspace: {workspace}[/]")
        if saved := len(self.messages) - 2:
            tools_log.write(f"[{t.dim}]Restored {saved} messages[/]")
        tools_log.write(f"[{t.dim}]Theme: {t.name}  (/theme to switch)[/]")

        self._chat_view = self.query_one("#chat-view", VerticalScroll)
        self._tools_log = tools_log
        self._footer = self.query_one(Footer)
        self._response_md = self.query_one("#response-md", Markdown)

        self._tree_node_map: dict[str, object] = {}
        self._pending_children: dict[str, list] = {}
        self._last_response: str = ""

        import time as _time
        self._session_start: float = _time.monotonic()

        self.query_one("#input", TextArea).focus()
        self.queue: Queue = Queue()
        self.worker: AgentWorker | None = None
        self._thinking_buf = ""
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
        self._current_response: Markdown | None = None
        self._current_response_text: str = ""

        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT.__dict__["_tui_queue"] = self.queue

        self._apply_theme()
        self._refresh_git_status()
        self.set_interval(1/60, self._drain)
        self.set_interval(2.0, self._update_status_bar)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    _SPINNER_FRAMES = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834",
                       "\u2826", "\u2827", "\u2807", "\u280f"]

    def _show_spinner(self) -> None:
        self._spinner_frame = 0
        self._spinner_timer = self.set_interval(1/8, self._animate_spinner)

    def _animate_spinner(self) -> None:
        if not hasattr(self, "_spinner_frame"):
            return
        frame = self._SPINNER_FRAMES[self._spinner_frame % len(self._SPINNER_FRAMES)]
        self._spinner_frame += 1
        # Show spinner in status bar via update
        self._update_status_bar(spinner_frame=frame)

    def _hide_spinner(self) -> None:
        if hasattr(self, "_spinner_timer"):
            self._spinner_timer.stop()
            del self._spinner_timer
        self._update_status_bar()

    def _refresh_git_status(self) -> None:
        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.config.workspace, capture_output=True, text=True, timeout=3)
            self._git_branch = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.workspace, capture_output=True, text=True, timeout=3)
            self._git_dirty = bool(r2.stdout.strip())
        except Exception:
            self._git_branch = ""
            self._git_dirty = False

    def _update_status_bar(self, spinner_frame: str | None = None) -> None:
        parts = []
        if spinner_frame:
            parts.append(f"{spinner_frame} thinking")
        if self._git_branch:
            dirty = "*" if self._git_dirty else ""
            parts.append(f"\u2387 {self._git_branch}{dirty}")
        if self._active_tool:
            parts.append(f"[tool] {self._active_tool}")
        if self._total_turns:
            parts.append(f"\u21bb turn {self._total_turns}")
        if self._total_tokens:
            tok = f"{self._total_tokens / 1000:.1f}k" if self._total_tokens >= 1000 else str(self._total_tokens)
            parts.append(f"\u2b21 {tok}")
        import time as _time
        elapsed = _time.monotonic() - self._session_start
        if elapsed >= 3600:
            parts.append(f"{elapsed/3600:.1f}h")
        elif elapsed >= 60:
            parts.append(f"{int(elapsed/60)}m{int(elapsed%60)}s")
        else:
            parts.append(f"{int(elapsed)}s")
        parts.append(self.config.model)
        self._footer._label = " \u2502 ".join(parts)
        if self._approval_active:
            self._footer.add_class("pulse")
        else:
            self._footer.remove_class("pulse")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_pane(self) -> None:
        for child in list(self._chat_view.children):
            child.remove()
        self._thinking_buf = ""
        self._in_thinking = False
        self._current_response = None
        self._current_response_text = ""
        self._response_md.styles.display = "none"
        self._response_md.update("")

    def action_help_overlay(self) -> None:
        t = self._tui_theme
        log = self._tools_log
        log.write("")
        log.write(f"[bold {t.accent}]Keyboard Shortcuts[/]")
        for key, desc in [
            ("Ctrl+C", "Cancel agent"), ("Ctrl+Q", "Quit"),
            ("Ctrl+Z", "Suspend to shell"), ("Ctrl+Shift+C", "Copy last response"),
            ("Ctrl+L", "Clear chat pane"), ("Ctrl+H / ?", "Show this help"),
            ("Enter", "Submit message"), ("Shift+Enter", "Newline"),
            ("Up/Down", "Browse history (empty input)"),
        ]:
            log.write(f"[{t.dim}]  {key:<16} {desc}[/]")
        log.write("")
        log.write(f"[bold {t.accent}]Commands[/]")
        for cmd, desc in [
            ("/clear", "Reset conversation"), ("/export", "Write to markdown"),
            ("/help", "Show commands"), ("/init", "Reinitialize rules+toml"),
            ("/shell", "Drop to shell"), (f"/theme <name>", f"Themes: {', '.join(THEMES)}"),
            ("/session <cmd>", "new | switch | delete | list"),
            ("/stats", "Session stats"), ("/workspace <path>", "Switch workspace"),
        ]:
            log.write(f"[{t.dim}]  {cmd:<18} {desc}[/]")

    def action_shell(self) -> None:
        import os as _os
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
        from tools import _TOOL_CONTEXT
        ap = getattr(_TOOL_CONTEXT, "_active_proc", None)
        if ap is not None:
            try: ap.kill()
            except Exception: pass
        shell = _os.environ.get("SHELL", "/bin/sh")
        with self.suspend():
            _os.system(shell)

    def action_suspend_process(self) -> None:
        import signal, os as _os
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
        from tools import _TOOL_CONTEXT
        ap = getattr(_TOOL_CONTEXT, "_active_proc", None)
        if ap is not None:
            try: ap.kill()
            except Exception: pass
        _os.kill(_os.getpid(), signal.SIGTSTP)

    def action_cancel(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel.set()
            self._turn_id += 1
            self.worker = None
            self._turn_finished = True
            self._in_thinking = False
            self._thinking_buf = ""
            self.messages = self.memory.save(self.messages)
            self._hide_spinner()
            self.query_one("#input", TextArea).focus()
            self._active_tool = ""
            self._approval_active = False

    def action_quit(self) -> None:
        self.messages = self.memory.save(self.messages)
        self.exit()

    def action_copy(self) -> None:
        try:
            import pyperclip
        except ImportError:
            self.notify("pip install pyperclip", severity="error", timeout=4)
            return
        text = getattr(self, "_last_response", "")
        if text:
            pyperclip.copy(text)
            self.notify(f"Copied {len(text)} chars", timeout=1.5)
        else:
            self.notify("Nothing to copy yet", severity="warning", timeout=2)

    def action_submit(self) -> None:
        focused = self.focused
        if isinstance(focused, TextArea) and focused.id == "input":
            self._submit()

    def on_key(self, event) -> None:
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
        return True  # Auto-approve in TUI

    def _export_to_file(self, path: str) -> None:
        from memory import export_conversation_markdown
        md = export_conversation_markdown(self.messages)
        ok, reason = self.write_gate.check(path)
        if not ok:
            self.notify(f"Export blocked: {reason}", severity="error")
            return
        with open(path, "w") as f:
            f.write(md)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def _submit(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            input_widget = self.query_one("#input", TextArea)
            text = input_widget.text.strip()
            if text:
                from interject import push_interjection
                push_interjection(text)
                input_widget.clear()
                t = self._tui_theme
                self._chat_view.mount(Static(
                    f"  [bold {t.purple}]\U0001f4ac queued:[/] [{t.purple}]{text[:120]}[/]"))
                self._chat_view.scroll_end(animate=False)
            return

        # Clear stale sub-agent panes
        if hasattr(self, "_sub_bufs"):
            self._sub_bufs.clear()
        if hasattr(self, "_sub_panes"):
            sap = self.query_one("#subagent-pane", HorizontalScroll)
            for child in sap.query(RichLog):
                child.remove()
            self._sub_panes.clear()
            self._sub_count = 0
            sap.styles.display = "none"
            tree = self.query_one("#agent-tree", Tree)
            tree.clear()
            self._tree_node_map.clear()
            self._pending_children.clear()

        input_widget = self.query_one("#input", TextArea)
        text = input_widget.text.strip()
        if not text:
            return
        input_widget.clear()

        if text.startswith("/"):
            self._handle_command(text)
            return

        self.messages.append({"role": "user", "content": text})
        self._history.append(text)
        self._history_pos = len(self._history)

        # Mount user message widget
        self._chat_view.mount(MsgUser(text))
        self._chat_view.scroll_end(animate=False)

        self._thinking_buf = ""
        self._in_thinking = False
        self._turn_finished = False
        self._active_tool = ""

        self._turn_id += 1
        self._show_spinner()
        self.worker = AgentWorker(
            self.messages, self.config,
            self.write_gate, self.read_gate,
            self.queue, self.session,
            approve_callback=self._approve if self.config.approve_write_ops else None,
            turn_id=self._turn_id,
        )
        self.worker.start()

    def _handle_command(self, text: str) -> None:
        cmd = text.lower().strip()
        t = self._tui_theme
        log = self._tools_log

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
            log.write(f"[{t.dim}]--- conversation cleared ---[/]")
            return

        if cmd == "/help":
            for line in [
                "/clear     Reset conversation memory",
                "/export    Write conversation to markdown",
                "/help      Show this help",
                "/init      Reinitialize rules + toml",
                "/shell     Drop to real shell",
                "/theme     Switch theme",
                "/session   new | switch | delete | list",
                "/stats     Show session stats",
                "/workspace Switch workspace",
            ]:
                log.write(f"[{t.dim}]{line}[/]")
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
                log.write(f"[{t.dim}]Sessions: {', '.join(sessions) if sessions else 'none'}[/]")
            elif sub == "new" and arg:
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                log.write(f"[{t.green}]Created session '{arg}'.[/]")
            elif sub == "switch" and arg:
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                log.write(f"[{t.green}]Switched to '{arg}'.[/]")
            elif sub == "delete" and arg:
                ok, msg = delete_session(ws, arg)
                log.write(f"[{t.dim}]{msg}[/]")
            else:
                log.write(f"[{t.yellow}]Usage: /session new <name> | switch <name> | delete <name> | list[/]")
            return

        if cmd == "/export":
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"conversation_{ts}.md"
            path = os.path.join(self.config.workspace, fname)
            self._export_to_file(path)
            log.write(f"[{t.dim}]Exported to {fname}[/]")
            return

        if cmd.startswith("/theme"):
            parts = cmd.split(None, 1)
            theme_name = parts[1].strip().lower() if len(parts) > 1 else ""
            if theme_name in THEMES:
                self._tui_theme = THEMES[theme_name]
                self._apply_theme()
                os.environ["MINI_AGENT_THEME"] = theme_name
                log.write(f"[{t.green}]Theme: {self._tui_theme.name}[/]")
            else:
                log.write(f"[{t.yellow}]Available: {', '.join(THEMES)}[/]")
            return

        if cmd.startswith("/workspace"):
            parts = text.split(maxsplit=1)
            new_path = parts[1].strip() if len(parts) > 1 else ""
            if not new_path:
                log.write(f"[{t.yellow}]Usage: /workspace <path>[/]")
                return
            new_workspace = os.path.abspath(new_path)
            if not os.path.isdir(new_workspace):
                log.write(f"[{t.yellow}]Not a directory: {new_workspace}[/]")
                return
            self.messages = self.memory.save(self.messages)
            self.memory.close()
            from config import init_session as _init_session
            try:
                new_data = _init_session(new_workspace)
            except Exception as exc:
                log.write(f"[{t.red}]Error: {exc}[/]")
                return
            self.config = new_data["config"]
            self.config.verbose = "--quiet" not in sys.argv
            self.write_gate = new_data["write_gate"]
            self.read_gate = new_data["read_gate"]
            self.memory = new_data["memory"]
            self.messages = new_data["messages"]
            self.session.close()
            self.session = new_data["session"]
            self.worker = None
            self._thinking_buf = ""
            self._in_thinking = False
            self._turn_finished = True
            self._active_tool = ""
            self._total_turns = 0
            self._total_tokens = 0
            self._turn_id += 1
            self._history = []
            self._history_pos = 0
            self._refresh_git_status()
            log.write(f"[{t.green}]Workspace: {new_workspace}[/]")
            return

        if cmd == "/init":
            from tools.file_ops import _init_rules
            from safety import ReadSafetyGate
            rg = ReadSafetyGate(self.config.workspace)
            result = _init_rules({}, None, rg)
            log.write(f"[{t.dim}]{result.content}[/]")
            return

        if cmd == "/shell":
            self.action_shell()
            return

        log.write(f"[{t.yellow}]Unknown command: {text}[/]")

    # ------------------------------------------------------------------
    # Drain queue
    # ------------------------------------------------------------------

    _BATCH_SIZE = 8  # drain at most this many messages per tick for visible streaming

    def _drain(self) -> None:
        if self.queue.empty():
            return
        try:
            for _ in range(self._BATCH_SIZE):
                msg = self.queue.get_nowait()

                if isinstance(msg, tuple):
                    if msg[0] == "sub_token" and len(msg) == 3:
                        self._drain_sub_token(*msg[1:])
                        continue
                    if msg[0] == "sub_tree":
                        if msg[1] == "spawn" and len(msg) >= 5:
                            self._drain_sub_tree_spawn(msg)
                            continue
                        if msg[1] == "status" and len(msg) >= 4:
                            self._drain_sub_tree_status(msg)
                            continue
                    if msg[0] == "sub_tool" and len(msg) >= 3:
                        self._drain_sub_tool(msg)
                        continue
                    if msg[0] == "sub_done" and len(msg) == 2:
                        self._drain_sub_done(msg)
                        continue

                if isinstance(msg, _TokenMsg):
                    self._drain_token(msg)
                elif isinstance(msg, _ToolStart):
                    self._drain_tool_start(msg)
                elif isinstance(msg, _ToolEnd):
                    self._drain_tool_end(msg)
                elif isinstance(msg, _ToolOutput):
                    pass
                elif isinstance(msg, _Error):
                    self._drain_error(msg)
                elif isinstance(msg, _Done):
                    if msg.turn_id == self._turn_id:
                        self._finish_turn(usage=msg.usage, turn_count=msg.turn_count)
                        return
        except Empty:
            pass

        if not self._turn_finished and (self.worker is None or not self.worker.is_alive()):
            self._finish_turn()

    # --- Token handler ---

    def _drain_token(self, msg: _TokenMsg) -> None:
        text = msg.text
        if text.startswith(THINKING_START):
            self._in_thinking = True
            self._thinking_buf = ""
            return
        if text == THINKING_END:
            self._in_thinking = False
            if self._thinking_buf.strip():
                self._chat_view.mount(MsgThinking(self._thinking_buf.strip()))
                self._chat_view.scroll_end(animate=False)
            self._thinking_buf = ""
            return
        if self._in_thinking:
            self._thinking_buf += text
            return

        # Streaming content — update (or create) current MsgAgent widget
        self._current_response_text += text
        if self._current_response is None:
            self._current_response = MsgAgent("")
            self._chat_view.mount(self._current_response)
        self._current_response.update(self._current_response_text)
        self._chat_view.scroll_end(animate=False)

    # --- Tool handlers ---

    def _drain_tool_start(self, msg: _ToolStart) -> None:
        if msg.turn_id != self._turn_id:
            return
        self._active_tool = msg.summary.split("(")[0].strip() if "(" in msg.summary else msg.summary[:20]
        t = self._tui_theme
        self._tools_log.write(f"[{t.yellow}][tool] {msg.summary}[/]")

    def _drain_tool_end(self, msg: _ToolEnd) -> None:
        if msg.turn_id != self._turn_id:
            return
        t = self._tui_theme
        symbol = "\u2713" if msg.ok else "\u2717"
        color = t.green if msg.ok else t.red
        detail = msg.detail
        if len(detail) > 120:
            detail = detail[:120] + "..."
        self._tools_log.write(f"[{color}]{symbol} {detail}[/]")
        self._active_tool = ""
        if msg.diff_preview:
            self._tools_log.write(f"[{t.dim}]--- diff ---[/]")
            for line in msg.diff_preview.split("\n")[:30]:
                self._tools_log.write(f"[{t.dim}]{line}[/]")

    def _drain_error(self, msg: _Error) -> None:
        self._chat_view.mount(MsgError(f"**Error:** {msg.msg}"))
        self._chat_view.scroll_end(animate=False)

    # --- Sub-agent handlers ---

    def _drain_sub_token(self, task_id: str, text: str) -> None:
        if not hasattr(self, "_sub_panes"):
            self._sub_panes = {}
            self._sub_count = 0
        if task_id not in self._sub_panes:
            sap = self.query_one("#subagent-pane", HorizontalScroll)
            sap.styles.display = "block"
            self._sub_count += 1
            rlog = RichLog(highlight=True, markup=True, wrap=True, max_lines=12)
            color = _AGENT_COLORS[(self._sub_count - 1) % len(_AGENT_COLORS)]
            ac = getattr(self._tui_theme, color)
            if not hasattr(self, "_sub_colors"):
                self._sub_colors = {}
            self._sub_colors[task_id] = ac
            rlog.border_title = f"{color} Agent {self._sub_count} ({task_id[:8]}...)"
            rlog.write(f"[{ac}]Agent {self._sub_count}  ({task_id})[/]")
            sap.mount(rlog)
            self._sub_panes[task_id] = rlog
        sublog = self._sub_panes[task_id]
        if not hasattr(self, "_sub_bufs"):
            self._sub_bufs = {}
        buf = self._sub_bufs.get(task_id, "") + text
        ac = self._sub_colors[task_id]
        for line in buf.split("\n")[:-1]:
            if line:
                sublog.write(f"[{ac}][/] {line}")
        self._sub_bufs[task_id] = buf.split("\n")[-1]

    def _drain_sub_tree_spawn(self, msg: tuple) -> None:
        _, _, task_id, parent_id = msg[0], msg[1], msg[2], msg[3]
        name = msg[4] if len(msg) > 4 else task_id
        desc = msg[5] if len(msg) > 5 else ""
        if hasattr(self, "_tree_node_map") and task_id in self._tree_node_map:
            return
        t = self._tui_theme
        tree = self.query_one("#agent-tree", Tree)
        label = f"[{t.yellow}]\u25b6 {name}[/]"
        parent_node = tree.root
        if parent_id and parent_id in self._tree_node_map:
            parent_node = self._tree_node_map[parent_id]
        elif parent_id:
            self._pending_children.setdefault(parent_id, []).append((task_id, name, desc))
            return
        node = parent_node.add(label)
        node.data = {"id": task_id, "label": name, "desc": desc}
        self._tree_node_map[task_id] = node
        tree.root.expand()
        parent_node.expand()
        tree.styles.display = "block"
        for child_id, child_name, child_desc in self._pending_children.pop(task_id, []):
            child_node = node.add(f"[{t.yellow}]\u25b6 {child_name}[/]")
            child_node.data = {"id": child_id, "label": child_name, "desc": child_desc}
            self._tree_node_map[child_id] = child_node
            node.expand()

    def _drain_sub_tree_status(self, msg: tuple) -> None:
        _, _, task_id, status = msg[0], msg[1], msg[2], msg[3]
        node = self._tree_node_map.get(task_id)
        if node is None:
            return
        t = self._tui_theme
        ol = str(node.label)
        for old_tag in (f"[{t.yellow}]\u25b6 ", f"[{t.green}]\u2713 ", f"[{t.red}]\u2717 "):
            if old_tag in ol:
                ol = ol.replace(old_tag, "")
                break
        if status == "running":
            node.set_label(f"[{t.yellow}]\u25b6 {ol}[/]")
        elif status == "completed":
            node.set_label(f"[{t.green}]\u2713 {ol}[/]")
        else:
            node.set_label(f"[{t.red}]\u2717 {ol}[/]")

    def _drain_sub_tool(self, msg: tuple) -> None:
        t = self._tui_theme
        _, action = msg[0], msg[1]
        name = msg[2] if len(msg) > 2 else "?"
        if action == "start":
            task_id = msg[3] if len(msg) > 3 else ""
            ac = t.dim
            if hasattr(self, "_sub_colors") and task_id in self._sub_colors:
                ac = self._sub_colors[task_id]
            label = task_id[:8] if task_id else "sub"
            self._tools_log.write(f"[{ac}][{label}] {name}[/]")
        elif action == "end":
            ok = msg[3] if len(msg) > 3 else True
            detail = msg[4] if len(msg) > 4 else ""
            symbol = "\u2713" if ok else "\u2717"
            color = t.green if ok else t.red
            self._tools_log.write(f"[{color}]{symbol} {detail[:60]}[/]")

    def _drain_sub_done(self, msg: tuple) -> None:
        _, task_id = msg
        if hasattr(self, "_sub_panes") and task_id in self._sub_panes:
            sublog = self._sub_panes.pop(task_id)
            sublog.remove()
            self._sub_colors.pop(task_id, None)
            self._sub_bufs.pop(task_id, None)

    # ------------------------------------------------------------------
    # Turn finish
    # ------------------------------------------------------------------

    def _finish_turn(self, usage: dict | None = None, turn_count: int = 0) -> None:
        self._in_thinking = False
        self._thinking_buf = ""
        self._active_tool = ""
        self._approval_active = False
        self._hide_spinner()
        self._refresh_git_status()

        # Save last response for clipboard (already streamed into chat-view MsgAgent)
        if self._current_response_text:
            self._last_response = self._current_response_text

        self._current_response = None
        self._current_response_text = ""
        self.messages = self.memory.save(self.messages)
        self.worker = None
        self._turn_finished = True
        self.query_one("#input", TextArea).focus()
        if turn_count:
            self._total_turns = turn_count
        if usage and usage.get("total_tokens"):
            self._total_tokens += usage["total_tokens"]


if __name__ == "__main__":
    app = MiniAgentTUI()
    app.run()
