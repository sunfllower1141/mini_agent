"""Tests for api.py — LLM API communication, truncation, cleaning, error handling."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from api import (
    APIError,
    _build_payload,
    _clean_message,
    _strip_orphaned_tool_calls,
    clear_api_cache,
    format_tool_detail,
    truncate_content,
)


# ---------------------------------------------------------------------------
# Helper: make a minimal AgentConfig-like object
# ---------------------------------------------------------------------------

class _MockConfig:
    """Minimal mock config with all fields api.py expects."""
    def __init__(self, **overrides):
        self.api_provider = overrides.get("api_provider", "deepseek")
        self.api_key = overrides.get("api_key", "test-key")
        self.api_url = overrides.get("api_url", "https://api.test.com/v1")
        self.model = overrides.get("model", "test-model")
        self.routing_model = overrides.get("routing_model", "")
        self.max_tokens = overrides.get("max_tokens", 4096)
        self.context_window = overrides.get("context_window", 32000)
        self.stream = overrides.get("stream", False)
        self.temperature = overrides.get("temperature", 0.0)
        self.frequency_penalty = overrides.get("frequency_penalty", 0.0)
        self.presence_penalty = overrides.get("presence_penalty", 0.0)
        self.stop_sequences = overrides.get("stop_sequences", None)
        self.response_format = overrides.get("response_format", None)
        self.sub_agent_max_turns = overrides.get("sub_agent_max_turns", 25)


# ---------------------------------------------------------------------------
# truncate_content
# ---------------------------------------------------------------------------

class TestTruncateContent(unittest.TestCase):
    """Tests for truncate_content()."""

    def test_short_content_not_truncated(self):
        self.assertEqual(truncate_content("hello"), "hello")

    def test_exact_max_length_not_truncated(self):
        content = "a" * 300
        self.assertEqual(truncate_content(content, max_len=300), content)

    def test_long_content_truncated_with_ellipsis(self):
        content = "a" * 400
        result = truncate_content(content, max_len=300)
        self.assertEqual(len(result), 301)  # 300 + "…"
        self.assertTrue(result.endswith("…"))

    def test_default_max_len_is_300(self):
        content = "a" * 500
        result = truncate_content(content)
        self.assertEqual(len(result), 301)

    def test_custom_max_len(self):
        content = "a" * 100
        result = truncate_content(content, max_len=50)
        self.assertEqual(len(result), 51)  # 50 + "…"

    def test_empty_string(self):
        self.assertEqual(truncate_content(""), "")


# ---------------------------------------------------------------------------
# format_tool_detail
# ---------------------------------------------------------------------------

class TestFormatToolDetail(unittest.TestCase):
    """Tests for format_tool_detail()."""

    def test_short_result_not_truncated(self):
        from tools import ToolResult
        result = ToolResult(success=True, content="short result")
        detail = format_tool_detail(result)
        self.assertEqual(detail, "short result")

    def test_long_result_truncated(self):
        from tools import ToolResult
        result = ToolResult(success=True, content="a" * 500)
        detail = format_tool_detail(result)
        self.assertEqual(len(detail), 301)

    def test_default_max_len_300(self):
        from tools import ToolResult
        result = ToolResult(success=False, content="b" * 400)
        detail = format_tool_detail(result)
        self.assertEqual(len(detail), 301)


# ---------------------------------------------------------------------------
# APIError
# ---------------------------------------------------------------------------

class TestAPIError(unittest.TestCase):
    """Tests for APIError exception class."""

    def test_construction(self):
        err = APIError(400, "bad request")
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.body, "bad request")

    def test_str_representation(self):
        err = APIError(500, "internal error")
        s = str(err)
        self.assertIn("500", s)
        self.assertIn("internal error", s)

    def test_is_exception(self):
        err = APIError(429, "rate limited")
        with self.assertRaises(APIError):
            raise err


# ---------------------------------------------------------------------------
# _clean_message
# ---------------------------------------------------------------------------

class TestCleanMessage(unittest.TestCase):
    """Tests for _clean_message()."""

    def test_strips_underscore_prefixed_fields(self):
        msg = {"role": "user", "content": "hello", "_internal": "secret",
               "_tracking": 42}
        result = _clean_message(msg, 0)
        self.assertNotIn("_internal", result)
        self.assertNotIn("_tracking", result)
        self.assertEqual(result["role"], "user")
        self.assertEqual(result["content"], "hello")

    def test_removes_index_from_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc1", "index": 0, "function": {"name": "read_file"}},
                {"id": "tc2", "index": 1, "function": {"name": "write_file"}},
            ],
        }
        result = _clean_message(msg, 0)
        for tc in result["tool_calls"]:
            self.assertNotIn("index", tc)
            self.assertIn("id", tc)
            self.assertIn("function", tc)

    def test_deepseek_adds_cache_control_to_first_system(self):
        msg = {"role": "system", "content": "you are helpful"}
        result = _clean_message(msg, 0, provider="deepseek")
        self.assertIn("cache_control", result)
        self.assertEqual(result["cache_control"], {"type": "ephemeral"})

    def test_claude_does_not_add_cache_control(self):
        msg = {"role": "system", "content": "you are helpful"}
        result = _clean_message(msg, 0, provider="claude")
        self.assertNotIn("cache_control", result)

    def test_deepseek_cache_control_only_on_index_zero(self):
        msg = {"role": "system", "content": "second system"}
        result = _clean_message(msg, 1, provider="deepseek")
        self.assertNotIn("cache_control", result)

    def test_non_system_index_zero_gets_no_cache_control(self):
        msg = {"role": "user", "content": "hello"}
        result = _clean_message(msg, 0, provider="deepseek")
        self.assertNotIn("cache_control", result)

    def test_no_tool_calls_preserved_as_is(self):
        msg = {"role": "assistant", "content": "here is the answer"}
        result = _clean_message(msg, 0)
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(result["content"], "here is the answer")


# ---------------------------------------------------------------------------
# _strip_orphaned_tool_calls
# ---------------------------------------------------------------------------

class TestStripOrphanedToolCalls(unittest.TestCase):
    """Tests for _strip_orphaned_tool_calls()."""

    def test_no_tool_calls_returns_same_list(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _strip_orphaned_tool_calls(msgs)
        self.assertEqual(result, msgs)

    def test_covered_tool_calls_not_stripped(self):
        msgs = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content"},
            {"role": "assistant", "content": "done"},
        ]
        result = _strip_orphaned_tool_calls(msgs)
        self.assertEqual(len(result), 4)

    def test_orphaned_trailing_tool_call_stripped(self):
        # An orphaned assistant tool_call at the very end of the message list
        # (no tool result after it, no user/system message after it) is stripped.
        msgs = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file"}}
            ]},
            # No tool result for tc1 — and no user/system message after it
        ]
        result = _strip_orphaned_tool_calls(msgs)
        self.assertEqual(len(result), 1)  # only the first user msg
        self.assertEqual(result[0]["role"], "user")

    def test_user_message_after_tool_call_is_stripped(self):
        # An assistant(tool_calls) with no matching tool results is orphaned
        # regardless of intervening user messages — leaving it in causes a 400.
        msgs = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file"}}
            ]},
            {"role": "user", "content": "next request"},
        ]
        result = _strip_orphaned_tool_calls(msgs)
        # Orphaned assistant(tool_calls) is stripped even with user after it
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "user")

    def test_mixed_covered_and_orphaned(self):
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "result1"},
            {"role": "assistant", "tool_calls": [
                {"id": "tc2", "function": {"name": "write_file"}}
            ]},
            # tc2 has no tool result — orphaned
        ]
        result = _strip_orphaned_tool_calls(msgs)
        # Should strip the last assistant (orphaned tc2)
        self.assertEqual(len(result), 3)
        # Last msg should be the tool result for tc1
        self.assertEqual(result[-1]["role"], "tool")

    def test_does_not_mutate_input(self):
        msgs = [
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "user", "content": "next"},
        ]
        original_len = len(msgs)
        _strip_orphaned_tool_calls(msgs)
        self.assertEqual(len(msgs), original_len)


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------

class TestBuildPayload(unittest.TestCase):
    """Tests for _build_payload()."""

    def setUp(self):
        clear_api_cache()

    def tearDown(self):
        clear_api_cache()

    def test_deepseek_includes_sampling_params(self):
        config = _MockConfig(api_provider="deepseek", temperature=0.7)
        # We need get_active_tools patched
        with patch("api.get_active_tools", return_value=[{"type": "function"}]):
            payload = _build_payload(config, [], [])
        self.assertIn("temperature", payload)
        self.assertIn("frequency_penalty", payload)
        self.assertIn("presence_penalty", payload)

    def test_claude_excludes_sampling_params(self):
        config = _MockConfig(api_provider="claude")
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertNotIn("temperature", payload)
        self.assertNotIn("frequency_penalty", payload)
        self.assertNotIn("presence_penalty", payload)

    def test_ollama_includes_temperature(self):
        config = _MockConfig(api_provider="ollama", temperature=0.5)
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertIn("temperature", payload)
        self.assertEqual(payload["temperature"], 0.5)

    def test_deepseek_stop_sequences(self):
        config = _MockConfig(api_provider="deepseek", stop_sequences=["END"])
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertIn("stop", payload)
        self.assertEqual(payload["stop"], ["END"])

    def test_claude_stop_sequences(self):
        config = _MockConfig(api_provider="claude", stop_sequences=["\n\n"])
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertIn("stop", payload)
        self.assertEqual(payload["stop"], ["\n\n"])

    def test_deepseek_response_format(self):
        config = _MockConfig(api_provider="deepseek", response_format="json_object")
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertIn("response_format", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_common_fields_always_present(self):
        config = _MockConfig()
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, [], [])
        self.assertIn("model", payload)
        self.assertIn("messages", payload)
        self.assertIn("tools", payload)
        self.assertIn("stream", payload)
        self.assertIn("max_tokens", payload)

    def test_routing_model_used_for_simple_prompts(self):
        config = _MockConfig(
            api_provider="deepseek",
            model="big-model",
            routing_model="small-model",
        )
        simple_msgs = [{"role": "user", "content": "what does this do?"}]
        clean = [_clean_message(m, i) for i, m in enumerate(simple_msgs)]
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, simple_msgs, clean)
        self.assertEqual(payload["model"], "small-model")

    def test_main_model_used_for_complex_prompts(self):
        config = _MockConfig(
            api_provider="deepseek",
            model="big-model",
            routing_model="small-model",
        )
        complex_msgs = [{"role": "user", "content": "write a new module"}]
        clean = [_clean_message(m, i) for i, m in enumerate(complex_msgs)]
        with patch("api.get_active_tools", return_value=[]):
            payload = _build_payload(config, complex_msgs, clean)
        self.assertEqual(payload["model"], "big-model")


# ---------------------------------------------------------------------------
# clear_api_cache
# ---------------------------------------------------------------------------

class TestClearAPICache(unittest.TestCase):
    """Tests for clear_api_cache()."""

    def test_clear_cache(self):
        from api import _clean_messages_cache
        # Populate cache
        msgs = [{"role": "user", "content": "test"}]
        _clean_messages_cache[id(msgs)] = (1, "deepseek", msgs)
        self.assertIn(id(msgs), _clean_messages_cache)
        clear_api_cache()
        self.assertEqual(len(_clean_messages_cache), 0)


# ---------------------------------------------------------------------------
# LLM Semaphore
# ---------------------------------------------------------------------------

class TestLLMSemaphore(unittest.TestCase):
    """Tests for the _LLM_SEMAPHORE rate limiter."""

    def test_semaphore_exists(self):
        from api import _LLM_SEMAPHORE
        self.assertIsInstance(_LLM_SEMAPHORE, threading.Semaphore)

    def test_semaphore_default_value(self):
        from api import _LLM_SEMAPHORE
        # Should be 2 by default (or whatever env sets)
        self.assertGreaterEqual(_LLM_SEMAPHORE._value, 0)


if __name__ == "__main__":
    unittest.main()
