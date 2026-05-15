# Audit Report: Principles & Foundations

> Generated from prompt.py, config.py, .mini_agent.rules, STATE.txt  
> Plus empirical verification (test count, file layout)

---

## (a) Design Principles — Complete Inventory

Every explicit design rule/principle found across all four sources, deduplicated.

### Code Structure & Style

| # | Principle | Source(s) |
|---|-----------|-----------|
| 1 | Keep modules small and single-purpose | prompt.py, rules |
| 2 | Prefer explicit control flow over hidden magic | prompt.py, rules |
| 3 | No circular imports | prompt.py, rules |
| 4 | No global mutable state **unless unavoidable** | prompt.py, rules |
| 5 | No magic numbers; use named UPPER_CASE constants | prompt.py, rules, config.py |
| 6 | All tool results must be structured dataclasses — never raw exceptions | prompt.py, rules |
| 7 | Every file write must go through the safety layer | prompt.py |
| 8 | Every new feature needs at least one test | prompt.py, rules |
| 9 | Run relevant tests after every implementation step | prompt.py |
| 10 | If tests fail, stop and diagnose before making additional changes | prompt.py, rules |
| 11 | Do not stack multiple speculative fixes before verifying results | prompt.py |
| 12 | Prefer small incremental edits over large rewrites | prompt.py, rules |
| 13 | Prefer readable code over clever code | prompt.py, rules |
| 14 | Add type hints for **all public functions** | prompt.py, rules |
| 15 | Use `from __future__ import annotations` in every module | rules |
| 16 | Prefer dataclasses for structured data | rules, config.py |
| 17 | Use clear names; avoid abbreviations | prompt.py |
| 18 | Reuse existing abstractions before introducing new ones | prompt.py, rules |
| 19 | Avoid duplicate logic; extract shared behavior carefully | prompt.py |
| 20 | Do not create new subsystems unless existing architecture cannot handle the need | prompt.py, rules |
| 21 | If a change touches **more than 3 core files**, pause and explain the plan first | prompt.py, rules |
| 22 | Prefer TODO comments over partially implemented systems | prompt.py |

### Process & Workflow

| # | Principle | Source(s) |
|---|-----------|-----------|
| 23 | Before coding, briefly explain what will change and why | prompt.py |
| 24 | After coding, summarize what changed, what tests ran, and results | prompt.py |
| 25 | Before major changes, consult STATE.txt for current architecture | prompt.py, rules |
| 26 | Update STATE.txt after every completed change | prompt.py, rules |
| 27 | Before proposing/starting a new feature, **confirm the plan with the user** | prompt.py |
| 28 | Use **plan** to declare numbered steps before multi-step work | prompt.py |
| 29 | After each step, call **plan_status** to mark it done | prompt.py |
| 30 | Use **write_scratchpad** for cross-turn working memory | prompt.py |
| 31 | If not updated scratchpad in 4 turns, stop and update | prompt.py |
| 32 | Structure scratchpad with ## Plan, ## Progress, ## Decisions, ## Open Questions | prompt.py |

### Communication & Tool Use

| # | Principle | Source(s) |
|---|-----------|-----------|
| 33 | Be direct and concise | prompt.py |
| 34 | Prefer normal answers when no tool is needed | prompt.py |
| 35 | Choose tools by capability, not by hardcoded names | prompt.py |
| 36 | Do not bypass the safety layer for writes/commands/destructive actions | prompt.py |
| 37 | **Batch independent tool calls in parallel** (never serialize unnecessarily) | prompt.py, rules |
| 38 | State plan in 1–3 sentences before making tool calls for non-trivial tasks | prompt.py, rules |
| 39 | Use `find_symbol` for symbol lookups (not grep/search_files) | prompt.py, rules |
| 40 | Use LSP tools (lsp_definition/references/hover/diagnostics) for code intelligence | prompt.py, rules |
| 41 | `edit_file` requires byte-for-byte match of old_string | prompt.py |
| 42 | Check tool result `hint` field on failure | prompt.py |
| 43 | Use `run_tests` with optional `path` for targeted test runs | prompt.py |
| 44 | Tool cache: read-only tools cached within a turn (don't cache manually) | prompt.py |
| 45 | Tool piping via `_pipe` field for dependent tool calls | prompt.py |

### Sub-Agent & Orchestration

| # | Principle | Source(s) |
|---|-----------|-----------|
| 46 | **Use sub-agents proactively** — decompose aggressively | prompt.py |
| 47 | Task touches 3+ independent files → one sub-agent per file | prompt.py |
| 48 | Task has distinct phases → pipeline | prompt.py |
| 49 | Task has N similar items (N>2) → scatter_gather | prompt.py |
| 50 | Single focused change → do it yourself | prompt.py |
| 51 | Fan-out then fan-in when gathering results | prompt.py |
| 52 | Sub-agents run **DeepSeek V4 Flash** (not Pro) | prompt.py, STATE.txt |
| 53 | Once you spawn sub-agents, you are an **orchestrator, not a worker** | prompt.py |
| 54 | Poll sub-agents every turn; extend proactively after ~10 turns | prompt.py |
| 55 | Only cancel at 35 turns exhausted or 3+ repeated errors | prompt.py |
| 56 | LLM generation is slow — wait minutes, not seconds | prompt.py |
| 57 | Track sub-agent task IDs, what each does, collection status in scratchpad | prompt.py |
| 58 | Check your inbox every turn while orchestrating | prompt.py |
| 59 | Prefer push (inbox) over poll (status) for structured handoffs; use both | prompt.py |
| 60 | 5 coordination patterns: fan_out, fan_in, pipeline, barrier, scatter_gather | prompt.py, ST |

### Config & Startup

| # | Principle | Source(s) |
|---|-----------|-----------|
| 61 | Priority: CLI > env var > .env > TOML > default | config.py, STATE.txt |
| 62 | Type-checked TOML parsing against _TOML_SCHEMA | config.py |
| 63 | Caching with mtime-gated invalidation | config.py, STATE.txt |

### Testing

| # | Principle | Source(s) |
|---|-----------|-----------|
| 64 | Test files at root: `test_<module>.py` | rules |
| 65 | Use `unittest.TestCase` | rules |
| 66 | Use `tempfile.mkdtemp` for test workspaces; clean up in tearDown | rules |

### Architecture (module separation)

| # | Principle | Source(s) |
|---|-----------|-----------|
| 67 | Keep prompts, execution logic, tools, and memory in separate modules | prompt.py |

---

## (b) Features Described in STATE.txt — Should Exist

Everything STATE.txt claims the codebase has or does:

### Core Runtime
- [ ] Terminal REPL entry point (`mini_agent.py`)
- [ ] Textual TUI (AgentWorker, themes, tree, diff preview)
- [ ] Electron desktop app (`electron_app/`)
- [ ] JSON-RPC bridge for Electron (`electron_bridge.py`)
- [ ] LLM turn orchestration (`llm.py`)
- [ ] Circuit breaker with `deque.popleft()`
- [ ] Tool piping / grouping
- [ ] API calls (`call_deepseek` in `api.py`)
- [ ] Incremental message cleaning cache (survives across turns via `id(messages)`)
- [ ] System prompt + `.mini_agent.rules` injection
- [ ] AgentConfig (`.env` + TOML + env + CLI)
- [ ] `build_startup_context` with tree, last 50 STATE.txt lines, git log, knowledge
- [ ] SQLite conversation store + pruning + `project_knowledge` table
- [ ] Read/Write safety gates + diff preview
- [ ] Thread-safe user interjection queue (`interject.py`)
- [ ] ANSI colour helpers (`terminal.py`)
- [ ] HTTP retry with jitter and exponential backoff (`retry.py`)
- [ ] SSE stream parser (`stream.py`)
- [ ] Sub-agent lifecycle (`agent_runtime.py`) — file reservations, inboxes, subscriptions, snapshots
- [ ] Sub-agent loop with turn budget, pruning, streaming, Flash model (`sub_agent.py`)

### Tools (44 total)
- [ ] Tool dispatch, cache, JSON repair, FILE_RESERVATIONS (`tools/__init__.py`)
- [ ] TOOLS definitions (`tools/schema.py`)
- [ ] File ops: read/write/edit/list/info — cross-agent collision detection
- [ ] Shell ops: run_shell, search_files, run_tests, git, task_status, verify
- [ ] Search ops: find_symbol, find_usages, semantic_search, web_search, recall_turn
- [ ] Agent ops: spawn/status/collect/message/read/extend/handoff/inbox/subscribe/cancel + remember
- [ ] Agent messages: 9 message types, validation, routing
- [ ] Agent patterns: fan_out, fan_in, pipeline, barrier, scatter_gather
- [ ] LSP client: pylsp integration, 4 tools (defn, refs, hover, diagnostics)
- [ ] MCP client: stdio JSON-RPC, tool discovery at startup
- [ ] Shared: `drain_stderr` + `is_subprocess_connected`

### Multi-Agent System
- [ ] Max 10 concurrent sub-agents (configurable)
- [ ] Auto-prune memory every 5 turns when >20 messages
- [ ] Streaming snapshots at 200-token granularity
- [ ] 5 coordination patterns
- [ ] 9 inter-agent message types with validation, routing
- [ ] FILE_RESERVATIONS with `threading.Lock`

### Persistence & Learning
- [ ] `project_knowledge` table for cross-session learning
- [ ] `remember` tool for manual capture
- [ ] `edit_file` mismatches auto-captured
- [ ] Workspace tree cached in `project_knowledge` (mtime-gated, skips os.walk)
- [ ] Inbox ring-buffer cap at 1000 messages
- [ ] Background test output persisted to `test_output` table

### Indexes & Performance
- [ ] Symbol index persisted to `.mini_agent_index.json` (mtime-gated, incremental)
- [ ] Semantic search with per-file mtime invalidation
- [ ] `_pipe` dependency detection short-circuits before JSON parse
- [ ] Circuit breaker uses `deque.popleft()` for O(1) window

### Interfaces
- [ ] Textual TUI with terminal REPL and Electron
- [ ] LSP integration via pylsp (auto-started)
- [ ] MCP client — discovers external tools at startup
- [ ] User interjection queue with `/cancel`
- [ ] Streaming token-by-token with live tool output

### Tests
- [ ] 40 test files (claim: inconsistent — see contradictions)
- [ ] 817 tests (claim: inconsistent — see contradictions)

---

## (c) Contradictions Between Sources

### 1. Test Count — TRIPLE CONFLICT

| Source | Claimed | Empirically Verified |
|--------|---------|---------------------|
| `.mini_agent.rules` | "562 tests currently, all must pass" | **827 tests** (per `pytest --collect-only`) |
| `STATE.txt` | "817 tests, 40 test files" | **_Also wrong_** — counts are stale by ~10 tests |
| **Reality** | — | 27 `test_*.py` at root + 1 in `tests/` = **28 test files, 827 tests** |

Severity: **High**. Both documentation sources are stale. The rules file is badly outdated (off by 265 tests).

### 2. Max Concurrent Sub-Agents — CONFLICT

| Source | Value |
|--------|-------|
| `prompt.py` (`_STATIC_PROMPT`) | "Max 5 concurrent sub-agents" |
| `STATE.txt` | "Max 10 concurrent sub-agents (configurable via `sub_agent_max_concurrent` in TOML)" |
| `config.py` | `DEFAULT_SUB_AGENT_MAX_CONCURRENT = 10` |

Severity: **Medium**. The prompt instructs agents to use max 5, but the config default is 10 and STATE.txt documents 10. The prompt text needs updating to match the config.

### 3. Sub-Agent Starting Turns — CONFLICT

| Source | Value |
|--------|-------|
| `prompt.py` | "start with 15 turns but can go up to 35" |
| `config.py` | `DEFAULT_SUB_AGENT_MAX_TURNS = 25` |
| `STATE.txt` | Implicitly 25 (matches config) |

Severity: **Medium**. If `default` = 25, agents start at 25, not 15. The prompt's instructions about turn budgets (extend at ~10, cancel at 35) are calibrated for a 15-start baseline and are inconsistent with the 25-default config.

### 4. Module Map — INCOMPLETE

`.mini_agent.rules` lists these modules:
- prompt.py, config.py, llm.py, memory.py, safety.py, tui.py, tools/, STATE.txt

STATE.txt additionally lists:
- mini_agent.py, electron_app/, electron_bridge.py, api.py, interject.py, terminal.py, retry.py, stream.py, agent_runtime.py, sub_agent.py
- tools/schema.py, tools/agent_messages.py, tools/agent_patterns.py, tools/lsp.py, tools/mcp_client.py, tools/_json_rpc_shared.py

Severity: **Low** (the rules doc says it's a supplement). But the rules' module map should be updated to reflect the full codebase for new developers.

### 5. Test File Location — CONFLICT

| Source | Rule |
|--------|------|
| `.mini_agent.rules` | "Test files live at root: `test_<module>.py`" |
| **Reality** | 27 test files at root, **1** (`test_lsp.py`) lives in `tests/` subdirectory |

Severity: **Low**. Minor inconsistency; the LSP test is in a subdirectory for its mock server support files.

### 6. Concurrent Agent Config Description

| Source | Detail |
|--------|--------|
| `prompt.py` | Hard limit of **5** (stated in behavioral rules) |
| `config.py` | Default **10**, configurable via TOML |
| Conflict | The agent runtime honors config.py's limit (10), but the system prompt tells agents to use max 5. An orchestrator reading the prompt will under-utilize the configured capacity. |

### Summary of Action Items

1. **Update `.mini_agent.rules`** test count from 562 → 827 (or remove hardcoded number)
2. **Update `STATE.txt`** test count from 817 → 827 and test files from 40 → 28
3. **Fix `prompt.py`** concurrency limit: change "Max 5" to "Max `sub_agent_max_concurrent` (default 10)"
4. **Fix `prompt.py`** turn budget: change "start with 15 turns" to "start with `sub_agent_max_turns` (default 25)"
5. **Add to rules module map**: agent_runtime.py, sub_agent.py, api.py, interject.py, terminal.py, retry.py, stream.py, plus tools sub-modules
6. **Either move `test_lsp.py` to root** or update the rules to allow tests/ subdirectory
