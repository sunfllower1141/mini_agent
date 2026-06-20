#!/usr/bin/env python3
"""Smoke tests for new Dirac-adopted context curation modules."""
from __future__ import annotations

import pytest


# ============================================================================
# context_error_detect
# ============================================================================

class TestContextErrorDetect:
    """Tests for core.context_error_detect -- multi-provider context window detection."""

    def test_openai_context_length_exceeded(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert is_context_window_error_from_body(
            400, '{"error": {"code": "context_length_exceeded"}}'
        )

    def test_reduce_length_message(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert is_context_window_error_from_body(
            400, "Please reduce the length of the messages or completion"
        )

    def test_input_too_long(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert is_context_window_error_from_body(400, "input is too long for model")

    def test_token_exceed_message(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert is_context_window_error_from_body(
            400, "token count exceeds maximum context window"
        )

    def test_context_window_exceeded_in_text(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert is_context_window_error_from_body(
            400, "input exceeds context window limit"
        )

    def test_non_context_errors_do_not_match(self):
        from core.context_error_detect import is_context_window_error_from_body
        assert not is_context_window_error_from_body(
            400, '{"error": {"code": "invalid_api_key"}}'
        )
        assert not is_context_window_error_from_body(429, "rate limit exceeded")
        assert not is_context_window_error_from_body(500, "internal server error")

    def test_is_context_window_error_with_object(self):
        from core.context_error_detect import is_context_window_error

        class FakeError:
            def __init__(self, status_code, message):
                self.status_code = status_code
                self.message = message

        assert is_context_window_error(
            FakeError(400, "context_length_exceeded: too many tokens")
        )
        assert not is_context_window_error(FakeError(400, "invalid request"))


# ============================================================================
# env_tracker
# ============================================================================

class TestEnvTracker:
    """Tests for core.env_tracker -- environment context snapshot tracking."""

    def test_record_and_deduplicate(self):
        from core.env_tracker import get_env_tracker, reset_env_tracker
        reset_env_tracker()
        t = get_env_tracker()
        assert t.record()  # first record should succeed
        assert not t.record()  # second record should be deduplicated (no change)

    def test_get_current_has_keys(self):
        from core.env_tracker import get_env_tracker, reset_env_tracker
        reset_env_tracker()
        t = get_env_tracker()
        t.record()
        env = t.get_current()
        assert "os_name" in env
        assert "host_name" in env
        assert "cwd" in env

    def test_get_startup_context(self):
        from core.env_tracker import get_env_tracker, reset_env_tracker
        reset_env_tracker()
        t = get_env_tracker()
        t.record()
        ctx = t.get_startup_context()
        assert "OS:" in ctx
        assert "Host:" in ctx
        assert "CWD:" in ctx

    def test_collect_environment_metadata(self):
        from core.env_tracker import collect_environment_metadata
        meta = collect_environment_metadata()
        assert "os_name" in meta
        assert "host_name" in meta
        assert isinstance(meta["os_name"], str)
        assert len(meta["os_name"]) > 0


# ============================================================================
# _inject_tool_result_stubs
# ============================================================================

class TestInjectToolResultStubs:
    """Tests for context_inject._inject_tool_result_stubs (Dirac pairing validation)."""

    def test_missing_tool_result_gets_stub(self):
        from core.context_inject import _inject_tool_result_stubs
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "read_file"}}],
            },
            {"role": "user", "content": []},  # content-block format, no tool_results
        ]
        injected = _inject_tool_result_stubs(msgs)
        assert injected == 1
        assert msgs[1]["content"][0]["type"] == "tool_result"
        assert msgs[1]["content"][0]["tool_use_id"] == "call_1"
        assert msgs[1]["content"][0]["content"] == "result missing"

    def test_no_missing_tool_results(self):
        from core.context_inject import _inject_tool_result_stubs
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "read_file"}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}
                ],
            },
        ]
        injected = _inject_tool_result_stubs(msgs)
        assert injected == 0

    def test_text_only_user_cannot_inject(self):
        from core.context_inject import _inject_tool_result_stubs
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "read_file"}}],
            },
            {"role": "user", "content": "some text"},
        ]
        injected = _inject_tool_result_stubs(msgs)
        assert injected == 1  # logged but can't inject

    def test_multiple_tool_calls_all_missing(self):
        from core.context_inject import _inject_tool_result_stubs
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "read_file"}},
                    {"id": "call_2", "function": {"name": "write_file"}},
                ],
            },
            {"role": "user", "content": []},
        ]
        injected = _inject_tool_result_stubs(msgs)
        assert injected == 2
        assert len(msgs[1]["content"]) == 2
        assert msgs[1]["content"][0]["tool_use_id"] == "call_1"
        assert msgs[1]["content"][1]["tool_use_id"] == "call_2"


# ============================================================================
# ContextWindowExceeded
# ============================================================================

class TestContextWindowExceeded:
    """Tests for the ContextWindowExceeded exception class."""

    def test_is_subclass_of_api_error(self):
        from api import ContextWindowExceeded, APIError
        assert issubclass(ContextWindowExceeded, APIError)

    def test_str_representation(self):
        from api import ContextWindowExceeded
        err = ContextWindowExceeded(400, "context window exceeded")
        assert "400" in str(err)
        assert "context window exceeded" in str(err)
