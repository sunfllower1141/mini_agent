#!/usr/bin/env python3
"""
prompt.py -- system prompt for mini_agent.

Kept in its own module so it can evolve independently of the orchestrator
and execution logic.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import AgentConfig


def build_system_prompt(config: "AgentConfig") -> str:
    """Build the immutable system prompt.

    Returns ONLY the static behavioural prompt + provider note -- no dynamic
    content (date, OS, workspace, rules, git).  This makes the system message
    unchanged across sessions, allowing DeepSeek's prefix-based disk cache to
    achieve cross-session cache hits on the ~2,000 token static prefix.

    Dynamic session metadata is now in ``build_session_header()`` (injected as
    a user message, after the immutable system prefix).
    """
    provider = getattr(config, "api_provider", None) or "deepseek"
    provider_notes: dict[str, str] = {
        "deepseek": (
            "\n\nNote: running on DeepSeek. DeepSeek is prone to tool-call loops in long "
            "contexts -- if you call the same tool with the same arguments twice, switch "
            "approaches immediately."
        ),
        "claude": (
            "\n\nNote: running on Claude. Claude excels at long-form code generation and "
            "architectural reasoning. Prefer larger, well-structured edits over many small ones."
        ),
        "xai": (
            "\n\nNote: running on xAI/Grok. Grok is capable but may need more explicit "
            "step-by-step guidance for complex multi-file refactors."
        ),
        "ollama": (
            "\n\nNote: running on local Ollama (qwen3.6). Context window is smaller "
            "(64K tokens). Be extra vigilant about scratchpad use and avoid very long "
            "conversations without pruning."
        ),
    }
    provider_note = provider_notes.get(provider, "")
    return _STATIC_PROMPT + provider_note


def build_session_header(config: "AgentConfig") -> str:
    """Build dynamic session metadata as a user message.

    Includes the session date, OS, shell, workspace path, safety flags,
    hierarchical .mini_agent.rules, and current git status.  All of this
    is dynamic (changes per session/workspace) so it's injected as a
    user message AFTER the immutable system prompt prefix.

    Keeping it out of the system message lets DeepSeek's prefix-based
    disk cache reuse the ~2,000 token static prompt across sessions.
    """
    workspace = os.path.abspath(config.workspace) if config.workspace else os.getcwd()

    # --- Environment metadata ---
    import datetime
    import platform as _platform

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    os_str = f"{_platform.system()} {_platform.release()} ({_platform.platform(aliased=True, terse=True)})"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown"))
    frontend = getattr(config, "frontend", "terminal") or "terminal"

    # --- Safety flags summary ---
    safety_lines: list[str] = []
    if config.unrestricted:
        safety_lines.append("  unrestricted = True  (NO workspace boundary checks)")
    else:
        safety_lines.append("  unrestricted = False (reads/writes restricted to workspace)")
    safety_lines.append(f"  allow_overwrites = {config.allow_overwrites}")
    safety_lines.append(f"  approve_write_ops = {config.approve_write_ops}")

    header = (
        "[SESSION METADATA -- injected once at session start]\n"
        "\n"
        "==========================================================\n"
        f"  DATE        : {date_str}\n"
        f"  OS          : {os_str}\n"
        f"  SHELL       : {shell}\n"
        f"  UI          : {frontend}\n"
        f"  WORKSPACE   : {workspace}\n"
        f"  SAFETY FLAGS:\n"
        + "\n".join(safety_lines) +
        "\n=========================================================="
    )

    parts = [header]

    # --- Hierarchical .mini_agent.rules ---
    rules_parts: list[str] = []
    search_dir = os.path.abspath(config.workspace)
    seen: set[str] = set()
    while True:
        rules_path = os.path.join(search_dir, ".mini_agent.rules")
        if os.path.isfile(rules_path) and rules_path not in seen:
            seen.add(rules_path)
            try:
                with open(rules_path, encoding="utf-8", errors="replace") as f:
                    rules = f.read().strip()
            except OSError:
                rules = ""
            if rules:
                rules_parts.append(f"# From: {rules_path}\n{rules}")
        parent = os.path.dirname(search_dir)
        if parent == search_dir:
            break
        search_dir = parent
    if rules_parts:
        parts.append("PROJECT RULES:\n" + "\n\n".join(rules_parts))

    # --- Git context ---
    try:
        import subprocess
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=config.workspace,
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        if branch:
            git_info = [f"Current branch: {branch}"]
            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=config.workspace,
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            if status:
                changed = status.split("\n")[:15]
                git_info.append("\n".join(changed))
                if len(status.split("\n")) > 15:
                    git_info.append(f"... and {len(status.split(chr(10))) - 15} more files")
            else:
                git_info.append("(working tree clean)")
            parts.append("REPOSITORY STATUS (git):\n" + "\n".join(git_info))
    except (OSError, subprocess.SubprocessError):
        pass

    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Core memory snapshot builder (Hermes-style frozen-at-load pattern)
# ---------------------------------------------------------------------------

def build_memory_snapshot(core_memory_content: str) -> str:
    """Build the frozen core memory snapshot injected at session start.

    This is a single bounded text blob (default 2,500 chars) that serves as
    the agent's persistent memory -- durable facts, preferences, conventions,
    environment notes, and learned corrections.

    The snapshot is frozen for the entire session (never changes mid-session)
    to preserve LLM prefix cache.  Writes hit disk immediately but appear in
    the next session.  The agent uses the ``memory_core`` tool to add, replace,
    or remove entries.

    Returns an empty string if there's no core memory content.
    """
    content = core_memory_content.strip()
    if not content:
        return ""

    return (
        "[CORE MEMORY -- frozen snapshot loaded at session start]\n"
        "The following is your persistent memory. It survives across sessions.\n"
        "It does NOT change during this session. Use the memory_core tool to\n"
        "add, replace, or remove entries (changes appear next session).\n"
        "\n"
        + content
        + "\n"
        "\n[END CORE MEMORY]\n"
    )


# Public immutable system prompt -- the ONLY content in the system message.
# Because it never changes, DeepSeek's prefix-based disk cache can reuse the
# KV-cache across every session (hours to days).  All dynamic content (date,
# OS, workspace, rules, git status) is injected as user messages instead.
STATIC_PROMPT = _STATIC_PROMPT = (
    "You are mini_agent, a coding agent harness. You help users by reading files, "
    "executing commands, editing code, and writing new files.\n"
    "\n"
    "Available tools include: read_file, write_file, edit_file, run_shell, "
    "search_files, find_symbol, list_directory, file_info, web_search, "
    "write_scratchpad, plan, plan_status, remember, memory_core, use_skill.\n"
    "\n"
    "In addition to the tools above, you may have access to other tools "
    "depending on skills activated via use_skill().\n"
    "\n"
    "Guidelines:\n"
    "- Be concise in your responses\n"
    "- Show file paths clearly when working with files\n"
    "- Use read_file with offset/limit for large files\n"
    "- Batch independent tool calls in a single response\n"
    "- Read before edit; verify after change\n"
    "- Prefer pi-style oldText/newText edits (no anchors needed)\n"
    "- Same tool + same args 2x = stuck; switch approach\n"
    "\n"
    "Project documentation (read only when the user asks about mini_agent itself):\n"
    "- STATE.txt — architecture map\n"
    "- CHANGELOG.md — audit trail\n"
    "- AGENTS.md — behavioral rules\n"
    "- README.md — architecture decisions\n"
    "- HANDOFF.md — previous session context (read at session start)\n"
    "- TASKS.md — task-to-file index\n"
)

# ---------------------------------------------------------------------------
# Startup context builder (moved from config.py)
# ---------------------------------------------------------------------------

def build_startup_context(
    workspace: str, *, knowledge: list[dict] | None = None,
) -> str:
    """Generate a one-shot system message describing the workspace at startup.

    Saves the agent discovery turns -- no need to list_directory / read STATE.txt
    before getting to work.

    If *knowledge* is provided (list of {summary, category, detail} dicts from
    the project_knowledge table), it is appended as a "Project Learnings" section
    so the agent benefits from past session experience.
    """
    import subprocess as _sp
    from core.config import TREE_TRUNCATION_LINES, GIT_LOG_COUNT, GIT_LOG_TIMEOUT

    parts: list[str] = []
    parts.append("[WORKSPACE CONTEXT -- injected once at session start]")

    # 1. File tree (skip hidden dirs, __pycache__, .git, venv, node_modules)
    SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}
    tree_lines: list[str] = []
    try:
        walk = list(os.walk(workspace))
    except OSError:
        walk = []
    for dirpath, dirnames, filenames in walk:
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith("."))
        depth = dirpath[len(workspace):].count(os.sep)
        indent = "  " * depth
        label = os.path.basename(dirpath) or workspace.rstrip(os.sep).rsplit(os.sep, 1)[-1]
        tree_lines.append(f"{indent}[d] {label}/")
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            tree_lines.append(f"{indent}  [f] {fname}")
        if len(tree_lines) > TREE_TRUNCATION_LINES:
            tree_lines.append(f"{indent}  ... (truncated)")
            break
    parts.append("```\n" + "\n".join(tree_lines) + "\n```")

    # 2. Codebase structure map (symbol-level: classes, functions, imports)
    #    Generated via AST/regex -- compact ~2K token map so the agent
    #    knows what lives where without exploratory tool calls.
    from core.codebase_map import build_codebase_map
    codebase_map = build_codebase_map(workspace)
    if codebase_map:
        parts.append("\n" + codebase_map)

    # 3. Recent git log (last 5 commits, if this is a git repo)
    try:
        r = _sp.run(["git", "-C", workspace, "log", "--oneline", f"-{GIT_LOG_COUNT}"],
                    capture_output=True, text=True, timeout=GIT_LOG_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            parts.append("\n## Recent git log\n```\n" + r.stdout.rstrip() + "\n```")
    except (OSError, _sp.TimeoutExpired):
        pass

    # 4. Project knowledge (cross-session learnings, grouped by category)
    if knowledge:
        lines = []
        session_entries = [e for e in knowledge if e.get("category") == "session_summary"]
        other_entries = [e for e in knowledge if e.get("category") != "session_summary"]
        if session_entries:
            lines.append("## Past Session Summaries")
            for e in session_entries[:3]:
                lines.append(f"- {e.get('summary', '')[:200]}")
        if other_entries:
            lines.append("## Project Knowledge")
            for e in other_entries[:10]:
                cat = e.get("category", "general")
                lines.append(f"- [{cat}] {e.get('summary', '')[:200]}")
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"
