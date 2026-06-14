# mini_agent

A terminal AI coding assistant powered by LLMs (DeepSeek, Claude, xAI/Grok) with 76+ tools,
multi-agent orchestration, SQLite memory, headless browser, desktop automation, and an Electron
desktop app. The agent observes, diagnoses, and improves its own codebase — it's self-modifying.

## Features

- **Multi-provider LLM support** — DeepSeek V3/R1, Claude Opus/Sonnet, xAI Grok 3, plus a
  provider fallback chain (primary fails → automatic failover)
- **76+ tools** across 11 skill groups: file ops, shell, search, LSP, browser automation,
  desktop control, git, testing, planning, multi-agent orchestration, and MCP
- **Multi-agent orchestration** — spawn sub-agents with turn budgets, typed inter-agent
  messaging (handoff, broadcast inbox), and parallel patterns (fan-out/in, pipeline, barrier,
  scatter-gather)
- **SQLite memory** — persistent conversations, project knowledge with FTS5 search, semantic
  search via CodeSearchNet embedding model, and automated mid-session compaction
- **Semantic response cache** — two-tier (exact SHA-256 + cosine similarity) with adaptive
  per-entry thresholds and online feedback loop; 15-25% cost reduction
- **Electron desktop app** — React renderer with streaming output, sub-agent tree view, and
  agent thinking visualization
- **Read-before-edit enforcement** — won't edit files it hasn't read; Python syntax validation
  gates every write
- **Self-modifying** — tracks its own tool calls, fingerprints failures, learns patterns, and
  can improve its own codebase (STATE.txt, HANDOFF.md, CHANGELOG.md audit trail)

## Quick Start (macOS / Linux)

```bash
git clone https://github.com/YOUR_USERNAME/mini_agent.git
cd mini_agent
./setup.sh
```

### Launch

```bash
# CLI mode (terminal)
python3 -m mini_agent

# Desktop app
cd mini_agent_electron
npm start
```

### API Key

Create a `.env` file in the repo root:

```env
DEEPSEEK_API_KEY=sk-your-key-here
```

Or use Claude: `ANTHROPIC_API_KEY=sk-ant-...` or xAI: `XAI_API_KEY=xai-...`

Keys can also be entered in the desktop app's settings panel (persisted to `~/.mini_agent_env`).

## Windows

See [WINDOWS_INSTALL.md](WINDOWS_INSTALL.md) for the comprehensive Windows 11 setup guide
(prerequisites, one-shot setup, troubleshooting Defender/firewall issues, and keyboard shortcuts).

Quick start: `setup.bat` → `cd mini_agent_electron && npm start`

## Tool System

The agent starts with **11 core tools** and unlocks more via skill groups. Available skills:

| Skill | Tools | What it enables |
|-------|-------|-----------------|
| `git` | 6 | Git operations (add, commit, diff, log, push, restore) |
| `test` | 3 | Test running, verification, test output parsing |
| `lsp` | 4 | Go-to-definition, find references, hover types, diagnostics |
| `web` | 10 | Web search, fetch URL, browser automation (Playwright), screenshot |
| `search` | 5 | Symbol index, find usages, semantic search, file search, session recall |
| `agents` | 14 | Spawn/collect/cancel sub-agents, messaging, orchestration patterns |
| `desktop` | 8 | macOS/Windows desktop automation (Atomacos, UIA) |
| `planning` | 5 | Plan, plan_status, todo tracking, scratchpad |
| `image` | 2 | Read and analyze images |
| `bootstrap` | 3 | Session init, workspace setup |
| `tasks` | 3 | Task management |

Lazy-loaded: skills activate on first use, and unused skills are pruned after turn 5 to save
API tokens and stabilize the KV-cache prefix.

## Architecture

```
mini_agent/
├── core/                   # Main loop, safety, bootstrap, config
│   ├── llm.py              # Turn orchestration, tool dispatch
│   ├── prompt.py           # System prompt assembly
│   ├── config.py           # TOML + env + CLI config (AgentConfig)
│   ├── safety.py           # Read/write safety gates (workspace isolation)
│   ├── bootstrap.py        # Session init, cleanup
│   ├── context_inject.py   # Per-turn context injection (1250 lines)
│   ├── codebase_map.py     # AST-based symbol extraction for startup context
│   └── knowledge_graph.py  # Entity-relationship graph (calls, imports, defs)
├── agents/                 # Multi-agent orchestration
│   ├── agent_runtime.py    # Sub-agent lifecycle, inboxes, reservations
│   └── sub_agent.py        # Sub-agent engine, turn budget, pruning
├── memory/                 # SQLite persistence
│   ├── memory.py           # MemoryStore: conversations, knowledge, scratchpad
│   ├── memory_prune.py     # Content-aware compression, orphan stripping
│   └── session.py          # Session lifecycle
├── tools/                  # Tool implementations (76+ tools)
│   ├── file_ops.py         # read/write/edit/list/info/scratchpad/diff
│   ├── shell_ops.py        # run_shell, search_files, run_tests, verify
│   ├── search_ops.py       # find_symbol, web_search, semantic_search
│   ├── agent_ops.py        # Sub-agent spawn/collect/cancel/extend
│   ├── agent_patterns.py   # fan_out, fan_in, pipeline, barrier, scatter_gather
│   ├── lsp.py              # LSP client (pylsp integration)
│   ├── browser_ops.py      # Playwright headless browser
│   └── ...
├── tests/                  # Test suite (~1100 tests)
├── eval/                   # Evaluation harness (YAML tasks + SWE-bench)
├── mini_agent_electron/    # Electron desktop app (React + Node.js)
│   ├── main.js             # Electron main process
│   ├── preload.js          # IPC bridge
│   ├── backend/server.py   # Python backend agent runner (WebSocket)
│   └── renderer/src/       # React UI components
├── .mini_agent.toml        # Runtime config
├── .mini_agent.rules       # Agent behavioral rules
├── STATE.txt               # Architecture decisions (read by agent at startup)
├── TASKS.md                # Task-to-file mapping index
└── CHANGELOG.md            # Self-modification audit trail
```

## Development

### Prerequisites

- Python 3.10–3.13
- Node.js 22+ LTS (for Electron desktop app)
- Optional: `ripgrep` (faster file search), `git`

### Setup

```bash
./setup.sh          # Creates venv, installs deps, builds Electron renderer
```

### Testing

```bash
make test           # Fast suite (~1100 tests, excludes slow + benchmarks)
make test-slow      # Slow tests (sub-agent threads, git, desktop ops)
make test-all       # Full suite (fast + slow + benchmarks)
make coverage       # With HTML coverage report
```

### Test Conventions

- Test files in `tests/` directory, `unittest.TestCase` style
- Slow tests marked with `@pytest.mark.slow` — excluded by default
- Benchmark tests require `--run-benchmarks` flag
- `conftest.py` at root provides shared fixtures, mocks, and test helpers

## Self-Modification

mini_agent is self-modifying: it can observe its own behavior, diagnose issues, and improve
itself. This is governed by safety gates:

- **Read-before-edit** — won't edit `.py` files it hasn't read this session
- **Syntax validation** — Python files compiled before every write; syntax errors rejected
- **Workspace isolation** — all reads/writes bounded to the workspace directory
- **Backup before write** — every `edit_file` / `write_file` creates a backup

### Tracking Files

| File | Purpose |
|------|---------|
| `STATE.txt` | Architecture map, active decisions (agent's orientation at startup) |
| `HANDOFF.md` | Auto-generated session handoff from `git diff` |
| `CHANGELOG.md` | Structured audit trail with dates and reasoning |
| `TASKS.md` | Task-to-file mapping index |

## Configuration

### `.mini_agent.toml`

```toml
[provider]
name = "deepseek"
model = "deepseek-chat"

[deepseek]
api_key = "sk-..."
api_base = "https://api.deepseek.com"

[agent]
max_turns = 50
workspace = "/Users/you/my-project"
```

### Environment Variables

- `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `XAI_API_KEY` — provider API keys
- `MINI_AGENT_CONFIG` — path to custom config file
- `MINI_AGENT_WORKSPACE` — override workspace directory

## License

MIT
