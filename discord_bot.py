#!/usr/bin/env python3
"""
discord_bot.py -- Discord bot frontend for mini_agent.

Connects to a Discord server, listens for @mentions, and runs agent
turns against a configured workspace.  One agent session per Discord
channel (shared context across all users in the channel).

Usage:
  DISCORD_BOT_TOKEN=... AGENT_WORKSPACE=/path/to/project python discord_bot.py

The bot token can also be placed in the workspace .env file or a
DISCORD_BOT_TOKEN env var.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from copy import deepcopy
from typing import Any

import discord

# Ensure the mini_agent package is importable (this script lives in the
# mini_agent root, but belt-and-suspenders).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core.config import AgentConfig, ENV_AGENT_WORKSPACE
from voice_handler import VoiceHandler
from core.llm import run_agent_turn
from core.bootstrap import init_session
from api import clear_api_cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_MAX_MSG = 1900  # Discord limit is 2000; leave margin

# Discord bot intents -- only what we need
INTENTS = discord.Intents.default()
INTENTS.message_content = True   # needed to read @mention content
INTENTS.messages = True          # needed for on_message events
INTENTS.voice_states = True      # needed for voice-channel join/leave detection
INTENTS.members = True           # needed for member lists & events
INTENTS.presences = True         # needed for user activity/status

# ---------------------------------------------------------------------------
# Per-channel session
# ---------------------------------------------------------------------------

class ChannelSession:
    """Holds the conversation state for one Discord channel."""

    def __init__(self, channel_id: int, base_messages: list[dict]):
        self.channel_id = channel_id
        self.messages: list[dict] = deepcopy(base_messages)
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._processing = False

    def cancel(self) -> None:
        """Signal the current turn to stop (best-effort)."""
        self._cancel_event.set()

    def reset(self, base_messages: list[dict]) -> None:
        """Clear conversation back to initial state."""
        with self._lock:
            self._cancel_event.set()  # cancel any running turn
            self.messages = deepcopy(base_messages)
            clear_api_cache()

    @property
    def message_count(self) -> int:
        return len(self.messages)


# ---------------------------------------------------------------------------
# Discord bot client
# ---------------------------------------------------------------------------

class MiniAgentDiscordBot(discord.Client):
    """Discord client that routes @mentions to mini_agent per channel."""

    def __init__(
        self,
        workspace: str,
        config: AgentConfig,
        write_gate: Any,
        read_gate: Any,
        memory: Any,
        base_messages: list[dict],
        voice: VoiceHandler | None = None,
    ):
        super().__init__(intents=INTENTS)
        self.workspace = workspace
        self.config = config
        self.write_gate = write_gate
        self.read_gate = read_gate
        self.memory = memory
        self.base_messages = base_messages
        self.voice = voice
        self.channels: dict[int, ChannelSession] = {}
        self._channels_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Discord event handlers
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        print(f"[discord_bot] Logged in as {self.user}  ({self.user.id})")
        print(f"[discord_bot] Workspace: {self.workspace}")
        print(f"[discord_bot] Model: {self.config.model}")
        print(f"[discord_bot] Ready -- listening for @mentions")

    async def on_message(self, msg: discord.Message) -> None:
        # Ignore our own messages
        if msg.author == self.user:
            return

        # Ignore messages that don't @mention us
        if not self._mentions_me(msg):
            # Also allow DM channels
            if isinstance(msg.channel, discord.DMChannel):
                pass
            else:
                return

        channel_id = msg.channel.id

        # Strip the @mention from the text
        content = self._strip_mentions(msg)

        # Handle commands
        if content.startswith("/"):
            await self._handle_command(msg, content, channel_id)
            return

        if not content.strip():
            await msg.channel.send("What's up? Ask me something about the project.")
            return

        # Get or create the channel session
        with self._channels_lock:
            if channel_id not in self.channels:
                self.channels[channel_id] = ChannelSession(channel_id, self.base_messages)
            channel_sess = self.channels[channel_id]

        # Check if already processing
        if channel_sess._processing:
            await msg.channel.send(
                "I'm still working on the last request. Give me a moment..."
            )
            return

        # Run the agent turn in a thread (don't block the event loop)
        async with msg.channel.typing():
            try:
                # Inject Discord context so the agent can search server history
                self._current_guild_id = msg.guild.id if msg.guild else None
                from tools.context import set_context
                set_context(
                    discord_guild_id=self._current_guild_id,
                    discord_token=self.http.token,
                )
                # Build conversation context from recent channel history
                history_context = await self._build_history_context(msg)
                user_msg = f"{history_context}[Discord user: {msg.author.display_name}] {content}"
                response_text = await asyncio.to_thread(
                    self._run_agent_turn,
                    channel_sess,
                    user_msg,
                )
            except Exception as e:
                response_text = f"**Error:** {e}"

        # Send the response (may need to split if long)
        if response_text:
            for chunk in self._chunk_text(response_text, DISCORD_MAX_MSG):
                await msg.channel.send(chunk)
        else:
            await msg.channel.send("(No response -- the turn may have been cancelled.)")

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    async def _handle_command(
        self, msg: discord.Message, content: str, channel_id: int
    ) -> None:
        cmd = content.strip().lower()

        if cmd == "/clear":
            with self._channels_lock:
                if channel_id in self.channels:
                    self.channels[channel_id].reset(self.base_messages)
            await msg.channel.send("Conversation cleared. What's next?")

        elif cmd == "/stats":
            with self._channels_lock:
                sess = self.channels.get(channel_id)
                if sess:
                    await msg.channel.send(
                        f"**Session stats:** {sess.message_count} messages in this channel"
                    )
                else:
                    await msg.channel.send("No active session in this channel.")

        elif cmd.startswith("/search"):
            # /search <keyword> -- scan channel history across the server
            term = content[7:].strip()
            if not term:
                await msg.channel.send("Usage: `/search <keyword>` — scans server history")
                return
            await self._search_server(msg, term)

        elif cmd == "/join":
            if self.voice is None:
                await msg.channel.send("Voice support not configured for this bot.")
            else:
                result = await self.voice.join(msg)
                await msg.channel.send(result)

        elif cmd == "/leave":
            if self.voice is None:
                await msg.channel.send("Voice support not configured for this bot.")
            elif msg.guild is None:
                await msg.channel.send("Voice commands only work in servers.")
            else:
                result = await self.voice.leave(msg.guild.id)
                await msg.channel.send(result)

        elif cmd.startswith("/say"):
            if self.voice is None:
                await msg.channel.send("Voice support not configured for this bot.")
            elif msg.guild is None:
                await msg.channel.send("Voice commands only work in servers.")
            else:
                text = content[4:].strip()
                if not text:
                    await msg.channel.send("Usage: `/say <text>` — speak in voice channel")
                else:
                    result = await self.voice.say(msg.guild.id, text)
                    if result:
                        await msg.channel.send(result)  # error message

        elif cmd == "/help":
            parts = [
                "**mini_agent Discord bot**",
                "Mention me with a question about the project.",
                "",
                "**Commands:**",
                "`/clear` — Reset this channel's conversation",
                "`/stats` — Show session stats",
                "`/search <keyword>` — Search server message history",
            ]
            if self.voice is not None:
                parts += [
                    "`/join` — Join your voice channel",
                    "`/leave` — Leave the voice channel",
                    "`/say <text>` — Speak in voice channel (TTS)",
                ]
            parts.append("`/help` — This message")
            await msg.channel.send("\n".join(parts))

        else:
            await msg.channel.send(f"Unknown command: `{cmd}`. Try `/help`.")

    # ------------------------------------------------------------------
    # Agent turn (runs in a thread)
    # ------------------------------------------------------------------

    def _run_agent_turn(self, sess: ChannelSession, user_message: str) -> str:
        """Execute one agent turn synchronously (called from a thread)."""
        # Safety: re-inject Discord context in case contextvars didn't propagate
        from tools.context import set_context
        guild_id = getattr(self, "_current_guild_id", None)
        if guild_id:
            set_context(discord_guild_id=guild_id, discord_token=self.http.token)

        with sess._lock:
            sess._processing = True
            sess._cancel_event.clear()

            try:
                sess.messages.append({"role": "user", "content": user_message})

                # Save/restore the stream setting -- Discord doesn't need
                # streaming tokens, so we can turn it off for cleaner logs.
                was_stream = self.config.stream
                self.config.stream = False

                try:
                    msg = run_agent_turn(
                        sess.messages,
                        self.config,
                        self.write_gate,
                        self.read_gate,
                        cancel_event=sess._cancel_event,
                        session=None,  # thread-safe: uses requests module directly
                    )
                finally:
                    self.config.stream = was_stream

                if sess._cancel_event.is_set():
                    return "_cancelled_"

                if msg is None:
                    return "(Agent returned no message.)"

                # Persist messages
                sess.messages = self.memory.save(sess.messages)

                return msg.get("content", "") or "(empty response)"

            except Exception:
                import traceback
                traceback.print_exc()
                return f"**Error during agent turn:**\n```\n{traceback.format_exc()[:1500]}\n```"
            finally:
                sess._processing = False

    # ------------------------------------------------------------------
    # Build recent-history context for the agent
    # ------------------------------------------------------------------

    async def _build_history_context(self, msg: discord.Message, limit: int = 15) -> str:
        """Fetch recent messages before *msg* in the same channel and format
        them as a conversation snippet for the agent.

        Excludes messages from this bot itself and the triggering message.
        Returns an empty string if no usable history is found.
        """
        try:
            recent = []
            async for hmsg in msg.channel.history(limit=limit + 1, before=msg):
                if hmsg.author.bot:
                    continue  # skip ourselves and other bots
                ts = hmsg.created_at.strftime("%H:%M")
                name = hmsg.author.display_name
                text = hmsg.content or "(attachment)"
                # Trim long messages
                if len(text) > 300:
                    text = text[:297] + "..."
                recent.append(f"[{ts}] {name}: {text}")
            if not recent:
                return ""
            recent.reverse()
            header = f"[Recent conversation in #{msg.channel.name}]\n"
            return header + "\n".join(recent) + "\n\n"
        except Exception:
            return ""  # silently degrade if history fetch fails

    # ------------------------------------------------------------------
    # Server search
    # ------------------------------------------------------------------

    async def _search_server(self, msg: discord.Message, term: str) -> None:
        """Search recent message history across all accessible channels."""
        await msg.channel.send(f'🔍 Searching server history for **"{term}"**...')

        results: list[tuple[str, str, str, str, str]] = []  # channel, author, ts, content, jump_url
        term_lower = term.lower()

        for channel in msg.guild.text_channels:
            perms = channel.permissions_for(msg.guild.me)
            if not perms.read_message_history:
                continue

            try:
                async for hist_msg in channel.history(limit=200):
                    if term_lower in hist_msg.content.lower():
                        ts = hist_msg.created_at.strftime("%Y-%m-%d %H:%M")
                        preview = hist_msg.content[:300].replace("\n", " ")
                        results.append((
                            channel.name,
                            hist_msg.author.display_name,
                            ts,
                            preview,
                            hist_msg.jump_url,
                        ))
                    if len(results) >= 25:
                        break
            except discord.Forbidden:
                continue
            if len(results) >= 25:
                break

        if not results:
            await msg.channel.send(f'No messages found containing **"{term}"**.')
            return

        out = [f'**{len(results)} match(es) for "{term}":**\n']
        for ch, author, ts, preview, url in results:
            out.append(f"**#{ch}** — {author} ({ts})")
            out.append(f">>> {preview}")
            out.append(f"🔗 {url}\n")

        response_text = "\n".join(out)
        for chunk in self._chunk_text(response_text, DISCORD_MAX_MSG):
            await msg.channel.send(chunk)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mentions_me(self, msg: discord.Message) -> bool:
        """Check if the message directly mentions our bot user."""
        if self.user is None:
            return False
        # Direct @mention
        if self.user in msg.mentions:
            return True
        # Role mentions that include us (unlikely for a bot but safe)
        for role in msg.role_mentions:
            if role in getattr(self.user, "roles", []):
                return True
        return False

    def _strip_mentions(self, msg: discord.Message) -> str:
        """Remove @mentions from message content, leaving the actual query."""
        content = msg.content
        if self.user and f"<@{self.user.id}>" in content:
            content = content.replace(f"<@{self.user.id}>", "").strip()
        if self.user and f"<@!{self.user.id}>" in content:
            content = content.replace(f"<@!{self.user.id}>", "").strip()
        return content

    @staticmethod
    def _chunk_text(text: str, max_len: int) -> list[str]:
        """Split text into max_len-sized chunks, breaking at newlines."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while len(text) > max_len:
            # Try to break at a newline
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind(" ", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n ")
        if text:
            chunks.append(text)
        return chunks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # --- Resolve workspace -------------------------------------------------
    workspace = os.environ.get(ENV_AGENT_WORKSPACE, "")
    if not workspace:
        # Default to emotion_game_unreal if available
        default = os.path.expanduser("~/Desktop/emotion_game_unreal")
        if os.path.isdir(default):
            workspace = default
        else:
            print("[discord_bot] FATAL: No workspace set. Use AGENT_WORKSPACE env var.")
            sys.exit(1)

    print(f"[discord_bot] Initializing agent session for: {workspace}")

    # --- Bootstrap the agent session (shared across channels) --------------
    # Set frontend so the system prompt knows it's Discord
    os.environ["MINI_AGENT_UI"] = "discord"

    session_data = init_session(workspace)
    config: AgentConfig = session_data["config"]
    write_gate = session_data["write_gate"]
    read_gate = session_data["read_gate"]
    memory = session_data["memory"]
    base_messages: list[dict] = session_data["messages"]
    # http_session is intentionally not used -- requests.Session is not
    # thread-safe for concurrent use across Discord channels.

    # We don't need the Electron sub-agent callback; clear it to avoid
    # accidental Electron JSON-lines noise on stdout.
    try:
        from tools import _TOOL_CONTEXT
        _TOOL_CONTEXT._subagent_callback = None
    except Exception:
        pass

    print(f"[discord_bot] Agent initialized (model={config.model}, "
          f"provider={config.api_provider})")

    # --- Resolve Discord token ---------------------------------------------
    # init_session() already loaded the workspace .env file via
    # AgentConfig.load() -> _load_dotenv().  So DISCORD_BOT_TOKEN is
    # available if it was placed in emotion_game_unreal/.env.
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("[discord_bot] FATAL: DISCORD_BOT_TOKEN not set.")
        print("[discord_bot] Add it to the workspace .env file:")
        print(f"[discord_bot]   echo 'DISCORD_BOT_TOKEN=...' >> {workspace}/.env")
        print("[discord_bot] Or export it: export DISCORD_BOT_TOKEN=...")
        sys.exit(1)

    # --- Start the bot -----------------------------------------------------
    # Voice handler: use ElevenLabs if key is available
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "")
    voice = VoiceHandler(elevenlabs_api_key=elevenlabs_key)
    if elevenlabs_key:
        print(f"[discord_bot] Voice TTS: ElevenLabs")
    else:
        print(f"[discord_bot] Voice TTS: macOS say (no ELEVENLABS_API_KEY set)")

    bot = MiniAgentDiscordBot(
        workspace=workspace,
        config=config,
        write_gate=write_gate,
        read_gate=read_gate,
        memory=memory,
        base_messages=base_messages,
        voice=voice,
    )

    try:
        bot.run(token)
    except discord.LoginFailure:
        print("[discord_bot] FATAL: Invalid Discord bot token.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[discord_bot] Shutting down...")
    finally:
        # Clean up
        try:
            memory.close()
        except Exception:
            pass
        print("[discord_bot] Done.")


if __name__ == "__main__":
    main()
