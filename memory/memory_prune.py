#!/usr/bin/env python3
"""
memory_prune.py -- message pruning, compression, and conversation summarization.

Extracted from memory.py to keep the MemoryStore module focused on
persistence while pruning logic lives here.

Functions
---------
_estimate_tokens      -- rough token count for a single message
_total_tokens         -- sum token counts across a message list
_compress_tool_results -- replace large tool results with summaries
_summarize_pruned     -- build a one-paragraph summary of pruned messages
_prune_by_tokens      -- drop oldest messages until under token budget
_strip_orphaned_tool_results -- remove tool results with no matching call
"""

from __future__ import annotations

import json
import os
import threading

from logging_setup import get_logger

_mem_log = get_logger("memory_prune")

_SAVE_MAX_RETRIES = 3
_SAVE_RETRY_DELAY = 0.25  # seconds, multiplied by attempt number


# ---------------------------------------------------------------------------
# Named constants (extracted from magic numbers)
# ---------------------------------------------------------------------------

# Token estimation
_CHARS_PER_TOKEN = 2                 # heuristic: ~2 characters per token (code is denser)
_CHARS_PER_TOKEN_ENGLISH = 4        # English prose: ~4 chars/token (e.g. user messages)
_CHARS_PER_TOKEN_JSON = 3           # JSON/structured tool results: ~3 chars/token
_MIN_TOKEN_ESTIMATE = 1              # floor for token count estimates

# Tool result compression
_COMPRESSION_KEEP_RECENT = 6         # messages at the tail left uncompressed
_COMPRESSION_GENTLE_RECENT = 20      # messages kept with only hard-truncation
_COMPRESSION_MAX_LINES = 5           # lines before a tool result is compressed
_COMPRESSION_GENTLE_MAX_LINES = 20   # lines for gentle-tier compression
_COMPRESSION_MAX_FIRST_LINE = 500    # max length of the first line kept
_TOOL_RESULT_MAX_CHARS = 8000        # per-result size budget before hard truncation
_TOOL_RESULT_GENTLE_CHARS = 16000    # per-result size budget for gentle-tier

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
# Per-save message caches -- avoid re-parsing JSON and re-estimating tokens
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

    Uses content-type-aware heuristics:
    - User messages (English prose): ~4 chars/token
    - Assistant messages (mixed): ~3 chars/token
    - Tool results (JSON): ~3 chars/token
    - Tool call arguments (code/structured): ~2 chars/token

    Caches results by message identity so repeated calls on the same
    message within a single save() are O(1) after the first call.
    """
    mid = id(msg)
    try:
        return _TOKEN_EST_CACHE[mid]
    except KeyError:
        pass

    role = msg.get("role", "")
    if role == "tool":
        text = _get_tool_content(msg)
        divisor = _CHARS_PER_TOKEN_JSON
    elif role == "assistant" and msg.get("tool_calls"):
        # Tool-call messages: count the arguments text (code/JSON, denser)
        total = len(msg.get("content", "") or "")
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            total += len(json.dumps(fn.get("arguments", "")))
        result = max(_MIN_TOKEN_ESTIMATE, total // _CHARS_PER_TOKEN)
        _TOKEN_EST_CACHE[mid] = result
        return result
    elif role == "user":
        text = msg.get("content", "") or ""
        divisor = _CHARS_PER_TOKEN_ENGLISH
    else:
        # assistant text, system, etc.
        text = msg.get("content", "") or json.dumps(msg)
        divisor = _CHARS_PER_TOKEN  # default: code-dense

    result = max(_MIN_TOKEN_ESTIMATE, len(text) // divisor)
    _TOKEN_EST_CACHE[mid] = result
    return result


def _total_tokens(messages: list[dict]) -> int:
    """Sum estimated tokens across all messages.

    Uses a per-list accumulator keyed by list identity so parent and
    sub-agent message lists never corrupt each other's counts.  When
    messages are only appended (the common case), only new messages are
    counted.  When the list shrinks (pruning) or messages are modified
    in-place (compression), a full recount is done.

    This avoids the O(n^2) behaviour of recounting every message on
    every turn as the conversation grows.
    """
    list_id = id(messages)
    n = len(messages)
    with _ACCUM_LOCK:
        entry = _ACCUM_STATE.get(list_id)
        if entry is not None:
            acc_count, acc_total = entry
        else:
            acc_count, acc_total = 0, 0

        if n >= acc_count and list_id == list_id:  # Same list, appending
            new_tokens = sum(_estimate_tokens(m) for m in messages[acc_count:])
            acc_total += new_tokens
            acc_count = n
        else:
            # List shrank (pruned), messages mutated in-place, or new list -- full recount
            acc_total = sum(_estimate_tokens(m) for m in messages)
            acc_count = n

        _ACCUM_STATE[list_id] = (acc_count, acc_total)
        # Prune stale entries -- each entry is just 2 ints, so we only
        # trim when the dict grows unreasonably large.  No gc.collect()
        # needed -- the entries are tiny and will be naturally overwritten
        # as message lists get recycled.
        if len(_ACCUM_STATE) > 64:
            # Keep only the 32 most recently updated entries
            excess = len(_ACCUM_STATE) - 32
            for lid in list(_ACCUM_STATE)[:excess]:
                _ACCUM_STATE.pop(lid, None)
        return acc_total


# Running accumulator for _total_tokens, keyed by list identity.
# Each message list (parent, sub-agents) gets its own accumulator slot.
# Protected by _ACCUM_LOCK for thread safety.
_ACCUM_STATE: dict[int, tuple[int, int]] = {}  # list_id -> (count, total)
_ACCUM_LOCK: threading.Lock = threading.Lock()


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
    gentle_recent: int | None = None,
) -> tuple[list[dict], bool]:
    """Shorten old tool results with content-aware compression.

    When *gentle_recent* is None (default), uses single-tier mode:
    everything older than *keep_recent* gets type-aware compression.

    When *gentle_recent* is set, uses two-tier mode:
    - **Untouched** (last *keep_recent* msgs): left intact.
    - **Gentle** (next *gentle_recent*-*keep_recent*): hard truncation only.
    - **Aggressive** (older): type-aware compression.

    Returns (messages, changed) -- *changed* is True if at least one
    message was compressed in-place.
    """
    changed = False
    if len(messages) <= keep_recent:
        return messages, changed

    # P0.3: Build forward tool_call_id -> name map in one pass (O(n))
    # instead of calling _find_tool_call_name (O(n^2)) per tool message.
    _tool_id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tcid = tc.get("id")
                fn = tc.get("function", {})
                tname = fn.get("name", "").strip()
                if tcid and tname:
                    _tool_id_to_name[tcid] = tname

    # Determine zone: gentle (two-tier mode only) or aggressive
    if gentle_recent is not None and gentle_recent > keep_recent:
        gentle_cutoff = len(messages) - gentle_recent
        two_tier = True
    else:
        gentle_cutoff = -1  # never gentle
        two_tier = False
    agg_cutoff = len(messages) - keep_recent
    for i, m in enumerate(messages):
        if i >= agg_cutoff:
            break  # untouched zone

        # Never compress the system prompt (critical for API prompt caching)
        if i == 0 and m.get("role") == "system":
            continue

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

        # Determine zone: gentle or aggressive
        is_gentle = two_tier and i >= gentle_cutoff
        budget = _TOOL_RESULT_GENTLE_CHARS if is_gentle else _TOOL_RESULT_MAX_CHARS
        max_lines = _COMPRESSION_GENTLE_MAX_LINES if is_gentle else _COMPRESSION_MAX_LINES

        # Hard truncation for oversized results (both zones)
        if len(text) > budget:
            truncated = text[:budget]
            truncated += (
                f"\n... (result truncated at {budget} chars "
                f"out of {len(text)} total. Use read_file with offset to see "
                f"specific sections if needed.)"
            )
            data["content"] = truncated
            m["content"] = json.dumps(data)
            _TOOL_PARSE_CACHE.pop(id(m), None)
            _TOKEN_EST_CACHE.pop(id(m), None)
            changed = True
            continue

        # Gentle tier: only hard truncation (keep more context)
        if is_gentle and len(lines) <= max_lines:
            continue  # short enough -- skip type-aware compression

        if is_gentle:
            # Gentle: keep first N lines instead of type-aware trimming
            if len(lines) > max_lines:
                kept = "\n".join(lines[:max_lines])
                kept += f"\n... (gentle truncation: {len(lines) - max_lines} lines omitted)"
                data["content"] = kept
                m["content"] = json.dumps(data)
                _TOOL_PARSE_CACHE.pop(id(m), None)
                _TOKEN_EST_CACHE.pop(id(m), None)
                changed = True
            continue

        # --- Aggressive tier: type-aware compression ---
        tcid = m.get("tool_call_id", "")
        tool_name = _tool_id_to_name.get(tcid, "")

        if tool_name == "read_file":
            kept = _compress_read_file(lines, messages, i)
        elif tool_name == "search_files":
            kept = _compress_search_files(lines)
        elif tool_name == "run_shell":
            kept = _compress_run_shell(lines)
        elif tool_name == "run_tests":
            kept = _compress_run_tests(lines)
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

    # read_file results have line numbers like "42: content" -- find the
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
        # No match lines found -- keep first 5 as default fallback
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
    """Keep exit code line(s) + last 20 meaningful lines of output.

    Shell output often starts with a marker like 'exit_code=0' or
    the exit code is embedded in the first/last few lines.  We keep
    any line that looks like an exit code / status marker, plus the
    trailing 20 lines (which usually contain the final result).
    """
    if len(lines) <= 20:
        return "\n".join(lines)

    # Identify lines that look like status/exit-code info
    head: list[str] = []
    for line in lines[:3]:
        s = line.strip().lower()
        if any(marker in s for marker in
               ("exit_code", "returncode", "exit", "failed", "ok", "success", "error", "status")):
            head.append(line)

    tail = lines[-20:]
    # Deduplicate if head and tail overlap
    kept = list(dict.fromkeys(head + tail))
    result = "\n".join(kept)
    if len(result) > _COMPRESSION_MAX_FIRST_LINE:
        result = result[:_COMPRESSION_MAX_FIRST_LINE] + "..."
    label = f"status + last {len(tail)}" if head else f"last {len(tail)}"
    return f"... (truncated, {label} of {len(lines)} lines)\n{result}"


def _compress_run_tests(lines: list[str]) -> str:
    """Keep pass/fail summary lines + list of FAILED test names.

    Pytest output is mostly dot-progress and per-test detail.  After
    compression we want: the summary line (X passed, Y failed),
    the list of FAILED test names, and any error summary footer.
    """
    if len(lines) <= _COMPRESSION_MAX_LINES * 4:
        return "\n".join(lines)

    kept_indices: list[int] = []
    for idx, line in enumerate(lines):
        s = line.strip()
        # Summary line: "X passed, Y failed" or "X passed"
        if "passed" in s.lower() and ("failed" in s.lower() or "passed" not in s.lower()):
            kept_indices.append(idx)
        # FAILED test marker
        elif s.startswith("FAILED") or s.startswith("ERRORS") or s.startswith("==="):
            kept_indices.append(idx)
        # Assertion summary / error footer
        elif s.startswith("!") and "short test summary" not in s.lower():
            kept_indices.append(idx)
        # Keep the "short test summary info" header + its lines
        elif "short test summary" in s.lower():
            kept_indices.append(idx)

    if not kept_indices:
        # Fall back: keep first 3 + last 3 lines to capture header + footer
        kept_indices = [0, 1, 2, len(lines) - 3, len(lines) - 2, len(lines) - 1]

    kept = sorted(set(kept_indices))
    parts: list[str] = []
    last = -2
    for k in kept:
        if k > last + 1:
            skipped = k - last - 1
            parts.append(f"... ({skipped} lines skipped) ...")
        parts.append(lines[k])
        last = k
    if last < len(lines) - 1:
        parts.append(f"... ({len(lines) - last - 1} lines skipped -- {len(lines)} total)")
    return "\n".join(parts)


def _compress_default(lines: list[str]) -> str:
    """Default: keep the first 5 lines + truncation marker."""
    if len(lines) <= _COMPRESSION_MAX_LINES:
        return "\n".join(lines)

    kept = "\n".join(lines[:_COMPRESSION_MAX_LINES])
    if len(kept) > _COMPRESSION_MAX_FIRST_LINE:
        kept = kept[:_COMPRESSION_MAX_FIRST_LINE] + "..."

    return kept + f"\n... (truncated at {_COMPRESSION_MAX_LINES} lines -- {len(lines)} total)"


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
            parts.append(f"... ({idx - last_kept - 1} lines skipped) ...")
        parts.append(lines[idx])
        last_kept = idx

    if last_kept < len(lines) - 1:
        parts.append(f"... ({len(lines) - last_kept - 1} lines skipped -- {len(lines)} total {tag})")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Conversation summarization
# ---------------------------------------------------------------------------

# TODO: _summarize_pruned is ~80 lines -- consider splitting into helpers for
#       each message role (user, tool, assistant) and file/command categorization.
def _summarize_pruned(pruned: list[dict]) -> str:
    """Build a one-paragraph summary of pruned messages using an LLM call.

    Falls back to a rules-based summary if the LLM call fails.

    The summary is injected as a synthetic 'user' message so the agent
    sees it as prior conversation context.
    """
    if not pruned:
        return ""

    # Always use rules-based summarization -- deterministic, fast,
    # and testable.  LLM summarization was a nice idea but breaks
    # tests that expect structured output with file names, commands, etc.
    return _summarize_pruned_rules(pruned)


def _summarize_pruned_rules(pruned: list[dict]) -> str:
    """Rules-based summary fallback for small pruned sets."""
    files_read: list[str] = []
    files_written: list[str] = []
    files_edited: list[str] = []
    commands_run: list[str] = []
    turns: list[str] = []

    for m in pruned:
        role = m.get("role", "")
        if role == "user":
            content = m.get("content", "")
            preview = content[:_SUMMARY_PREVIEW_LENGTH].replace("\n", " ")
            if len(content) > _SUMMARY_PREVIEW_LENGTH:
                preview += "..."
            turns.append(f"User: {preview}")

        elif role == "tool":
            text = _get_tool_content(m)

            if "bytes to" in text or "OK: wrote" in text or "OK: replaced" in text:
                path = text.split(" to ")[-1].split("\n")[0] if " to " in text else text
                if len(path) > _SUMMARY_PATH_PREVIEW:
                    path = path[:_SUMMARY_PATH_PREVIEW] + "..."
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
                        preview += "..."
                    commands_run.append(preview)
                elif name == "web_search":
                    q = args.get("query", "?")
                    turns.append(f"Searched web: {q[:_SUMMARY_PATH_PREVIEW]}")

    parts: list[str] = ["Earlier in this conversation:"]
    if turns:
        for t in turns[-_SUMMARY_MAX_TURNS:]:
            parts.append(f"- {t}")
    if files_read:
        unique = list(dict.fromkeys(files_read))
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


# _summarize_pruned_llm removed -- dead code (never called).
# _summarize_pruned always uses _summarize_pruned_rules -- deterministic,
# fast, and testable.  LLM summarization was a nice idea but broke tests
# that expect structured output with file names, commands, etc.


# ---------------------------------------------------------------------------
# Orphaned-tool cleanup
# ---------------------------------------------------------------------------

def _strip_orphaned_tool_messages(
    messages: list[dict],
    *,
    truncate: bool = False,
) -> list[dict]:
    """Remove orphaned tool messages and assistant(tool_calls) in one pass.

    Two fixes applied in sequence:

    1. **Strip orphaned tool results** -- remove ``tool`` messages whose
      ``tool_call_id`` has no preceding ``assistant(tool_calls)`` with a
      matching id.  Prevents 400: "role 'tool' must be a response to a
      preceding message with 'tool_calls'".

    2. **Strip orphaned tool calls** -- remove ``assistant`` messages whose
      ``tool_calls`` lack matching ``tool`` results *after* them in the
      conversation.  Prevents 400: "insufficient tool messages following
      tool_calls".

    When *truncate* is True, the second pass truncates the entire list
    at the first incomplete assistant(tool_calls) sequence -- i.e., all
    messages from that point onward are dropped.  Use ``truncate=True``
    for persistence (coherent conversation), ``truncate=False`` (default)
    for API calls (remove only the broken messages, keep the rest).

    Returns a new list (never mutates the input).
    """
    # Pass 1: remove orphaned tool results (tool messages with no
    #         preceding assistant(tool_calls) that owns their id).
    valid_ids: set[str] = set()
    pass1: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id")
                if tcid:
                    valid_ids.add(tcid)
            pass1.append(m)
        elif m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid and tcid in valid_ids:
                pass1.append(m)
            # else: orphaned -- drop
        else:
            pass1.append(m)

    # Pass 2: handle orphaned assistant(tool_calls).
    seen_ids: set[str] = set()
    orphan_indices: set[int] = set()
    for i in range(len(pass1) - 1, -1, -1):
        m = pass1[i]
        role = m.get("role", "")
        if role == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                seen_ids.add(tcid)
        elif role == "assistant" and "tool_calls" in m:
            tc_ids = [tc.get("id") for tc in m.get("tool_calls", []) if tc.get("id")]
            if tc_ids and not all(tcid in seen_ids for tcid in tc_ids):
                if truncate:
                    return pass1[:i]
                else:
                    orphan_indices.add(i)

    if orphan_indices:
        return [m for i, m in enumerate(pass1) if i not in orphan_indices]
    return pass1


# Backward-compatible aliases (used by tests).
_strip_orphaned_tool_calls = _strip_orphaned_tool_messages
_strip_orphaned_tool_results = _strip_orphaned_tool_messages


# ---------------------------------------------------------------------------
# Token-aware pruning
# ---------------------------------------------------------------------------

# TODO: _prune_by_tokens is ~50 lines -- consider splitting message-count cap
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

    The system prompt (index 0, role=\"system\") is NEVER pruned -- it is
    critical for API-side prompt caching and must remain intact.
    """
    if not messages:
        return [], []

    # Pin the system prompt if present
    sys_msg_start = 0
    sys_prompt: list[dict] = []
    if messages and messages[0].get("role") == "system":
        sys_prompt = [messages[0]]
        sys_msg_start = 1
        messages = messages[1:]  # work on the rest; re-attach at end

    if not messages:
        return sys_prompt, []

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

    # 2. Token budget -- trim oldest turns until under limit.
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
            break  # no user message found -- stop, can't safely prune further
        total -= sum(token_counts[start:cut])
        pruned.extend(messages[start:cut])
        start = cut

    if start > 0:
        messages = messages[start:]

    # Re-attach system prompt
    return sys_prompt + messages, pruned

