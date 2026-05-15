# IO Layer Audit Report

**Files audited:** `electron_bridge.py`, `stream.py`, `retry.py`, `interject.py`  
**Date:** 2025-07-16

---

## electron_bridge.py (140 lines)

| # | Severity | Line(s) | Finding |
|---|----------|---------|---------|
| 1 | **CRITICAL** | 136 | `handler(id_, params)` — no try/except around handler dispatch. Any unhandled exception in `_handle_chat` / `_handle_init` kills the bridge process entirely. Wrap in try/except and emit an error response. |
| 2 | **HIGH** | 116–136 | Main loop blocks on `sys.stdin` with no timeout/keepalive. If the frontend process dies without closing stdin, the bridge hangs forever. Add `select.select()` with a read timeout or heartbeat. |
| 3 | **HIGH** | 1–140 | No signal handling (SIGTERM/SIGINT). The bridge can't flush pending output or clean up on shutdown. |
| 4 | **MEDIUM** | 43 | `import os as _os` inside `_handle_init` — unnecessary deferred import. Move to top-level. |
| 5 | **MEDIUM** | 85–87 | `except Exception as exc: _error(id_, -4, f"turn error: {exc}")` — swallows traceback. Consider logging the full traceback to stderr. |
| 6 | **LOW** | 89 | `if result is None` treated as cancellation, but `run_agent_turn` may return None for other edge cases. Add a sentinel or explicit cancelled flag. |
| 7 | **LOW** | 72–73 | `on_token` closure doesn't check `_cancel_event` — token streaming continues even after cancel is set until `run_agent_turn` returns. |

---

## stream.py (171 lines)

| # | Severity | Line(s) | Finding |
|---|----------|---------|---------|
| 1 | **HIGH** | 58 | `response.iter_lines(decode_unicode=True)` — no timeout parameter. If the server stalls mid-stream (TCP connection open but no data), this blocks indefinitely. `requests` supports `timeout` on the session but `iter_lines` doesn't enforce per-line deadline. Use `urllib3`-level socket timeout or wrap in a timeout thread. |
| 2 | **MEDIUM** | 127–128 | Brace-balance heuristic (`args.count("{") == args.count("}")`) for detecting complete JSON tool arguments — false positive when balanced braces appear inside string values (e.g. `{"query": "a{b}c"}`). This fires `on_tool_ready` with malformed args; the JSON parse will catch it, but the `fired_indices` set prevents a retry once the real complete chunk arrives. |
| 3 | **MEDIUM** | 136–138 | Inner `except` catches 6 exception types (`JSONDecodeError, KeyError, TypeError, IndexError, ValueError, AttributeError`) and silently continues. A persistently malformed chunk loops forever with no error counter or bail-out. Add a consecutive-error limit or at minimum log the offending `data_str`. |
| 4 | **LOW** | 150–154 | Newline handling is conditional on `not on_token`. When `on_token` is supplied, callers (electron_bridge, TUI) get no trailing newlines — they must handle formatting themselves. Document this contract explicitly. |
| 5 | **LOW** | 49, 163–166 | Partial tool calls from a mid-stream disconnect are included in `tool_calls` even when arguments are incomplete JSON. Caller gets a tool call with fragmentary args and no way to know it's incomplete (no `_fired_indices` entry). |

---

## retry.py (89 lines)

| # | Severity | Line(s) | Finding |
|---|----------|---------|---------|
| 1 | **MEDIUM** | 48 | `post = session.post if hasattr(session, "post") and callable(session.post) else requests.post` — fragile detection of the session interface. A mock with a non-callable `post` attribute or a proxy will silently fall through to `requests.post`. Use an explicit protocol or `isinstance` check against `requests.Session`. |
| 2 | **MEDIUM** | 33, 55 | `stream=True` passed through to `requests.post()`. When streaming, retrying re-sends the request body but the original stream body may have been partially consumed — this is fine for our use case (same JSON payload each time) but the function has no guard against mutable body objects. |
| 3 | **LOW** | 87–88 | Dead code: `if last_exc is not None: raise last_exc` is unreachable when `_MAX_RETRIES=3` (the loop always returns or raises before falling through). Reachable only if `_MAX_RETRIES=0`. |
| 4 | **LOW** | 21 | `_RETRYABLE_STATUSES` missing `408` (Request Timeout) — standard for transient retry. |
| 5 | **LOW** | 27 | `random.random()` — no seed control for deterministic tests. Tests don't appear to mock this. Minor. |

### Inconsistent retry/backoff patterns

- `retry.py` uses jittered exponential backoff: `2^attempt * (0.5 + random)` (~0.5–1.5s, ~1–3s, ~2–6s). 
- `stream.py` has **zero retry** — connection drops are handled by returning partial results.
- `electron_bridge.py` has **zero retry** for RPC dispatch failures.
- **Verdict:** No cross-module inconsistency because each layer has different concerns. `retry.py` handles HTTP-level; `stream.py` handles parsing resilience (accumulate-what-you-can); `electron_bridge.py` is a stateless RPC bridge. **However**, `electron_bridge.py` should at minimum survive handler crashes (finding #1 above).

---

## interject.py (35 lines)

| # | Severity | Line(s) | Finding |
|---|----------|---------|---------|
| 1 | **LOW** | 12–13 | Module-level global `_INTERJECTIONS`/`_LOCK` — singleton pattern. Multiple `AgentWorker` instances in the same process (theoretical) would share this queue. Acceptable for current single-session TUI, but document the limitation. |
| 2 | **LOW** | 19 | `push_interjection` has no max queue bound. A user spamming input could grow memory unboundedly. Add `maxlen` to the deque or drop oldest. |

### Thread safety assessment

**All three functions correctly use `threading.Lock`.**  
- `push_interjection`: lock → append → unlock.  
- `poll_interjections`: lock → copy → clear → unlock → return.  
- `has_interjections`: lock → check length → unlock.  

`poll_interjections` drains atomically (copy then clear under one lock). The lock is a plain `threading.Lock` (not RLock), which is correct since none of the functions recurse into each other. No deadlock risk. **No thread-safety issues found.**

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH     | 3 |
| MEDIUM   | 7 |
| LOW      | 13 |
| **Total** | **24** |

### Top 3 action items

1. **[CRITICAL] electron_bridge.py:136** — Wrap handler dispatch in try/except so a single bad handler doesn't kill the bridge.
2. **[HIGH] stream.py:58** — Add timeout to `iter_lines` or use socket-level timeout to prevent indefinite hang on stalled server.
3. **[HIGH] electron_bridge.py:116** — Add `select.select()` or signal-based stdin monitoring so a dead frontend doesn't leave the bridge process orphaned.
