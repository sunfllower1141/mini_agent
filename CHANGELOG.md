## 2026-06-20 -- Gut Hash Cache read_file System

## 2026-06-22

### Fixed
- **`_inject_tool_result_stubs` OpenAI format bug.** When Dirac compaction
  (`_compact_if_needed`) truncated the conversation, assistant messages with
  `tool_calls` could lose their corresponding tool result messages (which were
  in the removed middle section).  `_inject_tool_result_stubs` was supposed to
  inject "result missing" stubs but silently SKIPPED rebuilding for OpenAI
  tool-role format (the `is_tool_role` path).  Additionally, the function only
  looked at the first consecutive tool message, missing subsequent ones; and
  the text-only-user-message case `continue`d without inserting stubs.
  
  **Fix:** (a) Initialize `needs_update` before format-specific blocks.
  (b) In the `is_tool_role` path, scan ALL consecutive tool messages (not just
  `next_msg`).  (c) Convert the text-only-user `continue` path to actually build
  and insert stubs before the user message.  (d) Change the rebuild guard from
  `elif is_tool_role and needs_update:` to `elif needs_update:` so both cases
  are handled.
  - File: `core/context_inject.py`

- **API clean-messages cache staleness after Dirac compaction.**
  `_compact_if_needed()` rebuilds messages in-place (`clear()` + `extend()`),
  keeping the same Python list object and `id()`.  The incremental cleaning
  cache in `api.py` (`_clean_messages_cache`), keyed by `id(messages)`, would
  then serve stale pre-compaction messages to the API on the next `call_llm`.
  
  **Fix:** Call `clear_api_cache()` at the end of `_compact_if_needed()`
  after the message list has been modified, invalidating the stale cache entry.
  - File: `core/context_inject.py`
- **Turn-boundary compaction reverted.** Removed `run_turn_boundary_compaction`
  call from the turn loop in `core/llm.py`. This function was running the full
  three-tier compaction pipeline (truncate → prune → LLM-compact) at EVERY turn
  boundary, when it should only run truncation (`compact_tool_results_at_turn_end`).
  At HARD compaction level (~80% context window, turn 20-25), `prune_stale_tool_results`
  was replacing recent tool results with compact placeholders in-place, causing
  "tools never spawned" in the UI and the AI being unaware of tool outputs.
  
  Reverted to bugfree behavior: only truncation at turn boundary, full compaction
  still runs through the existing `_compact_if_needed` path.


## 2026-06-21

### Fixed
- **Cost control: Dirac compaction never firing.** `_compact_if_needed()` used
  `HARD_LIMIT=1_000_000`, which meant models reporting 1M context windows
  (deepseek-v4-pro) never triggered conversation truncation. Every turn re-sent
  the full history. Added `COMPACTION_HARD_LIMIT=200_000` to `core/constants.py`
  and changed `_compact_if_needed` to use it.  Compaction now fires at 200k tokens
  regardless of model-reported window.
  - Files: `core/context_inject.py`, `core/constants.py`
**Rationale:** The hash-based re-read shortcut returned a `CACHED ->` stub
instead of file content when content hadn't changed. This confused models.
Every read_file now always reads from disk and returns full content.

### Changed
- **file_ops.py:** Removed `_FILE_HASHES` dict and hash-based cache shortcut (~lines 471-486).
  Removed `_FILE_CACHE`, `_get_cached_content()`, `_cache_file_content()`, `_CACHE_DISK_READS`.
  Removed `FileContextTracker` import and `mark_file_read` call. All read/edit paths
  now do direct disk I/O.
- **file_context_tracker.py:** Gutted to no-op stubs; module kept for import safety.
- **test_file_ops_extended.py:** Removed `_FILE_CACHE.clear()` reference.


## 2026-06-20 -- Cross-Session Conversation Continuity

**Rationale:** The agent was forgetting conversation context when the Electron
app restarted. The HANDOFF.md and session summary captured only git changes and
plan progress, with no record of what the user was actually discussing.

### Changed
- **memory.py (`write_session_handoff`):** Added `recent_conversation` parameter.
  When provided, a "### Recent Conversation" section is written to HANDOFF.md
  with the last 3-5 user messages. This gives the next session immediate context.
- **bootstrap.py (`_cleanup_on_exit`):** Extracts recent user messages from saved
  conversation (filtering out system metadata and summaries), passes them to
  `write_session_handoff` and `capture_session_summary`. Session summary now
  includes "Recent conversation" in addition to scratchpad and turn history.
- **prompt.py (`_STATIC_PROMPT`):** Added "PLACEHOLDER-VALUE GUARD" section
  telling all models that ?, ???, ... are refused as tool parameters.
- **.mini_agent.rules:** Added "Tool Usage Rules" (no placeholder values) and
  "Session Continuity" (read HANDOFF.md at startup) sections.
- **memory_core:** Added entry documenting the REFUSED guard for cross-model awareness.


# Changelog
## 2026-06-20 -- Compact Error Messages Everywhere

**Rationale:** Tool error outputs were verbose and inconsistent — some used the compact
`✗ CODE: what → fix` format while others exposed raw exceptions (`Error: {e}`) or
multi-line prose (`WARNING: DANGEROUS COMMAND BLOCKED: ...\nThis command was NOT
executed. If you are absolutely sure, set force=True...`).

### Changed
- **test_tools.py:** Added `TestErrorSteering` (15 tests) that verify the full
  error pipeline: compact format `✗ CODE: what → fix`, fingerprint extraction,
  error classification (NOT_FOUND/TRANSIENT/VALIDATION/AUTHORIZATION), recovery
  hint injection on repeated failures, and end-to-end steering correctness
  (NOT_FOUND → list_directory, ANCHOR → re-read with hash_lines, BLOCKED →
  force=true, TIMEOUT → simplify/break up, GUARD → read_file first, etc.).
- **shell_ops.py:** All error messages converted to `_err()` compact format.
  - `_check_dangerous_command`: 4-line block → `✗ BLOCKED: {reason} → force=true`
  - Timeout messages: `Command timed out after {n}s` → `✗ TIMEOUT: {n}s → increase timeout or simplify`
  - Safety blocked: `Search blocked by safety layer: {reason}` → `✗ BLOCKED: {reason}`
  - Invalid regex: `Invalid regex: {err}` → `✗ INVALID: regex → {err}`
  - File read error: `Error reading '{path}': {e}` → `✗ READ: {e}`
  - Not a file: `Not a file: {path}` → `✗ NOT_FOUND: {path}`
  - Generic error: `Error: {e} (hint: use --help or search_files)` → `✗ ERROR: {e}`
- **file_ops.py:** All `Error reading/writing/listing/stating` → `✗ READ/WRITE/LIST/STAT: {e}`
  - `Offset {n} exceeds file length ({m} lines)` → `✗ RANGE: offset={n} > {m}`
  - `No definitions found in any of the provided files: {paths}` → `✗ NOT_FOUND: no definitions`
  - All safety/access denied messages → `✗ BLOCKED: {reason}`
- **agent_ops.py:** Added `_err` import; converted `Read blocked`, `File not found`,
  `Failed to read image`, `OpenAI API request failed` to compact format.
- **error_hints.py:** Added `timeout` fingerprint for run_shell; added `blocked`
  fingerprint for read_file.

### Result
All tool errors now use the same `✗ CODE: what → fix` format, making them:
- **Terse** — fits one line, no preamble
- **Actionable** — the arrow points directly to the fix
- **Consistent** — same format regardless of which tool failed


Self-modification audit trail -- what the agent changed and why.

## 2026-06-20 -- C/C++ AST-Native Tool Support

**Rationale:** The 4 AST-native tools (`get_file_skeleton`, `get_function`,
`replace_symbol`, `get_symbol_range`) only supported Python, JS, and TS.
C/C++ is the most common compiled language family and needed support.

### Changed
- **`core/tree_sitter_parser.py`**: Added `.c`, `.h` → `c`/`tree_sitter_c` and
  `.cpp`, `.cc`, `.cxx`, `.c++`, `.hpp`, `.hh`, `.hxx`, `.h++` → `cpp`/`tree_sitter_cpp`
  to the extension-to-parser mapping.
- **`tools/ast_ops.py`**: 
  - Added `_get_declarator_name()` helper to traverse C/C++ nested declarator chains
    (`function_declarator` → `declarator` → `identifier`/`field_identifier`).
  - Extended `_collect_ts_definitions` with handlers for: `function_definition`,
    `struct_specifier`, `union_specifier`, `class_specifier`, `enum_specifier`,
    `type_definition` (typedef), `namespace_definition`, `template_declaration`,
    `preproc_def`, `preproc_function_def`.
  - Updated `_collect_calls` to handle C/C++ `call_expression` nodes (including
    `field_expression` and `qualified_identifier` function targets).
  - Updated `_walk_for_def` and `_get_enclosing_class_name` for C/C++ node types.
  - Extended `_are_types_compatible` with C/C++ kind synonyms (struct/union/enum/
    class/typedef/macro/namespace).
  - Added `c` and `cpp` to `_guess_ext_from_tree` lang_map.
  - Extended `_collect_definitions` routing to include C/C++ extensions.
- **`requirements.txt`**: Added commented-out entries for `tree-sitter-c` and
  `tree-sitter-cpp`.


## 2026-06-20 -- AST Tools Python `ast` Fallback (tree-sitter not installed)

## 2026-06-20 -- Edit Insert Dedup (duplicate lines after edit_file)

**Rationale:** When using `edit_file` with `edit_type: "insert_after"` or `"insert_before"`,
the LLM often includes the anchor line itself in `new_text`, causing duplicate lines.
The agent then wastes turns cleaning up these duplicates instead of doing real work.

### Changed
- **`tools/file_ops.py`**: Added dedup logic in `_edit_lines` insert path:
  - `insert_after`: if `new_text`'s first line matches the anchor line, strip it
  - `insert_before`: if `new_text`'s last line matches the anchor line, strip it

**Rationale:** `get_function`, `get_file_skeleton`, `replace_symbol`, `get_symbol_range`
all returned "Unsupported file type: .py" because `tree-sitter` packages are commented
out in `requirements.txt` and `ast_ops.py` had no fallback to Python's built-in `ast`.
This broke the "AST-NATIVE TOOLS" steering in the system prompt.

### Changed
- **`tools/ast_ops.py`**: Added `_ast_fallback_definitions(source)` using Python's built-in
  `ast` module to extract function/class definitions with byte offsets, signatures, and
  decorators. Added `_ast_fallback_skeleton`, `_ast_fallback_function`,
  `_ast_fallback_replace`, `_ast_fallback_range` that use this fallback. All four
  entry-point functions now attempt the Python `ast` fallback for `.py`/`.pyi` files
  when tree-sitter is unavailable (+315 lines).
## 2026-06-20 -- Eliminate Double-Read, Merge RAW_CACHE into FILE_CACHE, Remove Dead Code

**Rationale:** The read_file path was reading files twice (once for hash, once for content),
and RAW_CACHE duplicated FILE_CACHE's mtime semantics. Consolidating eliminates disk I/O
and ~50 lines of redundant code.

### Changed
- **`tools/file_ops.py`**:
  - Removed `_read_file_windows_worker` (~70 lines of dead code).
  - `_read_file` now reads file once in text mode, computes MD5 hash from content,
    passes `_content` to `_read_file_direct` (eliminating double-read).
  - `_read_file_direct` accepts optional `_content` param; computes `all_lines`
    from provided content instead of re-reading from disk.
  - Merged RAW_CACHE into FILE_CACHE: `_get_cached_content` now serves both
    read_file mtime caching and edit_lines disk-bypass, using same LRU cap.
  - `_edit_lines` uses `_get_cached_content` first, falls back to disk read
    (tracked by `_CACHE_DISK_READS`).
  - Added file-not-found hint to error output (regression fix).
  - Cleaned up extra blank lines.

### Bug fixes
- **`all_lines` undefined**: Fixed UnboundLocalError when `_content` param was passed
  (computation was inside `else` block that only ran on disk-read path).
- **Missing hint regression**: Restored "Hint: Check the path spelling" in read_file
  error output (was lost when OSError handling moved from `_read_file_direct` to `_read_file`).

### Tests
- `tests/test_tools.py`: 96/96 pass
- `tests/test_file_ops_extended.py`: 76/76 pass
- Net: -47 lines in file_ops.py (89 added, 136 removed)

## 2026-06-20 -- Raw-Content Cache for edit_file Speedup

**Rationale:** `edit_file` / `_edit_lines` was always re-reading file content from disk
even though `read_file` had just read the same file (seconds ago). Adding a parallel
raw-content cache alongside the existing `_FILE_CACHE` eliminates redundant disk I/O.
For single edits this saves ~0.5ms; for chained edits (edit→edit→edit) the win is larger.

### Changed
- **`tools/file_ops.py`**: Added `_RAW_CACHE` (plain-text content, LRU-capped at 50 entries)
  with `_raw_cache_file()` and `_raw_cache_get()`. `read_file` (`_read_file_direct`) populates
  the raw cache after reading. `_edit_lines` checks raw cache first, falls back to disk.
  `_CACHE_HITS` and `_CACHE_DISK_READS` counters for diagnostics. Both caches updated
  after successful edit.
- **`_bench_edit_tools.py`**: Confirmed edit_lines is 5.2x–11.5x faster for bulk edits
  (1 read + 1 write for N changes vs N reads + N writes).
- **`_test_raw_cache.py`**: Smoke test for cache hit/miss on mtime change.


## 2026-06-20 -- edit_file Flat Args Removed + Schema Auto-Generation

**Rationale:** The dual `edit_file` (flat args) / `edit_lines` (edits array) split caused
confusion. Consolidating both entry points into the `edits[{...}]` format simplifies the
codebase by ~90 lines and forces consistent editor behavior.

### Changed
- **`tools/file_ops.py`**: Deleted `_edit_file` wrapper (~60 lines). Both `edit_file` and
  `edit_lines` now route directly to `_edit_lines` which accepts only `edits` array.
  Added `to`/`to_hash` defaults (fall back to `from`/`from_hash`) for convenience.
  Renamed all error messages from `edit_lines:` to `edit_file:`.
- **`tools/schema.py`**: Auto-generated from 1816 lines to 95 lines. Both `edit_file`
  and `edit_lines` schemas use `edits` array format. `SUB_AGENT_TOOLS` set moved to
  runtime generation.
- **`tests/test_tools.py`**: Updated all `edit_file` calls from flat args (`**{from, from_hash, new_text}`)
  to `edits=[{from, from_hash, new_text}]` format (7 test sites).
- **`tests/test_file_ops_extended.py`**: Fixed `test_restore_after_edit` (missed flat-args conversion).
- **`STATE.txt`**: Updated architecture notes.
## 2026-06-19 -- old_string Fallback Removed from edit_file

**Rationale:** Hash-anchored editing has proven reliable. Maintaining dual code paths
(old_string substring matching and hash-anchored line-based editing) caused
confusion and maintenance burden. Gutting the old_string system simplifies the
codebase and forces consistent editor behavior.

**Changes:**
- `tools/file_ops.py`:
  - Removed `_apply_single_edit` function (~180 lines) -- no remaining callers
  - Removed `_EditResult` type alias
  - `_edit_file`: old_string/paths/count fallback replaced with error directing to hash anchors
  - `_edit_file_summary`: removed old_string fallback path
- `tools/schema.py`: edit_file definition simplified -- removed `old_string`, `new_string`,
  `count`, `paths` params. `from` and `from_hash` now required.
- `core/safety.py`: `generate_diff` for edit_file now uses line-range diffing instead of
  old_string/new_string substitution.
- `core/context_inject.py`: updated critical-failure hint for edit_file.
- Tests: 8+ tests updated from old_string to hash-anchored mode across
  test_tools.py, test_safety_diff.py, test_resilience.py, test_post_edit_verify.py,
  test_file_ops_extended.py.

## 2026-06-19 -- AST Tools Default to Hash-Anchored Output

### Changed
- **`tools/file_ops.py`**: Changed `include_anchors` default from `False` to `True` in
  `_get_file_skeleton` (line 1714) and `_get_function` (line 1766) dispatch wrappers.
- **`tools/schema.py`**: Updated both `include_anchors` descriptions from
  "Default false" to "Defaults to true" for `get_file_skeleton` and `get_function`.

### Why
AST-native tools (`get_file_skeleton`, `get_function`) are used as primary file-
navigation primitives (instead of `read_file`). The core memory convention states
that `read_file` should use `hash_lines=True` by default. The same must apply to
AST tools so the output format is consistent and directly pipeable to `edit_lines`.
Without anchors, editing requires a follow-up `read_file` call, cancelling the
token savings AST tools provide.

## 2026-06-14 -- Workspace Organization Audit & Doc Drift Fix

### Changed
- **`.mini_agent.rules`**: Updated Testing section to reflect `tests/` directory
  (not root `test_*.py`). Synced Module Map with STATE.txt — added 19 missing
  module entries (agent_spawn, agent_collect, result, context, reservations,
  skills, error_hints, failure_learning, tool_graph, _json_rpc_shared,
  desktop_ops, macos_ops, browser_ops, memory_consolidation, memory_core,
  discord_bot, voice_handler, workspace_bot, skills/).
- **`.mini_agent/rules.toml`**: Fixed `[rules.test_files]` pattern from
  `test_*.py` → `tests/test_*.py` (old pattern never fired — no root-level tests).
- **`.gitignore`**: Added `discord_bot.log` and `.bot.pid` to prevent
  workspace log/pid leakage (logs should live at `~/.mini_agent/logs/`).
- **`TASKS.md`**: Fixed `tools/tool_result.py` → `tools/result.py`. Updated
  agent orchestration section for agent_ops.py split (spawn → agent_spawn,
  collect → agent_collect). Updated Testing section to `tests/` directory.

### Why
Audit revealed 5 documentation drift issues from rapid iteration (Hermes skills,
agent_ops split, discord bot additions). No structural problems — directories
match architecture, no missing files or circular import landmines.

## 2026-06-14 -- Tool Result Cache with TTL
### Changed
- **tools/__init__.py**: Added 30-second TTL to the existing `_TOOL_CACHE`. Cache
  entries now store `(timestamp, ToolResult)` instead of raw `ToolResult`.
  Lookup checks `time.monotonic() - timestamp < _TOOL_CACHE_TTL` before
  returning a hit; expired entries are evicted on access. Added hit/miss
  tracking (`_TOOL_CACHE_HITS`, `_TOOL_CACHE_MISSES`) and a
  `get_tool_cache_stats()` function for observability.
- **tests/test_tools.py**: Added `test_cache_ttl_expiry` (artificially ages
  a cached entry past TTL, verifies fresh re-read) and
  `test_write_invalidates_path_in_cache` (write_file evicts read_file cache
  for the same path). All 6 cache-related tests pass.

### Why
Without TTL, cached read_file results lived forever until write invalidation.
TTL (3600s / 1 hour) is a safety net for edge cases where invalidation misses
a change (e.g., subprocess modifies a file outside the agent's knowledge).
1-hour TTL matches industry best practices (AgenticSkillset.org recommends
3600s for read_file/search_code) and is long enough to cover any reasonable
agent session while still bounding staleness. Primary invalidation remains
write-driven — editing a file instantly evicts its cached reads.

## 2026-06-14 -- Dead-Tool Pruning (Session-Level)
### Changed
- **tools/__init__.py**: Added per-session tool usage tracking (`_TOOL_USAGE_COUNT`),
  `get_tool_usage()`, `get_unused_tools()`, `reset_tool_usage()`. Counter
  incremented in `execute_tool()` for every tool call. Session reset wired
  into `core/bootstrap.py`.
- **tools/skills.py**: Added `prune_unused_skills(unused_tools)` — deactivates
  skills whose ALL tools have zero usage after the pruning threshold.
- **core/context_inject.py**: Added `_inject_dead_tool_pruning()` — runs at turn 5
  (configurable via `_DEAD_TOOL_PRUNE_TURN`), deactivates unused skills,
  injects a transient message so the agent knows. Shrinks API payload by
  500-2000 tokens and stabilizes the KV-cache prefix (tool definitions stop
  changing mid-session).
- **core/bootstrap.py**: Calls `reset_tool_usage()` at session init.

### Why
After 5 turns, the agent's tool usage pattern stabilizes. Any skill whose tools
have never been called is dead weight in every subsequent API request — both
in token cost and in KV-cache instability (changing tool definitions invalidate
the cached prefix). Pruning them yields ~5-10% fewer tokens per API call and
fewer cache misses.

## 2026-06-14 -- Self-Improvement: Speed, Cache Hit, Cost Saving, Memory
### Changed
- **tools/semantic_cache.py**: Two-tier cache (exact hash match + semantic cosine),
  per-entry adaptive thresholds with online feedback loop (`report_feedback()`),
  expanded stats (exact_hits, semantic_hits, avg_adaptive_threshold, feedback).
- **memory/memory_prune.py**: Two-tier compression (gentle zone keeps more context,
  aggressive zone uses type-aware trimming), system prompt preservation (never
  prune/compress index 0), new constants _COMPRESSION_GENTLE_RECENT,
  _COMPRESSION_GENTLE_MAX_LINES, _TOOL_RESULT_GENTLE_CHARS.
- **memory/memory.py**: Enabled two-tier compression with gentle_recent=20,
  imported _COMPRESSION_GENTLE_RECENT.
- **api.py**: Added `prompt_cache_key` for DeepSeek to improve KV cache
  routing stickiness and cache hit rate.

## 2026-06-14 -- python -c Diagnostic Hints
### Changed
- **tools/shell_ops.py**: Added two diagnostic hints for `python -c` no-output commands:
  1. `#` comment detection — warns `#` eats rest of line, suggests `;` separators
  2. Compound statement detection — warns `if/try/for/while/with/def/class` can't follow `;`
- Fixed detection to use `"python" in command` instead of `command.startswith("python")`

## 2026-06-14 -- Hermes-Style Skill Architecture
### Added
- **skills/ directory** with 10 SKILL.md files (git, test, lsp, web, agents, search, tasks, image, desktop, bootstrap)
- **SKILL.md format**: YAML frontmatter (name, description, version, author, category, tools) + markdown body
- **`Skill` dataclass** in `tools/skills.py` with `to_catalog_entry()` and `to_full_doc()`
- **`_parse_frontmatter()`**: zero-dependency YAML-like frontmatter parser (inline lists, block lists, booleans, comments)
- **`_discover_skills()`**: scans `skills/` in workspace, then `~/.mini_agent/skills/` for SKILL.md files
- **`skill_list()`**: compact catalog of all skills (cached), injected at session start
- **`skill_view(name)`**: full SKILL.md documentation for a specific skill
- **`get_active_skill_content()`**: returns concatenated body of newly activated skills for prompt injection (once per session per skill)
- **`reload_skills()`**: force re-discovery after skill file writes
- **`_use_skill` now returns full skill documentation** in its result so the agent can immediately learn how to use unlocked tools
- **`tests/test_skills_hermes.py`**: 25 new tests covering Skill dataclass, frontmatter parsing, disk discovery, skill_list, skill_view, active content injection
### Changed
- **`tools/skills.py`**: rewritten from simple dict-based skill list to full Hermes-style disk-based architecture
- Backward-compatible `SKILLS` dict maintained via `_get_skills_compat()` lazy init
- `USE_SKILL_SCHEMA` now dynamically built with available skill names from disk
- All 36 existing skills tests still pass

## 2026-06-14 -- Fix OpenRouter Kimi Model ID Prefix (moonshot -> moonshotai)
### Fixed
- **App.jsx**: Changed `moonshot/kimi-k2.7-code` -> `moonshotai/kimi-k2.7-code` and
  `moonshot/kimi-k2.6` -> `moonshotai/kimi-k2.6` in `OPENROUTER_MODEL_GROUPS`.
  OpenRouter uses provider prefix `moonshotai/` (not `moonshot/`). The old prefix
  caused API error 400: "moonshot/kimi-k2.7-code is not a valid model ID".
- **config.py**: Fixed `openrouter` provider default model from
  `moonshot/kimi-k2.7-code` to `moonshotai/kimi-k2.7-code`.
- Rebuilt renderer dist (`npx vite build`).

## 2026-06-14 -- Fix backend:response Handler Silent Drop
### Fixed
- **App.jsx**: `backend:response` event handler had `data.target === 'chat'` guard,
  but the Python backend never sets a `target` field on response messages.
  This caused ALL slash-command responses (`/stats`, `/session`, `/workspace`)
  and model-switch errors to be silently discarded. Removed the broken guard.

## 2026-06-14 -- Model Picker Two-Section Layout
### Changed
- **App.jsx**: Reorganized model picker into two clear sections:
  - `DIRECT_MODEL_GROUPS`: DeepSeek, Kimi/Moonshot, Qwen (qwen-plus/flash/3-max/3-coder), Free Tier (Gemini 3.5 Flash)
  - `OPENROUTER_MODEL_GROUPS`: Kimi (moonshotai/), Gemini (google/), Qwen (qwen/), Free Models (:free suffix)
  - Removed old `PROVIDER_MODELS` and `ALL_MODEL_GROUPS`; removed unused `allModelsExpanded` state
- **style.css**: Added `.model-dropdown-section`, `.model-dropdown-section-header`, `.model-dropdown-subheader`
- **server.py**: Added `qwen3-coder`, `gemini-3.5-flash`, `gemini-3.5-pro` to `_MODEL_TO_PROVIDER` mapping
- **config.py**: Set OpenRouter default model to `moonshot/kimi-k2.7-code` (later corrected to `moonshotai/`)

## 2026-06-13 -- ASCII-Only Codebase Cleanup
### Fixed
- **All 134 `.py` files now ASCII-only**: Removed all non-ASCII Unicode bytes
  (replaced with ASCII equivalents) and all `\uXXXX` escape sequences (replaced
  with literal ASCII chars). This eliminates Python `SyntaxWarning: invalid
  escape sequence` warnings and encoding fragility.
  - `api.py`: `...` -> `...`, `--` -> `--`, `\u201c` -> `'`, `\u201d` -> `'`, etc.
  - `core/llm.py`: `->` -> `->`, angle quotes -> `'`, `\u201c` -> `'`, etc.
  - `core/prompt.py`: curly quotes -> `'`, `--` -> `--`, etc.
  - `core/context_inject.py`: `\u2714` -> `V`
  - `agent_runtime.py`, `sub_agent.py`, `memory.py`, `memory_prune.py`, etc.
  - `tests/test_memory_compression.py`: Updated assertion for `...` (3 chars)
    vs `...` (1 char ellipsis)
  - 25 files total touched, all 1000 tests pass.

## 2026-06-13 -- Double-Escaped Ellipsis Fix
### Fixed
- **Double-escaped `\\...` in api.py**: The file had `\\...` (literal backslash
  followed by three dots) in string literals that should have been `...` (three
  dots). This is a separate issue from the `\u2026` Unicode ellipsis -- just
  a Python escaping error. Fixed 19 occurrences across api.py.
### Fixed
- **`retry.py` now uses config constants for timeouts**: `_request_with_retry()`
  previously hardcoded `timeout=(10, 120)` while `config.py` defined
  `HTTP_CONNECT_TIMEOUT=30` and `HTTP_READ_TIMEOUT=120` as dead constants.
  Now imports and uses `(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)` from
  `core.config`, increasing connect timeout from 10s -> 30s. This fixes
  `Read timed out (read timeout=10)` errors on slow/congested networks
  and keeps the timeout in sync with `bootstrap.py`'s session config.

## 2026-06-12 -- Semantic Cache + Multi-Provider Fallback
### Added
- **Semantic response cache** (`tools/semantic_cache.py`): New module implementing
  an in-memory semantic cache using the shared SentenceTransformer model. Caches
  non-tool-call LLM responses keyed by cosine similarity (threshold: 0.92) of the
  last user message embedding. Bounded to 128 entries with 1-hour TTL. Integrated
  into `call_llm()` in `api.py` -- cache lookup before API call, storage after
  successful plain-text response. Expected 15-25% cost reduction with zero quality
  risk. Stats tracked on `_TOOL_CONTEXT._semantic_cache_stats`.
- **Multi-provider fallback chain** (`api.py` + `core/config.py`): `call_llm()`
  now supports automatic failover on 429/5xx errors. Configured via
  `ProviderDefaults.fallback_providers` (tuple). DeepSeek defaults to
  `("claude",)`. Each fallback tries with its own API key/URL/model. Provider-
  specific params stripped from fallback payloads. Improves availability to
  ~99.95%. Only active for non-streaming calls.
- **`fallback_providers` field** added to `ProviderDefaults` dataclass in
  `core/config.py`.
- **`_get_fallback_api_key()`** helper in `api.py` for resolving provider-specific
  API keys from environment variables.
### Tests
- All 1147 existing tests pass with zero regressions.

## 2026-06-12 -- Flash->Pro Handoff + Expanded Action Keywords
### Added
- **Flash->Pro handoff** (`core/llm.py`): When Flash (read-only) completes its
  codebase exploration phase after using tools (`turn_count > 1`), it now hands
  off to Pro with full capabilities. Injects a transient handoff message carrying
  Flash's analysis for Pro to act on. Pure knowledge questions (turn_count == 1,
  Flash answered without tools) return directly -- no unnecessary handoff.
  Previously the handoff was removed for unconditionally forcing Pro to "execute
  write tools" even on pure read tasks; the new version lets Pro determine
  whether code changes are actually needed.
- **Expanded action keywords** (`api.py`): `_ROUTE_ACTION_KEYWORDS` now includes
  `improve`, `enhance`, `correct`, `rework`, `overhaul`, `adjust`, `tweak`,
  `polish`, `strengthen`, `harden`, `clean up`, `tidy up`, `extend`, `expand`,
  `simplify`, `optimize` -- words that imply code modifications without explicitly
  saying "write" or "edit". These now route directly to Pro instead of being
  misclassified as simple/read-only.
- **Tests** (`tests/test_routing_efficiency.py`): +20 tests (88 total):
  15 new keyword classification tests, 5 turn plan/handoff state tests.
  All 139 broader suite tests pass with zero regressions.

## 2026-06-12 -- Knowledge Confidence Scale + Web Search Nudge
### Added
- **Knowledge Confidence Scale** (`core/prompt.py`): Added self-assessment
  instructions to the system prompt (1-10 confidence scale). Agent is required
  to rate its confidence before answering knowledge questions and use
  `web_search` when confidence < 7/10.
- **Confidence web search nudge** (`core/context_inject.py`):
  `_inject_confidence_web_search_nudge()` monitors conversation for low-confidence
  patterns: (a) 3+ consecutive search misses with no successful results,
  (b) 2+ consecutive tool failures, (c) 6+ read-only turns. Injects a
  gentle nudge to use `web_search`. 4-turn cooldown to avoid nagging.
- **Tests** (`tests/test_confidence_nudge.py`): 15 test cases covering all
  three trigger conditions, cooldown, edge cases (empty messages, malformed
  JSON, mixed patterns), and verified no-regression on 83 existing tests.

### Fixed
- **UnboundLocalError** (`core/context_inject.py:1192`): `data` was undefined
  when `json.loads()` raised an exception in
  `_inject_confidence_web_search_nudge()`. Initialized `data = None` before
  the try block and `data = {}` in the except handler.
- **Break killed failure/miss counting** (`core/context_inject.py:1224`):
  The `break` on encountering a productive assistant turn stopped the entire
  reverse-iteration loop, preventing tool failure and search miss counting
  for messages before the break. Replaced with `_stopped_read_only` flag that
  only stops read-only turn counting while allowing the loop to continue for
  failure/miss tracking.

## 2026-06-12 -- Flash/Pro Routing Fix & Model Indicator
### Fixed
- **`_compute_complexity()` historical-message poisoning** (`api.py`): The function
  accumulated ALL historical user messages going backwards until 2000 chars. The
  first message of a session (e.g. "build a web app") contained action keywords
  that poisoned every subsequent classification -- no simple prompt could ever
  route to Flash after a complex first message. Fix: only the **last** user
  message is examined; older history is ignored.
- **Cache lifecycle bug** (`api.py`): `_compute_complexity()` cached by `id(messages)`,
  but the messages list is the same Python object for the entire session, so the
  first "complex" result was cached permanently and routing never re-evaluated.
  Fix: cache key now includes `hash(last_user_content)` so it changes on each
  turn.
### Added
- **Visual model indicator** (`api.py`): `_emit_model_tag()` emits `[? Flash]` or
  `[[BRAIN] Pro]` via `on_token` at the start of every API response, visible directly
  in the Electron app output stream.

## 2026-06-11 -- ACI Upgrades: Read-Before-Edit, Syntax Validation, Empty-Output, Dangerous Command Detection
### Added
- **Read-before-edit enforcement** (`tools/file_ops.py`): `write_file` and `_apply_single_edit`
  now reject writes/edits to .py files not yet `read_file`'d this session (tracked via
  `_READ_FILES` set). New file creation is exempt. Prevents hallucinated overwrites of
  unseen files. (SWE-agent / Claude Code pattern)
- **Syntax validation gate** (`tools/file_ops.py`): `_validate_python_syntax()` runs
  `compile()` on .py file content before any write/edit is applied. Catches SyntaxErrors
  with line pointer before they persist to disk. Non-.py files skipped. (SWE-agent linter pattern)
- **Explicit empty-output messages** (`tools/shell_ops.py`): Shell commands that exit 0 with
  no stdout/stderr now return `"Command completed successfully (no output)."` instead of
  empty string. Eliminates ambiguous silence. (SWE-agent ACI pattern)
- **Dangerous command detection** (`tools/shell_ops.py`): `_check_dangerous_command()` scans
  for 9 patterns (`rm -rf`, `git push --force`, `sudo`, `chmod 777`, `dd`, `mkfs`,
  raw disk redirect, `format`). Blocked by default; requires `force=True` to bypass.
- **Search result overflow hint** (`tools/shell_ops.py`): When 200-result cap is hit,
  shows narrowing guidance: "use a more specific pattern, subdirectory path, or find_symbol."
  (SWE-agent pattern)
- **Per-result size budget** (`memory/memory_prune.py`): `_TOOL_RESULT_MAX_CHARS` (8000)
  hard-truncates individual tool results during compression, with offset guidance.
### Changed
- **ACI prompt rules** (`core/prompt.py`): Added "Read-Before-Edit & Verify-After-Change
  (ACI guardrails)" and "Plan-before-Edit Enforcement" sections to the immutable system
  prompt. Covers all new guardrails: read-first, verify-after, file-scoped commands,
  empty-output meaning, search caps, dangerous commands, plan-first workflow.
- **Stronger post-edit verification** (`core/context_inject.py`): `_inject_post_edit_verification()`
  now fires whenever new files are modified since last check, in addition to the 6-turn
  periodic cycle. Catches immediate post-edit verification needs.
### Reason
Research across SWE-agent (NeurIPS 2024), Claude Code architecture, OpenAI Codex best
practices, and Plan-then-Execute papers showed the single most impactful factor for
coding agent accuracy is harness design (10-27 pt swing on SWE-bench). The 5 highest-impact
ACI patterns were all missing: read-before-edit, linter-in-edit, explicit empty-output,
search narrowing hints, and file-scoped command guidance. All now implemented.

## 2026-06-11 -- Windows fork prep: requirements.txt refresh, WINDOWS_INSTALL.md
### Added
- **WINDOWS_INSTALL.md**: Comprehensive Windows 11 install guide with prerequisites,
  manual setup steps, launch instructions, keyboard shortcuts, running tests, and
  a full troubleshooting section (Store Python, Defender exclusions, Electron white
  screen, proxy, C++ build tools, PATH length).
### Changed
- **requirements.txt**: Refreshed with Windows-specific comments. Added
  `pytest-timeout>=2.0` (prevents hanging tests). Documented system dependencies
  (ripgrep, Node.js, git, Python) with winget install commands. Added Windows notes
  about PyTorch CPU-only option and Defender exclusions.
- **README.md**: Added reference to WINDOWS_INSTALL.md for Windows users.
### Tests
- 1027 passed, 10 failed (all pre-existing), 34 errors (Win32 teardown PermissionError).

## 2026-06-11 -- First tool call: HF Hub warmup encode + sys.executable warmup
### Fixed
- **HF Hub download interrupts first tool call**: After model preload completes in
  bootstrap, added a warmup `model.encode("warmup")` call to trigger any lazy
  initialization (tokenizer downloads, HF Hub auth warnings). This ensures all
  SentenceTransformer setup happens during bootstrap, not during the first tool call
  where it can interfere with concurrent subprocess tool execution.
- **sys.executable warmup**: The bootstrap warmup thread only called `cmd.exe`, but
  tool calls (read_file, run_shell) spawn `python.exe` subprocesses. Added a
  `subprocess.run([sys.executable, "-c", "print"])` warmup to absorb the antivirus
  filter-driver cost for the Python executable separately from cmd.exe.

## 2026-06-11 -- First tool call hang: daemon-thread subprocess warmup + preload timeout fix
### Fixed
- **First tool call hangs on Windows (run_shell stuck)**: Two root causes fixed in
  `core/bootstrap.py`:
  1. `_warmup_thread_io` only warmed file I/O, not `subprocess.Popen`. On Windows, the
     first `CreateProcess` from a daemon thread triggers fresh antivirus filter-driver
     scans. Added a `cmd.exe /c rem` invocation inside the warmup daemon thread.
  2. The embedding model preload (`_sem_preload`) started late in bootstrap (after slow
     `build_symbol_index` + `set_lsp_root`) and only waited 30s. On a cold HF cache
     (first-ever run), the model download (~90 MB) would still be in progress when the
     first tool call dispatched to a daemon thread -- and the concurrent network I/O from
     the preload thread interfered with tool thread startup. Fix: start `_sem_preload`
     EARLY (before the slow scans), wait at the END with timeout=120s (matching
     `_SEM_MODEL_TIMEOUT`). The slow scans now overlap with the model download.

## 2026-06-11 -- Windows tool freeze fixes (bash quoting + CREATE_NEW_PROCESS_GROUP removal + startup warmup)
### Fixed
- **run_shell freeze on Windows**: Bash path `C:\Program Files\Git\bin\bash.exe` was unquoted
  in the wrapper at line 241, and the command was double-wrapped in bash (line 319-327).
  Fix: quoted the bash path, bypassed the double wrapping via `if _WINDOWS and False`.
- **CREATE_NEW_PROCESS_GROUP removed**: Removed `subprocess.CREATE_NEW_PROCESS_GROUP` from all
  subprocess spawns (`_run_shell`, `_run_tests`, `_verify`, `lsp.py`, `mcp_client.py`,
  `file_ops.py`). The flag is unnecessary (taskkill /T works without process groups) and may
  trigger EDR/antivirus behavioral analysis on first invocation, causing ~15-60s freezes.
  Only `CREATE_NO_WINDOW` is kept to prevent conhost.exe window flash.
- **Startup warmup for antivirus**: `core/bootstrap.py` now runs `cmd.exe /c rem` during
  `init_session()` to warm up cmd.exe/conhost.exe before the first user prompt. This absorbs
  any first-call EDR scan delay during startup rather than on the first tool call.
- **read_file hangs on Windows**: `tools/file_ops.py` line 340: `_worker.py` subprocess hangs
  at `open()` on some Windows 11 systems (antivirus filter driver). Bypassed via `if False:`
  -- all reads now use `_read_file_direct()` in-process.

## 2026-06-11 -- Windows subprocess hardening (run_shell hang + process bomb fix)
### Fixed
- **run_shell hangs on Windows**: Replaced ALL `proc.communicate()` calls in `_run_shell`,
  `_run_tests`, and `_verify` with read threads (`_stream_reader`) + shared `_communicate_windows()`
  helper using `threading.Timer` watchdog that calls `taskkill /F /T`. `proc.communicate()` on
  Windows uses `WaitForSingleObject` which can hang forever in kernel I/O (antivirus hooks,
  filter drivers). The kill-timer approach escapes via OS-level process tree termination.
- **Process bomb (thousands of base.exe)**: `main.js` now throttles backend restarts to max
  3 within 30s with exponential backoff (1.5s -> 3s -> 6s). Previously, each crash triggered
  an unconditional restart after 1.5s, causing runaway process multiplication.
- **conhost.exe per command**: Added `subprocess.CREATE_NO_WINDOW` to `creationflags` in ALL
  subprocess spawns (`_run_shell`, `_run_tests`, `_verify`, `lsp.py`, `mcp_client.py`) so
  shell subprocesses no longer spawn Windows Console Host instances.
- **Shutdown cleanup**: `window-all-closed`, `before-quit`, and `settings:restartBackend`
  now use `taskkill /F /T /PID` on Windows instead of `proc.kill()` (which only kills the
  immediate process, leaving child trees orphaned).
- **Timeout handler fix**: In `_run_tests` and `_verify`, the `TimeoutExpired` handler no
  longer calls `proc.communicate()` on Windows (the process is already killed by taskkill,
  and calling `communicate()` on a dead process is safe but we avoid it for safety).
- **`_stream_reader` hardening**: Now catches `OSError`/`ValueError`/`BrokenPipeError`
  (pipe breaks when process is killed externally) and safely closes the stream in `finally`.

### Changed Files
- `tools/shell_ops.py` -- Added `_communicate_windows()` shared helper; refactored
  `_run_shell`, `_run_tests`, `_verify` to use it; added `CREATE_NO_WINDOW` everywhere;
  hardened `_stream_reader`; fixed timeout handlers
- `tools/lsp.py` -- Added `CREATE_NO_WINDOW` to LSP server subprocess
- `tools/mcp_client.py` -- Added `CREATE_NO_WINDOW` to MCP server subprocess
- `mini_agent_electron/main.js` -- Restart throttle (max 3/30s + backoff); tree-kill on shutdown

## 2026-06-08 -- Windows setup.bat hardening
### Fixed
- **Node.js version check**: Now requires Node >= 22 (not just any version).
  Electron 42 bundles Node 22 internally; older host Node fails at build time.
- **npm version check**: Now requires npm >= 9 (vite 8 needs it).
- **Removed `--silent` from npm commands**: Errors during Electron binary download
  (~100 MB from GitHub) were completely hidden. Output is now visible.
- **Post-install verification**: Checks that `node_modules\electron\dist\electron.exe`
  exists and can run `--version`. Catches broken/corrupted downloads.
- **Broken node_modules cleanup**: Detects when `node_modules\` exists but the
  Electron binary is missing (previous failed install) and removes it.
- **Troubleshooting guidance**: Added ELECTRON_MIRROR, proxy config, npm cache
  clean, and VC++ redistributable hints to the npm install failure path.
- **Build error visibility**: Removed `--silent` from `npm run build`; expanded
  failure message with debug commands and npm cache fix hints.

## 2026-06-03 (evening) -- Code Audit: Injection, Import, and Data-Loss Fixes
### Fixed
- **Injection flag lifecycle**: 4 flags reset in `run_agent_turn()` (per user message)
  moved to `bootstrap.init_session()` (per session). One-time injections now
  properly run once per session, not once per message. (llm.py, bootstrap.py)
- **Duplicate failure pattern warning**: removed redundant direct call in
  `run_agent_turn()` phase 3; `_tool_execution_phase()` already handles it. (llm.py)
- **Startup context role mismatch**: session.py used `"system"` role for startup
  context; standardized on `"user"` to match bootstrap.py. (session.py)
- **Data loss in stale tool result compression**: context_inject now saves
  `_original_content` before shrinking tool results; memory_prune restores it
  for accurate content-aware compression. (context_inject.py, memory_prune.py)
### Changed
- **Removed build_startup_context re-export from config.py**. Importers now
  get it directly from prompt.py. (config.py, server.py, tests/test_smoke.py)
- **Eliminated fake tool call hack in _inject_experience_context**. New
  `build_experience_context_from_text()` in failure_learning.py accepts plain
  text with proper keyword extraction and scoring. (context_inject.py,
  tools/failure_learning.py)
- **Updated run_agent_turn docstring**: accurately describes message-count-based
  reminder injection. (llm.py)
### Reason
Code audit of startup/shutdown/prompt/injection architecture found 7 issues:
2 critical (flag lifecycle, duplicate injection), 3 medium (role inconsistency,
compression data loss, import spaghetti), 2 low (misleading docstring, fake
tool call hack). All fixed; 71 tests pass.

## 2026-06-03 (afternoon) -- STATE.txt Injection & Population
### Added
- `_inject_state_context()` in context_inject.py -- reads STATE.txt once per session
- `_state_txt_injected` flag on AgentContext (tools/__init__.py), reset in llm.py
- 6 tests for STATE.txt injection (test_agent_self_tracking.py, 35 total)
### Changed
- STATE.txt populated with full architecture map (module inventory, decisions, known issues)
- HANDOFF.md updated with session context

## 2026-06-03 (morning) -- Agent Self-Tracking System
### Added
- `STATE.txt` -- architecture decisions, module map, known issues
- `HANDOFF.md` -- session handoff for continuity across restarts
- `CHANGELOG.md` -- structured self-modification audit trail
- `test_agent_self_tracking.py` -- 29 tests for self-tracking system
### Changed
- `README.md` -- added "Agent Self-Modification" section
- `.mini_agent.rules` -- added self-review cycle, HANDOFF.md/CHANGELOG.md references
- `context_inject.py` -- added `_inject_handoff_context()` for session startup
- `memory.py` -- added `write_handoff()` and `read_handoff()` helpers
- `tools/__init__.py` -- added `_handoff_injected` flag on AgentContext
- `llm.py` -- reset `_handoff_injected` flag per session
- `README.md` -- added "Agent Self-Modification" section for human collaborators
- `.mini_agent.rules` -- added self-review prompt, HANDOFF.md reference
- `context_inject.py` -- inject HANDOFF.md at session startup
- `memory.py` -- added `write_handoff()` and `read_handoff()` helpers
### Reason
Research across 16+ self-modifying agent repos (AgentOS, claude-code-thyself, selfmodel, claude-super-evolution) showed consensus: agents need STATE.txt (architecture map), HANDOFF.md (session continuity), and CHANGELOG.md (self-mod audit trail). mini_agent had none.

## 2026-05-24 -- Code Audit: Deduplication & Separation of Concerns
### Changed
- `tools/__init__.py` -- split ToolResult -> `tools/result.py`, error hints -> `tools/error_hints.py`
- `config.py` -- removed `_start_windows_tunnel()` side effect from `load()`
- `bootstrap.py` -- added tunnel call after config load
### Reason
Code audit findings: (1) `tools/__init__.py` was too large at ~1500 lines, (2) config loading had hidden side effects. Moved tunnel to bootstrap where side effects are expected.

## 2026-05-23 -- Self-Learning System
### Added
- `failure_learning.py` -- FailurePatternStore (SQLite), SelfCritique, MistakeNotebook
- `test_failure_learning.py` -- 28 tests
### Reason
Agent was repeating the same mistakes across sessions. Implemented MPR/VIGIL-inspired failure fingerprinting -> pattern clustering -> fix distillation.

## 2026-05-22 -- Edit File Safety
### Changed
- `tools/file_ops.py` -- 6 `edit_file` improvements: quote normalization, unicode whitespace, read-before-edit enforcement, indentation preservation, confidence scoring, line-ending normalization
### Reason
`edit_file` was the #1 source of tool failures. Each improvement addresses a specific failure pattern observed in production use.

## 2026-05-20 -- SWE-bench Evaluation
### Added
- `eval/swebench_runner.py` -- SWE-bench Lite prediction pipeline
- `eval/agent.py` -- SWE-bench agent wrapper
- `test_benchmarks.py` -- local eval + SWE-bench tests
### Reason
Industry-standard benchmarking for coding agents. Validates tool-use and code-fix capabilities.

## 2026-05-18 -- Context Injection Refactor
### Changed
- `context_inject.py` -- extracted from `llm.py` (per-turn injection logic)
- `llm.py` -- slimmer orchestrator, imports context injection
### Reason
`llm.py` was growing too large. Per-turn context logic (scratchpad, git diff, orchestration, circuit breaker) is a separate concern from turn orchestration.
