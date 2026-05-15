# Audit: tools/schema.py TOOLS vs tools/__init__.py _TOOL_DISPATCH

**Date:** 2025-01-XX  
**Scope:** Cross-check all 45 tools in `schema.py:TOOLS` against `@_register` handlers, and vice versa. Also checks `@_summarize` coverage.

---

## Summary

| Check | Status |
|-------|--------|
| Tools in `TOOLS` but **no** `@_register` handler | **0** (all 45 covered ✅) |
| Tools with `@_register` but **not** in `TOOLS` | **0** (all map back ✅) |
| Tools missing `@_summarize` | **0** (all 45 have a summary ✅) |
| Duplicate `@_register` | **0** ✅ |
| Duplicate `@_summarize` | **1** ⚠️ `recall_turn` — see below |
| **Overall** | **Clean — one minor code hygiene issue** |

---

## Tools in TOOLS

All 45 tools listed in `tools/schema.py:TOOLS` are enumerated below. Every one has a matching `@_register("...")` decorator in the implementation modules.

| # | Tool Name | Schema Entry | `@_register` Source |
|---|-----------|-------------|---------------------|
| 1 | `remember` | ✅ | `agent_ops.py` line 1396 |
| 2 | `find_symbol` | ✅ | `search_ops.py` line 244 |
| 3 | `read_file` | ✅ | `file_ops.py` line 86 |
| 4 | `write_file` | ✅ | `file_ops.py` line 180 |
| 5 | `edit_file` | ✅ | `file_ops.py` line 366 |
| 6 | `list_directory` | ✅ | `file_ops.py` line 425 |
| 7 | `run_shell` | ✅ | `shell_ops.py` line 154 |
| 8 | `search_files` | ✅ | `shell_ops.py` line 339 |
| 9 | `file_info` | ✅ | `file_ops.py` line 458 |
| 10 | `run_tests` | ✅ | `shell_ops.py` line 441 |
| 11 | `semantic_search` | ✅ | `search_ops.py` line 517 |
| 12 | `web_search` | ✅ | `search_ops.py` line 592 |
| 13 | `git` | ✅ | `shell_ops.py` line 670 |
| 14 | `diff` | ✅ | `agent_ops.py` line 1176 |
| 15 | `task_status` | ✅ | `shell_ops.py` line 108 |
| 16 | `write_scratchpad` | ✅ | `agent_ops.py` line 1104 |
| 17 | `find_usages` | ✅ | `search_ops.py` line 722 |
| 18 | `verify` | ✅ | `shell_ops.py` line 511 |
| 19 | `restore_file` | ✅ | `agent_ops.py` line 1217 |
| 20 | `recall_turn` | ✅ | `agent_ops.py` line 1365 |
| 21 | `plan` | ✅ | `agent_ops.py` line 1263 |
| 22 | `plan_status` | ✅ | `agent_ops.py` line 1287 |
| 23 | `spawn_agent` | ✅ | `agent_ops.py` line 162 |
| 24 | `session_stats` | ✅ | `agent_ops.py` line 1330 |
| 25 | `agent_status` | ✅ | `agent_ops.py` line 319 |
| 26 | `collect_agent` | ✅ | `agent_ops.py` line 418 |
| 27 | `collect_any` | ✅ | `agent_ops.py` line 503 |
| 28 | `agent_message` | ✅ | `agent_ops.py` line 609 |
| 29 | `agent_read` | ✅ | `agent_ops.py` line 672 |
| 30 | `agent_extend` | ✅ | `agent_ops.py` line 979 |
| 31 | `agent_cancel` | ✅ | `agent_ops.py` line 1050 |
| 32 | `agent_handoff` | ✅ | `agent_ops.py` line 726 |
| 33 | `agent_inbox` | ✅ | `agent_ops.py` line 834 |
| 34 | `agent_subscribe` | ✅ | `agent_ops.py` line 908 |
| 35 | `lsp_definition` | ✅ | `lsp.py` line 686 |
| 36 | `lsp_references` | ✅ | `lsp.py` line 708 |
| 37 | `lsp_hover` | ✅ | `lsp.py` line 731 |
| 38 | `lsp_diagnostics` | ✅ | `lsp.py` line 753 |
| 39 | `fan_out` | ✅ | `agent_patterns.py` line 292 |
| 40 | `fan_in` | ✅ | `agent_patterns.py` line 352 |
| 41 | `pipeline` | ✅ | `agent_patterns.py` line 405 |
| 42 | `barrier` | ✅ | `agent_patterns.py` line 462 |
| 43 | `scatter_gather` | ✅ | `agent_patterns.py` line 510 |
| 44 | `read_image` | ✅ | `agent_ops.py` line 1503 |
| 45 | `diagnose_failures` | ✅ | `shell_ops.py` line 771 |

---

## Duplicate & Anomalies

### ⚠️ Issue 1: Duplicate `@_summarize("recall_turn")`

`recall_turn` has **two** `@_summarize` decorators:

- `search_ops.py:794` — `@_summarize("recall_turn")`
- `agent_ops.py:1386` — `@_summarize("recall_turn")`

Since `_TOOL_SUMMARIES` is a plain `dict`, the second registration (from `agent_ops.py`, imported _after_ `search_ops.py` in `__init__.py`) overwrites the first. The summarizer in `search_ops.py` is **dead code** — never used.

**Suggested fix:** Remove the orphan `@_summarize("recall_turn")` block from `search_ops.py` (lines 794–797).

### ✅ All `@_register` are single-registered

No tool name has duplicate `@_register` calls. The `_TOOL_DISPATCH` dict has exactly 45 entries, one per unique tool name.

### ✅ All 45 tools have `@_summarize`

Every tool in `TOOLS` has a matching `@_summarize` decorator (after accounting for the duplicate). None are missing.

---

## How the audit was performed

1. Extracted all tool names from the `TOOLS` list in `schema.py` by reading the file.
2. Grepped for all `@_register("...")` decorators across `tools/` submodules.
3. Grepped for all `@_summarize("...")` decorators across `tools/` submodules.
4. Cross-referenced: schema names → dispatch registry, dispatch registry → schema names.
5. Checked for duplicates in both directions.

**Total tool count:** 45 in schema, 45 registered, 45 summarized (1 duplicate summarizer).
