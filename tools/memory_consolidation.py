#!/usr/bin/env python3
"""
memory_consolidation.py — post-turn background memory consolidation.

Runs after each agent turn: extracts durable facts from the conversation
using a cheap model call and updates core memory via the memory_core tool.

Inspired by:
- Hermes Agent's "context distillation" after session end
- Mem0-style lightweight extraction
- The key insight: use a small, fast model (gpt-4o-mini / deepseek-chat)
  to pull out what's worth remembering, so the agent doesn't waste tokens
  re-discovering things every session.

Design:
- Non-blocking: consolidation runs in a background thread after the turn.
- Rate-limited: at most one consolidation in flight at a time.
- Cheap model: uses a configurable "consolidation model" (defaults to
  FAST_MODEL env var, falls back to the main model).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from logging_setup import get_logger, log_error_trace

_log = get_logger("memory_consolidation")

# --- Consolidation throttle ---
_CONSOLIDATION_LOCK = threading.Lock()
_LAST_CONSOLIDATION_TIME: float = 0.0
_MIN_CONSOLIDATION_INTERVAL: float = 5.0  # seconds between consolidations
_MAX_CONSOLIDATION_TURNS: int = 3         # consolidate every N turns

_consolidation_turn_counter: int = 0


def _get_memory_store() -> Any | None:
    """Get the MemoryStore from the tool context."""
    try:
        from tools import _TOOL_CONTEXT
        return getattr(_TOOL_CONTEXT, "_memory_store", None)
    except Exception:
        return None


def _get_consolidation_model(config: Any) -> str:
    """Pick a cheap model for consolidation.

    Priority: CONSOLIDATION_MODEL env var > FAST_MODEL env var > sub_agent_model > main model.
    All models are assumed compatible with the configured provider.
    """
    import os
    env_model = os.environ.get("CONSOLIDATION_MODEL") or os.environ.get("FAST_MODEL")
    if env_model:
        return env_model
    # Use sub_agent_model (cheaper worker model) if available, else main model, else provider default
    sub = getattr(config, "sub_agent_model", "")
    if sub:
        return sub
    model = getattr(config, "model", "")
    if model:
        return model
    # Hard fallback by provider
    provider = getattr(config, "api_provider", "deepseek")
    return {
        "deepseek": "deepseek-chat",
        "claude": "claude-3-haiku-20240307",
        "xai": "grok-2",
        "ollama": "llama3.2",
    }.get(provider, "deepseek-chat")


def _get_api_key(config: Any) -> str:
    """Get the API key from config or environment.

    Tries: config.api_key, then provider-specific env vars, then generic ones.
    """
    # Primary: the configured api_key
    key = getattr(config, "api_key", "")
    if key:
        return key
    # Provider-specific env vars (in priority order)
    import os as _os
    provider = getattr(config, "api_provider", "deepseek")
    provider_env_map = {
        "deepseek": ("DEEPSEEK_API_KEY",),
        "claude": ("ANTHROPIC_API_KEY",),
        "xai": ("XAI_API_KEY",),
        "ollama": ("OLLAMA_API_KEY",),
    }
    for env_var in provider_env_map.get(provider, ()):
        key = _os.environ.get(env_var, "")
        if key:
            return key
    # Generic fallbacks
    for env_var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        key = _os.environ.get(env_var, "")
        if key:
            return key
    return ""


def _get_api_base(config: Any) -> str:
    """Get the API base URL from config or environment.

    Uses the same api_url as the main agent so consolidation
    requests go to the correct provider (e.g. DeepSeek, not OpenAI).

    Returns the base URL WITHOUT the ``/chat/completions`` path suffix.
    The caller (``_extract_facts``) appends that.  This is important because
    ``config.api_url`` already includes ``/chat/completions`` (it's the full
    endpoint used directly by ``api.py``), so we strip it here to avoid
    double-pathing.
    """
    # config.api_url is the canonical attribute (set from TOML / env / provider defaults)
    url = getattr(config, "api_url", "") or getattr(config, "api_base", "")
    if url:
        # Strip /chat/completions suffix if present — the caller appends it.
        # config.api_url is the full endpoint (used directly by api.py),
        # but _extract_facts constructs the URL by appending to the base.
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        return url
    # Fall back to env vars (OPENAI_BASE_URL is common, but also check provider-specific)
    for env_var in ("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"):
        val = __import__("os").environ.get(env_var, "")
        if val:
            return val
    # Last resort: detect from api_provider
    provider = getattr(config, "api_provider", "deepseek")
    return {
        "deepseek": "https://api.deepseek.com/v1",
        "claude": "https://api.anthropic.com",
        "xai": "https://api.x.ai/v1",
        "ollama": "http://localhost:11434/v1",
    }.get(provider, "https://api.deepseek.com/v1")


# ---------------------------------------------------------------------------
# Consolidation prompt — minimal tokens, extracts only durable facts
# ---------------------------------------------------------------------------

_CONSOLIDATION_SYSTEM_PROMPT = """\
You are a memory consolidation engine. Extract ONLY durable, reusable facts
from the conversation that would help an AI agent in future sessions.

Rules:
- Extract facts about the project structure, conventions, user preferences,
  solutions to problems, patterns used, and workarounds discovered.
- DO NOT extract transient details (file paths that were temporary,
  one-off queries, simple file reads).
- Each fact should be ONE line, concise, and actionable.
- If there are no durable facts, output "NONE".
- Output format: one fact per line, no numbering, no commentary.

Current core memory (update/merge with this):
{existing_memory}
"""

_CONSOLIDATION_USER_TEMPLATE = """\
Recent conversation to extract facts from:

{conversation_snippet}
"""


# ---------------------------------------------------------------------------
# Extract facts via a cheap LLM call
# ---------------------------------------------------------------------------

def _extract_facts(
    conversation_snippet: str,
    existing_memory: str,
    config: Any,
    memory_store: Any | None = None,
) -> str | None:
    """Call a cheap model to extract durable facts from the conversation.

    Returns new facts as a multi-line string, or None if nothing to extract.
    """
    consolidation_model = _get_consolidation_model(config)
    api_key = _get_api_key(config)
    api_base = _get_api_base(config)

    if not api_key:
        _log.warning("No API key available for consolidation, skipping")
        return None

    system_prompt = _CONSOLIDATION_SYSTEM_PROMPT.format(
        existing_memory=existing_memory or "(empty)"
    )
    user_prompt = _CONSOLIDATION_USER_TEMPLATE.format(
        conversation_snippet=conversation_snippet
    )

    import requests
    try:
        resp = requests.post(
            f"{api_base}/chat/completions",
            json={
                "model": consolidation_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"].strip()

        if content.upper() == "NONE" or not content:
            return None
        return content
    except Exception as e:
        error_detail = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail += f" | body={e.response.text[:300]}"
            except Exception:
                pass
        log_error_trace("consolidation_extract", error_detail)
        return None


# ---------------------------------------------------------------------------
# Apply extracted facts to core memory
# ---------------------------------------------------------------------------

def _apply_facts_to_core(new_facts: str) -> bool:
    """Merge extracted facts into core memory via the memory_core tool."""
    try:
        from tools.memory_core import _memory_core
        from core.safety import WriteSafetyGate, ReadSafetyGate

        # Create minimal safety gates
        import os
        wg = WriteSafetyGate(
            allow_overwrites=True,
            unrestricted=True,
            workspace_root=os.getcwd(),
        )
        rg = ReadSafetyGate(
            unrestricted=True,
            workspace_root=os.getcwd(),
        )

        result = _memory_core(
            {"action": "add", "content": new_facts},
            wg,
            rg,
        )
        return result.success
    except Exception as e:
        log_error_trace("consolidation_apply", str(e))
        return False


# ---------------------------------------------------------------------------
# Main consolidation entry point
# ---------------------------------------------------------------------------

def consolidate_if_needed(
    messages: list[dict],
    config: Any,
    *,
    force: bool = False,
) -> None:
    """Run memory consolidation in a background thread if conditions are met.

    Called after each agent turn. Rate-limited: at most one consolidation
    every _MIN_CONSOLIDATION_INTERVAL seconds and every _MAX_CONSOLIDATION_TURNS.

    If *force* is True, skip rate limiting.
    """
    global _consolidation_turn_counter, _LAST_CONSOLIDATION_TIME

    _consolidation_turn_counter += 1

    if not force:
        # Rate limit by turn count
        if _consolidation_turn_counter < _MAX_CONSOLIDATION_TURNS:
            return
        # Rate limit by time
        now = time.time()
        if now - _LAST_CONSOLIDATION_TIME < _MIN_CONSOLIDATION_INTERVAL:
            return

    # Check if lock is already held (another consolidation in progress)
    if _CONSOLIDATION_LOCK.locked():
        return

    memory_store = _get_memory_store()
    if memory_store is None:
        return

    # Snapshot messages (last N messages to keep it cheap)
    conversation_snippet = _snapshot_conversation(messages)

    # Get existing core memory
    existing_memory = ""
    try:
        existing_memory = memory_store.get_core_memory()
    except Exception:
        pass

    # Run in background thread
    def _bg_consolidate() -> None:
        nonlocal existing_memory
        if not _CONSOLIDATION_LOCK.acquire(blocking=False):
            return
        try:
            _LAST_CONSOLIDATION_TIME = time.time()
            _consolidation_turn_counter = 0

            new_facts = _extract_facts(
                conversation_snippet, existing_memory, config, memory_store
            )
            if new_facts:
                success = _apply_facts_to_core(new_facts)
                if success:
                    _log.info(
                        "Consolidation: extracted %d fact lines into core memory",
                        len(new_facts.splitlines()),
                    )
        except Exception as e:
            log_error_trace("consolidation_background", str(e))
        finally:
            _CONSOLIDATION_LOCK.release()

    t = threading.Thread(target=_bg_consolidate, daemon=True, name="mem-consolidator")
    t.start()


def _snapshot_conversation(messages: list[dict], max_messages: int = 20) -> str:
    """Create a compact snapshot of recent conversation for extraction."""
    recent = messages[-max_messages:] if len(messages) > max_messages else messages
    parts: list[str] = []
    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str) and content:
            # Truncate long tool results
            if role == "tool" and len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"[{role}] {content[:400]}")
        elif isinstance(content, list):
            # Multimodal content
            parts.append(f"[{role}] (multimodal content)")
    return "\n".join(parts)
