#!/usr/bin/env python3
"""
test_agent_loop.py — integration tests for the full agent turn pipeline.

Mocks the DeepSeek API to verify that tool calls are executed, results are
appended, text responses are handled correctly, and API retry works.
"""

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

import requests as req_mod

from config import AgentConfig
from llm import call_deepseek, run_agent_turn
from safety import ReadSafetyGate, WriteSafetyGate
from tools import execute_tool, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_response(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    """Build a minimal DeepSeek-style API response."""
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def _tool_call(name: str, call_id: str, args: dict) -> dict:
    """Build a single tool_call object as returned by the API."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def _gates(workspace: str) -> tuple[WriteSafetyGate, ReadSafetyGate]:
    return WriteSafetyGate(workspace, allow_overwrites=True), ReadSafetyGate(workspace)


# ---------------------------------------------------------------------------
# Tests: turn pipeline
# ---------------------------------------------------------------------------

class TestAgentTurnPipeline(unittest.TestCase):
    """Simulate full turns: API → tool execution → response."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        self.config = AgentConfig.load(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("api.requests.post")
    def test_turn_with_one_tool_call(self, mock_post):
        """API returns one tool_call, tool executes, then API returns text."""
        call1_response = MagicMock()
        call1_response.ok = True
        call1_response.json.return_value = _make_api_response(
            tool_calls=[_tool_call("write_file", "call_1", {
                "path": os.path.join(self.workspace, "out.txt"),
                "content": "hello integration",
            })]
        )

        call2_response = MagicMock()
        call2_response.ok = True
        call2_response.json.return_value = _make_api_response(
            content="Done. Wrote the file."
        )

        mock_post.side_effect = [call1_response, call2_response]

        messages: list[dict] = [
            {"role": "user", "content": "write out.txt with hello integration"}
        ]

        msg1 = call_deepseek(messages, self.config)
        self.assertIn("tool_calls", msg1)
        self.assertEqual(len(msg1["tool_calls"]), 1)

        messages.append(msg1)
        for tc in msg1["tool_calls"]:
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.to_json(),
            })

        msg2 = call_deepseek(messages, self.config)
        self.assertNotIn("tool_calls", msg2)
        self.assertEqual(msg2["content"], "Done. Wrote the file.")
        messages.append(msg2)

        out_path = os.path.join(self.workspace, "out.txt")
        self.assertTrue(os.path.isfile(out_path))
        with open(out_path) as f:
            self.assertEqual(f.read(), "hello integration")

        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

    @patch("api.requests.post")
    def test_turn_with_multiple_tool_calls(self, mock_post):
        """API returns multiple tool_calls in one response, all execute."""
        call1_response = MagicMock()
        call1_response.ok = True
        call1_response.json.return_value = _make_api_response(
            tool_calls=[
                _tool_call("write_file", "call_a", {
                    "path": os.path.join(self.workspace, "a.txt"),
                    "content": "AAA",
                }),
                _tool_call("write_file", "call_b", {
                    "path": os.path.join(self.workspace, "b.txt"),
                    "content": "BBB",
                }),
            ]
        )

        call2_response = MagicMock()
        call2_response.ok = True
        call2_response.json.return_value = _make_api_response(
            content="Both files written."
        )

        mock_post.side_effect = [call1_response, call2_response]

        messages: list[dict] = [
            {"role": "user", "content": "write a.txt and b.txt"}
        ]

        msg1 = call_deepseek(messages, self.config)
        self.assertEqual(len(msg1["tool_calls"]), 2)

        messages.append(msg1)
        for tc in msg1["tool_calls"]:
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertTrue(result.success)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.to_json(),
            })

        msg2 = call_deepseek(messages, self.config)
        self.assertEqual(msg2["content"], "Both files written.")
        messages.append(msg2)

        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "b.txt")))
        self.assertEqual(len(messages), 5)

    @patch("api.requests.post")
    def test_turn_without_tools(self, mock_post):
        """API returns plain text — no tool execution needed."""
        call_response = MagicMock()
        call_response.ok = True
        call_response.json.return_value = _make_api_response(
            content="Hello, how can I help?"
        )

        mock_post.return_value = call_response

        messages: list[dict] = [
            {"role": "user", "content": "hi"}
        ]

        msg = call_deepseek(messages, self.config)
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(msg["content"], "Hello, how can I help?")
        messages.append(msg)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["role"], "assistant")

    @patch("api.requests.post")
    def test_tool_failure_does_not_crash(self, mock_post):
        """A failing tool returns a ToolResult with success=False."""
        call1_response = MagicMock()
        call1_response.ok = True
        call1_response.json.return_value = _make_api_response(
            tool_calls=[_tool_call("read_file", "call_fail", {
                "path": os.path.join(self.workspace, "nonexistent.xyz"),
            })]
        )

        call2_response = MagicMock()
        call2_response.ok = True
        call2_response.json.return_value = _make_api_response(
            content="That file doesn't exist."
        )

        mock_post.side_effect = [call1_response, call2_response]

        messages: list[dict] = [
            {"role": "user", "content": "read nonexistent.xyz"}
        ]

        msg1 = call_deepseek(messages, self.config)
        messages.append(msg1)

        for tc in msg1["tool_calls"]:
            result = execute_tool(tc, self.write_gate, self.read_gate)
            self.assertFalse(result.success)
            self.assertIsInstance(result, ToolResult)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result.to_json(),
            })

        msg2 = call_deepseek(messages, self.config)
        self.assertEqual(msg2["content"], "That file doesn't exist.")

        tool_msg = json.loads(messages[2]["content"])
        self.assertFalse(tool_msg["success"])

    @patch("api.requests.post")
    def test_safety_gate_blocks_write(self, mock_post):
        """Write outside workspace is blocked by WriteSafetyGate."""
        outside = tempfile.mkdtemp()
        try:
            call1_response = MagicMock()
            call1_response.ok = True
            call1_response.json.return_value = _make_api_response(
                tool_calls=[_tool_call("write_file", "call_block", {
                    "path": os.path.join(outside, "escape.txt"),
                    "content": "should not be written",
                })]
            )

            call2_response = MagicMock()
            call2_response.ok = True
            call2_response.json.return_value = _make_api_response(
                content="That path is outside the workspace."
            )

            mock_post.side_effect = [call1_response, call2_response]

            messages: list[dict] = [
                {"role": "user", "content": "write outside the workspace"}
            ]

            msg1 = call_deepseek(messages, self.config)
            messages.append(msg1)

            for tc in msg1["tool_calls"]:
                result = execute_tool(tc, self.write_gate, self.read_gate)
                self.assertFalse(result.success)
                self.assertIn("blocked by safety", result.content)

            self.assertFalse(os.path.isfile(os.path.join(outside, "escape.txt")))
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests: API retry
# ---------------------------------------------------------------------------

class TestAPIRetry(unittest.TestCase):
    """Verify that transient API failures are retried."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.config = AgentConfig.load(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("api.requests.post")
    def test_retries_on_503_then_succeeds(self, mock_post):
        """Two 503 failures then a successful response."""
        fail1 = MagicMock()
        fail1.ok = False
        fail1.status_code = 503

        fail2 = MagicMock()
        fail2.ok = False
        fail2.status_code = 503

        success = MagicMock()
        success.ok = True
        success.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        mock_post.side_effect = [fail1, fail2, success]

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        msg = call_deepseek(messages, self.config)
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(mock_post.call_count, 3)

    @patch("api.requests.post")
    def test_retries_on_network_error_then_succeeds(self, mock_post):
        """Two ConnectionErrors then a successful response."""
        success = MagicMock()
        success.ok = True
        success.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "recovered"}}]}

        mock_post.side_effect = [
            req_mod.ConnectionError("refused"),
            req_mod.ConnectionError("refused"),
            success,
        ]

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        msg = call_deepseek(messages, self.config)
        self.assertEqual(msg["content"], "recovered")
        self.assertEqual(mock_post.call_count, 3)

    @patch("api.requests.post")
    def test_non_retryable_error_raises_immediately(self, mock_post):
        """400 Bad Request should NOT be retried."""
        fail = MagicMock()
        fail.ok = False
        fail.status_code = 400
        fail.raise_for_status.side_effect = req_mod.HTTPError("400 Bad Request")

        mock_post.return_value = fail

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        with self.assertRaises(req_mod.HTTPError):
            call_deepseek(messages, self.config)
        # Only one attempt, no retry
        self.assertEqual(mock_post.call_count, 1)

    @patch("api.requests.post")
    def test_exhausted_retries_raises(self, mock_post):
        """All retries exhausted should raise."""
        mock_post.side_effect = req_mod.ConnectionError("always down")

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        with self.assertRaises(req_mod.ConnectionError):
            call_deepseek(messages, self.config)
        # 1 initial + 3 retries = 4 attempts
        self.assertEqual(mock_post.call_count, 4)


# ---------------------------------------------------------------------------
# Tests: run_agent_turn (shared loop)
# ---------------------------------------------------------------------------

class TestRunAgentTurn(unittest.TestCase):
    """Verify the shared run_agent_turn() used by both terminal and TUI."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        self.config = AgentConfig.load(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("api.requests.post")
    def test_text_only_response(self, mock_post):
        """No tool calls — returns text message directly."""
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = _make_api_response(
            content="Hello!"
        )

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        msg = run_agent_turn(messages, self.config, self.write_gate, self.read_gate)

        self.assertIsNotNone(msg)
        self.assertEqual(msg["content"], "Hello!")
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(len(messages), 2)  # user + assistant

    @patch("api.requests.post")
    def test_single_tool_call(self, mock_post):
        """One tool call, tool executes, then text response."""
        call1 = MagicMock()
        call1.ok = True
        call1.json.return_value = _make_api_response(
            tool_calls=[_tool_call("write_file", "c1", {
                "path": os.path.join(self.workspace, "f.txt"),
                "content": "data",
            })]
        )
        call2 = MagicMock()
        call2.ok = True
        call2.json.return_value = _make_api_response(content="Done.")

        mock_post.side_effect = [call1, call2]

        messages: list[dict] = [{"role": "user", "content": "write"}]
        msg = run_agent_turn(messages, self.config, self.write_gate, self.read_gate)

        self.assertEqual(msg["content"], "Done.")
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "f.txt")))
        self.assertEqual(len(messages), 5)  # user, asst(tools), tool, checkpoint, asst

    @patch("api.requests.post")
    def test_callbacks_fire(self, mock_post):
        """on_tool_start and on_tool_end are called."""
        mock_post.side_effect = [
            _mock_response(tool_calls=[
                _tool_call("file_info", "c1", {"path": self.workspace}),
            ]),
            _mock_response(content="ok"),
        ]

        starts = []
        ends = []

        messages: list[dict] = [{"role": "user", "content": "info"}]
        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
            on_tool_start=lambda s: starts.append(s),
            on_tool_end=lambda ok, d, **kw: ends.append((ok, d)),
        )

        self.assertEqual(len(starts), 1)
        self.assertIn("file_info", starts[0])
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0][0])  # ok=True

    @patch("api.requests.post")
    def test_multiple_rounds(self, mock_post):
        """Two rounds of tool calls before final text."""
        mock_post.side_effect = [
            _mock_response(tool_calls=[
                _tool_call("file_info", "c1", {"path": self.workspace}),
            ]),
            _mock_response(tool_calls=[
                _tool_call("write_file", "c2", {
                    "path": os.path.join(self.workspace, "out.txt"),
                    "content": "multi-round",
                }),
            ]),
            _mock_response(content="All done."),
        ]

        messages: list[dict] = [{"role": "user", "content": "do stuff"}]
        msg = run_agent_turn(messages, self.config, self.write_gate, self.read_gate)

        self.assertEqual(msg["content"], "All done.")
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "out.txt")))
        self.assertEqual(len(messages), 7)  # user, asst, tool, asst, tool, checkpoint, asst

    def test_token_budget_code_path_exists(self):
        """Verify _save_turn_summary stores turn history and _total_tokens counts."""
        from llm import _save_turn_summary
        from memory import _total_tokens
        from tools import _TOOL_CONTEXT

        # Test _total_tokens with sample messages
        messages = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there, how can I help?"},
        ]
        token_count = _total_tokens(messages)
        self.assertIsInstance(token_count, int)
        self.assertGreater(token_count, 0)

        # Test _save_turn_summary stores in _TOOL_CONTEXT._turn_history
        self.config.model = "test"
        self.config.api_key = "key"
        msg = {"role": "assistant", "content": "I wrote the file.", "tool_calls": []}
        _save_turn_summary(
            turn=1,
            msg=msg,
            deferred_results=[],
            messages=[{"role": "user", "content": "write file"}],
        )
        self.assertIn(1, _TOOL_CONTEXT._turn_history)
        self.assertIn("I wrote the file", _TOOL_CONTEXT._turn_history[1])

    def test_cancel_mid_turn_returns_none(self):
        """Cancel event set before call returns None."""
        cancel = threading.Event()
        cancel.set()

        messages: list[dict] = [{"role": "user", "content": "hi"}]
        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
            cancel_event=cancel,
        )
        self.assertIsNone(msg)

    @patch("api.requests.post")
    def test_max_turns_cap(self, mock_post):
        """Returns last assistant message when max_turns is exceeded."""
        # Each call returns a tool call — never a plain text response
        responses = []
        for i in range(5):
            responses.append(_mock_response(tool_calls=[
                _tool_call("file_info", f"c{i}", {"path": self.workspace}),
            ]))
        mock_post.side_effect = responses

        messages: list[dict] = [{"role": "user", "content": "loop"}]
        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
            max_turns=3,
        )

        self.assertIsNotNone(msg)
        # Should have tool_calls (it's the last assistant message before cap)
        self.assertIn("tool_calls", msg)
        # 3 API calls, not 5
        self.assertEqual(mock_post.call_count, 3)

    @patch("api.requests.post")
    def test_parallel_tool_execution(self, mock_post):
        """Multiple tool calls in one response execute in parallel."""
        mock_post.side_effect = [
            _mock_response(tool_calls=[
                _tool_call("write_file", "pa", {
                    "path": os.path.join(self.workspace, "a.txt"),
                    "content": "A",
                }),
                _tool_call("write_file", "pb", {
                    "path": os.path.join(self.workspace, "b.txt"),
                    "content": "B",
                }),
            ]),
            _mock_response(content="Both written in parallel."),
        ]

        starts = []
        ends = []

        messages: list[dict] = [{"role": "user", "content": "write two files"}]
        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
            on_tool_start=lambda s, parallel=False: starts.append((s, parallel)),
            on_tool_end=lambda ok, d, **kw: ends.append((ok, d)),
        )

        self.assertEqual(msg["content"], "Both written in parallel.")
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "b.txt")))

        # Both tool starts should have parallel=True
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[0][1])  # parallel flag
        self.assertTrue(starts[1][1])
        self.assertIn("write_file", starts[0][0])
        self.assertIn("write_file", starts[1][0])

        # Both should succeed
        self.assertEqual(len(ends), 2)
        self.assertTrue(ends[0][0])
        self.assertTrue(ends[1][0])


def _mock_response(content: str = "", tool_calls=None) -> MagicMock:
    """Build a mocked requests.Response for use in side_effect lists."""
    m = MagicMock()
    m.ok = True
    m.json.return_value = _make_api_response(content=content, tool_calls=tool_calls)
    return m


# ---------------------------------------------------------------------------
# Tests: real agent loop with run_agent_turn (integration-style)
# ---------------------------------------------------------------------------

class TestRealAgentLoop(unittest.TestCase):
    """Exercise run_agent_turn end-to-end with mocked API — not abstract."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)
        self.config = AgentConfig.load(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("api.requests.post")
    def test_full_pipeline_write_then_read(self, mock_post):
        """Agent writes a file, reads it back, and confirms in one turn."""
        mock_post.side_effect = [
            # Turn 1: write file
            _mock_response(tool_calls=[
                _tool_call("write_file", "w1", {
                    "path": os.path.join(self.workspace, "greeting.txt"),
                    "content": "hello from agent",
                }),
            ]),
            # Turn 2: read it back
            _mock_response(tool_calls=[
                _tool_call("read_file", "r1", {
                    "path": os.path.join(self.workspace, "greeting.txt"),
                }),
            ]),
            # Turn 3: text response
            _mock_response(content="File contains 'hello from agent'. Done."),
        ]

        messages: list[dict] = [
            {"role": "user", "content": "write greeting.txt then read it back"}
        ]

        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
            max_turns=5,
        )

        self.assertIsNotNone(msg)
        self.assertEqual(msg["content"], "File contains 'hello from agent'. Done.")
        # Verify the file was actually written
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "greeting.txt")))
        with open(os.path.join(self.workspace, "greeting.txt")) as f:
            self.assertEqual(f.read(), "hello from agent")

    @patch("api.requests.post")
    def test_run_agent_turn_exercises_real_loop(self, mock_post):
        """run_agent_turn with text-only response exercises the full loop."""
        mock_post.return_value = _mock_response(content="I am ready to help.")

        messages: list[dict] = [
            {"role": "user", "content": "are you ready?"}
        ]

        msg = run_agent_turn(
            messages, self.config, self.write_gate, self.read_gate,
        )

        self.assertIsNotNone(msg)
        self.assertEqual(msg["content"], "I am ready to help.")
        self.assertNotIn("tool_calls", msg)
        # messages list should have user + assistant
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
