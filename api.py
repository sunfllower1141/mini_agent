#!/usr/bin/env python3
"""
api.py -- LLM API communication for mini_agent.

Provides ``call_llm()`` for non-streaming and streaming API
requests, with provider dispatch for DeepSeek and Claude (via
Anthropic's OpenAI-compatible endpoint).  Extracted from llm.py
to break the circular dependency chain:
llm.py -> tools -> agent_ops -> sub_agent -> llm.py.

Both ``llm.py`` and ``sub_agent.py`` import from here -- no cycle.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tools import ToolResult

import requests

from core.config import AgentConfig
from retry import _request_with_retry
from stream import _parse_stream
from tools.skills import get_active_tools
from tools.semantic_cache import get_semantic_cache
from logging_setup import log_api_error

# ---------------------------------------------------------------------------
# API rate limiter -- prevents thundering-herd when N sub-agents share one key
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
    return content[:max_len] + "..."


def format_tool_detail(result: "ToolResult", max_len: int = 300) -> str:
    """Format a ToolResult's content for display, truncated to *max_len*."""
    detail = result.content[:max_len]
    if len(result.content) > max_len:
        detail += "..."
    return detail


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

# Incremental message cleaning cache: keyed by id(messages), stores a tuple
# of (last_cleaned_len, provider, clean_messages) so repeated calls within a
# turn only clean newly appended messages rather than the entire list.
_clean_messages_cache: dict[int, tuple[int, str, list[dict], int]] = {}
_clean_messages_cache_lock: threading.Lock = threading.Lock()
_MAX_CLEAN_CACHE_ENTRIES = 16  # cap to prevent unbounded growth from stale entries


def _clean_message(msg: dict, index: int, provider: str = "deepseek") -> dict | None:
    """Clean a single message dict for sending to the API.

    Strips internal tracking fields (keys starting with '_'), removes the
    ``index`` field from tool_calls.  Returns ``None`` for transient
    messages that should never be sent to the API (scratchpad nudges,
    progress reminders, circuit breaker warnings, etc.).

    For DeepSeek, marks the first system message with ``cache_control``
    for prompt caching (not supported by Claude's OpenAI-compatible
    endpoint).
    """
    if msg.get("_transient"):
        return None
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


# _strip_orphaned_tool_messages moved to memory/memory_prune.py -- canonical
# single source of truth.  Backward-compatible aliases kept here.
from memory.memory_prune import _strip_orphaned_tool_messages  # noqa: E402

_strip_orphaned_tool_calls = _strip_orphaned_tool_messages
_strip_orphaned_tool_results = _strip_orphaned_tool_messages


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

    model = config.model
    tools = get_active_tools()

    payload: dict = {
        "model": model,
        "messages": clean_messages,
        "tools": tools,
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
        # Enable thinking mode for DeepSeek V4 at full reasoning depth.
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "high"

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
    # Fingerprint to detect id() reuse: if Python recycles the same id for a
    # new list, the first element's identity will differ.
    fp = id(messages[0]) if messages else 0
    with _clean_messages_cache_lock:
        cached_entry = _clean_messages_cache.get(list_id)
        if cached_entry is not None:
            cached_len, cached_provider, clean_messages, cached_fp = cached_entry
            # Detect id() reuse: same list_id but different first element.
            if cached_fp != fp:
                cached_len, cached_provider, clean_messages = 0, provider, []
        else:
            cached_len, cached_provider, clean_messages, cached_fp = 0, provider, [], 0

        current_len = len(messages)

        # Invalidate cache if provider changed mid-session
        if cached_provider != provider:
            _clean_messages_cache.clear()
            cached_len, cached_provider, clean_messages, cached_fp = 0, provider, [], 0

        if cached_len >= current_len:
            # Same list, no new messages -- reuse cache as-is
            pass
        else:
            # Clean any new messages beyond the cached length
            for i in range(cached_len, current_len):
                cleaned = _clean_message(messages[i], i, provider)
                if cleaned is not None:
                    clean_messages.append(cleaned)
            _clean_messages_cache[list_id] = (current_len, provider, clean_messages, fp)
            # Cap cache size: evict oldest entry when over limit.
            # Python 3.7+ dicts preserve insertion order, so the first
            # key is the oldest.
            if len(_clean_messages_cache) > _MAX_CLEAN_CACHE_ENTRIES:
                _clean_messages_cache.pop(next(iter(_clean_messages_cache)))

    # Safety net: strip orphaned tool calls/results in one O(n) pass.
    # Memory pruning can leave orphaned tool messages or assistant(tool_calls)
    # causing 400 errors from the API.
    safe_messages = _strip_orphaned_tool_messages(clean_messages)

    payload = _build_payload(config, messages, safe_messages)

    # --- Semantic cache check (Layer 1: bypass API entirely on similar query) ---
    # Only check cache for non-streaming, non-cancelled calls.
    # Extract last user message ONCE -- used for both cache lookup and storage.
    _last_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user" and not m.get("_transient"):
            _last_user_text = m.get("content", "") or ""
            break

    if _last_user_text and not config.stream and cancel_event is None:
        _cache = get_semantic_cache()
        _cached_response, _cache_sim = _cache.lookup(_last_user_text)
        if _cached_response is not None:
            _log = __import__("logging_setup", fromlist=["get_logger"]).get_logger("api")
            _log.info(
                "semantic_cache_hit similarity=%.3f model=%s text=%s",
                _cache_sim,
                _cached_response.get("model", payload.get("model", "?")),
                _last_user_text[:80].replace("\n", " "),
            )
            # Track semantic cache stats on _TOOL_CONTEXT
            try:
                from tools import _TOOL_CONTEXT
                if _TOOL_CONTEXT is not None:
                    if not hasattr(_TOOL_CONTEXT, "_semantic_cache_stats"):
                        _TOOL_CONTEXT._semantic_cache_stats = {
                            "hits": 0, "misses": 0, "estimated_usd_saved": 0.0,
                        }
                    _TOOL_CONTEXT._semantic_cache_stats["hits"] += 1
            except Exception:
                pass
            return _cached_response

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
        )
    finally:
        _LLM_SEMAPHORE.release()

    if r is None:
        return None  # cancelled during retry

    # --- Multi-provider fallback (Layer 5: on 429/5xx, try next provider) ---
    _fallback_providers: tuple[str, ...] = ()
    try:
        from core.config import PROVIDER_DEFAULTS
        pd = PROVIDER_DEFAULTS.get(config.api_provider)
        if pd:
            _fallback_providers = pd.fallback_providers
    except Exception:
        _fallback_providers = ()

    _last_error: APIError | None = None
    if not r.ok and _fallback_providers and not config.stream:
        try:
            err_body = r.json()
        except (ValueError, AttributeError):
            err_body = r.text
        log_api_error(
            provider=config.api_provider, model=payload.get("model", "?"),
            status_code=r.status_code, error_body=str(err_body),
            turn=getattr(config, "turn_count", 0),
        )
        _last_error = APIError(status_code=r.status_code, body=str(err_body))

        for fb_prov in _fallback_providers:
            fb = PROVIDER_DEFAULTS.get(fb_prov)
            if not fb:
                continue
            fb_key = _get_fallback_api_key(fb_prov)
            if not fb_key:
                continue
            fb_payload = dict(payload)
            fb_payload["model"] = fb.model
            fb_payload.pop("cache_control", None)
            fb_payload.pop("thinking", None)
            fb_payload.pop("reasoning_effort", None)
            fb_payload.pop("frequency_penalty", None)
            fb_payload.pop("presence_penalty", None)
            fb_payload.pop("response_format", None)

            acquired2 = _LLM_SEMAPHORE.acquire(timeout=60)
            if not acquired2:
                continue
            try:
                r2 = _request_with_retry(
                    session, fb.api_url,
                    headers={
                        "Authorization": f"Bearer {fb_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "mini_agent/1.0",
                    },
                    json=fb_payload, stream=False,
                    cancel_event=cancel_event,
                )
            finally:
                _LLM_SEMAPHORE.release()

            if r2 is None:
                continue
            if r2.ok:
                r = r2
                payload = fb_payload
                _last_error = None
                break
            else:
                try:
                    fb_err_body = r2.json()
                except (ValueError, AttributeError):
                    fb_err_body = r2.text
                _last_error = APIError(r2.status_code, str(fb_err_body))

    if _last_error is not None:
        raise _last_error

    if not r.ok:
        try:
            err = r.json()
        except (ValueError, AttributeError):
            err = r.text
        log_api_error(
            provider=config.api_provider, model=payload.get("model", "?"),
            status_code=r.status_code, error_body=str(err),
            turn=getattr(config, "turn_count", 0),
        )
        raise APIError(status_code=r.status_code, body=str(err))

    if config.stream:
        msg = _parse_stream(r, on_token, on_tool_ready, cancel_event=cancel_event)
    else:
        body = r.json()
        msg = body["choices"][0]["message"]
        # Capture usage from non-streaming response
        usage = body.get("usage", {})
        if usage:
            msg["_usage"] = usage

    # --- Cache hit monitoring ---
    # DeepSeek returns prompt_cache_hit_tokens / prompt_cache_miss_tokens in
    # usage.  Log the hit rate and store on _TOOL_CONTEXT for session_stats.
    _report_cache_hit(msg.get("_usage", {}), config)

    # --- Semantic cache storage (Layer 1: store non-tool responses) ---
    _store_semantic_cache(msg, _last_user_text, payload, config)

    return msg


def _report_cache_hit(usage: dict, config: "AgentConfig") -> None:
    """Log prompt cache hit rate from API usage and store for session_stats."""
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", 0)
    total_prompt_cache = hit + miss
    if total_prompt_cache <= 0:
        return
    hit_rate = 100.0 * hit / total_prompt_cache
    _log = __import__("logging_setup", fromlist=["get_logger"]).get_logger("api")
    _log.info(
        "cache_hit=%.1f%% hit_tokens=%d miss_tokens=%d prompt=%d completion=%d",
        hit_rate, hit, miss,
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
    )
    # Store for session_stats visibility
    try:
        from tools import _TOOL_CONTEXT
        if _TOOL_CONTEXT is not None:
            if not hasattr(_TOOL_CONTEXT, "_cache_stats"):
                _TOOL_CONTEXT._cache_stats = {
                    "hits": 0, "misses": 0, "calls": 0,
                    "input_tokens": 0, "output_tokens": 0,
                }
            _TOOL_CONTEXT._cache_stats["hits"] += hit
            _TOOL_CONTEXT._cache_stats["misses"] += miss
            _TOOL_CONTEXT._cache_stats["calls"] += 1
            _TOOL_CONTEXT._cache_stats["input_tokens"] += usage.get("prompt_tokens", 0)
            _TOOL_CONTEXT._cache_stats["output_tokens"] += usage.get("completion_tokens", 0)
            # --- Per-turn tracking for degradation detection ---
            turn = int(getattr(_TOOL_CONTEXT, "_turn_count", 0) or 0)
            if not hasattr(_TOOL_CONTEXT, "_cache_turn_history"):
                _TOOL_CONTEXT._cache_turn_history = []
            history = _TOOL_CONTEXT._cache_turn_history
            # Append per-turn entry (or merge into last entry if same turn)
            if history and history[-1].get("turn") == turn:
                entry = history[-1]
            else:
                entry = {"turn": turn, "hits": 0, "misses": 0, "calls": 0}
                history.append(entry)
                # Cap at 64 entries (prevents unbounded growth)
                if len(history) > 64:
                    history[:] = history[-64:]
            entry["hits"] += hit
            entry["misses"] += miss
            entry["calls"] += 1
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cache degradation detection
# ---------------------------------------------------------------------------

# Thresholds for anomaly alerting
_DEGRADE_MIN_CALLS = 3           # need at least 3 turns with cache data
_DEGRADE_MIN_BASELINE_CALLS = 2  # baseline needs at least 2 turns
_DEGRADE_RATIO_THRESHOLD = 0.50  # alert if hit rate < 50% of baseline
_DEGRADE_ABSOLUTE_THRESHOLD = 25.0  # alert if hit rate < 25% (absolute)
_DEGRADE_ALERT_COOLDOWN = 8      # turns before re-alerting


def _check_cache_degradation() -> str | None:
    """Return an alert string if cache hit rate has degraded, else None.

    Compares recent turns (last 3) against baseline (all earlier turns).
    Fires only once per cooldown period to avoid alert fatigue.

    Returns a one-line warning suitable for injecting as a transient user
    message, or None when everything looks normal.
    """
    try:
        from tools import _TOOL_CONTEXT
        if _TOOL_CONTEXT is None:
            return None
        history = getattr(_TOOL_CONTEXT, "_cache_turn_history", None)
        if not history or len(history) < _DEGRADE_MIN_CALLS:
            return None

        # Enforce cooldown (only if an alert has actually fired before)
        last_alert_turn = getattr(_TOOL_CONTEXT, "_cache_alert_last_turn", 0)
        current_turn = int(getattr(_TOOL_CONTEXT, "_turn_count", 0) or 0)
        if last_alert_turn > 0 and (current_turn - last_alert_turn) < _DEGRADE_ALERT_COOLDOWN:
            return None

        # Split: last 3 turns vs all earlier
        recent = history[-3:]
        baseline = history[:-3]
        if len(baseline) < _DEGRADE_MIN_BASELINE_CALLS:
            return None

        def _rate(entries):
            h = sum(e["hits"] for e in entries)
            m = sum(e["misses"] for e in entries)
            total = h + m
            return (h / total * 100) if total > 0 else 0.0

        baseline_rate = _rate(baseline)
        recent_rate = _rate(recent)

        # Only alert on meaningful degradation
        if baseline_rate <= 0:
            return None
        ratio = recent_rate / baseline_rate

        if ratio < _DEGRADE_RATIO_THRESHOLD or recent_rate < _DEGRADE_ABSOLUTE_THRESHOLD:
            _TOOL_CONTEXT._cache_alert_last_turn = current_turn
            return (
                f"WARNING: Cache hit rate dropped to {recent_rate:.0f}% "
                f"(was {baseline_rate:.0f}% avg). "
                f"Session may restart cheaply."
            )
        return None
    except Exception:
        return None


# Backward-compatible alias
call_deepseek = call_llm


def _get_fallback_api_key(provider: str) -> str:
    """Get the API key for a fallback provider from environment.

    Each provider has its own env var: DEEPSEEK_API_KEY, CLAUDE_API_KEY, etc.
    """
    env_map = {
        "deepseek": "DEEPSEEK_API_KEY",
        "claude": "CLAUDE_API_KEY",
        "xai": "XAI_API_KEY",
        "ollama": "OLLAMA_API_KEY",
    }
    env_var = env_map.get(provider, "")
    if not env_var:
        return ""
    import os
    return os.environ.get(env_var, "")


def clear_api_cache() -> None:
    """Clear the message-cleaning cache (called at turn start)."""
    _clean_messages_cache.clear()

    # Also clear semantic cache at session end / reset
    from tools.semantic_cache import clear_semantic_cache
    clear_semantic_cache()


def _store_semantic_cache(
    msg: dict,
    last_user_text: str,
    payload: dict,
    config: "AgentConfig",
) -> None:
    """Store a non-tool-call LLM response in the semantic cache.

    Only cache plain text responses (no tool_calls) from non-streaming calls.
    The cached response includes the model tag so the UI can show it on replay.
    """
    if not last_user_text:
        return
    if msg.get("tool_calls"):
        return  # don't cache tool-call responses
    content = msg.get("content")
    if not content or not str(content).strip():
        return  # don't cache empty responses

    model = payload.get("model", "?")
    usage = msg.get("_usage", {})
    input_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    output_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0

    # Get pricing for savings estimate
    from core.config import PROVIDER_DEFAULTS
    provider = config.api_provider
    pd_defaults = PROVIDER_DEFAULTS.get(provider)
    input_price = pd_defaults.input_price if pd_defaults else 0.0
    output_price = pd_defaults.output_price if pd_defaults else 0.0

    cache = get_semantic_cache()
    # Build a clean cached response dict (no _usage, no streaming metadata)
    cached_response = {
        "role": "assistant",
        "content": content,
        "model": model,
    }
    cache.store(
        query_text=last_user_text,
        response=cached_response,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_per_million_input=input_price,
        cost_per_million_output=output_price,
    )
