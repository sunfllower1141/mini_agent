#!/usr/bin/env python3
"""Tests for prompt.py — system prompt builder."""

import os
import unittest

from core.config import AgentConfig
from core.prompt import build_system_prompt


class TestBuildSystemPrompt(unittest.TestCase):

    def _config(self, **kwargs) -> AgentConfig:
        """Build a minimal AgentConfig with the given overrides."""
        cfg = AgentConfig()
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    # ------------------------------------------------------------------
    # unrestricted flag
    # ------------------------------------------------------------------

    def test_unrestricted_true_shown(self):
        prompt = build_system_prompt(self._config(unrestricted=True))
        self.assertIn("unrestricted = True", prompt)
        self.assertIn("NO workspace boundary checks", prompt)

    def test_unrestricted_false_shown(self):
        prompt = build_system_prompt(self._config(unrestricted=False))
        self.assertIn("unrestricted = False", prompt)
        self.assertIn("reads/writes restricted to workspace", prompt)

    # ------------------------------------------------------------------
    # allow_overwrites flag
    # ------------------------------------------------------------------

    def test_allow_overwrites_true_shown(self):
        prompt = build_system_prompt(self._config(allow_overwrites=True))
        self.assertIn("allow_overwrites = True", prompt)

    def test_allow_overwrites_false_shown(self):
        prompt = build_system_prompt(self._config(allow_overwrites=False))
        self.assertIn("allow_overwrites = False", prompt)

    # ------------------------------------------------------------------
    # approve_write_ops flag
    # ------------------------------------------------------------------

    def test_approve_write_ops_true_shown(self):
        prompt = build_system_prompt(self._config(approve_write_ops=True))
        self.assertIn("approve_write_ops = True", prompt)

    def test_approve_write_ops_false_shown(self):
        prompt = build_system_prompt(self._config(approve_write_ops=False))
        self.assertIn("approve_write_ops = False", prompt)

    # ------------------------------------------------------------------
    # workspace path in header
    # ------------------------------------------------------------------

    def test_workspace_in_header(self):
        prompt = build_system_prompt(self._config(workspace="/tmp/test_ws"))
        self.assertIn("WORKSPACE   : /tmp/test_ws", prompt)

    def test_workspace_defaults_to_cwd(self):
        prompt = build_system_prompt(self._config())
        cwd = os.path.abspath(os.getcwd())
        self.assertIn(f"WORKSPACE   : {cwd}", prompt)

    # ------------------------------------------------------------------
    # static prompt key phrases exist
    # ------------------------------------------------------------------

    def test_static_prompt_has_identity(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("You are mini_agent", prompt)
        self.assertIn("terminal AI coding assistant", prompt)

    def test_static_prompt_has_key_modules(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("prompt.py", prompt)
        self.assertIn("config.py", prompt)
        self.assertIn("agent_runtime.py", prompt)
        self.assertIn("llm.py", prompt)
        self.assertIn("memory.py", prompt)
        self.assertIn("tools/", prompt)
        self.assertIn("safety.py", prompt)

    def test_static_prompt_has_behavior_section(self):
        prompt = build_system_prompt(self._config())
        self.assertIn("Behavior:", prompt)
        self.assertIn("Be direct and concise", prompt)

    def test_static_prompt_has_tool_guidance(self):
        prompt = build_system_prompt(self._config())
        # Tool guidance may be implicit rather than having a dedicated section header.
        # Verify key tool names are mentioned in the prompt.
        self.assertIn("find_symbol", prompt)
        self.assertIn("edit_file", prompt)

    # ------------------------------------------------------------------
    # prompt length
    # ------------------------------------------------------------------

    def test_prompt_length_within_limit(self):
        prompt = build_system_prompt(self._config())
        length = len(prompt)
        # Prompt is substantial (~26k chars) but should not grow unbounded
        self.assertLess(length, 30000,
                        f"Prompt is {length} chars (expected ~26300, adjust if content changed)")
        self.assertGreater(length, 4000,
                           f"Prompt is only {length} chars, expected > 4000")

    def test_prompt_not_empty(self):
        prompt = build_system_prompt(self._config())
        self.assertGreater(len(prompt), 500,
                           "Prompt should be substantially longer than 500 chars")


if __name__ == "__main__":
    unittest.main()
