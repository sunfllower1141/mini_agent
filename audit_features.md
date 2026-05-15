# Features Wiring Audit

Every multi-agent feature checked: defined, tested, and used in real code paths.

---

## Legend
- ✅ Defined + Tested + Used in real agent path
- ⚠️ Defined + Tested, but only via dynamic dispatch (by name, not import)
- ❌ Defined but never used / dead code

---

## Core Multi-Agent Features

| Feature | Defined | Tested | Real Path | Notes |
|---------|---------|--------|-----------|-------|
| `spawn_agent` | agent_ops.py | test_sub_agent.py, test_integration.py | ✅ agent_patterns.py calls it | Core spawning |
| `agent_status` | agent_ops.py | test_sub_agent.py | ✅ llm.py orchestrator | Polling |
| `collect_agent` | agent_ops.py | test_sub_agent.py | ✅ agent_patterns.py (fan_in, pipeline) | Blocking collect |
| `collect_any` | agent_ops.py | test_sub_agent.py, test_integration.py | ✅ agent_patterns.py internals | Fast-path collect |
| `agent_extend` | agent_ops.py | test_sub_agent.py | ✅ prompt tells orchestrator to use | Extend budget |
| `agent_cancel` | agent_ops.py | test_sub_agent.py | ✅ prompt tells orchestrator to use | Cancel agent |

## Coordination Patterns

| Feature | Defined | Tested | Real Path | Notes |
|---------|---------|--------|-----------|-------|
| `fan_out` | agent_patterns.py | test_agent_patterns.py, test_integration.py | ✅ registered as tool | Spawn N workers |
| `fan_in` | agent_patterns.py | test_agent_patterns.py | ✅ registered as tool, called by fan_out | Collect all |
| `pipeline` | agent_patterns.py | test_agent_patterns.py | ✅ registered as tool | Sequential stages |
| `barrier` | agent_patterns.py | test_agent_patterns.py | ✅ registered as tool | Synchronize |
| `scatter_gather` | agent_patterns.py | test_agent_patterns.py | ✅ registered as tool | N items → M workers |

## Inter-Agent Communication

| Feature | Defined | Tested | Real Path | Notes |
|---------|---------|--------|-----------|-------|
| `agent_message` | agent_ops.py | test_agent_messages.py | ✅ registered as tool | Broadcast |
| `agent_read` | agent_ops.py | test_agent_messages.py | ✅ registered as tool | Read broadcasts |
| `agent_handoff` | agent_ops.py | test_integration.py | ✅ registered as tool | Typed handoff |
| `agent_inbox` | agent_ops.py | test_integration.py | ✅ registered as tool | Typed inbox |
| `agent_subscribe` | agent_ops.py | minimal | ✅ registered as tool | Subscription config |

## Message Types (9 Total)

| Type | Defined | Used in Tool Path | 
|------|---------|-------------------|
| `handoff.result` | agent_messages.py | ✅ agent_handoff |
| `handoff.request` | agent_messages.py | ✅ agent_handoff |
| `handoff.ack` | agent_messages.py | ✅ agent_handoff |
| `status.heartbeat` | agent_messages.py | ✅ sub_agent.py sends every 3 turns |
| `status.error` | agent_messages.py | ✅ agent_handoff |
| `coord.fan_out` | agent_messages.py | ✅ fan_out/scatter_gather |
| `coord.fan_in` | agent_messages.py | ✅ fan_in |
| `coord.sync` | agent_messages.py | ✅ barrier |

Wait — that's 8 message types. STATE.txt claims 9. Checking...

The 9th type is not immediately visible. Checking agent_messages.py for the full registry.

---

## Tool Registration Audit

| Status | Count | Details |
|--------|-------|---------|
| Schema tools | 44 | In `TOOLS` list |
| Dispatch handlers | 45 | In `_TOOL_DISPATCH` |
| In schema, not dispatch | 0 | ✅ All schema tools have handlers |
| In dispatch, not schema | 1 | ❌ **`remember`** — dead code, model can't call it |

---

## Summary of Issues

1. **CRITICAL: `remember` tool is dead** — in dispatch but not in schema. Model never sees it.
2. **Message type count mismatch** — STATE.txt says 9, found 8 registered. Audit needed.
3. **All coordination patterns fully wired** — fan_out, fan_in, pipeline, barrier, scatter_gather all have schema entries, dispatch handlers, and tests.
4. **All IAC features fully wired** — agent_message, agent_read, agent_handoff, agent_inbox, agent_subscribe all registered.
