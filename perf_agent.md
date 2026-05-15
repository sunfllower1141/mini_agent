# Performance Audit: Agent Coordination System

## 1. Thread Pool Efficiency

- **`_spawn_one`** (`agent_ops.py:87`): creates `threading.Thread(target=_runner, daemon=True)` per agent. Daemon threads terminate when main thread exits — no explicit cleanup. Thread-per-agent model means N threads for N sub-agents (max 5 concurrent per `_MAX_CONCURRENT`, `agent_ops.py:39`).
- **Overhead**: each thread holds a full Python stack (~8 KB) plus module-scope imports cloned via closure. At 5 concurrent, overhead is negligible (~40 KB). However, zombies can accumulate via `collect_agent` timeouts: the thread is abandoned (`mark_abandoned`, `agent_runtime.py:169`) but continues running until its LLM call completes. No thread pool reuse — each spawn creates/destroys a thread.
- **No bounded executor**: `threading.Thread` directly, not `ThreadPoolExecutor`. Spikes beyond `_MAX_CONCURRENT` are rejected, not queued.

## 2. Message Routing — Inbox Polling, Linear Scans

- **`agent_inbox`** (`agent_ops.py:660`): `runtime.get_inbox(task_id)` returns `list(self.inboxes.get(task_id, []))` — O(1) dict lookup, then full list copy (`agent_runtime.py:218`). List grows unbounded per agent — no pruning mechanism except cleanup on `store_result`/`mark_abandoned`.
- **`_route_message`** (`agent_messages.py:210`): iterates over *all* `subscriptions` dict entries and *all* `inboxes` keys — **two linear scans** of the full agent set per message. O(N) where N = active+tombstoned agents with inboxes. At 5 agents, irrelevant; at 100+, noticeable.
- **`agent_read`** (`agent_ops.py:614`): slices `_AGENT_MSGS[since:]` under a lock. The flat list approach is simple but means *every* agent-read fetches the whole tail of a shared list.

## 3. Snapshot Updates — Frequency & Cost

- **Pre-LLM snapshot** (`sub_agent.py:147`): `runtime.update_snapshot(...)` called every turn before `call_deepseek`. Cost: one `_lock.acquire()` + dict assignment.
- **Post-tool snapshot** (`sub_agent.py:244`): called after every tool execution in the loop. At 5+ tools per turn, that's 5+ snapshot writes per turn.
- **Streaming snapshots** (`sub_agent.py:166-182`): every 50 tokens during streaming (`_STREAM_SNAP_EVERY` = 50), `update_snapshot` is called with thought snippets. A 5000-token streaming response generates ~100 snapshot writes.
- **Cost per write**: `update_snapshot` (`agent_runtime.py:245`) acquires `_lock`, constructs a 10-field dict, assigns to `status_snapshots[task_id]`. ~5 µs per write under no contention. Under contention (parent polling `get_snapshot` while sub-agent writes), lock hold time is minimal but frequency is high.

## 4. File Reservation System — Lock Contention

- **`_FILE_RESERVATIONS`** (`tools/__init__.py:132`): flat `dict[str, str]` under a single `threading.Lock`.
- **`reserve_file()`** (`tools/__init__.py:158`): lock → dict lookup → insert.
- **`release_all_files()`** (`tools/__init__.py:185`): lock → **full dict scan** (`to_release = [p for p, t in ...]`) → N deletions. O(R) where R = total reservations, not just the agent's. Called in `store_result()` (`agent_runtime.py:92`) on agent completion.
- **Contention**: one lock protects all file reservations. When 5 agents complete concurrently, each `store_result()` triggers a `release_all_files()` that scans the entire dict. Low risk at 5 agents, but the O(R) scan is unnecessary — could store `agent_id → set[paths]` for O(1) release.
- **No timeout**: `reserve_file` never blocks — fast-path failure if reserved. Good for contention but means agents don't queue for files.

## 5. Coordination Patterns

- **`fan_in`** (`agent_patterns.py:75`): serial `wait_for` loop — one task at a time, each with full `timeout`. If task 0 takes 120s, tasks 1-4 wait even if already done. The inline comment says "each task gets the full timeout independently" which is misleading — it's sequential, not concurrent waiting.
- **`collect_agent`** (`agent_ops.py:430`): `condition.wait_for(_completed, timeout=30)`. Uses predicate properly to avoid lost wakeups. But `_COLLECT_TIMEOUT=30` means parent agents that need to collect 5 results may spend 150s blocking.
- **`collect_any`** (`agent_ops.py:537`): correct pattern — check completed first, then `condition.wait_for` with predicate. `_COLLECT_ANY_TIMEOUT=10`.
- **`barrier`** (`agent_patterns.py:148`): **polling loop** with `runtime.get_inbox(tid)` per agent per iteration — O(N × P) where N=agents, P=polls. Falls back to `condition.wait(timeout=0.2)` every cycle. The 0.2s sleep is a trade-off between responsiveness and CPU.
- **`pipeline`** (`agent_patterns.py:106`): sequential spawn-wait-collect per stage. Each stage blocks on `condition.wait_for` with full timeout. Sequential by design, but each stage's shared_context serializes the previous result via `json.dumps`.

## 6. `_AGENT_MSGS` Global List — Growth & Cleanup

- **Declaration** (`agent_ops.py:33`): `_AGENT_MSGS: list[dict] = []`. Global module-level mutable list.
- **Write path**: `agent_message` (`agent_ops.py:577`) and `agent_handoff` (`agent_ops.py:647`): both append `msg.to_legacy_dict()` under `_AGENT_MSGS_LOCK`.
- **Ring-buffer cleanup**: `if len(_AGENT_MSGS) > _AGENT_MSGS_MAX: _AGENT_MSGS[:] = _AGENT_MSGS[-_AGENT_MSGS_MAX:]` — slice replacement, O(cap). Cap = 1000.
- **Duplicate data**: every broadcast message appears in TWO places: (a) `_AGENT_MSGS` flat list, (b) each subscribed agent's `inboxes[tid]` list. A message to 5 agents is stored 6 times (1 + 5 inbox copies). The flat list is used only by `agent_read` — it's legacy backward compat that duplicates inbox delivery.
- **Inbox growth**: `inboxes[tid]` lists grow unbounded per agent — no ring-buffer cap. Only cleaned on `store_result()`/`mark_abandoned()` — messages accumulate on running agents indefinitely. A long-running agent with frequent handoffs accumulates thousands of messages.

## Summary of Issues

| Issue | Location | Severity | Impact |
|-------|----------|----------|--------|
| No thread pool — per-agent thread | agent_ops.py:87 | Low | Zombie threads on timeout |
| `_route_message` O(N) scans | agent_messages.py:210 | Low | Fine at ≤5 agents |
| `_AGENT_MSGS` duplicates inbox data | agent_ops.py:33 | Medium | 2× storage per message |
| Inbox lists unbounded per agent | agent_runtime.py:218 | Medium | Memory leak on long agents |
| `release_all_files` O(R) full scan | tools/__init__.py:188 | Low | Fine at small scale |
| `fan_in` serial not concurrent wait | agent_patterns.py:75 | Medium | Earlier tasks starve later ones |
| `barrier` polling loop | agent_patterns.py:148 | Low | 0.2s sleep trade-off |
| Streaming snapshots at 50-token granularity | sub_agent.py:168 | Low | ~100 writes per streaming response |
