#!/usr/bin/env python3
"""
tui_pt.py — prompt-toolkit TUI frontend for mini_agent.

Usage: python tui_pt.py [--workspace PATH] [--quiet]

prompt-toolkit was chosen over Textual because it natively supports
transparent backgrounds (Float(transparent=True)) and has no internal
assertions that crash on transparent widget backgrounds.

Layout (top to bottom):
  Header
  ─────────────────────────────────────────
  ╭─ Tools & Thinking ─╮ │ ╭─ Chat ───────╮
  │ ...                 │ │ │ ...          │
  ╰─────────────────────╯ │ ╰──────────────╯
  ─────────────────────────────────────────
  ╭─ Input ───────────────────────────────╮
  │ > _                                    │
  ╰────────────────────────────────────────╯
  ─────────────────────────────────────────
  Status bar
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
import queue

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    HSplit, VSplit, Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea as PTTextArea
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers.python import PythonLexer
from pygments.style import Style as PygmentsStyle
from pygments.token import Token

import re as _re
import traceback
from datetime import datetime, timezone

from config import (
    resolve_workspace, init_session, parse_args,
    build_startup_context, list_sessions, switch_session, delete_session,
)
from llm import run_agent_turn
from stream import THINKING_START, THINKING_END
from safety import ReadSafetyGate, WriteSafetyGate
from api import clear_api_cache
from prompt import build_system_prompt

# Pre-compiled regex for stripping ANSI escape sequences (used in _strip_ansi).
_ANSI_RE = _re.compile(
    r'\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[PX^_].*?\x1b\\'
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rounded box-drawing characters (Unicode)
BOX_TL = "\u256d"  # ╭
BOX_TR = "\u256e"  # ╮
BOX_BL = "\u2570"  # ╰
BOX_BR = "\u256f"  # ╯
BOX_H  = "\u2500"  # ─
BOX_V  = "\u2502"  # │

# ---------------------------------------------------------------------------
# Theme — Catppuccin Mocha (same palette as tui.py)
# ---------------------------------------------------------------------------

THEME = {
    "bg":           "#1e1e2e",
    "surface":      "#313244",
    "border":       "#45475a",
    "accent":       "#89b4fa",
    "text":         "#cdd6f4",
    "dim":          "#6c7086",
    "green":        "#a6e3a1",
    "yellow":       "#f9e2af",
    "red":          "#f38ba8",
    "thinking":     "#585b70",
    "pulse":        "#cba6f7",
}

# ---------------------------------------------------------------------------
# Monochrome Pygments style — greyscale syntax highlighting
# ---------------------------------------------------------------------------

# Shades of grey from Catppuccin Mocha palette
_GREY_LIGHT = "#bac2de"   # light grey for keywords, strings
_GREY_MID   = "#9399b2"   # mid grey for names, builtins
_GREY_DIM   = "#6c7086"   # dim grey for comments, operators
_GREY_BOLD  = "#cdd6f4"   # bold grey (white-ish) for emphasis


class MonochromeStyle(PygmentsStyle):
    """Pygments style using only shades of grey."""
    background_color = THEME["bg"]
    styles = {
        Token.Comment:             f"italic {_GREY_DIM}",
        Token.Keyword:             f"bold {_GREY_LIGHT}",
        Token.Keyword.Constant:    f"bold {_GREY_LIGHT}",
        Token.Keyword.Declaration: f"bold {_GREY_LIGHT}",
        Token.Keyword.Namespace:   f"bold {_GREY_LIGHT}",
        Token.Keyword.Type:        f"bold {_GREY_LIGHT}",
        Token.Name:                _GREY_MID,
        Token.Name.Builtin:        f"bold {_GREY_MID}",
        Token.Name.Function:       _GREY_BOLD,
        Token.Name.Class:          f"bold {_GREY_BOLD}",
        Token.Name.Decorator:      _GREY_DIM,
        Token.Name.Exception:      _GREY_BOLD,
        Token.String:              _GREY_LIGHT,
        Token.String.Doc:          f"italic {_GREY_DIM}",
        Token.Number:              _GREY_LIGHT,
        Token.Operator:            _GREY_DIM,
        Token.Punctuation:         _GREY_DIM,
        Token.Literal:             _GREY_LIGHT,
    }

STYLE = Style.from_dict({
    "header":       f"fg:{THEME['dim']}",
    "footer":       f"fg:{THEME['thinking']}",
    "status":       f"fg:{THEME['thinking']}",
    "border":       f"fg:{THEME['border']}",
    "line":         f"fg:{THEME['border']}",
    "accent":       f"fg:{THEME['dim']}",
    "green":        f"fg:{THEME['dim']}",
    "yellow":       f"fg:{THEME['thinking']}",
    "red":          f"fg:{THEME['red']}",
    "thinking":     f"fg:{THEME['thinking']}",
    "dim":          f"fg:{THEME['thinking']}",
    "text":         f"fg:{THEME['text']}",
    "input":        f"fg:{THEME['text']}",
    "input-focus":  f"fg:{THEME['text']}",
    "pulse":        f"fg:{THEME['dim']}",
    "msg-user":     f"fg:{THEME['accent']}",
    "msg-agent":    f"fg:{THEME['text']}",
    "msg-error":    f"fg:{THEME['red']}",
    "msg-thinking": f"fg:{THEME['thinking']}",
    # TextArea styling
    "textarea":     f"fg:{THEME['text']}",
    # Monochrome Pygments syntax highlighting (greyscale)
    "pygments.comment":              f"italic {_GREY_DIM}",
    "pygments.keyword":              f"bold {_GREY_LIGHT}",
    "pygments.keyword.constant":     f"bold {_GREY_LIGHT}",
    "pygments.keyword.declaration":  f"bold {_GREY_LIGHT}",
    "pygments.keyword.namespace":    f"bold {_GREY_LIGHT}",
    "pygments.keyword.type":         f"bold {_GREY_LIGHT}",
    "pygments.name":                 _GREY_MID,
    "pygments.name.builtin":         f"bold {_GREY_MID}",
    "pygments.name.function":        _GREY_BOLD,
    "pygments.name.class":           f"bold {_GREY_BOLD}",
    "pygments.name.decorator":       _GREY_DIM,
    "pygments.name.exception":       _GREY_BOLD,
    "pygments.literal.string":       _GREY_LIGHT,
    "pygments.literal.string.doc":   f"italic {_GREY_DIM}",
    "pygments.literal.number":       _GREY_LIGHT,
    "pygments.operator":             _GREY_DIM,
    "pygments.punctuation":          _GREY_DIM,
    "pygments.literal":              _GREY_LIGHT,
})


# ---------------------------------------------------------------------------
# Error trace logging — captures full context when errors hit the chat window
# ---------------------------------------------------------------------------

_ERROR_LOG_PATH: str = "error_traces.log"
_ERROR_LOG_MAX_MESSAGES: int = 20  # last N messages to include in trace
_ERROR_LOG_LOCK: threading.Lock = threading.Lock()  # protects concurrent error logging


def _log_error_trace(
    error: Exception,
    messages: list[dict],
    *,
    turn_id: int = 0,
) -> None:
    """Log an error with full conversation context to error_traces.log."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        tb = traceback.format_exc()
        # Snapshot last N messages (strip image content to keep readable)
        recent = messages[-_ERROR_LOG_MAX_MESSAGES:]
        msg_lines = []
        for i, m in enumerate(recent):
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            if isinstance(content, list):
                # multimodal content — extract text parts only
                content = " | ".join(
                    p.get("text", "[image]") if isinstance(p, dict) else str(p)
                    for p in content
                )
            if len(content) > 500:
                content = content[:500] + "..."
            tool_calls = m.get("tool_calls", [])
            tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            tc_str = f" [tools: {', '.join(tc_names)}]" if tc_names else ""
            msg_lines.append(f"  [{i}] {role}{tc_str}: {content}")

        entry = (
            f"\n{'=' * 80}\n"
            f"ERROR TRACE — {ts} — turn #{turn_id}\n"
            f"{'=' * 80}\n"
            f"Error: {error}\n"
            f"{'─' * 60}\n"
            f"Traceback:\n{tb}\n"
            f"{'─' * 60}\n"
            f"Last {len(recent)} messages:\n"
            + "\n".join(msg_lines)
            + f"\n{'─' * 60}\n"
        )
        with _ERROR_LOG_LOCK:
            with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(entry)
    except Exception:
        pass  # never let logging itself crash the agent


def _log_tool_error(
    tool_name: str,
    error_content: str,
    messages: list[dict],
    *,
    turn_id: int = 0,
    diff_preview: str | None = None,
) -> None:
    """Log a tool failure with conversation context to error_traces.log."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        recent = messages[-_ERROR_LOG_MAX_MESSAGES:]
        msg_lines = []
        for i, m in enumerate(recent):
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            if isinstance(content, list):
                content = " | ".join(
                    p.get("text", "[image]") if isinstance(p, dict) else str(p)
                    for p in content
                )
            if len(content) > 500:
                content = content[:500] + "..."
            tool_calls = m.get("tool_calls", [])
            tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            tc_str = f" [tools: {', '.join(tc_names)}]" if tc_names else ""
            msg_lines.append(f"  [{i}] {role}{tc_str}: {content}")

        entry = (
            f"\n{'=' * 80}\n"
            f"TOOL ERROR — {ts} — turn #{turn_id}\n"
            f"{'=' * 80}\n"
            f"Tool: {tool_name}\n"
            f"Error: {error_content}\n"
        )
        if diff_preview:
            entry += f"{'─' * 60}\nDiff preview:\n{diff_preview}\n"
        entry += (
            f"{'─' * 60}\n"
            f"Last {len(recent)} messages:\n"
            + "\n".join(msg_lines)
            + f"\n{'─' * 60}\n"
        )
        with _ERROR_LOG_LOCK:
            with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(entry)
    except Exception:
        pass  # never let logging itself crash the agent

# ---------------------------------------------------------------------------
# Chat buffer — thread-safe append-only text store
# ---------------------------------------------------------------------------

class ChatBuffer:
    """Thread-safe append-only text log.  Workers write here; the main
    thread syncs to TextArea widgets for display.

    Lines are stored as (style_class, text) tuples for styled rendering.
    If style_class is None/empty, the default style is used.
    """

    MAX_LINES = 2000

    def __init__(self):
        self._lines: list[tuple[str, str]] = []  # (style_class, text)
        self._lock = threading.Lock()
        self._dirty = False

    def append(self, text: str, style: str = ""):
        with self._lock:
            for line in text.split('\n'):
                self._lines.append((style, line))
            excess = len(self._lines) - self.MAX_LINES
            if excess > 0:
                self._lines = self._lines[excess:]
            self._dirty = True

    def append_last(self, text: str, style: str = ""):
        """Append text to the last line if same style (for streaming tokens)."""
        with self._lock:
            if self._lines and self._lines[-1][0] == style:
                prev_style, prev_text = self._lines[-1]
                self._lines[-1] = (prev_style, prev_text + text)
            else:
                self._lines.append((style, text))
            excess = len(self._lines) - self.MAX_LINES
            if excess > 0:
                self._lines = self._lines[excess:]
            self._dirty = True

    def clear(self):
        """Clear all lines (for reinitializing a per-task buffer)."""
        with self._lock:
            self._lines.clear()
            self._dirty = True

    def get_text(self) -> str:
        """Return full log as a single string without clearing the dirty flag."""
        with self._lock:
            return '\n'.join(text for _, text in self._lines)

    def consume_text(self) -> str:
        """Return full log as a single string and clear the dirty flag.
        Call only when syncing to display — never call twice in one cycle."""
        with self._lock:
            self._dirty = False
            return '\n'.join(text for _, text in self._lines)

    def get_formatted(self) -> "FormattedText":
        """Return styled FormattedText for use in FormattedTextControl."""
        with self._lock:
            return FormattedText([
                (f"class:{style}" if style else "", line + '\n')
                for style, line in self._lines
            ])

    @property
    def dirty(self) -> bool:
        with self._lock:
            return self._dirty


# ---------------------------------------------------------------------------
# Layout helpers — rounded borders
# ---------------------------------------------------------------------------

def _h_line() -> Window:
    return Window(height=1, char=BOX_H, style="class:border")


def rounded_frame(content, title: str | None = None, width=None) -> HSplit:
    """Wrap *content* in a rounded border (╭─╮│╰─╯) with optional title."""
    if title:
        top = VSplit([
            Window(width=1, height=1,
                   content=FormattedTextControl(BOX_TL),
                   style="class:border", dont_extend_width=True),
            Window(height=1, width=len(title) + 2,
                   content=FormattedTextControl(f" {title} "),
                   style="class:border", dont_extend_width=True),
            Window(height=1, char=BOX_H, style="class:border"),
            Window(width=1, height=1,
                   content=FormattedTextControl(BOX_TR),
                   style="class:border", dont_extend_width=True),
        ], height=1)
    else:
        top = VSplit([
            Window(width=1, height=1,
                   content=FormattedTextControl(BOX_TL),
                   style="class:border", dont_extend_width=True),
            Window(height=1, char=BOX_H, style="class:border"),
            Window(width=1, height=1,
                   content=FormattedTextControl(BOX_TR),
                   style="class:border", dont_extend_width=True),
        ], height=1)

    body = VSplit([
        Window(width=1, char=BOX_V, style="class:border", dont_extend_width=True),
        content,
        Window(width=1, char=BOX_V, style="class:border", dont_extend_width=True),
    ])

    bottom = VSplit([
        Window(width=1, height=1,
               content=FormattedTextControl(BOX_BL),
               style="class:border", dont_extend_width=True),
        Window(height=1, char=BOX_H, style="class:border"),
        Window(width=1, height=1,
               content=FormattedTextControl(BOX_BR),
               style="class:border", dont_extend_width=True),
    ], height=1)

    return HSplit([top, body, bottom], width=width)

# ---------------------------------------------------------------------------
# Worker thread — runs agent turn, writes to ChatBuffers
# ---------------------------------------------------------------------------

class AgentWorker(threading.Thread):
    """The ONE and only agent worker.  Lives for the lifetime of the TUI.

    Reads user messages from an input queue, runs run_agent_turn for each,
    and writes output to ChatBuffers.  There is never a second instance.
    """

    def __init__(self, messages, config, write_gate, read_gate,
                 session, memory_store,
                 chat_buf: ChatBuffer,
                 tools_buf: ChatBuffer,
                 thinking_buf: ChatBuffer):
        super().__init__(daemon=True)
        self.messages = messages
        self.config = config
        self.write_gate = write_gate
        self.read_gate = read_gate
        self.session = session
        self.memory_store = memory_store
        self.chat_buf = chat_buf
        self.tools_buf = tools_buf
        self.thinking_buf = thinking_buf
        self.cancel = threading.Event()
        self._in_thinking = False
        self.total_tokens = 0
        self.total_turns = 0
        self._turn_id = 0
        # Input queue — the ONE place user messages land.
        self._input_queue: queue.Queue[str] = queue.Queue()

    def submit(self, text: str) -> None:
        """Drop a user message into the input queue.  Thread-safe."""
        self._input_queue.put(text)

    def run(self):
        self.config.stream = True
        while True:
            # Block until the next user message arrives.
            first = self._input_queue.get()
            # Drain any additional messages that queued up while we were busy.
            texts = [first]
            while True:
                try:
                    texts.append(self._input_queue.get_nowait())
                except queue.Empty:
                    break
            text = "\n\n".join(texts)

            # Reset cancel for this turn.
            self.cancel.clear()

            self._turn_id += 1
            self.messages.append({"role": "user", "content": text})

            try:
                msg = run_agent_turn(
                    self.messages, self.config,
                    self.write_gate, self.read_gate,
                    on_token=self._on_token,
                    on_tool_start=self._on_tool_start,
                    on_tool_end=self._on_tool_end,
                    on_tool_output=self._on_tool_output,
                    cancel_event=self.cancel,
                    session=self.session,
                )
            except Exception as e:
                if not self.cancel.is_set():
                    self.chat_buf.append(f"Error: {e}", style="msg-error")
                    _log_error_trace(
                        e, self.messages, turn_id=self._turn_id,
                    )
                continue

            if self.cancel.is_set():
                continue

            if msg is not None:
                usage = msg.get("_total_usage") or {}
                self.total_tokens += usage.get("total_tokens", 0)
                self.total_turns += msg.get("_turn_count", 0)
                self.chat_buf.append("")  # blank line after agent output

            # Persist conversation after each turn.
            self.messages = self.memory_store.save(self.messages)

    # -- callbacks ----------------------------------------------------

    def _on_token(self, text: str):
        if text == THINKING_START:
            self._in_thinking = True
            return
        if text == THINKING_END:
            self._in_thinking = False
            self._needs_chat_newline = True
            return
        if self._in_thinking:
            self.thinking_buf.append_last(text)
        else:
            if getattr(self, '_needs_chat_newline', False):
                self.chat_buf.append("\n", style="msg-agent")
                self._needs_chat_newline = False
            self.chat_buf.append_last(text, style="msg-agent")

    def _on_tool_start(self, summary: str, parallel: bool = False):
        label = f"⚡ {summary}" if parallel else f"🔧 {summary}"
        self.tools_buf.append(label)

    def _on_tool_end(self, ok: bool, detail: str,
                     turn_id: int = 0, diff_preview=None):
        self.tools_buf.append(f"  {'OK' if ok else 'ERR'} {detail}")
        if not ok:
            # Extract tool name from detail: "read_file · /path" → "read_file"
            tool_name = detail.split(" · ")[0].strip() if " · " in detail else "?"
            _log_tool_error(
                tool_name, detail,
                self.messages,
                turn_id=self._turn_id,
                diff_preview=diff_preview,
            )

    def _on_tool_output(self, line: str, turn_id: int = 0):
        for sub in line.split('\n'):
            if sub.strip():
                self.tools_buf.append(f"    {sub}")


# ---------------------------------------------------------------------------
# Main TUI class
# ---------------------------------------------------------------------------

class MiniAgentTUI:
    """prompt-toolkit based TUI — transparent backgrounds, rounded borders,
    scrollable log areas."""

    def __init__(self):
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
        self.workspace = workspace

        # Thread-safe log buffers
        self.chat_buf = ChatBuffer()
        self.tools_buf = ChatBuffer()
        self.thinking_buf = ChatBuffer()
        self.subagents_buf = ChatBuffer()  # single scrolling log for all sub-agent activity

        # Startup info
        self.tools_buf.append(f"mini_agent — {self.config.model}")
        self.tools_buf.append(f"Workspace: {workspace}")
        if len(self.messages) > 2:
            self.tools_buf.append(f"Restored {len(self.messages) - 2} messages")

        # Input buffer
        self.input_buffer = Buffer(
            multiline=False,
            accept_handler=self._on_submit,
            enable_history_search=True,
        )

        # Sub-agent streaming queue (shared with tools via _TOOL_CONTEXT)
        self._subagent_queue = queue.Queue()
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._tui_queue = self._subagent_queue

        # The ONE agent worker — created once, lives forever.
        self.worker = AgentWorker(
            self.messages, self.config,
            self.write_gate, self.read_gate,
            self.session, self.memory,
            chat_buf=self.chat_buf,
            tools_buf=self.tools_buf,
            thinking_buf=self.thinking_buf,
        )
        self.worker.start()

        # Git status
        self._git_branch = ""
        self._git_dirty = False
        self._refresh_git_status()

        # Build TextArea widgets (will be referenced in layout and synced)
        self.tools_area: PTTextArea | None = None
        self.thinking_area: PTTextArea | None = None
        self._status_window: Window | None = None

        # Build app — refresh_interval + before_render handle streaming:
        # every 50ms the display syncs ChatBuffers → TextAreas and redraws.
        self.app = Application(
            layout=self._build_layout(),
            key_bindings=self._build_keybindings(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.05,
            before_render=self._sync_display,
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        # Header
        header = Window(
            content=FormattedTextControl(self._header_text),
            height=1,
            style="class:header",
        )

        # Tools log (left pane, top half)
        self.tools_area = PTTextArea(
            text="",
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            lexer=PygmentsLexer(PythonLexer),
            style="class:dim",
        )

        # Thinking log (left pane, bottom)
        self.thinking_area = PTTextArea(
            text="",
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            height=D(max=10),
            style="class:thinking",
        )

        # Sub-agents scrolling log (single area, no per-task dynamic widgets)
        self.subagents_area = PTTextArea(
            text="",
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            height=D(max=10),
            style="class:dim",
        )

        left_pane = rounded_frame(
            HSplit([self.tools_area, _h_line(), self.thinking_area,
                    _h_line(), Window(height=1, content=FormattedTextControl([('class:dim', ' Sub-agents')]), style='class:dim'),
                    self.subagents_area]),
            title="Tools & Thinking",
            width=D(weight=40),
        )

        # Chat view (right pane)
        self.chat_area = PTTextArea(
            text="",
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            style="class:text",
        )
        right_pane = rounded_frame(self.chat_area, title="Chat",
                                   width=D(weight=60))

        # Body: left pane | right pane (no vertical divider)
        body = VSplit([left_pane, right_pane])

        # Input
        input_window = Window(
            content=BufferControl(buffer=self.input_buffer, focusable=True),
            height=1,
            style="class:input",
        )
        input_frame = rounded_frame(input_window)

        # Status bar
        self._status_window = Window(
            content=FormattedTextControl(self._status_text),
            height=1,
            style="class:footer",
        )

        root = HSplit([
            header,
            _h_line(),
            body,
            input_frame,
            _h_line(),
            self._status_window,
        ], padding=0)

        return Layout(root, focused_element=input_window)

    # ------------------------------------------------------------------
    # Display sync — copies ChatBuffers → TextAreas, auto-scrolls
    # ------------------------------------------------------------------


    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI escape sequences (colour codes, etc.) so they don't
        corrupt the prompt-toolkit alternate screen."""
        return _ANSI_RE.sub('', text)

    def _drain_subagent_queue(self):
        """Drain sub-agent streaming tokens into the single scrolling log."""
        q = getattr(self, "_subagent_queue", None)
        if q is None:
            return
        _strip = self._strip_ansi
        buf = self.subagents_buf
        try:
            while True:
                msg = q.get_nowait()
                msg_type = msg[0]
                if msg_type == "sub_tree":
                    _, action, task_id, *rest = msg
                    task_id = str(task_id)[:8]
                    if action == "spawn":
                        _parent_id, short_name, desc = rest[0], rest[1], rest[2] if len(rest) > 2 else ""
                        buf.append(f"🤖 {_strip(short_name)} [{task_id}]")
                        if desc:
                            buf.append(f"   {_strip(desc)}")
                    elif action == "status":
                        status = rest[0] if rest else "?"
                        icon = "✅" if status == "completed" else "❌" if status == "error" else "⏳"
                        buf.append(f" {icon} [{task_id}] {_strip(status)}")
                elif msg_type == "sub_token":
                    _, task_id, text = msg
                    task_id = str(task_id)[:8]
                    buf.append_last(f" [{task_id}] {_strip(text)}", style="")
                elif msg_type == "sub_tool":
                    _, action, name, task_id, *rest = msg
                    task_id = str(task_id)[:8]
                    if action == "start":
                        buf.append(f" 🔧 [{task_id}] {_strip(name)}")
                    elif action == "end":
                        ok, detail = rest[0], rest[1] if len(rest) > 1 else ""
                        status = "OK" if ok else "ERR"
                        buf.append(f"   [{task_id}] {status} {_strip(detail)}")
        except queue.Empty:
            pass  # queue drained

    def _sync_display(self, app=None):
        """Sync ChatBuffers to TextArea widgets.  Called by before_render
        on every refresh cycle.  Skips buffers that haven't changed (dirty
        tracking) to keep input responsive during fast typing."""
        # Guard: TextAreas not yet created (before _build_layout runs)
        if self.chat_area is None:
            return

        if self.tools_area is not None and self.tools_buf.dirty:
            self.tools_area.text = self.tools_buf.consume_text()
            self.tools_area.buffer.cursor_position = \
                len(self.tools_area.buffer.text)

        if self.thinking_area is not None and self.thinking_buf.dirty:
            self.thinking_area.text = self.thinking_buf.consume_text()
            self.thinking_area.buffer.cursor_position = \
                len(self.thinking_area.buffer.text)

        # Drain sub-agent streaming queue into single scrolling log
        self._drain_subagent_queue()
        if self.subagents_area is not None and self.subagents_buf.dirty:
            self.subagents_area.text = self.subagents_buf.consume_text()
            self.subagents_area.buffer.cursor_position = \
                len(self.subagents_area.buffer.text)

        if self.chat_area is not None and self.chat_buf.dirty:
            self.chat_area.text = self.chat_buf.consume_text()
            self.chat_area.buffer.cursor_position = \
                len(self.chat_area.buffer.text)

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-q")
        def _(event):
            self.worker.cancel.set()
            self.messages = self.memory.save(self.messages)
            event.app.exit()

        @kb.add("enter")
        def _(event):
            if event.app.layout.has_focus(self.input_buffer):
                self.input_buffer.validate_and_handle()

        return kb

    # ------------------------------------------------------------------
    # Submit handler
    # ------------------------------------------------------------------

    def _on_submit(self, buffer: Buffer) -> bool:
        text = buffer.text.strip()
        if not text:
            # Empty submit — re-display the input prompt
            return False

        # Route slash commands
        if text.startswith("/"):
            self._handle_command(text)
            buffer.reset()
            return True

        # Show the message in the chat log
        self.chat_buf.append(f"─ You", style="msg-user")
        self.chat_buf.append(text, style="msg-user")
        self.chat_buf.append("", style="")
        buffer.reset()

        # Feed to the ONE worker.  If it's mid-turn the message waits
        # in the input queue until the current turn finishes.
        self.worker.submit(text)
        return True

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _handle_command(self, text: str) -> None:
        """Route /slash commands.  Results write to tools_buf."""
        cmd = text.lower().strip()

        if cmd == "/clear":
            # Cancel current turn, then reset conversation
            self.worker.cancel.set()
            new_messages = [
                {"role": "system", "content": build_system_prompt(self.config)},
                {"role": "system", "content": build_startup_context(self.config.workspace)},
            ]
            self.messages = new_messages
            self.worker.messages = new_messages
            clear_api_cache()
            self.memory.clear()
            self.worker.total_turns = 0
            self.worker.total_tokens = 0
            self.tools_buf.append("--- conversation cleared ---")
            return

        if cmd == "/help":
            for line in [
                "/clear     Reset conversation memory",
                "/export    Write conversation to markdown",
                "/help      Show this help",
                "/init      Reinitialize rules + toml",
                "/stats     Show session stats",
                "/session   new | switch | delete | list",
                "/python    Drop to Python REPL (Ctrl+D to return)",
                "/theme     Show theme info",
                "/workspace Switch workspace",
            ]:
                self.tools_buf.append(line)
            return

        if cmd == "/stats":
            turns = self.worker.total_turns
            tokens = self.worker.total_tokens
            self.tools_buf.append(
                f"Session: {len(self.messages)} msgs, {turns} turns, "
                f"{tokens} tokens, {self.config.model}"
            )
            return

        if cmd.startswith("/session"):
            parts = cmd.split(maxsplit=2)
            sub = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""
            ws = self.config.workspace
            if sub == "list":
                sessions = list_sessions(ws)
                self.tools_buf.append(
                    f"Sessions: {', '.join(sessions) if sessions else 'none'}"
                )
            elif sub == "new" and arg:
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                self.tools_buf.append(f"Created session '{arg}'.")
            elif sub == "switch" and arg:
                self.messages = self.memory.save(self.messages)
                self.memory.close()
                session_data = switch_session(ws, arg, self.memory, self.config)
                self.memory = session_data["memory"]
                self.messages = session_data["messages"]
                self._total_turns = 0
                self._total_tokens = 0
                self.tools_buf.append(f"Switched to '{arg}'.")
            elif sub == "delete" and arg:
                ok, msg = delete_session(ws, arg)
                self.tools_buf.append(msg)
            else:
                self.tools_buf.append(
                    "Usage: /session new <name> | switch <name> | delete <name> | list"
                )
            return

        if cmd == "/export":
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"conversation_{ts}.md"
            path = os.path.join(self.config.workspace, fname)
            self._export_to_file(path)
            self.tools_buf.append(f"Exported to {fname}")
            return

        if cmd.startswith("/theme"):
            self.tools_buf.append("Theme: Catppuccin Mocha (only palette available)")
            return

        if cmd.startswith("/workspace"):
            parts = text.split(maxsplit=1)
            new_path = parts[1].strip() if len(parts) > 1 else ""
            if not new_path:
                self.tools_buf.append("Usage: /workspace <path>")
                return
            new_workspace = os.path.abspath(new_path)
            if not os.path.isdir(new_workspace):
                self.tools_buf.append(f"Not a directory: {new_workspace}")
                return
            self.messages = self.memory.save(self.messages)
            self.memory.close()
            from config import init_session as _init_session
            try:
                new_data = _init_session(new_workspace)
            except Exception as exc:
                self.tools_buf.append(f"Error: {exc}")
                _log_error_trace(
                    exc, self.messages, turn_id=0,
                )
                return
            self.config = new_data["config"]
            self.config.verbose = "--quiet" not in sys.argv
            self.write_gate = new_data["write_gate"]
            self.read_gate = new_data["read_gate"]
            self.memory = new_data["memory"]
            self.messages = new_data["messages"]
            self.session.close()
            self.session = new_data["session"]
            self.worker.messages = new_data["messages"]
            self.worker.total_turns = 0
            self.worker.total_tokens = 0
            self._refresh_git_status()
            self.tools_buf.append(f"Workspace: {new_workspace}")
            return

        if cmd == "/init":
            from tools.file_ops import _init_rules
            from safety import ReadSafetyGate
            rg = ReadSafetyGate(self.config.workspace)
            result = _init_rules({}, None, rg)
            self.tools_buf.append(result.content)
            return

        if cmd == "/python":
            self.tools_buf.append("Dropping to Python REPL — type 'exit' or Ctrl+D to return.")
            self.messages = self.memory.save(self.messages)
            self.app.exit()
            import code
            code.interact(local=locals())
            self.app = Application(
                layout=self._build_layout(),
                key_bindings=self._build_keybindings(),
                style=STYLE,
                full_screen=True,
                mouse_support=True,
                refresh_interval=0.05,
                before_render=self._sync_display,
            )
            self.app.run()
            return

        self.tools_buf.append(f"Unknown command: {text}")

    def _export_to_file(self, path: str) -> None:
        """Export conversation to a markdown file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mini_agent Conversation\n\n")
            for msg in self.messages:
                role = msg["role"].upper()
                content = msg.get("content", "")
                if isinstance(content, str):
                    f.write(f"## {role}\n\n{content}\n\n")
                elif isinstance(content, list):
                    # Multi-part content (e.g. tool calls)
                    f.write(f"## {role}\n\n")
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "tool_use":
                                f.write(f"- **Tool**: {part.get('name', 'unknown')}\n")
                                f.write(f"  ```\n{part.get('input', {})}\n  ```\n")
                            elif part.get("type") == "text":
                                f.write(f"{part.get('text', '')}\n")
                    f.write("\n")

    # ------------------------------------------------------------------
    # Header / Status text
    # ------------------------------------------------------------------

    def _header_text(self) -> FormattedText:
        return FormattedText([
            ("class:header", f" mini_agent — {self.config.model}")
        ])

    def _status_text(self) -> FormattedText:
        parts: list[tuple[str, str]] = []
        if self._git_branch:
            dirty = "*" if self._git_dirty else ""
            parts.append(("class:status", f"⎇ {self._git_branch}{dirty}"))
        if self.worker.is_alive():
            parts.append(("class:status", " ●"))
            if self.worker.total_turns:
                parts.append(("class:status",
                              f"↻ turn {self.worker.total_turns}"))
            if self.worker.total_tokens:
                tok = self.worker.total_tokens
                tok_s = f"{tok / 1000:.1f}k" if tok >= 1000 else str(tok)
                parts.append(("class:status", f"⊙ {tok_s} tok"))
        return FormattedText(parts)

    def _refresh_git_status(self):
        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.config.workspace,
                capture_output=True, text=True, encoding="utf-8", timeout=3)
            self._git_branch = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.workspace,
                capture_output=True, text=True, encoding="utf-8", timeout=3)
            self._git_dirty = bool(r2.stdout.strip())
        except Exception:
            self._git_branch = ""
            self._git_dirty = False

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        # Register an atexit handler BEFORE running the app so it fires
        # after prompt_toolkit's own cleanup on any exit path (Ctrl+C,
        # Ctrl+Q, crash, KeyboardInterrupt).  Writing directly to /dev/tty
        # avoids the risk that sys.stdout has been disconnected from the
        # real terminal by prompt_toolkit's internal output layer.
        import atexit
        cleaned_up = []

        def _force_terminal_reset():
            # Only run once (atexit handlers may fire on both normal exit
            # and during interpreter finalization).
            if cleaned_up:
                return
            cleaned_up.append(True)
            codes = [
                '\033[?1000l',  # Disable basic mouse tracking
                '\033[?1002l',  # Disable button-event mouse
                '\033[?1003l',  # Disable any-event mouse
                '\033[?1006l',  # Disable SGR mouse mode
                '\033[?1049l',  # Exit alternate screen
                '\033[?25h',    # Show cursor
                '\033[0m',      # Reset SGR attributes
            ]
            # Prefer /dev/tty — the real terminal.  Fall back to stdout
            # (which may work depending on how prompt_toolkit exits).
            for target in ('/dev/tty', None):
                try:
                    if target is not None:
                        f = open(target, 'w', encoding='utf-8')
                    else:
                        f = sys.stdout
                    for code in codes:
                        f.write(code)
                    f.flush()
                    if target is not None:
                        f.close()
                    break  # succeeded, don't try next target
                except (OSError, IOError):
                    continue

        atexit.register(_force_terminal_reset)

        try:
            self.app.run()
        finally:
            # Belt-and-suspenders: also run the reset immediately when
            # app.run() returns, in case atexit hasn't fired yet.
            _force_terminal_reset()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _cleanup_orphans():
    """Kill stale pylsp processes from previous sessions.
    Called both at startup (to clean up after a previous crash) and at
    exit (to clean up after the current session)."""
    try:
        subprocess.run(
            ["pkill", "-f", "pylsp"],
            capture_output=True, timeout=5)
    except Exception:
        pass  # pkill may not exist, or no orphans to kill

if __name__ == "__main__":
    # Ensure stdout/stderr use UTF-8 regardless of the system locale.
    # Without this, Unicode box-drawing chars and emojis will break on
    # terminals or CI environments that default to ASCII/Latin-1.
    for _stream, _name in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass  # Python < 3.7 or already closed — ignore

    # Redirect stderr to a log file so random warnings / debug prints from
    # tools and subprocess modules don't corrupt the prompt_toolkit TUI layout.
    _stderr_log_path = os.path.join(os.path.dirname(__file__), "tui_stderr.log")
    _original_stderr = sys.stderr
    sys.stderr = open(_stderr_log_path, "a", encoding="utf-8")

    # Kill orphaned LSP processes from previous crashed sessions
    _cleanup_orphans()

    # Ensure cleanup on normal exit (also restores stderr)
    import atexit
    def _exit_handler():
        _cleanup_orphans()
        sys.stderr.close()
        sys.stderr = _original_stderr
    atexit.register(_exit_handler)

    MiniAgentTUI().run()
