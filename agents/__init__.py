"""Sub-agent system: runtime registry, sub-agent engine.

Note: run_sub_agent is NOT imported here to avoid a circular import chain:
  api -> core.config -> core.bootstrap -> agents.agent_runtime -> agents.__init__
  -> agents.sub_agent -> api.
Import it directly: ``from agents.sub_agent import run_sub_agent``.
"""

from .agent_runtime import AgentRuntime, SubAgentResult
