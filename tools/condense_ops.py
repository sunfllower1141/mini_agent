#!/usr/bin/env python3
"""
condense_ops.py -- Conversation condensation tool for mini_agent.

Implements Dirac's 'condense' tool pattern: the LLM can proactively
request context compression when it notices the conversation getting
too long.  The tool compacts oversized tool results and summarizes
older conversation turns to free up context window space.

Also provides proactive/emergency condensation thresholds (Dirac's
ContextManager pattern).
"""

from __future__ import annotations

from typing import Any

from core.compaction import (
    compact_tool_results_at_turn_end,
    prune_stale_tool_results,
    should_compact,
    estimate_context_tokens,
    COMPACTION_RATIO_SOFT,
    COMPACTION_RATIO_HARD,
    COMPACTION_RATIO_FORCE,
    TURN_END_RESULT_CAP_TOKENS,
    TURN_END_RESULT_CAP_CHARS,
)
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from core.safety import ReadSafetyGate, WriteSafetyGate


# ---------------------------------------------------------------------------
# condense tool
# ---------------------------------------------------------------------------

@_register("condense")
def _condense(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate,
              on_output=None, approve_callback=None, cancel_event=None) -> ToolResult:
    """Proactively condense the conversation to free up context window space."""
    context_note = args.get("context", "")

    # Get the current messages from the orchestrator
    messages = getattr(_TOOL_CONTEXT, "_current_messages", None)
    if messages is None:
        return ToolResult(
            success=False,
            content="Cannot condense: no active conversation messages available."
        )

    # Estimate current token usage
    estimated_tokens = estimate_context_tokens(messages)

    # TIER 1: Prune stale tool results (FREE)
    prune_stats = prune_stale_tool_results(messages)

    # TIER 2: Compact oversized tool results
    compacted = compact_tool_results_at_turn_end(messages)

    # Check if we should do deeper compaction
    context_limit = 128000  # Default for many models
    new_tokens = estimate_context_tokens(messages)
    need = should_compact(new_tokens, context_limit)

    lines = []
    lines.append(f"Condensation complete.")
    lines.append(f"  Estimated tokens before: ~{estimated_tokens}")
    if prune_stats.results > 0:
        lines.append(f"  Pruned tool results: {prune_stats.results}"
                     f" (~{prune_stats.estimated_tokens_saved} tokens saved)")
    lines.append(f"  Compacted tool results: {compacted}")
    lines.append(f"  Estimated tokens after: ~{new_tokens}")

    if need:
        lines.append(f"  Context ratio: {new_tokens / context_limit:.1%}")
        lines.append(f"  Recommendation: {need} compaction recommended")
        if need in ("hard", "force"):
            lines.append(
                f"  Consider summarizing older conversation turns or "
                f"requesting a fresh session."
            )

    if context_note:
        lines.append(f"  Note: {context_note}")

    # Reset tool cache to reflect cleanup
    from tools import clear_tool_cache
    clear_tool_cache()

    return ToolResult(success=True, content="\n".join(lines))


@_summarize("condense")
def _condense_summary(args: dict) -> str:
    ctx = args.get("context", "")
    preview = ctx[:60] if ctx else "no context"
    return f"condense({preview})"


# ---------------------------------------------------------------------------
# Proactive condensation hook
# ---------------------------------------------------------------------------

def should_auto_condense(messages: list[dict], context_limit: int = 128000) -> str | None:
    """
    Check if the conversation should trigger automatic condensation.

    Called before each API call by the orchestrator.  Returns:
      - 'soft': context is filling up, consider compacting
      - 'hard': context is critical, prune + compact needed
      - 'force': context emergency, compact regardless of economics
      - None: no condensation needed

    This mirrors Dirac's ContextManager.shouldCompactContextWindow().
    """
    estimated_tokens = estimate_context_tokens(messages)
    return should_compact(estimated_tokens, context_limit)


def inject_condensation_warning(
    messages: list[dict],
    context_limit: int = 128000,
) -> bool:
    """
    Inject a condensation warning into the messages list if context is tight.

    Returns True if a warning was injected.

    Called by the orchestrator before each API call.
    """
    need = should_auto_condense(messages, context_limit)
    if not need:
        return False

    estimated_tokens = estimate_context_tokens(messages)
    ratio = estimated_tokens / context_limit if context_limit > 0 else 0

    if need == "force":
        warning = (
            f"CRITICAL: Context window is {ratio:.0%} full (~{estimated_tokens}/{context_limit} tokens). "
            f"Context MUST be freed before continuing. "
            f"Use the condense tool immediately."
        )
    elif need == "hard":
        warning = (
            f"URGENT: Context window is {ratio:.0%} full (~{estimated_tokens}/{context_limit} tokens). "
            f"Use the condense tool NOW to free up space before continuing. "
            f"Summarize older turns or compact large tool results."
        )
    else:  # soft
        warning = (
            f"Note: Context window is {ratio:.0%} full (~{estimated_tokens}/{context_limit} tokens). "
            f"Consider using the condense tool to free up space."
        )

    messages.append({
        "role": "user",
        "content": warning,
        "_transient": True,
    })
    return True
