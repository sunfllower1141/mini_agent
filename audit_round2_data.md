# Audit Report: memory.py + safety.py

Generated: analysis of type hints, magic numbers, non-structured returns, and global mutable state.

---

## File: memory.py (≈1065 lines)

### 1. Missing Type Hints

**None found.** Every public and private function in memory.py has full type annotations on all parameters and return types. This includes:

- All module-level functions (`_get_tool_content`, `_estimate_tokens`, `_compress_*`, `_prune_by_tokens`, `_clean_messages`, `export_conversation_markdown`, etc.)
- All `MemoryStore` methods (`__init__`, `load`, `save`, `clear`, `get_scratchpad`, `set_scratchpad`, `add_knowledge`, `get_top_knowledge`, etc.)
- All private helpers (`_db_path`, `_row_to_msg`, `_migrate_*`, `_is_match_line`)

### 2. Magic Numbers

Several bare numeric literals leak into logic instead of using the module-level named constants.

| Line / Context | Bare Number | Suggested Fix |
|---|---|---|
| `_compress_run_shell`: `for line in lines[:3]` | `3` (scan first 3 lines for status markers) | Extract as `_SHELL_HEAD_SCAN_LINES = 3` |
| `_compress_run_shell`: `tail = lines[-20:]` (×2 uses) | `20` (tail window for shell output) | Extract as `_SHELL_TAIL_LINES = 20` |
| `_compress_run_tests`: `if len(lines) <= _COMPRESSION_MAX_LINES * 4` | `4` (multiplier on max lines threshold) | Extract as `_TEST_LINE_THRESHOLD_MULT = 4` |
| `_compress_run_tests`: `kept_indices = [0, 1, 2, len(lines) - 3, len(lines) - 2, len(lines) - 1]` | `0, 1, 2, -3, -2, -1` (hardcoded indices) | No clean constant — could use slice constants `_HEAD_KEEP=3`, `_TAIL_KEEP=3` |
| `save()` call: `_compress_tool_results(cleaned, keep_recent=6)` | `6` instead of `_COMPRESSION_KEEP_RECENT` | Replace with `keep_recent=_COMPRESSION_KEEP_RECENT` |
| `_summarize_pruned`: `content[:120]` | `120` instead of `_SUMMARY_PREVIEW_LENGTH` | Replace with `_SUMMARY_PREVIEW_LENGTH` |
| `capture_session_summary(..., importance=3)` | `3` (hardcoded importance value) | Extract as `_SESSION_SUMMARY_IMPORTANCE = 3` |

**Severity:** Low — named constants already exist for most values; the issue is inconsistent usage.

### 3. Functions Returning Non-Structured Types

The project convention states "All tool results must be structured dataclasses — never raw exceptions." However, `MemoryStore` is not a tool — it's internal persistence. Still, multiple public methods return raw `dict` / `list[dict]` rather than typed dataclasses:

| Method | Return Type | Notes |
|---|---|---|
| `load()` | `list[dict]` | Raw message dicts — caller must know structure |
| `get_top_knowledge()` | `list[dict]` | Returns raw dicts with string keys |
| `get_latest_session_summary()` | `dict \| None` | Returns raw dict or None |
| `save()` | `list[dict]` | Returns raw list of message dicts |

Also, several private helpers return `dict` rather than a typed NamedTuple or dataclass:
- `_find_tool_call_args()` → `dict`
- `_row_to_msg()` → `dict`

**Severity:** Medium — not technically violating the tool-result rule (these aren't tool handlers), but the fuzzy typing makes the API harder to use correctly. Could introduce `MessageDict = dict` alias or dataclass wrapper.

### 4. Global Mutable State

Four module-level mutable variables are declared with no thread-safety guards:

| Variable | Type | Purpose |
|---|---|---|
| `_TOOL_PARSE_CACHE` | `dict[int, str]` | Per-save cache — cleared at start of `save()` |
| `_TOKEN_EST_CACHE` | `dict[int, int]` | Per-save cache — cleared at start of `save()` |
| `_ACCUM_COUNT` | `int` | Running accumulator for token estimation |
| `_ACCUM_TOTAL` | `int` | Running accumulator for token estimation |

Both caches (`_TOOL_PARSE_CACHE`, `_TOKEN_EST_CACHE`) are reset per `save()` call, so they don't leak between calls. However, `_ACCUM_COUNT` / `_ACCUM_TOTAL` persist across calls to `_total_tokens` and could drift if `save()` interleaves with other operations. No threading locks are used.

**Severity:** Low-Medium — the caches are scoped to a single `save()` call, but the accumulators are a subtle state leak across calls.

### 5. Other Observations

- `_ensure_table()` duplicates the `CREATE TABLE` logic already in `__init__()` for `scratchpad` and `test_output` tables.
- `set_scratchpad()` and `get_scratchpad()` also duplicate the `CREATE TABLE IF NOT EXISTS` logic unnecessarily.
- `_summarize_pruned` has a TODO noting it's ~80 lines and should be split.
- `_prune_by_tokens` has a TODO noting it's ~50 lines and should be split.
- `save()` has a TODO noting it's ~50 lines and should be split.

---

## File: safety.py (≈178 lines)

### 1. Missing Type Hints

**None found.** All class methods, properties, and module-level functions have complete type annotations:

- `_is_within_workspace(resolved: str, root: str, root_prefix: str) -> bool`
- `ReadSafetyGate.__init__`, `.workspace_root`, `.unrestricted`, `.check`
- `WriteSafetyGate.__init__`, `.workspace_root`, `.unrestricted`, `.check`, `.generate_diff`, `.approve`, `._format_new_file`, `._format_diff`
- `SafetyResult` (dataclass with typed fields)
- `DiffPreview` (dataclass with typed fields)

### 2. Magic Numbers

**None found.** All ANSI escape codes are named constants (`_GREEN`, `_RED`, `_CYAN`, `_RESET`, `_BOLD`). No bare numeric literals appear in logic.

### 3. Functions Returning Non-Structured Types

- `approve()` returns `str` — but this is explicitly documented as a backward-compatible adapter that delegates to `generate_diff()` → `DiffPreview`. Acceptable legacy wrapper.
- `_format_new_file()` and `_format_diff()` return `str` — private helpers, acceptable.
- `ReadSafetyResult` / `WriteSafetyResult` are deprecated aliases for `SafetyResult`. Fine.

All public-facing API (`check`, `generate_diff`) returns structured dataclasses (`SafetyResult`, `DiffPreview`).

### 4. Global Mutable State

**None found.** All state is instance-level (`self._root`, `self._root_prefix`, etc.). The module has no module-level mutable variables.

### 5. Other Observations

- Clean, well-structured module. No issues found.

---

## Summary

| Category | memory.py | safety.py |
|---|---|---|
| Missing type hints | ✅ None | ✅ None |
| Magic numbers | ⚠️ 7 instances (all low severity) | ✅ None |
| Non-structured returns | ⚠️ 4 public methods return raw `dict`/`list[dict]` | ✅ Structured dataclasses |
| Global mutable state | ⚠️ 4 module-level variables (caches + accumulators) | ✅ None |

**memory.py** is well-typed but has minor magic-number inconsistency (using bare literals where named constants exist), a few public methods returning raw dicts, and subtle global state in `_ACCUM_COUNT`/`_ACCUM_TOTAL`.

**safety.py** is clean across all four categories with no issues.
