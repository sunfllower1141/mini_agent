---
name: agents
description: Multi-agent orchestration -- spawn sub-agents, fan-out/fan-in, pipelines, barriers, scatter-gather.
version: "1.0"
author: mini_agent
category: autonomous-ai-agents
tools:
  - spawn_agent
  - agent_status
  - collect_agent
  - collect_any
  - agent_message
  - agent_read
  - agent_extend
  - agent_cancel
  - agent_handoff
  - agent_inbox
  - agent_subscribe
  - fan_out
  - fan_in
  - barrier
  - pipeline
  - scatter_gather
  - audit_parallel
  - wait_for_agent
---

# Agents Skill

Multi-agent orchestration for parallel and pipeline workloads. Use for:

## Lifecycle
- **spawn_agent** -- launch a sub-agent with a task description; returns agent ID
- **agent_status** -- check running/completed/failed status of any agent
- **collect_agent** -- collect results from a specific agent
- **collect_any** -- collect results from the first available completed agent
- **agent_extend** -- grant additional turns to a productive agent
- **agent_cancel** -- cancel a running agent
- **wait_for_agent** -- block until an agent completes

## Communication
- **agent_message** -- broadcast a message to agents
- **agent_read** -- read a message from the message bus
- **agent_handoff** -- pass a typed structured result between agents
- **agent_inbox** -- check an agent's inbox for messages
- **agent_subscribe** -- subscribe an agent to message channels

## Orchestration Patterns
- **fan_out** -- dispatch N parallel tasks, collect all results
- **fan_in** -- dispatch tasks, process as they complete
- **pipeline** -- chain agents sequentially: A → B → C
- **barrier** -- synchronize multiple agents at a checkpoint
- **scatter_gather** -- split work, process in parallel, merge results
- **audit_parallel** -- audit 3 parallel agents for correctness

## Best Practices
- Use `fan_out` for independent tasks (test writing, search, code review)
- Use `pipeline` when output of A feeds into B
- Limit sub-agent turns (5-30) based on task complexity
- Collect results promptly to avoid zombie agents
- Use `agent_handoff` for typed results between pipeline stages
