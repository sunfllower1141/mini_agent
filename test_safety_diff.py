#!/usr/bin/env python3
"""Tests for safety.py's generate_diff() function."""

from __future__ import annotations

import os
import tempfile
import unittest

from safety import DiffPreview, WriteSafetyGate


class TestGenerateDiff(unittest.TestCase):
    """Tests for WriteSafetyGate.generate_diff()."""

    def setUp(self):
        """Create a temporary workspace directory and a gate for each test."""
        self.tmpdir = tempfile.mkdtemp(prefix="test_safety_diff_")
        # allow_overwrites=True so check() doesn't block, but generate_diff
        # doesn't call check() — it just needs the gate for formatting.
        self.gate = WriteSafetyGate(
            self.tmpdir, allow_overwrites=True, unrestricted=False
        )

    def tearDown(self):
        """Clean up the temp workspace."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, filename: str, content: str) -> str:
        """Write a file inside tmpdir and return its full path."""
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            f.write(content)
        return filename  # relative path

    def _result(self, tool_name: str, args: dict) -> DiffPreview:
        """Shortcut to call generate_diff."""
        return self.gate.generate_diff(tool_name, args)

    # ------------------------------------------------------------------
    # write_file — new file (no existing file)
    # ------------------------------------------------------------------

    def test_write_new_file_empty(self):
        """New file with empty content: changed=True (bool('') = False, so False)."""
        result = self._result("write_file", {"path": "new.txt", "content": ""})
        self.assertIsInstance(result, DiffPreview)
        # content is empty string, bool("") is False
        self.assertFalse(result.changed)
        self.assertIn("--- /dev/null", result.preview_text)

    def test_write_new_file_single_char(self):
        """New file with a single character."""
        result = self._result("write_file", {"path": "s.txt", "content": "X"})
        self.assertTrue(result.changed)
        self.assertIn("--- /dev/null", result.preview_text)
        self.assertIn("+X", result.preview_text)

    def test_write_new_file_multiline(self):
        """New file with multiple lines."""
        content = "line1\nline2\nline3\n"
        result = self._result("write_file", {"path": "m.txt", "content": content})
        self.assertTrue(result.changed)
        self.assertIn("+line1", result.preview_text)
        self.assertIn("+line2", result.preview_text)
        self.assertIn("+line3", result.preview_text)

    # ------------------------------------------------------------------
    # write_file — existing file, identical content
    # ------------------------------------------------------------------

    def test_write_identical_content(self):
        """Overwriting a file with exactly the same content: changed=False."""
        self._write("f.txt", "abc\ndef\n")
        result = self._result("write_file", {"path": "f.txt", "content": "abc\ndef\n"})
        self.assertFalse(result.changed)
        # difflib.unified_diff with identical lines yields no diff lines
        # (the generator is empty).  _format_diff produces an empty string.
        self.assertEqual(result.preview_text, "")

    def test_write_identical_empty(self):
        """Overwriting an empty file with empty content: changed=False."""
        self._write("empty.txt", "")
        result = self._result("write_file", {"path": "empty.txt", "content": ""})
        self.assertFalse(result.changed)
        self.assertEqual(result.preview_text, "")

    # ------------------------------------------------------------------
    # write_file — existing file, changed content
    # ------------------------------------------------------------------

    def test_write_added_lines(self):
        """Overwrite with extra lines added."""
        self._write("f.txt", "a\nb\n")
        result = self._result("write_file", {"path": "f.txt", "content": "a\nb\nc\n"})
        self.assertTrue(result.changed)
        self.assertIn("+c", result.preview_text)

    def test_write_removed_lines(self):
        """Overwrite with lines removed."""
        self._write("f.txt", "a\nb\nc\n")
        result = self._result("write_file", {"path": "f.txt", "content": "a\nc\n"})
        self.assertTrue(result.changed)
        self.assertIn("-b", result.preview_text)

    def test_write_mixed_changes(self):
        """Overwrite with both additions and removals."""
        self._write("f.txt", "a\nb\nc\n")
        result = self._result("write_file", {"path": "f.txt", "content": "a\nX\nc\n"})
        self.assertTrue(result.changed)
        preview = result.preview_text
        self.assertIn("-b", preview)
        self.assertIn("+X", preview)

    def test_write_empty_to_nonempty(self):
        """Overwrite non-empty file with empty content."""
        self._write("f.txt", "hello\n")
        result = self._result("write_file", {"path": "f.txt", "content": ""})
        self.assertTrue(result.changed)
        # unified_diff shows the removal: -hello
        self.assertIn("-hello", result.preview_text)

    # ------------------------------------------------------------------
    # edit_file — existing file
    # ------------------------------------------------------------------

    def test_edit_identical_strings(self):
        """Edit with old==new: no change."""
        self._write("f.txt", "hello\nworld\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "hello\n", "new_string": "hello\n"
        })
        self.assertFalse(result.changed)

    def test_edit_add_line(self):
        """Edit replaces part, effectively adding a line."""
        self._write("f.txt", "hello\nworld\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "hello\n", "new_string": "hello\nmid\n"
        })
        self.assertTrue(result.changed)
        self.assertIn("+mid", result.preview_text)

    def test_edit_remove_line(self):
        """Edit removes a line."""
        self._write("f.txt", "hello\nmid\nworld\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "hello\nmid\n", "new_string": "hello\n"
        })
        self.assertTrue(result.changed)
        self.assertIn("-mid", result.preview_text)

    def test_edit_single_char_change(self):
        """Edit a single character."""
        self._write("f.txt", "abc\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "a", "new_string": "X"
        })
        self.assertTrue(result.changed)
        preview = result.preview_text
        self.assertIn("-a", preview)
        self.assertIn("+X", preview)

    def test_edit_empty_old_string(self):
        """Edit with empty old_string (prepend)."""
        self._write("f.txt", "world\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "", "new_string": "hello\n"
        })
        self.assertTrue(result.changed)
        preview = result.preview_text
        self.assertIn("+hello", preview)

    def test_edit_empty_new_string(self):
        """Edit with empty new_string (deletion)."""
        self._write("f.txt", "hello\nworld\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "hello\n", "new_string": ""
        })
        self.assertTrue(result.changed)
        self.assertIn("-hello", result.preview_text)

    def test_edit_count_all(self):
        """Edit with count=-1 replaces all occurrences."""
        self._write("f.txt", "x x x\n")
        result = self._result("edit_file", {
            "path": "f.txt", "old_string": "x", "new_string": "y", "count": -1
        })
        self.assertTrue(result.changed)
        preview = result.preview_text
        # unified diff shows the whole line: -x x x  →  +y y y
        self.assertIn("-x x x", preview)
        self.assertIn("+y y y", preview)

    # ------------------------------------------------------------------
    # edit_file — new file (file doesn't exist)
    # ------------------------------------------------------------------

    def test_edit_new_file(self):
        """edit_file on a non-existent file: treated as new file with new_string."""
        result = self._result("edit_file", {
            "path": "new.txt", "old_string": "old", "new_string": "hello\n"
        })
        self.assertTrue(result.changed)
        self.assertIn("--- /dev/null", result.preview_text)
        self.assertIn("+hello", result.preview_text)

    def test_edit_new_file_empty(self):
        """edit_file on non-existent file with empty new_string."""
        result = self._result("edit_file", {
            "path": "new.txt", "old_string": "old", "new_string": ""
        })
        self.assertFalse(result.changed)
        self.assertIn("--- /dev/null", result.preview_text)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_path(self):
        """Empty path still resolves (to tmpdir)."""
        result = self._result("write_file", {"path": "", "content": "hi"})
        # path="" resolves to tmpdir itself → not a file, so exists=False
        self.assertTrue(result.changed)

    def test_unknown_tool_name(self):
        """Unknown tool name returns empty, unchanged DiffPreview."""
        result = self._result("unknown_tool", {"path": "f.txt", "content": "hi"})
        self.assertFalse(result.changed)
        self.assertEqual(result.preview_text, "")

    def test_write_file_empty_content_on_nonexistent(self):
        """write_file with empty content on new file."""
        result = self._result("write_file", {"path": "nonexistent.txt", "content": ""})
        self.assertFalse(result.changed)

    # ------------------------------------------------------------------
    # ANSI color presence
    # ------------------------------------------------------------------

    def test_ansi_colors_present_in_diff(self):
        """verify colored diff output contains ANSI escape codes."""
        self._write("color.txt", "line1\nline2\n")
        result = self._result("write_file", {
            "path": "color.txt", "content": "lineA\nline2\nline3\n"
        })
        self.assertTrue(result.changed)
        preview = result.preview_text
        self.assertIn("\033[", preview)  # ANSI codes present


if __name__ == "__main__":
    unittest.main()
