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

import sys
import subprocess
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    FloatContainer, HSplit, VSplit, Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea as PTTextArea

from config import resolve_workspace, init_session, parse_args
from llm import run_agent_turn
from stream import THINKING_START, THINKING_END
from safety import ReadSafetyGate, WriteSafetyGate

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
    "msg-user":     f"fg:{THEME['text']}",
    "msg-agent":    f"fg:{THEME['text']}",
    "msg-error":    f"fg:{THEME['red']}",
    "msg-thinking": f"fg:{THEME['thinking']}",
    # TextArea styling
    "textarea":     f"fg:{THEME['text']}",
})


# ---------------------------------------------------------------------------
# Chat buffer — thread-safe append-only text store
# ---------------------------------------------------------------------------

class ChatBuffer:
    """Thread-safe append-only text log.  Workers write here; the main
    thread syncs to TextArea widgets for display."""

    MAX_LINES = 2000

    def __init__(self):
        self._lines: list[str] = []  # plain text lines
        self._lock = threading.Lock()
        self._dirty = True

    def append(self, text: str):
        with self._lock:
            for line in text.split('\n'):
                self._lines.append(line)
            excess = len(self._lines) - self.MAX_LINES
            if excess > 0:
                self._lines = self._lines[excess:]
            self._dirty = True

    def append_last(self, text: str):
        """Append text to the last line (for streaming tokens)."""
        with self._lock:
            if text == '\n':
                self._lines.append('')
            elif self._lines:
                self._lines[-1] += text
            else:
                self._lines.append(text)
            excess = len(self._lines) - self.MAX_LINES
            if excess > 0:
                self._lines = self._lines[excess:]
            self._dirty = True

    def get_text(self) -> str:
        """Return full log as a single string (for syncing to TextArea)."""
        with self._lock:
            self._dirty = False
            return '\n'.join(self._lines)

    @property
    def dirty(self) -> bool:
        with self._lock:
            return self._dirty


# ---------------------------------------------------------------------------
# Layout helpers — rounded borders
# ---------------------------------------------------------------------------

def _h_line() -> Window:
    return Window(height=1, char=BOX_H, style="class:border")


def rounded_frame(content, title: str | None = None) -> HSplit:
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

    return HSplit([top, body, bottom])

# ---------------------------------------------------------------------------
# Worker thread — runs agent turn, writes to ChatBuffers
# ---------------------------------------------------------------------------

class AgentWorker(threading.Thread):
    """Runs run_agent_turn in a background thread.

    Callbacks append directly to thread-safe ChatBuffer instances.
    The main thread syncs ChatBuffers → TextAreas via before_render
    at refresh_interval (50ms), so no manual sync calls are needed.
    """

    def __init__(self, messages, config, write_gate, read_gate,
                 session, turn_id: int,
                 chat_buf: ChatBuffer,
                 tools_buf: ChatBuffer,
                 thinking_buf: ChatBuffer):
        super().__init__(daemon=True)
        self.messages = messages
        self.config = config
        self.write_gate = write_gate
        self.read_gate = read_gate
        self.session = session
        self.turn_id = turn_id
        self.chat_buf = chat_buf
        self.tools_buf = tools_buf
        self.thinking_buf = thinking_buf
        self.cancel = threading.Event()
        self._thinking_text = ""
        self._in_thinking = False
        self._thinking_flushed = 0  # chars already written to buffer
        self.total_tokens = 0
        self.total_turns = 0

    def run(self):
        self.config.stream = True
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
            self.chat_buf.append(f"Error: {e}")
            return

        if msg is not None:
            usage = msg.get("_total_usage") or {}
            self.total_tokens = usage.get("total_tokens", 0)
            self.total_turns = msg.get("_turn_count", 0)

    # -- callbacks ----------------------------------------------------

    def _on_token(self, text: str):
        if text == THINKING_START:
            self._in_thinking = True
            return
        if text == THINKING_END:
            self._in_thinking = False
            return
        if self._in_thinking:
            self.thinking_buf.append_last(text)
        else:
            self.chat_buf.append_last(text)
    def _on_tool_start(self, summary: str, parallel: bool = False):
        label = f"⚡ {summary}" if parallel else f"🔧 {summary}"
        self.tools_buf.append(label)

    def _on_tool_end(self, ok: bool, detail: str,
                     turn_id: int = 0, diff_preview=None):
        self.tools_buf.append(f"  {'OK' if ok else 'ERR'} {detail}")

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

        # Worker state
        self.worker: AgentWorker | None = None
        self._turn_id = 0

        # Git status
        self._git_branch = ""
        self._git_dirty = False
        self._refresh_git_status()

        # Build TextArea widgets (will be referenced in layout and synced)
        self.chat_area: PTTextArea | None = None
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

        left_pane = rounded_frame(
            HSplit([self.tools_area, _h_line(), self.thinking_area]),
            title="Tools & Thinking",
        )

        # Chat view (right pane)
        self.chat_area = PTTextArea(
            text="",
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            style="class:text",
        )
        right_pane = rounded_frame(self.chat_area, title="Chat")

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

    def _sync_display(self, app=None):
        """Sync all ChatBuffers to their TextArea widgets.  Called by
        before_render on every refresh cycle.  After sync, each TextArea
        auto-scrolls to the bottom."""
        if self.chat_area is not None:
            self.chat_area.text = self.chat_buf.get_text()
            self.chat_area.buffer.cursor_position = \
                len(self.chat_area.buffer.text)

        if self.tools_area is not None:
            self.tools_area.text = self.tools_buf.get_text()
            self.tools_area.buffer.cursor_position = \
                len(self.tools_area.buffer.text)

        if self.thinking_area is not None:
            self.thinking_area.text = self.thinking_buf.get_text()
            self.thinking_area.buffer.cursor_position = \
                len(self.thinking_area.buffer.text)

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-q")
        def _(event):
            if self.worker and self.worker.is_alive():
                self.worker.cancel.set()
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
            return False

        self.chat_buf.append(f"You: {text}")
        buffer.reset()
        self.messages.append({"role": "user", "content": text})

        self._turn_id += 1
        self.worker = AgentWorker(
            self.messages, self.config,
            self.write_gate, self.read_gate,
            self.session,
            turn_id=self._turn_id,
            chat_buf=self.chat_buf,
            tools_buf=self.tools_buf,
            thinking_buf=self.thinking_buf,
        )
        self.worker.start()
        self._sync_display()
        return True

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
        if self.worker and self.worker.is_alive():
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
                capture_output=True, text=True, timeout=3)
            self._git_branch = r.stdout.strip()
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.config.workspace,
                capture_output=True, text=True, timeout=3)
            self._git_dirty = bool(r2.stdout.strip())
        except Exception:
            self._git_branch = ""
            self._git_dirty = False

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        self.app.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    MiniAgentTUI().run()
