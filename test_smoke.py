#!/usr/bin/env python3
"""Smoke tests that verify the agent can actually boot without crashing.

These tests run the full startup path — init_session, build_system_prompt,
build_startup_context, and tool dispatch — in a real temp workspace.
Catches AttributeErrors, ImportErrors, and missing tool handlers that
unit tests in isolation miss.
"""

import os
import sys
import tempfile
import unittest


class TestStartupSessionNoCrash(unittest.TestCase):
    """Verify that init_session completes without any Attribute/Import errors."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini_agent_smoke_")
        # Create a minimal STATE.txt so build_startup_context doesn't fail
        with open(os.path.join(self.tmp, "STATE.txt"), "w") as f:
            f.write("# test state\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_session_does_not_crash(self):
        """Runs the full init_session() startup path in a temp workspace."""
        from config import init_session

        session_data = init_session(self.tmp)
        messages = session_data["messages"]
        config = session_data["config"]

        # Basic sanity checks
        self.assertIsInstance(messages, list)
        self.assertGreater(len(messages), 0, "Expected at least one system message")
        self.assertIsNotNone(config)


class TestSystemPromptBuilds(unittest.TestCase):
    """Verify build_system_prompt runs without errors."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini_agent_smoke_")
        with open(os.path.join(self.tmp, "STATE.txt"), "w") as f:
            f.write("# test state\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_builds_with_default_config(self):
        """build_system_prompt should return a non-empty string."""
        from prompt import build_system_prompt
        from config import AgentConfig

        config = AgentConfig()
        config.workspace = self.tmp
        prompt = build_system_prompt(config)
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 100, "Prompt looks suspiciously short")


class TestBuildStartupContext(unittest.TestCase):
    """Verify build_startup_context completes without errors."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini_agent_smoke_")
        with open(os.path.join(self.tmp, "STATE.txt"), "w") as f:
            f.write("# test\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_startup_context_builds(self):
        """build_startup_context should return a non-empty string."""
        from config import build_startup_context

        ctx = build_startup_context(self.tmp)
        self.assertIsInstance(ctx, str)
        self.assertIn("WORKSPACE CONTEXT", ctx)

    def test_startup_context_with_knowledge(self):
        """build_startup_context should accept and display project knowledge."""
        from config import build_startup_context

        knowledge = [
            {"category": "pattern", "summary": "Test pattern", "detail": "Details here"},
            {"category": "session_summary", "summary": "Last session summary", "detail": ""},
        ]
        ctx = build_startup_context(self.tmp, knowledge=knowledge)
        self.assertIn("Test pattern", ctx)
        self.assertIn("Last session summary", ctx)

    def test_startup_context_without_knowledge(self):
        """build_startup_context should work fine with knowledge=None."""
        from config import build_startup_context

        ctx = build_startup_context(self.tmp, knowledge=None)
        self.assertIn("WORKSPACE CONTEXT", ctx)


class TestAllToolsDispatchable(unittest.TestCase):
    """Verify that every tool in the schema has a registered handler."""

    def test_all_tools_have_handlers(self):
        """No tool in TOOLS should be missing from _TOOL_DISPATCH."""
        from tools import _TOOL_DISPATCH, _TOOL_SUMMARIES
        from tools.schema import TOOLS

        for tool_def in TOOLS:
            name = tool_def["function"]["name"]
            self.assertIn(name, _TOOL_DISPATCH,
                          f"Tool '{name}' has no dispatch handler in _TOOL_DISPATCH")
            self.assertIn(name, _TOOL_SUMMARIES,
                          f"Tool '{name}' has no summary handler in _TOOL_SUMMARIES")

    def test_tool_count_matches_state_txt(self):
        """Sanity check: 44 tools expected per STATE.txt."""
        from tools.schema import TOOLS
        self.assertEqual(len(TOOLS), 44,
                         f"Expected 44 tools, got {len(TOOLS)}. Update STATE.txt if changed.")


class TestProjectKnowledgeMethods(unittest.TestCase):
    """Verify MemoryStore has all methods referenced by init_session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mini_agent_smoke_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_memory_has_session_summary_methods(self):
        """get_latest_session_summary and capture_session_summary must exist."""
        from memory import MemoryStore

        db_path = os.path.join(self.tmp, ".mini_agent_memory.db")
        store = MemoryStore(db_path, max_messages=500)

        self.assertTrue(hasattr(store, "get_latest_session_summary"),
                        "MemoryStore missing get_latest_session_summary method")
        self.assertTrue(hasattr(store, "capture_session_summary"),
                        "MemoryStore missing capture_session_summary method")
        self.assertTrue(hasattr(store, "add_knowledge"),
                        "MemoryStore missing add_knowledge method")
        self.assertTrue(hasattr(store, "get_top_knowledge"),
                        "MemoryStore missing get_top_knowledge method")
        self.assertTrue(hasattr(store, "bump_knowledge"),
                        "MemoryStore missing bump_knowledge method")

    def test_session_summary_methods_work(self):
        """get_latest_session_summary should return None for empty DB."""
        from memory import MemoryStore

        db_path = os.path.join(self.tmp, ".mini_agent_memory.db")
        store = MemoryStore(db_path, max_messages=500)
        result = store.get_latest_session_summary()
        self.assertIsNone(result, "Empty DB should return None, not crash")

    def test_knowledge_roundtrip(self):
        """add_knowledge → get_top_knowledge should return the entry."""
        from memory import MemoryStore

        db_path = os.path.join(self.tmp, ".mini_agent_memory.db")
        store = MemoryStore(db_path, max_messages=500)
        store.add_knowledge("Test learning", category="test", detail="for roundtrip test")
        entries = store.get_top_knowledge(limit=5)
        self.assertGreaterEqual(len(entries), 1)
        self.assertEqual(entries[0]["summary"], "Test learning")


if __name__ == "__main__":
    unittest.main()
