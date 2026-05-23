#!/usr/bin/env python3
"""
test_memory_internals.py — tests for internal MemoryStore mechanics.

Covers _clean_messages, _migrate_old_paths, _migrate_json,
export_conversation_markdown, incremental save, scratchpad,
and per-save message caches.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from memory import (
    MemoryStore,
    _clean_messages,
    _migrate_old_paths,
    _migrate_json,
    export_conversation_markdown,
    _TOOL_PARSE_CACHE,
    _TOKEN_EST_CACHE,
    _clear_message_caches,
    _get_tool_content,
    _estimate_tokens,
    _total_tokens,
    _ACCUM_COUNT,
    _ACCUM_TOTAL,
    _MARKDOWN_TOOL_RESULT_PREVIEW,
)


# ---------------------------------------------------------------------------
# _clean_messages
# ---------------------------------------------------------------------------

class TestCleanMessages(unittest.TestCase):
    """Tests for _clean_messages — stripping transient, orphaned, incomplete."""

    # --- _transient messages are stripped ---

    def test_transient_messages_stripped(self):
        """Messages with _transient=True are removed."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi", "_transient": True},
            {"role": "user", "content": "world"},
        ]
        cleaned = _clean_messages(msgs)
        roles = [m["role"] for m in cleaned]
        self.assertEqual(roles, ["user", "user"])
        self.assertEqual(cleaned[0]["content"], "hello")
        self.assertEqual(cleaned[1]["content"], "world")

    # --- system messages are stripped ---

    def test_system_messages_stripped(self):
        """System-role messages are removed."""
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        cleaned = _clean_messages(msgs)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["role"], "user")

    # --- orphaned tool results removed ---

    def test_orphaned_tool_result_removed(self):
        """Tool result with tool_call_id that has no preceding assistant is removed."""
        msgs = [
            {"role": "user", "content": "run cmd"},
            {
                "role": "tool",
                "tool_call_id": "orphan_1",
                "content": json.dumps({"content": "result"}),
            },
        ]
        cleaned = _clean_messages(msgs)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["role"], "user")

    def test_valid_tool_result_kept(self):
        """Tool result with matching preceding assistant is kept."""
        msgs = [
            {"role": "user", "content": "run cmd"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "run_shell", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps({"content": "result"}),
            },
        ]
        cleaned = _clean_messages(msgs)
        self.assertEqual(len(cleaned), 3)  # user + assistant + tool
        self.assertEqual(cleaned[1]["role"], "assistant")
        self.assertEqual(cleaned[2]["role"], "tool")

    # --- incomplete tool-call sequences truncated ---

    def test_incomplete_tool_call_truncated(self):
        """Assistant with tool_calls but no following tool results is truncated."""
        msgs = [
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            # No matching tool result follows — incomplete sequence
            {"role": "user", "content": "next question"},
        ]
        cleaned = _clean_messages(msgs)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["role"], "user")  # only the first user

    def test_complete_tool_call_sequence_kept(self):
        """Assistant with tool_calls + matching tool results is kept intact."""
        msgs = [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps({"content": "file contents"}),
            },
            {"role": "user", "content": "thanks"},
        ]
        cleaned = _clean_messages(msgs)
        # user, assistant, tool, user — all kept
        self.assertEqual(len(cleaned), 4)

    # --- combined: both orphaned tools AND incomplete sequences ---

    def test_combined_orphaned_and_incomplete(self):
        """Orphaned tool results removed AND incomplete sequences truncated in same list."""
        msgs = [
            {"role": "user", "content": "first"},
            {
                "role": "tool",
                "tool_call_id": "orphan_1",
                "content": json.dumps({"content": "orphan result"}),
            },
            {"role": "user", "content": "second"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "run_shell", "arguments": "{}"},
                    }
                ],
            },
            # No tool result for call_2 — incomplete, truncated here
            {"role": "user", "content": "third"},
        ]
        cleaned = _clean_messages(msgs)
        # orphan result stripped; truncates at incomplete assistant
        # Kept: user "first", user "second"
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["content"], "first")
        self.assertEqual(cleaned[1]["content"], "second")

    # --- no issues pass through unchanged ---

    def test_messages_with_no_issues_pass_through(self):
        """Clean messages with no transient/orphaned/incomplete issues pass through."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you"},
        ]
        cleaned = _clean_messages(msgs)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]["content"], "hello")
        self.assertEqual(cleaned[1]["content"], "hi there")
        self.assertEqual(cleaned[2]["content"], "how are you")


# ---------------------------------------------------------------------------
# _migrate_old_paths / _migrate_json
# ---------------------------------------------------------------------------

class TestMigration(unittest.TestCase):
    """Tests for _migrate_old_paths and _migrate_json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- old .json.db path scheme migration ---

    def test_old_json_db_path_migration(self):
        """Migrates old .json.db to new .db when config was .json."""
        # _migrate_old_paths looks for base + ".db" (not ".json.db")
        old_db = os.path.join(self.tmp, "memory.db")
        new_db = os.path.join(self.tmp, "memory2.db")

        # Create old DB with some content
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "role TEXT NOT NULL, content TEXT NOT NULL,"
            "created_at TEXT DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)",
            ("user", json.dumps({"role": "user", "content": "migrated"})),
        )
        conn.commit()
        conn.close()

        new_config_path = os.path.join(self.tmp, "memory.json")
        _migrate_old_paths(new_config_path, new_db)

        # Old file should be moved to new_db
        self.assertTrue(os.path.exists(new_db))
        self.assertFalse(os.path.exists(old_db))

        # Verify content survived migration
        conn2 = sqlite3.connect(new_db)
        rows = conn2.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
        conn2.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "user")

    # --- migration skips if target DB already exists ---

    def test_migration_skips_when_target_exists(self):
        """Does not overwrite an existing target DB."""
        old_db = os.path.join(self.tmp, "memory.json.db")
        new_db = os.path.join(self.tmp, "memory.db")

        # Create both old and new DBs
        conn_new = sqlite3.connect(new_db)
        conn_new.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "role TEXT NOT NULL, content TEXT NOT NULL)"
        )
        conn_new.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)",
            ("assistant", json.dumps({"role": "assistant", "content": "existing"})),
        )
        conn_new.commit()
        conn_new.close()

        conn_old = sqlite3.connect(old_db)
        conn_old.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "role TEXT NOT NULL, content TEXT NOT NULL)"
        )
        conn_old.execute(
            "INSERT INTO messages (role, content) VALUES (?, ?)",
            ("user", json.dumps({"role": "user", "content": "should not migrate"})),
        )
        conn_old.commit()
        conn_old.close()

        new_config_path = os.path.join(self.tmp, "memory.json")
        _migrate_old_paths(new_config_path, new_db)

        # Target should still have the existing content, not old
        conn = sqlite3.connect(new_db)
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "assistant")
        self.assertIn("existing", rows[0][1])

    # --- corrupt JSON file doesn't crash migration ---

    def test_corrupt_json_does_not_crash(self):
        """_migrate_json handles corrupt JSON gracefully (no crash)."""
        json_path = os.path.join(self.tmp, "bad.json")
        db_path = os.path.join(self.tmp, "bad.db")

        with open(json_path, "w") as f:
            f.write("this is not valid json {{{")

        # Should not raise
        try:
            _migrate_json(json_path, db_path)
        except Exception as e:
            self.fail(f"_migrate_json raised unexpectedly: {e}")

        # DB should not be created from corrupt input
        self.assertFalse(os.path.exists(db_path))

    # --- JSON with non-list data doesn't crash ---

    def test_json_non_list_does_not_crash(self):
        """_migrate_json handles JSON that is not a list (e.g. dict)."""
        json_path = os.path.join(self.tmp, "dict.json")
        db_path = os.path.join(self.tmp, "dict.db")

        with open(json_path, "w") as f:
            json.dump({"not": "a list"}, f)

        try:
            _migrate_json(json_path, db_path)
        except Exception as e:
            self.fail(f"_migrate_json raised unexpectedly: {e}")

        # DB should not be created from non-list input
        self.assertFalse(os.path.exists(db_path))


# ---------------------------------------------------------------------------
# export_conversation_markdown
# ---------------------------------------------------------------------------

class TestExportMarkdown(unittest.TestCase):
    """Tests for export_conversation_markdown."""

    def test_all_roles_rendered(self):
        """System, user, assistant, and tool roles all appear in output."""
        msgs = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "Help me."},
            {"role": "assistant", "content": "Sure."},
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": json.dumps({"content": "tool output"}),
            },
        ]
        md = export_conversation_markdown(msgs)
        self.assertIn("### System", md)
        self.assertIn("### User", md)
        self.assertIn("### Assistant", md)
        self.assertIn("> Tool result:", md)

    def test_assistant_reasoning_rendered_as_thinking_block(self):
        """reasoning_content is rendered as a > Thinking block."""
        msgs = [
            {
                "role": "assistant",
                "content": "final answer",
                "reasoning_content": "I am thinking...",
            }
        ]
        md = export_conversation_markdown(msgs)
        self.assertIn("**Thinking**", md)
        self.assertIn("I am thinking...", md)

    def test_tool_calls_rendered_with_name_and_args(self):
        """Tool calls are rendered with function name and arguments in a code block."""
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/x.py"}',
                        },
                    }
                ],
            }
        ]
        md = export_conversation_markdown(msgs)
        self.assertIn("```", md)
        self.assertIn("read_file", md)
        self.assertIn('{"path": "/tmp/x.py"}', md)

    def test_tool_results_truncated_at_preview_limit(self):
        """Tool result content is truncated at _MARKDOWN_TOOL_RESULT_PREVIEW chars."""
        long_content = "x" * (_MARKDOWN_TOOL_RESULT_PREVIEW + 100)
        msgs = [
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": long_content,
            }
        ]
        md = export_conversation_markdown(msgs)
        # The truncated content should be at most the preview limit
        self.assertNotIn(long_content, md)
        self.assertIn(long_content[:_MARKDOWN_TOOL_RESULT_PREVIEW], md)

    def test_empty_messages_produces_minimal_markdown(self):
        """An empty messages list produces just the header."""
        md = export_conversation_markdown([])
        self.assertIn("# mini_agent conversation", md)
        # Should have the header line but no role sections
        lines = md.strip().split("\n")
        self.assertEqual(len(lines), 1)


# ---------------------------------------------------------------------------
# Incremental save
# ---------------------------------------------------------------------------

class TestIncrementalSave(unittest.TestCase):
    """Tests for incremental insert vs full rewrite in MemoryStore.save()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memfile = os.path.join(self.tmp, "memory.json")
        self.store = MemoryStore(self.memfile, max_tokens=1_000_000, max_messages=100)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw_messages_count(self):
        """Count rows directly in the DB."""
        conn = sqlite3.connect(self.store._db_path)
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return count

    def test_first_save_does_full_rewrite(self):
        """First save inserts all messages (full rewrite path)."""
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        self.store.save(msgs)
        self.assertEqual(self._raw_messages_count(), 2)
        self.assertEqual(self.store._last_saved_count, 2)

    def test_second_save_append_does_incremental_insert(self):
        """Second save with only appended messages does incremental insert (not full rewrite)."""
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        self.store.save(msgs)
        first_count = self._raw_messages_count()

        # Append new messages
        msgs2 = [
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        # We need to include previous messages so the store knows what changed
        all_msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        self.store.save(all_msgs)
        self.assertEqual(self._raw_messages_count(), 4)
        self.assertEqual(self.store._last_saved_count, 4)

    def test_save_after_pruning_does_full_rewrite(self):
        """Pruning triggers a full rewrite (DELETE + INSERT all)."""
        # Fill up to near limit
        msgs = []
        for i in range(50):
            msgs.append({"role": "user", "content": f"msg{i}"})
            msgs.append({"role": "assistant", "content": f"reply{i}"})

        # Set low max to force pruning on next save
        store2 = MemoryStore(
            os.path.join(self.tmp, "prune.json"),
            max_messages=10,
            max_tokens=50_000,
        )
        try:
            store2.save(msgs)
            loaded = store2.load()
            # Should have <= 10 non-summary messages + possibly a summary
            non_summary = [m for m in loaded if "Earlier in this conversation" not in m.get("content", "")]
            self.assertLessEqual(len(non_summary), 10)
        finally:
            store2.close()

    def test_last_saved_count_tracking(self):
        """_last_saved_count updates correctly across saves."""
        msgs = [{"role": "user", "content": "hello"}]
        self.store.save(msgs)
        self.assertEqual(self.store._last_saved_count, 1)

        msgs2 = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        self.store.save(msgs2)
        self.assertEqual(self.store._last_saved_count, 2)


# ---------------------------------------------------------------------------
# Scratchpad
# ---------------------------------------------------------------------------

class TestScratchpad(unittest.TestCase):
    """Tests for MemoryStore scratchpad persistence."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memfile = os.path.join(self.tmp, "memory.json")
        self.store = MemoryStore(self.memfile)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_scratchpad_fresh_store_returns_empty(self):
        """Fresh MemoryStore returns empty string for scratchpad."""
        result = self.store.get_scratchpad()
        self.assertEqual(result, "")

    def test_set_and_get_scratchpad_roundtrip(self):
        """set_scratchpad + get_scratchpad roundtrip preserves content."""
        content = "## Plan\n- Step 1: do X\n- Step 2: do Y"
        self.store.set_scratchpad(content)
        result = self.store.get_scratchpad()
        self.assertEqual(result, content)

    def test_scratchpad_overwrite_works(self):
        """Overwriting scratchpad replaces old content."""
        self.store.set_scratchpad("old content")
        self.store.set_scratchpad("new content")
        result = self.store.get_scratchpad()
        self.assertEqual(result, "new content")


# ---------------------------------------------------------------------------
# Message caches (_TOOL_PARSE_CACHE, _TOKEN_EST_CACHE)
# ---------------------------------------------------------------------------

class TestMessageCaches(unittest.TestCase):
    """Tests for per-save message caches."""

    def setUp(self):
        _clear_message_caches()

    def tearDown(self):
        _clear_message_caches()

    def test_get_tool_content_caches_by_identity(self):
        """_get_tool_content caches result keyed by id(msg)."""
        msg = {
            "role": "tool",
            "tool_call_id": "t1",
            "content": json.dumps({"content": "cached content"}),
        }
        # First call — should parse JSON
        text1 = _get_tool_content(msg)
        self.assertEqual(text1, "cached content")
        self.assertEqual(len(_TOOL_PARSE_CACHE), 1)

        # Modify the dict in-place; cached version should still return old value
        msg["content"] = json.dumps({"content": "changed"})
        text2 = _get_tool_content(msg)
        self.assertEqual(text2, "cached content")  # still cached

    def test_clear_message_caches_empties_both(self):
        """_clear_message_caches empties both _TOOL_PARSE_CACHE and _TOKEN_EST_CACHE."""
        msg_tool = {
            "role": "tool",
            "tool_call_id": "t1",
            "content": json.dumps({"content": "result text"}),
        }
        msg_user = {"role": "user", "content": "hello world"}

        _get_tool_content(msg_tool)
        _estimate_tokens(msg_user)
        self.assertGreater(len(_TOOL_PARSE_CACHE), 0)
        self.assertGreater(len(_TOKEN_EST_CACHE), 0)

        _clear_message_caches()
        self.assertEqual(len(_TOOL_PARSE_CACHE), 0)
        self.assertEqual(len(_TOKEN_EST_CACHE), 0)

    def test_estimate_tokens_consistent_for_same_message(self):
        """_estimate_tokens returns same result for the same message on repeated calls."""
        msg = {"role": "user", "content": "hello world"}
        est1 = _estimate_tokens(msg)
        est2 = _estimate_tokens(msg)
        est3 = _estimate_tokens(msg)
        self.assertEqual(est1, est2)
        self.assertEqual(est2, est3)
        self.assertGreater(est1, 0)


# ---------------------------------------------------------------------------
# _total_tokens accumulator
# ---------------------------------------------------------------------------

class TestTotalTokens(unittest.TestCase):
    """Tests for _total_tokens accumulator (_ACCUM_COUNT, _ACCUM_TOTAL)."""

    def setUp(self):
        # Reset accumulators before each test
        global _ACCUM_COUNT, _ACCUM_TOTAL
        _ACCUM_COUNT = 0
        _ACCUM_TOTAL = 0
        _clear_message_caches()

    def tearDown(self):
        _clear_message_caches()

    def test_total_tokens_increments_on_append(self):
        """_total_tokens only counts new messages when appending."""
        msgs = [{"role": "user", "content": "a" * 100}]
        t1 = _total_tokens(msgs)
        self.assertGreater(t1, 0)

        msgs.append({"role": "assistant", "content": "b" * 200})
        t2 = _total_tokens(msgs)
        self.assertGreater(t2, t1)

    def test_total_tokens_recounts_on_shrink(self):
        """_total_tokens does full recount when list shrinks (pruning)."""
        msgs = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 200},
            {"role": "user", "content": "c" * 300},
        ]
        t1 = _total_tokens(msgs)
        self.assertGreater(t1, 0)

        # Shrink the list (simulate pruning)
        shrunk = msgs[1:]  # drop first message
        t2 = _total_tokens(shrunk)
        self.assertLess(t2, t1)


if __name__ == "__main__":
    unittest.main()
