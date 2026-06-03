#!/usr/bin/env python3
"""Tests for the agent self-tracking system.

Covers STATE.txt, HANDOFF.md, CHANGELOG.md, handoff injection,
and self-review cycle support in memory.py and context_inject.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core import context_inject
from tools import _TOOL_CONTEXT


def _project_root() -> str:
    """Return the project root directory (where this test file lives)."""
    return os.path.dirname(os.path.abspath(__file__))


class TestStateFile(unittest.TestCase):
    """STATE.txt must exist and contain key architecture details."""

    def test_state_file_exists(self):
        """STATE.txt should exist in the project root."""
        root = _project_root()
        state_path = os.path.join(root, "STATE.txt")
        self.assertTrue(
            os.path.isfile(state_path),
            f"STATE.txt not found at {state_path}",
        )

    def test_state_file_has_key_sections(self):
        """STATE.txt must document active decisions, module map, and known issues."""
        root = _project_root()
        state_path = os.path.join(root, "STATE.txt")
        with open(state_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Active Decisions", content)
        self.assertIn("Module Map", content)
        self.assertIn("Known Issues", content)

    def test_state_file_lists_core_modules(self):
        """STATE.txt must reference the main modules."""
        root = _project_root()
        state_path = os.path.join(root, "STATE.txt")
        with open(state_path, encoding="utf-8") as f:
            content = f.read()
        for module in ("core/prompt.py", "core/config.py", "core/llm.py", "memory/memory.py",
                       "core/context_inject.py", "tools/failure_learning.py", "core/safety.py"):
            self.assertIn(
                module, content,
                f"STATE.txt should mention {module} in the module map",
            )


class TestHandoffFile(unittest.TestCase):
    """HANDOFF.md must exist and follow the expected format."""

    def test_handoff_file_exists(self):
        """HANDOFF.md should exist in the project root."""
        root = _project_root()
        handoff_path = os.path.join(root, "HANDOFF.md")
        self.assertTrue(
            os.path.isfile(handoff_path),
            f"HANDOFF.md not found at {handoff_path}",
        )

    def test_handoff_has_required_sections(self):
        """HANDOFF.md must document changes, pending items, and modified files."""
        root = _project_root()
        handoff_path = os.path.join(root, "HANDOFF.md")
        with open(handoff_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("What I Changed", content)
        self.assertIn("What's Pending", content)
        self.assertIn("Modified Files", content)

    def test_handoff_has_session_date(self):
        """HANDOFF.md must include a last session date."""
        root = _project_root()
        handoff_path = os.path.join(root, "HANDOFF.md")
        with open(handoff_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Last Session:", content)


class TestChangelogFile(unittest.TestCase):
    """CHANGELOG.md must exist and be structured."""

    def test_changelog_file_exists(self):
        """CHANGELOG.md should exist in the project root."""
        root = _project_root()
        changelog_path = os.path.join(root, "CHANGELOG.md")
        self.assertTrue(
            os.path.isfile(changelog_path),
            f"CHANGELOG.md not found at {changelog_path}",
        )

    def test_changelog_has_date_headings(self):
        """CHANGELOG.md must use date-based headings."""
        root = _project_root()
        changelog_path = os.path.join(root, "CHANGELOG.md")
        with open(changelog_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("## 2026-", content)

    def test_changelog_has_reason_entries(self):
        """CHANGELOG.md entries must include a 'Reason' section."""
        root = _project_root()
        changelog_path = os.path.join(root, "CHANGELOG.md")
        with open(changelog_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("### Reason", content)


class TestRulesHaveSelfModification(unittest.TestCase):
    """.mini_agent.rules must include self-modification guidance."""

    def test_rules_mention_state_txt(self):
        """Rules must reference STATE.txt."""
        root = _project_root()
        rules_path = os.path.join(root, ".mini_agent.rules")
        with open(rules_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("STATE.txt", content)

    def test_rules_mention_handoff(self):
        """Rules must reference HANDOFF.md."""
        root = _project_root()
        rules_path = os.path.join(root, ".mini_agent.rules")
        with open(rules_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("HANDOFF.md", content)

    def test_rules_mention_changelog(self):
        """Rules must reference CHANGELOG.md."""
        root = _project_root()
        rules_path = os.path.join(root, ".mini_agent.rules")
        with open(rules_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("CHANGELOG.md", content)

    def test_rules_have_self_review_cycle(self):
        """Rules must define a self-review cycle (Observe/Diagnose/Improve/Verify/Document)."""
        root = _project_root()
        rules_path = os.path.join(root, ".mini_agent.rules")
        with open(rules_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Self-Review Cycle", content)
        self.assertIn("Observe", content)
        self.assertIn("Diagnose", content)
        self.assertIn("Improve", content)
        self.assertIn("Verify", content)
        self.assertIn("Document", content)

    def test_rules_have_self_modification_header(self):
        """Rules must mention self-modification."""
        root = _project_root()
        rules_path = os.path.join(root, ".mini_agent.rules")
        with open(rules_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("SELF-MODIFICATION", content)


class TestMemoryHandoff(unittest.TestCase):
    """MemoryStore.write_handoff and read_handoff."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_memory.db")
        # Import memory module and create a MemoryStore
        from memory.memory import MemoryStore
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_handoff_creates_file(self):
        """write_handoff creates HANDOFF.md in the workspace root."""
        # Override filepath so it uses tmpdir
        self.store._filepath = os.path.join(self.tmpdir, "dummy.json")
        self.store.write_handoff(
            changes="- Added STATE.txt\n- Updated README.md",
            pending="- _json_rpc_shared.py cleanup",
            modified_files="- STATE.txt\n- README.md",
        )
        handoff_path = os.path.join(self.tmpdir, "HANDOFF.md")
        self.assertTrue(os.path.isfile(handoff_path))

    def test_write_handoff_has_correct_format(self):
        """write_handoff produces correctly formatted content."""
        self.store._filepath = os.path.join(self.tmpdir, "dummy.json")
        self.store.write_handoff(
            changes="- Test feature",
            pending="Nothing",
            modified_files="- test.py",
        )
        handoff_path = os.path.join(self.tmpdir, "HANDOFF.md")
        with open(handoff_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# Session Handoff", content)
        self.assertIn("What I Changed", content)
        self.assertIn("- Test feature", content)
        self.assertIn("What's Pending", content)
        self.assertIn("Nothing", content)
        self.assertIn("Modified Files", content)
        self.assertIn("- test.py", content)

    def test_read_handoff_returns_content(self):
        """read_handoff returns the HANDOFF.md content."""
        self.store._filepath = os.path.join(self.tmpdir, "dummy.json")
        self.store.write_handoff(changes="- A change")
        content = self.store.read_handoff()
        self.assertIsNotNone(content)
        self.assertIn("- A change", content)

    def test_read_handoff_returns_none_when_missing(self):
        """read_handoff returns None when HANDOFF.md doesn't exist."""
        self.store._filepath = os.path.join(self.tmpdir, "dummy.json")
        # Don't write anything
        content = self.store.read_handoff()
        self.assertIsNone(content)

    def test_write_handoff_empty_pending_ok(self):
        """write_handoff works with no pending/modified_files."""
        self.store._filepath = os.path.join(self.tmpdir, "dummy.json")
        self.store.write_handoff(changes="- Just a change")
        handoff_path = os.path.join(self.tmpdir, "HANDOFF.md")
        self.assertTrue(os.path.isfile(handoff_path))
        with open(handoff_path, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("What's Pending", content)
        self.assertNotIn("Modified Files", content)


class TestHandoffContextInjection(unittest.TestCase):
    """context_inject._inject_handoff_context injects HANDOFF.md at session start."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create a dummy HANDOFF.md
        self.handoff_path = os.path.join(self.tmpdir, "HANDOFF.md")
        with open(self.handoff_path, "w", encoding="utf-8") as f:
            f.write("# Session Handoff\n## Last Session\n### What I Changed\n- Test change")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_injects_when_flag_false(self):
        """Should inject handoff content into messages on first call."""
        _TOOL_CONTEXT._handoff_injected = False
        messages: list[dict] = []
        context_inject._inject_handoff_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertIn("Session handoff from your previous session", msg["content"])
        self.assertIn("Test change", msg["content"])
        self.assertTrue(msg["_transient"])

    def test_sets_flag_after_injection(self):
        """Should set _handoff_injected = True after injection."""
        _TOOL_CONTEXT._handoff_injected = False
        messages: list[dict] = []
        context_inject._inject_handoff_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertTrue(_TOOL_CONTEXT._handoff_injected)

    def test_skips_when_flag_true(self):
        """Should not inject if already injected this session."""
        _TOOL_CONTEXT._handoff_injected = True
        messages: list[dict] = []
        context_inject._inject_handoff_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertEqual(len(messages), 0)

    def test_skips_when_no_workspace(self):
        """Should not inject if workspace_root is empty."""
        _TOOL_CONTEXT._handoff_injected = False
        messages: list[dict] = []
        context_inject._inject_handoff_context(
            messages, workspace_root="",
        )
        self.assertEqual(len(messages), 0)

    def test_skips_when_file_missing(self):
        """Should not inject if HANDOFF.md doesn't exist."""
        _TOOL_CONTEXT._handoff_injected = False
        messages: list[dict] = []
        empty_dir = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty_dir, exist_ok=True)
        context_inject._inject_handoff_context(
            messages, workspace_root=empty_dir,
        )
        self.assertEqual(len(messages), 0)

    def test_handoff_injected_flag_initialized(self):
        """_handoff_injected must be initialized on AgentContext."""
        self.assertTrue(
            hasattr(_TOOL_CONTEXT, "_handoff_injected"),
            "_handoff_injected should be defined on _TOOL_CONTEXT",
        )


class TestStateContextInjection(unittest.TestCase):
    """context_inject._inject_state_context injects STATE.txt at session start."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create a dummy STATE.txt
        self.state_path = os.path.join(self.tmpdir, "STATE.txt")
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("# Architecture State\n- Module: foo\n- Issue: bar")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_injects_when_flag_false(self):
        """Should inject state content into messages on first call."""
        _TOOL_CONTEXT._state_txt_injected = False
        messages: list[dict] = []
        context_inject._inject_state_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertIn("Architecture state from your last session", msg["content"])
        self.assertIn("Module: foo", msg["content"])
        self.assertTrue(msg["_transient"])

    def test_sets_flag_after_injection(self):
        """Should set _state_txt_injected = True after injection."""
        _TOOL_CONTEXT._state_txt_injected = False
        messages: list[dict] = []
        context_inject._inject_state_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertTrue(_TOOL_CONTEXT._state_txt_injected)

    def test_skips_when_flag_true(self):
        """Should not inject if already injected this session."""
        _TOOL_CONTEXT._state_txt_injected = True
        messages: list[dict] = []
        context_inject._inject_state_context(
            messages, workspace_root=self.tmpdir,
        )
        self.assertEqual(len(messages), 0)

    def test_skips_when_no_workspace(self):
        """Should not inject if workspace_root is empty."""
        _TOOL_CONTEXT._state_txt_injected = False
        messages: list[dict] = []
        context_inject._inject_state_context(
            messages, workspace_root="",
        )
        self.assertEqual(len(messages), 0)

    def test_skips_when_file_missing(self):
        """Should not inject if STATE.txt doesn't exist."""
        _TOOL_CONTEXT._state_txt_injected = False
        messages: list[dict] = []
        empty_dir = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty_dir, exist_ok=True)
        context_inject._inject_state_context(
            messages, workspace_root=empty_dir,
        )
        self.assertEqual(len(messages), 0)

    def test_state_txt_flag_initialized(self):
        """_state_txt_injected must be initialized on AgentContext."""
        self.assertTrue(
            hasattr(_TOOL_CONTEXT, "_state_txt_injected"),
            "_state_txt_injected should be defined on _TOOL_CONTEXT",
        )


class TestReadmeHasSelfModSection(unittest.TestCase):
    """README.md must include the Agent Self-Modification section."""

    def test_readme_has_self_modification_section(self):
        """README.md must document self-modification architecture."""
        root = _project_root()
        readme_path = os.path.join(root, "README.md")
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Agent Self-Modification", content)

    def test_readme_references_tracking_files(self):
        """README.md must mention STATE.txt, HANDOFF.md, CHANGELOG.md."""
        root = _project_root()
        readme_path = os.path.join(root, "README.md")
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("STATE.txt", content)
        self.assertIn("HANDOFF.md", content)
        self.assertIn("CHANGELOG.md", content)

    def test_readme_documents_safety_boundaries(self):
        """README.md must explain safety boundaries for self-modification."""
        root = _project_root()
        readme_path = os.path.join(root, "README.md")
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Safety Boundaries", content)

    def test_readme_has_evolution_cycle(self):
        """README.md must document the self-evolution loop."""
        root = _project_root()
        readme_path = os.path.join(root, "README.md")
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Observe", content)
        self.assertIn("Diagnose", content)
        self.assertIn("Improve", content)


class TestAutoHandoff(unittest.TestCase):
    """write_session_handoff auto-generates HANDOFF.md from git diff."""

    def test_write_session_handoff_static_method(self):
        """MemoryStore.write_session_handoff should exist and be callable."""
        from memory.memory import MemoryStore
        self.assertTrue(hasattr(MemoryStore, "write_session_handoff"))
        self.assertTrue(callable(MemoryStore.write_session_handoff))

    def test_write_session_handoff_writes_file(self):
        """write_session_handoff should create HANDOFF.md in workspace."""
        import tempfile
        from memory.memory import MemoryStore
        with tempfile.TemporaryDirectory() as tmpdir:
            # Init a git repo so the function can run git commands
            subprocess.run(["git", "init", tmpdir], capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "--allow-empty",
                          "-m", "init"], capture_output=True)
            start_head = subprocess.check_output(
                ["git", "-C", tmpdir, "rev-parse", "HEAD"], text=True,
            ).strip()

            path = MemoryStore.write_session_handoff(
                tmpdir, start_head=start_head,
                pending="Fix the flux capacitor",
                notes="Don't forget the tests",
            )
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("## Last Session", content)
            self.assertIn("### What I Changed", content)
            self.assertIn("### What's Pending", content)
            self.assertIn("Fix the flux capacitor", content)
            self.assertIn("### Modified Files", content)
            self.assertIn("### Notes", content)
            self.assertIn("Don't forget the tests", content)

    def test_write_session_handoff_no_git(self):
        """write_session_handoff should not crash when git is unavailable."""
        import tempfile
        from memory.memory import MemoryStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = MemoryStore.write_session_handoff(
                tmpdir, start_head=None, pending="N/A",
            )
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("### What I Changed", content)
            self.assertIn("(no git changes detected)", content)

    def test_tool_dispatched(self):
        """write_session_handoff must be registered in tool dispatch."""
        from tools import _TOOL_DISPATCH
        self.assertIn("write_session_handoff", _TOOL_DISPATCH)
        self.assertTrue(callable(_TOOL_DISPATCH["write_session_handoff"]))

    def test_tool_in_schema(self):
        """write_session_handoff must be in TOOLS schema."""
        from tools.schema import TOOLS
        names = [t["function"]["name"]
                for t in TOOLS if t.get("type") == "function"]
        self.assertIn("write_session_handoff", names)

    def test_prompt_mentions_handoff(self):
        """System prompt must instruct the agent to call write_session_handoff."""
        root = _project_root()
        prompt_path = os.path.join(root, "core", "prompt.py")
        with open(prompt_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("write_session_handoff", content)
        self.assertIn("Session handoff", content)

    def test_session_start_head_initialized(self):
        """_session_start_head must be initialized on AgentContext."""
        from tools import _TOOL_CONTEXT
        self.assertTrue(hasattr(_TOOL_CONTEXT, "_session_start_head"))


if __name__ == "__main__":
    unittest.main()
