#!/usr/bin/env python3
"""result.py -- structured tool result for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).

Structured error semantics (2026 best practice):
  - error_class categorises failures so the LLM can decide retryability
    without guessing: validation, authorization, not_found, transient,
    rate_limit, permanent, partial_success.
  - retryable signals whether the same call can be retried.
  - retry_after_ms gives a suggested backoff.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field


class ErrorClass(str, enum.Enum):
    """Machine-readable error category for tool failures.

    Mirrors the 2026 structured-error consensus:
      - validation: bad parameters / malformed input (fix args, retry)
      - authorization: permissions / safety gate blocked (may need approval)
      - not_found: file / symbol / resource doesn't exist (check path/name)
      - transient: timeout, network blip, temporary unavailability (retry with backoff)
      - rate_limit: API rate limit hit (retry after delay)
      - permanent: unrecoverable (don't retry, try different approach)
      - partial_success: some results available but incomplete
    """

    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    PERMANENT = "permanent"
    PARTIAL_SUCCESS = "partial_success"


@dataclass
class ToolResult:
    """Structured result from a tool execution -- never a raw exception.

    Attributes
    ----------
    success: Whether the tool executed without error.
    content: Human-readable output (truncated for display).
    hint: Optional short diagnostic for the LLM to self-correct.
    diff_preview: Optional unified-diff preview for edit operations.
    error_class: Machine-readable error category (None on success).
    retryable: Whether the same call can be retried safely.
    retry_after_ms: Suggested backoff in milliseconds (0 if unknown).
    idempotency_key: Optional key for idempotent write replay.
    """

    success: bool
    content: str
    hint: str = ""
    diff_preview: str | None = None
    error_class: ErrorClass | None = None
    retryable: bool = False
    retry_after_ms: int = 0
    idempotency_key: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"success": self.success, "content": self.content}
        if self.hint:
            d["hint"] = self.hint
        if self.diff_preview:
            d["diff_preview"] = self.diff_preview
        if not self.success and self.error_class is not None:
            d["error_class"] = self.error_class.value
            d["retryable"] = self.retryable
            if self.retry_after_ms:
                d["retry_after_ms"] = self.retry_after_ms
        if self.idempotency_key:
            d["idempotency_key"] = self.idempotency_key
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    # ------------------------------------------------------------------
    # Factory helpers for common error patterns
    # ------------------------------------------------------------------

    @classmethod
    def validation_error(
        cls, content: str, *, hint: str = "", valid_params: str = ""
    ) -> "ToolResult":
        """Build a validation-error result (bad parameters / malformed input)."""
        full_hint = hint or f"Invalid parameters. Valid: {valid_params}"
        return cls(
            success=False,
            content=content,
            hint=full_hint,
            error_class=ErrorClass.VALIDATION,
            retryable=True,  # fix args and retry
            retry_after_ms=0,
        )

    @classmethod
    def not_found_error(
        cls, content: str, *, hint: str = ""
    ) -> "ToolResult":
        """Build a not-found result (file, symbol, resource missing)."""
        return cls(
            success=False,
            content=content,
            hint=hint or content,
            error_class=ErrorClass.NOT_FOUND,
            retryable=False,
        )

    @classmethod
    def transient_error(
        cls, content: str, *, hint: str = "", retry_after_ms: int = 2000
    ) -> "ToolResult":
        """Build a transient-error result (timeout, network blip)."""
        return cls(
            success=False,
            content=content,
            hint=hint or content,
            error_class=ErrorClass.TRANSIENT,
            retryable=True,
            retry_after_ms=retry_after_ms,
        )

    @classmethod
    def rate_limit_error(
        cls, content: str, *, hint: str = "", retry_after_ms: int = 5000
    ) -> "ToolResult":
        """Build a rate-limit result."""
        return cls(
            success=False,
            content=content,
            hint=hint or content,
            error_class=ErrorClass.RATE_LIMIT,
            retryable=True,
            retry_after_ms=retry_after_ms,
        )

    @classmethod
    def permanent_error(
        cls, content: str, *, hint: str = ""
    ) -> "ToolResult":
        """Build a permanent-error result (unrecoverable)."""
        return cls(
            success=False,
            content=content,
            hint=hint or content,
            error_class=ErrorClass.PERMANENT,
            retryable=False,
        )

    @classmethod
    def authorization_error(
        cls, content: str, *, hint: str = ""
    ) -> "ToolResult":
        """Build an authorization-error result (permissions / safety gate)."""
        return cls(
            success=False,
            content=content,
            hint=hint or content,
            error_class=ErrorClass.AUTHORIZATION,
            retryable=False,
        )
