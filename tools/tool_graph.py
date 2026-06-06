#!/usr/bin/env python3
"""
tool_graph.py — tool dependency and sequencing graph for mini_agent.

Tracks tool co-occurrence and sequential dependencies from observed
tool-call history.  Builds a weighted directed graph where edges
represent "tool A → tool B" transitions observed in successful turns.

Provides:
  A. Tool transition recording — track which tools follow which others
  B. Sequencing hints — suggest the most likely next tool given context
  C. Co-occurrence clusters — identify tool pairs that frequently appear together
  D. Anti-pattern detection — flag suboptimal sequences (e.g. edit_file
     without preceding read_file)

Inspired by: SEARL (tool graph), ToolExpNet (semantic tool relationships),
             TOOLMEM (tool capability memories).

Table: tool_transitions (shared SQLite connection via MemoryStore)
  - from_tool: str     — source tool name
  - to_tool:   str     — destination tool name
  - count:     int     — number of times this transition was observed
  - success_count: int — transitions that led to eventual task success
  - last_seen: str     — ISO timestamp
"""

from __future__ import annotations

import sqlite3
import threading
import warnings

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

# Co-occurrence window: how many tool calls back to look for transitions
_COOCCURRENCE_WINDOW = 5

# Minimum transition count before suggesting a tool sequence
_MIN_TRANSITION_COUNT = 2

# Minimum success rate to consider a transition "proven"
_MIN_SUCCESS_RATE = 0.4

# Maximum hints to inject per turn
_MAX_TRANSITION_HINTS = 2

# Anti-patterns: undesirable sequences that should trigger a warning
_ANTI_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "edit_file": [
        ("search_files", "Prefer read_file over search_files before editing — you need exact text, not patterns."),
        ("find_symbol", "Use read_file before edit_file to see the exact text you're replacing."),
    ],
    "write_file": [
        ("read_file", None),  # read_file before write_file is actually good — skip
    ],
}

# Tools that "read" state (preliminary step before writes)
_READ_TOOLS = frozenset({"read_file", "file_info", "list_directory", "find_symbol",
                          "find_usages", "search_files", "lsp_definition",
                          "lsp_hover", "lsp_references", "lsp_diagnostics"})

# Tools that "write" state
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "run_shell"})

# Tools that "verify" state
_VERIFY_TOOLS = frozenset({"verify", "run_tests", "lsp_diagnostics"})


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

TOOL_TRANSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS tool_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_tool       TEXT    NOT NULL,
    to_tool         TEXT    NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1,
    success_count   INTEGER NOT NULL DEFAULT 0,
    last_seen       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

TOOL_TRANSITIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tt_from ON tool_transitions(from_tool)",
    "CREATE INDEX IF NOT EXISTS idx_tt_to ON tool_transitions(to_tool)",
    "CREATE INDEX IF NOT EXISTS idx_tt_count ON tool_transitions(count DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tt_pair ON tool_transitions(from_tool, to_tool)",
]


# ---------------------------------------------------------------------------
# ToolGraph
# ---------------------------------------------------------------------------

class ToolGraph:
    """Weighted directed graph of tool transitions learned from observation.

    Thread-safe via a module-level lock.  Uses the same SQLite connection
    as MemoryStore for cross-session persistence.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a cached, resilient connection."""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
            except sqlite3.Error:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init_schema(self) -> None:
        """Create tool_transitions table and indexes."""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(TOOL_TRANSITIONS_DDL)
                for idx_sql in TOOL_TRANSITIONS_INDEXES:
                    try:
                        conn.execute(idx_sql)
                    except sqlite3.Error:
                        pass
                conn.commit()
            except sqlite3.Error:
                warnings.warn("Failed to init tool_transitions table", stacklevel=2)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_transition(
        self,
        from_tool: str,
        to_tool: str,
        *,
        successful_turn: bool = False,
    ) -> None:
        """Record a tool transition observation.

        Called after each tool execution (except the first in a turn).
        *successful_turn* should be True when the overall user task succeeded.
        """
        if not from_tool or not to_tool:
            return
        if from_tool == to_tool:
            return  # Skip self-loops

        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(
                    "INSERT INTO tool_transitions (from_tool, to_tool, count, success_count)"
                    " VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(from_tool, to_tool) DO UPDATE SET"
                    " count = count + 1,"
                    " success_count = success_count + ?,"
                    " last_seen = datetime('now')",
                    (from_tool, to_tool,
                     1 if successful_turn else 0,
                     1 if successful_turn else 0),
                )
                conn.commit()
            except sqlite3.Error:
                pass  # Non-critical

    def record_turn_tool_sequence(
        self,
        tool_names: list[str],
        *,
        successful_turn: bool = False,
    ) -> None:
        """Record all transitions within a single turn's tool sequence.

        Records each adjacent pair (A→B) within the co-occurrence window.
        """
        if len(tool_names) < 2:
            return
        # Record adjacent transitions
        for i in range(len(tool_names) - 1):
            self.record_transition(
                tool_names[i], tool_names[i + 1],
                successful_turn=successful_turn,
            )
        # Also record within a sliding window for broader co-occurrence
        if len(tool_names) > 2:
            for i in range(len(tool_names)):
                for j in range(i + 1, min(i + _COOCCURRENCE_WINDOW + 1, len(tool_names))):
                    if j > i + 1:  # Non-adjacent within window
                        self.record_transition(
                            tool_names[i], tool_names[j],
                            successful_turn=successful_turn,
                        )

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_next_tool_hints(
        self,
        current_tool: str,
        *,
        limit: int = _MAX_TRANSITION_HINTS,
    ) -> list[str]:
        """Return suggested next tools after *current_tool*, ranked by count.

        Only returns transitions with count >= _MIN_TRANSITION_COUNT.
        """
        with self._lock:
            try:
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT to_tool, count, success_count"
                    " FROM tool_transitions"
                    " WHERE from_tool = ? AND count >= ?"
                    " ORDER BY count DESC LIMIT ?",
                    (current_tool, _MIN_TRANSITION_COUNT, limit),
                ).fetchall()
                hints: list[str] = []
                for to_tool, count, success_count in rows:
                    success_rate = success_count / max(count, 1)
                    marker = " ✅" if success_rate >= _MIN_SUCCESS_RATE else ""
                    hints.append(
                        f"After {current_tool}, common next: {to_tool} "
                        f"(seen {count}×, {int(success_rate * 100)}% success rate){marker}"
                    )
                return hints
            except sqlite3.Error:
                return []

    def get_tool_context_hints(
        self,
        recent_tools: list[str],
        *,
        limit: int = _MAX_TRANSITION_HINTS,
    ) -> str | None:
        """Build a context message with sequencing hints based on recent tools.

        Analyzes the last few tools used and suggests what to do next.
        Returns a string for injection, or None.
        """
        if not recent_tools:
            return None

        last_tool = recent_tools[-1]
        hints = self.get_next_tool_hints(last_tool, limit=limit)

        if not hints:
            return None

        parts = ["📊 TOOL SEQUENCING HINTS (learned from past sessions):"]
        parts.extend(f"  {h}" for h in hints)

        # Anti-pattern detection: check if agent is writing without reading
        if last_tool in _WRITE_TOOLS:
            recent_reads = sum(1 for t in recent_tools[-5:] if t in _READ_TOOLS)
            if recent_reads == 0:
                parts.append(
                    "  ⚠️ No recent read tool used before writing. "
                    "Consider read_file first to understand current state."
                )

        # Anti-pattern: consecutive edits without verification
        if last_tool == "edit_file":
            edit_count = sum(1 for t in recent_tools[-5:] if t == "edit_file")
            verify_count = sum(1 for t in recent_tools[-5:] if t in _VERIFY_TOOLS)
            if edit_count >= 3 and verify_count == 0:
                parts.append(
                    "  ⚠️ Multiple edits without verification. "
                    "Run verify or check LSP diagnostics to confirm correctness."
                )

        return "\n".join(parts)

    def detect_read_before_write_gap(
        self,
        pending_tool_calls: list[dict],
        recent_tools: list[str],
    ) -> str | None:
        """Detect if the agent is about to write without reading first.

        Checks pending tool calls for write operations and warns if
        no read operations have been performed recently.
        """
        pending_names = [
            tc.get("function", {}).get("name", "")
            for tc in pending_tool_calls
        ]
        has_write = any(n in _WRITE_TOOLS for n in pending_names)
        if not has_write:
            return None

        # Check recent tools for reads
        recent_reads = sum(1 for t in recent_tools[-5:] if t in _READ_TOOLS)
        if recent_reads == 0:
            # Check if this is a new session (no recent tools at all)
            if len(recent_tools) == 0:
                return None  # First turn — normal to start with reads
            write_names = [n for n in pending_names if n in _WRITE_TOOLS]
            return (
                "⚠️ TOOL SEQUENCING WARNING: About to use "
                f"{', '.join(write_names)} without any recent read operations. "
                "Strongly consider reading the relevant file(s) first to "
                "understand the current state before modifying them."
            )
        return None

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return summary statistics."""
        with self._lock:
            try:
                conn = self._get_conn()
                total = conn.execute(
                    "SELECT COUNT(*) FROM tool_transitions"
                ).fetchone()[0]
                top_pairs = conn.execute(
                    "SELECT from_tool, to_tool, count FROM tool_transitions"
                    " ORDER BY count DESC LIMIT 10"
                ).fetchall()
                top_from = conn.execute(
                    "SELECT from_tool, SUM(count) as total FROM tool_transitions"
                    " GROUP BY from_tool ORDER BY total DESC LIMIT 5"
                ).fetchall()
                return {
                    "total_transitions": total,
                    "top_pairs": [
                        {"from": r[0], "to": r[1], "count": r[2]}
                        for r in top_pairs
                    ],
                    "top_source_tools": [
                        {"tool": r[0], "total": r[1]} for r in top_from
                    ],
                }
            except sqlite3.Error:
                return {"total_transitions": 0, "top_pairs": [], "top_source_tools": []}
