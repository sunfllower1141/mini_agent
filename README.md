# mini_agent

A terminal coding agent with 64+ tools. Powered by DeepSeek, Claude, or xAI/Grok.
Multi-agent orchestration, SQLite memory, headless browser, desktop automation, and an Electron desktop app.

## Quick Start

### Prerequisites

| Tool | Version | macOS / Linux | Windows |
|------|---------|---------------|---------|
| **Python** | 3.10+ | [python.org](https://www.python.org/downloads/) | [python.org](https://www.python.org/downloads/) _(check "Add Python to PATH")_ |
| **Node.js** | 18+ | [nodejs.org](https://nodejs.org/) | [nodejs.org](https://nodejs.org/) (LTS) |
| **ripgrep** | any | `brew install ripgrep` / `apt install ripgrep` | `winget install BurntSushi.ripgrep.MSVC` |
| **git** | any | `brew install git` / `apt install git` | `winget install Git.Git` |

### One-step install

Before running setup, verify your tools are reachable:

```bash
node --version   # should print v18+, v20+, or v22+
npm --version    # should print 9+ or 10+
python3 --version
rg --version     # optional but recommended
```

If `node` is not found but you know it's installed:
- **nvm users:** run `source ~/.nvm/nvm.sh` first (add to `~/.zshrc` or `~/.bashrc` to persist)
- **Homebrew (Apple Silicon):** the path is `/opt/homebrew/bin/node` — ensure it's in your `$PATH`
- **Windows:** the installer should add `C:\Program Files\nodejs\` to PATH; reinstall if `where node` returns nothing

Then:

**macOS / Linux:**
```bash
git clone https://github.com/GabrielMalone/mini_agent.git
cd mini_agent
bash setup.sh
```

**Windows (Command Prompt or PowerShell):**
```bat
git clone https://github.com/GabrielMalone/mini_agent.git
cd mini_agent
setup.bat
```

The setup script checks prerequisites, creates a Python venv, installs all dependencies (including Playwright browsers), and builds the Electron renderer.

### Launch

```bash
cd mini_agent_electron
npm start            # auto-builds renderer if needed, then opens the desktop app
```

> **Windows note:** On first launch, Windows Defender Firewall may prompt you to allow Node.js network access. Click "Allow" — the app needs this to communicate with the AI provider's API.

## Features

- **76 tools**: file ops (with robust edit_file), shell, search, LSP, MCP, browser automation (Playwright), desktop automation (atomacos/mss), vision (GPT-4o), planning
- **Self-learning**: the agent learns from its own mistakes across sessions. Failed tool calls are fingerprinted, clustered into patterns, and distilled into reusable fixes. Before repeating a call that's failed before, it gets a warning with what went wrong and how to fix it. Three subsystems work together:
  - *Failure Pattern Store* — SQLite-backed database of tool failures with confidence scoring. When `edit_file` fails with "string not found" 5 times, the agent remembers and warns itself before the next attempt.
  - *Self-Critique* — detects failure clusters mid-conversation and injects corrective guidance ("stop retrying, read the file first, try a different approach").
  - *Mistake Notebook* — batch-clusters recurring failures across different arguments and distills generalized fixes. If "not found" happens on 3 different files, it learns a universal rule: *"Before editing, read the exact text and copy-paste it."*
- **Multi-agent**: up to 10 concurrent sub-agents with fan-out/in, pipeline, barrier, scatter-gather patterns. Inter-agent messaging with typed handoffs.
- **Memory**: SQLite-backed conversation store with token-aware pruning and cross-session project knowledge (categorized learnings with confidence scoring and relevance-based injection)
- **Providers**: DeepSeek (default), Claude Sonnet 4.5, xAI Grok 4.3 — auto-detect or set `API_PROVIDER`
- **Interface**: Electron desktop app (`mini_agent_electron/`)
- **Skills**: lazy-load tool groups via `use_skill` — git, web, test, planning, agents, search, image, lsp, desktop
- **Safety**: workspace read/write gates, backup-before-write, diff previews, confirm mode, read-before-edit enforcement, must-match gating
- **Cross-platform**: macOS, Linux, Windows

## Configuration

API keys via `.env`:

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek (default provider) |
| `CLAUDE_API_KEY` | Anthropic Claude |
| `XAI_API_KEY` | xAI Grok |
| `OPENAI_API_KEY` | GPT-4o vision (optional) |
| `EXA_API_KEY` | Web search (optional) |

Advanced settings in `.mini_agent.toml`: model, temperature, max tokens, sub-agent concurrency, etc.

Priority: CLI flags > env vars > `.env` > `.mini_agent.toml` > defaults.

## Running Tests

```bash
python -m pytest          # 1,298 tests
make test                 # same, via Makefile
```

## Electron App

The desktop GUI lives in [`mini_agent_electron/`](mini_agent_electron/). It has:

- Streaming chat with Markdown + syntax-highlighted code blocks
- Session picker with SQLite-backed conversation history
- Workspace management (sandboxed read/write by default)
- Drag-and-drop file support
- Agent tree visualization (React Flow)
- In-app settings for API keys (persisted to `~/.mini_agent_env`)

```bash
cd mini_agent_electron
npm run dev          # development mode with hot-reload + DevTools
npm start            # production mode
```

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Shift+Enter` | New line |
| `Escape` | Cancel streaming response |

## Agent Self-Modification

mini_agent is designed to modify and improve its own codebase. If you're a **human collaborator** working alongside the agent, here's what you need to know:

### The Agent's Memory System

| File | Purpose | Updated |
|------|---------|---------|
| `STATE.txt` | Architecture map — current decisions, module map, known issues. The agent reads this at startup. | By agent after each change |
| `HANDOFF.md` | Session handoff — what changed, what's pending. Picks up where it left off. | Auto-written at session end |
| `CHANGELOG.md` | Self-mod audit trail — what changed and why, structured by date. | By agent after significant changes |
| `.mini_agent.rules` | Rules and conventions the agent follows. Also serves as the CLAUDE.md equivalent. | By agent when patterns crystallize |
| `project_knowledge` (SQLite) | Cross-session learnings with confidence scoring. Survives workspace resets. | By agent via `remember` tool |

### Safety Boundaries

- **Workspace gates**: reads/writes are sandboxed to the workspace directory (unless `--unrestricted`)
- **Read-before-edit**: `edit_file` requires a recent `read_file` call
- **Diff previews**: all file writes produce unified diffs for review
- **Backup-before-write**: destructive operations create `.bak` files
- **Circuit breaker**: repeated identical tool calls are detected and halted
- **Self-critique**: failure patterns are detected mid-conversation and corrective guidance is injected

### How the Agent Evolves

1. **Observe**: failed tool calls are fingerprinted and stored in SQLite
2. **Diagnose**: patterns are clustered across different arguments and tools
3. **Improve**: fixes are distilled into reusable strategies (stored in `project_knowledge`)
4. **Verify**: tests are run after every change; the agent stops if they fail
5. **Document**: `STATE.txt`, `HANDOFF.md`, and `CHANGELOG.md` are updated to track what happened

### Contributing Alongside the Agent

- The agent reads `.mini_agent.rules` on every startup — update it if you want to change its behavior
- The agent reads `STATE.txt` for architecture context — keep it current
- The agent reads `HANDOFF.md` for session continuity — it's auto-generated but you can add notes
- All modifications are tracked in `CHANGELOG.md` — review it to see what the agent has been doing
- Run `python -m pytest` before and after letting the agent work — it won't proceed with broken tests

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DESKTOP WRAPPER                          │
│  Electron ──→ server.py                                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                     CORE ORCHESTRATION                           │
│                                                                  │
│  bootstrap.py ──→ llm.py ──→ api.py ──→ DeepSeek / Claude / xAI │
│  (session init)   (agent loop,   (provider                │
│                    tool dispatch)  abstraction)            │
│       │                │                                    │
│       ▼                ▼                                    │
│  context_inject.py  config.py   safety.py  prompt.py        │
│  (per-turn hints,   (TOML cfg)  (rw gates) (sys prompt)     │
│   circuit breaker)                                          │
└─────────────────────────┬──────────────────────────────────────┘
                          │
      ┌───────────────────┼───────────────────────┐
      ▼                   ▼                       ▼
┌─────────────┐  ┌──────────────┐    ┌────────────────────┐
│   TOOLS     │  │    MEMORY    │    │   SUB-AGENTS        │
│  (76 total, │  │   (SQLite)   │    │                     │
│   11 core,  │  │              │    │  agent_runtime.py   │
│   11 skills)│  │  memory.py   │    │  sub_agent.py       │
│             │  │  session.py  │    │                     │
│  __init__.py│  │  prune.py    │    └────────────────────┘
│  context.py │  │              │
│  schema.py  │  │              │
│  skills.py  │  │              │
│             │  │              │
│  file_ops   │  │              │
│  shell_ops  │  │              │
│  search_ops │  │              │
│  agent_ops  │  │              │
│  agent_msgs │  │              │
│  agent_pat. │  │              │
│  agent_todos│  │              │
│  browser_ops│  │              │
│  desktop_ops│  │              │
│  macos_ops  │  │              │
│  lsp.py     │  │              │
│  mcp_client │  │              │
│  failure_   │  │              │
│   learning  │  │              │
│  tool_graph │  │              │
│  error_hints│  │              │
│  reservat.  │  │              │
└─────────────┘  └──────────────┘
```

### Key Design Decisions

| Decision | Detail |
|----------|--------|
| **Skills system** | 11 core tools always visible, 11 skill groups (65 tools) lazy-loaded via `use_skill`. Keeps prompt focused for simple tasks. |
| **SQLite memory** | Separate session DBs in `memory/memory.py`. Persists messages, scratchpad, learnings, handoff. Compresses stale tool results with content-aware pruning. |
| **Per-turn context injection** | `context_inject.py` runs every turn: hints stale tool results, suggests better tools, checks for repeated mistakes, enforces circuit breaker. STATE.txt/HANDOFF.md injected once per session. |
| **Safety gates** | All file and shell operations go through `core/safety.py` ReadSafetyGate/WriteSafetyGate, enforcing workspace boundaries. |
| **Sub-agent isolation** | Each sub-agent gets its own context, turn budget, and broadcast inbox. File reservations (`tools/reservations.py`) prevent write collisions. |
| **Circuit breaker** | Detects 3+ repeated identical tool calls in a 6-call window and warns before API cost spirals. |
| **Self-modification tracking** | `STATE.txt` (architecture map), `HANDOFF.md` (session continuity), `CHANGELOG.md` (audit trail). Agent reads/updates these to maintain cross-session context. |

### Module Map

| Directory | Key Files | Purpose |
|-----------|-----------|---------|
| `core/` | `bootstrap.py`, `llm.py`, `context_inject.py`, `config.py`, `safety.py`, `prompt.py` | Session init, agent loop, context injection, config, safety, system prompt |
| (root) | `api.py`, `stream.py`, `retry.py`, `interject.py`, `logging_setup.py` | Provider abstraction, SSE streaming, HTTP retry, user interjection polling, structured logging |
| `tools/` | `__init__.py`, `schema.py`, `skills.py`, `context.py`, `reservations.py`, `file_ops.py`, `shell_ops.py`, `search_ops.py`, `agent_ops.py`, `agent_patterns.py`, `browser_ops.py`, `desktop_ops.py`, `macos_ops.py`, `lsp.py`, `mcp_client.py`, `failure_learning.py`, `tool_graph.py`, `error_hints.py` | Tool dispatch, schema definitions, skill gates, agent context, file reservations, all tool implementations |
| `memory/` | `memory.py`, `memory_prune.py`, `session.py` | SQLite persistence, message pruning/compression, session lifecycle |
| `agents/` | `agent_runtime.py`, `sub_agent.py` | Sub-agent runtime, task delegation |
| `eval/` | `scorer.py`, `swebench_runner.py` | Local eval tasks + SWE-bench integration |

## Benchmarks

mini_agent includes a built-in evaluation harness for benchmarking tool-use and code-fix capabilities.

### Local eval tasks (fast)

9 YAML-defined tasks exercising core tools: file creation, editing, testing, diffing, semantic search, multi-agent fan-out:

```bash
# Run all local eval tasks (requires API key):
python -m pytest test_benchmarks.py --run-benchmarks -v -k "local"

# Run a specific task:
python -m pytest test_benchmarks.py --run-benchmarks -v -k "hello_world"
```

### SWE-bench (industry standard)

[SWE-bench](https://www.swebench.com/) is the standard benchmark for coding agents — 2,300 real GitHub issues across 12 Python repositories. mini_agent can generate predictions for official evaluation:

```bash
# Install optional dependency:
pip install datasets

# Smoke test: run 1 SWE-bench Lite task locally:
python -m pytest test_benchmarks.py --run-benchmarks --swebench -v -k "smoke"

# Generate predictions for the first 5 SWE-bench Lite tasks:
python -m eval.swebench_runner \
  --dataset princeton-nlp/SWE-bench_Lite \
  --max-tasks 5 --output predictions.jsonl

# Resume a previous run:
python -m eval.swebench_runner \
  --dataset princeton-nlp/SWE-bench_Lite \
  --resume predictions.jsonl --output predictions.jsonl
```

Then evaluate with the official SWE-bench harness:

```bash
git clone https://github.com/princeton-nlp/SWE-bench.git
cd SWE-bench
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path /path/to/predictions.jsonl \
  --max_workers 4 --run_id mini_agent
```

See [`eval/`](eval/) for the full evaluation architecture and [`eval/swebench_runner.py`](eval/swebench_runner.py) for the SWE-bench pipeline.
