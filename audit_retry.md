# Audit: retry.py — Standards Violations

## Findings

| Severity | File | Line | Issue | Fix |
|----------|------|------|-------|-----|
| MEDIUM | retry.py | 32 | `session` parameter has no type hint — only a comment `# requests.Session or the requests module itself` | Annotate as `session: requests.Session \| type[requests]` |
| LOW | retry.py | 22 | `_RETRYABLE_STATUSES` is a mutable `set[int]` at module level — global mutable state | Change to `frozenset[int]` |
| LOW | retry.py | 36 | Magic numbers `(10, 120)` as default timeout values | Define `_DEFAULT_CONNECT_TIMEOUT = 10` and `_DEFAULT_READ_TIMEOUT = 120`, then `timeout: tuple[float, float] = (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)` |
| LOW | retry.py | 28 | Magic numbers `0.5` and implied `1.0` range in jitter formula `(0.5 + random.random())` | Define `_JITTER_MIN = 0.5` and `_JITTER_RANGE = 1.0` constants |
| INFO | retry.py | 49 | Duck-typing via `hasattr(session, "post") and callable(session.post)` is implicit control flow | Use `isinstance(session, requests.Session)` or accept a callable directly |
| N/A | retry.py | 86 | Re-raises raw `requests.RequestException` — not a tool module so structured-dataclass rule doesn't strictly apply | If this were ever called as a tool result, wrap in a dataclass; acceptable for infrastructure |

## Criteria-by-Criteria Summary

| # | Criterion | Status | Detail |
|---|-----------|--------|--------|
| 1 | `from __future__ import annotations` | ✅ PASS | Line 8 |
| 2 | Public functions have type hints | ⚠️ PARTIAL | `session` param (line 32) missing annotation |
| 3 | No magic numbers | ⚠️ PARTIAL | Default timeout `(10,120)` and jitter `0.5` are unnamed |
| 4 | No circular imports | ✅ PASS | Only imports `requests`, `os`, `random`, `sys`, `threading` |
| 5 | No global mutable state | ⚠️ PARTIAL | `_RETRYABLE_STATUSES` is a mutable `set` |
| 6 | Tool results are structured dataclasses | ✅ N/A | Not a tool module; returns `Response \| None` |
| 7 | Explicit control flow | ✅ MOSTLY OK | Duck-typing at line 49 is the only gray area |

## Notes

- Both functions (`_jittered_delay`, `_request_with_retry`) are underscore-prefixed (private by convention) but are effectively public API — `api.py` imports `_request_with_retry` directly. The missing type hint on `session` is the only real type-annotation gap.
- The mutable `set` for `_RETRYABLE_STATUSES` is benign in practice (never mutated) but violates the letter of the rule. A `frozenset` is a trivial fix.
- All findings are low-to-medium severity. No critical architectural violations.
