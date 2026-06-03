#!/usr/bin/env python3
"""Tests for SSE stream parsing edge cases."""

import json
import unittest
from unittest.mock import MagicMock

from stream import _parse_stream, THINKING_START, THINKING_END


def _sse_response(lines):
    """Mock response object that yields SSE lines from a list."""
    resp = MagicMock()
    resp.iter_lines.return_value = lines
    return resp


class TestParseStream(unittest.TestCase):

    def test_content_only(self):
        """Plain text content across multiple chunks."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": "Hello"}}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": " world"}}]
            }),
            'data: [DONE]',
        ]
        tokens = []
        msg = _parse_stream(_sse_response(lines), on_token=tokens.append)
        self.assertEqual(msg["content"], "Hello world")
        self.assertEqual("".join(tokens), "Hello world")

    def test_single_tool_call_fragments(self):
        """Tool call arguments arrive in fragments across chunks."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": ""},
                    }]
                }}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '{"path": "/f'},
                    }]
                }}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": 'oo.py"}'},
                    }]
                }}]
            }),
            'data: [DONE]',
        ]
        msg = _parse_stream(_sse_response(lines))
        self.assertIn("tool_calls", msg)
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "read_file")
        self.assertEqual(tc["function"]["arguments"], '{"path": "/foo.py"}')

    def test_reasoning_then_content(self):
        """Reasoning content followed by text content."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {"reasoning_content": "Let me think..."}}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {"reasoning_content": "hmm"}}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": "I think the answer is 42."}}]
            }),
            'data: [DONE]',
        ]
        tokens = []
        msg = _parse_stream(_sse_response(lines), on_token=tokens.append)
        self.assertIn("reasoning_content", msg)
        self.assertEqual(msg["reasoning_content"], "Let me think...hmm")
        self.assertEqual(msg["content"], "I think the answer is 42.")
        # THINKING_START should appear before reasoning, END before content
        self.assertIn(THINKING_START, tokens)
        self.assertIn(THINKING_END, tokens)

    def test_connection_drop_returns_partial(self):
        """If stream drops, accumulated content is returned."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": "Partial response..."}}]
            }),
        ]
        resp = _sse_response(lines)

        def _broken_iter_lines(*args, **kwargs):
            yield from lines
            raise ConnectionError("Connection reset")

        resp.iter_lines = _broken_iter_lines
        msg = _parse_stream(resp)
        self.assertEqual(msg["content"], "Partial response...")

    def test_malformed_json_skipped(self):
        """Malformed SSE chunks are silently skipped."""
        lines = [
            'data: {not valid json}',
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": "valid content"}}]
            }),
            'data: [DONE]',
        ]
        msg = _parse_stream(_sse_response(lines))
        self.assertEqual(msg["content"], "valid content")

    def test_usage_in_chunk(self):
        """Usage info is captured when present."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {"content": "Hi"}}],
                "usage": {"total_tokens": 42, "completion_tokens": 1},
            }),
            'data: [DONE]',
        ]
        msg = _parse_stream(_sse_response(lines))
        self.assertEqual(msg["content"], "Hi")
        self.assertEqual(msg.get("_usage", {}).get("total_tokens"), 42)

    def test_empty_stream(self):
        """Stream with no content returns empty assistant message."""
        resp = _sse_response([])
        msg = _parse_stream(resp)
        self.assertEqual(msg["content"], "")
        self.assertEqual(msg["role"], "assistant")

    def test_multiple_tool_calls_parallel(self):
        """Two tool calls arriving interleaved by index."""
        lines = [
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [
                        {"index": 0, "id": "c0", "type": "function",
                         "function": {"name": "read_file", "arguments": ""}},
                        {"index": 1, "id": "c1", "type": "function",
                         "function": {"name": "write_file", "arguments": ""}},
                    ]
                }}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '{"path":'}},
                        {"index": 1, "function": {"arguments": '{"path":'}},
                    ]
                }}]
            }),
            'data: ' + json.dumps({
                "choices": [{"delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '{"path": "a.txt"}'}},
                        {"index": 1, "function": {"arguments": '{"path": "b.txt"}'}},
                    ]
                }}]
            }),
            'data: [DONE]',
        ]
        msg = _parse_stream(_sse_response(lines))
        self.assertEqual(len(msg["tool_calls"]), 2)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(msg["tool_calls"][1]["function"]["name"], "write_file")


if __name__ == "__main__":
    unittest.main()
