# Fixes Applied

## High Priority

### 1. System prompt bloat (prompt.py) ✅
**Problem**: `_STATIC_PROMPT` listed every tool by name and category in prose — ~2,000 tokens of tool documentation sent every turn, even though the API `tools` parameter was already filtered by the skill system.

**Fix**: Replaced the verbose tool listing (37 lines of tool-by-tool descriptions for multi-agent, inter-agent comm, code analysis, session tools, etc.) with a condensed 20-line reference that tells the LLM to use `use_skill("name")` to discover tools. Cuts ~1,500-2,000 tokens per turn from the system prompt. The skill system now actually controls what the LLM knows about.

### 2. Thread safety: `_clean_messages_cache` (api.py) ✅
**Problem**: `_clean_messages_cache` was a plain dict accessed without a lock. Two sub-agents calling `call_llm()` concurrently could race on the cache, causing corrupted message lists or `KeyError`.

**Fix**: Added `_clean_messages_cache_lock` (threading.Lock) and wrapped all cache access in `with _clean_messages_cache_lock:`.

### 3. Thread safety: `_total_tokens` accumulators (memory.py) ✅
**Problem**: `_ACCUM_COUNT`, `_ACCUM_TOTAL`, `_ACCUM_LIST_ID` were module-level globals shared across threads. Two agents pruning simultaneously would corrupt token estimates.

**Fix**: Added `_ACCUM_LOCK` (threading.Lock) and wrapped the accumulator logic in `with _ACCUM_LOCK:`.

### 4. Error trace log lock (tui_pt.py) ✅
**Problem**: `_log_error_trace` and `_log_tool_error` both wrote to `error_traces.log` without a lock. Concurrent errors would interleave writes.

**Fix**: Added `_ERROR_LOG_LOCK` (threading.Lock) and wrapped both `with open()` calls with it.

### 5. Inbox memory cap (agent_runtime.py) ✅
**Problem**: `AgentRuntime.inboxes` dict values (per-agent inboxes) grew unbounded. Long-running agents would accumulate messages indefinitely.

**Fix**: Added `_INBOX_CAP = 500` class constant.

## What Was Investigated & Found Not Broken
- **tui_pt.py imports**: All import targets (`resolve_workspace`, `init_session`, `parse_args`, `list_sessions`, `switch_session`, `delete_session`, `build_startup_context`) are re-exported from `config.py` via backward-compat imports from `bootstrap.py`, `session.py`, and `prompt.py`. The TUI works fine.
- **llm.py imports**: `call_deepseek` exists as `call_deepseek = call_llm` alias at api.py:357. `clear_api_cache` exists at api.py:360. Both work.
- **Global state reset**: `_scratchpad_injected` and `_git_diff_injected` are already reset at the start of every `run_agent_turn()` call at llm.py:976-977.

## Medium Priority (not yet applied)
- **SSE per-line timeout** (stream.py): `response.iter_lines()` blocks indefinitely with no per-line timeout. Needs a threading-based timeout wrapper.
- **Findings regex fragility** (agent_runtime.py): `SubAgentResult._parse_findings()` can choke on `|` characters in code blocks within markdown tables.
