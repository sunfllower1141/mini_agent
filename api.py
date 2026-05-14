#!/usr/bin/env python3
"""
api.py — DeepSeek API communication for mini_agent.

Provides ``call_deepseek()`` for non-streaming and streaming API
requests.  Extracted from llm.py to break the circular dependency
chain: llm.py → tools → agent_ops → sub_agent → llm.py.

Both ``llm.py`` and ``sub_agent.py`` import from here — no cycle.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from typing import Any

import requests

from config import AgentConfig
from retry import _request_with_retry
from stream import _parse_stream, THINKING_START, THINKING_END
from tools.schema import TOOLS


# ---------------------------------------------------------------------------
# Shared truncation / utility functions
# ---------------------------------------------------------------------------

def truncate_content(content: str, max_len: int = 300) -> str:
    """Truncate a string to *max_len* chars, appending '…' if truncated."""
    if len(content) <= max_len:
        return content
    return content[:max_len] + "…"


def format_tool_detail(result: "ToolResult", max_len: int = 300) -> str:
    """Format a ToolResult's content for display, truncated to *max_len*."""
    from tools import ToolResult as TR
    detail = result.content[:max_len]
    if len(result.content) > max_len:
        detail += "…"
    return detail


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

# Incremental message cleaning cache: keyed by id(messages), stores a tuple
# of (last_cleaned_len, clean_messages) so repeated calls within a turn
# only clean newly appended messages rather than the entire list.
_clean_messages_cache: dict[int, tuple[int, list[dict]]] = {}


def _clean_message(msg: dict, index: int) -> dict:
    """Clean a single message dict for sending to the API.

    Strips internal tracking fields (keys starting with '_'), removes the
    ``index`` field from tool_calls, and marks the first system message
    with ``cache_control`` for prompt caching.
    """
    m2 = {k: v for k, v in msg.items()
          if not k.startswith("_")}
    if "tool_calls" in m2:
        m2["tool_calls"] = [
            {k: v for k, v in tc.items() if k != "index"}
            for tc in m2["tool_calls"]
        ]
    if index == 0 and m2.get("role") == "system":
        m2["cache_control"] = {"type": "ephemeral"}
    return m2


def call_deepseek(
    messages: list[dict],
    config: AgentConfig,
    on_token: Callable[[str], Any] | None = None,
    session: requests.Session | None = None,
    on_tool_ready: Callable[[dict], Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict | None:
    """Send messages to DeepSeek, return the assistant message dict.

    DeepSeek thinking mode requires ``reasoning_content`` to be passed back
    on subsequent requests. The ``index`` field inside tool_calls must be
    stripped (it is an output-only artefact).

    Returns a message dict with ``content`` and optionally ``tool_calls``.
    When *stream* is True, text content is printed chunk-by-chunk as it
    arrives and tool_calls are accumulated from the stream (single-pass).

    Automatically retries on transient failures (429, 5xx) up to 3 times
    with exponential backoff.  If *session* is provided it is used for
    connection reuse across calls within a turn.
    """
    if session is None:
        session = requests  # use module-level .post (testable via mock)

    # Incremental cleaning: only clean messages appended since last call.
    # This avoids O(n) deep-copy of the entire message list on every API call.
    list_id = id(messages)
    cached_len, clean_messages = _clean_messages_cache.get(list_id, (0, []))
    current_len = len(messages)
    if list_id in _clean_messages_cache and cached_len >= current_len:
        # Same list, no new messages — reuse cache as-is
        pass
    else:
        # Clean any new messages beyond the cached length
        for i in range(cached_len, current_len):
            clean_messages.append(_clean_message(messages[i], i))
        _clean_messages_cache[list_id] = (current_len, clean_messages)

    r = _request_with_retry(
        session,
        config.api_url,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.model,
            "messages": clean_messages,
            "tools": TOOLS,
            "stream": config.stream,
        },
        stream=config.stream,
        cancel_event=cancel_event,
    )

    if r is None:
        return None  # cancelled during retry

    if not r.ok:
        try:
            err = r.json()
        except (ValueError, AttributeError):
            err = r.text
        print(f"\n[API {r.status_code}] {err}", file=sys.stderr, flush=True)
    r.raise_for_status()

    if config.stream:
        return _parse_stream(r, on_token, on_tool_ready)
    else:
        return r.json()["choices"][0]["message"]


def clear_api_cache() -> None:
    """Clear the incremental message-cleaning cache (called at turn start)."""
    _clean_messages_cache.clear()
