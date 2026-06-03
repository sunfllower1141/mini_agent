#!/usr/bin/env python3
"""
test_memory.py — tests for the conversation memory persistence layer (SQLite).
"""

import json
import os
import sqlite3
import tempfile
import unittest

from memory.memory import MemoryStore, _db_path, _prune_by_tokens


class TestMemoryStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memfile = os.path.join(self.tmp, "memory.json")
        self.store = MemoryStore(self.memfile)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- load from empty state ---

    def test_load_returns_empty_list_when_no_file(self):
        result = self.store.load()
        self.assertEqual(result, [])

    def test_load_returns_empty_list_when_db_empty(self):
        result = self.store.load()
        self.assertEqual(result, [])

    # --- save and load round-trip ---

    def test_save_and_load_roundtrip(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "write file x"},
        ]
        self.store.save(messages)
        loaded = self.store.load()
        self.assertEqual(loaded, messages)

    def test_system_messages_are_stripped_on_save(self):
        messages = [
            {"role": "system", "content": "you are a bot"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ]
        self.store.save(messages)
        loaded = self.store.load()
        self.assertEqual(len(loaded), 2)
        roles = [m["role"] for m in loaded]
        self.assertNotIn("system", roles)

    def test_tool_call_roundtrip(self):
        messages = [
            {"role": "user", "content": "run a tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"/x"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"success":true,"content":"hello"}',
            },
            {"role": "assistant", "content": "done"},
        ]
        self.store.save(messages)
        loaded = self.store.load()
        self.assertEqual(len(loaded), 4)
        self.assertIn("tool_calls", loaded[1])
        self.assertEqual(loaded[1]["tool_calls"][0]["id"], "call_1")

    def test_incomplete_tool_sequence_stripped_on_save(self):
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "orphan", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}],
            },
        ]
        self.store.save(messages)
        loaded = self.store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["role"], "user")

    def test_save_creates_parent_directories(self):
        deep = os.path.join(self.tmp, "a", "b", "c", "mem.json")
        store = MemoryStore(deep)
        store.save([{"role": "user", "content": "nested"}])
        self.assertTrue(os.path.exists(_db_path(deep)))

    # --- clear ---

    def test_clear_removes_rows(self):
        self.store.save([{"role": "user", "content": "x"}])
        self.assertTrue(len(self.store.load()) > 0)
        self.store.clear()
        self.assertEqual(self.store.load(), [])

    def test_clear_no_file_is_noop(self):
        os.remove(self.store._db_path)
        self.store.clear()

    # --- properties ---

    def test_filepath_property(self):
        self.assertEqual(self.store.filepath, self.memfile)

    def test_db_path_is_derived(self):
        self.assertEqual(self.store._db_path, _db_path(self.memfile))
        self.assertTrue(self.store._db_path.endswith(".db"))

    # --- edge cases ---

    def test_save_bare_filename_does_not_crash(self):
        bare = os.path.join(self.tmp, "bare_memory.json")
        store = MemoryStore(bare)
        store.save([{"role": "user", "content": "bare test"}])
        self.assertTrue(os.path.exists(_db_path(bare)))
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["content"], "bare test")

    def test_corrupt_db_returns_empty(self):
        self.store.save([{"role": "user", "content": "x"}])
        # Shared connection caches in-memory; close it so the
        # corruption on disk is actually visible on next open.
        self.store.close()
        with open(self.store._db_path, "w") as f:
            f.write("not a valid sqlite database!!!!")
        result = self.store.load()
        self.assertEqual(result, [])

    def test_load_handles_bad_json_in_row(self):
        db_path = self.store._db_path
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO messages (role, content) VALUES (?, ?)",
                         ("user", "{{{ bad json"))
        loaded = self.store.load()
        self.assertTrue(len(loaded) >= 1)

    # ── test_output table ─────────────────────────────────────────────

    def test_test_output_get_empty_by_default(self):
        result = self.store.get_test_output()
        self.assertEqual(result, "")

    def test_test_output_save_and_get_roundtrip(self):
        content = "FAILED test_x.py::test_y - assert 1 == 2\n1 failed, 247 passed"
        self.store.save_test_output(content)
        result = self.store.get_test_output()
        self.assertEqual(result, content)

    def test_test_output_overwrite(self):
        self.store.save_test_output("first run")
        self.store.save_test_output("second run")
        self.assertEqual(self.store.get_test_output(), "second run")


# ---------------------------------------------------------------------------
# Pruning tests
# ---------------------------------------------------------------------------

class TestPruning(unittest.TestCase):
    """Verify that old messages are trimmed to stay within max_messages."""

    def _make_turn(self, n: int) -> list[dict]:
        """Build a complete turn: user → assistant."""
        return [
            {"role": "user", "content": f"q{n}"},
            {"role": "assistant", "content": f"a{n}"},
        ]

    def _make_tool_turn(self, n: int) -> list[dict]:
        """Build a tool-call turn: user → assistant(tool_calls) → tool."""
        return [
            {"role": "user", "content": f"run{n}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"call_{n}", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"call_{n}",
                "content": '{"success":true}',
            },
        ]

    def test_under_limit_passes_through(self):
        msgs = self._make_turn(1) + self._make_turn(2)
        kept, pruned = _prune_by_tokens(msgs, max_tokens=999999, max_messages=10)
        self.assertEqual(kept, msgs)
        self.assertEqual(pruned, [])

    def test_at_limit_passes_through(self):
        msgs = self._make_turn(1) + self._make_turn(2)  # 4 messages
        kept, pruned = _prune_by_tokens(msgs, max_tokens=999999, max_messages=4)
        self.assertEqual(kept, msgs)
        self.assertEqual(pruned, [])

    def test_over_limit_trims_oldest_turns(self):
        msgs = (self._make_turn(1) + self._make_turn(2) +
                self._make_turn(3) + self._make_turn(4))  # 8 messages
        kept, pruned = _prune_by_tokens(msgs, max_tokens=999999, max_messages=4)
        # Should keep turns 3 and 4 (last 4 messages)
        self.assertEqual(len(kept), 4)
        self.assertEqual(kept[0]["content"], "q3")

    def test_prune_preserves_user_boundary(self):
        """Cut always lands on a user message, not mid-turn."""
        msgs = (self._make_turn(1) + self._make_turn(2) +
                self._make_turn(3))  # 6 messages
        kept, pruned = _prune_by_tokens(msgs, max_tokens=999999, max_messages=3)
        # 3 messages would cut mid-turn-2. Should adjust to start at turn 2.
        self.assertEqual(kept[0]["role"], "user")
        self.assertIn(kept[0]["content"], {"q2", "q3"})

    def test_tool_turn_preserved_by_user_boundary(self):
        """Tool-call sequences stay intact because cut aligns to user."""
        msgs = (self._make_turn(1) +
                self._make_tool_turn(2) +
                self._make_turn(3))  # 2 + 3 + 2 = 7 messages
        kept, pruned = _prune_by_tokens(msgs, max_tokens=999999, max_messages=5)
        # Should keep tool turn 2 + turn 3 intact
        self.assertGreaterEqual(len(kept), 5)
        # The tool message should be present alongside its assistant
        roles = [m["role"] for m in kept]
        if "tool" in roles:
            tool_idx = roles.index("tool")
            self.assertEqual(roles[tool_idx - 1], "assistant")

    def test_save_prunes_automatically(self):
        """End-to-end: saving with a low max_messages prunes on write.
        A summary of pruned context is injected, so count may be max+1."""
        tmp = tempfile.mkdtemp()
        try:
            memfile = os.path.join(tmp, "mem.json")
            store = MemoryStore(memfile, max_messages=2, max_tokens=999999)
            msgs = self._make_turn(1) + self._make_turn(2) + self._make_turn(3)
            store.save(msgs)
            loaded = store.load()
            # 2 kept + optional 1 summary injection = ≤3
            self.assertLessEqual(len(loaded), 3)
            # The kept messages should include the most recent turn
            contents = [m["content"] for m in loaded]
            self.assertIn("q3", contents)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSON migration tests
# ---------------------------------------------------------------------------

class TestJSONMigration(unittest.TestCase):
    """Verify that old JSON memory files are migrated to SQLite."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_creates_db_from_json(self):
        json_path = os.path.join(self.tmp, "memory.json")
        old_messages = [
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "old reply"},
        ]
        with open(json_path, "w") as f:
            json.dump(old_messages, f)

        store = MemoryStore(json_path)
        loaded = store.load()
        self.assertEqual(loaded, old_messages)
        self.assertTrue(os.path.exists(store._db_path))
        self.assertTrue(os.path.exists(json_path))

    def test_migration_skips_if_db_exists(self):
        json_path = os.path.join(self.tmp, "memory.json")
        db_path = _db_path(json_path)

        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                ("user", '{"role":"user","content":"db message"}'),
            )
            conn.commit()

        with open(json_path, "w") as f:
            json.dump([{"role": "user", "content": "json message"}], f)

        store = MemoryStore(json_path)
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["content"], "db message")


if __name__ == "__main__":
    unittest.main()
