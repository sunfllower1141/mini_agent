# stream.py Standards Audit

## Findings
| Severity | File | Line | Issue | Fix |
|----------|------|------|-------|-----|
| LOW | stream.py | 67 | Magic string `"[DONE]"` is an SSE protocol sentinel used inline, while `_SSE_PREFIX` (line 25) is already a named constant. Inconsistent. | Add `_SSE_DONE = "[DONE]"` module-level constant |
| INFO | stream.py | 27 | Return type annotated as `-> dict` only. The returned dict has a known shape (`role`, `content`, optional `reasoning_content`, `tool_calls`, `_usage`, `_fired_indices`). A TypedDict or dataclass would improve safety. | Consider `StreamedMessage` TypedDict/dataclass |
| INFO | stream.py | 27 | Function name `_parse_stream` has a leading underscore (suggesting private), yet the module docstring advertises it as the module's public API. Misleading naming. | Rename to `parse_stream` or remove underscore |

## Detailed Analysis

### (1) `from __future__ import annotations` — ✅ PASS
Present at line 9. No violation.

### (2) All public functions have type hints — ✅ PASS
`_parse_stream` (line 27) has full type hints: parameters `response: requests.Response`, `on_token: Callable[[str], None] | None`, `on_tool_ready: Callable[[dict], None] | None`, return `-> dict`. No other public functions exist. No violation.

### (3) Magic numbers that should be named UPPER_CASE constants — ⚠️ BORDERLINE
- `THINKING_START`, `THINKING_END` (lines 20-21): properly named constants ✅
- `_SSE_PREFIX` (line 25): properly named constant ✅
- `"[DONE]"` (line 67): SSE protocol sentinel used inline. `_SSE_PREFIX` is already a constant for the same protocol — inconsistent. Flagged as LOW.
- No numeric magic numbers found. All integer literals are standard defaults (0, 1).

### (4) Circular imports — ✅ PASS
Imports: `json`, `sys`, `Callable` (stdlib), `requests` (third-party), `terminal.c, DIM` (internal). Verified `terminal.py` does not import `stream.py`. No circular dependency.

### (5) Global mutable state — ✅ PASS
Module-level names are all immutable strings: `THINKING_START`, `THINKING_END`, `_SSE_PREFIX`. No lists, dicts, sets, or other mutable objects at module scope. No violation.

### (6) Tool results are structured dataclasses not raw exceptions — ✅ PASS
`_parse_stream` returns a plain `dict` with a well-defined shape. Exceptions are caught and handled gracefully (lines 141-153) — the function never raises raw exceptions to callers. While not a dataclass, the return value is structured and this module is a utility, not a tool implementation. No violation.

### (7) Control flow is explicit not magical — ✅ PASS
- `iter_lines` generator in a `for` loop — documented blocking behavior in NOTE comment (lines 59-62)
- `try/except` with specific exception types — explicit error handling
- Brace-balance heuristic (line 132) — explained in comments (lines 127-129)
- No metaclasses, dynamic dispatch, `__getattr__`, `eval`/`exec`, or implicit context managers
- No violation.
