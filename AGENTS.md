# Agent Rules

## Git Operations
- The `git` and `diff` tools have been **removed**. All git commands go through `run_shell`:
  - `git status --short`
  - `git diff`
  - `git add -A`
  - `git commit -m "..."`
  - `git push origin branch:branch`
  - `git log --oneline`
