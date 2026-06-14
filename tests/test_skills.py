"""Tests for tools/skills.py -- lazy tool loading via skill gates."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import tools.skills as skills


class TestActivateSkill(unittest.TestCase):
    """Tests for activate_skill()."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_activate_known_skill(self):
        ok, msg = skills.activate_skill("test")
        self.assertTrue(ok)
        self.assertIn("test", msg)
        self.assertIn("Activated", msg)
        self.assertIn("test", skills._active_skills)

    def test_activate_unknown_skill(self):
        ok, msg = skills.activate_skill("nonexistent_skill_xyz")
        self.assertFalse(ok)
        self.assertIn("Unknown skill", msg)
        self.assertIn("Available:", msg)

    def test_activate_already_active_skill(self):
        skills.activate_skill("test")
        ok, msg = skills.activate_skill("test")
        self.assertTrue(ok)
        self.assertIn("already active", msg)

    def test_activate_multiple_skills(self):
        skills.activate_skill("test")
        skills.activate_skill("web")
        self.assertIn("test", skills._active_skills)
        self.assertIn("web", skills._active_skills)
        self.assertEqual(len(skills._active_skills), 2)

    def test_whitespace_name_not_recognized(self):
        # activate_skill does NOT strip whitespace -- the name must match exactly
        ok, msg = skills.activate_skill("  test  ")
        self.assertFalse(ok)
        self.assertIn("Unknown skill", msg)


class TestDeactivateSkill(unittest.TestCase):
    """Tests for deactivate_skill()."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_deactivate_active_skill(self):
        skills.activate_skill("test")
        ok, msg = skills.deactivate_skill("test")
        self.assertTrue(ok)
        self.assertIn("Deactivated", msg)
        self.assertNotIn("test", skills._active_skills)

    def test_deactivate_not_active_skill(self):
        ok, msg = skills.deactivate_skill("test")
        self.assertFalse(ok)
        self.assertIn("not active", msg)

    def test_deactivate_then_reactivate(self):
        skills.activate_skill("test")
        skills.deactivate_skill("test")
        skills.activate_skill("test")
        self.assertIn("test", skills._active_skills)


class TestListSkills(unittest.TestCase):
    """Tests for list_skills()."""

    def test_returns_copy_of_skills(self):
        result = skills.list_skills()
        self.assertEqual(result, skills.SKILLS)
        # Verify it's a copy, not the original
        self.assertIsNot(result, skills.SKILLS)

    def test_all_expected_skill_names(self):
        result = skills.list_skills()
        expected = {"test", "lsp", "web", "agents",
                    "search", "tasks", "image", "bootstrap", "desktop"}
        self.assertTrue(expected.issubset(set(result.keys())))


class TestActiveSkills(unittest.TestCase):
    """Tests for active_skills()."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_empty_initially(self):
        self.assertEqual(skills.active_skills(), set())

    def test_returns_copy(self):
        skills.activate_skill("test")
        result = skills.active_skills()
        self.assertEqual(result, {"test"})
        # Mutating returned set should not affect internal state
        result.add("web")
        self.assertNotIn("web", skills._active_skills)

    def test_tracks_multiple(self):
        skills.activate_skill("test")
        skills.activate_skill("lsp")
        self.assertEqual(skills.active_skills(), {"test", "lsp"})


class TestResetSkills(unittest.TestCase):
    """Tests for reset_skills()."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_reset_clears_all(self):
        skills.activate_skill("test")
        skills.activate_skill("web")
        skills.activate_skill("lsp")
        skills.reset_skills()
        self.assertEqual(skills._active_skills, set())
        self.assertEqual(skills.active_skills(), set())

    def test_reset_empty_is_noop(self):
        skills.reset_skills()
        self.assertEqual(skills._active_skills, set())


class TestGetActiveToolNames(unittest.TestCase):
    """Tests for get_active_tool_names()."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_core_tools_only_when_nothing_active(self):
        names = skills.get_active_tool_names()
        self.assertEqual(names, skills.CORE_TOOLS)

    def test_includes_active_skill_tools(self):
        skills.activate_skill("test")
        names = skills.get_active_tool_names()
        for tool in skills.SKILLS["test"]:
            self.assertIn(tool, names)
        # Core tools still present
        for tool in skills.CORE_TOOLS:
            self.assertIn(tool, names)

    def test_deduplicates_overlapping_tools(self):
        # Just verify no duplicates in output
        skills.activate_skill("test")
        skills.activate_skill("web")
        names = skills.get_active_tool_names()
        self.assertEqual(len(names), len(set(names)))

    def test_order_preserves_core_first(self):
        skills.activate_skill("test")
        names = skills.get_active_tool_names()
        # Core tools should appear first, in original order
        core_slice = names[:len(skills.CORE_TOOLS)]
        self.assertEqual(core_slice, skills.CORE_TOOLS)


class TestGetActiveTools(unittest.TestCase):
    """Tests for get_active_tools() -- filters TOOLS list."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_returns_core_tool_schemas_only(self):
        with patch("tools.skills.get_active_tool_names",
                   return_value=["read_file", "write_file", "run_shell"]):
            # Only the 3 tools named above should be in the result
            result = skills.get_active_tools()
            result_names = [td["function"]["name"] for td in result]
            self.assertEqual(result_names, ["read_file", "write_file", "run_shell"])

    def test_empty_names_returns_empty_list(self):
        with patch("tools.skills.get_active_tool_names", return_value=[]):
            result = skills.get_active_tools()
            self.assertEqual(result, [])

    def test_unknown_tool_name_skipped(self):
        with patch("tools.skills.get_active_tool_names",
                   return_value=["read_file", "nonexistent_tool_xyz"]):
            result = skills.get_active_tools()
            result_names = [td["function"]["name"] for td in result]
            self.assertIn("read_file", result_names)
            self.assertNotIn("nonexistent_tool_xyz", result_names)


class TestUseSkillImpl(unittest.TestCase):
    """Tests for _use_skill() -- the use_skill tool implementation."""

    def setUp(self):
        skills.reset_skills()

    def tearDown(self):
        skills.reset_skills()

    def test_valid_skill_activates_and_returns_tool_count(self):
        result = skills._use_skill({"name": "test"})
        self.assertTrue(result.success)
        self.assertIn("Activated", result.content)
        self.assertIn("Total tools now available", result.content)

    def test_unknown_skill_returns_failure(self):
        result = skills._use_skill({"name": "nonexistent_xyz"})
        self.assertFalse(result.success)
        self.assertIn("Unknown skill", result.content)

    def test_empty_name_returns_failure(self):
        result = skills._use_skill({"name": ""})
        self.assertFalse(result.success)
        self.assertIn("No skill name provided", result.content)

    def test_missing_name_key_returns_failure(self):
        result = skills._use_skill({})
        self.assertFalse(result.success)
        self.assertIn("No skill name provided", result.content)

    def test_whitespace_name_returns_failure(self):
        result = skills._use_skill({"name": "   "})
        self.assertFalse(result.success)

    def test_already_active_returns_success_with_count(self):
        skills._use_skill({"name": "test"})
        result = skills._use_skill({"name": "test"})
        self.assertTrue(result.success)
        self.assertIn("already active", result.content)


class TestUseSkillSchema(unittest.TestCase):
    """Tests for USE_SKILL_SCHEMA validity."""

    def test_schema_has_correct_type(self):
        self.assertEqual(skills.USE_SKILL_SCHEMA["type"], "function")

    def test_schema_has_function_name(self):
        self.assertEqual(skills.USE_SKILL_SCHEMA["function"]["name"], "use_skill")

    def test_schema_has_description(self):
        desc = skills.USE_SKILL_SCHEMA["function"]["description"]
        self.assertIsInstance(desc, str)
        self.assertGreater(len(desc), 10)

    def test_schema_has_parameters_with_name(self):
        params = skills.USE_SKILL_SCHEMA["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIn("name", params["properties"])
        self.assertIn("name", params["required"])

    def test_schema_description_lists_available_skills(self):
        desc = skills.USE_SKILL_SCHEMA["function"]["description"]
        for skill_name in sorted(skills.SKILLS.keys()):
            self.assertIn(skill_name, desc)


class TestCoreToolsInvariants(unittest.TestCase):
    """Structural invariants for CORE_TOOLS."""

    def test_use_skill_is_in_core_tools(self):
        self.assertIn("use_skill", skills.CORE_TOOLS)

    def test_core_tools_no_duplicates(self):
        self.assertEqual(len(skills.CORE_TOOLS), len(set(skills.CORE_TOOLS)))

    def test_all_skill_tool_names_are_strings(self):
        for skill_name, tool_list in skills.SKILLS.items():
            for tool in tool_list:
                self.assertIsInstance(tool, str,
                    f"Tool in skill '{skill_name}' is not a string: {tool!r}")


if __name__ == "__main__":
    unittest.main()
