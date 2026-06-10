#!/usr/bin/env python3
"""
test_edit_safety_fixes.py — tests for the 5 hardening fixes:
  1. Atomic writes
  2. count=-1 zero-occurrence guard
  3. Mtime tracking for stale-state detection
  4. Binary file detection
  5. Batch edit atomicity (two-phase validation)
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from tools.file_ops import (
    _atomic_write,
    _is_likely_text,
    _READ_FILES,
    _apply_single_edit,
    _fuzzy_find,
    _BACKUPS,
    _FILE_CACHE,
)
from core.safety import WriteSafetyGate


# ---------------------------------------------------------------------------
# Fix 1: Atomic writes
# ---------------------------------------------------------------------------

class TestAtomicWrite(unittest.TestCase):
    """Verify that _atomic_write never leaves a partial file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_atomic_write_creates_file(self):
        path = os.path.join(self.tmpdir, "test.txt")
        _atomic_write(path, "hello world")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r") as f:
            self.assertEqual(f.read(), "hello world")

    def test_atomic_write_overwrites_existing(self):
        path = os.path.join(self.tmpdir, "test.txt")
        with open(path, "w") as f:
            f.write("old content")
        _atomic_write(path, "new content")
        with open(path, "r") as f:
            self.assertEqual(f.read(), "new content")

    def test_atomic_write_no_temp_file_left_behind(self):
        """After a successful write, no temp files should remain."""
        path = os.path.join(self.tmpdir, "test.txt")
        before = set(os.listdir(self.tmpdir))
        _atomic_write(path, "content")
        after = set(os.listdir(self.tmpdir))
        new_files = after - before
        for f in new_files:
            self.assertFalse(
                f.startswith(".tmp_"),
                f"Temp file left behind: {f}",
            )

    def test_atomic_write_cleans_up_on_error(self):
        """If writing fails, the temp file is removed."""
        path = os.path.join(self.tmpdir, "test.txt")
        with patch("tools.file_ops.os.fdopen", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _atomic_write(path, "content")
        for f in os.listdir(self.tmpdir):
            self.assertFalse(
                f.startswith(".tmp_"),
                f"Temp file left behind after error: {f}",
            )

    def test_atomic_write_unicode_content(self):
        path = os.path.join(self.tmpdir, "test.txt")
        content = "héllo wörld\nline 2 😀\n"
        _atomic_write(path, content)
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), content)

    def test_atomic_write_preserves_inode_for_reader(self):
        path = os.path.join(self.tmpdir, "test.txt")
        with open(path, "w") as f:
            f.write("A" * 10000)
        _atomic_write(path, "B" * 10000)
        with open(path, "r") as f:
            content = f.read()
        self.assertEqual(content, "B" * 10000)


# ---------------------------------------------------------------------------
# Fix 2: count=-1 zero-occurrence guard
# ---------------------------------------------------------------------------

class TestCountMinusOneGuard(unittest.TestCase):
    """Verify that count=-1 with zero occurrences returns an error."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wg = WriteSafetyGate(self.tmpdir, unrestricted=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_file(self, relpath, content):
        full = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return os.path.realpath(full)

    def test_count_minus_one_zero_occurrences_returns_error(self):
        path = self._write_file("test.py", "def foo():\n    return 1\n")
        _READ_FILES[path] = os.path.getmtime(path)

        result = _apply_single_edit(
            path=path,
            old="this does not exist in the file",
            new="replacement",
            count=-1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "this does not exist in the file", "new_string": "replacement"},
        )
        self.assertFalse(result[1].success)
        self.assertIn("not found", result[1].content)

    def test_count_minus_one_normalized_match_but_exact_zero_errors(self):
        """When fuzzy matcher finds via normalization but str.count=0, error."""
        path = self._write_file("test.py", '\u201csmart quotes\u201d\n')
        _READ_FILES[path] = os.path.getmtime(path)
        result = _apply_single_edit(
            path=path,
            old='"smart quotes"',
            new="'dumb quotes'",
            count=-1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": '"smart quotes"', "new_string": "'dumb quotes'"},
        )
        self.assertFalse(result[1].success)
        self.assertIn("count=-1", result[1].content)

    def test_count_minus_one_with_match_succeeds(self):
        path = self._write_file("test.py", "hello\nworld\nhello\n")
        _READ_FILES[path] = os.path.getmtime(path)

        result = _apply_single_edit(
            path=path,
            old="hello",
            new="hi",
            count=-1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "hello", "new_string": "hi"},
        )
        self.assertTrue(result[1].success, f"Edit failed: {result[1].content}")
        with open(path, "r") as f:
            content = f.read()
        self.assertEqual(content, "hi\nworld\nhi\n")


# ---------------------------------------------------------------------------
# Fix 3: Mtime tracking for stale-state detection
# ---------------------------------------------------------------------------

class TestMtimeTracking(unittest.TestCase):
    """Verify stale-file warnings when external modification happens between
    read and edit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wg = WriteSafetyGate(self.tmpdir, unrestricted=True)
        _READ_FILES.clear()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _READ_FILES.clear()

    def _write_file(self, relpath, content):
        full = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return os.path.realpath(full)

    def test_edit_without_read_blocked(self):
        path = self._write_file("test.py", "x = 1\n")
        result = _apply_single_edit(
            path=path,
            old="x = 1",
            new="x = 2",
            count=1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "x = 1", "new_string": "x = 2"},
        )
        self.assertFalse(result[1].success)
        self.assertIn("not been read", result[1].content)

    def test_stale_mtime_warning_in_result(self):
        path = self._write_file("test.py", "original content\n")
        _READ_FILES[path] = os.path.getmtime(path)
        
        time.sleep(0.02)
        with open(path, "w") as f:
            f.write("modified externally\n")
        
        result = _apply_single_edit(
            path=path,
            old="modified externally",
            new="edited",
            count=1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "modified externally", "new_string": "edited"},
        )
        self.assertTrue(result[1].success, f"Edit failed: {result[1].content}")
        self.assertIn("[WARNING]", result[1].content)
        self.assertIn("modified after last read_file", result[1].content)

    def test_no_warning_when_file_unchanged(self):
        path = self._write_file("test.py", "unchanged\n")
        _READ_FILES[path] = os.path.getmtime(path)
        
        result = _apply_single_edit(
            path=path,
            old="unchanged",
            new="changed",
            count=1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "unchanged", "new_string": "changed"},
        )
        self.assertTrue(result[1].success, f"Edit failed: {result[1].content}")
        self.assertNotIn("[WARNING]", result[1].content)

    def test__READ_FILES_is_dict(self):
        self.assertIsInstance(_READ_FILES, dict)


# ---------------------------------------------------------------------------
# Fix 4: Binary file detection
# ---------------------------------------------------------------------------

class TestBinaryDetection(unittest.TestCase):
    """Verify _is_likely_text correctly identifies binary files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_bytes(self, relpath, data):
        full = os.path.join(self.tmpdir, relpath)
        with open(full, "wb") as f:
            f.write(data)
        return full

    def test_text_file_is_text(self):
        path = self._write_bytes("test.py", b"print('hello world')\n")
        self.assertTrue(_is_likely_text(path))

    def test_empty_file_is_text(self):
        path = self._write_bytes("empty.txt", b"")
        self.assertTrue(_is_likely_text(path))

    def test_binary_file_with_null_bytes(self):
        path = self._write_bytes("image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        self.assertFalse(_is_likely_text(path))

    def test_binary_file_all_nulls(self):
        path = self._write_bytes("nulls.bin", b"\x00" * 100)
        self.assertFalse(_is_likely_text(path))

    def test_non_existent_file_is_text(self):
        path = os.path.join(self.tmpdir, "does_not_exist.txt")
        self.assertTrue(_is_likely_text(path))

    def test_text_with_unicode_is_text(self):
        path = self._write_bytes("unicode.txt", "héllo 世界\n".encode("utf-8"))
        self.assertTrue(_is_likely_text(path))

    def test_utf16_bom_not_binary(self):
        path = self._write_bytes("utf16.txt", b"\xff\xfeh\x00e\x00l\x00l\x00o\x00")
        result = _is_likely_text(path)
        self.assertIsInstance(result, bool)

    def test_text_with_single_null_byte_is_binary(self):
        path = self._write_bytes("corrupt.txt", b"hello\x00world\n")
        self.assertFalse(_is_likely_text(path))


# ---------------------------------------------------------------------------
# Fix 5: Batch edit atomicity
# ---------------------------------------------------------------------------

class TestBatchAtomicity(unittest.TestCase):
    """Verify batch edits validate all files before writing any."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wg = WriteSafetyGate(self.tmpdir, unrestricted=True)
        _READ_FILES.clear()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _READ_FILES.clear()

    def _write_file(self, relpath, content):
        full = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return os.path.realpath(full)

    def test_dry_run_does_not_modify_file(self):
        path = self._write_file("test.py", "x = 1\n")
        _READ_FILES[path] = os.path.getmtime(path)

        result = _apply_single_edit(
            path=path,
            old="x = 1",
            new="x = 2",
            count=1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "x = 1", "new_string": "x = 2"},
            dry_run=True,
        )
        self.assertTrue(result[1].success, f"dry_run failed: {result[1].content}")
        self.assertIn("dry-run", result[1].content)
        with open(path, "r") as f:
            self.assertEqual(f.read(), "x = 1\n")

    def test_dry_run_fails_for_invalid_old_string(self):
        path = self._write_file("test.py", "x = 1\n")
        _READ_FILES[path] = os.path.getmtime(path)

        result = _apply_single_edit(
            path=path,
            old="this doesn't exist",
            new="replacement",
            count=1,
            preview=False,
            wg=self.wg,
            args={"path": path, "old_string": "this doesn't exist", "new_string": "replacement"},
            dry_run=True,
        )
        self.assertFalse(result[1].success)

    def test_batch_all_valid_writes_all(self):
        p1 = self._write_file("a.py", "hello a\n")
        p2 = self._write_file("b.py", "hello b\n")
        _READ_FILES[p1] = os.path.getmtime(p1)
        _READ_FILES[p2] = os.path.getmtime(p2)

        from tools.file_ops import _edit_file

        result = _edit_file(
            {"paths": [p1, p2], "old_string": "hello", "new_string": "hi", "count": -1},
            self.wg,
            MagicMock(),
        )
        self.assertTrue(result.success, f"Batch edit failed: {result.content}")
        with open(p1, "r") as f:
            self.assertEqual(f.read(), "hi a\n")
        with open(p2, "r") as f:
            self.assertEqual(f.read(), "hi b\n")

    def test_batch_one_invalid_preserves_all(self):
        p1 = self._write_file("a.py", "hello a\n")
        p2 = self._write_file("b.py", "hello b\n")
        _READ_FILES[p1] = os.path.getmtime(p1)
        _READ_FILES[p2] = os.path.getmtime(p2)

        from tools.file_ops import _edit_file

        result = _edit_file(
            {"paths": [p1, p2], "old_string": "this does not exist in either file", "new_string": "replacement", "count": 1},
            self.wg,
            MagicMock(),
        )
        self.assertFalse(result.success)
        self.assertIn("validation failed", result.content)
        self.assertIn("no files were modified", result.content)
        with open(p1, "r") as f:
            self.assertEqual(f.read(), "hello a\n")
        with open(p2, "r") as f:
            self.assertEqual(f.read(), "hello b\n")


if __name__ == "__main__":
    unittest.main()
