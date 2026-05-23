#!/usr/bin/env python3
"""
failure_learning.py — self-learning system for mini_agent.

Implements three self-learning capabilities inspired by recent research:
  A. Structured Failure Pattern Database (MPR/VIGIL-inspired)
     Auto-analyzes tool failures, extracts reusable patterns, persists
     across sessions, and injects relevant patterns before similar calls.

  B. Self-Critique at Tool Boundaries (SAMULE/PreFlect-inspired)
     Lightweight post-execution assessment that detects failure clusters
     and injects corrective context before the next LLM call.

  C. Enhanced Project Knowledge (cross-session pattern memory)
     Categorized learnings with confidence scoring, hit-based decay,
     and smart context injection filtered by relevance.

Table: failure_patterns (added to memory.py schema)
  - tool_name: str       — which tool failed
  - error_fingerprint: str — stable error category (e.g. 'not found', 'whitespace')
  - args_signature: str  — normalized args pattern for matching
  - fix_strategy: str    — what fixed it (or general advice if never fixed)
  - success_count: int   — times this fix worked
  - failure_count: int   — total times this pattern occurred
  - last_seen: str       — ISO timestamp
  - created_at: str      — ISO timestamp
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import warnings
from typing import Optional

# Reuse _fingerprint_error from tools/__init__ when available;
# define a fallback here for bootstrapping.
try:
    from tools import _fingerprint_error as _core_fingerprint
except ImportError:
    _core_fingerprint = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

# Failure pattern management
_MAX_PATTERNS = 200                # cap on stored patterns
_PATTERN_CONFIDENCE_THRESHOLD = 0.3  # min confidence to inject a pattern
_PATTERN_MIN_OCCURRENCES = 2       # min occurrences before considering injection
_MAX_INJECTED_PATTERNS = 3         # max patterns injected per turn

# Self-critique
_MAX_CRITIQUE_FAILURES = 3         # failures in a turn that trigger self-critique
_CRITIQUE_COOLDOWN_TURNS = 2       # turns between self-critique injections

# Args signature normalization
_ARGS_MAX_STRING_LENGTH = 80       # truncate long string args for signatures
_ARGS_SKIP_KEYS = {"timeout", "_pipe", "force", "background"}  # keys to drop from signatures


# ---------------------------------------------------------------------------
# SQL table creation
# ---------------------------------------------------------------------------

FAILURE_PATTERNS_DDL = """
CREATE TABLE IF NOT EXISTS failure_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name       TEXT    NOT NULL,
    error_fingerprint TEXT  NOT NULL,
    args_signature  TEXT    NOT NULL DEFAULT '',
    fix_strategy    TEXT    NOT NULL DEFAULT '',
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 1,
    confidence      REAL    NOT NULL DEFAULT 0.0,
    last_seen       TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

FAILURE_PATTERNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_fp_tool_err ON failure_patterns(tool_name, error_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_fp_confidence ON failure_patterns(confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_fp_last_seen ON failure_patterns(last_seen DESC)",
]


# ---------------------------------------------------------------------------
# Error fingerprinting (standalone, mirrors tools/__init__._fingerprint_error)
# ---------------------------------------------------------------------------

def _fingerprint_error(name: str, content: str) -> str:
    """Extract a stable error fingerprint from tool result content."""
    cl = content.lower()
    if name == "edit_file":
        if "not found" in cl or "does not exist" in cl:
            return "not_found"
        if "whitespace" in cl or "indentation" in cl or "tab" in cl or "trailing" in cl:
            return "whitespace"
        if "ambiguous" in cl or "multiple" in cl or "appears" in cl:
            return "ambiguous"
        if "count" in cl or "invalid count" in cl:
            return "count"
    elif name == "write_file":
        if "blocked" in cl or "safety" in cl:
            return "blocked"
        if "exists" in cl or "overwrite" in cl:
            return "exists"
    elif name == "read_file":
        if "not found" in cl or "no such file" in cl:
            return "not_found"
        if "offset" in cl or "exceeds" in cl:
            return "offset"
    elif name == "search_files":
        if "no matches" in cl or "not found" in cl:
            return "not_found"
        if "invalid" in cl and "regex" in cl:
            return "invalid_regex"
    elif name == "run_shell":
        if "not found" in cl or "command not found" in cl:
            return "not_found"
        if "blocked" in cl or "destructive" in cl:
            return "blocked"
        if "timed out" in cl or "timeout" in cl:
            return "timed_out"
    elif name in ("find_symbol", "find_usages"):
        if "no match" in cl or "not found" in cl:
            return "not_found"
    elif name in ("run_tests", "verify"):
        if "fail" in cl or "FAILED" in cl:
            return "test_failures"
    # Generic: hash the content for a stable fingerprint
    return "generic:" + hashlib.md5(content[:120].encode()).hexdigest()[:12]


def _normalize_args(name: str, args: dict) -> str:
    """Create a stable, compact signature of tool arguments for pattern matching.

    Drops uninteresting keys (timeout, _pipe, etc.) and truncates long values.
    Returns a sorted JSON string that can be used for prefix matching.
    """
    if not args:
        return ""
    filtered = {}
    for k, v in sorted(args.items()):
        if k in _ARGS_SKIP_KEYS:
            continue
        if isinstance(v, str) and len(v) > _ARGS_MAX_STRING_LENGTH:
            # Truncate but keep first ~60 chars for matching
            filtered[k] = v[:_ARGS_MAX_STRING_LENGTH]
        elif isinstance(v, (int, float, bool, type(None))):
            filtered[k] = v
        elif isinstance(v, list):
            filtered[k] = f"[list:{len(v)}]"
        elif isinstance(v, dict):
            filtered[k] = f"[dict:{len(v)}]"
        else:
            filtered[k] = str(v)[:_ARGS_MAX_STRING_LENGTH]
    return json.dumps(filtered, sort_keys=True)


def _args_similarity(sig1: str, sig2: str) -> float:
    """Compute rough similarity between two args signatures.

    Returns 0.0–1.0 where 1.0 = identical, ~0.5 = share tool name context.
    Used to find relevant patterns for a pending tool call.
    """
    if not sig1 or not sig2:
        return 0.0
    if sig1 == sig2:
        return 1.0
    # Extract key-value pairs and compute overlap
    try:
        d1 = json.loads(sig1)
        d2 = json.loads(sig2)
    except (json.JSONDecodeError, TypeError):
        return 0.3 if sig1[:40] == sig2[:40] else 0.0
    if not d1 or not d2:
        return 0.3
    # Key overlap
    keys1 = set(d1.keys())
    keys2 = set(d2.keys())
    if not keys1 or not keys2:
        return 0.2
    key_overlap = len(keys1 & keys2) / len(keys1 | keys2)
    # Value overlap for shared keys
    shared = keys1 & keys2
    if not shared:
        return key_overlap * 0.5
    val_match = sum(1 for k in shared if str(d1[k])[:30] == str(d2[k])[:30])
    val_overlap = val_match / len(shared)
    return 0.4 * key_overlap + 0.6 * val_overlap


# ---------------------------------------------------------------------------
# FailurePatternStore — persists and retrieves failure patterns
# ---------------------------------------------------------------------------

class FailurePatternStore:
    """SQLite-backed store for tool failure patterns with confidence scoring.

    Thread-safe via a module-level lock.  Designed to be attached to
    the existing MemoryStore's SQLite connection.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a cached connection."""
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
        """Create failure_patterns table and indexes if they don't exist."""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(FAILURE_PATTERNS_DDL)
                for idx_sql in FAILURE_PATTERNS_INDEXES:
                    try:
                        conn.execute(idx_sql)
                    except sqlite3.Error:
                        pass
                conn.commit()
            except sqlite3.Error:
                warnings.warn("Failed to init failure_patterns table", stacklevel=2)

    def record_failure(
        self,
        tool_name: str,
        error_content: str,
        args: dict | None = None,
        *,
        fix_strategy: str = "",
    ) -> int | None:
        """Record a tool failure.  Returns the pattern ID if stored.

        If a matching pattern already exists, bumps failure_count.
        Otherwise inserts a new row.
        """
        fingerprint = _fingerprint_error(tool_name, error_content)
        args_sig = _normalize_args(tool_name, args or {})

        with self._lock:
            try:
                conn = self._get_conn()
                # Look for existing matching pattern
                row = conn.execute(
                    "SELECT id, failure_count, success_count FROM failure_patterns"
                    " WHERE tool_name = ? AND error_fingerprint = ?"
                    " AND args_signature = ?"
                    " LIMIT 1",
                    (tool_name, fingerprint, args_sig),
                ).fetchone()

                if row:
                    pid, fc, sc = row
                    new_fc = fc + 1
                    confidence = self._compute_confidence(sc, new_fc)
                    conn.execute(
                        "UPDATE failure_patterns SET failure_count = ?,"
                        " confidence = ?, last_seen = datetime('now')"
                        " WHERE id = ?",
                        (new_fc, confidence, pid),
                    )
                    if fix_strategy:
                        conn.execute(
                            "UPDATE failure_patterns SET fix_strategy = ? WHERE id = ?",
                            (fix_strategy, pid),
                        )
                    conn.commit()
                    self._prune_if_needed(conn)
                    return pid
                else:
                    # New pattern
                    confidence = self._compute_confidence(0, 1)
                    cursor = conn.execute(
                        "INSERT INTO failure_patterns"
                        " (tool_name, error_fingerprint, args_signature,"
                        "  fix_strategy, failure_count, confidence)"
                        " VALUES (?, ?, ?, ?, 1, ?)",
                        (tool_name, fingerprint, args_sig, fix_strategy, confidence),
                    )
                    conn.commit()
                    self._prune_if_needed(conn)
                    return cursor.lastrowid
            except sqlite3.Error:
                warnings.warn("Failed to record failure pattern", stacklevel=2)
                return None

    def record_success(self, tool_name: str, args: dict | None = None) -> None:
        """Record that a previously-failing pattern succeeded.

        Finds the closest matching failure pattern for this tool/args
        and bumps its success_count, which increases confidence.
        """
        args_sig = _normalize_args(tool_name, args or {})

        with self._lock:
            try:
                conn = self._get_conn()
                # Find patterns for this tool, ordered by recency
                rows = conn.execute(
                    "SELECT id, args_signature, failure_count, success_count"
                    " FROM failure_patterns"
                    " WHERE tool_name = ?"
                    " ORDER BY last_seen DESC LIMIT 10",
                    (tool_name,),
                ).fetchall()

                best_id = None
                best_sim = 0.0
                for pid, stored_sig, fc, sc in rows:
                    sim = _args_similarity(args_sig, stored_sig)
                    if sim > best_sim:
                        best_sim = sim
                        best_id = pid

                if best_id is not None and best_sim > 0.4:
                    row = conn.execute(
                        "SELECT failure_count, success_count FROM failure_patterns"
                        " WHERE id = ?", (best_id,),
                    ).fetchone()
                    if row:
                        fc, sc = row
                        new_sc = sc + 1
                        confidence = self._compute_confidence(new_sc, fc)
                        conn.execute(
                            "UPDATE failure_patterns SET success_count = ?,"
                            " confidence = ?, last_seen = datetime('now')"
                            " WHERE id = ?",
                            (new_sc, confidence, best_id),
                        )
                        conn.commit()
            except sqlite3.Error:
                pass  # Non-critical; don't warn

    def get_relevant_patterns(
        self, tool_name: str, args: dict | None = None, *, limit: int = _MAX_INJECTED_PATTERNS,
    ) -> list[dict]:
        """Return failure patterns relevant to a pending tool call.

        Filters by confidence >= threshold and sorts by relevance
        (args similarity × confidence).
        """
        args_sig = _normalize_args(tool_name, args or {})

        with self._lock:
            try:
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT id, tool_name, error_fingerprint, args_signature,"
                    "       fix_strategy, failure_count, success_count, confidence"
                    " FROM failure_patterns"
                    " WHERE tool_name = ? AND confidence >= ?"
                    "   AND failure_count >= ?"
                    " ORDER BY confidence DESC LIMIT ?",
                    (tool_name, _PATTERN_CONFIDENCE_THRESHOLD,
                     _PATTERN_MIN_OCCURRENCES, limit * 3),
                ).fetchall()

                if not rows:
                    return []

                # Score by similarity to current args
                scored = []
                for row in rows:
                    pid, tn, fp, stored_sig, fix, fc, sc, conf = row
                    sim = _args_similarity(args_sig, stored_sig)
                    score = sim * conf
                    scored.append({
                        "id": pid,
                        "tool_name": tn,
                        "error_fingerprint": fp,
                        "args_signature": stored_sig,
                        "fix_strategy": fix,
                        "failure_count": fc,
                        "success_count": sc,
                        "confidence": conf,
                        "similarity": sim,
                        "score": score,
                    })

                scored.sort(key=lambda x: x["score"], reverse=True)
                return scored[:limit]
            except sqlite3.Error:
                return []

    def get_fix_strategy(self, tool_name: str, error_content: str) -> str | None:
        """Look up the best-known fix strategy for a specific error."""
        fingerprint = _fingerprint_error(tool_name, error_content)
        with self._lock:
            try:
                conn = self._get_conn()
                row = conn.execute(
                    "SELECT fix_strategy, confidence FROM failure_patterns"
                    " WHERE tool_name = ? AND error_fingerprint = ?"
                    " AND fix_strategy != ''"
                    " ORDER BY confidence DESC LIMIT 1",
                    (tool_name, fingerprint),
                ).fetchone()
                if row and row[1] >= _PATTERN_CONFIDENCE_THRESHOLD:
                    return row[0]
                return None
            except sqlite3.Error:
                return None

    def _compute_confidence(self, success_count: int, failure_count: int) -> float:
        """Compute a confidence score (0.0–1.0) for a pattern.

        Uses a Bayesian-like smoothing: confidence = (success_count + 1) / (total + 2).
        This biases toward 0.5 for patterns with few observations and moves
        toward true rate as evidence accumulates.
        """
        total = success_count + failure_count
        if total == 0:
            return 0.0
        # Wilson-like: add pseudo-counts to avoid 0/1 extremes
        return (success_count + 0.5) / (total + 1.0)

    def _prune_if_needed(self, conn: sqlite3.Connection) -> None:
        """Drop lowest-confidence patterns if over the cap."""
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM failure_patterns"
            ).fetchone()[0]
            if count > _MAX_PATTERNS:
                excess = count - _MAX_PATTERNS
                conn.execute(
                    "DELETE FROM failure_patterns WHERE id IN ("
                    "  SELECT id FROM failure_patterns"
                    "  ORDER BY confidence ASC, last_seen ASC"
                    "  LIMIT ?"
                    ")",
                    (excess,),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        """Return summary statistics for the failure pattern store."""
        with self._lock:
            try:
                conn = self._get_conn()
                total = conn.execute(
                    "SELECT COUNT(*) FROM failure_patterns"
                ).fetchone()[0]
                high_conf = conn.execute(
                    "SELECT COUNT(*) FROM failure_patterns WHERE confidence >= 0.7"
                ).fetchone()[0]
                by_tool = conn.execute(
                    "SELECT tool_name, COUNT(*) as cnt FROM failure_patterns"
                    " GROUP BY tool_name ORDER BY cnt DESC LIMIT 5"
                ).fetchall()
                return {
                    "total_patterns": total,
                    "high_confidence_patterns": high_conf,
                    "top_tools": [{"tool": r[0], "count": r[1]} for r in by_tool],
                }
            except sqlite3.Error:
                return {"total_patterns": 0, "high_confidence_patterns": 0, "top_tools": []}


# ---------------------------------------------------------------------------
# Self-critique: post-turn failure analysis
# ---------------------------------------------------------------------------

class SelfCritique:
    """Lightweight post-execution assessment of tool results.

    Detects failure clusters within a turn and generates corrective
    context to inject before the next LLM call.
    """

    def __init__(self):
        self._last_critique_turn = -_CRITIQUE_COOLDOWN_TURNS
        self._consecutive_failure_turns = 0

    def assess_turn_results(
        self, results: list[tuple[dict, object]], turn_count: int,
    ) -> str | None:
        """Analyze a turn's tool results and return a critique message if warranted.

        Args:
            results: list of (tool_call_dict, ToolResult) tuples
            turn_count: current turn number

        Returns:
            A critique context message string, or None if no critique needed.
        """
        if not results:
            return None

        # Import locally to avoid circular dependency
        from tools import ToolResult as TR

        failures = []
        for tc, result in results:
            name = tc.get("function", {}).get("name", "unknown")
            if isinstance(result, TR) and not result.success:
                failures.append((name, result))

        if not failures:
            self._consecutive_failure_turns = max(0, self._consecutive_failure_turns - 1)
            return None

        # Check cooldown
        if turn_count - self._last_critique_turn < _CRITIQUE_COOLDOWN_TURNS:
            return None

        self._consecutive_failure_turns += 1

        if len(failures) >= _MAX_CRITIQUE_FAILURES:
            self._last_critique_turn = turn_count
            return self._build_cluster_critique(failures)

        # Single failure but with consecutive failure pattern
        if self._consecutive_failure_turns >= 3:
            self._last_critique_turn = turn_count
            return self._build_escalation_critique(failures, self._consecutive_failure_turns)

        return None

    def _build_cluster_critique(
        self, failures: list[tuple[str, object]],
    ) -> str:
        """Build critique for multiple failures in one turn."""
        from tools import ToolResult as TR

        lines = [
            "⚠️ SELF-CRITIQUE: Multiple tool failures detected this turn.",
            "Stop and diagnose before retrying:",
        ]
        for name, result in failures:
            if isinstance(result, TR):
                hint = result.hint or result.content[:200]
            else:
                hint = str(result)[:200]
            lines.append(f"  - {name}: {hint}")
        lines.append("")
        lines.append("SUGGESTED ACTIONS:")
        lines.append("  1. Read relevant files to understand current state")
        lines.append("  2. Check for missing dependencies or wrong paths")
        lines.append("  3. Try a fundamentally different approach")
        lines.append("  4. Use remember() to capture any pattern you discover")
        return "\n".join(lines)

    def _build_escalation_critique(
        self, failures: list[tuple[str, object]], consecutive: int,
    ) -> str:
        """Build critique for escalating failure pattern across turns."""
        from tools import ToolResult as TR

        names = [n for n, _ in failures]
        lines = [
            f"⚠️ SELF-CRITIQUE: Tool failures for {consecutive} consecutive turns.",
            f"Failing tools: {', '.join(names)}",
            "",
            "This pattern suggests a systematic issue. Consider:",
            "  - Is the workspace missing required files or dependencies?",
            "  - Are you using the right tool for the task?",
            "  - Should you break the problem into smaller steps?",
            "  - Would a web_search help with the current blocker?",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Enhanced knowledge categories for project knowledge
# ---------------------------------------------------------------------------

# Valid categories for auto-learned project knowledge
KNOWLEDGE_CATEGORIES = {
    "tool_usage": "Pattern for how to use a specific tool effectively in this project",
    "code_pattern": "Recurring code pattern or convention in this codebase",
    "error_pattern": "Known error pattern and its fix or workaround",
    "convention": "Project-specific convention (naming, structure, imports)",
    "architecture": "High-level architecture insight about the codebase",
    "workaround": "Known workaround for a limitation or bug",
    "dependency": "Information about external dependencies or setup",
    "session_summary": "Auto-generated session context (system-managed)",
    "general": "Uncategorized learning",
}

# Confidence scoring parameters
_INITIAL_CONFIDENCE = 0.5       # new learnings start at 50% confidence
_CONFIDENCE_BOOST_ON_HIT = 0.05  # each hit adds 5% confidence
_CONFIDENCE_DECAY_PER_WEEK = 0.02  # unused learnings decay 2%/week
_MAX_CONFIDENCE = 0.95
_MIN_CONFIDENCE = 0.1
_MIN_CONFIDENCE_FOR_INJECTION = 0.3


def compute_knowledge_confidence(
    hits: int, created_at: str, updated_at: str,
) -> float:
    """Compute confidence score for a knowledge entry.

    Starts at _INITIAL_CONFIDENCE, boosted by hits, decayed by age.
    """
    confidence = _INITIAL_CONFIDENCE + (hits * _CONFIDENCE_BOOST_ON_HIT)
    confidence = min(confidence, _MAX_CONFIDENCE)

    # Age decay (simple: assume 0 for fresh entries)
    # Full implementation would parse ISO timestamps and compute weeks
    # For now, just cap at min

    return max(confidence, _MIN_CONFIDENCE)


def suggest_category(topic: str, detail: str = "") -> str:
    """Auto-suggest a knowledge category based on topic/detail keywords."""
    combined = f"{topic} {detail}".lower()

    if any(kw in combined for kw in ("edit_file", "whitespace", "mismatch", "fuzzy")):
        return "tool_usage"
    if any(kw in combined for kw in ("import", "module", "package", "dependency")):
        return "code_pattern"
    if any(kw in combined for kw in ("error", "fail", "crash", "bug", "fix")):
        return "error_pattern"
    if any(kw in combined for kw in ("convention", "naming", "style", "pattern")):
        return "convention"
    if any(kw in combined for kw in ("architecture", "design", "structure", "pipeline")):
        return "architecture"
    if any(kw in combined for kw in ("workaround", "hack", "bypass", "known issue")):
        return "workaround"
    if any(kw in combined for kw in ("install", "setup", "version", "require")):
        return "dependency"
    return "general"


# ---------------------------------------------------------------------------
# Convenience: build self-learning context for injection
# ---------------------------------------------------------------------------

def build_self_learning_context(
    pattern_store: FailurePatternStore | None,
    pending_tool_calls: list[dict] | None = None,
) -> str | None:
    """Build a context message with relevant failure patterns for pending tool calls.

    Called before tool execution to inject preventative guidance.
    Returns a string to append as a system message, or None.
    """
    if pattern_store is None or not pending_tool_calls:
        return None

    warnings_parts: list[str] = []
    for tc in pending_tool_calls:
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}

        patterns = pattern_store.get_relevant_patterns(name, args)
        if patterns:
            for p in patterns:
                confidence_pct = int(p["confidence"] * 100)
                fp = p["error_fingerprint"]
                fix = p["fix_strategy"] or "no known fix yet"
                warnings_parts.append(
                    f"  ⚠️ {name}: pattern '{fp}' has failed {p['failure_count']}x "
                    f"(confidence: {confidence_pct}%). Fix: {fix}"
                )

    if warnings_parts:
        return (
            "🛡️ FAILURE PATTERN WARNINGS (learned from past sessions):\n"
            + "\n".join(warnings_parts)
            + "\n\nConsider these before making the same call. "
            "Read the file first, check paths, try a different approach."
        )

    return None
