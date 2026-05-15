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

import json
import os
import sqlite3
import warnings
from typing import Optional


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,   -- JSON blob of the full message dict
    created_at TEXT    DEFAULT (datetime('now'))
)
"""

_INSERT  = "INSERT INTO messages (role, content) VALUES (?, ?)"
_SELECT  = "SELECT role, content FROM messages ORDER BY id ASC"
_DELETE  = "DELETE FROM messages"
_VACUUM  = "VACUUM"


# ---------------------------------------------------------------------------
# Named constants (extracted from magic numbers)
# ---------------------------------------------------------------------------

# Token estimation
_CHARS_PER_TOKEN = 4                 # heuristic: ~4 characters per token
_MIN_TOKEN_ESTIMATE = 1              # floor for token count estimates

# Tool result compression
_COMPRESSION_KEEP_RECENT = 6         # messages at the tail left uncompressed
_COMPRESSION_MAX_LINES = 5           # lines before a tool result is compressed
_COMPRESSION_MAX_FIRST_LINE = 500    # max length of the first line kept

# Conversation summarization
_SUMMARY_PREVIEW_LENGTH = 120        # character limit for content previews
_SUMMARY_PATH_PREVIEW = 80           # character limit for path / command previews
_SUMMARY_MAX_TURNS = 3               # max recent user turns shown
_SUMMARY_MAX_FILES = 5               # max files listed per category
_SUMMARY_MAX_COMMANDS = 3            # max commands listed

# Context budget injection
# Markdown export
_MARKDOWN_TOOL_RESULT_PREVIEW = 500  # char limit for tool results in export


# ---------------------------------------------------------------------------
# Per-save message caches — avoid re-parsing JSON and re-estimating tokens
# for the same message within a single save() call.
# ---------------------------------------------------------------------------

_TOOL_PARSE_CACHE: dict[int, str] = {}   # id(msg) -> extracted text content
_TOKEN_EST_CACHE: dict[int, int] = {}     # id(msg) -> estimated token count


def _clear_message_caches() -> None:
    """Clear per-save caches. Called at the start of MemoryStore.save()."""
    _TOOL_PARSE_CACHE.clear()
    _TOKEN_EST_CACHE.clear()


def _get_tool_content(msg: dict) -> str:
    """Extract the text content from a tool-result message.

    Caches by message identity (id(msg)) so the same message's JSON
    is parsed at most once per save().
    """
    mid = id(msg)
    try:
        return _TOOL_PARSE_CACHE[mid]
    except KeyError:
        pass
    try:
        data = json.loads(msg["content"])
        text = data.get("content", "")
    except (json.JSONDecodeError, TypeError):
        text = msg.get("content", "")
    _TOOL_PARSE_CACHE[mid] = text
    return text


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(msg: dict) -> int:
    """Rough token estimate for a single message.

    Heuristic: ~4 characters per token (works well for English/code).
    For tool results (JSON content), we parse and estimate just the
    content field — the JSON wrapper overhead is negligible.

    Caches results by message identity so repeated calls on the same
    message within a single save() are O(1) after the first call.
    """
    mid = id(msg)
    try:
        return _TOKEN_EST_CACHE[mid]
    except KeyError:
        pass

    if msg.get("role") == "tool":
        text = _get_tool_content(msg)
    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
        # Tool-call messages: count the arguments text
        total = len(msg.get("content", ""))
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            total += len(json.dumps(fn.get("arguments", "")))
        result = max(_MIN_TOKEN_ESTIMATE, total // _CHARS_PER_TOKEN)
        _TOKEN_EST_CACHE[mid] = result
        return result
    else:
        text = msg.get("content", "") or json.dumps(msg)

    result = max(_MIN_TOKEN_ESTIMATE, len(text) // _CHARS_PER_TOKEN)
    _TOKEN_EST_CACHE[mid] = result
    return result


def _total_tokens(messages: list[dict]) -> int:
    """Sum estimated tokens across all messages.

    Uses a running accumulator keyed by list length.  When messages are
    only appended (the common case), only new messages are counted.
    When the list shrinks (pruning), a full recount is done.
    This avoids the O(n²) behaviour of recounting every message on
    every turn as the conversation grows.
    """
    global _ACCUM_COUNT, _ACCUM_TOTAL
    n = len(messages)
    if n >= _ACCUM_COUNT:
        # Only count new messages appended since last call
        new_tokens = sum(_estimate_tokens(m) for m in messages[_ACCUM_COUNT:])
        _ACCUM_TOTAL += new_tokens
        _ACCUM_COUNT = n
    else:
        # List shrank (pruned) — full recount
        _ACCUM_TOTAL = sum(_estimate_tokens(m) for m in messages)
        _ACCUM_COUNT = n
    return _ACCUM_TOTAL



# Running accumulator for _total_tokens (length-based, not identity-based)
_ACCUM_COUNT: int = 0
_ACCUM_TOTAL: int = 0


# ---------------------------------------------------------------------------
# Tool result compression
# ---------------------------------------------------------------------------

def _find_tool_call_name(messages: list[dict], tool_idx: int) -> str | None:
    """Find the tool function name for a tool-result message at *tool_idx*.

    Walks backward from *tool_idx* to find the preceding assistant message
    whose tool_calls include the matching tool_call_id.
    """
    tool_call_id = messages[tool_idx].get("tool_call_id")
    if not tool_call_id:
        return None
    for j in range(tool_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in prev.get("tool_calls", []):
            if tc.get("id") == tool_call_id:
                fn = tc.get("function", {})
                return fn.get("name", "").strip()
    return None


def _find_tool_call_args(messages: list[dict], tool_idx: int) -> dict:
    """Find the tool call arguments for a tool-result message at *tool_idx*."""
    tool_call_id = messages[tool_idx].get("tool_call_id")
    if not tool_call_id:
        return {}
    for j in range(tool_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in prev.get("tool_calls", []):
            if tc.get("id") == tool_call_id:
                fn = tc.get("function", {})
                raw = fn.get("arguments", "{}")
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return {}
    return {}


def _compress_tool_results(
    messages: list[dict],
    keep_recent: int = _COMPRESSION_KEEP_RECENT,
) -> tuple[list[dict], bool]:
    """Shorten old tool results with content-aware compression.

    Tool results within the last *keep_recent* messages are left intact.
    Older ones are trimmed based on their tool type:

    - **read_file**: keep lines around the offset/limit range requested.
    - **search_files**: keep lines with actual matches (``file:line:`` pattern).
    - **run_shell**: keep the last 20 lines (exit code + tail).
    - **default**: keep the first 5 lines + truncation marker.

    Returns (messages, changed) — *changed* is True if at least one
    message was compressed in-place.
    """
    changed = False
    if len(messages) <= keep_recent:
        return messages, changed

    # Messages that are "recent" (within the tail window) stay untouched
    cutoff = len(messages) - keep_recent
    for i, m in enumerate(messages):
        if i >= cutoff:
            break
        if m.get("role") != "tool":
            continue
        text = _get_tool_content(m)
        if not text:
            continue

        # Re-parse to get the mutable dict for modification
        try:
            data = json.loads(m["content"])
        except (json.JSONDecodeError, TypeError):
            continue

        lines = text.split("\n")

        # Detect tool type from the preceding assistant message
        tool_name = _find_tool_call_name(messages, i) or ""

        if tool_name == "read_file":
            kept = _compress_read_file(lines, messages, i)
        elif tool_name == "search_files":
            kept = _compress_search_files(lines)
        elif tool_name == "run_shell":
            kept = _compress_run_shell(lines)
        else:
            kept = _compress_default(lines)

        # Skip if nothing changed (e.g. already short enough)
        if kept == text:
            continue

        data["content"] = kept
        m["content"] = json.dumps(data)
        # Invalidate cache for this message since content changed
        _TOOL_PARSE_CACHE.pop(id(m), None)
        _TOKEN_EST_CACHE.pop(id(m), None)
        changed = True

    return messages, changed


def _compress_read_file(lines: list[str], messages: list[dict], tool_idx: int) -> str:
    """Keep lines around the requested offset/limit range for read_file results."""
    if len(lines) <= _COMPRESSION_MAX_LINES:
        return "\n".join(lines)

    args = _find_tool_call_args(messages, tool_idx)
    offset = args.get("offset", 0) if isinstance(args.get("offset"), int) else 0
    limit = args.get("limit", 300) if isinstance(args.get("limit"), int) else 300

    # read_file results have line numbers like "42: content" — find the
    # range of lines that fall within [offset, offset + limit).
    request_start = offset
    request_end = offset + limit

    kept_indices: set[int] = set()
    for idx, line in enumerate(lines):
        try:
            col_sep = line.index(":")
            line_num_str = line[:col_sep].strip()
            if line_num_str.isdigit():
                line_no = int(line_num_str)
                # Line numbers are 1-based, offset is 0-based: line 1 == offset 0
                if request_start <= line_no - 1 < request_end:
                    kept_indices.add(idx)
        except (ValueError, IndexError):
            pass

    return _build_compressed(lines, kept_indices, tag="lines around offset")


def _compress_search_files(lines: list[str]) -> str:
    """Keep only lines with actual matches (``file:line:`` pattern) in search_files results."""
    if len(lines) <= _COMPRESSION_MAX_LINES:
        return "\n".join(lines)

    kept_indices: set[int] = set()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Search results look like: path/to/file:line_num: content
        # We detect by checking if the line starts with a path containing ":" then ":"
        if _is_match_line(stripped):
            kept_indices.add(idx)

    if not kept_indices:
        # No match lines found — keep first 5 as default fallback
        return _compress_default(lines)

    return _build_compressed(lines, kept_indices, tag="matching lines")


def _is_match_line(line: str) -> bool:
    """Check if a line looks like a search_files match: ``path:line: content``."""
    # Pattern: starts with a path-like prefix containing '/' or '.py',
    # followed by ':digits:' and then content.
    try:
        first_colon = line.index(":")
        prefix = line[:first_colon]
        if "/" not in prefix and "\\" not in prefix and "." not in prefix:
            return False
        rest = line[first_colon + 1:]
        second_colon = rest.index(":")
        between = rest[:second_colon].strip()
        return between.isdigit() and len(between) > 0
    except ValueError:
        return False


def _compress_run_shell(lines: list[str]) -> str:
    """Keep the last 20 lines for run_shell output (exit code + tail summary)."""
    if len(lines) <= 20:
        return "\n".join(lines)

    kept = lines[-20:]
    result = "\n".join(kept)
    if len(result) > _COMPRESSION_MAX_FIRST_LINE:
        result = result[:_COMPRESSION_MAX_FIRST_LINE] + "…"
    return f"… (truncated, last {len(kept)} of {len(lines)} lines)\n{result}"


def _compress_default(lines: list[str]) -> str:
    """Default: keep the first 5 lines + truncation marker."""
    if len(lines) <= _COMPRESSION_MAX_LINES:
        return "\n".join(lines)

    kept = "\n".join(lines[:_COMPRESSION_MAX_LINES])
    if len(kept) > _COMPRESSION_MAX_FIRST_LINE:
        kept = kept[:_COMPRESSION_MAX_FIRST_LINE] + "…"

    return kept + f"\n… (truncated at {_COMPRESSION_MAX_LINES} lines — {len(lines)} total)"


def _build_compressed(
    lines: list[str],
    kept_indices: set[int],
    tag: str = "lines",
) -> str:
    """Build compressed output from selected line indices, with gaps marked."""
    if not kept_indices or kept_indices == set(range(len(lines))):
        # Nothing to compress or keeping everything
        return "\n".join(lines)

    parts: list[str] = []
    sorted_indices = sorted(kept_indices)
    last_kept = -2

    for idx in sorted_indices:
        if idx > last_kept + 1:
            parts.append(f"… ({idx - last_kept - 1} lines skipped) …")
        parts.append(lines[idx])
        last_kept = idx

    if last_kept < len(lines) - 1:
        parts.append(f"… ({len(lines) - last_kept - 1} lines skipped — {len(lines)} total {tag})")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Conversation summarization
# ---------------------------------------------------------------------------

# TODO: _summarize_pruned is ~80 lines — consider splitting into helpers for
#       each message role (user, tool, assistant) and file/command categorization.
def _summarize_pruned(pruned: list[dict]) -> str:
    """Build a one-paragraph summary of pruned messages.

    The summary is injected as a synthetic 'user' message so the agent
    sees it as prior conversation context.
    """
    if not pruned:
        return ""

    files_read: list[str] = []
    files_written: list[str] = []
    files_edited: list[str] = []
    commands_run: list[str] = []
    turns: list[str] = []

    for m in pruned:
        role = m.get("role", "")
        if role == "user":
            content = m.get("content", "")
            preview = content[:120].replace("\n", " ")
            if len(content) > _SUMMARY_PREVIEW_LENGTH:
                preview += "…"
            turns.append(f"User: {preview}")

        elif role == "tool":
            text = _get_tool_content(m)

            if "bytes to" in text or "OK: wrote" in text or "OK: replaced" in text:
                # Extract path
                path = text.split(" to ")[-1].split("\n")[0] if " to " in text else text
                if len(path) > _SUMMARY_PATH_PREVIEW:
                    path = path[:_SUMMARY_PATH_PREVIEW] + "…"
                if "replaced" in text:
                    files_edited.append(path)
                else:
                    files_written.append(path)

        elif role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                if name == "read_file":
                    p = args.get("path", "?")
                    if p not in files_read:
                        files_read.append(p)
                elif name == "run_shell":
                    cmd = args.get("command", "?")
                    preview = cmd[:_SUMMARY_PATH_PREVIEW]
                    if len(cmd) > _SUMMARY_PATH_PREVIEW:
                        preview += "…"
                    commands_run.append(preview)
                elif name == "web_search":
                    q = args.get("query", "?")
                    turns.append(f"Searched web: {q[:_SUMMARY_PATH_PREVIEW]}")

    parts: list[str] = ["Earlier in this conversation:"]
    if turns:
        for t in turns[-_SUMMARY_MAX_TURNS:]:  # last N user messages
            parts.append(f"- {t}")
    if files_read:
        unique = list(dict.fromkeys(files_read))  # dedupe, preserve order
        parts.append(f"- Files read: {', '.join(unique[:_SUMMARY_MAX_FILES])}")
    if files_written:
        unique = list(dict.fromkeys(files_written))
        parts.append(f"- Files written: {', '.join(unique[:_SUMMARY_MAX_FILES])}")
    if files_edited:
        unique = list(dict.fromkeys(files_edited))
        parts.append(f"- Files edited: {', '.join(unique[:_SUMMARY_MAX_FILES])}")
    if commands_run:
        parts.append(f"- Commands run: {', '.join(commands_run[:_SUMMARY_MAX_COMMANDS])}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Token-aware pruning
# ---------------------------------------------------------------------------

# TODO: _prune_by_tokens is ~50 lines — consider splitting message-count cap
#       and token-budget pruning into separate helpers.
def _prune_by_tokens(
    messages: list[dict],
    max_tokens: int,
    max_messages: int,
) -> tuple[list[dict], list[dict]]:
    """Trim *messages* from the front to stay within budget.

    Returns (kept_messages, pruned_messages).  Pruning preserves turn
    boundaries: cuts only at ``user`` message boundaries, so tool-call
    sequences are never split.  *max_messages* is a hard cap applied
    first, then *max_tokens* is the soft budget.
    """
    if not messages:
        return [], []

    # 1. Hard cap by message count
    if len(messages) > max_messages:
        excess = len(messages) - max_messages
        cut = excess
        for i in range(excess, len(messages)):
            if messages[i].get("role") == "user":
                cut = i
                break
        else:
            cut = excess
        pruned = messages[:cut]
        messages = messages[cut:]
    else:
        pruned = []

    # 2. Token budget — trim oldest turns until under limit.
    #    Precompute per-message token estimates and subtract incrementally
    #    instead of re-scanning all messages on every iteration.
    token_counts = [_estimate_tokens(m) for m in messages]
    total = sum(token_counts)
    start = 0
    while total > max_tokens and start < len(messages) - 1:
        # Find first user message boundary from current start
        cut = start
        for i in range(start + 1, len(messages)):
            if messages[i].get("role") == "user":
                cut = i
                break
        if cut == start:
            break  # no user message found — stop, can't safely prune further
        total -= sum(token_counts[start:cut])
        pruned.extend(messages[start:cut])
        start = cut

    if start > 0:
        messages = messages[start:]

    return messages, pruned


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

    DEFAULT_MAX_MESSAGES = 500
    DEFAULT_MAX_TOKENS   = 800_000

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
            conn.commit()
        except sqlite3.Error:
            warnings.warn("Failed to initialize test_output table", stacklevel=2)
            pass  # will retry on next operation

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
        """
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

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

    # TODO: MemoryStore.save is ~50 lines — consider splitting compression,
    #       pruning, summarization, and SQL writes into separate helpers.
    def save(self, messages: list[dict]) -> None:
        """Persist *messages* to the database.

        1. Strip system messages and incomplete tool-call sequences.
        2. Compress old tool results (first-line only).
        3. Prune by token budget, preserving turn boundaries.
        4. Summarize pruned messages into a context note.
        5. Write atomically to SQLite.
        """
        _clear_message_caches()
        cleaned = _clean_messages(messages)
        cleaned, compressed = _compress_tool_results(cleaned, keep_recent=6)

        # Incremental token accounting: only count new messages since
        # last save.  When compression or pruning occurs, adjust below.
        new_start = min(self._last_saved_count, len(cleaned))
        new_tokens = sum(_estimate_tokens(m) for m in cleaned[new_start:])
        self._token_count += new_tokens

        kept, pruned = _prune_by_tokens(
            cleaned, self._max_tokens, self._max_messages,
        )

        # Subtract tokens for pruned messages
        if pruned:
            self._token_count -= sum(_estimate_tokens(m) for m in pruned)

        # Inject summary of pruned context
        if pruned:
            summary = _summarize_pruned(pruned)
            if summary:
                summary_msg = {"role": "user", "content": summary}
                kept.insert(0, summary_msg)
                self._token_count += _estimate_tokens(summary_msg)

        self._ensure_parent()
        try:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            # Incremental save: if no pruning happened and we only
            # appended messages, INSERT just the new rows instead of
            # rewriting everything.
            need_full_rewrite = (bool(pruned) or compressed
                                 or len(kept) < self._last_saved_count)
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
            self._last_saved_count = len(kept)
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except (sqlite3.Error, AttributeError):
                pass
            import sys
            print(f"Warning: memory save failed: {exc}", file=sys.stderr)

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

    Two-pass validation:

    1. **Backward pass** — remove ``tool`` messages whose ``tool_call_id``
       has no *preceding* assistant message with a matching ``tool_calls``
       entry.  This catches the "tool result before assistant" ordering bug
       that causes API 400 errors.

    2. **Forward pass** — truncate at any assistant message whose
       ``tool_calls`` have no matching ``tool`` results *after* it.  This
       catches incomplete / dangling tool-call sequences.
    """
    # ---- backward pass: remove orphaned tool results ----
    valid_ids: set[str] = set()  # tool_call_ids seen so far from assistants
    pass1: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        if m.get("_transient"):
            continue  # scratchpad, progress, circuit breaker — never persist
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid and tcid not in valid_ids:
                continue  # orphaned — no preceding assistant owns this id
        pass1.append(m)
        # Accumulate valid ids from this message (only assistant with tool_calls)
        for tc in m.get("tool_calls", []):
            tcid = tc.get("id", "")
            if tcid:
                valid_ids.add(tcid)

    # ---- forward pass (single reverse scan): truncate incomplete tool-call sequences ----
    # Scan backward collecting tool_call_ids from tool messages.
    # When we hit an assistant with tool_calls, its ids must all be in the
    # set (meaning matching tool results exist *after* it in forward order).
    # The first incomplete assistant we find going backward is the truncation point.
    seen_tool_ids: set[str] = set()
    truncate_at = len(pass1)
    for i in range(len(pass1) - 1, -1, -1):
        m = pass1[i]
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid:
                seen_tool_ids.add(tcid)
        else:
            tool_ids = {tc["id"] for tc in m.get("tool_calls", [])}
            if tool_ids and not tool_ids.issubset(seen_tool_ids):
                truncate_at = i
    return pass1[:truncate_at]


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
        with open(json_path, "r") as f:
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
