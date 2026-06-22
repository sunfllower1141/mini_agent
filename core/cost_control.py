#!/usr/bin/env python3
"""
cost_control.py -- Cost control via proactive context management.

Reasonix Pillar 3: Cost Control.

Three-tier architecture:

1. TIER 1 (FREE): Prune stale tool results -- prune_stale_tool_results()
   Replaces old tool results with compact placeholders. If this alone
   drops context below the hard threshold, the expensive compaction is
   skipped entirely.  (Reasonix compact.go:maybeCompact pattern.)

2. TIER 2 (FREE): Tool-result truncation -- compact_tool_results_at_turn_end()
   Truncates oversized tool results at turn boundary.

3. TIER 3 (PAID): LLM summarization -- only when tiers 1+2 are exhausted.

Multi-zone thresholds (Reasonix compact.go:25-35):
  - SOFT  (50%): context growing -- emit warning only
  - HARD  (80%): prune first + compact if still above threshold
  - FORCE (90%): prune + compact regardless of economic gate

Also: dead-tool pruning, model escalation, budget hard-stop.
"""

from __future__ import annotations

from typing import Any

from core.compaction import (
    should_compact,
    compact_tool_results_at_turn_end,
    append_compaction_summary,
    prune_stale_tool_results,
    PruneStats,
    fold_economics,
    estimate_context_tokens,
    COMPACTION_RATIO_HARD,
    COMPACTION_RATIO_SOFT,
)
from memory.memory_prune import _estimate_tokens

# --- Model escalation ---


def should_escalate_model(
    config: Any,
    turn_failures: list[bool],
    max_failures: int = 2,
    window_turns: int = 3,
) -> str | None:
    """Return the escalated model name, or None if escalation not needed.

    If the model has failed N times in the last M turns, escalate
    from flash → pro (or pro → auto, etc.).

    turn_failures: list of bools (True = failure) in chronological order.
    """
    recent = turn_failures[-window_turns:] if len(turn_failures) > window_turns else turn_failures
    failures = sum(1 for f in recent if f)
    if failures < max_failures:
        return None

    current = getattr(config, "model", "")
    if not current:
        return None

    # Escalate: flash → pro → auto (deepseek provider)
    if "flash" in current:
        return current.replace("flash", "pro")
    if "pro" in current and "auto" not in current:
        return current.replace("pro", "auto")

    return None  # already at highest tier, no further escalation


# --- Compact-if-needed ---


def compact_if_needed(
    messages: list[dict],
    config: Any,
    *,
    force: bool = False,
) -> int:
    """Compact context using the 3-tier prune-before-compact pipeline.

    Called at turn boundaries (after tool execution, before next API call).

    Pipeline:
      1. TIER 1 (FREE): prune_stale_tool_results() -- replace old tool results
         with compact placeholders.
      2. Re-check threshold: if pruning dropped it below hard, skip compaction.
      3. TIER 2 (FREE): compact_tool_results_at_turn_end() -- truncate oversized.
      4. TIER 3 (PAID): LLM summarization -- only if still above threshold AND
         the economic gate passes (foldable region >= 400 tokens).

    force=True skips the economic gate (for 'force' level and manual /compact).

    Returns number of messages compacted (not counting pruned).
    """
    context_limit = getattr(config, "context_limit", 128_000) or 128_000
    token_count = estimate_context_tokens(messages)

    level = should_compact(token_count, context_limit)
    if level is None and not force:
        return 0

    total_compacted = 0

    # --- TIER 1: Prune stale tool results (FREE) ---
    # Reasonix pattern: prune before compact. If pruning alone clears the
    # trigger, skip the expensive summarization entirely.
    if level is not None or force:
        prune_stats = prune_stale_tool_results(messages)
        if prune_stats.results > 0:
            # Re-estimate after pruning
            new_count = estimate_context_tokens(messages)
            # If pruning dropped us below the hard threshold, skip compaction
            new_level = should_compact(new_count, context_limit)
            if new_level not in ("hard", "force") and not force:
                return prune_stats.results  # pruned, nothing more needed
            token_count = new_count  # use updated count for rest of pipeline

    # --- TIER 2: Turn-end tool-result truncation (always safe) ---
    compacted = compact_tool_results_at_turn_end(messages)
    total_compacted += compacted

    # Re-check after truncation
    if level != "force" and not force:
        token_count = estimate_context_tokens(messages)
        if should_compact(token_count, context_limit) not in ("hard", "force"):
            return total_compacted

    # --- TIER 3: Emergency/hard compaction (PAID -- LLM summarization) ---
    # Only do paid summarization for 'hard', 'force', or forced compaction
    if not force and level != "hard" and level != "force":
        return total_compacted

    # Collapse old conversation turns (before the last 4) into a summary
    # appended at the END.  Never rewrites the prefix.
    if not messages:
        return total_compacted
    if messages[0].get("role") != "system":
        return total_compacted

    system_msg = messages[0]
    rest = messages[1:]

    # Find last 4 assistant messages (turn boundaries)
    assistant_indices = [
        i for i, m in enumerate(rest)
        if m.get("role") == "assistant"
    ]
    if len(assistant_indices) <= 4:
        return total_compacted

    # Estimate foldable tokens for economic gate
    keep_from = assistant_indices[-4]
    old = rest[:keep_from]
    foldable_tokens = sum(_estimate_tokens(m) for m in old)

    # Economic gate: skip if not worth the API call (unless forced)
    if not force and level != "force" and not fold_economics(foldable_tokens):
        return total_compacted

    kept = rest[keep_from:]

    if not old:
        return total_compacted

    # Summarize old turns
    turn_count = len([m for m in old if m.get("role") == "assistant"])
    summary = (
        f"[CONTEXT COMPACTION -- {turn_count} earlier turns summarized]\n"
        f"The conversation prefix is unchanged.  Key context from earlier turns:\n\n"
    )
    # Extract key info: tool names called
    tools_called: set[str] = set()
    for m in old:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []):
                tools_called.add(tc.get("function", {}).get("name", "?"))
    if tools_called:
        summary += f"Tools used: {', '.join(sorted(tools_called))}\n"

    # Rebuild messages: system + compacted summary + recent
    messages[:] = [system_msg] + [{"role": "user", "content": summary}] + kept
    total_compacted += len(old)

    return total_compacted


# ---------------------------------------------------------------------------
# Budget hard-stop
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Raised when the estimated API cost exceeds the configured budget limit."""

    def __init__(self, estimated_cost: float, budget_limit: float) -> None:
        self.estimated_cost = estimated_cost
        self.budget_limit = budget_limit
        super().__init__(
            f"Budget exceeded: estimated ${estimated_cost:.4f} > "
            f"limit ${budget_limit:.2f}"
        )


# Thread-safe accumulator for estimated cost.
# Keyed by thread ID so sub-agents don't interfere with the parent.
import threading as _threading
_budget_accumulator: dict[int, float] = {}
_budget_lock = _threading.Lock()


def _get_provider_pricing(config: Any) -> tuple[float, float]:
    """Return (input_price_per_1M, output_price_per_1M) for the current provider.

    Falls back to DeepSeek V4 Pro pricing if the provider is unknown.
    """
    provider = getattr(config, "api_provider", "deepseek")
    try:
        from core.config import PROVIDER_DEFAULTS
        pd = PROVIDER_DEFAULTS.get(provider)
        if pd:
            return pd.input_price, pd.output_price
    except Exception:
        pass
    # Fallback: DeepSeek V4 Pro promo pricing
    return 0.435, 0.87


def reset_budget_tracker() -> None:
    """Reset the budget accumulator for the current thread."""
    tid = _threading.get_ident()
    with _budget_lock:
        _budget_accumulator.pop(tid, None)


def track_api_cost(input_tokens: int, output_tokens: int, config: Any) -> float:
    """Estimate and accumulate the cost of one API call.

    Args:
        input_tokens: number of prompt tokens sent.
        output_tokens: number of completion tokens received.
        config: AgentConfig (or mock) with api_provider field.

    Returns:
        The estimated cost of this call in USD.
    """
    input_price, output_price = _get_provider_pricing(config)
    cost = (input_tokens / 1_000_000) * input_price + \
          (output_tokens / 1_000_000) * output_price
    tid = _threading.get_ident()
    with _budget_lock:
        _budget_accumulator[tid] = _budget_accumulator.get(tid, 0.0) + cost
    return cost


def check_budget_limit(config: Any) -> None:
    """Check if the accumulated cost exceeds the configured budget limit.

    Raises ``BudgetExceeded`` if the limit (in USD) is exceeded.
    No-op when ``config.budget_limit`` is 0 (disabled).

    Call this at turn boundaries (before making the next API call)
    to prevent runaway agent loops.
    """
    budget_limit = getattr(config, "budget_limit", 0.0) or 0.0
    if budget_limit <= 0.0:
        return
    tid = _threading.get_ident()
    with _budget_lock:
        accumulated = _budget_accumulator.get(tid, 0.0)
    if accumulated > budget_limit:
        raise BudgetExceeded(accumulated, budget_limit)
