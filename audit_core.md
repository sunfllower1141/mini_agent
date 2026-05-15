# Core Orchestration Layer Audit

**Date**: 2025-07-17  
**Files audited**: `llm.py`, `api.py`, `safety.py`, `config.py`, `prompt.py`, `memory.py`, `mini_agent.py`  
**Methodology**: Full file reads + `find_usages` symbol verification + AST-level unused-import analysis.

---

## 1. Dead Code

### 1.1 Functions/Classes/Variables Never Called

| Symbol | File:Line | Details |
|---|---|---|
| `_inject_token_budget()` | `memory.py:170` | Body is `return` — no-op. Imported by `llm.py:367` and `sub_agent.py:140` and called. Three imports across two files to call a function that does nothing. |
| `_ensure_table()` | `memory.py:824` | Method on `MemoryStore`. Defined, never called anywhere. `__init__` creates tables directly via `_get_conn().execute(_CREATE_TABLE)` instead. |
| `_CONTEXT_BUDGET_INJECT` | `memory.py:66` | Constant (800_000). Only defined, never referenced. Related to the disabled `_inject_token_budget`. |
| `_CONTEXT_PERCENT_CAP` | `memory.py:67` | Constant (100). Only defined, never referenced. |
| `_SUMMARY_MAX_TURNS` | `memory.py:61` | Constant (3). Only defined, never used. `_summarize_pruned()` hardcodes `-3` in slice `turns[-3:]`. |
| `_SUMMARY_MAX_FILES` | `memory.py:62` | Constant (5). Only defined, never used. `_summarize_pruned()` hardcodes `[:5]`. |
| `_SUMMARY_MAX_COMMANDS` | `memory.py:63` | Constant (3). Only defined, never used. `_summarize_pruned()` hardcodes `[:3]`. |
| `_SUMMARY_PREVIEW_LENGTH` | `memory.py:59` | Constant (120). Only defined, never used. `_summarize_pruned()` hardcodes `120` at line ~440. |

### 1.2 Unused Imports

| File | Line | Unused Import |
|---|---|---|
| `llm.py` | 19 | `deque` from `collections` (used as `collections.deque` instead) |
| `llm.py` | 25 | `_request_with_retry` from `retry` (only used in `api.py`) |
| `llm.py` | 26 | `_parse_stream`, `THINKING_START`, `THINKING_END` from `stream` (only used in `api.py`) |
| `llm.py` | 28 | `truncate_content` from `api` (never called in `llm.py`) |
| `llm.py` | 31 | `c`, `DIM` from `terminal` (never referenced in `llm.py`) |
| `llm.py` | 32 | `TOOLS` from `tools` (never referenced in `llm.py`) |
| `llm.py` | 33 | `_total_tokens` from `memory` (never referenced in `llm.py`) |
| `api.py` | 24 | `THINKING_START`, `THINKING_END` from `stream` (imported, never used) |
| `api.py` | 41 | `from tools import ToolResult as TR` inside `format_tool_detail()` (imported, never used) |
| `mini_agent.py` | 42 | `requests` (never referenced directly; session is obtained via `init_session()`) |
| `mini_agent.py` | 44 | `AgentConfig` (returned by `init_session`, never referenced directly) |
| `mini_agent.py` | 47 | `ReadSafetyGate`, `WriteSafetyGate` (never referenced directly) |
| `mini_agent.py` | 48 | `MemoryStore` (never referenced directly) |
| `mini_agent.py` | 50 | `set_context`, `build_symbol_index` (called inside `init_session()`, never directly) |

**Total**: 22 dead imports across 3 files. `llm.py` is the worst offender with 10 unused imports, largely leftover from the `api.py` extraction.

---

## 2. Design Principle Violations

### 2.1 Global Mutable State

| Location | Variable | Risk |
|---|---|---|
| `memory.py:175-176` | `_ACCUM_COUNT`, `_ACCUM_TOTAL` | Module-level mutable globals used by `_total_tokens()`. Not thread-safe for concurrent `MemoryStore` instances. |
| `memory.py:78-79` | `_TOOL_PARSE_CACHE`, `_TOKEN_EST_CACHE` | Module-level dicts keyed by `id(msg)`. Shared across all `MemoryStore` instances. Thread-unsafe. |
| `llm.py:139-140` | `_scratchpad_injected`, `_git_diff_injected` | Module-level flags. Reset in `run_agent_turn()`. If two turns happen concurrently (e.g. TUI background tasks), these will race. |
| `api.py:51` | `_clean_messages_cache` | Module-level dict keyed by `id(messages)`. Shared across all API callers. |

**Impact**: The `_TOOL_PARSE_CACHE` and `_TOKEN_EST_CACHE` module-level dicts mean that two concurrent `MemoryStore.save()` calls could interfere with each other's caches, though in practice the REPL is single-threaded for memory operations. The `_ACCUM_COUNT`/`_ACCUM_TOTAL` pair is particularly fragile — any concurrent call to `_total_tokens` with different message lists will produce wrong results.

### 2.2 Magic Numbers Not Named

| File:Line | Number | Should Be |
|---|---|---|
| `memory.py:440` | `120` (`.replace("\n"," ")` slice) | `_SUMMARY_PREVIEW_LENGTH` (defined at line 59 but unused!) |
| `memory.py:479` | `-3` (`turns[-3:]`) | `_SUMMARY_MAX_TURNS` (defined at line 61, unused) |
| `memory.py:484-485` | `[:5]` (file lists) | `_SUMMARY_MAX_FILES` (defined at line 62, unused) |
| `memory.py:489` | `[:3]` (command list) | `_SUMMARY_MAX_COMMANDS` (defined at line 63, unused) |
| `memory.py:448-449` | `80` (path slice) | Uses `_SUMMARY_PATH_PREVIEW` correctly — good. |

The named constants exist but are **never referenced** in the code that should use them. The values are hardcoded instead. This is a self-contradiction: the module defines the constants but ignores them.

### 2.3 Circular Imports

**No circular imports found.** The extraction of `api.py` from `llm.py` successfully broke the cycle: `llm.py → tools → agent_ops → sub_agent → llm.py` was resolved. Current import graph is acyclic:
```
mini_agent.py → config, llm, prompt, safety, memory, tools, terminal
llm.py → api, config, tools, memory, safety, interject, retry, stream
api.py → config, tools.schema, retry, stream
config.py → (no core imports except memory inside functions)
memory.py → (stdlib only)
safety.py → (stdlib only)
prompt.py → config (TYPE_CHECKING only)
```

### 2.4 Duplicate Logic

| Pattern | Locations | Issue |
|---|---|---|
| Pruning pipeline (`_clean_messages` + `_compress_tool_results` + `_prune_by_tokens` + `_summarize_pruned`) | `config.py:430` (inside `switch_session`), `config.py:490` (inside `init_session`) | Same 4-call pipeline duplicated verbatim across two functions. If you add a fifth step, you must update both. |
| `init_session` and `switch_session` | `config.py:470` and `config.py:414` | Both build messages lists the same way: system prompt + startup context + saved. Consolidate into a shared helper. |

### 2.5 Backward-Compatibility Aliases Without Deprecation Warnings

| File:Line | Alias |
|---|---|
| `safety.py:38` | `ReadSafetyResult = SafetyResult` |
| `safety.py:39` | `WriteSafetyResult = SafetyResult` |

These are used only in `test_safety.py`. No deprecation warning, no `__all__` restriction. The core codebase (`llm.py`, `config.py`, `mini_agent.py`) already uses `SafetyResult` directly. These can be removed or at least deprecated.

---

## 3. Correctness

### 3.1 Error Handling Gaps

| File:Line | Issue |
|---|---|
| `llm.py:170` | `_inject_git_diff` catches generic `Exception` (line 170: `except Exception as exc`). Swallows `MemoryError`, `KeyboardInterrupt`, etc. Should catch `OSError` + `subprocess.SubprocessError` specifically. |
| `llm.py:213` | Same broad `except Exception` in `_inject_orchestration_context`. |
| `llm.py:646` | `_api_call_phase` accesses `msg.pop("_fired_indices", [])` — if `msg` is `None` (cancellation during retry), this raises `AttributeError`. The check `if cancel_event is not None and cancel_event.is_set(): return None` on line 642 happens *before* this, but `call_deepseek` can also return `None` when cancelled. In that case `msg` is `None` and the pop crashes. |
| `llm.py:896` | `run_agent_turn` catches `if 'msg' not in locals()` — this is Python, not JavaScript. If `max_turns` is 0, the for-loop body never runs and `msg` is indeed undefined, but this pattern is fragile (e.g. if the loop body raises an exception after assigning `msg`). Better to initialize `msg = None` before the loop. |
| `api.py:133` | `call_deepseek` returns `None` on cancellation (line 122-123), but the caller `_api_call_phase` at `llm.py:641` does not check for `None` before calling `msg.pop()`. |
| `config.py:437-440` | `switch_session` catches `OSError` but if `os.remove(db_path)` raises `PermissionError`, the session is left in a broken state. |
| `memory.py:745` | `save()` catches `sqlite3.Error` but does not distinguish transient errors (SQLITE_BUSY from WAL contention) from permanent ones. |
| `safety.py:80` | `ReadSafetyGate.check` accepts `path: str | None` but the TOCTOU race comment (lines 83-86) is a known vulnerability — no fix offered. |

### 3.2 Edge Cases Not Handled

| File:Line | Edge Case |
|---|---|
| `llm.py:85` | `_tool_call_key` calls `json.loads(fn["arguments"])` — if `fn["arguments"]` is already a parsed dict (not a string), it raises `TypeError`. The except catches it and falls back, but this could mask malformed inputs. |
| `llm.py:529` | `_execute_groups` — when a cycle is detected in `_build_execution_groups`, the fallback sequential execution returns `[]` (empty list) instead of the actual results. The tool results are silently lost. |
| `llm.py:905-906` | `run_agent_turn` finally block closes `session` if it was created internally, but uses `hasattr(session, "close")` — the `requests` module has no `close()` method, so this is a non-issue, but the check is misleading. Better: `if session is not requests`. |
| `memory.py:147-165` | `_total_tokens` — if messages are pruned and then new messages appended in the same save cycle, the accumulator runs a full recount (correct). But if `_ACCUM_COUNT` is reset externally (e.g. different call site), the accumulator silently produces wrong counts until the next prune. |
| `config.py:264-282` | `_load_dotenv` — the simple parser does not handle multi-line values, escaped characters, or `export` prefix. Standard for a `.env` parser but worth documenting as a limitation. |
| `prompt.py:56` | `build_system_prompt` reads `.mini_agent.rules` at prompt-build time. If the file changes mid-session, the prompt is stale until the next `build_system_prompt` call (which never happens after startup). |

### 3.3 Missing Type Hints on Public APIs

| File:Line | Function |
|---|---|
| `llm.py:781` | `run_agent_turn()` — missing return type annotation (should be `dict | None` but not annotated explicitly) |
| `llm.py:605` | `_execute_tools()` — missing return type |
| `llm.py:318` | `_inject_context()` — no return type |
| `memory.py:900` | `_clean_messages()` — returns `list[dict]` but return type not annotated (only docstring) |
| `memory.py:671` | `MemoryStore.save()` — no return type annotation |
| `memory.py:658` | `MemoryStore.load()` — returns `list[dict]` but not annotated |
| `api.py:31` | `truncate_content()` — has type hints, good |
| `api.py:38` | `format_tool_detail()` — missing return type |

The `_STATIC_PROMPT` string in `prompt.py` says "Add type hints for public functions" — this advice is inconsistently followed.

---

## 4. Architecture Coherence

### 4.1 Layering — Generally Good

```
mini_agent.py  (entry point — REPL loop, user I/O)
     ↓
  llm.py       (turn orchestration — context injection, tool piping, circuit breaker)
     ↓
  api.py       (API transport — call_deepseek, streaming, caching)
     ↓  ↓
tools/   memory.py  (tool implementations, conversation persistence)
     ↓
safety.py     (filesystem guard — read/write boundary checks)
config.py     (configuration — loaded once, consulted everywhere)
prompt.py     (system prompt — built once, modified never)
```

**The layering is clean.** Each layer depends only on the layer below. No upward dependencies.

### 4.2 Abstractions at the Right Level

| Grade | Module | Notes |
|---|---|---|
| ★★★ | `safety.py` | Single responsibility. Clean `SafetyResult` dataclass. Read/write gates are separate classes despite sharing logic — acceptable because the sharing is minimal. |
| ★★★ | `prompt.py` | Single function, one large string constant. Exactly right size. |
| ★★★ | `api.py` | Extracted from `llm.py` for cycle-breaking. Clean. `call_deepseek` is the only public API. |
| ★★☆ | `config.py` | Grown too large (~500 lines). `init_session` does too much: config loading, memory init, safety gates, MCP startup, symbol index, session management. Should be split. The `build_startup_context` function (70+ lines) has its own TODO to split. |
| ★★☆ | `memory.py` | ~1000 lines. Compression, pruning, summarization, persistence, export are all in one file. The TODO comments at lines ~556, ~667, ~nan acknowledge this. Worth splitting into `memory_store.py` + `memory_compression.py` + `memory_pruning.py`. |
| ★★☆ | `llm.py` | ~950 lines. Turn orchestration, context injection (8 helpers), tool piping (Kahn's algorithm), circuit breaker, turn summaries. Many concerns in one file. The `_inject_*` helpers could move to a separate `context.py`. |
| ★★☆ | `mini_agent.py` | ~200 lines. REPL loop, exported conversation, session commands. Good size but imports 6 things it doesn't use directly. |

### 4.3 Layers That Do Too Much

1. **`config.py` — `init_session()` is a God function** (lines 455-520). It creates config, both safety gates, MemoryStore, AgentRuntime, builds symbol index, starts MCP servers, creates HTTP session, prunes saved messages, and returns a 6-key dict. This is infrastructure assembly, not configuration. Move to a `bootstrap.py` or split into smaller factories.

2. **`MemoryStore` has grown beyond a store** (memory.py:559-830). It now handles compression, pruning, summarization, token estimation, migration, and scratchpad persistence. The store should just save/load; the memory management should be separate composable functions (which they mostly are — they're just in the same file).

### 4.4 Positive Architectural Decisions

- **`api.py` extraction** successfully broke a real circular dependency (`llm → tools → agent_ops → sub_agent → llm`). Well-executed.
- **Named constants** are well-organized at the top of each module. The system has extract-magic-numbers discipline.
- **`_TOOL_CONTEXT` (AgentContext dataclass)** replaced a plain mutable dict. Good upgrade for type safety.
- **Token accumulator optimization** (`_ACCUM_COUNT`/`_ACCUM_TOTAL`) is a smart performance hack documented with its tradeoffs.
- **Incremental message cleaning cache** (`_clean_messages_cache`) in `api.py` avoids O(n) deep-copy on every API call. Good optimization.

---

## 5. Summary of Findings

| Category | Count | Severity |
|---|---|---|
| Dead code (never-called functions) | 2 | Low |
| Dead constants (defined, never used) | 6 | Low |
| Unused imports | 22 | Low |
| Global mutable state risks | 4 | Medium |
| Magic numbers with unused named constants | 5 | Low |
| Duplicate logic | 2 | Medium |
| Error handling gaps | 8 | Medium-High |
| Missing type hints | 6 | Low |
| God functions / oversized modules | 3 | Medium |

### Quick Wins (low effort, high impact)

1. **Delete `_inject_token_budget`** (memory.py:170) and its two call sites (llm.py:367-368, sub_agent.py:140,162). Save 3 imports.
2. **Use named constants** in `_summarize_pruned` instead of hardcoded `120`, `-3`, `[:5]`, `[:3]`. The constants are already defined — just use them.
3. **Remove unused imports** from `llm.py` (10 dead imports), `api.py` (3 dead imports), `mini_agent.py` (7 dead imports). 20 imports gone.
4. **Delete `_ensure_table`** (memory.py:824) and the dead constants (`_CONTEXT_BUDGET_INJECT`, `_CONTEXT_PERCENT_CAP`).
5. **Add `msg is None` check** after `call_deepseek` in `_api_call_phase` (llm.py:641) to prevent `AttributeError` on cancellation.
6. **Initialize `msg = None`** before the for-loop in `run_agent_turn` (llm.py:860) instead of the `'msg' not in locals()` hack.

### Medium Effort

7. **Extract pruning pipeline** from `init_session` and `switch_session` into a shared helper (`_load_and_prune_messages`) in config.py.
8. **Split `init_session`** — move MCP startup, HTTP session setup, and symbol index building into separate bootstrap functions.
9. **Deprecate `ReadSafetyResult`/`WriteSafetyResult`** aliases with a `DeprecationWarning` or remove them and update test_safety.py to use `SafetyResult`.
