"""
semantic_cache.py -- two-tier response cache for mini_agent.

Tier 1 (exact match): O(1) SHA-256 hash lookup -- identical queries (common
  with sub-agents and retries) hit in ~0ms with zero embedding cost.
Tier 2 (semantic): cosine similarity against cached embeddings using the
  shared SentenceTransformer model -- catches paraphrased queries.

On cache hit, the response is returned directly, bypassing the LLM API
call entirely.  Uses the same model (CodeSearchNet) already loaded by
search_ops.py, so no additional memory footprint.

Cache entries expire after TTL seconds (default: 1 hour) and the cache
is bounded to MAX_ENTRIES to prevent unbounded memory growth.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import numpy as np

from logging_setup import get_logger

_log = get_logger("semantic_cache")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SIMILARITY_THRESHOLD: float = 0.92  # cosine similarity >= this -> cache hit
MAX_ENTRIES: int = 128                       # max cache entries (LRU eviction)
DEFAULT_TTL_SECONDS: int = 3600              # 1 hour TTL
MIN_QUERY_LENGTH: int = 10                   # don't cache queries shorter than this

# Adaptive threshold tuning (per-entry, inspired by vCache)
ADAPTIVE_THRESHOLD_MIN: float = 0.75          # floor — never go below this
ADAPTIVE_THRESHOLD_DECAY: float = 0.005       # reduction per successful verified hit
ADAPTIVE_THRESHOLD_PENALTY: float = 0.03      # increase on false positive feedback


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

class CacheEntry:
    """A single cached LLM response with metadata and adaptive threshold."""
    __slots__ = (
        "query_hash", "embedding", "response", "model", "created_at",
        "input_tokens", "output_tokens", "cost_saved",
        "hit_count",          # number of times this entry was served
        "false_positives",    # number of times feedback said this was wrong
        "adaptive_threshold", # per-entry threshold (starts at global default)
    )

    def __init__(
        self,
        query_hash: str,
        embedding: np.ndarray,
        response: dict,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self.query_hash = query_hash
        self.embedding = embedding
        self.response = response
        self.model = model
        self.created_at = time.time()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        # Estimated cost saved by serving from cache (set after creation)
        self.cost_saved: float = 0.0
        # Adaptive threshold tracking
        self.hit_count: int = 0
        self.false_positives: int = 0
        self.adaptive_threshold: float = threshold


# ---------------------------------------------------------------------------
# Two-tier (exact + semantic) cache
# ---------------------------------------------------------------------------

class SemanticCache:
    """Thread-safe two-tier response cache.

    Tier 1 (exact match): O(1) hash dict lookup -- catches identical
    queries with zero embedding cost.  Tier 2 (semantic): batched
    cosine similarity via NumPy -- catches paraphrased queries.

    On cache hit, the response is returned immediately.  On miss, the
    caller makes the API call and stores the result.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_entries: int = MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._threshold = threshold
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: list[CacheEntry] = []
        self._embeddings: np.ndarray | None = None  # cached matrix: (N, D)
        self._exact_cache: dict[str, CacheEntry] = {}  # Tier 1: hash -> entry
        self._lock = threading.Lock()
        self._hits: int = 0
        self._exact_hits: int = 0
        self._semantic_hits: int = 0
        self._misses: int = 0
        self._total_saved: float = 0.0  # estimated USD saved
        # Adaptive threshold tracking
        self._feedback_positive: int = 0   # times feedback said "correct"
        self._feedback_negative: int = 0   # times feedback said "wrong"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, query_text: str) -> tuple[dict | None, float]:
        """Check if *query_text* matches a cached query.

        Returns ``(cached_response, similarity)`` on hit, or ``(None, 0.0)``
        on miss.  Tier 1 exact match returns similarity=1.0; Tier 2
        semantic returns the cosine similarity score.
        """
        if len(query_text) < MIN_QUERY_LENGTH:
            self._misses += 1
            return None, 0.0

        query_hash = self._hash_text(query_text)

        # --- Tier 1: exact hash match (O(1), no embedding cost) ---
        with self._lock:
            self._evict_expired()
            exact_entry = self._exact_cache.get(query_hash)
            if exact_entry is not None:
                self._hits += 1
                self._exact_hits += 1
                self._total_saved += exact_entry.cost_saved
                _log.info(
                    "cache_hit_exact query_hash=%s model=%s",
                    query_hash[:16], exact_entry.model,
                )
                return exact_entry.response, 1.0

        # --- Tier 2: semantic cosine similarity ---
        embedding = self._embed(query_text)
        if embedding is None:
            self._misses += 1
            return None, 0.0

        with self._lock:
            if self._embeddings is None or len(self._entries) == 0:
                self._misses += 1
                return None, 0.0

            # Cosine similarity: (N, D) @ (D,) -> (N,)
            # embeddings are already L2-normalized by SentenceTransformer
            similarities = np.dot(self._embeddings, embedding)
            best_idx = int(np.argmax(similarities))
            best_sim = float(similarities[best_idx])

            if best_sim >= self._threshold:
                entry = self._entries[best_idx]
                # Per-entry adaptive threshold check: use the entry's own
                # threshold (which may be lower than global) if it has enough
                # successful history.
                entry_threshold = entry.adaptive_threshold
                if best_sim < entry_threshold:
                    self._misses += 1
                    return None, 0.0

                entry.hit_count += 1
                self._hits += 1
                self._semantic_hits += 1
                self._total_saved += entry.cost_saved
                # Lower threshold slightly on successful hit (confidence grows)
                if entry.hit_count > 2:
                    entry.adaptive_threshold = max(
                        ADAPTIVE_THRESHOLD_MIN,
                        entry.adaptive_threshold - ADAPTIVE_THRESHOLD_DECAY,
                    )
                # Move to end of LRU list (most recently used)
                self._entries.pop(best_idx)
                self._entries.append(entry)
                # Rebuild embedding matrix preserving order
                self._embeddings = np.vstack([
                    self._embeddings[:best_idx],
                    self._embeddings[best_idx + 1:],
                    entry.embedding.reshape(1, -1),
                ])
                _log.info(
                    "cache_hit_semantic query_hash=%s similarity=%.3f model=%s",
                    entry.query_hash[:16], best_sim, entry.model,
                )
                return entry.response, best_sim
            else:
                self._misses += 1
                return None, 0.0

    def store(
        self,
        query_text: str,
        response: dict,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_per_million_input: float = 0.0,
        cost_per_million_output: float = 0.0,
    ) -> None:
        """Store a response in both tiers of the cache.

        *cost_per_million_input* and *cost_per_million_output* are used
        to estimate the USD savings on future cache hits.
        """
        if len(query_text) < MIN_QUERY_LENGTH:
            return
        if response.get("tool_calls"):
            # Don't cache tool-call responses -- they depend on tool results
            # which change the conversation state and can't be cached.
            return

        embedding = self._embed(query_text)
        if embedding is None:
            return

        query_hash = self._hash_text(query_text)
        entry = CacheEntry(
            query_hash=query_hash,
            embedding=embedding,
            response=response,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            threshold=self._threshold,
        )
        # Estimated cost per API call: input + output tokens at provider rates
        entry.cost_saved = (
            (input_tokens / 1_000_000) * cost_per_million_input
            + (output_tokens / 1_000_000) * cost_per_million_output
        )

        with self._lock:
            # Tier 1: exact hash -> entry (deduplicates on same key)
            self._exact_cache[query_hash] = entry

            # Tier 2: embedding + LRU list
            self._entries.append(entry)
            new_vec = embedding.reshape(1, -1)
            if self._embeddings is None:
                self._embeddings = new_vec
            else:
                self._embeddings = np.vstack([self._embeddings, new_vec])

            # LRU eviction: remove oldest entries if over max
            while len(self._entries) > self._max_entries:
                removed = self._entries.pop(0)
                self._embeddings = self._embeddings[1:]
                # Also evict from exact cache if this hash still maps here
                if self._exact_cache.get(removed.query_hash) is removed:
                    self._exact_cache.pop(removed.query_hash, None)
                _log.debug("cache_evict query_hash=%s", removed.query_hash[:16])

    def stats(self) -> dict[str, Any]:
        """Return cache statistics with tier breakdown and adaptive info."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0.0
            # Compute average adaptive threshold across entries
            avg_adaptive = 0.0
            if self._entries:
                avg_adaptive = round(
                    sum(e.adaptive_threshold for e in self._entries)
                    / len(self._entries), 3,
                )
            return {
                "hits": self._hits,
                "exact_hits": self._exact_hits,
                "semantic_hits": self._semantic_hits,
                "misses": self._misses,
                "total": total,
                "hit_rate": round(hit_rate, 1),
                "entries": len(self._entries),
                "exact_entries": len(self._exact_cache),
                "max_entries": self._max_entries,
                "threshold": self._threshold,
                "avg_adaptive_threshold": avg_adaptive,
                "feedback_correct": self._feedback_positive,
                "feedback_wrong": self._feedback_negative,
                "estimated_usd_saved": round(self._total_saved, 6),
            }

    def clear(self) -> None:
        """Clear all cache entries (both tiers) and reset all counters."""
        with self._lock:
            self._entries.clear()
            self._embeddings = None
            self._exact_cache.clear()
            self._hits = 0
            self._exact_hits = 0
            self._semantic_hits = 0
            self._misses = 0
            self._total_saved = 0.0
            self._feedback_positive = 0
            self._feedback_negative = 0

    def set_threshold(self, threshold: float) -> None:
        """Update the similarity threshold at runtime."""
        self._threshold = max(0.0, min(1.0, threshold))

    def report_feedback(self, query_text: str, was_correct: bool) -> None:
        """Report whether a cache response was correct (verified by LLM).

        On false positive (was_correct=False), raises the matching entry's
        adaptive threshold by ADAPTIVE_THRESHOLD_PENALTY.  On true positive,
        the entry's threshold is already lowered in lookup().

        This feedback loop means entries that never produce false positives
        get progressively lower thresholds (higher cache hit rate), while
        entries that produced a bad match get penalized immediately.
        """
        if len(query_text) < MIN_QUERY_LENGTH:
            return
        query_hash = self._hash_text(query_text)
        with self._lock:
            if was_correct:
                self._feedback_positive += 1
            else:
                self._feedback_negative += 1
                # Find matching entry and penalize
                entry = self._exact_cache.get(query_hash)
                if entry is None:
                    # Fall back: search by hash in entries list
                    for e in self._entries:
                        if e.query_hash == query_hash:
                            entry = e
                            break
                if entry is not None:
                    entry.false_positives += 1
                    entry.adaptive_threshold = min(
                        1.0,
                        entry.adaptive_threshold + ADAPTIVE_THRESHOLD_PENALTY,
                    )
                    _log.info(
                        "cache_feedback_false_positive query_hash=%s "
                        "new_threshold=%.3f fp_count=%d",
                        query_hash[:16], entry.adaptive_threshold,
                        entry.false_positives,
                    )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> np.ndarray | None:
        """Embed *text* using the shared SentenceTransformer model.

        Returns a 1-D float64 array, or None if the model is unavailable.
        """
        try:
            from tools.search_ops import _sem_get_model
            model = _sem_get_model()
            if model is None:
                return None
            result = model.encode(
                [text],
                show_progress_bar=False,
                normalize_embeddings=True,  # L2-normalized for cosine sim
            )
            if isinstance(result, np.ndarray):
                return result[0].astype(np.float64)
            return np.array(result[0], dtype=np.float64)
        except Exception as e:
            _log.warning("embed_failed error=%s", e)
            return None

    @staticmethod
    def _hash_text(text: str) -> str:
        """Return a short hash of *text* for logging/debugging."""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    def _evict_expired(self) -> None:
        """Remove entries older than TTL from both tiers (must hold lock)."""
        if self._ttl_seconds <= 0:
            return
        now = time.time()
        cutoff = now - self._ttl_seconds
        keep_from = 0
        for i, entry in enumerate(self._entries):
            if entry.created_at >= cutoff:
                keep_from = i
                break
        else:
            keep_from = len(self._entries)  # all expired

        if keep_from > 0:
            # Remove expired entries from exact cache
            for entry in self._entries[:keep_from]:
                if self._exact_cache.get(entry.query_hash) is entry:
                    self._exact_cache.pop(entry.query_hash, None)
            self._entries = self._entries[keep_from:]
            if self._embeddings is not None and keep_from < len(self._embeddings):
                self._embeddings = self._embeddings[keep_from:]
            else:
                self._embeddings = None


# ---------------------------------------------------------------------------
# Global singleton (created lazily)
# ---------------------------------------------------------------------------

_SEMANTIC_CACHE: SemanticCache | None = None
_SEMANTIC_CACHE_LOCK: threading.Lock = threading.Lock()


def get_semantic_cache() -> SemanticCache:
    """Return the global SemanticCache singleton, creating it if needed."""
    global _SEMANTIC_CACHE
    if _SEMANTIC_CACHE is None:
        with _SEMANTIC_CACHE_LOCK:
            if _SEMANTIC_CACHE is None:
                _SEMANTIC_CACHE = SemanticCache()
    return _SEMANTIC_CACHE


def clear_semantic_cache() -> None:
    """Clear the global semantic cache (used at session end)."""
    global _SEMANTIC_CACHE
    with _SEMANTIC_CACHE_LOCK:
        if _SEMANTIC_CACHE is not None:
            _SEMANTIC_CACHE.clear()


def semantic_cache_stats() -> dict[str, Any]:
    """Return stats from the global semantic cache."""
    cache = get_semantic_cache()
    return cache.stats()
