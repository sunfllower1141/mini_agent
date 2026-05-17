#!/usr/bin/env python3
"""
config.py — project-level configuration for mini_agent.

Looks for ``.mini_agent.toml`` in the workspace root and merges settings
with env vars and CLI flags.  Priority: CLI > env var > config file > default.
"""
from __future__ import annotations

import os
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

DEFAULT_MODEL        = "deepseek-v4-pro"
DEFAULT_SUB_AGENT_MODEL = "deepseek-v4-pro"
DEFAULT_SUB_AGENT_MAX_CONCURRENT = 10
DEFAULT_API_URL      = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_API_KEY      = ""  # set via DEEPSEEK_API_KEY env var, .env file, or .mini_agent.toml
DEFAULT_MAX_MESSAGES = 500
DEFAULT_MAX_TOKENS   = 200_000
DEFAULT_SUB_AGENT_MAX_TURNS = 25
DEFAULT_EXA_API_KEY = ""  # set via EXA_API_KEY env var or .mini_agent.toml
DEFAULT_OPENAI_API_KEY = ""  # set via OPENAI_API_KEY env var or .mini_agent.toml

# Truncation / timeout / connection-pool constants
TREE_TRUNCATION_LINES   = 60   # max lines in workspace tree before truncating
STATE_TAIL_LINES        = 50   # last N lines of STATE.txt shown on startup
GIT_LOG_TIMEOUT         = 5    # seconds to wait for git log
GIT_LOG_COUNT            = 5    # number of recent commits to show on startup
HTTP_CONNECT_TIMEOUT    = 30   # seconds to establish HTTP connection
HTTP_READ_TIMEOUT       = 120  # seconds to read HTTP response
HTTP_POOL_CONNECTIONS   = 2    # max connections per host
HTTP_POOL_MAXSIZE       = 4    # max total pool size

# Environment variable names used during config loading
ENV_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
ENV_SUB_AGENT_API_KEY = "SUB_AGENT_API_KEY"
ENV_DEEPSEEK_API_URL = "DEEPSEEK_API_URL"
ENV_AGENT_WORKSPACE   = "AGENT_WORKSPACE"
ENV_EXA_API_KEY       = "EXA_API_KEY"
ENV_OPENAI_API_KEY    = "OPENAI_API_KEY"

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

        return config


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
        with open(env_path) as f:
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
    if os.environ.get(ENV_DEEPSEEK_API_KEY):
        config.api_key = os.environ[ENV_DEEPSEEK_API_KEY]
    if os.environ.get(ENV_SUB_AGENT_API_KEY):
        config.sub_agent_api_key = os.environ[ENV_SUB_AGENT_API_KEY]
    if os.environ.get(ENV_DEEPSEEK_API_URL):
        config.api_url = os.environ[ENV_DEEPSEEK_API_URL]
    if os.environ.get(ENV_AGENT_WORKSPACE):
        config.workspace = os.environ[ENV_AGENT_WORKSPACE]
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

    # 2. STATE.txt content (if it exists)
    state_path = os.path.join(workspace, "STATE.txt")
    if os.path.isfile(state_path):
        try:
            with open(state_path) as f:
                state_content = f.read()
            # Only include last ~50 lines to keep it brief
            state_lines = state_content.split("\n")
            if len(state_lines) > STATE_TAIL_LINES:
                state_content = "\n".join(state_lines[-STATE_TAIL_LINES:])
                parts.append("\n## Latest STATE.txt (last 50 lines)\n" + state_content)
            else:
                parts.append("\n## STATE.txt\n" + state_content)
        except OSError:
            pass

    # 3. Recent git log (last 5 commits, if this is a git repo)
    try:
        r = _sp.run(["git", "-C", workspace, "log", "--oneline", f"-{GIT_LOG_COUNT}"],
                    capture_output=True, text=True, timeout=GIT_LOG_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            parts.append("\n## Recent git log\n```\n" + r.stdout.rstrip() + "\n```")
    except OSError | subprocess.TimeoutExpired:
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
        saved, _ = _compress_tool_results(saved, keep_recent=6)
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
        saved, _ = _compress_tool_results(saved, keep_recent=6)
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
