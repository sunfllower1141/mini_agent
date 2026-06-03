#!/usr/bin/env python3
"""Tests for llm.py — pure functions: circuit breaker, tool call key, compression."""

from __future__ import annotations

from core.llm import (
    _tool_call_key,
    _check_circuit,
    _compress_stale_tool_results,
    _save_turn_summary,
)


# ---------------------------------------------------------------------------
# _tool_call_key tests
# ---------------------------------------------------------------------------

class TestToolCallKey:
    """Tests for the _tool_call_key function."""

    def test_basic(self):
        tc = {"function": {"name": "read_file", "arguments": '{"path": "/foo"}'}}
        key = _tool_call_key(tc)
        assert "read_file" in key
        assert "/foo" in key

    def test_normalizes_argument_order(self):
        tc1 = {"function": {"name": "edit", "arguments": '{"b": 2, "a": 1}'}}
        tc2 = {"function": {"name": "edit", "arguments": '{"a": 1, "b": 2}'}}
        assert _tool_call_key(tc1) == _tool_call_key(tc2)

    def test_different_args_produce_different_keys(self):
        tc1 = {"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
        tc2 = {"function": {"name": "read_file", "arguments": '{"path": "b.py"}'}}
        assert _tool_call_key(tc1) != _tool_call_key(tc2)

    def test_different_names_produce_different_keys(self):
        tc1 = {"function": {"name": "read_file", "arguments": '{"path": "x"}'}}
        tc2 = {"function": {"name": "write_file", "arguments": '{"path": "x"}'}}
        assert _tool_call_key(tc1) != _tool_call_key(tc2)

    def test_malformed_json_falls_back_to_raw_string(self):
        tc = {"function": {"name": "run_shell", "arguments": "not valid json {"}}
        key = _tool_call_key(tc)
        assert "run_shell" in key
        assert "not valid json" in key

    def test_non_string_arguments(self):
        tc = {"function": {"name": "tool", "arguments": 123}}
        key = _tool_call_key(tc)
        assert "tool" in key


# ---------------------------------------------------------------------------
# _check_circuit tests
# ---------------------------------------------------------------------------

class TestCheckCircuit:
    """Tests for the _check_circuit function."""

    def test_below_threshold_no_warning(self):
        keys = ["a:1", "b:2"]
        assert _check_circuit(keys) is None

    def test_short_list_no_warning(self):
        keys = ["a:1"]
        assert _check_circuit(keys) is None

    def test_at_threshold_triggers_warning(self):
        keys = ["a:1", "a:1", "a:1"]
        result = _check_circuit(keys)
        assert result is not None
        assert "a:1" in result
        assert "3 times" in result

    def test_exceeds_threshold_triggers_warning(self):
        keys = ["x:y"] * 5
        result = _check_circuit(keys)
        assert result is not None

    def test_different_calls_no_warning(self):
        keys = ["a:1", "b:2", "c:3", "a:1", "b:2"]
        assert _check_circuit(keys) is None

    def test_empty_list(self):
        assert _check_circuit([]) is None

    def test_mixed_with_one_repeater(self):
        keys = ["read:path-a", "write:path-b", "read:path-a", "read:path-a"]
        result = _check_circuit(keys)
        assert result is not None
        assert "read:path-a" in result


# ---------------------------------------------------------------------------
# _compress_stale_tool_results tests
# ---------------------------------------------------------------------------

class TestCompressStaleToolResults:
    """Tests for _compress_stale_tool_results."""

    def test_compresses_old_multi_line_tool_result(self):
        msgs = [{"role": "tool", "content": "line1\nline2\nline3\nline4"}]
        # Pad with enough messages to push tool result beyond STALE_THRESHOLD (15)
        padding = [{"role": "user", "content": f"msg {i}"} for i in range(17)]
        msgs = msgs + padding
        _compress_stale_tool_results(msgs)
        # The tool message (index 0) should be compressed
        assert "compressed" in msgs[0]["content"]

    def test_skips_recent_tool_results(self):
        msgs = [{"role": "user", "content": "a"}] * 3 + [
            {"role": "tool", "content": "line1\nline2\nline3"}
        ]
        _compress_stale_tool_results(msgs)
        # Last message is tool, age 0 < 15, should NOT be compressed
        assert "compressed" not in msgs[-1]["content"]

    def test_skips_already_compressed(self):
        msgs = [{"role": "tool", "content": "line1\nline2 \u2026 (compressed: 5 lines, 100 chars)"}]
        padding = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        msgs = msgs + padding
        original = msgs[0]["content"]
        _compress_stale_tool_results(msgs)
        assert msgs[0]["content"] == original  # unchanged

    def test_skips_single_line_results(self):
        msgs = [{"role": "tool", "content": "single line only"}]
        padding = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        msgs = msgs + padding
        _compress_stale_tool_results(msgs)
        # Single line should not be compressed
        assert msgs[0]["content"] == "single line only"

    def test_skips_non_string_content(self):
        msgs = [{"role": "tool", "content": 42}]
        padding = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        msgs = msgs + padding
        _compress_stale_tool_results(msgs)
        assert msgs[0]["content"] == 42  # unchanged

    def test_skips_non_tool_messages(self):
        msgs = [{"role": "assistant", "content": "hello\nworld\nfoo\nbar"}]
        padding = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        msgs = msgs + padding
        _compress_stale_tool_results(msgs)
        assert "compressed" not in msgs[0]["content"]


# ---------------------------------------------------------------------------
# _save_turn_summary tests
# ---------------------------------------------------------------------------

class TestSaveTurnSummary:
    """Tests for _save_turn_summary."""

    def test_basic_save(self):
        # We need _TOOL_CONTEXT to be available
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._turn_history = {}
        if hasattr(_TOOL_CONTEXT, '_min_turn'):
            del _TOOL_CONTEXT._min_turn

        from tools import ToolResult
        msg = {"content": "I will edit the file.", "tool_calls": [
            {"function": {"name": "edit_file", "arguments": '{"path": "x"}'}}
        ]}
        deferred = [
            ({"function": {"name": "edit_file", "arguments": '{}'}},
             ToolResult(success=True, content="File edited successfully."))
        ]
        _save_turn_summary(turn=1, msg=msg, deferred_results=deferred, messages=[])

        assert 1 in _TOOL_CONTEXT._turn_history
        summary = _TOOL_CONTEXT._turn_history[1]
        assert "I will edit" in summary
        assert "edit_file" in summary

    def test_truncates_long_assistant_content(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._turn_history = {}
        if hasattr(_TOOL_CONTEXT, '_min_turn'):
            del _TOOL_CONTEXT._min_turn

        msg = {"content": "x" * 300, "tool_calls": []}
        _save_turn_summary(turn=1, msg=msg, deferred_results=[], messages=[])
        summary = _TOOL_CONTEXT._turn_history[1]
        # Should be truncated to TURN_SUMMARY_ASSISTANT_PREVIEW (200)
        assert len(summary) < 300

    def test_no_tool_calls_no_content(self):
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._turn_history = {}
        if hasattr(_TOOL_CONTEXT, '_min_turn'):
            del _TOOL_CONTEXT._min_turn

        msg = {"content": "", "tool_calls": []}
        _save_turn_summary(turn=1, msg=msg, deferred_results=[], messages=[])
        # Should not crash, summary may be empty
        assert 1 in _TOOL_CONTEXT._turn_history
