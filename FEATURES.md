# mini_agent — Feature Reference

Quick reference of tool categories and counts. See README for overview.

## Tools (59+)

| Category | Tools |
|----------|-------|
| File Ops | read_file, write_file, edit_file, list_directory, file_info, write_scratchpad, diff, restore_file, plan, plan_status |
| Shell | run_shell, run_tests, git, task_status, verify, diagnose_failures |
| Search | find_symbol, find_usages, search_files, semantic_search, web_search, recall_turn, fetch_url, read_image |
| LSP | lsp_definition, lsp_references, lsp_hover, lsp_diagnostics |
| MCP | mcp_discover, mcp_call |
| Browser | browser_navigate, browser_snapshot, browser_click, browser_type, browser_screenshot, open_url |
| Multi-Agent | spawn_agent, agent_status, collect_agent, collect_any, agent_extend, agent_cancel, wait_for_agent |
| Patterns | fan_out, fan_in, pipeline, barrier, scatter_gather |
| Comms | agent_message, agent_read, agent_handoff, agent_inbox, agent_subscribe |
| Session | remember, init, session_stats, todo_write, todo_read, use_skill |

## Multi-Agent System

- **10 concurrent** sub-agents in background threads (configurable)
- **Turn budgets**: 25 turns default, extendable to 35, safety cap 200
- **Progress-based termination**: hung detection (300s no tool calls), error loop detection
- **Streaming snapshots**: 200-token granularity for live status
- **Reports**: written to `reports/<task_id>.md` per agent
- **Auto-prune**: sub-agents prune memory every 5 turns

## Memory & Learning

- SQLite conversation store with token-aware pruning
- Cross-session `project_knowledge` table
- `remember` tool for manual capture; auto-learn from failures
- Scratchpad persists across turns

## Interface

- **Electron** — desktop GUI in `mini_agent_electron/` with live streaming, tools/chat panels, slash commands

## Test Coverage

```
1,113 tests collected | 28 test files
Covering: file ops, shell, search, multi-agent, memory, UI, MCP, LSP, retry, stream, safety, config
```
