---
name: git
description: Git version control -- status, diff, log, add, commit, push. All commands run with safe timeouts (no hangs on Windows).
version: "1.0"
author: mini_agent
category: software-development
tools:
  - git_status
  - git_diff
  - git_log
  - git_add
  - git_commit
---

# Git Skill

Safe git operations that never hang on Windows. Use instead of `run_shell` for git.

- **git_status** -- show working tree status (`git status --short`)
- **git_diff** -- show changes (`git diff` or `git diff --staged`)
- **git_log** -- show commit history (`git log --oneline`)
- **git_add** -- stage files (`git add <paths>`)
- **git_commit** -- commit staged changes (`git commit -m "<message>"`)

## When to Use
- Before making changes: `git_status` to see what's dirty
- After changes: `git_diff` to review, `git_add` + `git_commit` to save
- Session start: `git_log` to see recent commits
- Push is done via `run_shell(command='git push', ...)` with force protection

## Windows Safety
All commands use subprocess.run() with capture_output, text=True, and a 15-second
timeout. No interactive prompts. On timeout or failure, the error is returned as
content rather than hanging the agent.
