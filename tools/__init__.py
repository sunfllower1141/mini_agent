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

import contextvars
import json
import re
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass

from safety import ReadSafetyGate, WriteSafetyGate
from tools.schema import TOOLS
from logging_setup import get_logger, log_tool_failure, log_tool_success, log_error_trace

_log = get_logger("tools")

# Hardcoded core schema for remember — always present even if schema.py is missing
_REMEMBER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": "Manually capture a learning or observation to project_knowledge for cross-session persistence. Use this when you discover a pattern, workaround, or convention worth remembering in future sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Short topic label for this learning (e.g. 'edit_file whitespace', 'module import pattern')"
                },
                "detail": {
                    "type": "string",
                    "description": "The learning itself — what to remember, the pattern, workaround, or convention."
                },
                "category": {
                    "type": "string",
                    "description": "Optional: category hint (tool_usage, code_pattern, error_pattern, convention, architecture, workaround, dependency, general). Auto-detected if omitted."
                }
            },
            "required": ["topic", "detail"]
        }
    }
}
if not any(td["function"]["name"] == "remember" for td in TOOLS):
    TOOLS.insert(0, _REMEMBER_SCHEMA)

# ---------------------------------------------------------------------------
# TOOL_SCHEMA_MAP — O(1) name→schema lookup for execute_tool() validation
# ---------------------------------------------------------------------------

# Lazily-built dict mirroring TOOLS for O(1) lookup.  Invalidated when
# TOOLS length changes (skills appending tools between turns).
_TOOL_SCHEMA_MAP: dict[str, dict] = {}
_TOOL_SCHEMA_MAP_LEN: int = 0


def _get_tool_schema(name: str) -> dict | None:
    """Look up a tool's parameter schema from TOOLS at runtime.

    Builds an O(1) dict cache on first call; invalidates automatically
    when TOOLS grows (skills activate new tool groups).
    """
    global _TOOL_SCHEMA_MAP, _TOOL_SCHEMA_MAP_LEN
    if len(TOOLS) != _TOOL_SCHEMA_MAP_LEN:
        _TOOL_SCHEMA_MAP = {
            td["function"]["name"]: td["function"].get("parameters", {})
            for td in TOOLS
        }
        _TOOL_SCHEMA_MAP_LEN = len(TOOLS)
    return _TOOL_SCHEMA_MAP.get(name)

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
# Cache: dispatch function signature inspection (P0.1 perf)
# Maps tool name -> bool (whether dispatch fn accepts on_output kwarg)
_DISPATCH_SIGNATURES: dict[str, bool] = {}
# Cache: pre-built valid params strings for _build_error_hint (P0.2 perf)
# Maps tool name -> (valid_params_str, required_set)
_TOOL_PARAM_CACHE: dict[str, tuple[str, set[str]]] = {}
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
        self._failure_pattern_store = None  # FailurePatternStore (set by init_session)
        self._self_critique = None  # SelfCritique instance (set by init_session)
        self._subagent_callback: callable | None = None  # (event_type, data) for Electron sub-agent events


_TOOL_CONTEXT_VAR: contextvars.ContextVar[AgentContext] = contextvars.ContextVar(
    "tool_context", default=AgentContext()
)


class _ContextProxy:
    """Proxy that transparently delegates attribute access to the current
    ``AgentContext`` inside a ``ContextVar``.  Each thread / async task
    gets its own copy, so concurrent tool execution (background shells,
    sub-agents, etc.) cannot cross-contaminate context state."""

    __slots__ = ("_cv",)

    def __init__(self, cv: contextvars.ContextVar):
        super().__setattr__("_cv", cv)

    def __getattr__(self, name: str):
        return getattr(self._cv.get(), name)

    def __setattr__(self, name: str, value):
        if name == "_cv":
            super().__setattr__(name, value)
        else:
            setattr(self._cv.get(), name, value)

    def __delattr__(self, name: str):
        delattr(self._cv.get(), name)

    @property
    def __dict__(self):
        return self._cv.get().__dict__

    def get(self) -> AgentContext:
        """Explicit accessor for the raw ``AgentContext`` (rarely needed)."""
        return self._cv.get()


_TOOL_CONTEXT = _ContextProxy(_TOOL_CONTEXT_VAR)

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
_CACHEABLE = frozenset({
    "read_file", "file_info", "list_directory",
    "search_files", "semantic_search", "web_search",
    "lsp_definition", "lsp_references", "lsp_hover", "lsp_diagnostics",
})
_TOOL_TIMEOUT = 120  # P3.1: per-tool execution timeout (seconds)


# P1.4: Dispatch mapping for set_context — replaces if/elif chain
_CTX_DISPATCH = {
    "scratchpad_path": lambda ctx, v: setattr(ctx, "scratchpad_path", v),
    "exa_api_key": lambda ctx, v: setattr(ctx, "exa_api_key", v),
    "openai_api_key": lambda ctx, v: setattr(ctx, "openai_api_key", v),
    "workspace": lambda ctx, v: setattr(ctx, "workspace", v),
}


def set_context(**kwargs) -> None:
    """Set module-level context accessible to tool implementations."""
    ctx = _TOOL_CONTEXT
    for key, value in kwargs.items():
        handler = _CTX_DISPATCH.get(key)
        if handler is not None:
            handler(ctx, value)
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


# ---------------------------------------------------------------------------
# remember — hardcoded core tool (not dependent on workspace files)
# ---------------------------------------------------------------------------


def _remember(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Store a project-level learning that persists across sessions.

    Saved to the ``project_knowledge`` table via MemoryStore.add_knowledge().
    Auto-categorizes the learning if no category is provided.
    Deduplicates by checking for an existing entry before inserting.
    Returns a summary of what was stored.
    """
    import warnings as _warnings

    topic = args.get("topic", "")
    detail = args.get("detail", "")
    category = args.get("category", "")
    if not topic.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'topic' (short topic label for this learning).",
        )
    if not detail.strip():
        return ToolResult(
            success=False,
            content="Missing required parameter: 'detail' (the learning itself).",
        )

    # Auto-categorize if no category provided
    if not category:
        try:
            from tools.failure_learning import suggest_category, KNOWLEDGE_CATEGORIES
            category = suggest_category(topic, detail)
            if category not in KNOWLEDGE_CATEGORIES:
                category = "general"
        except ImportError:
            _warnings.warn("failure_learning not available; using category='general'")
            category = "general"

    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    topic_preview = topic[:200] + ("..." if len(topic) > 200 else "")
    detail_preview = detail[:200] + ("..." if len(detail) > 200 else "")

    if memory_store is not None:
        # Check for existing entry before inserting (dedup)
        existing = memory_store.find_knowledge(category, topic)
        if existing is not None:
            memory_store.bump_knowledge(existing["id"])
            return ToolResult(
                success=True,
                content=(
                    f"Already known [{category}]:\\n"
                    f"  Topic: {topic_preview}\\n"
                    f"  (bumped hit counter)"
                ),
            )
        try:
            memory_store.add_knowledge(topic, category=category, detail=detail)
        except Exception as e:
            return ToolResult(
                success=True,
                content=f"Remember noted, but DB insert failed: {e}",
            )
        return ToolResult(
            success=True,
            content=(
                f"Stored in project knowledge [{category}]:\\n"
                f"  Topic: {topic_preview}\\n"
                f"  Detail: {detail_preview}"
            ),
        )

    # No memory store available — report non-persistent fallback.
    # The memory store is always set during bootstrap; this path only
    # triggers if bootstrap failed or set_context was never called.
    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)
    if memory_store is None:
        return ToolResult(
            success=True,
            content=(
                f"Remember noted (no persistent store — session init may have skipped)"
            ),
        )


def _remember_summary(args: dict) -> str:
    topic = args.get("topic", "?")
    preview = topic[:60]
    if len(topic) > 60:
        preview += "\u2026"
    return f"remember(\"{preview}\")"


_TOOL_DISPATCH["remember"] = _remember
_TOOL_SUMMARIES["remember"] = _remember_summary

# ── use_skill gate: lazy tool loading ──
from tools.skills import USE_SKILL_SCHEMA, _use_skill  # noqa: E402

_TOOL_DISPATCH["use_skill"] = _use_skill
_TOOL_SUMMARIES["use_skill"] = lambda args: f"use_skill({args.get('name', '?')})"
# Inject schema so it's always in TOOLS (it's a core tool, always visible)
TOOLS.append(USE_SKILL_SCHEMA)


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

    # P0.2: Pre-built cache avoids O(n) TOOLS scan on every failure
    cached = _TOOL_PARAM_CACHE.get(name)
    if cached is None:
        valid_params_list: list[str] = []
        for tool_def in TOOLS:
            if tool_def["function"]["name"] == name:
                props = tool_def["function"].get("parameters", {}).get("properties", {})
                required = set(tool_def["function"].get("parameters", {}).get("required", []))
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    marker = " (required)" if pname in required else ""
                    valid_params_list.append(f"{pname}: {ptype}{marker}")
                _TOOL_PARAM_CACHE[name] = (", ".join(valid_params_list), required)
                break
        else:
            _TOOL_PARAM_CACHE[name] = ("", set())
    valid_params_str, _required_set = _TOOL_PARAM_CACHE[name]
    if valid_params_str:
        hint_parts.append(f"Valid parameters: {valid_params_str}")

    hint_parts.append("Please fix your tool call arguments and retry.")
    return "\n".join(hint_parts)


def _fingerprint_error(name: str, content: str) -> str:
    """Extract a stable, short fingerprint from a tool error message.

    Returns one of: 'not found', 'whitespace', 'ambiguous', 'count',
    'blocked', 'exists', 'offset', 'invalid regex', 'timed out',
    'failures', or a truncated version of the first 60 chars of content.
    """
    cl = content.lower()
    if name == "edit_file":
        if "not found" in cl or "does not exist" in cl:
            return "not found"
        if "whitespace" in cl or "indentation" in cl or "tab" in cl or "trailing" in cl:
            return "whitespace"
        if "ambiguous" in cl or "multiple" in cl or "appears" in cl:
            return "ambiguous"
        if "count" in cl or "invalid count" in cl:
            return "count"
    elif name == "write_file":
        if "blocked" in cl or "safety" in cl:
            return "blocked"
        if "exists" in cl or "overwrite" in cl:
            return "exists"
    elif name == "read_file":
        if "not found" in cl or "no such file" in cl:
            return "not found"
        if "offset" in cl or "exceeds" in cl:
            return "offset"
    elif name == "search_files":
        if "no matches" in cl or "not found" in cl:
            return "not found"
        if "invalid" in cl and "regex" in cl:
            return "invalid regex"
    elif name == "run_shell":
        if "not found" in cl or "command not found" in cl:
            return "not found"
        if "blocked" in cl or "destructive" in cl:
            return "blocked"
        if "timed out" in cl or "timeout" in cl:
            return "timed out"
    elif name in ("find_symbol", "find_usages"):
        if "no match" in cl or "not found" in cl:
            return "not found"
    elif name in ("run_tests", "verify"):
        if "fail" in cl or "FAILED" in cl:
            return "failures"
    return content[:60].strip().lower()


# Mapping of (tool_name, fingerprint) -> recovery hint injected on repeated failure.
# Fingerprints come from _fingerprint_error() above.
_FAILURE_PATTERNS: dict[str, dict[str, str]] = {
    "edit_file": {
        "not found": "The string must match exactly -- check whitespace, indentation, and line endings. Try read_file first to see the exact text.",
        "whitespace": "Whitespace mismatch. Try copying the exact text from read_file output, including all leading/trailing spaces.",
        "ambiguous": "Multiple matches found. Use a more specific old_string or set count=-1 to replace all.",
        "count": "Invalid count value. Use count=1 (first only) or count=-1 (all occurrences).",
    },
    "write_file": {
        "blocked": "Use force=True to bypass overwrite protection, or write to a different path.",
        "exists": "File already exists. Use force=True to overwrite, or write to a different path.",
    },
    "read_file": {
        "not found": "File does not exist. Check the path with list_directory or file_info first.",
        "offset": "Offset exceeds file length. Use file_info to check the file size, then reduce offset.",
    },
    "run_shell": {
        "not found": "Command not found. Check the spelling and that it is installed.",
        "blocked": "Command blocked by safety guard. Use force=True to bypass, or rephrase to use only safe operations.",
        "timed out": "Command timed out. Try breaking the work into smaller steps, or increase timeout.",
    },
    "search_files": {
        "not found": "No matches found. Try broadening the search pattern, or search in a parent directory.",
        "invalid regex": "Invalid regex pattern. Check escaping -- use raw strings or double-escape backslashes.",
    },
    "find_symbol": {
        "not found": "Symbol not found. Try find_usages instead, or search_files for the function name as text.",
    },
    "find_usages": {
        "not found": "No usages found. The symbol may not be referenced anywhere, or try search_files for a substring match.",
    },
    "run_tests": {
        "failures": "Tests failed. Use diagnose_failures to get structured failure details, then read the failing test files and fix them.",
    },
    "verify": {
        "failures": "Verification found issues. Review the lint output and test failures above, fix them, then re-run verify.",
    },
}


def _learn_from_failure(name: str, result: "ToolResult | None") -> None:
    """Detect failure patterns, escalate knowledge, and inject recovery hints.

    On first failure: store a low-importance knowledge entry.
    On repeated failure (same fingerprint): escalate to importance=2 and
    inject a specific recovery hint into result.hint for same-turn correction.

    Mutates *result* in-place to add recovery hints.

    Dual-store rationale: writes to both ``project_knowledge`` (injected at
    session start via build_startup_context, human-reviewable) and
    ``failure_patterns`` (structured per-turn matching via
    FailurePatternStore).  Both serve different consumers — startup context
    vs. real-time tool guidance — so the duplication is intentional.
    """
    if result is None:
        return

    content = result.content or ""
    fingerprint = _fingerprint_error(name, content)

    # Track failures per (name, fingerprint) in process memory
    patterns = getattr(_TOOL_CONTEXT, "_failure_patterns", None)
    if patterns is None:
        patterns = {}
        _TOOL_CONTEXT._failure_patterns = patterns
    key = f"{name}:{fingerprint}"
    count = patterns.get(key, 0) + 1
    patterns[key] = count

    # --- Inject same-turn recovery hint ---
    recovery = _FAILURE_PATTERNS.get(name, {}).get(fingerprint)
    if recovery and count >= 2:
        if result.hint:
            result.hint += "\nRecovery: " + recovery
        else:
            result.hint = recovery
        # P7: also surface recovery in content so the LLM sees it even if
        # it doesn't explicitly read the hint field
        if "\nRecovery:" not in (result.content or ""):
            result.content = (result.content or "") + "\n\n[Recovery hint] " + recovery
    elif count >= 3 and not recovery:
        # Generic escalating hint for unclassified patterns
        generic = f"Tool '{name}' failed {count} times with: {fingerprint}. Try a different approach."
        if result.hint:
            result.hint += "\nRecovery: " + generic
        else:
            result.hint = generic
        if "\nRecovery:" not in (result.content or ""):
            result.content = (result.content or "") + f"\n\n[Recovery hint] {generic}"

    # --- Persist to cross-session knowledge ---
    try:
        memory = getattr(_TOOL_CONTEXT, "_memory_store", None)
        if memory is None:
            return

        summary = f"Tool failure: {name} [{fingerprint}]"
        recovery = _FAILURE_PATTERNS.get(name, {}).get(fingerprint)
        detail = (
            f"Tool '{name}' failed {count} time(s) with pattern '{fingerprint}': "
            f"{content[:200]}"
        )
        if recovery:
            detail += f"\nFix: {recovery}"
        existing = memory.find_knowledge(category="error", summary=summary)

        if existing is not None:
            # Repeated pattern: bump hit count and escalate importance
            memory.bump_knowledge(existing["id"])
            if existing["importance"] < 2:
                conn = memory._get_conn()
                conn.execute(
                    "UPDATE project_knowledge SET importance = 2"
                    " WHERE id = ?",
                    (existing["id"],),
                )
                conn.commit()
        else:
            importance = 1 if count < 2 else 2
            memory.add_knowledge(
                category="error",
                summary=summary,
                detail=detail,
                importance=importance,
            )

        # --- Also record in structured FailurePatternStore for cross-session pattern matching ---
        pattern_store = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
        if pattern_store is not None:
            try:
                # Extract args from result context (best-effort)
                fix_strategy = recovery or ""
                pattern_store.record_failure(
                    tool_name=name,
                    error_content=content,
                    fix_strategy=fix_strategy,
                )
            except Exception:
                _log.warning("FailurePatternStore.record_failure failed", exc_info=True)
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        _log.warning("_learn_from_failure failed", exc_info=True)


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

    # Pass on_output to the tool if it accepts it (P0.1: cached signature check)
    accepts_on_output = _DISPATCH_SIGNATURES.get(name)
    if accepts_on_output is None:
        import inspect
        accepts_on_output = "on_output" in inspect.signature(dispatch).parameters
        _DISPATCH_SIGNATURES[name] = accepts_on_output

    # P3.1: Per-tool execution timeout via background thread
    # Using threading.Thread instead of ThreadPoolExecutor avoids
    # creating/destroying a pool for every single tool call.
    _result_container: list[ToolResult | Exception] = []

    def _run_and_capture():
        try:
            if accepts_on_output:
                _result_container.append(dispatch(args, write_gate, read_gate, on_output=on_output))
            else:
                _result_container.append(dispatch(args, write_gate, read_gate))
        except Exception as exc:
            _result_container.append(exc)

    t = threading.Thread(target=_run_and_capture, daemon=True)
    t.start()
    t.join(timeout=_TOOL_TIMEOUT)
    if t.is_alive():
        # Thread still running after timeout — it's stuck.
        # We cannot safely kill a Python thread, so return a timeout result.
        # The daemon thread will continue running but will be terminated
        # when the process exits.
        return ToolResult(
            success=False,
            content=f"Tool '{name}' timed out after {_TOOL_TIMEOUT}s.",
            hint=_build_error_hint(name, error_msg=f"timed out after {_TOOL_TIMEOUT}s"),
        )

    raw = _result_container[0]
    if isinstance(raw, Exception):
        raise raw
    result = raw

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

    # --- Auto-learn from tool failures (pattern detection + escalation) ---
    if not result.success:
        log_tool_failure(name, result.content)
        _learn_from_failure(name, result)
    else:
        log_tool_success(name)
        # Only record success when this tool has known failure patterns;
        # skips unnecessary DB queries for the vast majority of successful calls.
        in_memory = _TOOL_CONTEXT.__dict__.get("_failure_patterns", {})
        if any(k.startswith(name + ":") for k in in_memory):
            pattern_store = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
            if pattern_store is not None:
                try:
                    pattern_store.record_success(name, args)
                except Exception:
                    _log.warning("FailurePatternStore.record_success failed", exc_info=True)
            # Clear in-memory counters on success — the agent recovered
            _TOOL_CONTEXT.__dict__.pop("_failure_patterns", None)

    # --- Post-edit auto-verification: run LSP diagnostics after file writes ---
    # Guarded by a module-level lock to prevent concurrent LSP connections
    # from deadlocking (two threads both trying to connect to the same pylsp).
    if result.success and name in ("write_file", "edit_file"):
        try:
            file_path = args.get("path", "")
            if file_path and threading.current_thread() is threading.main_thread():
                from tools.lsp import _lsp_diagnostics
                diag_result = _lsp_diagnostics({"file_path": file_path}, write_gate, read_gate)
                if diag_result.success and diag_result.content:
                    result.content += "\n\n[auto-verify] LSP diagnostics:\n" + diag_result.content[:500]
        except Exception:
            _log.warning("auto-verify LSP diagnostics failed for %s", file_path, exc_info=True)

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
        _log.warning("tool_summary JSON parse failed for '%s': %s", name, exc)
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
from tools import browser_ops  # noqa: E402, F401  # browser automation tools
from tools import desktop_ops # noqa: E402, F401  # desktop automation tools
from tools import macos_ops   # noqa: E402, F401  # intensive macOS API integrations
from tools import agent_ops   # noqa: E402, F401
from tools import agent_todos  # noqa: E402, F401
from tools import agent_patterns  # noqa: E402, F401
from tools import agent_messages  # noqa: E402, F401
from tools import lsp         # noqa: E402, F401
from tools.search_ops import build_symbol_index  # noqa: E402, F401
from tools.mcp_client import get_mcp_manager, init_mcp_servers, shutdown_mcp  # noqa: E402, F401

# ---------------------------------------------------------------------------
# mcp_discover / mcp_call — MCP client tools
# ---------------------------------------------------------------------------


@_register("mcp_discover")
def _mcp_discover(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List all tools from all connected MCP servers."""
    manager = get_mcp_manager()
    return manager.discover()


@_summarize("mcp_discover")
def _mcp_discover_summary(_args: dict) -> str:
    return "mcp_discover()"


@_register("mcp_call")
def _mcp_call(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Call a tool on a specific MCP server."""
    server = args.get("server", "")
    tool = args.get("tool", "")
    arguments = args.get("arguments", {})
    if not server:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'server' (MCP server name).",
            hint="Use mcp_discover to see available servers, then mcp_call with a server and tool name.",
        )
    if not tool:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'tool' (tool name to call).",
            hint="Use mcp_discover to see available tools on each server.",
        )
    manager = get_mcp_manager()
    return manager.call(server, tool, arguments)


@_summarize("mcp_call")
def _mcp_call_summary(args: dict) -> str:
    server = args.get("server", "?")
    tool = args.get("tool", "?")
    return f"mcp_call({server}/{tool})"
from tools.mcp_client import get_mcp_manager, init_mcp_servers, shutdown_mcp  # noqa: E402, F401
