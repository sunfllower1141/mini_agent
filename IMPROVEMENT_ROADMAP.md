# mini_agent — Improvement Roadmap

> Based on 4-agent research audit comparing mini_agent against Claude Code, Cursor Agent, Aider, Cline, Windsurf, OpenAI Agents SDK, LangGraph, and CrewAI. 2025-06-30.

## Where We Lead (Crown Jewels)

| Feature | Competitors | Our Edge |
|---------|------------|----------|
| **Multi-agent system** | Claude Code (1 sub-agent), Cursor (explore sub-agent) | 11 tools, 5 coordination patterns, recursive sub-agents, typed handoffs |
| **Tool count** | 8-15 per competitor | **48 tools** |
| **Inter-agent communication** | None have typed handoffs | 8 message types, subscription filtering, inboxes |
| **Safety architecture** | Basic permissions | Read/write gates, approval callback, snapshots, backup-before-delete |
| **MCP support** | Claude Code only | `mcp_discover` + `mcp_call` |
| **Electron bridge** | Terminal-only competitors | JSON-RPC bridge ready for desktop GUI |

---

## Quick Wins ✅ ALL DONE (Ship Today — ~200 lines total)

### 1. Git context ✅ in system prompt
- **What**: Include `git status`, current branch, recent commits in the system prompt
- **Effort**: ~10 lines in `prompt.py`
- **Competitor**: Claude Code does this
- **Impact**: Agent knows repo state without searching

### 2. Directory-walking ✅ rules (hierarchical `.mini_agent.rules`)
- **What**: Walk directory tree upward, merge all `.mini_agent.rules` files
- **Effort**: ~30 lines in `prompt.py`
- **Competitor**: Claude Code's `CLAUDE.md` hierarchy
- **Impact**: Teams get org/project/team-level conventions

### 3. Post-failure auto-remember ✅
- **What**: When `edit_file` fails or `run_shell` returns error, auto-call `remember()`
- **Effort**: ~50 lines hook into `execute_tool` result processing
- **Competitor**: Claude Code's auto-memory
- **Impact**: Agent stops repeating same mistakes

### 4. `/init` rules generator
- **What**: Command that analyzes codebase and auto-generates `.mini_agent.rules`
- **Effort**: ~100 lines, uses existing `build_symbol_index` + `semantic_search`
- **Competitor**: Claude Code's `/init`
- **Impact**: Zero-config onboarding

### 5. Auto-extend for orchestrator ✅
- **What**: Same auto-extend logic that sub-agents have, applied to parent
- **Effort**: ~20 lines in `llm.py`
- **Competitor**: None have this
- **Impact**: Parent doesn't budget-exhaust on complex tasks

---

## Must-Have (Week 1-2)

| # | Feature | Effort | Competitor |
|---|---------|--------|-----------|
| 1 | **Auto-learn from tool failures** — detect patterns (edit_file whitespace, missing imports) and persist | Medium | Claude Code auto-memory |
| 2 | **`/init` command** — auto-generate `.mini_agent.rules` from codebase analysis | Low | Claude Code |
| 3 | **Git-aware context** — branch, status, recent commits in system prompt | Low | Claude Code |
| 4 | **Hierarchical rules files** — walk directory tree merging `.mini_agent.rules` | Low | Claude Code |

## Should-Have (Week 3-4)

| # | Feature | Effort | Competitor |
|---|---------|--------|-----------|
| 5 | **Auto tool strategy hints** ✅ **DONE** — pre-turn hint suggesting search strategy based on prompt | Low-Med | Cursor |
| 6 | **Onboarding wizard** — "What kind of project? Python? Node? Here's how to start." | Medium | — |
| 7 | **Error recovery patterns** ✅ **DONE** — inject hints when same tool fails 2+ times in a row | Low | Claude Code |

## Future (Post-MVP)

| # | Feature | Effort | Competitor |
|---|---------|--------|-----------|
| 8 | Background codebase indexing ✅ **DONE** with embeddings cache | High | Cursor, Windsurf |
| 9 | Tree-sitter repo map (Aider-style proactive context) | High | Aider |
| 10 | Conversation rewind/resume/fork | Medium | Claude Code |
| 11 | Multi-root workspace support | Medium | Cursor |
| 12 | Multi-format editing (diff/udiff/search-replace) | Medium | Aider (5 formats) |
| 13 | Ripgrep-backed `search_files` | Medium | Cursor Instant Grep |
| 14 | Browser automation tool | Medium | Claude Code |
| 15 | Multi-language LSP (not just pylsp) | High | Cursor, Copilot |

---

## Competitor Deep Dives

### Claude Code — Auto-memory is the killer feature
- Claude automatically saves learnings across sessions to `~/.claude/projects/<project>/memory/`
- `MEMORY.md` acts as index, first 200 lines loaded at session start
- Claude decides what's worth remembering: build commands, debugging insights, code style
- **We have the storage** (`project_knowledge` table, `remember()` tool) but **no automation**

### Cursor Agent — Deep IDE integration
- Semantic codebase indexing: chunks into functions/classes → embeddings → vector DB
- **Instant Grep**: Custom search engine faster than ripgrep
- **Explore subagent**: Spawns parallel searches, returns summaries
- **Auto tool selection**: Agent picks grep vs semantic vs LSP based on prompt type

### Aider — Repo map is the differentiator
- Tree-sitter extracts symbol definitions from entire repo → dependency graph → context
- Map sent at start of EVERY turn, optimized for most important symbols
- Git-backed: edits are atomic commits, easy to undo
- **We lack proactive repo map**, but our on-demand approach is better for large repos

### Windsurf (Codeium) — Cascade agentic flow
- Proprietary context engine for cross-file understanding
- Cascade plans multi-step workflows before executing
- **We match this** with multi-agent patterns (pipeline, fan-out)

---

## Bottom Line

**Our multi-agent system is genuinely ahead of every competitor.** Recursive sub-agents with structured handoffs and 5 coordination patterns is unmatched. Combined with 48 tools, MCP, and the Electron bridge, this is a strong foundation.

**The largest UX gap: memory that actually learns.** Claude Code remembers your preferences without you lifting a finger. We have the infrastructure but no automation. Closing this gap (auto-learn + `/init`) makes mini_agent a 2025 product.

**Recommendation:** Ship the 5 quick wins this session. Then 4 must-haves next. That gets to private beta readiness with a story that beats Claude Code on multi-agent coordination and matches it on learning.
