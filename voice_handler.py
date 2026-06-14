"""
voice_handler.py -- Shared voice-channel + TTS support for Discord bots.

Provides join, leave, and text-to-speech for any bot that imports it.
Uses ElevenLabs for TTS (high-quality voices).  Falls back to macOS `say`
when ElevenLabs is not configured.

Usage inside a discord.Client subclass::

    from voice_handler import VoiceHandler

    class MyBot(discord.Client):
        def __init__(self):
            self.voice = VoiceHandler(elevenlabs_api_key="...")

        async def on_message(self, msg):
            if msg.content == "/join":
                result = await self.voice.join(msg)
                await msg.channel.send(result)
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import discord


# ---------------------------------------------------------------------------
# TTS providers
# ---------------------------------------------------------------------------

@dataclass
class TTSResult:
    """Audio data returned by a TTS engine."""
    data: bytes
    mime_type: str = "audio/mpeg"


class ElevenLabsTTS:
    """Text-to-speech via the ElevenLabs API."""

    # Well-known voices (IDs change, so we use names and resolve)
    DEFAULT_VOICE = "Rachel"  # warm, natural female voice

    def __init__(self, api_key: str, voice_name: str = "") -> None:
        self._api_key = api_key
        self._voice_name = voice_name or self.DEFAULT_VOICE
        self._voice_id: Optional[str] = None

    async def speak(self, text: str) -> TTSResult:
        """Generate audio bytes for *text*.  Runs in a thread (API call)."""
        return await asyncio.to_thread(self._speak_sync, text)

    def _speak_sync(self, text: str) -> TTSResult:
        from elevenlabs import ElevenLabs

        client = ElevenLabs(api_key=self._api_key)

        # Resolve voice ID lazily
        if self._voice_id is None:
            voices = client.voices.get_all()
            for v in voices.voices:
                if v.name and v.name.lower() == self._voice_name.lower():
                    self._voice_id = v.voice_id
                    break
            if self._voice_id is None:
                # Fall back to first available voice
                if voices.voices:
                    self._voice_id = voices.voices[0].voice_id
                    self._voice_name = voices.voices[0].name or "unknown"
                else:
                    raise RuntimeError("No ElevenLabs voices available")

        # Generate audio (the SDK returns an iterator of bytes)
        audio_iter = client.text_to_speech.convert(
            voice_id=self._voice_id,
            text=text,
            model_id="eleven_flash_v2_5",  # fast, low-latency model
        )

        # Collect chunks
        chunks: list[bytes] = []
        for chunk in audio_iter:
            if chunk:
                chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))

        return TTSResult(data=b"".join(chunks), mime_type="audio/mpeg")


class MacSayTTS:
    """Fallback TTS using macOS built-in ``say`` command."""

    async def speak(self, text: str) -> TTSResult:
        return await asyncio.to_thread(self._speak_sync, text)

    def _speak_sync(self, text: str) -> TTSResult:
        # Use macOS `say` to generate an aiff file, then convert to mp3 via
        # afplay doesn't give us bytes, so we pipe through ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as aiff:
            aiff_path = aiff.name
        try:
            subprocess.run(
                ["say", "-o", aiff_path, "--data-format", "LEI16@22050", text],
                check=True,
                capture_output=True,
            )
            # Convert aiff -> mp3 via ffmpeg
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", aiff_path,
                    "-f", "mp3", "-b:a", "64k", "pipe:1",
                ],
                check=True,
                capture_output=True,
            )
            return TTSResult(data=result.stdout, mime_type="audio/mpeg")
        finally:
            try:
                os.unlink(aiff_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------

# Maximum chars to speak in one go (Discord voice has practical limits)
MAX_SPEAK_CHARS = 400


class VoiceHandler:
    """Manages voice connections and TTS for a Discord bot."""

    def __init__(
        self,
        elevenlabs_api_key: str = "",
        elevenlabs_voice: str = "",
    ) -> None:
        # guild_id -> discord.VoiceClient
        self._clients: dict[int, discord.VoiceClient] = {}

        if elevenlabs_api_key:
            self._tts = ElevenLabsTTS(elevenlabs_api_key, elevenlabs_voice)
            self._tts_name = "ElevenLabs"
        else:
            self._tts = MacSayTTS()
            self._tts_name = "macOS say"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def join(self, msg: discord.Message) -> str:
        """Join the voice channel that *msg.author* is currently in.

        Returns a human-readable status string.
        """
        guild = msg.guild
        if guild is None:
            return "Voice commands only work in servers, not DMs."

        author = msg.author
        if author.voice is None or author.voice.channel is None:
            return f"{author.display_name}, you're not in a voice channel."

        channel = author.voice.channel

        # Check permissions
        perms = channel.permissions_for(guild.me)
        if not perms.connect or not perms.speak:
            return (
                f"I don't have permission to join/speak in **{channel.name}**.\n"
                "Please grant me `Connect` and `Speak` permissions."
            )

        # Already connected in this guild?
        existing = self._clients.get(guild.id)
        if existing and existing.is_connected():
            if existing.channel.id == channel.id:
                return f"I'm already in **{channel.name}**."
            # Move to new channel
            await existing.move_to(channel)
            return f"Moved to **{channel.name}**."

        # Disconnect any stale client
        if existing:
            await self._disconnect(guild.id)

        # Connect
        try:
            vc = await channel.connect()
            self._clients[guild.id] = vc
            return f"Joined **{channel.name}** (TTS: {self._tts_name})."
        except discord.ClientException as e:
            return f"Failed to join: {e}"

    async def leave(self, guild_id: int) -> str:
        """Leave the voice channel in *guild_id*.

        Returns a human-readable status string.
        """
        vc = self._clients.get(guild_id)
        if vc is None or not vc.is_connected():
            return "I'm not in a voice channel right now."

        channel_name = vc.channel.name if vc.channel else "voice"
        await self._disconnect(guild_id)
        return f"Left **{channel_name}**."

    async def say(self, guild_id: int, text: str) -> str:
        """Speak *text* in the voice channel for *guild_id*.

        Returns a status string (or empty string on success).
        """
        vc = self._clients.get(guild_id)
        if vc is None or not vc.is_connected():
            return "I'm not in a voice channel. Use `/join` first."

        if not text.strip():
            return "Nothing to say."

        # Trim very long text
        if len(text) > MAX_SPEAK_CHARS:
            text = text[: MAX_SPEAK_CHARS - 3] + "..."

        try:
            tts_result = await self._tts.speak(text)
        except Exception as e:
            return f"TTS failed: {e}"

        # Write audio to a temp file (FFmpegPCMAudio needs a file path)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(tts_result.data)
            tmp_path = tmp.name

        # Play
        try:
            self._play_file(vc, tmp_path)
            return ""  # success
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return f"Playback failed: {e}"

    def is_in_voice(self, guild_id: int) -> bool:
        """Return True if the bot is currently in a voice channel in this guild."""
        vc = self._clients.get(guild_id)
        return vc is not None and vc.is_connected()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _disconnect(self, guild_id: int) -> None:
        """Disconnect and clean up."""
        vc = self._clients.pop(guild_id, None)
        if vc:
            try:
                await vc.disconnect()
            except Exception:
                pass

    def _play_file(self, vc: discord.VoiceClient, path: str) -> None:
        """Play an audio file, cleaning it up when done."""
        # Stop any current playback
        if vc.is_playing():
            vc.stop()

        source = discord.FFmpegPCMAudio(
            path,
            before_options="-nostdin",
            options="-vn",
        )

        def _after(error: Optional[Exception]) -> None:
            try:
                os.unlink(path)
            except OSError:
                pass
            if error:
                print(f"[voice_handler] Playback error: {error}")

        vc.play(source, after=_after)
