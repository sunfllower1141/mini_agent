# Agent Rules

## Git Operations
- **NEVER use the `git` tool** — it freezes/hangs.
- **ALWAYS use `run_shell`** for all git operations:
  - `git status --short`
  - `git diff`
  - `git add -A`
  - `git commit -m "..."`
  - `git push origin branch:branch`
  - `git log --oneline`
