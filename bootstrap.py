#!/usr/bin/env python3
"""bootstrap.py — agent session initialization for mini_agent.

Extracted from config.py to keep the config module focused on
configuration loading.  This module ties together config, safety,
memory, tools, LSP, semantic search, and the requests session.
"""
from __future__ import annotations

import os
import sys
import atexit
import functools

import requests as _requests

from config import (
    AgentConfig,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_POOL_CONNECTIONS,
    HTTP_POOL_MAXSIZE,
    _is_remote_workspace,
)
from safety import ReadSafetyGate, WriteSafetyGate
from memory import MemoryStore
from prompt import build_system_prompt, build_startup_context
from agent_runtime import AgentRuntime

# MCP tool schemas — injected into TOOLS lazily when config.mcp_servers is non-empty.
_MCP_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "mcp_discover",
            "description": "Discover tools from all configured MCP (Model Context Protocol) servers. Lists every tool available across all connected servers with their descriptions. Use this before mcp_call to see what tools are available.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "Call a tool on a connected MCP (Model Context Protocol) server. Use mcp_discover first to see available servers and tools. Servers are configured in .mini_agent.toml [agent.mcp_servers.<name>].",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "MCP server name (as configured in .mini_agent.toml)."
                    },
                    "tool": {
                        "type": "string",
                        "description": "Tool name to call on the server."
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Optional: arguments to pass to the MCP tool as a JSON object."
                    }
                },
                "required": ["server", "tool"]
            }
        }
    }
]


def init_session(workspace: str, cli_args: object | None = None) -> dict:
    """Shared agent initialization used by both terminal and TUI.

    *cli_args* is an optional argparse namespace. Pass it to forward
    CLI flags to AgentConfig.load().

    Returns dict with keys: config, write_gate, read_gate, memory,
    messages, session.
    """
    from tools import set_context, build_symbol_index
    from tools.skills import reset_skills

    config = AgentConfig.load(workspace, cli_args=cli_args)
    # Windows SOCKS tunnel auto-start (no-op on other platforms)
    from config import _start_windows_tunnel
    _start_windows_tunnel(config)
    write_gate = WriteSafetyGate(workspace, allow_overwrites=config.allow_overwrites,
                                 unrestricted=config.unrestricted)
    read_gate = ReadSafetyGate(workspace, unrestricted=config.unrestricted)
    memory_path = os.path.join(workspace or os.getcwd(), config.memory_filename)
    memory = MemoryStore(memory_path, max_messages=config.max_messages,
                         max_tokens=config.context_window)
    set_context(exa_api_key=config.exa_api_key, openai_api_key=config.openai_api_key,
                scratchpad_path=memory._db_path, _memory_store=memory)

    # Initialize self-learning systems (FailurePatternStore + SelfCritique)
    try:
        from tools.failure_learning import FailurePatternStore, SelfCritique
        fps = FailurePatternStore(memory._db_path)
        fps.init_schema()
        sc = SelfCritique()
        set_context(_failure_pattern_store=fps, _self_critique=sc)
    except Exception:
        pass  # Self-learning is best-effort, never blocks startup

    # Initialize ToolGraph and MistakeNotebook
    try:
        from tools.tool_graph import ToolGraph
        from tools.failure_learning import MistakeNotebook
        tg = ToolGraph(memory._db_path)
        tg.init_schema()
        mn = MistakeNotebook(memory._db_path)
        mn.init_schema()
        set_context(_tool_graph=tg, _mistake_notebook=mn)
    except Exception:
        pass  # Best-effort, never blocks startup

    # Capture git HEAD at session start for auto-handoff diff
    from tools import _TOOL_CONTEXT
    try:
        import subprocess as _sp
        r = _sp.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            _TOOL_CONTEXT._session_start_head = r.stdout.strip()
    except (OSError, _sp.TimeoutExpired):
        _TOOL_CONTEXT._session_start_head = None

    # Reset skill gates — start each session with core tools only
    reset_skills()

    # Initialize multi-agent runtime
    runtime = AgentRuntime()
    set_context(_agent_config=config, _agent_runtime=runtime)

    # --- workspace scanning (skip on remote filesystems to avoid hangs) ---
    remote = _is_remote_workspace(workspace)

    if not remote:
        build_symbol_index(workspace)
    else:
        print(f"  ⚠ Remote workspace detected ({workspace}) — skipping symbol index scan",
              file=sys.stderr)

    # Initialize LSP (pylsp) with workspace root so LSP tools work.
    # Skip on remote workspaces — LSP scanning over SMB can hang.
    from tools.lsp import set_lsp_root, shutdown_lsp as _shutdown_lsp
    if not remote:
        set_lsp_root(workspace)

    # Initialize MCP client with servers from config (graceful fallback if none)
    if config.mcp_servers:
        try:
            from tools.mcp_client import init_mcp_servers, shutdown_mcp as _shutdown_mcp
            init_mcp_servers(config.mcp_servers)
            atexit.register(_shutdown_mcp)
            # Lazily inject mcp_discover / mcp_call schemas into TOOLS
            # only when MCP servers are actually configured.
            from tools.schema import TOOLS
            if not any(td["function"]["name"] == "mcp_discover" for td in TOOLS):
                TOOLS.extend(_MCP_SCHEMAS)
        except Exception:
            pass  # MCP servers are optional — tolerate startup failures

    # Preload semantic search model in background (non-blocking)
    # so the ~9s cold start hides behind the first user interaction.
    try:
        from tools.search_ops import _sem_preload
        _sem_preload()
    except Exception:
        pass  # sentence-transformers may not be installed — tolerate

    # Auto-init .mini_agent.rules and .mini_agent.toml if they don't exist yet.
    # Skip on remote workspaces — os.path.isfile() can hang on stale SMB mounts.
    if not remote:
        rules_path = os.path.join(workspace, ".mini_agent.rules")
        if not os.path.isfile(rules_path):
            try:
                from tools.file_ops import _init_rules
                result = _init_rules({}, None, read_gate)
                if result.success:
                    print(f"  ✨ Auto-init: {result.content[:120]}", file=sys.stderr)
            except OSError as exc:
                print(f"  ⚠ Auto-init skipped: {exc}", file=sys.stderr)

    saved = memory.load()
    # Prune loaded conversation to avoid massive first-turn payload
    if saved:
        from memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned
        saved, _ = _compress_tool_results(saved, keep_recent=20)
        saved, pruned = _prune_by_tokens(saved, config.context_window, config.max_messages)
        if pruned:
            summary = _summarize_pruned(pruned)
            if summary:
                saved.insert(0, {"role": "user", "content": summary})
    knowledge = memory.get_top_knowledge(limit=15) if memory else []
    startup_ctx = build_startup_context(workspace, knowledge=knowledge)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(config)},
        {"role": "user", "content": startup_ctx},
    ]
    if saved:
        messages.extend(saved)

    # Reset one-time injection flags for this session (per-session, not per-turn).
    # These gates prevent HANDOFF.md, STATE.txt, scratchpad, and git diff from
    # being re-injected on every user message when run_agent_turn() is called
    # multiple times in the same session.
    _TOOL_CONTEXT._scratchpad_injected = False
    _TOOL_CONTEXT._git_diff_injected = False
    _TOOL_CONTEXT._handoff_injected = False
    _TOOL_CONTEXT._state_txt_injected = False

    session = _requests.Session()
    # Set default timeout (connect, read) for every request.
    session.request = functools.partial(session.request, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
    # Limit connection pool to avoid resource waste on long-running sessions.
    session.mount("https://", _requests.adapters.HTTPAdapter(
        pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE))

    # Combined exit handler — runs in correct order: summary capture → LSP
    # shutdown → HTTP session close.  Wrapped in broad try/except so no
    # single failure blocks the rest or prints tracebacks during interpreter
    # teardown (when stderr may already be closed).
    def _cleanup_on_exit() -> None:
        # 1. Auto-write HANDOFF.md for next-session continuity
        try:
            import warnings as _wrn
            from tools import _TOOL_CONTEXT
            with _wrn.catch_warnings():
                _wrn.simplefilter("ignore")
                scratchpad = memory.get_scratchpad()
            start_head = getattr(_TOOL_CONTEXT, "_session_start_head", None)
            # Derive pending items from scratchpad (look for "Pending" or "TODO" section)
            pending = ""
            if scratchpad:
                import re as _re
                m = _re.search(
                    r"(?:##\s*Pending|##\s*TODO|##\s*What.s Pending)(.*?)(?:##|$)",
                    scratchpad, _re.DOTALL | _re.IGNORECASE,
                )
                if m:
                    pending = m.group(1).strip()[:500]
            memory.write_session_handoff(
                workspace, start_head=start_head,
                pending=pending, notes="",
            )
        except Exception:
            pass

        # 2. Capture session summary (best-effort, must run before memory is
        #    affected by LSP or session teardown).
        try:
            import warnings as _wrn
            from tools import _TOOL_CONTEXT
            with _wrn.catch_warnings():
                _wrn.simplefilter("ignore")
                scratchpad = memory.get_scratchpad()
            turn_keys = sorted(getattr(_TOOL_CONTEXT, "_turn_history", {}).keys())
            recent_turns = []
            for k in turn_keys[-5:]:
                recent_turns.append(_TOOL_CONTEXT._turn_history.get(k, ""))
            turn_text = "\n".join(recent_turns)
            summary = scratchpad[:300] if scratchpad else turn_text[:300]
            detail = f"Scratchpad:\n{scratchpad[:500]}\n\nTurn history:\n{turn_text[:500]}"
            if summary.strip():
                memory.capture_session_summary(summary[:200], detail[:1000])
        except Exception:
            pass

        # 2. Shutdown LSP connections (skip on remote workspaces).
        if not remote:
            try:
                _shutdown_lsp()
            except Exception:
                pass

        # 3. Close HTTP session.
        try:
            session.close()
        except Exception:
            pass

    atexit.register(_cleanup_on_exit)

    return {
        "config": config,
        "write_gate": write_gate,
        "read_gate": read_gate,
        "memory": memory,
        "messages": messages,
        "session": session,
    }
