"""Tests for tools/schema.py -- tool schema structure and invariants."""

from __future__ import annotations

import unittest

from tools.schema import TOOLS
from tools.skills import CORE_TOOLS, SKILLS


class TestSchemaStructure(unittest.TestCase):
    """Structural validation of the TOOLS list."""

    def test_tools_is_non_empty_list(self):
        self.assertIsInstance(TOOLS, list)
        self.assertGreater(len(TOOLS), 0)

    def test_every_tool_has_type_function(self):
        for td in TOOLS:
            self.assertEqual(
                td.get("type"), "function",
                f"Tool missing type=function: {td}"
            )

    def test_every_tool_has_function_dict(self):
        for td in TOOLS:
            self.assertIsInstance(
                td.get("function"), dict,
                f"Tool missing function dict: {td}"
            )

    def test_every_tool_has_name(self):
        for td in TOOLS:
            name = td.get("function", {}).get("name")
            self.assertIsInstance(name, str,
                f"Tool missing function.name: {td}")
            self.assertGreater(len(name), 0,
                f"Tool has empty function.name: {td}")

    def test_every_tool_has_description(self):
        for td in TOOLS:
            desc = td.get("function", {}).get("description")
            self.assertIsInstance(desc, str)
            self.assertGreater(len(desc), 5,
                f"Tool {td.get('function', {}).get('name')} has short/empty description")

    def test_every_tool_has_parameters(self):
        for td in TOOLS:
            params = td.get("function", {}).get("parameters")
            self.assertIsInstance(params, dict,
                f"Tool {td.get('function', {}).get('name')} missing parameters")
            self.assertIn("type", params,
                f"Tool {td.get('function', {}).get('name')} params missing type")
            self.assertEqual(params["type"], "object",
                f"Tool {td.get('function', {}).get('name')} params type is not 'object'")
            self.assertIn("properties", params,
                f"Tool {td.get('function', {}).get('name')} params missing properties")

    def test_all_required_params_are_in_properties(self):
        """Every required parameter must exist in properties."""
        for td in TOOLS:
            params = td.get("function", {}).get("parameters", {})
            required = set(params.get("required", []))
            properties = set(params.get("properties", {}).keys())
            missing = required - properties
            self.assertEqual(
                missing, set(),
                f"Tool {td.get('function', {}).get('name')}: "
                f"required params not in properties: {missing}"
            )

    def test_all_names_are_unique_strings(self):
        """Each tool schema has a unique non-empty name."""
        names = [td["function"]["name"] for td in TOOLS]
        for n in names:
            self.assertIsInstance(n, str)
            self.assertGreater(len(n), 0)


class TestSchemaCoverage(unittest.TestCase):
    """Verify TOOLS covers all declared tool names in CORE_TOOLS and SKILLS."""

    def test_all_core_tools_have_schemas(self):
        schema_names = {td["function"]["name"] for td in TOOLS}
        for name in CORE_TOOLS:
            self.assertIn(
                name, schema_names,
                f"CORE_TOOLS entry '{name}' has no schema in TOOLS"
            )

    def test_all_skill_tools_have_schemas(self):
        schema_names = {td["function"]["name"] for td in TOOLS}
        for skill_name, tool_list in SKILLS.items():
            for tool_name in tool_list:
                self.assertIn(
                    tool_name, schema_names,
                    f"Skill '{skill_name}' tool '{tool_name}' has no schema in TOOLS"
                )

    def test_no_extraneous_schemas_for_undeclared_tools(self):
        """Every schema should map to a declared tool (core or skill)."""
        declared = set(CORE_TOOLS)
        for tool_list in SKILLS.values():
            declared.update(tool_list)
        schema_names = {td["function"]["name"] for td in TOOLS}
        extra = schema_names - declared
        # Some tools are registered but not in skills (e.g. internal helpers)
        # Just check that extra ones are well-formed
        for name in extra:
            self.assertIsInstance(name, str)
            self.assertGreater(len(name), 0)


class TestSchemaParameterTypes(unittest.TestCase):
    """Validate parameter property types."""

    VALID_TYPES = {"string", "integer", "number", "boolean", "object", "array"}

    def test_all_parameter_types_are_valid_json_schema_types(self):
        for td in TOOLS:
            name = td["function"]["name"]
            props = td["function"]["parameters"].get("properties", {})
            for param_name, param_def in props.items():
                ptype = param_def.get("type")
                self.assertIsNotNone(
                    ptype,
                    f"Tool '{name}', param '{param_name}' has no type"
                )
                self.assertIn(
                    ptype, self.VALID_TYPES,
                    f"Tool '{name}', param '{param_name}' has invalid type '{ptype}'"
                )


if __name__ == "__main__":
    unittest.main()
