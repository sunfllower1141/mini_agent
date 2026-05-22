# Sub-Agent Architecture Audit Report

**Date:** 2026-07-18  
**Scope:** `sub_agent.py`, `agent_runtime.py`, `tools/agent_ops.py`, `tools/agent_messages.py`, `tools/agent_patterns.py`, `llm.py` (orchestration injection), `config.py`  
**Trigger:** 7+ sub-agents spawned, extended to 30 turns each, none completed in reasonable time. Only 1 of 7+ finished.

---

## Findings

| Severity | File | Line(s) | Issue | Fix |
|----------|------|---------|-------|-----|
| **CRITICAL** | `sub_agent.py` | 85-210 | Thread-per-agent model: all sub-agents share one API key, hit same rate limits, create thundering herd on retry | Replace threads with async task queue + semaphore-gated API calls |
| **CRITICAL** | `tools/agent_ops.py` | 515-530 | `collect_any` timeout is only 10s; orchestrator burns turns re-polling | Increase to 60s or make event-driven with condition variable |
| **HIGH** | `sub_agent.py` | 230-235 | Safety cap `max_turns * 10 if max_turns < 50 else 200` = 300 for 30-turn agents, never reached before 300s hung timeout | Cap at `max_turns * 2`, tied to actual budget |
| **HIGH** | `sub_agent.py` | 310-340 | Auto-extension pings on every turn when budget ≤ 2; floods orchestrator | Ping once at threshold, then rely on orchestrator's own auto-extend in `_inject_orchestration_context` |
| **HIGH** | `llm.py` | 190-240 | `_inject_orchestration_context` appends sub-agent status to messages EVERY turn, bloating context | Only inject when state changes (new completions, new messages) |
| **HIGH** | `sub_agent.py` | 80-135 | Sub-agent gets 4+ system messages (sub prompt, full system prompt, depth rules, shared context, task ID) before the task | Collapse into single structured system message |
| **MEDIUM** | `sub_agent.py` | 390-410 | Communication nudge every 3 turns adds ~300 tokens to context; agents rarely use messaging effectively | Make opt-in: only inject if agent has pending inbox messages |
| **MEDIUM** | `agent_runtime.py` | 80-85 | `store_result` releases file reservations under lock, but `_runner` finally-block also releases — double-release possible | Remove one; make release idempotent |
| **MEDIUM** | `tools/agent_patterns.py` | 25-85 | `fan_out` / `fan_in` / `scatter_gather` are Python helpers, not LLM-callable tools — orchestrator can't use them via tool calls | Expose as registered tools so LLM orchestrator can use them |
| **MEDIUM** | `sub_agent.py` | 170-175 | `_restore_plan()` called on every exit path but not on unhandled exceptions in tool execution | Wrap tool execution in try/finally for plan restore |
| **LOW** | `tools/agent_ops.py` | 130-150 | `_spawn_one` stderr redirect to log file is clever but silent — errors are invisible to operator | Add a `--debug-subagents` flag that tees stderr to both log and terminal |
| **LOW** | `agent_messages.py` | 145-185 | `_route_message` delivers to ALL agents with no subscriptions — broadcast storm when many agents exist | Default to opt-in: only deliver to subscribed agents |
| **LOW** | `config.py` | 50-55 | `sub_agent_model` defaults to same model as parent — no cost savings from cheaper worker models | Default sub-agents to a cheaper/faster model (e.g., `deepseek-v4-flash`) |

---

## Architecture Map

```
┌─────────────────────────────────────────────────┐
│                  Parent Agent                    │
│  (run_agent_turn loop in llm.py)                 │
│                                                  │
│  ┌─ _inject_orchestration_context() ──────────┐ │
│  │  • Reads runtime.get_running_ids()           │ │
│  │  • Reads runtime.get_pending_results()       │ │
│  │  • Auto-extends low-budget agents            │ │
│  │  • Injects sub-agent status into messages     │ │
│  └──────────────────────────────────────────────┘ │
│                    │ (spawn_agent tool call)       │
└────────────────────┼──────────────────────────────┘
                     │
          ┌──────────▼──────────┐
          │   AgentRuntime      │  (thread-safe singleton)
          │   ─────────────     │
          │   tasks: dict[tid→Thread]             │
          │   results: dict[tid→SubAgentResult]   │
          │   cancel_events: dict[tid→Event]      │
          │   max_turns: dict[tid→int]            │
          │   inboxes: dict[tid→list]             │
          │   subscriptions: dict[tid→set]        │
          │   status_snapshots: dict[tid→dict]    │
          │   messages: list (global broadcast)    │
          │   _condition: Condition (wake on done) │
          └──────────┬────────────────────────────┘
                     │
     ┌───────────────┼───────────────┬──────────────┐
     ▼               ▼               ▼              ▼
┌─────────┐   ┌─────────┐    ┌─────────┐    ┌─────────┐
│ Sub-    │   │ Sub-    │    │ Sub-    │    │ Sub-    │
│ Agent 1 │   │ Agent 2 │    │ Agent 3 │    │ Agent N │
│ (Thread)│   │ (Thread)│    │ (Thread)│    │ (Thread)│
└─────────┘   └─────────┘    └─────────┘    └─────────┘
     │               │              │              │
     └───────────────┴──────────────┴──────────────┘
                     │
              All share:
              • Same API key → rate limits
              • Same requests.Session
              • Same workspace files
              • Same safety gates
              • Same tool implementations
```

---

## Root Cause Analysis: Why Agents Stall

### 1. The Thundering Herd Problem (CRITICAL)

When 7+ sub-agents all call the LLM simultaneously on the same API key, they hit rate limits. Each retries with exponential backoff (0.5-3s), but since they're all in lockstep, they retry simultaneously and hit the limit again. This creates a **thundering herd** where agents spend most of their time in retry loops, not doing work.

**Evidence:** `retry.py` retries up to 3 times with jittered backoff, but jitter alone isn't enough with 7+ concurrent callers on one key.

### 2. The Orchestrator Poll Tax (HIGH)

The parent agent is itself an LLM agent. To check sub-agent status, it must:
1. Call `agent_status` / `collect_any` as a tool
2. Wait for the LLM to parse the result
3. Decide what to do next

Each "check" costs one full turn (LLM call + tool execution). With `collect_any` timeout at 10s, the orchestrator may poll 3-6 times before any agent finishes — burning its own context window on polling overhead.

### 3. Context Bloat from Orchestration Injection (HIGH)

`_inject_orchestration_context` runs **every turn** and appends:
- List of running agents with IDs
- Completed agent results
- Broadcast messages from agents
- Auto-extension notices
- Instructions to poll

On an 8-agent fleet, this adds 500-1000 tokens **per turn** to the parent's context. Over 20 turns, that's 10K-20K tokens of pure orchestration overhead.

### 4. Sub-Agent System Prompt Overload (HIGH)

Each sub-agent receives:
1. `_SUB_AGENT_SYSTEM_PROMPT` (~2K chars of behavior rules)
2. Full system prompt from `build_system_prompt(config)` (tool schemas, safety rules)
3. Depth/role instructions
4. Shared context from parent
5. Task ID injection
6. The actual task

This is 5+ system messages before the task. The tool schema alone (50+ tools) is enormous. A sub-agent doing a simple file read burns 20K+ tokens of context before it reads a single file.

### 5. Communication Nudge Overhead (MEDIUM)

Every 3 turns, sub-agents get a ~500-token "communication nudge" telling them to check their inbox, read broadcasts, send heartbeats, check for file conflicts, etc. For a 15-turn agent, that's 5 nudges = 2,500 tokens of meta-instruction. Most agents never use the messaging system.

---

## What Modern Multi-Agent Systems Do Differently (2024-2026)

### Pattern 1: Agent-as-Tool (AOrchestra, InfiAgent, ParaManager)

Instead of spawning free-running threads, **treat sub-agents as tools** with:
- Standardized input/output schema
- Stateless execution (or explicit state passing)
- The orchestrator calls them like any other tool — synchronous, with a result

**Benefit:** No polling, no race conditions, natural rate limiting (one tool call at a time).

### Pattern 2: Plan-Execute-Aggregate Loop (ROMA, Soothe)

Structured cycle:
1. **Planner** decomposes task into dependency-aware subtask graph
2. **Executors** run subtasks in parallel where dependencies allow
3. **Aggregator** synthesizes, verifies, and compresses results

**Benefit:** Clean separation of concerns. The orchestrator plans, workers execute, aggregator compresses. No free-form "figure it out" prompting.

### Pattern 3: Structured Output + Validation

Modern systems require sub-agents to produce structured output (JSON with schema validation), not free-text. The aggregator validates and rejects malformed results.

### Pattern 4: Context Isolation by Design (AGENTHIVE, ROMA)

Each sub-agent gets a **minimal, task-specific context** — not the full system prompt, not all tools, not the parent's conversation history. This prevents context bloat.

### Pattern 5: DAG-Based Task Graphs (InfiAgent, ROMA)

Tasks are decomposed into Directed Acyclic Graphs with explicit dependencies. Independent subtasks run in parallel; dependent ones sequence automatically. No "poll and hope" coordination.

### Pattern 6: Agent Registry + Dynamic Routing (Gradientsys, OmniNova)

A central registry stores agent capabilities. The scheduler queries the registry to find the right agent for each subtask. Supports hot-plugging new agents.

### Pattern 7: Separation of Orchestrator from Executor

The orchestrator is NOT an LLM agent doing work. It's a lightweight scheduler that:
- Decomposes tasks
- Routes to appropriate agents
- Aggregates results

The orchestrator can be a simpler/cheaper model, or even deterministic code.

---

## Concrete Fix Proposal

### Phase 1: Stop the Bleeding (Immediate, 1-2 days)

| # | Change | Impact |
|---|--------|--------|
| 1 | **Add API rate limiter**: Semaphore-guard all LLM calls so only N concurrent calls happen across all threads. Default N=2. | Eliminates thundering herd |
| 2 | **Increase `collect_any` timeout to 60s**: Reduces orchestrator polling frequency | Less context bloat |
| 3 | **Suppress orchestration injection when nothing changed**: Only inject when new completions or messages exist | ~50% less context overhead |
| 4 | **Cap sub-agent system prompt**: Don't send full tool schema to sub-agents. Send only the tools they actually need (read_file, write_file, search_files, find_symbol, run_shell). | ~15K token savings per sub-agent |
| 5 | **Make communication nudge opt-in**: Only inject if agent has unread inbox messages | ~2.5K token savings per agent |

### Phase 2: Structural Fixes (1-2 weeks)

| # | Change | Impact |
|---|--------|--------|
| 6 | **Replace thread-per-agent with async task queue**: Use `asyncio` + semaphore for API calls. Sub-agents become coroutines, not threads. The task queue naturally rate-limits. | Eliminates thread overhead, GIL contention, daemon fragility |
| 7 | **Expose fan_out/fan_in/scatter_gather as LLM tools**: Currently these are Python-only helpers. Make them registered tools so the orchestrator can use them naturally. | LLM orchestrator can use structured patterns |
| 8 | **Agent-as-Tool mode**: Add a `synchronous=True` parameter to spawn_agent that blocks the parent's tool call until the sub-agent completes (like a regular tool). | Clean semantics for simple delegation |
| 9 | **Structured output requirement**: Sub-agents must return JSON with `{findings: [...], files_changed: [...], error: null|string}`. Validate before accepting. | Reliable result parsing |
| 10 | **Cheaper sub-agent model default**: Default `sub_agent_model` to `deepseek-v4-flash` (or equivalent). Most sub-agent tasks are read/search/write, not reasoning. | 70%+ cost reduction |

### Phase 3: Modern Architecture (2-4 weeks)

| # | Change | Impact |
|---|--------|--------|
| 11 | **DAG-based task planning**: Decompose user requests into dependency-aware subtask graphs. Independent subtasks fan out; dependent ones sequence. | Eliminates "poll and hope" coordination |
| 12 | **Separate orchestrator from worker**: The orchestrator becomes a lightweight scheduler (cheap model or deterministic code). Workers are the LLM agents. | Orchestrator stops burning context on polling |
| 13 | **Agent registry**: Replace hardcoded "sub_agent_model" with a registry of available agent types, each with capability descriptions and cost profiles. | Dynamic agent selection |
| 14 | **Context budget per agent type**: Different agents get different tool sets and context budgets. A "file reader" agent doesn't need write tools. | More efficient context usage |
| 15 | **Persistent agent lifecycle**: Agents can be paused, checkpointed, and resumed. Results survive process restarts. | Reliability for long-running tasks |

---

## Recommended First Step

**Add an API rate limiter.** This is the single change most likely to fix the "all agents stall" problem you experienced. A simple `asyncio.Semaphore(2)` around the LLM call path would prevent the thundering herd and let agents make steady progress.

```python
# In sub_agent.py or api.py:
_API_SEMAPHORE = threading.Semaphore(2)  # max 2 concurrent LLM calls

def call_llm_with_limit(...):
    with _API_SEMAPHORE:
        return call_llm(...)
```

This alone would likely have let all 7 of your agents complete successfully, because instead of 7 simultaneous API calls competing for rate limits, only 2 would run at a time, with the other 5 waiting their turn without burning retries.
