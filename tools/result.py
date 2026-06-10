#!/usr/bin/env python3
"""result.py — structured tool result for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


# Known typed-error reason hints (used by the agent to dispatch recovery logic)
TYPED_REASON_READ_BEFORE_EDIT = "read_before_edit"
TYPED_REASON_NOT_FOUND = "not_found"
TYPED_REASON_WHITESPACE = "whitespace"
TYPED_REASON_AMBIGUOUS = "ambiguous"
TYPED_REASON_BLOCKED = "blocked"
TYPED_REASON_STALE = "stale"
TYPED_REASON_CIRCUIT = "circuit_breaker"


@dataclass
class ToolResult:
    """Structured result from a tool execution — never a raw exception.

    *hint* is an optional short diagnostic shown to the LLM to help it
    self-correct on malformed calls (invalid JSON, unknown parameters,
    wrong types, etc.).  It is included only on failure.

    *typed_error* is a machine-actionable error envelope (set on failure):
      { reason: str,   # one of TYPED_REASON_* above
        retry_budget: int,  # suggested max retries remaining
        suggested_action: str  # what the agent should do next
      }
    """

    success: bool
    content: str
    hint: str = ""
    diff_preview: str | None = None
    typed_error: dict | None = None

    def with_typed_error(
        self, reason: str, *, retry_budget: int = 1, suggested_action: str = "",
    ) -> "ToolResult":
        """Attach a typed error envelope for machine-actionable recovery."""
        self.typed_error = {
            "reason": reason,
            "retry_budget": retry_budget,
            "suggested_action": suggested_action,
        }
        return self

    def to_dict(self) -> dict:
        d: dict = {"success": self.success, "content": self.content}
        if self.hint:
            d["hint"] = self.hint
        if self.typed_error:
            d["typed_error"] = self.typed_error
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
