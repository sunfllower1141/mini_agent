# Orchestration Code Review

Files reviewed: `llm.py`, `api.py`, `stream.py`, `memory.py`.

---

## 1. Function Length

| Function | Lines | File |
|---|---|---|
| `run_agent_turn` | ~130 | llm.py:608–738 |
| `_parse_stream` | ~95 | stream.py:34–129 |
| `_summarize_pruned` | ~80 | memory.py:~430–510 |
| `save()` | ~60 | memory.py:~640–700 |
| `_compress_tool_results` | ~60 | memory.py:~230–290 |

`run_agent_turn` is ~130 lines (not 400 — but with all extracted helpers the orchestration surface is large). It's well-factored into phases. Flagging per instruction — no rewrite.

`_summarize_pruned` has its own TODO comment noting it should be split by message role.

---

## 2. Mutable Defaults & Global State

- **`_TOOL_CONTEXT`** (llm.py) — module-level mutable singleton mutated across turns. Used for `_turn_history`, `_plan_steps`, `_scratchpad_updated`. Shadow state hard to trace.
- **`_clean_messages_cache`** (api.py:31) — keyed by `id(messages)`. Identity collision risk if GC reuses address. Cache is intentionally NOT cleared per turn (see llm.py `run_agent_turn` comment ~line 651), meaning stale entries if the messages list is rebuilt externally.
- **`_ACCUM_COUNT` / `_ACCUM_TOTAL`** (memory.py:88–89) — module-level running token accumulators. Not thread-safe. If `save()` is called concurrently (unlikely but architecturally risky), counts corrupt.
- **`_scratchpad_injected` / `_git_diff_injected`** (llm.py:85–86) — module-level flags. If `run_agent_turn` is called recursively (sub-agents calling LLM), flags stomp across calls. Already reset at top of `run_agent_turn`.

---

## 3. Repeated Code Patterns

- **Callback-passing chain** — `on_tool_start`, `on_tool_end`, `on_tool_output` threaded through 7+ functions (`run_agent_turn` → `_api_call_phase` → `_tool_execution_phase` → `_execute_tools` → `_execute_groups` → `_execute_single_no_pipe` / `_execute_parallel_no_pipes`). A single `Callbacks` dataclass would cut boilerplate.
- **`_find_tool_call_name`** and **`_find_tool_call_args`** (memory.py:97–131) — identical backward-walk logic, differ only in return value. Should merge into one function returning both.
- **`get_scratchpad()` / `set_scratchpad()` / `get_test_output()` / `save_test_output()`** (memory.py) — all repeat `CREATE TABLE IF NOT EXISTS` + `INSERT OR IGNORE` boilerplate. A `_ensure_table(tablename, schema)` helper would DRY this.
- **`execute_tool` call sites** — same 3–4 kwargs (`on_output`, `approve_callback`, `write_gate`, `read_gate`) repeated across 5 functions in llm.py.

---

## 4. Stale/Incorrect Comments

- **llm.py ~line 650**: `"# One-time cleanup / cache invalidation"` followed by `"# Note: clear_api_cache is intentionally NOT called here"`. The comment contradicts the heading.
- **memory.py docstring (~line 18)**: `"Keep only the first line for results more than N messages ago"` — the actual code splits smarter per tool type (`read_file`, `search_files`, `run_shell`), not just "first line". Docstring is stale.
- **api.py docstring ~line 37**: Says `DeepSeek thinking mode requires reasoning_content` — it's the API that returns this field, not a DeepSeek-specific requirement (it's an OpenAI-compatible field).
- **llm.py ~line 52**: Bare section heading `# API call` with no code underneath it — leftover from an extraction.

---

## 5. Deep Nesting (>3 levels)

- **`_parse_stream`** (stream.py:50–129): `try` → `for line in iter_lines` → inner `try` → `if/elif` on `delta` keys → `for tc_delta in tool_calls` → `if on_tool_ready` → nested `try/except`. **4–5 levels**. Could flatten with early continues.
- **`_execute_groups`** (llm.py:~530–580): `for group` → `if len==1` (else branch) → `ThreadPoolExecutor` → `for future` → `if cancel` → `_append_tool_result`. **5 levels**.
- **`save()`** (memory.py:~640–700): `if need_full_rewrite` / `else` → `if new_msgs` → `conn.executemany` → `try/except` → `conn.rollback`. **4 levels** plus nested try.
- **`_compress_tool_results`** (memory.py:~140–195): `for i, m` → `if role != tool` → `try` re-parse → `if tool_name` branching → `if kept == text` → continue. **5 levels**.

---

## 6. Exception Handling — Re-raise vs Swallow

| Location | Pattern | Assessment |
|---|---|---|
| `_inject_git_diff` (llm.py:124) | `except Exception: print(...)` | ⚠️ Silent swallow, no traceback |
| `_inject_orchestration_context` (llm.py~147) | `except Exception: print(...)` | ⚠️ Silent swallow |
| `MemoryStore.__init__` (memory.py:577) | `except sqlite3.Error: pass` | 🔴 Absolute silent — DB init failures invisible |
| `MemoryStore.get_scratchpad` (memory.py~690) | `except sqlite3.Error: return ""` | ⚠️ Silent, indistinguishable from "no scratchpad" |
| `_migrate_json` (memory.py:950) | `except (JSONDecodeError, OSError): return` | ⚠️ Silent |
| `call_deepseek` (api.py:104) | `print(err)` then `r.raise_for_status()` | ✅ Prints then re-raises |
| `_parse_stream` (stream.py:112–117) | catches stream errors, returns partial | ✅ Intentional resilience |
| `MemoryStore.save` (memory.py~688) | `except sqlite3.Error: print(...)` then rollback | ⚠️ Rollback in except, not finally |

**Pattern**: most `sqlite3` errors are silently swallowed across `memory.py` — makes debugging corruption or schema drift very hard.

---

## 7. Naming Issues

| Name | File/Line | Problem |
|---|---|---|
| `_TOOL_CONTEXT` | llm.py:10 | UPPER_CASE suggests constant but is mutable singleton |
| `_original_session` | llm.py:735 | Unclear if "original" means "given by caller" vs "created by us" |
| `_clean_message` vs `_clean_messages` | api.py:45 vs memory.py:780 | Singular strips internal fields for API; plural strips system/transient/incomplete rows. Different concerns, near-identical names. |
| `_accumulate_usage` | llm.py:493 | Name suggests mutation but returns new dict; `_merge_usage` clearer |
| `_inject_scratchpad_nudge` vs `_inject_scratchpad_context` | llm.py:195, 90 | One injects content, the other nudge to update — names too similar |
| `_build_compressed` | memory.py:405 | "build compressed" is vague — could mean compress or decompress |

---

## Summary

**Strengths**: Good extraction of injection helpers, separate phases for API call vs tool execution, clean module boundaries. Circuit breaker and pipe dependency resolution are well-encapsulated.

**Top 3 actionable fixes**:
1. **Wrap the callback chain** → single `Callbacks` dataclass
2. **Kill silent swallows** → at minimum `from w import warn` in memory.py's `except sqlite3.Error` blocks
3. **Deduplicate `_find_tool_call_name` / `_find_tool_call_args`** → single function returning `(name, args)`
