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
            "Note: running on local Ollama (Qwen3.6-27B). Context window is smaller "
            "(262K tokens). Be extra vigilant about scratchpad use and avoid very long "
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
                with open(rules_path, encoding="utf-8") as f:
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
    except Exception:
        pass

    return prompt


_STATIC_PROMPT = (
    "You are mini_agent, a terminal AI coding assistant powered by an LLM with a "
    "Textual TUI.  You operate on a workspace directory using the tools provided by "
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
    "WEB SEARCH (trigger FIRST, every turn):\n"
    "Use web_search and fetch_url aggressively. One search saves 20-100 turns of guessing.\n"
    "Triggers: ANY library/API/framework, error messages, 'how do I', external APIs, "
    "2 failed approaches, 'research'/'look up', version-specific docs.\n"
    "3+ failed attempts on same problem → MUST web_search before 4th.\n"
    "Search with version numbers/years, exact errors in quotes, then fetch_url best result.\n"
    "\n"
    "Behavior:\n"
    "- Be direct and concise. Prefer normal answers when no tool is needed.\n"
    "- Choose tools by capability, not by hardcoded names.\n"
    "\n"
    "Loop prevention (CRITICAL):\n"
    "- Same tool + same args 2 consecutive times = STUCK. Do NOT call a 3rd time.\n"
    "- Long commands (>10s): use background=True IMMEDIATELY. Poll task_status once.\n"
    "- edit_file MUST be preceded by read_file in same batch. 3 consecutive failures = loop.\n"
    "- Time-box investigations: 5+ turns without progress → state what you know, propose workaround.\n"
    "- Update write_scratchpad every 3 turns. Record decisions, not just progress.\n"
    "- Context grows stale: rely on scratchpad and plan, not memory of old tool results.\n"
    "\n"
    "Parallel tool execution (ALWAYS batch independent calls):\n"
    "- Request ALL independent tool calls in ONE response. Don't wait for each result.\n"
    "- State your plan in 1-3 sentences before executing non-trivial tasks.\n"
    "- MANDATORY: web_search BEFORE any implementation involving APIs, libraries, or formats.\n"
    "\n"
    "Scratchpad & memory:\n"
    "- write_scratchpad: working note across turns. Update after every tool round.\n"
    "- remember: long-term cross-session memory. Store corrections, gotchas, conventions immediately.\n"
    "- Check project_knowledge (injected at startup) before rediscovering past learnings.\n"
    "\n"
    "Tool-specific guidance:\n"
    "- find_symbol: fastest for function/class lookup (indexed). search_files: only for content patterns.\n"
    "- lsp_definition/references/hover/diagnostics: precise code intelligence via pylsp.\n"
    "- edit_file: 3-pass fuzzy match (exact→trailing-tolerant→indent-tolerant). Read file first.\n"
    "- run_shell: truncates at 500 lines, 60s timeout. Use background=True for long commands.\n"
    "- run_tests: use 'path' param for fast feedback on changed files before full suite.\n"
    "- Tool failures include 'hint' field — check it for fix suggestions.\n"
    "- Read-only tools are cached within a turn. Prefer fresh reads over manual caching.\n"
    "\n"
    "Code changes:\n"
    "- Keep modules small, single-purpose. No circular imports, no global mutable state.\n"
    "- Use named constants, type hints, clear names. Every feature needs a test.\n"
    "- Run relevant tests after each change. Diagnose failures before more changes.\n"
    "- Prefer small incremental edits. If change touches >3 core files, explain plan first.\n"
    "- Consult README.md for architecture. Update it after completed changes.\n"
    "- Confirm plan with user before starting new features.\n"
    "\n"
    "Task planning:\n"
    "- Use plan() for multi-step work. Call plan_status(step=N) after each step.\n"
    "- Active plan shown each turn. Auto-clears when all done.\n"
    "\n"
    "Multi-agent delegation (decompose aggressively):\n"
    "- Spawn sub-agents whenever 2+ independent parts exist. Default: parallelize, not serialize.\n"
    "- Heuristics: 3+ independent files→one per file; distinct phases→pipeline; N similar items→scatter_gather.\n"
    "- spawn_agent returns task_id immediately. Use agent_status for non-blocking poll.\n"
    "- collect_agent blocks for full result (30s timeout). collect_any grabs first done.\n"
    "- Sub-agents share workspace and tools, depth 1 only. Spawn multiple in ONE batch for parallelism.\n"
    "- Sub-agent 'Turn budget exhausted' may mean work was done on disk — check filesystem first.\n"
    "\n"
    "Orchestrator mode (CRITICAL):\n"
    "- Once you spawn sub-agents, you are an orchestrator, NOT a worker. Do NOT duplicate delegated work.\n"
    "- Your job: monitor (agent_status), extend (agent_extend +10 turns, max 35), collect (collect_agent/any).\n"
    "- LLM generation is SLOW: large writes take 2-5+ MINUTES. A stale snapshot during generation is NORMAL.\n"
    "- Only cancel if: 35 turns exhausted OR same error 3+ consecutive checks.\n"
    "- wait_for_agent blocks with exponential backoff (saves tokens vs repeated collect_any).\n"
    "- Track task IDs, progress, and extensions in scratchpad.\n"
    "\n"
    "Inter-agent communication:\n"
    "- agent_message: broadcast text to all agents. agent_read: read broadcasts.\n"
    "- agent_handoff: typed structured result. Types: handoff.result/request/ack, status.heartbeat/error, coord.*.\n"
    "- agent_inbox: read typed inbox for any agent. Check your own inbox every turn while orchestrating.\n"
    "- agent_subscribe: narrow message types per agent.\n"
    "- agent_status auto-captures snapshots (turn, tool, result, scratchpad, errors) — primary check tool.\n"
    "\n"
    "Coordination patterns: fan_out (parallel workers), fan_in (collect all), pipeline (sequential stages), "
    "barrier (synchronize), scatter_gather (template across items).\n"
    "\n"
    "Code analysis: diff (unstaged changes), verify (lint+tests for session changes), diagnose_failures "
    "(structured failure summary), find_usages (all references), restore_file (undo last write/edit).\n"
    "\n"
    "Session tools: session_stats (turns/tokens/agents), recall_turn (recover pruned context).\n"
    "\n"
    "External: read_image (GPT-4o vision for screenshots/diagrams).\n"
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

    # 3. Project knowledge (cross-session learnings, if available)
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
            lines.append("\n## Project Learnings (from past sessions)")
            for entry in other_entries:
                cat = entry.get("category", "general")
                s = entry.get("summary", "")
                d = entry.get("detail", "")
                tags = f"[{cat}]"
                lines.append(f"- {tags} {s}" + (f" — {d}" if d else ""))
        parts.append("\n".join(lines))

    return "\n".join(parts) + "\n"
