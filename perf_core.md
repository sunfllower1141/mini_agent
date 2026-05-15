# Performance Audit: Core Orchestration (llm.py + api.py)

## 1. Redundant Operations Per Turn

**`clear_api_cache()` called every turn — kills the incremental cache.**
`llm.py:724-725` — `clear_api_cache()` (→ `api.py:103`) and `clear_tool_cache()` fire at the START of `run_agent_turn`, on every turn. The incremental message-cleaning cache (`api.py:34-35`) is therefore always cold for the first (and usually only) API call of the turn. For the common case of 1 API call/turn, the cache provides zero benefit while adding dict-lookup overhead (`api.py:52-56`).

**`clear_tool_cache()` called every turn unnecessarily.**
`llm.py:726` — if no tools were executed in the previous turn, this is a wasted operation.

**`_TOOL_CONTEXT._turn_history` cap scan is O(k) every turn.**
`llm.py:34-37` — when history exceeds 200 entries, it scans for the lowest missing key via `while oldest not in _TOOL_CONTEXT._turn_history: oldest += 1`. For dense sequential keys this is O(1), but after gaps this becomes O(k) linear scan.

---

## 2. Message Cleaning — Incremental Cache Effectiveness

**The incremental cache (`api.py:34-35`) is technically correct but has minimal real-world value.**
Keyed by `id(messages)`, which is stable since the same list is mutated across turns. But since `clear_api_cache()` empties it at turn start (`llm.py:724`), and most turns make 1 API call, the incremental path (`api.py:56-59` — append clean copy of new messages) only helps during multi-API-call turns (e.g., streaming with tool loops). Within a single-turn corpus of ~200 messages, the cache saves ~1 out of 200 cleanings — a ~0.5% win.

**`_clean_message` is O(n) per first API call of every turn.**
`api.py:46-49` — every message is dict-comprehended (`{k: v for k, v in msg.items() if not k.startswith("_")}`), then `tool_calls` are also recomprehended. For long conversations (hundreds of messages), this means a deep-ish copy of ALL messages every single turn. The `cache_control` injection on the first system message (`api.py:51`) re-checks index==0 every call — trivial.

**Fix opportunity:** Use `list(messages)` shallow-referencing instead of deep-copying every turn, or cache by a stable conversation ID rather than clearing every turn.

---

## 3. Context Injection Helpers

**`_inject_context` (`llm.py:280`) runs ALL helpers every turn — including 6 per-turn helpers that often short-circuit with no work.**
Each turn: `_inject_orchestration_context`, `_inject_interjections`, `_inject_progress_check`, `_inject_circuit_breaker`, `_inject_scratchpad_nudge`, `_inject_plan_status` are called unconditionally. For a turn with no sub-agents, no interjections, and no circuit trip, that's ~6 function calls + attribute accesses to do nothing.

**`_inject_orchestration_context` (`llm.py:138`) checks `get_running_ids()` and `get_pending_results()` every turn.**
`llm.py:140-141` — these call into the runtime object even when no sub-agents have ever been spawned. A fast-path `if not hasattr(runtime, "_agents")` check could short-circuit instantly.

**`_inject_interjections` (`llm.py:166`) calls `poll_interjections()` every turn.**
If `poll_interjections` does a `os.path.isfile` stat on an interjection file that doesn't exist, that's a wasted filesystem stat every turn (~10-50µs each, trivial, but it adds up).

**`_inject_modified_files_checkpoint` (`llm.py:184`) calls `get_modified_files()` only on turn 2.**
Then calls `os.path.isfile` for each modified file to find test candidates. One-time, efficient.

**One-time helpers are fine:** `_inject_scratchpad_context` (flag-gated, `llm.py:86`), `_inject_git_diff` (flag-gated, `llm.py:99`).

**Scalability note:** For very long sessions (500+ turns), the `_inject_scratchpad_nudge` (`llm.py:238`) runs every 3 turns after turn 5 — that's ~165 calls. Each does a trivial attribute check (`_TOOL_CONTEXT._scratchpad_updated`). Fine.

---

## 4. Tool Piping / Execution Groups

**`_extract_pipe_deps` does O(n) JSON round-trip on EVERY tool call batch, even when no tool uses `_pipe`.**
`llm.py:307-322` — iterates all tool calls, runs `json.loads(raw)`, pops `_pipe`, runs `json.dumps(ad)` on each. For the common case (0 tools with `_pipe`), this is pure waste. Each load/dump pair for a typical tool call args (~1KB) takes ~10-20µs. For 20 tools, that's ~400µs of zero-value work.

**Fix opportunity:** Check `"_pipe"` in the raw string (`fn["arguments"]`) before bothering with json.loads. Since `_pipe` is serialized in the JSON, a simple substring check avoids the full parse.

**`_build_execution_groups` (`llm.py:349`) is only called when `pipe_deps` is non-empty** — thanks to the early-return at `llm.py:629-648`. Good.

**`_execute_groups` (`llm.py:380`) creates `results_lock` (`llm.py:415`) for every group, even single-element groups.**
For single-element groups (common with piping), the lock is created but only used inside the `else: len(group) > 1` branch. Micro-optimization, but it's a pattern: 1 per group × N groups = wasted Lock objects.

**`_on_tool_start` double-fire?** In `_execute_parallel_no_pipes` (`llm.py:335-337`), all tools fire `on_tool_start` before any execute. In `_execute_groups` (`llm.py:389-391`), same pattern. No double-fire per tool since only one path is taken.

---

## 5. Circuit Breaker — Efficiency

**`_check_circuit` (`llm.py:45`) is O(k) where k ≤ `_CIRCUIT_WINDOW` (6).**
Builds `Counter(recent_keys)`, iterates to find ≥3 counts. For k=6, this is about as fast as it gets. Efficient.

**`recent_tool_keys` is managed as a list with `pop(0)`.**
`llm.py:975,980` — `pop(0)` on a list is O(n) due to element shifting. For the max window of 6, this is ~6 element shifts per append. At ~6 tool calls per turn, that's ~36 shifts. Negligible, but `collections.deque` with `popleft()` would be the correct O(1) pattern.

**Lock overhead is unnecessary for single-threaded tool execution.**
`llm.py:972,975` — `tool_keys_lock` is always passed (a `threading.Lock`), but when tools are executed sequentially (1 tool, or non-parallel group), there's no contention. The `if lock is not None: with lock:` check runs every time. Fine overhead (~50ns per lock check), but the lock itself is allocated at `llm.py:734` on every `run_agent_turn` call.

---

## Summary of Top-Impact Issues

| Issue | Location | Impact |
|---|---|---|
| `clear_api_cache()` at turn start | llm.py:724, api.py:103 | Kills incremental cache — always cold on first API call |
| O(n) message dict-copy every turn | api.py:46-49 | ~200 dict comprehensions per turn for long sessions |
| `_extract_pipe_deps` JSON round-trip | llm.py:311-319 | ~400µs wasted per tool batch when no pipe deps exist |
| 6 per-turn injectors always called | llm.py:280-305 | ~6 function calls + ~20 attribute accesses per turn even when idle |
| `list.pop(0)` for circuit tracking | llm.py:975,980 | Should use `deque.popleft()` for correctness |
