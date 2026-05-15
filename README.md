# mini_agent

A coding agent powered by DeepSeek V4 Pro with **44 tools**. Terminal REPL, Textual TUI, or Electron desktop app. SQLite-backed memory with cross-session project knowledge.

## Features

### Core
- **44 tools**: file operations, shell commands, search, git, web search, semantic search, symbol lookup, test running, LSP integration (pylsp), MCP client for external tool servers, read_image (GPT-4o vision), and more
- **Two models**: orchestrator uses DeepSeek V4 Pro; sub-agents use Flash (cheaper, faster workers). Separate API keys supported via `SUB_AGENT_API_KEY`
- **Streaming**: token-by-token responses with live tool output
- **Safety**: workspace isolation, destructive command guard, overwrite protection (opt-in), file reservation system preventing cross-agent write collisions

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
- **Electron desktop app** (`electron_app/`) — JSON-RPC bridge with token streaming

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
allow_overwrites = false
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
# 817 tests in ~13 seconds
```

## Architecture

```
mini_agent.py        Terminal REPL entry point
tui.py               Textual TUI (AgentWorker, themes, tree, diff preview)
electron_app/        Electron desktop app (main.js, preload.cjs, index.html)
electron_bridge.py   JSON-RPC bridge for Electron (stdin/stdout)
llm.py               LLM turn orchestration, circuit breaker, tool piping/grouping
api.py               API calls (call_deepseek, incremental message cleaning cache)
prompt.py            System prompt + .mini_agent.rules injection
config.py            AgentConfig (.env + TOML + env + CLI priority)
memory.py            SQLite conversation store + pruning + project_knowledge table
safety.py            Read/Write safety gates + diff preview
interject.py         Thread-safe user interjection queue
terminal.py          ANSI colour helpers
retry.py             HTTP retry with jitter and exponential backoff
stream.py            SSE stream parser
agent_runtime.py     Sub-agent lifecycle, file reservations, inboxes, subscriptions, snapshots
sub_agent.py         Sub-agent loop with turn budget, pruning, streaming, heartbeats
tools/
  __init__.py        Tool dispatch, cache, JSON repair, FILE_RESERVATIONS
  schema.py          TOOLS definitions (44 tools)
  file_ops.py        read/write/edit/list/info — cross-agent collision detection
  shell_ops.py       run_shell, search_files, run_tests, git, task_status, verify
  search_ops.py      find_symbol, find_usages, semantic_search, web_search
  agent_ops.py       spawn/status/collect/message/read/extend/handoff/inbox/subscribe/cancel
  agent_messages.py  AgentMessage, 9 message types, validation, routing
  agent_patterns.py  fan_out, fan_in, pipeline, barrier, scatter_gather
  lsp.py             LSP client — pylsp integration, 4 tools
  mcp_client.py      MCP client — stdio JSON-RPC, tool discovery
  _json_rpc_shared.py  Shared subprocess management for LSP and MCP clients
tests/
  test_*.py          40 test files, 817 tests
```

## License

MIT
