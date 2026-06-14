---
name: git
description: Git version control operations -- status, log, diff, blame, restore, and commit.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - git
  - diff
  - restore_file
---

# Git Skill

Full git version control integration. Use for:

- **git status** -- check current branch, staged/unstaged changes, untracked files
- **diff** -- show detailed diffs between commits, branches, or working tree
- **git log** -- browse commit history with formatting options
- **git blame** -- trace line-level authorship and commit timing
- **restore_file** -- recover a file from any git revision (HEAD, commit hash, branch)
- **git add** / **git commit** -- stage and commit changes
- **git branch** / **git checkout** -- branch management

## Best Practices
- Always run `git status --short` before staging to verify what will be committed
- Use `diff` to review changes before committing
- Write descriptive commit messages: `type: brief description`
- Use `restore_file` to recover from bad edits rather than re-writing

## Gitignore Awareness
The agent respects `.gitignore` rules when searching and listing files.
Files matching gitignore patterns are excluded from workspace visibility.
