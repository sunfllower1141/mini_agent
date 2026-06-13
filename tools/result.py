#!/usr/bin/env python3
"""result.py -- structured tool result for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Structured result from a tool execution -- never a raw exception.

    *hint* is an optional short diagnostic shown to the LLM to help it
    self-correct on malformed calls (invalid JSON, unknown parameters,
    wrong types, etc.).  It is included only on failure.
    """

    success: bool
    content: str
    hint: str = ""
    diff_preview: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"success": self.success, "content": self.content}
        if self.hint:
            d["hint"] = self.hint
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
