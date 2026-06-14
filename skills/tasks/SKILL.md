---
name: tasks
description: Background task management -- check status of long-running shell commands.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - task_status
---

# Tasks Skill

Monitor long-running background tasks. Use for:

- **task_status** -- check the status of a background shell command; returns running/completed/timeout with stdout/stderr

## Background Task Workflow
1. Start a long command with `background=True` (e.g., `run_shell("npm install", background=True)`)
2. Poll with `task_status` periodically
3. Read final output when complete

## Best Practices
- Use `background=True` for commands expected to take >10 seconds
- Poll `task_status` once per turn, not more frequently
- Commands have a maximum timeout of 300 seconds
