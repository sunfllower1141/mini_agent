# Codebase Audit — mini_agent

> Date: 2026-05-22  
> Scope: All source modules, tools, tests (excluding security)  
> Tests: 1,083 reported in FEATURES.md (auto-generated)

---

## 1. Architecture Overview

```
bootstrap.py         — init_session(): wires config → safety → memory → tools → LSP → MCP → session
    │
    ├── config.py    — AgentConfig, ProviderDefaults, TOML/ENV/CLI loading
    ├── safety.py    — ReadSafetyGate, WriteSafetyGate, diff preview generator
    ├── memory.py    — SQLite persistence, token estimation, compression, pruning, summarization
    ├── prompt.py    — build_system_prompt, build_startup_context, hierarchical rules
    │
    ├── api.py       — call_llm(), _strip_orphaned_tool_calls(), rate limiter, provider dispatch
    ├── retry.py     — _request_with_retry(): jittered exponential backoff
    ├── stream.py    — _parse_stream(): SSE parsing, tool call accumulation, reasoning content
    │
    ├── llm.py       — run_agent_turn(): circuit breaker, context injection, turn summaries
    ├── sub_agent.py — run_sub_agent(): isolated agent loop, depth control, safety caps
    ├── agent_runtime.py — SubAgentResult, AgentRuntime: thread-safe sub-agent registry
    │
    ├── tools/       — Tool dispatch, 50+ implementations
    │   ├── __init__.py — ToolResult, ToolCache, file reservations, agent context
    │   ├── schema.py  — TOOLS list (parameter schemas for LLM)
    │   ├── skills.py  — Skill gating: CORE_TOOLS + SKILLS + get_active_tools()
    │   ├── file_ops.py — read/write/edit/list/info/backup/plan
    │   ├── shell_ops.py — run_shell/task_status/run_tests/git/search
    │   ├── search_ops.py — find_symbol/find_usages/semantic_search/web_search
    │   ├── agent_ops.py — spawn/status/collect/cancel/extend sub-agents
    │   ├── agent_patterns.py — fan_out/fan_in/pipeline/barrier/scatter_gather
    │   ├── agent_messages.py — AgentMessage, typed inboxes, subscription routing
    │   ├── lsp.py     — pylsp integration (definition/references/hover/diagnostics)
    │   ├── mcp_client.py — MCP stdio JSON-RPC client
    │   ├── browser_ops.py — Playwright headless browser
    │   └── _json_rpc_shared.py — JSON-RPC shared utilities
    │
    ├── interject.py — Thread-safe user interjection queue
    ├── session.py   — Session save/switch/delete
    ├── terminal.py  — ANSI terminal helpers
    │
    ├── tui_pt.py    — prompt-toolkit TUI frontend
    ├── bootstrap.py — Session initialization
    │
    └── test_*.py    — 25+ test files covering all modules
```

**Layering**: Clear three-layer architecture:
1. **Configuration layer**: `config.py` → `bootstrap.py`
2. **Engine layer**: `api.py` → `retry.py` → `stream.py` → `llm.py` → `sub_agent.py`
3. **Tool layer**: `tools/*.py` with dispatch in `tools/__init__.py`

**Circular dependency prevention**: `api.py` was extracted from `llm.py` specifically to break the cycle `llm → tools → agent_ops → sub_agent → llm`. Successful — `api.py` imports only `config`, `retry`, `stream`, `tools.schema`, `tools.skills`.

---

## 2. Code Quality Assessment

### 2.1 Strengths

| Area | Assessment |
|------|-----------|
| **Type hints** | ~90% of functions have full type annotations. `from __future__ import annotations` used throughout. |
| **Docstrings** | Every module and public function has a docstring. `tools/__init__.py` even includes the pattern for adding new tools. |
| **Named constants** | Consistently extracted from magic numbers. `memory.py` has a dedicated `# Named constants` block. `sub_agent.py` has `_SHARED_CONTEXT_CAP`, `_TASK_CAP`, etc. |
| **Error handling** | `ToolResult` returned instead of raising exceptions. `_request_with_retry` has jittered backoff. Tool failures include a `hint` field for self-correction. |
| **Thread safety** | `AgentRuntime` uses `threading.Lock` for all shared state. File reservations use `_FILE_RESERVATIONS_LOCK`. `ChatBuffer` in TUI is thread-safe. `_INTERJECTIONS` uses `threading.Lock`. |
| **Cache invalidation** | `_clean_messages_cache` cleared on provider change. `_total_tokens` accumulator detects in-place mutation. `_FILE_CACHE` uses mtime. `get_pending_results` tracks `_seen_completions`. |
| **Performance patterns** | Forward tool-call name map (O(n²)→O(n)). Pre-built error hints (O(1) lookup). Symbol index persisted to JSON. Dispatch signatures cached at registration. |

### 2.2 Weaknesses & Risks

| Issue | Location | Severity | Detail |
|-------|----------|----------|--------|
| **Global mutable state** | `tools/__init__.py` | **Medium** | `_TOOL_DISPATCH`, `_TOOL_CACHE`, `_MODIFIED_FILES`, `_FILE_RESERVATIONS`, `_TOOL_CONTEXT`, `_TASK_REGISTRY`, `_AGENT_RUNTIME`, `_scratchpad_injected`, `_git_diff_injected` — all module-level globals. Cross-session contamination if `reset_skills()` or `init_session()` misses one. |
| **Global state in llm.py** | `llm.py` | **Medium** | `_scratchpad_injected`, `_git_diff_injected` — module-level booleans. If multiple sessions run in the same process (test reuse), these won't reset. |
| **Thread safety gaps** | `api.py` | **Medium** | `_clean_messages_cache` and `_LLM_SEMAPHORE` are module-level. `_clean_messages_cache` is a plain dict accessed without a lock. Two sub-agents calling `call_llm` concurrently could race on the cache. |
| **Thread safety gaps** | `sub_agent.py` | **Medium** | `_scratchpad` is a local variable, but `_TOOL_CONTEXT._plan_steps` / `_plan_done` are restored via `_restore_plan()` without a lock. If a sub-agent crashes mid-restore, parent state is corrupted. |
| **Thread safety gaps** | `memory.py` | **Low** | `_total_tokens` uses module-level `_ACCUM_COUNT`, `_ACCUM_TOTAL`, `_ACCUM_LIST_ID` — global accumulators shared across threads. Two agents pruning simultaneously will corrupt estimates. |
| **Thread safety gaps** | `tui_pt.py` | **Low** | `_log_error_trace` and `_log_tool_error` write to the same file without a lock — interleaved writes on concurrent errors. |
| **Memory leak risk** | `tools/__init__.py` | **Low** | `_TOOL_CACHE` is a plain dict — never cleared except at turn boundaries by the parent. If a sub-agent runs many turns, its tool cache grows unbounded. |
| **TUI imports** | `tui_pt.py` | **High** | Imports from `config` include `resolve_workspace`, `init_session`, `parse_args`, `list_sessions`, `switch_session`, `delete_session`, `build_startup_context` — but these don't exist in `config.py`. This would crash at startup. Likely a refactor where config.py functions were moved to bootstrap.py/session.py but the import wasn't updated. |
| **Broken import chain** | `llm.py` | **Medium** | Imports `call_deepseek` from `api` — but `call_deepseek` was renamed to `call_llm` during the refactor. Also imports `clear_api_cache` from `api` but `clear_api_cache` may not exist. |
| **Injected context bloat** | `prompt.py` | **Medium** | System prompt with full tool descriptions + header + provider note + hierarchical rules + git context can exceed 5,000 tokens before any user message. Sub-agents rebuild this from scratch. |
| **Circular import risk** | `tools/agent_ops.py` | **Low** | Late import: `from sub_agent import run_sub_agent` inside `_spawn_one()`. Works but fragile — if `sub_agent.py` ever imports from `tools.agent_ops`, this breaks. |
| **Sub-agent import hack** | `sub_agent.py` | **Medium** | Monkey-patches stderr redirect (`sys.stderr = _StringIO`). If any tool or import writes to `sys.stderr` during startup (before the redirect), it goes to the wrong place. |

### 2.3 Architecture Observations

| Observation | Detail |
|------------|--------|
| **Skill system works well** | 11 core tools + 9 skill gates = ~55 tools loaded lazily. Significantly reduces prompt size for simple tasks. Well-documented architecture in `skills.py`. |
| **Provider abstraction is clean** | `PROVIDER_DEFAULTS` registry in config — adding a new provider is one dict entry + env var names. `_build_payload` handles per-provider differences. |
| **Sub-agent isolation is good** | Fresh message list, own tool cache, own `_MODIFIED_FILES`, own scratchpad, own plan state. Plan isolation with save/restore is clean. |
| **Error trace logging is excellent** | `_log_error_trace` captures last 20 messages, traceback, timestamps. Best-in-class debugging support for production issues. |
| **File reservation system** | Prevention of cross-agent write collisions. `reserve_file` / `release_file` / `release_all_files` API. |
| **3-pass fuzzy edit** | Whitespace-tolerant matching cascade is a novel solution to a common LLM editing problem. |

---

## 3. Test Coverage

**Total**: 1,083 tests (per FEATURES.md auto-audit).

| Area | Key Test Files | Coverage |
|------|---------------|----------|
| **API** | `test_api.py` | call_llm, provider dispatch, message cleaning |
| **Config** | `test_config.py` | Provider detection, env var overrides, TOML loading |
| **Memory** | `test_memory.py`, `test_memory_compression.py`, `test_memory_internals.py`, `test_memory_summarize.py` | SQLite persistence, pruning, compression, summarization |
| **Safety** | `test_safety.py`, `test_safety_diff.py` | Workspace boundary checks, diff preview |
| **Sub-agents** | `test_sub_agent.py` | Spawn/collect/status/extend/cancel |
| **Agent patterns** | `test_agent_patterns.py`, `test_agent_patterns_extended.py` | fan_out, fan_in, pipeline, barrier, scatter_gather |
| **Integration** | `test_integration.py` | End-to-end agent loop |
| **Tools** | `test_tools.py` | All tool implementations |
| **File ops** | `test_file_ops_extended.py` | write/edit/read edge cases |
| **Smoke** | `test_smoke.py` | Quick startup sanity check |
| **Resilience** | `test_resilience.py` | Retry, API errors, network drops |
| **Stream** | `test_parse_stream.py` | SSE parsing edge cases |
| **Fuzzy match** | `test_fuzzy_match.py` | 3-pass edit_file matching |
| **JSON-RPC** | `test_json_rpc_shared.py` | MCP JSON-RPC utilities |
| **Skills** | `test_skills.py` | Skill gating, tool availability |
| **Routing** | `test_routing.py` | Model routing (simple vs complex prompts) |
| **Schema** | `test_schema.py` | Tool schema validation |
| **Post-edit verify** | `test_post_edit_verify.py` | Auto-verify after edits |
| **Interject** | `test_interject.py` | User interjection queue |
| **Git** | `test_git.py` | Git tool operations |
| **Agent messages** | `test_agent_messages.py` | Inter-agent communication |
| **Agent loop** | `test_agent_loop.py` | Agent turn orchestration |

### Coverage gaps

| Gap | Impact | Suggested |
|-----|--------|-----------|
| **No TUI tests** | TUI (`tui_pt.py`, `tui.py`) has zero tests. UI regressions go undetected. | At minimum: smoke test that import succeeds |
| **No MCP tests** | `mcp_client.py` untested | Mock stdio server for unit tests |
| **No browser tests** | `browser_ops.py` untested | Mock Playwright for unit tests |
| **Thread safety not tested** | No concurrent tests for `AgentRuntime`, `_clean_messages_cache`, `_total_tokens` | Multi-threaded stress tests |
| **Sub-agent isolation** | No test verifying plan isolation survives sub-agent crash in `_restore_plan()` | Inject exception at midpoint |
| **Provider dispatch** | No test for `_build_payload` with xAI or ollama (only DeepSeek/Claude) | Add test for each provider |

---

## 4. Key Modules Analysis

### 4.1 `config.py` — Configuration
- **Good**: `ProviderDefaults` dataclass, `PROVIDER_DEFAULTS` registry, 3-phase loading (TOML → env → CLI), Windows SSH tunnel auto-start
- **Needs attention**: `_start_windows_tunnel` uses hardcoded IP/user/key — should be configurable. The class-level parse cache comment is cut off at end of file.
- **File truncated**: The file cuts off mid-sentence at the TOML parse cache comment.

### 4.2 `api.py` — LLM API
- **Good**: Semaphore-based rate limiter, incremental message cleaning cache, `_strip_orphaned_tool_calls`, provider-specific parameter building
- **Needs attention**: `_clean_messages_cache` is not thread-safe. `_compute_complexity` is heuristic — no way for user to override. `call_deepseek` is imported in `llm.py` but renamed to `call_llm`.

### 4.3 `llm.py` — Turn Orchestration
- **Good**: Circuit breaker with configurable window/threshold, context injection helpers (scratchpad, git diff, orchestration status, interjections), turn summaries, progress nudges
- **Needs attention**: Global `_scratchpad_injected` / `_git_diff_injected` flags are not reset between sessions in same process. Orchestration context injection computes `pending_fp` with `r.success` — if `r` is None this crashes.

### 4.4 `sub_agent.py` — Sub-Agent Engine
- **Good**: Depth control, safety caps (200 turns), hung detection (300s), error loop detection (3x), auto-extension, report writing with smart inline preview
- **Needs attention**: `_restore_plan()` is called multiple times in error paths but only the last one matters — earlier calls are wasted. Report cleanup (`_MAX_REPORTS=20`) uses `os.listdir` without a lock.

### 4.5 `memory.py` — SQLite Persistence
- **Good**: Token estimation with incremental accumulator, progressive compression (read_file/search_files/run_shell aware), conversation summarization, mid-story recovery
- **Needs attention**: `_total_tokens` global accumulators are not thread-safe. `_compress_tool_results` modifies messages in-place (returns the SAME list object) — callers that expect a new list will corrupt other references.

### 4.6 `agent_runtime.py` — Sub-Agent Registry
- **Good**: Thread-safe `AgentRuntime` with `_gc_stale()` cleanup, `mark_abandoned` for zombie threads, `get_pending_results` with `_seen_completions` dedup
- **Needs attention**: Inbox ring-buffer cap is implemented in `agent_ops.py` (`_AGENT_MSGS_MAX=1000`) but NOT in `AgentRuntime.inboxes` — those grow unbounded.

### 4.7 `tools/skills.py` — Skill Gating
- **Good**: Clean architecture — `CORE_TOOLS`, `SKILLS`, `_active_skills`, `get_active_tools()`. Well-documented.
- **Minor**: `activate_skill` returns `(bool, str)` tuple but `_use_skill` calls it — the tuple unpacking is fragile if the tuple format changes.

---

## 5. Performance Profile

| Aspect | Current | Bottleneck? |
|--------|---------|-------------|
| **System prompt size** | ~3,000-5,000 tokens (full tools + header + rules + git) | **Yes** — every turn sends 50+ tool schemas to the API, even when only 3-4 are needed. Skill system helps but the full schema is still built in `prompt.py`. |
| **Message cleaning** | Incremental (O(append)) per turn | No — amortized O(1) per new message |
| **Tool cache** | Per-turn, cleared at start of each turn | No — ~O(1) hit rate for repeated reads |
| **Token estimation** | Incremental accumulator, O(1) per new message | No |
| **Memory compression** | O(n) scan building forward name map, O(1) per compressed result | No — optimized from O(n²) |
| **SSE streaming** | Single-threaded, blocking on `iter_lines()` | **Yes** — server stall blocks the entire agent. No per-line timeout. |
| **Sub-agent context** | ~2,000-3,000 tokens (subset of tools, no full schema) | No — significantly leaner than parent context |
| **LSP initialization** | ~2s cold start via pylsp | No — one-time cost |
| **Semantic search** | ~9s cold start (sentence-transformers load) | **Maybe** — preloaded in background, but blocks first semantic_search call if not done |
| **SQLite VACUUM** | Only when freelist > 1,000 pages | No — well-gated to avoid per-save overhead |

---

## 6. Recommendations

### Critical (fix before next release)

1. **Fix TUI imports in `tui_pt.py`**: Imports `resolve_workspace`, `init_session`, `parse_args`, etc. from `config.py` — these don't exist there. They were moved to `bootstrap.py` / `session.py`. The TUI will crash on startup.

2. **Fix `llm.py` imports from `api`**: Imports `call_deepseek` (doesn't exist — renamed to `call_llm`) and `clear_api_cache` (may not exist — doesn't appear in `api.py`).

3. **Thread-safety for `_clean_messages_cache`**: Add `threading.Lock` to the message cleaning cache in `api.py`. Two concurrent sub-agents race on this dict.

4. **Thread-safety for `_total_tokens` accumulators**: Add thread-local storage or a lock around `_ACCUM_COUNT`, `_ACCUM_TOTAL`, `_ACCUM_LIST_ID` in `memory.py`.

### High Priority

5. **Global state reset test**: Add a test that calls `init_session()` twice and verifies no cross-session contamination (e.g., `_scratchpad_injected` = False on second call).

6. **SSE timeout**: Add per-line timeout to `response.iter_lines()` in `stream.py`. Currently a server stall blocks indefinitely.

7. **Inbox memory cap**: Apply the ring-buffer cap (1,000) to `AgentRuntime.inboxes` — currently each sub-agent's inbox grows unbounded.

8. **TUI tests**: At minimum, a smoke test that `tui_pt.py` imports cleanly and all referenced symbols exist.

### Medium Priority

9. **Parse findings recovery**: `SubAgentResult._parse_findings()` regex is fragile — markdown table rows with embedded `|` (e.g., in code blocks) will break parsing. Add escaping logic.

10. **Sub-agent depth context**: The depth-guard message in `sub_agent.py` (lines 130-148) is written in system prompt — could be more dynamic (e.g., list exactly which tools are blocked).

11. **Error trace log lock**: `_log_error_trace` and `_log_tool_error` in `tui_pt.py` write to `error_traces.log` without a lock. Add `threading.Lock` for concurrent error logging.

12. **Remove dead code**: `terminal.py` may contain code only used by the deprecated `mini_agent.py` and `tui.py` CLIs. Worth auditing and removing.

### Low Priority

13. **_config.py truncation**: The TOML parse cache comment at end of `config.py` is cut off mid-sentence. File may have been truncated during an edit.

14. **Windows tunnel hardcoding**: `_WINDOWS_TUNNEL_HOST`, `_WINDOWS_TUNNEL_USER`, `_WINDOWS_TUNNEL_KEY` are hardcoded — should be configurable via `.mini_agent.toml`.

15. **Report cleanup race**: `_write_report` in `sub_agent.py` does `os.listdir` + `os.remove` without a lock — concurrent sub-agents finishing simultaneously could race on cleanup.

---

## 7. Summary

**Overall**: The codebase is well-structured with clean layering, good type hints, comprehensive docstrings, and thoughtful performance optimizations. The multi-agent system is sophisticated with proper isolation, safety caps, and coordination patterns.

**State management**: The single biggest risk is global mutable state across modules. While each global is individually correct, there's no systematic reset mechanism — a process hosting multiple sessions would see cross-contamination.

**Thread safety**: The rate limiter semaphore and AgentRuntime lock are correct, but `_clean_messages_cache`, `_total_tokens` accumulators, and `error_traces.log` are unprotected.

**Test coverage**: Excellent breadth (25+ test files, 1,083 tests) with notable gaps in TUI, MCP, browser ops, and multi-threaded stress testing.

**The fix we verified earlier** (`_strip_orphaned_tool_calls` in `api.py`) is solid and resolved real 400 errors from orphaned tool calls after memory pruning.
