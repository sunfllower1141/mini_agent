#!/usr/bin/env python3
"""
memory_core.py -- Hermes-style bounded persistent core memory tool.

The agent uses this to manage its core memory -- a bounded (~2,500 char)
snapshot injected frozen at session start.  Changes persist immediately
but appear in the system prompt NEXT session.
"""

from __future__ import annotations

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT


@_register("memory_core")
def _memory_core(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Manage persistent core memory (frozen snapshot injected at session start).

    Actions:
        add     -- append a line/entry to core memory
        replace -- replace the entire core memory content (when you need to
                  restructure or consolidate). Use this after reading the
                  current snapshot if you need to merge, dedup, or compress.
        remove  -- remove an entry by line number (1-indexed). The agent should
                  read the current snapshot first to identify the line number.
        read    -- read the current core memory content

    Core memory is hard-capped at ~2,500 chars. When full, **consolidate**
    (merge similar entries, remove stale ones) before adding more.
    Changes persist immediately but appear in the system prompt NEXT session.
    """

    action = args.get("action", "read")
    content = args.get("content", "")
    line_number = args.get("line", 0)

    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    if memory_store is None:
        # Fallback: try SQLite directly via scratchpad_path
        db_path = getattr(_TOOL_CONTEXT, "scratchpad_path", None)
        if db_path:
            return _memory_core_fallback(db_path, action, content, line_number)
        return ToolResult(
            success=False,
            content="No persistent storage available for core memory.",
        )

    # Happy path: use MemoryStore
    try:
        current = memory_store.get_core_memory()
        info = memory_store.get_core_memory_info()
        char_limit = info["char_limit"]

        if action == "read":
            if current:
                bar_len = 50
                filled = min(len(current) * bar_len // max(char_limit, 1), bar_len)
                bar = "#" * filled + "." * (bar_len - filled)
                pct = min(len(current) * 100 // max(char_limit, 1), 100)
                return ToolResult(
                    success=True,
                    content=(
                        f"Core memory ({len(current)}/{char_limit} chars):\n\n"
                        f"{current}\n\n{bar} {pct}% full"
                    ),
                )
            return ToolResult(success=True, content="Core memory: (empty)")

        elif action == "add":
            new_content = (
                (current + "\n" + content).strip() if current else content
            )
            result = memory_store.write_core_memory(new_content)
            if result["ok"]:
                return ToolResult(success=True, content=result["message"])
            return ToolResult(success=False, content=result["message"])

        elif action == "replace":
            result = memory_store.write_core_memory(content)
            if result["ok"]:
                return ToolResult(success=True, content=result["message"])
            return ToolResult(success=False, content=result["message"])

        elif action == "remove":
            if not current:
                return ToolResult(
                    success=False,
                    content="Core memory is empty. Nothing to remove.",
                )
            lines = current.split("\n")
            if line_number < 1 or line_number > len(lines):
                return ToolResult(
                    success=False,
                    content=(
                        f"Line {line_number} out of range. "
                        f"Lines: 1-{len(lines)}."
                    ),
                )
            removed = lines.pop(line_number - 1)
            new_content = "\n".join(lines).strip()
            result = memory_store.write_core_memory(new_content)
            if result["ok"]:
                return ToolResult(
                    success=True,
                    content=(
                        f"Removed line {line_number}: "
                        f"\"{removed.strip()[:100]}\""
                    ),
                )
            return ToolResult(success=False, content=result["message"])

        else:
            return ToolResult(
                success=False,
                content=(
                    f"Unknown action: '{action}'. "
                    f"Use: add, replace, remove, read."
                ),
            )
    except Exception as e:
        return ToolResult(success=False, content=f"Core memory error: {e}")


@_summarize("memory_core")
def _memory_core_summary(args: dict) -> str:
    action = args.get("action", "?")
    content = args.get("content", "")
    preview = (
        content[:50] + ("..." if len(content) > 50 else "")
        if content else ""
    )
    return f"memory_core({action}, \"{preview}\")"


@_register("session_search")
def _session_search(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Search past session history using FTS5 full-text search.

    Use this when the user references something from a previous conversation
    ("we fixed this before," "use the approach from last time," "what did we
    change last week?"). Searches across all saved messages in the session DB.

    Returns up to 10 matching message excerpts ordered by relevance.
    """
    query = args.get("query", "")
    limit = args.get("limit", 10)

    if not query.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'query' (search terms).",
        )

    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    if memory_store is None:
        db_path = getattr(_TOOL_CONTEXT, "scratchpad_path", None)
        if db_path:
            return _session_search_fallback(db_path, query, limit)
        return ToolResult(
            success=False,
            content="No persistent storage available for session search.",
        )

    try:
        results = memory_store.search_messages(query, limit=limit)
        if not results:
            return ToolResult(
                success=True,
                content=f"No results found for: \"{query[:200]}\"",
            )
        lines = [f"Search results for \"{query[:200]}\":\n"]
        for r in results:
            content = r["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(
                f"  [{r['rowid']}] (rank={r['rank']:.2f}) {content}"
            )
        return ToolResult(success=True, content="\n".join(lines))
    except Exception as e:
        return ToolResult(
            success=False, content=f"Session search error: {e}"
        )


@_summarize("session_search")
def _session_search_summary(args: dict) -> str:
    query = args.get("query", "?")
    return f"session_search(\"{query[:60]}\")"


def _session_search_fallback(
    db_path: str, query: str, limit: int,
) -> ToolResult:
    """Fallback FTS5 search without MemoryStore."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        safe_query = query.replace('"', '""')
        rows = conn.execute(
            "SELECT rowid, content, rank"
            " FROM messages_fts"
            " WHERE messages_fts MATCH ?"
            " ORDER BY rank"
            " LIMIT ?",
            (f'"{safe_query}"', limit),
        ).fetchall()
        if not rows:
            conn.close()
            return ToolResult(
                success=True,
                content=f"No results found for: \"{query[:200]}\"",
            )
        lines = [f"Search results for \"{query[:200]}\":\n"]
        for r in rows:
            content = r[1]
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(
                f"  [{r[0]}] (rank={r[2]:.2f}) {content}"
            )
        conn.close()
        return ToolResult(success=True, content="\n".join(lines))
    except Exception as e:
        return ToolResult(
            success=False, content=f"Session search error: {e}"
        )


def _memory_core_fallback(
    db_path: str, action: str, content: str, line_number: int,
) -> ToolResult:
    """Fallback implementation that uses SQLite directly (no MemoryStore)."""
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")

        if action == "read":
            row = conn.execute(
                "SELECT content, char_limit FROM core_memory WHERE id = 1"
            ).fetchone()
            if row:
                c = row[0]
                cl = row[1]
                bar_filled = min(len(c) * 50 // max(cl, 1), 50)
                bar = "#" * bar_filled + "." * (50 - bar_filled)
                result = ToolResult(
                    success=True,
                    content=(
                        f"Core memory ({len(c)}/{cl} chars):\n\n"
                        f"{c if c else '(empty)'}\n\n"
                        f"{bar} {len(c) * 100 // max(cl, 1)}% full\n"
                    ),
                )
                conn.close()
                return result
            conn.close()
            return ToolResult(success=True, content="Core memory: (empty)")

        elif action == "add":
            row = conn.execute(
                "SELECT content, char_limit FROM core_memory WHERE id = 1"
            ).fetchone()
            if row:
                current = row[0]
                char_limit = row[1]
                new_content = (
                    (current + "\n" + content).strip() if current else content
                )
                if len(new_content) > char_limit:
                    remaining = (
                        char_limit - len(current) if current else char_limit
                    )
                    conn.close()
                    return ToolResult(
                        success=False,
                        content=(
                            f"Cannot add: would exceed {char_limit} char limit. "
                            f"({len(new_content)} chars with addition, only "
                            f"{remaining} remaining). Consolidate memory first: "
                            f"merge similar entries, remove stale ones."
                        ),
                    )
                conn.execute(
                    "UPDATE core_memory SET content = ? WHERE id = 1",
                    (new_content,),
                )
                conn.commit()
                rem = char_limit - len(new_content)
                conn.close()
                return ToolResult(
                    success=True,
                    content=(
                        f"Added to core memory. {rem} chars remaining "
                        f"({len(new_content)}/{char_limit} used)."
                    ),
                )
            conn.close()
            return ToolResult(success=False, content="Core memory table not found.")

        elif action == "replace":
            row = conn.execute(
                "SELECT char_limit FROM core_memory WHERE id = 1"
            ).fetchone()
            char_limit = row[0] if row else 2500
            if len(content) > char_limit:
                conn.close()
                return ToolResult(
                    success=False,
                    content=(
                        f"Cannot replace: content ({len(content)} chars) exceeds "
                        f"limit of {char_limit} chars. Consolidate first."
                    ),
                )
            conn.execute(
                "UPDATE core_memory SET content = ? WHERE id = 1", (content,)
            )
            conn.commit()
            rem = char_limit - len(content)
            conn.close()
            return ToolResult(
                success=True,
                content=(
                    f"Core memory replaced. {rem} chars remaining "
                    f"({len(content)}/{char_limit} used)."
                ),
            )

        elif action == "remove":
            row = conn.execute(
                "SELECT content FROM core_memory WHERE id = 1"
            ).fetchone()
            if not row or not row[0]:
                conn.close()
                return ToolResult(
                    success=False,
                    content="Core memory is empty. Nothing to remove.",
                )
            lines = row[0].split("\n")
            if line_number < 1 or line_number > len(lines):
                conn.close()
                return ToolResult(
                    success=False,
                    content=(
                        f"Line {line_number} out of range. "
                        f"Lines: 1-{len(lines)}."
                    ),
                )
            removed = lines.pop(line_number - 1)
            new_content = "\n".join(lines).strip()
            conn.execute(
                "UPDATE core_memory SET content = ? WHERE id = 1",
                (new_content,),
            )
            conn.commit()
            conn.close()
            return ToolResult(
                success=True,
                content=(
                    f"Removed line {line_number}: "
                    f"\"{removed.strip()[:100]}\""
                ),
            )

        else:
            conn.close()
            return ToolResult(
                success=False,
                content=(
                    f"Unknown action: '{action}'. "
                    f"Use: add, replace, remove, read."
                ),
            )
    except Exception as e:
        return ToolResult(success=False, content=f"Core memory error: {e}")
