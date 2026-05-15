#!/usr/bin/env python3
"""
retry.py — HTTP retry logic for mini_agent.

Provides ``_request_with_retry()`` with exponential backoff on transient
failures (429, 5xx).  Used by ``api.py::call_deepseek()``.
"""
from __future__ import annotations

import os
import random
import sys
import threading

import requests

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRYABLE_STATUSES: set[int] = {429, 500, 502, 503, 504}
_TESTING = "PYTEST_CURRENT_TEST" in os.environ


def _jittered_delay(attempt: int) -> float:
    """Return jittered exponential backoff delay: ~0.5-1.5s, ~1-3s, ~2-6s."""
    return (2 ** attempt) * (0.5 + random.random())


def _request_with_retry(
    session,  # requests.Session or the requests module itself
    *args,
    stream: bool = False,
    cancel_event: threading.Event | None = None,
    timeout: tuple[float, float] = (10, 120),
    **kwargs,
) -> requests.Response | None:
    """Send an HTTP request with retry on transient errors.

    Retries up to *_MAX_RETRIES* times with jittered exponential backoff
    (~0.5-1.5s, ~1-3s, ~2-6s)
    on 429 / 5xx status codes.  Non-retryable errors raise immediately.

    *session* is a requests.Session for connection reuse, or the requests
    module itself (for testability — tests patch requests.post).
    *timeout* is the (connect, read) timeout tuple passed to requests.post.
    """
    post = session.post if hasattr(session, "post") and callable(session.post) else requests.post
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        # Check cancel before making a request (avoids wasted HTTP call on shutdown)
        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            r = post(*args, stream=stream, timeout=timeout, **kwargs)
            if r.ok or r.status_code not in _RETRYABLE_STATUSES:
                return r
            # Transient error — retry
            if attempt < _MAX_RETRIES:
                r.close()  # close connection before retry
                delay = _jittered_delay(attempt)
                if not _TESTING:
                    print(
                        f"  ⚠ API {r.status_code}, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                        file=sys.stderr, flush=True,
                    )
                if cancel_event is not None and cancel_event.wait(delay):
                    return None  # cancelled during wait
            else:
                return r  # exhausted retries — return last response (caller checks r.ok)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _jittered_delay(attempt)
                if not _TESTING:
                    print(
                        f"  ⚠ network error ({exc}), retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                        file=sys.stderr, flush=True,
                    )
                if cancel_event is not None and cancel_event.wait(delay):
                    return None  # cancelled during wait
            else:
                raise  # exhausted retries, re-raise

    if last_exc is not None:
        raise last_exc
    return r  # pragma: no cover
