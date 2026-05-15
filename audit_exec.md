# Execution Layer Audit Report

Audited: `llm.py`, `sub_agent.py`, `agent_runtime.py`, `api.py`

---

## (a) Public Function Type Hints

| Module | Function | Has Hints? | Notes |
|--------|----------|------------|-------|
| llm.py | `run_agent_turn` | ✅ | Full signature |
| llm.py | `_build_execution_groups` | ✅ | Private but annotated |
| llm.py | `_execute_groups` | ✅ | Annotated |
| llm.py | `_execute_tools` | ✅ | Annotated |
| sub_agent.py | `run_sub_agent` | ✅ | Full signature |
| agent_runtime.py | All methods | ✅ | `@dataclass` used extensively |
| api.py | `call_deepseek` | ✅ | Full signature |
| api.py | `truncate_content` | ✅ | Annotated |

**Verdict: ✅ Clean — all public functions have type hints.**

---

## (b) Magic Numbers

| Location | Value | Is Magic? |
|----------|-------|-----------|
| `sub_agent.py:106` | `4000` chars | ⚠️ shared_context truncation — not a named constant |
| `sub_agent.py:146` | `8000` chars | ⚠️ task truncation — not a named constant |
| `sub_agent.py:200` | `_STREAM_SNAP_EVERY = 200` | ✅ Named |
| `sub_agent.py:400` | `keep_recent=6` | ✅ Calls compression with configurable |
| `sub_agent.py:402` | `max_tokens=80_000` | ⚠️ Hardcoded 80k sub-agent token limit |
| `sub_agent.py:402` | `max_messages=60` | ⚠️ Hardcoded 60 msg sub-agent limit |
| `sub_agent.py:125` | `max_turns=15` | ✅ Configurable param default |
| `sub_agent.py:132` | `max_depth=3` | ✅ Configurable param default |

**Verdict: ⚠️ 4 magic numbers (shared_context 4000, task 8000, sub-agent 80k tokens, 60 messages). Should be named constants.**

---

## (c) Circular Imports

| Import Chain | Status |
|--------------|--------|
| llm.py → tools → ... | ✅ Clean (tools is leaf) |
| sub_agent.py → tools, prompt, safety, agent_runtime, api | ✅ Clean (all one-way) |
| agent_runtime.py → minimal imports | ✅ Clean |
| api.py → standard library only | ✅ Clean |

**Verdict: ✅ No circular imports.**

---

## (d) Global Mutable State

| Module | State | Acceptable? |
|--------|-------|-------------|
| llm.py | `_CIRCUIT_BREAKER` (deque) | ✅ O(1) for circuit breaker tracking |
| sub_agent.py | None (all locals) | ✅ Clean |
| agent_runtime.py | None (all instance state) | ✅ Clean |
| api.py | None | ✅ Clean |

**Verdict: ✅ Minimal, acceptable.**

---

## (e) Duplicate Logic

| Pattern | In | Also In | Duplicate? |
|---------|----|---------|------------|
| Pruning/compression | sub_agent.py:400 | memory.py | ✅ sub_agent imports from memory — no duplication |
| Agent polling | sub_agent.py ~117 | llm.py | ⚠️ Both have polling loops — different contexts (sub-agent internal vs orchestrator) |
| Cancel check | sub_agent.py:130 | llm.py | ⚠️ Same pattern (cancel_event.is_set()) in both |
| Tool execution | sub_agent.py ~230 | llm.py `_execute_tools` | ✅ sub_agent calls `execute_tool` directly; llm uses `_execute_tools` for parallel groups |

**Verdict: ✅ No problematic duplication. Cancel-check pattern could be extracted but is only 1 line each.**

---

## (f) Agent Runtime Features → Actually Used?

| agent_runtime.py method | Used by sub_agent.py? | Used by llm.py? |
|------------------------|----------------------|-----------------|
| `get_max_turns` | ✅ turn 130+ | ❌ |
| `update_snapshot` | ✅ turn 170+ (pre-call + streaming) | ❌ |
| `set_result` / `mark_completed` | ✅ by tool functions | ❌ |
| `get_result` | ✅ collect_agent, collect_any | ❌ |
| `get_status` | ✅ agent_status tool | ❌ |
| `get_running_count` | ✅ fan_out throttle | ❌ |
| `cancel_task` | ✅ agent_cancel tool | ❌ |
| `extend_turns` | ✅ agent_extend tool | ❌ |
| Inbox / subscribe | ✅ agent_inbox, agent_handoff | ❌ |
| File reservations | ✅ file_ops.py | ❌ |

**Verdict: ✅ All agent_runtime.py features are used by sub-agent or tool paths. Nothing orphaned.**

---

## Summary

| Check | Result |
|-------|--------|
| Type hints | ✅ All public functions annotated |
| Magic numbers | ⚠️ 4 found (4k, 8k, 80k, 60) |
| Circular imports | ✅ None |
| Global mutable state | ✅ Minimal (1 deque) |
| Duplicate logic | ✅ None problematic |
| Runtime features used | ✅ All wired |

**Action items:**
- Extract `_SUB_SHARED_CONTEXT_CAP = 4000`, `_SUB_TASK_CAP = 8000`, `_SUB_MAX_TOKENS = 80_000`, `_SUB_MAX_MESSAGES = 60` as named constants in sub_agent.py
