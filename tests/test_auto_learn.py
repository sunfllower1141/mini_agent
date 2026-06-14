"""Tests for the auto-learn failure detection system in tools/__init__.py.

Covers _FAILURE_PATTERNS, _fingerprint_error, and _learn_from_failure.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tools import _TOOL_CONTEXT
from tools.__init__ import (
    _FAILURE_PATTERNS,
    _fingerprint_error,
    _learn_from_failure,
    ToolResult,
)


class TestFailurePatternsDict(unittest.TestCase):
    """_FAILURE_PATTERNS must have entries for all supported tool types."""

    # All tool types that have fingerprint-based recovery hints
    EXPECTED_TOOLS = frozenset({
        "edit_file",
        "write_file",
        "read_file",
        "run_shell",
        "search_files",
        "find_symbol",
        "find_usages",
        "run_tests",
        "verify",
    })

    def test_all_tool_types_present(self) -> None:
        """Every expected tool type has an entry in _FAILURE_PATTERNS."""
        for tool in self.EXPECTED_TOOLS:
            self.assertIn(tool, _FAILURE_PATTERNS,
                          f"Missing tool type '{tool}' in _FAILURE_PATTERNS")

    def test_all_values_are_str_to_str_dicts(self) -> None:
        """Each tool entry must be a dict[str, str]."""
        for tool, fingerprints in _FAILURE_PATTERNS.items():
            self.assertIsInstance(fingerprints, dict,
                                  f"Entry for '{tool}' is not a dict")
            for key, hint in fingerprints.items():
                self.assertIsInstance(key, str,
                                      f"Key in '{tool}' is not str: {key!r}")
                self.assertIsInstance(hint, str,
                                      f"Hint for '{tool}:{key}' is not str: {hint!r}")

    def test_count_matches_fingerprint_error_coverage(self) -> None:
        """Every fingerprint returned by _fingerprint_error for a tool
        should have a matching entry in _FAILURE_PATTERNS (except
        the fallback truncated-content fingerprint)."""
        # Tools covered by _fingerprint_error
        tests = [
            ("edit_file", "not found in file", "not found"),
            ("edit_file", "whitespace mismatch", "whitespace"),
            ("edit_file", "ambiguous match", "ambiguous"),
            ("edit_file", "invalid count", "count"),
            ("write_file", "blocked by safety", "blocked"),
            ("write_file", "file already exists", "exists"),
            ("read_file", "file not found", "not found"),
            ("read_file", "offset exceeds file", "offset"),
            ("run_shell", "command not found", "not found"),
            ("run_shell", "blocked: destructive", "blocked"),
            ("run_shell", "timed out after 60s", "timed out"),
            ("search_files", "no matches found", "not found"),
            ("search_files", "invalid regex pattern", "invalid regex"),
            ("find_symbol", "no matches found", "not found"),
            ("find_usages", "no matches found", "not found"),
            ("run_tests", "FAILED", "failures"),
            ("verify", "FAILED", "failures"),
        ]
        for tool, content, fingerprint in tests:
            with self.subTest(tool=tool, fingerprint=fingerprint):
                self.assertIn(fingerprint, _FAILURE_PATTERNS.get(tool, {}),
                              f"Fingerprint '{fingerprint}' for '{tool}' "
                              f"missing from _FAILURE_PATTERNS")


class TestFingerprintError(unittest.TestCase):
    """_fingerprint_error must return the correct fingerprint for each error type."""

    def test_edit_file_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("edit_file", "old_string not found in file"), "not found")
        self.assertEqual(_fingerprint_error("edit_file", "file does not exist"), "not found")

    def test_edit_file_whitespace(self) -> None:
        self.assertEqual(_fingerprint_error("edit_file", "whitespace mismatch detected"), "whitespace")
        self.assertEqual(_fingerprint_error("edit_file", "check indentation"), "whitespace")
        self.assertEqual(_fingerprint_error("edit_file", "tab characters found"), "whitespace")
        self.assertEqual(_fingerprint_error("edit_file", "trailing whitespace"), "whitespace")

    def test_edit_file_ambiguous(self) -> None:
        self.assertEqual(_fingerprint_error("edit_file", "ambiguous match"), "ambiguous")
        self.assertEqual(_fingerprint_error("edit_file", "multiple occurrences"), "ambiguous")
        self.assertEqual(_fingerprint_error("edit_file", "old_string appears twice"), "ambiguous")

    def test_edit_file_count(self) -> None:
        self.assertEqual(_fingerprint_error("edit_file", "invalid count value"), "count")
        self.assertEqual(_fingerprint_error("edit_file", "count parameter error"), "count")

    def test_write_file_blocked(self) -> None:
        self.assertEqual(_fingerprint_error("write_file", "write blocked by safety layer"), "blocked")
        self.assertEqual(_fingerprint_error("write_file", "safety check failed"), "blocked")

    def test_write_file_exists(self) -> None:
        self.assertEqual(_fingerprint_error("write_file", "file already exists"), "exists")
        self.assertEqual(_fingerprint_error("write_file", "overwrite is disabled"), "exists")

    def test_read_file_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("read_file", "file not found"), "not found")
        self.assertEqual(_fingerprint_error("read_file", "no such file or directory"), "not found")

    def test_read_file_offset(self) -> None:
        self.assertEqual(_fingerprint_error("read_file", "offset exceeds file length"), "offset")
        self.assertEqual(_fingerprint_error("read_file", "offset 5000 exceeds"), "offset")

    def test_search_files_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("search_files", "no matches found"), "not found")
        self.assertEqual(_fingerprint_error("search_files", "pattern not found"), "not found")

    def test_search_files_invalid_regex(self) -> None:
        self.assertEqual(_fingerprint_error("search_files", "invalid regex pattern"), "invalid regex")

    def test_run_shell_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("run_shell", "command not found"), "not found")
        self.assertEqual(_fingerprint_error("run_shell", "bash: thing: not found"), "not found")

    def test_run_shell_blocked(self) -> None:
        self.assertEqual(_fingerprint_error("run_shell", "blocked by safety"), "blocked")
        self.assertEqual(_fingerprint_error("run_shell", "destructive command blocked"), "blocked")

    def test_run_shell_timed_out(self) -> None:
        self.assertEqual(_fingerprint_error("run_shell", "command timed out"), "timed out")
        self.assertEqual(_fingerprint_error("run_shell", "timeout after 60 seconds"), "timed out")

    def test_find_symbol_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("find_symbol", "no matches for symbol"), "not found")
        self.assertEqual(_fingerprint_error("find_symbol", "not found"), "not found")

    def test_find_usages_not_found(self) -> None:
        self.assertEqual(_fingerprint_error("find_usages", "no matches for usages"), "not found")
        self.assertEqual(_fingerprint_error("find_usages", "not found"), "not found")

    def test_run_tests_failures(self) -> None:
        self.assertEqual(_fingerprint_error("run_tests", "tests FAILED"), "failures")
        self.assertEqual(_fingerprint_error("run_tests", "3 test failures"), "failures")

    def test_verify_failures(self) -> None:
        self.assertEqual(_fingerprint_error("verify", "verification FAILED"), "failures")
        self.assertEqual(_fingerprint_error("verify", "1 failure found"), "failures")

    def test_unknown_tool_fallback(self) -> None:
        """Unknown tool names return truncated lowercase content."""
        result = _fingerprint_error("unknown_tool", "Some Error Message Here")
        self.assertEqual(result, "some error message here"[:60])
        self.assertLessEqual(len(result), 60)


class TestLearnFromFailure(unittest.TestCase):
    """_learn_from_failure tracks failure counts and injects recovery hints."""

    def setUp(self) -> None:
        """Reset failure patterns before each test."""
        _TOOL_CONTEXT.__dict__.pop("_failure_patterns", None)

    def tearDown(self) -> None:
        """Clean up after each test."""
        _TOOL_CONTEXT.__dict__.pop("_failure_patterns", None)

    def _make_result(self, success: bool = False, content: str = "", hint: str = "") -> ToolResult:
        return ToolResult(success=success, content=content, hint=hint)

    def test_first_failure_no_hint(self) -> None:
        """First failure should not inject a recovery hint."""
        result = self._make_result(
            success=False,
            content="edit_file old_string not found in file",
        )
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result)

        self.assertNotIn("Recovery:", result.hint,
                         f"First failure should not inject recovery, got: {result.hint!r}")

    def test_second_failure_injects_hint(self) -> None:
        """Second occurrence of the same fingerprint injects a recovery hint."""
        content = "edit_file invalid count value 99"  # fingerprints to "count"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result1)

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result2)

        self.assertIn("count", result2.hint.lower(),
                      f"Expected count recovery hint, got: {result2.hint!r}")

    def test_third_failure_injects_hint(self) -> None:
        """Third occurrence should also inject a recovery hint."""
        content = "edit_file whitespace mismatch detected"

        for _ in range(3):
            result = self._make_result(success=False, content=content)
            with patch.object(_TOOL_CONTEXT, '_memory_store', None):
                _learn_from_failure("edit_file", result)

        self.assertIn("Whitespace", result.hint,
                      f"Expected whitespace hint on 3rd failure, got: {result.hint!r}")

    def test_unclassified_pattern_generic_hint_on_third(self) -> None:
        """When _FAILURE_PATTERNS has no entry for a fingerprint,
        a generic escalating hint is injected on count >= 3."""
        # Use a tool name that has no fingerprints in _FAILURE_PATTERNS
        # or content that produces a fingerprint not in the dict.
        # 'unknown_tool' with content that will be the fallback fingerprint.
        content = "some obscure error message xyz"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("unknown_tool", result1)
        self.assertEqual(result1.hint, "")

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("unknown_tool", result2)
        self.assertEqual(result2.hint, "",
                         "No hint on 2nd failure for unclassified pattern")

        result3 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("unknown_tool", result3)

        self.assertIn("failed 3 times", result3.hint,
                      f"Expected generic escalating hint, got: {result3.hint!r}")

    def test_hint_appended_to_existing_hint(self) -> None:
        """If result already has a hint, the recovery hint is appended."""
        content = "edit_file old_string not found in file"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result1)

        result2 = self._make_result(
            success=False, content=content,
            hint="Original hint about something",
        )
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result2)

        self.assertIn("Original hint", result2.hint)
        self.assertIn("Recovery:", result2.hint)

    def test_different_fingerprints_count_separately(self) -> None:
        """Different fingerprints for the same tool are tracked independently."""
        content_a = "edit_file old_string not found in file"
        content_b = "edit_file whitespace mismatch detected"

        # First failure: not_found
        result_a1 = self._make_result(success=False, content=content_a)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result_a1)
        self.assertEqual(result_a1.hint, "")

        # Different fingerprint: whitespace -- count=1, no hint
        result_b1 = self._make_result(success=False, content=content_b)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result_b1)
        self.assertEqual(result_b1.hint, "",
                         "Different fingerprint should start at count=1")

        # Second whitespace failure -- now gets a hint
        result_b2 = self._make_result(success=False, content=content_b)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result_b2)
        self.assertIn("Whitespace", result_b2.hint)

    def test_none_result_is_noop(self) -> None:
        """Passing None as the result should not raise."""
        _learn_from_failure("edit_file", None)  # must not raise

    def test_empty_content_fingerprint(self) -> None:
        """Empty content produces a fallback fingerprint."""
        result = self._make_result(success=False, content="")
        # First call
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result)
        # Second call -- but empty content for edit_file falls through
        # all if/elif branches, so fingerprint is empty string (content[:60])
        result2 = self._make_result(success=False, content="")
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("edit_file", result2)
        # For empty string fingerprint + count=2, no recovery in patterns,
        # so no hint should be injected
        self.assertEqual(result2.hint, "")

    def test_run_tests_failures_injects_hint(self) -> None:
        """Second run_tests failure injects the diagnose_failures hint."""
        content = "run_tests had 5 FAILED tests"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("run_tests", result1)
        self.assertEqual(result1.hint, "")

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("run_tests", result2)

        self.assertIn("diagnose_failures", result2.hint)

    def test_verify_failures_injects_hint(self) -> None:
        """Second verify failure injects the verification hint."""
        content = "verify found issues: FAILED"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("verify", result1)
        self.assertEqual(result1.hint, "")

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("verify", result2)

        self.assertIn("Verification", result2.hint)

    def test_write_file_blocked_hint(self) -> None:
        """Second write_file blocked failure injects the force=True hint."""
        content = "write_file blocked by safety"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("write_file", result1)
        self.assertEqual(result1.hint, "")

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("write_file", result2)

        self.assertIn("force=True", result2.hint)

    def test_run_shell_timed_out_hint(self) -> None:
        """Second run_shell timeout injects the timeout hint."""
        content = "run_shell command timed out"

        result1 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("run_shell", result1)
        self.assertEqual(result1.hint, "")

        result2 = self._make_result(success=False, content=content)
        with patch.object(_TOOL_CONTEXT, '_memory_store', None):
            _learn_from_failure("run_shell", result2)

        self.assertIn("timeout", result2.hint.lower())


if __name__ == "__main__":
    unittest.main()
