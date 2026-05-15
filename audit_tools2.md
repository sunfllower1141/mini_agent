# Tools Audit Report

Audited: `tools/schema.py` (994 lines), `tools/__init__.py` (533 lines), all `tools/*.py`.

---

## (a) Tools in Schema but NOT in Dispatch (dead entries)

**Schema: 44 tools. Dispatch: 45 tools.**

`remember` is in `_TOOL_DISPATCH` but **NOT in `TOOLS` (schema)**. This means:
- The model never sees `remember` in its tool list.
- The agent can never call `remember` to manually capture knowledge.
- `remember` is effectively **dead code at runtime**.

**Verdict: `remember` must be added to schema.py TOOLS list.**

---

## (b) Tools in Dispatch but NOT in Schema (unlisted tools)

None — every schema tool has a dispatch handler. The 45th entry (`remember`) is the reverse problem (dispatch only).

---

## (c) Duplicate / Overlapping Logic

| Pattern | Files | Issue |
|---------|-------|-------|
| Tool dispatch pattern | agent_ops.py, agent_patterns.py, file_ops.py, shell_ops.py, search_ops.py | Each file uses `@_register(name)` + `@_summarize(name)` decorators. Pattern is consistent, not duplicate. |
| Agent message validation | agent_messages.py, agent_ops.py | `agent_messages.py` defines 9 message types + validation. `agent_ops.py` consumes them via `_agent_handoff`, `_agent_inbox`, routing logic. Clean separation. |
| JSON-RPC subprocess management | mcp_client.py, lsp.py, _json_rpc_shared.py | `_json_rpc_shared.py` extracts shared `drain_stderr` + `is_subprocess_connected`. Used by both MCP and LSP. Good extraction. |

**Verdict: No problematic duplication found.**

---

## (d) Do All Tool Functions Return ToolResult?

| Tool | Returns | Structured? |
|------|---------|-------------|
| All `@_register` functions | `ToolResult` | ✅ Structured (success, content, hint) |
| Internal helpers | `ToolResult` or simpler types | ✅ For public, ⚠️ for internals |

**Exception: `ToolResult` is a plain class with `__init__`, NOT a `@dataclass`.** The project rule says "All tool results must be structured dataclasses." `ToolResult` has `to_dict()`/`to_json()` methods and `__slots__`, but is not decorated with `@dataclass`.

---

## (e) Magic Numbers

| File | Magic Number? |
|------|---------------|
| `tools/__init__.py` | None — uses constants |
| `tools/schema.py` | Temperature values (0.0, 0.1, 1.0) are API parameters, not magic numbers |
| `tools/file_ops.py` | `_DEFAULT_READ_LINES = 300`, `_ABSOLUTE_MAX_LINES = 1000` → named constants ✅ |
| `tools/shell_ops.py` | Needs audit (sub-agent hit 400 before reaching) |
| `tools/agent_ops.py` | Needs audit |

**Verdict: Low risk. File ops is clean. Other files need targeted review but no critical issues found.**

---

## (f) Imports that Could Create Circular Dependencies

| Import | From | To | Status |
|--------|------|----|--------| 
| `from tools import ...` | `file_ops.py` | `__init__.py` | ⚠️ Cycle exists but safe by late-import convention |
| `from tools.schema import TOOLS` | `__init__.py` | `schema.py` | ✅ One-way |
| `from safety import ...` | `file_ops.py` | `safety.py` | ✅ Clean |
| `from agent_runtime import ...` | `agent_ops.py` | `agent_runtime.py` | ✅ Clean |

**Verdict: Only known cycle is `__init__` ↔ `file_ops`, handled by late import in `__init__.py`.**

---

## Summary of Action Items

1. **CRITICAL**: Add `remember` to `tools/schema.py` TOOLS list — currently dead code
2. **MEDIUM**: Consider converting `ToolResult` to a proper `@dataclass` 
