#!/usr/bin/env python3
"""
context_error_detect.py -- Multi-provider context-window-exceeded detection.

Adopted from Dirac's src/core/context/context-management/context-error-handling.ts.

Detects context-window-exceeded errors across providers (OpenAI, Anthropic,
OpenRouter, Bedrock, Cerebras, Vercel, DeepSeek) so the agent can trigger
compaction instead of crashing.

Pattern matching covers:
- OpenAI / DeepSeek: "context_length_exceeded", "Please reduce the length"
- Anthropic: 400 with "prompt is too long" / "input tokens exceed"
- OpenRouter: 400 with context-window messages or JSON-encoded errors mid-stream
- Bedrock: ValidationException with token-exceed patterns
- Vercel: AI_APICallError context-length-exceeded
- Cerebras: "input is too long" / "exceeds model limit"
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Compiled regex patterns (shared across providers)
# ---------------------------------------------------------------------------

_CONTEXT_ERROR_PATTERNS: list[re.Pattern] = [
    re.compile(r"context_length_exceeded", re.IGNORECASE),  # OpenAI error code
    re.compile(r"context[\s_]*length\b.*exceed", re.IGNORECASE),  # also matches context_length
    re.compile(r"context[\s_]*window\b.*exceed", re.IGNORECASE),  # also matches context_window
    re.compile(r"input is too long", re.IGNORECASE),
    re.compile(r"input token count exceeds.*maximum.*tokens?\s+allowed", re.IGNORECASE),
    re.compile(r"input exceeds.*context window", re.IGNORECASE),
    re.compile(r"requested input length.*exceeds.*maximum input length", re.IGNORECASE),
    re.compile(r"prompt is too long.*tokens?\s*>\s*\d+\s*maximum", re.IGNORECASE),
    re.compile(r"\bcontext\s*(?:length|window)\b.*exceed", re.IGNORECASE),
    re.compile(r"\bmaximum\s*context\b", re.IGNORECASE),
    re.compile(r"\b(?:input\s*)?tokens?\s*exceed", re.IGNORECASE),
    re.compile(r"too\s+many\b", re.IGNORECASE),  # "Too many input tokens", "too many tokens", etc.
    re.compile(r"reduce\b.*\blength", re.IGNORECASE),  # "Reduce the input length", "reduce the length"

    re.compile(r"maximum tokens.*exceeds.*model limit", re.IGNORECASE),
    re.compile(r"context length.*exceeds", re.IGNORECASE),
    re.compile(r"total number of tokens.*exceeds.*limit", re.IGNORECASE),
    re.compile(r"requested.*tokens.*exceeds.*limit", re.IGNORECASE),
]

# Known context error codes
_CONTEXT_ERROR_CODES: set[str] = {"context_length_exceeded"}

# Bedrock-specific patterns
_BEDROCK_CONTEXT_PATTERNS: list[re.Pattern] = [
    re.compile(r"maximum tokens.*exceeds.*model limit", re.IGNORECASE),
    re.compile(r"input length and max_tokens exceed context limit", re.IGNORECASE),
    re.compile(r"context length.*exceeds", re.IGNORECASE),
    re.compile(r"total number of tokens.*exceeds.*limit", re.IGNORECASE),
    re.compile(r"requested.*tokens.*exceeds.*limit", re.IGNORECASE),
    re.compile(r"reduce.*length.*messages.*completion", re.IGNORECASE),
    re.compile(r"input is too long", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_context_window_error(error: Any) -> bool:
    """Check if an error indicates context window exceeded.

    Accepts an Exception, dict, or string and tests against all known
    provider-specific context-window-exceeded patterns.

    Returns True if this is a context-window-exceeded error.
    """
    return (
        _is_openai_context_error(error)
        or _is_anthropic_context_error(error)
        or _is_openrouter_context_error(error)
        or _is_bedrock_context_error(error)
        or _is_vercel_context_error(error)
        or _is_cerebras_context_error(error)
        or _is_deepseek_context_error(error)
    )


def is_context_window_error_from_body(status_code: int, body: str) -> bool:
    """Check if a response body indicates context window exceeded.

    Fast path for checking raw API responses without wrapping in an exception.
    """
    if status_code != 400:
        return False

    body_lower = body.lower()

    # Quick string checks before regex
    quick_hits = [
        "context_length_exceeded",
        "reduce the length",
        "prompt is too long",  # Anthropic
        "input is too long",
        "too many tokens",
        "exceeds context",
        "maximum context",
        "token count exceeds",
        "context window",
        "validationexception",
    ]
    if not any(hit in body_lower for hit in quick_hits):
        return False

    return any(pattern.search(body_lower) for pattern in _CONTEXT_ERROR_PATTERNS)


# ---------------------------------------------------------------------------
# Provider-specific detectors
# ---------------------------------------------------------------------------

def _is_openai_context_error(error: Any) -> bool:
    """OpenAI / OpenAI-compatible: 'context_length_exceeded' error code."""
    try:
        # Direct error attribute
        if hasattr(error, "code") and str(getattr(error, "code", "")) == "context_length_exceeded":
            return True

        # Nested error
        inner = _get_attr(error, "error")
        if isinstance(inner, dict):
            if inner.get("code") == "context_length_exceeded":
                return True

        # OpenAI SDK LengthFinishReasonError
        if type(error).__name__ == "LengthFinishReasonError":
            return True

        message: str = String(_get_attr(error, "message") or "")
        if "context_length_exceeded" in message:
            return True

        return False
    except Exception:
        return False


def _is_anthropic_context_error(error: Any) -> bool:
    """Anthropic: 400 with 'prompt is too long' or token-exceed patterns."""
    try:
        status = _get_status(error)
        if String(status) != "400":
            return False

        message: str = String(
            _get_attr(error, "message")
            or _get_attr(_get_attr(error, "error"), "message")
            or ""
        )

        anthropic_patterns = [
            "prompt is too long",
            "input tokens exceed",
            "input is too long",
            "maximum context",
        ]
        if any(p in message.lower() for p in anthropic_patterns):
            return True

        return False
    except Exception:
        return False


def _is_openrouter_context_error(error: Any) -> bool:
    """OpenRouter: 400 with context-window messages, or JSON-encoded errors mid-stream."""
    try:
        status = _get_status(error)
        message: str = String(
            _get_attr(error, "message")
            or _get_attr(_get_attr(error, "error"), "message")
            or ""
        )

        # Direct context error in message
        if String(status) == "400" and (
            "context" in message.lower() and "exceed" in message.lower()
        ):
            return True

        # JSON-encoded error mid-stream (OpenRouter wraps errors)
        if "context_length_exceeded" in message:
            return True

        # Try parsing JSON from message
        try:
            import json
            parsed = json.loads(message) if message.startswith("{") else None
            if parsed and isinstance(parsed, dict):
                inner_code = parsed.get("code") or parsed.get("error", {}).get("code")
                if inner_code == "context_length_exceeded":
                    return True
        except (json.JSONDecodeError, TypeError):
            pass

        return False
    except Exception:
        return False


def _is_bedrock_context_error(error: Any) -> bool:
    """AWS Bedrock: ValidationException with token-exceed patterns."""
    try:
        error_type = String(
            _get_attr(error, "name")
            or _get_attr(_get_attr(error, "error"), "type")
            or _get_attr(error, "__type")
        )
        error_code = String(
            _get_attr(error, "code")
            or _get_attr(_get_attr(error, "error"), "code")
            or _get_attr(error, "$metadata", "httpStatusCode")
        )

        # Check for ValidationException or related types
        is_validation = error_type in (
            "ValidationException", "AI_APICallError",
        ) or error_code == "400"

        if not is_validation:
            # Check nested error structures
            nested_code = String(_get_attr(_get_attr(error, "error"), "param", "statusCode"))
            if nested_code == "400":
                is_validation = True

        if not is_validation:
            return False

        message: str = String(
            _get_attr(error, "message")
            or _get_attr(_get_attr(error, "error"), "message")
            or ""
        )

        return any(pattern.search(message.lower()) for pattern in _BEDROCK_CONTEXT_PATTERNS)
    except Exception:
        return False


def _is_vercel_context_error(error: Any) -> bool:
    """Vercel AI SDK: context_length_exceeded or 400 context errors."""
    try:
        status = String(
            _get_attr(error, "status")
            or _get_attr(_get_attr(error, "error"), "param", "statusCode")
            or _get_attr(error, "statusCode")
        )

        # Direct code check
        error_code = _get_attr(_get_attr(error, "error"), "error", "code")
        if String(error_code) == "context_length_exceeded":
            return True

        messages: list[str] = [
            String(_get_attr(error, "message")),
            String(_get_attr(_get_attr(error, "error"), "message")),
            String(_get_attr(_get_attr(error, "error"), "param", "message")),
            String(_get_attr(_get_attr(error, "error"), "param", "error")),
            String(_get_attr(_get_attr(error, "error"), "error", "message")),
            String(_get_attr(_get_attr(error, "error"), "value", "error_message")),
        ]
        messages = [m for m in messages if m]

        if not messages:
            return False

        # Must be 400 or have 400 in error_message (Alibaba Qwen case)
        has_valid_status = status == "400"
        error_message = String(_get_attr(_get_attr(error, "error"), "value", "error_message"))
        has_400_in_msg = "400" in error_message

        if not has_valid_status and not has_400_in_msg:
            return False

        return any(
            pattern.search(msg.lower())
            for msg in messages
            for pattern in _CONTEXT_ERROR_PATTERNS
        )
    except Exception:
        return False


def _is_cerebras_context_error(error: Any) -> bool:
    """Cerebras: 400 with 'input is too long'."""
    try:
        status = _get_status(error)
        if String(status) != "400":
            return False

        message: str = String(
            _get_attr(error, "message")
            or _get_attr(_get_attr(error, "error"), "message")
            or ""
        )

        return "input is too long" in message.lower() or "exceeds" in message.lower()
    except Exception:
        return False


def _is_deepseek_context_error(error: Any) -> bool:
    """DeepSeek: OpenAI-compatible, uses same error codes as OpenAI."""
    return _is_openai_context_error(error)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_attr(obj: Any, *attrs: str, default: Any = None) -> Any:
    """Safely get a nested attribute from an object or dict."""
    for attr in attrs:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(attr)
        elif hasattr(obj, attr):
            obj = getattr(obj, attr)
        else:
            return default
    return obj


def _get_status(error: Any) -> str:
    """Extract HTTP status from an error, trying multiple paths."""
    return String(
        _get_attr(error, "status")
        or _get_attr(error, "status_code")
        or _get_attr(error, "code")
        or _get_attr(_get_attr(error, "error"), "status")
        or _get_attr(_get_attr(error, "response"), "status")
        or _get_attr(error, "response", "status_code")
    )


def String(val: Any) -> str:
    """Coerce a value to str, returning '' for None/non-string."""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return str(int(val))
    return str(val)
