#!/usr/bin/env python3
"""
workspace_bot.py -- A Discord bot that knows everything about *this* workspace
(the mini_agent project itself).  Add it to a different Discord channel for
help with development, debugging, architecture questions, etc.

Usage:
  WORKSPACE_BOT_TOKEN=... python workspace_bot.py

The token can also be placed in mini_agent/.env as WORKSPACE_BOT_TOKEN.
"""

from __future__ import annotations

import os
import sys

# Ensure the mini_agent package is importable
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from discord_bot import MiniAgentDiscordBot, DISCORD_MAX_MSG, INTENTS
from core.config import AgentConfig
from core.bootstrap import init_session


def main() -> None:
    workspace = _HERE  # this project itself

    print(f"[workspace_bot] Workspace: {workspace}")

    # Load .env first so WORKSPACE_BOT_TOKEN (and any API keys in the
    # mini_agent .env) are available before we bootstrap the session.
    from core.config import _load_dotenv
    _load_dotenv(workspace)

    # Resolve token (must happen before init_session which may print a
    # confusing "no DISCORD_BOT_TOKEN" warning)
    token = os.environ.get("WORKSPACE_BOT_TOKEN", "")
    if not token:
        print("[workspace_bot] FATAL: WORKSPACE_BOT_TOKEN not set.")
        print("[workspace_bot] Add it to mini_agent/.env:")
        print("[workspace_bot]   echo 'WORKSPACE_BOT_TOKEN=...' >> .env")
        print("[workspace_bot] Or export it: export WORKSPACE_BOT_TOKEN=...")
        sys.exit(1)

    # Bootstrap the agent session
    os.environ["MINI_AGENT_UI"] = "discord"
    session_data = init_session(workspace)
    config: AgentConfig = session_data["config"]
    write_gate = session_data["write_gate"]
    read_gate = session_data["read_gate"]
    memory = session_data["memory"]
    base_messages = session_data["messages"]

    # Clear sub-agent callback to avoid Electron noise
    try:
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._subagent_callback = None
    except Exception:
        pass

    print(f"[workspace_bot] Agent initialized (model={config.model}, "
          f"provider={config.api_provider})")

    bot = MiniAgentDiscordBot(
        workspace=workspace,
        config=config,
        write_gate=write_gate,
        read_gate=read_gate,
        memory=memory,
        base_messages=base_messages,
    )

    try:
        bot.run(token)
    except Exception as e:
        print(f"[workspace_bot] FATAL: {e}")
        sys.exit(1)
    finally:
        try:
            memory.close()
        except Exception:
            pass
        print("[workspace_bot] Done.")


if __name__ == "__main__":
    main()
