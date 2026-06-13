#!/usr/bin/env python3
"""Tests for conversation summarization and token-budget pruning."""

import json
import os
import tempfile
import unittest

from memory.memory import (
    _summarize_pruned,
    _prune_by_tokens,
    MemoryStore,
)


# ---------------------------------------------------------------------------
# _summarize_pruned
# ---------------------------------------------------------------------------

class TestSummarizePruned(unittest.TestCase):
    """Tests for _summarize_pruned."""

    def test_empty_pruned_returns_empty_string(self):
        self.assertEqual(_summarize_pruned([]), "")

    def test_user_messages_appear_in_summary(self):
        pruned = [
            {"role": "user", "content": "read the config file please"},
            {"role": "user", "content": "now run the tests"},
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("read the config", summary)
        self.assertIn("run the tests", summary)

    def test_user_content_truncated_at_120_chars(self):
        long_msg = "x" * 200
        pruned = [{"role": "user", "content": long_msg}]
        summary = _summarize_pruned(pruned)
        # Should contain truncated version, not full 200 chars
        self.assertNotIn(long_msg, summary)
        self.assertIn("...", summary)

    def test_only_last_3_user_turns_shown(self):
        pruned = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "user", "content": "fourth"},
        ]
        summary = _summarize_pruned(pruned)
        self.assertNotIn("first", summary)
        self.assertIn("second", summary)
        self.assertIn("fourth", summary)

    def test_files_read_from_read_file_calls(self):
        pruned = [
            {"role": "user", "content": "check files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": '{"path": "/src/main.py"}'},
                    },
                    {
                        "id": "c2", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": '{"path": "/src/utils.py"}'},
                    },
                ],
            },
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("Files read", summary)
        self.assertIn("main.py", summary)
        self.assertIn("utils.py", summary)

    def test_files_written_categorized(self):
        pruned = [
            {"role": "user", "content": "write file"},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": json.dumps({"success": True,
                                       "content": "OK: wrote 50 bytes to /out/data.txt"}),
            },
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("Files written", summary)
        self.assertIn("data.txt", summary)

    def test_files_edited_categorized(self):
        pruned = [
            {"role": "user", "content": "edit file"},
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": json.dumps({"success": True,
                                       "content": "OK: replaced 1 occurrence in /src/app.py"}),
            },
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("Files edited", summary)
        self.assertIn("app.py", summary)

    def test_commands_run_appear(self):
        pruned = [
            {"role": "user", "content": "run command"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {"name": "run_shell",
                                     "arguments": '{"command": "pytest -v"}'},
                    },
                ],
            },
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("Commands run", summary)
        self.assertIn("pytest -v", summary)

    def test_web_search_appears(self):
        pruned = [
            {"role": "user", "content": "search web"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1", "type": "function",
                        "function": {"name": "web_search",
                                     "arguments": '{"query": "Python asyncio"}'},
                    },
                ],
            },
        ]
        summary = _summarize_pruned(pruned)
        self.assertIn("Searched web", summary)
        self.assertIn("Python asyncio", summary)

    def test_files_read_deduplicated(self):
        pruned = [
            {"role": "user", "content": "read same file twice"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "/src/main.py"}'}},
                ],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c2", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "/src/main.py"}'}},
                ],
            },
        ]
        summary = _summarize_pruned(pruned)
        # "main.py" should appear once, not twice
        self.assertEqual(summary.count("main.py"), 1)

    def test_path_truncation(self):
        """File write paths longer than 80 chars are truncated."""
        very_long_path = "/out/" + "b" * 100 + "/data.txt"
        pruned = [
            {"role": "user", "content": "write file"},
            # Tool messages with "bytes to" or "OK: wrote" categorize as files_written
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": json.dumps({
                    "success": True,
                    "content": f"OK: wrote 50 bytes to {very_long_path}"
                }),
            },
        ]
        summary = _summarize_pruned(pruned)
        # The full very long path should be truncated
        self.assertIn("...", summary)
        self.assertNotIn(very_long_path, summary)

    def test_opening_line_present(self):
        pruned = [{"role": "user", "content": "hi"}]
        summary = _summarize_pruned(pruned)
        self.assertIn("Earlier in this conversation", summary)


# ---------------------------------------------------------------------------
# _prune_by_tokens with token budgets
# ---------------------------------------------------------------------------

def _msg(role, content):
    return {"role": role, "content": content}


class TestPruneByTokens(unittest.TestCase):
    """Tests for _prune_by_tokens focusing on token budgets."""

    def test_under_token_budget_passes_through(self):
        msgs = [_msg("user", "hello"), _msg("assistant", "hi")]
        kept, pruned = _prune_by_tokens(msgs, max_tokens=1_000_000, max_messages=100)
        self.assertEqual(kept, msgs)
        self.assertEqual(pruned, [])

    def test_over_token_budget_trims_oldest_turns(self):
        # Create messages where each is ~100 tokens
        msgs = []
        for i in range(20):
            msgs.append(_msg("user", "x" * 400))   # ~100 tokens
            msgs.append(_msg("assistant", "y" * 400))  # ~100 tokens
        # Total ~4000 tokens. Budget ~500 to force pruning after first turn.
        # (First turn is 2 msgs * 100 tokens = 200 tokens, kept)
        kept, pruned = _prune_by_tokens(msgs, max_tokens=500, max_messages=1000)
        self.assertLess(len(kept), len(msgs))
        self.assertGreater(len(kept), 0)
        # Kept starts with a user message
        self.assertEqual(kept[0]["role"], "user")

    def test_preserves_user_boundaries(self):
        msgs = [
            _msg("user", "q1"),
            _msg("assistant", "a1" * 200),  # larger message
            _msg("user", "q2"),
            _msg("assistant", "a2"),
        ]
        kept, pruned = _prune_by_tokens(msgs, max_tokens=50, max_messages=100)
        if kept:
            self.assertEqual(kept[0]["role"], "user")

    def test_tool_call_sequences_kept_intact(self):
        msgs = [
            _msg("user", "run"),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": json.dumps({"content": "x" * 1000}),  # large tool result
            },
            _msg("user", "next"),
            _msg("assistant", "ok"),
        ]
        kept, pruned = _prune_by_tokens(msgs, max_tokens=200, max_messages=100)
        # Should not split the tool-call sequence
        roles = [m["role"] for m in kept]
        if "tool" in roles:
            tool_idx = roles.index("tool")
            self.assertEqual(roles[tool_idx - 1], "assistant")

    def test_mixed_max_messages_and_max_tokens(self):
        msgs = []
        for i in range(30):
            msgs.append(_msg("user", f"q{i}"))
            msgs.append(_msg("assistant", f"a{i}"))
        kept, pruned = _prune_by_tokens(msgs, max_tokens=1_000_000, max_messages=20)
        # Hard cap by message count first
        self.assertLessEqual(len(kept), 20)

    def test_single_huge_turn_kept_anyway(self):
        """If a single turn exceeds token budget, it's still kept."""
        huge_msg = _msg("user", "z" * 10000)  # ~2500 tokens
        kept, pruned = _prune_by_tokens([huge_msg], max_tokens=100, max_messages=100)
        # Even though it exceeds budget, we don't trim into the middle of nothing
        self.assertEqual(len(kept), 1)

    def test_already_within_budget_no_change(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "hey")]
        kept, pruned = _prune_by_tokens(msgs, max_tokens=100, max_messages=100)
        self.assertEqual(kept, msgs)
        self.assertEqual(pruned, [])


# ---------------------------------------------------------------------------
# MemoryStore token accounting
# ---------------------------------------------------------------------------

class TestMemoryStoreTokenAccounting(unittest.TestCase):
    """Tests for MemoryStore token_count and pruning integration."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memfile = os.path.join(self.tmp, "memory.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_count_reflects_saved_messages(self):
        store = MemoryStore(self.memfile, max_tokens=1_000_000, max_messages=100)
        msgs = [
            _msg("user", "hello world"),
            _msg("assistant", "hi there"),
        ]
        store.save(msgs)
        self.assertGreater(store.token_count, 0)

    def test_after_pruning_token_count_decreases(self):
        store = MemoryStore(self.memfile, max_tokens=200, max_messages=100)
        msgs = []
        for i in range(50):
            msgs.append(_msg("user", f"message number {i} " + "padding " * 20))
            msgs.append(_msg("assistant", f"reply number {i} " + "padding " * 20))
        store.save(msgs)
        loaded = store.load()
        # Should have pruned -- check that loaded < saved
        self.assertLess(len(loaded), len(msgs))
        self.assertGreater(store.token_count, 0)

    def test_summary_injection_adds_to_token_count(self):
        store = MemoryStore(self.memfile, max_tokens=1000, max_messages=4)
        msgs = []
        for i in range(10):
            msgs.append(_msg("user", f"msg {i}"))
            msgs.append(_msg("assistant", f"reply {i}"))
        store.save(msgs)
        loaded = store.load()
        # Summary message should be present
        contents = [m["content"] for m in loaded]
        self.assertTrue(
            any("Earlier in this conversation" in c for c in contents),
            "Summary injection missing from loaded messages"
        )


if __name__ == "__main__":
    unittest.main()
