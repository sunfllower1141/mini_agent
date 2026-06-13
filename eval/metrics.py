#!/usr/bin/env python3
"""Metrics collection for the Agent Evaluation Harness.

Instruments the agent loop via existing callbacks (on_tool_start,
on_tool_end) -- no changes to core modules required.

New in this revision:
    - Per-tool wall time tracking (start/end timestamps, deltas)
    - Per-tool latency histogram (bucketed in ms)
    - Peak memory tracking via tracemalloc (opt-in)
    - Tool latency summary (min, max, avg, p50, p95, p99)
"""

from __future__ import annotations

import time
import tracemalloc
from collections import Counter, defaultdict
from statistics import median


class MetricsCollector:
    """Collects per-run metrics via agent callbacks.

    The callbacks match the signatures used by run_agent_turn():
      - on_tool_start(summary: str, parallel: bool = False)
      - on_tool_end(success: bool, detail: str)

    Tool names are extracted from the summary string, which has the
    format ``tool_name(args_summary)``.

    Optional memory tracking is enabled via ``enable_memory()``.
    When enabled, ``peak_memory_kb`` is populated at the end of the run.
    """

    # Latency buckets in milliseconds
    LATENCY_BUCKETS = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

    def __init__(self) -> None:
        self.tool_counts: Counter[str] = Counter()
        self.turn_count: int = 0
        self.tool_successes: int = 0
        self.tool_failures: int = 0
        self._current_batch_size: int = 0

        # Per-tool wall time
        self._tool_start_times: dict[str, float] = {}  # tool_name -> perf_counter
        self.tool_wall_ms: dict[str, list[float]] = defaultdict(list)  # tool_name -> [ms, ...]

        # Latency histogram
        self._latency_histogram: Counter[str] = Counter()

        # Memory tracking
        self._track_memory: bool = False
        self._mem_snap_before: tracemalloc.Snapshot | None = None
        self.peak_memory_kb: float = 0.0

        # Cache of start timestamps keyed by call index (for parallel batches)
        self._start_times: dict[int, float] = {}
        self._call_index: int = 0

    # ---- memory tracking ----

    def enable_memory(self) -> None:
        """Start tracemalloc and capture a baseline snapshot."""
        tracemalloc.start()
        self._track_memory = True
        self._mem_snap_before = tracemalloc.take_snapshot()

    def _finalize_memory(self) -> None:
        """Take final snapshot and compute peak memory delta."""
        if not self._track_memory or self._mem_snap_before is None:
            return
        snap_after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        stats = snap_after.compare_to(self._mem_snap_before, "lineno")
        # Sum of all positive allocations as peak memory delta in KB
        total_kb = sum(s.size_diff for s in stats if s.size_diff > 0) / 1024.0
        self.peak_memory_kb = total_kb

    # ---- tool callbacks ----

    def on_tool_start(self, summary: str, parallel: bool = False) -> None:
        """Called before each tool executes.

        Extract tool name from the summary string and increment count.
        Record start time for latency tracking.
        """
        name = summary.split("(", 1)[0].strip() if "(" in summary else summary
        self.tool_counts[name] += 1
        self._current_batch_size += 1

        idx = self._call_index
        self._call_index += 1
        self._start_times[idx] = time.perf_counter()

    def on_tool_end(self, success: bool, detail: str, diff_preview: str | None = None, content: str = "") -> None:
        """Called after each tool completes.

        Records success/failure, computes wall time from start timestamp.
        """
        if success:
            self.tool_successes += 1
        else:
            self.tool_failures += 1

        # Compute latency -- match to most recent un-matched start
        # In parallel batches, we pair in order (FIFO of start times)
        if self._start_times:
            idx = min(self._start_times.keys())
            t0 = self._start_times.pop(idx)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            # Fallback -- use summary to extract tool name
            name = getattr(self, '_last_tool_name', None)
            if name:
                self.tool_wall_ms[name].append(elapsed_ms)
                self._bucket_latency(elapsed_ms)

    def _bucket_latency(self, ms: float) -> None:
        """Place latency in the correct bucket for histogram."""
        for bucket in self.LATENCY_BUCKETS:
            if ms <= bucket:
                self._latency_histogram[f"<={bucket}ms"] += 1
                return
        self._latency_histogram[">10000ms"] += 1

    # ---- turn tracking ----

    def mark_turn(self) -> None:
        """Mark the start of a new agent turn (LLM -> tools -> LLM cycle)."""
        self.turn_count += 1
        self._current_batch_size = 0

    # ---- serialization ----

    def _compute_latency_stats(self, values: list[float]) -> dict:
        """Compute min, max, avg, p50, p95, p99 for a list of millisecond values."""
        if not values:
            return {}
        s = sorted(values)
        n = len(s)
        return {
            "count": n,
            "min_ms": round(s[0], 2),
            "max_ms": round(s[-1], 2),
            "avg_ms": round(sum(s) / n, 2),
            "p50_ms": round(median(s), 2),
            "p95_ms": round(s[int(n * 0.95)] if n > 1 else s[0], 2),
            "p99_ms": round(s[int(n * 0.99)] if n > 1 else s[0], 2),
        }

    def to_dict(self) -> dict:
        """Return metrics as a serializable dict.

        Includes per-tool latency stats and histogram when available.
        """
        self._finalize_memory()

        result: dict = {
            "turns": self.turn_count,
            "tool_calls": dict(self.tool_counts),
            "total_tool_calls": sum(self.tool_counts.values()),
            "tool_successes": self.tool_successes,
            "tool_failures": self.tool_failures,
        }

        # Per-tool latency stats
        if self.tool_wall_ms:
            latency_by_tool: dict = {}
            for tool, values in sorted(self.tool_wall_ms.items()):
                latency_by_tool[tool] = self._compute_latency_stats(values)
            result["tool_latency"] = latency_by_tool

        # Latency histogram
        if self._latency_histogram:
            result["latency_histogram"] = dict(
                sorted(self._latency_histogram.items(),
                       key=lambda x: int(x[0].lstrip("<=").rstrip("ms").replace(">", "99999")))
            )

        # Memory
        if self._track_memory:
            result["peak_memory_kb"] = round(self.peak_memory_kb, 2)

        return result
