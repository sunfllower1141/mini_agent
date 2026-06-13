#!/usr/bin/env python3
"""Tests for prompt.py — system prompt builder and session header.

Refactored for the immutable system prompt design:
- build_system_prompt() → static behavioral rules + provider note only
- build_session_header() → dynamic metadata (date, OS, workspace, safety flags, etc.)
"""

import os
import unittest

from core.config import AgentConfig
from core.prompt import build_system_prompt, build_session_header


class TestBuildSystemPrompt(unittest.TestCase):
    """Tests for the STATIC (immutable) system prompt."""

    def _config(self, **kwargs) -> AgentConfig:
        cfg = AgentConfig()
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    # ------------------------------------------------------------------
    # Identity & key phrases
    # ------------------------------------------------------------------

    def test_static_prompt_has_identity(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("You are mini_agent", prompt)
        self.assertIn("terminal AI coding assistant", prompt)

    def test_static_prompt_has_key_modules(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("prompt.py", prompt)
        self.assertIn("config.py", prompt)
        self.assertIn("llm.py", prompt)
        self.assertIn("api.py", prompt)
        self.assertIn("memory.py", prompt)
        self.assertIn("safety.py", prompt)
        self.assertIn("README.md", prompt)

    def test_static_prompt_has_behavior_section(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("Behavior:", prompt)
        self.assertIn("Be direct and concise", prompt)

    def test_static_prompt_has_tool_guidance(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("find_symbol", prompt)
        self.assertIn("edit_file", prompt)

    def test_static_prompt_has_loop_prevention(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("Loop prevention", prompt)
        self.assertIn("Same tool + same args 2x = STUCK", prompt)

    def test_static_prompt_has_provider_note(self):
        prompt = build_system_prompt(self._config())
        # Default provider is deepseek
        self.assertIn("DeepSeek is prone to tool-call loops", prompt)

    # ------------------------------------------------------------------
    # Immutability: NO dynamic content
    # ------------------------------------------------------------------

    def test_no_dynamic_content_in_system_prompt(self):
        """System prompt must NOT contain session metadata (immutability check)."""
        prompt = build_system_prompt(self._config(unrestricted=True))
        self.assertNotIn("unrestricted = True", prompt)
        self.assertNotIn("unrestricted = False", prompt)
        self.assertNotIn("allow_overwrites", prompt)
        self.assertNotIn("approve_write_ops", prompt)
        self.assertNotIn("WORKSPACE   :", prompt)
        self.assertNotIn("[SESSION METADATA", prompt)

    def test_system_prompt_unchanged_by_config(self):
        """build_system_prompt should return identical output regardless of config flags."""
        p1 = build_system_prompt(self._config(unrestricted=True, workspace="/foo"))
        p2 = build_system_prompt(self._config(unrestricted=False, workspace="/bar"))
        self.assertEqual(p1, p2,
                         "System prompt should be immutable regardless of config")

    # ------------------------------------------------------------------
    # Prompt length
    # ------------------------------------------------------------------

    def test_prompt_length_within_limit(self):
        prompt = build_system_prompt(self._config())
        length = len(prompt)
        # Static prompt is ~3,900 chars — allow some headroom for provider notes
        self.assertLess(length, 8000,
                        f"Prompt is {length} chars (expected ~3900)")
        self.assertGreater(length, 2000,
                           f"Prompt is only {length} chars, expected > 2000")

    def test_prompt_not_empty(self):
        prompt = build_system_prompt(self._config())
        self.assertGreater(len(prompt), 500,
                           "Prompt should be substantially longer than 500 chars")


class TestBuildSessionHeader(unittest.TestCase):
    """Tests for the DYNAMIC session header (injected as a user message)."""

    def _config(self, **kwargs) -> AgentConfig:
        cfg = AgentConfig()
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    # ------------------------------------------------------------------
    # unrestricted flag
    # ------------------------------------------------------------------

    def test_unrestricted_true_shown(self):
        hdr = build_session_header(self._config(unrestricted=True))
        self.assertIn("unrestricted = True", hdr)
        self.assertIn("NO workspace boundary checks", hdr)

    def test_unrestricted_false_shown(self):
        hdr = build_session_header(self._config(unrestricted=False))
        self.assertIn("unrestricted = False", hdr)
        self.assertIn("reads/writes restricted to workspace", hdr)

    # ------------------------------------------------------------------
    # allow_overwrites flag
    # ------------------------------------------------------------------

    def test_allow_overwrites_true_shown(self):
        hdr = build_session_header(self._config(allow_overwrites=True))
        self.assertIn("allow_overwrites = True", hdr)

    def test_allow_overwrites_false_shown(self):
        hdr = build_session_header(self._config(allow_overwrites=False))
        self.assertIn("allow_overwrites = False", hdr)

    # ------------------------------------------------------------------
    # approve_write_ops flag
    # ------------------------------------------------------------------

    def test_approve_write_ops_true_shown(self):
        hdr = build_session_header(self._config(approve_write_ops=True))
        self.assertIn("approve_write_ops = True", hdr)

    def test_approve_write_ops_false_shown(self):
        hdr = build_session_header(self._config(approve_write_ops=False))
        self.assertIn("approve_write_ops = False", hdr)

    # ------------------------------------------------------------------
    # workspace path
    # ------------------------------------------------------------------

    def test_workspace_in_header(self):
        test_ws = os.path.abspath("/tmp/test_ws")
        hdr = build_session_header(self._config(workspace="/tmp/test_ws"))
        self.assertIn(f"WORKSPACE   : {test_ws}", hdr)

    def test_workspace_defaults_to_cwd(self):
        hdr = build_session_header(self._config())
        cwd = os.path.abspath(os.getcwd())
        self.assertIn(f"WORKSPACE   : {cwd}", hdr)

    # ------------------------------------------------------------------
    # session metadata structure
    # ------------------------------------------------------------------

    def test_session_metadata_header(self):
        hdr = build_session_header(self._config())
        self.assertIn("[SESSION METADATA", hdr)
        self.assertIn("DATE", hdr)
        self.assertIn("OS", hdr)
        self.assertIn("SHELL", hdr)
        self.assertIn("SAFETY FLAGS", hdr)

    def test_session_header_not_empty(self):
        hdr = build_session_header(self._config())
        self.assertGreater(len(hdr), 500,
                           "Session header should be substantial")


if __name__ == "__main__":
    unittest.main()
