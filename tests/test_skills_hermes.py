"""Tests for Hermes-style skill architecture in tools/skills.py.

Covers: Skill dataclass, frontmatter parsing, disk discovery,
skill_view, skill_list, get_active_skill_content, reload_skills.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import textwrap

import tools.skills as skills


# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------


class TestSkillDataclass(unittest.TestCase):
    """Tests for the Skill dataclass."""

    def test_basic_fields(self):
        s = skills.Skill(
            name="test-skill",
            description="A test skill",
            version="2.0",
            author="tester",
            category="testing",
            tools=["tool_a", "tool_b"],
            body="This is the body.",
            path="/path/to/SKILL.md",
        )
        self.assertEqual(s.name, "test-skill")
        self.assertEqual(s.description, "A test skill")
        self.assertEqual(s.version, "2.0")
        self.assertEqual(s.tools, ["tool_a", "tool_b"])
        self.assertEqual(s.body, "This is the body.")

    def test_default_values(self):
        s = skills.Skill(name="minimal")
        self.assertEqual(s.name, "minimal")
        self.assertEqual(s.description, "")
        self.assertEqual(s.version, "1.0")
        self.assertEqual(s.tools, [])
        self.assertEqual(s.body, "")

    def test_to_catalog_entry(self):
        s = skills.Skill(
            name="web",
            description="Web browsing tools",
            category="browsing",
            tools=["web_search", "fetch_url"],
        )
        entry = s.to_catalog_entry()
        self.assertIn("web", entry)
        self.assertIn("Web browsing tools", entry)
        self.assertIn("web_search", entry)
        self.assertIn("fetch_url", entry)
        self.assertIn("browsing", entry)

    def test_to_full_doc(self):
        s = skills.Skill(
            name="git",
            description="Git tools",
            tools=["git", "diff"],
            body="# Git Skill\n\nUse git.",
        )
        doc = s.to_full_doc()
        self.assertIn("# Skill: git", doc)
        self.assertIn("Git tools", doc)
        self.assertIn("# Git Skill", doc)
        self.assertIn("Use git.", doc)


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


class TestParseFrontmatter(unittest.TestCase):
    """Tests for _parse_frontmatter()."""

    def test_basic_frontmatter(self):
        content = textwrap.dedent("""\
            ---
            name: test-skill
            description: A test skill
            version: "1.0"
            ---
            # Body

            This is the body.
        """)
        fm, body = skills._parse_frontmatter(content)
        self.assertEqual(fm["name"], "test-skill")
        self.assertEqual(fm["description"], "A test skill")
        self.assertEqual(fm["version"], "1.0")
        self.assertIn("This is the body.", body)

    def test_inline_list(self):
        content = textwrap.dedent("""\
            ---
            name: test
            tools: [a, b, c]
            ---
            Body.
        """)
        fm, _ = skills._parse_frontmatter(content)
        self.assertEqual(fm["tools"], ["a", "b", "c"])

    def test_block_list(self):
        content = textwrap.dedent("""\
            ---
            name: test
            tools:
              - tool_a
              - tool_b
              - tool_c
            ---
            Body.
        """)
        fm, _ = skills._parse_frontmatter(content)
        self.assertEqual(fm["tools"], ["tool_a", "tool_b", "tool_c"])

    def test_no_frontmatter(self):
        content = "Just a body without frontmatter."
        fm, body = skills._parse_frontmatter(content)
        self.assertEqual(fm, {})
        self.assertEqual(body, "Just a body without frontmatter.")

    def test_empty_frontmatter(self):
        content = textwrap.dedent("""\
            ---
            ---
            Body.
        """)
        fm, body = skills._parse_frontmatter(content)
        self.assertEqual(fm, {})
        self.assertIn("Body.", body)

    def test_frontmatter_with_comments(self):
        content = textwrap.dedent("""\
            ---
            # This is a comment
            name: test
            ---
            Body.
        """)
        fm, _ = skills._parse_frontmatter(content)
        self.assertEqual(fm["name"], "test")

    def test_boolean_values(self):
        content = textwrap.dedent("""\
            ---
            name: test
            active: true
            deprecated: false
            ---
            Body.
        """)
        fm, _ = skills._parse_frontmatter(content)
        self.assertTrue(fm["active"])
        self.assertFalse(fm["deprecated"])


# ---------------------------------------------------------------------------
# Disk-based skill discovery
# ---------------------------------------------------------------------------


class TestSkillDiscovery(unittest.TestCase):
    """Tests for _discover_skills(), _load_skill(), reload_skills()."""

    def setUp(self):
        # Save original state
        self._orig_catalog = dict(skills._SKILL_CATALOG)
        self._orig_discovered = skills._discovered
        self._orig_workspace = os.environ.get("MINI_AGENT_WORKSPACE", "")

    def tearDown(self):
        # Restore original state
        skills._SKILL_CATALOG = self._orig_catalog
        skills._discovered = self._orig_discovered
        skills.skill_list.cache_clear()
        if self._orig_workspace:
            os.environ["MINI_AGENT_WORKSPACE"] = self._orig_workspace
        elif "MINI_AGENT_WORKSPACE" in os.environ:
            del os.environ["MINI_AGENT_WORKSPACE"]

    def test_load_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = os.path.join(tmp, "my-skill")
            os.makedirs(skill_dir)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            with open(skill_md, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent("""\
                    ---
                    name: my-skill
                    description: My custom skill
                    tools: [tool1, tool2]
                    ---
                    # My Skill
                    Skill body here.
                """))

            skill = skills._load_skill(skill_md)
            self.assertIsNotNone(skill)
            self.assertEqual(skill.name, "my-skill")
            self.assertEqual(skill.description, "My custom skill")
            self.assertEqual(skill.tools, ["tool1", "tool2"])
            self.assertIn("Skill body here.", skill.body)

    def test_load_skill_no_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = os.path.join(tmp, "bad-skill")
            os.makedirs(skill_dir)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            with open(skill_md, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent("""\
                    ---
                    description: Missing name
                    ---
                    Body.
                """))

            skill = skills._load_skill(skill_md)
            self.assertIsNone(skill)

    def test_discover_from_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            # _skill_search_paths looks for <workspace>/skills/<name>/SKILL.md
            skills_root = os.path.join(tmp, "skills")
            for name in ["skill-a", "skill-b"]:
                skill_dir = os.path.join(skills_root, name)
                os.makedirs(skill_dir)
                with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                    f.write(textwrap.dedent(f"""\
                        ---
                        name: {name}
                        description: Skill {name}
                        tools: [{name}_tool]
                        ---
                        # {name}
                        Body for {name}.
                    """))

            os.environ["MINI_AGENT_WORKSPACE"] = tmp
            skills.reload_skills()

            catalog = skills._discover_skills()
            self.assertIn("skill-a", catalog)
            self.assertIn("skill-b", catalog)
            self.assertIn("skill-b", catalog)
            self.assertEqual(catalog["skill-a"].tools, ["skill-a_tool"])

    def test_reload_clears_cache(self):
        skills.reload_skills()
        self.assertTrue(skills._discovered)
        self.assertGreater(len(skills._discover_skills()), 0)


# ---------------------------------------------------------------------------
# skill_list and skill_view
# ---------------------------------------------------------------------------


class TestSkillListAndView(unittest.TestCase):
    """Tests for skill_list() and skill_view()."""

    def setUp(self):
        self._orig_catalog = dict(skills._SKILL_CATALOG)
        self._orig_discovered = skills._discovered

    def tearDown(self):
        skills._SKILL_CATALOG = self._orig_catalog
        skills._discovered = self._orig_discovered
        skills.skill_list.cache_clear()

    def test_skill_list_returns_string(self):
        skills.reload_skills()
        catalog_str = skills.skill_list()
        self.assertIsInstance(catalog_str, str)
        self.assertGreater(len(catalog_str), 50)

    def test_skill_view_known(self):
        skills.reload_skills()
        doc = skills.skill_view("test")
        self.assertIsNotNone(doc)
        self.assertIn("Skill: test", doc)

    def test_skill_view_unknown(self):
        skills.reload_skills()
        doc = skills.skill_view("nonexistent_skill_xyz")
        self.assertIsNone(doc)

    def test_skill_list_cached(self):
        skills.reload_skills()
        first = skills.skill_list()
        second = skills.skill_list()
        self.assertEqual(first, second)  # Cached


# ---------------------------------------------------------------------------
# get_active_skill_content
# ---------------------------------------------------------------------------


class TestActiveSkillContent(unittest.TestCase):
    """Tests for get_active_skill_content()."""

    def setUp(self):
        self._orig_catalog = dict(skills._SKILL_CATALOG)
        self._orig_discovered = skills._discovered
        skills.reset_skills()

    def tearDown(self):
        skills._SKILL_CATALOG = self._orig_catalog
        skills._discovered = self._orig_discovered
        skills.skill_list.cache_clear()
        skills.reset_skills()

    def test_empty_when_no_skills_active(self):
        skills.reload_skills()
        content = skills.get_active_skill_content()
        self.assertEqual(content, "")

    def test_returns_content_for_active_skill(self):
        skills.reload_skills()
        skills.activate_skill("test")
        content = skills.get_active_skill_content()
        self.assertIn("Skill: test", content)
        self.assertIn("# Test Skill", content)

    def test_only_injects_once_per_session(self):
        skills.reload_skills()
        skills.activate_skill("test")
        first = skills.get_active_skill_content()
        self.assertNotEqual(first, "")

        second = skills.get_active_skill_content()
        self.assertEqual(second, "")  # Already injected

    def test_multiple_skills_inject_once_each(self):
        skills.reload_skills()
        skills.activate_skill("test")
        skills.activate_skill("web")

        content = skills.get_active_skill_content()
        self.assertIn("Skill: test", content)
        self.assertIn("Skill: web", content)

        second = skills.get_active_skill_content()
        self.assertEqual(second, "")


# ---------------------------------------------------------------------------
# _use_skill returns documentation
# ---------------------------------------------------------------------------


class TestUseSkillReturnsDocs(unittest.TestCase):
    """Tests that _use_skill now returns full skill documentation."""

    def setUp(self):
        self._orig_catalog = dict(skills._SKILL_CATALOG)
        self._orig_discovered = skills._discovered
        skills.reset_skills()

    def tearDown(self):
        skills._SKILL_CATALOG = self._orig_catalog
        skills._discovered = self._orig_discovered
        skills.skill_list.cache_clear()
        skills.reset_skills()

    def test_use_skill_returns_skill_documentation(self):
        skills.reload_skills()
        result = skills._use_skill({"name": "test"})
        self.assertTrue(result.success)
        self.assertIn("Skill: test", result.content)
        self.assertIn("# Test Skill", result.content)

    def test_use_skill_unknown_no_docs(self):
        skills.reload_skills()
        result = skills._use_skill({"name": "nonexistent"})
        self.assertFalse(result.success)
        self.assertNotIn("Skill:", result.content)


# ---------------------------------------------------------------------------
# _skill_list and _skill_view tool wrappers
# ---------------------------------------------------------------------------


class TestSkillToolWrappers(unittest.TestCase):
    """Tests for _skill_list() and _skill_view() tool functions."""

    def setUp(self):
        self._orig_catalog = dict(skills._SKILL_CATALOG)
        self._orig_discovered = skills._discovered
        skills.reset_skills()

    def tearDown(self):
        skills._SKILL_CATALOG = self._orig_catalog
        skills._discovered = self._orig_discovered
        skills.skill_list.cache_clear()
        skills.reset_skills()

    def test_skill_list_tool_returns_catalog(self):
        skills.reload_skills()
        result = skills._skill_list({})
        self.assertTrue(result.success)
        self.assertIn("Bundled Skills Catalog", result.content)

    def test_skill_view_tool_known(self):
        skills.reload_skills()
        result = skills._skill_view({"name": "test"})
        self.assertTrue(result.success)
        self.assertIn("Skill: test", result.content)

    def test_skill_view_tool_unknown(self):
        skills.reload_skills()
        result = skills._skill_view({"name": "nope"})
        self.assertFalse(result.success)
        self.assertIn("Unknown", result.content)

    def test_skill_view_tool_empty_name(self):
        skills.reload_skills()
        result = skills._skill_view({"name": ""})
        self.assertFalse(result.success)
        self.assertIn("No skill name", result.content)

    def test_skill_view_tool_missing_name_key(self):
        skills.reload_skills()
        result = skills._skill_view({})
        self.assertFalse(result.success)
        self.assertIn("No skill name", result.content)


if __name__ == "__main__":
    unittest.main()
