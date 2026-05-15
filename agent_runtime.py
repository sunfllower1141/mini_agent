#!/usr/bin/env python3
"""
agent_runtime.py — thread-safe sub-agent registry and result type.

Separated from sub_agent.py and tools/__init__.py to avoid circular imports.
Both modules import from here; this module imports from nothing in the project.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Structured result
# ---------------------------------------------------------------------------

@dataclass
class SubAgentResult:
    """Result returned when a sub-agent completes (or fails).

    Mirrors ToolResult so the parent can consume it like any other tool output.
    """
    success: bool
    content: str              # final answer or summary
    turns_used: int = 0
    tool_calls_made: int = 0
    scratchpad: str = ""      # final scratchpad state for parent context
    error: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return {
            "success": self.success,
            "content": self.content,
            "turns_used": self.turns_used,
            "tool_calls_made": self.tool_calls_made,
            "scratchpad": self.scratchpad,
            "error": self.error,
        }

    def to_json(self) -> str:
        import json
        return json.dumps({
            "success": self.success,
            "content": self.content,
            "turns_used": self.turns_used,
            "tool_calls_made": self.tool_calls_made,
            "scratchpad": self.scratchpad,
            "error": self.error,
        })


# ---------------------------------------------------------------------------
# Thread-safe runtime registry (extensible)
# ---------------------------------------------------------------------------

class AgentRuntime:
    """Thread-safe registry for running sub-agent tasks.

    Designed so fields can be added later without breaking callers:
        - inboxes: dict[str, list]     (inter-agent messages)
        - deps: dict[str, list[str]]   (dependency tracking)
        - keep_alive: set[str]         (persistent agents)
    """

    _ABSOLUTE_MAX_TURNS: int = 35  # hard cap for extend_turns()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition()  # notified when a sub-agent completes
        self.tasks: dict[str, threading.Thread] = {}
        self.results: dict[str, SubAgentResult] = {}
        self._collected: set[str] = set()  # IDs already returned by collect_any/collect_agent
        self.cancel_events: dict[str, threading.Event] = {}
        self.max_turns: dict[str, int] = {}  # mutable per-task turn budgets
        self.task_labels: dict[str, str] = {}  # human-readable label per task
        self.task_parents: dict[str, str] = {}  # parent_task_id per task ("" = root)
        self.abandoned: set[str] = set()     # zombie tasks whose store_result() is a no-op
        self._seen_completions: set[str] = set()  # task_ids already surfaced to parent
        # Inter-agent communication
        self.inboxes: dict[str, list] = {}          # task_id → list of AgentMessage
        self.subscriptions: dict[str, set[str]] = {} # task_id → set of message types
        # Auto-snapshot cache: each sub-agent's latest turn status, updated automatically
        # by the sub-agent loop every turn. The parent can read these via agent_status
        # to see what a running sub-agent is doing without waiting for a heartbeat.
        self.status_snapshots: dict[str, dict] = {}

    # ---- spawn ----

    def register(self, task_id: str, thread: threading.Thread,
                 cancel_event: threading.Event, max_turns: int = 20,
                 label: str = "", parent_task_id: str = "") -> None:
        with self._lock:
            self.tasks[task_id] = thread
            self.cancel_events[task_id] = cancel_event
            self.max_turns[task_id] = max_turns
            self.task_labels[task_id] = label
            self.task_parents[task_id] = parent_task_id

    def store_result(self, task_id: str, result: SubAgentResult) -> None:
        with self._lock:
            if task_id in self.abandoned:
                # Zombie thread finally finished after being abandoned —
                # discard its result to avoid corrupting state.
                import sys
                print(
                    f"[runtime] WARNING: discarding result from abandoned zombie "
                    f"task '{task_id}' (thread completed after timeout)",
                    file=sys.stderr, flush=True,
                )
                self.abandoned.discard(task_id)
                return
            if task_id in self.results:
                return  # idempotent: result already stored
            self.results[task_id] = result
            self.tasks.pop(task_id, None)
            self.cancel_events.pop(task_id, None)
            self.max_turns.pop(task_id, None)
            # Release file reservations held by this sub-agent
            from tools import release_all_files  # late import avoids circular dep (tools -> agent_ops -> agent_runtime)
            release_all_files(task_id)
            self.task_labels.pop(task_id, None)
            self.task_parents.pop(task_id, None)
            # Clean up inbox/subscriptions to prevent memory leak
            self.inboxes.pop(task_id, None)
            self.subscriptions.pop(task_id, None)
            self.status_snapshots.pop(task_id, None)
        # Notify condition OUTSIDE _lock to avoid deadlock:
        # collect_agent's wait_for predicate acquires _condition then _lock,
        # so we must never hold _lock while acquiring _condition.
        with self._condition:
            self._condition.notify_all()

    # ---- query ----

    def get_status(self, task_id: str) -> str:
        """Return 'running', 'completed', or 'not_found'."""
        with self._lock:
            if task_id in self.results:
                return "completed"
            if task_id in self.tasks:
                return "running"
            return "not_found"

    def get_result(self, task_id: str) -> SubAgentResult | None:
        with self._lock:
            return self.results.get(task_id)

    def extend_turns(self, task_id: str, additional: int) -> bool:
        """Bump the max_turns budget for a running sub-agent. Returns True if found."""
        with self._lock:
            if task_id in self.max_turns:
                self.max_turns[task_id] = min(
                    self.max_turns[task_id] + additional, self._ABSOLUTE_MAX_TURNS
                )
                return True
            return False

    def get_max_turns(self, task_id: str) -> int | None:
        """Read current max_turns for a running sub-agent."""
        with self._lock:
            return self.max_turns.get(task_id)

    def get_pending_results(self) -> list[tuple[str, "SubAgentResult"]]:
        """Return results for sub-agents that completed since last call.

        Each call returns newly-completed results and marks them as seen.
        Subsequent calls return only completions that happened after this call.
        """
        with self._lock:
            pending: list[tuple[str, "SubAgentResult"]] = []
            for tid, result in self.results.items():
                if tid not in self._seen_completions:
                    self._seen_completions.add(tid)
                    pending.append((tid, result))
            return pending

    def get_running_ids(self) -> list[str]:
        """Return task_ids of all currently running sub-agents."""
        with self._lock:
            return list(self.tasks.keys())

    def mark_abandoned(self, task_id: str) -> None:
        """Mark a task as abandoned so its store_result() is a no-op.

        Used after collect_agent times out and the thread can't be joined —
        the zombie thread will eventually call store_result(), which must be
        ignored to avoid corrupting runtime state.
        """
        with self._lock:
            self.abandoned.add(task_id)
            # Also clean up tracking entries so status reports "not_found".
            self.tasks.pop(task_id, None)
            self.cancel_events.pop(task_id, None)
            self.max_turns.pop(task_id, None)
            self.task_labels.pop(task_id, None)
            self.task_parents.pop(task_id, None)
            self._seen_completions.discard(task_id)
            self.inboxes.pop(task_id, None)
            self.subscriptions.pop(task_id, None)
            self.status_snapshots.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of a running sub-agent. Returns True if found."""
        with self._lock:
            event = self.cancel_events.get(task_id)
            if event is not None:
                event.set()
                return True
            return False

    def cancel_all(self) -> int:
        """Cancel all running sub-agents. Returns count of cancelled agents."""
        with self._lock:
            count = 0
            for event in self.cancel_events.values():
                if not event.is_set():
                    event.set()
                    count += 1
            return count

    # ---- inter-agent messaging ----

    def set_subscriptions(self, task_id: str, types: list[str]) -> None:
        """Declare which message types a task_id wants to receive.

        An empty list means the agent receives ALL message types
        (backward-compatible default behavior).
        """
        with self._lock:
            self.subscriptions[task_id] = set(types)
            if task_id not in self.inboxes:
                self.inboxes[task_id] = []

    def get_inbox(self, task_id: str) -> list:
        """Return the list of AgentMessages for a task_id (or empty list)."""
        with self._lock:
            return list(self.inboxes.get(task_id, []))

    def append_inbox(self, task_id: str, msg) -> None:
        """Append a message to a task_id's inbox. Creates inbox if missing."""
        with self._lock:
            inbox = self.inboxes.setdefault(task_id, [])
            inbox.append(msg)
            # Ring-buffer cap at 1000 to prevent unbounded growth on long-running agents
            if len(inbox) > 1000:
                inbox[:] = inbox[-1000:]

    def clear_inbox(self, task_id: str) -> None:
        """Remove inbox and subscriptions for a task_id (cleanup on completion)."""
        with self._lock:
            self.inboxes.pop(task_id, None)
            self.subscriptions.pop(task_id, None)

    # ---- status snapshots (auto-recorded every turn, no sub-agent action needed) ----

    _SNAPSHOT_FIELDS = (
        "timestamp", "turn", "turns_budget", "last_action",
        "last_tool", "last_tool_summary", "scratchpad_snippet",
        "tool_calls_made", "last_error", "thought_snippet",
        "streamed_tokens",
    )

    def update_snapshot(
        self, task_id: str, turn: int, turns_budget: int,
        last_action: str, last_tool: str, last_tool_summary: str,
        scratchpad_snippet: str, tool_calls_made: int,
        last_error: str | None = None,
        thought_snippet: str = "",
        streamed_tokens: int = 0,
    ) -> None:
        """Record a status snapshot for a running sub-agent.

        Called automatically by the sub-agent loop every turn, and
        periodically during streaming so the orchestrator can see live
        progress (thought content + token count + timestamp for deltas).

        Thread-safe — acquires _lock briefly to write the dict.
        """
        import time as _time
        snap = {
            "timestamp": _time.monotonic(),
            "turn": turn,
            "turns_budget": turns_budget,
            "last_action": last_action,          # "tool_call" | "final_answer" | "error" | "thinking" | "calling_llm"
            "last_tool": last_tool,               # tool name or None
            "last_tool_summary": last_tool_summary,  # first ~120 chars of result
            "scratchpad_snippet": scratchpad_snippet,  # last ~200 chars
            "tool_calls_made": tool_calls_made,
            "last_error": last_error,
            "thought_snippet": thought_snippet,    # last ~200 chars of streamed thought (empty if not streaming)
            "streamed_tokens": streamed_tokens,    # tokens streamed so far in current LLM call
        }
        with self._lock:
            self.status_snapshots[task_id] = snap

    def get_snapshot(self, task_id: str) -> dict | None:
        """Return the latest status snapshot for a running sub-agent, or None."""
        with self._lock:
            return self.status_snapshots.get(task_id)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for tid in self.tasks
                if tid not in self.results
            )
