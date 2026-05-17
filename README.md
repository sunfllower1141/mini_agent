# mini_agent

A coding agent powered by DeepSeek V4 Pro with **63 tools**. Terminal REPL or Textual TUI. SQLite-backed memory with cross-session project knowledge. Headless browser automation via Playwright. Cross-platform: macOS, Linux, and Windows.

## Features

### Core
- **63 tools**: file operations, shell commands, search, git, web search, semantic search, symbol lookup, test running, LSP integration (pylsp), MCP client for external tool servers, headless browser automation (Playwright — navigate, snapshot, click, type, screenshot), read_image (GPT-4o vision), diff, verify, diagnose_failures, find_usages, restore_file, remember, init, open_url, wait_for_agent, agent_cancel, session_stats, recall_turn, fetch_url, and more
- **Two models**: orchestrator and sub-agents both use DeepSeek V4 Pro. Separate API keys supported via `SUB_AGENT_API_KEY` to isolate quota
- **Cross-platform**: macOS, Linux, and full Windows support — ANSI terminal, Git Bash/PowerShell/cmd.exe shell execution, LSP queue-based reader
- **Streaming**: token-by-token responses with live tool output
- **File reservations**: threading.Lock prevents cross-agent write collisions

### Multi-Agent
- **10 concurrent sub-agents** (configurable up to 15) running in background threads
- **5 coordination patterns**: fan_out, fan_in, pipeline, barrier, scatter_gather
- **9 inter-agent message types**: text, handoff.result, handoff.request, handoff.ack, status.heartbeat, status.error, coord.fan_out, coord.fan_in, coord.sync
- Sub-agents auto-prune memory every 5 turns to prevent API errors
- Streaming snapshots at 200-token granularity for real-time status

### Memory & Learning
- **SQLite conversation store** with token-aware pruning, progressive compression, and summarization
- **Project knowledge**: cross-session pattern memory — edit_file mismatches auto-captured, workspace tree cached, manual `remember` tool. Injected at session start
- **Inbox ring-buffer**: prevents memory leaks on long-running agents
- **Background test output**: persisted to DB, not discarded

### Interfaces
- **Textual TUI** (`python tui.py`) — rich terminal UI with themes, diff preview, file tree
- **Terminal REPL** (`python mini_agent.py`) — lightweight CLI

### Dev Tools
- **LSP integration**: definition, references, hover, diagnostics via pylsp (auto-started on first use)
- **MCP support**: discover tools from external MCP servers at startup (stdio JSON-RPC), configured via `[[mcp_server]]` TOML blocks
- **Eval harness**: YAML task format, 8 checker types, binary scoring, temp workspace copies
- **User interjection**: type while agent works — messages queued at tool boundaries. `/cancel` to abort

### Performance
- **Symbol index**: persisted to `.mini_agent_index.json`, mtime-gated, incremental reindex on writes
- **Semantic search**: embeddings-based code search with per-file mtime invalidation
- **Tool piping**: JSON parse short-circuit when no `_pipe` deps exist
- **Deque circuit breaker**: O(1) pop vs O(n) list pop(0)
- **Workspace tree cache**: skips `os.walk` on unchanged workspaces

## Quick Start

```bash
# 1. Clone
git clone https://github.com/GabrielMalone/mini_agent.git
cd mini_agent

# 2. Set up a virtual environment
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create a .env file with your API keys
cat > .env << 'EOF'
DEEPSEEK_API_KEY="sk-your-key-here"        # required — https://platform.deepseek.com
SUB_AGENT_API_KEY="sk-your-sub-key-here"   # optional — separate key for sub-agents
OPENAI_API_KEY="sk-your-key-here"          # optional — https://platform.openai.com
EXA_API_KEY="your-exa-key-here"            # optional — https://exa.ai
EOF

# 5. Run
python tui.py          # Textual TUI (recommended)
# or
python mini_agent.py   # Terminal REPL
```

## Configuration

Priority: CLI flags > environment variables > `.env` file > `.mini_agent.toml` > defaults.

### `.env` (API keys — gitignored)
```
DEEPSEEK_API_KEY="sk-..."       # required
SUB_AGENT_API_KEY="sk-..."      # optional — isolates sub-agent quota
OPENAI_API_KEY="sk-..."         # optional — GPT-4o vision
EXA_API_KEY="..."               # optional — web search
```

### `.mini_agent.toml` (advanced settings)
```toml
model = "deepseek-v4-pro"
sub_agent_model = "deepseek-v4-flash"
sub_agent_max_concurrent = 10
sub_agent_max_turns = 25
max_messages = 500
max_tokens = 200_000
temperature = 0.0
frequency_penalty = 0.3
presence_penalty = 0.1
# stop_sequences = ["```"]
# response_format = "json_object"
allow_overwrites = false
stream = true
stream = false

[[mcp_server]]
name = "my-server"
command = "python"
args = ["-m", "my_mcp_server"]
```

### CLI Flags

| Flag | Description |
|------|-------------|
| `--workspace PATH` | Set workspace root (default: cwd) |
| `--stream` | Stream responses token-by-token |
| `--quiet` | Suppress tool execution logs |
| `--no-color` | Disable ANSI colours |
| `--approve` | Ask confirmation before write/destructive tools |
| `--allow-overwrites` | Allow overwriting existing files |
| `--unrestricted` | Remove workspace boundary checks |
| `--timeout SECONDS` | Max seconds for shell commands (default 60, max 300) |
| `-h, --help` | Show help |

## Running Tests

```bash
python -m pytest
# 1,083 tests in ~23 seconds
```

## Architecture

```
mini_agent.py        Terminal REPL entry point
tui.py               Textual TUI (AgentWorker, themes, tree, diff preview)
llm.py               LLM turn orchestration, circuit breaker, tool piping/grouping
api.py               API calls (call_deepseek), message cleaning cache, complexity routing
prompt.py            System prompt + .mini_agent.rules injection
config.py            AgentConfig (.env + TOML + env + CLI), build_startup_context
memory.py            SQLite conversation store + pruning + project_knowledge table
safety.py            Read/Write safety gates + diff preview + _safe_resolve
interject.py         Thread-safe user interjection queue
terminal.py          ANSI colour helpers
retry.py             HTTP retry with jitter + exponential backoff (408, 429, 5xx)
stream.py            SSE stream parser
agent_runtime.py     Sub-agent lifecycle, file reservations, inboxes, subscriptions, snapshots
sub_agent.py         Sub-agent loop with turn budget, pruning, streaming, heartbeats
tools/
  __init__.py        Tool dispatch, cache, JSON repair, FILE_RESERVATIONS,
                     auto-learn failure patterns, post-edit LSP auto-verify
  schema.py          TOOLS definitions (63 tools)
  file_ops.py        read/write/edit/list/info — cross-agent collision detection,
                     cascading fuzzy whitespace match (3-pass: exact→trailing→indent)
  shell_ops.py       run_shell, search_files, run_tests, git, task_status, verify,
                     diagnose_failures, cross-platform shell + python detection
  search_ops.py      find_symbol, find_usages, semantic_search, web_search,
                     recall_turn, fetch_url
  agent_ops.py       spawn/status/collect/message/read/extend/handoff/inbox/subscribe/cancel + remember
  agent_messages.py  AgentMessage, 9 message types, validation, routing
  agent_patterns.py  fan_out, fan_in, pipeline, barrier, scatter_gather
  browser_ops.py     Headless browser automation — navigate, snapshot, click, type,
                     screenshot via Playwright Chromium
  lsp.py             LSP client — pylsp integration, 4 tools (definition, references, hover, diagnostics),
                     cross-platform (select + queue-based reader)
  mcp_client.py      MCP client — stdio JSON-RPC, tool discovery at startup
  _json_rpc_shared.py  Shared subprocess drain_stderr + is_subprocess_connected
tests/
  test_*.py          34 test files, 1,083 tests
```

## Key State

### Models & API
- Orchestrator: DeepSeek V4 Pro. Sub-agents: DeepSeek V4 Pro.
- Separate API keys via `DEEPSEEK_API_KEY` and `SUB_AGENT_API_KEY` env vars.
- `max_tokens = 200k`, `max_messages = 500`. Priority: CLI > env > `.env` > TOML > default.
- `temperature` (0.0), `frequency_penalty` (0.3), `presence_penalty` (0.1), `stop_sequences`, `response_format` configurable via TOML.
- Prompt caching: static identity content first for DeepSeek cache hits (~2,000 tokens never change).
- Multi-model routing: `routing_model` config routes simple prompts to cheaper model (disabled by default).

### Multi-Agent System
- Max 10 concurrent sub-agents (configurable via `sub_agent_max_concurrent` in TOML).
- Auto-wake: completions injected as user messages before `input()` blocks — no missed finishes.
- Sub-agents auto-extend when ≤3 turns remaining and making progress (max 35 turns).
- Stale agent GC: threads from previous sessions cleaned up on startup.
- Sub-agents auto-prune memory every 5 turns when >20 messages to avoid 400 errors.
- Streaming snapshots at 200-token granularity.
- 5 coordination patterns: fan_out, fan_in, pipeline, barrier, scatter_gather.
- 9 inter-agent message types with validation, routing (direct, subscription, broadcast).
- FILE_RESERVATIONS with `threading.Lock` prevents cross-agent write collisions.

### Memory & Learning
- SQLite-backed conversation store with token-aware pruning and progressive compression.
- `project_knowledge` table persists learnings across sessions in same workspace.
- `remember` tool for manual capture; `edit_file` mismatches auto-captured.
- Workspace tree cached in project_knowledge (mtime-gated, skips `os.walk` on restart).
- Inbox ring-buffer cap at 1000 messages prevents memory leaks.
- Background test output persisted to `test_output` table.
- Auto-learn: `_FAILURE_PATTERNS` dict injects recovery hints on repeated tool failures.

### Performance
- Symbol index persisted to `.mini_agent_index.json` (mtime-gated, incremental reindex).
- Semantic search with per-file mtime invalidation.
- Incremental message cleaning cache (survives across turns via `id(messages)` key).
- `_pipe` dependency detection short-circuits before JSON parse when not present.
- Circuit breaker uses `deque.popleft()` for O(1) window management.

### Cross-Platform
- Windows ANSI support via `SetConsoleMode` (Windows 10+).
- Cross-platform shell: Git Bash → PowerShell → `cmd.exe` fallback chain.
- LSP client uses queue-based reader on Windows (select works on pipes only on Unix).
- `_safe_resolve` in safety.py handles Windows non-existent path resolution.
- Python detection: `py -3` → `python3` → `python` fallback chain.
- Windows destructive patterns: `del /f`, `diskpart`, `rmdir /s`, `rd /s`, `reg delete`.
- HTTP 408 (Request Timeout) added to retryable statuses.

### Interfaces
- Textual TUI with terminal REPL.
- LSP integration via pylsp (auto-started on first use).
- MCP client discovers external tools at startup via stdio JSON-RPC.
- User interjection queue with `/cancel` support.
- Streaming token-by-token with live tool output.
