#!/usr/bin/env python3
"""Tests for memory compression functions."""

import json
import unittest

from memory import (
    _compress_tool_results,
    _compress_read_file,
    _compress_search_files,
    _compress_run_shell,
    _compress_default,
    _build_compressed,
    _is_match_line,
    _find_tool_call_name,
    _find_tool_call_args,
    _COMPRESSION_MAX_FIRST_LINE,
)


def _make_tool_msg(tool_call_id: str, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps({"success": True, "content": content}),
    }


def _make_assistant_with_tool_call(tool_call_id: str, tool_name: str,
                                   arguments: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments or {}),
            },
        }],
    }


class TestFindToolCallName(unittest.TestCase):
    """Tests for _find_tool_call_name."""

    def test_finds_name_from_preceding_assistant(self):
        msgs = [
            _make_assistant_with_tool_call("call_1", "read_file",
                                           {"path": "/x", "offset": 0, "limit": 50}),
            _make_tool_msg("call_1", "line 1\nline 2\nline 3\n"),
        ]
        name = _find_tool_call_name(msgs, 1)
        self.assertEqual(name, "read_file")

    def test_returns_none_when_no_tool_call_id(self):
        msgs = [{"role": "tool", "content": "{}"}]
        name = _find_tool_call_name(msgs, 0)
        self.assertIsNone(name)

    def test_returns_none_when_no_matching_assistant(self):
        msgs = [
            {"role": "user", "content": "hi"},
            _make_tool_msg("orphan_id", "result"),
        ]
        name = _find_tool_call_name(msgs, 1)
        self.assertIsNone(name)

    def test_searches_backward_past_other_messages(self):
        msgs = [
            _make_assistant_with_tool_call("t1", "read_file"),
            {"role": "user", "content": "interrupt"},
            _make_tool_msg("t1", "file content"),
        ]
        name = _find_tool_call_name(msgs, 2)
        self.assertEqual(name, "read_file")


class TestFindToolCallArgs(unittest.TestCase):
    """Tests for _find_tool_call_args."""

    def test_returns_args_dict(self):
        msgs = [
            _make_assistant_with_tool_call("c1", "read_file",
                                           {"path": "/a", "offset": 10, "limit": 20}),
            _make_tool_msg("c1", "content"),
        ]
        args = _find_tool_call_args(msgs, 1)
        self.assertEqual(args, {"path": "/a", "offset": 10, "limit": 20})

    def test_returns_empty_for_missing_args(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "f"},
                }],
            },
            _make_tool_msg("c1", "result"),
        ]
        args = _find_tool_call_args(msgs, 1)
        self.assertEqual(args, {})

    def test_returns_empty_when_no_match(self):
        msgs = [_make_tool_msg("c1", "result")]
        args = _find_tool_call_args(msgs, 0)
        self.assertEqual(args, {})

    def test_handles_bad_json_in_arguments(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "not-json"},
                }],
            },
            _make_tool_msg("c1", "result"),
        ]
        args = _find_tool_call_args(msgs, 1)
        self.assertEqual(args, {})


class TestIsMatchLine(unittest.TestCase):
    """Tests for _is_match_line."""

    def test_standard_match_line(self):
        self.assertTrue(_is_match_line("src/foo.py:42: some code here"))

    def test_no_slash_or_dot_fails(self):
        self.assertFalse(_is_match_line("foobar:42: content"))

    def test_windows_path_match(self):
        self.assertTrue(_is_match_line(r"src\foo.py:10: content"))

    def test_non_digit_between_colons_fails(self):
        self.assertFalse(_is_match_line("src/foo.py:abc: content"))

    def test_no_second_colon_fails(self):
        self.assertFalse(_is_match_line("src/foo.py: just a line"))

    def test_empty_between_colons_fails(self):
        self.assertFalse(_is_match_line("src/foo.py:: content"))

    def test_dot_only_prefix(self):
        self.assertTrue(_is_match_line("test.py:5: import os"))

    def test_deeply_nested_path(self):
        self.assertTrue(
            _is_match_line("a/b/c/d/e/f/g.py:1234: some long line"))


class TestBuildCompressed(unittest.TestCase):
    """Tests for _build_compressed."""

    def test_all_indices_kept_no_compression(self):
        lines = ["a", "b", "c"]
        result = _build_compressed(lines, {0, 1, 2}, tag="test")
        self.assertEqual(result, "a\nb\nc")

    def test_empty_set_returns_full(self):
        lines = ["a", "b", "c"]
        result = _build_compressed(lines, set(), tag="test")
        self.assertEqual(result, "a\nb\nc")

    def test_some_indices_kept_with_gaps(self):
        lines = [f"line {i}" for i in range(10)]
        result = _build_compressed(lines, {0, 5, 9}, tag="lines")
        self.assertIn("line 0", result)
        self.assertIn("line 5", result)
        self.assertIn("line 9", result)
        self.assertIn("lines skipped", result)

    def test_gap_at_start(self):
        lines = ["a", "b", "c", "d", "e"]
        result = _build_compressed(lines, {3, 4}, tag="test")
        self.assertIn("d", result)
        self.assertIn("e", result)
        self.assertIn("skipped", result)

    def test_gap_at_end_marked(self):
        lines = ["a", "b", "c", "d", "e"]
        result = _build_compressed(lines, {0, 1}, tag="test")
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("total test", result.lower())


class TestCompressDefault(unittest.TestCase):
    """Tests for _compress_default."""

    def test_short_output_passes_through(self):
        lines = ["a", "b", "c"]
        result = _compress_default(lines)
        self.assertEqual(result, "a\nb\nc")

    def test_long_output_truncated(self):
        lines = [f"line {i}" for i in range(50)]
        result = _compress_default(lines)
        self.assertIn("truncated", result)
        self.assertIn("50 total", result)

    def test_very_long_first_line_truncated(self):
        lines = ["x" * 600, "b", "c", "d", "e", "f", "g"]
        result = _compress_default(lines)
        # First line should be truncated at _COMPRESSION_MAX_FIRST_LINE
        self.assertLessEqual(
            len(result.split("\n")[0]), _COMPRESSION_MAX_FIRST_LINE + 1)


class TestCompressRunShell(unittest.TestCase):
    """Tests for _compress_run_shell."""

    def test_short_output_passes_through(self):
        lines = [f"line {i}" for i in range(10)]
        result = _compress_run_shell(lines)
        self.assertEqual(result, "\n".join(lines))

    def test_long_output_keeps_last_20(self):
        lines = [f"line {i}" for i in range(100)]
        result = _compress_run_shell(lines)
        self.assertIn("truncated", result)
        self.assertIn("last 20 of 100", result)
        # Should contain the last lines
        self.assertIn("line 99", result)
        self.assertIn("line 80", result)
        self.assertNotIn("line 0", result)

    def test_exactly_20_lines_passes_through(self):
        lines = [f"line {i}" for i in range(20)]
        result = _compress_run_shell(lines)
        self.assertEqual(result, "\n".join(lines))


class TestCompressSearchFiles(unittest.TestCase):
    """Tests for _compress_search_files."""

    def test_short_output_passes_through(self):
        lines = ["a", "b", "c"]
        result = _compress_search_files(lines)
        self.assertEqual(result, "a\nb\nc")

    def test_keeps_only_match_lines(self):
        lines = [
            "Found 5 results",
            "",
            "src/foo.py:10: def foo():",
            "  some context line",
            "src/bar.py:20: class Bar:",
            "  more context",
            "",
            "Done.",
        ]
        result = _compress_search_files(lines)
        self.assertIn("src/foo.py:10:", result)
        self.assertIn("src/bar.py:20:", result)
        # Non-match lines should be gone (replaced by skip markers)
        self.assertNotIn("some context line", result)

    def test_no_match_lines_falls_back_to_default(self):
        lines = ["no matches found", "try again", "still nothing",
                 "nope", "nada", "zilch"]
        result = _compress_search_files(lines)
        # Should fall back to default (first 5 lines + truncation)
        self.assertIn("truncated", result)


class TestCompressReadFile(unittest.TestCase):
    """Tests for _compress_read_file."""

    def test_short_file_passes_through(self):
        lines = ["1: a", "2: b", "3: c"]
        # Need a message list so _find_tool_call_args can work
        msgs = [
            _make_assistant_with_tool_call("c1", "read_file",
                                           {"offset": 0, "limit": 50}),
            _make_tool_msg("c1", "\n".join(lines)),
        ]
        result = _compress_read_file(lines, msgs, 1)
        self.assertEqual(result, "\n".join(lines))

    def test_keeps_lines_in_offset_range(self):
        lines = [f"{i}: line {i}" for i in range(1, 101)]
        msgs = [
            _make_assistant_with_tool_call("c1", "read_file",
                                           {"offset": 40, "limit": 20}),
            _make_tool_msg("c1", "\n".join(lines)),
        ]
        result = _compress_read_file(lines, msgs, 1)
        # Lines 41-60 (offset 40, limit 20) should be kept
        self.assertIn("41: line 41", result)
        self.assertIn("60: line 60", result)
        # Lines outside range should not appear verbatim
        self.assertNotIn("1: line 1", result)
        self.assertNotIn("100: line 100", result)

    def test_no_line_numbers_falls_back(self):
        lines = ["plain text", "no line numbers", "here"] * 10
        msgs = [
            _make_assistant_with_tool_call("c1", "read_file",
                                           {"offset": 0, "limit": 50}),
            _make_tool_msg("c1", "\n".join(lines)),
        ]
        result = _compress_read_file(lines, msgs, 1)
        # Should still produce output (default behavior)
        self.assertTrue(len(result) > 0)


class TestCompressToolResults(unittest.TestCase):
    """Tests for _compress_tool_results."""

    def test_empty_list_unchanged(self):
        msgs, changed = _compress_tool_results([])
        self.assertEqual(msgs, [])
        self.assertFalse(changed)

    def test_no_tool_messages_unchanged(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ]
        result, changed = _compress_tool_results(msgs)
        self.assertEqual(result, msgs)
        self.assertFalse(changed)

    def test_recent_tool_messages_not_compressed(self):
        msgs = [
            _make_assistant_with_tool_call("c1", "run_shell",
                                           {"command": "ls"}),
            _make_tool_msg("c1", "\n".join([f"line {i}" for i in range(30)])),
        ]
        result, changed = _compress_tool_results(msgs, keep_recent=6)
        # Only 2 messages, all within keep_recent=6 window
        self.assertFalse(changed)
        # Full output preserved
        content = json.loads(result[1]["content"])["content"]
        self.assertIn("line 29", content)

    def test_old_tool_results_compressed(self):
        lines = [f"line {i}" for i in range(100)]
        msgs = [
            _make_assistant_with_tool_call("c1", "run_shell",
                                           {"command": "longcmd"}),
            _make_tool_msg("c1", "\n".join(lines)),
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "sure"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "yep"},
        ]
        # keep_recent=2: only last 2 msgs untouched; the tool msg at index 1
        # is old and should be compressed.
        result, changed = _compress_tool_results(msgs, keep_recent=2)
        self.assertTrue(changed)
        content = json.loads(result[1]["content"])["content"]
        self.assertIn("truncated", content)
        # run_shell compression keeps last 20 lines (80-99), so early lines are gone
        self.assertNotIn("line 0", content)

    def test_change_flag_false_when_nothing_to_compress(self):
        msgs = [
            _make_assistant_with_tool_call("c1", "run_shell",
                                           {"command": "echo hi"}),
            _make_tool_msg("c1", "hi"),
        ]
        _, changed = _compress_tool_results(msgs, keep_recent=0)
        # Short result shouldn't trigger compression
        self.assertFalse(changed)

    def test_read_file_compression_with_args(self):
        lines = [f"{i}: line {i}" for i in range(1, 51)]
        msgs = [
            _make_assistant_with_tool_call("c1", "read_file",
                                           {"offset": 0, "limit": 10}),
            _make_tool_msg("c1", "\n".join(lines)),
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "x"},
        ]
        result, changed = _compress_tool_results(msgs, keep_recent=2)
        self.assertTrue(changed)
        content = json.loads(result[1]["content"])["content"]
        # Lines 1-10 should be kept (offset 0, limit 10)
        self.assertIn("1: line 1", content)
        self.assertIn("10: line 10", content)
        self.assertNotIn("50: line 50", content)


if __name__ == "__main__":
    unittest.main()
