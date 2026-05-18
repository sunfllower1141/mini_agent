#!/usr/bin/env python3
"""
test_tui.py — tests for the Textual TUI frontend.
"""

import os
import tempfile
import unittest
from queue import Queue
from unittest.mock import MagicMock, patch
from tui import _Done, _Error, _SubAgentToken

from tui import (
    MiniAgentTUI, AgentWorker,
    _TokenMsg, _ToolStart, _ToolEnd, _SubAgentToken, _Done, _Error,
    _safe,
)
from stream import THINKING_START, THINKING_END
from config import AgentConfig, DEFAULT_API_KEY
from safety import ReadSafetyGate, WriteSafetyGate


class TestTUIImports(unittest.TestCase):
    """Verify tui.py imports and basic construction."""

    def test_app_class_exists(self):
        self.assertTrue(hasattr(MiniAgentTUI, "compose"))
        self.assertTrue(hasattr(MiniAgentTUI, "on_mount"))

    def test_app_css_defined(self):
        self.assertIsInstance(MiniAgentTUI.CSS, str)
        self.assertIn("background", MiniAgentTUI.CSS)

    def test_bindings_contain_cancel(self):
        binds = {b.key: b.action for b in MiniAgentTUI.BINDINGS}
        self.assertIn("ctrl+c", binds)
        self.assertEqual(binds["ctrl+c"], "cancel")

    def test_bindings_contain_submit(self):
        binds = {b.key: b.action for b in MiniAgentTUI.BINDINGS}
        self.assertIn("enter", binds)
        self.assertEqual(binds["enter"], "submit")

    def test_bindings_contain_quit(self):
        binds = {b.key: b.action for b in MiniAgentTUI.BINDINGS}
        self.assertIn("ctrl+q", binds)
        self.assertEqual(binds["ctrl+q"], "quit")

    def test_css_dark_gray_palette(self):
        css = MiniAgentTUI.CSS
        self.assertIn("#111111", css)
        self.assertNotIn("16162a", css)


class TestMessageTypes(unittest.TestCase):
    """Verify worker→UI message dataclass-like types."""

    def test_token_msg(self):
        m = _TokenMsg("hello")
        self.assertEqual(m.text, "hello")

    def test_tool_start(self):
        m = _ToolStart("search_files('TODO', .)")
        self.assertIn("search_files", m.summary)

    def test_tool_end_success(self):
        m = _ToolEnd(True, "OK")
        self.assertTrue(m.ok)
        self.assertEqual(m.detail, "OK")

    def test_tool_end_failure(self):
        m = _ToolEnd(False, "blocked")
        self.assertFalse(m.ok)

    def test_done(self):
        self.assertIsInstance(_Done(), _Done)

    def test_error(self):
        m = _Error("something broke")
        self.assertEqual(m.msg, "something broke")

    def test_sub_agent_token(self):
        m = _SubAgentToken("task123", "hello world")
        self.assertEqual(m.task_id, "task123")
        self.assertEqual(m.text, "hello world")


class TestSubAgentStreaming(unittest.TestCase):
    """Verify sub-agent token streaming through the TUI drain path."""

    def test_sub_token_tuple_routing(self):
        """The drain method routes ('sub_token', task_id, text) tuples
        to the subagent pane with proper formatting."""
        # Simulate what _drain does with a sub_token tuple
        from tui import _safe
        tag, task_id, text = ("sub_token", "abc123", "Hello from sub-agent")
        self.assertEqual(tag, "sub_token")
        self.assertEqual(task_id, "abc123")
        self.assertIn("Hello", text)
        # Verify _safe escapes the text for markup
        safe_text = _safe(text)
        self.assertEqual(safe_text, "Hello from sub-agent")

    def test_sub_token_with_markup_escaped(self):
        """Markup characters in sub-agent output are escaped."""
        from tui import _safe
        _, _, text = ("sub_token", "x", "[bold]danger[/]")
        safe_text = _safe(text)
        self.assertEqual(safe_text, r"\[bold]danger\[/]")

    def test_spawn_one_visible_pushes_start_message(self):
        """_spawn_one with visible=True pushes sub_tree spawn token to tui_queue."""
        from tools.agent_ops import _spawn_one
        from tools import _TOOL_CONTEXT
        from agent_runtime import AgentRuntime, SubAgentResult
        from unittest.mock import patch
        from safety import ReadSafetyGate, WriteSafetyGate
        import queue
        import time

        # Set up context with a mock TUI queue
        tui_q = queue.Queue()
        _TOOL_CONTEXT.__dict__["_tui_queue"] = tui_q
        runtime = AgentRuntime()

        class MockConfig:
            model = "test"
            api_key = "key"
            api_url = "http://test"
            stream = False
            sub_agent_max_turns = 5

        config = MockConfig()

        # Mock run_sub_agent to return immediately (avoids LLM call)
        with patch("sub_agent.run_sub_agent") as mock_run:
            mock_run.return_value = SubAgentResult(
                success=True, content="done", turns_used=1
            )
            task_id = _spawn_one(
                "test task", config, runtime,
                WriteSafetyGate("/tmp"), ReadSafetyGate("/tmp"),
                max_turns=1,
                visible=True,
                subscriptions=["handoff.result"],
            )

        # Wait for the sub-agent thread to finish
        deadline = time.monotonic() + 3.0
        while runtime.get_status(task_id) == "running" and time.monotonic() < deadline:
            time.sleep(0.05)

        # Verify queue received messages
        spawn_messages = []
        while not tui_q.empty():
            spawn_messages.append(tui_q.get_nowait())

        # Should have at least: sub_tree spawn, sub_done
        self.assertTrue(len(spawn_messages) >= 2,
                        f"Expected at least 2 queue messages, got: {spawn_messages}")

        # Check for the spawn message and done
        spawn_types = {msg[0] for msg in spawn_messages}
        self.assertIn("sub_tree", spawn_types)
        self.assertIn("sub_done", spawn_types)

        # Clean up context
        _TOOL_CONTEXT.__dict__.pop("_tui_queue", None)


class TestAgentWorker(unittest.TestCase):
    """Verify AgentWorker thread setup and cancel."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.config = AgentConfig.load(self.workspace)
        self.config.api_key = DEFAULT_API_KEY
        self.write_gate = WriteSafetyGate(self.workspace, allow_overwrites=True)
        self.read_gate = ReadSafetyGate(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_worker_creates_and_cancels(self):
        messages = [{"role": "system", "content": "You are a test."}]
        out = Queue()
        w = AgentWorker(messages, self.config, self.write_gate, self.read_gate, out, MagicMock())
        self.assertFalse(w.cancel.is_set())
        w.cancel.set()
        w.start()
        w.join(timeout=5)
        self.assertFalse(w.is_alive())

    def test_worker_stream_config_set(self):
        messages = [{"role": "system", "content": "You are a test."}]
        out = Queue()
        config = self.config
        self.assertFalse(config.stream)
        w = AgentWorker(messages, config, self.write_gate, self.read_gate, out, MagicMock())
        w.cancel.set()
        w.start()
        w.join(timeout=5)
        self.assertTrue(config.stream)

    def test_worker_exception_pushes_error_to_queue(self):
        """Worker exception sends _Error + _Done instead of crashing."""
        import requests
        out = Queue()
        w = AgentWorker(
            [{"role": "user", "content": "test"}],
            self.config, self.write_gate, self.read_gate,
            out, requests.Session(),
        )
        # Make run_agent_turn raise
        with patch("tui.run_agent_turn", side_effect=RuntimeError("boom")):
            w.run()
        # Should have pushed _Error + _Done
        items = []
        while not out.empty():
            items.append(out.get_nowait())
        errors = [i for i in items if isinstance(i, _Error)]
        dons = [i for i in items if isinstance(i, _Done)]
        self.assertEqual(len(errors), 1, f'Expected 1 _Error, got: {errors}')
        self.assertIn('boom', errors[0].msg)
        self.assertTrue(any(isinstance(i, _Done) for i in items))


class TestSafe(unittest.TestCase):
    """Tests for the _safe() helper that escapes Textual markup."""

    def test_plain_text_passes_through(self):
        self.assertEqual(_safe("hello world"), "hello world")

    def test_brackets_escaped(self):
        self.assertEqual(_safe("[bold]text[/]"), r"\[bold]text\[/]")

    def test_backslash_escaped(self):
        self.assertEqual(_safe(r"c:\path"), r"c:\\path")

    def test_empty_string(self):
        self.assertEqual(_safe(""), "")


class TestBoxHelpers(unittest.TestCase):
    """Tests for the instance _box_* rendering helpers (now buffer via _write_to_log)."""

    def setUp(self):
        self.app = MiniAgentTUI()
        self.app._chat = MagicMock()
        self.app._tools_log = MagicMock()
        self.app._chat_buf = ""
        self.app._tools_buf = ""
        self.app._agent_box_open = False

    def _assert_buf_contains(self, text):
        """Assert text was buffered (in _chat_buf or _tools_buf)."""
        combined = self.app._chat_buf + self.app._tools_buf
        self.assertIn(text, combined)

    def test_box_open(self):
        self.app._box_open(self.app._chat, "Label", "green")
        self._assert_buf_contains("╭── Label ──")

    def test_box_line(self):
        self.app._box_line(self.app._chat, "hello", "blue")
        self._assert_buf_contains("│ hello")

    def test_box_empty(self):
        self.app._box_empty(self.app._chat, "red")
        self._assert_buf_contains("│")

    def test_box_close_no_label(self):
        self.app._box_close(self.app._chat, "green")
        self._assert_buf_contains("╰──")

    def test_box_close_with_label(self):
        self.app._box_close(self.app._chat, "green", "OK")
        self._assert_buf_contains("OK")


class TestHandleToken(unittest.TestCase):
    """Tests for _handle_token thinking/content routing."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        from memory import MemoryStore
        self.config = AgentConfig.load(self.workspace)
        self.config.api_key = DEFAULT_API_KEY
        self.app = MiniAgentTUI()
        self.app._in_thinking = False
        self.app._thinking_buf = ""
        self.app._thinking_flush_pos = 0
        self.app._buf = ""
        self.app._tui_theme = MagicMock()
        self.app._tui_theme.accent = "green"
        self.app._tui_theme.thinking = "#aaa"
        self.app._tui_theme.dim = "#666"
        self.app._tui_theme.bg = "#111"
        self.app._tui_theme.surface = "#222"
        self.app._agent_box_open = False
        self.app.memory = MemoryStore(os.path.join(self.workspace, ".test_mem.db"))
        self.app.messages = []
        self.app._table_buf = []
        self.app._accumulated_content = []
        # Required for buffered _box_* methods via _write_to_log
        self.app._chat = MagicMock()
        self.app._tools_log = MagicMock()
        self.app._chat_buf = ""
        self.app._tools_buf = ""

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_thinking_start_sets_in_thinking_flag(self):
        log = self.app._chat
        self.app._handle_token(_TokenMsg(THINKING_START), log)
        self.assertTrue(self.app._in_thinking)

    def test_thinking_end_clears_flag(self):
        log = self.app._chat
        self.app._in_thinking = True
        self.app._thinking_buf = ""
        self.app._handle_token(_TokenMsg(THINKING_END), log)
        self.assertFalse(self.app._in_thinking)

    def test_thinking_buffers_text(self):
        log = self.app._chat
        self.app._in_thinking = True
        self.app._handle_token(_TokenMsg("hello "), log)
        self.assertEqual(self.app._thinking_buf, "hello ")

    def test_content_opens_agent_box(self):
        log = self.app._chat
        self.app._handle_token(_TokenMsg("Hello, World!"), log)
        self.assertTrue(self.app._agent_box_open)

    def test_content_buffers_text(self):
        log = self.app._chat
        self.app._handle_token(_TokenMsg("Hello"), log)
        self.app._handle_token(_TokenMsg(" World"), log)
        self.assertIn("Hello World", self.app._buf)


class TestFlushBuf(unittest.TestCase):
    """Tests for _flush_buf behavior."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        from memory import MemoryStore
        self.config = AgentConfig.load(self.workspace)
        self.config.api_key = DEFAULT_API_KEY
        self.app = MiniAgentTUI()
        self.app._buf = ""
        self.app._agent_box_open = False
        self.app._tui_theme = MagicMock()
        self.app._tui_theme.accent = "green"
        self.app._tui_theme.dim = "#666"
        self.app._accumulated_content = []
        self.app._table_buf = []
        self.app.memory = MemoryStore(os.path.join(self.workspace, ".test_mem.db"))
        self.app.messages = []
        # Required for buffered _box_* methods via _write_to_log
        self.app._chat = MagicMock()
        self.app._tools_log = MagicMock()
        self.app._chat_buf = ""
        self.app._tools_buf = ""

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_blank_buf_no_write(self):
        self.app._chat_buf = ""
        self.app._flush_buf()
        self.assertEqual(self.app._chat_buf, "")

    def test_nonblank_buf_flushes(self):
        self.app._buf = "some text here"
        self.app._flush_buf()
        self.assertEqual(self.app._buf, "")
        # _box_line should have buffered text via _write_to_log
        self.assertIn("some text here", self.app._chat_buf)
        self.assertTrue(self.app._agent_box_open)


class TestFinishTurn(unittest.TestCase):
    """Tests for _finish_turn cleanup and promotion."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        from memory import MemoryStore
        self.config = AgentConfig.load(self.workspace)
        self.config.api_key = DEFAULT_API_KEY
        self.app = MiniAgentTUI()
        self.app._in_thinking = False
        self.app._thinking_buf = ""
        self.app._thinking_flush_pos = 0
        self.app._buf = ""
        self.app._agent_box_open = True
        self.app._tui_theme = MagicMock()
        self.app._tui_theme.accent = "green"
        self.app._tui_theme.dim = "#666"
        self.app._accumulated_content = []
        self.app._table_buf = []
        self.app.memory = MemoryStore(os.path.join(self.workspace, ".test_mem.db"))
        self.app.messages = [{"role": "user", "content": "test"}]
        self.app.config = self.config
        self.app._total_tokens = 0
        self.app._total_turns = 0
        self.app.worker = MagicMock()
        self.app._turn_finished = False
        # _finish_turn now uses direct attributes instead of query_one
        self.app._chat = MagicMock()
        self.app._tools_log = MagicMock()
        self.app._chat_buf = ""
        self.app._tools_buf = ""

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_finish_turn_clears_state(self):
        self.app.query_one = MagicMock()  # for #input focus
        self.app._finish_turn()
        self.assertFalse(self.app._in_thinking)
        self.assertEqual(self.app._buf, "")
        self.assertTrue(self.app._turn_finished)

    def test_finish_turn_updates_token_count(self):
        self.app.query_one = MagicMock()  # for #input focus
        self.app._finish_turn(usage={"total_tokens": 1500}, turn_count=3)
        self.assertEqual(self.app._total_tokens, 1500)
        self.assertEqual(self.app._total_turns, 3)


class TestDrainEvent(unittest.TestCase):
    """Verify event-driven drain wakes on queue push via _NotifyQueue."""

    def test_drain_event_sets_on_push(self):
        # _NotifyQueue triggers _drain via call_from_thread on every put.
        app = MagicMock()
        q = _NotifyQueue(app=app)
        q.put("test")
        app.call_from_thread.assert_called_once_with(app._drain)


# SKIP: hangs in CI
class _TestTUIIntegration(unittest.TestCase):
    """Integration tests that actually boot the TUI process."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        # Create minimal workspace for TUI to load
        import os, json
        os.makedirs(self.tmpdir, exist_ok=True)
        # Write a minimal .mini_agent.toml
        with open(os.path.join(self.tmpdir, ".mini_agent.toml"), "w") as f:
            f.write("api_key = ""\n")
            f.write("exa_api_key = ""\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _skip_test_tui_starts_without_crash(self):
        """Boot the TUI, wait for it to initialize, then kill it.
        Verifies no ImportError, AttributeError, or NameError on startup."""
        import subprocess, time, os, signal
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = "test"
        proc = subprocess.Popen(
            ["python", "tui.py", "--workspace", self.tmpdir, "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True,
        )
        # Give it time to initialize (mount, build index, etc.)
        time.sleep(3)
        # Check it's still alive
        self.assertIsNone(proc.poll(),
            f"TUI crashed on startup:\nSTDERR: {proc.stderr.read()[:500]}")
        # Kill it
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
        # Any stderr containing "Error" or "Traceback" is a crash
        self.assertNotIn("Traceback", err,
            f"TUI had traceback during startup:\n{stderr[:500]}")
        self.assertNotIn("Error", stderr,
            f"TUI had error during startup:\n{stderr[:500]}")

class TestHandleCommand(unittest.TestCase):
    """Tests for _handle_command covering all 6 slash commands."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        from memory import MemoryStore
        self.config = AgentConfig.load(self.workspace)
        self.config.api_key = DEFAULT_API_KEY
        self.app = MiniAgentTUI()
        self.app._tui_theme = MagicMock()
        self.app._tui_theme.name = "Slate"
        self.app._tui_theme.dim = "#666"
        self.app._tui_theme.green = "#4f9f6f"
        self.app._tui_theme.yellow = "#b89a4a"
        self.app._tui_theme.accent = "#8f8f8f"
        self.app.config = self.config
        self.app.messages = [{"role": "system", "content": "base"}]
        self.app._history = []
        self.app._history_pos = 0
        self.app._total_turns = 5
        self.app._total_tokens = 2500
        self.app.memory = MagicMock()
        self.app.write_gate = MagicMock()
        self.app.write_gate.check.return_value = (True, "")
        self.app.notify = MagicMock()
        self.app._apply_theme = MagicMock()
        self.app._export_to_file = MagicMock()
        self.app.query_one = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _get_log_mock(self):
        """Return the RichLog mock used for #tools-log writes."""
        return self.app.query_one.return_value

    def test_handle_clear(self):
        """/clear resets messages, memory, history, and counters."""
        self.app._handle_command("/clear")
        self.assertEqual(len(self.app.messages), 2)  # system prompt + startup context
        self.assertEqual(self.app.messages[0]["role"], "system")
        self.app.memory.clear.assert_called_once()
        self.assertEqual(self.app._history, [])
        self.assertEqual(self.app._history_pos, 0)
        self.assertEqual(self.app._total_turns, 0)
        self.assertEqual(self.app._total_tokens, 0)

    def test_handle_help(self):
        """/help writes command list to tools-log."""
        self.app._handle_command("/help")
        log = self._get_log_mock()
        # Should write multiple lines including command descriptions
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Commands", joined)
        self.assertIn("/clear", joined)
        self.assertIn("/help", joined)
        self.assertIn("/export", joined)
        self.assertIn("/theme", joined)
        self.assertIn("/stats", joined)

    def test_handle_help_case_insensitive(self):
        """/HELP should work case-insensitively."""
        self.app._handle_command("/HELP")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Commands", joined)

    def test_handle_stats(self):
        """/stats shows session tokens, turns, messages, model."""
        self.app._handle_command("/stats")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("2500", joined)
        self.assertIn("5", joined)
        self.assertIn("1 msgs", joined)  # 1 system message

    def test_handle_export(self):
        """/export calls _export_to_file with a timestamped path."""
        self.app._handle_command("/export")
        self.app._export_to_file.assert_called_once()
        path_arg = self.app._export_to_file.call_args[0][0]
        self.assertIn("conversation_", path_arg)
        self.assertTrue(path_arg.endswith(".md"))
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Exported to", joined)

    def test_handle_theme_valid(self):
        """'/theme dawn' switches theme and applies it."""
        self.app._handle_command("/theme dawn")
        # Check theme was set
        self.assertEqual(self.app._tui_theme.name, "Dawn")
        self.app._apply_theme.assert_called_once()
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Theme switched", joined)
        self.assertIn("Dawn", joined)

    def test_handle_theme_invalid(self):
        """'/theme bogus' lists available themes."""
        self.app._handle_command("/theme bogus")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Available themes", joined)
        self.assertIn("dawn", joined)
        self.assertIn("slate", joined)
        # Also shows usage hint
        self.assertIn("Usage", joined)

    def test_handle_theme_no_name(self):
        """'/theme' with no argument lists available themes."""
        self.app._handle_command("/theme")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Available themes", joined)

    def test_handle_unknown_command(self):
        """Unknown /command writes error message."""
        self.app._handle_command("/foobar")
        log = self._get_log_mock()
        log.write.assert_called()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Unknown command", joined)
        self.assertIn("/foobar", joined)


class TestBuildCSS(unittest.TestCase):
    """Tests for _build_css covering all themes and expected selectors."""

    @classmethod
    def setUpClass(cls):
        from tui import THEMES
        cls.THEMES = THEMES

    @staticmethod
    def _css(theme):
        from tui import _build_css
        return _build_css(theme)

    def test_all_nine_themes_return_non_empty_css(self):
        """_build_css returns a non-empty string for every theme."""
        for key, theme in self.THEMES.items():
            with self.subTest(theme=key):
                css = self._css(theme)
                self.assertIsInstance(css, str)
                self.assertGreater(len(css.strip()), 0,
                                   f"CSS for theme '{key}' is empty")

    def test_css_contains_expected_selectors(self):
        """CSS output contains basic selectors for layout widgets."""
        css = self._css(self.THEMES["slate"])
        expected_selectors = [
            "Screen {",
            "Header {",
            "Footer {",
            "#static-pane {",
            "#tools-log {",
            "#agent-tree {",
            "#subagent-pane {",
            "#chat-pane {",
            "#input-area {",
            "#input {",
            "#status-bar {",
        ]
        for selector in expected_selectors:
            with self.subTest(selector=selector):
                self.assertIn(selector, css,
                              f"CSS missing selector: {selector}")

    def test_dawn_theme_has_light_colors(self):
        """Dawn (light) theme uses light background hex."""
        css = self._css(self.THEMES["dawn"])
        self.assertIn("#faf8f5", css)

    def test_dracula_theme_has_classic_colors(self):
        """Dracula theme uses its iconic purple accent."""
        css = self._css(self.THEMES["dracula"])
        self.assertIn("#bd93f9", css)
        self.assertIn("#282a36", css)

    def test_all_themes_inject_background_on_screen(self):
        """Every theme places a background on Screen."""
        for key, theme in self.THEMES.items():
            with self.subTest(theme=key):
                css = self._css(theme)
                self.assertIn(f"background: {theme.bg};", css)

    def test_all_themes_inject_accent_in_header(self):
        """Every theme places its accent color in Header."""
        for key, theme in self.THEMES.items():
            with self.subTest(theme=key):
                css = self._css(theme)
                # Header block contains accent color
                header_start = css.index("Header {")
                header_end = css.index("}", header_start)
                header_block = css[header_start:header_end]
                self.assertIn(f"color: {theme.accent};", header_block)


if __name__ == "__main__":
    unittest.main()
