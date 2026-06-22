#!/usr/bin/env python3
"""
pi_compaction.py — Pi-style LLM summarization compaction for mini_agent.

Replaces Dirac-style half/quarter truncation with pi's approach:
- Structured summaries (Goal, Progress, Key Decisions, Next Steps, etc.)
- LLM-powered summarization of old conversation turns
- Keeps recent ~20K tokens verbatim
- Iterative: updates previous summary instead of regenerating
- File tracking across compactions
- Configurable reserve/keep thresholds

Based on pi's compaction.ts design:
https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/src/core/compaction/
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from logging_setup import get_logger

_log = get_logger("pi_compaction")

# ---------------------------------------------------------------------------
# Pi-style thresholds (matching pi defaults)
# ---------------------------------------------------------------------------

RESERVE_TOKENS = 16384
"""Tokens to reserve for the LLM response. Compaction fires when
contextTokens > contextWindow - reserveTokens."""

KEEP_RECENT_TOKENS = 20000
"""Recent tokens to keep verbatim (not summarized).
Pi default: 20000."""

MIN_MESSAGES_TO_COMPACT = 8
"""Minimum conversation messages before compaction is meaningful."""

SUMMARIZATION_MAX_TOKENS = 1200
"""Max output tokens for the summarization LLM call."""

SUMMARIZATION_TIMEOUT = 60
"""Seconds before summarization API call times out."""

# ---------------------------------------------------------------------------
# Pi-style summarization prompts
# ---------------------------------------------------------------------------

SUMMARIZATION_SYSTEM_PROMPT = """You are a conversation summarizer for a coding agent session.
Create a structured context checkpoint summary that the agent will use to continue
work after old messages are removed from context.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate
into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use the same EXACT format as the previous summary."""


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------

class CompactionResult:
    """Result of a compaction attempt."""

    def __init__(self) -> None:
        self.did_compact: bool = False
        self.summary: str = ""
        self.messages_before: int = 0
        self.messages_after: int = 0
        self.tokens_before: int = 0
        self.tokens_after: int = 0
        self.previous_summary: str = ""
        self.reason: str = ""  # "threshold" or "manual"
        self.error: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_compact(
    token_count: int,
    context_window: int,
    reserve_tokens: int = RESERVE_TOKENS,
) -> bool:
    """Pi-style: check if compaction should trigger.

    Compaction fires when contextTokens > contextWindow - reserveTokens.
    Returns True if compaction is needed.
    """
    if context_window <= 0:
        return False
    threshold = context_window - reserve_tokens
    return token_count > threshold


def compact(
    messages: list[dict],
    context_window: int,
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    previous_summary: str = "",
    reserve_tokens: int = RESERVE_TOKENS,
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
    custom_instructions: str = "",
) -> CompactionResult:
    """Pi-style compaction: summarize old messages, keep recent ones.

    This is the main entry point. It:
    1. Checks if compaction is needed (context exceeds threshold)
    2. Finds the cut point (keep recent ~keep_recent_tokens tokens)
    3. Renders old messages to text
    4. Calls LLM to generate/update structured summary
    5. Rebuilds the message list: summary + recent messages
    6. Strips orphaned tool results
    7. Invalidates API message cache

    Args:
        messages: Conversation message list (mutated in place).
        context_window: Provider context window size (e.g., 1_000_000 for deepseek-v4).
        api_key: API key for summarizer (falls back to DEEPSEEK_API_KEY env).
        model: Model for summarizer (falls back to DEEPSEEK_MODEL env or "deepseek-chat").
        base_url: Base URL for summarizer (falls back to DEEPSEEK_BASE_URL env).
        previous_summary: Previous compaction summary for iterative updates.
        reserve_tokens: Tokens to reserve for response (default 16384).
        keep_recent_tokens: Recent tokens to keep verbatim (default 20000).
        custom_instructions: Optional instructions to focus the summary.

    Returns:
        CompactionResult with details about what happened.
    """
    result = CompactionResult()
    result.messages_before = len(messages)
    result.previous_summary = previous_summary

    # Estimate current token count
    token_count = _estimate_messages_tokens(messages)
    result.tokens_before = token_count

    # Check if compaction is needed
    if not should_compact(token_count, context_window, reserve_tokens):
        return result

    # Need enough messages to be meaningful
    if len(messages) < MIN_MESSAGES_TO_COMPACT:
        return result

    # Find cut point: keep recent ~keep_recent_tokens tokens
    cut_index = _find_cut_point(messages, keep_recent_tokens)
    if cut_index <= 2:
        # Not enough old messages to summarize (only system + first user)
        return result

    # Split: old messages to summarize, recent messages to keep
    # Always preserve the first 2 messages (system prompt + session header)
    prefix = messages[:2]
    middle = messages[2:cut_index]
    recent = messages[cut_index:]

    if len(middle) < 2:
        return result  # nothing meaningful to summarize

    # Track files mentioned in the region being summarized
    read_files, modified_files = _extract_files(middle)

    # Generate summary via LLM
    summary = ""
    try:
        summary = _summarize(
            middle,
            api_key=api_key,
            model=model,
            base_url=base_url,
            previous_summary=previous_summary,
            custom_instructions=custom_instructions,
        )
    except Exception as e:
        _log.warning("pi_compaction: summarization failed: %s", e)
        result.error = str(e)
        # Fall back to simple message-count note
        summary = (
            f"[{len(middle)} earlier messages were compacted. "
            f"They covered file operations and conversation context. "
            f"Ask the user if you need details that are no longer visible.]"
        )

    # Append file lists to summary (pi-style)
    if read_files or modified_files:
        summary += "\n\n"
        if read_files:
            summary += "<read-files>\n"
            for f in sorted(read_files)[:20]:
                summary += f"{f}\n"
            summary += "</read-files>\n"
        if modified_files:
            summary += "<modified-files>\n"
            for f in sorted(modified_files)[:20]:
                summary += f"{f}\n"
            summary += "</modified-files>\n"

    # Rebuild message list: prefix + summary + recent
    compacted: list[dict] = []
    compacted.extend(prefix)
    compacted.append({
        "role": "user",
        "content": (
            "[COMPACTION SUMMARY — earlier conversation was summarized to save context space]\n"
            "The following is a structured summary of what happened earlier. "
            "Use it to maintain context without the full message history.\n\n"
            + summary
        ),
    })
    compacted.extend(recent)

    # Strip orphaned tool results (tool_call_id no longer exists)
    _strip_orphaned_tool_results(compacted)

    # Replace messages in place
    messages.clear()
    messages.extend(compacted)

    # Invalidate API message-cleaning cache
    try:
        from api import clear_api_cache
        clear_api_cache()
    except Exception:
        pass

    result.did_compact = True
    result.summary = summary
    result.messages_after = len(messages)
    result.tokens_after = _estimate_messages_tokens(messages)
    result.reason = "threshold"

    _log.info(
        "pi_compaction: %d -> %d messages, %d -> %d tokens (saved ~%d tokens)",
        result.messages_before, result.messages_after,
        result.tokens_before, result.tokens_after,
        result.tokens_before - result.tokens_after,
    )

    return result


def compact_manual(
    messages: list[dict],
    custom_instructions: str = "",
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    context_window: int = 1_000_000,
) -> CompactionResult:
    """Manual compaction via /compact command. Summarizes ALL old messages.

    Used when the user explicitly asks to compact context.
    """
    result = CompactionResult()
    result.messages_before = len(messages)
    result.reason = "manual"

    if len(messages) < MIN_MESSAGES_TO_COMPACT:
        return result

    # Keep only the last ~KEEP_RECENT_TOKENS tokens
    cut_index = _find_cut_point(messages, KEEP_RECENT_TOKENS)
    if cut_index <= 2:
        return result

    prefix = messages[:2]
    middle = messages[2:cut_index]
    recent = messages[cut_index:]

    if len(middle) < 2:
        return result

    read_files, modified_files = _extract_files(middle)

    try:
        summary = _summarize(
            middle,
            api_key=api_key,
            model=model,
            base_url=base_url,
            custom_instructions=custom_instructions,
        )
    except Exception as e:
        _log.warning("pi_compaction manual: summarization failed: %s", e)
        summary = (
            f"[{len(middle)} earlier messages were compacted.]"
        )

    if read_files or modified_files:
        summary += "\n\n"
        if read_files:
            summary += "<read-files>\n"
            for f in sorted(read_files)[:20]:
                summary += f"{f}\n"
            summary += "</read-files>\n"
        if modified_files:
            summary += "<modified-files>\n"
            for f in sorted(modified_files)[:20]:
                summary += f"{f}\n"
            summary += "</modified-files>\n"

    compacted: list[dict] = []
    compacted.extend(prefix)
    compacted.append({
        "role": "user",
        "content": (
            "[COMPACTION SUMMARY — manual /compact]\n\n"
            + summary
        ),
    })
    compacted.extend(recent)

    _strip_orphaned_tool_results(compacted)

    messages.clear()
    messages.extend(compacted)

    try:
        from api import clear_api_cache
        clear_api_cache()
    except Exception:
        pass

    result.did_compact = True
    result.summary = summary
    result.messages_after = len(messages)
    result.tokens_after = _estimate_messages_tokens(messages)
    return result


def get_compaction_context_info(messages: list[dict]) -> dict[str, Any]:
    """Return context usage info for UI display (pi-style footer).

    Returns dict with tokens, context_window, percent, and compaction_needed.
    """
    tokens = _estimate_messages_tokens(messages)
    return {
        "tokens": tokens,
        "needs_compaction": False,  # caller should check with should_compact()
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens in a list of messages (chars/4 heuristic)."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "")))
        # Tool calls
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            total += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return total // 4  # conservative: 4 chars per token


def _find_cut_point(messages: list[dict], keep_recent_tokens: int) -> int:
    """Find the message index to cut at, keeping ~keep_recent_tokens tokens.

    Walks backwards from the end, accumulating estimated token counts.
    Returns the index of the first message to KEEP (everything before
    will be summarized).

    Never cuts after a tool message (must stay with tool call) or
    before index 2 (preserves system + first user).
    """
    accumulated = 0
    # Walk backwards
    for i in range(len(messages) - 1, 1, -1):
        m = messages[i]
        # Estimate this message's tokens
        content = m.get("content", "")
        if isinstance(content, str):
            msg_tokens = len(content) // 4
        else:
            msg_tokens = len(str(content)) // 4
        accumulated += msg_tokens

        # Stop when we have enough recent context
        if accumulated >= keep_recent_tokens:
            # Don't cut at a tool message — tool must stay with its call
            cut = i
            while cut < len(messages) and messages[cut].get("role") == "tool":
                cut += 1
            return min(cut, len(messages))

    # Keep everything — not enough tokens to trim
    return len(messages)


def _extract_files(messages: list[dict]) -> tuple[set[str], set[str]]:
    """Extract read and modified file paths from a list of messages.

    Looks at tool calls for read_file, write_file, edit_file, git_add.
    """
    read_files: set[str] = set()
    modified_files: set[str] = set()

    for m in messages:
        role = m.get("role", "")
        if role == "assistant":
            for tc in m.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                path = args.get("path", args.get("file_path", ""))
                if not path:
                    continue
                if name in ("read_file", "get_file_skeleton", "get_function"):
                    read_files.add(path)
                elif name in ("write_file", "edit_file", "edit_lines", "replace_symbol"):
                    modified_files.add(path)
                elif name == "git_add":
                    paths = args.get("paths", "")
                    if paths and paths != "all":
                        for p in str(paths).split(","):
                            modified_files.add(p.strip())
        elif role == "tool":
            # Check tool name from content (fallback)
            pass

    return read_files, modified_files


def _render_transcript(messages: list[dict]) -> str:
    """Render messages into a compact transcript for the summarizer LLM.

    Pi-style serialization: role-prefixed plain text, tool results truncated.
    """
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")

        if role == "system":
            lines.append(f"[System]: {str(content)[:500]}")
        elif role == "user":
            lines.append(f"[User]: {str(content)[:1000]}")
        elif role == "assistant":
            tool_calls = m.get("tool_calls", [])
            thinking = m.get("thinking", "")
            if thinking:
                lines.append(f"[Assistant thinking]: {str(thinking)[:300]}")
            if content:
                lines.append(f"[Assistant]: {str(content)[:500]}")
            if tool_calls:
                tc_descs = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = fn.get("arguments", "{}")
                    try:
                        args_str = str(args)[:200]
                    except Exception:
                        args_str = "..."
                    tc_descs.append(f"{name}({args_str})")
                lines.append(f"[Assistant tool calls]: {'; '.join(tc_descs)}")
        elif role == "tool":
            # Truncate tool results to 2000 chars
            c = str(content)
            if len(c) > 2000:
                c = c[:1000] + f"\n... [truncated, ~{len(c)} chars total] ...\n" + c[-500:]
            lines.append(f"[Tool result]: {c}")
        else:
            lines.append(f"[{role}]: {str(content)[:500]}")

    return "\n".join(lines)


def _summarize(
    messages: list[dict],
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    previous_summary: str = "",
    custom_instructions: str = "",
) -> str:
    """Call the LLM to generate a structured summary of messages.

    Uses the pi-style structured format. If previous_summary is provided,
    uses the UPDATE_SUMMARIZATION_PROMPT to merge new information.
    """
    import os as _os

    if not api_key:
        api_key = _os.environ.get("DEEPSEEK_API_KEY", "")
    if not model:
        model = _os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if not base_url:
        base_url = _os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    if not api_key:
        raise RuntimeError("No API key available for pi-compaction summarization")

    # Build the transcript
    transcript = _render_transcript(messages)

    # Choose prompt: initial or update
    if previous_summary.strip():
        base_prompt = UPDATE_SUMMARIZATION_PROMPT
        if custom_instructions:
            base_prompt += f"\n\nAdditional focus: {custom_instructions}"
        user_content = (
            f"<conversation>\n{transcript}\n</conversation>\n\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            f"{base_prompt}"
        )
    else:
        base_prompt = SUMMARIZATION_SYSTEM_PROMPT
        if custom_instructions:
            base_prompt += f"\n\nAdditional focus: {custom_instructions}"
        user_content = (
            f"<conversation>\n{transcript}\n</conversation>\n\n"
            f"{base_prompt}"
        )

    try:
        import requests as _requests
    except ImportError:
        raise RuntimeError("requests library not available for pi-compaction")

    resp = _requests.post(
        f"{base_url}/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": SUMMARIZATION_MAX_TOKENS,
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=SUMMARIZATION_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    summary = data["choices"][0]["message"]["content"].strip()

    # Remove any "Output ONLY the summary text" preamble the model might echo
    if summary.startswith("Output ONLY"):
        nl = summary.find("\n")
        if nl > 0:
            summary = summary[nl + 1:]

    return summary


def _strip_orphaned_tool_results(messages: list[dict]) -> int:
    """Remove tool messages whose tool_call_id doesn't exist in any
    assistant message. Returns count stripped."""
    valid_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []) or []:
                tid = tc.get("id")
                if tid:
                    valid_ids.add(tid)

    if not valid_ids:
        return 0

    stripped = 0
    keep = []
    for m in messages:
        if m.get("role") == "tool":
            if m.get("tool_call_id") not in valid_ids:
                stripped += 1
                continue
        keep.append(m)

    if stripped:
        messages.clear()
        messages.extend(keep)

    return stripped
