#!/usr/bin/env python3
"""
skills.py -- Lazy tool loading via skill gates with SKILL.md disk-based catalog.

Inspired by the Hermes Agent skills architecture:
- Skills are defined as SKILL.md files (YAML frontmatter + markdown body)
- Skills are discovered from disk at startup: workspace skills/ first, then ~/.mini_agent/skills/
- ``use_skill`` activates a skill's tools AND triggers injection of its content into the prompt
- Progressive disclosure: compact catalog shown at session start, full content on activation

Architecture
------------
- ``CORE_TOOLS``: always visible, always loaded
- ``_SKILL_CATALOG``: dict of skill_name -> Skill (populated by _discover_skills)
- ``_active_skills``: set of activated skill names (cleared each session)
- ``get_active_tools()``: returns core tool schemas + schemas from active skills
- ``skill_view(name)``: returns full SKILL.md content (frontmatter + body) for a skill
- ``get_active_skill_content()``: returns concatenated content of all active skills
"""

from __future__ import annotations

import os
import re
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools import ToolResult

# -- Skill dataclass ---------------------------------------------------------


@dataclass
class Skill:
    """A skill defined by a SKILL.md file on disk."""

    name: str
    description: str = ""
    version: str = "1.0"
    author: str = "mini_agent"
    category: str = "software-development"
    tools: list[str] = field(default_factory=list)
    body: str = ""  # Markdown body (everything after frontmatter)
    path: str = ""  # Absolute path to the SKILL.md file

    def to_catalog_entry(self) -> str:
        """Compact catalog entry for the skills_list() catalog."""
        tool_list = ", ".join(self.tools) if self.tools else "none"
        return textwrap.dedent(f"""\
            ### {self.name}
            {self.description}
            **Tools**: {tool_list}
            **Category**: {self.category} | **Version**: {self.version}""")

    def to_full_doc(self) -> str:
        """Full documentation: frontmatter summary + body."""
        tools_str = ", ".join(self.tools) if self.tools else "none"
        header = textwrap.dedent(f"""\
            # Skill: {self.name}
            > {self.description}
            
            **Category**: {self.category} | **Version**: {self.version} | **Author**: {self.author}
            **Tools unlocked**: {tools_str}
            
            ---
            """)
        return header + "\n" + self.body


# -- YAML frontmatter parser (no external deps) ------------------------------


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter from a markdown string.

    Expects content to start with '---' on its own line,
    followed by key: value pairs, then '---' on its own line.
    Returns (frontmatter_dict, body_string).

    Supports simple types: strings, lists (inline [a, b] or block - a\\n - b),
    and booleans (true/false).
    """
    fm: dict = {}
    body = content

    # Match frontmatter block: ---\n...\n---
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return fm, body

    fm_text = fm_match.group(1)
    body = content[fm_match.end():]

    current_key: str | None = None
    current_list: list[str] = []

    def _flush_list() -> None:
        nonlocal current_key, current_list
        if current_key and current_list:
            fm[current_key] = current_list
            current_list = []
            current_key = None

    for line in fm_text.split("\n"):
        # Skip empty lines and comments
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Block list continuation: "  - item"
        if stripped.startswith("- "):
            if current_key:
                current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Inline list: "tools: [a, b, c]"
        inline_match = re.match(r"^(\w[\w-]*)\s*:\s*\[(.*)\]$", stripped)
        if inline_match:
            _flush_list()
            key = inline_match.group(1).lower()
            items = [i.strip().strip('"').strip("'") for i in inline_match.group(2).split(",") if i.strip()]
            fm[key] = items
            continue

        # Key: value pair
        kv_match = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", stripped)
        if kv_match:
            _flush_list()
            key = kv_match.group(1).lower()
            value = kv_match.group(2).strip().strip('"').strip("'")
            # Boolean
            if value.lower() == "true":
                fm[key] = True
            elif value.lower() == "false":
                fm[key] = False
            else:
                fm[key] = value
            current_key = key  # Could be a block list key
        else:
            # Continuation of a simple value (e.g., multi-line description)
            if current_key and current_key in fm and isinstance(fm[current_key], str):
                fm[current_key] += " " + stripped

    _flush_list()
    return fm, body


# -- Skill discovery from disk -----------------------------------------------

#: Global skill catalog, populated by _discover_skills()
_SKILL_CATALOG: dict[str, Skill] = {}
_discovered: bool = False


def _skill_search_paths() -> list[str]:
    """Return paths to search for skill directories, in priority order."""
    paths: list[str] = []

    # 1. Workspace skills/ directory
    # Determine workspace from environment or current directory
    workspace = os.environ.get("MINI_AGENT_WORKSPACE", os.getcwd())
    ws_skills = os.path.join(workspace, "skills")
    if os.path.isdir(ws_skills):
        paths.append(ws_skills)

    # 2. User-level ~/.mini_agent/skills/
    user_skills = os.path.join(os.path.expanduser("~"), ".mini_agent", "skills")
    if os.path.isdir(user_skills):
        paths.append(user_skills)

    return paths


def _discover_skills() -> dict[str, Skill]:
    """Discover all skills from disk.

    Scans skills/ directories in priority order:
    1. Workspace skills/ (project-specific)
    2. ~/.mini_agent/skills/ (user-level)

    A skill directory must contain a SKILL.md file.
    If the same skill name exists in both locations, workspace wins.
    """
    global _SKILL_CATALOG, _discovered

    if _discovered and _SKILL_CATALOG:
        return _SKILL_CATALOG

    catalog: dict[str, Skill] = {}

    for search_path in _skill_search_paths():
        try:
            entries = os.listdir(search_path)
        except OSError:
            continue

        for entry in sorted(entries):
            skill_dir = os.path.join(search_path, entry)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue

            skill = _load_skill(skill_md)
            if skill and skill.name:
                # Workspace skills override user-level skills
                if skill.name not in catalog:
                    catalog[skill.name] = skill

    _SKILL_CATALOG = catalog
    _discovered = True
    return catalog


def _load_skill(path: str) -> Skill | None:
    """Load a single SKILL.md file and return a Skill object.

    Returns None if the file cannot be read or has no name in frontmatter.
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    fm, body = _parse_frontmatter(content)

    name = fm.get("name", "")
    if not name:
        return None

    return Skill(
        name=name,
        description=fm.get("description", ""),
        version=fm.get("version", "1.0"),
        author=fm.get("author", "mini_agent"),
        category=fm.get("category", "software-development"),
        tools=fm.get("tools", []),
        body=body.strip(),
        path=path,
    )


def reload_skills() -> dict[str, Skill]:
    """Force re-discovery of skills from disk (used after file writes)."""
    global _discovered, _SKILL_CATALOG
    _discovered = False
    _SKILL_CATALOG.clear()
    skill_list.cache_clear()
    return _discover_skills()


# -- Backward-compatible SKILLS dict (tool mapping) ---------------------------
# This mirrors the old hardcoded SKILLS dict, but built from discovered skills.
# If no skills are discovered from disk, fall back to the hardcoded defaults.


def _get_skills_tool_map() -> dict[str, list[str]]:
    """Build the tool-name mapping dict from discovered skills.

    Falls back to hardcoded defaults if no SKILL.md files are found.
    """
    catalog = _discover_skills()
    if catalog:
        return {name: skill.tools.copy() for name, skill in catalog.items()}

    # Hardcoded fallback for backward compatibility
    return {
        "git": ["git", "diff", "restore_file"],
        "test": ["run_tests", "verify", "diagnose_failures"],
        "lsp": ["lsp_definition", "lsp_references", "lsp_hover", "lsp_diagnostics"],
        "web": ["fetch_url", "open_url", "browser_navigate", "browser_snapshot",
                "browser_click", "browser_type", "browser_screenshot"],
        "agents": ["spawn_agent", "agent_status", "collect_agent", "collect_any",
                   "agent_message", "agent_read", "agent_extend", "agent_cancel",
                   "agent_handoff", "agent_inbox", "agent_subscribe",
                   "fan_out", "fan_in", "barrier", "pipeline", "scatter_gather",
                   "audit_parallel", "wait_for_agent"],
        "search": ["find_usages", "semantic_search", "recall_turn"],
        "tasks": ["task_status"],
        "image": ["read_image"],
        "desktop": ["desktop_snapshot", "desktop_click", "desktop_type",
                    "desktop_find", "desktop_screenshot", "desktop_apps",
                    "desktop_launch", "desktop_quit", "desktop_focus",
                    "desktop_clipboard", "desktop_windows", "desktop_system_info",
                    "desktop_key", "desktop_open", "desktop_reveal", "desktop_notify"],
        "bootstrap": ["init", "session_stats"],
    }


#: Backward-compatible SKILLS dict: name -> list of tool names.
#: Lazily initialized from disk discovery on first access.
SKILLS: dict[str, list[str]] | None = None


def _get_skills_compat() -> dict[str, list[str]]:
    """Return the SKILLS dict, initializing from disk if needed."""
    global SKILLS
    if SKILLS is None:
        SKILLS = _get_skills_tool_map()
    return SKILLS


# -- Runtime state -----------------------------------------------------------

_active_skills: set[str] = set()
_skill_content_injected: set[str] = set()  # Tracks which skill contents have been injected


# -- Skill management API ----------------------------------------------------


@lru_cache(maxsize=1)
def skill_list() -> str:
    """Return a compact catalog of all available skills.

    Similar to Hermes' bundled skills catalog page.
    Cached; use reload_skills() to invalidate.
    """
    catalog = _discover_skills()
    if not catalog:
        # Fall back to generating from hardcoded map
        tool_map = _get_skills_tool_map()
        lines = ["## Available Skills\n"]
        for name, tools in sorted(tool_map.items()):
            desc = f"Unlocks: {', '.join(tools)}"
            lines.append(f"### {name}")
            lines.append(desc)
            lines.append("")
        return "\n".join(lines)

    lines = ["## Bundled Skills Catalog\n"]
    for name, skill in sorted(catalog.items()):
        lines.append(skill.to_catalog_entry())
        lines.append("")
    return "\n".join(lines)


def skill_view(name: str) -> str | None:
    """Return the full SKILL.md documentation for a skill.

    Returns None if the skill is not found.
    """
    catalog = _discover_skills()
    skill = catalog.get(name.lower())
    if not skill:
        return None
    return skill.to_full_doc()


def activate_skill(name: str) -> tuple[bool, str]:
    """Activate a skill group. Returns (ok, message).

    Note: does NOT strip whitespace or lowercase the name.
    Callers (like _use_skill) handle normalization.
    """
    tool_map = _get_skills_tool_map()

    if name not in tool_map:
        available = ", ".join(sorted(tool_map.keys()))
        return False, f"Unknown skill '{name}'. Available: {available}"
    if name in _active_skills:
        return True, f"Skill '{name}' is already active ({len(tool_map[name])} tools)."
    _active_skills.add(name)
    tool_list = ", ".join(tool_map[name])
    return True, f"Activated '{name}': {tool_list} ({len(tool_map[name])} tools)."


def deactivate_skill(name: str) -> tuple[bool, str]:
    """Deactivate a skill group. Returns (ok, message)."""
    if name not in _active_skills:
        return False, f"Skill '{name}' is not active."
    _active_skills.discard(name)
    _skill_content_injected.discard(name)
    return True, f"Deactivated '{name}'."


def list_skills() -> dict[str, list[str]]:
    """Return all available skills and their tool lists."""
    # Return a copy for backward compat (tests expect is not SKILLS)
    return dict(_get_skills_compat())


def active_skills() -> set[str]:
    """Return currently active skill names."""
    return set(_active_skills)


def reset_skills() -> None:
    """Clear all active skills (called at session start)."""
    _active_skills.clear()
    _skill_content_injected.clear()


def get_active_tool_names() -> list[str]:
    """Return the full list of tool names currently available to the model."""
    tool_map = _get_skills_tool_map()
    names = list(CORE_TOOLS)
    for skill_name in _active_skills:
        names.extend(tool_map.get(skill_name, []))
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
    Slow path: builds a name->schema lookup once per call.
    """
    from tools.schema import TOOLS

    active_names = frozenset(get_active_tool_names())
    return [td for td in TOOLS if td["function"]["name"] in active_names]


def get_active_skill_content() -> str:
    """Return the concatenated SKILL.md content for all active skills.

    This is injected into the conversation context so the agent knows
    HOW to use the tools it has unlocked. Each skill's content is injected
    only once per session (tracked by _skill_content_injected).

    Returns empty string if no new skills need injection.
    """
    catalog = _discover_skills()
    parts: list[str] = []
    for skill_name in sorted(_active_skills):
        if skill_name in _skill_content_injected:
            continue
        skill = catalog.get(skill_name)
        if skill and skill.body:
            _skill_content_injected.add(skill_name)
            # Compact header + body
            tool_list = ", ".join(skill.tools) if skill.tools else "none"
            parts.append(
                f"## Skill: {skill.name}\n"
                f"> {skill.description}\n"
                f"**Tools**: {tool_list}\n\n"
                f"{skill.body}"
            )
    return "\n\n---\n\n".join(parts)


# -- Core tools (always visible) ---------------------------------------------

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
    "memory_core",
    "session_search",
    "use_skill",
    # Extended core -- fundamental for all sessions
    "web_search",
    "todo_write",
    "todo_read",
    "plan",
    "plan_status",
]


# -- use_skill tool schema ---------------------------------------------------

def _build_use_skill_schema() -> dict:
    """Build the use_skill tool schema with dynamic available skills list."""
    tool_map = _get_skills_tool_map()
    available = ", ".join(sorted(tool_map.keys()))
    return {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": (
                "Activate a skill group to gain access to additional tools. "
                "Call this when you need tools not in the core set. "
                "Skills stay active for the rest of the session. "
                "Available skills: "
                + available
                + "."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name to activate (e.g. 'git', 'test', 'web', 'agents', 'search').",
                    }
                },
                "required": ["name"],
            },
        },
    }


USE_SKILL_SCHEMA: dict = _build_use_skill_schema()

SKILL_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skill_list",
        "description": "List all available skills with their descriptions and unlocked tools. "
        "Use this to browse the skill catalog before activating one with use_skill.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

SKILL_VIEW_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": "View the full SKILL.md documentation for a specific skill. "
        "Use this to read detailed usage instructions for a skill without activating it.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to view (e.g. 'git', 'test', 'lsp').",
                }
            },
            "required": ["name"],
        },
    },
}


# -- tool implementations ----------------------------------------------------


def _skill_list(args: dict, _wg: object = None, _rg: object = None) -> "ToolResult":
    """Tool: List all available skills with descriptions and unlocked tools."""
    from tools import ToolResult

    catalog = skill_list()
    if not catalog:
        return ToolResult(success=False, content="No skills available.")
    return ToolResult(success=True, content=catalog)


def _skill_view(args: dict, _wg: object = None, _rg: object = None) -> "ToolResult":
    """Tool: View full SKILL.md documentation for a skill."""
    from tools import ToolResult

    name = args.get("name", "").strip().lower()
    if not name:
        return ToolResult(success=False, content="No skill name provided.")
    doc = skill_view(name)
    if doc is None:
        tool_map = _get_skills_tool_map()
        available = ", ".join(sorted(tool_map.keys()))
        return ToolResult(
            success=False,
            content=f"Unknown skill '{name}'. Available: {available}",
        )
    return ToolResult(success=True, content=doc)


# -- use_skill tool implementation -------------------------------------------


def _use_skill(args: dict, _wg: object = None, _rg: object = None) -> "ToolResult":
    """Activate a skill group, unlocking its tools for the next turn.

    Also returns the skill's full documentation so the agent can
    immediately learn how to use the unlocked tools.
    """
    from tools import ToolResult

    name = args.get("name", "").strip().lower()
    tool_map = _get_skills_tool_map()

    if not name:
        available = ", ".join(sorted(tool_map.keys()))
        return ToolResult(
            success=False,
            content=f"No skill name provided. Available: {available}",
        )

    ok, msg = activate_skill(name)
    if not ok:
        return ToolResult(success=False, content=msg)

    # Get full skill documentation for the agent to read
    skill_doc = skill_view(name)
    total = len(get_active_tool_names())

    result_text = f"{msg}\nTotal tools now available: {total}"
    if skill_doc:
        result_text += f"\n\n--- Skill Documentation ---\n\n{skill_doc}"

    return ToolResult(success=True, content=result_text)
