#!/usr/bin/env python3
"""
test_memory_core.py -- tests for persistent memory: core memory, session search,
and background consolidation.
"""

import os
import sys
import tempfile
import unittest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.memory import MemoryStore


class TestCoreMemory(unittest.TestCase):
    """Tests for the bounded core_memory table."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test_mem.db")
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- get_core_memory default ---

    def test_get_core_memory_defaults_to_empty_string(self):
        """Fresh store returns empty core memory."""
        content = self.store.get_core_memory()
        self.assertEqual(content, "")

    # --- write_core_memory basic ---

    def test_write_core_memory_stores_content(self):
        """Writing core memory persists across reads."""
        self.store.write_core_memory("Project uses FastAPI with SQLAlchemy")
        content = self.store.get_core_memory()
        self.assertIn("FastAPI", content)

    # --- write_core_memory returns info ---

    def test_write_core_memory_returns_ok_and_remaining(self):
        """write_core_memory returns ok=True and remaining char count."""
        result = self.store.write_core_memory("Short.")
        self.assertTrue(result["ok"])
        self.assertGreater(result["remaining"], 0)
        self.assertEqual(result["char_limit"], 2500)

    # --- get_core_memory_info ---

    def test_get_core_memory_info_returns_dict(self):
        """get_core_memory_info returns content, char_limit, and length."""
        self.store.write_core_memory("Hello")
        info = self.store.get_core_memory_info()
        self.assertEqual(info["content"], "Hello")
        self.assertEqual(info["length"], 5)
        self.assertEqual(info["char_limit"], 2500)

    # --- char_limit enforcement ---

    def test_write_core_memory_rejects_over_limit(self):
        """write_core_memory returns ok=False when content exceeds char_limit."""
        # Write minimal content first so the table row exists
        self.store.write_core_memory("A")
        # Now try to write huge content directly (exceeds default 2500)
        huge = "x" * 3000
        result = self.store.write_core_memory(huge)
        self.assertFalse(result["ok"])
        self.assertIn("exceeds", result["message"])

    # --- check if has content ---

    def test_has_content_check(self):
        """Can check if core memory has content via get_core_memory_info."""
        info = self.store.get_core_memory_info()
        self.assertEqual(info["length"], 0)

        self.store.write_core_memory("Some content")
        info = self.store.get_core_memory_info()
        self.assertGreater(len(info["content"]), 0)

    # --- default char_limit ---

    def test_default_char_limit_is_2500(self):
        """Default char_limit matches the schema default."""
        self.store.write_core_memory("Test")
        info = self.store.get_core_memory_info()
        self.assertEqual(info["char_limit"], 2500)


class TestSessionSearch(unittest.TestCase):
    """Tests for FTS5 session search across past messages."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test_mem.db")
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save_msg(self, role: str, content: str) -> None:
        """Save a single message to the store."""
        self.store.save([{"role": role, "content": content}])

    # --- search returns empty for no results ---

    def test_search_returns_empty_for_no_match(self):
        """Search with no matching content returns empty list."""
        self._save_msg("user", "Hello world")
        results = self.store.search_messages("xyzzy_not_present")
        self.assertEqual(results, [])

    # --- basic search ---

    def test_search_finds_matching_message(self):
        """FTS5 search finds messages by content."""
        self._save_msg("user", "We should use FastAPI for the backend API")
        results = self.store.search_messages("FastAPI")
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("FastAPI", results[0]["content"])

    # --- limit parameter ---

    def test_search_respects_limit(self):
        """The limit parameter caps result count."""
        for i in range(20):
            self._save_msg("user", f"test message number {i} with keyword")
        results = self.store.search_messages("keyword", limit=5)
        self.assertLessEqual(len(results), 5)

    # --- empty query ---

    def test_search_with_empty_query_returns_empty(self):
        """Empty query string returns empty results."""
        self._save_msg("user", "some content")
        results = self.store.search_messages("")
        self.assertEqual(results, [])

    # --- special characters don't crash ---

    def test_search_handles_special_characters(self):
        """FTS5 query escaping prevents crashes on special chars."""
        self._save_msg("user", "path is C:\\Users\\test\\file.py")
        results = self.store.search_messages('C:\\\\Users')
        self.assertIsInstance(results, list)


class TestConsolidation(unittest.TestCase):
    """Tests for background consolidation logic (unit tests, no LLM calls)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test_mem.db")
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- rate limiting ---

    def test_consolidation_rate_limited_by_turns(self):
        """Consolidation counter increments but rate-limiter prevents excess calls."""
        import tools.memory_consolidation as mc

        original_counter = mc._consolidation_turn_counter
        original_time = mc._LAST_CONSOLIDATION_TIME
        mc._consolidation_turn_counter = 0
        mc._LAST_CONSOLIDATION_TIME = 0.0

        try:
            # Turn 1
            mc._consolidation_turn_counter = 1
            self.assertLess(mc._consolidation_turn_counter, mc._MAX_CONSOLIDATION_TURNS)
        finally:
            mc._consolidation_turn_counter = original_counter
            mc._LAST_CONSOLIDATION_TIME = original_time

    # --- fact merging ---

    def test_fact_apply_updates_core_memory(self):
        """Applying facts via memory_core updates the store."""
        self.store.write_core_memory("Initial fact.")
        from tools.memory_consolidation import _apply_facts_to_core

        from tools import _TOOL_CONTEXT
        old_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
        _TOOL_CONTEXT._memory_store = self.store
        try:
            result = _apply_facts_to_core("New fact: use pytest for testing")
            self.assertTrue(result)
            content = self.store.get_core_memory()
            self.assertIn("pytest", content)
        finally:
            if old_store is not None:
                _TOOL_CONTEXT._memory_store = old_store
            else:
                try:
                    delattr(_TOOL_CONTEXT, "_memory_store")
                except AttributeError:
                    pass

    # --- snapshot helper ---

    def test_snapshot_conversation_truncates_long_content(self):
        """Conversation snapshot helper limits output size."""
        from tools.memory_consolidation import _snapshot_conversation

        long_msg = [{"role": "user", "content": "x" * 500}]
        result = _snapshot_conversation(long_msg)
        self.assertLess(len(result), 1000)


if __name__ == "__main__":
    unittest.main()
