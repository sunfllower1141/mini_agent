# Changelog

Self-modification audit trail — what the agent changed and why.

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
