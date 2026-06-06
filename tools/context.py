#!/usr/bin/env python3
"""
context.py — agent context and thread-safe context-variable proxy.

Extracted from tools/__init__.py to keep the dispatch module focused.
"""

from __future__ import annotations

import contextvars


# Context keys used across tools and llm
CTX_SCRATCHPAD_PATH = "scratchpad_path"
CTX_SCRATCHPAD_UPDATED = "_scratchpad_updated"
CTX_TURN_HISTORY = "_turn_history"  # dict[int, str] — turn number → summary
CTX_PLAN_STEPS = "_plan_steps"      # list[str] — from plan tool
CTX_PLAN_DONE = "_plan_done"        # set[int] — completed step indices
CTX_PLAN_LAST_ADVANCED = "_plan_last_advanced_turn"  # int — turn when last step advanced


class AgentContext:
    """Mutable context shared across tools and the agent loop.

    Initialized once at startup via ``set_context()``, then read/written
    by tools and ``llm.py`` through the ``_TOOL_CONTEXT`` proxy.

    Attributes (all optional, defaulting to None or empty):
        scratchpad_path       SQLite DB path for scratchpad persistence
        exa_api_key           API key for Exa web search
        workspace             Workspace root directory
        _scratchpad_updated   Flag: scratchpad was updated this turn
        _turn_history         dict[int, str] — turn number → summary
        _plan_steps           list[str] — declared plan steps
        _plan_done            set[int] — completed step indices
        _plan_last_advanced_turn  int — turn number when a step was last completed
    """

    def __init__(self):
        self.scratchpad_path: str | None = None
        self.exa_api_key: str | None = None
        self.openai_api_key: str | None = None
        self.workspace: str | None = None
        self._scratchpad_updated: bool = False
        self._turn_history: dict[int, str] = {}
        self._plan_steps: list[str] = []
        self._plan_done: set[int] = set()
        self._plan_last_advanced_turn: int = 0
        self._memory_store = None  # MemoryStore instance (set by init_session)
        self._failure_pattern_store = None  # FailurePatternStore (set by init_session)
        self._self_critique = None  # SelfCritique instance (set by init_session)
        self._subagent_callback: callable | None = None  # (event_type, data) for Electron sub-agent events
        self._scratchpad_injected: bool = False  # one-time scratchpad context injected this session
        self._git_diff_injected: bool = False    # one-time git diff context injected this session
        self._handoff_injected: bool = False     # one-time handoff context injected this session
        self._state_txt_injected: bool = False   # one-time STATE.txt context injected this session
        self._session_start_head: str | None = None  # git HEAD hash at session start (for auto-handoff)
        self._consecutive_read_only_turns: int = 0  # turns of pure reads (reset on write/shell)


_TOOL_CONTEXT_VAR: contextvars.ContextVar[AgentContext] = contextvars.ContextVar(
    "tool_context", default=AgentContext()
)


class _ContextProxy:
    """Proxy that transparently delegates attribute access to the current
    ``AgentContext`` inside a ``ContextVar``.  Each thread / async task
    gets its own copy, so concurrent tool execution (background shells,
    sub-agents, etc.) cannot cross-contaminate context state."""

    __slots__ = ("_cv",)

    def __init__(self, cv: contextvars.ContextVar):
        super().__setattr__("_cv", cv)

    def __getattr__(self, name: str):
        return getattr(self._cv.get(), name)

    def __setattr__(self, name: str, value):
        if name == "_cv":
            super().__setattr__(name, value)
        else:
            setattr(self._cv.get(), name, value)

    def __delattr__(self, name: str):
        delattr(self._cv.get(), name)

    @property
    def __dict__(self):
        return self._cv.get().__dict__

    def get(self) -> AgentContext:
        """Explicit accessor for the raw ``AgentContext`` (rarely needed)."""
        return self._cv.get()


_TOOL_CONTEXT = _ContextProxy(_TOOL_CONTEXT_VAR)


# P1.4: Dispatch mapping for set_context — replaces if/elif chain
_CTX_DISPATCH = {
    "scratchpad_path": lambda ctx, v: setattr(ctx, "scratchpad_path", v),
    "exa_api_key": lambda ctx, v: setattr(ctx, "exa_api_key", v),
    "openai_api_key": lambda ctx, v: setattr(ctx, "openai_api_key", v),
    "workspace": lambda ctx, v: setattr(ctx, "workspace", v),
}


def set_context(**kwargs) -> None:
    """Set module-level context accessible to tool implementations."""
    ctx = _TOOL_CONTEXT
    for key, value in kwargs.items():
        handler = _CTX_DISPATCH.get(key)
        if handler is not None:
            handler(ctx, value)
        else:
            setattr(ctx, key, value)
