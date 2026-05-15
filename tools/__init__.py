#!/usr/bin/env python3
"""
tools package — tool definitions, execution, and structured results for mini_agent.

Every tool execution returns a ToolResult (never a raw exception).
All read and write paths route through the safety gates.
Shell commands and searches run sandboxed inside the workspace root.

Adding a new tool requires:
    1. A ``_<name>`` implementation function decorated with ``@_register("name")``.
    2. A ``_<name>_summary`` function decorated with ``@_summarize("name")``.
    3. An entry in ``TOOLS`` (the API schema sent to the LLM).

Submodules:
    file_ops    — read_file, write_file, edit_file, list_directory, file_info,
                  write_scratchpad, diff, restore_file, plan, plan_status
    shell_ops   — run_shell, task_status, search_files, run_tests, verify, git
    search_ops  — find_symbol, find_usages, semantic_search, web_search, recall_turn
    agent_ops   — spawn_agent, agent_status, collect_agent, collect_any,
                  agent_message, agent_read, agent_handoff, agent_inbox,
                  agent_subscribe, agent_extend, agent_cancel
    lsp         — lsp_definition, lsp_references, lsp_hover, lsp_diagnostics
"""

import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass

from safety import ReadSafetyGate, WriteSafetyGate
from tools.schema import TOOLS

# ---------------------------------------------------------------------------
# TOOL_SCHEMA_MAP — O(1) name→schema lookup for execute_tool() validation
# ---------------------------------------------------------------------------

def _get_tool_schema(name: str) -> dict | None:
    """Look up a tool's parameter schema from TOOLS at runtime.

    This is done at call time (not import time) so that MCP tools
    dynamically appended to TOOLS after startup are always included.
    """
    for td in TOOLS:
        if td["function"]["name"] == name:
            return td["function"].get("parameters", {})
    return None

# ---------------------------------------------------------------------------
# Structured tool result
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Structured result from a tool execution — never a raw exception.

    *hint* is an optional short diagnostic shown to the LLM to help it
    self-correct on malformed calls (invalid JSON, unknown parameters,
    wrong types, etc.).  It is included only on failure.
    """

    success: bool
    content: str
    hint: str = ""
    diff_preview: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"success": self.success, "content": self.content}
        if self.hint:
            d["hint"] = self.hint
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# Tool dispatch registry
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, callable] = {}
_TOOL_SUMMARIES: dict[str, callable] = {}
# Context keys used across tools and llm
CTX_SCRATCHPAD_PATH = "scratchpad_path"
CTX_SCRATCHPAD_UPDATED = "_scratchpad_updated"
CTX_TURN_HISTORY = "_turn_history"  # dict[int, str] — turn number → summary
CTX_PLAN_STEPS = "_plan_steps"      # list[str] — from plan tool
CTX_PLAN_DONE = "_plan_done"        # set[int] — completed step indices

class AgentContext:
    """Mutable context shared across tools and the agent loop.

    Replaces the old ``_TOOL_CONTEXT`` dict.  Initialized once at startup
    via ``set_context()``, then read/written by tools and ``llm.py``.

    Attributes (all optional, defaulting to None or empty):
        scratchpad_path     SQLite DB path for scratchpad persistence
        exa_api_key         API key for Exa web search
        workspace           Workspace root directory
        _scratchpad_updated Flag: scratchpad was updated this turn
        _turn_history       dict[int, str] — turn number → summary
        _plan_steps         list[str] — declared plan steps
        _plan_done          set[int] — completed step indices
    """

    def __init__(self):
        self.scratchpad_path: str | None = None
        self.exa_api_key: str | None = None
        self.openai_api_key: str | None = None
        self.workspace: str | None = None
        self._scratchpad_updated: bool = False
        self._turn_history: dict[int, str] = {}
        self._plan_steps: list[str] = []
        self._plan_done: set[int] = set()
        self._memory_store = None  # MemoryStore instance (set by init_session)


_TOOL_CONTEXT = AgentContext()

# Per-turn cache for read-only tools. Cleared by run_agent_turn each turn.
# Key: (tool_name, sorted_args_json). Cached read_file/file_info/etc.
_TOOL_CACHE: dict[str, "ToolResult"] = {}

# Files modified by write/edit — used by verify
_MODIFIED_FILES: set[str] = set()
_MODIFIED_FILES_LOCK = threading.Lock()

_TASK_REGISTRY: dict[str, subprocess.Popen] = {}  # background shell task registry

# File reservation system — prevents sub-agent write collisions
# Maps file_path (relative to workspace) → task_id of owning agent
_FILE_RESERVATIONS: dict[str, str] = {}
_FILE_RESERVATIONS_LOCK = threading.Lock()

# Sub-agent runtime registry (lazy init in config.init_session)
_AGENT_RUNTIME = None  # AgentRuntime — set by init_session
_MCP_MANAGER = None    # McpClientManager — set by init_session
_CACHEABLE = frozenset({
    "read_file", "file_info", "list_directory",
    "search_files", "semantic_search", "web_search",
    "lsp_definition", "lsp_references", "lsp_hover", "lsp_diagnostics",
})


def set_context(**kwargs) -> None:
    """Set module-level context accessible to tool implementations."""
    ctx = _TOOL_CONTEXT
    for key, value in kwargs.items():
        if key == "scratchpad_path":
            ctx.scratchpad_path = value
        elif key == "exa_api_key":
            ctx.exa_api_key = value
        elif key == "openai_api_key":
            ctx.openai_api_key = value
        elif key == "workspace":
            ctx.workspace = value
        elif key == "_mcp_manager":
            global _MCP_MANAGER
            _MCP_MANAGER = value
        else:
            setattr(ctx, key, value)


def reserve_file(path: str, task_id: str) -> tuple[bool, str]:
    """Try to reserve a file for writing. Returns (ok, message).

    Fails if the file is already reserved by another agent.
    Call this before write_file/edit_file to prevent collisions.
    """
    with _FILE_RESERVATIONS_LOCK:
        existing = _FILE_RESERVATIONS.get(path)
        if existing is not None and existing != task_id:
            return False, f"File '{path}' is reserved by agent '{existing[:8]}'"
        _FILE_RESERVATIONS[path] = task_id
    return True, ""


def release_file(path: str, task_id: str) -> None:
    """Release a file reservation. No-op if not reserved by this agent."""
    with _FILE_RESERVATIONS_LOCK:
        if _FILE_RESERVATIONS.get(path) == task_id:
            del _FILE_RESERVATIONS[path]


def release_all_files(task_id: str) -> None:
    """Release all file reservations held by an agent."""
    with _FILE_RESERVATIONS_LOCK:
        to_release = [p for p, t in _FILE_RESERVATIONS.items() if t == task_id]
        for path in to_release:
            del _FILE_RESERVATIONS[path]


def add_modified_file(path: str) -> None:
    """Record a file as modified (thread-safe). Used by write_file/edit_file."""
    with _MODIFIED_FILES_LOCK:
        _MODIFIED_FILES.add(path)


def get_modified_files() -> list[str]:
    """Return a snapshot of modified files (thread-safe)."""
    with _MODIFIED_FILES_LOCK:
        return sorted(_MODIFIED_FILES)


def _register(name: str):
    """Decorator: register an implementation function in the dispatch table."""
    def decorator(fn):
        _TOOL_DISPATCH[name] = fn
        return fn
    return decorator


def _summarize(name: str):
    """Decorator: register a summary function for verbose logging."""
    def decorator(fn):
        _TOOL_SUMMARIES[name] = fn
        return fn
    return decorator


def clear_tool_cache() -> None:
    """Clear the per-turn tool cache. Called at the start of each agent turn."""
    _TOOL_CACHE.clear()


def _repair_json(raw: str) -> tuple[object, bool]:
    """Attempt to repair common LLM-generated JSON malformations.

    Returns (parsed_value, was_repaired).  If all repair attempts fail the
    original raw string is re-raised via json.loads so callers see a standard
    JSONDecodeError.

    Repairs attempted (in order, each retried independently, then combinations):
    1. Trailing commas before ``]`` or ``}``
    2. Single-quoted strings → double quotes
    3. Unquoted object keys
    4. 1+2, 1+3, 2+3, 1+2+3 (combinations)
    """
    import re

    # Helper: apply unquoted-key fix only outside strings
    def _fix_unquoted_keys(text: str) -> str:
        """Quote bare keys but skip content inside double-quoted strings."""
        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == '"':
                # Find end of string (handle backslash escapes)
                j = i + 1
                while j < len(text):
                    if text[j] == '\\' and j + 1 < len(text):
                        j += 2
                        continue
                    if text[j] == '"':
                        j += 1
                        break
                    j += 1
                result.append(text[i:j])
                i = j
            else:
                # Accumulate consecutive non-quoted chars into one segment
                j = i
                while j < len(text) and text[j] != '"':
                    j += 1
                result.append(text[i:j])
                i = j
        # Only apply regex to segments at even indices (outside strings).
        # Use [A-Za-z_]\w* instead of \w+ to avoid matching numeric keys
        # like '1:' which would produce '"1":}' instead of leaving '1:' alone.
        for idx in range(0, len(result), 2):
            result[idx] = re.sub(r'([A-Za-z_]\w*)(\s*:)', r'"\1"\2', result[idx])
        return ''.join(result)

    # Individual fixes
    fix1 = re.sub(r',\s*([}\]])', r'\1', raw)

    fix2 = raw
    if "'" in raw:
        fix2 = raw.replace("'", '"')

    fix3 = raw
    if not raw.strip().startswith('['):
        fix3 = _fix_unquoted_keys(raw)

    # Combinations — apply fixes in sequence on copies
    def _apply_combo(base: str, *indices: int) -> str:
        s = base
        for i in indices:
            if i == 1:
                s = re.sub(r',\s*([}\]])', r'\1', s)
            elif i == 2:
                s = s.replace("'", '"')
            elif i == 3:
                if not s.strip().startswith('['):
                    s = _fix_unquoted_keys(s)
        return s

    attempts: list[str] = [
        fix1,
        fix2,
        fix3,
        _apply_combo(raw, 1, 2),
        _apply_combo(raw, 1, 3),
        _apply_combo(raw, 2, 3),
        _apply_combo(raw, 1, 2, 3),
    ]

    for attempt in attempts:
        if attempt == raw:
            continue
        try:
            return json.loads(attempt), True
        except (json.JSONDecodeError, ValueError):
            continue

    # Last resort: try the original
    return json.loads(raw), False


# ---------------------------------------------------------------------------
# Heuristic hints for common tool failures
# ---------------------------------------------------------------------------
_ERROR_HINTS: dict[str, list[tuple[str, str]]] = {
    "read_file": [
        ("not found", "The file does not exist. Try list_directory to see available files."),
        ("No such file", "The file does not exist. Try list_directory to see available files."),
        ("FileNotFoundError", "The file does not exist. Try list_directory to explore the workspace."),
    ],
    "search_files": [
        ("No matches", "No matches found. Try find_symbol, or broaden your search with regex or a shorter/simpler pattern."),
    ],
    "write_file": [
        ("blocked", "Write blocked by safety layer. Use a path inside the workspace or enable unrestricted mode."),
        ("outside workspace", "Write blocked — path is outside the workspace. Try a path inside the workspace root."),
    ],
    "edit_file": [
        ("blocked", "Edit blocked by safety layer. Use a path inside the workspace or enable unrestricted mode."),
        ("outside workspace", "Edit blocked — path is outside the workspace. Try a path inside the workspace root."),
    ],
    "run_shell": [
        ("not found", "Command not found. Check that it is installed and on your PATH."),
        ("command not found", "Command not found. Check that it is installed and on your PATH."),
        ("No such file or directory", "Command not found. Check that it is installed and on your PATH, or check for typos."),
    ],
}


def _build_error_hint(name: str, exc: Exception = None, error_msg: str = "") -> str:
    """Build a short self-correction hint for the LLM when a tool call fails.

    Includes the tool name, the parse/execution error, the valid parameter
    names, and heuristics for common failure patterns so the LLM can
    immediately retry with corrected arguments.
    """
    error_text = error_msg or str(exc) if exc else ""

    # 1. Check heuristic pattern hints
    heuristic: str | None = None
    name_lower = name.lower()
    if name_lower in _ERROR_HINTS:
        for pattern, suggestion in _ERROR_HINTS[name_lower]:
            if pattern.lower() in error_text.lower():
                heuristic = suggestion
                break

    # 2. Build the hint message
    hint_parts = []
    if exc is not None:
        hint_parts.append(f"Tool '{name}' failed: {exc}")
    elif error_msg:
        hint_parts.append(f"Tool '{name}' failed: {error_msg[:200]}")

    if heuristic:
        hint_parts.append(f"Hint: {heuristic}")

    valid_params: list[str] = []
    for tool_def in TOOLS:
        if tool_def["function"]["name"] == name:
            props = tool_def["function"].get("parameters", {}).get("properties", {})
            required = tool_def["function"].get("parameters", {}).get("required", [])
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "any")
                marker = " (required)" if pname in required else ""
                valid_params.append(f"{pname}: {ptype}{marker}")
            break
    if valid_params:
        hint_parts.append(f"Valid parameters: {', '.join(valid_params)}")

    hint_parts.append("Please fix your tool call arguments and retry.")
    return "\n".join(hint_parts)


def execute_tool(
    tool_call: dict,
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    on_output: callable = None,
    approve_callback: callable = None,
) -> ToolResult:
    """Execute a single tool call.  All read/write paths go through safety gates.

    Read-only tools (read_file, file_info, etc.) are cached within a turn
    so repeated reads of the same file hit the cache instead of disk.

    If *on_output* is provided, it is called with (tool_name, line_str) for
    real-time output streaming (currently only run_shell uses this).

    Malformed JSON arguments are repaired automatically (trailing commas,
    single quotes, unquoted keys) before parsing.  On failure a *hint* is
    attached to the ToolResult so the LLM can self-correct.
    """
    fn = tool_call["function"]
    name = fn["name"]
    raw_args = fn["arguments"]
    try:
        args, _repaired = _repair_json(raw_args)
    except json.JSONDecodeError as exc:
        hint = _build_error_hint(name, exc)
        return ToolResult(
            success=False,
            content=f"Malformed JSON in tool arguments: {exc}",
            hint=hint,
        )

    # --- strip _pipe meta-field before validation AND cache check (tool piping) ---
    # Must happen BEFORE cache key is computed, otherwise piped calls
    # get a different key and never hit the cache.
    pipe_config = None
    if isinstance(args, dict):
        pipe_config = args.pop("_pipe", None)

    # Check cache for read-only tools (skip if on_output is streaming)
    cache_key = ""
    if on_output is None and name in _CACHEABLE:
        cache_key = json.dumps([name, args], sort_keys=True)
        if cache_key in _TOOL_CACHE:
            return _TOOL_CACHE[cache_key]

    # --- schema validation: check parameter names against tool definition ---
    if isinstance(args, dict):
        tool_schema = _get_tool_schema(name)
        if tool_schema:
            valid_params = set(tool_schema.get("properties", {}).keys())
            required_params = set(tool_schema.get("required", []))
            provided = set(args.keys())
            unknown = provided - valid_params
            missing = required_params - provided
            if unknown or missing:
                hint_parts = []
                if unknown:
                    hint_parts.append(
                        f"Unknown parameter(s): {', '.join(sorted(unknown))}")
                if missing:
                    hint_parts.append(
                        f"Missing required: {', '.join(sorted(missing))}")
                hint_parts.append(
                    f"Valid parameters: {', '.join(sorted(valid_params))}")
                return ToolResult(
                    success=False,
                    content=f"Invalid arguments: {'; '.join(hint_parts[:2])}",
                    hint="\n".join(hint_parts),
                )

    dispatch = _TOOL_DISPATCH.get(name)
    if dispatch is None:
        known = sorted(td["function"]["name"] for td in TOOLS)
        return ToolResult(
            success=False,
            content=f"Unknown tool: {name}",
            hint=f"Tool '{name}' is not recognized. Available tools: {', '.join(known)}. Please use one of these.",
        )

    # Approval gate for write/destructive tools
    if approve_callback is not None and name in ("write_file", "edit_file", "run_shell"):
        if not approve_callback(name, args):
            return ToolResult(
                success=False,
                content=f"{name} not approved by user.",
                hint=f"Tool '{name}' requires user approval and was denied. Consider an alternative approach or ask the user to approve.",
            )

    # Pass on_output to the tool if it accepts it
    import inspect
    sig = inspect.signature(dispatch)
    if "on_output" in sig.parameters:
        result = dispatch(args, write_gate, read_gate, on_output=on_output)
    else:
        result = dispatch(args, write_gate, read_gate)

    # Normalize: every failed result gets a _build_error_hint so the LLM
    # always sees the same structure (tool name, error, valid params, retry
    # nudge).  If the tool already set a hint that adds unique context,
    # append it to the standard hint instead of discarding it.
    if not result.success:
        standard_hint = _build_error_hint(name, error_msg=result.content)
        if result.hint and result.hint != standard_hint:
            # Merge: standard first, then the tool-specific hint as extra context
            result.hint = standard_hint + "\nAdditional info: " + result.hint
        else:
            result.hint = standard_hint

    # Cache successful read-only results (only when not streaming)
    if cache_key and result.success:
        _TOOL_CACHE[cache_key] = result

    return result


def tool_summary(tc: dict) -> str:
    """Return a compact one-line summary of a tool call for display."""
    fn = tc["function"]
    name = fn["name"]
    try:
        args = json.loads(fn["arguments"])
    except Exception as exc:
        print(f"  ⚠ tool summary parse failed for '{name}': {exc}", file=sys.stderr, flush=True)
        args = {}

    summarize = _TOOL_SUMMARIES.get(name)
    if summarize is None:
        return f"{name}(…)"
    return summarize(args)


# ---------------------------------------------------------------------------
# Import submodules to trigger @_register / @_summarize side effects
# ---------------------------------------------------------------------------

from tools import file_ops    # noqa: E402, F401
from tools import shell_ops   # noqa: E402, F401
from tools import search_ops  # noqa: E402, F401
from tools import agent_ops   # noqa: E402, F401
from tools import agent_patterns  # noqa: E402, F401
from tools import agent_messages  # noqa: E402, F401
from tools import lsp         # noqa: E402, F401
from tools.search_ops import build_symbol_index  # noqa: E402, F401
