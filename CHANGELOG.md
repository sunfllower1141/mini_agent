# Changelog

Self-modification audit trail — what the agent changed and why.

## 2026-06-11 — Windows fork prep: requirements.txt refresh, WINDOWS_INSTALL.md
### Added
- **WINDOWS_INSTALL.md**: Comprehensive Windows 11 install guide with prerequisites,
  manual setup steps, launch instructions, keyboard shortcuts, running tests, and
  a full troubleshooting section (Store Python, Defender exclusions, Electron white
  screen, proxy, C++ build tools, PATH length).
### Changed
- **requirements.txt**: Refreshed with Windows-specific comments. Added
  `pytest-timeout>=2.0` (prevents hanging tests). Documented system dependencies
  (ripgrep, Node.js, git, Python) with winget install commands. Added Windows notes
  about PyTorch CPU-only option and Defender exclusions.
- **README.md**: Added reference to WINDOWS_INSTALL.md for Windows users.
### Tests
- 1027 passed, 10 failed (all pre-existing), 34 errors (Win32 teardown PermissionError).

## 2026-06-11 — First tool call: HF Hub warmup encode + sys.executable warmup
### Fixed
- **HF Hub download interrupts first tool call**: After model preload completes in
  bootstrap, added a warmup `model.encode("warmup")` call to trigger any lazy
  initialization (tokenizer downloads, HF Hub auth warnings). This ensures all
  SentenceTransformer setup happens during bootstrap, not during the first tool call
  where it can interfere with concurrent subprocess tool execution.
- **sys.executable warmup**: The bootstrap warmup thread only called `cmd.exe`, but
  tool calls (read_file, run_shell) spawn `python.exe` subprocesses. Added a
  `subprocess.run([sys.executable, "-c", "print"])` warmup to absorb the antivirus
  filter-driver cost for the Python executable separately from cmd.exe.

## 2026-06-11 — First tool call hang: daemon-thread subprocess warmup + preload timeout fix
### Fixed
- **First tool call hangs on Windows (run_shell stuck)**: Two root causes fixed in
  `core/bootstrap.py`:
  1. `_warmup_thread_io` only warmed file I/O, not `subprocess.Popen`. On Windows, the
     first `CreateProcess` from a daemon thread triggers fresh antivirus filter-driver
     scans. Added a `cmd.exe /c rem` invocation inside the warmup daemon thread.
  2. The embedding model preload (`_sem_preload`) started late in bootstrap (after slow
     `build_symbol_index` + `set_lsp_root`) and only waited 30s. On a cold HF cache
     (first-ever run), the model download (~90 MB) would still be in progress when the
     first tool call dispatched to a daemon thread — and the concurrent network I/O from
     the preload thread interfered with tool thread startup. Fix: start `_sem_preload`
     EARLY (before the slow scans), wait at the END with timeout=120s (matching
     `_SEM_MODEL_TIMEOUT`). The slow scans now overlap with the model download.

## 2026-06-11 — Windows tool freeze fixes (bash quoting + CREATE_NEW_PROCESS_GROUP removal + startup warmup)
### Fixed
- **run_shell freeze on Windows**: Bash path `C:\Program Files\Git\bin\bash.exe` was unquoted
  in the wrapper at line 241, and the command was double-wrapped in bash (line 319-327).
  Fix: quoted the bash path, bypassed the double wrapping via `if _WINDOWS and False`.
- **CREATE_NEW_PROCESS_GROUP removed**: Removed `subprocess.CREATE_NEW_PROCESS_GROUP` from all
  subprocess spawns (`_run_shell`, `_run_tests`, `_verify`, `lsp.py`, `mcp_client.py`,
  `file_ops.py`). The flag is unnecessary (taskkill /T works without process groups) and may
  trigger EDR/antivirus behavioral analysis on first invocation, causing ~15-60s freezes.
  Only `CREATE_NO_WINDOW` is kept to prevent conhost.exe window flash.
- **Startup warmup for antivirus**: `core/bootstrap.py` now runs `cmd.exe /c rem` during
  `init_session()` to warm up cmd.exe/conhost.exe before the first user prompt. This absorbs
  any first-call EDR scan delay during startup rather than on the first tool call.
- **read_file hangs on Windows**: `tools/file_ops.py` line 340: `_worker.py` subprocess hangs
  at `open()` on some Windows 11 systems (antivirus filter driver). Bypassed via `if False:`
  — all reads now use `_read_file_direct()` in-process.

## 2026-06-11 — Windows subprocess hardening (run_shell hang + process bomb fix)
### Fixed
- **run_shell hangs on Windows**: Replaced ALL `proc.communicate()` calls in `_run_shell`,
  `_run_tests`, and `_verify` with read threads (`_stream_reader`) + shared `_communicate_windows()`
  helper using `threading.Timer` watchdog that calls `taskkill /F /T`. `proc.communicate()` on
  Windows uses `WaitForSingleObject` which can hang forever in kernel I/O (antivirus hooks,
  filter drivers). The kill-timer approach escapes via OS-level process tree termination.
- **Process bomb (thousands of base.exe)**: `main.js` now throttles backend restarts to max
  3 within 30s with exponential backoff (1.5s → 3s → 6s). Previously, each crash triggered
  an unconditional restart after 1.5s, causing runaway process multiplication.
- **conhost.exe per command**: Added `subprocess.CREATE_NO_WINDOW` to `creationflags` in ALL
  subprocess spawns (`_run_shell`, `_run_tests`, `_verify`, `lsp.py`, `mcp_client.py`) so
  shell subprocesses no longer spawn Windows Console Host instances.
- **Shutdown cleanup**: `window-all-closed`, `before-quit`, and `settings:restartBackend`
  now use `taskkill /F /T /PID` on Windows instead of `proc.kill()` (which only kills the
  immediate process, leaving child trees orphaned).
- **Timeout handler fix**: In `_run_tests` and `_verify`, the `TimeoutExpired` handler no
  longer calls `proc.communicate()` on Windows (the process is already killed by taskkill,
  and calling `communicate()` on a dead process is safe but we avoid it for safety).
- **`_stream_reader` hardening**: Now catches `OSError`/`ValueError`/`BrokenPipeError`
  (pipe breaks when process is killed externally) and safely closes the stream in `finally`.

### Changed Files
- `tools/shell_ops.py` — Added `_communicate_windows()` shared helper; refactored
  `_run_shell`, `_run_tests`, `_verify` to use it; added `CREATE_NO_WINDOW` everywhere;
  hardened `_stream_reader`; fixed timeout handlers
- `tools/lsp.py` — Added `CREATE_NO_WINDOW` to LSP server subprocess
- `tools/mcp_client.py` — Added `CREATE_NO_WINDOW` to MCP server subprocess
- `mini_agent_electron/main.js` — Restart throttle (max 3/30s + backoff); tree-kill on shutdown

## 2026-06-08 — Windows setup.bat hardening
### Fixed
- **Node.js version check**: Now requires Node ≥ 22 (not just any version).
  Electron 42 bundles Node 22 internally; older host Node fails at build time.
- **npm version check**: Now requires npm ≥ 9 (vite 8 needs it).
- **Removed `--silent` from npm commands**: Errors during Electron binary download
  (~100 MB from GitHub) were completely hidden. Output is now visible.
- **Post-install verification**: Checks that `node_modules\electron\dist\electron.exe`
  exists and can run `--version`. Catches broken/corrupted downloads.
- **Broken node_modules cleanup**: Detects when `node_modules\` exists but the
  Electron binary is missing (previous failed install) and removes it.
- **Troubleshooting guidance**: Added ELECTRON_MIRROR, proxy config, npm cache
  clean, and VC++ redistributable hints to the npm install failure path.
- **Build error visibility**: Removed `--silent` from `npm run build`; expanded
  failure message with debug commands and npm cache fix hints.

## 2026-06-03 (evening) — Code Audit: Injection, Import, and Data-Loss Fixes
### Fixed
- **Injection flag lifecycle**: 4 flags reset in `run_agent_turn()` (per user message)
  moved to `bootstrap.init_session()` (per session). One-time injections now
  properly run once per session, not once per message. (llm.py, bootstrap.py)
- **Duplicate failure pattern warning**: removed redundant direct call in
  `run_agent_turn()` phase 3; `_tool_execution_phase()` already handles it. (llm.py)
- **Startup context role mismatch**: session.py used `"system"` role for startup
  context; standardized on `"user"` to match bootstrap.py. (session.py)
- **Data loss in stale tool result compression**: context_inject now saves
  `_original_content` before shrinking tool results; memory_prune restores it
  for accurate content-aware compression. (context_inject.py, memory_prune.py)
### Changed
- **Removed build_startup_context re-export from config.py**. Importers now
  get it directly from prompt.py. (config.py, server.py, tests/test_smoke.py)
- **Eliminated fake tool call hack in _inject_experience_context**. New
  `build_experience_context_from_text()` in failure_learning.py accepts plain
  text with proper keyword extraction and scoring. (context_inject.py,
  tools/failure_learning.py)
- **Updated run_agent_turn docstring**: accurately describes message-count-based
  reminder injection. (llm.py)
### Reason
Code audit of startup/shutdown/prompt/injection architecture found 7 issues:
2 critical (flag lifecycle, duplicate injection), 3 medium (role inconsistency,
compression data loss, import spaghetti), 2 low (misleading docstring, fake
tool call hack). All fixed; 71 tests pass.

## 2026-06-03 (afternoon) — STATE.txt Injection & Population
### Added
- `_inject_state_context()` in context_inject.py — reads STATE.txt once per session
- `_state_txt_injected` flag on AgentContext (tools/__init__.py), reset in llm.py
- 6 tests for STATE.txt injection (test_agent_self_tracking.py, 35 total)
### Changed
- STATE.txt populated with full architecture map (module inventory, decisions, known issues)
- HANDOFF.md updated with session context

## 2026-06-03 (morning) — Agent Self-Tracking System
### Added
- `STATE.txt` — architecture decisions, module map, known issues
- `HANDOFF.md` — session handoff for continuity across restarts
- `CHANGELOG.md` — structured self-modification audit trail
- `test_agent_self_tracking.py` — 29 tests for self-tracking system
### Changed
- `README.md` — added "Agent Self-Modification" section
- `.mini_agent.rules` — added self-review cycle, HANDOFF.md/CHANGELOG.md references
- `context_inject.py` — added `_inject_handoff_context()` for session startup
- `memory.py` — added `write_handoff()` and `read_handoff()` helpers
- `tools/__init__.py` — added `_handoff_injected` flag on AgentContext
- `llm.py` — reset `_handoff_injected` flag per session
- `README.md` — added "Agent Self-Modification" section for human collaborators
- `.mini_agent.rules` — added self-review prompt, HANDOFF.md reference
- `context_inject.py` — inject HANDOFF.md at session startup
- `memory.py` — added `write_handoff()` and `read_handoff()` helpers
### Reason
Research across 16+ self-modifying agent repos (AgentOS, claude-code-thyself, selfmodel, claude-super-evolution) showed consensus: agents need STATE.txt (architecture map), HANDOFF.md (session continuity), and CHANGELOG.md (self-mod audit trail). mini_agent had none.

## 2026-05-24 — Code Audit: Deduplication & Separation of Concerns
### Changed
- `tools/__init__.py` — split ToolResult → `tools/result.py`, error hints → `tools/error_hints.py`
- `config.py` — removed `_start_windows_tunnel()` side effect from `load()`
- `bootstrap.py` — added tunnel call after config load
### Reason
Code audit findings: (1) `tools/__init__.py` was too large at ~1500 lines, (2) config loading had hidden side effects. Moved tunnel to bootstrap where side effects are expected.

## 2026-05-23 — Self-Learning System
### Added
- `failure_learning.py` — FailurePatternStore (SQLite), SelfCritique, MistakeNotebook
- `test_failure_learning.py` — 28 tests
### Reason
Agent was repeating the same mistakes across sessions. Implemented MPR/VIGIL-inspired failure fingerprinting → pattern clustering → fix distillation.

## 2026-05-22 — Edit File Safety
### Changed
- `tools/file_ops.py` — 6 `edit_file` improvements: quote normalization, unicode whitespace, read-before-edit enforcement, indentation preservation, confidence scoring, line-ending normalization
### Reason
`edit_file` was the #1 source of tool failures. Each improvement addresses a specific failure pattern observed in production use.

## 2026-05-20 — SWE-bench Evaluation
### Added
- `eval/swebench_runner.py` — SWE-bench Lite prediction pipeline
- `eval/agent.py` — SWE-bench agent wrapper
- `test_benchmarks.py` — local eval + SWE-bench tests
### Reason
Industry-standard benchmarking for coding agents. Validates tool-use and code-fix capabilities.

## 2026-05-18 — Context Injection Refactor
### Changed
- `context_inject.py` — extracted from `llm.py` (per-turn injection logic)
- `llm.py` — slimmer orchestrator, imports context injection
### Reason
`llm.py` was growing too large. Per-turn context logic (scratchpad, git diff, orchestration, circuit breaker) is a separate concern from turn orchestration.
