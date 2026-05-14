# mini_agent

A coding agent powered by DeepSeek V4 Pro with 28+ tools. Runs as a terminal REPL or a Textual TUI.

## Features

- **29+ tools**: file operations, shell commands, search, git, web search, semantic search, symbol lookup, multi-agent delegation with 9 message types, fan-out/fan-in/pipeline/barrier/scatter-gather patterns, read_image (GPT-4o vision), MCP client for external tool servers, test running, and more
- **MCP support**: discover tools from external MCP servers at startup (stdio JSON-RPC), configured via `[[mcp_server]]` TOML blocks
- **Eval harness**: YAML task format, 8 checker types, binary scoring, temp workspace copies — zero core changes
- **User interjection**: type while agent works — messages queued and surfaced at tool boundaries. Purple indicator in TUI. `/cancel` command to abort in-progress work.
- **Streaming**: token-by-token responses with live tool output
- **Two interfaces**: terminal REPL (`python mini_agent.py`) or rich TUI (`python tui.py`)
- **Safety layer**: workspace isolation, destructive command guard, overwrite protection (opt-in per config)
- **Multi-agent**: spawn sub-agents for parallel task execution; structured inter-agent messages with validation and routing; 5 patterns (fan_out, fan_in, pipeline, barrier, scatter_gather)
- **Memory**: SQLite-backed conversation store with token-aware pruning and incremental saves
- **~185 bugs fixed** across 20+ files in a single session via parallel multi-agent code review and remediation
- **717 tests**

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

# 4. Set your API keys
# DeepSeek (required) — get one at https://platform.deepseek.com
export DEEPSEEK_API_KEY="sk-your-key-here"
# OpenAI (for embeddings / alternate models) — https://platform.openai.com
export OPENAI_API_KEY="sk-your-key-here"
# Exa (web search) — https://exa.ai
export EXA_API_KEY="your-exa-key-here"
# or copy and edit the config file:
cp .mini_agent.toml.example .mini_agent.toml
# then edit .mini_agent.toml with your keys and MCP servers

# 5. Run
python tui.py          # Textual TUI (recommended)
# or
python mini_agent.py   # Terminal REPL
```

## Configuration

Settings are loaded from (in priority order):
1. CLI flags (e.g. `--stream`, `--quiet`)
2. Environment variables (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `EXA_API_KEY`, `AGENT_WORKSPACE`)
3. `.mini_agent.toml` in the workspace root

Copy `.mini_agent.toml.example` to `.mini_agent.toml` for local configuration.

### MCP Servers

Define external MCP servers in `.mini_agent.toml`:

```toml
[[mcp_server]]
name = "my-server"
command = "python"
args = ["-m", "my_mcp_server"]
```

Tools from each server are registered as `mcp/<server>/<tool>` and are available automatically at startup.

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
| `--help, -h` | Show help |

## Running Tests

```bash
python -m pytest
# 466 tests in ~6 seconds
```

## Architecture

```
mini_agent/
  mini_agent.py       Terminal REPL entry point
  tui.py              Textual TUI interface
  config.py           Configuration loading (TOML, env, CLI)
  llm.py              API calls, agent loop, circuit breaker
  safety.py           File read/write safety gates
  memory.py           SQLite-backed conversation store
  prompt.py           System prompt template
  stream.py           SSE stream parser
  retry.py            HTTP retry with jitter and exponential backoff
  sub_agent.py        Sub-agent execution
  agent_runtime.py    Sub-agent registry and lifecycle
  terminal.py         ANSI colour helpers
  tools/
    __init__.py       Tool dispatch, registration, JSON repair
    schema.py         Tool JSON schemas
    file_ops.py       read/write/edit/list/info/scratchpad/diff/restore/plan
    shell_ops.py      run_shell, search_files, run_tests, git, task_status, verify
    search_ops.py     find_symbol, find_usages, semantic_search, web_search, recall_turn
    agent_ops.py      spawn/status/collect/collect_any/message/read/extend/handoff/inbox/subscribe + read_image
    agent_messages.py AgentMessage, 9 message types, validation, routing
    agent_patterns.py fan_out, fan_in, pipeline, barrier, scatter_gather
    lsp.py            LSP client — JSON-RPC 2.0 over stdio, 4 tools
    mcp_client.py     MCP client — JSON-RPC over stdio, tool discovery at startup
  eval/
    __init__.py       Eval package
    runner.py         Task runner with temp workspace copies
    scorer.py         Binary scoring with 8 checker types
    metrics.py        Aggregate metrics
    tasks/            YAML task definitions
    fixtures/         Test fixtures for eval tasks
    reports/          Eval run output
```

## License

MIT
