# Audit Report: memory/runtime modules

## Files Audited
| File | Lines | Purpose |
|------|-------|---------|
| `memory.py` | ~1290 | SQLite persistence, token pruning, compression, project knowledge |
| `agent_runtime.py` | ~340 | Thread-safe sub-agent registry, structured results, inter-agent messaging |
| `sub_agent.py` | ~717 | Sub-agent execution loop, safety caps, system prompt |

---

## Findings

| Severity | File | Line/Area | Issue | Fix |
|----------|------|-----------|-------|-----|
| LOW | memory.py | `_prune_by_tokens` (~L620) | Unresolved TODO: "consider splitting message-count cap and token-budget pruning into separate helpers". Function is ~50 lines. | Split into `_cap_by_count()` and `_cap_by_tokens()` as the TODO suggests. |
| LOW | memory.py | `MemoryStore.save` (~L960) | Unresolved TODO: "consider splitting compression, pruning, summarization, and SQL writes into separate helpers". Function is ~70 lines. | Extract `_persist_incremental()` and `_persist_full_rewrite()` helpers. |
| LOW | memory.py | `_clean_messages` | Two-pass validation is ~60 lines. Well-commented and correct but borders on "too long". | Consider extracting the forward-pass reverse-scan into `_truncate_incomplete_tool_sequences()`. |
| INFO | memory.py | `_get_conn` | Dead-connection detection with `SELECT 1` ping + transparent reconnect. **This is well done.** | None — keep this pattern. |
| INFO | memory.py | `_token_count` accumulator | Incremental token accounting avoids re-scanning all messages on every save. Clever but fragile: if save fails and caller discards return value, the accumulator drifts. | Document that callers MUST replace their list with `save()`'s return value, or reset `_token_count` on save failure. |
| LOW | memory.py | `_start_background_vacuum` | Daemon thread VACUUM. If agent process exits mid-VACUUM, the DB could be left in a transient state (though WAL + SQLite crash recovery should handle this). | Low risk; WAL mode + SQLite's atomic commit should cover this. Add a comment noting the assumption. |
| INFO | memory.py | Project knowledge (`add_knowledge`, `get_top_knowledge`, `bump_knowledge`) | Clean cross-session learning with `importance * (hits + 1)` scoring. **Well designed.** | None. |
| INFO | memory.py | `export_conversation_markdown` | Shared export helper used by both terminal REPL and TUI. **Good deduplication.** | None. |
| LOW | agent_runtime.py | `store_result` | `release_all_files(task_id)` is late-imported from `tools` to avoid circular deps. Works but is fragile if `tools` module is restructured. | Document the circular dependency in a module-level comment. |
| INFO | agent_runtime.py | `_gc_stale` | Cleans up tasks from previous sessions on init. **Good hygiene.** | None. |
| INFO | agent_runtime.py | `mark_abandoned` + zombie detection | Prevents zombie threads from corrupting runtime state after timeout. **Solid pattern.** | None. |
| LOW | agent_runtime.py | `append_inbox` | Ring-buffer cap at 1000 messages. The cap is arbitrary — if an agent genuinely needs to track >1000 messages, this silently drops old ones. | Make the cap configurable or log a warning when truncation occurs. |
| INFO | agent_runtime.py | `update_snapshot` | Auto-recorded status snapshots every turn with thought streaming support. **Well designed for orchestrator visibility.** | None. |
| INFO | sub_agent.py | Compression in sub-agent loop | Sub-agents run compression every turn once above threshold, not just every 5th. **Good fix for 400 errors.** | None. |
| INFO | sub_agent.py | System prompt | Clear COMPLETION CRITERIA, REPORT FORMAT, and SCOUT-THEN-DRILL guidance. **Strong prompt engineering.** | None. |
| LOW | sub_agent.py | `_ABSOLUTE_SAFETY_CAP` | Hard cap at a fixed number of turns. If a sub-agent is genuinely making progress on a large task, it hits the wall. | Consider a "progress-based" extension: if the sub-agent has produced output recently, auto-extend. |
| INFO | All | Error handling | Consistently uses `warnings.warn` for non-fatal DB errors and falls back gracefully. | None — consistent pattern. |
| INFO | All | Type annotations | `from __future__ import annotations` used consistently. Most public APIs have type hints. | None — already at a good standard. |
| LOW | All | Thread safety | `AgentRuntime` uses `_lock` for all state mutations. `MemoryStore` has no internal lock — relies on callers serializing access. If two threads call `save()` concurrently on the same store, the `_token_count` and `_last_saved_count` could race. | Add a `threading.Lock` to `MemoryStore` or document that it is not thread-safe for concurrent writes. |

---

## Summary

**Overall quality: HIGH.** The codebase shows mature patterns:
- **Defensive SQLite**: WAL mode, busy timeout, dead-connection detection, retry loops, background VACUUM
- **Token-aware pruning**: Turn-boundary-preserving, incremental accounting, summary injection for pruned context
- **Thread-safe runtime**: Lock-guarded state, zombie detection, ring-buffer inboxes, auto-snapshots
- **Clean inter-agent comms**: Typed handoffs, subscriptions, broadcast stream
- **Sub-agent safety**: Multiple layers of caps, compression, orphan stripping

**Actionable items (all LOW severity):**
1. Resolve the two TODO markers in `memory.py` (split long functions)
2. Add a lock or documentation to `MemoryStore` for thread safety
3. Log a warning when inbox ring-buffer truncation occurs
4. Consider progress-based auto-extension for sub-agents near the safety cap

**No HIGH or MEDIUM severity issues found.** No data-loss risks, no race conditions that would corrupt state in single-agent use, and no API contract violations.
