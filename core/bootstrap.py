#!/usr/bin/env python3
"""bootstrap.py -- agent session initialization for mini_agent.

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

from .config import (
    AgentConfig,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_POOL_CONNECTIONS,
    HTTP_POOL_MAXSIZE,
    _is_remote_workspace,
)
from .safety import ReadSafetyGate, WriteSafetyGate
from memory.memory import MemoryStore
from .prompt import build_system_prompt, build_startup_context, build_session_header, build_memory_snapshot
from agents.agent_runtime import AgentRuntime

# MCP tool schemas -- injected into TOOLS lazily when config.mcp_servers is non-empty.
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
    # Suppress HF Hub warnings EARLY -- before any huggingface_hub import.
    # The sentence-transformers model is cached locally; the "unauthenticated
    # requests" warning is pure noise in the Electron stderr log and can
    # cause the first tool call to appear hung while the warning writes to
    # stderr (especially on Windows where I/O is synchronous).
    import os as _os_bootstrap
    _os_bootstrap.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    _os_bootstrap.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

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

    # Restore persisted plan state from previous session
    try:
        from tools import _TOOL_CONTEXT
        saved_steps, saved_done = memory.get_plan()
        if saved_steps:
            _TOOL_CONTEXT._plan_steps = saved_steps
            _TOOL_CONTEXT._plan_done = set(saved_done)
            _TOOL_CONTEXT._plan_last_advanced_turn = 0  # fresh session
    except Exception:
        pass  # best-effort; plan is in-memory anyway

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

    # Warmup: run a trivial cmd.exe call to absorb any first-invocation
    # antivirus scan delay (Windows Defender is known to pause first
    # cmd.exe / conhost.exe launches for behavioral analysis).
    if os.name == 'nt':
        try:
            _sp.run(["cmd.exe", "/c", "rem"], capture_output=True, timeout=10)
        except Exception:
            pass

    # Warmup: do a trivial open() to absorb any first-file-I/O antivirus
    # scan delay.  On Windows, the first CreateFile call from a new process
    # can be intercepted by minifilter drivers (antivirus, backup agents)
    # and delayed by several seconds.  Doing this here, before the user
    # sends their first prompt, hides that latency.
    _warmup_path = os.path.join(workspace, "CHANGELOG.md")
    try:
        if not os.path.isfile(_warmup_path):
            _warmup_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGELOG.md")
        if os.path.isfile(_warmup_path):
            with open(_warmup_path, "r", encoding="utf-8", errors="replace") as _wf:
                _wf.read(4096)  # read a small chunk to warm the FS cache
    except Exception:
        pass  # best-effort; never block startup on warmup failure

    # Warmup: thread I/O -- execute_tool() dispatches every tool call in a
    # fresh daemon thread.  Some Windows filter drivers (antivirus, DLP,
    # backup agents) associate I/O operations with thread context, so the
    # first CreateFile in a *new thread* can still be delayed even after the
    # main-thread warmup above.  Spawn a thread that does a trivial
    # open() + read() so the filter drivers absorb their scan cost before
    # the user's first prompt.
    #
    # We warm up MULTIPLE files because the first tool call typically
    # reads a different file than CHANGELOG.md, and some filter drivers
    # trigger per-file scanning on first access.
    if os.name == 'nt':
        import threading as _thr
        _thr_warmup_done = _thr.Event()
        # Files the LLM is likely to read on its first turn (based on
        # system prompt rules and startup context).
        _warmup_files = []
        for _wf_name in ("CHANGELOG.md", "STATE.txt", "README.md",
                         ".mini_agent.rules", "HANDOFF.md"):
            _wp = os.path.join(workspace, _wf_name)
            if os.path.isfile(_wp):
                _warmup_files.append(_wp)
        def _warmup_thread_io():
            try:
                import sys as _sys_warmup
                _sys_warmup.stderr.write("[warmup] thread started\n")
                _sys_warmup.stderr.flush()
                for _wp in _warmup_files:
                    _sys_warmup.stderr.write(f"[warmup] reading {os.path.basename(_wp)}\n")
                    _sys_warmup.stderr.flush()
                    with open(_wp, "r", encoding="utf-8", errors="replace") as _wtf:
                        _wtf.read(4096)
                # Also warm subprocess.Popen from a daemon thread: on Windows,
                # the first CreateProcess call in a new thread can be delayed
                # by antivirus filter drivers (same as the file-I/O warmup).
                # A trivial cmd.exe invocation absorbs this latency so the
                # user's first tool call doesn't hang.
                try:
                    _sys_warmup.stderr.write("[warmup] spawning cmd.exe\n")
                    _sys_warmup.stderr.flush()
                    _sp.run(["cmd.exe", "/c", "rem"],
                            capture_output=True, timeout=10)
                except Exception:
                    pass
                # Also warm sys.executable (python.exe) -- tool calls like
                # read_file spawn "python -m tools._worker" subprocesses.
                # The first CreateProcess for a new executable can trigger
                # separate antivirus scanning even after cmd.exe is warm.
                try:
                    _sys_warmup.stderr.write("[warmup] spawning python.exe\n")
                    _sys_warmup.stderr.flush()
                    _sp.run([sys.executable, "-c", "print"],
                            capture_output=True, timeout=10)
                except Exception:
                    pass
                # Also warm the SentenceTransformer encode() call so any
                # HF Hub warnings / lazy downloads / tokenizer warmup happen
                # in the background before the first tool call.
                try:
                    _sys_warmup.stderr.write("[warmup] loading embedding model\n")
                    _sys_warmup.stderr.flush()
                    from tools.search_ops import _sem_get_model
                    _model = _sem_get_model()
                    if _model is not None:
                        _sys_warmup.stderr.write("[warmup] running encode()\n")
                        _sys_warmup.stderr.flush()
                        _model.encode("warmup", show_progress_bar=False)
                except Exception:
                    pass
                _sys_warmup.stderr.write("[warmup] thread done\n")
                _sys_warmup.stderr.flush()
            except Exception:
                pass
            finally:
                _thr_warmup_done.set()
        _tw = _thr.Thread(target=_warmup_thread_io, daemon=True)
        _tw.start()
        _thr_warmup_done.wait(timeout=30)

    # Reset skill gates -- start each session with core tools only
    reset_skills()

    # Initialize multi-agent runtime
    runtime = AgentRuntime()
    set_context(_agent_config=config, _agent_runtime=runtime)

    # --- Start embedding model preload EARLY so the download overlaps with
    # the slow workspace scan and LSP init below.  _sem_preload() starts a
    # daemon thread and returns immediately.  We wait for it at the end of
    # bootstrap, after all other slow init has been kicked off.
    _sem_preload_event = None
    try:
        from tools.search_ops import _sem_preload, _SEM_PRELOAD_EVENT
        _sem_preload()
        _sem_preload_event = _SEM_PRELOAD_EVENT  # snapshot: set atomically
    except Exception:
        pass  # model preload is best-effort; semantic_search degrades gracefully

    # --- workspace scanning (skip on remote filesystems to avoid hangs) ---
    remote = _is_remote_workspace(workspace)

    if not remote:
        build_symbol_index(workspace)
    else:
        print(f"  WARNING: Remote workspace detected ({workspace}) -- skipping symbol index scan",
              file=sys.stderr)

    # Initialize LSP (pylsp) with workspace root so LSP tools work.
    # Skip on remote workspaces -- LSP scanning over SMB can hang.
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
            pass  # MCP servers are optional -- tolerate startup failures

    # Wait for the embedding model preload to finish (started above).
    # On a cold cache (first run), SentenceTransformer downloads ~90 MB from
    # HuggingFace Hub.  We give it up to 120 s (matching _SEM_MODEL_TIMEOUT).
    # The overlap with build_symbol_index / set_lsp_root above means much of
    # this wait is already elapsed by the time we get here.
    if _sem_preload_event is not None:
        _sem_preload_event.wait(timeout=120)
        # Warmup: do a trivial encoding to trigger any lazy initialization
        # (tokenizer download, HF Hub auth warnings, etc.) NOW during bootstrap
        # instead of during the first tool call. On Windows, the first
        # SentenceTransformer.encode() call can trigger HuggingFace Hub
        # downloads that interfere with concurrent subprocess tool calls.
        try:
            from tools.search_ops import _sem_get_model
            model = _sem_get_model()
            if model is not None:
                model.encode("warmup", show_progress_bar=False)
        except Exception:
            pass  # best-effort; model is optional

    # Auto-init .mini_agent.rules and .mini_agent.toml if they don't exist yet.
    # Skip on remote workspaces -- os.path.isfile() can hang on stale SMB mounts.
    if not remote:
        rules_path = os.path.join(workspace, ".mini_agent.rules")
        if not os.path.isfile(rules_path):
            try:
                from tools.file_ops import _init_rules
                result = _init_rules({}, None, read_gate)
                if result.success:
                    print(f"  (*) Auto-init: {result.content[:120]}", file=sys.stderr)
            except OSError as exc:
                print(f"  WARNING: Auto-init skipped: {exc}", file=sys.stderr)

    saved = memory.load()
    # Prune loaded conversation to avoid massive first-turn payload
    if saved:
        from memory.memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned
        saved, _ = _compress_tool_results(saved, keep_recent=20)
        saved, pruned = _prune_by_tokens(saved, config.context_window, config.max_messages)
        if pruned:
            summary = _summarize_pruned(pruned)
            if summary:
                saved.insert(0, {"role": "user", "content": summary})
                memory.last_prune_summary = summary
    knowledge = memory.get_top_knowledge(limit=15) if memory else []
    core_memory = memory.get_core_memory() if memory else ""
    memory_snapshot = build_memory_snapshot(core_memory)

    startup_ctx = build_startup_context(workspace, knowledge=knowledge)
    session_header = build_session_header(config)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(config)},
        {"role": "user", "content": session_header},
    ]
    if memory_snapshot:
        messages.append({"role": "user", "content": memory_snapshot})
    messages.append({"role": "user", "content": startup_ctx})
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
    _TOOL_CONTEXT._tasks_injected = False
    # Reset pattern rules for new session
    from core.context_inject import _reset_pattern_rules
    _reset_pattern_rules()

    session = _requests.Session()
    # Set default timeout (connect, read) for every request.
    session.request = functools.partial(session.request, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
    # Limit connection pool to avoid resource waste on long-running sessions.
    session.mount("https://", _requests.adapters.HTTPAdapter(
        pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE))

    # Combined exit handler -- runs in correct order: summary capture -> LSP
    # shutdown -> HTTP session close.  Wrapped in broad try/except so no
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
            # Capture plan state for handoff
            plan_steps = getattr(_TOOL_CONTEXT, "_plan_steps", [])
            plan_done = list(getattr(_TOOL_CONTEXT, "_plan_done", set()))
            memory.write_session_handoff(
                workspace, start_head=start_head,
                pending=pending, notes="",
                plan_steps=plan_steps if plan_steps else None,
                plan_done=plan_done if plan_done else None,
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

        # 3. Shutdown LSP connections (skip on remote workspaces).
        if not remote:
            try:
                _shutdown_lsp()
            except Exception:
                pass

        # 4. Close HTTP session.
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
