---
name: bootstrap
description: Session management -- initialize workspace, view session statistics.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - init
  - session_stats
---

# Bootstrap Skill

Session initialization and diagnostics. Use for:

- **init** -- initialize a new mini_agent workspace with config, rules, and tracking files
- **session_stats** -- view current session statistics: turn count, token usage, tool calls

## When to Use
- User asks to set up mini_agent in a new project: use `init`
- User wants to check session progress or resource usage: use `session_stats`

## Best Practices
- `init` creates `.mini_agent.toml`, `.mini_agent.rules`, `STATE.txt`, `CHANGELOG.md`, `HANDOFF.md`, `TASKS.md`
- `init` is idempotent -- existing files are not overwritten
- `session_stats` is read-only and free to call any time
