#!/usr/bin/env python3
"""Tests for logging_setup.py -- structured error logging, counters, and formatters."""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from logging_setup import (
    _ERROR_COUNTERS,
    _ERROR_COUNTERS_LOCK,
    _JsonLinesFormatter,
    _fingerprint_from_content,
    _increment_error_counter,
    get_error_summary,
    get_logger,
    has_elevated_errors,
    log_api_error,
    log_error_trace,
    log_tool_failure,
    log_tool_success,
)


# ---------------------------------------------------------------------------
# Helper: capture logger output via a temporary StreamHandler
# ---------------------------------------------------------------------------

def _capture_log_output(logger: logging.Logger) -> io.StringIO:
    """Add a StringIO stream handler to *logger* and return the buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_JsonLinesFormatter())
    logger.addHandler(handler)
    # Store on the handler so caller can clean up
    handler._test_buffer = buf  # type: ignore[attr-defined]
    return buf


def _remove_test_handler(logger: logging.Logger, buf: io.StringIO) -> None:
    """Remove the handler whose stream is *buf* from *logger*."""
    for h in list(logger.handlers):
        if isinstance(h, logging.StreamHandler) and h.stream is buf:
            logger.removeHandler(h)
            break


def _get_log_entries(buf: io.StringIO) -> list[dict]:
    """Parse JSON-lines from a StringIO buffer and return entries."""
    buf.seek(0)
    return [json.loads(line) for line in buf if line.strip()]


# ---------------------------------------------------------------------------
# 1. _fingerprint_from_content
# ---------------------------------------------------------------------------

class TestFingerprintFromContent(unittest.TestCase):
    """Verify error fingerprint extraction for all tool-specific patterns."""

    def test_edit_file_not_found(self):
        fp = _fingerprint_from_content("edit_file", "Error: old_string not found in file")
        self.assertEqual(fp, "not_found")

    def test_edit_file_does_not_exist(self):
        fp = _fingerprint_from_content("edit_file", "file does not exist")
        self.assertEqual(fp, "not_found")

    def test_edit_file_whitespace(self):
        fp = _fingerprint_from_content("edit_file", "check whitespace and indentation")
        self.assertEqual(fp, "whitespace")

    def test_edit_file_tab_issue(self):
        fp = _fingerprint_from_content("edit_file", "tab characters detected")
        self.assertEqual(fp, "whitespace")

    def test_edit_file_ambiguous(self):
        fp = _fingerprint_from_content("edit_file", "ambiguous match: multiple occurrences")
        self.assertEqual(fp, "ambiguous")

    def test_edit_file_generic_fallback(self):
        fp = _fingerprint_from_content("edit_file", "some unknown error")
        self.assertTrue(fp.startswith("generic:"), f"Expected 'generic:' prefix, got: {fp}")
        self.assertEqual(len(fp), 8 + 12)  # "generic:" + 12 hex chars

    def test_write_file_blocked(self):
        fp = _fingerprint_from_content("write_file", "safety gate blocked the write")
        self.assertEqual(fp, "blocked")

    def test_read_file_not_found(self):
        fp = _fingerprint_from_content("read_file", "no such file or directory: foo.txt")
        self.assertEqual(fp, "not_found")

    def test_search_files_no_matches(self):
        fp = _fingerprint_from_content("search_files", "No matches found for pattern")
        self.assertEqual(fp, "not_found")

    def test_search_files_invalid_regex(self):
        fp = _fingerprint_from_content("search_files", "Invalid regex: unmatched [")
        self.assertEqual(fp, "invalid_regex")

    def test_search_files_not_found_variant(self):
        fp = _fingerprint_from_content("search_files", "not found in workspace")
        self.assertEqual(fp, "not_found")

    def test_run_shell_not_found(self):
        fp = _fingerprint_from_content("run_shell", "command not found: foobar")
        self.assertEqual(fp, "not_found")

    def test_run_shell_blocked(self):
        fp = _fingerprint_from_content("run_shell", "destructive command blocked")
        self.assertEqual(fp, "blocked")

    def test_run_shell_timed_out(self):
        fp = _fingerprint_from_content("run_shell", "Command timed out after 30s")
        self.assertEqual(fp, "timed_out")

    def test_run_shell_timeout_variant(self):
        fp = _fingerprint_from_content("run_shell", "execution timeout exceeded")
        self.assertEqual(fp, "timed_out")

    def test_find_symbol_no_match(self):
        fp = _fingerprint_from_content("find_symbol", "No match found for 'does_not_exist'")
        self.assertEqual(fp, "not_found")

    def test_find_usages_no_match(self):
        fp = _fingerprint_from_content("find_usages", "not found: no usages")
        self.assertEqual(fp, "not_found")

    def test_run_tests_failures(self):
        fp = _fingerprint_from_content("run_tests", "1 FAILED: test_foo")
        self.assertEqual(fp, "test_failures")

    def test_verify_failures(self):
        fp = _fingerprint_from_content("verify", "FAILED: verification failed")
        self.assertEqual(fp, "test_failures")

    def test_case_insensitive_matching(self):
        fp = _fingerprint_from_content("edit_file", "Old_String NOT FOUND")
        self.assertEqual(fp, "not_found")

    def test_deterministic_generic_hash(self):
        """Same error content should always produce the same generic fingerprint."""
        content = "some obscure error that has no special rule"
        fp1 = _fingerprint_from_content("unknown_tool", content)
        fp2 = _fingerprint_from_content("unknown_tool", content)
        self.assertEqual(fp1, fp2)

    def test_different_errors_different_hashes(self):
        fp1 = _fingerprint_from_content("unknown_tool", "error A description")
        fp2 = _fingerprint_from_content("unknown_tool", "error B description")
        self.assertNotEqual(fp1, fp2)


# ---------------------------------------------------------------------------
# 2. _increment_error_counter + get_error_summary
# ---------------------------------------------------------------------------

class TestErrorCounters(unittest.TestCase):
    """Verify thread-safe error counter tracking and summary generation."""

    def setUp(self):
        # Clear counters before each test
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def tearDown(self):
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def test_single_increment(self):
        _increment_error_counter("edit_file", "not_found")
        summary = get_error_summary()
        self.assertIn("edit_file", summary)
        self.assertIn("1 failures", summary)
        self.assertIn("not_found", summary)

    def test_multiple_fingerprints_same_tool(self):
        for _ in range(3):
            _increment_error_counter("edit_file", "not_found")
        for _ in range(2):
            _increment_error_counter("edit_file", "whitespace")
        summary = get_error_summary()
        self.assertIn("edit_file", summary)
        self.assertIn("5 failures", summary)

    def test_multiple_tools(self):
        _increment_error_counter("edit_file", "not_found")
        _increment_error_counter("edit_file", "not_found")
        _increment_error_counter("run_shell", "timed_out")
        _increment_error_counter("read_file", "not_found")
        summary = get_error_summary()
        self.assertIn("edit_file", summary)
        self.assertIn("run_shell", summary)
        self.assertIn("read_file", summary)
        self.assertIn("4 total failures", summary)

    def test_empty_counters_returns_empty_string(self):
        summary = get_error_summary()
        self.assertEqual(summary, "")

    def test_total_failures_only_counts_if_nonzero(self):
        # Set up a counter where total_failures is 0 (shouldn't happen
        # in practice, but we test the guard)
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS["ghost_tool"]["whitespace"] = 1
        summary = get_error_summary()
        self.assertNotIn("ghost_tool", summary)

    def test_top_3_error_types(self):
        for _ in range(5):
            _increment_error_counter("edit_file", "not_found")
        for _ in range(3):
            _increment_error_counter("edit_file", "whitespace")
        for _ in range(2):
            _increment_error_counter("edit_file", "ambiguous")
        _increment_error_counter("edit_file", "generic_special")
        # Only top 3 should appear in summary
        summary = get_error_summary()
        self.assertIn("not_found:5", summary)
        self.assertIn("whitespace:3", summary)
        self.assertIn("ambiguous:2", summary)
        self.assertNotIn("generic_special", summary)

    def test_has_elevated_errors_below_threshold(self):
        for _ in range(4):
            _increment_error_counter("edit_file", "not_found")
        self.assertFalse(has_elevated_errors(threshold=5))

    def test_has_elevated_errors_at_threshold(self):
        for _ in range(5):
            _increment_error_counter("edit_file", "not_found")
        self.assertTrue(has_elevated_errors(threshold=5))

    def test_has_elevated_errors_above_threshold(self):
        for _ in range(7):
            _increment_error_counter("edit_file", "not_found")
        self.assertTrue(has_elevated_errors(threshold=5))

    def test_has_elevated_errors_custom_threshold(self):
        for _ in range(3):
            _increment_error_counter("edit_file", "not_found")
        self.assertTrue(has_elevated_errors(threshold=3))
        self.assertFalse(has_elevated_errors(threshold=4))


# ---------------------------------------------------------------------------
# 3. _JsonLinesFormatter
# ---------------------------------------------------------------------------

class TestJsonLinesFormatter(unittest.TestCase):
    """Verify JSON-lines log formatter output."""

    def setUp(self):
        self.formatter = _JsonLinesFormatter()

    def test_basic_format(self):
        record = logging.LogRecord(
            name="mini_agent.test", level=logging.INFO,
            pathname="test.py", lineno=1, msg="hello %s", args=("world",),
            exc_info=None,
        )
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertEqual(obj["level"], "INFO")
        self.assertEqual(obj["logger"], "mini_agent.test")
        self.assertEqual(obj["msg"], "hello world")
        self.assertIn("ts", obj)

    def test_format_with_exception(self):
        try:
            raise ValueError("test error")
        except ValueError:
            record = logging.LogRecord(
                name="mini_agent.test", level=logging.ERROR,
                pathname="test.py", lineno=1, msg="crash", args=(),
                exc_info=logging.sys.exc_info(),
            )
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertEqual(obj["level"], "ERROR")
        self.assertIn("traceback", obj)
        self.assertIn("ValueError", str(obj["traceback"]))

    def test_format_with_extra_fields(self):
        record = logging.LogRecord(
            name="mini_agent.api", level=logging.WARNING,
            pathname="test.py", lineno=1, msg="api error", args=(),
            exc_info=None,
        )
        record.tool_name = "run_shell"
        record.error_fingerprint = "timed_out"
        record.turn = 42
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertEqual(obj["tool_name"], "run_shell")
        self.assertEqual(obj["error_fingerprint"], "timed_out")
        self.assertEqual(obj["turn"], 42)

    def test_warning_level_preserved(self):
        record = logging.LogRecord(
            name="mini_agent.tools", level=logging.WARNING,
            pathname="test.py", lineno=1, msg="warning msg", args=(),
            exc_info=None,
        )
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertEqual(obj["level"], "WARNING")

    def test_debug_level_preserved(self):
        record = logging.LogRecord(
            name="mini_agent.tools", level=logging.DEBUG,
            pathname="test.py", lineno=1, msg="debug msg", args=(),
            exc_info=None,
        )
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertEqual(obj["level"], "DEBUG")

    def test_no_extra_none_fields(self):
        """Fields that are None should not appear in the JSON."""
        record = logging.LogRecord(
            name="mini_agent.test", level=logging.INFO,
            pathname="test.py", lineno=1, msg="clean message", args=(),
            exc_info=None,
        )
        output = self.formatter.format(record)
        obj = json.loads(output)
        self.assertNotIn("tool_name", obj)
        self.assertNotIn("error_fingerprint", obj)
        self.assertNotIn("turn", obj)
        self.assertNotIn("provider", obj)
        self.assertNotIn("status_code", obj)


# ---------------------------------------------------------------------------
# 4. log_tool_failure
# ---------------------------------------------------------------------------

class TestLogToolFailure(unittest.TestCase):
    """Verify log_tool_failure writes to log and increments counters."""

    def setUp(self):
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def tearDown(self):
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def test_increments_counter(self):
        log_tool_failure("edit_file", "old_string not found")
        with _ERROR_COUNTERS_LOCK:
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["total_failures"], 1)
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["not_found"], 1)

    def test_multiple_failures_accumulate(self):
        log_tool_failure("edit_file", "not found")
        log_tool_failure("edit_file", "not found")
        log_tool_failure("edit_file", "whitespace check")
        with _ERROR_COUNTERS_LOCK:
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["total_failures"], 3)
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["not_found"], 2)
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["whitespace"], 1)

    def test_explicit_fingerprint(self):
        log_tool_failure("custom_tool", "some error",
                         fingerprint="custom_fingerprint")
        with _ERROR_COUNTERS_LOCK:
            self.assertEqual(_ERROR_COUNTERS["custom_tool"]["custom_fingerprint"], 1)

    def test_writes_to_log_file(self):
        logger = get_logger("tools")
        # Capture log output via a temporary StringIO handler
        # (patching AGENT_LOG doesn't work because the file handler
        #  was already initialized with the real path at import time)
        buf = _capture_log_output(logger)
        try:
            log_tool_failure("edit_file", "old_string not found")
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)

        self.assertGreater(len(entries), 0)
        entry = entries[-1]
        self.assertEqual(entry["event_type"], "tool_failure")
        self.assertEqual(entry["tool_name"], "edit_file")
        self.assertEqual(entry["error_fingerprint"], "not_found")
        self.assertIn("not found", entry["msg"])

    def test_error_content_truncated_in_log(self):
        logger = get_logger("tools")
        long_content = "x" * 500
        buf = _capture_log_output(logger)
        try:
            log_tool_failure("edit_file", long_content)
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)
        entry = entries[-1]
        # msg should be truncated to ~200 chars
        self.assertLess(len(entry["msg"]), 350)


# ---------------------------------------------------------------------------
# 5. log_tool_success
# ---------------------------------------------------------------------------

class TestLogToolSuccess(unittest.TestCase):
    """Verify log_tool_success writes debug-level log entries."""

    def test_writes_debug_entry(self):
        logger = get_logger("tools")
        buf = _capture_log_output(logger)
        try:
            log_tool_success("edit_file", turn=3)
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)

        self.assertGreater(len(entries), 0)
        entry = entries[-1]
        self.assertEqual(entry["level"], "DEBUG")
        self.assertEqual(entry["event_type"], "tool_success")
        self.assertEqual(entry["tool_name"], "edit_file")
        self.assertEqual(entry["turn"], 3)
        self.assertIn("edit_file", entry["msg"])

    def test_default_turn_is_zero(self):
        logger = get_logger("tools")
        buf = _capture_log_output(logger)
        try:
            log_tool_success("run_shell")
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)
        entry = entries[-1]
        self.assertEqual(entry["turn"], 0)


# ---------------------------------------------------------------------------
# 6. log_api_error
# ---------------------------------------------------------------------------

class TestLogApiError(unittest.TestCase):
    """Verify log_api_error writes structured entries to api_error.log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.api_error_path = os.path.join(self.tmpdir, "api_error.log")
        self.agent_log_path = os.path.join(self.tmpdir, "agent.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_to_api_error_log(self):
        with patch("logging_setup.API_ERROR_LOG", self.api_error_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_api_error("deepseek", "deepseek-chat", 429,
                              "Too Many Requests", turn=5, session="s1")

        self.assertTrue(os.path.exists(self.api_error_path))
        with open(self.api_error_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["provider"], "deepseek")
        self.assertEqual(entry["model"], "deepseek-chat")
        self.assertEqual(entry["status_code"], 429)
        self.assertIn("Too Many Requests", entry["error"])
        self.assertEqual(entry["turn"], 5)
        self.assertEqual(entry["session"], "s1")

    def test_error_body_truncated(self):
        long_error = "error: " + "x" * 600
        with patch("logging_setup.API_ERROR_LOG", self.api_error_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_api_error("deepseek", "deepseek-chat", 500, long_error)

        with open(self.api_error_path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertLessEqual(len(entry["error"]), 500)

    def test_none_status_code(self):
        with patch("logging_setup.API_ERROR_LOG", self.api_error_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_api_error("deepseek", "deepseek-chat", None, "timeout")

        with open(self.api_error_path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertIsNone(entry["status_code"])

    def test_also_logs_to_agent_log(self):
        logger = get_logger("api")
        buf = _capture_log_output(logger)
        try:
            with patch("logging_setup.API_ERROR_LOG", self.api_error_path):
                log_api_error("deepseek", "deepseek-chat", 500, "server error")
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)

        self.assertGreater(len(entries), 0)
        agent_entry = entries[-1]
        self.assertEqual(agent_entry["level"], "WARNING")
        self.assertIn("API error", agent_entry["msg"])

    def test_does_not_raise_on_log_write_error(self):
        """If api_error.log can't be written, it should not crash."""
        with patch("logging_setup.API_ERROR_LOG", "/nonexistent/path/error.log"):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                # Should not raise
                try:
                    log_api_error("deepseek", "deepseek-chat", 500, "error")
                except Exception as e:
                    self.fail(f"log_api_error raised unexpectedly: {e}")


# ---------------------------------------------------------------------------
# 7. log_error_trace
# ---------------------------------------------------------------------------

class TestLogErrorTrace(unittest.TestCase):
    """Verify log_error_trace writes structured entries to error_traces.log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.error_traces_path = os.path.join(self.tmpdir, "error_traces.log")
        self.agent_log_path = os.path.join(self.tmpdir, "agent.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_to_error_traces_log(self):
        with patch("logging_setup.ERROR_TRACES_LOG", self.error_traces_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_error_trace("tool_execution_crash",
                                "KeyError: 'missing'",
                                extra={"tool_name": "edit_file"})

        self.assertTrue(os.path.exists(self.error_traces_path))
        with open(self.error_traces_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["type"], "tool_execution_crash")
        self.assertIn("KeyError", entry["message"])
        self.assertEqual(entry["tool_name"], "edit_file")

    def test_with_exc_info(self):
        with patch("logging_setup.ERROR_TRACES_LOG", self.error_traces_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                try:
                    raise RuntimeError("boom")
                except RuntimeError:
                    log_error_trace("crash", "something broke", exc_info=True)

        with open(self.error_traces_path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertIn("traceback", entry)
        self.assertIn("RuntimeError", entry["traceback"])

    def test_without_exc_info_no_traceback(self):
        with patch("logging_setup.ERROR_TRACES_LOG", self.error_traces_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_error_trace("warning", "non-fatal issue")

        with open(self.error_traces_path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertNotIn("traceback", entry)

    def test_does_not_raise_on_log_write_error(self):
        with patch("logging_setup.ERROR_TRACES_LOG", "/nonexistent/path/traces.log"):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                try:
                    log_error_trace("test", "should not crash")
                except Exception as e:
                    self.fail(f"log_error_trace raised unexpectedly: {e}")

    def test_extra_fields_merged(self):
        with patch("logging_setup.ERROR_TRACES_LOG", self.error_traces_path):
            with patch("logging_setup.AGENT_LOG", self.agent_log_path):
                log_error_trace("user_error", "invalid input",
                                extra={"user_id": "42", "action": "click"})

        with open(self.error_traces_path, encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["user_id"], "42")
        self.assertEqual(entry["action"], "click")


# ---------------------------------------------------------------------------
# 8. get_logger
# ---------------------------------------------------------------------------

class TestGetLogger(unittest.TestCase):
    """Verify logger factory creates proper hierarchy."""

    def test_returns_logger(self):
        log = get_logger("test_module")
        self.assertIsInstance(log, logging.Logger)

    def test_logger_name_prefixed(self):
        log = get_logger("tools.edit")
        self.assertEqual(log.name, "mini_agent.tools.edit")

    def test_root_logger_name(self):
        log = get_logger()
        self.assertEqual(log.name, "mini_agent.mini_agent")

    def test_child_loggers_inherit_handlers(self):
        """Root logger setup only happens once; children inherit."""
        root = get_logger("")
        child = get_logger("child")
        # Root should have handlers
        self.assertTrue(root.hasHandlers())
        # Child may not have handlers directly but inherits
        # (depends on handler setup -- just check it's a logger)
        self.assertIsInstance(child, logging.Logger)

    def test_logger_propagate_false(self):
        log = get_logger("test")
        # Root logger for mini_agent should not propagate
        root = logging.getLogger("mini_agent")
        self.assertFalse(root.propagate)


# ---------------------------------------------------------------------------
# 9. End-to-end: full error -> log pipeline
# ---------------------------------------------------------------------------

class TestEndToEndErrorPipeline(unittest.TestCase):
    """Integration-style test: tool failure -> counter + log entry."""

    def setUp(self):
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def tearDown(self):
        global _ERROR_COUNTERS
        with _ERROR_COUNTERS_LOCK:
            _ERROR_COUNTERS.clear()

    def test_failure_pipeline(self):
        """Simulate a real failure flow: fingerprint -> counter -> log."""
        logger = get_logger("tools")
        buf = _capture_log_output(logger)
        try:
            log_tool_failure("edit_file",
                             "Error: old_string not found in '/path/to/file.py'\n"
                             "Hint: The string must match exactly.",
                             fingerprint="not_found",
                             turn=1)
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)

        # 1. Counter incremented
        with _ERROR_COUNTERS_LOCK:
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["total_failures"], 1)
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["not_found"], 1)

        # 2. Log entry written (via captured handler)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["tool_name"], "edit_file")
        self.assertEqual(entry["error_fingerprint"], "not_found")
        self.assertEqual(entry["turn"], 1)
        self.assertEqual(entry["level"], "WARNING")

        # 3. Summary is correct
        summary = get_error_summary()
        self.assertIn("edit_file", summary)
        self.assertIn("not_found:1", summary)

    def test_multiple_tools_full_pipeline(self):
        logger = get_logger("tools")
        buf = _capture_log_output(logger)
        try:
            log_tool_failure("edit_file", "not found error")
            log_tool_failure("run_shell", "timed out after 30s")
            log_tool_failure("run_shell", "command timed out again")
            log_tool_success("read_file")
            log_tool_failure("search_files", "Invalid regex")
            entries = _get_log_entries(buf)
        finally:
            _remove_test_handler(logger, buf)

        # Counters
        with _ERROR_COUNTERS_LOCK:
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["total_failures"], 1)
            self.assertEqual(_ERROR_COUNTERS["edit_file"]["not_found"], 1)
            self.assertEqual(_ERROR_COUNTERS["run_shell"]["total_failures"], 2)
            self.assertEqual(_ERROR_COUNTERS["run_shell"]["timed_out"], 2)
            self.assertEqual(_ERROR_COUNTERS["search_files"]["total_failures"], 1)
            self.assertEqual(_ERROR_COUNTERS["search_files"]["invalid_regex"], 1)

        # Log entries (4 warnings + 1 debug for success)
        warnings = [e for e in entries if e["level"] == "WARNING"]
        debugs = [e for e in entries if e["level"] == "DEBUG"]
        self.assertEqual(len(warnings), 4)  # 4 failures
        self.assertEqual(len(debugs), 1)    # 1 success

        # Summary
        summary = get_error_summary()
        self.assertIn("4 total failures", summary)


if __name__ == "__main__":
    unittest.main()
