# Performance Audit: Tools I/O

**Files analyzed:** `tools/file_ops.py`, `tools/shell_ops.py`, `tools/search_ops.py`  
**Supporting:** `tools/__init__.py`

---

## 1. Filesystem Walking — os.walk Frequency

| Caller | File:Line | Runs per call | Cached? |
|--------|-----------|---------------|---------|
| `build_symbol_index` | search_ops.py:50 | 1 walk on first call | ✅ Memory (`_SYMBOL_INDEX`) + disk (`.mini_agent_index.json`). Skipped if `_INDEX_MAX_MTIME` ≤ cache mtime. |
| `_sem_index` | search_ops.py:~280 | 1 walk, combined pass | ✅ Per-file mtime check against `_SEMANTIC_STORE`. No-op if nothing changed. Same walk also feeds symbol+ref indices. |
| `_reindex_file` | search_ops.py:170 | 0 walks — single file | N/A. Incremental update after `.py` writes. |
| `search_files` | shell_ops.py:~370 | **1 walk per call** | ❌ Not cached across calls (cache is per-turn and only for identical args). No mtime skip. |

**Impact:** `search_files` does a fresh `os.walk` on every invocation. For large repos (10k+ files), this dominates I/O. The symbol/semantic index walks are well-cached — single walk at startup, incremental thereafter.

---

## 2. search_files — Streaming vs Full-Read

**Line-by-line streaming** (shell_ops.py:387-405): `for lineno, line in enumerate(f, 1)` — lazy iterator, never calls `.read()` or `.readlines()`. Early break at `_SEARCH_MAX_RESULTS` (200). Skip-dir filter and binary-ext filter applied before opening files.

**Memory profile on large dirs:**  
- Only one file open at a time.  
- Only results list grows (capped at 200 entries).  
- 500-file periodic yield comment at line 395 (no actual yield).  
- **Good for large directories** — does not load all files into memory.

**Caveat:** `errors="replace"` on open (line 399) handles binary but wastes CPU on false positives. The binary-ext pre-filter mitigates this.

---

## 3. File Reading — Buffering and Re-reads

### `read_file` (file_ops.py:50-90)
- **Streaming:** `for lineno, line in enumerate(f)` — Python's built-in buffered I/O (~8KB buffer).
- **No full read:** continues past the requested limit to count total lines (line 77: `total_lines = lineno + 1`), but breaks at `offset + limit + 1`.
- **Maximum memory:** at most `_ABSOLUTE_MAX_LINES` (1000) strings held simultaneously.

### `edit_file` (file_ops.py:~160)
- **Full read:** `original = f.read()` — entire file loaded into memory. Necessary for string replacement. Acceptable for typical code files (<1MB).

### `build_symbol_index` (search_ops.py:~110)
- **Full per-file iteration:** `for lineno, line in enumerate(f, 1)` — streaming, but reads every line of every `.py` file during initial walk. After first pass, only re-reads changed files via `_reindex_file`.

### `_sem_index` (search_ops.py:~340)
- **Full read:** `file_lines = f.readlines()` — loads each `.py` file entirely into memory. Required for semantic chunking (needs random line access). Same walk pass shares these lines for symbol/reference indexing — **no second read**.

### Re-read summary
| Scenario | Re-read |
|----------|---------|
| Same tool call | Never — each opens the file once. |
| write → find_symbol | Yes — `_reindex_file` re-reads the written file. Only one file, acceptable. |
| semantic_search after write | Yes — `_sem_index` re-reads changed files (mtime check). Still only changed files. |

---

## 4. Test Output Persistence

**`_persist_test_output`** (shell_ops.py:20-40):  
- Writes to SQLite `test_output` table: single row (id=1), `INSERT OR REPLACE`.  
- Called after: foreground `run_tests`, `verify` test targets.  
- **Not called** for background test runs — `_stream_reader` drains to `[]` (empty list), output is **lost**.

**Efficiency:** Single-row CRUD is cheap. No batching or streaming — entire output string stored as a TEXT column. Acceptable for pytest output (<1MB).

**Opportunity:** Background test runs silently discard all output. If a background test fails, there's no way to get the failure details.

---

## 5. Cache Hit Rates

| Tool | Cache Layer | Scope | Hit Rate Potential |
|------|-------------|-------|-------------------|
| `find_symbol` | Memory + disk (`.mini_agent_index.json`) | Across sessions | **Very high** — only invalidated on `.py` writes. |
| `find_usages` | Same index (built alongside symbol index) | Across sessions | **Very high** — shares index with find_symbol. |
| `semantic_search` | Memory (`_SEMANTIC_STORE`) + per-file mtime | Across sessions | **High** — mtime-gated; unchanged files skip re-indexing. |
| `search_files` | Per-turn `_TOOL_CACHE` | Within one turn only | **Low** — cache cleared every turn (`clear_tool_cache`, `__init__.py:223`). Only helps if same pattern called twice in same turn. |
| `read_file` | Per-turn `_TOOL_CACHE` | Within one turn only | **Low** — same file+params within one turn hits cache. |
| `file_info` / `list_directory` | Per-turn `_TOOL_CACHE` | Within one turn only | **Low** — rarely called with identical args twice. |

**Note:** The per-turn cache (`_TOOL_CACHE`) is keyed by JSON-serialized `(name, args)` and cleared at the start of every turn. It's a micro-cache — prevents duplicate reads within a single tool-call batch, not a persistent cache.

---

## 6. Shell Command Output — Buffering & Truncation

### Output paths:

| Mode | Mechanism | Buffer | Truncation |
|------|-----------|--------|------------|
| **Foreground** (no streaming) | `proc.communicate()` | Full read into memory | stdout: 500 lines, stderr: 100 lines (shell_ops.py:280-290) |
| **Streaming** (`on_output`) | Threaded `_stream_reader` | Line-by-line via callback | `_STREAM_READER_MAX_LINES` = 10,000 (shell_ops.py:35) |
| **Background** | Daemon thread → `[]` | Discarded immediately | N/A — output is lost |

### Buffering details:
- **`_stream_reader`** (shell_ops.py:45-56): reads line-by-line via `iter(stream.readline, "")`. Collects into a list. Caps at 10,000 lines with a single truncation marker. Threaded, daemon=true — won't block process exit.
- **`communicate()` path**: reads everything into memory before truncation. A 100MB stdout spike would be fully buffered, then truncated to 500 lines. **Potential memory pressure** on verbose commands.
- **`subprocess.PIPE`** risk: Background mode starts drain threads immediately to prevent pipe-buffer deadlock.

### Truncation strategy:
- stdout: 500 lines max (shell_ops.py:285-288). Message includes `"(truncated at 500 lines — N total)"`.
- stderr: 100 lines max (shell_ops.py:290-293).
- Stream reader: 10,000 lines cap for long-lived processes.
- All truncation is **post-facto** for the `communicate()` path — entire output must fit in memory first.

---

## Key Findings Summary

1. ✅ **Symbol index walk is cached** — single pass, disk-persisted, mtime-gated.  
2. ⚠️ **`search_files` does a fresh `os.walk` every call** — no mtime cache, no incremental mode.  
3. ✅ **`_sem_index` combines walks** — one pass for semantic + symbol + reference indices.  
4. ✅ **File reads are line-by-line** (except `edit_file` and `_sem_index`).  
5. ⚠️ **Background test output is lost** — `_stream_reader` drains to `[]`.  
6. ✅ **Per-turn tool cache** avoids duplicate reads within a turn.  
7. ⚠️ **`communicate()` buffers fully** before truncation — risk on verbose commands.  
8. ✅ **Streaming mode** uses bounded 10K-line buffer and forwards output in real-time.

**Top optimization opportunity:** Add mtime-gated caching to `search_files` (similar to `_sem_index`'s per-file mtime check) to skip unchanged directories/files on repeated searches.
