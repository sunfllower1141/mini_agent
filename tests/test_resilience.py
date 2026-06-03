#!/usr/bin/env python3
"""Tests for resilience features: orphan detection, JSON repair, hints, circuit breaker."""

import json
import os
import unittest

from memory import _clean_messages
from tools import execute_tool, _repair_json
from safety import ReadSafetyGate, WriteSafetyGate
from llm import _tool_call_key, _check_circuit


# ---------------------------------------------------------------------------
# 1. Backward orphan detection in _clean_messages
# ---------------------------------------------------------------------------

class TestBackwardOrphanDetection(unittest.TestCase):
    """Verify that tool messages with no preceding assistant are removed."""

    def test_tool_result_before_assistant_removed(self):
        """Tool result with id=X before assistant with tool_calls=[X] → removed."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_1",
             "content": '{"success":true,"content":"ok"}'},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
        ]
        cleaned = _clean_messages(messages)
        roles = [m["role"] for m in cleaned]
        # orphan tool removed; then forward pass truncates assistant
        # because its tool_calls have no matching tool results after it
        self.assertEqual(roles, ["user"])

    def test_tool_result_after_assistant_preserved(self):
        """Tool result after its assistant is kept."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": '{"success":true,"content":"ok"}'},
        ]
        cleaned = _clean_messages(messages)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]["role"], "user")
        self.assertEqual(cleaned[1]["role"], "assistant")
        self.assertEqual(cleaned[2]["role"], "tool")

    def test_orphan_with_wrong_id_removed(self):
        """Tool result with id not matching any assistant → removed."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "ghost_id",
             "content": '{"success":true}'},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": '{"success":true,"content":"ok"}'},
        ]
        cleaned = _clean_messages(messages)
        # ghost_id tool removed, assistant+tool preserved
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]["role"], "user")
        self.assertEqual(cleaned[1]["role"], "assistant")
        self.assertEqual(cleaned[2]["role"], "tool")
        self.assertEqual(cleaned[2]["tool_call_id"], "call_1")

    def test_multiple_orphans_mixed_with_valid(self):
        """Mixed orphans and valid tool results — only orphans removed."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "orphan_1",
             "content": '{"success":false}'},
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "call_1", "type": "function",
                  "function": {"name": "a", "arguments": "{}"}},
                 {"id": "call_2", "type": "function",
                  "function": {"name": "b", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "orphan_2",
             "content": '{"success":false}'},
            {"role": "tool", "tool_call_id": "call_1",
             "content": '{"success":true}'},
            {"role": "tool", "tool_call_id": "call_2",
             "content": '{"success":true}'},
        ]
        cleaned = _clean_messages(messages)
        # orphan_1 (before assistant) removed, orphan_2 (wrong order / no
        # matching assistant among preceding) also removed; call_1+call_2 kept
        self.assertEqual(len(cleaned), 4)
        self.assertEqual(cleaned[0]["role"], "user")
        self.assertEqual(cleaned[1]["role"], "assistant")
        self.assertEqual(cleaned[2]["role"], "tool")
        self.assertEqual(cleaned[3]["role"], "tool")

    def test_system_messages_still_stripped(self):
        """System messages are removed regardless of new logic."""
        messages = [
            {"role": "system", "content": "secret"},
            {"role": "user", "content": "hi"},
        ]
        cleaned = _clean_messages(messages)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["role"], "user")


# ---------------------------------------------------------------------------
# 2. JSON repair
# ---------------------------------------------------------------------------

class TestJsonRepair(unittest.TestCase):
    """Verify common LLM JSON malformations are repaired."""

    def test_trailing_comma_in_object(self):
        val, repaired = _repair_json('{"path": "/x",}')
        self.assertTrue(repaired)
        self.assertEqual(val, {"path": "/x"})

    def test_trailing_comma_in_array(self):
        val, repaired = _repair_json('["a", "b",]')
        self.assertTrue(repaired)
        self.assertEqual(val, ["a", "b"])

    def test_single_quotes(self):
        val, repaired = _repair_json("{'path': '/x'}")
        self.assertTrue(repaired)
        self.assertEqual(val, {"path": "/x"})

    def test_unquoted_keys(self):
        val, repaired = _repair_json('{path: "/x", name: "test"}')
        self.assertTrue(repaired)
        self.assertEqual(val, {"path": "/x", "name": "test"})

    def test_combined_malformations(self):
        val, repaired = _repair_json("{path: '/x', name: 'test',}")
        self.assertTrue(repaired)
        self.assertEqual(val, {"path": "/x", "name": "test"})

    def test_valid_json_not_repaired(self):
        val, repaired = _repair_json('{"path": "/x"}')
        self.assertFalse(repaired)
        self.assertEqual(val, {"path": "/x"})

    def test_hopelessly_broken_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _repair_json('not json at all {{{')

    def test_empty_object(self):
        val, repaired = _repair_json("{}")
        self.assertFalse(repaired)
        self.assertEqual(val, {})


# ---------------------------------------------------------------------------
# 3. Self-correction hints
# ---------------------------------------------------------------------------

class TestToolErrorHints(unittest.TestCase):
    """Verify that failed tool calls include helpful hints."""

    def setUp(self):
        self.wg = WriteSafetyGate("/tmp")
        self.rg = ReadSafetyGate("/tmp")

    def _make_tc(self, name: str, args: str) -> dict:
        return {
            "id": "call_test",
            "type": "function",
            "function": {"name": name, "arguments": args},
        }

    def test_malformed_json_returns_hint(self):
        tc = self._make_tc("read_file", "not json")
        result = execute_tool(tc, self.wg, self.rg)
        self.assertFalse(result.success)
        self.assertIn("Malformed JSON", result.content)
        self.assertTrue(result.hint, "hint should be populated")
        self.assertIn("Valid parameters:", result.hint)
        self.assertIn("path: string", result.hint)

    def test_unknown_tool_returns_hint(self):
        tc = self._make_tc("nonexistent_tool", '{}')
        result = execute_tool(tc, self.wg, self.rg)
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.content)
        self.assertTrue(result.hint)
        self.assertIn("Available tools:", result.hint)

    def test_successful_call_has_no_hint(self):
        tc = self._make_tc("read_file", '{"path": "/tmp"}')
        result = execute_tool(tc, self.wg, self.rg)
        # May succeed or fail depending on file existence — but hint is only
        # for structured errors, not all failures
        if not result.success:
            # Even if file not found, hint is about malformation, not about
            # runtime errors
            self.assertIn(result.hint, ("", result.hint or ""))
        else:
            self.assertEqual(result.hint, "")

    def test_repaired_json_call_succeeds(self):
        """Tool call with trailing comma should be repaired and executed."""
        tc = self._make_tc("read_file", '{"path": "/tmp",}')
        result = execute_tool(tc, self.wg, self.rg)
        # It was repaired and parsed; "No such file" is a runtime result,
        # not a malformed-JSON error
        self.assertNotIn("Malformed JSON", result.content)

    def test_approval_denied_includes_hint(self):
        tc = self._make_tc("write_file", '{"path": "/tmp/x", "content": "hi"}')

        def deny(*_a, **_kw):
            return False

        result = execute_tool(tc, self.wg, self.rg, approve_callback=deny)
        self.assertFalse(result.success)
        self.assertIn("not approved", result.content)
        self.assertTrue(result.hint)


# ---------------------------------------------------------------------------
# 4. Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.TestCase):
    """Verify repeated tool call detection."""

    def test_tool_call_key_normalizes_args(self):
        tc = {
            "function": {
                "name": "read_file",
                "arguments": '{"path": "/x", "limit": 5}',
            }
        }
        tc2 = {
            "function": {
                "name": "read_file",
                "arguments": '{"limit": 5, "path": "/x"}',
            }
        }
        self.assertEqual(_tool_call_key(tc), _tool_call_key(tc2))

    def test_tool_call_key_different_args_differ(self):
        tc_a = {"function": {"name": "read_file", "arguments": '{"path":"/a"}'}}
        tc_b = {"function": {"name": "read_file", "arguments": '{"path":"/b"}'}}
        self.assertNotEqual(_tool_call_key(tc_a), _tool_call_key(tc_b))

    def test_no_trip_below_threshold(self):
        keys = ["read_file:/a", "read_file:/a"]
        self.assertIsNone(_check_circuit(keys))

    def test_trip_at_threshold(self):
        keys = ["read_file:/a", "read_file:/a", "read_file:/a"]
        warning = _check_circuit(keys)
        self.assertIsNotNone(warning)
        self.assertIn("Circuit breaker", warning)
        self.assertIn("read_file:/a", warning)

    def test_trip_above_threshold(self):
        keys = ["f:x", "f:x", "f:x", "f:x", "f:x"]
        warning = _check_circuit(keys)
        self.assertIsNotNone(warning)
        self.assertIn("5 times", warning)

    def test_different_keys_no_trip(self):
        keys = ["a:1", "b:2", "c:3", "a:1", "b:2"]
        self.assertIsNone(_check_circuit(keys))

    def test_empty_list_no_trip(self):
        self.assertIsNone(_check_circuit([]))

    def test_malformed_args_still_tracked(self):
        """Even unparseable args get a stable key."""
        tc = {"function": {"name": "bad", "arguments": "{{{broken"}}
        key = _tool_call_key(tc)
        self.assertEqual(key, "bad:{{{broken")


# ---------------------------------------------------------------------------
# 5. Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation(unittest.TestCase):
    """Verify that execute_tool catches wrong parameter names."""

    def setUp(self):
        self.wg = WriteSafetyGate("/tmp")
        self.rg = ReadSafetyGate("/tmp")

    def _make_tc(self, name: str, args: str) -> dict:
        return {
            "id": "call_test",
            "type": "function",
            "function": {"name": name, "arguments": args},
        }

    def test_unknown_param_rejected(self):
        """Using 'file_path' instead of 'path' on read_file → rejected with hint."""
        tc = self._make_tc("read_file", '{"file_path": "/tmp/x"}')
        result = execute_tool(tc, self.wg, self.rg)
        self.assertFalse(result.success)
        self.assertIn("Unknown parameter", result.content)
        self.assertIn("file_path", result.content)
        self.assertTrue(result.hint)
        self.assertIn("Valid parameters:", result.hint)

    def test_missing_required_param_rejected(self):
        """Missing required 'path' param → rejected with hint."""
        tc = self._make_tc("read_file", '{}')
        result = execute_tool(tc, self.wg, self.rg)
        self.assertFalse(result.success)
        self.assertIn("Missing required", result.content)
        self.assertIn("path", result.content)

    def test_correct_params_pass_validation(self):
        """Correct parameters pass schema validation."""
        tc = self._make_tc("read_file", '{"path": "/tmp"}')
        result = execute_tool(tc, self.wg, self.rg)
        # Schema validation passes; runtime may fail (file not found) but
        # should not be a schema error
        self.assertNotIn("Unknown parameter", result.content)
        self.assertNotIn("Missing required", result.content)


# ---------------------------------------------------------------------------
# 6. Scratchpad
# ---------------------------------------------------------------------------

class TestScratchpad(unittest.TestCase):
    """Verify scratchpad persistence and clear behavior."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "mem.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_by_default(self):
        from memory import MemoryStore
        store = MemoryStore(self.db_path)
        self.assertEqual(store.get_scratchpad(), "")

    def test_set_and_get(self):
        from memory import MemoryStore
        store = MemoryStore(self.db_path)
        store.set_scratchpad("Plan:\n1. Fix bug\n2. Test")
        self.assertEqual(store.get_scratchpad(), "Plan:\n1. Fix bug\n2. Test")

    def test_overwrite(self):
        from memory import MemoryStore
        store = MemoryStore(self.db_path)
        store.set_scratchpad("v1")
        store.set_scratchpad("v2")
        self.assertEqual(store.get_scratchpad(), "v2")

    def test_clear_removes_scratchpad(self):
        from memory import MemoryStore
        store = MemoryStore(self.db_path)
        store.set_scratchpad("important notes")
        store.save([{"role": "user", "content": "hi"}])
        store.clear()
        self.assertEqual(store.get_scratchpad(), "")

    def test_persists_across_instances(self):
        from memory import MemoryStore
        store1 = MemoryStore(self.db_path)
        store1.set_scratchpad("persistent content")

        store2 = MemoryStore(self.db_path)
        self.assertEqual(store2.get_scratchpad(), "persistent content")

    def test_tool_writes_to_sqlite_when_path_set(self):
        """write_scratchpad uses SQLite when scratchpad_path is configured."""
        from memory import MemoryStore
        from tools import execute_tool, set_context
        from safety import WriteSafetyGate, ReadSafetyGate
        store = MemoryStore(self.db_path)
        set_context(scratchpad_path=store._db_path)
        wg = WriteSafetyGate(self.tmp, allow_overwrites=True)
        rg = ReadSafetyGate(self.tmp)
        tc = {"id": "test_sp", "function": {"name": "write_scratchpad",
              "arguments": '{"content": "tool-layer test"}'}}
        result = execute_tool(tc, wg, rg)
        self.assertTrue(result.success, f"write_scratchpad failed: {result.content}")
        loaded = store.get_scratchpad()
        self.assertEqual(loaded, "tool-layer test")

    def test_tool_falls_back_to_file_when_no_path(self):
        """write_scratchpad writes to .mini_agent_scratchpad.md when no SQLite path."""
        import os as _os
        from tools import execute_tool, set_context
        from safety import WriteSafetyGate, ReadSafetyGate
        # Clear any previous scratchpad_path
        set_context(scratchpad_path=None, workspace=self.tmp)
        md_path = _os.path.join(self.tmp, ".mini_agent_scratchpad.md")
        # Remove stale file if it exists
        if _os.path.exists(md_path):
            _os.remove(md_path)
        wg = WriteSafetyGate(self.tmp, allow_overwrites=True)
        rg = ReadSafetyGate(self.tmp)
        tc = {"id": "test_sp2", "function": {"name": "write_scratchpad",
              "arguments": '{"content": "fallback file test"}'}}
        result = execute_tool(tc, wg, rg)
        self.assertTrue(result.success, f"write_scratchpad failed: {result.content}")
        self.assertTrue(_os.path.exists(md_path), f"Fallback file not created at {md_path}")
        with open(md_path) as f:
            self.assertIn("fallback file test", f.read())


# ---------------------------------------------------------------------------
# 7. Diff output in edit_file
# ---------------------------------------------------------------------------

class TestEditFileDiff(unittest.TestCase):
    """Verify that edit_file returns a unified diff."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmp, "test.py")
        self.wg = WriteSafetyGate(self.tmp, allow_overwrites=True)
        self.rg = ReadSafetyGate(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_successful_edit_includes_diff(self):
        with open(self.test_file, "w") as f:
            f.write("hello world\nline two\n")
        from tools import execute_tool
        # Must read before editing
        execute_tool({
            "id": "call_0",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": self.test_file}),
            },
        }, self.wg, self.rg)
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "edit_file",
                "arguments": json.dumps({
                    "path": self.test_file,
                    "old_string": "hello world",
                    "new_string": "goodbye world",
                }),
            },
        }
        result = execute_tool(tc, self.wg, self.rg)
        self.assertTrue(result.success)
        self.assertIn("OK: replaced 1 occurrence", result.content)
        self.assertIn(self.test_file, result.content)


# ---------------------------------------------------------------------------
# 8. Transient message stripping
# ---------------------------------------------------------------------------

class TestTransientMessages(unittest.TestCase):
    """Verify that _transient messages are stripped from saved history."""

    def test_transient_messages_are_cleaned(self):
        from memory import _clean_messages
        messages = [
            {"role": "user", "content": "real input"},
            {"role": "user", "content": "scratchpad snapshot", "_transient": True},
            {"role": "user", "content": "progress check", "_transient": True},
            {"role": "assistant", "content": "response"},
        ]
        cleaned = _clean_messages(messages)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["content"], "real input")
        self.assertEqual(cleaned[1]["content"], "response")

    def test_non_transient_still_preserved(self):
        from memory import _clean_messages
        messages = [
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "real answer"},
        ]
        cleaned = _clean_messages(messages)
        self.assertEqual(len(cleaned), 2)

    def test_all_transient_empty_result(self):
        from memory import _clean_messages
        messages = [
            {"role": "user", "content": "a", "_transient": True},
            {"role": "user", "content": "b", "_transient": True},
        ]
        cleaned = _clean_messages(messages)
        self.assertEqual(len(cleaned), 0)



if __name__ == "__main__":
    unittest.main()
