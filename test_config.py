#!/usr/bin/env python3
"""
test_config.py — tests for the AgentConfig configuration layer.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from config import AgentConfig, CONFIG_FILENAME, DEFAULT_API_KEY, DEFAULT_MODEL, DEFAULT_API_URL, OLLAMA_DEFAULT_MODEL, OLLAMA_DEFAULT_API_URL


# Env vars that override AgentConfig defaults / TOML values. Tests must clear
# these so behavior is deterministic regardless of the developer's shell.
_OVERRIDING_ENV_VARS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_URL",
    "CLAUDE_API_KEY", "CLAUDE_API_URL", "CLAUDE_MODEL",
    "XAI_API_KEY", "XAI_API_URL", "XAI_MODEL",
    "OLLAMA_API_URL", "OLLAMA_API_KEY", "OLLAMA_MODEL",
    "SUB_AGENT_API_KEY", "API_PROVIDER",
    "AGENT_WORKSPACE", "EXA_API_KEY", "OPENAI_API_KEY",
)


def _pop_overriding_env() -> dict:
    """Remove env vars that would override config; return prior values."""
    saved = {}
    for name in _OVERRIDING_ENV_VARS:
        if name in os.environ:
            saved[name] = os.environ.pop(name)
    return saved


def _restore_env(saved: dict) -> None:
    """Restore env vars previously removed by _pop_overriding_env."""
    for name, value in saved.items():
        os.environ[name] = value


class TestAgentConfigDefaults(unittest.TestCase):
    """Test default values and factory behaviour without overrides."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        # Unset env vars that override defaults
        self._saved_env = _pop_overriding_env()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        _restore_env(self._saved_env)

    def test_defaults_are_set(self):
        config = AgentConfig.load(self.workspace)
        # No API keys set → falls back to Ollama (reachable via Tailscale)
        self.assertEqual(config.api_provider, "ollama")
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)
        self.assertEqual(config.api_key, DEFAULT_API_KEY)
        self.assertEqual(config.api_url, OLLAMA_DEFAULT_API_URL)
        self.assertEqual(config.workspace, self.workspace)
        self.assertFalse(config.allow_overwrites)
        self.assertFalse(config.stream)
        self.assertTrue(config.verbose)
        self.assertFalse(config.unrestricted)

    def test_workspace_is_stored(self):
        config = AgentConfig.load("/some/path")
        self.assertEqual(config.workspace, "/some/path")

    def test_unrestricted_default_is_false(self):
        """Verify unrestricted defaults to False for workspace safety."""
        config = AgentConfig()
        self.assertFalse(config.unrestricted)


class TestAgentConfigTOML(unittest.TestCase):
    """Test loading from .mini_agent.toml."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        # Unset env vars that override TOML values
        self._saved_env = _pop_overriding_env()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)
        _restore_env(self._saved_env)

    def _write_toml(self, content: str) -> None:
        path = os.path.join(self.workspace, CONFIG_FILENAME)
        with open(path, "w") as f:
            f.write(content)

    def test_toml_overrides_defaults(self):
        self._write_toml("""[agent]\nmodel = "custom-model"\nallow_overwrites = true\n""")
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, "custom-model")
        self.assertTrue(config.allow_overwrites)
        # No API keys → falls back to Ollama URL
        self.assertEqual(config.api_url, OLLAMA_DEFAULT_API_URL)

    def test_missing_toml_does_not_crash(self):
        # No config file written — falls back to Ollama (no API keys)
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)

    def test_corrupt_toml_prints_warning(self):
        self._write_toml("not valid {{{ toml")
        with patch("sys.stderr") as mock_stderr:
            config = AgentConfig.load(self.workspace)
        # Should not crash, falls back to Ollama (no API keys)
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)
        # Should have printed a warning
        self.assertTrue(mock_stderr.write.called)

    def test_unknown_keys_are_ignored(self):
        self._write_toml("""[agent]\nunknown_key = "should be ignored"\nmodel = "ok"\n""")
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, "ok")
        self.assertFalse(hasattr(config, "unknown_key"))

    def test_wrong_type_prints_warning_and_skips(self):
        self._write_toml("""[agent]\nmodel = 123\nallow_overwrites = "not a bool"\n""")
        with patch("sys.stderr") as mock_stderr:
            config = AgentConfig.load(self.workspace)
        # model should stay default (wrong type) — falls back to Ollama
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)
        # allow_overwrites should stay default (wrong type)
        self.assertFalse(config.allow_overwrites)
        self.assertTrue(mock_stderr.write.called)

    def test_all_recognised_keys_load(self):
        self._write_toml("""[agent]
model = "m"
api_key = "k"
api_url = "u"
allow_overwrites = true
stream = true
verbose = false
""")
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, "m")
        self.assertEqual(config.api_key, "k")
        self.assertEqual(config.api_url, "u")
        self.assertTrue(config.allow_overwrites)
        self.assertTrue(config.stream)
        self.assertFalse(config.verbose)

    def test_empty_toml_is_fine(self):
        self._write_toml("")
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)

    def test_toml_without_agent_section_is_fine(self):
        self._write_toml("[other]\nkey = 1\n")
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.model, OLLAMA_DEFAULT_MODEL)


class TestAgentConfigEnvVars(unittest.TestCase):
    """Test environment variable overrides."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}, clear=True)
    def test_env_api_key_overrides_default(self):
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.api_key, "env-key")

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}, clear=True)
    def test_env_api_key_overrides_toml(self):
        path = os.path.join(self.workspace, CONFIG_FILENAME)
        with open(path, "w") as f:
            f.write('[agent]\napi_key = "toml-key"\n')
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.api_key, "env-key")

    @patch.dict(os.environ, {"DEEPSEEK_API_URL": "https://custom.api/v1"}, clear=True)
    def test_env_api_url_overrides_default(self):
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.api_url, "https://custom.api/v1")

    @patch.dict(os.environ, {"DEEPSEEK_API_URL": "https://env.url"}, clear=True)
    def test_env_api_url_overrides_toml(self):
        path = os.path.join(self.workspace, CONFIG_FILENAME)
        with open(path, "w") as f:
            f.write('[agent]\napi_url = "https://toml.url"\n')
        config = AgentConfig.load(self.workspace)
        self.assertEqual(config.api_url, "https://env.url")

    @patch.dict(os.environ, {"AGENT_WORKSPACE": "/env/ws"}, clear=True)
    def test_env_workspace_overrides_default(self):
        config = AgentConfig.load(self.workspace)
        # workspace is always set to the passed-in value in step 3
        self.assertEqual(config.workspace, self.workspace)


class TestAgentConfigCLIFlags(unittest.TestCase):
    """Test CLI flag overrides."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_stream_flag(self):
        with patch.object(sys, "argv", ["mini_agent.py", "--stream"]):
            config = AgentConfig.load(self.workspace)
        self.assertTrue(config.stream)

    def test_quiet_flag(self):
        with patch.object(sys, "argv", ["mini_agent.py", "--quiet"]):
            config = AgentConfig.load(self.workspace)
        self.assertFalse(config.verbose)

    def test_cli_overrides_toml(self):
        path = os.path.join(self.workspace, CONFIG_FILENAME)
        with open(path, "w") as f:
            f.write("[agent]\nstream = false\nverbose = true\n")
        with patch.object(sys, "argv", ["mini_agent.py", "--stream", "--quiet"]):
            config = AgentConfig.load(self.workspace)
        self.assertTrue(config.stream)
        self.assertFalse(config.verbose)

    def test_no_flags_leaves_toml_values(self):
        path = os.path.join(self.workspace, CONFIG_FILENAME)
        with open(path, "w") as f:
            f.write("[agent]\nstream = true\nverbose = false\n")
        with patch.object(sys, "argv", ["mini_agent.py"]):
            config = AgentConfig.load(self.workspace)
        self.assertTrue(config.stream)
        self.assertFalse(config.verbose)

    def test_workspace_flag_passthrough(self):
        # --workspace is resolved before load(), we just verify it's stored
        config = AgentConfig.load("/custom/ws")
        self.assertEqual(config.workspace, "/custom/ws")


if __name__ == "__main__":
    unittest.main()
