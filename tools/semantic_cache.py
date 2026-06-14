"""
semantic_cache.py -- semantic response cache for mini_agent.

Caches LLM responses keyed by the semantic embedding of the last user
message.  When a new query has cosine similarity >= threshold to a cached
query, the cached response is returned directly, bypassing the LLM API
call entirely.

Uses the same SentenceTransformer model (all-MiniLM-L6-v2-code-search-512) already loaded
by search_ops.py, so no additional memory footprint.

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


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

class CacheEntry:
    """A single cached LLM response with metadata."""
    __slots__ = (
        "query_hash", "embedding", "response", "model", "created_at",
        "input_tokens", "output_tokens", "cost_saved",
    )

    def __init__(
        self,
        query_hash: str,
        embedding: np.ndarray,
        response: dict,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
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


# ---------------------------------------------------------------------------
# Semantic cache
# ---------------------------------------------------------------------------

class SemanticCache:
    """Thread-safe semantic response cache using cosine similarity.

    Uses NumPy for fast batched cosine-similarity computation against all
    cached embeddings.  On cache hit, the response is returned immediately.
    On miss, the caller makes the API call and stores the result.
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
        self._lock = threading.Lock()
        self._hits: int = 0
        self._misses: int = 0
        self._total_saved: float = 0.0  # estimated USD saved

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, query_text: str) -> tuple[dict | None, float]:
        """Check if *query_text* matches a cached query.

        Returns ``(cached_response, similarity)`` on hit, or ``(None, 0.0)``
        on miss.
        """
        if len(query_text) < MIN_QUERY_LENGTH:
            self._misses += 1
            return None, 0.0

        embedding = self._embed(query_text)
        if embedding is None:
            self._misses += 1
            return None, 0.0

        with self._lock:
            # Evict expired entries before lookup
            self._evict_expired()

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
                self._hits += 1
                self._total_saved += entry.cost_saved
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
                    "cache_hit query_hash=%s similarity=%.3f model=%s",
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
        """Store a response in the cache.

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
        )
        # Estimated cost per API call: input + output tokens at provider rates
        entry.cost_saved = (
            (input_tokens / 1_000_000) * cost_per_million_input
            + (output_tokens / 1_000_000) * cost_per_million_output
        )

        with self._lock:
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
                _log.debug("cache_evict query_hash=%s", removed.query_hash[:16])

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "total": total,
                "hit_rate": round(hit_rate, 1),
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "threshold": self._threshold,
                "estimated_usd_saved": round(self._total_saved, 6),
            }

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._entries.clear()
            self._embeddings = None

    def set_threshold(self, threshold: float) -> None:
        """Update the similarity threshold at runtime."""
        self._threshold = max(0.0, min(1.0, threshold))

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
        """Remove entries older than TTL (must hold lock)."""
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
