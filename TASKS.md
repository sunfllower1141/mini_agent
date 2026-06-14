# Task-to-File Index

A curated guide mapping common development tasks to the relevant files in mini_agent.
Read before starting work to orient yourself in the codebase.

## Core System Changes

### Modify the System Prompt
- **`core/prompt.py`** -- System prompt assembly, personality rules, tool descriptions, startup context builder
- **`.mini_agent.rules`** -- Project conventions injected as prompt appendix

### Change Agent Loop / Turn Logic
- **`core/llm.py`** -- Main agent loop: turn management, tool dispatch, API calls, retry logic
- **`core/context_inject.py`** -- Per-turn context injection (handoff, state, scratchpad, git diff, circuit breaker, edit risk, pattern rules, compaction)

### Modify Configuration
- **`core/config.py`** -- TOML config loading, provider settings, environment variables, CLI args
- **`.mini_agent.toml`** or `~/.mini_agent/config.toml` -- User config files

### Session Bootstrap / Shutdown
- **`core/bootstrap.py`** -- Entry point, session init, workspace setup, cleanup, handoff auto-write
- **`core/safety.py`** -- Read/write safety gates (SafetyResult dataclass)

## Tools

### Add a New Tool
1. **`tools/schema.py`** -- Add tool definition to TOOLS list (name, description, parameters)
2. **`tools/__init__.py`** -- Add dispatch handler; re-export if used externally
3. **`tests/`** -- Write test for the new tool
4. **`README.md`** -- Document if user-facing

### Modify File Operations
- **`tools/file_ops.py`** -- read_file, write_file, edit_file, list_directory, file_info, write_scratchpad, diff
- **`tools/search_ops.py`** -- find_symbol, find_usages, search_files, web_search, semantic_search, recall_turn

### Modify Shell / Process Execution
- **`tools/shell_ops.py`** -- run_shell, run_tests, task_status, verify, git operations
- **`logging_setup.py`** -- Structured logging for tool calls

### Modify Agent Orchestration
- **`tools/agent_ops.py`** -- extend, cancel, wait, restore, session_stats, recall_turn, remember, read_image
- **`tools/agent_spawn.py`** -- spawn_agent, _spawn_one (sub-agent spawning)
- **`tools/agent_collect.py`** -- agent_status, collect_agent, collect_any (status & collection)
- **`tools/agent_messages.py`** -- Typed inter-agent messaging (handoff, fan-out/in, pipeline)
- **`tools/agent_patterns.py`** -- fan_out, fan_in, pipeline, barrier, scatter_gather
- **`agents/sub_agent.py`** -- Sub-agent engine, turn budget, pruning

### Modify Tool Context / Reservations
- **`tools/context.py`** -- AgentContext, _ContextProxy, _TOOL_CONTEXT, set_context
- **`tools/reservations.py`** -- File reservation system (per-agent file locks)
- **`tools/result.py`** -- ToolResult dataclass, JSON repair, error formatting

### Modify Planning / Task Tracking
- **`tools/agent_todos.py`** -- plan, plan_status, todo_write, todo_read, write_scratchpad
- **`tools/context.py`** -- _plan_steps, _plan_done, _plan_last_advanced_turn state
- **`memory/memory.py`** -- Plan persistence (set_plan, get_plan), HANDOFF.md plan export
- **`core/context_inject.py`** -- _inject_plan_status (per-turn plan injection, staleness detection)
- **`tools/file_ops.py`** -- _auto_advance_plan (auto-completes steps on write/edit)
- **`tools/skills.py`** -- "planning" skill group (todo_write, todo_read, plan, plan_status)

### Modify LSP Integration
- **`tools/lsp.py`** -- LSP client (pylsp integration for code intelligence)

### Modify MCP Client
- **`tools/mcp_client.py`** -- MCP client (stdio JSON-RPC tool discovery)

## Memory & Persistence

### Modify Memory Storage
- **`memory/memory.py`** -- SQLite-backed memory store, scratchpad, learnings, handoff, session summary, pruning

### Add/Modify Knowledge Entries
- **`memory/memory.py`** -- `save_knowledge()`, `get_top_knowledge()`, `search_knowledge()`

## Sub-Agent System

### Modify Sub-Agent Behavior
- **`agents/sub_agent.py`** -- Complete sub-agent engine
- **`agents/agent_runtime.py`** -- Sub-agent lifecycle manager, inboxes, runtime state
- **`agents/__init__.py`** -- Public API exports

### Add Agent Patterns
- **`tools/agent_patterns.py`** -- Pattern implementations
- **`tools/agent_ops.py`** -- Orchestration hooks

## Desktop App (Electron)

### Modify Electron Shell
- **`mini_agent_electron/main.js`** -- Electron main process, window management, lifecycle
- **`mini_agent_electron/preload.js`** -- IPC bridge between main and renderer
- **`mini_agent_electron/src/`** -- React renderer components

## Testing

### Add/Modify Tests
- **`tests/`** directory -- Unit tests using unittest.TestCase
- **`tests/`** subdirectories -- Tests with supporting files, fixtures, mock servers

### Test Infrastructure
- **`conftest.py`** -- Shared fixtures, mocks, test helpers
- **`pyproject.toml`** -- Pytest configuration

## Project Maintenance

### Update Tracking Files
- **`STATE.txt`** -- Architecture map: update Module Map and Active Decisions after changes
- **`HANDOFF.md`** -- Session handoff: auto-written at session end
- **`CHANGELOG.md`** -- Audit trail: log significant changes with date and reasoning
- **`TASKS.md`** -- This file: update when module structure changes

### Add Dependencies
- **`pyproject.toml`** -- Python dependencies
- **`package.json`** -- Electron (Node.js) dependencies

## Code Intelligence

### Use Codebase Map (auto-generated)
- **`core/codebase_map.py`** -- AST-based symbol extraction for startup context
- Agents see this map in their first-turn context. It automatically reflects
  current code structure -- no manual updates needed.
