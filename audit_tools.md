# Tools Directory Audit Report

Generated from full read of all `tools/` files + targeted `find_usages` checks.

---

## 1. Dead Code

### 1.1 `_sem_chunk_py()` — unreferenced helper
- **File:** `tools/search_ops.py`, line 307
- **Issue:** Function defined but never called. `find_usages` returns only the definition site.
- **Why dead:** The `_sem_index()` function (line 389) performs identical chunking logic inline (lines 455–474), making `_sem_chunk_py` redundant.
- **Recommendation:** Remove `_sem_chunk_py` and its duplicate chunking logic, or refactor `_sem_index` to call `_sem_chunk_py` instead of inlining.

### 1.2 `_COLLECT_ANY_POLL` — unused constant
- **File:** `tools/agent_ops.py`, line 490
- **Issue:** `_COLLECT_ANY_POLL = 0.2` is explicitly commented "unused, kept for reference". It was replaced by condition-variable-based waiting.
- **Recommendation:** Delete.

### 1.3 `_persist_test_output()` — only used by `run_tests` and `verify` (same module)
- **File:** `tools/shell_ops.py`, line 18
- **Status:** Not dead — used twice internally. No issue.

---

## 2. Design Violations

### 2.1 Duplicate `_SKIP_REF_NAMES` frozenset — TWO identical copies
- **Files:**
  - `tools/search_ops.py`, lines 64–80 (inside `build_symbol_index()`)
  - `tools/search_ops.py`, lines 400–416 (inside `_sem_index()`)
- **Issue:** The exact same 50+ symbol names are defined twice as local variables inside two different functions. Any change must be made in both places.
- **Recommendation:** Extract to module-level constant `_SKIP_REF_NAMES` and reference from both functions.

### 2.2 `build_symbol_index()` and `_sem_index()` duplicate workspace-walk logic
- **File:** `tools/search_ops.py`
  - `build_symbol_index()` (line 30): walks workspace, builds `_SYMBOL_INDEX` and `_REF_INDEX`
  - `_sem_index()` (line 389): walks workspace, builds `_SYMBOL_INDEX` and `_REF_INDEX` AND semantic embeddings
- **Issue:** Both functions do `os.walk`, regex-match def/class, collect references, and merge into the same global `_SYMBOL_INDEX` and `_REF_INDEX`. `_sem_index` additionally builds embedding vectors. The symbol/reference indexing is fully duplicated.
- **Impact:** Two separate workspace walks on first call. Wasted I/O.
- **Recommendation:** Have `_sem_index` call `build_symbol_index` first, or merge the walk logic so only one traversal occurs. The `_sem_index` function already attempts to serve double duty (it calls `_merge_symbol_data`), but `build_symbol_index` remains a separate full walk for the non-semantic path.

### 2.3 Duplicate `DESTRUCTIVE_PATTERNS` check is in `_check_destructive` but not applied to `run_tests`
- **File:** `tools/shell_ops.py`, line 86 (`_DESTRUCTIVE_PATTERNS`)
- **Issue:** `run_tests` (line 430) runs `subprocess.Popen(["python", "-m", "pytest", ...])` without the destructive-command check that `run_shell` performs. This is intentional (pytest is safe), but worth documenting.
- **Risk:** Low — hardcoded command, not user-controlled.

### 2.4 Magic numbers (not defined as named constants)
| Constant | File:Line | Value | Comment |
|---|---|---|---|
| `_STREAM_READER_MAX_LINES` | shell_ops.py:44 | 10000 | Named but arbitrary — is this enough? |
| `_SEARCH_MAX_RESULTS` | shell_ops.py:280 | 200 | Named but arbitrary cap |
| `_AGENT_MSGS_MAX` | agent_ops.py:38 | 1000 | Ring buffer cap |
| `_MAX_CONCURRENT` | agent_ops.py:40 | 5 | Hard cap on sub-agents |
| `_SEMANTIC_MAX_ENTRIES` | search_ops.py:291 | 500 | LRU eviction threshold |
| `_COLLECT_TIMEOUT` | agent_ops.py:385 | 30 | Seconds |
| `_COLLECT_ANY_TIMEOUT` | agent_ops.py:492 | 10 | Seconds |
| `_DEBOUNCE_S` | search_ops.py:222 | 2.0 | Seconds for index persist debounce |
| `CONTEXT_BUDGET` | agent_ops.py:1336 | 800_000 | Local variable in `_session_stats`, not module-level |

**Assessment:** Most of these are already named module-level constants, which is good. `_DEBOUNCE_S = 2.0` is a rare exception of a magic number used inline but also named. `CONTEXT_BUDGET = 800_000` is a local inside a function — should be module-level or sourced from config.

### 2.5 Inconsistent `ToolResult` usage — `diff_preview` field
- **File:** `tools/__init__.py`, line 55 — `ToolResult.__init__` accepts `diff_preview: str | None = None`
- **Usage:** Only `write_file` and `edit_file` in `file_ops.py` set `diff_preview`. All other tools ignore it.
- **Issue:** The field exists in the dataclass but is excluded from `to_dict()` (line 61) — it exists only for in-process use by the TUI. This is intentional but undocumented.
- **Recommendation:** Add a docstring note explaining that `diff_preview` is an out-of-band field for TUI rendering, not serialized.

### 2.6 `agent_patterns.py` tool wrappers duplicate spawn_agent logic
- **File:** `tools/agent_patterns.py`, lines 296–326 (`_fan_out` tool wrapper)
- **Issue:** The fan_out tool wrapper re-implements runtime/config retrieval, parameter validation, and error messages that are nearly identical to `_spawn_agent` in `agent_ops.py`. The Python functions (`fan_out`, `fan_in`, etc.) are clean abstractions, but the `@_register` tool wrappers duplicate boilerplate.
- **Recommendation:** Extract shared validation into a helper used by both agent_ops.py and agent_patterns.py.

### 2.7 `_backup_before_write` imports `shutil` at module level but `restore_file` imports `shutil` inside the function
- **Files:** `file_ops.py` line 9 (module-level `import shutil`), `agent_ops.py` line 1203 (function-level `import shutil`)
- **Issue:** Inconsistent import style. `restore_file` does `import shutil` inside the `@_register` function body.
- **Recommendation:** Move to module-level import.

---

## 3. Safety Gaps

### 3.1 CRITICAL: `read_image` uses raw `path` instead of `sr.resolved_path` — workspace boundary bypass
- **File:** `tools/agent_ops.py`, lines 1410–1450
- **Issue:** 
  ```python
  sr = rg.check(path)          # sr.resolved_path = /workspace/foo.png
  if not sr.allowed:
      return ...
  if not _os.path.isfile(path):  # BUG: uses raw 'path', not sr.resolved_path
      ...
  with open(path, "rb") as f:    # BUG: uses raw 'path', not sr.resolved_path
  ```
  The safety check passes, but the actual file access uses the raw user-supplied `path`. If `path` is absolute (e.g., `/etc/passwd`), the safety check would block it. But if `path` is a relative path with `..` components, `os.path.realpath(os.path.join(root, path))` may resolve differently than the raw `path` resolved against CWD.
- **Severity:** High. This is a workspace boundary enforcement gap.
- **Fix:** Use `sr.resolved_path` consistently:
  ```python
  if not _os.path.isfile(sr.resolved_path):
      ...
  with open(sr.resolved_path, "rb") as f:
  ```

### 3.2 `read_image` sends image bytes to external API (GPT-4o)
- **File:** `tools/agent_ops.py`, lines 1437–1470
- **Issue:** Binary image data is base64-encoded and sent to `api.openai.com`. No size limit is enforced on the image before encoding and sending.
- **Risk:** A large image could consume significant memory/tokens. The `max_tokens=1000` limits response cost, but the request itself has no size guard.
- **Recommendation:** Add a file size check before reading (e.g., 20 MB max).

### 3.3 `_web_search_ddg` makes unauthenticated outbound HTTP to DuckDuckGo
- **File:** `tools/search_ops.py`, lines 633–690
- **Issue:** This is a fallback when no Exa API key is configured. It scrapes DuckDuckGo's HTML results page.
- **Risk:** Rate limiting, IP-based blocking, terms of service. No `robots.txt` compliance.
- **Recommendation:** Document the fallback behavior. Consider adding a `User-Agent` that identifies the tool.

### 3.4 `run_shell` — `force=True` bypasses the destructive-command guard
- **File:** `tools/shell_ops.py`, lines 111–115
- **Issue:** The `force` parameter is explicitly designed to bypass `_check_destructive`. This is intentional but worth noting as a potential footgun.
- **Risk:** An LLM could be tricked into passing `force=True` for a destructive command.
- **Mitigation:** The TUI's `approve_callback` mechanism adds a user-approval gate for `run_shell` (see `execute_tool` in `__init__.py` line 471).

### 3.5 `write_file` and `edit_file` — file reservation check is NOT atomic with the write
- **File:** `tools/file_ops.py`, lines 129–137 (write), lines 198–206 (edit)
- **Issue:** The file reservation is checked, then the write happens. Another agent could race in between. The reservation system uses `_current_agent_id` threading.local but the write itself is not under the `_FILE_RESERVATIONS_LOCK`.
- **Risk:** Low in practice (single-writer workspace assumption), but a TOCTOU gap exists.
- **Recommendation:** Document the TOCTOU acceptance explicitly (as safety.py does for symlinks).

### 3.6 `verify` tool runs `flake8` and `pytest` as subprocesses
- **File:** `tools/shell_ops.py`, lines 480–590
- **Issue:** The `verify` tool spawns flake8 and pytest without the destructive-command check that `run_shell` applies. Commands are hardcoded (`["python", "-m", "flake8", ...]`), so risk is minimal.
- **Risk:** Low — fixed command, not user-controlled.

---

## 4. Error Handling

### 4.1 All tools return `ToolResult` — no raw exceptions escape
- **Status:** PASS. Every `@_register` decorated function returns a `ToolResult` on all code paths. Exceptions are caught and wrapped.

### 4.2 `execute_tool()` enriches failures with heuristic hints
- **File:** `tools/__init__.py`, lines 315–380 (`_build_error_hint`)
- **Status:** PASS. Unknown parameters, missing required params, and common error patterns (file not found, command not found) all produce structured hints.

### 4.3 JSON repair handles malformed LLM output
- **File:** `tools/__init__.py`, lines 226–314 (`_repair_json`)
- **Status:** PASS. Trailing commas, single quotes, unquoted keys, and combinations are repaired.

### 4.4 LSP tools catch `LspRpcError` and `LspConnectionError`
- **File:** `tools/lsp.py`, methods `definition`, `references`, `hover`, `get_diagnostics`
- **Status:** PASS. Structured `ToolResult` returned on all error paths.

### 4.5 MCP tools catch `McpRpcError`, `McpConnectionError`, and reconnect once
- **File:** `tools/mcp_client.py`, `call_tool()` method
- **Status:** PASS. One automatic reconnect attempt on connection failure.

### 4.6 Wide exception catching — acceptable but noted
- `tools/search_ops.py` line 620: `except Exception as e:` in `_web_search` (Exa API call) — acceptable for network I/O.
- `tools/search_ops.py` line 690: `except Exception as e:` in `_web_search_ddg` — acceptable for HTTP scraping.
- `tools/shell_ops.py` line 401: `except Exception as e:` in `_search_files` during `os.walk` — acceptable for filesystem operations.
- `tools/file_ops.py` line 93: `except Exception as e:` in `_read_file` — acceptable, but could be narrowed to `OSError`.

### 4.7 `_persist_test_output` swallows all exceptions silently
- **File:** `tools/shell_ops.py`, lines 18–38
- **Issue:** The `except Exception: pass` on line 37 silently ignores any DB write failure for test output persistence.
- **Assessment:** Intentional (best-effort persistence). Not a bug, but could log a warning.

---

## 5. Summary

| Category | Findings | Critical |
|---|---|---|
| Dead code | 2 (unreferenced function, unused constant) | 0 |
| Design violations | 7 (duplicate logic, magic numbers, inconsistent patterns) | 0 |
| Safety gaps | 6 (workspace bypass in read_image, missing size limit, TOCTOU, etc.) | **1** |
| Error handling | 7 checks — all pass, minor notes | 0 |

### Top Priority Fixes
1. **`read_image` safety bypass** — use `sr.resolved_path` instead of raw `path` (agent_ops.py ~line 1418)
2. **Duplicate `_SKIP_REF_NAMES`** — extract to module-level constant (search_ops.py)
3. **`_sem_chunk_py` dead code** — delete or refactor (search_ops.py:307)
4. **`_COLLECT_ANY_POLL` dead code** — delete (agent_ops.py:490)
5. **`read_image` size limit** — add file size check before base64 encoding (agent_ops.py)
6. **`CONTEXT_BUDGET` magic number** — move to module-level constant or config (agent_ops.py:1336)
