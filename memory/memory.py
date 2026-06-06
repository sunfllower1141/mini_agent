#!/usr/bin/env python3
"""
memory.py — persistent conversation memory for mini_agent (SQLite backend).

Stores messages as rows in a local SQLite database.  Provides the same API
as the old JSON-backed MemoryStore so the orchestrator is unchanged.

Memory management (in order, applied on every save):
    1. Compress old tool results — keep only the first line for results
       more than N messages ago.
    2. Token-aware pruning — drop oldest turns until under max_tokens
       (preserving tool-call sequences and turn boundaries).
    3. Conversation summarization — when pruning removes messages, a
       synthetic "Earlier context" summary is injected so the agent
       retains awareness of what happened even when details are gone.

Migrates existing ``.mini_agent_memory.json`` files automatically on first run.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import warnings
from typing import Optional

from logging_setup import get_logger

_mem_log = get_logger("memory")

# --- sqlite3 error escalation: track consecutive errors ---
_consecutive_sqlite_errors: int = 0
_CONSECUTIVE_ERROR_THRESHOLD = 3

def _on_sqlite_error(operation: str) -> None:
    """Track consecutive sqlite3 errors and log/escalate when threshold hit."""
    global _consecutive_sqlite_errors
    _consecutive_sqlite_errors += 1
    _mem_log.warning("sqlite3 error in %s (consecutive=%d)", operation, _consecutive_sqlite_errors)
    if _consecutive_sqlite_errors >= _CONSECUTIVE_ERROR_THRESHOLD:
        _mem_log.error("sqlite3 error escalation: %d consecutive errors — memory persistence may be degraded", 
                       _consecutive_sqlite_errors)

def _reset_sqlite_errors() -> None:
    """Reset the consecutive sqlite3 error counter on successful operation."""
    global _consecutive_sqlite_errors
    if _consecutive_sqlite_errors > 0:
        _consecutive_sqlite_errors = 0


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,   -- JSON blob of the full message dict
    created_at TEXT    DEFAULT CURRENT_TIMESTAMP
)
"""

_INSERT  = "INSERT INTO messages (role, content) VALUES (?, ?)"
_SELECT  = "SELECT role, content FROM messages ORDER BY id ASC"
_DELETE  = "DELETE FROM messages"
_VACUUM  = "VACUUM"

# VACUUM threshold: only reclaim disk space when freelist exceeds this
# page count.  Avoids running VACUUM on every full-rewrite save.
_VACUUM_FREELIST_THRESHOLD = 1000

# Performance: avoid pruning on every save at long conversations.
# Only prune when token usage exceeds this fraction of max_tokens
# (e.g. 1.15 = only prune when 15% over budget), and skip pruning
# for at least _PRUNE_COOLDOWN saves after a prune.
_PRUNE_OVERAGE_BUFFER = 1.15
_PRUNE_COOLDOWN = 3  # saves to skip before pruning again

# VACUUM interval: run VACUUM every N saves regardless of freelist count.
# This matters now that we do fewer full rewrites — the freelist may not
# bloat rapidly but periodic compaction is still healthy.
_VACUUM_INTERVAL = 50

# Save retry: when the database is locked, retry with backoff before
# surfacing the warning to the user.
_SAVE_MAX_RETRIES = 3
_SAVE_RETRY_DELAY = 0.25  # seconds, multiplied by attempt number

# ---------------------------------------------------------------------------
# Pruning / compression / summarization — imported from memory_prune.py
# (extracted to keep MemoryStore focused on persistence).
# ---------------------------------------------------------------------------

from .memory_prune import (  # noqa: F401 — re-exported for backward compatibility
    _CHARS_PER_TOKEN,
    _MIN_TOKEN_ESTIMATE,
    _COMPRESSION_KEEP_RECENT,
    _COMPRESSION_MAX_LINES,
    _COMPRESSION_MAX_FIRST_LINE,
    _SUMMARY_PREVIEW_LENGTH,
    _SUMMARY_PATH_PREVIEW,
    _SUMMARY_MAX_TURNS,
    _SUMMARY_MAX_FILES,
    _SUMMARY_MAX_COMMANDS,
    _MARKDOWN_TOOL_RESULT_PREVIEW,
    _TOOL_PARSE_CACHE,
    _TOKEN_EST_CACHE,
    _ACCUM_STATE,
    _clear_message_caches,
    _get_tool_content,
    _estimate_tokens,
    _total_tokens,
    _find_tool_call_name,
    _find_tool_call_args,
    _compress_tool_results,
    _compress_read_file,
    _compress_search_files,
    _is_match_line,
    _compress_run_shell,
    _compress_run_tests,
    _compress_default,
    _build_compressed,
    _summarize_pruned,
    _summarize_pruned_rules,
    _strip_orphaned_tool_results,
    _strip_orphaned_tool_messages,
    _prune_by_tokens,
)


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """Persists conversation messages in a SQLite database.

    The system message is intentionally excluded from persistence.
    On load, callers are expected to prepend their own system prompt.

    *max_tokens* controls token-aware pruning: old turns are removed
    when the estimated token count exceeds the limit.  *max_messages*
    is a hard cap applied first.  Both preserve tool-call sequences
    and turn boundaries.

    Old tool results are compressed (first-line only) after they fall
    more than 6 messages behind the tail.  Pruned messages are summarized
    into a synthetic context message.
    """

    DEFAULT_MAX_MESSAGES = 50
    DEFAULT_MAX_TOKENS   = 80_000

    def __init__(
        self,
        filepath: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._filepath = filepath
        self._db_path = _db_path(filepath)
        self._max_messages = max_messages
        self._max_tokens = max_tokens
        self._last_saved_count = 0  # for incremental save
        self._conn: Optional[sqlite3.Connection] = None
        self._token_count: int = 0  # running accumulator for saved messages
        self._vacuum_thread: Optional[threading.Thread] = None  # background VACUUM
        self._skip_load: bool = False  # set True to skip loading knowledge/summaries (used by switch_session)
        self._save_count: int = 0  # monotonic save counter for periodic VACUUM
        self._prune_cooldown: int = 0  # saves remaining before next pruning allowed

        # Detect remote filesystems early — if the workspace is on a network
        # mount, pre-emptively switch to a local path so that downstream
        # consumers (FailurePatternStore, etc.) also use the correct path.
        if _is_remote_fs(self._db_path):
            self._db_path = _local_db_path(self._db_path)
            _mem_log.info("remote filesystem detected — using local database: %s", self._db_path)

        # Migrate from old paths if needed
        _migrate_old_paths(filepath, self._db_path)

        # Ensure parent directory exists
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            conn = self._get_conn()
            conn.execute(_CREATE_TABLE)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "content TEXT NOT NULL DEFAULT ''"
                ")"
            )
            # Ensure a row always exists
            conn.execute("INSERT OR IGNORE INTO scratchpad (id, content) VALUES (1, '')")
            # Plan state table — persists plan steps across sessions
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plan_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "steps_json TEXT NOT NULL DEFAULT '[]',"
                "done_json TEXT NOT NULL DEFAULT '[]'"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO plan_state (id, steps_json, done_json) VALUES (1, '[]', '[]')")
            # Test output table — persisted so agent can read failures without re-running
            conn.execute(
                "CREATE TABLE IF NOT EXISTS test_output ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "output TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO test_output (id, output) VALUES (1, '')")
            # Project knowledge table — persists across sessions within a workspace
            conn.execute(
                "CREATE TABLE IF NOT EXISTS project_knowledge ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "category TEXT NOT NULL DEFAULT 'general',"
                "summary TEXT NOT NULL,"
                "detail TEXT NOT NULL DEFAULT '',"
                "importance INTEGER NOT NULL DEFAULT 1,"
                "hits INTEGER NOT NULL DEFAULT 0,"
                "created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            # Failure patterns table — self-learning from tool failures (MPR/VIGIL-inspired)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS failure_patterns ("
                "id              INTEGER PRIMARY KEY AUTOINCREMENT,"
                "tool_name       TEXT    NOT NULL,"
                "error_fingerprint TEXT  NOT NULL,"
                "args_signature  TEXT    NOT NULL DEFAULT '',"
                "fix_strategy    TEXT    NOT NULL DEFAULT '',"
                "success_count   INTEGER NOT NULL DEFAULT 0,"
                "failure_count   INTEGER NOT NULL DEFAULT 1,"
                "confidence      REAL    NOT NULL DEFAULT 0.0,"
                "last_seen       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_tool_err"
                " ON failure_patterns(tool_name, error_fingerprint)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_confidence"
                " ON failure_patterns(confidence DESC)"
            )
            conn.commit()
        except sqlite3.Error as e:
            warnings.warn(
                f"Failed to initialize memory tables: {e}. "
                f"(path={self._db_path})",
                stacklevel=2,
            )
            # Reset connection so _get_conn() will retry (possibly with
            # a local fallback path) on the next operation.
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    @property
    def filepath(self) -> str:
        return self._filepath

    @property
    def token_count(self) -> int:
        """Return the running token estimate for the currently saved messages."""
        return self._token_count

    def _get_conn(self) -> sqlite3.Connection:
        """Return a cached SQLite connection with WAL mode enabled.

        Creates the connection on first call.  All internal methods
        share this single connection instead of opening a new one
        for every operation.

        Pings the cached connection with ``SELECT 1`` before use.
        If the connection was closed (e.g. by a forked subprocess
        or a prior error), it is transparently recreated.

        On remote/network filesystems (SMB, NFS, AFP), WAL journal
        mode is unreliable due to POSIX lock limitations.  Falls back:
          1. journal_mode=DELETE + locking_mode=EXCLUSIVE
          2. If even that fails, uses a local temp path
             (~/.mini_agent/memory/<hash>.db)
        """
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
            except sqlite3.Error:
                # Connection is dead — recreate it
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

        if self._conn is not None:
            return self._conn

        # Determine the best database path and journal mode
        db_path = self._db_path
        use_wal = True

        if _is_remote_fs(db_path):
            use_wal = False
            _mem_log.info("remote filesystem detected for %s — falling back to DELETE journal mode", db_path)

        # Attempt 1: WAL mode on local FS (fast path)
        if use_wal:
            try:
                self._conn = sqlite3.connect(db_path)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA cache_size=-8000")
                self._conn.execute("PRAGMA temp_store=MEMORY")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.execute("PRAGMA foreign_keys=ON")
                return self._conn
            except sqlite3.OperationalError:
                _mem_log.warning("WAL mode failed for %s — trying DELETE journal mode", db_path)
                try:
                    if self._conn is not None:
                        self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

        # Attempt 2: DELETE journal mode (works on remote FS, but slower)
        try:
            self._conn = sqlite3.connect(db_path)
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            self._conn.execute("PRAGMA cache_size=-8000")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            return self._conn
        except sqlite3.OperationalError as e:
            _mem_log.warning("DELETE journal mode failed for %s: %s", db_path, e)
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

        # Attempt 3: Local fallback path (last resort for remote FS)
        local_path = _local_db_path(db_path)
        _mem_log.info("falling back to local database path: %s", local_path)
        self._conn = sqlite3.connect(local_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-8000")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Update _db_path so future reconnect attempts use the local path
        self._db_path = local_path
        return self._conn

    def _start_background_vacuum(self) -> None:
        """Run VACUUM on a private connection in a daemon thread.

        VACUUM rewrites the entire database file and can be slow on
        large databases.  Offloading it to a background thread keeps
        the main agent loop responsive.  If a previous background
        VACUUM is still running, this is a no-op (the previous vacuum
        will already reclaim free pages).
        """
        # If a previous vacuum thread is still running, don't pile on.
        if self._vacuum_thread is not None and self._vacuum_thread.is_alive():
            return

        db_path = self._db_path

        def _run() -> None:
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("VACUUM")
                conn.close()
            except sqlite3.Error:
                pass

        t = threading.Thread(target=_run, daemon=True)
        self._vacuum_thread = t
        t.start()

    def close(self) -> None:
        """Close the shared database connection (if open)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                warnings.warn("Failed to close DB connection", stacklevel=3)
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> list[dict]:
        """Load saved messages, stripping incomplete tool-call sequences."""
        try:
            conn = self._get_conn()
            rows = conn.execute(_SELECT).fetchall()
        except sqlite3.Error:
            warnings.warn("Failed to query result messages", stacklevel=2)
            return []

        return _clean_messages([_row_to_msg(r) for r in rows])


    # ------------------------------------------------------------------
    # Project knowledge (cross-session learning)
    # ------------------------------------------------------------------

    def add_knowledge(
        self, summary: str, category: str = "general",
        detail: str = "", importance: int = 1,
    ) -> None:
        """Store a project-level learning that persists across sessions."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail, importance)"
                " VALUES (?, ?, ?, ?)",
                (category, summary, detail, importance),
            )
            conn.commit()
        except sqlite3.Error:
            warnings.warn("Failed to store project knowledge", stacklevel=2)

    def get_top_knowledge(self, limit: int = 20) -> list[dict]:
        """Return highest-importance knowledge entries."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, category, summary, detail, importance, hits"
                " FROM project_knowledge"
                " ORDER BY importance * (hits + 1) DESC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"id": r[0], "category": r[1], "summary": r[2],
                 "detail": r[3], "importance": r[4], "hits": r[5]}
                for r in rows
            ]
        except sqlite3.Error:
            warnings.warn("Failed to query project knowledge", stacklevel=2)
            return []

    def bump_knowledge(self, knowledge_id: int) -> None:
        """Increment the hit counter for a knowledge entry."""
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE project_knowledge SET hits = hits + 1,"
                " updated_at = datetime('now') WHERE id = ?",
                (knowledge_id,),
            )
            conn.commit()
        except sqlite3.Error:
            warnings.warn("Failed to bump project knowledge", stacklevel=2)

    def find_knowledge(self, category: str, summary: str) -> dict | None:
        """Find a knowledge entry by category + summary prefix match.
        Returns the row dict or None. Used by auto-learn to bump existing entries."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, category, summary, detail, importance, hits"
                " FROM project_knowledge"
                " WHERE category = ? AND summary LIKE ?"
                " LIMIT 1",
                (category, summary + "%"),
            ).fetchall()
            if rows:
                r = rows[0]
                return {
                    "id": r[0], "category": r[1], "summary": r[2],
                    "detail": r[3], "importance": r[4], "hits": r[5],
                }
            return None
        except sqlite3.Error:
            warnings.warn("Failed to query project knowledge", stacklevel=2)
            return None

    def list_knowledge(
        self, category: str = "", importance_min: int = 0, limit: int = 200,
    ) -> list[dict]:
        """List project knowledge entries, optionally filtered.

        Args:
            category: filter by category prefix (empty = all)
            importance_min: minimum importance to include (default 0 = all)
            limit: max entries to return

        Returns list of dicts with keys: id, category, summary, detail,
        importance, hits.
        """
        try:
            conn = self._get_conn()
            if category:
                rows = conn.execute(
                    "SELECT id, category, summary, detail, importance, hits"
                    " FROM project_knowledge"
                    " WHERE category = ? AND importance >= ?"
                    " ORDER BY importance * (hits + 1) DESC"
                    " LIMIT ?",
                    (category, importance_min, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, category, summary, detail, importance, hits"
                    " FROM project_knowledge"
                    " WHERE importance >= ?"
                    " ORDER BY importance * (hits + 1) DESC"
                    " LIMIT ?",
                    (importance_min, limit),
                ).fetchall()
            return [
                {"id": r[0], "category": r[1], "summary": r[2],
                "detail": r[3], "importance": r[4], "hits": r[5]}
                for r in rows
            ]
        except sqlite3.Error:
            warnings.warn("Failed to list project knowledge", stacklevel=2)
            return []

    def write_handoff(
        self, changes: str, pending: str = "", modified_files: str = "",
    ) -> None:
        """Write a HANDOFF.md file in the workspace root for session continuity.

        This is complementary to the DB-backed session_summary.  HANDOFF.md
        is a plain-text file the agent can read at next startup without
        needing the database to be loaded first.

        Args:
            changes: what was changed this session (bullet list or paragraph)
            pending: what's still in progress / upcoming
            modified_files: list of files touched this session
        """
        import datetime
        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        workspace = os.path.dirname(self._db_path)
        # Walk up from db_path to find workspace root
        candidate = os.path.dirname(self._filepath)
        if os.path.isdir(candidate):
            workspace = candidate
        handoff_path = os.path.join(workspace, "HANDOFF.md")
        content = (
            f"# Session Handoff\n"
            f"# Auto-generated at session end. Read at next session start for continuity.\n\n"
            f"## Last Session: {date_str}\n\n"
            f"### What I Changed\n{changes}\n\n"
        )
        if pending:
            content += f"### What's Pending\n{pending}\n\n"
        if modified_files:
            content += f"### Modified Files\n{modified_files}\n"
        try:
            with open(handoff_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            _mem_log.warning("Failed to write HANDOFF.md: %s", e)

    def read_handoff(self) -> str | None:
        """Read HANDOFF.md from the workspace root, returning its content.

        Returns None if the file doesn't exist or can't be read.
        """
        workspace = os.path.dirname(self._filepath)
        if not os.path.isdir(workspace):
            workspace = os.path.dirname(self._db_path)
        handoff_path = os.path.join(workspace, "HANDOFF.md")
        if not os.path.isfile(handoff_path):
            return None
        try:
            with open(handoff_path, encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def write_session_handoff(
        workspace: str,
        start_head: str | None = None,
        pending: str = "",
        notes: str = "",
        plan_steps: list[str] | None = None,
        plan_done: list[int] | None = None,
    ) -> str:
        """Auto-generate and write HANDOFF.md from session state.

        Uses git diff since *start_head* (if provided) to determine what
        changed.  Falls back to ``git diff --stat`` from last commit if
        *start_head* is None.

        Returns the path written, or raises OSError on failure.
        """
        import datetime
        import subprocess as _sp

        date_str = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        # --- What changed ---
        changes_lines: list[str] = []
        modified_files: list[str] = []

        try:
            if start_head:
                # Diff since session start
                r = _sp.run(
                    ["git", "-C", workspace, "diff", "--stat", f"{start_head}.."],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    stat_out = r.stdout.strip()
                    changes_lines.append(f"```\n{stat_out}\n```")
                    # Parse modified files from stat output
                    for line in stat_out.split("\n"):
                        if "|" in line:
                            fname = line.split("|")[0].strip()
                            if fname:
                                modified_files.append(fname)

                # New commits since session start
                r2 = _sp.run(
                    ["git", "-C", workspace, "log", "--oneline", f"{start_head}.."],
                    capture_output=True, text=True, timeout=5,
                )
                if r2.returncode == 0 and r2.stdout.strip():
                    changes_lines.insert(0, f"### Commits\n```\n{r2.stdout.strip()}\n```")
            else:
                # Fallback: diff from last commit
                r = _sp.run(
                    ["git", "-C", workspace, "diff", "--stat", "HEAD~1.."],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    changes_lines.append(f"```\n{r.stdout.strip()}\n```")
        except (OSError, _sp.TimeoutExpired):
            pass

        changes_text = "\n".join(changes_lines) if changes_lines else "(no git changes detected)"
        pending_text = pending or "(none recorded)"
        files_text = "\n".join(f"- {f}" for f in modified_files) if modified_files else "(none tracked)"

        # Build plan progress section
        plan_text = ""
        if plan_steps:
            done_set = set(plan_done or [])
            plan_lines = [f"Plan ({len(done_set)}/{len(plan_steps)} complete):"]
            for i, s in enumerate(plan_steps, 1):
                mark = "✓" if (i - 1) in done_set else "○"
                plan_lines.append(f"  [{mark}] {i}. {s}")
            plan_text = "\n".join(plan_lines) + "\n"

        handoff_path = os.path.join(workspace, "HANDOFF.md")
        content = (
            f"# Session Handoff\n"
            f"# Auto-generated at session end. Read at next session start for continuity.\n\n"
            f"## Last Session: {date_str}\n\n"
            f"### What I Changed\n{changes_text}\n\n"
            f"### What's Pending\n{pending_text}\n\n"
        )
        if plan_text:
            content += f"### Plan Progress\n{plan_text}\n"
        content += f"### Modified Files\n{files_text}\n"
        if notes:
            content += f"\n### Notes\n{notes}\n"

        with open(handoff_path, "w", encoding="utf-8") as f:
            f.write(content)
        return handoff_path

    def capture_session_summary(
        self, summary: str, detail: str = "",
    ) -> None:
        """Store a session summary for next startup injection."""
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM project_knowledge WHERE category = 'session_summary'"
            )
            conn.execute(
                "INSERT INTO project_knowledge (category, summary, detail, importance)"
                " VALUES ('session_summary', ?, ?, 3)",
                (summary, detail),
            )
            conn.commit()
        except sqlite3.Error:
            warnings.warn("Failed to capture session summary", stacklevel=2)

    def get_latest_session_summary(self) -> dict | None:
        """Return the most recent session summary, or None."""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT summary, detail FROM project_knowledge"
                " WHERE category = 'session_summary'"
                " ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return {"summary": row[0], "detail": row[1]}
            return None
        except sqlite3.Error:
            warnings.warn("Failed to query session summary", stacklevel=2)
            return None

    # ------------------------------------------------------------------
    # save() helpers — split from the main method (was ~100 lines).
    # ------------------------------------------------------------------

    def _prepare_messages(self, messages: list[dict]) -> tuple[list[dict], list[dict], bool]:
        """Clean, compress, prune, and summarise messages.  Does NOT write to DB.

        Returns (kept, pruned, compressed) where:
          - *kept* is the final message list after all processing
          - *pruned* is the list of messages that were removed (may be empty)
          - *compressed* is True if any tool results were compressed in-place
        """
        _clear_message_caches()
        cleaned = _clean_messages(messages)
        cleaned, compressed = _compress_tool_results(cleaned, keep_recent=_COMPRESSION_KEEP_RECENT)

        # Incremental token accounting: only count new messages since last save.
        new_start = min(self._last_saved_count, len(cleaned))
        new_tokens = sum(_estimate_tokens(m) for m in cleaned[new_start:])
        self._token_count += new_tokens

        # Decide whether to prune: always prune when over the hard message-count
        # cap, or when token budget is significantly exceeded and cooldown expired.
        overage_ratio = (self._token_count / self._max_tokens) if self._max_tokens > 0 else 0.0
        over_message_cap = len(cleaned) > self._max_messages
        should_prune = over_message_cap or (
            self._prune_cooldown <= 0
            and overage_ratio > _PRUNE_OVERAGE_BUFFER
        )

        if should_prune:
            kept, pruned = _prune_by_tokens(cleaned, self._max_tokens, self._max_messages)
            if pruned:
                self._token_count -= sum(_estimate_tokens(m) for m in pruned)
                summary = _summarize_pruned(pruned)
                if summary:
                    summary_msg = {"role": "user", "content": summary}
                    kept.insert(0, summary_msg)
                    self._token_count += _estimate_tokens(summary_msg)
            self._prune_cooldown = _PRUNE_COOLDOWN
        else:
            pruned: list[dict] = []
            kept = cleaned
            if self._prune_cooldown > 0:
                self._prune_cooldown -= 1

        return kept, pruned, compressed

    def _write_messages(
        self, kept: list[dict], pruned: list[dict], compressed: bool,
    ) -> list[dict]:
        """Write *kept* to SQLite with retry logic.  Returns *kept* on success,
        or *messages* (untouched input) on failure so the caller never loses data.
        """
        self._ensure_parent()
        last_exc = None
        for attempt in range(_SAVE_MAX_RETRIES):
            try:
                conn = self._get_conn()
                conn.execute("BEGIN IMMEDIATE")
                need_full_rewrite = (
                    bool(pruned) or compressed or len(kept) < self._last_saved_count
                )
                if need_full_rewrite:
                    conn.execute(_DELETE)
                    conn.executemany(
                        _INSERT,
                        [(m["role"], json.dumps(m)) for m in kept],
                    )
                else:
                    new_msgs = kept[self._last_saved_count:]
                    if new_msgs:
                        conn.executemany(
                            _INSERT,
                            [(m["role"], json.dumps(m)) for m in new_msgs],
                        )
                conn.commit()
                self._save_count += 1
                self._maybe_vacuum(conn, after_full_rewrite=need_full_rewrite)
                self._last_saved_count = len(kept)
                return kept
            except sqlite3.Error as exc:
                _on_sqlite_error("save")
                last_exc = exc
                try:
                    conn.rollback()
                except (sqlite3.Error, AttributeError):
                    pass
                if attempt < _SAVE_MAX_RETRIES - 1:
                    import time
                    time.sleep(_SAVE_RETRY_DELAY * (attempt + 1))
                    if self._conn is not None:
                        try:
                            self._conn.close()
                        except sqlite3.Error:
                            pass
                        self._conn = None

        import sys
        print(f"Warning: memory save failed: {last_exc}", file=sys.stderr)
        return kept  # best-effort: return processed messages so caller can retry

    def _maybe_vacuum(self, conn: sqlite3.Connection, *, after_full_rewrite: bool) -> None:
        """Trigger background VACUUM when freelist exceeds threshold or periodically."""
        should_check = after_full_rewrite or (self._save_count % _VACUUM_INTERVAL == 0)
        if not should_check:
            return
        try:
            row = conn.execute("PRAGMA freelist_count").fetchone()
            if row and row[0] > _VACUUM_FREELIST_THRESHOLD:
                self._start_background_vacuum()
        except sqlite3.Error:
            pass  # VACUUM is opportunistic; ignore failures

    def save(self, messages: list[dict]) -> list[dict]:
        """Persist *messages* to the database.

        1. Strip system messages and incomplete tool-call sequences.
        2. Compress old tool results (content-aware, per-tool-type).
        3. Prune by token budget, preserving turn boundaries.
        4. Summarise pruned messages into a context note.
        5. Write atomically to SQLite (incremental or full rewrite).

        Returns *kept*, the processed list of messages that was written.
        Callers should replace their in-memory list with this return value
        to keep their working set in sync with the compacted/pruned store.
        """
        kept, pruned, compressed = self._prepare_messages(messages)
        return self._write_messages(kept, pruned, compressed)

    # -----------------------------------------------------------------------
    # Mid-session pruning (triggers at 70% token capacity)
    # -----------------------------------------------------------------------

    def force_prune(self, messages: list[dict]) -> list[dict]:
        """Proactively prune when approaching the token budget.

        Checks if current token count exceeds 70% of *max_tokens* and, if so,
        trims oldest turns, compresses tool results, and injects a summary.
        Returns the (possibly reduced) message list.

        Unlike ``save()``, this does NOT write to the SQLite database — it's
        purely an in-memory compaction.  Persistence is handled by ``save()``.
        """
        trigger = int(self._max_tokens * 0.70)
        current = sum(_estimate_tokens(m) for m in messages)
        if current <= trigger:
            return messages  # under threshold, nothing to do

        # Prune to 60% capacity to avoid churn
        target = int(self._max_tokens * 0.60)
        budget_messages = max(self._max_messages // 2, 25)

        # Temporarily swap max_tokens for this one-shot prune
        saved_tokens = self._max_tokens
        saved_messages = self._max_messages
        self._max_tokens = target
        self._max_messages = budget_messages

        try:
            cleaned = _clean_messages(messages)
            cleaned, _ = _compress_tool_results(cleaned, keep_recent=_COMPRESSION_KEEP_RECENT)
            kept, pruned = _prune_by_tokens(cleaned, target, budget_messages)

            if pruned:
                summary = _summarize_pruned(pruned)
                if summary:
                    summary_msg = {"role": "user", "content": summary}
                    kept.insert(0, summary_msg)

            # Re-inject persisted project knowledge so key facts survive pruning
            knowledge = self.get_top_knowledge(limit=10)
            if knowledge:
                facts = "\n".join(
                    f"- [{k['category']}] {k['summary']}"
                    for k in knowledge
                )
                kept.insert(0, {
                    "role": "user",
                    "content": f"[Project learnings from past sessions:\n{facts}]",
                })

            self._token_count = sum(_estimate_tokens(m) for m in kept)
            self._last_saved_count = len(kept)
            return kept
        finally:
            self._max_tokens = saved_tokens
            self._max_messages = saved_messages

    def clear(self) -> None:
        """Remove all messages and reclaim disk space."""
        self._last_saved_count = 0
        self._token_count = 0
        try:
            conn = self._get_conn()
            conn.execute(_DELETE)
            conn.execute("DELETE FROM scratchpad")
            conn.execute("DELETE FROM test_output")
            conn.commit()
            conn.execute(_VACUUM)
        except (sqlite3.Error, OSError):
            try:
                os.remove(self._db_path)
            except FileNotFoundError:
                pass

    def get_scratchpad(self) -> str:
        """Return the current scratchpad content (empty string if none)."""
        try:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "content TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO scratchpad (id, content) VALUES (1, '')")
            row = conn.execute(
                "SELECT content FROM scratchpad WHERE id = 1"
            ).fetchone()
            return row[0] if row else ""
        except sqlite3.Error:
            warnings.warn("Failed to query scratchpad or test output", stacklevel=2)
            return ""

    def set_scratchpad(self, content: str) -> None:
        """Update the scratchpad content."""
        try:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "content TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO scratchpad (id, content) VALUES (1, '')")
            conn.execute(
                "INSERT OR REPLACE INTO scratchpad (id, content) VALUES (1, ?)",
                (content,),
            )
            conn.commit()
        except sqlite3.Error as exc:
            import sys
            print(f"Warning: scratchpad write failed: {exc}", file=sys.stderr)

    def get_plan(self) -> tuple[list[str], list[int]]:
        """Return (steps, done_indices) from persisted plan state.  Returns ([], []) if none."""
        try:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plan_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "steps_json TEXT NOT NULL DEFAULT '[]',"
                "done_json TEXT NOT NULL DEFAULT '[]'"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO plan_state (id, steps_json, done_json) VALUES (1, '[]', '[]')")
            row = conn.execute(
                "SELECT steps_json, done_json FROM plan_state WHERE id = 1"
            ).fetchone()
            if row:
                steps = json.loads(row[0]) if row[0] else []
                done = json.loads(row[1]) if row[1] else []
                return steps, done
            return [], []
        except (sqlite3.Error, json.JSONDecodeError):
            return [], []

    def set_plan(self, steps: list[str], done_indices: list[int]) -> None:
        """Persist plan steps and completed indices to SQLite."""
        try:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plan_state ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "steps_json TEXT NOT NULL DEFAULT '[]',"
                "done_json TEXT NOT NULL DEFAULT '[]'"
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO plan_state (id, steps_json, done_json) VALUES (1, '[]', '[]')")
            conn.execute(
                "INSERT OR REPLACE INTO plan_state (id, steps_json, done_json) VALUES (1, ?, ?)",
                (json.dumps(steps), json.dumps(sorted(done_indices))),
            )
            conn.commit()
        except sqlite3.Error as exc:
            import sys
            print(f"Warning: plan state write failed: {exc}", file=sys.stderr)

    def get_test_output(self) -> str:
        """Return the last saved test output (empty string if none)."""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT output FROM test_output WHERE id = 1"
            ).fetchone()
            return row[0] if row else ""
        except sqlite3.Error:
            warnings.warn("Failed to query scratchpad or test output", stacklevel=2)
            return ""

    def save_test_output(self, output: str) -> None:
        """Save the latest test run output."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO test_output (id, output) VALUES (1, ?)",
                (output,),
            )
            conn.commit()
        except sqlite3.Error:
            warnings.warn("Failed to save test output", stacklevel=2)
            pass  # fail gracefully

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        """Create parent directories of the database file if needed."""
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _ensure_table(self) -> None:
        """Create the messages table if it doesn't exist."""
        self._ensure_parent()
        try:
            conn = self._get_conn()
            conn.execute(_CREATE_TABLE)
        except sqlite3.Error:
            warnings.warn("Failed to create table schema", stacklevel=2)
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Filesystem type constants for remote-filesystem detection.
# Network filesystems that don't support POSIX locks or WAL shared memory.
_REMOTE_FS_TYPES: frozenset[int] = frozenset({
    0x517B,   # SMB / CIFS
    0x6969,   # NFS
    0x01021997,  # AFP (Apple Filing Protocol)
    0x2FC12FC1,  # AFS (Andrew File System)
})


def _is_remote_fs(path: str) -> bool:
    """Detect whether *path* lives on a network/remote filesystem.

    SQLite WAL journal mode and POSIX file locking are unreliable on
    network filesystems (SMB, NFS, AFP).  Returns True for remote paths.
    """
    # Quick check: macOS network mounts are under /Volumes/ (but not the
    # root volume).  '/Volumes/Macintosh HD' is local; anything else is
    # typically a network mount or external drive.
    if path.startswith("/Volumes/") and path != "/Volumes/Macintosh HD" and not path.startswith("/Volumes/Macintosh HD/"):
        return True
    # UNC paths (SMB): //server/share/...
    if path.startswith("//"):
        return True
    # Stat the mount point and check filesystem type magic numbers
    try:
        st = os.statvfs(path)
        if st.f_fsid in _REMOTE_FS_TYPES:
            return True
    except OSError:
        pass
    return False


def _local_db_path(db_path: str) -> str:
    """Derive a local fallback path for SQLite when *db_path* is on a remote FS.

    Uses ~/.mini_agent/memory/<sha256_of_original_path>.db so that
    different workspaces get isolated databases.
    """
    path_hash = hashlib.sha256(db_path.encode()).hexdigest()[:16]
    local_dir = os.path.join(os.path.expanduser("~"), ".mini_agent", "memory")
    os.makedirs(local_dir, exist_ok=True)
    return os.path.join(local_dir, f"{path_hash}.db")


def _db_path(filepath: str) -> str:
    """Derive the SQLite database path from the configured filepath."""
    if filepath.endswith(".db"):
        return filepath
    base, _ = os.path.splitext(filepath)
    return base + ".db"


def _row_to_msg(row: tuple[str, str]) -> dict:
    """Decode a database row into a message dict."""
    try:
        return json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return {"role": row[0], "content": ""}


def _clean_messages(messages: list[dict]) -> list[dict]:
    """Strip system messages, orphaned tool results, and incomplete tool-call sequences.

    Uses the canonical ``_strip_orphaned_tool_messages`` from
    ``memory_prune.py`` with ``truncate=True`` for persistence: incomplete
    tool-call sequences are truncated (everything from that point onward
    is dropped), rather than just removing individual orphaned messages.
    """
    from memory.memory_prune import _strip_orphaned_tool_messages

    # ---- strip system messages and transient messages ----
    cleaned: list[dict] = [
        m for m in messages
        if m.get("role") != "system" and not m.get("_transient")
    ]

    # ---- strip orphaned tool messages (canonical implementation from memory_prune) ----
    return _strip_orphaned_tool_messages(cleaned, truncate=True)


def _migrate_old_paths(new_filepath: str, db_path: str) -> None:
    """Migrate from old naming schemes to the current db_path.

    Old scheme: config said .json, _db_path appended .db → .json.db
    New scheme: config says .db, _db_path uses it directly → .db

    Also migrates raw JSON files if present.
    """
    if os.path.exists(db_path):
        return  # already migrated

    # Old path: if config was .json, old db was .json.db
    base, ext = os.path.splitext(new_filepath)
    if ext != ".db":
        old_db = base + ".db"
        if os.path.isfile(old_db):
            try:
                os.rename(old_db, db_path)
                return
            except OSError:
                pass  # fall through — will start fresh

    # Old JSON file — migrate its contents
    if os.path.isfile(new_filepath):
        _migrate_json(new_filepath, db_path)


def _migrate_json(json_path: str, db_path: str) -> None:
    """Migrate an existing JSON memory file to SQLite."""
    try:
        with open(json_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(data, list):
        return

    cleaned = _clean_messages(data)
    if not cleaned:
        return

    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                _INSERT,
                [(m["role"], json.dumps(m)) for m in cleaned],
            )
            conn.commit()
    except sqlite3.Error:
        warnings.warn("Failed to migrate JSON cache", stacklevel=2)
        return


# ---------------------------------------------------------------------------
# Shared export helper — used by both terminal REPL and TUI
# ---------------------------------------------------------------------------

def export_conversation_markdown(messages: list[dict]) -> str:
    """Generate markdown text for a conversation export.

    Returns the complete markdown string. Callers handle path/timestamp logic.
    """
    blocks: list[str] = []
    blocks.append("# mini_agent conversation\n")
    for m in messages:
        role = m.get("role", "?")
        if role == "system":
            blocks.append(f"### System\n\n{m.get('content', '')}\n")
        elif role == "user":
            blocks.append(f"### User\n\n{m.get('content', '')}\n")
        elif role == "assistant":
            content = m.get("content", "")
            if m.get("reasoning_content"):
                blocks.append("> **Thinking**\n>")
                for line in m["reasoning_content"].split("\n"):
                    blocks.append(f"> {line}")
                blocks.append("")
            if content:
                blocks.append(f"### Assistant\n\n{content}\n")
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = fn.get("arguments", "{}")
                    blocks.append(f"```\n{name}({args})\n```\n")
        elif role == "tool":
            blocks.append(f"> Tool result:\n>\n> {m.get('content', '')[:_MARKDOWN_TOOL_RESULT_PREVIEW]}\n")
    return "\n".join(blocks)
