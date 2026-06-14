#!/usr/bin/env python3
"""
stream.py -- SSE stream parsing for mini_agent.

Provides ``_parse_stream()`` for parsing DeepSeek's server-sent event response
stream, accumulating text content, reasoning blocks, and tool call fragments.
Resilient to connection drops -- returns partial results instead of crashing.
"""
from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable

import requests

from terminal import c, DIM, GREEN

# Thinking-mode delimiters sent through the on_token stream
THINKING_START = "\n[thinking] "
THINKING_END = "\n[/thinking]"


# SSE prefix for DeepSeek's server-sent event stream
_SSE_PREFIX = "data: "

def _parse_stream(response: requests.Response, on_token: Callable[[str], None] | None = None, on_tool_ready: Callable[[dict], None] | None = None, cancel_event: threading.Event | None = None) -> dict:
    """Parse an SSE streamed response, printing text as it arrives.

    Accumulates both text content and tool_calls from deltas.  Tool call
    arguments arrive in fragments across multiple chunks and are reassembled
    by index.  Reasoning (thinking) content is printed dimmed in real-time
    for debugging visibility.

    If *on_token* is provided, it is called with each content token (str)
    instead of printing to stdout.

    If *on_tool_ready* is provided, it is called with a complete tool call
    dict the moment its arguments form valid JSON.  This allows incremental
    tool execution while the stream tail is still arriving.

    If the connection drops mid-stream, whatever was accumulated so far is
    returned rather than crashing -- a warning is printed to stderr.

    Returns a reconstructed message dict (role, content, optional
    reasoning_content, optional tool_calls).
    """
    full_content = ""
    full_reasoning = ""
    tool_calls_by_index: dict[int, dict] = {}  # index -> accumulated tc dict
    fired_indices: set[int] = set()
    reasoning_header_printed = False
    thinking_ended = False
    usage: dict | None = None

    if not on_token:
        print(flush=True)  # separate streaming output from the prompt line

    try:
        # NOTE: iter_lines has no per-line timeout.  If the server stalls
        # mid-stream (TCP open but no data), this blocks indefinitely.
        # Callers should set requests-level (connect, read) timeouts on the
        # session and consider wrapping in a timeout thread for long streams.
        for line in response.iter_lines(decode_unicode=True):
            if cancel_event is not None and cancel_event.is_set():
                break
            if not line or not line.startswith(_SSE_PREFIX):
                continue
            data_str = line[len(_SSE_PREFIX):]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                if "choices" not in chunk:
                    continue
                delta = chunk["choices"][0].get("delta", {})

                # Usage may appear in any chunk (usually the last)
                if "usage" in chunk and chunk["usage"]:
                    usage = chunk["usage"]

                # Text content -- print and accumulate
                if "content" in delta and delta["content"]:
                    if full_reasoning and not full_content:
                        # First content token after reasoning -- signal end of thinking
                        if on_token:
                            on_token(THINKING_END)
                        thinking_ended = True
                    full_content += delta["content"]
                    if on_token:
                        on_token(delta["content"])
                    else:
                        # In Electron/sub-agent mode, stdout is reserved for JSON messages.
                        # Fall back to stderr so we don't break the protocol.
                        print(delta["content"], end="", file=sys.stderr, flush=True)

                # Reasoning content (thinking mode) -- forward via on_token or print
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    if not reasoning_header_printed and not full_content:
                        if on_token:
                            on_token(THINKING_START)
                        else:
                            print(c("  thinking...", DIM), file=sys.stderr, flush=True)
                        reasoning_header_printed = True
                    full_reasoning += delta["reasoning_content"]
                    if on_token:
                        on_token(delta["reasoning_content"])
                    else:
                        print(c(delta["reasoning_content"], GREEN), end="", file=sys.stderr, flush=True)

                # Tool calls -- accumulate fragments by index and detect completion
                if "tool_calls" in delta:
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        tc = tool_calls_by_index[idx]
                        if "id" in tc_delta:
                            tc["id"] = tc_delta["id"]
                        if "type" in tc_delta:
                            tc["type"] = tc_delta["type"]
                        if "function" in tc_delta:
                            fn_delta = tc_delta["function"]
                            if "name" in fn_delta and fn_delta["name"]:
                                tc["function"]["name"] = fn_delta["name"]
                            if "arguments" in fn_delta:
                                tc["function"]["arguments"] += fn_delta["arguments"]

                        # Fire on_tool_ready when arguments form valid JSON.
                        # Brace-balance pre-check avoids most premature parse
                        # failures on still-fragmented arguments.
                        if on_tool_ready and idx not in fired_indices:
                            args = tc["function"]["arguments"]
                            if args.count("{") == args.count("}") and args.count("[") == args.count("]"):
                                try:
                                    json.loads(args)
                                    fired_indices.add(idx)
                                    ready = dict(tc)
                                    ready["_index"] = idx
                                    on_tool_ready(ready)
                                except (json.JSONDecodeError, ValueError):
                                    pass  # still fragmentary
            except (json.JSONDecodeError, KeyError, TypeError, IndexError, ValueError, AttributeError) as _e:
                print(f"[SSE] stream parse: {_e}", file=sys.stderr)
                continue
    except (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        requests.exceptions.StreamConsumedError,
        OSError,
    ) as exc:
        print(
            f"\n  WARNING: stream interrupted ({exc}) -- using partial response",
            file=sys.stderr, flush=True,
        )

    if full_reasoning and not on_token:
        print(file=sys.stderr, flush=True)  # newline after dimmed reasoning block

    if full_content and not on_token:
        print(flush=True)  # final newline after streamed text

    # If thinking was opened but never closed (e.g. reasoning-only response
    # with tool calls and no text content), close it now so subsequent tokens
    # in this turn don't get stuck in the thinking panel.
    if full_reasoning and not thinking_ended and on_token:
        on_token(THINKING_END)

    msg: dict = {"role": "assistant", "content": full_content}
    if full_reasoning:
        msg["reasoning_content"] = full_reasoning
    if usage:
        msg["_usage"] = usage

    if tool_calls_by_index:
        msg["tool_calls"] = [
            tool_calls_by_index[i]
            for i in sorted(tool_calls_by_index)
        ]
        # Tag which ones were already incrementally executed
        if fired_indices:
            msg["_fired_indices"] = list(fired_indices)

    return msg
