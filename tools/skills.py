#!/usr/bin/env python3
"""
skills.py — Lazy tool loading via skill gates.

Instead of loading all 55 tools into the prompt every turn, the model starts
with a core set (~10 tools) and calls ``use_skill("name")`` to unlock more.

This reduces prompt bloat by ~6x for simple tasks and keeps local models
(like qwen) focused on the tools they actually need.

Architecture
------------
- ``CORE_TOOLS``: always visible, always loaded
- ``SKILLS``: dict of skill_name → list of tool names
- ``_active_skills``: set of activated skill names (cleared each session)
- ``get_active_tools()``: returns core tool schemas + schemas from active skills
- ``activate_skill(name)``: called by the use_skill tool implementation
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ── Skill definitions ───────────────────────────────────────────────────────

CORE_TOOLS: list[str] = [
    "read_file",
    "write_file",
    "edit_file",
    "run_shell",
    "search_files",
    "list_directory",
    "file_info",
    "find_symbol",
    "write_scratchpad",
    "remember",
    "use_skill",
]

SKILLS: dict[str, list[str]] = {
    "git": [
        "git",
        "diff",
        "restore_file",
    ],
    "test": [
        "run_tests",
        "verify",
        "diagnose_failures",
    ],
    "lsp": [
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
        "lsp_diagnostics",
    ],
    "web": [
        "web_search",
        "fetch_url",
        "open_url",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    ],
    "agents": [
        "spawn_agent",
        "agent_status",
        "collect_agent",
        "collect_any",
        "agent_message",
        "agent_read",
        "agent_extend",
        "agent_cancel",
        "agent_handoff",
        "agent_inbox",
        "agent_subscribe",
        "fan_out",
        "fan_in",
        "barrier",
        "pipeline",
        "scatter_gather",
        "wait_for_agent",
    ],
    "planning": [
        "todo_write",
        "todo_read",
        "plan",
        "plan_status",
    ],
    "search": [
        "find_usages",
        "semantic_search",
        "recall_turn",
    ],
    "tasks": [
        "task_status",
    ],
    "image": [
        "read_image",
    ],
    "bootstrap": [
        "init",
        "session_stats",
    ],
}

# ── Runtime state ───────────────────────────────────────────────────────────

_active_skills: set[str] = set()


def activate_skill(name: str) -> tuple[bool, str]:
    """Activate a skill group. Returns (ok, message)."""
    if name not in SKILLS:
        available = ", ".join(sorted(SKILLS.keys()))
        return False, f"Unknown skill '{name}'. Available: {available}"
    if name in _active_skills:
        return True, f"Skill '{name}' is already active ({len(SKILLS[name])} tools)."
    _active_skills.add(name)
    tool_list = ", ".join(SKILLS[name])
    return True, f"Activated '{name}': {tool_list} ({len(SKILLS[name])} tools)."


def deactivate_skill(name: str) -> tuple[bool, str]:
    """Deactivate a skill group. Returns (ok, message)."""
    if name not in _active_skills:
        return False, f"Skill '{name}' is not active."
    _active_skills.discard(name)
    return True, f"Deactivated '{name}'."


def list_skills() -> dict[str, list[str]]:
    """Return all available skills and their tool lists."""
    return dict(SKILLS)


def active_skills() -> set[str]:
    """Return currently active skill names."""
    return set(_active_skills)


def reset_skills() -> None:
    """Clear all active skills (called at session start)."""
    _active_skills.clear()


def get_active_tool_names() -> list[str]:
    """Return the full list of tool names currently available to the model."""
    names = list(CORE_TOOLS)
    for skill_name in _active_skills:
        names.extend(SKILLS[skill_name])
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


def get_active_tools() -> list[dict]:
    """Return the list of tool schemas currently available to the model.

    Filters the global TOOLS list to only include core + active skill tools.
    Slow path: builds a name→schema lookup once per call.
    """
    from tools.schema import TOOLS

    active_names = frozenset(get_active_tool_names())
    return [td for td in TOOLS if td["function"]["name"] in active_names]


# ── use_skill tool schema ───────────────────────────────────────────────────

USE_SKILL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": (
            "Activate a skill group to gain access to additional tools. "
            "Call this when you need tools not in the core set. "
            "Skills stay active for the rest of the session. "
            "Available skills: "
            + ", ".join(sorted(SKILLS.keys()))
            + "."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to activate (e.g. 'git', 'test', 'web', 'agents', 'planning').",
                }
            },
            "required": ["name"],
        },
    },
}


# ── use_skill tool implementation ───────────────────────────────────────────

def _use_skill(args: dict, _wg: object = None, _rg: object = None) -> "ToolResult":
    """Activate a skill group, unlocking its tools for the next turn."""
    from tools import ToolResult

    name = args.get("name", "").strip().lower()
    if not name:
        available = ", ".join(sorted(SKILLS.keys()))
        return ToolResult(
            success=False,
            content=f"No skill name provided. Available: {available}",
        )

    ok, msg = activate_skill(name)
    if not ok:
        return ToolResult(success=False, content=msg)

    # Count now-available tools
    total = len(get_active_tool_names())
    return ToolResult(
        success=True,
        content=f"{msg}\nTotal tools now available: {total}",
    )
