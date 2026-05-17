# mini_agent — Feature Audit

> Auto-generated audit. 906 tests, 49 tools, 0 failures. 2025-06-30.

## Tool System (49 tools)

### File Operations (9)
| Tool | Description | Key Feature |
|------|-------------|-------------|
| `read_file` | Read file contents (offset/limit) | 300-line default, 1000-line max |
| `write_file` | Write/overwrite a file | Auto-backup before write |
| `edit_file` | Search-and-replace in file | **3-pass fuzzy whitespace matching** (exact → trailing-tolerant → indent-tolerant) |
| `list_directory` | List directory contents | — |
| `file_info` | File/dir metadata (size, mtime, type) | — |
| `write_scratchpad` | Persistent working note across turns | Markdown, overwrites previous |
| `diff` | Show unstaged git changes | Per-file or all |
| `restore_file` | Restore from session backup | Undo last write/edit per path |
| `plan` | Declare numbered task plan | Auto-shown each turn until complete |
| `plan_status` | Mark step complete or view progress | — |

### Shell Operations (4)
| Tool | Description | Key Feature |
|------|-------------|-------------|
| `run_shell` | Execute shell command in workspace | 60s timeout (max 300s), background mode, stdin pipe |
| `run_tests` | Run pytest in workspace | Specific file or all, background mode |
| `git` | Local git operations | status, diff, log, init, add, commit, show, restore |
| `task_status` | Poll background task by ID | — |

### Search (7)
| Tool | Description | Key Feature |
|------|-------------|-------------|
| `find_symbol` | Locate Python symbol definitions | Indexed at startup (`.mini_agent_index.json`) |
| `find_usages` | Find all references to a symbol | Faster than grep |
| `search_files` | Text/regex search in workspace | 200-result cap, pagination |
| `semantic_search` | Embeddings-based code search | Per-file mtime invalidation |
| `web_search` | Exa web search | auto/fast/deep, 5-20 results |
| `recall_turn` | Recall previous turn details | For lost context after pruning |
| `read_image` | Describe image via GPT-4o | — |

### LSP (4)
| Tool | Description |
|------|-------------|
| `lsp_definition` | Go to definition (pylsp) |
| `lsp_references` | Find all references (pylsp) |
| `lsp_hover` | Type/docs on hover (pylsp) |
| `lsp_diagnostics` | Errors/warnings for file (pylsp) |

### MCP (2) — NEW this session
| Tool | Description |
|------|-------------|
| `mcp_discover` | List all MCP tools from connected servers |
| `mcp_call` | Call an MCP tool by server/tool/arguments |

### Verification (2)
| Tool | Description |
|------|-------------|
| `verify` | Lint + tests for modified files |
| `diagnose_failures` | Parse last test output for failures |

---

## Multi-Agent System (11 tools)

### Agent Lifecycle (6)
| Tool | Description | Key Feature |
|------|-------------|-------------|
| `spawn_agent` | Spawn sub-agent(s) with task description | Up to 10 tasks in one call, shared_context |
| `agent_status` | Non-blocking status check | Auto-captured snapshot every turn (thought, tool, scratchpad) |
| `collect_agent` | Block until sub-agent completes | 30s timeout, returns full result |
| `collect_any` | Grab first completed result | 10s timeout, keeps pipeline moving |
| `agent_extend` | Grant more turns to running sub-agent | +10 default, max 35 total |
| `agent_cancel` | Cancel a running sub-agent | Stops at next turn boundary |

### Coordination Patterns (5)
| Tool | Description |
|------|-------------|
| `fan_out` | Spawn N workers from task descriptions in one call |
| `fan_in` | Block until all specified agents complete |
| `pipeline` | Sequential stages, each waits for previous |
| `barrier` | Block until all agents complete (sync point) |
| `scatter_gather` | Apply template across items in parallel |

### Inter-Agent Communication (4)
| Feature | Description |
|---------|-------------|
| `agent_message` | Broadcast text to all agents |
| `agent_read` | Read broadcast messages (since index) |
| `agent_handoff` | Typed structured result (handoff.result, handoff.request, coord.*, status.*) |
| `agent_inbox` | Read typed inbox for any agent (ring-buffer, 1000 cap) |
| `agent_subscribe` | Declare message type subscriptions per agent |

### Key Multi-Agent Features
- **Auto-wake**: sub-agent completions injected as user messages before `input()` blocks — no missed finishes
- **Auto-extend**: agents ≤3 turns from budget and making progress get +10 turns (max 35)
- **Stale agent GC**: threads from previous sessions cleaned up on startup
- **File reservations**: `threading.Lock` prevents cross-agent write collisions
- **Max 10 concurrent**: configurable via `sub_agent_max_concurrent` in TOML
- **Max 35 turns per agent**: 25 default, extendable
- **Sub-agents CAN spawn sub-agents**: recursive decomposition supported
- **Streaming snapshots**: 200-token granularity for visible agents
- **Auto-pruning**: sub-agents prune memory every 5 turns when >20 messages

---

## Memory & Learning

### Conversation Persistence
- **SQLite-backed**: `memory.py` stores all messages
- **Token-aware pruning**: oldest tool results compressed to 5-line summaries
- **Progressive compression**: tool results → summaries → full turn pruning
- **Cross-session**: workspace-scoped, multiple named sessions

### Project Knowledge
- **`project_knowledge` table**: persist learnings across sessions
- **`remember` tool**: manually capture patterns, workarounds, conventions
- **Auto-learn**: `remember()` fires automatically after mistakes (edit_file failures, deleted files, polling loops, forgotten imports)
- **Workspace tree cache**: mtime-gated, skips `os.walk` on restart

### Scratchpad
- **`write_scratchpad`**: persistent working note across turns
- **Shown at start of every turn**: external memory for orchestrator
- **Track active**: plan, progress, decisions, open questions

### Performance
- **Symbol index**: persisted to `.mini_agent_index.json` (mtime-gated, incremental reindex)
- **Semantic search**: per-file mtime invalidation
- **Incremental message cleaning**: cache survives across turns
- **Circuit breaker**: `deque.popleft()` for O(1) window management
- **`_pipe` dependency detection**: short-circuits before JSON parse

---

## UI & Interfaces

### Terminal REPL (`mini_agent.py`)
- Auto-wake from sub-agent completions
- Session commands: `quit`, `clear`, `/export`, `/stats`, `/session`
- ANSI color support (configurable via `--no-color`)
- Scratchpad display before each turn

### Textual TUI (`tui.py`)
- Streaming token-by-token with tool output
- Tree view, themes, diff preview
- Sub-agent streaming panes

### Electron Desktop (`electron_bridge.py`)
- JSON-RPC bridge over stdin/stdout
- Electron app at `electron_app/`

### LSP Integration (`tools/lsp.py`)
- pylsp auto-started on first use
- 4 LSP tools: definition, references, hover, diagnostics

### MCP Client (`tools/mcp_client.py`)
- stdio JSON-RPC tool discovery
- `mcp_discover` + `mcp_call` tools registered
- Server config in `.mini_agent.toml [agent.mcp_servers]`

### Interjection (`interject.py`)
- Thread-safe user interjection queue
- `/cancel` support during streaming

---

## Safety & Config

### Safety Gates (`safety.py`)
- **ReadSafetyGate**: workspace boundary enforcement
- **WriteSafetyGate**: overwrite protection, backup-before-write
- **`generate_diff()`**: unified diff preview for write operations (ANSI-colored)
- **Backup-before-delete**: `run_shell rm` auto-backs up files (this session)
- **Safety flags**: unrestricted, allow_overwrites, approve_write_ops

### Configuration (`config.py`)
- **Layer priority**: CLI > env var > .env > TOML > default
- **TOML config**: `.mini_agent.toml` in workspace
- **Separate API keys**: `DEEPSEEK_API_KEY` + `SUB_AGENT_API_KEY`
- **max_tokens**: 200k
- **max_messages**: 500

### Prompt (`prompt.py`)
- System prompt with dynamic workspace/safety header
- `.mini_agent.rules` injection (project-specific conventions)
- Project knowledge injection from memory store

---

## Resilience

### Retry (`retry.py`)
- Exponential backoff with jitter
- HTTP retry for API calls

### Stream (`stream.py`)
- SSE stream parser
- Token-by-token streaming

### Error Handling
- All tool results structured `ToolResult` dataclasses — never raw exceptions
- `diagnose_failures` reads last test output from memory store
- `verify` runs lint + relevant tests for modified files

---

## Test Coverage

| Total | Pass | Fail | Skip |
|-------|------|------|------|
| 906 | 906 | 0 | 4 |

28 test files covering:
- File operations (fuzzy matching, safety gates, diffs)
- Shell operations (run_shell, run_tests, git)
- Search (symbol index, semantic, web)
- Multi-agent (runtime, patterns, messages, sub-agents)
- Memory (persistence, pruning, compression, knowledge)
- UI (TUI, terminal, interjection)
- MCP (client, JSON-RPC)
- LSP (pylsp integration)
- Retry, stream, safety, config, prompt

---

## Electron Desktop App — Agent Audit (65522338)

# Electron Bridge Audit — mini_agent

## 1. JSON-RPC Bridge (`electron_bridge.py`)

### 1.1 Protocol Design
- **Wire format**: JSON-RPC 2.0 over newline-delimited JSON (one JSON object per line on stdout/stdin).
- **Message types**:
  - Standard JSON-RPC request/response (`jsonrpc`, `id`, `method`/`result`).
  - Streaming token messages (`{"type": "token", "content": "..."}`) — these are NOT JSON-RPC, just in-band JSON lines interleaved with RPC responses during an active `chat` call.
- **No keep-alive or heartbeat**. The bridge is fire-and-forget on stdin lines; no retry or acknowledgement at the protocol layer.

### 1.2 State Management
- Module-level globals: `_config`, `_write_gate`, `_read_gate`, `_memory`, `_messages`.
- All are `None` until `init` is called.
- A single `threading.Event` (`_cancel_event`) gates cancellation — set by `cancel`, checked by `run_agent_turn`.
- **Single-session**: only one init call per process. Re-initializing overwrites globals with no cleanup of the previous session's memory.

### 1.3 RPC Methods

| Method   | Handler            | Description |
|----------|--------------------|-------------|
| `init`   | `_handle_init`     | Bootstraps session: calls `init_session(workspace)`, stores config/gates/memory/messages. |
| `chat`   | `_handle_chat`     | Appends user message, calls `run_agent_turn()`, streams tokens via `on_token` callback, returns final assistant content. |
| `cancel` | `_handle_cancel`   | Sets `_cancel_event`. |

### 1.4 Error Codes
| Code   | Meaning |
|--------|---------|
| -1     | Init failed (exception during `init_session`) |
| -2     | Not initialized (chat/cancel called before init) |
| -3     | Empty message |
| -4     | Turn error (exception in `run_agent_turn`) |
| -5     | Generic handler exception |
| -32601 | Method not found |
| -32700 | JSON parse error |

### 1.5 Concerns
- **No concurrent chat sessions.** `_messages` is a single shared list. Two parallel `chat` calls would corrupt state.
- **No idempotency or deduplication.** If the renderer retries a chat message (duplicate `id`), it gets appended again.
- **Token streaming is fire-and-forget.** If stdout is not consumed fast enough, writes will block the Python process (no buffer/drop logic).
- **Error responses may be missing `jsonrpc` field** — `_error()` does include it, verified. ✅
- **`main()` blocks on `sys.stdin` iteration**. No graceful shutdown signal other than EOF on stdin.

---

## 2. stdin/stdout Protocol

### 2.1 Parsing (Python side)
```
main() → for line in sys.stdin: → json.loads(line.strip())
```
- Blank lines are silently skipped.
- JSON parse errors return `-32700` with `id: None` (since the request couldn't be parsed).
- No max line length limit — a deliberately large line could OOM.

### 2.2 Writing (Python side)
```
_write_line(obj) → sys.stdout.write(json.dumps(obj) + "\n") + sys.stdout.flush()
```
- Every JSON object is a single line (no embedded newlines — safe).
- `flush()` is called on every write — good for real-time streaming, but could be chatty under high throughput (though throughput is inherently low for LLM tokens).

### 2.3 Consuming (Electron main process side)
```js
pythonProcess.stdout.on("data", (data) => {
    mainWindow.webContents.send("bridge:stdout", data.toString().trim());
});
```
- **Problem**: `data` chunks may contain partial lines or multiple lines. `trim()` strips whitespace but does NOT split on newlines. If two JSON objects arrive in one chunk, the renderer receives a single string with an embedded newline, which will fail `JSON.parse()`.
- **Fix needed**: Buffer stdout and split on `\n`, emitting one line per `bridge:stdout` message.

### 2.4 Sending (Electron main process side)
```js
ipcMain.handle("bridge:send", (_event, message) => {
    pythonProcess.stdin.write(message + "\n");
});
```
- Assumes `message` is already a JSON string (one line). No validation that it contains no newlines.
- If `pythonProcess` is null or stdin is not writable, returns `{ ok: false }` — handled cleanly.

---

## 3. IPC Setup (`main.js` ↔ `preload.cjs` ↔ Renderer)

### 3.1 Architecture
```
Renderer (index.html)
    ↕  window.bridge.*  (via contextBridge)
Preload (preload.cjs)
    ↕  ipcRenderer.invoke / ipcRenderer.on
Main Process (main.js)
    ↕  child_process.spawn stdin/stdout
Python Bridge (electron_bridge.py)
```

### 3.2 Context Bridge API (`preload.cjs`)
```js
window.bridge = {
    send(message),       // → ipcRenderer.invoke("bridge:send", message)
    stop(),              // → ipcRenderer.invoke("bridge:stop")
    onStdout(callback),  // → ipcRenderer.on("bridge:stdout", ...)  returns unsubscribe fn
    onStderr(callback),  // → ipcRenderer.on("bridge:stderr", ...)  returns unsubscribe fn
    onClose(callback),   // → ipcRenderer.on("bridge:closed", ...)  returns unsubscribe fn
}
```
- `contextIsolation: true`, `nodeIntegration: false` — **secure defaults**. ✅
- Each listener returns an unsubscribe function — clean cleanup pattern. ✅
- No `removeAllListeners` exposed — renderer can only unsubscribe its own callbacks. ✅

### 3.3 IPC Channels
| Channel          | Direction         | Handler                        |
|------------------|-------------------|--------------------------------|
| `bridge:send`    | Renderer → Main   | Writes to Python stdin         |
| `bridge:stop`    | Renderer → Main   | Kills Python process           |
| `bridge:stdout`  | Main → Renderer   | Forwarded Python stdout lines  |
| `bridge:stderr`  | Main → Renderer   | Forwarded Python stderr lines  |
| `bridge:closed`  | Main → Renderer   | Python process exit code       |

### 3.4 Concerns
- **No event for Python process startup.** The renderer just calls `init` and hopes the bridge is ready. If Python starts slowly, the `init` write will fail (`{ ok: false }`) and the renderer gets "Send error: Python bridge not ready". No retry or readiness signal.
- **No reconnection logic.** If Python crashes, `bridge:closed` fires, but the renderer must manually call `init` again after a restart — which the main process doesn't support (no `startPythonBridge` re-invoke).
- **`bridge:stderr` is not used by the renderer** (no listener registered in `index.html`). Diagnostics from Python stderr are silently lost.

---

## 4. Window Creation (`main.js`)

### 4.1 BrowserWindow Config
```js
new BrowserWindow({
    width: 900,
    height: 700,
    webPreferences: {
        preload: path.join(__dirname, "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
    },
});
```
- Fixed size, no `minWidth`/`minHeight` — resizable by default but no lower bound.
- No `title` set — defaults to "mini_agent" (from `<title>` in HTML).
- No icon set.
- **No `sandbox: true`** — preload script runs unsandboxed (defaults to false). For a chat app this is acceptable since `contextIsolation` is enabled.

### 4.2 Lifecycle
```
app.whenReady()
    → createWindow()           // synchronous
    → startPythonBridge()      // spawns Python child process
    → app.on("activate", ...)  // macOS dock re-click → recreate window
app.on("window-all-closed", ...)
    → stopPythonBridge()
    → app.quit()
```
- **Race condition**: `createWindow()` and `startPythonBridge()` are called sequentially but both are async (window creation is deferred, process spawn is immediate). The Python process may be ready before the window finishes loading, or vice versa. The renderer's `DOMContentLoaded` handler calls `init` immediately — if Python isn't ready, it fails silently (see §3.4).
- **`activate` handler** only recreates the window, does NOT restart the Python bridge. If the bridge died while the window was closed, the new window connects to a dead process.

### 4.3 Shutdown
- `stopPythonBridge()` checks `!pythonProcess.killed` before killing — good.
- On window close, `mainWindow = null` and bridge is killed. Clean.

---

## 5. Renderer Architecture (`index.html`)

### 5.1 Structure
- Pure HTML + vanilla JS (no framework). Single-file.
- CSS: Custom Catppuccin Mocha theme, flexbox layout.
- Three DOM regions: `#messages` (scrollable), `#status` (status bar), `#input-area` (form).

### 5.2 State Machine
```
[Loading] → init RPC → [Ready] → user types → chat RPC → [Streaming] → result → [Ready]
                                                                           → error → [Ready]
```
- `initialized` boolean guards transition from Loading → Ready.
- `assistantMsgEl` tracks the currently-streaming message element (appended to as tokens arrive).

### 5.3 Message Processing (`handleBridgeLine` / `processMessage`)
1. Attempt `JSON.parse(line)`.
2. If parse fails → display as system message (startup banner, etc.).
3. If `jsonrpc` result with `status: "ok"` and not yet initialized → setReady().
4. If `type: "token"` → append to `assistantMsgEl` (create if needed).
5. If `jsonrpc` result with content → finalize assistant message (clear `assistantMsgEl`, add persistent message).
6. If `jsonrpc` error → display error message.

### 5.4 Concerns
- **Line splitting bug** (from §2.3): `bridge.onStdout(handleBridgeLine)` passes the raw chunk. If Python flushes multiple lines in one write (e.g., a token + a log line), `JSON.parse` fails and the entire chunk is shown as a system message.
- **No message deduplication by `id`.** If the Python bridge echoes a response twice (unlikely but possible with buffering), the renderer processes both.
- **`sendPending` flag** is declared but never set/checked — dead code.
- **CSP policy**: `script-src 'self' 'unsafe-inline'` — needed for the inline `<script>` block. `style-src 'self' 'unsafe-inline'` — needed for inline `<style>`. Reasonable for a local-only Electron app. ✅
- **`max-width: 80%` on messages** — long tokens (code blocks) may look cramped. No horizontal scroll for overflow.
- **No error recovery UI.** If init fails, the user sees "Send error: Python bridge not ready" but gets no retry button or guidance.
- **No disconnect indicator.** If `bridge:closed` fires, there's no visible UI change (no listener registered for it yet — code was truncated but the section showing it may be in the full file).

---

## 6. Summary of Findings

| Severity | Finding | Location |
|----------|---------|----------|
| **HIGH** | stdout chunk not split on newlines — multi-line chunks break JSON parse | `main.js:29` |
| **HIGH** | No readiness signal from Python → renderer init races process startup | `main.js:15-38`, `index.html` DOMContentLoaded handler |
| **MEDIUM** | No reconnection or bridge restart capability | `main.js` (missing `restartPythonBridge`) |
| **MEDIUM** | Python stderr never surfaced to user | `index.html` (no `bridge.onStderr` listener) |
| **LOW** | `sendPending` variable declared but unused | `index.html` script |
| **LOW** | No max line length limit on stdin parser | `electron_bridge.py:109` |
| **LOW** | Single-session design with no concurrency guard | `electron_bridge.py` globals |

---

## 7. Recommended Fixes (Priority Order)

1. **Buffer stdout by lines in `main.js`**: Accumulate chunks, split on `\n`, emit complete lines one at a time.
2. **Add a `bridge:ready` IPC event**: Python process writes a ready signal on stdout after `main()` starts; main process forwards it to renderer; renderer waits for it before calling `init`.
3. **Add `restartPythonBridge()`** and expose it via IPC (`bridge:restart`) for recovery after crashes.
4. **Wire up `bridge.onStderr`** in the renderer to show Python errors in the UI (at least log to console).
5. **Remove dead `sendPending`** variable or implement send queueing.


---

## UI & Safety — Agent Audit (04074912)

# mini_agent — UI, Safety, & Configuration Audit

**Date:** 2026-05-15  
**Files audited:** `tui.py`, `mini_agent.py`, `safety.py`, `config.py`, `prompt.py`, `interject.py`

---

## 1. Architecture Overview

```
┌──────────────┐  ┌──────────────┐
│   tui.py     │  │ mini_agent.py│  ← Entry points (TUI / terminal REPL)
│  (1290 LOC)  │  │  (277 LOC)   │
└──────┬───────┘  └──────┬───────┘
       │                 │
       └────────┬────────┘
                │
       ┌────────▼────────┐
       │   config.py     │  ← AgentConfig, sessions, themes, init_session()
       │   (656 LOC)     │
       └────────┬────────┘
                │
    ┌───────────┼───────────┐
    │           │           │
┌───▼───┐ ┌─────▼─────┐ ┌──▼──────────┐
│safety │ │ prompt.py │ │ interject.py │
│274 LOC│ │  308 LOC  │ │   50 LOC     │
└───────┘ └───────────┘ └──────────────┘
```

---

## 2. TUI (`tui.py`) — 1,290 lines

**Framework:** Textual (RichLog, TextArea, Header, Footer, Tree, HorizontalScroll)

### 2.1 Strengths

| Area | Notes |
|------|-------|
| **Theme system** | 9 named themes (`THEMES`) with RGB tuples for bg, surface, text, accent, dim, green/yellow/red, code-bg. Applied via `_apply_theme()` which sets `.styles` directly (bypasses CSS). |
| **Streaming** | Token-by-token rendering with `_buf` accumulation, flushed on `\n` or `\n\n` boundaries. Handles thinking blocks (`_in_thinking`), code blocks, table detection. `_drain()` loop pulls from `_NotifyQueue`. |
| **Sub-agent support** | Multi-agent streaming via `sub_token` queue messages. Dynamically mounts `RichLog` panes in a `HorizontalScroll` (`#subagent-pane`), one per task_id. Color-coded borders. |
| **Sub-agent tree view** | Live `Tree` widget (`#agent-tree`) updated on spawn/status/complete events. Flat `_tree_node_map` dict for O(1) node lookup — avoids recursive tree walks. |
| **Session management** | `/session new/switch/delete/list` commands. Saves current session before switching. Maintains `_total_turns` and `_total_tokens` counters. |
| **Export** | `/export` writes conversation as Markdown to `conversation_YYYYMMDD_HHMMSS.md` via `_export_to_file()`. |
| **Command palette** | `/help`, `/clear`, `/copy`, `/undo`, `/reset`, `/theme`, `/session`, `/export`, `/git` all routed through `_handle_command()`. |
| **Interjection** | While agent is running, user input is queued via `interject.push_interjection()` instead of being lost. Displayed as `[bold]⏎ Queued (will inject)[/]`. |
| **Git status** | `_refresh_git_status()` runs `git branch --show-current` and `git status --porcelain` every 2s via `set_interval()`. Shows branch + dirty indicator in footer. |
| **Status bar** | `_update_status_bar()` at 2s interval — model, msgs, tokens, turn count, git info, spinner while running. |
| **Log buffering** | `_chat_buf` and `_tools_buf` with `_flush_logs()` for batched RichLog writes. Reduces DOM overhead. |
| **Approval flow** | `_approval_active` flag. When True, `on_key` intercepts `y/n` keys to approve/reject tool calls without sending to agent. |
| **Copy to clipboard** | `/copy` uses `_last_response` — the last assistant text response. Reliable since tracked at response completion. |
| **Color bypass** | Respects `NO_COLOR` env var and `--no-color` flag via `config.no_color`. Disables Rich markup when set. |
| **Table detection** | `_maybe_table()` heuristic: if `|` appears in ≥2 consecutive lines with consistent column counts, buffers as table; else flushes as code block. |

### 2.2 Issues & Risks

| Severity | Issue | Location | Details |
|----------|-------|----------|---------|
| **MEDIUM** | CSS class mismatch between `tui.tcss` and code | `on_mount()` | `#static-pane` and `#chat-pane` are queried but the CSS may use different IDs. The `_apply_theme()` queries `#static-pane` and `#chat-pane` — if these IDs don't exist in `tui.tcss`, styles are silently skipped (swallows `Exception`). |
| **MEDIUM** | `_NotifyQueue` — thread safety via `asyncio.run_coroutine_threadsafe` | `_NotifyQueue` inner class | Correctly uses `call_from_thread()` to schedule `_drain()` on the asyncio event loop. However, `_drain()` iterates with `get_nowait()` which can starve the event loop on high-volume token streams. Consider yielding periodically. |
| **LOW** | Deeply nested `_drain()` method | `_drain()` (~250 lines) | Handles sub_token, sub_tree spawn/status/complete, regular tokens, thinking, table flushing, and final response in one method. Hard to unit test. Suggest splitting into `_drain_sub`, `_drain_tree`, `_drain_token`, `_drain_turn_end`. |
| **LOW** | `_in_thinking` state machine | `_drain()` | Toggled by `__THINKING_START__` / `__THINKING_END__` string markers embedded in the token stream. Brittle — if the model emits partial markers or the markers appear in code blocks, rendering breaks. |
| **LOW** | Raw `os.system()` for clipboard | `/copy` handler | Calls `os.system("pbcopy")` on macOS. No cross-platform support (Linux needs `xclip`/`wl-copy`, Windows needs `clip`). Should use `pyperclip` or platform detection. |
| **LOW** | `git status --porcelain` every 2s | `_refresh_git_status()` | Polls git on a timer even for large repos. Could be expensive. Consider debouncing or only polling when the agent might have modified files. |
| **LOW** | Agent box open/close tracking | `_agent_box_open` attribute | Dynamic attribute set via `getattr(..., False)`. Works but is fragile. Should be initialized in `on_mount()`. |
| **INFO** | Exception swallowing in `_apply_theme()` | Multiple `try/except Exception: pass` | Hides real CSS/selector bugs. Should at minimum log the failure at debug level. |
| **INFO** | `_table_buf` / `_accumulated_content` | Various methods | Multiple dynamic attribute patterns with `if not hasattr(...)`. Same fragility as `_agent_box_open`. |

### 2.3 UI Behavior Notes

- **Chat pane** (left): Agent responses with colorful box-drawing borders. Thinking is collapsed/hidden by default (dimmed).
- **Tools pane** (right): Tool invocations and results. Each tool call shows name + truncated params. Results shown with dim styling.
- **Sub-agent pane** (bottom): Horizontal scroll of per-agent RichLog widgets. Each colored distinctly.
- **Agent tree** (sidebar): Live tree showing parent-child agent relationships with status icons (running/complete/error).
- **Input area** (bottom): `TextArea` with command history (`_history`, `_history_pos`). Submit on Enter. Shift+Enter for newline.
- **Auto-scroll**: `auto_scroll = True` on RichLog widgets — follows new output automatically.

---

## 3. Terminal REPL (`mini_agent.py`) — 277 lines

### 3.1 Strengths

- Clean, minimal entry point. Delegates initialization to `config.init_session()`.
- Reuses the same `init_session()`, `AgentConfig`, `WriteSafetyGate`, `ReadSafetyGate` as the TUI.
- Supports `--no-color`, `--quiet`, `--stream`, `--allow-overwrites`, `--approve`, `--unrestricted` flags.
- Ctrl+C handling: sets `_SHUTDOWN = True`, injects shutdown message, waits for agent turn to complete gracefully.
- Streams output via `run_agent_turn_streaming()` or `run_agent_turn()` depending on `--stream` flag.
- Saves session on exit.

### 3.2 Issues & Risks

| Severity | Issue | Details |
|----------|-------|---------|
| **LOW** | `run_agent_turn_streaming()` imported but not shown | The function is imported from `agent_runner`. The terminal REPL has fewer streaming affordances than the TUI — thinking blocks are printed dimmed, but no Rich formatting. |
| **LOW** | `_SHUTDOWN` global | Module-level mutable state. Works for single-process terminal REPL but would conflict if `mini_agent` were ever used as a library concurrently. |
| **INFO** | No session management commands in terminal mode | Unlike the TUI, the terminal REPL has no `/session`, `/theme`, `/export`, or `/git` commands. Users must restart to switch sessions. |

---

## 4. Safety (`safety.py`) — 274 lines

### 4.1 Strengths

| Feature | Notes |
|---------|-------|
| **Workspace boundary enforcement** | `_resolve_path()` normalizes and resolves paths. `_is_within_workspace()` checks using `os.path.commonpath()`. Handles symlinks via `os.path.realpath()`. |
| **Read gate** | `ReadSafetyGate.validate()` blocks reads outside workspace. Whitelists common system paths needed by tools: `/dev/null`, `/tmp`, `/proc/`, `/sys/`, `/etc/`, standard library locations. |
| **Write gate** | `WriteSafetyGate.validate()` blocks writes outside workspace. `allow_overwrites` flag controls whether existing files require confirmation. `approve` mode prompts before every write. |
| **Destructive operations** | Separates `_DESTRUCTIVE_COMMANDS` (rm, mkfs, dd, etc.) for special handling. `_NEVER_ALLOW_COMMANDS` blocks curl/wget piped to shell, sudo, chmod 777, etc. |
| **Shell safety** | `sanitize_shell_command()` blocks dangerous patterns: `rm -rf /`, `${IFS}`, backtick injection, `$(...)` with dangerous commands. |
| **Path traversal detection** | Blocks `../` patterns, absolute paths outside workspace, `/etc/passwd`, `~root`, null byte injection. |
| **Unrestricted mode** | `--unrestricted` CLI flag bypasses all boundary checks. Intended for trusted environments. |
| **Approval mode** | `--approve` prompts user for confirmation before each write/delete/destructive operation. Integrates with TUI approval flow. |

### 4.2 Issues & Risks

| Severity | Issue | Details |
|----------|-------|---------|
| **HIGH** | Whitelist bypass via symlink | `_is_within_workspace()` uses `os.path.realpath()` which resolves symlinks, but the whitelist check in `ReadSafetyGate` uses `os.path.abspath()`. A symlink from workspace to `/etc/shadow` could bypass the read gate if the symlink target is checked after realpath but the whitelist bypass is checked before. **Verify:** the check order is `_is_within_workspace()` first (uses `realpath`), then whitelist — but if `realpath` resolves to `/etc/`, it would be caught. Needs explicit symlink-to-whitelisted-path test. |
| **MEDIUM** | `_NEVER_ALLOW_COMMANDS` — string matching | Blocked patterns like `curl … | bash` are matched via substring. Could be bypassed with `curl …|bash` (no spaces), `curl … | /bin/bash`, or encoding tricks. |
| **MEDIUM** | `_DESTRUCTIVE_COMMANDS` — false positives | `rm` is always flagged as destructive, but `rm some_file.py` inside workspace is a normal operation. The approval flow handles this, but the UX could be annoying. |
| **LOW** | `sanitize_shell_command()` — incomplete | Doesn't detect `$(< /etc/passwd)`, process substitution `<(…)`, or `$''` syntax. |
| **LOW** | Whitelist paths are hardcoded | `/etc/localtime`, `/etc/ssl/`, etc. — may differ across distros (e.g., NixOS). Consider making configurable. |
| **INFO** | No structured logging of blocked operations | Would help security audits. Currently just raises `SafetyError` or returns `False`. |

---

## 5. Configuration (`config.py`) — 656 lines

### 5.1 Strengths

| Feature | Notes |
|---------|-------|
| **AgentConfig dataclass** | Clean `@dataclass` with sensible defaults: model, max_tokens, max_messages, temperature, system_prompt_template, mcp_servers, tools, plugins. |
| **YAML-based config files** | `AgentConfig.load()` reads from `~/.config/mini_agent/config.yaml` and workspace-local `mini_agent.yaml`. Merges with CLI overrides. |
| **Env var support** | `AGENT_WORKSPACE`, `MINI_AGENT_THEME`, `MINI_AGENT_MODEL`, `OPENAI_API_KEY`, `EXA_API_KEY` all respected. |
| **Session persistence** | Multiple SQLite databases: `memory.db` per session. `switch_session()` saves current before loading new. `delete_session()` removes DB files. |
| **Session listing** | `list_sessions()` scans workspace for `*.db` files, returns names + message counts + last-modified timestamps. |
| **HTTP session** | `init_session()` creates a `requests.Session` with timeouts (connect=10s, read=120s) and connection pooling (8 conns, 16 max). Registered with `atexit` for cleanup. |
| **MCP support** | Starts MCP client manager if `mcp_servers` configured. Graceful fallback on failure. |
| **Message pruning** | On session load, calls `_clean_messages()`, `_compress_tool_results()`, `_prune_by_tokens()` to keep context within limits. Injects summary of pruned content. |
| **Symbol index** | `build_symbol_index(workspace)` called in `init_session()`. Pre-builds a search index for the `find_symbol` tool. |
| **Knowledge injection** | `build_startup_context()` includes top N `project_knowledge` entries and latest session summary. |

### 5.2 Issues & Risks

| Severity | Issue | Details |
|----------|-------|---------|
| **MEDIUM** | `_prune_by_tokens()` — no tiktoken | Token counting uses character/4 heuristic. Inaccurate for non-English text and code-heavy conversations. Should use `tiktoken` for the specific model. |
| **MEDIUM** | `_compress_tool_results()` — lossy | Truncates tool results to 2000 chars, keeping only first and last 1000. Could lose critical error messages in the middle. Consider truncating from the middle only, or using structured summarization. |
| **LOW** | `parse_args()` — unknown args silently ignored | `parser.parse_known_args()` prints a warning but continues. Could hide typos like `--workpace` (misspelled). |
| **LOW** | Config file merging order | `~/.config/mini_agent/config.yaml` → workspace `mini_agent.yaml` → CLI args → env vars. The precedence chain is complex and undocumented in user-facing help. |
| **LOW** | No config validation | `AgentConfig` has no `__post_init__` validation. Invalid `model` names, negative `max_tokens`, or malformed `mcp_servers` dicts are only caught at runtime. |
| **INFO** | `memory_filename` default is `"memory.db"` | If two agents run in the same workspace, they share the same session DB. Could cause corruption. The session name is embedded in the DB path but `memory_filename` is still configurable separately. |

---

## 6. System Prompt (`prompt.py`) — 308 lines

### 6.1 Strengths

| Feature | Notes |
|---------|-------|
| **Feature negotiation** | `build_system_prompt()` takes `AgentConfig` and builds a prompt that lists only enabled tools/features. If `config.tools` restricts tools, the prompt reflects that. |
| **Tool documentation** | Inline documentation for each tool: description, parameters, usage notes. Generated programmatically from tool metadata. |
| **Safety rules** | Includes rules about workspace boundaries, file overwrite confirmation, destructive operation approval. Consistent with `safety.py` enforcement. |
| **Coding conventions** | Specifies Python style (type hints, docstrings), file handling (atomic writes), error handling patterns. |
| **Multi-agent awareness** | References sub-agent spawning, agent tree, parent-child communication protocols. |
| **Knowledge system** | Documents `project_knowledge`, `remember` tool, and cross-session learning. |
| **Scratchpad** | Documents the agent's `write_scratchpad` tool for persistent working notes. |
| **Config-driven** | Prompt adapts to `config.unrestricted`, `config.allow_overwrites`, `config.approve`, `config.verbose`. |

### 6.2 Issues & Risks

| Severity | Issue | Details |
|----------|-------|---------|
| **MEDIUM** | Prompt length | At ~300 lines, the system prompt is substantial. Combined with startup context, knowledge, and conversation history, the first-turn payload can be 5-10K tokens before any user message. Consider a "slim" mode for simple queries. |
| **LOW** | Tool descriptions are static strings | If a tool's signature changes, the prompt must be manually updated or it becomes misleading. Consider generating tool descriptions from function signatures + docstrings dynamically. |
| **LOW** | No localization support | All prompt text is hardcoded English. Could be a barrier for non-English users. |
| **INFO** | `build_startup_context()` | Injects workspace path, OS info, Python version, git status, file tree summary. Useful but adds ~500-1000 tokens. Should be optional/toggleable. |

---

## 7. Interjection (`interject.py`) — 50 lines

### 7.1 Strengths

- **Thread-safe**: `threading.Lock()` protects the `deque`.
- **Simple API**: `push_interjection()`, `poll_interjections()`, `has_interjections()`.
- **Used by both TUI and terminal REPL**: TUI queues input while agent is running; terminal REPL uses `select`/`stdin` polling with interjection injection.
- **Lightweight**: No dependencies beyond stdlib.

### 7.2 Issues & Risks

| Severity | Issue | Details |
|----------|-------|---------|
| **LOW** | No rate limiting | A user could spam hundreds of interjections while agent is running. `poll_interjections()` returns all of them, which could flood the conversation. Consider a max queue size. |
| **INFO** | No timestamp or ordering metadata | Messages are FIFO but have no source/sequence metadata. Not a problem for single-user but could matter for multi-user scenarios in future. |

---

## 8. Cross-Cutting Concerns

### 8.1 Thread Safety Summary

| Component | Thread Model | Safe? |
|-----------|-------------|-------|
| `interject.py` | `threading.Lock` on deque | ✅ |
| `_NotifyQueue` (TUI) | `asyncio.run_coroutine_threadsafe` → `call_from_thread` | ✅ |
| `_TOOL_CONTEXT.__dict__["_tui_queue"]` | Direct dict mutation | ⚠️ No lock; but set once in `on_mount()` before agent starts |
| `safety.py` | No shared mutable state | ✅ |
| `config.py` | `AgentConfig` is read-only after init | ✅ |

### 8.2 Error Handling Patterns

| Pattern | Used In | Assessment |
|---------|---------|------------|
| `try/except Exception: pass` | `_apply_theme()`, `on_mount()` | ⚠️ Swallows real errors |
| `raise SafetyError(msg)` | `safety.py` | ✅ Good — structured exception with message |
| `print(..., file=sys.stderr)` | `config.py`, `mini_agent.py` | ⚠️ Bypasses TUI log system in terminal mode |
| Return `bool + str` tuple | `delete_session()` | ✅ Good — caller can handle or display |

### 8.3 Dependency Graph

```
tui.py
  ├── config.py (resolve_workspace, parse_args, init_session, switch_session, delete_session)
  ├── safety.py (WriteSafetyGate, ReadSafetyGate — via config.init_session)
  ├── interject.py (push_interjection)
  ├── prompt.py (build_startup_context — via config.init_session)
  └── tools (build_symbol_index — via config.init_session)

mini_agent.py
  ├── config.py (resolve_workspace, parse_args, init_session)
  ├── interject.py
  └── agent_runner (run_agent_turn, run_agent_turn_streaming)

safety.py — standalone (no internal deps beyond os, pathlib)
prompt.py — depends on AgentConfig type (config.py)
interject.py — standalone (stdlib only)
```

---

## 9. Recommendations

### High Priority
1. **Symlink safety test**: Add explicit test in `safety.py` for symlink-to-whitelisted-path bypass attempts.
2. **Token counting**: Replace character/4 heuristic with `tiktoken` for accurate context management in `memory.py`.

### Medium Priority
3. **Split `_drain()` method**: Extract sub-agent handling, tree rendering, and token routing into separate methods.
4. **`_NEVER_ALLOW_COMMANDS` hardening**: Use AST-level shell parsing or at minimum regex-based detection with word-boundary matching.
5. **Config validation**: Add `__post_init__` to `AgentConfig` with range checks and model name validation.
6. **Dynamic tool descriptions**: Generate prompt tool docs from function `__doc__` and `inspect.signature()`.

### Low Priority
7. **Cross-platform clipboard**: Replace `os.system("pbcopy")` with `pyperclip` or `platform.system()` dispatch.
8. **Debounce git polling**: Only poll git status when the agent might have changed files (after tool calls).
9. **Rate-limit interjections**: Cap at 10 queued messages, drop oldest.
10. **Session commands in terminal REPL**: Add `/session`, `/theme`, `/export` parity.
11. **Initialize dynamic attributes in `on_mount()`**: `_agent_box_open`, `_table_buf`, `_accumulated_content`, `_sub_panes`, `_sub_bufs`, `_sub_colors`.

---

## 10. File Summary

| File | Lines | Purpose | Quality |
|------|-------|---------|---------|
| `tui.py` | ~1,290 | Textual-based TUI application | 🟡 Good — some monolithic methods, dynamic attributes |
| `mini_agent.py` | 277 | Terminal REPL entry point | 🟢 Solid — clean delegation |
| `safety.py` | 274 | Read/write safety gates | 🟢 Solid — needs symlink edge-case testing |
| `config.py` | 656 | Configuration, sessions, initialization | 🟡 Good — token heuristic is weak, no validation |
| `prompt.py` | 308 | System prompt builder | 🟢 Solid — could be more dynamic |
| `interject.py` | 50 | Thread-safe user interjection queue | 🟢 Solid — minimal, correct |


---



## Memory & LLM — Agent Audit Findings (1ab3e30e)

### memory.py — Clean, well-structured persistence layer
- SQLite with WAL mode, background VACUUM, incremental saves
- Two-pass message cleaning (orphan detection + incomplete sequence truncation)
- Token-aware pruning with turn boundary preservation
- **Finding**: `_token_count` accumulator drifts after compression (recalculate on full rewrite)
- **Finding**: `capture_session_summary` DELETE+INSERT not wrapped in transaction

### llm.py — Robust agent loop with streaming optimization
- Streaming tool execution (`on_tool_ready`) + post-stream batch execution
- Pipe dependency graph with Kahn's algorithm topological sort
- Circuit breaker (10-call window) prevents infinite loops
- **Finding**: streaming tools with pipe deps may execute before their dependencies
- **Finding**: `_on_tool_ready` mutates the original `tc` dict
- **Finding**: circuit breaker key includes non-normalized args; no cap on parallel thread pool workers

