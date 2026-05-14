#!/usr/bin/env python3
"""Metrics collection for the Agent Evaluation Harness.

Instruments the agent loop via existing callbacks (on_tool_start,
on_tool_end) — no changes to core modules required.
"""

from collections import Counter


class MetricsCollector:
    """Collects per-run metrics via agent callbacks.

    The callbacks match the signatures used by run_agent_turn():
      - on_tool_start(summary: str, parallel: bool = False)
      - on_tool_end(success: bool, detail: str)

    Tool names are extracted from the summary string, which has the
    format ``tool_name(args_summary)``.
    """

    def __init__(self) -> None:
        self.tool_counts: Counter[str] = Counter()
        self.turn_count: int = 0
        self.tool_successes: int = 0
        self.tool_failures: int = 0
        self._current_batch_size: int = 0

    def on_tool_start(self, summary: str, parallel: bool = False) -> None:
        """Called before each tool executes.

        Extract tool name from the summary string and increment count.
        """
        name = summary.split("(", 1)[0].strip() if "(" in summary else summary
        self.tool_counts[name] += 1
        self._current_batch_size += 1

    def on_tool_end(self, success: bool, detail: str) -> None:
        """Called after each tool completes."""
        if success:
            self.tool_successes += 1
        else:
            self.tool_failures += 1

    def mark_turn(self) -> None:
        """Mark the start of a new agent turn (LLM → tools → LLM cycle)."""
        self.turn_count += 1
        self._current_batch_size = 0

    def to_dict(self) -> dict:
        """Return metrics as a serializable dict."""
        return {
            "turns": self.turn_count,
            "tool_calls": dict(self.tool_counts),
            "total_tool_calls": sum(self.tool_counts.values()),
            "tool_successes": self.tool_successes,
            "tool_failures": self.tool_failures,
        }
