# Performance Audit: `tools/` Directory

## Summary

| Metric | Value |
|--------|-------|
| Total tools | 38 (22 built-in + async MCP) |
| Cold-start worst case | **5–30s** (symbol index build on large repo) |
| LSP first-query latency | **1–3s** (pylsp subprocess spawn + initialize handshake) |
| MCP first-query latency | **1–3s** per server (subprocess + init + tool discovery) |
| Hot-path worst case | **300s** (run_shell timeout) / **10s** (LSP query timeout) |
| Per-tool FS calls (median) | 1–2 |
| Per-tool FS calls (worst) | **O(files)** (find_symbol cold, search_files) |

---

## Per-Tool Breakdown

### 1. `read_file` (file_ops.py)
- **FS calls:** 2 (safety `realpath` → `open`)
- **Worst-case time:** O(lines) but capped at 1000 lines; ~1ms typical
- **Cache:** Cross-turn mtime cache (`_FILE_CACHE`) — O(1) hit when file unchanged
- **Notes:** Efficient early-break loop. Offset/limit avoid reading whole file. Cache stores `(content, mtime)` dict in memory.

### 2. `write_file` (file_ops.py)
- **FS calls:** 3–4 (`realpath`, `makedirs`, `backup` copy, `open`+write)
- **Worst-case time:** O(bytes written); ~1–10ms typical
- **Notes:** Generates diff preview before write. File reservation check (thread-safe). Session backup via `_backup_before_write()`. Atomic for small/medium files.

### 3. `edit_file` (file_ops.py)
- **FS calls:** 2 + backup (read → write)
- **Worst-case time:** O(file size) for string search + write
- **Notes:** Supports `count=-1` for replace-all. `preview=True` skips write, returns unified diff. Batch edit across multiple files via `paths` parameter.

### 4. `list_directory` (file_ops.py)
- **FS calls:** 1 (`os.scandir` or `listdir`)
- **Worst-case time:** O(entries); ~1–5ms typical
- **Notes:** Returns size/type/permissions. Skips hidden dirs.

### 5. `file_info` (file_ops.py)
- **FS calls:** 1 (`os.stat`)
- **Worst-case time:** ~0.1ms
- **Notes:** Returns mtime, size, permissions, type. For dirs also returns child count.

### 6. `run_shell` (shell_ops.py)
- **FS calls:** 0
- **Subprocess:** 1 spawn per call
- **Worst-case time:** 300s (hard cap timeout), default 60s
- **Notes:** Destructive-command guard (regex patterns). Background mode spawns daemon drain threads. Foreground uses `communicate()` (no thread overhead). Output capped at 500 lines stdout, 200 stderr. Streaming mode for real-time output.

### 7. `search_files` (shell_ops.py)
- **FS calls:** O(files) — recursive `os.walk` across workspace
- **Worst-case time:** 5–30s on large repos (tens of thousands of files)
- **Notes:** Reads every non-binary, non-hidden file line-by-line, applies regex. Result cap: 200 matches. No caching. **This is a linear scan — the largest cold-path cost after symbol index.**

### 8. `find_symbol` (search_ops.py)
- **FS calls (cold):** O(.py files) — `os.walk` + `open` each `.py` file
- **FS calls (warm):** 0 — in-memory dict lookup
- **Worst-case cold time:** 5–30s on large repos (~10k .py files)
- **Disk cache:** `.mini_agent_index.json` — persists across sessions. Mtime-based invalidation: re-walks only if any `.py` is newer than cache.
- **Notes:** Also builds `_REF_INDEX` in same pass. Deduplicates references. `_reindex_file()` incremental update on write.

### 9. `find_usages` (search_ops.py)
- **FS calls:** 0 (uses `_REF_INDEX` built by `find_symbol`)
- **Worst-case time:** O(matches) dict lookup; ~0.1ms
- **Notes:** Only works for symbols known to the index. `_SKIP_REF_NAMES` filters out builtins.

### 10. `semantic_search` (search_ops.py)
- **Network call:** 1 (embeddings API)
- **Worst-case time:** 2–10s (API latency + embedding computation)
- **Notes:** No local FS walk — indexes files live via embeddings service. Returns top 10 matches.

### 11. `web_search` (search_ops.py)
- **Network call:** 1 (Exa API)
- **Worst-case time:** 2–10s (API latency)
- **Notes:** Configurable depth (`auto`/`fast`/`deep`). Default 5 results, max 20.

### 12. `run_tests` (shell_ops.py)
- **Subprocess:** 1 (`pytest`)
- **Worst-case time:** 120s (default timeout)
- **Notes:** Background mode supported. Persists output to SQLite memory store for later retrieval.

### 13. `verify` (shell_ops.py)
- **Subprocess:** 1–2 (lint + pytest)
- **Worst-case time:** ~60s
- **Notes:** Runs lint + relevant tests for files modified in the current session.

### 14. `git` (shell_ops.py)
- **Subprocess:** 1 (`git <subcommand>`)
- **Worst-case time:** <5s typical
- **Notes:** Local-only operations (no push/pull). Subcommands: status, diff, log, init, add, commit, show, restore.

### 15. `lsp_definition`, `lsp_references`, `lsp_hover`, `lsp_diagnostics` (lsp.py)

| Phase | Latency |
|-------|---------|
| First query (cold) | **1–3s** — subprocess spawn (`pylsp`) + `initialize` handshake + `initialized` notification |
| Subsequent queries (warm) | **50–500ms** — single JSON-RPC round-trip over stdio |
| Query timeout | **10s** hard cap |
| Connection teardown | `SIGTERM` → wait 2s → `SIGKILL` |

- **FS calls:** 0 (uses stdio pipe to pylsp subprocess)
- **Thread safety:** `threading.Lock` serialises stdin writes
- **Diagnostics cache:** Per-URI dict, populated from `textDocument/publishDiagnostics` notifications
- **Language support:** Python only (`.py`, `.pyi`, `.pyx`) via pylsp
- **Reconnection:** Automatic — if `poll()` returns non-None, reconnects on next query

### 16. MCP tools (mcp_client.py)

| Phase | Latency |
|-------|---------|
| First tool call (cold) | **1–3s** per server — subprocess spawn + `initialize` + `tools/list` |
| Subsequent calls (warm) | **50–500ms** — single `tools/call` JSON-RPC round-trip |
| Tool discovery | On connect, registers into `_TOOL_DISPATCH` under `mcp/<server>/<tool>` |
| Connection teardown | Same as LSP |

- **FS calls:** 0
- **Notes:** Schema conversion strips unsupported JSON Schema keywords for OpenAI compatibility. Stderr drain thread prevents pipe deadlock. Shared `_json_rpc_shared.py` utilities with LSP.

### 17. `spawn_agent` (agent_ops.py)
- **FS calls:** 0
- **Worst-case time:** <1ms (thread spawn, non-blocking)
- **Notes:** Max 5 concurrent sub-agents. Each gets 15–35 turn budget. Optional streaming visibility.

### 18. `agent_status`, `collect_agent`, `collect_any`, `agent_read`, `agent_inbox`
- **FS calls:** 0
- **Worst-case time:** <1ms (dict/queue operations)
- **Notes:** Thread-safe via `threading.Condition`.

---

## Worst-Case Scenarios

| Scenario | Tool(s) | Latency | Root Cause |
|----------|---------|---------|------------|
| First `find_symbol` on large repo | find_symbol | 5–30s | Full workspace `.py` file walk |
| First LSP query | lsp_* | 1–3s | pylsp subprocess startup |
| First MCP tool call | mcp/* | 1–3s | Server subprocess startup |
| `search_files` on large repo | search_files | 5–30s | Full workspace file walk |
| Long-running shell command | run_shell | ≤300s | User command duration |
| Slow test suite | run_tests | ≤120s | pytest execution |
| Exa API timeout | web_search | ≤10s | Network |
| Embedding API timeout | semantic_search | ≤10s | Network |
| LSP query timeout | lsp_* | ≤10s | Server hang |

---

## Filesystem Call Summary

| Tool | FS calls (typical) | FS calls (worst) | Pattern |
|------|-------------------|-------------------|---------|
| read_file | 2 | 2 | realpath + open |
| write_file | 3–4 | 3–4 | realpath + makedirs + backup + write |
| edit_file | 2–3 | 2–3 | read + write (+ backup) |
| list_directory | 1 | 1 | scandir |
| file_info | 1 | 1 | stat |
| search_files | O(files) | O(files) | walk + open each |
| find_symbol (cold) | O(.py files) | O(.py files) | walk + open each .py |
| find_symbol (warm) | 0 | 0 | dict lookup |
| find_usages | 0 | 0 | dict lookup |
| everything else | 0 | 0 | subprocess/network only |

---

## Key Bottlenecks & Recommendations

### 1. Symbol index cold start (HIGH IMPACT)
**Problem:** First `find_symbol` blocks for 5–30s walking the workspace.
**Mitigations already in place:**
- Disk cache (`.mini_agent_index.json`) with mtime invalidation
- Incremental reindex on write (`_reindex_file`)
- Single-pass build (symbols + references in one walk)
**Further improvements:**
- Consider a background thread to eagerly build the index at session start
- Add `.gitignore`-aware skipping (currently only skips hidden dirs + `_SKIP_DIRS`)

### 2. LSP/MCP startup latency (MEDIUM IMPACT)
**Problem:** First LSP/MCP query incurs 1–3s subprocess spawn.
**Mitigations already in place:**
- Connection reuse across queries (single persistent subprocess)
- Auto-reconnection if process dies
**Further improvements:**
- Eagerly start LSP server at session init (not on first query)
- Warm-up query on a known file to trigger pylsp indexing

### 3. `search_files` linear scan (MEDIUM IMPACT)
**Problem:** Every invocation walks the full workspace. No caching.
**Recommendation:** Consider ripgrep (`rg`) subprocess for 10–50x speedup vs Python regex walk. Already has subprocess infrastructure.

### 4. `run_shell` timeout (LOW IMPACT, BY DESIGN)
**Problem:** 300s max timeout can block the agent loop.
**Mitigation:** Background mode + `task_status` polling. This is the intended pattern for long commands.

### 5. Thread count under concurrency (LOW IMPACT)
**Problem:** Each background `run_shell` spawns 2–3 daemon threads (stdout, stderr, optional stdin).
**Mitigation:** Daemon threads exit on EOF. `communicate()` used for foreground (zero thread overhead). Max 5 concurrent sub-agents × ~3 threads each = 15 threads max.
