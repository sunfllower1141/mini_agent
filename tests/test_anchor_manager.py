"""
Tests for core/anchor_manager.py -- Dirac-style hash-anchored editing.

Covers: FNV-1a hashing, Myers/LCS/greedy diffs, AnchorStateManager
reconcile/clear/reset, and line formatting helpers.
"""
from __future__ import annotations

import unittest
import tempfile
import os
import threading

from core.anchor_manager import (
    _fnv1a_32,
    _compute_hashes,
    _myers_diff,
    _lcs_diff,
    _greedy_diff,
    _merge_adjacent,
    _TrackedDocument,
    AnchorStateManager,
    format_line_with_anchor,
    format_lines_with_anchors,
    _ANCHOR_WORDS,
)


class TestFNV1aHash(unittest.TestCase):
    """Tests for _fnv1a_32 hash function."""

    def test_deterministic(self):
        """Same input produces same hash."""
        h1 = _fnv1a_32("import os")
        h2 = _fnv1a_32("import os")
        self.assertEqual(h1, h2)
        self.assertIsInstance(h1, int)
        # FNV-1a 32-bit: result is unsigned 32-bit
        self.assertGreaterEqual(h1, 0)
        self.assertLess(h1, 0xFFFFFFFF + 1)

    def test_different_content_different_hash(self):
        """Different content produces different hashes (almost always)."""
        h1 = _fnv1a_32("import os")
        h2 = _fnv1a_32("import sys")
        self.assertNotEqual(h1, h2)

    def test_empty_string(self):
        """Empty string has a consistent hash."""
        h = _fnv1a_32("")
        self.assertIsInstance(h, int)

    def test_single_char(self):
        """Single character produces a hash."""
        h = _fnv1a_32("a")
        self.assertIsInstance(h, int)


class TestComputeHashes(unittest.TestCase):
    """Tests for _compute_hashes."""

    def test_empty_list(self):
        self.assertEqual(_compute_hashes([]), [])

    def test_single_line(self):
        hashes = _compute_hashes(["hello"])
        self.assertEqual(len(hashes), 1)
        self.assertIsInstance(hashes[0], int)

    def test_multiple_lines(self):
        lines = ["line1", "line2", "line3"]
        hashes = _compute_hashes(lines)
        self.assertEqual(len(hashes), 3)
        self.assertEqual(hashes[0], _fnv1a_32("line1"))
        self.assertEqual(hashes[1], _fnv1a_32("line2"))
        self.assertEqual(hashes[2], _fnv1a_32("line3"))


class TestMyersDiff(unittest.TestCase):
    """Tests for diff algorithm used by anchor reconciliation."""

    def test_identical_lists(self):
        a = _compute_hashes(["a", "b", "c"])
        diff = _myers_diff(a, a)
        self.assertEqual(diff, [("equal", 3)])

    def test_insert_at_start(self):
        a = _compute_hashes(["b", "c"])
        b = _compute_hashes(["a", "b", "c"])
        diff = _myers_diff(a, b)
        # Should be insert 1, equal 2
        self.assertEqual(diff, [("insert", 1), ("equal", 2)])

    def test_insert_at_end(self):
        a = _compute_hashes(["a", "b"])
        b = _compute_hashes(["a", "b", "c"])
        diff = _myers_diff(a, b)
        self.assertEqual(len(diff), 2)
        self.assertIn(("insert", 1), diff)

    def test_delete_from_start(self):
        a = _compute_hashes(["a", "b", "c"])
        b = _compute_hashes(["b", "c"])
        diff = _myers_diff(a, b)
        self.assertEqual(diff, [("delete", 1), ("equal", 2)])

    def test_delete_from_end(self):
        a = _compute_hashes(["a", "b", "c"])
        b = _compute_hashes(["a", "b"])
        diff = _myers_diff(a, b)
        self.assertEqual(len(diff), 2)
        self.assertIn(("delete", 1), diff)

    def test_replace_middle(self):
        a = _compute_hashes(["a", "b", "c"])
        b = _compute_hashes(["a", "x", "c"])
        diff = _myers_diff(a, b)
        # equal 1, delete 1, insert 1, equal 1
        self.assertEqual(len(diff), 4)
        ops = [op for op, _ in diff]
        self.assertEqual(ops, ["equal", "delete", "insert", "equal"])

    def test_empty_old(self):
        a: list[int] = []
        b = _compute_hashes(["a", "b"])
        diff = _myers_diff(a, b)
        self.assertEqual(diff, [("insert", 2)])

    def test_empty_new(self):
        a = _compute_hashes(["a", "b"])
        b: list[int] = []
        diff = _myers_diff(a, b)
        self.assertEqual(diff, [("delete", 2)])

    def test_both_empty(self):
        diff = _myers_diff([], [])
        self.assertEqual(diff, [])


class TestLCSDiff(unittest.TestCase):
    """Tests for _lcs_diff."""

    def test_identical(self):
        a = [1, 2, 3]
        diff = _lcs_diff(a, a)
        self.assertEqual(diff, [("equal", 3)])

    def test_replace(self):
        a = [1, 2, 3]
        b = [1, 9, 3]
        diff = _lcs_diff(a, b)
        self.assertIn(("delete", 1), diff)
        self.assertIn(("insert", 1), diff)


class TestGreedyDiff(unittest.TestCase):
    """Tests for _greedy_diff fallback for large files."""

    def test_identical(self):
        a = [1, 2, 3, 4, 5]
        diff = _greedy_diff(a, a)
        self.assertEqual(diff, [("equal", 5)])

    def test_insert(self):
        a = [1, 3]
        b = [1, 2, 3]
        diff = _greedy_diff(a, b)
        self.assertIn(("insert", 1), diff)

    def test_empty(self):
        diff = _greedy_diff([], [])
        self.assertEqual(diff, [])


class TestMergeAdjacent(unittest.TestCase):
    """Tests for _merge_adjacent helper."""

    def test_no_merge_needed(self):
        ops = [("equal", 1), ("insert", 1), ("equal", 1)]
        self.assertEqual(_merge_adjacent(ops), ops)

    def test_merges_same_type(self):
        ops = [("equal", 1), ("equal", 2), ("insert", 1)]
        self.assertEqual(_merge_adjacent(ops), [("equal", 3), ("insert", 1)])

    def test_multiple_merges(self):
        ops = [("equal", 1), ("equal", 1), ("insert", 1), ("insert", 2), ("equal", 3)]
        self.assertEqual(_merge_adjacent(ops), [("equal", 2), ("insert", 3), ("equal", 3)])

    def test_empty(self):
        self.assertEqual(_merge_adjacent([]), [])

    def test_single(self):
        self.assertEqual(_merge_adjacent([("equal", 5)]), [("equal", 5)])


class TestTrackedDocument(unittest.TestCase):
    """Tests for _TrackedDocument dataclass-like class."""

    def test_create(self):
        doc = _TrackedDocument(
            hashes=[1, 2, 3],
            anchors=["Apple", "Brave", "Cider"],
            used_words={"Apple", "Brave", "Cider"},
            pool=["Delta", "Eagle"],
        )
        self.assertEqual(doc.hashes, [1, 2, 3])
        self.assertEqual(doc.anchors, ["Apple", "Brave", "Cider"])
        self.assertEqual(doc.used_words, {"Apple", "Brave", "Cider"})
        self.assertEqual(doc.available_pool, ["Delta", "Eagle"])


class TestAnchorStateManager(unittest.TestCase):
    """Tests for AnchorStateManager class -- Dirac-style anchor tracking."""

    def setUp(self):
        """Reset anchor state before each test."""
        AnchorStateManager.reset()

    def tearDown(self):
        """Clean up after each test."""
        AnchorStateManager.reset()

    def test_reconcile_first_call(self):
        """First reconcile assigns fresh word anchors to all lines."""
        lines = ["import os", "import sys", "print('hello')"]
        anchors = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test1")

        self.assertEqual(len(anchors), 3)
        # All anchors should be from the word pool
        for anchor in anchors:
            self.assertIn(anchor, _ANCHOR_WORDS)
        # All anchors should be unique
        self.assertEqual(len(set(anchors)), 3)

    def test_reconcile_unchanged_preserves_anchors(self):
        """Unchanged lines keep EXACT same anchor words."""
        lines = ["line1", "line2", "line3"]
        anchors1 = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test2")

        # Reconcile again with same content
        anchors2 = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test2")

        self.assertEqual(anchors1, anchors2, "Unchanged lines must preserve anchors")

    def test_reconcile_insert_lines(self):
        """Inserting new lines gives them fresh anchors, unchanged lines keep theirs."""
        lines = ["lineA", "lineB", "lineC"]
        anchors1 = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test3")

        # Insert a line between A and B
        new_lines = ["lineA", "lineX", "lineB", "lineC"]
        anchors2 = AnchorStateManager.reconcile("/test/file.py", new_lines, task_id="test3")

        self.assertEqual(len(anchors2), 4)
        # LineA and lineC should preserve their anchors
        self.assertEqual(anchors2[0], anchors1[0], "lineA anchor preserved")
        self.assertEqual(anchors2[3], anchors1[2], "lineC anchor preserved")
        # lineB moved to index 2, should keep its anchor
        self.assertEqual(anchors2[2], anchors1[1], "lineB anchor preserved after insert")
        # lineX is new -- gets a fresh anchor
        self.assertIn(anchors2[1], _ANCHOR_WORDS)
        self.assertNotIn(anchors2[1], anchors1, "New line gets unique anchor")

    def test_reconcile_delete_lines(self):
        """Deleting lines preserves anchors for remaining lines."""
        lines = ["line1", "line2", "line3", "line4"]
        anchors1 = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test4")

        # Delete line2 (index 1)
        new_lines = ["line1", "line3", "line4"]
        anchors2 = AnchorStateManager.reconcile("/test/file.py", new_lines, task_id="test4")

        self.assertEqual(len(anchors2), 3)
        self.assertEqual(anchors2[0], anchors1[0], "line1 anchor preserved")
        self.assertEqual(anchors2[1], anchors1[2], "line3 anchor preserved")
        self.assertEqual(anchors2[2], anchors1[3], "line4 anchor preserved")

    def test_reconcile_replace_line(self):
        """Replacing a line content gives it a new anchor."""
        lines = ["original", "common1", "common2"]
        anchors1 = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test5")

        # Replace "original" with "modified"
        new_lines = ["modified", "common1", "common2"]
        anchors2 = AnchorStateManager.reconcile("/test/file.py", new_lines, task_id="test5")

        self.assertEqual(len(anchors2), 3)
        # The replaced line gets a NEW anchor
        self.assertNotEqual(anchors2[0], anchors1[0], "Replaced line gets new anchor")
        # Unchanged lines keep theirs
        self.assertEqual(anchors2[1], anchors1[1], "common1 anchor preserved")
        self.assertEqual(anchors2[2], anchors1[2], "common2 anchor preserved")

    def test_is_tracking(self):
        """is_tracking returns True after reconcile, False for unknown."""
        self.assertFalse(AnchorStateManager.is_tracking("/test/unknown.py", task_id="test6"))

        AnchorStateManager.reconcile("/test/file.py", ["hello"], task_id="test6")
        self.assertTrue(AnchorStateManager.is_tracking("/test/file.py", task_id="test6"))

    def test_get_anchors(self):
        """get_anchors returns current anchors after reconcile."""
        lines = ["a", "b", "c"]
        anchors = AnchorStateManager.reconcile("/test/file.py", lines, task_id="test7")

        retrieved = AnchorStateManager.get_anchors("/test/file.py", task_id="test7")
        self.assertEqual(retrieved, anchors)

        # Unknown file returns None
        self.assertIsNone(AnchorStateManager.get_anchors("/test/nonexistent.py", task_id="test7"))

    def test_clear_state(self):
        """clear_state removes tracking for a file."""
        AnchorStateManager.reconcile("/test/file.py", ["hello"], task_id="test8")
        self.assertTrue(AnchorStateManager.is_tracking("/test/file.py", task_id="test8"))

        AnchorStateManager.clear_state("/test/file.py", task_id="test8")
        self.assertFalse(AnchorStateManager.is_tracking("/test/file.py", task_id="test8"))

    def test_clear_state_nonexistent(self):
        """clear_state on untracked file does not raise."""
        AnchorStateManager.clear_state("/test/nonexistent.py", task_id="test9")
        # Should not raise

    def test_reset_specific_task(self):
        """reset with task_id clears only that task."""
        AnchorStateManager.reconcile("/test/file1.py", ["a"], task_id="taskA")
        AnchorStateManager.reconcile("/test/file2.py", ["b"], task_id="taskB")

        AnchorStateManager.reset(task_id="taskA")

        self.assertFalse(AnchorStateManager.is_tracking("/test/file1.py", task_id="taskA"))
        self.assertTrue(AnchorStateManager.is_tracking("/test/file2.py", task_id="taskB"))

    def test_reset_all_tasks(self):
        """reset with no task_id clears everything."""
        AnchorStateManager.reconcile("/test/file1.py", ["a"], task_id="taskA")
        AnchorStateManager.reconcile("/test/file2.py", ["b"], task_id="taskB")

        AnchorStateManager.reset()

        self.assertFalse(AnchorStateManager.is_tracking("/test/file1.py", task_id="taskA"))
        self.assertFalse(AnchorStateManager.is_tracking("/test/file2.py", task_id="taskB"))

    def test_default_task_id(self):
        """Reconcile without task_id uses 'default'."""
        AnchorStateManager.reconcile("/test/file.py", ["hello"])
        self.assertTrue(AnchorStateManager.is_tracking("/test/file.py"))

    def test_multiple_files_same_task(self):
        """Multiple files can be tracked in the same task."""
        AnchorStateManager.reconcile("/test/a.py", ["a"], task_id="task10")
        AnchorStateManager.reconcile("/test/b.py", ["b"], task_id="task10")

        self.assertTrue(AnchorStateManager.is_tracking("/test/a.py", task_id="task10"))
        self.assertTrue(AnchorStateManager.is_tracking("/test/b.py", task_id="task10"))

    def test_thread_safety(self):
        """Concurrent reconciles from multiple threads don't corrupt state."""
        errors = []

        def reconcile_file(fname: str):
            try:
                for _ in range(50):
                    AnchorStateManager.reconcile(fname, [f"line{i}" for i in range(10)],
                                                 task_id="thread_test")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reconcile_file, args=(f"/test/t{i}.py",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread safety errors: {errors}")

    def test_large_file_diff_triggers_greedy(self):
        """Large files (>100k lines) trigger greedy diff without crashing."""
        # Create a file with enough lines to trigger greedy diff
        lines = [f"line{i}" for i in range(100)]
        AnchorStateManager.reconcile("/test/large.py", lines, task_id="large_test")
        self.assertTrue(AnchorStateManager.is_tracking("/test/large.py", task_id="large_test"))


class TestFormatHelpers(unittest.TestCase):
    """Tests for formatting helpers."""

    def test_format_line_with_anchor(self):
        result = format_line_with_anchor("Brave", "import os")
        self.assertEqual(result, "Brave|import os")

    def test_format_line_with_anchor_custom_delimiter(self):
        result = format_line_with_anchor("Fox", "content", delimiter=":")
        self.assertEqual(result, "Fox:content")

    def test_format_lines_with_anchors(self):
        lines = ["import os", "import sys"]
        anchors = ["Apple", "Brave"]
        result = format_lines_with_anchors(lines, anchors)
        self.assertEqual(result, "Apple| import os\nBrave| import sys")

    def test_format_lines_with_anchors_and_numbers(self):
        lines = ["a", "b", "c"]
        anchors = ["Xenon", "Yacht", "Zebra"]
        result = format_lines_with_anchors(lines, anchors, line_numbers=True)
        self.assertEqual(
            result,
            "1:Xenon| a\n2:Yacht| b\n3:Zebra| c"
        )

    def test_format_lines_empty(self):
        self.assertEqual(format_lines_with_anchors([], []), "")




class TestAnchorWordPool(unittest.TestCase):
    """Tests for the anchor word pool constants."""

    def test_words_are_unique(self):
        """All anchor words must be unique."""
        self.assertEqual(len(_ANCHOR_WORDS), len(set(_ANCHOR_WORDS)))

    def test_words_are_strings(self):
        """All words are non-empty strings."""
        for w in _ANCHOR_WORDS:
            self.assertIsInstance(w, str)
            self.assertTrue(len(w) > 0)

    def test_minimum_pool_size(self):
        """Pool should have at least 500 words for reasonable coverage."""
        self.assertGreater(len(_ANCHOR_WORDS), 500)


if __name__ == "__main__":
    unittest.main()
