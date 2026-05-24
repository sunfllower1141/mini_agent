# mini_agent Code Audit Report

## Severity Legend
- 🔴 **CRITICAL** — Runtime crash, data loss, or silent corruption
- 🟠 **HIGH** — Bug with observable incorrect behavior
- 🟡 **MEDIUM** — Code smell, performance issue, or maintainability hazard
- 🟢 **LOW** — Minor improvement, style, or future-proofing

---

## 🔴 CRITICAL

### C1. `edit_file` — NameError on file reservation check
**File:** `tools/file_ops.py`, line ~402
```python
from tools import _FILE_RESERVATIONS_LOCK
with _FILE_RESERVATIONS_LOCK:
    existing = _FILE_RESERVATIONS.get(path)  # NameError!
```
`_FILE_RESERVATIONS` is never imported in this scope. The `write_file` path (line 197) correctly does `from tools import _FILE_RESERVATIONS, _FILE_RESERVATIONS_LOCK`, but the `edit_file` path only imports the lock. This will raise `NameError` at runtime whenever a sub-agent tries to edit a reserved file.
**Fix:** Add `_FILE_RESERVATIONS` to the import on line 402.

---

## 🟠 HIGH

### H1. File reservation TOCTOU race
**File:** `tools/file_ops.py`, lines 197-203 and ~400-408
Both `write_file` and `edit_file` check the reservation under the lock, release the lock, then proceed to write. Between the check and the write, another thread could reserve the same file. The reservation is advisory — the actual `open(..., "w")` call is not protected.
**Fix:** Either hold the lock through the write (not practical — writes can be slow) or use an atomic check-and-reserve pattern: `reserve_file(path, agent_id)` returns (ok, msg) and the caller aborts if False.

### H2. `_ACCUM_COUNT` / `_ACCUM_TOTAL` global mutable state
**File:** `memory.py`, lines ~136-142
```python
_ACCUM_COUNT: int = 0
_ACCUM_TOTAL: int = 0
_ACCUM_LIST_ID: int = 0
```
These module-level globals are mutated by `_total_tokens()` which runs in both the parent agent thread and sub-agent threads. The `_ACCUM_LOCK` protects the mutation, but the accumulated total is **per-list-identity**, and different message lists (parent vs sub-agent) will corrupt each other's counts if their `id()` values collide or if a context switch happens mid-accumulation.
**Fix:** Make these thread-local or pass the accumulator as a parameter rather than using module globals.

### H3. `record_success` called unconditionally on every tool success
**File:** `tools/__init__.py`, line ~927
```python
if not result.success:
    _learn_from_failure(name, result)
else:
    pattern_store.record_success(name, args)
```
`record_success` queries the DB, computes arg similarity across up to 10 patterns, and updates SQLite — for **every** successful tool call regardless of whether that tool/arg combination ever failed before. This adds O(n) DB overhead to every read_file, search_files, etc.
**Fix:** Skip `record_success` unless the tool name has a non-zero in-memory failure counter (check `_TOOL_CONTEXT.__dict__.get("_failure_patterns", {}).get(f"{name}:*")` first).

### H4. `_remember` creates a second MemoryStore connection
**File:** `tools/__init__.py`, lines ~335-355
When `_TOOL_CONTEXT._memory_store` is None, `_remember` creates a fallback `MemoryStore(db_path)` and calls `close()` on it. This opens a second SQLite connection to the same WAL database. Under concurrent sub-agent access, this can cause "database is locked" errors.
**Fix:** Always set `_memory_store` during session init (it already is in bootstrap.py). Remove the fallback or make it a warning-only path.

---

## 🟡 MEDIUM

### M1. `_check_destructive` is dead code
**File:** `tools/shell_ops.py`
```python
def _check_destructive(command: str) -> str | None:
    """Safety guards removed — all commands always allowed."""
    return None
```
The function always returns None, but `_run_shell` still calls it and checks the result. The entire `not force` code path is dead. The `_DESTRUCTIVE_PATTERNS` list still exists but is never used.
**Fix:** Remove `_check_destructive`, its call site, and `_DESTRUCTIVE_PATTERNS`, or reinstate the safety check.

### M2. `_FILE_CACHE` unbounded growth
**File:** `tools/file_ops.py`, line ~28
The cross-turn file content cache (`_FILE_CACHE: dict[str, tuple[str, float]]`) has no eviction policy. In long sessions reading many files, this dict grows without bound. Each entry holds the full file content.
**Fix:** Add an LRU eviction policy (e.g., `functools.lru_cache` or a manual cap of ~50 entries).

### M3. `_inject_strategy_hint` uses hardcoded `messages.insert(1, ...)`
**File:** `llm.py`, line ~429
```python
messages.insert(1, {"role": "system", "content": hint})
```
This assumes index 1 is always safe. If messages has only one element (system prompt during streaming or edge case), this will insert at the wrong position or fail silently. Also, the dedup check `any(m["role"] == "system" and m["content"] == hint for m in messages)` is O(n) per injection.
**Fix:** Insert after the last system message, or use a set of injected hints for O(1) dedup.

### M4. `_TOOL_CONTEXT` shared state leaks across sub-agents
**File:** `tools/__init__.py`, `sub_agent.py`
The `AgentContext` singleton is shared across all threads. Sub-agents save/restore `_plan_steps` and `_plan_done`, but other attributes like `_scratchpad_updated`, `_turn_history`, and `_failure_patterns` are not isolated. A sub-agent's tool failures leak into the parent's in-memory failure counters.
**Fix:** Use `contextvars.ContextVar` or pass a per-agent context object explicitly.

### M5. `_AGENT_TODOS` is in-memory only, no persistence
**File:** `tools/agent_ops.py`
The todo list is a global list with no disk persistence. Session restart loses all todos. This contradicts the project's emphasis on cross-session persistence.
**Fix:** Persist todos to SQLite (same DB as scratchpad) or document that they're ephemeral.

### M6. `_repair_json` complexity
**File:** `tools/__init__.py`, lines ~430-500
The JSON repair function tries 7 different fix combinations. While thorough, it's complex (nested string-walking, regex on segments). Edge cases with escaped quotes inside strings could produce incorrect repairs.
**Suggestion:** Consider using `json5` or a dedicated JSON repair library, or add property-based tests.

### M7. Double `_inject_failure_pattern_warnings` call
**File:** `llm.py`
`_inject_failure_pattern_warnings` is called inside `_inject_context` (pre-API), and `_inject_failure_pattern_warnings_for` is called inside `run_agent_turn` (between API and execution). Both scan for the last assistant message with tool_calls and inject warnings. If the API call returns the same tool_calls as the previous turn, the same warnings are injected twice — once before the API call and once after.
**Fix:** Deduplicate by tracking which tool_call_ids have already been warned about per turn.

### M8. Per-turn `ThreadPoolExecutor` in `execute_tool`
**File:** `tools/__init__.py`, line ~878
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(dispatch, ...)
    result = future.result(timeout=_TOOL_TIMEOUT)
```
Creates and destroys a thread pool for every single tool call just to get a timeout. This adds ~0.1-0.5ms overhead per call.
**Suggestion:** Use `threading.Thread` + `join(timeout)` directly, or keep a reusable single-thread executor.

---

## 🟢 LOW

### L1. `_safe_resolve` OSError catch is too broad
**File:** `safety.py`, line ~29
Catches all `OSError` and falls back to `abspath`. On some platforms, `realpath` can raise other errors that should propagate (e.g., permission errors on parent directories, which indicate a real problem).

### L2. Missing `__all__` in most modules
Most modules have no `__all__`, making `from module import *` unpredictable. Not a runtime issue but poor practice for a package.

### L3. `_TOOL_CONTEXT.__dict__.setdefault("_failure_patterns", {})` — fragile attribute access
Uses `__dict__` bypass rather than `hasattr`/`getattr`. If `AgentContext` ever defines `__slots__` or a property named `_failure_patterns`, this will break silently.

### L4. `_inject_system_reminder` uses function attribute for state
**File:** `llm.py`, line ~380
```python
_inject_system_reminder._last_msg_count = 0
```
Function attributes as mutable state are fragile (not thread-safe, not reset between sessions). A closure or module-level variable with explicit reset would be cleaner.

### L5. `_SKIP_REF_NAMES` — large frozenset recreated on every `build_symbol_index` call
**File:** `tools/search_ops.py`, line ~89
The frozenset is defined inside the function, so it's recreated on every call. Move to module level.

### L6. Hardcoded `"reports/"` directory in sub_agent.py
The report directory is hardcoded as `"reports"` rather than being configurable or relative to the workspace.

---

## Summary

| Severity | Count | Key Areas |
|----------|-------|-----------|
| 🔴 CRITICAL | 1 | NameError in edit_file reservation check |
| 🟠 HIGH | 4 | TOCTOU race, global state corruption, unnecessary DB writes, duplicate connections |
| 🟡 MEDIUM | 8 | Dead code, unbounded caches, shared state leaks, double-injection, JSON repair complexity |
| 🟢 LOW | 6 | Broad exception handling, style, missing exports, minor perf |

### Recommended Fix Priority
1. **C1** — Immediate: `NameError` will crash sub-agent file edits
2. **H3** — Remove unconditional `record_success` DB writes (performance regression on every tool call)
3. **H2** — Fix global accumulator state (silent corruption risk under concurrent sub-agents)
4. **H1** — Fix reservation TOCTOU (actual collision risk is low but real)
5. **M1** — Remove dead `_check_destructive` code (cleanup)
6. **M2** — Add cache eviction to `_FILE_CACHE`
