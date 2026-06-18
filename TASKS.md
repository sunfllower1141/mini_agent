# Task-to-File Mapping Index

Quick reference for which files to touch when making common changes.

## Core System Changes

Changes to the agent loop, system prompt, configuration, or context injection:

- `core/prompt.py` — system prompt assembly, personality rules, tool descriptions
- `core/llm.py` — turn orchestration, tool dispatch, API calls, storm-breaker
- `core/config.py` — TOML config loading, provider settings, CLI args
- `core/context_inject.py` — per-turn context injection (handoff, state, tasks, scratchpad, git diff, circuit breaker)
- `core/bootstrap.py` — session init, workspace setup, cleanup
- `core/safety.py` — read/write safety gates, workspace isolation
- `core/codebase_map.py` — AST-based symbol extraction for startup context
- `core/knowledge_graph.py` — entity-relationship graph (calls, imports, defs)
- `core/tree_sitter_parser.py` — multi-language source parsing (Python/JS/TS)
- `core/constants.py` — shared constants (no internal project imports)

## Tools

Adding, modifying, or removing tools:

- `tools/schema.py` — TOOLS definitions sent to LLM (all tool schemas)
- `tools/__init__.py` — tool dispatch, cache, ToolResult
- `tools/json_repair.py` — JSON repair and parse-error recovery
- `tools/memory_core.py` — persistent core memory (read/add/replace/remove)
- `tools/memory_consolidation.py` — memory consolidation and summarization
- `tools/reservations.py` — edit reservation system (reserve, release, list)
- `tools/file_ops.py` — read, write, edit, list, info, scratchpad, diff, plan
- `tools/shell_ops.py` — run_shell, search_files, run_tests, verify
- `tools/search_ops.py` — find_symbol, find_usages, web_search, semantic_search
- `tools/agent_ops.py` — extend, cancel, wait, restore, session_stats, recall_turn
- `tools/agent_spawn.py` — sub-agent spawning (spawn_agent)
- `tools/agent_collect.py` — sub-agent status & collection
- `tools/agent_messages.py` — typed inter-agent messaging, handoff
- `tools/agent_patterns.py` — fan_out, fan_in, pipeline, barrier, scatter_gather
- `tools/agent_todos.py` — plan, plan_status, todo tracking, scratchpad
- `tools/skills.py` — Hermes-style skill discovery and lazy loading
- `tools/lsp.py` — LSP client (pylsp integration)
- `tools/mcp_client.py` — MCP client (stdio JSON-RPC tool discovery)
- `tools/browser_ops.py` — Playwright headless browser automation
- `tools/desktop_ops.py` — desktop automation (macOS Atomacos, Windows UIA)
- `tools/macos_ops.py` — macOS-specific tools (apps, windows, clipboard, keys)
- `tools/semantic_cache.py` — semantic response cache (cosine-sim match)
- `tools/failure_learning.py` — failure pattern store, self-critique, experience context
- `tools/error_hints.py` — error hint generation and failure fingerprinting
- `tools/tool_graph.py` — tool transition graph for sequencing hints
- `tools/context.py` — AgentContext, _ContextProxy, tool context
- `tools/result.py` — ToolResult dataclass

## Memory & Persistence

Changes to conversation storage, pruning, or knowledge management:

- `memory/memory.py` — SQLite store: conversations, knowledge, scratchpad, handoff
- `memory/memory_prune.py` — content-aware compression, orphan stripping
- `memory/session.py` — session lifecycle and persistence
- `api.py` — LLM API calls, message cache, semantic cache integration
- `interject.py` — user interjection queue
- `logging_setup.py` — structured logging

## Testing

Changes to test infrastructure or new test additions:

- `tests/` — all test files (unittest.TestCase style, pytest runner)
- `conftest.py` — shared fixtures, mocks, test helpers
- `eval/` — evaluation harness (YAML tasks + SWE-bench runner)
- `Makefile` — test targets (test, test-slow, test-all, coverage)
