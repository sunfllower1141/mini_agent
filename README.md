# mini_agent

A terminal coding agent with 59+ tools. Powered by DeepSeek, Claude, or xAI/Grok.
Multi-agent orchestration, SQLite memory, headless browser, and an Electron desktop app.

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

- **59 tools**: file ops, shell, search, LSP, MCP, browser automation (Playwright), vision (GPT-4o), planning
- **Multi-agent**: up to 10 concurrent sub-agents with fan-out/in, pipeline, barrier, scatter-gather patterns. Inter-agent messaging with typed handoffs.
- **Memory**: SQLite-backed conversation store with token-aware pruning and cross-session project knowledge
- **Providers**: DeepSeek (default), Claude Sonnet 4.5, xAI Grok 4.3 — auto-detect or set `API_PROVIDER`
- **Interface**: Electron desktop app (`mini_agent_electron/`)
- **Skills**: lazy-load tool groups via `use_skill` — git, web, test, planning, agents, search, image, lsp
- **Safety**: workspace read/write gates, backup-before-write, diff previews, confirm mode
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
python -m pytest          # 1,113 tests
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
    ┌───────────┼───────────┐
    ▼           ▼           ▼
 tools/     memory.py   agent_runtime.py
 (59)       (SQLite)    (sub-agents)
```

Core modules: `config.py` (settings), `safety.py` (gates), `prompt.py` (system prompt), `memory.py` (persistence), `retry.py` (HTTP), `stream.py` (SSE).

Tool implementations live in `tools/` — each module self-contained.
