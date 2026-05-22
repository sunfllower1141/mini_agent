#!/usr/bin/env python3
"""
api.py — LLM API communication for mini_agent.

Provides ``call_llm()`` for non-streaming and streaming API
requests, with provider dispatch for DeepSeek and Claude (via
Anthropic's OpenAI-compatible endpoint).  Extracted from llm.py
to break the circular dependency chain:
llm.py -> tools -> agent_ops -> sub_agent -> llm.py.

Both ``llm.py`` and ``sub_agent.py`` import from here — no cycle.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Callable
from typing import Any

import requests

from config import AgentConfig
from retry import _request_with_retry
from stream import _parse_stream
from tools.schema import TOOLS
from tools.skills import get_active_tools

# ---------------------------------------------------------------------------
# API rate limiter — prevents thundering-herd when N sub-agents share one key
# ---------------------------------------------------------------------------
# All LLM API calls (parent + sub-agents) funnel through this semaphore.
# Default is 2 concurrent calls; set SUB_AGENT_MAX_CONCURRENT_CALLS env var
# to override (e.g. for higher-tier API keys with looser rate limits).
_MAX_CONCURRENT_LLM_CALLS = int(
    __import__("os").environ.get("SUB_AGENT_MAX_CONCURRENT_CALLS", "2")
)
_LLM_SEMAPHORE = threading.Semaphore(_MAX_CONCURRENT_LLM_CALLS)


# ---------------------------------------------------------------------------
# APIError exception class
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Raised when the LLM API returns a non-OK HTTP status."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"API {status_code}: {body}")

    def __str__(self) -> str:
        return f"APIError({self.status_code}): {self.body}"


# ---------------------------------------------------------------------------
# Shared truncation / utility functions
# ---------------------------------------------------------------------------

def truncate_content(content: str, max_len: int = 300) -> str:
    """Truncate a string to *max_len* chars, appending '...' if truncated."""
    if len(content) <= max_len:
        return content
    return content[:max_len] + "\u2026"


def format_tool_detail(result: "ToolResult", max_len: int = 300) -> str:
    """Format a ToolResult's content for display, truncated to *max_len*."""
    detail = result.content[:max_len]
    if len(result.content) > max_len:
        detail += "\u2026"
    return detail


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

# Incremental message cleaning cache: keyed by id(messages), stores a tuple
# of (last_cleaned_len, provider, clean_messages) so repeated calls within a
# turn only clean newly appended messages rather than the entire list.
_clean_messages_cache: dict[int, tuple[int, str, list[dict]]] = {}


def _clean_message(msg: dict, index: int, provider: str = "deepseek") -> dict:
    """Clean a single message dict for sending to the API.

    Strips internal tracking fields (keys starting with '_'), removes the
    ``index`` field from tool_calls.  For DeepSeek, marks the first system
    message with ``cache_control`` for prompt caching (not supported by
    Claude's OpenAI-compatible endpoint).
    """
    m2 = {k: v for k, v in msg.items()
          if not k.startswith("_")}
    if "tool_calls" in m2:
        m2["tool_calls"] = [
            {k: v for k, v in tc.items() if k != "index"}
            for tc in m2["tool_calls"]
        ]
    if index == 0 and m2.get("role") == "system" and provider == "deepseek":
        m2["cache_control"] = {"type": "ephemeral"}
    return m2


# Simple-prompt keywords for model routing.
_ROUTE_SIMPLE_KEYWORDS = re.compile(
    r"\b(write|edit|delete|create|modify|refactor|implement|build|fix|patch|"
    r"restructure|rewrite|replace|change|update|rename|move|remove|add)\b",
    re.IGNORECASE,
)


def _compute_complexity(messages: list[dict]) -> str:
    """Return 'simple' or 'complex' for the last user message, for model routing."""
    if not messages:
        return "complex"
    # Check the last 2 user messages
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text += " " + str(m.get("content", ""))
            if len(user_text) > 2000:
                break
    if len(user_text) < 300 and not _ROUTE_SIMPLE_KEYWORDS.search(user_text):
        return "simple"
    return "complex"


def _strip_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """Remove trailing assistant messages whose tool_calls lack matching
    tool result messages.  Prevents 400 "insufficient tool messages
    following tool_calls" errors from the API.

    Returns a new list (never mutates the cached *messages* list).
    """
    # Walk backwards tracking which tool_call_ids have been seen.
    seen_ids: set[str] = set()
    strip_count = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        role = m.get("role", "")
        if role == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                seen_ids.add(tcid)
        elif role == "assistant" and "tool_calls" in m:
            tc_ids = [tc.get("id") for tc in m.get("tool_calls", []) if tc.get("id")]
            if all(tcid in seen_ids for tcid in tc_ids):
                # Fully covered — not orphaned. Continue scanning.
                continue
            else:
                # Orphan: assistant has tool_calls with no matching results after it.
                strip_count += 1
                continue
        elif role in ("user", "system"):
            # user / system — stop, they break the tool-result chain
            break
        # else: text-only assistant or other — keep scanning backwards;
        # a text-only assistant does not break the tool-call chain, it
        # just sits between completed tool-call blocks.
    if strip_count:
        return messages[:-strip_count]
    return messages


def _build_payload(
    config: AgentConfig,
    messages: list[dict],
    clean_messages: list[dict],
) -> dict:
    """Build the JSON payload for an API request, adapting to the provider.

    Claude's OpenAI-compatible endpoint does not support:
    - ``frequency_penalty``
    - ``presence_penalty``
    - ``top_p`` (rejected by Claude 4.x models; Opus 4.7 rejects all sampling params)
    - ``response_format``
    - ``cache_control`` (handled in ``_clean_message``)
    """
    provider = config.api_provider

    # Model selection (routing model for simple prompts, if configured)
    model = config.model
    if config.routing_model and _compute_complexity(messages) == "simple":
        model = config.routing_model

    payload: dict = {
        "model": model,
        "messages": clean_messages,
        "tools": get_active_tools(),
        "stream": config.stream,
        "max_tokens": config.max_tokens,
    }

    # --- provider-specific parameters ---
    if provider == "deepseek":
        payload["temperature"] = config.temperature
        payload["frequency_penalty"] = config.frequency_penalty
        payload["presence_penalty"] = config.presence_penalty
        if config.stop_sequences:
            payload["stop"] = config.stop_sequences
        if config.response_format:
            payload["response_format"] = {"type": config.response_format}

    elif provider == "claude":
        # Claude OpenAI-compat: no temperature, top_p, freq/presence penalties,
        # or response_format. Claude 4.x models reject top_p + temperature combos,
        # and Opus 4.7 rejects all sampling parameters entirely.
        # Rely on Anthropic's defaults for sampling behaviour.
        if config.stop_sequences:
            payload["stop"] = config.stop_sequences

    elif provider == "xai":
        # xAI/Grok reasoning models (grok-4.3, etc.) do not support
        # frequency_penalty, presence_penalty, or stop.
        # Sending them returns an error.
        # https://docs.x.ai/docs/guides/reasoning
        payload["temperature"] = config.temperature
        if config.response_format:
            payload["response_format"] = {"type": config.response_format}

    elif provider == "ollama":
        # Ollama's OpenAI-compatible endpoint supports temperature, stop, and tools.
        # No frequency_penalty, presence_penalty, or response_format.
        # Tool calling works with recent models (qwen3.6, llama3.x, etc.)
        payload["temperature"] = config.temperature
        if config.stop_sequences:
            payload["stop"] = config.stop_sequences

    return payload


def call_llm(
    messages: list[dict],
    config: AgentConfig,
    on_token: Callable[[str], Any] | None = None,
    session: requests.Session | None = None,
    on_tool_ready: Callable[[dict], Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict | None:
    """Send messages to the LLM, return the assistant message dict.

    Dispatches to the configured provider (DeepSeek or Claude via
    Anthropic's OpenAI-compatible endpoint).  Both use the same
    OpenAI-compatible JSON format, so no message translation is needed.

    Returns a message dict with ``content`` and optionally ``tool_calls``.
    When *stream* is True, text content is printed chunk-by-chunk as it
    arrives and tool_calls are accumulated from the stream (single-pass).

    Automatically retries on transient failures (429, 5xx) up to 3 times
    with exponential backoff.  If *session* is provided it is used for
    connection reuse across calls within a turn.
    """
    if session is None:
        session = requests  # use module-level .post (testable via mock)

    provider = config.api_provider

    # Incremental cleaning: only clean messages appended since last call.
    # This avoids O(n) deep-copy of the entire message list on every API call.
    list_id = id(messages)
    cached_entry = _clean_messages_cache.get(list_id)
    if cached_entry is not None:
        cached_len, cached_provider, clean_messages = cached_entry
    else:
        cached_len, cached_provider, clean_messages = 0, provider, []

    current_len = len(messages)

    # Invalidate cache if provider changed mid-session
    if cached_provider != provider:
        _clean_messages_cache.clear()
        cached_len, cached_provider, clean_messages = 0, provider, []

    if cached_len >= current_len:
        # Same list, no new messages — reuse cache as-is
        pass
    else:
        # Clean any new messages beyond the cached length
        for i in range(cached_len, current_len):
            clean_messages.append(_clean_message(messages[i], i, provider))
        _clean_messages_cache[list_id] = (current_len, provider, clean_messages)

    # Safety net: strip any trailing assistant(tool_calls) messages that
    # lack matching tool result messages.  An orphan here causes a 400
    # "insufficient tool messages following tool_calls" from the API.
    safe_messages = _strip_orphaned_tool_calls(clean_messages)

    payload = _build_payload(config, messages, safe_messages)

    # Build proxies dict if SOCKS proxy is configured
    proxies_dict = None
    if config.socks_proxy:
        proxies_dict = {"http": config.socks_proxy, "https": config.socks_proxy}

    # Anthropic's OpenAI-compatible endpoint uses Bearer auth (same as DeepSeek)
    # Gate all LLM API calls through a semaphore to prevent thundering-herd
    # rate-limit storms when N sub-agents share the same API key.
    acquired = _LLM_SEMAPHORE.acquire(timeout=120)  # 2 min max wait
    if not acquired:
        raise APIError(
            status_code=429,
            body="API rate limiter: timed out waiting for a free call slot (120s). "
                 "Too many concurrent LLM calls. Reduce sub-agent count or increase "
                 "SUB_AGENT_MAX_CONCURRENT_CALLS env var."
        )
    try:
        r = _request_with_retry(
            session,
            config.api_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "mini_agent/1.0",
            },
            json=payload,
            stream=config.stream,
            cancel_event=cancel_event,
            proxies=proxies_dict,
        )
    finally:
        _LLM_SEMAPHORE.release()

    if r is None:
        return None  # cancelled during retry

    if not r.ok:
        try:
            err = r.json()
        except (ValueError, AttributeError):
            err = r.text
        # --- Persist full error payload/response to api_error.log ---
        import datetime as _dt, os as _os
        _log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "api_error.log")
        try:
            with open(_log_path, "a") as _f:
                _f.write(f"\n{'='*80}\n")
                _f.write(f"TIME: {_dt.datetime.now().isoformat()}\n")
                _f.write(f"STATUS: {r.status_code}\n")
                _f.write(f"RESPONSE_BODY:\n{str(err)}\n")
                _f.write(f"PAYLOAD_MESSAGES_COUNT: {len(safe_messages)}\n")
                _f.write(f"PAYLOAD:\n{json.dumps(payload, indent=2, default=str)}\n")
                _f.write(f"{'='*80}\n")
        except OSError:
            pass  # Don't let logging break the error path
        raise APIError(status_code=r.status_code, body=str(err))

    if config.stream:
        return _parse_stream(r, on_token, on_tool_ready, cancel_event=cancel_event)
    else:
        return r.json()["choices"][0]["message"]


# Backward-compatible alias
call_deepseek = call_llm


def clear_api_cache() -> None:
    """Clear the incremental message-cleaning cache (called at turn start)."""
    _clean_messages_cache.clear()
