#!/usr/bin/env python3
"""
cost_control.py -- Cost control via proactive context management.

Reasonix Pillar 3: Cost Control.

Three mechanisms:

1. Proactive compaction at 40% context ratio, emergency at 80%.
   Compacts tool results and folds old turns before the context
   window fills up, preventing expensive truncation.

2. Dead-tool pruning: after N turns, remove MCP/skill tools that
   were never called.  Shrinks the tool spec payload for future
   turns.  The prefix must be re-established if tool specs change.

3. Model escalation: start with v4-flash, escalate to v4-pro only
   on repeated failures (2+ failures in 3 turns).

Also provides a compact_if_needed() function that can be called
at turn boundaries.

Budget hard-stop: ``check_budget_limit()`` estimates cumulative cost
from token counts and raises ``BudgetExceeded`` when the configured
``budget_limit`` (in USD) is exceeded.  This prevents runaway agent
loops from draining API balances.
"""

from __future__ import annotations

from typing import Any

from core.compaction import (
    should_compact,
    compact_tool_results_at_turn_end,
    append_compaction_summary,
    COMPACTION_RATIO_EMERGENCY,
)
from memory.memory_prune import _estimate_tokens


# --- Dead-tool pruning ---

DEAD_TOOL_PRUNE_TURN = 6
"""Number of turns before checking for unused tools to prune."""

DEAD_TOOL_RESET_TURN = 4
"""If all tools were called in the last N turns, skip pruning."""


def prune_dead_tools(
    active_tools: list[dict],
    called_tool_names: set[str],
    turn_count: int,
    preserve_tools: set[str] | None = None,
) -> tuple[list[dict], set[str]]:
    """Remove tools that have never been called after DEAD_TOOL_PRUNE_TURN turns.

    Returns (pruned_tools, pruned_names).  The caller must re-establish
    the prefix if tools change (fingerprint changes → cache invalidated).

    preserve_tools: set of tool names that should never be pruned
        (e.g. read_file, write_file, run_shell, plan, todo_write, etc.)
    """
    if turn_count < DEAD_TOOL_PRUNE_TURN:
        return active_tools, set()

    if preserve_tools is None:
        preserve_tools = {
            "read_file", "write_file", "edit_file", "run_shell",
            "search_files", "find_symbol", "list_directory", "file_info",
            "web_search", "todo_write", "todo_read",
            "plan", "plan_status", "memory_core", "remember",
            "write_scratchpad", "session_search",
        }

    pruned: set[str] = set()
    kept: list[dict] = []

    for tool in active_tools:
        name = tool.get("function", {}).get("name", "")
        if name in preserve_tools:
            kept.append(tool)
        elif name in called_tool_names:
            kept.append(tool)
        else:
            pruned.add(name)

    if pruned:
        import logging
        _log = logging.getLogger("cost_control")
        _log.info(
            "dead_tool_prune: removed %d unused tools (turn %d): %s",
            len(pruned), turn_count, sorted(pruned),
        )

    return kept, pruned


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
    """Compact context if the token ratio exceeds thresholds.

    Called at turn boundaries (after tool execution, before next API call).

    Returns number of messages compacted.
    """
    context_limit = getattr(config, "context_limit", 128_000) or 128_000
    token_count = sum(_estimate_tokens(m) for m in messages)

    level = should_compact(token_count, context_limit)
    if level is None and not force:
        return 0

    # Tool result compaction (always safe, always append-only)
    compacted = compact_tool_results_at_turn_end(messages)

    # Emergency: aggressive compaction of old turns
    if level == "emergency" or force:
        compacted += _emergency_compact(messages, context_limit)

    return compacted


def _emergency_compact(messages: list[dict], context_limit: int) -> int:
    """Aggressive compaction for emergency situations (>80% context).

    Collapses old conversation turns (before the last 4) into a summary
    message appended at the END.  Never rewrites the prefix.
    """
    # Find the system message (index 0) — preserve it
    if not messages:
        return 0
    if messages[0].get("role") != "system":
        return 0  # no system message to anchor prefix

    # Keep: system (index 0) + last 4 turns worth of messages
    # A "turn" = assistant + tool messages
    system_msg = messages[0]
    rest = messages[1:]

    # Find last 4 assistant messages (turn boundaries)
    assistant_indices = [
        i for i, m in enumerate(rest)
        if m.get("role") == "assistant"
    ]
    if len(assistant_indices) <= 4:
        return 0  # not enough turns to compact

    # Keep last 4 turns
    keep_from = assistant_indices[-4]
    old = rest[:keep_from]
    kept = rest[keep_from:]

    if not old:
        return 0

    # Summarize old turns
    turn_count = len([m for m in old if m.get("role") == "assistant"])
    summary = (
        f"[CONTEXT COMPACTION — {turn_count} earlier turns summarized]\n"
        f"The conversation prefix is unchanged.  Key context from earlier turns:\n\n"
    )
    # Extract key info: tool names called, files read/written
    tools_called: set[str] = set()
    for m in old:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []):
                tools_called.add(tc.get("function", {}).get("name", "?"))
    if tools_called:
        summary += f"Tools used: {', '.join(sorted(tools_called))}\n"

    # Rebuild messages: system + compacted summary + recent
    messages[:] = [system_msg] + [{"role": "user", "content": summary}] + kept

    return len(old)


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
