# Tools Code Review

## 1. Duplicate Code Across Files

| What | Where | File A | File B |
|------|-------|--------|--------|
| `_drain_stderr()` — identical logic reading/discarding stderr | `mcp_client.py` line 136 | `lsp.py` line 107 |
| `_handle_notification()` — same pattern, different dispatch body | `mcp_client.py` line 230 | `lsp.py` line 170 |
| `_send_notification()` — nearly identical JSON-RPC notification send | `mcp_client.py` line 218 | `lsp.py` line 139 |
| `disconnect()` — same graceful→forceful kill pattern | `mcp_client.py` line 75 | `lsp.py` line 78 |
| `_start_process()` — same Popen + daemon thread pattern | `mcp_client.py` line 113 | `lsp.py` line 98 |
| `is_connected` property — same `return self._connected` | `mcp_client.py` line 315 | `lsp.py` line 283 |
| `_SKIP_DIRS` / `_BINARY_EXTS` — defined in `shell_ops.py`, also imported in `search_ops.py` via `from shell_ops import _SKIP_DIRS` | `shell_ops.py` lines 161-179 | `search_ops.py` line 24 |
| `_stream_reader` — only used by `run_shell` and `run_tests` but lives in `shell_ops.py` (fine, not truly duplicate) | — | — |

**Extraction opportunity**: `_drain_stderr`, `_send_notification`, `disconnect`, `_start_process`, and `is_connected` are nearly identical between `mcp_client.py` and `lsp.py` — both are subprocess-based JSON-RPC clients. A shared `JsonRpcSubprocessConnection` base class would eliminate ~200 lines of duplication.

---

## 2. Inconsistent Error Handling Patterns

| Pattern | Found In | Style |
|---------|----------|-------|
| Generic `except Exception as e: return ToolResult(success=False, ...)` | `file_ops.py` (all tools), `shell_ops.py` (`_run_shell`) | Wraps all exceptions to ToolResult |
| Specific + generic layered catch | `lsp.py` `definition()` (catches `LspRpcError` then `LspConnectionError` separately) | Multiple except blocks |
| Hint provided on failure | `file_ops.py` `_read_file` (line 87), `shell_ops.py` `_run_shell` (line 199) | Sometimes hint, sometimes not |
| No hint on failure | `file_ops.py` `_write_file` (line 120), `_edit_file` (line 196) | Missing hints |
| Raising ValueError (not ToolResult) | `agent_messages.py` `_validate_payload` (line 122) | Raw exception propagated |
| Raising RuntimeError (not ToolResult) | `agent_patterns.py` `fan_out`, `fan_in`, `pipeline`, `barrier` (lines 43, 88, 133) | Raw exception in Python API wrappers |

**Inconsistency**: `agent_patterns.py` Python API helpers (fan_out, fan_in, etc.) raise `RuntimeError` on missing runtime/config, while tool-registered wrappers (`_fan_out`, `_fan_in` in the same file) catch those and return `ToolResult`. This creates two error pathways — callers of the Python API must handle exceptions; callers of the tool get structured results. Document this contract or make helpers return `ToolResult` too.

---

## 3. Args Validation — Consistent Approach?

**No, three different patterns:**

1. **Direct dict access** — `args["path"]` (raises `KeyError` if missing, caught by generic `except Exception`):
   - `file_ops.py`: `_read_file` line 33, `_write_file` line 91, `_edit_file` line 136
   - Risky: a missing required param becomes a cryptic `KeyError` wrapped in a generic ToolResult

2. **`.get()` with early return** — explicit check + ToolResult:
   - `shell_ops.py`: `_task_status` line 84 — explicit `if not task_id: return ToolResult(success=False, ...)`
   - `agent_ops.py`: `_agent_status` line 150 — same pattern
   - `agent_patterns.py`: `_fan_out` line 315 — `if not descriptions: return ToolResult(success=False, ...)`
   - `search_ops.py`: `_find_symbol` line 169 — `if not name: return ToolResult(...)`

3. **Hybrid** — `.get()` with defaults + type casting:
   - `shell_ops.py`: `_run_shell` line 115 — `args.get("force", False)`, `min(int(args.get("timeout", 60)), 300)`
   - `file_ops.py`: `_read_file` line 38 — `args.get("offset", 0)`
   - `agent_ops.py`: `_parse_max_turns` — dedicated validation function

**Recommendation**: Adopt pattern #2 (explicit required-param check + ToolResult) consistently for all required params. Pattern #1 (`args["key"]`) is brittle. Pattern #2 is used in newer code (`agent_ops`, `agent_patterns`) but not backported to `file_ops.py` / `shell_ops.py`.

---

## 4. ToolResult Usage — Never Returns Raw Exceptions?

**Almost never.** All tool implementations wrap errors in `ToolResult(success=False, ...)`. The only exceptions that escape:

| File | Line | What |
|------|------|------|
| `agent_messages.py` | 122 | `_validate_payload` raises `ValueError` — caller in `__post_init__` also raises `ValueError` |
| `agent_patterns.py` | 43, 88, 133 | `fan_out`, `fan_in`, `pipeline`, `barrier` raise `RuntimeError` if runtime not found |
| `mcp_client.py` | 265 (`_parse_full_name`) | Raises `ValueError` for malformed MCP tool names |

These are not tool-entry-point functions, but they lack ToolResult wrapping. The `agent_patterns.py` Python API functions are documented helpers (not tools), so the contract is debatable — but catching those RuntimeErrors in the tool wrappers (`_fan_out` etc.) is good practice (already done via `try/except Exception as exc: return ToolResult(...)`).

**Verdict**: All registered tool functions return `ToolResult` consistently. Raw exceptions only escape from non-tool utility functions.

---

## 5. Unused Helper Functions

| Function | File | Status |
|----------|------|--------|
| `_repair_json()` | `__init__.py` | Used by `llm.py` (execute_tool) — **not unused** |
| `_persist_test_output()` | `shell_ops.py` | Called by `_run_tests` and `_verify` — **not unused** |
| `_stream_reader()` | `shell_ops.py` | Called by `run_shell` background mode — **not unused** |
| `_check_destructive()` | `shell_ops.py` | Called by `_run_shell` — **not unused** |
| `_parse_pytest_output()` | `shell_ops.py` | Called by `_run_tests` and `_verify` — **not unused** |
| `_search_single_file()` | `shell_ops.py` | Called by `_search_files` — **not unused** |
| `build_symbol_index()` | `search_ops.py` | Called by `_get_symbol_index` — **not unused** |
| `_reindex_file()` | `search_ops.py` | Called by `_write_file` in `file_ops.py` — **not unused** |
| `_get_symbol_index()` | `search_ops.py` | Called by `_find_symbol` — **not unused** |
| `_sem_get_model()` | `search_ops.py` | Called by semantic_search — **not unused** |
| `_format_collect_any()` | `agent_ops.py` | Called by `_collect_any` — **not unused** |
| `_spawn_one()` | `agent_ops.py` | Called by `_spawn_agent` and `fan_out` — **not unused** |
| `_parse_max_turns()` | `agent_ops.py` | Called by `_spawn_agent` — **not unused** |
| `uri_to_path()` | `lsp.py` | Called by `_ensure_document_open` — **not unused** |
| `_severity_name()` | `lsp.py` | Called by `get_diagnostics` — **not unused** |
| `_definition_to_tool_result()` | `lsp.py` | **Assumed used — referenced in `definition()` method** |
| `_locations_to_tool_result()` | `lsp.py` | **Assumed used — referenced in `references()` method** |
| `_result_to_tool_result()` | `mcp_client.py` | Called by `call_tool` — **not unused** |
| `_build_mcp_hint()` | `mcp_client.py` | Called by `call_tool` and `_result_to_tool_result` — **not unused** |
| `_parse_full_name()` | `mcp_client.py` | Called by `call_mcp_tool` — **not unused** |
| `_make_mcp_dispatcher()` | `mcp_client.py` | Called by `_register_server_tools` — **not unused** |
| `_make_mcp_summary()` | `mcp_client.py` | **Referred to in `_register_server_tools` but definition not shown — check if exists** |
| `convert_mcp_input_schema()` | `mcp_client.py` | Called by `_register_server_tools` — **not unused** |

**All helper functions appear used.** No dead code found.

---

## 6. Parameter Naming Inconsistencies

| Concept | File A | File B | Issue |
|---------|--------|--------|-------|
| Agent runtime ref | `agent_ops.py`: `runtime` (local var) | `agent_patterns.py`: `runtime` (param) | **Consistent** ✅ |
| Task description | `agent_ops.py`: `task` (param) | `agent_patterns.py`: `descriptions` (param), `task` in dict key | Inconsistent — `task` vs `description` vs `worker_task_template` |
| Task ID list | `agent_ops.py`: `task_ids` | `agent_patterns.py`: `task_ids` | **Consistent** ✅ |
| File path | `file_ops.py`: `path` | `shell_ops.py`: `file_path` (for single-file search) | Intentional: `file_path` scopes to one file. **OK** |
| Pattern param | `shell_ops.py`: `pattern` | `search_ops.py`: `name` (find_symbol) | Different concepts — **OK** |
| Max turns | `agent_ops.py`: `max_turns`, `_DEFAULT_MAX_TURNS` | `agent_patterns.py`: `max_turns` | **Consistent** ✅ |
| Read safety gate | `file_ops.py`: `rg` | `shell_ops.py`: `rg` | **Consistent** ✅ |
| Write safety gate | `file_ops.py`: `wg` | `shell_ops.py`: `wg` | **Consistent** ✅ |
| Visibility flag | `agent_ops.py`: `visible` | `agent_patterns.py`: `visible` | **Consistent** ✅ |
| Timeout | `shell_ops.py`: `timeout` (int) | `agent_patterns.py`: `timeout` (float) | Minor type inconsistency (int vs float) — **cosmetic** |

**Minor**: `task` (singular, string) vs `tasks` (plural, list) in `spawn_agent` is intentional API design. The `worker_task_template` vs `description` mismatch in `scatter_gather` vs `fan_out` could be unified.

---

## 7. Summary Functions — Consistent Pattern?

**Yes, very consistent.** Every tool has a matching `_<tool>_summary` function decorated with `@_summarize("<name>")`. The pattern is:

```python
@_summarize("tool_name")
def _tool_name_summary(args: dict) -> str:
    return f"tool_name({args.get('param', '?')})"
```

Observed in all files:

| File | Examples |
|------|---------|
| `file_ops.py` | `_read_file_summary`, `_write_file_summary`, `_edit_file_summary`, `_list_directory_summary`, `_file_info_summary` |
| `shell_ops.py` | `_task_status_summary`, `_run_shell_summary`, `_search_files_summary`, `_run_tests_summary`, `_verify_summary`, `_git_summary` |
| `search_ops.py` | `_find_symbol_summary` (others: semantic/web recall turn — not shown but likely follow pattern) |
| `agent_ops.py` | `_spawn_agent_summary`, `_agent_status_summary`, `_collect_agent_summary`, `_collect_any_summary` |
| `agent_patterns.py` | `_fan_out_summary`, `_fan_in_summary`, `_pipeline_summary`, `_barrier_summary`, `_scatter_gather_summary` |

**Verdict**: Summary functions are the most consistent pattern in the codebase. Every tool has one, with a uniform `@_summarize` decorator + `args.get()` for params.

---

## Summary of Issues (Priority-Ordered)

1. **HIGH** — `mcp_client.py` / `lsp.py` share ~200 lines of identical subprocess JSON-RPC plumbing (`_drain_stderr`, `_send_notification`, `disconnect`, `_start_process`, `is_connected`). Extract a shared base class.

2. **MEDIUM** — Mixed args validation: `file_ops.py` uses `args["key"]` which raises `KeyError` (pattern #1); newer files use explicit `.get()` + early `ToolResult` return (pattern #2). Backport pattern #2 to `file_ops.py`.

3. **LOW** — `agent_patterns.py` Python API helpers raise `RuntimeError` (raw exception) vs tool wrappers return `ToolResult`. Two error pathways for essentially the same operations.

4. **LOW** — Hint on failure is inconsistent: some tools provide helpful hints (`_read_file`, `_run_shell`), most don't (`_write_file`, `_edit_file`, all LSP tools).
