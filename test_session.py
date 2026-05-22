"""Tests for session.py — session management and DB path resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config as cfg
import session


class TestSessionDBPath(unittest.TestCase):
    """Tests for _session_db_path()."""

    def test_default_session_uses_memory_filename(self):
        path = session._session_db_path("/workspace", None)
        expected = os.path.join("/workspace", cfg.MEMORY_FILENAME)
        self.assertEqual(path, expected)

    def test_named_session_gets_suffixed_path(self):
        path = session._session_db_path("/workspace", "mysession")
        base = cfg.MEMORY_FILENAME.replace(".db", "")
        expected = os.path.join("/workspace", f"{base}_session_mysession.db")
        self.assertEqual(path, expected)

    def test_empty_string_session_uses_default(self):
        # Empty string is falsy, so it falls through to the default path
        path = session._session_db_path("/workspace", "")
        expected = os.path.join("/workspace", cfg.MEMORY_FILENAME)
        self.assertEqual(path, expected)

    def test_path_uses_workspace_root(self):
        path = session._session_db_path("/tmp/ws", "test")
        self.assertTrue(path.startswith("/tmp/ws"))


class TestListSessions(unittest.TestCase):
    """Tests for list_sessions()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_workspace_returns_empty(self):
        sessions_list = session.list_sessions(self.tmpdir)
        self.assertEqual(sessions_list, [])

    def test_default_session_present(self):
        default_path = os.path.join(self.tmpdir, cfg.MEMORY_FILENAME)
        with open(default_path, "w") as f:
            f.write("")
        sessions_list = session.list_sessions(self.tmpdir)
        self.assertIn("default", sessions_list)
        self.assertEqual(sessions_list[0], "default")  # default inserted first

    def test_named_sessions_detected(self):
        base = cfg.MEMORY_FILENAME.replace(".db", "_session_")
        # Create a named session DB
        named_path = os.path.join(self.tmpdir, f"{base}mysession.db")
        with open(named_path, "w") as f:
            f.write("")
        sessions_list = session.list_sessions(self.tmpdir)
        self.assertIn("mysession", sessions_list)
        self.assertNotIn("default", sessions_list)

    def test_multiple_named_sessions(self):
        base = cfg.MEMORY_FILENAME.replace(".db", "_session_")
        for name in ("alpha", "beta", "gamma"):
            path = os.path.join(self.tmpdir, f"{base}{name}.db")
            with open(path, "w") as f:
                f.write("")
        sessions_list = session.list_sessions(self.tmpdir)
        self.assertEqual(set(sessions_list), {"alpha", "beta", "gamma"})

    def test_non_db_files_ignored(self):
        # Create a file that looks like a session but isn't .db
        base = cfg.MEMORY_FILENAME.replace(".db", "_session_")
        with open(os.path.join(self.tmpdir, f"{base}junk.txt"), "w") as f:
            f.write("")
        # Also a file with wrong prefix
        with open(os.path.join(self.tmpdir, "other_session_test.db"), "w") as f:
            f.write("")
        sessions_list = session.list_sessions(self.tmpdir)
        self.assertEqual(sessions_list, [])


class TestDeleteSession(unittest.TestCase):
    """Tests for delete_session()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cannot_delete_default(self):
        ok, msg = session.delete_session(self.tmpdir, "default")
        self.assertFalse(ok)
        self.assertIn("Cannot delete", msg)

    def test_delete_nonexistent_returns_error(self):
        ok, msg = session.delete_session(self.tmpdir, "ghost")
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_delete_existing_session(self):
        base = cfg.MEMORY_FILENAME.replace(".db", "_session_")
        named_path = os.path.join(self.tmpdir, f"{base}mysession.db")
        with open(named_path, "w") as f:
            f.write("data")
        self.assertTrue(os.path.isfile(named_path))
        ok, msg = session.delete_session(self.tmpdir, "mysession")
        self.assertTrue(ok)
        self.assertIn("Deleted", msg)
        self.assertFalse(os.path.isfile(named_path))


class TestSwitchSession(unittest.TestCase):
    """Tests for switch_session() — session save + load."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_config(self, **overrides):
        """Minimal AgentConfig for session tests."""
        from config import AgentConfig
        defaults = {
            "workspace": self.tmpdir,
            "api_provider": "deepseek",
            "api_key": "test-key",
            "model": "test-model",
            "max_tokens": 4096,
            "context_window": 32000,
            "max_messages": 500,
            "stream": False,
            "temperature": 0.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "stop_sequences": None,
            "response_format": None,
            "routing_model": "",
            "socks_proxy": "",
            "sub_agent_max_turns": 25,
        }
        defaults.update(overrides)
        return AgentConfig(**defaults)

    @patch("memory.MemoryStore")
    @patch("prompt.build_system_prompt", return_value="system prompt")
    @patch("prompt.build_startup_context", return_value="startup context")
    def test_switch_with_no_current_memory(self, mock_ctx, mock_prompt, MockStore):
        mock_memory = MagicMock()
        mock_memory._skip_load = True
        mock_memory.load.return_value = []
        mock_memory.get_top_knowledge.return_value = []
        MockStore.return_value = mock_memory

        config = self._make_config()
        result = session.switch_session(
            self.tmpdir, "newsession", None, config
        )

        self.assertIn("memory", result)
        self.assertIn("messages", result)
        # Messages should have 2 system messages (prompt + context)
        self.assertGreaterEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][0]["role"], "system")

    @patch("memory.MemoryStore")
    @patch("prompt.build_system_prompt", return_value="system prompt")
    @patch("prompt.build_startup_context", return_value="startup context")
    def test_switch_closes_current_memory(self, mock_ctx, mock_prompt, MockStore):
        current_memory = MagicMock()
        mock_memory = MagicMock()
        mock_memory._skip_load = True
        mock_memory.load.return_value = []
        mock_memory.get_top_knowledge.return_value = []
        MockStore.return_value = mock_memory

        config = self._make_config()
        session.switch_session(self.tmpdir, "newsession", current_memory, config)

        current_memory.close.assert_called_once()

    @patch("memory.MemoryStore")
    @patch("prompt.build_system_prompt", return_value="system prompt")
    @patch("prompt.build_startup_context", return_value="startup context")
    def test_switch_restores_saved_messages(self, mock_ctx, mock_prompt, MockStore):
        saved_msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        mock_memory = MagicMock()
        mock_memory._skip_load = False
        mock_memory.load.return_value = saved_msgs
        mock_memory.get_top_knowledge.return_value = []
        MockStore.return_value = mock_memory

        config = self._make_config()
        result = session.switch_session(
            self.tmpdir, "newsession", None, config
        )

        # The saved messages should be appended after system messages
        contents = [m.get("content") for m in result["messages"] if m.get("role") == "user"]
        self.assertIn("hello", contents)


if __name__ == "__main__":
    unittest.main()
