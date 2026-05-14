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

Configuration:
  Set EXA_API_KEY in your environment or .mini_agent.toml for web search.
  See STATE.txt for architecture overview and tool reference.
"""

import json
import os
import sys
import time

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
    with open(path, "w") as f:
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

    session = session_data["session"]
    try:
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                memory.save(messages)
                break

            if user_input.lower() == "quit":
                memory.save(messages)
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

            if user_input.lower() == "/stats":
                print(f"Turns: {stats['turns']}  Tool calls: {stats['tool_calls']}  Messages: {len(messages)}")
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
            memory.save(messages)
            _log(config.verbose, c("─" * 50, DIM), file=sys.stderr)
    finally:
        session.close()


if __name__ == "__main__":
    main()
