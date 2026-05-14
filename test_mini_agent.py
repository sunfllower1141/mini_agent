#!/usr/bin/env python3
"""Tests for mini_agent REPL helper functions (_approve, _export_conversation)."""

import os
import tempfile
import unittest
from unittest.mock import patch

from mini_agent import _approve, _export_conversation


class TestApprove(unittest.TestCase):

    def test_approve_yes_returns_true(self):
        with patch("builtins.input", return_value="y"):
            self.assertTrue(_approve("write_file", {"path": "x.txt", "content": "hi"}))

    def test_approve_yes_full_returns_true(self):
        with patch("builtins.input", return_value="yes"):
            self.assertTrue(_approve("run_shell", {"command": "ls"}))

    def test_approve_no_returns_false(self):
        with patch("builtins.input", return_value="n"):
            self.assertFalse(_approve("write_file", {"path": "x.txt", "content": "no"}))

    def test_approve_empty_returns_false(self):
        with patch("builtins.input", return_value=""):
            self.assertFalse(_approve("edit_file", {"path": "x.txt"}))

    def test_approve_eof_returns_false(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_approve("write_file", {"path": "x.txt", "content": "hi"}))

    def test_approve_keyboard_interrupt_returns_false(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertFalse(_approve("run_shell", {"command": "ls"}))

    def test_approve_truncates_long_args(self):
        with patch("builtins.input", return_value="y"):
            result = _approve("write_file", {"content": "x" * 200})
            self.assertTrue(result)


class TestExport(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_export_creates_file(self):
        messages = [
            {"role": "system", "content": "You are a test assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        path = _export_conversation(messages, self.workspace)
        self.assertTrue(os.path.isfile(path))
        with open(path) as f:
            content = f.read()
        self.assertIn("Hello", content)
        self.assertIn("Hi!", content)
        self.assertIn("# mini_agent", content)

    def test_export_includes_reasoning(self):
        messages = [
            {"role": "assistant", "reasoning_content": "I should say hello.",
             "content": "Hello!"},
        ]
        path = _export_conversation(messages, self.workspace)
        with open(path) as f:
            content = f.read()
        self.assertIn("I should say hello", content)

    def test_export_includes_tool_calls(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_file",
                              "arguments": '{"path": "x.py"}'}}]},
            {"role": "tool", "content": "file content here"},
        ]
        path = _export_conversation(messages, self.workspace)
        with open(path) as f:
            content = f.read()
        self.assertIn("read_file", content)
        self.assertIn("Tool result", content)

    def test_export_uniquely_named(self):
        import time
        p1 = _export_conversation([], self.workspace)
        time.sleep(1.1)
        p2 = _export_conversation([], self.workspace)
        self.assertNotEqual(p1, p2)
        self.assertTrue(os.path.isfile(p1))
        self.assertTrue(os.path.isfile(p2))


if __name__ == "__main__":
    unittest.main()
