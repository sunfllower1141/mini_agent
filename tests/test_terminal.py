#!/usr/bin/env python3
"""Tests for terminal.py -- ANSI colour helpers and table formatting."""

import sys
import unittest
from unittest.mock import patch

from terminal import _color_enabled, c, format_table
from terminal import _RED, _YELLOW, _CYAN, _RESET
from terminal import GREEN


class TestColorEnabled(unittest.TestCase):

    def test_color_enabled_tty_no_flag(self):
        """Colour on when stderr is a TTY and --no-color absent."""
        with patch.object(sys.stderr, "isatty", return_value=True):
            with patch.object(sys, "argv", ["mini_agent"]):
                self.assertTrue(_color_enabled())

    def test_color_enabled_not_tty(self):
        """Colour off when stderr is not a TTY."""
        with patch.object(sys.stderr, "isatty", return_value=False):
            with patch.object(sys, "argv", ["mini_agent"]):
                self.assertFalse(_color_enabled())

    def test_color_enabled_no_color_flag(self):
        """Colour off when --no-color is passed."""
        with patch.object(sys.stderr, "isatty", return_value=True):
            with patch.object(sys, "argv", ["mini_agent", "--no-color"]):
                self.assertFalse(_color_enabled())

    def test_color_enabled_not_tty_with_flag(self):
        """Colour off when both non-TTY and --no-color."""
        with patch.object(sys.stderr, "isatty", return_value=False):
            with patch.object(sys, "argv", ["mini_agent", "--no-color"]):
                self.assertFalse(_color_enabled())


class TestC(unittest.TestCase):

    def setUp(self):
        # Force colours on for these tests
        patcher_tty = patch.object(sys.stderr, "isatty", return_value=True)
        patcher_argv = patch.object(sys, "argv", ["mini_agent"])
        self.mock_tty = patcher_tty.start()
        self.mock_argv = patcher_argv.start()
        self.addCleanup(patcher_tty.stop)
        self.addCleanup(patcher_argv.stop)

    def test_c_wraps_red(self):
        result = c("hello", _RED)
        self.assertTrue(result.startswith(_RED))
        self.assertTrue(result.endswith(_RESET))
        self.assertIn("hello", result)

    def test_c_wraps_green(self):
        result = c("ok", GREEN)
        self.assertTrue(result.startswith(GREEN))
        self.assertTrue(result.endswith(_RESET))
        self.assertIn("ok", result)

    def test_c_wraps_yellow(self):
        result = c("warn", _YELLOW)
        self.assertTrue(result.startswith(_YELLOW))
        self.assertTrue(result.endswith(_RESET))

    def test_c_wraps_cyan(self):
        result = c("info", _CYAN)
        self.assertTrue(result.startswith(_CYAN))
        self.assertTrue(result.endswith(_RESET))

    def test_c_colors_off_returns_plain_text(self):
        """When colours are disabled, c() returns text unchanged."""
        with patch.object(sys.stderr, "isatty", return_value=False):
            result = c("hello", _RED)
            self.assertEqual(result, "hello")

    def test_c_multiple_calls_no_bleed(self):
        """Each c() call produces properly reset output."""
        r1 = c("foo", _RED)
        r2 = c("bar", GREEN)
        self.assertTrue(r1.endswith(_RESET))
        self.assertTrue(r2.endswith(_RESET))


class TestFormatTable(unittest.TestCase):

    def test_format_table_empty_rows(self):
        result = format_table(["Col", "Desc"], [])
        self.assertIn("Col", result)
        self.assertIn("Desc", result)
        # Should have header, separator, but no data rows
        lines = result.split("\n")
        self.assertEqual(len(lines), 2)  # header + separator only

    def test_format_table_single_column(self):
        result = format_table(["Name"], [["Alice"], ["Bob"]])
        self.assertIn("Name", result)
        self.assertIn("Alice", result)
        self.assertIn("Bob", result)

    def test_format_table_wide_columns(self):
        result = format_table(
            ["Short", "VeryLongColumnName"],
            [["a", "value"], ["longer_row", "another"]],
        )
        self.assertIn("Short", result)
        self.assertIn("VeryLongColumnName", result)
        # Wider column should be padded consistently
        self.assertIn("longer_row", result)

    def test_format_table_pipe_delimited(self):
        """Verify the pipe-delimited format is correct."""
        result = format_table(["A", "B"], [["x", "y"]])
        lines = result.split("\n")
        # header
        self.assertTrue(lines[0].startswith("|"))
        self.assertTrue(lines[0].endswith("|"))
        # separator
        self.assertTrue(lines[1].startswith("|"))
        self.assertIn("-", lines[1])
        # data
        self.assertTrue(lines[2].startswith("|"))
        self.assertTrue(lines[2].endswith("|"))

    def test_format_table_padding_consistent(self):
        """Column widths should match so pipes line up."""
        result = format_table(["Col", "Desc"], [["a", "first"], ["bb", "second"]])
        lines = result.split("\n")
        # All lines should have the same length
        lengths = {len(line) for line in lines}
        self.assertEqual(len(lengths), 1, f"Lines have different lengths: {lines}")


if __name__ == "__main__":
    unittest.main()
