"""Core orchestration: agent loop, bootstrap, config, prompt, safety, context injection.

This __init__.py is intentionally minimal to avoid circular imports.
Import specific submodules directly (e.g. ``from core.config import AgentConfig``).
"""

# Re-export nothing at module level — import submodules directly.
# The following are lazy-loaded convenience accessors for external code.
# Internal code should import submodules directly.

def __getattr__(name):
    """Lazy import to avoid circular dependencies at package-init time."""
    if name == "AgentConfig":
        from core.config import AgentConfig
        return AgentConfig
    if name == "init_session":
        from core.bootstrap import init_session
        return init_session
    if name == "run_agent_turn":
        from core.llm import run_agent_turn
        return run_agent_turn
    if name == "build_system_prompt":
        from core.prompt import build_system_prompt
        return build_system_prompt
    if name == "build_startup_context":
        from core.prompt import build_startup_context
        return build_startup_context
    if name == "ReadSafetyGate":
        from core.safety import ReadSafetyGate
        return ReadSafetyGate
    if name == "WriteSafetyGate":
        from core.safety import WriteSafetyGate
        return WriteSafetyGate
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
