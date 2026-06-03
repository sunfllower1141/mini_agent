#!/usr/bin/env python3
"""
test_safety.py — tests for the file-read and file-write safety layers.
"""

import os
import tempfile
import unittest

from core.safety import ReadSafetyGate, ReadSafetyResult, WriteSafetyGate, WriteSafetyResult


class TestReadSafetyGate(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.gate = ReadSafetyGate(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    # --- workspace boundary ---

    def test_read_within_workspace_allowed(self):
        path = os.path.join(self.workspace, "notes.txt")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_read_workspace_root_allowed(self):
        result = self.gate.check(self.workspace)
        self.assertTrue(result.allowed)

    def test_read_outside_workspace_allowed(self):
        outside = os.path.join(tempfile.gettempdir(), "secret.txt")
        result = self.gate.check(outside)
        self.assertTrue(result.allowed)

    def test_path_traversal_allowed(self):
        path = os.path.join(self.workspace, "..", "..", "etc", "passwd")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_symlink_outside_workspace_allowed(self):
        outside_target = os.path.join(tempfile.gettempdir(), "outside_target.txt")
        with open(outside_target, "w") as f:
            f.write("secret")
        symlink_path = os.path.join(self.workspace, "link_out")
        os.symlink(outside_target, symlink_path)
        try:
            result = self.gate.check(symlink_path)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(symlink_path)
            os.unlink(outside_target)

    def test_relative_path_resolves_correctly(self):
        orig = os.getcwd()
        try:
            os.chdir(self.workspace)
            result = self.gate.check("rel.txt")
            self.assertTrue(result.allowed)
            expected = os.path.realpath(os.path.join(self.workspace, "rel.txt"))
            self.assertEqual(result.resolved_path, expected)
        finally:
            os.chdir(orig)

    # --- structured result ---

    def test_result_is_structured_not_exception(self):
        path = os.path.join(tempfile.gettempdir(), "bad.txt")
        result = self.gate.check(path)
        self.assertIsInstance(result, ReadSafetyResult)
        self.assertIsInstance(result.allowed, bool)
        self.assertIsInstance(result.reason, str)
        self.assertIsInstance(result.resolved_path, str)

    def test_result_is_frozen(self):
        result = self.gate.check(os.path.join(self.workspace, "x.txt"))
        with self.assertRaises(Exception):
            result.allowed = False

    # --- edge cases ---

    def test_nested_subdirectory_allowed(self):
        path = os.path.join(self.workspace, "a", "b", "c", "deep.txt")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_empty_filename_allowed_if_workspace(self):
        result = self.gate.check("")
        self.assertIsInstance(result, ReadSafetyResult)

    def test_trailing_slash_workspace_root_handled(self):
        ws = self.workspace + os.sep
        gate = ReadSafetyGate(ws)
        path = os.path.join(self.workspace, "slash_test.txt")
        result = gate.check(path)
        self.assertTrue(result.allowed)

    # --- unrestricted mode ---

    def test_unrestricted_reads_anywhere(self):
        gate = ReadSafetyGate(self.workspace, unrestricted=True)
        outside = os.path.join(tempfile.gettempdir(), "anywhere.txt")
        result = gate.check(outside)
        self.assertTrue(result.allowed)
        self.assertIn("OK", result.reason)

    def test_unrestricted_allows_path_traversal(self):
        gate = ReadSafetyGate(self.workspace, unrestricted=True)
        path = os.path.join(self.workspace, "..", "..", "etc", "passwd")
        result = gate.check(path)
        self.assertTrue(result.allowed)

    def test_unrestricted_still_has_root(self):
        gate = ReadSafetyGate(self.workspace, unrestricted=True)
        self.assertEqual(gate.workspace_root, os.path.realpath(self.workspace))


class TestWriteSafetyGate(unittest.TestCase):

    def setUp(self):
        # Create a temp workspace root
        self.workspace = tempfile.mkdtemp()
        self.gate = WriteSafetyGate(self.workspace)

    def tearDown(self):
        # Clean up any files created
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    # --- helper ---

    def _touch(self, relpath: str) -> str:
        """Create an empty file inside workspace, return its absolute path."""
        full = os.path.join(self.workspace, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write("")
        return full

    # --- workspace boundary tests ---

    def test_write_within_workspace_allowed(self):
        path = os.path.join(self.workspace, "notes.txt")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)
        self.assertEqual(result.resolved_path, os.path.realpath(path))
        self.assertEqual(result.reason, "OK")

    def test_write_to_workspace_root_allowed(self):
        result = self.gate.check(self.workspace)
        self.assertTrue(result.allowed)
        self.assertEqual(result.resolved_path, os.path.realpath(self.workspace))

    def test_write_outside_workspace_allowed(self):
        outside = os.path.join(tempfile.gettempdir(), "hack.txt")
        result = self.gate.check(outside)
        self.assertTrue(result.allowed)

    def test_path_traversal_allowed(self):
        path = os.path.join(self.workspace, "..", "..", "etc", "passwd")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_relative_path_resolves_correctly(self):
        # Relative paths are resolved before checking
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.workspace)
            result = self.gate.check("relative_file.txt")
            self.assertTrue(result.allowed)
            expected = os.path.realpath(os.path.join(self.workspace, "relative_file.txt"))
            self.assertEqual(result.resolved_path, expected)
        finally:
            os.chdir(orig_cwd)

    def test_symlink_outside_workspace_allowed(self):
        # Create a symlink inside workspace pointing outside
        outside_target = os.path.join(tempfile.gettempdir(), "outside_target.txt")
        with open(outside_target, "w") as f:
            f.write("danger")
        symlink_path = os.path.join(self.workspace, "link_to_outside")
        os.symlink(outside_target, symlink_path)
        try:
            result = self.gate.check(symlink_path)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(symlink_path)
            os.unlink(outside_target)

    # --- overwrite protection tests ---

    def test_overwrite_allowed_by_default(self):
        # Overwrite check removed from safety layer — let file operations handle it.
        existing = self._touch("existing.txt")
        result = self.gate.check(existing)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "OK")

    def test_overwrite_when_allow_overwrites_set(self):
        # allow_overwrites parameter is accepted but no longer gates writes.
        gate = WriteSafetyGate(self.workspace, allow_overwrites=True)
        existing = self._touch("existing.txt")
        result = gate.check(existing)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "OK")

    def test_new_file_allowed_default_gate(self):
        path = os.path.join(self.workspace, "brand_new.txt")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    # --- structured result shape ---

    def test_result_is_structured_not_exception(self):
        path = os.path.join(tempfile.gettempdir(), "bad.txt")
        result = self.gate.check(path)
        # Should not raise — always returns a result
        self.assertIsInstance(result, WriteSafetyResult)
        self.assertIsInstance(result.allowed, bool)
        self.assertIsInstance(result.reason, str)
        self.assertIsInstance(result.resolved_path, str)

    def test_result_is_frozen(self):
        path = os.path.join(self.workspace, "frozen_test.txt")
        result = self.gate.check(path)
        with self.assertRaises(Exception):
            result.allowed = False  # dataclass(frozen=True)

    # --- edge cases ---

    def test_nested_subdirectory_allowed(self):
        path = os.path.join(self.workspace, "a", "b", "c", "deep.txt")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_hidden_file_allowed(self):
        path = os.path.join(self.workspace, ".gitignore")
        result = self.gate.check(path)
        self.assertTrue(result.allowed)

    def test_empty_filename_allowed_if_workspace(self):
        # An empty path resolves to the workspace root (cwd) — may or may not
        # be the workspace root. We just verify no crash.
        result = self.gate.check("")
        # Just check structured result returned, no exception
        self.assertIsInstance(result, WriteSafetyResult)

    def test_workspace_root_trailing_slash_handled(self):
        ws = self.workspace + os.sep
        gate = WriteSafetyGate(ws)
        path = os.path.join(self.workspace, "slash_test.txt")
        result = gate.check(path)
        self.assertTrue(result.allowed)

    # --- unrestricted mode ---

    def test_unrestricted_allows_write_anywhere(self):
        gate = WriteSafetyGate(self.workspace, unrestricted=True,
                               allow_overwrites=True)
        outside = os.path.join(tempfile.gettempdir(), "anywhere_write.txt")
        result = gate.check(outside)
        self.assertTrue(result.allowed)
        self.assertIn("OK", result.reason)

    def test_unrestricted_allows_path_traversal_write(self):
        gate = WriteSafetyGate(self.workspace, unrestricted=True,
                               allow_overwrites=True)
        path = os.path.join(self.workspace, "..", "..", "etc", "hack")
        result = gate.check(path)
        self.assertTrue(result.allowed)

    def test_unrestricted_no_overwrite_block(self):
        # Overwrite check removed; unrestricted mode allows writes anywhere.
        outside = os.path.join(tempfile.gettempdir(), "existing_outside.txt")
        with open(outside, "w") as f:
            f.write("existing")
        try:
            gate = WriteSafetyGate(self.workspace, unrestricted=True)
            result = gate.check(outside)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(outside)

    def test_unrestricted_with_overwrites_allows_overwrite_anywhere(self):
        outside = os.path.join(tempfile.gettempdir(), "overwrite_outside.txt")
        with open(outside, "w") as f:
            f.write("existing")
        try:
            gate = WriteSafetyGate(self.workspace, unrestricted=True,
                                   allow_overwrites=True)
            result = gate.check(outside)
            self.assertTrue(result.allowed)
        finally:
            os.unlink(outside)


if __name__ == "__main__":
    unittest.main()
