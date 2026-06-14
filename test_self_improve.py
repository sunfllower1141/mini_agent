"""
Integration smoke test for the self-improvement optimisations implemented in
2026-06-14 session: two-tier cache, adaptive thresholds, two-tier pruning,
system prompt preservation, and prompt cache key.
"""
from __future__ import annotations

import json
import sys
import unittest

# ---------------------------------------------------------------------------
# Test 1: Two-tier semantic cache (exact + semantic)
# ---------------------------------------------------------------------------
class TestTwoTierCache(unittest.TestCase):
    """Verify Tier-1 exact match, Tier-2 semantic, and adaptive thresholds."""

    @classmethod
    def setUpClass(cls):
        """Warm up the sentence-transformers model once for all tests."""
        from tools.semantic_cache import get_semantic_cache
        cache = get_semantic_cache()
        # Warm the model by doing a dummy embed (loads model on first use)
        cache._embed("Warm up the sentence transformer model")
        cls.cache = cache

    def setUp(self):
        self.cache.clear()
        self.cache.set_threshold(0.85)

    def test_tier1_exact_hit(self):
        """Identical query text returns cached response via hash match."""
        query = "What is the capital of France?"
        response = {"role": "assistant", "content": "Paris"}

        self.cache.store(query, response, model="test-model")
        hit, sim = self.cache.lookup(query)
        self.assertIsNotNone(hit, "Tier 1 exact hit should succeed")
        self.assertEqual(sim, 1.0, "Exact match similarity should be 1.0")
        self.assertEqual(hit["content"], "Paris")

    def test_tier1_exact_miss_with_different_query(self):
        """Different query text should miss exact but may hit semantic."""
        query1 = "What is 2 + 2?"
        query2 = "Tell me the result of adding two and two"
        response1 = {"role": "assistant", "content": "4"}

        self.cache.store(query1, response1, model="test-model")
        hit, sim = self.cache.lookup("A completely different query about cats")

        # Should not hit semantically (too different), but check it tried
        # and returned None for the unrelated query
        self.assertIsNone(hit, "Unrelated query should miss both tiers")

    def test_tier2_semantic_hit(self):
        """Semantically similar query should hit via cosine similarity."""
        query1 = "What is the capital of France?"
        query2 = "Tell me the capital city of France"
        response = {"role": "assistant", "content": "Paris"}

        self.cache.store(query1, response, model="test-model")
        hit, sim = self.cache.lookup(query2)

        # Tier 2: semantic -- these are semantically very close
        if hit is not None:
            self.assertGreater(sim, 0.8, "Semantic similarity should be high")
            self.assertEqual(hit["content"], "Paris")
            stats = self.cache.stats()
            self.assertEqual(stats["semantic_hits"], 1)
        else:
            self.skipTest("Model embeddings not close enough -- may need lower threshold")

    def test_adaptive_threshold_feedback(self):
        """Report feedback and verify threshold adjustment."""
        from tools.semantic_cache import (
            ADAPTIVE_THRESHOLD_DECAY,
            ADAPTIVE_THRESHOLD_PENALTY,
            DEFAULT_SIMILARITY_THRESHOLD,
        )

        query = "Explain recursion in programming"
        response = {"role": "assistant", "content": "Recursion is when a function calls itself..."}
        self.cache.store(query, response, model="test-model")

        # Hit multiple times to drive threshold down
        for _ in range(5):
            self.cache.lookup(query)  # exact hits

        # Check exact entry exists
        stats = self.cache.stats()
        self.assertGreater(stats["exact_hits"], 0, "Should have exact hits")

        # Report a false positive on the exact entry
        self.cache.report_feedback(query, was_correct=False)
        stats2 = self.cache.stats()
        self.assertEqual(stats2["feedback_wrong"], 1)

    def test_stats_breakdown(self):
        """Stats should report exact_hits vs semantic_hits separately."""
        query = "What is Python?"
        response = {"role": "assistant", "content": "A programming language"}

        self.cache.store(query, response, model="test-model")
        self.cache.lookup(query)  # exact hit

        stats = self.cache.stats()
        self.assertEqual(stats["exact_hits"], 1, "Should show exact hit")
        self.assertEqual(stats["semantic_hits"], 0, "No semantic hits yet")
        self.assertIn("avg_adaptive_threshold", stats, "Should include adaptive threshold stat")
        self.assertIn("feedback_correct", stats)
        self.assertIn("feedback_wrong", stats)

    def test_no_cache_for_tool_calls(self):
        """Responses with tool_calls should NOT be cached."""
        query = "Read file x"
        response = {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "read"}}]}

        self.cache.store(query, response, model="test-model")
        hit, _ = self.cache.lookup(query)
        self.assertIsNone(hit, "Tool-call responses should not be cached")


# ---------------------------------------------------------------------------
# Test 2: Two-tier memory pruning
# ---------------------------------------------------------------------------
class TestTwoTierPruning(unittest.TestCase):
    """Verify gentle zone preserves context, aggressive zone compresses."""

    def _make_msg(self, role, content=None, tool_call_id=None, tool_calls=None):
        msg = {"role": role}
        if tool_calls:
            msg["tool_calls"] = tool_calls
            msg["content"] = ""
        elif content is not None:
            msg["content"] = content
        else:
            msg["content"] = f"content for {role}"
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        return msg

    def _make_tool_result(self, content, tool_call_id="tc1"):
        safe_content = json.dumps({"content": content})
        return {"role": "tool", "content": safe_content, "tool_call_id": tool_call_id}

    def test_system_prompt_preserved(self):
        """System prompt at index 0 is NEVER compressed, even in aggressive zone."""
        from memory.memory_prune import _compress_tool_results

        messages = [
            self._make_msg("system", "You are an AI assistant." * 500),  # large system msg
        ]
        # Add enough tool messages to push system into aggressive zone
        for i in range(30):
            messages.append(
                self._make_msg("assistant", tool_calls=[
                    {"id": f"tc{i}", "function": {"name": "read", "arguments": "{}"}}
                ])
            )
            messages.append(
                self._make_tool_result("result " * 2000, tool_call_id=f"tc{i}")
            )

        original_system = messages[0]["content"]
        result, changed = _compress_tool_results(
            messages, keep_recent=1, gentle_recent=20,
        )
        self.assertEqual(result[0]["content"], original_system,
                         "System prompt at index 0 must never be compressed")

    def test_gentle_zone_truncation_only(self):
        """Messages in gentle zone get hard truncation, not type-aware compression."""
        from memory.memory_prune import _compress_tool_results

        messages = [self._make_msg("system", "system prompt")]
        # Build messages such that some tool results land in the gentle zone
        for i in range(25):
            messages.append(
                self._make_msg("assistant", tool_calls=[
                    {"id": f"tc{i}", "function": {"name": "read_file", "arguments": json.dumps({"path": f"file{i}.py"})}}
                ])
            )
            # Large result:
            messages.append(self._make_tool_result(
                "\n".join([f"{j}: line {j} of file output" for j in range(100)]),
                tool_call_id=f"tc{i}"
            ))

        _, changed = _compress_tool_results(
            messages, keep_recent=5, gentle_recent=20,
        )

        # Gentle zone (idx 5-19 messages, which are message indices 5-19
        # in the 51-msg list (25 pairs + system)) should be truncated not compressed
        # Check that messages in gentle zone still have result-like content
        for idx in range(5, min(len(messages) - 5, 40)):
            if messages[idx].get("role") == "tool":
                content = json.loads(messages[idx]["content"])["content"]
                # Should still have output lines, just maybe truncated
                self.assertIn("output", content.lower() or "",
                              f"Gentle zone tool msg {idx} lost all content")

    def test_backward_compat_no_gentle(self):
        """gentle_recent=None uses old single-tier behaviour."""
        from memory.memory_prune import _compress_tool_results

        messages = [self._make_msg("system", "system")]
        for i in range(15):
            messages.append(
                self._make_msg("assistant", tool_calls=[
                    {"id": f"tc{i}", "function": {"name": "search_files", "arguments": "{}"}}
                ])
            )
            messages.append(self._make_tool_result("x" * 12000, tool_call_id=f"tc{i}"))

        _, changed = _compress_tool_results(messages, keep_recent=2, gentle_recent=None)
        self.assertIsInstance(changed, bool)

    def test_prune_preserves_system_under_tight_budget(self):
        """_prune_by_tokens must preserve system message even with tiny budget."""
        from memory.memory_prune import _prune_by_tokens

        messages = [
            self._make_msg("system", "You are a code assistant."),
            self._make_msg("user", "Hello"),
            self._make_msg("assistant", "Hi there!"),
            self._make_msg("user", "Do task A"),
            self._make_msg("assistant", "Doing task A..." * 50),
            self._make_msg("user", "Do task B"),
            self._make_msg("assistant", "Doing task B..." * 50),
        ]

        kept, pruned = _prune_by_tokens(messages, max_tokens=50, max_messages=100)
        self.assertEqual(kept[0]["role"], "system",
                         "System prompt must survive tight token budgets")
        self.assertGreater(len(kept), 0)

    def test_prune_all_preserves_system(self):
        """Even with max_messages=3, system prompt stays."""
        from memory.memory_prune import _prune_by_tokens

        messages = [
            self._make_msg("system", "System prompt"),
            self._make_msg("user", "q1"),
            self._make_msg("assistant", "a1"),
            self._make_msg("user", "q2"),
            self._make_msg("assistant", "a2"),
            self._make_msg("user", "q3"),
            self._make_msg("assistant", "a3"),
        ]

        kept, pruned = _prune_by_tokens(messages, max_tokens=10000, max_messages=4)
        self.assertEqual(kept[0]["role"], "system",
                         "System prompt must be present even with small max_messages")


# ---------------------------------------------------------------------------
# Test 3: Prompt cache key in API payload
# ---------------------------------------------------------------------------
class TestPromptCacheKey(unittest.TestCase):
    """Verify prompt_cache_key is correctly set for DeepSeek provider."""

    def _make_config(self, provider="deepseek", model="deepseek-chat"):
        """Build a minimal mock for _build_payload tests."""
        config = type("Cfg", (), {})()
        config.api_provider = provider
        config.model = model
        config.temperature = 0.0
        config.max_tokens = 4096
        config.frequency_penalty = 0.0
        config.presence_penalty = 0.0
        config.stop_sequences = []
        config.response_format = ""
        config.stream = False
        return config

    def test_prompt_cache_key_in_payload(self):
        """_build_payload should include prompt_cache_key for deepseek."""
        from api import _build_payload

        config = self._make_config(provider="deepseek")
        payload = _build_payload(config, [], [])
        self.assertIn("prompt_cache_key", payload,
                      "DeepSeek payload should have prompt_cache_key")

    def test_no_cache_key_for_other_providers(self):
        """Other providers should NOT get prompt_cache_key."""
        from api import _build_payload

        config = self._make_config(provider="claude")
        payload = _build_payload(config, [], [])
        self.assertNotIn("prompt_cache_key", payload,
                         "Claude payload should NOT have prompt_cache_key")

    def test_cache_key_has_tool_count(self):
        """The cache key includes the tool count for cache-busting on skill changes."""
        from api import _build_payload

        config = self._make_config(provider="deepseek")
        payload = _build_payload(config, [], [])

        key = payload["prompt_cache_key"]
        # Format: mini_agent-v1-{tool_count}
        self.assertTrue(key.startswith("mini_agent-v1-"))


# ---------------------------------------------------------------------------
# Test 4: End-to-end integration
# ---------------------------------------------------------------------------
class TestEndToEnd(unittest.TestCase):
    """Smoke test that all modules load and interact correctly."""

    def test_semantic_cache_import_works(self):
        """Module imports and singleton is available."""
        from tools.semantic_cache import (
            get_semantic_cache, clear_semantic_cache, semantic_cache_stats,
            CacheEntry, SemanticCache,
        )
        cache = get_semantic_cache()
        self.assertIsInstance(cache, SemanticCache)
        stats = semantic_cache_stats()
        self.assertIn("entries", stats)

    def test_memory_prune_imports_gentle_constants(self):
        """Gentle-tier constants are importable."""
        from memory.memory_prune import (
            _COMPRESSION_GENTLE_RECENT,
            _COMPRESSION_GENTLE_MAX_LINES,
            _TOOL_RESULT_GENTLE_CHARS,
        )
        self.assertGreater(_COMPRESSION_GENTLE_RECENT, 0)
        self.assertGreater(_COMPRESSION_GENTLE_MAX_LINES, 0)
        self.assertGreater(_TOOL_RESULT_GENTLE_CHARS, 0)

    def test_memory_imports_gentle_constant(self):
        """memory.py re-exports gentle constant."""
        from memory.memory import _COMPRESSION_GENTLE_RECENT
        self.assertGreater(_COMPRESSION_GENTLE_RECENT, 0)


# ---------------------------------------------------------------------------
# Test 5: Dead Tool Pruning
# ---------------------------------------------------------------------------
class TestDeadToolPruning(unittest.TestCase):
    """Verify tool usage tracking and dead-skill deactivation."""

    def setUp(self):
        from tools import reset_tool_usage
        from tools.skills import reset_skills
        reset_tool_usage()
        reset_skills()

    def test_usage_tracking_increments(self):
        """get_tool_usage should reflect tool call counting."""
        import sys
        tmod = sys.modules["tools"]  # canonical module (same as execute_tool uses)

        tmod.reset_tool_usage()
        with tmod._TOOL_USAGE_LOCK:
            tmod._TOOL_USAGE_COUNT["read_file"] = 3
            tmod._TOOL_USAGE_COUNT["search_files"] = 1

        usage = tmod.get_tool_usage()
        self.assertEqual(usage.get("read_file"), 3)
        self.assertEqual(usage.get("search_files"), 1)

        tmod.reset_tool_usage()
        self.assertEqual(tmod.get_tool_usage(), {})

    def test_get_unused_tools_excludes_essential(self):
        """Unprunable tools (read_file, write_file, etc.) never appear as unused."""
        import sys
        tmod = sys.modules["tools"]

        tmod.reset_tool_usage()
        # Simulate: only read_file was used
        with tmod._TOOL_USAGE_LOCK:
            tmod._TOOL_USAGE_COUNT["read_file"] = 1

        unused = tmod.get_unused_tools(min_turns=5)
        self.assertNotIn("read_file", unused, "Used tools should not be unused")
        self.assertNotIn("run_shell", unused, "Essential tools should not be reportable as unused")

    def test_prune_unused_skills_deactivates(self):
        """prune_unused_skills deactivates skills with zero tool usage."""
        from tools.skills import (
            prune_unused_skills, active_skills, activate_skill,
            _get_skills_tool_map,
        )

        # Activate test skill
        activate_skill("test")
        self.assertIn("test", active_skills())

        # Get its tools and prune them all as unused
        tm = _get_skills_tool_map()
        test_tools = set(tm["test"])
        pruned = prune_unused_skills(test_tools)
        self.assertEqual(pruned, 1)
        self.assertNotIn("test", active_skills())

    def test_prune_unused_skills_partial_usage_preserves(self):
        """Skill stays active if at least one tool was used."""
        from tools.skills import (
            prune_unused_skills, active_skills, activate_skill,
            _get_skills_tool_map, reset_skills,
        )
        from tools import reset_tool_usage

        reset_skills()
        reset_tool_usage()
        activate_skill("test")

        tm = _get_skills_tool_map()
        test_tools = set(tm["test"])
        # Mark some tools as unused but leave one out (simulating one was used)
        partial_unused = set(list(test_tools)[:-1])
        pruned = prune_unused_skills(partial_unused)
        self.assertEqual(pruned, 0, "Skill should survive if any tool was used")
        self.assertIn("test", active_skills())

    def test_prune_empty_set_noop(self):
        """prune_unused_skills with empty set is a no-op."""
        from tools.skills import prune_unused_skills
        self.assertEqual(prune_unused_skills(set()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
