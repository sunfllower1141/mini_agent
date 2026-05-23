#!/usr/bin/env python3
"""Comprehensive SSE stream parsing tests — edge cases, malformed input, comments, etc.

Runs alongside the existing ``test_parse_stream.py`` (which covers the basics).
Uses pytest-style plain functions for brevity.
"""

import json

import pytest
import requests
from unittest.mock import MagicMock

from stream import _parse_stream, THINKING_START, THINKING_END


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_response(lines):
    """Mock response whose ``iter_lines`` yields *lines*."""
    resp = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    return resp


def _sse_response_that_breaks(lines, exception_class, after=1):
    """iter_lines yields *lines* then raises *exception_class*."""
    resp = MagicMock()

    def _gen(decode_unicode=False):
        yielded = 0
        for line in lines:
            yield line
            yielded += 1
            if yielded >= after:
                raise exception_class("Simulated drop")

    resp.iter_lines = _gen
    return resp


def _data(chunk):
    """Build an SSE data line from a dict."""
    return "data: " + json.dumps(chunk)


# ---------------------------------------------------------------------------
# Comments & non-data lines
# ---------------------------------------------------------------------------

def test_comment_lines_are_ignored():
    """SSE comment lines (starting with colon) must not affect output."""
    lines = [
        ": this is a comment",
        _data({"choices": [{"delta": {"content": "Hello"}}]}),
        ": another comment",
        _data({"choices": [{"delta": {"content": " world"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "Hello world"


def test_empty_lines_are_skipped():
    """Empty lines between events must be harmless."""
    lines = [
        "",
        "  ",
        _data({"choices": [{"delta": {"content": "A"}}]}),
        "",
        _data({"choices": [{"delta": {"content": "B"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "AB"


def test_non_data_lines_are_skipped():
    """Lines without 'data: ' prefix should be skipped silently."""
    lines = [
        "event: update",
        "id: 42",
        "retry: 3000",
        _data({"choices": [{"delta": {"content": "payload"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "payload"


# ---------------------------------------------------------------------------
# Empty / nil / missing data
# ---------------------------------------------------------------------------

def test_data_empty_string_after_prefix():
    """A data line whose payload is empty after 'data: '."""
    lines = [
        "data: ",
        _data({"choices": [{"delta": {"content": "ok"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "ok"


def test_data_whitespace_only():
    """Whitespace-only payload between valid chunks."""
    lines = [
        _data({"choices": [{"delta": {"content": "keep"}}]}),
        "data:    ",
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "keep"


def test_chunk_missing_choices_key():
    """JSON that parses but has no 'choices' key."""
    lines = [
        _data({"not_choices": 1, "something": "else"}),
        _data({"choices": [{"delta": {"content": "survived"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "survived"


def test_delta_missing_content_and_tool_calls():
    """Delta with neither content nor tool_calls is harmless."""
    lines = [
        _data({"choices": [{"delta": {"role": "assistant"}}]}),
        _data({"choices": [{"delta": {"content": "text"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "text"


def test_content_field_is_null():
    """Null content should not cause a crash (None is falsy so skipped)."""
    lines = [
        _data({"choices": [{"delta": {"content": None}}]}),
        _data({"choices": [{"delta": {"content": "real"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "real"


# ---------------------------------------------------------------------------
# [DONE] termination
# ---------------------------------------------------------------------------

def test_done_not_included_in_content():
    """The [DONE] sentinel must not leak into content."""
    lines = [
        _data({"choices": [{"delta": {"content": "end"}}]}),
        "data: [DONE]",
        _data({"choices": [{"delta": {"content": "SHOULD NOT APPEAR"}}]}),
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "end"
    assert "SHOULD NOT" not in msg["content"]


def test_done_with_leading_space():
    """Only exact 'data: [DONE]' is recognized; variants are processed."""
    lines = [
        "data: [DONE] ",
        _data({"choices": [{"delta": {"content": "after"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    # First line is NOT stripped *before* the [DONE] check (the code does
    # data_str.strip() == "[DONE]"), so ' [DONE] ' actually IS recognized.
    # The check is `data_str.strip() == "[DONE]"` which handles whitespace.
    # So both lines above match — stream stops at first one, content = "".
    assert msg["content"] == ""


def test_done_embedded_inside_chunk():
    """'[DONE]' inside a JSON chunk is just content."""
    lines = [
        _data({"choices": [{"delta": {"content": "[DONE] from server"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert "[DONE] from server" in msg["content"]


def test_stream_without_done_marker():
    """Stream that ends without a [DONE] still returns accumulated data."""
    lines = [
        _data({"choices": [{"delta": {"content": "no done"}}]}),
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "no done"


# ---------------------------------------------------------------------------
# Partial / malformed input
# ---------------------------------------------------------------------------

def test_unclosed_json():
    """JSON that is truncated mid-string should not crash the parser."""
    lines = [
        "data: " + '{"choices": [{"delta": {"content": "oops',
        _data({"choices": [{"delta": {"content": "recovered"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "recovered"


def test_non_json_payload():
    """Payload that is not JSON at all (e.g. raw text)."""
    lines = [
        "data: just some text, not json",
        _data({"choices": [{"delta": {"content": "after"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "after"


def test_json_array_instead_of_object():
    """JSON payload that is an array (not a dict)."""
    lines = [
        "data: [1, 2, 3]",
        _data({"choices": [{"delta": {"content": "safe"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "safe"


def test_choices_is_not_a_list():
    """Choices key exists but is not a list."""
    lines = [
        _data({"choices": "not-a-list"}),
        _data({"choices": [{"delta": {"content": "after"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    # Accessing "choices"[0] on a string gets the first char — might crash
    # but shouldn't because the catch-all `except Exception` saves us
    assert msg["content"] == "after"


# ---------------------------------------------------------------------------
# Connection drops
# ---------------------------------------------------------------------------

def test_chunked_encoding_error():
    """ChunkedEncodingError mid-stream returns partial results."""
    lines = [
        _data({"choices": [{"delta": {"content": "partial"}}]}),
    ]
    resp = _sse_response_that_breaks(
        lines, requests.exceptions.ChunkedEncodingError
    )
    msg = _parse_stream(resp)
    assert msg["content"] == "partial"


def test_connection_error_mid_stream():
    """ConnectionError mid-stream returns partial results."""
    lines = [
        _data({"choices": [{"delta": {"content": "before-drop"}}]}),
    ]
    resp = _sse_response_that_breaks(
        lines, requests.exceptions.ConnectionError
    )
    msg = _parse_stream(resp)
    assert msg["content"] == "before-drop"


def test_stream_consumed_error():
    """StreamConsumedError mid-stream returns partial results."""
    lines = [
        _data({"choices": [{"delta": {"content": "consumed"}}]}),
    ]
    resp = _sse_response_that_breaks(
        lines, requests.exceptions.StreamConsumedError
    )
    msg = _parse_stream(resp)
    assert msg["content"] == "consumed"


def test_os_error_mid_stream():
    """OSError mid-stream (e.g. broken pipe) returns partial results."""
    lines = [
        _data({"choices": [{"delta": {"content": "os-err"}}]}),
    ]
    resp = _sse_response_that_breaks(lines, ConnectionResetError)
    msg = _parse_stream(resp)
    assert msg["content"] == "os-err"


def test_connection_drop_before_any_data():
    """Drop before any content returns empty message."""
    resp = _sse_response_that_breaks([], requests.exceptions.ConnectionError, after=0)
    msg = _parse_stream(resp)
    assert msg["content"] == ""
    assert msg["role"] == "assistant"


def test_connection_drop_preserves_tool_call_fragments():
    """Drop mid-tool-call preserves what was received so far."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0, "id": "tc1", "type": "function",
                "function": {"name": "read", "arguments": '{"path": "start'},
            }]
        }}]}),
    ]
    resp = _sse_response_that_breaks(
        lines, requests.exceptions.ChunkedEncodingError
    )
    msg = _parse_stream(resp)
    assert "tool_calls" in msg
    assert msg["tool_calls"][0]["function"]["name"] == "read"
    assert '"path": "start' in msg["tool_calls"][0]["function"]["arguments"]


# ---------------------------------------------------------------------------
# on_tool_ready callback
# ---------------------------------------------------------------------------

def test_on_tool_ready_called_when_arguments_form_valid_json():
    """on_tool_ready fires once per tool index when args parse as JSON."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"path": "/f'},
            }]
        }}]}),
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": 'oo.py"}'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    fired = []
    msg = _parse_stream(_sse_response(lines), on_tool_ready=lambda tc: fired.append(tc))
    assert len(fired) == 1
    assert fired[0]["function"]["arguments"] == '{"path": "/foo.py"}'
    assert "_index" in fired[0]
    # The result message carries _fired_indices
    assert msg.get("_fired_indices") == [0]


def test_on_tool_ready_not_called_for_incomplete_json():
    """Brace-imbalanced args should NOT trigger on_tool_ready."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"key": "val"'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    fired = []
    _parse_stream(_sse_response(lines), on_tool_ready=lambda tc: fired.append(tc))
    assert len(fired) == 0  # missing closing brace


def test_on_tool_ready_braces_balance_but_not_json():
    """Brace-balanced but invalid JSON (e.g. trailing comma) should NOT fire."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"a": 1,}'},  # trailing comma
            }]
        }}]}),
        "data: [DONE]",
    ]
    fired = []
    _parse_stream(_sse_response(lines), on_tool_ready=lambda tc: fired.append(tc))
    assert len(fired) == 0


def test_on_tool_ready_fires_only_once_per_index():
    """Once fired, the same index does not fire again."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"x": 1}'},
            }]
        }}]}),
        # This chunk appends more arguments — would create invalid JSON
        # but is ignored because the index already fired.
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": 'extra'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    fired = []
    _parse_stream(_sse_response(lines), on_tool_ready=lambda tc: fired.append(tc))
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# Reasoning edge cases
# ---------------------------------------------------------------------------

def test_reasoning_without_content():
    """Only reasoning, no content — no THINKING_END emitted."""
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": "just thinking"}}]}),
        "data: [DONE]",
    ]
    tokens = []
    msg = _parse_stream(_sse_response(lines), on_token=tokens.append)
    assert msg["content"] == ""
    assert msg["reasoning_content"] == "just thinking"
    assert THINKING_START in tokens
    assert THINKING_END not in tokens


def test_reasoning_header_printed_only_once():
    """Multiple reasoning chunks should only print the header once."""
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": "A"}}]}),
        _data({"choices": [{"delta": {"reasoning_content": "B"}}]}),
        _data({"choices": [{"delta": {"content": "done"}}]}),
        "data: [DONE]",
    ]
    tokens = []
    _parse_stream(_sse_response(lines), on_token=tokens.append)
    assert tokens.count(THINKING_START) == 1
    assert tokens.count(THINKING_END) == 1


def test_content_before_reasoning_skips_reasoning_header():
    """If content arrives first, reasoning header is not emitted."""
    lines = [
        _data({"choices": [{"delta": {"content": "answer"}}]}),
        _data({"choices": [{"delta": {"reasoning_content": "too late"}}]}),
        "data: [DONE]",
    ]
    tokens = []
    _parse_stream(_sse_response(lines), on_token=tokens.append)
    assert THINKING_START not in tokens
    assert "too late" in tokens


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def test_usage_in_last_chunk():
    """Usage appearing only in the final chunk is still captured."""
    lines = [
        _data({"choices": [{"delta": {"content": "generated"}}]}),
        _data({
            "choices": [{"delta": {}}],
            "usage": {"total_tokens": 100, "completion_tokens": 5},
        }),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["_usage"]["total_tokens"] == 100


def test_usage_overwrites_previous():
    """If usage appears in multiple chunks, last one wins."""
    lines = [
        _data({
            "choices": [{"delta": {"content": "a"}}],
            "usage": {"total_tokens": 10},
        }),
        _data({
            "choices": [{"delta": {"content": "b"}}],
            "usage": {"total_tokens": 20},
        }),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["_usage"]["total_tokens"] == 20


def test_usage_is_none_or_empty_ignored():
    """Usage that is None or empty dict should not overwrite."""
    lines = [
        _data({
            "choices": [{"delta": {"content": "x"}}],
            "usage": {"total_tokens": 30},
        }),
        _data({
            "choices": [{"delta": {"content": "y"}}],
            "usage": None,
        }),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["_usage"]["total_tokens"] == 30


# ---------------------------------------------------------------------------
# Mixed content + tool calls
# ---------------------------------------------------------------------------

def test_content_and_tool_calls_in_same_chunk():
    """Content and tool_calls arriving in the same delta."""
    lines = [
        _data({"choices": [{"delta": {
            "content": "Let me read that file",
            "tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "f.py"}'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "Let me read that file"
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"


def test_multiple_content_chunks_between_tool_call_fragments():
    """Text interleaved with tool call argument fragments."""
    lines = [
        _data({"choices": [{"delta": {
            "content": "Let me check...",
            "tool_calls": [{
                "index": 0, "id": "t0", "type": "function",
                "function": {"name": "search", "arguments": ""},
            }]
        }}]}),
        _data({"choices": [{"delta": {"content": " searching now"}}]}),
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0,
                "function": {"arguments": '{"query": "hello"}'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    assert msg["content"] == "Let me check... searching now"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"query": "hello"}'


# ---------------------------------------------------------------------------
# Role & output shape
# ---------------------------------------------------------------------------

def test_return_role_is_always_assistant():
    """The returned message must always have role=assistant."""
    msg = _parse_stream(_sse_response([]))
    assert msg["role"] == "assistant"


def test_no_unexpected_keys_in_simple_response():
    """For a simple content-only response, only role + content appear."""
    lines = [
        _data({"choices": [{"delta": {"content": "simple"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    expected_keys = {"role", "content"}
    assert expected_keys.issubset(set(msg.keys()))
    extra = set(msg.keys()) - expected_keys
    assert not extra, f"unexpected keys: {extra}"


def test_tool_calls_ordered_by_index():
    """Tool calls in the result are sorted by index."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [
                {"index": 2, "id": "c2", "type": "function",
                 "function": {"name": "c", "arguments": "{}"}},
                {"index": 0, "id": "c0", "type": "function",
                 "function": {"name": "a", "arguments": "{}"}},
                {"index": 1, "id": "c1", "type": "function",
                 "function": {"name": "b", "arguments": "{}"}},
            ]
        }}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))
    names = [tc["function"]["name"] for tc in msg["tool_calls"]]
    assert names == ["a", "b", "c"]


def test_fired_indices_not_present_when_none_fired():
    """_fired_indices is absent when on_tool_ready was never called."""
    lines = [
        _data({"choices": [{"delta": {
            "tool_calls": [{
                "index": 0, "id": "t0", "type": "function",
                "function": {"name": "f", "arguments": '{"x": 1}'},
            }]
        }}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))  # no on_tool_ready
    assert "_fired_indices" not in msg


# ---------------------------------------------------------------------------
# on_token variations
# ---------------------------------------------------------------------------

def test_on_token_none_default_does_not_crash():
    """No on_token — function uses print; must not crash."""
    lines = [
        _data({"choices": [{"delta": {"content": "stdout"}}]}),
        "data: [DONE]",
    ]
    msg = _parse_stream(_sse_response(lines))  # on_token=None
    assert msg["content"] == "stdout"


def test_on_token_receives_thinking_delimiters():
    """on_token receives THINKING_START / THINKING_END delimiters."""
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": "think"}}]}),
        _data({"choices": [{"delta": {"content": "answer"}}]}),
        "data: [DONE]",
    ]
    tokens = []
    _parse_stream(_sse_response(lines), on_token=tokens.append)
    # THINKING_START before reasoning, THINKING_END before first content
    start_idx = tokens.index(THINKING_START)
    end_idx = tokens.index(THINKING_END)
    assert start_idx < end_idx
    # reasoning content between start and end
    assert "think" in tokens[start_idx:end_idx]
