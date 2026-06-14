"""Tests for _inject_confidence_web_search_nudge in core/context_inject.py.

Tests the Knowledge Confidence Scale nudge that detects when the agent is
flailing with local codebase tools and nudges it to use web_search instead.
"""

from __future__ import annotations

import json
import unittest

from core.context_inject import _inject_confidence_web_search_nudge
from tools import _TOOL_CONTEXT


def _assistant_msg(tool_calls: list[dict]) -> dict:
    return {"role": "assistant", "tool_calls": tool_calls}


def _tool_msg(call_id: str, success: bool = True, content: str = "") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"success": success, "content": content}),
    }


def _call(id: str, name: str, args: str = "{}") -> dict:
    return {
        "id": id,
        "function": {"name": name, "arguments": args},
    }


class TestConfidenceWebSearchNudge(unittest.TestCase):
    """Verify _inject_confidence_web_search_nudge triggers correctly."""

    def setUp(self):
        # Reset cooldown tracking so each test starts fresh
        _TOOL_CONTEXT._confidence_nudge_last_turn = 0

    def _assert_nudge(self, messages: list[dict], turn_count: int = 10) -> str | None:
        """Call the injector and return the nudge content if one was appended."""
        before = len(messages)
        _inject_confidence_web_search_nudge(messages, turn_count=turn_count)
        if len(messages) > before:
            msg = messages[-1]
            self.assertEqual(msg["role"], "user")
            self.assertTrue(msg.get("_transient", False))
            return msg["content"]
        return None

    # ------------------------------------------------------------------
    # Happy path: no nudge when everything is fine
    # ------------------------------------------------------------------

    def test_no_nudge_when_searches_succeed(self):
        """No nudge when find_symbol/search_files return real results."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"foo"}')]),
            _tool_msg("c1", success=True, content="Found at line 42"),
        ]
        self.assertIsNone(self._assert_nudge(messages))

    def test_no_nudge_when_few_searches_miss(self):
        """Fewer than 3 consecutive misses should not trigger."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"foo"}')]),
            _tool_msg("c1", success=True, content="no match found"),
            _assistant_msg([_call("c2", "search_files", '{"pattern":"bar"}')]),
            _tool_msg("c2", success=True, content="no results"),
        ]
        self.assertIsNone(self._assert_nudge(messages))

    def test_no_nudge_on_single_failure(self):
        """A single tool failure should not trigger (threshold is 2)."""
        messages = [
            _assistant_msg([_call("c1", "run_shell", '{"cmd":"false"}')]),
            _tool_msg("c1", success=False, content="exit code 1"),
        ]
        self.assertIsNone(self._assert_nudge(messages))

    # ------------------------------------------------------------------
    # Trigger: 3+ consecutive search misses, no successful results
    # ------------------------------------------------------------------

    def test_nudge_on_three_consecutive_search_misses(self):
        """3+ consecutive search misses without ANY success should nudge."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"foo"}')]),
            _tool_msg("c1", success=True, content="no match found"),
            _assistant_msg([_call("c2", "search_files", '{"pattern":"bar"}')]),
            _tool_msg("c2", success=True, content="No results"),
            _assistant_msg([_call("c3", "find_usages", '{"name":"baz"}')]),
            _tool_msg("c3", success=True, content="not found"),
        ]
        nudge = self._assert_nudge(messages)
        self.assertIsNotNone(nudge)
        self.assertIn("CONFIDENCE CHECK", nudge)
        self.assertIn("web_search", nudge)

    def test_nudge_five_search_misses(self):
        """5 consecutive misses should also nudge."""
        messages = [
            _assistant_msg([_call(f"c{i}", "find_symbol", '{"name":"x"}')])
            for i in range(5)
        ] + [
            _tool_msg(f"c{i}", success=True, content="no match")
            for i in range(5)
        ]
        nudge = self._assert_nudge(messages)
        self.assertIsNotNone(nudge)
        self.assertIn("5", nudge)  # mentions the count

    def test_no_nudge_if_one_search_succeeded(self):
        """If there was at least one successful search result, don't nudge."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"foo"}')]),
            _tool_msg("c1", success=True, content="Found at line 42"),
            _assistant_msg([_call("c2", "find_symbol", '{"name":"bar"}')]),
            _tool_msg("c2", success=True, content="no match"),
            _assistant_msg([_call("c3", "find_symbol", '{"name":"baz"}')]),
            _tool_msg("c3", success=True, content="no match"),
            _assistant_msg([_call("c4", "find_symbol", '{"name":"qux"}')]),
            _tool_msg("c4", success=True, content="no match"),
        ]
        self.assertIsNone(self._assert_nudge(messages, turn_count=10))

    # ------------------------------------------------------------------
    # Trigger: 2+ consecutive tool failures
    # ------------------------------------------------------------------

    def test_nudge_on_two_consecutive_tool_failures(self):
        """2 consecutive edit/write/shell/test failures should nudge."""
        messages = [
            _assistant_msg([_call("c1", "edit_file", '{"path":"x.py"}')]),
            _tool_msg("c1", success=False, content="syntax error"),
            _assistant_msg([_call("c2", "run_shell", '{"cmd":"make"}')]),
            _tool_msg("c2", success=False, content="exit code 2"),
        ]
        nudge = self._assert_nudge(messages)
        self.assertIsNotNone(nudge)
        self.assertIn("CONFIDENCE CHECK", nudge)
        self.assertIn("tool calls failed", nudge)

    def test_no_nudge_on_interleaved_success(self):
        """A success between failures resets the consecutive counter."""
        messages = [
            _assistant_msg([_call("c1", "edit_file", '{"path":"x.py"}')]),
            _tool_msg("c1", success=False, content="error"),
            _assistant_msg([_call("c2", "run_shell", '{"cmd":"echo ok"}')]),
            _tool_msg("c2", success=True, content="ok"),
            _assistant_msg([_call("c3", "edit_file", '{"path":"y.py"}')]),
            _tool_msg("c3", success=False, content="error"),
        ]
        self.assertIsNone(self._assert_nudge(messages))

    # ------------------------------------------------------------------
    # Trigger: 6+ read-only turns
    # ------------------------------------------------------------------

    def test_nudge_on_six_read_only_turns(self):
        """6+ turns of pure reads (read_file, find_symbol, etc.) should nudge."""
        messages = []
        for i in range(6):
            messages.append(
                _assistant_msg([_call(f"c{i}", "read_file", '{"path":"x.py"}')])
            )
            messages.append(_tool_msg(f"c{i}", success=True, content="def foo(): pass"))
        nudge = self._assert_nudge(messages)
        self.assertIsNotNone(nudge)
        self.assertIn("reading code", nudge)

    def test_read_only_counter_breaks_on_write(self):
        """A write/shell tool call breaks the read-only streak."""
        messages = []
        for i in range(6):
            messages.append(
                _assistant_msg([_call(f"c{i}", "read_file", '{"path":"x.py"}')])
            )
            messages.append(_tool_msg(f"c{i}", success=True, content="code"))
        # Add a productive turn at the end
        messages.append(
            _assistant_msg([_call("c_write", "write_file", '{"path":"x.py"}')])
        )
        messages.append(_tool_msg("c_write", success=True, content="written"))
        self.assertIsNone(self._assert_nudge(messages, turn_count=12))

    # ------------------------------------------------------------------
    # Cooldown
    # ------------------------------------------------------------------

    def test_cooldown_prevents_repeat_nudge(self):
        """Nudge should not fire again within 4 turns of a previous nudge."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"a"}')]),
            _tool_msg("c1", success=True, content="no match"),
            _assistant_msg([_call("c2", "search_files", '{"pattern":"b"}')]),
            _tool_msg("c2", success=True, content="no results"),
            _assistant_msg([_call("c3", "find_usages", '{"name":"c"}')]),
            _tool_msg("c3", success=True, content="not found"),
        ]
        # First call at turn 10 -- should nudge
        nudge1 = self._assert_nudge(messages, turn_count=10)
        self.assertIsNotNone(nudge1)

        # Second call at turn 11 -- within 4-turn cooldown, should NOT nudge
        nudge2 = self._assert_nudge(messages, turn_count=11)
        self.assertIsNone(nudge2)

        # Third call at turn 15 -- 5 turns later, cooldown expired, should nudge again
        nudge3 = self._assert_nudge(messages, turn_count=15)
        self.assertIsNotNone(nudge3)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_messages(self):
        """Empty message list should not crash and should not nudge."""
        self.assertIsNone(self._assert_nudge([]))

    def test_only_user_messages(self):
        """Messages with only user role should not nudge."""
        messages = [{"role": "user", "content": "hello"}]
        self.assertIsNone(self._assert_nudge(messages))

    def test_malformed_tool_content(self):
        """Malformed tool result JSON should not crash."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"x"}')]),
            {"role": "tool", "tool_call_id": "c1", "content": "not valid json {{{"},
        ]
        self.assertIsNone(self._assert_nudge(messages))

    def test_mixed_search_and_failure_triggers_search_first(self):
        """When both search misses AND failures are present, search miss triggers first."""
        messages = [
            _assistant_msg([_call("c1", "find_symbol", '{"name":"a"}')]),
            _tool_msg("c1", success=True, content="no match"),
            _assistant_msg([_call("c2", "search_files", '{"pattern":"b"}')]),
            _tool_msg("c2", success=True, content="no results"),
            _assistant_msg([_call("c3", "edit_file", '{"path":"x.py"}')]),
            _tool_msg("c3", success=False, content="error"),
            _assistant_msg([_call("c4", "run_shell", '{"cmd":"make"}')]),
            _tool_msg("c4", success=False, content="exit 1"),
            _assistant_msg([_call("c5", "find_usages", '{"name":"c"}')]),
            _tool_msg("c5", success=True, content="not found"),
        ]
        nudge = self._assert_nudge(messages)
        self.assertIsNotNone(nudge)
        # Should mention search misses (3 > failure threshold of 2)
        self.assertIn("searches returned no results", nudge)


if __name__ == "__main__":
    unittest.main()
