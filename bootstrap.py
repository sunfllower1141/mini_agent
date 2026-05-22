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
)
from safety import ReadSafetyGate, WriteSafetyGate
from memory import MemoryStore
from prompt import build_system_prompt, build_startup_context
from agent_runtime import AgentRuntime


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
    write_gate = WriteSafetyGate(workspace, allow_overwrites=config.allow_overwrites,
                                 unrestricted=config.unrestricted)
    read_gate = ReadSafetyGate(workspace, unrestricted=config.unrestricted)
    memory_path = os.path.join(workspace or os.getcwd(), config.memory_filename)
    memory = MemoryStore(memory_path, max_messages=config.max_messages,
                         max_tokens=config.context_window)
    set_context(exa_api_key=config.exa_api_key, openai_api_key=config.openai_api_key,
                scratchpad_path=memory._db_path, _memory_store=memory)

    # Reset skill gates — start each session with core tools only
    reset_skills()

    # Initialize multi-agent runtime
    runtime = AgentRuntime()
    set_context(_agent_config=config, _agent_runtime=runtime)

    build_symbol_index(workspace)

    # Initialize LSP (pylsp) with workspace root so LSP tools work
    from tools.lsp import set_lsp_root, shutdown_lsp as _shutdown_lsp
    set_lsp_root(workspace)

    # Preload semantic search model in background (non-blocking)
    # so the ~9s cold start hides behind the first user interaction.
    try:
        from tools.search_ops import _sem_preload
        _sem_preload()
    except Exception:
        pass  # sentence-transformers may not be installed — tolerate

    # Auto-init .mini_agent.rules and .mini_agent.toml if they don't exist yet
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

    session = _requests.Session()
    # Set default timeout (connect, read) for every request.
    session.request = functools.partial(session.request, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
    # Limit connection pool to avoid resource waste on long-running sessions.
    session.mount("https://", _requests.adapters.HTTPAdapter(
        pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE))

    # Ensure the session is closed on normal interpreter shutdown.
    atexit.register(session.close)
    atexit.register(_shutdown_lsp)

    return {
        "config": config,
        "write_gate": write_gate,
        "read_gate": read_gate,
        "memory": memory,
        "messages": messages,
        "session": session,
    }
