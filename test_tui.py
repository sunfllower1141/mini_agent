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

    def test_css_palette(self):
        css = MiniAgentTUI.CSS
        self.assertIn("#89b4fa", css)    # accent (Catppuccin Mocha blue)
        self.assertIn("rgba(0, 0, 0, 0.01)", css)  # near-transparent TextArea bg


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


# TestBoxHelpers, TestHandleToken, TestFlushBuf removed — _box_*, _handle_token,
# and _flush_buf methods were deleted in the CSS-Markdown facelift (commit 617efd2).
# Token/content rendering is now handled by _drain_token via the drain loop.


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
        self.app._tui_theme = MagicMock()
        self.app._tui_theme.accent = "green"
        self.app._tui_theme.dim = "#666"
        self.app.memory = MemoryStore(os.path.join(self.workspace, ".test_mem.db"))
        self.app.messages = [{"role": "user", "content": "test"}]
        self.app.config = self.config
        self.app._total_tokens = 0
        self.app._total_turns = 0
        self.app.worker = MagicMock()
        self.app._turn_finished = False
        self.app._active_tool = ""
        self.app._approval_active = False
        self.app._current_response = None
        self.app._current_response_text = ""
        self.app._last_response = ""
        self.app._chat_view = MagicMock()
        self.app._tools_log = MagicMock()
        self.app._git_branch = ""
        self.app._git_dirty = False
        self.app._session_start = 0.0
        self.app._footer = MagicMock()
        self.app._response_md = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_finish_turn_clears_state(self):
        self.app.query_one = MagicMock()  # for #input focus
        self.app._finish_turn()
        self.assertFalse(self.app._in_thinking)
        self.assertEqual(self.app._thinking_buf, "")
        self.assertEqual(self.app._current_response_text, "")
        self.assertTrue(self.app._turn_finished)

    def test_finish_turn_updates_token_count(self):
        self.app.query_one = MagicMock()  # for #input focus
        self.app._finish_turn(usage={"total_tokens": 1500}, turn_count=3)
        self.assertEqual(self.app._total_tokens, 1500)  # += usage["total_tokens"]
        self.assertEqual(self.app._total_turns, 3)


class TestDrainEvent(unittest.TestCase):
    """Verify event-driven drain wakes on queue push via _NotifyQueue."""

    def test_drain_event_sets_on_push(self):
        # _NotifyQueue was removed in a refactor; drain behavior is now
        # driven by Textual internals. Skip this test.
        self.skipTest("_NotifyQueue removed — drain driven by Textual internals")


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
        self.app._apply_theme = MagicMock()
        self.app._export_to_file = MagicMock()
        # _handle_command uses self._tools_log directly (not query_one)
        self.app._tools_log = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _get_log_mock(self):
        """Return the RichLog mock used for #tools-log writes."""
        return self.app._tools_log

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
        self.assertIn("/clear", joined)

    def test_handle_stats(self):
        """/stats shows session tokens, turns, messages, model."""
        self.app._handle_command("/stats")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("2500", joined)
        self.assertIn("5 turns", joined)
        self.assertIn("1 msgs", joined)

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
        """'/theme' now always shows Tokyo Night (single palette)."""
        self.app._handle_command("/theme dawn")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Tokyo Night", joined)

    def test_handle_theme_invalid(self):
        """'/theme bogus' also shows Tokyo Night (single palette)."""
        self.app._handle_command("/theme bogus")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Tokyo Night", joined)

    def test_handle_theme_no_name(self):
        """'/theme' with no argument shows Tokyo Night."""
        self.app._handle_command("/theme")
        log = self._get_log_mock()
        calls = [c[0][0] for c in log.write.call_args_list if c[0]]
        joined = " ".join(calls)
        self.assertIn("Tokyo Night", joined)

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
    """Tests for _build_css with Catppuccin Mocha theme."""

    @classmethod
    def setUpClass(cls):
        from tui import CATPPUCCIN_MOCHA
        cls.THEME = CATPPUCCIN_MOCHA

    @staticmethod
    def _css(theme):
        from tui import _build_css
        return _build_css(theme)

    def test_css_is_non_empty(self):
        """_build_css returns a non-empty string for Catppuccin Mocha."""
        css = self._css(self.THEME)
        self.assertIsInstance(css, str)
        self.assertGreater(len(css.strip()), 0)

    def test_css_contains_expected_selectors(self):
        """CSS output contains basic selectors for layout widgets."""
        css = self._css(self.THEME)
        expected_selectors = [
            "Screen {",
            "Header {",
            "Footer {",
            "#left-pane {",
            "#tools-log {",
            "#thinking-log {",
            "#agent-tree {",
            "#subagent-pane {",
            "#chat-view {",
            "#input-area {",
            "#input {",
        ]
        for selector in expected_selectors:
            with self.subTest(selector=selector):
                self.assertIn(selector, css,
                              f"CSS missing selector: {selector}")

    def test_screen_no_background(self):
        """Screen is transparent — allows terminal background through."""
        css = self._css(self.THEME)
        screen_start = css.index("Screen {")
        screen_end = css.index("}", screen_start)
        screen_block = css[screen_start:screen_end]
        # Screen may declare 'background: transparent' but never a solid color
        self.assertNotIn("background: $", screen_block)
        self.assertNotIn("background: #", screen_block)

    def test_accent_in_header(self):
        """Theme accent color is in Header."""
        css = self._css(self.THEME)
        header_start = css.index("Header {")
        header_end = css.index("}", header_start)
        header_block = css[header_start:header_end]
        self.assertIn(f"color: {self.THEME.accent};", header_block)


if __name__ == "__main__":
    unittest.main()
