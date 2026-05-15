#!/usr/bin/env python3
"""test_retry.py — dedicated tests for retry.py"""

import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from retry import _jittered_delay, _request_with_retry, _MAX_RETRIES, _RETRYABLE_STATUSES


class TestJitteredDelay(unittest.TestCase):
    """Tests for _jittered_delay bounds."""

    def test_attempt_0_bounds(self):
        """attempt=0 returns 0.5-1.5 (exclusive upper)."""
        for _ in range(100):
            d = _jittered_delay(0)
            self.assertGreaterEqual(d, 0.5)
            self.assertLess(d, 1.5)

    def test_attempt_1_bounds(self):
        """attempt=1 returns 1.0-3.0 (exclusive upper)."""
        for _ in range(100):
            d = _jittered_delay(1)
            self.assertGreaterEqual(d, 1.0)
            self.assertLess(d, 3.0)

    def test_attempt_2_bounds(self):
        """attempt=2 returns 2.0-6.0 (exclusive upper)."""
        for _ in range(100):
            d = _jittered_delay(2)
            self.assertGreaterEqual(d, 2.0)
            self.assertLess(d, 6.0)

    def test_attempt_3_bounds(self):
        """attempt=3 returns 4.0-12.0 (exclusive upper)."""
        for _ in range(100):
            d = _jittered_delay(3)
            self.assertGreaterEqual(d, 4.0)
            self.assertLess(d, 12.0)


class TestCancelEvent(unittest.TestCase):
    """Tests for cancel_event behaviour in _request_with_retry."""

    def test_cancel_before_request_returns_none(self):
        """When cancel_event is set before the request, return None immediately."""
        cancel = threading.Event()
        cancel.set()
        mock_post = MagicMock()
        with patch.object(requests, "post", mock_post):
            result = _request_with_retry(
                requests,  # session = requests module
                "http://api.example.com",
                json={"messages": []},
                cancel_event=cancel,
            )
        self.assertIsNone(result)
        mock_post.assert_not_called()

    def test_cancel_during_delay_returns_none(self):
        """When cancel fires during a retry delay, return None."""
        cancel = threading.Event()

        # Return a 503 response first, then set cancel during the delay
        retry_resp = MagicMock()
        retry_resp.ok = False
        retry_resp.status_code = 503

        def side_effect(*args, **kwargs):
            # After the first call, set the cancel event so the delay wait returns True
            cancel.set()
            return retry_resp

        mock_post = MagicMock(side_effect=side_effect)
        with patch.object(requests, "post", mock_post):
            result = _request_with_retry(
                requests,
                "http://api.example.com",
                json={"messages": []},
                cancel_event=cancel,
            )
        self.assertIsNone(result)
        # Should have been called exactly once (first attempt fails, then cancel)
        self.assertEqual(mock_post.call_count, 1)


class TestRetryableStatusCodes(unittest.TestCase):
    """Tests that individual status codes are retried or not."""

    def setUp(self):
        self.success_resp = MagicMock()
        self.success_resp.ok = True
        self.success_resp.status_code = 200

    def _make_resp(self, status_code: int):
        resp = MagicMock()
        resp.ok = (status_code < 400)
        resp.status_code = status_code
        return resp

    def test_429_is_retried(self):
        """429 Too Many Requests is retryable, succeeds on retry."""
        retry_resp = self._make_resp(429)
        mock_post = MagicMock(side_effect=[retry_resp, self.success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):  # don't actually sleep
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, self.success_resp)
        self.assertEqual(mock_post.call_count, 2)

    def test_500_is_retried(self):
        """500 Internal Server Error is retryable."""
        retry_resp = self._make_resp(500)
        mock_post = MagicMock(side_effect=[retry_resp, self.success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, self.success_resp)
        self.assertEqual(mock_post.call_count, 2)

    def test_502_is_retried(self):
        """502 Bad Gateway is retryable."""
        retry_resp = self._make_resp(502)
        mock_post = MagicMock(side_effect=[retry_resp, self.success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, self.success_resp)
        self.assertEqual(mock_post.call_count, 2)

    def test_503_is_retried(self):
        """503 Service Unavailable is retryable."""
        retry_resp = self._make_resp(503)
        mock_post = MagicMock(side_effect=[retry_resp, self.success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, self.success_resp)
        self.assertEqual(mock_post.call_count, 2)

    def test_504_is_retried(self):
        """504 Gateway Timeout is retryable."""
        retry_resp = self._make_resp(504)
        mock_post = MagicMock(side_effect=[retry_resp, self.success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, self.success_resp)
        self.assertEqual(mock_post.call_count, 2)

    def test_400_is_not_retried(self):
        """400 Bad Request is NOT retryable — returns immediately."""
        resp_400 = self._make_resp(400)
        mock_post = MagicMock(return_value=resp_400)
        with patch.object(requests, "post", mock_post):
            result = _request_with_retry(
                requests,
                "http://api.example.com",
                json={"messages": []},
            )
        self.assertIs(result, resp_400)
        self.assertEqual(mock_post.call_count, 1)


class TestCancelDuringNetworkErrorDelay(unittest.TestCase):
    """Test cancellation during retry-wait after a RequestException (line 84)."""

    def test_cancel_during_network_error_delay_returns_none(self):
        """When cancel fires during retry delay after a network error, return None."""
        cancel = threading.Event()

        exc = requests.ConnectionError("connection refused")

        def side_effect(*args, **kwargs):
            # Set cancel during the post call so cancel_event.wait() returns True
            cancel.set()
            raise exc

        mock_post = MagicMock(side_effect=side_effect)
        with patch.object(requests, "post", mock_post):
            result = _request_with_retry(
                requests,
                "http://api.example.com",
                json={"messages": []},
                cancel_event=cancel,
            )
        self.assertIsNone(result)
        # Only one attempt — cancelled during first retry delay
        self.assertEqual(mock_post.call_count, 1)


class TestExhaustedRetries(unittest.TestCase):
    """Tests for exhausted retry behaviour."""

    def test_exhausted_on_status_code_returns_response(self):
        """All retries exhausted on a retryable status code returns the response (structured, not exception)."""
        retry_resp = MagicMock()
        retry_resp.ok = False
        retry_resp.status_code = 503
        retry_resp.text = "Service Unavailable"

        mock_post = MagicMock(return_value=retry_resp)
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        # Returns the last response — caller checks result.ok
        self.assertIs(result, retry_resp)
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 503)
        # _MAX_RETRIES + 1 = 4 attempts
        self.assertEqual(mock_post.call_count, _MAX_RETRIES + 1)

    def test_exhausted_on_network_error_raises(self):
        """All retries exhausted on network errors re-raises the last exception."""
        exc = requests.ConnectionError("connection refused")
        mock_post = MagicMock(side_effect=exc)
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                with self.assertRaises(requests.ConnectionError):
                    _request_with_retry(
                        requests,
                        "http://api.example.com",
                        json={"messages": []},
                    )
        self.assertEqual(mock_post.call_count, _MAX_RETRIES + 1)

    def test_network_error_retries_then_succeeds(self):
        """Network error on first attempt, succeeds on retry."""
        exc = requests.ConnectionError("connection refused")
        success_resp = MagicMock()
        success_resp.ok = True
        success_resp.status_code = 200
        mock_post = MagicMock(side_effect=[exc, success_resp])
        with patch.object(requests, "post", mock_post):
            with patch("time.sleep"):
                result = _request_with_retry(
                    requests,
                    "http://api.example.com",
                    json={"messages": []},
                )
        self.assertIs(result, success_resp)
        self.assertEqual(mock_post.call_count, 2)


class TestConnectionReuse(unittest.TestCase):
    """Tests for session.post connection reuse."""

    def test_session_post_used_when_available(self):
        """When session has a .post method, it is used instead of requests.post."""
        success_resp = MagicMock()
        success_resp.ok = True
        success_resp.status_code = 200

        mock_session = MagicMock()
        mock_session.post.return_value = success_resp

        with patch.object(requests, "post") as mock_requests_post:
            result = _request_with_retry(
                mock_session,
                "http://api.example.com",
                json={"messages": []},
            )
        self.assertIs(result, success_resp)
        mock_session.post.assert_called_once()
        # requests.post should NOT have been called
        mock_requests_post.assert_not_called()

    def test_requests_post_used_when_session_lacks_post(self):
        """When session does not have .post, requests.post is used as fallback."""
        success_resp = MagicMock()
        success_resp.ok = True
        success_resp.status_code = 200

        # An object without a .post attribute
        class NoPost:
            pass

        session = NoPost()
        with patch.object(requests, "post", return_value=success_resp) as mock_post:
            result = _request_with_retry(
                session,
                "http://api.example.com",
                json={"messages": []},
            )
        self.assertIs(result, success_resp)
        mock_post.assert_called_once()

    def test_session_post_retries_on_503(self):
        """Session.post is reused across retries."""
        retry_resp = MagicMock()
        retry_resp.ok = False
        retry_resp.status_code = 503
        success_resp = MagicMock()
        success_resp.ok = True
        success_resp.status_code = 200

        mock_session = MagicMock()
        mock_session.post.side_effect = [retry_resp, success_resp]

        with patch("time.sleep"):
            result = _request_with_retry(
                mock_session,
                "http://api.example.com",
                json={"messages": []},
            )
        self.assertIs(result, success_resp)
        self.assertEqual(mock_session.post.call_count, 2)


class TestRetryableStatusesSet(unittest.TestCase):
    """Sanity-check the retryable status codes set."""

    def test_all_expected_statuses_present(self):
        """Verify _RETRYABLE_STATUSES contains exactly the expected codes."""
        expected = {429, 500, 502, 503, 504}
        self.assertEqual(_RETRYABLE_STATUSES, expected)

    def test_400_not_in_retryable(self):
        """400 is not in the retryable set."""
        self.assertNotIn(400, _RETRYABLE_STATUSES)


if __name__ == "__main__":
    unittest.main()
