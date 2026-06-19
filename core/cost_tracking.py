#!/usr/bin/env python3
"""cost_tracking.py -- per-turn and session cost accounting.

Port of Reasonix src/telemetry/stats.ts DEEPSEEK_PRICING +
SessionStats cost accumulation.

Prices in USD per 1M tokens (matching Reasonix as of 2026-06).
Display currency conversion happens at the UI boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# DeepSeek V4 pricing (USD per 1M tokens)
# Aligned with Reasonix src/telemetry/stats.ts commit 159c500
# ---------------------------------------------------------------------------
DEEPSEEK_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {
        "inputCacheHit": 0.003625,
        "inputCacheMiss": 0.435,
        "output": 0.87,
    },
    "deepseek-v4-flash": {
        "inputCacheHit": 0.0028,
        "inputCacheMiss": 0.14,
        "output": 0.28,
    },
    # Compat aliases — priced as v4-flash per deprecation notice
    "deepseek-chat": {
        "inputCacheHit": 0.0028,
        "inputCacheMiss": 0.14,
        "output": 0.28,
    },
    "deepseek-reasoner": {
        "inputCacheHit": 0.0028,
        "inputCacheMiss": 0.14,
        "output": 0.28,
    },
    # OpenRouter models (cost tracking for session_stats)
    "xiaomi/mimo-v2.5-pro": {
        "inputCacheHit": 0.435,  # MiMo V2.5 Pro: no cache-hit discount via OpenRouter
        "inputCacheMiss": 0.435,  # $0.435 / 1M input tokens
        "output": 0.87,           # $0.87 / 1M output tokens
    },
}

# Fallback for unknown models — use v4-flash pricing
_DEFAULT_PRICING = DEEPSEEK_PRICING["deepseek-v4-flash"]


def _get_pricing(model: str) -> dict[str, float]:
    """Get pricing for a model, falling back to flash defaults."""
    return DEEPSEEK_PRICING.get(model, _DEFAULT_PRICING)


@dataclass
class TurnCost:
    """Cost breakdown for a single turn."""
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class SessionCost:
    """Session-cumulative cost tracking.

    Thread-safe: all mutations are on the instance, controlled by
    the caller (single-threaded agent loop).
    """
    turn_count: int = 0
    total_cost: float = 0.0
    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    last_turn: TurnCost | None = None

    def record_turn(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> TurnCost:
        """Record a turn's usage and return the cost breakdown."""
        pricing = _get_pricing(model)

        # Cost = (cache-hit tokens / 1M) * cache-hit price
        #      + (cache-miss tokens / 1M) * cache-miss price
        #      + (completion tokens / 1M) * output price
        #
        # When cache breakdown is available, use it for accurate pricing.
        # When unavailable (e.g. OpenRouter, older APIs), fall back to
        # charging all prompt tokens at the miss rate.
        if cache_hit_tokens + cache_miss_tokens > 0:
            input_cost = (
                (cache_hit_tokens / 1_000_000) * pricing["inputCacheHit"]
                + (cache_miss_tokens / 1_000_000) * pricing["inputCacheMiss"]
            )
            # Safety: any prompt tokens not covered by cache breakdown
            # (shouldn't happen, but be defensive)
            uncategorized = max(0, prompt_tokens - cache_hit_tokens - cache_miss_tokens)
            if uncategorized > 0:
                input_cost += (uncategorized / 1_000_000) * pricing["inputCacheMiss"]
        else:
            # No cache breakdown -- charge all prompt tokens at miss rate
            input_cost = (prompt_tokens / 1_000_000) * pricing["inputCacheMiss"]
        output_cost = (completion_tokens / 1_000_000) * pricing["output"]
        total = input_cost + output_cost

        tc = TurnCost(
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=total,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        self.turn_count += 1
        self.total_cost += total
        self.total_cache_hit_tokens += cache_hit_tokens
        self.total_cache_miss_tokens += cache_miss_tokens
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.last_turn = tc

        return tc

    @property
    def cache_hit_rate(self) -> float | None:
        """Session-aggregate cache hit rate (0.0-1.0), or None if no data."""
        total_cache = self.total_cache_hit_tokens + self.total_cache_miss_tokens
        if total_cache == 0:
            return None
        return self.total_cache_hit_tokens / total_cache

    @property
    def last_cache_hit_rate(self) -> float | None:
        """Last turn's cache hit rate, or None if no data."""
        if self.last_turn is None:
            return None
        total = self.last_turn.cache_hit_tokens + self.last_turn.cache_miss_tokens
        if total == 0:
            return None
        return self.last_turn.cache_hit_tokens / total


def format_cost(amount: float | None, currency: str = "CNY") -> str:
    """Format a cost amount for display.

    Color thresholds (matching Reasonix):
      green  < $0.05, $0.05-0.20 yellow, >= $0.20 red
    """
    if amount is None or amount <= 0:
        return "-"
    symbol = "\u00a5" if currency.upper() in ("CNY", "RMB") else "$"
    if amount < 1:
        return f"{symbol}{amount:.4f}"
    return f"{symbol}{amount:.2f}"


def format_cost_usd(amount: float | None) -> str:
    """Format cost in USD with $ symbol (DeepSeek pricing is in USD)."""
    return format_cost(amount, "USD")


# Backwards-compatible alias — prefer format_cost_usd for clarity
format_cost_cny = format_cost_usd
