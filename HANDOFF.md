# Session Handoff
# Auto-generated at session end. Read at next session start for continuity.

## Last Session: 2026-06-22 15:51 UTC

### What I Changed
### Commits
```
b0db9fb fix: git tools now work correctly on Windows
```
```
AGENTS.md                             | 15 ++++++++-------
 HANDOFF.md                            |  6 ++++--
 core/prompt.py                        |  8 +++++++-
 mini_agent_electron/backend/server.py | 10 ++++++++--
 tools/git_ops.py                      | 22 ++++++++++++++++++----
 tools/schema.py                       |  2 +-
 tools/shell_ops.py                    |  5 +++++
 7 files changed, 51 insertions(+), 17 deletions(-)
```

### What's Pending
(none recorded)

### Recent Conversation
- use git tool to push to main
- call git directly to push to main
- call git directly to push to main
- change the commit message to fix git tools to work on Windows

### Plan Progress
Plan (3/5 complete):
  [V] 1. Tune cache constants (threshold, max entries, TTL, adaptive params) for higher hit rate
  [V] 2. Add query normalization to increase both exact and semantic matches
  [V] 3. Add query key extraction to cache at a better granularity than raw last-user-message
  [o] 4. Run existing tests to verify no regressions
  [o] 5. Add a simple unit test for the query normalization

### Modified Files
- AGENTS.md
- HANDOFF.md
- core/prompt.py
- mini_agent_electron/backend/server.py
- tools/git_ops.py
- tools/schema.py
- tools/shell_ops.py
