#!/usr/bin/env python3
"""
compaction.py -- Context compaction at turn boundaries for cache stability.

Reasonix Pillar 1 (Cache-First Loop) + Pillar 3 (Cost Control).

Three-tier architecture (matching Reasonix's approach):

1. TIER 1 (FREE): Observation masking -- prune_stale_tool_results()
   Replace old tool results with compact placeholders.  No API call.
   If this alone drops context below the hard threshold, skip compaction entirely.
   This is the "prune before compact" pattern from Reasonix's compact.go::maybeCompact().

2. TIER 2 (FREE): Tool-result truncation -- compact_tool_results_at_turn_end()
   Truncate oversized tool results at turn boundary.  The model saw the full
   result during the turn; subsequent turns get a compact summary.

3. TIER 3 (PAID): LLM summarization -- triggered by the orchestrator
   Only when tiers 1+2 are exhausted.  Economic gate applies (min 400 tokens).

Multi-zone thresholds (Reasonix compact.go:24-35):
  - SOFT (50%): context growing -- emit warning, keep cache-stable prefix
  - HARD (80%): trigger pruning + compaction (with economic gate)
  - FORCE (90%): compact regardless of economics

ORCHESTRATOR (maybe_compact):
  Called at every turn boundary.  Replaces the old single-call to
  compact_tool_results_at_turn_end with the full Reasonix pipeline:
    1. Check thresholds from last usage
    2. SOFT: warn once, return
    3. HARD: prune -> if still high -> partition_fold -> economic gate -> summarize
    4. FORCE: prune -> partition_fold -> summarize (skip economic gate)
    5. Detect re-compaction loop -> stuck latch

All compaction is APPEND-ONLY -- we never rewrite the prefix or reorder
log entries.  Compacted content is added at the END, never at the front.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from memory.memory_prune import _estimate_tokens

# ---------------------------------------------------------------------------
# Multi-zone thresholds (Reasonix compact.go:24-35)
# ---------------------------------------------------------------------------

COMPACTION_RATIO_SOFT = 0.50
"""Context ratio (tokens/limit) that emits a soft warning.
The prefix remains cache-stable; no compaction triggered yet."""

COMPACTION_RATIO_HARD = 0.80
"""Context ratio (tokens/limit) that triggers compaction.
Prune first; if still above this ratio after pruning, compact."""

COMPACTION_RATIO_FORCE = 0.90
"""Context ratio (tokens/limit) that forces compaction.
Compact regardless of economic gate (skip the foldEconomics check)."""

# Keep old names for backward-compat with any existing callers
COMPACTION_RATIO_PROACTIVE = COMPACTION_RATIO_SOFT
COMPACTION_RATIO_EMERGENCY = COMPACTION_RATIO_HARD

COMPACTION_TARGET = 0.50
"""Safety cap: after compaction, context should land below this ratio."""

TAIL_TOKEN_BUDGET = 8192
"""Fixed token budget for the verbatim recent tail.
A fixed count (not a fraction) so a huge window still compacts rarely
while a small one still lands below the trigger.  (Reasonix defaults to 16384;
mini_agent uses 8192 to be conservative with smaller models.)"""

MIN_RECENT_KEEP = 2
"""Never keep fewer than this many recent messages in the tail."""

MIN_FOLD_TOKENS = 400
"""Economic gate: skip summarization if the fold region is below this.
The summarization API call costs money; it must save more than it costs."""

# ---------------------------------------------------------------------------
# Turn-end tool result truncation
# ---------------------------------------------------------------------------

TURN_END_RESULT_CAP_TOKENS = 800
"""Max tokens for a tool result retained in subsequent turns.
Reduced from 3000 to 800 -- matching the research finding that 10 recent
turns verbatim is sufficient; older context benefits more from placeholders."""

TURN_END_RESULT_CAP_CHARS = 3200
"""Character cap for tool results.  Approximate -- 1 token ~= 4 chars."""

# ---------------------------------------------------------------------------
# Pruning constants (Reasonix prune.go:14-17)
# ---------------------------------------------------------------------------

_PRUNED_MARKER_PREFIX = "[pruned]"
"""Prefix for pruned tool result placeholders so the model can distinguish
them from live results."""

_MIN_PRUNE_CHARS = 1024
"""Tool results smaller than this are never pruned -- not worth the noise.
Matches Reasonix prune.go minPruneBytes (1024)."""

# Error markers that should NEVER be pruned even when stale (Reasonix KeepErrors).
# The model must see errors verbatim; eliding them loses debugging context.
_ERROR_PATTERNS: tuple[str, ...] = (
    "Error:", "error:", "ERROR:", "FAILED", "Failed",
    "Traceback", "Exception", "PermissionError", "FileNotFoundError",
    "exit code", "status code", "[ERROR]",
    # Reasonix isErrorMessage also checks for "blocked:" prefix (compact.go:462-463)
    "blocked:", "Blocked:", "BLOCKED:",
)

# User-marking prefixes for KeepUserMarked policy (Reasonix compact.go:466-474)
_KEEP_MARKERS: tuple[str, ...] = (
    "[[keep]]", "[keep]", "<keep>", "<!-- keep -->",
)

# ---------------------------------------------------------------------------
# PruneStats
# ---------------------------------------------------------------------------

@dataclass
class PruneStats:
    """Returned by prune_stale_tool_results()."""
    results: int = 0
    """Number of tool results that were pruned to placeholders."""
    saved_chars: int = 0
    """Total characters saved by pruning (original - placeholder)."""
    estimated_tokens_saved: int = 0
    """Estimated token savings (saved_chars // 4)."""


@dataclass
class CompactionResult:
    """Returned by maybe_compact() with details about what happened."""
    level: str = ""                    # 'none', 'soft', 'hard', 'force'
    pruned: PruneStats = field(default_factory=PruneStats)
    compacted: bool = False           # True if LLM summarization ran
    summary: str = ""                  # Compaction summary text (if any)
    archive_path: str = ""            # Path to archived messages (if any)
    stuck: bool = False               # True if re-compaction loop detected
    truncated: int = 0                # Number of tool results truncated (Tier 2)
    tokens_before: int = 0            # Estimated tokens before compaction
    tokens_after: int = 0             # Estimated tokens after compaction


# ---------------------------------------------------------------------------
# Module-level state (matching Reasonix Agent fields on compact.go:128-134)
# ---------------------------------------------------------------------------

_consecutive_compacts: int = 0
"""Number of consecutive turns where compaction ran.  >= 2 triggers stuck latch."""

_compact_stuck: bool = False
"""True when the window is too small for compaction to help.  Auto-compaction
pauses until the prompt drops back below the trigger."""

_soft_compact_noticed: bool = False
"""True once we've emitted the SOFT warning.  Only warn once per session."""

_last_tok_per_char: float = 0.25
"""Calibrated tokens-per-character ratio from last real API usage.
Starts at 0.25 (~4 chars/token) and gets updated after each API call."""


def _reset_compaction_state() -> None:
    """Reset module-level compaction state (for testing / new sessions)."""
    global _consecutive_compacts, _compact_stuck, _soft_compact_noticed, _last_tok_per_char
    _consecutive_compacts = 0
    _compact_stuck = False
    _soft_compact_noticed = False
    _last_tok_per_char = 0.25


# ---------------------------------------------------------------------------
# Tier 1: Observation masking (FREE)
# ---------------------------------------------------------------------------

def _is_error_message(content: str) -> bool:
    """Check if tool result content looks like an error that should be preserved.

    Reasonix: KeepErrors policy -- error messages are preserved verbatim so
    the model can debug failures.  Only non-error tool results are prunable.
    """
    if not content:
        return False
    # Check first 500 chars -- errors typically surface early
    head = content[:500]
    return any(pat in head for pat in _ERROR_PATTERNS)


def _is_user_marked(content: str) -> bool:
    """Check if a user message is marked with [[keep]] or equivalent.

    Reasonix KeepUserMarked policy (compact.go:466-474): users can protect
    specific messages from compaction by prefixing them with a keep marker.
    """
    if not content:
        return False
    stripped = content.strip()
    for marker in _KEEP_MARKERS:
        if stripped.startswith(marker):
            return True
    return False


def _is_compaction_summary(message: dict) -> bool:
    """Check if a message is a prior compaction summary.

    Reasonix isCompactionSummary (compact.go): checks for summaryTagOpen prefix.
    Previous digests are kept verbatim in partitionFold so a later fold never
    re-summarizes an earlier digest and drops the facts it already captured.
    """
    content = message.get("content", "")
    return "<compaction-summary>" in content


def _find_tool_name(messages: list[dict], tool_idx: int) -> str:
    """Find the tool function name for a tool-result message at *tool_idx*.

    Walks backward from *tool_idx* to find the preceding assistant message
    whose tool_calls include the matching tool_call_id.
    """
    tool_call_id = messages[tool_idx].get("tool_call_id")
    if not tool_call_id:
        return "unknown"
    for j in range(tool_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in prev.get("tool_calls", []):
            if tc.get("id") == tool_call_id:
                fn = tc.get("function", {})
                return fn.get("name", "unknown").strip()
    return "unknown"


def _describe_tool_call(messages: list[dict], tool_idx: int) -> str:
    """Build a compact description of the tool call for the placeholder.

    Examples:
      read_file("core/config.py", offset=140, limit=30)
      search_files("compaction" in mini_agent/)
      run_shell("pytest tests/ -v")
      write_file("core/out.py")
    """
    tool_name = _find_tool_name(messages, tool_idx)
    if tool_name == "unknown":
        return "tool result"

    tool_call_id = messages[tool_idx].get("tool_call_id")
    if not tool_call_id:
        return tool_name

    # Find the original tool call arguments for a useful description
    for j in range(tool_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in prev.get("tool_calls", []):
            if tc.get("id") == tool_call_id:
                fn = tc.get("function", {})
                raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw)
                    return _format_tool_desc(tool_name, args)
                except (json.JSONDecodeError, TypeError):
                    return tool_name
    return tool_name


def _format_tool_desc(name: str, args: dict) -> str:
    """Format a compact description of a tool call from its args dict."""
    if name == "read_file":
        path = args.get("path", "")
        parts = path.replace("\\", "/").split("/")
        short_path = "/".join(parts[-2:]) if len(parts) > 2 else path
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None and limit is not None:
            return f'read_file("{short_path}", offset={offset}, limit={limit})'
        return f'read_file("{short_path}")'

    if name == "search_files":
        pattern = str(args.get("pattern", ""))[:60]
        path = args.get("path", "")
        parts = path.replace("\\", "/").split("/")
        short_path = "/".join(parts[-2:]) if len(parts) > 2 else path
        return f'search_files("{pattern}" in {short_path or "/"})'

    if name == "list_directory":
        path = args.get("path", "")
        parts = path.replace("\\", "/").split("/")
        short_path = "/".join(parts[-2:]) if len(parts) > 2 else path
        return f"list_directory({short_path})"

    if name == "run_shell":
        cmd = str(args.get("command", ""))[:80]
        return f'run_shell("{cmd}")'

    if name == "write_file":
        path = args.get("path", "")
        parts = path.replace("\\", "/").split("/")
        short_path = "/".join(parts[-2:]) if len(parts) > 2 else path
        return f'write_file("{short_path}")'

    if name == "web_search":
        query = str(args.get("query", ""))[:80]
        return f'web_search("{query}")'

    if name == "edit_file":
        path = args.get("path", "")
        parts = path.replace("\\", "/").split("/")
        short_path = "/".join(parts[-2:]) if len(parts) > 2 else path
        return f'edit_file("{short_path}")'

    # Generic: show first arg or name only
    if args:
        first_key = next(iter(args))
        first_val = str(args[first_key])[:60]
        return f'{name}({first_key}="{first_val}")'
    return name


def prune_stale_tool_results(
    messages: list[dict],
    keep_recent: int | None = None,
    tail_token_budget: int = TAIL_TOKEN_BUDGET,
) -> PruneStats:
    """Prune old tool results to compact placeholders (TIER 1 -- FREE).

    Replaces tool-result content outside the protected recent tail with
    compact placeholders like:
        "[pruned] read_file(\"core/compaction.py\", offset=1, limit=30)
        -- 45 lines / ~1200 bytes.  Re-read the tool if details are needed."

    This is the "prune before compact" pattern from Reasonix:
    - prune.go:PruneStaleToolResults() runs BEFORE the paid compaction
    - If pruning alone drops context below the hard threshold,
      the expensive summarization is skipped entirely
    - Error messages are NEVER pruned (KeepErrors policy)

    Args:
        messages: The conversation message list (mutated in-place).
        keep_recent: Number of recent messages to protect (None = auto-calculate
            from tail_token_budget).
        tail_token_budget: Token budget for the verbatim recent tail.

    Returns:
        PruneStats with count of pruned messages and estimated savings.
    """
    stats = PruneStats()
    if not messages or len(messages) < 4:
        return stats

    # Determine protected tail: last N messages (token-budgeted) or keep_recent
    if keep_recent is None:
        keep_recent = _tail_start_by_tokens(messages, tail_token_budget)
    protected_cutoff = max(0, len(messages) - keep_recent)

    # Determine pinned prefix: system message + first user turn (capped)
    head_protected = _pinned_prefix_len(messages)

    for i in range(len(messages)):
        # Skip protected zones
        if i < head_protected:
            continue
        if i >= protected_cutoff:
            continue

        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        if msg.get("_pruned"):
            continue

        content = msg.get("content", "")
        if not content or len(content) < _MIN_PRUNE_CHARS:
            continue

        # Never prune error messages (Reasonix KeepErrors policy)
        if _is_error_message(content):
            continue

        # Build compact placeholder
        tool_desc = _describe_tool_call(messages, i)
        line_count = content.count("\n") + 1
        byte_count = len(content.encode("utf-8"))

        placeholder = (
            f"{_PRUNED_MARKER_PREFIX} {tool_desc}"
            f" -- {line_count} lines / ~{byte_count} bytes."
            f"  Re-read the tool if details are needed."
        )

        saved = len(content) - len(placeholder)
        if saved <= 0:
            continue

        msg["content"] = placeholder
        msg["_pruned"] = True
        msg["_original_length"] = len(content)
        msg["_original_line_count"] = line_count
        stats.results += 1
        stats.saved_chars += saved

    stats.estimated_tokens_saved = stats.saved_chars // 4
    return stats


# ---------------------------------------------------------------------------
# Tier 2: Tool-result truncation (FREE)
# ---------------------------------------------------------------------------

def compact_tool_results_at_turn_end(
    messages: list[dict],
    cap_tokens: int = TURN_END_RESULT_CAP_TOKENS,
    cap_chars: int = TURN_END_RESULT_CAP_CHARS,
) -> int:
    """Truncate large tool results at turn boundary (TIER 2 -- FREE).

    The model saw the full result during the turn; subsequent turns only
    need the gist.  Keeps first ~15% and last ~60% of each oversized result.

    CRITICAL (Cache-First Loop): Only truncates tool results AFTER the last
    non-transient user message.  Tool results in the cached prefix must stay
    byte-identical or DeepSeek's KV-cache is invalidated.

    Returns number of tool results compacted.
    """
    compacted = 0
    if not messages:
        return compacted

    # Find the last non-transient user message.
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and not m.get("_transient"):
            last_user_idx = i
            break

    # Only process messages in the uncached tail
    start_idx = last_user_idx + 1 if last_user_idx >= 0 else 0

    for i in range(start_idx, len(messages)):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        if msg.get("_turn_end_compacted") or msg.get("_pruned"):
            continue
        content = msg.get("content", "")
        if not content:
            continue
        token_est = _estimate_tokens(msg)
        if token_est <= cap_tokens and len(content) <= cap_chars:
            continue

        # Keep: first 15% + last 60% of content.  Cut the middle.
        head_keep = max(int(len(content) * 0.15), 200)
        tail_keep = max(int(len(content) * 0.60), 1500)
        head = content[:head_keep]
        tail = content[-tail_keep:]
        truncated_len = len(content) - head_keep - tail_keep

        if truncated_len > 0:
            msg["content"] = (
                head
                + f"\n\n... [compacted {truncated_len:} chars / ~{truncated_len // 4:} tokens] ...\n\n"
                + tail
            )
            msg["_turn_end_compacted"] = True
            msg["_original_length"] = len(content)
            compacted += 1

    return compacted


# ---------------------------------------------------------------------------
# Tier 3 gate: should we compact?
# ---------------------------------------------------------------------------

def should_compact(token_count: int, context_limit: int) -> str | None:
    """Return compaction level: 'soft', 'hard', 'force', or None.

    Multi-zone thresholds (Reasonix compact.go:24-35):
      - 'soft'  (50%): context growing -- emit warning, cache-stable prefix
      - 'hard'  (80%): trigger pruning + compaction (with economic gate)
      - 'force' (90%): compact regardless of economics
      - None: no action needed

    Call this BEFORE each API call to decide whether to trigger compaction.
    """
    if context_limit <= 0:
        return None
    ratio = token_count / context_limit
    if ratio >= COMPACTION_RATIO_FORCE:
        return "force"
    if ratio >= COMPACTION_RATIO_HARD:
        return "hard"
    if ratio >= COMPACTION_RATIO_SOFT:
        return "soft"
    return None


# ---------------------------------------------------------------------------
# Economic gate (Reasonix compact.go:138-144)
# ---------------------------------------------------------------------------

def fold_economics(foldable_tokens: int) -> bool:
    """Return True if folding (summarization) is worth the API call cost.

    Returns False when the foldable region is too small for the savings to
    justify the extra round-trip cost and latency of calling the summarizer.
    """
    return foldable_tokens >= MIN_FOLD_TOKENS


# ---------------------------------------------------------------------------
# Compaction summary appending
# ---------------------------------------------------------------------------

def append_compaction_summary(
    messages: list[dict],
    pruned: list[dict],
    summary_text: str,
) -> None:
    """Append a compaction summary message (never insert at front!).

    Contrast with memory.py's current `kept.insert(0, summary_msg)` which
    breaks DeepSeek's prefix cache.  We APPEND the summary so the prefix
    stays byte-stable.

    Uses the <compaction-summary> tag wrapper from Reasonix so the model
    can distinguish compacted context from live user input.
    """
    if not summary_text.strip():
        return
    msg = {
        "role": "user",
        "content": (
            "<compaction-summary>\n"
            "Summary of earlier conversation (older messages were compacted"
            " to save context):\n"
            + summary_text + "\n"
            "</compaction-summary>"
        ),
    }
    messages.append(msg)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_context_tokens(messages: list[dict]) -> int:
    """Estimate total token count for a message list.

    Uses the calibrated tok-per-char ratio if available, falling back to
    the tokenizer-agnostic heuristic from memory.py.
    """
    return sum(_estimate_tokens(m) for m in messages)


def _tok_per_char_calibrated(messages: list[dict]) -> float:
    """Return the calibrated tokens-per-character ratio.

    Uses actual API usage data if available (Reasonix compact.go:566-576),
    otherwise falls back to ~4 chars/token.
    """
    global _last_tok_per_char
    return _last_tok_per_char


def update_tok_per_char(prompt_tokens: int, messages: list[dict]) -> None:
    """Calibrate tokens-per-character from real API usage.

    Call this after each API call with the actual prompt_tokens from the
    usage response.  Reasoning content is excluded from char count to
    match what was actually sent.
    """
    global _last_tok_per_char
    if prompt_tokens <= 0:
        return
    total_chars = _count_provider_chars(messages)
    if total_chars <= 0:
        return
    ratio = prompt_tokens / total_chars
    if 0.05 < ratio < 2:  # sanity bounds
        _last_tok_per_char = ratio


def _count_provider_chars(messages: list[dict]) -> int:
    """Count characters that actually reach the provider (excludes reasoning)."""
    n = 0
    for m in messages:
        n += len(m.get("content", ""))
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            n += len(fn.get("name", ""))
            n += len(fn.get("arguments", "{}"))
    return n


# ---------------------------------------------------------------------------
# Pinned prefix (Reasonix compact.go:500-528 pinnedPrefixLen)
# ---------------------------------------------------------------------------

_PINNED_FIRST_USER_MAX_TOKENS = 1500
"""Ceiling on pinning the first user turn verbatim (Reasonix: maxPinnedFirstUserTokens)."""

_PINNED_FIRST_USER_WINDOW_FRAC = 0.15
"""Never pin a first turn worth more than this fraction of the window."""


def _pinnable_user_turn(
    message: dict,
    context_window: int = 0,
    tok_per_char: float = 0.25,
) -> bool:
    """Report whether a user turn is small enough to keep verbatim in a fold.

    Reasonix pinnableUserTurn (compact.go:371-382): uses the same budget as
    pinnedFirstUserTokens (max 1500 tokens or 15% of context window).
    A longer turn (e.g. pasted content) folds like any other message so the
    kept-verbatim floor never starves the window.
    """
    budget = _PINNED_FIRST_USER_MAX_TOKENS
    if context_window > 0:
        window_budget = int(context_window * _PINNED_FIRST_USER_WINDOW_FRAC)
        if window_budget < budget:
            budget = window_budget
    return _estimate_chars_tok(message, tok_per_char) <= budget


def _estimate_chars_tok(message: dict, tok_per_char: float) -> int:
    """Estimate tokens for a single message using msgChars * tokPerChar.

    Reasonix msgChars (compact.go:581-587): counts content plus tool-call
    names and arguments, but NOT reasoning (stripped on send).
    """
    chars = len(message.get("content", ""))
    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        chars += len(fn.get("name", ""))
        chars += len(fn.get("arguments", "{}"))
    return int(chars * tok_per_char)


def _pinned_prefix_len(
    messages: list[dict],
    context_window: int = 0,
) -> int:
    """Number of leading messages to preserve verbatim.

    Always protects: system message (index 0) + first user-assistant pair.
    Caps the first user turn at 1500 tokens or 15% of context_window.
    """
    if not messages:
        return 0

    pinned = 0

    # Always keep system message if present
    if messages[0].get("role") == "system":
        pinned = 1

    # Keep first user message (task definition), but cap it (Reasonix pinnableUserTurn)
    if pinned < len(messages):
        first_user = messages[pinned]
        if first_user.get("role") == "user":
            if _pinnable_user_turn(first_user, context_window):
                pinned += 1
                # Also keep the assistant reply if present
                if pinned < len(messages) and messages[pinned].get("role") == "assistant":
                    pinned += 1

    return max(pinned, 0)


# ---------------------------------------------------------------------------
# Tail calculation (Reasonix compact.go:537-560 tailStart)
# ---------------------------------------------------------------------------

def _tail_start_by_tokens(messages: list[dict], budget_tokens: int) -> int:
    """Calculate how many messages from the end fit within *budget_tokens*.

    Walks newest->oldest, growing the verbatim tail until the next message
    would push the token estimate past *budget_tokens* (but never below
    MIN_RECENT_KEEP).  Matches Reasonix's tailStart() in compact.go.

    Returns the number of messages to keep from the end.
    """
    acc = 0
    kept = 0
    for i in range(len(messages) - 1, -1, -1):
        tok = _estimate_tokens(messages[i])
        if kept >= MIN_RECENT_KEEP and acc + tok > budget_tokens:
            break
        acc += tok
        kept += 1
    # Align: don't start tail on an orphaned tool result
    while kept < len(messages) and messages[-kept].get("role") == "tool":
        kept += 1
    return max(kept, MIN_RECENT_KEEP)


def _tail_start_index(
    messages: list[dict],
    head: int,
    budget_tokens: int,
    tok_per_char: float = 0.25,
    min_keep: int = MIN_RECENT_KEEP,
) -> int:
    """Return the index into messages where the verbatim tail begins.

    Reasonix tailStart() (compact.go:542-560): walks newest->oldest, growing
    the verbatim tail until budget_tokens is exceeded, then aligns boundary
    back off any orphaned tool result.
    """
    start = len(messages)
    acc = 0
    for i in range(len(messages) - 1, head - 1, -1):
        chars = len(messages[i].get("content", ""))
        for tc in messages[i].get("tool_calls", []):
            fn = tc.get("function", {})
            chars += len(fn.get("name", "")) + len(fn.get("arguments", "{}"))
        c = int(chars * tok_per_char)
        if len(messages) - i > min_keep and acc + c > budget_tokens:
            break
        acc += c
        start = i

    # Align boundary off orphaned tool results
    while start > head and start < len(messages) and messages[start].get("role") == "tool":
        start -= 1

    return start


# ---------------------------------------------------------------------------
# Partition fold (Reasonix compact.go:388-429)
# ---------------------------------------------------------------------------

def _partition_fold(
    region: list[dict],
    context_window: int = 0,
    tok_per_char: float = 0.25,
) -> tuple[list[dict], list[dict]]:
    """Split a compactable region into (kept_verbatim, foldable).

    Reasonix partitionFold (compact.go:388-429):
    - Pinnable user turns (≤1500 tokens / 15% window) are kept verbatim
    - User-marked messages ([[keep]], etc.) are kept verbatim
    - Previous compaction summaries are kept verbatim (digests never re-summarized)
    - Tool calls and their results stay together (never split a pair)
    - Everything else goes into the foldable region

    Returns (kept, fold) where kept is the list to preserve verbatim
    and fold is the list that will be summarized.
    """
    if not region:
        return [], []

    keep_mask = [False] * len(region)

    # First pass: identify individually-keepable messages
    for i, m in enumerate(region):
        role = m.get("role", "")
        content = m.get("content", "")

        # Keep pinnable user turns verbatim (Reasonix pinnableUserTurn)
        if role == "user" and _pinnable_user_turn(m, context_window, tok_per_char):
            keep_mask[i] = True

        # Keep user-marked messages ([[keep]], [keep], <keep>, <!-- keep -->)
        if role == "user" and _is_user_marked(content):
            keep_mask[i] = True

        # Keep previous compaction summaries (digests never re-summarized)
        if _is_compaction_summary(m):
            keep_mask[i] = True

        # Keep error tool results (Reasonix KeepErrors policy)
        # Preserve error messages verbatim so the summarizer can reference
        # them when distilling the fold region.  Paired tool-call groups are
        # expanded in the second pass below.
        if role == "tool" and _is_error_message(content):
            keep_mask[i] = True
    # Second pass: keep tool_call/result groups that belong to kept messages
    for i, m in enumerate(region):
        if not keep_mask[i]:
            continue
        role = m.get("role", "")
        if role == "assistant" and m.get("tool_calls"):
            # Keep all tool results that follow this assistant message
            for j in range(i + 1, len(region)):
                if region[j].get("role") == "tool":
                    keep_mask[j] = True
                else:
                    break
        elif role == "tool":
            # Find the assistant that called this tool and keep it too
            tc_id = m.get("tool_call_id")
            if tc_id:
                for j in range(i - 1, -1, -1):
                    prev = region[j]
                    if prev.get("role") != "assistant":
                        continue
                    for tc in prev.get("tool_calls", []):
                        if tc.get("id") == tc_id:
                            keep_mask[j] = True
                            break
                    if keep_mask[j]:
                        break

    kept = [region[i] for i in range(len(region)) if keep_mask[i]]
    fold = [region[i] for i in range(len(region)) if not keep_mask[i]]
    return kept, fold


# ---------------------------------------------------------------------------
# Mechanical fold digest (Reasonix compact.go: fallback when summarizer fails)
# ---------------------------------------------------------------------------

def mechanical_fold_digest(num_folded: int, archive_path: str = "") -> str:
    """Return a deterministic placeholder when LLM summarization fails.

    Reasonix (compact.go:250-252): when the summarizer call fails, a
    mechanical fold frees context with a deterministic marker instead
    of aborting compaction entirely.  This prevents context exhaustion
    when the summarizer is unavailable.
    """
    detail = ""
    if archive_path:
        detail = f" Original messages were archived to {archive_path}."
    return (
        f"{num_folded} earlier message(s) were folded here to save context."
        f" The key actions, decisions, errors, and file changes from those"
        f" turns are summarized above in earlier compaction summaries."
        f"{detail}"
        f" Ask the user if you need details that are no longer visible."
    )


# ---------------------------------------------------------------------------
# Message archiving (Reasonix compact.go: archiveMessages)
# ---------------------------------------------------------------------------

def _archive_messages(archive_dir: str, messages: list[dict]) -> str:
    """Archive messages to a timestamped .jsonl file before dropping them.

    Returns the path to the archive file, or empty string on failure.
    """
    if not archive_dir or not messages:
        return ""
    try:
        os.makedirs(archive_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(archive_dir, f"compaction_archive_{ts}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Transcript rendering for the summarizer
# ---------------------------------------------------------------------------

def _render_transcript(messages: list[dict]) -> str:
    """Render a list of messages into a compact transcript for the summarizer.

    The summarizer gets a condensed view: role prefixes + content, with
    tool results trimmed to keep the prompt small.
    """
    lines = []
    for i, m in enumerate(messages):
        role = m.get("role", "unknown")
        content = m.get("content", "")

        if role == "tool":
            # Truncate tool results aggressively for summarizer
            tool_name = m.get("name", "unknown")
            if len(content) > 600:
                content = content[:300] + f"\n... [truncated, ~{len(content)} chars total] ...\n" + content[-300:]
            lines.append(f"[{i}] tool:{tool_name}: {content}")
        elif role == "assistant":
            tool_calls = m.get("tool_calls", [])
            if tool_calls:
                tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                lines.append(f"[{i}] assistant: called {', '.join(tc_names)}")
                if content:
                    lines.append(f"  reasoning: {content[:300]}")
            else:
                lines.append(f"[{i}] assistant: {content[:500]}")
        else:
            lines.append(f"[{i}] {role}: {content[:800]}")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Summarizer system prompt
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = """You are a conversation summarizer. Your job is to distill the
conversation below into a concise briefing that preserves ALL critical
information for continuing the session. Include:

1. The user's main goal(s) and any constraints they specified
2. Key decisions made (with rationale)
3. Files that were created, modified, or read (with paths)
4. Errors encountered and how they were resolved
5. Facts, conventions, and patterns discovered
6. The current state: what was just done and what should happen next
7. Any open questions or unresolved issues

Be specific -- include file paths, function names, error messages, and
concrete details.  A reader should be able to continue the session
without losing context.

Output ONLY the summary text, no preamble or meta-commentary."""


# ---------------------------------------------------------------------------
# LLM summarization (Tier 3 - PAID)
# ---------------------------------------------------------------------------

def _summarize_via_llm(
    fold_messages: list[dict],
    instructions: str = "",
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    timeout: int = 45,
) -> str:
    """Call a lightweight model to summarize the foldable region.

    Uses the same provider as the main agent (from env vars), but with
    a compact system prompt and no tools.  Falls back gracefully on failure.

    Returns the summary text, or raises an exception on failure.
    """
    import os as _os

    if not api_key:
        api_key = _os.environ.get("DEEPSEEK_API_KEY", "")
    if not model:
        model = _os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if not base_url:
        base_url = _os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    if not api_key:
        raise RuntimeError("No API key available for summarization")

    transcript = _render_transcript(fold_messages)
    sys_prompt = _SUMMARY_SYSTEM_PROMPT
    if instructions.strip():
        sys_prompt += "\n\nAdditional focus for this compaction (prioritize keeping this):\n" + instructions.strip()

    # Use httpx or requests -- whichever is available
    try:
        import requests as _requests
        resp = _requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": transcript},
                ],
                "temperature": 0.3,
                "max_tokens": 1200,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        raise


def _summarize_with_retry(
    fold_messages: list[dict],
    instructions: str = "",
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    max_retries: int = 2,
) -> str:
    """Summarize with one retry on transient failures.

    Reasonix summarizeWithRetry pattern (compact.go: reference in compact()).
    On failure, waits 2s and retries once before falling back to mechanical fold.
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            return _summarize_via_llm(
                fold_messages,
                instructions=instructions,
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2)

    raise last_err  # type: ignore[misc]


# ===========================================================================
# ORCHESTRATOR: maybe_compact()
# ===========================================================================

def maybe_compact(
    messages: list[dict],
    context_window: int,
    *,
    last_prompt_tokens: int = 0,
    archive_dir: str = "",
    summarizer_api_key: str = "",
    summarizer_model: str = "",
    summarizer_base_url: str = "",
) -> CompactionResult:
    """The main entry point: run compaction at a turn boundary.

    Called AFTER each LLM response (before the next turn starts).
    Replaces the old single-call to compact_tool_results_at_turn_end
    with the full Reasonix pipeline:

      1. Estimate token count and check thresholds
      2. SOFT (50%): emit warning, no action
      3. HARD (80%): Tier 1 prune -> if still high -> Tier 3 compact (with economic gate)
      4. FORCE (90%): Tier 1 prune -> Tier 3 compact (skip economic gate)
      5. Detect re-compaction loop -> stuck latch + warning
      6. Mechanical fold fallback when summarizer fails

    Args:
        messages: The conversation message list (mutated in place).
        context_window: Provider context window size (0 = disabled).
        last_prompt_tokens: Actual prompt tokens from last API usage response.
            If 0, falls back to estimated token count.
        archive_dir: Optional directory to archive dropped messages.
        summarizer_api_key: API key for the summarizer (falls back to env).
        summarizer_model: Model for summarizer (falls back to env).
        summarizer_base_url: Base URL for summarizer (falls back to env).

    Returns:
        CompactionResult with details about what happened.
    """
    global _consecutive_compacts, _compact_stuck, _soft_compact_noticed, _last_tok_per_char

    result = CompactionResult()

    # Disabled: no context window configured
    if context_window <= 0:
        return result

    # Estimate current token count (prefer real usage, fall back to heuristic)
    if last_prompt_tokens > 0:
        token_count = last_prompt_tokens
    else:
        token_count = estimate_context_tokens(messages)

    result.tokens_before = token_count

    # Calibrate tok-per-char from real usage
    if last_prompt_tokens > 0:
        update_tok_per_char(last_prompt_tokens, messages)

    # Determine level
    level = should_compact(token_count, context_window)
    if level is None:
        # Under SOFT threshold -- breathing room! Clear stuck latch and counter.
        _consecutive_compacts = 0
        _compact_stuck = False
        _soft_compact_noticed = False
        return result

    result.level = level
    high_water = int(context_window * COMPACTION_RATIO_HARD)
    force_water = int(context_window * COMPACTION_RATIO_FORCE)

    # --- SOFT: emit warning once, keep cache-stable prefix ---
    if level == "soft":
        if not _soft_compact_noticed:
            _soft_compact_noticed = True
            # Warning emitted via return value -- caller should log it
        return result

    # --- Stuck latch: if we're in a re-compaction loop, pause auto-compaction ---
    if _compact_stuck:
        return result

    # --- HARD / FORCE: prune first (Tier 1 - FREE) ---
    force = (level == "force")
    prune_stats = prune_stale_tool_results(messages)
    result.pruned = prune_stats

    # Re-estimate after pruning
    token_count = estimate_context_tokens(messages)
    result.tokens_after = token_count

    # If pruning alone dropped us below HARD threshold, skip paid compaction
    if not force and token_count < high_water:
        _consecutive_compacts = 0
        return result

    # --- Plan compaction: find head/tail split ---
    head = _pinned_prefix_len(messages, context_window)
    tail_budget = TAIL_TOKEN_BUDGET
    target_max = int(context_window * COMPACTION_TARGET)
    if target_max < tail_budget:
        tail_budget = target_max

    tok_per_char = _tok_per_char_calibrated(messages)
    start = _tail_start_index(messages, head, tail_budget, tok_per_char)

    if start <= head:
        # Recent tail already covers everything worth keeping
        return result

    region = messages[head:start]

    # --- Partition fold: keep pinnable user turns & prior digests verbatim ---
    kept, fold = _partition_fold(region, context_window, tok_per_char)
    if not fold:
        # Nothing foldable -- just kept user turns
        return result

    # --- Economic gate: skip if fold region too small (unless forced) ---
    fold_tokens = sum(_estimate_tokens(m) for m in fold)
    if not force and not fold_economics(fold_tokens):
        return result

    # --- Archive originals before dropping them ---
    archive_path = ""
    if archive_dir:
        archive_path = _archive_messages(archive_dir, fold)
        result.archive_path = archive_path

    # --- Summarize (Tier 3 - PAID) ---
    try:
        summary = _summarize_with_retry(
            fold,
            api_key=summarizer_api_key,
            model=summarizer_model,
            base_url=summarizer_base_url,
        )
    except Exception as e:
        # Mechanical fold fallback when summarizer fails
        summary = mechanical_fold_digest(len(fold), archive_path)
        result.summary = summary
        result.compacted = True

    # --- Rebuild message list: prefix + kept + summary + tail ---
    compacted_messages = []
    compacted_messages.extend(messages[:head])
    compacted_messages.extend(kept)
    append_compaction_summary(compacted_messages, fold, summary)
    compacted_messages.extend(messages[start:])

    # Replace in place
    messages.clear()
    messages.extend(compacted_messages)

    result.compacted = True
    result.summary = summary

    # Re-estimate after compaction
    result.tokens_after = estimate_context_tokens(messages)

    # --- Detect re-compaction loop ---
    _consecutive_compacts += 1
    if _consecutive_compacts >= 2:
        _compact_stuck = True
        result.stuck = True

    return result


# ===========================================================================
# Convenience: run full pipeline at turn boundary
# ===========================================================================

def run_turn_boundary_compaction(
    messages: list[dict],
    context_window: int,
    *,
    last_prompt_tokens: int = 0,
    archive_dir: str = "",
    summarizer_api_key: str = "",
    summarizer_model: str = "",
    summarizer_base_url: str = "",
) -> CompactionResult:
    """Run the full three-tier compaction pipeline at a turn boundary.

    Convenience wrapper that runs Tier 2 (truncation) followed by the
    full maybe_compact() orchestrator (which handles Tier 1 + Tier 3).

    This is the function to call from the agent loop at each turn boundary.
    """
    # Tier 2: truncate oversized tool results (FREE)
    truncated = compact_tool_results_at_turn_end(messages)

    # Tier 1 + Tier 3: full maybe_compact orchestrator
    result = maybe_compact(
        messages,
        context_window,
        last_prompt_tokens=last_prompt_tokens,
        archive_dir=archive_dir,
        summarizer_api_key=summarizer_api_key,
        summarizer_model=summarizer_model,
        summarizer_base_url=summarizer_base_url,
    )
    result.truncated = truncated
    return result
