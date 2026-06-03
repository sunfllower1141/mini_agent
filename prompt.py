#!/usr/bin/env python3
"""
prompt.py — system prompt for mini_agent.

Kept in its own module so it can evolve independently of the orchestrator
and execution logic.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AgentConfig


def build_system_prompt(config: "AgentConfig") -> str:
    """Build the full system prompt with dynamic context injected.

    Includes a header showing the current workspace location and
    safety flag status, followed by the static behavioural prompt.
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
        "\n"
        "══════════════════════════════════════════════════════════════\n"
        f"  DATE        : {date_str}\n"
        f"  OS          : {os_str}\n"
        f"  SHELL       : {shell}\n"
        f"  UI          : {frontend}\n"
        f"  WORKSPACE   : {workspace}\n"
        f"  SAFETY FLAGS:\n"
        + "\n".join(safety_lines) +
        "\n══════════════════════════════════════════════════════════════"
    )

    # --- Cached prefix: static identity FIRST for DeepSeek prompt caching ---
    # DeepSeek's cache_control is on the first system message.
    # By putting _STATIC_PROMPT at the front, the cache can hit ~2,000
    # tokens of static content that never changes across workspaces.
    # Dynamic header + rules + git status are appended after so they
    # don't invalidate the cached prefix.
    # --- Provider-specific note ---
    provider = getattr(config, "api_provider", None) or "deepseek"
    provider_notes: dict[str, str] = {
        "deepseek": (
            "Note: running on DeepSeek. DeepSeek is prone to tool-call loops in long "
            "contexts — if you call the same tool with the same arguments twice, switch "
            "approaches immediately."
        ),
        "claude": (
            "Note: running on Claude. Claude excels at long-form code generation and "
            "architectural reasoning. Prefer larger, well-structured edits over many small ones."
        ),
        "xai": (
            "Note: running on xAI/Grok. Grok is capable but may need more explicit "
            "step-by-step guidance for complex multi-file refactors."
        ),
        "ollama": (
            "Note: running on local Ollama (qwen3.6). Context window is smaller "
            "(64K tokens). Be extra vigilant about scratchpad use and avoid very long "
            "conversations without pruning."
        ),
    }
    provider_note = provider_notes.get(provider, "")
    if provider_note:
        provider_note = f"\n\n{provider_note}\n"

    prompt = _STATIC_PROMPT + "\n\n" + header + provider_note
    # --- Win 2: Hierarchical .mini_agent.rules ---
    # Walk directory tree upward from workspace, merging all rules files
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
        prompt += "\n\nPROJECT RULES:\n" + "\n\n".join(rules_parts) + "\n"

    # --- Win 1: Git context ---
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
            prompt += "\n\nREPOSITORY STATUS (git):\n" + "\n".join(git_info) + "\n"
    except (OSError, subprocess.SubprocessError):
        # git not installed or repo not initialized — skip status block
        pass

    return prompt


_STATIC_PROMPT = (
    "You are mini_agent, a terminal AI coding assistant powered by an LLM with a "
    "Electron desktop UI.  You operate on a workspace directory using the tools provided by "
    "the runtime.  Key modules:\n"
    "\n"
    "  prompt.py  — this system prompt (edit to change personality/rules)\n"
    "  config.py  — AgentConfig, TOML loading, startup bootstrap\n"
    "  llm.py     — turn orchestration (run_agent_turn), tool piping\n"
    "  api.py     — LLM API calls, message cache\n"
    "  memory.py  — SQLite conversation persistence + pruning\n"
    "  safety.py  — workspace read / write gates\n"
    "  README.md  — architecture decisions and current state (consult before major changes)\n"
    "\n"
    "When asked about yourself or this project, use find_symbol / read_file to consult "
    "the relevant module rather than guessing.\n"
    "\n"
    "TOOLS & SKILLS:\n"
    "- Start with ~11 core tools. Use **use_skill(\"name\")** to unlock more:\n"
    "    agents, git, test, lsp, web, planning, search, image, tasks, bootstrap, desktop\n"
    "- Each skill adds 2-15 tools. The API `tools` parameter only includes\n"
    "  core + activated skill schemas (not all 63 tools).\n"
    "\n"
    "WEB SEARCH (use when needed):\n"
    "Use web_search and fetch_url for documentation, API references, external\n"
    "knowledge, or current information not in your training data. Also useful\n"
    "when you're stuck (3+ consecutive tool failures) or the user asks about\n"
    "something outside the codebase. For local repo tasks, search the codebase\n"
    "first before reaching for the web.\n"
    "\n"
    "Behavior:\n"
    "- Be direct and concise. Prefer normal answers when no tool is needed.\n"
    "\n"
    "Ambiguity handling:\n"
    "- When the user's request is broad/vague, ask ONE clarifying question\n"
    "  before launching into a full investigation. Don't guess.\n"
    "- For codebase reviews: start with README.md, check .mini_agent.rules,\n"
    "  then target the most-changed files from recent git history.\n"
    "- State your scope upfront: 'I'll review X, Y, Z — does that match?'\n"
    "- Don't read every file. Use find_symbol, search_files, and git log\n"
    "  to triage before diving deep.\n"
    "\n"
    "Loop prevention (CRITICAL):\n"
    "- Same tool + same args 2x = STUCK. Switch approach immediately.\n"
    "- Long commands (>10s): background=True. Poll task_status once.\n"
    "- edit_file MUST be preceded by read_file in same batch.\n"
    "- Time-box: 5+ turns without progress → state what you know, propose workaround.\n"
    "- Update write_scratchpad every 3 turns.\n"
    "- Context grows stale: rely on scratchpad and plan, not old tool results.\n"
    "\n"
    "Parallel tool execution: batch ALL independent tool calls in ONE response.\n"
    "\n"
    "Scratchpad & memory:\n"
    "- write_scratchpad: working note across turns. Update after every tool round.\n"
    "- remember: long-term cross-session memory.\n"
    "- Check project_knowledge (injected at startup) before rediscovering.\n"
    "\n"
    "Code changes:\n"
    "- Keep modules small, single-purpose. No circular imports, no global mutable state.\n"
    "- Use named constants, type hints, clear names. Every feature needs a test.\n"
    "- Run relevant tests after each change. Prefer small incremental edits.\n"
    "- Confirm plan with user before starting new features.\n"
    "\n"
    "Task planning: use plan() for multi-step work. plan_status(step=N) after each.\n"
    "\n"
    "Multi-agent: spawn_agent for independent sub-tasks (use_skill('agents')\n"
    "first). Orchestrate, don't duplicate. Patterns: fan_out, fan_in, pipeline,\n"
    "barrier, scatter_gather.\n"
    "\n"
    "Inter-agent: agent_message (broadcast), agent_handoff (typed structured\n"
    "result), agent_inbox (per-agent), agent_subscribe.\n"
    "\n"
    "Code analysis: diff, verify, diagnose_failures, find_usages, restore_file.\n"
    "\n"
    "Session: session_stats, recall_turn. External: read_image.\n"
)

# ---------------------------------------------------------------------------
# Startup context builder (moved from config.py)
# ---------------------------------------------------------------------------

def build_startup_context(
    workspace: str, *, knowledge: list[dict] | None = None,
) -> str:
    """Generate a one-shot system message describing the workspace at startup.

    Saves the agent discovery turns — no need to list_directory / read STATE.txt
    before getting to work.

    If *knowledge* is provided (list of {summary, category, detail} dicts from
    the project_knowledge table), it is appended as a "Project Learnings" section
    so the agent benefits from past session experience.
    """
    import subprocess as _sp
    from config import TREE_TRUNCATION_LINES, GIT_LOG_COUNT, GIT_LOG_TIMEOUT

    parts: list[str] = []
    parts.append("[WORKSPACE CONTEXT — injected once at session start]")

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

    # 2. Recent git log (last 5 commits, if this is a git repo)
    try:
        r = _sp.run(["git", "-C", workspace, "log", "--oneline", f"-{GIT_LOG_COUNT}"],
                    capture_output=True, text=True, timeout=GIT_LOG_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            parts.append("\n## Recent git log\n```\n" + r.stdout.rstrip() + "\n```")
    except (OSError, _sp.TimeoutExpired):
        pass

    # 3. Project knowledge (cross-session learnings, grouped by category)
    if knowledge:
        lines = []
        session_entries = [e for e in knowledge if e.get("category") == "session_summary"]
        other_entries = [e for e in knowledge if e.get("category") != "session_summary"]

        if session_entries:
            summary = session_entries[0].get("summary", "")
            detail = session_entries[0].get("detail", "")
            lines.append("\n## Last Session Summary")
            lines.append(f"{summary}")
            if detail:
                lines.append(f"{detail}")

        if other_entries:
            # Group by category for cleaner presentation
            from collections import defaultdict
            by_cat: dict[str, list[dict]] = defaultdict(list)
            for entry in other_entries:
                cat = entry.get("category", "general")
                by_cat[cat].append(entry)

            CATEGORY_LABELS = {
                "tool_usage": "Tool Usage Patterns",
                "code_pattern": "Code Patterns & Conventions",
                "error_pattern": "Known Error Patterns & Fixes",
                "convention": "Project Conventions",
                "architecture": "Architecture Insights",
                "workaround": "Known Workarounds",
                "dependency": "Dependencies & Setup",
                "error": "Learned Error Patterns",
                "general": "General Learnings",
            }

            lines.append("\n## Project Learnings (from past sessions)")
            for cat, cat_label in CATEGORY_LABELS.items():
                entries = by_cat.get(cat, [])
                if entries:
                    lines.append(f"\n### {cat_label}")
                    for entry in entries[:8]:  # Cap per category
                        s = entry.get("summary", "")
                        d = entry.get("detail", "")
                        hits = entry.get("hits", 0)
                        hit_info = f" [{hits}× used]" if hits > 1 else ""
                        lines.append(f"- {s}{hit_info}" + (f" — {d}" if d else ""))

        parts.append("\n".join(lines))

    return "\n".join(parts) + "\n"
