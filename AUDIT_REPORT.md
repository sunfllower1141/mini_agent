# mini_agent Codebase Standards Audit Report

**Date**: 2025-05-20 | **Updated**: 2025-05-20 (fixes applied) | **Scope**: All modules except `safety.py`

---

## Executive Summary (Revised)

| Severity | Found | Fixed | False Alarm | Remaining |
|----------|-------|-------|-------------|-----------|
| 🔴 Critical | 5 | 2 | 3 | 0 |
| 🔴 High | 8 | 4 | 3 | 1 |
| 🟡 Medium | 20 | 0 | 0 | 20 |
| 🟢 Low | 30 | 0 | 0 | 30 |

**Key corrections:**
- C1 (`set_context` import): Works correctly — `from tools import set_context` is valid.
- C2 (`remember` duplicate): Intentional safety fallback — only inserts if schema.py is missing it.
- C4 (mutable globals): `_FILE_RESERVATIONS` already protected by `_FILE_RESERVATIONS_LOCK`; `_AGENT_MSGS`/`_FILE_CACHE` don't exist.
- H3 (`_approve()`): Gated by `config.approve_write_ops` at caller (tui.py:835). Intentional bypass for TUI.
- H4/H5 (ChatBuffer races): Already thread-safe — `self._lock` used in all methods.
- H8 (`APIError` raise): Exception caught in llm.py; intentional design pattern.

---

## ✅ Fixed Issues

| # | Severity | File | Issue | Fix Applied |
|---|----------|------|-------|-------------|
| C3 | Critical | `tools/agent_ops.py:100` | `_DEFAULT_MAX_TURNS = 15` vs config's 25 | Changed to `25` |
| C5 | Critical | `tools/browser_ops.py` | Shared `_PAGE` across agents | Added `_BROWSER_LOCK` + docstring |
| H1 | High | `mini_agent.py:86` | `/export` no write_gate check | Added `write_gate.check(path)` |
| H2 | High | `tui_pt.py:696` | `/export` no write_gate check | Added `write_gate.check(path)` |
| H6 | High | `mini_agent.py:272` | Workspace switch data loss risk | Added try/finally + old_session.close() |
| H7 | High | `api.py:73` | Global mutable `_clean_messages_cache` | Replaced with `threading.local()` |

---

## 🔴 Critical Bugs (Verified)

| # | Status | Issue | Location |
|---|--------|-------|----------|
| C1 | ~~FALSE~~ | `set_context` import works correctly | mini_agent.py:51 |
| C2 | ~~INTENTIONAL~~ | `_REMEMBER_SCHEMA` is safety fallback | tools/__init__.py:36 |
| C3 | ✅ FIXED | `_DEFAULT_MAX_TURNS = 15` → `25` | tools/agent_ops.py:100 |
| C4 | ~~FALSE~~ | `_FILE_RESERVATIONS` already has lock; others nonexistent | tools/__init__.py:165 |
| C5 | ✅ FIXED | Browser page shared globally — added `_BROWSER_LOCK` | tools/browser_ops.py:40 |

---

## 🔴 High Severity (Verified)

| # | Status | Issue | Location |
|---|--------|-------|----------|
| H1 | ✅ FIXED | CLI `/export` bypasses write_gate | mini_agent.py:86 |
| H2 | ✅ FIXED | TUI PT `/export` bypasses write_gate | tui_pt.py:696 |
| H3 | ~~BY DESIGN~~ | `_approve()` gated by `config.approve_write_ops` at caller | tui.py:760,835 |
| H4 | ~~FALSE~~ | ChatBuffer already uses `self._lock` (threading.Lock) | tui_pt.py:126 |
| H5 | ~~FALSE~~ | ChatBuffer `_lines` protected by `with self._lock:` | tui_pt.py:132 |
| H6 | ✅ FIXED | Workspace switch: save→init→close with try/finally | mini_agent.py:272 |
| H7 | ✅ FIXED | Global mutable `_clean_messages_cache` → thread-local | api.py:73 |
| H8 | ~~BY DESIGN~~ | `APIError` exception caught in llm.py:263 | api.py:263 |

---

## 🟡 Medium Severity (Remaining)

| # | File | Issue |
|---|------|-------|
| M1 | `config.py:714` | `parse_args() -> object` instead of `argparse.Namespace` |
| M2 | `config.py:543,605` | `switch_session()/init_session() -> dict` — bare dict, no key shape |
| M3 | `retry.py:32` | `session` parameter has no type hint |
| M4 | `llm.py:246` | `remaining <= 3` — auto-extend threshold |
| M5 | `llm.py:248` | `extend_turns(tid, 10)` — magic number |
| M6 | `llm.py:394` | `messages.insert(1, ...)` — assumes index 1 |
| M7 | `llm.py:973-974` | `max_turns - 3`, `max_turns + 10` — auto-extend bounds |
| M8 | `api.py:33,115` | Magic `300` used 3× with different semantics |
| M9 | `api.py:113` | Magic `2000` for complexity limit |
| M10 | `prompt.py:88,90,91` | Magic `15` for git status truncation |
| M11 | `config.py:542,570,671` | `keep_recent=6` repeated 3× |
| M12 | `config.py:591,676` | `limit=15` repeated 2× |
| M13 | `terminal.py:33-42` | Windows console API constants (`-11`, `0x0004`, etc.) |
| M14 | `sub_agent.py ~285` | Auto-extension uses `status.error` for budget warning |
| M15 | `sub_agent.py ~85` | `_write_report()` catches OSError silently |
| M16 | `tui.py ~1090` | `hasattr` guard instead of `on_mount` init |
| M17 | `tui.py ~550` | Spinner timer via `hasattr` + `del` |
| M18 | `tui.py ~1100` | `_safe()` usage inconsistent |
| M19 | `mini_agent.py ~335` | `cancel_event` not passed in CLI mode |
| M20 | `tui_pt.py ~680` | `/shell` exits app and leaks FDs |

---

## 🟢 Low Severity (Remaining)

| # | File | Issue |
|---|------|-------|
| L1 | `api.py:208` | Cache keyed by `id(messages)` — fragile |
| L2 | `api.py:240` | Hardcoded `"mini_agent/1.0"` User-Agent |
| L3 | `retry.py:22` | `_RETRYABLE_STATUSES` mutable set → should be frozenset |
| L4 | `retry.py:36` | Magic `(10, 120)` timeout defaults |
| L5 | `retry.py:28` | Magic `0.5` jitter |
| L6 | `stream.py:67` | `"[DONE]"` inline vs `_SSE_PREFIX` constant |
| L7 | `prompt.py:95` | `except Exception: pass` swallows git failures |
| L8 | `prompt.py:76` | `import subprocess` in function body |
| L9 | `config.py:529` | `".db"` literal vs MEMORY_FILENAME |
| L10 | `memory.py ~620` | TODO: split pruning functions |
| L11 | `memory.py ~960` | TODO: split save functions |
| L12 | `agent_runtime.py` | Late import of `release_all_files` |
| L13 | `agent_runtime.py` | Inbox ring-buffer silent truncation |
| L14 | `sub_agent.py ~235` | Safety cap formula inconsistency |
| L15 | `sub_agent.py ~370` | Token budget check twice per turn |
| L16 | `terminal.py:44` | `except Exception: pass` |
| L17 | `terminal.py:16` | `_WINDOWS_ANSI_ENABLED` mutable global |
| L18 | `tools/lsp.py ~110` | No timeout on LSP communicate() |
| L19 | `tools/mcp_client.py ~45` | Hardcoded 5s startup wait |
| L20 | `tools/mcp_client.py ~180` | Ad-hoc client manager on None |
| L21 | `tools/browser_ops.py ~120` | Hardcoded screenshot path |
| L22 | `tools/__init__.py:171` | `globals().__setitem__()` |
| L23 | `tui.py:815 vs mini_agent.py:200` | Inconsistent `/clear` |
| L24 | `tui_pt.py:700` | `open(path, "w")` without encoding |
| L25 | `tui_pt.py:625` | Double backslash in format string |
| L26 | `tui.py:530` | `_TOOL_CONTEXT.__dict__` mutation |
| L27 | `tui.py:540` | Drain at 60fps wastes CPU |
| L28 | `tui.py:470; tui_pt.py:630` | `/theme` stub, no switching |
| L29 | `memory.py` | No lock on MemoryStore |
| L30 | `sub_agent.py ~590` | Auto-snapshot fragile to refactoring |

---

## Files Modified This Session

| File | Change |
|------|--------|
| `tools/agent_ops.py` | `_DEFAULT_MAX_TURNS`: 15 → 25 |
| `tools/browser_ops.py` | Added `_BROWSER_LOCK`, docstring |
| `api.py` | `threading.local()` cache |
| `mini_agent.py` | write_gate check + try/finally |
| `tui_pt.py` | write_gate check + proper export |
| `test_mini_agent.py` | Updated for new `_export_conversation` signature |
