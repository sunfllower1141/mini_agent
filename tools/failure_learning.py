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

import json
import sqlite3
import threading
import warnings

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
    """Extract a stable error fingerprint from tool result content.

    Returns the same space-separated format as tools/__init__._fingerprint_error
    so that fingerprints stored by FailurePatternStore match those from
    _learn_from_failure and _FAILURE_PATTERNS.
    """
    cl = content.lower()
    if name == "edit_file":
        if "not found" in cl or "does not exist" in cl:
            return "not found"
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
            return "not found"
        if "offset" in cl or "exceeds" in cl:
            return "offset"
    elif name == "search_files":
        if "no matches" in cl or "not found" in cl:
            return "not found"
        if "invalid" in cl and "regex" in cl:
            return "invalid regex"
    elif name == "run_shell":
        if "not found" in cl or "command not found" in cl:
            return "not found"
        if "blocked" in cl or "destructive" in cl:
            return "blocked"
        if "timed out" in cl or "timeout" in cl:
            return "timed out"
    elif name in ("find_symbol", "find_usages"):
        if "no match" in cl or "not found" in cl:
            return "not found"
    elif name in ("run_tests", "verify"):
        if "fail" in cl or "FAILED" in cl:
            return "failures"
    # Fallback: return truncated content (matches tools/__init__ behavior)
    return content[:60].strip().lower()


# Prefer the canonical version from tools/__init__ when available so
# fingerprints stored by FailurePatternStore match those from
# _learn_from_failure and _FAILURE_PATTERNS.  The local fallback above
# is only used when tools/__init__ cannot be imported (bootstrapping).
if _core_fingerprint is not None:
    _fingerprint_error = _core_fingerprint  # type: ignore[assignment]


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
        """Compute a confidence score (0.0–1.0) for a pattern's existence.

        Confidence increases with total observations (more data = more certain
        this is a real pattern).  Starts at ~0.65 for a single observation
        and asymptotically approaches 1.0.

        Success/failure ratio affects the score: all-failure patterns get
        slightly lower confidence than patterns with some successes
        (indicating the fix works), but the dominant factor is sample size.
        """
        total = success_count + failure_count
        if total == 0:
            return 0.0

        # Base: sample-size confidence (0.3–1.0 as observations grow)
        base = 0.3 + 0.5 * (1.0 - 1.0 / (total + 1.0))

        # Fix-quality bonus (0.0–0.2 extra if fix strategy works)
        if total > 0:
            fix_bonus = 0.2 * (success_count / total)
        else:
            fix_bonus = 0.0

        return min(0.95, base + fix_bonus)

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



# ---------------------------------------------------------------------------
# MistakeNotebook — MNL-lite: batch-cluster failures, distill generalized fixes
# ---------------------------------------------------------------------------

# Minimum cluster size (shared fingerprint across different args) to create
# a notebook entry.
_NOTEBOOK_MIN_CLUSTER_SIZE = 3

# Minimum confidence for a notebook entry to be considered "accepted"
_NOTEBOOK_ACCEPTANCE_CONFIDENCE = 0.6

# Maximum notebook entries stored (prune lowest-confidence)
_NOTEBOOK_MAX_ENTRIES = 100

# Cooldown: turns between notebook entry injections
_NOTEBOOK_INJECTION_COOLDOWN = 5

# Maximum notebook entries injected per turn
_NOTEBOOK_MAX_INJECTED = 2

MISTAKE_NOTEBOOK_DDL = """
CREATE TABLE IF NOT EXISTS mistake_notebook (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name       TEXT    NOT NULL,
    error_fingerprint TEXT  NOT NULL,
    generalized_fix TEXT    NOT NULL DEFAULT '',
    cluster_size    INTEGER NOT NULL DEFAULT 0,
    distinct_args   INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 0.0,
    times_applied   INTEGER NOT NULL DEFAULT 0,
    times_succeeded INTEGER NOT NULL DEFAULT 0,
    last_distilled  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

MISTAKE_NOTEBOOK_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mn_tool_err ON mistake_notebook(tool_name, error_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_mn_confidence ON mistake_notebook(confidence DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mn_unique ON mistake_notebook(tool_name, error_fingerprint)",
]


class MistakeNotebook:
    """MNL-lite: structured mistake notebook with batch-clustering.

    Periodically scans failure_patterns for recurring fingerprints across
    different argument signatures.  When a fingerprint appears with
    multiple distinct args patterns, it distills a generalized fix and
    stores it in the ``mistake_notebook`` table.

    Uses an "accept-if-improves" rule: entries are only injected when
    their confidence exceeds _NOTEBOOK_ACCEPTANCE_CONFIDENCE.

    Inspired by: MNL (Mistake Notebook Learning) — batch-clustered
    mistake abstraction with structured notebooks.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_distill_turn = -1
        self._last_injection_turn = -_NOTEBOOK_INJECTION_COOLDOWN

    def _get_conn(self) -> sqlite3.Connection:
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
        """Create mistake_notebook table and indexes."""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(MISTAKE_NOTEBOOK_DDL)
                for idx_sql in MISTAKE_NOTEBOOK_INDEXES:
                    try:
                        conn.execute(idx_sql)
                    except sqlite3.Error:
                        pass
                conn.commit()
            except sqlite3.Error:
                warnings.warn("Failed to init mistake_notebook table", stacklevel=2)

    # ------------------------------------------------------------------
    # Distillation: batch-cluster failure patterns into notebook entries
    # ------------------------------------------------------------------

    def distill(self, turn_count: int) -> int:
        """Scan failure_patterns for clusters and distill generalized fixes.

        Only runs every _NOTEBOOK_INJECTION_COOLDOWN turns to avoid churn.
        Returns the number of new/updated notebook entries.
        """
        if turn_count - self._last_distill_turn < _NOTEBOOK_INJECTION_COOLDOWN:
            return 0

        self._last_distill_turn = turn_count
        new_entries = 0

        with self._lock:
            try:
                conn = self._get_conn()

                # Find fingerprints that appear with >= MIN_CLUSTER_SIZE
                # distinct args_signatures for the same tool+error combo.
                rows = conn.execute(
                    "SELECT tool_name, error_fingerprint,"
                    "       COUNT(DISTINCT args_signature) as distinct_args,"
                    "       SUM(failure_count) as total_failures,"
                    "       SUM(success_count) as total_successes,"
                    "       GROUP_CONCAT(fix_strategy, '|') as all_fixes"
                    " FROM failure_patterns"
                    " WHERE args_signature != ''"
                    " GROUP BY tool_name, error_fingerprint"
                    " HAVING COUNT(DISTINCT args_signature) >= ?"
                    " ORDER BY total_failures DESC",
                    (_NOTEBOOK_MIN_CLUSTER_SIZE,),
                ).fetchall()

                for (tool_name, fingerprint, distinct_args,
                     total_failures, total_successes, all_fixes) in rows:

                    # --- Distill best fix from collected strategies ---
                    fixes = [
                        f for f in (all_fixes or "").split("|")
                        if f and f.strip()
                    ]
                    generalized_fix = self._distill_fix(
                        tool_name, fingerprint, fixes,
                    )

                    # Compute confidence: aggregate across cluster
                    total = total_failures + total_successes
                    confidence = ((total_successes or 0) + 0.5) / (total + 1.0)

                    # Upsert into mistake_notebook
                    conn.execute(
                        "INSERT INTO mistake_notebook"
                        " (tool_name, error_fingerprint, generalized_fix,"
                        "  cluster_size, distinct_args, confidence)"
                        " VALUES (?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(tool_name, error_fingerprint) DO UPDATE SET"
                        "  generalized_fix = excluded.generalized_fix,"
                        "  cluster_size = excluded.cluster_size,"
                        "  distinct_args = excluded.distinct_args,"
                        "  confidence = excluded.confidence,"
                        "  last_distilled = datetime('now')",
                        (tool_name, fingerprint, generalized_fix,
                         total_failures, distinct_args, confidence),
                    )
                    new_entries += 1

                conn.commit()

                # Prune low-confidence entries if over cap
                self._prune_if_needed(conn)

            except sqlite3.Error:
                pass  # Non-critical

        return new_entries

    def _distill_fix(
        self,
        tool_name: str,
        fingerprint: str,
        collected_fixes: list[str],
    ) -> str:
        """Distill a generalized fix from collected strategies.

        For known fingerprints, provides tool-specific default guidance.
        Otherwise picks the most common strategy from collected fixes.
        """
        # Tool-specific default guidance for common fingerprints
        known_guidance: dict[str, dict[str, str]] = {
            "edit_file": {
                "not_found": (
                    "The old_string was not found in the file. "
                    "Use read_file FIRST to see exact text (including "
                    "whitespace/indentation), then copy-paste the exact "
                    "old_string. Do NOT type it from memory."
                ),
                "whitespace": (
                    "Whitespace/indentation mismatch in old_string. "
                    "Use read_file with line_numbers=true to see exact "
                    "indentation. Copy-paste, don't retype."
                ),
                "ambiguous": (
                    "old_string appears multiple times in the file. "
                    "Use count=-1 to replace all occurrences, or include "
                    "more surrounding context to make old_string unique."
                ),
            },
            "read_file": {
                "not_found": (
                    "File does not exist. Use list_directory to verify "
                    "the path, or check for typos in the filename."
                ),
            },
            "run_shell": {
                "not_found": (
                    "Command not found. Verify it's installed (pip install "
                    "or brew install). Check PATH and command spelling."
                ),
                "timed_out": (
                    "Command timed out. Try increasing the timeout parameter "
                    "or breaking the work into smaller steps."
                ),
            },
            "search_files": {
                "not_found": (
                    "No matches found. Try find_symbol for symbol lookups, "
                    "or broaden your search pattern. Consider using regex=false "
                    "for literal text searches."
                ),
            },
        }

        # Check known guidance
        tool_guidance = known_guidance.get(tool_name, {})
        if fingerprint in tool_guidance:
            return tool_guidance[fingerprint]

        # Fallback: use most common collected fix
        if collected_fixes:
            from collections import Counter
            most_common = Counter(collected_fixes).most_common(1)[0][0]
            return most_common

        return f"Unknown fix pattern for {fingerprint}. Try a different approach."

    # ------------------------------------------------------------------
    # Injection: get relevant notebook entries for context injection
    # ------------------------------------------------------------------

    def get_injectable_entries(
        self,
        pending_tool_calls: list[dict] | None = None,
        *,
        limit: int = _NOTEBOOK_MAX_INJECTED,
    ) -> list[dict]:
        """Return notebook entries relevant to pending tool calls.

        Only returns entries with confidence >= _NOTEBOOK_ACCEPTANCE_CONFIDENCE.
        """
        if not pending_tool_calls:
            return []

        tool_names = list({
            tc.get("function", {}).get("name", "")
            for tc in pending_tool_calls
        })
        if not tool_names:
            return []

        with self._lock:
            try:
                conn = self._get_conn()
                placeholders = ",".join("?" for _ in tool_names)
                rows = conn.execute(
                    f"SELECT tool_name, error_fingerprint, generalized_fix,"
                    f"       cluster_size, confidence, times_applied, times_succeeded"
                    f" FROM mistake_notebook"
                    f" WHERE tool_name IN ({placeholders})"
                    f"  AND confidence >= ?"
                    f" ORDER BY confidence DESC LIMIT ?",
                    (*tool_names, _NOTEBOOK_ACCEPTANCE_CONFIDENCE, limit),
                ).fetchall()

                return [
                    {
                        "tool_name": r[0],
                        "error_fingerprint": r[1],
                        "generalized_fix": r[2],
                        "cluster_size": r[3],
                        "confidence": r[4],
                        "times_applied": r[5],
                        "times_succeeded": r[6],
                    }
                    for r in rows
                ]
            except sqlite3.Error:
                return []

    def build_notebook_context(
        self,
        pending_tool_calls: list[dict] | None = None,
        turn_count: int = 0,
    ) -> str | None:
        """Build a context message with relevant notebook entries.

        Respects injection cooldown to avoid flooding.
        """
        if not pending_tool_calls:
            return None

        if turn_count - self._last_injection_turn < _NOTEBOOK_INJECTION_COOLDOWN:
            return None

        entries = self.get_injectable_entries(pending_tool_calls)
        if not entries:
            return None

        self._last_injection_turn = turn_count

        parts = ["MISTAKE NOTEBOOK (generalized fixes from past failures):"]
        for e in entries:
            confidence_pct = int(e["confidence"] * 100)
            parts.append(
                f"  - {e['tool_name']} [{e['error_fingerprint']}]: "
                f"{e['generalized_fix']} "
                f"(seen {e['cluster_size']}x, "
                f"{confidence_pct}% confidence)"
            )

        return "\n".join(parts)

    def record_application(
        self,
        tool_name: str,
        error_fingerprint: str,
        *,
        was_successful: bool = False,
    ) -> None:
        """Record that a notebook entry was applied (and whether it succeeded)."""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.execute(
                    "UPDATE mistake_notebook SET"
                    "  times_applied = times_applied + 1,"
                    "  times_succeeded = times_succeeded + ?"
                    " WHERE tool_name = ? AND error_fingerprint = ?",
                    (1 if was_successful else 0, tool_name, error_fingerprint),
                )
                conn.commit()
            except sqlite3.Error:
                pass

    def _prune_if_needed(self, conn: sqlite3.Connection) -> None:
        """Drop lowest-confidence entries if over the cap."""
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM mistake_notebook"
            ).fetchone()[0]
            if count > _NOTEBOOK_MAX_ENTRIES:
                excess = count - _NOTEBOOK_MAX_ENTRIES
                conn.execute(
                    "DELETE FROM mistake_notebook WHERE id IN ("
                    "  SELECT id FROM mistake_notebook"
                    "  ORDER BY confidence ASC, last_distilled ASC"
                    "  LIMIT ?"
                    ")",
                    (excess,),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        """Return summary statistics for the mistake notebook."""
        with self._lock:
            try:
                conn = self._get_conn()
                total = conn.execute(
                    "SELECT COUNT(*) FROM mistake_notebook"
                ).fetchone()[0]
                accepted = conn.execute(
                    "SELECT COUNT(*) FROM mistake_notebook"
                    " WHERE confidence >= ?",
                    (_NOTEBOOK_ACCEPTANCE_CONFIDENCE,),
                ).fetchone()[0]
                return {
                    "total_entries": total,
                    "accepted_entries": accepted,
                    "acceptance_threshold": _NOTEBOOK_ACCEPTANCE_CONFIDENCE,
                }
            except sqlite3.Error:
                return {"total_entries": 0, "accepted_entries": 0}


# ---------------------------------------------------------------------------
# Step-level experience retrieval — dynamic project_knowledge injection
# ---------------------------------------------------------------------------

# Maximum knowledge entries to inject per turn
_MAX_KNOWLEDGE_INJECT_PER_TURN = 3

# Minimum importance for dynamic injection
_MIN_KNOWLEDGE_IMPORTANCE_DYNAMIC = 1


def build_experience_context(
    memory_store,
    tool_name: str,
    args: dict | None = None,
    *,
    limit: int = _MAX_KNOWLEDGE_INJECT_PER_TURN,
) -> str | None:
    """Build context with relevant past experiences for a pending tool call.

    Dynamically searches project_knowledge for entries relevant to the
    current tool and arguments.  Returns a context string or None.

    Uses keyword matching against the knowledge topic + detail fields
    to find relevant past learnings.
    """
    if memory_store is None:
        return None

    try:
        # Build search terms from tool name and args
        search_terms = [tool_name]

        if args:
            # Extract path-like args for matching
            path = args.get("path", "") or args.get("file_path", "")
            if path:
                import os
                ext = os.path.splitext(path)[1]
                if ext:
                    search_terms.append(ext)
                basename = os.path.basename(path)
                if basename:
                    search_terms.append(basename)

            # Extract command-like args
            command = args.get("command", "") or args.get("pattern", "")
            if command and len(command) < 60:
                search_terms.append(command)

            # Extract old_string snippets for edit_file
            old = args.get("old_string", "")
            if old and len(old) < 80:
                first_word = old.strip().split()[0] if old.strip() else ""
                if first_word:
                    search_terms.append(first_word)

        # Query project_knowledge with relevance ranking
        all_knowledge = memory_store.list_knowledge(
            importance_min=_MIN_KNOWLEDGE_IMPORTANCE_DYNAMIC,
        )

        if not all_knowledge:
            return None

        # Score each knowledge entry by term overlap
        scored = []
        for entry in all_knowledge:
            topic = (entry.get("summary") or entry.get("topic", "")).lower()
            detail = (entry.get("detail", "")).lower()
            category = (entry.get("category", "")).lower()
            combined = f"{topic} {detail} {category}"

            score = 0
            for term in search_terms:
                term_lower = term.lower()
                if term_lower in topic:
                    score += 3  # Topic match is strongest
                elif term_lower in detail:
                    score += 2
                elif term_lower in category:
                    score += 1

            if score > 0:
                scored.append((score, entry))

        if not scored:
            return None

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]

        parts = ["RELEVANT PAST EXPERIENCES:"]
        for score, entry in top:
            topic = entry.get("summary") or entry.get("topic", "?")
            detail = entry.get("detail", "")[:200]
            cat = entry.get("category", "general")
            parts.append(f"  [{cat}] {topic}: {detail}")

        return "\n".join(parts)

    except Exception:
        return None


def build_experience_context_from_text(
    memory_store,
    text: str,
    *,
    limit: int = _MAX_KNOWLEDGE_INJECT_PER_TURN,
) -> str | None:
    """Build context with relevant past experiences from a plain text query.

    Unlike build_experience_context(), this takes arbitrary text (e.g., the
    user's last message) instead of requiring a tool_name + args dict.
    Extracts keywords from the text and scores project_knowledge entries
    by overlap.
    """
    if memory_store is None or not text:
        return None

    try:
        # Tokenize text into search terms (words 3+ chars, skip common words)
        import re as _re
        STOP = {
            "the", "and", "for", "that", "this", "with", "from", "have",
            "what", "when", "where", "which", "does", "will", "would",
            "should", "could", "about", "your", "just", "like", "been",
        }
        words = _re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2}", text.lower())
        search_terms = [w for w in words if w not in STOP][:10]

        if not search_terms:
            return None

        # Query project_knowledge with relevance ranking
        all_knowledge = memory_store.list_knowledge(
            importance_min=_MIN_KNOWLEDGE_IMPORTANCE_DYNAMIC,
        )
        if not all_knowledge:
            return None

        # Score each knowledge entry by term overlap
        scored = []
        for entry in all_knowledge:
            topic = (entry.get("summary") or entry.get("topic", "")).lower()
            detail = (entry.get("detail", "")).lower()
            category = (entry.get("category", "")).lower()
            combined = f"{topic} {detail} {category}"

            score = 0
            for term in search_terms:
                if term in topic:
                    score += 3
                elif term in detail:
                    score += 2
                elif term in category:
                    score += 1

            if score > 0:
                scored.append((score, entry))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]

        parts = ["RELEVANT PAST EXPERIENCES:"]
        for score, entry in top:
            topic = entry.get("summary") or entry.get("topic", "?")
            detail = entry.get("detail", "")[:200]
            cat = entry.get("category", "general")
            parts.append(f"  [{cat}] {topic}: {detail}")

        return "\n".join(parts)

    except Exception:
        return None


def build_experience_context_batch(
    memory_store,
    pending_tool_calls: list[dict],
    *,
    limit: int = _MAX_KNOWLEDGE_INJECT_PER_TURN,
) -> str | None:
    """Build experience context for a batch of pending tool calls.

    Aggregates relevant past experiences across all pending calls.
    """
    if not memory_store or not pending_tool_calls:
        return None

    all_parts: list[str] = []
    seen_topics: set[str] = set()

    for tc in pending_tool_calls[:5]:  # Limit to first 5 calls
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}

        ctx = build_experience_context(
            memory_store, name, args, limit=2,
        )
        if ctx:
            for line in ctx.split("\n"):
                line_stripped = line.strip()
                if line_stripped and line_stripped not in seen_topics:
                    seen_topics.add(line_stripped)
                    all_parts.append(line_stripped)

    if not all_parts:
        return None

    header = "RELEVANT PAST EXPERIENCES:"
    return header + "\n" + "\n".join(all_parts)
