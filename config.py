#!/usr/bin/env python3
"""
config.py — project-level configuration for mini_agent.

Looks for ``.mini_agent.toml`` in the workspace root and merges settings
with env vars and CLI flags.  Priority: CLI > env var > config file > default.
"""
from __future__ import annotations

import os
import platform
import subprocess as _sp
import sys
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILENAME = ".mini_agent.toml"
MEMORY_FILENAME = ".mini_agent_memory.db"

DEFAULT_API_PROVIDER = "deepseek"  # "deepseek", "claude", "xai", or "ollama"

# ---------------------------------------------------------------------------
# Provider defaults registry
# ---------------------------------------------------------------------------
# Each provider has a ProviderDefaults entry.  The registry is the single
# source of truth for provider-specific defaults.  Legacy module-level
# constants (DEEPSEEK_DEFAULT_MODEL, etc.) are kept as aliases for
# backward compatibility but new code should use PROVIDER_DEFAULTS[provider].

@dataclass
class ProviderDefaults:
    """Default values for one LLM provider."""
    model: str
    sub_agent_model: str
    api_url: str
    max_tokens: int
    context_window: int
    routing_model: str = ""  # cheaper model for read/search prompts; "" = disabled

PROVIDER_DEFAULTS: dict[str, ProviderDefaults] = {
    "deepseek": ProviderDefaults(
        model="deepseek-v4-pro",
        sub_agent_model="deepseek-v4-flash",  # cheaper worker model for sub-agents
        api_url="https://api.deepseek.com/v1/chat/completions",
        max_tokens=200_000,
        context_window=1_000_000,  # V4-Pro native context length
        routing_model="deepseek-v4-flash",  # also use flash for simple read/search prompts
    ),
    "claude": ProviderDefaults(
        model="claude-sonnet-4-5",
        sub_agent_model="claude-sonnet-4-5",  # Anthropic: same model for sub-agents (no cheaper tier via API)
        api_url="https://api.anthropic.com/v1/chat/completions",
        max_tokens=32_000,
        context_window=1_000_000,  # Opus 4.7 / Sonnet 4.5 native context length
        routing_model="",
    ),
    "xai": ProviderDefaults(
        model="grok-4.3",
        sub_agent_model="grok-4.3",  # xAI: same model for sub-agents
        api_url="https://api.x.ai/v1/chat/completions",
        max_tokens=200_000,
        context_window=1_000_000,  # Grok 4.3 native context length
        routing_model="",
    ),
    "ollama": ProviderDefaults(
        model="qwen3.6:27b",
        sub_agent_model="qwen3.6:27b",  # local LLM: no cheaper variant installed, use same model
        # Camoproj VM: RTX 6000 Ada 48GB, accessible via Tailscale at 100.79.96.42
        api_url="http://100.79.96.42:11434/v1/chat/completions",
        max_tokens=8_192,
        context_window=65_536,  # qwen3.6: capped at 64K to fit KV cache in 48GB VRAM
        routing_model="",
    ),
}

# Legacy module-level aliases (kept for backward compatibility with tests
# and external consumers that import e.g. DEEPSEEK_DEFAULT_MODEL directly).
DEEPSEEK_DEFAULT_MODEL         = PROVIDER_DEFAULTS["deepseek"].model
DEEPSEEK_DEFAULT_SUB_AGENT_MODEL = PROVIDER_DEFAULTS["deepseek"].sub_agent_model
DEEPSEEK_DEFAULT_API_URL       = PROVIDER_DEFAULTS["deepseek"].api_url
DEEPSEEK_DEFAULT_MAX_TOKENS    = PROVIDER_DEFAULTS["deepseek"].max_tokens
DEEPSEEK_DEFAULT_CONTEXT_WINDOW = PROVIDER_DEFAULTS["deepseek"].context_window
DEEPSEEK_DEFAULT_ROUTING_MODEL = PROVIDER_DEFAULTS["deepseek"].routing_model

CLAUDE_DEFAULT_MODEL           = PROVIDER_DEFAULTS["claude"].model
CLAUDE_DEFAULT_SUB_AGENT_MODEL = PROVIDER_DEFAULTS["claude"].sub_agent_model
CLAUDE_DEFAULT_API_URL         = PROVIDER_DEFAULTS["claude"].api_url
CLAUDE_DEFAULT_MAX_TOKENS      = PROVIDER_DEFAULTS["claude"].max_tokens
CLAUDE_DEFAULT_CONTEXT_WINDOW  = PROVIDER_DEFAULTS["claude"].context_window
CLAUDE_DEFAULT_ROUTING_MODEL   = PROVIDER_DEFAULTS["claude"].routing_model

XAI_DEFAULT_MODEL              = PROVIDER_DEFAULTS["xai"].model
XAI_DEFAULT_SUB_AGENT_MODEL    = PROVIDER_DEFAULTS["xai"].sub_agent_model
XAI_DEFAULT_API_URL            = PROVIDER_DEFAULTS["xai"].api_url
XAI_DEFAULT_MAX_TOKENS         = PROVIDER_DEFAULTS["xai"].max_tokens
XAI_DEFAULT_CONTEXT_WINDOW     = PROVIDER_DEFAULTS["xai"].context_window
XAI_DEFAULT_ROUTING_MODEL      = PROVIDER_DEFAULTS["xai"].routing_model

OLLAMA_DEFAULT_MODEL           = PROVIDER_DEFAULTS["ollama"].model
OLLAMA_DEFAULT_SUB_AGENT_MODEL = PROVIDER_DEFAULTS["ollama"].sub_agent_model
OLLAMA_DEFAULT_API_URL         = PROVIDER_DEFAULTS["ollama"].api_url
OLLAMA_DEFAULT_MAX_TOKENS      = PROVIDER_DEFAULTS["ollama"].max_tokens
OLLAMA_DEFAULT_CONTEXT_WINDOW  = PROVIDER_DEFAULTS["ollama"].context_window
OLLAMA_DEFAULT_ROUTING_MODEL   = PROVIDER_DEFAULTS["ollama"].routing_model

DEFAULT_MODEL        = DEEPSEEK_DEFAULT_MODEL
DEFAULT_SUB_AGENT_MODEL = DEEPSEEK_DEFAULT_SUB_AGENT_MODEL
DEFAULT_SUB_AGENT_MAX_CONCURRENT = 10
DEFAULT_API_URL      = DEEPSEEK_DEFAULT_API_URL
DEFAULT_API_KEY      = ""  # set via DEEPSEEK_API_KEY/CLAUDE_API_KEY env var, .env file, or .mini_agent.toml
DEFAULT_MAX_MESSAGES = 50
DEFAULT_MAX_TOKENS   = DEEPSEEK_DEFAULT_MAX_TOKENS
DEFAULT_CONTEXT_WINDOW = DEEPSEEK_DEFAULT_CONTEXT_WINDOW
DEFAULT_SUB_AGENT_MAX_TURNS = 25
DEFAULT_ROUTING_MODEL = ""  # disabled by default; set to provider-specific cheap model
DEFAULT_EXA_API_KEY = ""  # set via EXA_API_KEY env var or .mini_agent.toml
DEFAULT_OPENAI_API_KEY = ""  # set via OPENAI_API_KEY env var or .mini_agent.toml

# Windows SOCKS tunnel (auto-started on Windows to route all LLM traffic)
_WINDOWS_TUNNEL_HOST   = "172.31.2.42"
_WINDOWS_TUNNEL_PORT   = 1080
_WINDOWS_TUNNEL_USER   = "gabriel"
_WINDOWS_TUNNEL_KEY    = "gabekey"  # relative to $HOME
SOCKS_PROXY_URL        = f"socks5://localhost:{_WINDOWS_TUNNEL_PORT}"

# Truncation / timeout / connection-pool constants
TREE_TRUNCATION_LINES   = 60   # max lines in workspace tree before truncating
GIT_LOG_TIMEOUT         = 5    # seconds to wait for git log
GIT_LOG_COUNT            = 5    # number of recent commits to show on startup
HTTP_CONNECT_TIMEOUT    = 30   # seconds to establish HTTP connection
HTTP_READ_TIMEOUT       = 120  # seconds to read HTTP response
HTTP_POOL_CONNECTIONS   = 2    # max connections per host
HTTP_POOL_MAXSIZE       = 4    # max total pool size

# Environment variable names used during config loading
ENV_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
ENV_CLAUDE_API_KEY   = "CLAUDE_API_KEY"
ENV_XAI_API_KEY      = "XAI_API_KEY"
ENV_SUB_AGENT_API_KEY = "SUB_AGENT_API_KEY"
ENV_DEEPSEEK_API_URL = "DEEPSEEK_API_URL"
ENV_CLAUDE_API_URL   = "CLAUDE_API_URL"
ENV_XAI_API_URL      = "XAI_API_URL"
ENV_CLAUDE_MODEL     = "CLAUDE_MODEL"
ENV_XAI_MODEL        = "XAI_MODEL"
ENV_OLLAMA_MODEL     = "OLLAMA_MODEL"
ENV_OLLAMA_API_URL   = "OLLAMA_API_URL"
ENV_OLLAMA_API_KEY   = "OLLAMA_API_KEY"
ENV_API_PROVIDER     = "API_PROVIDER"  # "deepseek", "claude", "xai", or "ollama" — overrides auto-detection
ENV_AGENT_WORKSPACE  = "AGENT_WORKSPACE"
ENV_EXA_API_KEY      = "EXA_API_KEY"
ENV_OPENAI_API_KEY   = "OPENAI_API_KEY"

# CLI flag strings (matched against sys.argv or argparse namespace)
CLI_STREAM           = "--stream"
CLI_QUIET            = "--quiet"
CLI_ALLOW_OVERWRITES = "--allow-overwrites"
CLI_APPROVE          = "--approve"
CLI_UNRESTRICTED     = "--unrestricted"


# ---------------------------------------------------------------------------
# Config object
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """All configuration for a single agent session.

    Fields are populated in priority order:
    1. CLI flags (--workspace, --stream, --quiet)
    2. Environment variables (DEEPSEEK_API_KEY, AGENT_WORKSPACE)
    3. .mini_agent.toml file in workspace root
    4. Hard-coded defaults
    """

    api_provider: str = DEFAULT_API_PROVIDER  # "deepseek" or "claude"
    model: str = DEFAULT_MODEL
    sub_agent_model: str = DEFAULT_SUB_AGENT_MODEL
    sub_agent_api_key: str = DEFAULT_API_KEY  # separate key for sub-agents
    sub_agent_max_concurrent: int = DEFAULT_SUB_AGENT_MAX_CONCURRENT
    api_key: str = DEFAULT_API_KEY
    api_url: str = DEFAULT_API_URL
    workspace: str = ""
    allow_overwrites: bool = False
    stream: bool = False
    verbose: bool = True
    memory_filename: str = MEMORY_FILENAME
    max_messages: int = DEFAULT_MAX_MESSAGES
    max_tokens: int = DEFAULT_MAX_TOKENS
    context_window: int = DEFAULT_CONTEXT_WINDOW  # memory budget for pruning; defaults to provider context window
    sub_agent_max_turns: int = DEFAULT_SUB_AGENT_MAX_TURNS
    routing_model: str = DEFAULT_ROUTING_MODEL  # cheaper model for simple read/search prompts; "" = disabled
    temperature: float = 0.0
    frequency_penalty: float = 0.3
    presence_penalty: float = 0.1
    stop_sequences: list[str] = field(default_factory=list)
    response_format: str = ""  # "" = default, "json_object" for JSON mode
    exa_api_key: str = DEFAULT_EXA_API_KEY
    openai_api_key: str = DEFAULT_OPENAI_API_KEY
    approve_write_ops: bool = False
    unrestricted: bool = False
    frontend: str = "terminal"  # "terminal", "electron" — injected into system prompt
    socks_proxy: str = ""  # SOCKS5 proxy URL (auto-set on Windows for SSH tunnel)
    mcp_servers: dict = field(default_factory=dict)  # {name: {command: [...], env: {...}}}

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, workspace: str, cli_args: object | None = None) -> "AgentConfig":
        """Build an AgentConfig from all sources.

        *workspace* is the already-resolved workspace root (from
        ``--workspace`` flag, ``AGENT_WORKSPACE`` env var, or cwd).

        *cli_args* is an optional argparse namespace from ``parse_args()``.
        When provided, its values take precedence over ``sys.argv`` checks.
        """
        config = cls()

        # Phase 1: TOML config file
        _load_toml_from_workspace(config, workspace)
        # Phase 1.5: .env file (loads into os.environ, skips keys already set)
        _load_dotenv(workspace)
        # Phase 2: environment variable overrides
        _apply_env_overrides(config)
        # Phase 3: CLI flag overrides (highest priority)
        _apply_cli_overrides(config, cli_args)
        # --workspace is resolved before we get here; store it
        config.workspace = workspace
        # Phase 4: Windows SOCKS tunnel auto-start (no-op on other platforms)
        _start_windows_tunnel(config)

        return config


def _start_windows_tunnel(config: AgentConfig) -> None:
    """If running on Windows, launch an SSH SOCKS tunnel in the background.

    Uses ``ssh -i %HOME%\\gabekey -D 1080 gabriel@172.31.2.42`` to create
    a SOCKS5 proxy on localhost:1080.  All subsequent LLM API traffic is
    routed through this tunnel.
    """
    if platform.system() != "Windows":
        return  # no-op on macOS / Linux

    key_path = os.path.join(os.path.expanduser("~"), _WINDOWS_TUNNEL_KEY)
    if not os.path.isfile(key_path):
        print(
            f"Warning: Windows tunnel key not found at {key_path} — "
            "skipping SOCKS tunnel",
            file=sys.stderr,
        )
        return

    cmd = [
        "ssh",
        "-i", key_path,
        "-D", str(_WINDOWS_TUNNEL_PORT),
        "-L", "11434:localhost:11434",  # forward Ollama API port
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=60",
        "-N",  # no shell, just tunnel
        f"{_WINDOWS_TUNNEL_USER}@{_WINDOWS_TUNNEL_HOST}",
    ]

    try:
        _sp.Popen(
            cmd,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            stdin=_sp.DEVNULL,
        )
        config.socks_proxy = SOCKS_PROXY_URL
        print(
            f"  \u2139 Windows SOCKS tunnel started: {SOCKS_PROXY_URL} "
            f"-> {_WINDOWS_TUNNEL_USER}@{_WINDOWS_TUNNEL_HOST}",
            file=sys.stderr,
            flush=True,
        )
    except OSError as exc:
        print(
            f"Warning: failed to start Windows tunnel: {exc}",
            file=sys.stderr,
        )


# Class-level TOML parse cache: maps (workspace, CONFIG_FILENAME) to the
# parsed agent data dict so repeated calls to load() don't re-parse the file.
#
# Set after the class body (not inside) because it's a mutable default that must
# be shared across *all* AgentConfig instances.  Defining ``_toml_cache = {}``
# inside the class body would create a single class attribute, which works, but
# this out-of-line assignment makes the shared-mutable-default property explicit
# and avoids any risk of dataclass field machinery interfering with it.
AgentConfig._toml_cache = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Keys recognised in TOML and their expected types
_TOML_SCHEMA: dict[str, type] = {
    "api_provider": str,
    "model": str,
    "sub_agent_model": str,
    "sub_agent_api_key": str,
    "sub_agent_max_concurrent": int,
    "api_key": str,
    "api_url": str,
    "allow_overwrites": bool,
    "stream": bool,
    "verbose": bool,
    "max_messages": int,
    "max_tokens": int,
    "sub_agent_max_turns": int,
    "routing_model": str,
    "temperature": float,
    "frequency_penalty": float,
    "presence_penalty": float,
    "exa_api_key": str,
    "openai_api_key": str,
    "approve_write_ops": bool,
    "unrestricted": bool,
    "mcp_servers": dict,
}


def _apply_toml(config: AgentConfig, data: dict) -> None:
    """Apply recognised keys from TOML data onto *config* with type checking.

    Unknown keys are skipped.  Values with wrong types are warned and skipped.
    """
    for key, value in data.items():
        if key not in _TOML_SCHEMA:
            continue
        expected = _TOML_SCHEMA[key]

        if not isinstance(value, expected):
            print(
                f"Warning: .mini_agent.toml key '{key}' expected {expected.__name__}, "
                f"got {type(value).__name__} — skipping",
                file=sys.stderr,
            )
            continue
        setattr(config, key, value)


def _load_toml_from_workspace(config: AgentConfig, workspace: str) -> None:
    """Phase 1: load agent settings from ``.mini_agent.toml`` in *workspace*.

    Uses a class-level cache keyed by ``(workspace, CONFIG_FILENAME)`` so
    repeated calls to ``load()`` do not re-parse the same file.
    """
    cache_key = (workspace, CONFIG_FILENAME)
    if cache_key in AgentConfig._toml_cache:
        agent_data = AgentConfig._toml_cache[cache_key]
        _apply_toml(config, agent_data)
        return

    config_path = os.path.join(workspace, CONFIG_FILENAME)
    if not os.path.isfile(config_path):
        return

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        agent_data = data.get("agent", {})
        AgentConfig._toml_cache[cache_key] = agent_data
        _apply_toml(config, agent_data)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"Warning: failed to parse {config_path}: {exc}",
              file=sys.stderr)


def _load_dotenv(workspace: str) -> None:
    """Load key=value pairs from ``.env`` in *workspace* into ``os.environ``.

    Supports simple VAR=value, VAR="value", VAR='value' syntax.
    Blank lines and #-comments are ignored.
    Does NOT overwrite existing environment variables (host env wins).
    """
    env_path = os.path.join(workspace, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip inline comments (e.g. KEY="val"# comment)
                if '#' in value:
                    value = value.split('#')[0].strip()
                # Remove optional surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                # Only set if not already in environment (env vars take priority)
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


# Provider → env var name for API key lookup.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "deepseek": ENV_DEEPSEEK_API_KEY,
    "claude": ENV_CLAUDE_API_KEY,
    "xai": ENV_XAI_API_KEY,
    "ollama": ENV_OLLAMA_API_KEY,
}
# Provider → env var name for API URL override.
_PROVIDER_URL_ENV: dict[str, str] = {
    "deepseek": ENV_DEEPSEEK_API_URL,
    "claude": ENV_CLAUDE_API_URL,
    "xai": ENV_XAI_API_URL,
    "ollama": ENV_OLLAMA_API_URL,
}
# Provider → env var name for model override.
_PROVIDER_MODEL_ENV: dict[str, str] = {
    "claude": ENV_CLAUDE_MODEL,
    "xai": ENV_XAI_MODEL,
    "ollama": ENV_OLLAMA_MODEL,
}

# Auto-detection priority order (first provider with an available key wins).
_AUTO_DETECT_ORDER = ["deepseek", "claude", "xai", "ollama"]


def _detect_provider() -> str | None:
    """Return the first provider whose API key env var is set, or None."""
    for provider in _AUTO_DETECT_ORDER:
        key_env = _PROVIDER_KEY_ENV.get(provider)
        if key_env and os.environ.get(key_env):
            return provider
    # Ollama is always reachable via Tailscale (camoproj VM); fall back to it.
    return "ollama"


def _apply_env_overrides(config: AgentConfig) -> None:
    """Phase 2: apply environment variable overrides on top of TOML/defaults."""
    # --- explicit provider override ---
    if os.environ.get(ENV_API_PROVIDER):
        config.api_provider = os.environ[ENV_API_PROVIDER]

    # --- auto-detect from available keys (if not explicitly set) ---
    if not os.environ.get(ENV_API_PROVIDER):
        detected = _detect_provider()
        if detected:
            config.api_provider = detected

    # --- apply provider-specific defaults on switch ---
    # NOTE: uses equality against deepseek defaults to detect "not yet
    # overridden".  A TOML value equal to a deepseek default will be
    # overwritten — this is a known limitation (rare in practice).
    provider = config.api_provider
    defaults = PROVIDER_DEFAULTS.get(provider)
    if defaults is not None:
        deepseek = PROVIDER_DEFAULTS["deepseek"]
        # Only swap if the URL hasn't been explicitly overridden via env
        if not os.environ.get(_PROVIDER_URL_ENV.get(provider, "")):
            if config.api_url in (deepseek.api_url, DEEPSEEK_DEFAULT_API_URL):
                config.api_url = defaults.api_url
        if config.model == deepseek.model:
            config.model = defaults.model
        if config.sub_agent_model == deepseek.sub_agent_model:
            config.sub_agent_model = defaults.sub_agent_model
        if config.max_tokens == deepseek.max_tokens:
            config.max_tokens = defaults.max_tokens
        if config.context_window == deepseek.context_window:
            config.context_window = defaults.context_window
        if config.routing_model == deepseek.routing_model:
            config.routing_model = defaults.routing_model

    # --- API keys ---
    for prov, env_name in _PROVIDER_KEY_ENV.items():
        if os.environ.get(env_name):
            config.api_key = os.environ[env_name]
            break  # first available key wins

    if os.environ.get(ENV_SUB_AGENT_API_KEY):
        config.sub_agent_api_key = os.environ[ENV_SUB_AGENT_API_KEY]
    elif not os.environ.get(ENV_SUB_AGENT_API_KEY):
        # Fall back to primary key in provider priority order
        for prov in _AUTO_DETECT_ORDER:
            key_env = _PROVIDER_KEY_ENV.get(prov)
            if key_env and os.environ.get(key_env):
                config.sub_agent_api_key = os.environ[key_env]
                break

    # --- API URL overrides ---
    for _prov, env_name in _PROVIDER_URL_ENV.items():
        if os.environ.get(env_name):
            config.api_url = os.environ[env_name]

    # --- model override ---
    for _prov, env_name in _PROVIDER_MODEL_ENV.items():
        if os.environ.get(env_name):
            config.model = os.environ[env_name]
            config.sub_agent_model = os.environ[env_name]

    # --- workspace ---
    if os.environ.get(ENV_AGENT_WORKSPACE):
        config.workspace = os.environ[ENV_AGENT_WORKSPACE]

    # --- frontend / UI mode ---
    if os.environ.get("MINI_AGENT_UI"):
        config.frontend = os.environ["MINI_AGENT_UI"]

    # --- third-party keys ---
    if os.environ.get(ENV_EXA_API_KEY):
        config.exa_api_key = os.environ[ENV_EXA_API_KEY]
    if os.environ.get(ENV_OPENAI_API_KEY):
        config.openai_api_key = os.environ[ENV_OPENAI_API_KEY]


def _apply_cli_overrides(config: AgentConfig,
                         cli_args: object | None) -> None:
    """Phase 3: apply CLI flag overrides (highest priority).

    When *cli_args* is an argparse namespace, its attributes are used
    directly.  Otherwise ``sys.argv`` is scanned for known flags.
    """
    if cli_args is not None:
        if cli_args.stream is not None:
            config.stream = cli_args.stream
        if cli_args.quiet is not None:
            config.verbose = not cli_args.quiet
        if cli_args.allow_overwrites is not None:
            config.allow_overwrites = cli_args.allow_overwrites
        if cli_args.approve is not None:
            config.approve_write_ops = cli_args.approve
        if cli_args.unrestricted is not None:
            config.unrestricted = cli_args.unrestricted
        return

    _argv = sys.argv[1:]  # skip program name
    if any(a == CLI_STREAM for a in _argv):
        config.stream = True
    if any(a == CLI_QUIET for a in _argv):
        config.verbose = False
    if any(a == CLI_ALLOW_OVERWRITES for a in _argv):
        config.allow_overwrites = True
    if any(a == CLI_APPROVE for a in _argv):
        config.approve_write_ops = True
    if any(a == CLI_UNRESTRICTED for a in _argv):
        config.unrestricted = True

def parse_args(argv: list[str] | None = None) -> object:
    """Parse CLI flags with argparse.

    Returns a namespace with: workspace, stream, quiet, allow_overwrites,
    approve, no_color.
    All attributes default to None so callers can distinguish "not passed"
    from "passed with False". Workspace defaults to the env var or cwd.
    """
    import argparse as _ap
    parser = _ap.ArgumentParser(
        prog="mini_agent",
        description="A coding agent powered by DeepSeek V4 Pro.",
    )
    parser.add_argument(
        "--workspace", default=None,
        help="Workspace root directory (env: AGENT_WORKSPACE, default: cwd)",
    )
    parser.add_argument(
        "--stream", action="store_true", default=None,
        help="Stream responses token-by-token (default: off)",
    )
    parser.add_argument(
        "--quiet", action="store_true", default=None,
        help="Suppress tool execution logs (default: off)",
    )
    parser.add_argument(
        "--allow-overwrites", action="store_true", default=None,
        help="Allow overwriting existing files without confirmation (default: off)",
    )
    parser.add_argument(
        "--approve", action="store_true", default=None,
        help="Prompt for approval before each write/destructive operation (default: off)",
    )
    parser.add_argument(
        "--no-color", action="store_true", default=None,
        help="Disable ANSI colours in output (default: off)",
    )
    parser.add_argument(
        "--unrestricted", action="store_true", default=None,
        help="Remove workspace boundary checks (allows read/write anywhere)",
    )
    # ----- UI selection (prompt_toolkit TUI by default) -----
    parser.add_argument(
        "--no-ui", action="store_true", default=None,
        help="Run the plain stdin REPL instead of the Electron UI",
    )

    parser.add_argument(
        "--theme", default=None,
        help="Initial UI theme (slate, dawn, sepia, ember, midnight, cobalt, neon, forest, dracula)",
    )
    ns, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"Warning: unknown CLI arguments ignored: {' '.join(unknown)}",
              file=sys.stderr)
    return ns


def resolve_workspace(override: str | None = None) -> str:
    """Resolve workspace root from CLI arg, env var, or default to cwd.

    *override* takes priority (from argparse).  Falls back to sys.argv,
    then AGENT_WORKSPACE env var, then cwd.
    Used by the terminal REPL and Electron backend.
    """
    if override is not None:
        return override
    import sys as _sys
    args = _sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--workspace" and i + 1 < len(args):
            return args[i + 1]
    return os.environ.get("AGENT_WORKSPACE", os.getcwd())


# ---------------------------------------------------------------------------
# Backward-compatible re-exports (at end of file to avoid circular imports)
# ---------------------------------------------------------------------------
# These functions have been moved to dedicated modules but are re-exported
# here so existing callers (mini_agent.py, eval/runner.py,
# tests) continue to work unchanged.

from bootstrap import init_session  # noqa: F401, E402
from session import (  # noqa: F401, E402
    _session_db_path,
    list_sessions,
    switch_session,
    delete_session,
)
from prompt import build_startup_context  # noqa: F401, E402
