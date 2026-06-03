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

- **75 tools**: file ops (with robust edit_file), shell, search, LSP, MCP, browser automation (Playwright), desktop automation (atomacos/mss), vision (GPT-4o), planning
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

## Architecture

```
Electron ──→ server.py ──→ llm.py ──→ api.py ──→ DeepSeek / Claude / xAI
                │
    ┌───────────┼───────────────┐
    ▼           ▼               ▼
 tools/     memory.py      agent_runtime.py
 (75)       (SQLite)       (sub-agents)
            │
    ┌───────┴───────┐
    ▼               ▼
 failure_learning.py  project_knowledge
 (self-learning)      (cross-session)
```

Core modules: `config.py` (settings), `safety.py` (gates), `prompt.py` (system prompt), `memory.py` (persistence), `failure_learning.py` (self-learning), `retry.py` (HTTP), `stream.py` (SSE).

Tool implementations live in `tools/` — each module self-contained.

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
