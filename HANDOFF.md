# Session Handoff
# Auto-generated at session end. Read at next session start for continuity.

## Last Session: 2026-06-21 21:26 UTC

### What I Changed
- Fixed token display: was showing gross tokens (including cache hits), now shows billable (cache-miss + output). For a "push to main" with 85% cache rate, display drops from ~135k to ~15-30k.
- Added `billable_tokens` property to both `TurnCost` and `SessionCost` in `core/cost_tracking.py`
- Updated `server.py` to send billable token counts via `send_status` and `turn_complete`
- Updated `App.jsx` token counter to show per-turn billable tokens with clarifying tooltip; cost tooltips now include token counts on hover
- Also in this commit: COMPACTION_MAX_TOKENS constant, fix _compact_if_needed HARD_LIMIT, removed unused cost_control code

### Commits
```
445f347 fix: use billable tokens (cache-miss + output) instead of gross for token display
59b27e0 chore: remove broken drag-and-drop file handler
```

### What's Pending
(none)

### Plan Progress
Plan: all complete. Token count fix deployed.

### Modified Files
- core/cost_tracking.py (added billable_tokens)
- core/constants.py (COMPACTION_MAX_TOKENS)
- core/context_inject.py
- mini_agent_electron/backend/server.py
- mini_agent_electron/renderer/src/App.jsx
- CHANGELOG.md
- STATE.txt
- HANDOFF.md
