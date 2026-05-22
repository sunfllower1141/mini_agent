# mini_agent

A coding agent with **59 tools** powered by DeepSeek, Claude, or xAI/Grok. Prompt-toolkit TUI or plain terminal REPL. SQLite-backed memory with cross-session project knowledge. Headless browser automation via Playwright. Multi-agent orchestration with 5 coordination patterns and inter-agent messaging. Lazy skill loading via `use_skill` gate. MCP (Model Context Protocol) integration. Error trace logging. Cross-platform: macOS, Linux, and Windows.

## Features

### Core
- **59 tools**: file operations (read/write/edit/list/info/scratchpad/diff/restore), shell commands (run_shell, run_tests, git, task_status), search (find_symbol, find_usages, search_files, semantic_search, web_search, recall_turn, fetch_url), planning (plan, plan_status, todo_write, todo_read), verification (verify, diagnose_failures), LSP integration via pylsp (definition, references, hover, diagnostics), MCP integration (mcp_discover, mcp_call), headless browser automation via Playwright (navigate, snapshot, click, type, screenshot, open_url), read_image (GPT-4o vision), skills (use_skill with lazy tool loading), session_stats, init, remember, and 17 multi-agent tools
- **Three LLM providers**: DeepSeek V4 Pro (default), Claude Sonnet 4.5, and xAI Grok 4.3 — switch via `API_PROVIDER` env var or `api_provider` in `.mini_agent.toml`. Separate API keys supported via `SUB_AGENT_API_KEY` to isolate sub-agent quota
- **Cross-platform**: macOS, Linux, and full Windows support — ANSI terminal, Git Bash/PowerShell/cmd.exe shell execution, LSP queue-based reader
- **Streaming**: token-by-token responses with live tool output
- **File reservations**: threading.Lock prevents cross-agent write collisions
- **3-pass fuzzy edit**: edit_file uses cascading whitespace-tolerant matching (exact → trailing-tolerant → indent-tolerant)
- **Error trace logging**: exceptions in the TUI chat window are captured with timestamps, tracebacks, and last 20 messages to `error_traces.log`
- **Skills system**: `use_skill` gate activates lazy-loaded tool sets (git, web, test, planning, agents, search, image, lsp, bootstrap) — tools loaded on first use to keep the base tool set lean
- **MCP integration**: Model Context Protocol client for stdio JSON-RPC tool discovery and invocation (`mcp_discover` + `mcp_call`), server config in `.mini_agent.toml [agent.mcp_servers]`

### Multi-Agent
- **10 concurrent sub-agents** (configurable via `sub_agent_max_concurrent` in TOML) running in background threads
- **Turn budget**: 25 turns default, extendable to 35, with a generous safety cap at 200 turns
- **Progress-based termination**: sub-agents self-govern via hung detection (300s no tool calls), error loop detection (3 consecutive identical failures)
- **Orchestrator sleep/wake**: `wait_for_agent` blocks with exponential backoff (1s→2s→4s→…→30s), waking on completion, hung detection, or inbox messages — zero tokens consumed while idle
- **Sub-agent reports**: results written to `reports/<task_id>.md` files (unique per agent) instead of bloating inline context
- **Plan isolation**: sub-agents get a clean plan state; parent plan is restored on exit — no cross-agent corruption
- **5 coordination patterns**: fan_out, fan_in, pipeline, barrier, scatter_gather
- **9 inter-agent message types**: text, handoff.result, handoff.request, handoff.ack, status.heartbeat, status.error, coord.fan_out, coord.fan_in, coord.sync
- Sub-agents auto-prune memory every 5 turns to prevent API errors
- Streaming snapshots at 200-token granularity for real-time status

### Memory & Learning
- **SQLite conversation store** with token-aware pruning, progressive compression, and summarization
- **Project knowledge**: cross-session pattern memory — edit_file mismatches auto-captured, workspace tree cached, manual `remember` tool. Injected at session start
- **Inbox ring-buffer**: prevents memory leaks on long-running agents (capped at 1000 messages)
- **Background test output**: persisted to DB, not discarded

### Interfaces
- **Prompt-toolkit TUI** (`python tui_pt.py`) — the **current** main interface: live token streaming, tools & thinking panel, chat panel, slash commands (`/help`, `/clear`, `/stats`, `/session`, `/export`, `/workspace`, `/shell`, `/init`), git status, session tracking, rounded borders, sub-agent output window
- **Textual TUI** (`python tui.py`) — **deprecated**, kept as backup: rich terminal UI with 9 themes, live token streaming, tool log with diffs, sub-agent panes, agent tree
- **Terminal REPL** (`python mini_agent.py`) — **deprecated** plain terminal REPL for headless/piped usage, ANSI-coloured output, stdin/stdout streaming
- `--legacy-tui` flag runs `tui.py`, `--no-ui` flag runs the plain REPL

### Dev Tools
- **Browser automation**: headless Playwright Chromium — navigate, accessibility tree snapshots, click, type, full-page screenshots
- **Eval harness**: YAML task format, 8 checker types, binary scoring, temp workspace copies
- **User interjection**: type while agent works — messages queued at tool boundaries. `/cancel` to abort

### Performance
- **Symbol index**: persisted to `.mini_agent_index.json`, mtime-gated, incremental reindex on writes
- **Semantic search**: embeddings-based code search with per-file mtime invalidation
- **Tool piping**: JSON parse short-circuit when no `_pipe` deps exist
- **Deque circuit breaker**: O(1) pop vs O(n) list pop(0)
- **Workspace tree cache**: skips `os.walk` on unchanged workspaces
- **Cached dispatch signatures**: `inspect.signature()` computed once at registration, not per call (~3x speedup)
- **Pre-built error hints**: `_build_error_hint` param lookup is O(1) dict, not O(n) TOOLS scan
- **Forward tool-call name map**: memory compression builds `tool_call_id→name` map in one pass (O(n²)→O(n))

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
DEEPSEEK_API_KEY="sk-your-key-here"        # required for DeepSeek — https://platform.deepseek.com
# Or use Claude/xAI instead:
# CLAUDE_API_KEY="sk-ant-..."               # https://console.anthropic.com
# XAI_API_KEY="xai-..."                     # https://x.ai/api
# API_PROVIDER="claude"                     # or "xai" — overrides auto-detection

SUB_AGENT_API_KEY="sk-your-sub-key-here"   # optional — separate key for sub-agents
OPENAI_API_KEY="sk-your-key-here"          # optional — GPT-4o vision (read_image tool)
EXA_API_KEY="your-exa-key-here"            # optional — web search (web_search tool)
EOF

# 5. Run
python tui_pt.py             # Prompt-toolkit TUI
```

## Configuration

Priority: CLI flags > environment variables > `.env` file > `.mini_agent.toml` > defaults.

### LLM Providers

mini_agent supports three providers. The provider is auto-detected from available API keys, or set explicitly:

| Provider | Env Var | Default Model | Max Tokens |
|----------|---------|---------------|------------|
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` | 200,000 |
| Claude (Anthropic) | `CLAUDE_API_KEY` | `claude-sonnet-4-5` | 32,000 |
| xAI / Grok | `XAI_API_KEY` | `grok-4.3` | 200,000 |

Set `API_PROVIDER="claude"` or `API_PROVIDER="xai"` to override auto-detection. The orchestrator and sub-agents use the same provider, but separate API keys can isolate sub-agent quota via `SUB_AGENT_API_KEY`.

### `.env` (API keys — gitignored)
```
DEEPSEEK_API_KEY="sk-..."       # DeepSeek (default provider)
CLAUDE_API_KEY="sk-ant-..."     # Claude (Anthropic)
XAI_API_KEY="xai-..."           # xAI / Grok
SUB_AGENT_API_KEY="sk-..."      # optional — isolates sub-agent quota
OPENAI_API_KEY="sk-..."         # optional — GPT-4o vision
EXA_API_KEY="..."               # optional — web search
API_PROVIDER="deepseek"         # "deepseek", "claude", or "xai"
```

### `.mini_agent.toml` (advanced settings)
```toml
api_provider = "deepseek"           # "deepseek", "claude", or "xai"
model = "deepseek-v4-pro"
sub_agent_model = "deepseek-v4-pro"
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
| `--theme NAME` | Initial UI theme (slate, dawn, sepia, ember, midnight, cobalt, neon, forest, dracula) |
| `--timeout SECONDS` | Max seconds for shell commands (default 60, max 300) |
| `-h, --help` | Show help |

## Running Tests

```bash
python -m pytest
# 1,013 tests pass, 39 skipped, 0 failures — ~19 seconds
```

## Tool Reference

### File Operations (10)
| Tool | Description |
|------|-------------|
| `read_file` | Read file contents (offset/limit, 300-line default, 1000-line max) |
| `write_file` | Write/overwrite a file (auto-backup before write) |
| `edit_file` | Search-and-replace with 3-pass fuzzy whitespace matching |
| `list_directory` | List directory contents |
| `file_info` | File/dir metadata (size, mtime, type, exists) |
| `write_scratchpad` | Persistent working note across turns (markdown) |
| `diff` | Show unstaged git changes (per-file or all) |
| `restore_file` | Restore from session backup (undo last write/edit) |
| `plan` | Declare numbered task plan (auto-shown each turn until complete) |
| `plan_status` | Mark step complete or view progress |

### Shell Operations (6)
| Tool | Description |
|------|-------------|
| `run_shell` | Execute shell command (60s timeout, max 300s, background mode, stdin pipe) |
| `run_tests` | Run pytest (specific file or all, background mode) |
| `git` | Local git operations (status, diff, log, init, add, commit, show, restore) |
| `task_status` | Poll background task by ID |
| `verify` | Lint + tests for files modified this session |
| `diagnose_failures` | Parse last test output for structured failure details |

### Search (8)
| Tool | Description |
|------|-------------|
| `find_symbol` | Locate Python symbol definitions (persisted index, mtime-gated) |
| `find_usages` | Find all references to a symbol (faster than grep) |
| `search_files` | Text/regex search (200-result cap, pagination) |
| `semantic_search` | Embeddings-based code search (per-file mtime invalidation) |
| `web_search` | Exa web search (auto/fast/deep, 5–20 results) |
| `recall_turn` | Recall previous turn details (for lost context after pruning) |
| `fetch_url` | Fetch and read a web page URL (text/html, text/plain) |
| `read_image` | Describe image via GPT-4o vision |

### LSP (4)
| Tool | Description |
|------|-------------|
| `lsp_definition` | Go to definition (pylsp) |
| `lsp_references` | Find all references (pylsp) |
| `lsp_hover` | Type/docs on hover (pylsp) |
| `lsp_diagnostics` | Errors/warnings for file (pylsp) |

### MCP (2)
| Tool | Description |
|------|-------------|
| `mcp_discover` | List all MCP tools from connected servers (stdio JSON-RPC) |
| `mcp_call` | Call an MCP tool by server/tool/arguments |

### Browser Automation (6)
| Tool | Description |
|------|-------------|
| `browser_navigate` | Navigate headless Playwright Chromium to a URL |
| `browser_snapshot` | Capture accessibility tree (structured, LLM-friendly) |
| `browser_click` | Click element by accessibility role + name |
| `browser_type` | Type text into an input by role + name |
| `browser_screenshot` | Full-page PNG screenshot |
| `open_url` | Open URL in user's default browser |

### Multi-Agent Lifecycle (7)
| Tool | Description |
|------|-------------|
| `spawn_agent` | Spawn sub-agent(s) with task description (up to 10 in one call, shared_context) |
| `agent_status` | Non-blocking status check with auto-captured snapshot (thought, tool, scratchpad) |
| `collect_agent` | Block until sub-agent completes (30s timeout, returns full result) |
| `collect_any` | Grab first completed result from a set (10s timeout) |
| `agent_extend` | Grant more turns to running sub-agent (+10 default, max 35 total) |
| `agent_cancel` | Cancel a running sub-agent (stops at next turn boundary) |
| `wait_for_agent` | Block with exponential backoff until any agent completes |

### Coordination Patterns (5)
| Tool | Description |
|------|-------------|
| `fan_out` | Spawn N workers from task descriptions in one call |
| `fan_in` | Block until all specified agents complete |
| `pipeline` | Sequential stages, each waits for previous |
| `barrier` | Block until all agents complete (sync point) |
| `scatter_gather` | Apply template across items in parallel |

### Inter-Agent Communication (5)
| Tool | Description |
|------|-------------|
| `agent_message` | Broadcast text to all agents |
| `agent_read` | Read broadcast messages (since index) |
| `agent_handoff` | Typed structured result (handoff.result, handoff.request, coord.*, status.*) |
| `agent_inbox` | Read typed inbox for any agent (ring-buffer, 1000 cap) |
| `agent_subscribe` | Declare message type subscriptions per agent |

### Session & Learning (5)
| Tool | Description |
|------|-------------|
| `remember` | Manually capture a learning to project_knowledge for cross-session persistence |
| `init` | Analyze workspace, auto-generate .mini_agent.rules and .mini_agent.toml |
| `session_stats` | Show session statistics (turns, tokens, active sub-agents, plan progress) |
| `todo_write` | Create or update a todo item for tracking progress |
| `todo_read` | Read current todo list (filter by id or status) |

### Skills (1)
| Tool | Description |
|------|-------------|
| `use_skill` | Activate lazy-loaded skill groups (git, web, test, planning, agents, search, image, lsp, bootstrap) — tools loaded on first use |

## Architecture

```
tui_pt.py             Prompt-toolkit TUI — live streaming, tools/chat panels, slash commands,
                      error trace logging to error_traces.log
llm.py                LLM turn orchestration, circuit breaker, tool piping/grouping, context injection
api.py                API calls (call_llm), provider routing (deepseek/claude/xai), message cleaning cache
prompt.py             System prompt + .mini_agent.rules injection + project knowledge
config.py             AgentConfig (.env + TOML + env + CLI), multi-provider, build_startup_context
memory.py             SQLite conversation store + pruning + project_knowledge table
safety.py             Read/Write safety gates + diff preview + _safe_resolve
interject.py          Thread-safe user interjection queue
terminal.py           ANSI colour helpers
retry.py              HTTP retry with jitter + exponential backoff (408, 429, 5xx)
stream.py             SSE stream parser
agent_runtime.py      Sub-agent lifecycle, file reservations, inboxes, subscriptions, snapshots
sub_agent.py          Sub-agent loop — progress-based termination (hung/error-loop detection),
                      streaming snapshots, heartbeats, reports to reports/<id>.md
conftest.py           Pytest configuration + shared test helpers (make_tool_call, gates, fixtures)
tools/
  __init__.py         Tool dispatch, cached signatures, per-tool timeout (120s),
                      auto-learn failure patterns, JSON repair, file reservations,
                      use_skill gate for lazy skill loading
  schema.py           TOOLS definitions (59 tools)
  file_ops.py         read/write/edit/list/info — cross-agent collision detection,
                      cascading fuzzy whitespace match (3-pass: exact→trailing→indent)
  shell_ops.py        run_shell, search_files, run_tests, git, task_status, verify,
                      diagnose_failures, cross-platform shell + python detection
  search_ops.py       find_symbol, find_usages, semantic_search, web_search,
                      recall_turn, fetch_url
  agent_ops.py        spawn/status/collect/message/read/extend/handoff/inbox/subscribe/cancel +
                      remember, todo_write, todo_read
  agent_messages.py   AgentMessage, 9 message types, validation, routing
  agent_patterns.py   fan_out, fan_in, pipeline, barrier, scatter_gather
  browser_ops.py      Headless browser automation — navigate, snapshot, click, type,
                      screenshot via Playwright Chromium + open_url
  lsp.py              LSP client — pylsp integration, 4 tools (definition, references, hover, diagnostics),
                      cross-platform (select + queue-based reader)
  mcp_client.py       MCP client — stdio JSON-RPC, tool discovery + invocation
  skills.py           Lazy skill groups (git, web, test, planning, agents, search, image, lsp, bootstrap)
  _json_rpc_shared.py Shared subprocess drain_stderr + is_subprocess_connected
tests/
  test_*.py           37 test files, ~1,050 tests
```

## Key State

### Models & API
- **Three providers**: DeepSeek (default), Claude (Anthropic), xAI/Grok
- Auto-detection: picks provider based on available API keys. Explicit override via `API_PROVIDER` env var or `api_provider` in TOML
- Separate API keys via `DEEPSEEK_API_KEY`, `CLAUDE_API_KEY`, `XAI_API_KEY` and `SUB_AGENT_API_KEY` env vars
- `max_tokens`: 200k (DeepSeek/Grok), 32k (Claude). `max_messages`: 500
- Priority: CLI > env > `.env` > TOML > default
- `temperature` (0.0), `frequency_penalty` (0.3), `presence_penalty` (0.1), `stop_sequences`, `response_format` configurable via TOML
- Prompt caching: static identity content first for DeepSeek cache hits (~2,000 tokens never change)
- Multi-model routing: `routing_model` config routes simple prompts to cheaper model (disabled by default)

### Multi-Agent System
- Max 10 concurrent sub-agents (configurable via `sub_agent_max_concurrent` in TOML)
- **Turn budgets**: 25 turns default, extendable to 35, absolute safety cap at 200
- Progress-based termination: hung detection (300s no tool calls), error loop detection (3 consecutive identical failures)
- **Orchestrator sleep**: `wait_for_agent` blocks with exponential backoff (1s→30s), wakes on completion, hung detection, or inbox messages
- Sub-agent reports written to `reports/<task_id>.md` — unique per agent, persists across sessions
- Plan state isolated per sub-agent — parent plan restored on exit
- Stale agent GC: threads from previous sessions cleaned up on startup
- Sub-agents auto-prune memory every 5 turns when >20 messages to avoid 400 errors
- Streaming snapshots at 200-token granularity

### Memory & Learning
- SQLite-backed conversation store with token-aware pruning and progressive compression
- `project_knowledge` table persists learnings across sessions in same workspace
- `remember` tool for manual capture; `edit_file` mismatches auto-captured
- Workspace tree cached in project_knowledge (mtime-gated, skips `os.walk` on restart)
- Inbox ring-buffer cap at 1000 messages prevents memory leaks
- Background test output persisted to `test_output` table
- Auto-learn: `_FAILURE_PATTERNS` dict injects recovery hints on repeated tool failures

### Performance
- Symbol index persisted to `.mini_agent_index.json` (mtime-gated, incremental reindex)
- Semantic search with per-file mtime invalidation
- Incremental message cleaning cache (survives across turns via `id(messages)` key)
- `_pipe` dependency detection short-circuits before JSON parse when not present
- Circuit breaker uses `deque.popleft()` for O(1) window management

### Cross-Platform
- Windows ANSI support via `SetConsoleMode` (Windows 10+)
- Cross-platform shell: Git Bash → PowerShell → `cmd.exe` fallback chain
- LSP client uses queue-based reader on Windows (select works on pipes only on Unix)
- `_safe_resolve` in safety.py handles Windows non-existent path resolution
- Python detection: `py -3` → `python3` → `python` fallback chain
- Windows destructive patterns: `del /f`, `diskpart`, `rmdir /s`, `rd /s`, `reg delete`
- HTTP 408 (Request Timeout) added to retryable statuses

### Interfaces
- **Prompt-toolkit TUI** (`python tui_pt.py`) — live streaming, tools & thinking panel, chat panel, slash commands, git status, session tracking, sub-agent output window
- LSP integration via pylsp (auto-started on first use)
- Headless browser automation via Playwright (6 tools)
- User interjection queue with `/cancel` support
- Streaming token-by-token with live tool output
