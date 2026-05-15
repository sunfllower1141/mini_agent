# Agent Coordination System Audit

**Date**: 2025-07-18  
**Scope**: `agent_runtime.py`, `sub_agent.py`, `interject.py`, `tools/agent_ops.py`, `tools/agent_messages.py`, `tools/agent_patterns.py`

---

## 1. Dead Code

### 1.1 Unused Message Types

| Type | Registered | Sent? | Subscribed/Checked? |
|------|-----------|-------|---------------------|
| `handoff.request` | agent_messages.py:55 | Supported by `agent_handoff` (agent_ops.py:756) but never sent by sub-agent system prompt or any coordination pattern. | Not used by `fan_in`, `pipeline`, `barrier`, or `scatter_gather`. |
| `coord.fan_in` | agent_messages.py:80 | `agent_handoff` supports it (agent_ops.py:773) but it is never actually sent. | `fan_in()` (agent_patterns.py:86) uses `runtime.get_status`/`get_result`, not inboxes. |
| `coord.fan_out` | agent_messages.py:75 | `agent_handoff` supports it (agent_ops.py:770) but it is never actually sent. | Same — the `fan_out` pattern uses `_spawn_one`, not message routing. |

**Finding**: 3 of 9 registered message types (33%) are dead — defined, validated on construction, routed, but never produced or consumed by any coordination pattern.

### 1.2 Stale / Duplicate Structures

- **`SubAgentResult.to_json()` and `to_dict()`**: (agent_runtime.py:32-55) These two methods produce identical dict structures — `to_json()` just calls `json.dumps(to_dict())`. Neither is used anywhere except tests. The only consumer is `run_sub_agent` which returns the dataclass directly; callers use `.content`, `.success`, etc.
- **`AgentRuntime._SNAPSHOT_FIELDS`**: (agent_runtime.py:253-258) A tuple of field names defined as a class constant but never referenced anywhere. The `update_snapshot` method builds dicts manually without using this tuple.
- **`AgentRuntime.clear_inbox()`**: (agent_runtime.py:248-251) Defined but never called. All cleanup happens via `store_result(mark_abandoned)` which directly does `inboxes.pop(task_id, None)`.
- **`_COLLECT_ANY_POLL`**: (agent_ops.py:~490) Named constant with comment `"(unused, kept for reference)"`.
- **`_AGENT_MSGS` global list**: (agent_ops.py:32-34) This is a legacy flat broadcast list populated by `agent_message` and `agent_handoff` via `to_legacy_dict()`. It is read only by `agent_read`. The typed inbox system (AgentMessage objects in `runtime.inboxes`) serves the same purpose. Two parallel message systems with overlapping data.

### 1.3 Unused Parameters

- **`_spawn_one(reserved_files)`**: (agent_ops.py:61, parameter at line 70) The function accepts a `reserved_files` list and wires it to file reservation (lines 139-143), but no caller — not `fan_out`, `pipeline`, `scatter_gather`, nor the tool wrapper `_spawn_agent` — ever passes this parameter.

### 1.4 `interject.py` — Not Integrated with Sub-Agents

`interject.py` provides `push_interjection`, `poll_interjections`, `has_interjections`. These are consumed only by `_inject_interjections` in `llm.py:242`, which runs in the parent agent's main loop. Sub-agents (`run_sub_agent`) never call `poll_interjections`, so user interjections during sub-agent execution are silently ignored.

---

## 2. Correctness

### 2.1 Race: `_collect_any` Candidate Set Build

**File**: `tools/agent_ops.py:522-528`  
**Issue**: When `task_ids` is None, `_collect_any` builds a candidate set under `runtime._lock`, then releases the lock and iterates. Between the lock release and iteration, a sub-agent could complete (store_result removes it from `runtime.tasks` and `runtime.results`), making the candidate list stale. The subsequent `get_status` calls are individually locked, so no corruption — but the `"No sub-agents to collect"` error could fire falsely if all agents complete in the gap.

### 2.2 `agent_handoff` Type-Shifting Bug for `handoff.result`

**File**: `tools/agent_ops.py:753-754`  
```python
elif msg_type == "handoff.result":
    payload = {"result": result_payload, "task": str(result_payload)}
```
The `task` field is supposed to be a string per the schema (`agent_messages.py:51: {"result": "object", "task": "string"}`), but `str(result_payload)` stringifies the entire dict, producing something like `"{'progress': '50%'}"`. The actual content of the result is unrecoverable from the `task` field. The `result` field preserves the dict, but `task` is semantically garbled.

### 2.3 `fan_in` Timeout Behavior

**File**: `tools/agent_patterns.py:103-123`  
**Issue**: The `deadline` is calculated once at function entry, then each task_id in the loop gets `remaining = deadline - time.monotonic()`. If task 0 times out after 120s, `remaining` is already ≤ 0 for all subsequent tasks — they all get `None` without any wait. This means `fan_in` with a 120s timeout and 10 tasks where task 0 is stuck will mark the other 9 as timed out without waiting. The comment says "Returns results in the same order as task_ids" but this behavior silently drops results for later tasks.

### 2.4 `_route_message` Default Subscription Edge Case

**File**: `tools/agent_messages.py:207-223`  
**Issue**: The routing loop iterates `subscriptions.items()`. If a task_id has never called `set_subscriptions` and thus has no entry in the `subscriptions` dict, it will NEVER receive any messages. The docstring says "agents with empty/no subscriptions receive ALL message types" — but "no subscription entry" is not the same as "empty set." To get the default behavior, `set_subscriptions(task_id, [])` must be called explicitly. If a sub-agent is spawned without subscriptions being set (e.g., via `fan_out` in agent_patterns.py:75-79 where subscriptions=None), the `_spawn_one` function checks `if subscriptions is not None` before setting (agent_ops.py:145-146). If None, no entry is created, and that agent receives zero inbox messages.

### 2.5 File Reservation Leak on Sub-Agent Crash

**File**: `tools/agent_ops.py:93-130` (`_runner` closure)  
**Issue**: The try/finally in `_runner` only restores `config.stream`. `store_result` is called inside the `try` block (line 124). If `run_sub_agent` raises an exception that is not caught by the sub-agent loop (e.g., a `KeyboardInterrupt` or `MemoryError`), `store_result` is never called, and `release_all_files` (which is called inside `store_result`) is never invoked. Files reserved by this agent are leaked until the process exits.

### 2.6 Missing `_AGENT_MSGS` Reset Between Tests

The `_AGENT_MSGS` global list is a module-level variable with no lifecycle management. Tests clear it manually (`_AGENT_MSGS.clear()` in test setUp methods), but in production, it grows unbounded until the ring-buffer cap of 1000 messages. This is acceptable but worth noting.

### 2.7 Parameter Name Mismatch in `scatter_gather` Tool Wrapper

**File**: `tools/agent_patterns.py:526`  
```python
template = args.get("template", "")
```
But the Python API function `scatter_gather()` at line 248 takes the parameter as `worker_task_template`. The schema in `tools/schema.py` defines it as `worker_task_template`. The tool wrapper uses `"template"` which will always be empty when called by the LLM, causing a `"Missing required parameter: 'template'"` error.

---

## 3. Design

### 3.1 Dual Message System

Two parallel, partially overlapping communication channels:
1. **Legacy flat list** (`_AGENT_MSGS`): Populated by `agent_message` and `agent_handoff` via `to_legacy_dict()`. Read by `agent_read`.
2. **Typed inboxes** (`runtime.inboxes`): Populated by the same tools via `_route_message`. Read by `agent_inbox`.

A sub-agent that calls `agent_read` sees different data than `agent_inbox` for the same messages. The legacy format loses type information and correlation IDs. The inbox system preserves them. The prompt instructs sub-agents to use both but doesn't explain that they are disconnected.

### 3.2 Threading Model

Sub-agents run as `daemon=True` threads (agent_ops.py:133). This means:
- If the main process exits, all running sub-agents are killed mid-operation without cleanup.
- Files may be left half-written.
- `store_result` with `release_all_files` never fires.
- The workspace could be left in an inconsistent state.

No graceful shutdown protocol exists for daemon sub-agent threads.

### 3.3 Agent Lifecycle Gaps

- **No restart/retry**: When a sub-agent returns `error="Turn budget exhausted"`, the parent gets `success=False`. There is no mechanism to respawn the same task with more turns — the parent must manually call `spawn_agent` again with the same description.
- **No progress-based extension**: The orchestrator is told to extend turns "after ~10 turns" but has no way to know if the agent is stuck or making progress beyond the auto-snapshot and heartbeats. Extending a looping agent wastes resources.
- **Cancellation is signal-only**: `cancel()` sets a threading.Event. The sub-agent checks it at turn boundaries (sub_agent.py:151-159). During a long API call, the sub-agent ignores cancellation until the call returns. There is no mechanism to abort an in-flight HTTP request.

### 3.4 Race Condition in `store_result` Notification

**File**: `agent_runtime.py:102-130`  
The comment at line 127 notes that `_condition.notify_all()` is called outside `_lock` to avoid deadlock. However, there is a TOCTOU gap: between `store_result` popping entries (under `_lock`) and `notify_all` (outside `_lock`), a `collect_agent` that was not waiting on the condition could check `get_status` and see the popped state. In practice, this is benign because `collect_agent` uses `wait_for` with a predicate that rechecks under lock, and `store_result` sets `results[task_id]` before releasing lock.

### 3.5 Coordination Pattern Consistency

The five coordination patterns have inconsistent implementations:

| Pattern | Completion Detection | Timeout Handling |
|---------|---------------------|------------------|
| `fan_out` | N/A (just spawns) | N/A |
| `fan_in` | `condition.wait_for` with per-task deadline | Sequential deadline — later tasks starved |
| `pipeline` | `condition.wait_for` per stage | Single timeout per stage |
| `barrier` | Polls `get_inbox` with `condition.wait(0.2)` | Sleep-poll hybrid |
| `scatter_gather` | Delegates to `fan_in` | Inherits `fan_in` bug |

`barrier` is the only pattern that reads inboxes directly. `pipeline` uses `get_status` but passes results via shared_context. `fan_in` uses `get_status`/`get_result`. No unified convention.

---

## 4. Integration (Tools ↔ Runtime)

### 4.1 Coverage: All Agent Tools Properly Mapped

The 12 agent tools in `agent_ops.py` all correctly delegate to `AgentRuntime` methods:

| Tool | Runtime Method | Notes |
|------|---------------|-------|
| `spawn_agent` | `register`, `set_subscriptions` | Also creates thread + cancel_event |
| `agent_status` | `get_status`, `get_snapshot` | Enriches with snapshot data |
| `collect_agent` | `get_status`, `get_result`, `condition.wait_for` | Correct timeout + retry advice |
| `collect_any` | `get_status`, `get_result`, `condition.wait_for` | Also manipulates `_collected` |
| `agent_message` | `_route_message` to inboxes | Also appends to `_AGENT_MSGS` |
| `agent_read` | None — reads `_AGENT_MSGS` directly | Does NOT use runtime |
| `agent_handoff` | `_route_message` to inboxes | Also appends to `_AGENT_MSGS` |
| `agent_inbox` | `get_inbox` | Correct |
| `agent_subscribe` | `set_subscriptions` | Validates types against registry |
| `agent_extend` | `extend_turns`, `get_max_turns` | Correct |
| `agent_cancel` | `cancel` | Correct |

**Gap**: `agent_read` does not interact with `AgentRuntime` at all — it reads a module-level global list. This makes it impossible to test in isolation without manipulating `_AGENT_MSGS`.

### 4.2 `fan_out` / `fan_in` Tool Wrappers vs. Python API

Both `agent_patterns.py` and `agent_ops.py` register LLM-callable tools. `agent_patterns.py` also exports Python functions (`fan_out()`, `fan_in()`, etc.) that are used by:
- `fan_out` Python API → called by `_fan_out` tool wrapper and by `scatter_gather`
- `fan_in` Python API → called by `_fan_in` tool wrapper and by `scatter_gather`
- `pipeline` Python API → called by `_pipeline` tool wrapper
- `barrier` Python API → called by `_barrier` tool wrapper

This dual-layer design is sound, but the naming (identical function names for Python API and tool wrappers with underscore-prefixed tool names) could confuse maintainers.

### 4.3 Snapshot Integration

`AgentRuntime.update_snapshot()` is called in two places:
1. **Pre-LLM-call** (sub_agent.py:182-187): `last_action="calling_llm"`
2. **Post-tool-execution** (sub_agent.py:350-361): `last_action="tool_call"`
3. **Streaming mid-LLM** (sub_agent.py:204-212): `last_action="thinking"`

`agent_status` reads these snapshots (agent_ops.py:342) and enriches its output. The integration is clean and complete. No gaps found in the snapshot pipeline.

### 4.4 Orchestration Context Injection

`_inject_orchestration_context` in `llm.py:203-240` reads `runtime.get_running_ids()` and `runtime.get_pending_results()` to inject status into the parent agent's context. This is the bridge between the runtime layer and the LLM prompt. It only runs for the parent — sub-agents do not get this injection (they rely on the communication nudge every 3 turns instead). This is by design but worth noting as a potential gap: sub-agent orchestrators cannot see their own sub-agents' status via context injection.

---

## Summary of Findings

| Severity | Count | Key Items |
|----------|-------|-----------|
| **Critical** | 1 | `scatter_gather` tool uses wrong parameter name → always fails |
| **High** | 3 | `fan_in` timeout starvation, `_route_message` missing subscription entry = no messages, file reservation leak on crash |
| **Medium** | 5 | `handoff.result` task field garbled, dual message system, `_collect_any` race, no interjections for sub-agents, daemon thread cleanup |
| **Low** | 4 | Dead message types, duplicate structures, unused `clear_inbox`, `_COLLECT_ANY_POLL` |

**Tests**: The agent system has solid test coverage (`test_sub_agent.py`, `test_agent_patterns.py`, `test_agent_messages.py`, `test_integration.py`) but the integration tests mock `call_deepseek` and never exercise the real coordination paths. No test covers the `scatter_gather` parameter mismatch, the `fan_in` timeout bug, or the default subscription edge case.
