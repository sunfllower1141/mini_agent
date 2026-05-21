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

DEFAULT_API_PROVIDER = "deepseek"  # "deepseek", "claude", or "xai"

# DeepSeek defaults
DEEPSEEK_DEFAULT_MODEL         = "deepseek-v4-pro"
DEEPSEEK_DEFAULT_SUB_AGENT_MODEL = "deepseek-v4-pro"
DEEPSEEK_DEFAULT_API_URL       = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_DEFAULT_MAX_TOKENS    = 200_000
DEEPSEEK_DEFAULT_ROUTING_MODEL = ""  # disabled by default; set to "deepseek-v4-flash" to enable

# Claude defaults (via OpenAI-compatible endpoint)
CLAUDE_DEFAULT_MODEL           = "claude-sonnet-4-5"
CLAUDE_DEFAULT_SUB_AGENT_MODEL = "claude-sonnet-4-5"
CLAUDE_DEFAULT_API_URL         = "https://api.anthropic.com/v1/chat/completions"
CLAUDE_DEFAULT_MAX_TOKENS      = 32_000
# Claude has no cheap routing model; leave disabled
CLAUDE_DEFAULT_ROUTING_MODEL   = ""

# xAI / Grok defaults (OpenAI-compatible endpoint)
XAI_DEFAULT_MODEL              = "grok-4.3"
XAI_DEFAULT_SUB_AGENT_MODEL    = "grok-4.3"
XAI_DEFAULT_API_URL            = "https://api.x.ai/v1/chat/completions"
XAI_DEFAULT_MAX_TOKENS         = 200_000
XAI_DEFAULT_ROUTING_MODEL      = ""  # xAI has no cheap routing model; leave disabled

DEFAULT_MODEL        = DEEPSEEK_DEFAULT_MODEL
DEFAULT_SUB_AGENT_MODEL = DEEPSEEK_DEFAULT_SUB_AGENT_MODEL
DEFAULT_SUB_AGENT_MAX_CONCURRENT = 10
DEFAULT_API_URL      = DEEPSEEK_DEFAULT_API_URL
DEFAULT_API_KEY      = ""  # set via DEEPSEEK_API_KEY/CLAUDE_API_KEY env var, .env file, or .mini_agent.toml
DEFAULT_MAX_MESSAGES = 50
DEFAULT_MAX_TOKENS   = DEEPSEEK_DEFAULT_MAX_TOKENS
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
ENV_API_PROVIDER     = "API_PROVIDER"  # "deepseek", "claude", or "xai" — overrides auto-detection
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
# MCP server config
# ---------------------------------------------------------------------------


@dataclass
class McpServerConfig:
    """One MCP server definition, parsed from ``[[mcp_server]]`` TOML blocks."""

    name: str                           # unique, e.g. "filesystem"
    command: str = ""                    # executable (required for stdio)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""                       # "" means inherit workspace
    enabled: bool = True


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
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    socks_proxy: str = ""  # SOCKS5 proxy URL (auto-set on Windows for SSH tunnel)

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
    "mcp_server": list,
}


def _apply_toml(config: AgentConfig, data: dict) -> None:
    """Apply recognised keys from TOML data onto *config* with type checking.

    Unknown keys are skipped.  Values with wrong types are warned and skipped.
    """
    for key, value in data.items():
        if key not in _TOML_SCHEMA:
            continue
        expected = _TOML_SCHEMA[key]

        # ``[[mcp_server]]`` TOML syntax produces a list of dicts.
        if key == "mcp_server":
            if not isinstance(value, list):
                print(
                    f"Warning: .mini_agent.toml key 'mcp_server' expected list, "
                    f"got {type(value).__name__} — skipping",
                    file=sys.stderr,
                )
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                config.mcp_servers.append(McpServerConfig(
                    name=entry.get("name", ""),
                    command=entry.get("command", ""),
                    args=entry.get("args", []),
                    env=entry.get("env", {}),
                    cwd=entry.get("cwd", ""),
                    enabled=entry.get("enabled", True),
                ))
            continue

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


def _apply_env_overrides(config: AgentConfig) -> None:
    """Phase 2: apply environment variable overrides on top of TOML/defaults."""
    # --- explicit provider override ---
    if os.environ.get(ENV_API_PROVIDER):
        config.api_provider = os.environ[ENV_API_PROVIDER]

    # --- auto-detect provider from available keys (if not explicitly set) ---
    has_deepseek = bool(os.environ.get(ENV_DEEPSEEK_API_KEY))
    has_claude = bool(os.environ.get(ENV_CLAUDE_API_KEY))
    has_xai = bool(os.environ.get(ENV_XAI_API_KEY))
    if not os.environ.get(ENV_API_PROVIDER):
        if has_xai and not has_deepseek and not has_claude:
            config.api_provider = "xai"
        elif has_claude and not has_deepseek:
            config.api_provider = "claude"
        elif has_deepseek:
            config.api_provider = "deepseek"

    # --- apply provider-specific defaults if switching ---
    if config.api_provider == "claude":
        if not os.environ.get(ENV_DEEPSEEK_API_URL) and config.api_url == DEEPSEEK_DEFAULT_API_URL:
            config.api_url = CLAUDE_DEFAULT_API_URL
        if config.model == DEEPSEEK_DEFAULT_MODEL:
            config.model = CLAUDE_DEFAULT_MODEL
        if config.sub_agent_model == DEEPSEEK_DEFAULT_SUB_AGENT_MODEL:
            config.sub_agent_model = CLAUDE_DEFAULT_SUB_AGENT_MODEL
        if config.max_tokens == DEEPSEEK_DEFAULT_MAX_TOKENS:
            config.max_tokens = CLAUDE_DEFAULT_MAX_TOKENS
        if config.routing_model == DEEPSEEK_DEFAULT_ROUTING_MODEL:
            config.routing_model = CLAUDE_DEFAULT_ROUTING_MODEL
    elif config.api_provider == "xai":
        if not os.environ.get(ENV_XAI_API_URL) and config.api_url == DEEPSEEK_DEFAULT_API_URL:
            config.api_url = XAI_DEFAULT_API_URL
        if config.model == DEEPSEEK_DEFAULT_MODEL:
            config.model = XAI_DEFAULT_MODEL
        if config.sub_agent_model == DEEPSEEK_DEFAULT_SUB_AGENT_MODEL:
            config.sub_agent_model = XAI_DEFAULT_SUB_AGENT_MODEL
        if config.max_tokens == DEEPSEEK_DEFAULT_MAX_TOKENS:
            config.max_tokens = XAI_DEFAULT_MAX_TOKENS
        if config.routing_model == DEEPSEEK_DEFAULT_ROUTING_MODEL:
            config.routing_model = XAI_DEFAULT_ROUTING_MODEL

    # --- API keys ---
    if has_deepseek:
        config.api_key = os.environ[ENV_DEEPSEEK_API_KEY]
    if has_claude:
        config.api_key = os.environ[ENV_CLAUDE_API_KEY]
    if has_xai:
        config.api_key = os.environ[ENV_XAI_API_KEY]
    if os.environ.get(ENV_SUB_AGENT_API_KEY):
        config.sub_agent_api_key = os.environ[ENV_SUB_AGENT_API_KEY]
    elif has_claude and not os.environ.get(ENV_SUB_AGENT_API_KEY):
        config.sub_agent_api_key = os.environ[ENV_CLAUDE_API_KEY]
    elif has_deepseek and not os.environ.get(ENV_SUB_AGENT_API_KEY):
        config.sub_agent_api_key = os.environ[ENV_DEEPSEEK_API_KEY]

    # --- API URL overrides ---
    if os.environ.get(ENV_DEEPSEEK_API_URL):
        config.api_url = os.environ[ENV_DEEPSEEK_API_URL]
    if os.environ.get(ENV_CLAUDE_API_URL):
        config.api_url = os.environ[ENV_CLAUDE_API_URL]
    if os.environ.get(ENV_XAI_API_URL):
        config.api_url = os.environ[ENV_XAI_API_URL]

    # --- model override ---
    if os.environ.get(ENV_CLAUDE_MODEL):
        config.model = os.environ[ENV_CLAUDE_MODEL]
        config.sub_agent_model = os.environ[ENV_CLAUDE_MODEL]
    if os.environ.get(ENV_XAI_MODEL):
        config.model = os.environ[ENV_XAI_MODEL]
        config.sub_agent_model = os.environ[ENV_XAI_MODEL]

    # --- workspace ---
    if os.environ.get(ENV_AGENT_WORKSPACE):
        config.workspace = os.environ[ENV_AGENT_WORKSPACE]

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


# TODO: build_startup_context is ~70 lines — consider splitting tree generation,
#       STATE.txt reading, and git log into separate helpers.
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

    # 4. Project knowledge (cross-session learnings, if available)
    if knowledge:
        lines = []
        # Session summary first (if present)
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


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _session_db_path(workspace: str, session_name: str | None = None) -> str:
    """Return the memory DB path for a given session name."""
    if session_name:
        base = MEMORY_FILENAME.replace(".db", "")
        return os.path.join(workspace, f"{base}_session_{session_name}.db")
    return os.path.join(workspace, MEMORY_FILENAME)


def list_sessions(workspace: str) -> list[str]:
    """Return list of available session names in the workspace."""
    sessions: list[str] = []
    prefix = MEMORY_FILENAME.replace(".db", "_session_")
    for fname in os.listdir(workspace):
        if fname.startswith(prefix) and fname.endswith(".db"):
            name = fname[len(prefix):-len(".db")]
            sessions.append(name)
    # Also check if default session DB exists
    default_path = os.path.join(workspace, MEMORY_FILENAME)
    if os.path.isfile(default_path) and "default" not in sessions:
        sessions.insert(0, "default")
    return sessions


def switch_session(
    workspace: str,
    session_name: str,
    current_memory: "MemoryStore | None",
    current_config: "AgentConfig",
) -> dict:
    """Save current session and load a new one. Returns new session dict."""
    from memory import MemoryStore
    from prompt import build_system_prompt

    # Save current session
    if current_memory is not None:
        current_memory.close()

    db_path = _session_db_path(workspace, session_name)
    memory = MemoryStore(db_path, max_messages=current_config.max_messages,
                         max_tokens=current_config.max_tokens)
    saved = memory.load()
    if saved:
        from memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned
        saved, _ = _compress_tool_results(saved, keep_recent=20)
        saved, pruned = _prune_by_tokens(saved, current_config.max_tokens, current_config.max_messages)
        if pruned:
            summary = _summarize_pruned(pruned)
            if summary:
                saved.insert(0, {"role": "user", "content": summary})

    knowledge = memory.get_top_knowledge(limit=15) if not memory._skip_load else []
    # Also inject the latest session summary for context
    session_summary = memory.get_latest_session_summary() if not memory._skip_load else None
    startup_ctx = build_startup_context(workspace, knowledge=knowledge)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(current_config)},
        {"role": "system", "content": startup_ctx},
    ]
    if saved:
        messages.extend(saved)

    return {"memory": memory, "messages": messages}


def delete_session(workspace: str, session_name: str) -> tuple[bool, str]:
    """Delete a session's memory DB. Returns (ok, message)."""
    if session_name == "default":
        return False, "Cannot delete the default session."
    db_path = _session_db_path(workspace, session_name)
    if not os.path.isfile(db_path):
        return False, f"Session '{session_name}' not found."
    os.remove(db_path)
    return True, f"Deleted session '{session_name}'."


def init_session(workspace: str, cli_args: object | None = None) -> dict:
    """Shared agent initialization used by both terminal and TUI.

    *cli_args* is an optional argparse namespace. Pass it to forward
    CLI flags to AgentConfig.load().

    Returns: config, write_gate, read_gate, memory, messages
    """
    from safety import ReadSafetyGate, WriteSafetyGate
    from memory import MemoryStore
    from prompt import build_system_prompt
    from tools import set_context, build_symbol_index
    from agent_runtime import AgentRuntime

    config = AgentConfig.load(workspace, cli_args=cli_args)
    write_gate = WriteSafetyGate(workspace, allow_overwrites=config.allow_overwrites,
                                 unrestricted=config.unrestricted)
    read_gate = ReadSafetyGate(workspace, unrestricted=config.unrestricted)
    memory_path = os.path.join(workspace or os.getcwd(), config.memory_filename)
    memory = MemoryStore(memory_path, max_messages=config.max_messages,
                        max_tokens=config.max_tokens)
    set_context(exa_api_key=config.exa_api_key, openai_api_key=config.openai_api_key,
                scratchpad_path=memory._db_path, _memory_store=memory)
    
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
                print(f"  \u2728 Auto-init: {result.content[:120]}", file=sys.stderr)
        except OSError as exc:
            print(f"  \u26a0 Auto-init skipped: {exc}", file=sys.stderr)

    # Start MCP connections if configured
    mcp_manager = None
    if config.mcp_servers:
        try:
            from tools.mcp_client import McpClientManager
            mcp_manager = McpClientManager(config.mcp_servers)
            connected = mcp_manager.start_all()
            if connected:
                set_context(_mcp_manager=mcp_manager)
                print(
                    f"MCP: connected to {len(connected)} server(s): "
                    f"{', '.join(connected)}",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(f"Warning: MCP init failed: {exc}", file=sys.stderr)

    saved = memory.load()
    # Prune loaded conversation to avoid massive first-turn payload
    if saved:
        from memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned
        saved, _ = _compress_tool_results(saved, keep_recent=20)
        saved, pruned = _prune_by_tokens(saved, config.max_tokens, config.max_messages)
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

    import requests as _requests
    session = _requests.Session()
    # Set default timeout (connect, read) for every request.
    import functools
    session.request = functools.partial(session.request, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
    # Limit connection pool to avoid resource waste on long-running sessions.
    session.mount("https://", _requests.adapters.HTTPAdapter(
        pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE))

    # Ensure the session is closed on normal interpreter shutdown.
    import atexit
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
        help="Run the plain stdin REPL (mini_agent.py) instead of the prompt_toolkit TUI",
    )
    parser.add_argument(
        "--legacy-tui", action="store_true", default=None,
        help="Launch the legacy Textual TUI (tui.py) instead of the prompt_toolkit TUI",
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
    Used by both the terminal REPL (mini_agent.py) and TUI (tui.py).
    """
    if override is not None:
        return override
    import sys as _sys
    args = _sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--workspace" and i + 1 < len(args):
            return args[i + 1]
    return os.environ.get("AGENT_WORKSPACE", os.getcwd())
