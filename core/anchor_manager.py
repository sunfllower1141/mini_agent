#!/usr/bin/env python3
"""
anchor_manager.py -- Persistent anchor state manager for mini_agent.

Implements Dirac's hash-anchored editing pattern:
  1. Compute FNV-1a 32-bit hash of every line in a file.
  2. Use Myers Diff on integer hashes to detect changed vs unchanged lines.
  3. Unchanged lines KEEP their word anchors across edits (Apple, Brave, ...).
  4. New lines get fresh words from a shuffled dictionary pool.

This enables multi-file batched edits (one LLM roundtrip = N files modified)
without anchors going stale between reads.

Design:
  - Per-task-id storage (defaults to "default" if no task_id provided).
  - LRU eviction: max 1024 files, max 50K lines per file.
  - Dictionary of random word anchors loaded from a bundled file.
  - Thread-safe: uses threading.Lock for state mutations.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

# ---------------------------------------------------------------------------
# 1721 random words for stable anchors (from Dirac's anchor dictionary).
# These are opaque tags that carry no meaning -- just unique identifiers for lines.
# The pool is large enough that synthetic fallback anchors are rarely needed.
from ._anchor_words import _ANCHOR_WORDS

# ---------------------------------------------------------------------------
# FNV-1a 32-bit hash (same as Dirac)
# ---------------------------------------------------------------------------

def _fnv1a_32(line: str) -> int:
    """FNV-1a 32-bit hash of a string."""
    h = 2166136261
    for ch in line:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


def _compute_hashes(lines: list[str]) -> list[int]:
    """Compute FNV-1a hashes for all lines."""
    return [_fnv1a_32(line) for line in lines]


# ---------------------------------------------------------------------------
# Myers Diff on integer arrays (simplified: line-by-line diff)
# ---------------------------------------------------------------------------

def _myers_diff(old_hashes: list[int], new_hashes: list[int]) -> list[tuple[str, int]]:
    """
    Compute a minimal diff between two hash arrays.
    
    Returns a list of (op, count) tuples:
      - ('equal', N): N lines unchanged
      - ('insert', N): N lines added in new
      - ('delete', N): N lines removed from old
    
    Uses a simple LCS-based approach (good enough for typical file edits).
    For very large files, we use a line-by-line sliding window instead.
    """
    # For simplicity and reliability, use a line-by-line greedy diff
    # that handles common edit patterns well
    
    old_len = len(old_hashes)
    new_len = len(new_hashes)
    
    # Build LCS table for small-to-medium files
    if old_len * new_len <= 10_000_000:  # ~10M cells max
        return _lcs_diff(old_hashes, new_hashes)
    else:
        return _greedy_diff(old_hashes, new_hashes)


def _lcs_diff(old: list[int], new: list[int]) -> list[tuple[str, int]]:
    """LCS-based diff for accurate results."""
    m, n = len(old), len(new)
    
    # LCS length table
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old[i - 1] == new[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    
    # Backtrack to produce diff
    result: list[tuple[str, int]] = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and old[i - 1] == new[j - 1]:
            # Equal line
            count = 1
            i -= 1
            j -= 1
            while i > 0 and j > 0 and old[i - 1] == new[j - 1]:
                count += 1
                i -= 1
                j -= 1
            result.append(('equal', count))
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            # Insert
            count = 1
            j -= 1
            while j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
                # Check if still an insert
                if i > 0 and old[i - 1] == new[j - 1]:
                    break
                count += 1
                j -= 1
            result.append(('insert', count))
        else:
            # Delete
            count = 1
            i -= 1
            while i > 0 and (j == 0 or dp[i - 1][j] >= dp[i][j - 1]):
                if j > 0 and old[i - 1] == new[j - 1]:
                    break
                count += 1
                i -= 1
            result.append(('delete', count))
    
    result.reverse()
    return _merge_adjacent(result)


def _greedy_diff(old: list[int], new: list[int]) -> list[tuple[str, int]]:
    """Greedy diff for large files -- less accurate but O(n) memory."""
    # Use Python's difflib for large files
    import difflib
    old_str = [str(h) for h in old]
    new_str = [str(h) for h in new]
    
    result: list[tuple[str, int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_str, new_str).get_opcodes():
        if tag == 'equal':
            result.append(('equal', i2 - i1))
        elif tag == 'insert':
            result.append(('insert', j2 - j1))
        elif tag == 'replace':
            result.append(('delete', i2 - i1))
            result.append(('insert', j2 - j1))
        elif tag == 'delete':
            result.append(('delete', i2 - i1))
    
    return _merge_adjacent(result)


def _merge_adjacent(ops: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Merge adjacent operations of the same type."""
    if not ops:
        return []
    merged = [ops[0]]
    for op, count in ops[1:]:
        prev_op, prev_count = merged[-1]
        if op == prev_op:
            merged[-1] = (op, prev_count + count)
        else:
            merged.append((op, count))
    return merged


# ---------------------------------------------------------------------------
# Tracked document state
# ---------------------------------------------------------------------------

class _TrackedDocument:
    """Per-file anchor state."""
    __slots__ = ('hashes', 'anchors', 'used_words', 'available_pool')

    def __init__(self, hashes: list[int], anchors: list[str],
                 used_words: set[str], pool: list[str]):
        self.hashes = hashes
        self.anchors = anchors
        self.used_words = used_words
        self.available_pool = pool


# ---------------------------------------------------------------------------
# AnchorStateManager
# ---------------------------------------------------------------------------

class AnchorStateManager:
    """
    Per-task persistent anchor state.
    
    Tracks file content by hash, preserves anchor words for unchanged lines
    across edits using Myers Diff.  Anchors are opaque word tags like
    "Inflater", "Normalizer", etc. that the LLM uses to target edits.
    
    Usage:
        # Get anchors for a file (reconciles with previous state)
        anchors = AnchorStateManager.reconcile("/path/to/file.py", lines)
        
        # The anchors are returned as a list, one per line.
        # Use format_line(anchor, content) to produce "Anchor|content" format.
        
        # Reset all state for a task
        AnchorStateManager.reset(task_id="my-task")
    """
    
    # Per-task storage: task_id -> (file_path -> _TrackedDocument)
    _storage: dict[str, dict[str, _TrackedDocument]] = {}
    _lock: threading.Lock = threading.Lock()
    
    MAX_TRACKED_LINES: int = 50000
    MAX_TRACKED_FILES: int = 1024
    MAX_TRACKED_TASKS: int = 50
    
    @staticmethod
    def _get_available_words() -> list[str]:
        """Return a shuffled copy of the anchor word pool (Dirac-style)."""
        import random as _rnd3
        pool = list(_ANCHOR_WORDS)
        _rnd3.shuffle(pool)
        return pool
    
    @staticmethod
    def _get_unique_word(used: set[str], pool: list[str]) -> str:
        """Get a unique word from the pool, refilling from base dictionary if needed.

        Dirac-style recycling: when the pool runs dry, we refill it with words
        from the master dictionary that aren't currently in use by any tracked file.
        Only when all 1721 dictionary words are in active use do we fall back to
        short synthetic anchors.
        """
        while pool:
            w = pool.pop()
            if w not in used:
                return w
        # Pool exhausted -- recycle unused words from the full dictionary
        for w in _ANCHOR_WORDS:
            if w not in used:
                pool.append(w)
        if pool:
            import random as _rnd2
            _rnd2.shuffle(pool)
            return AnchorStateManager._get_unique_word(used, pool)
        # Truly exhausted (all 1721 words in use) -- rare fallback: short hex anchors
        i = len(used)
        while True:
            w = f"a{i:04x}"
            i += 1
            if w not in used:
                return w
    
    @staticmethod
    def _get_task_state(task_id: Optional[str] = None) -> dict[str, _TrackedDocument]:
        """Get or create task state."""
        tid = task_id or "default"
        with AnchorStateManager._lock:
            if tid not in AnchorStateManager._storage:
                # Evict oldest task if limit exceeded
                if len(AnchorStateManager._storage) >= AnchorStateManager.MAX_TRACKED_TASKS:
                    oldest = next(iter(AnchorStateManager._storage))
                    del AnchorStateManager._storage[oldest]
                AnchorStateManager._storage[tid] = {}
            return AnchorStateManager._storage[tid]
    
    @staticmethod
    def _update_state(file_path: str, doc: _TrackedDocument,
                      task_id: Optional[str] = None) -> None:
        """Update file state with LRU eviction."""
        state = AnchorStateManager._get_task_state(task_id)
        with AnchorStateManager._lock:
            # LRU: delete and re-insert
            state.pop(file_path, None)
            state[file_path] = doc
            
            # Evict oldest if over limit
            if len(state) > AnchorStateManager.MAX_TRACKED_FILES:
                oldest_key = next(iter(state))
                del state[oldest_key]
    
    @staticmethod
    def reconcile(file_path: str, current_lines: list[str],
                  task_id: Optional[str] = None) -> list[str]:
        """
        Reconcile current file content with saved anchor state.
        
        Unchanged lines keep their exact word anchors.
        New/added lines get new words.
        Deleted lines are simply removed.
        
        Args:
            file_path: Absolute path to the file.
            current_lines: Current content split by lines.
            task_id: Optional task identifier for isolation.
            
        Returns:
            List of anchor words, one per line in current_lines.
        """
        # Safeguard for massive files
        if len(current_lines) > AnchorStateManager.MAX_TRACKED_LINES:
            return [f"L{i + 1}" for i in range(len(current_lines))]
        
        state = AnchorStateManager._get_task_state(task_id)
        current_hashes = _compute_hashes(current_lines)
        
        with AnchorStateManager._lock:
            tracked = state.get(file_path)
        
        # Fast path: hashes identical -> nothing changed
        if tracked and len(tracked.hashes) == len(current_hashes):
            if tracked.hashes == current_hashes:
                AnchorStateManager._update_state(file_path, tracked, task_id)
                return list(tracked.anchors)
        
        # First time seeing this file?
        if not tracked:
            used_words: set[str] = set()
            pool = AnchorStateManager._get_available_words()
            # Shuffle the pool
            import random as _rnd
            _rnd.shuffle(pool)
            
            anchors = []
            for _ in current_lines:
                w = AnchorStateManager._get_unique_word(used_words, pool)
                used_words.add(w)
                anchors.append(w)
            
            tracked = _TrackedDocument(
                hashes=list(current_hashes),
                anchors=anchors,
                used_words=used_words,
                pool=pool,
            )
            AnchorStateManager._update_state(file_path, tracked, task_id)
            return anchors
        
        # We have history -- diff old vs new hashes
        changes = _myers_diff(tracked.hashes, current_hashes)
        
        new_anchors: list[str] = []
        new_used_words = set(tracked.used_words)
        pool = list(tracked.available_pool) if tracked.available_pool else []
        
        # Replenish pool if needed
        if len(pool) == 0 and len(new_used_words) < len(_ANCHOR_WORDS):
            for w in _ANCHOR_WORDS:
                if w not in new_used_words:
                    pool.append(w)
            import random as _rnd
            _rnd.shuffle(pool)
        
        old_idx = 0
        for op, count in changes:
            if op == 'insert':
                # New lines get new words
                for _ in range(count):
                    w = AnchorStateManager._get_unique_word(new_used_words, pool)
                    new_anchors.append(w)
                    new_used_words.add(w)
            elif op == 'delete':
                # Deleted lines: advance old index
                old_idx += count
            elif op == 'equal':
                # Unchanged lines: CARRY OVER EXACT SAME ANCHOR
                for _ in range(count):
                    preserved = tracked.anchors[old_idx]
                    new_anchors.append(preserved)
                    new_used_words.add(preserved)
                    old_idx += 1
        
        tracked = _TrackedDocument(
            hashes=list(current_hashes),
            anchors=new_anchors,
            used_words=new_used_words,
            pool=pool,
        )
        AnchorStateManager._update_state(file_path, tracked, task_id)
        return new_anchors
    
    @staticmethod
    def is_tracking(file_path: str, task_id: Optional[str] = None) -> bool:
        """Check if a file is being tracked."""
        state = AnchorStateManager._get_task_state(task_id)
        with AnchorStateManager._lock:
            return file_path in state
    
    @staticmethod
    def get_anchors(file_path: str, task_id: Optional[str] = None) -> Optional[list[str]]:
        """Get current anchors for a tracked file, or None."""
        state = AnchorStateManager._get_task_state(task_id)
        with AnchorStateManager._lock:
            doc = state.get(file_path)
            return list(doc.anchors) if doc else None
    
    @staticmethod
    def clear_state(file_path: str, task_id: Optional[str] = None) -> None:
        """Clear anchor state for a single file."""
        state = AnchorStateManager._get_task_state(task_id)
        with AnchorStateManager._lock:
            state.pop(file_path, None)
    
    @staticmethod
    def reset(task_id: Optional[str] = None) -> None:
        """Reset all anchors for a task, or all tasks if no task_id given."""
        with AnchorStateManager._lock:
            if task_id:
                AnchorStateManager._storage.pop(task_id, None)
            else:
                AnchorStateManager._storage.clear()


# ---------------------------------------------------------------------------
# Line formatting helpers (for hash-anchored read outputs)
# ---------------------------------------------------------------------------

def format_line_with_anchor(anchor: str, content: str, delimiter: str = "|") -> str:
    """Format a line as 'Anchor|content' for hash-anchored output."""
    return f"{anchor}{delimiter}{content}"


def format_lines_with_anchors(lines: list[str], anchors: list[str],
                               line_numbers: bool = False,
                               delimiter: str = "|") -> str:
    """Format lines with anchors, optionally with line numbers."""
    out: list[str] = []
    for i, (anchor, line) in enumerate(zip(anchors, lines)):
        prefix = f"{i + 1}:{anchor}{delimiter} " if line_numbers else f"{anchor}{delimiter} "
        out.append(prefix + line)
    return "\n".join(out)



