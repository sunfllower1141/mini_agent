#!/usr/bin/env python3
"""
mini_agent — a coding agent powered by DeepSeek V4 Pro with 11 tools.

All file reads and writes go through the safety layer (safety.py).
Memory persists between sessions via SQLite (memory.py).
Tools are defined and executed in tools.py.
Config lives in .mini_agent.toml (config.py).
LLM communication is handled by llm.py.
The system prompt lives in prompt.py.

Flags:
  --workspace PATH       Set workspace root (default: current directory)
  --stream               Stream responses token-by-token (default: off)
  --quiet                Suppress tool execution logs
  --no-color             Disable ANSI colours in output
  --allow-overwrites     Allow overwriting existing files without confirmation
  --approve              Prompt for approval before each write/destructive op
  --help, -h             Show this message and exit

Environment:
  AGENT_WORKSPACE        Workspace root (overridden by --workspace)
  DEEPSEEK_API_KEY       API key for the LLM provider
  EXA_API_KEY            API key for web search (Exa)

Config file (.mini_agent.toml) can set model, stream, and other defaults.

Session commands (type at the prompt):
  quit                Save memory and exit
  clear               Reset conversation memory
  /init               Reinitialize .mini_agent.rules + .mini_agent.toml
  /workspace <path>   Switch to a different workspace directory
  /export             Export conversation to a markdown file
  /stats              Show session statistics (turns, tool calls, messages)
  /session <cmd>      Manage named sessions: new, switch, delete, list

Configuration:
  Set EXA_API_KEY in your environment or .mini_agent.toml for web search.
  See STATE.txt for architecture overview and tool reference.
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading

import requests

from config import AgentConfig, CONFIG_FILENAME, resolve_workspace, init_session, parse_args
from llm import run_agent_turn
from prompt import build_system_prompt
from safety import ReadSafetyGate, WriteSafetyGate
from memory import MemoryStore
from terminal import c, DIM, _CYAN, _YELLOW, _GREEN, _RED
from tools import set_context, build_symbol_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approve(tool_name: str, args: dict) -> bool:
    """Ask the user to approve a write/destructive tool call."""
    from terminal import c, _YELLOW, _RED
    brief = json.dumps(args)
    if len(brief) > 100:
        brief = brief[:100] + "..."
    prompt = f"  {c('Allow', _YELLOW)} {tool_name}({brief})? [y/N] "
    try:
        answer = input(prompt).strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _log(verbose: bool, *args, **kwargs) -> None:
    """Print diagnostic output, unless verbose is disabled.  Always flushes."""
    if verbose:
        kwargs.setdefault("flush", True)
        print(*args, **kwargs)


def _export_conversation(messages: list[dict], workspace: str) -> str:
    """Write conversation to a timestamped markdown file."""
    import datetime
    from memory import export_conversation_markdown
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"conversation_{ts}.md"
    path = os.path.join(workspace, fname)
    md = export_conversation_markdown(messages)
    md = md.replace("mini_agent conversation", f"mini_agent conversation — {ts}", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    # Parse with argparse (gives --help for free)
    cli = parse_args()

    workspace = resolve_workspace(override=cli.workspace)
    session_data = init_session(workspace, cli_args=cli)
    config = session_data["config"]
    write_gate = session_data["write_gate"]
    read_gate = session_data["read_gate"]
    memory = session_data["memory"]
    messages = session_data["messages"]

    _log(config.verbose, f"mini_agent — workspace: {write_gate.workspace_root}")

    # Session stats
    stats = {"turns": 0, "tool_calls": 0}
    _log(config.verbose, f"model: {config.model}  stream: {config.stream}")
    if os.path.isfile(os.path.join(config.workspace, CONFIG_FILENAME)):
        _log(config.verbose, f"config: {CONFIG_FILENAME} loaded")
    _log(config.verbose, "Type 'quit' to exit, 'clear' to reset memory, --help for flags.")
    if not config.verbose:
        _log(config.verbose, "(quiet mode — use --quiet to suppress tool logs)")
    _log(config.verbose)

    def _auto_wake_subagents(messages):
        """Check for sub-agent completions/messages and inject as user turn."""
        try:
            runtime = getattr(tools._TOOL_CONTEXT, "_agent_runtime", None)
            if runtime is not None:
                pending = runtime.get_pending_results()
                if pending:
                    parts = ["[Auto-wake] Sub-agent(s) completed since last response:"]
                    for tid, result in pending:
                        status = "OK" if result.success else "FAILED"
                        parts.append(f"  - {tid}: [{status}] {str(result.content)[:300]}")
                    parts.append("Respond to the completions above.")
                    messages.append({"role": "user", "content": "\n".join(parts)})
                    return True
        except Exception:
            pass
        return False

    session = session_data["session"]
    
    # --- Non-blocking stdin: background thread reads lines into a queue ---
    import queue as _q
    _stdin_queue: _q.Queue = _q.Queue()
    def _read_stdin():
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    _stdin_queue.put(None)  # EOF
                    return
                _stdin_queue.put(line.strip())
            except Exception:
                _stdin_queue.put(None)
                return
    _stdin_thread = threading.Thread(target=_read_stdin, daemon=True, name="stdin-reader")
    _stdin_thread.start()

    try:
        while True:
            # --- Auto-wake: check sub-agents before blocking on input ---
            if _auto_wake_subagents(messages):
                continue

            # --- Wait for stdin input ---
            user_input = None
            while not user_input:
                try:
                    stdin_line = _stdin_queue.get(timeout=0.5)
                    if stdin_line is None:  # EOF
                        print("\nGoodbye.")
                        messages = memory.save(messages)
                        return
                    if stdin_line:
                        user_input = stdin_line
                except _q.Empty:
                    pass

            if user_input == "/init":
                from tools.file_ops import _init_rules
                from tools.schema import ToolResult
                from safety import ReadSafetyGate
                rg = ReadSafetyGate(workspace)
                result = _init_rules({}, None, rg)
                print(f"  {result.content}")
                continue

            if user_input.lower() == "quit":
                messages = memory.save(messages)
                if stats["turns"] > 0:
                    print(f"Session: {stats['turns']} turns, {stats['tool_calls']} tool calls")
                break

            if user_input.lower() == "clear":
                messages = [{"role": "system", "content": build_system_prompt(config)}]
                memory.clear()
                stats = {"turns": 0, "tool_calls": 0}
                _log(config.verbose, "Memory cleared.\n")
                continue

            if user_input.lower() == "/export":
                path = _export_conversation(messages, workspace)
                print(f"Exported to {path}")
                continue

            if user_input.lower() in ("/help", "/h", "-h", "--help"):
                print("Session commands:")
                print("  quit                Save memory and exit")
                print("  clear               Reset conversation memory")
                print("  /init               Reinitialize .mini_agent.rules + .mini_agent.toml")
                print("  /workspace <path>   Switch to a different workspace directory")
                print("  /export             Export conversation to a markdown file")
                print("  /stats              Show session statistics (turns, tool calls, messages)")
                print("  /session <cmd>      Manage named sessions: new, switch, delete, list")
                continue

            if user_input.lower() == "/stats":
                print(f"Turns: {stats['turns']}  Tool calls: {stats['tool_calls']}  Messages: {len(messages)}")
                continue

            if user_input.lower().startswith("/session"):
                parts = user_input.split(maxsplit=2)
                sub = parts[1] if len(parts) > 1 else ""
                arg = parts[2] if len(parts) > 2 else ""
                from config import list_sessions, switch_session, delete_session
                if sub == "list":
                    sessions = list_sessions(workspace)
                    if sessions:
                        print(f"Sessions: {', '.join(sessions)}")
                    else:
                        print("No saved sessions found.")
                elif sub == "new" and arg:
                    session_data = switch_session(workspace, arg, memory, config)
                    messages = memory.save(messages)
                    memory = session_data["memory"]
                    messages = session_data["messages"]
                    stats = {"turns": 0, "tool_calls": 0}
                    print(f"Created and switched to session '{arg}'.")
                elif sub == "switch" and arg:
                    messages = memory.save(messages)
                    session_data = switch_session(workspace, arg, memory, config)
                    memory = session_data["memory"]
                    messages = session_data["messages"]
                    stats = {"turns": 0, "tool_calls": 0}
                    print(f"Switched to session '{arg}'.")
                elif sub == "delete" and arg:
                    ok, msg = delete_session(workspace, arg)
                    print(msg)
                else:
                    print("Usage: /session new <name> | switch <name> | delete <name> | list")
                continue

            if user_input.lower().startswith("/workspace"):
                parts = user_input.split(maxsplit=1)
                new_path = parts[1].strip() if len(parts) > 1 else ""
                if not new_path:
                    print("Usage: /workspace <path>")
                    continue
                new_workspace = os.path.abspath(new_path)
                if not os.path.isdir(new_workspace):
                    print(f"Not a directory: {new_workspace}")
                    continue
                # Save current session, then reinitialize at new workspace
                messages = memory.save(messages)
                from config import init_session as _init_session
                try:
                    new_data = _init_session(new_workspace, cli_args=cli)
                except Exception as exc:
                    print(f"Error switching workspace: {exc}")
                    continue
                # Replace all session state
                config = new_data["config"]
                write_gate = new_data["write_gate"]
                read_gate = new_data["read_gate"]
                memory = new_data["memory"]
                messages = new_data["messages"]
                session.close()
                session = new_data["session"]
                workspace = new_workspace
                stats = {"turns": 0, "tool_calls": 0}
                print(f"Workspace switched to: {workspace}")
                continue

            if not user_input:
                continue

            # Show scratchpad
            sp = memory.get_scratchpad()
            if sp.strip():
                _log(config.verbose, f"  {c('📝 scratchpad:', DIM)}")
                for line in sp.strip().split("\n"):
                    _log(config.verbose, f"  {c(line, DIM)}")
                _log(config.verbose)

            # ----- User turn -----
            messages.append({"role": "user", "content": user_input})

            # ----- Agent turn -----
            _log(config.verbose,
                 f"  {c('⏳', _CYAN)} calling API…", file=sys.stderr)
            t0 = time.monotonic()

            def _tool_start(summary: str, parallel: bool = False) -> None:
                nonlocal t0, stats
                stats["tool_calls"] += 1
                elapsed = time.monotonic() - t0
                _log(config.verbose,
                     f"  {c('←', _YELLOW)} tool call(s) after {elapsed:.1f}s",
                     file=sys.stderr)
                _log(config.verbose,
                     f"  {c('🔧', _YELLOW)} {summary}",
                     file=sys.stderr)

            def _tool_end(ok: bool, detail: str, diff_preview: str | None = None) -> None:
                if ok:
                    _log(config.verbose,
                         f"     {c('✓', _GREEN)}  ok",
                         file=sys.stderr)
                else:
                    _log(config.verbose,
                         f"     {c('✗', _RED)}  FAILED: {c(detail, _RED)}",
                         file=sys.stderr)

            msg = run_agent_turn(
                messages, config, write_gate, read_gate,
                on_tool_start=_tool_start,
                on_tool_end=_tool_end,
                session=session,
                memory_store=memory,
                approve_callback=_approve if config.approve_write_ops else None,
            )
            elapsed = time.monotonic() - t0

            if msg is not None and not msg.get("tool_calls"):
                if not config.stream:
                    print(msg.get("content", ""))
                _log(config.verbose,
                     f"  {c('←', DIM)} text response ({elapsed:.1f}s)",
                     file=sys.stderr)

            # Track stats
            stats["turns"] += 1
            # Persist after every turn
            messages = memory.save(messages)
            _log(config.verbose, c("─" * 50, DIM), file=sys.stderr)
    finally:
        session.close()


if __name__ == "__main__":
    main()

