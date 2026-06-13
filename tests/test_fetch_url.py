#!/usr/bin/env python3
"""
test_fetch_url.py -- tests for the fetch_url tool (_fetch_url in tools.search_ops).
"""

import unittest
from unittest.mock import patch, MagicMock

from conftest import make_gates as _gates
from tools import ToolResult


def _make_mock_response(status=200, content_type="text/html", data=b"<html><body>Hello World</body></html>"):
    """Build a mock urlopen response."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = {"Content-Type": content_type}
    mock_resp.read.return_value = data
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchUrl(unittest.TestCase):

    def setUp(self):
        self.write_gate, self.read_gate = _gates()

    # -- import is done inside each test to avoid module-level side effects --

    def test_valid_url_returns_content(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response(
            data=b"<html><body>Hello World</body></html>",
        )
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertIn("Hello World", result.content)

    def test_invalid_url_returns_error(self):
        from tools.search_ops import _fetch_url
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("bad url")):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://invalid.example"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.success)
        self.assertIn("Failed to fetch URL", result.content)

    def test_non_text_content_type_returns_error(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response(
            content_type="application/json",
            data=b'{"key": "value"}',
        )
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com/data.json"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.success)
        self.assertIn("Cannot read content type", result.content)
        self.assertIn("application/json", result.content)

    def test_truncation_with_max_chars(self):
        from tools.search_ops import _fetch_url
        long_data = b"A" * 5000
        mock_resp = _make_mock_response(data=long_data)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com", "max_chars": 100},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        self.assertIn("showing first 100", result.content)
        self.assertEqual(len(result.content.split("\n\n", 1)[1]), 100)

    def test_timeout_clamped_to_30(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response()
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com", "timeout": 999},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        # timeout should be clamped to 30, not 999
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs["timeout"], 30)

    def test_default_timeout_is_15(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response()
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs["timeout"], 15)

    def test_timeout_below_30_preserved(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response()
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com", "timeout": 5},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs["timeout"], 5)

    def test_text_plain_content_type_accepted(self):
        from tools.search_ops import _fetch_url
        mock_resp = _make_mock_response(
            content_type="text/plain",
            data=b"Plain text content here.",
        )
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com/robots.txt"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        self.assertIn("Plain text content here", result.content)

    def test_generic_exception_returns_error(self):
        from tools.search_ops import _fetch_url
        with patch("urllib.request.urlopen", side_effect=ValueError("unexpected error")):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.success)
        self.assertIn("Error fetching URL", result.content)

    def test_default_max_chars_is_10000(self):
        from tools.search_ops import _fetch_url
        data = b"A" * 500
        mock_resp = _make_mock_response(data=data)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("urllib.request.Request", return_value=MagicMock()):
                result = _fetch_url(
                    {"url": "http://example.com"},
                    self.write_gate,
                    self.read_gate,
                )
        self.assertTrue(result.success)
        # 500 chars total, no truncation
        self.assertNotIn("showing first", result.content)


if __name__ == "__main__":
    unittest.main()
