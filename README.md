# mini_agent

A terminal AI coding assistant powered by LLMs (DeepSeek, Claude, xAI/Grok) with 96 registered tools (19 core, 62 skill-gated across 10 skill groups),
multi-agent orchestration, SQLite memory, headless browser, desktop automation, and an Electron
desktop app. The agent observes, diagnoses, and improves its own codebase — it's self-modifying.

Tool-execution boundaries, what is enforced versus declared, and the known gaps are documented
in [SECURITY.md](SECURITY.md).

## Features

- **Multi-provider LLM support** — DeepSeek V3/R1, Claude Opus/Sonnet, xAI Grok 3, plus a
  provider fallback chain (primary fails → automatic failover)
- **96 tools** — 19 always available; the rest unlocked on demand from 10 skill groups spanning
  file ops, shell, search, LSP, browser automation, desktop control, testing, multi-agent
  orchestration, and web
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
git clone https://github.com/sunfllower1141/mini_agent.git
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

## Linux

See [LINUX_INSTALL.md](LINUX_INSTALL.md) for the comprehensive Linux setup guide
(prerequisites, step-by-step manual install, troubleshooting Wayland, Electron deps, and more).

Quick start: `bash setup.sh` → `cd mini_agent_electron && npm start`

## Windows

See [WINDOWS_INSTALL.md](WINDOWS_INSTALL.md) for the comprehensive Windows 11 setup guide
(prerequisites, one-shot setup, troubleshooting Defender/firewall issues, and keyboard shortcuts).

Quick start: `setup.bat` → `cd mini_agent_electron && npm start`

## Tool System

The agent starts with **19 core tools** and unlocks more via skill groups. Available skills:

| Skill | Tools | What it enables |
|-------|-------|-----------------|
| `agents` | 18 | Spawn/collect/cancel sub-agents, messaging, orchestration patterns |
| `desktop` | 16 | Windows/macOS desktop automation (UIA, Atomacos) |
| `web` | 8 | Web search, fetch URL, browser automation (Playwright), screenshots |
| `git` | 5 | Git operations (status, diff, log, add, commit) |
| `lsp` | 4 | Go-to-definition, find references, hover types, diagnostics |
| `search` | 4 | Symbol index, find usages, semantic search, session recall |
| `test` | 3 | Test running, verification, failure diagnosis |
| `bootstrap` | 2 | Session init, session stats |
| `image` | 1 | Read and analyze images |
| `tasks` | 1 | Background task status |

Planning, scratchpad, and todo tools are core — always available.

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
├── tools/                  # Tool implementations (96 registered tools)
│   ├── file_ops.py         # read/write/edit/list/info/scratchpad/diff
│   ├── shell_ops.py        # run_shell, search_files, run_tests, verify
│   ├── search_ops.py       # find_symbol, web_search, semantic_search
│   ├── agent_ops.py        # Sub-agent spawn/collect/cancel/extend
│   ├── agent_patterns.py   # fan_out, fan_in, pipeline, barrier, scatter_gather
│   ├── lsp.py              # LSP client (pylsp integration)
│   ├── browser_ops.py      # Playwright headless browser
│   └── ...
├── tests/                  # Test suite (1,500+ tests)
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
make test           # Fast suite (1,500+ tests, excludes slow + benchmarks)
make test-slow      # Slow tests (sub-agent threads, git, desktop ops)
make test-all       # Full suite (fast + slow + benchmarks)
make coverage       # With HTML coverage report
```

### Test Conventions

- Test files in `tests/` directory, `unittest.TestCase` style
- Slow tests marked with `@pytest.mark.slow` — excluded by default
- Benchmark tests require `--run-benchmarks` flag
- `conftest.py` at root provides shared fixtures, mocks, and test helpers

## Agent Self-Modification

mini_agent is self-modifying: it can observe its own behavior, diagnose issues, and improve
itself. This is governed by safety gates:

### Safety Boundaries

**Enforced in code:**

- **Read-before-edit** — `.py` writes are rejected unless the file was read this session
  (`tools/file_ops.py`, `_READ_FILES`)
- **Syntax validation** — Python is `compile()`d before every write; broken edits are rejected
- **Placeholder guard** — tool arguments that are placeholders (`?`, `...`, empty) are refused
  before they reach the OS (`_BOGUS_PATH_MARKERS`)
- **Blocked-command policy** — 19 patterns rejected in `run_shell`: recursive/forced delete,
  in-place edits, shell redirects that overwrite source, privilege escalation, raw disk writes,
  force-push. Overriding requires an explicit `force=True` argument (`tools/shell_ops.py`)
- **Backup before write** — `edit_file` / `write_file` snapshot previous content
- **Bounded autonomy** — sub-agents run under a turn budget, and file reservations prevent two
  agents from writing the same path

**Known gaps** (documented, remediation scoped):

- **Workspace isolation is declared but not enforced.** `ReadSafetyGate.check()` and
  `WriteSafetyGate.check()` (`core/safety.py`) currently return `allowed=True` unconditionally:
  the containment prefix is computed but never used, and `tests/test_safety.py` pins the
  permissive behavior as expected. Call sites branch on `result.allowed`, so those branches
  cannot reject today. Naive enforcement is not sufficient — the agent legitimately touches paths
  outside the workspace (logs in `~/.mini_agent/logs`, temp files, environment probing) — so the
  fix is deny-by-default containment for writes plus an explicit read allow-list.
- **No defense against instruction injection from untrusted content.** File contents, web pages,
  and tool output are fed back to the model as-is, so content the agent reads can attempt to
  steer it. Mitigation direction: provenance tracking for untrusted text, and refusing tool
  arguments derived from it.

See [SECURITY.md](SECURITY.md) for the full tool-execution security model.

### Self-Review Cycle

After completing a significant change, the agent follows a structured review cycle:

1. **Observe** — What did I just do? What files did I touch?
2. **Diagnose** — Did I encounter any new failure modes, edge cases, or patterns?
3. **Improve** — Should this be crystallized as a project rule, knowledge entry, or fix strategy?
4. **Verify** — Run relevant tests. If they fail, stop and diagnose before continuing.
5. **Document** — Update STATE.txt (architecture), HANDOFF.md (session), CHANGELOG.md (audit).

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
