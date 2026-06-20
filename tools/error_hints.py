#!/usr/bin/env python3
"""error_hints.py -- heuristic error hints and failure-pattern learning for mini_agent.

Provides:
  - _ERROR_HINTS        -- heuristic hint table for common tool failures
  - _build_error_hint   -- build a self-correction hint for LLM on tool failure
  - _fingerprint_error  -- extract a stable error fingerprint from a tool result
  - _FAILURE_PATTERNS   -- recovery strategies keyed by (tool_name, fingerprint)
  - _learn_from_failure -- detect repeated failures, escalate, inject recovery hints
"""

from __future__ import annotations

import sqlite3

from tools.result import ToolResult, ErrorClass
from tools.schema import TOOLS
from logging_setup import get_logger

_log = get_logger("error_hints")


# ---------------------------------------------------------------------------
# Compact error formatting -- token-efficient, action-steering errors
# ---------------------------------------------------------------------------
# Format: "✗ CODE: what → fix" or "✗ CODE: what"
# The ✗ prefix signals failure; CODE is short (≤20 chars); → points to fix.

def _err(code: str, what: str, fix: str = "") -> str:
    """Build a compact, actionable error message."""
    suffix = f" → {fix}" if fix else ""
    return f"✗ {code}: {what}{suffix}"


def _hint(fix: str) -> str:
    """Build a compact hint suffix."""
    return f"  ⓘ {fix}"

# Cache: pre-built valid params strings for _build_error_hint (P0.2 perf)
# Maps tool name -> (valid_params_str, required_set)
_TOOL_PARAM_CACHE: dict[str, tuple[str, set[str]]] = {}

# ---------------------------------------------------------------------------
# Heuristic hints for common tool failures
# ---------------------------------------------------------------------------
_ERROR_HINTS: dict[str, list[tuple[str, str]]] = {
    "read_file": [
        ("not found", "file not found → list_directory"),
        ("No such file", "file not found → list_directory"),
        ("FileNotFoundError", "file not found → list_directory"),
    ],
    "search_files": [
        ("No matches", "no match → enable regex=true or shorten pattern"),
    ],
    "write_file": [
        ("blocked", "path blocked → use workspace path"),
        ("outside workspace", "path outside workspace → use workspace path"),
    ],
    "edit_file": [
        ("blocked", "path blocked → use workspace path"),
        ("outside workspace", "path outside workspace → use workspace path"),
        ("hash mismatch", "stale anchors → re-read with hash_lines=True"),
        ("missing edit specification", "provide from/from_hash"),
    ],
    "run_shell": [
        ("not found", "cmd not found → check PATH/spelling"),
        ("command not found", "cmd not found → check PATH/spelling"),
        ("No such file or directory", "cmd not found → check PATH/spelling"),
    ],
}


def _build_error_hint(name: str, exc: Exception = None, error_msg: str = "") -> str:
    """Build a compact self-correction hint for the LLM when a tool call fails.

    Returns a single line: "retry → params: path (required), content (required)"
    Only includes heuristics when the error isn't already self-explanatory.
    """
    error_text = error_msg or str(exc) if exc else ""

    # 1. Check heuristic pattern hints (only for non-compact errors)
    heuristic: str | None = None
    name_lower = name.lower()
    if name_lower in _ERROR_HINTS:
        for pattern, suggestion in _ERROR_HINTS[name_lower]:
            if pattern.lower() in error_text.lower():
                heuristic = suggestion
                break

    # 2. Build compact hint: "retry → params: ..."
    cached = _TOOL_PARAM_CACHE.get(name)
    if cached is None:
        valid_params_list: list[str] = []
        for tool_def in TOOLS:
            if tool_def["function"]["name"] == name:
                props = tool_def["function"].get("parameters", {}).get("properties", {})
                required = set(tool_def["function"].get("parameters", {}).get("required", []))
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    marker = "*" if pname in required else ""
                    valid_params_list.append(f"{pname}{marker}:{ptype}")
                _TOOL_PARAM_CACHE[name] = (", ".join(valid_params_list), required)
                break
        else:
            _TOOL_PARAM_CACHE[name] = ("", set())
    valid_params_str, _required_set = _TOOL_PARAM_CACHE[name]

    parts = []
    if valid_params_str:
        parts.append(f"retry → {valid_params_str}")
    if heuristic:
        parts.append(heuristic)
    return " | ".join(parts) if parts else "retry with corrected args"


def _fingerprint_error(name: str, content: str) -> str:
    """Extract a stable, short fingerprint from a tool error message.

    Returns one of: 'not_found', 'guard', 'blocked', 'anchor', 'content',
    'missing', 'range', 'whitespace', 'ambiguous', 'count', 'exists',
    'offset', 'invalid_regex', 'timed_out', 'failures', 'not_found',
    or a truncated version of the first 60 chars of content.
    """
    cl = content.lower()

    # Compact format: "✗ CODE: ..." → extract CODE as fingerprint
    if cl.startswith("✗ "):
        try:
            code = cl[2:].split(":", 1)[0].strip().lower()
            if code:
                return code
        except (ValueError, IndexError):
            pass

    # Legacy / fallback substring matching
    if name == "edit_file":
        if "not_found" in cl or "not found" in cl or "does not exist" in cl:
            return "not_found"
        if "whitespace" in cl or "indentation" in cl or "tab" in cl or "trailing" in cl:
            return "whitespace"
        if "ambiguous" in cl or "multiple" in cl or "appears" in cl:
            return "ambiguous"
        if "count" in cl or "invalid count" in cl:
            return "count"
    elif name == "write_file":
        if "blocked" in cl or "safety" in cl or "guard" in cl:
            return "guard" if "guard" in cl else "blocked"
        if "exists" in cl or "overwrite" in cl:
            return "exists"
    elif name == "read_file":
        if "not_found" in cl or "not found" in cl or "no such file" in cl:
            return "not_found"
        if "offset" in cl or "exceeds" in cl:
            return "offset"
    elif name == "search_files":
        if "no matches" in cl or "not found" in cl:
            return "not_found"
        if "invalid" in cl and "regex" in cl:
            return "invalid_regex"
    elif name == "run_shell":
        if "not_found" in cl or "not found" in cl or "command not found" in cl:
            return "not_found"
        if "blocked" in cl or "destructive" in cl:
            return "blocked"
        if "timed out" in cl or "timeout" in cl:
            return "timed_out"
    elif name in ("find_symbol", "find_usages"):
        if "no match" in cl or "not found" in cl:
            return "not_found"
    elif name in ("run_tests", "verify"):
        if "fail" in cl or "FAILED" in cl:
            return "failures"
    return content[:60].strip().lower()


# ---------------------------------------------------------------------------
# Error class mapping: fingerprint -> ErrorClass + retryable + retry_after_ms
# ---------------------------------------------------------------------------
# Used by execute_tool() to annotate ToolResult failures with structured
# error semantics so the LLM can choose the right recovery strategy without
# guessing.  Keyed by (tool_name, fingerprint).

_ERROR_CLASS_MAP: dict[str, dict[str, tuple[ErrorClass, bool, int]]] = {
    "edit_file": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
        "guard": (ErrorClass.VALIDATION, True, 0),
        "missing": (ErrorClass.VALIDATION, True, 0),
        "range": (ErrorClass.VALIDATION, True, 0),
        "anchor": (ErrorClass.VALIDATION, True, 0),
        "content": (ErrorClass.VALIDATION, True, 0),
        "whitespace": (ErrorClass.VALIDATION, True, 0),
        "ambiguous": (ErrorClass.VALIDATION, True, 0),
        "count": (ErrorClass.VALIDATION, True, 0),
        "hash mismatch": (ErrorClass.VALIDATION, True, 0),
        "missing edit specification": (ErrorClass.VALIDATION, True, 0),
    },
    "write_file": {
        "blocked": (ErrorClass.AUTHORIZATION, False, 0),
        "guard": (ErrorClass.VALIDATION, True, 0),
        "exists": (ErrorClass.VALIDATION, True, 0),
    },
    "read_file": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
        "missing": (ErrorClass.VALIDATION, True, 0),
        "blocked": (ErrorClass.AUTHORIZATION, False, 0),
        "offset": (ErrorClass.VALIDATION, True, 0),
    },

    "run_shell": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
        "blocked": (ErrorClass.AUTHORIZATION, False, 0),
        "timed_out": (ErrorClass.TRANSIENT, True, 2000),
        "timeout": (ErrorClass.TRANSIENT, True, 2000),
    },
    "search_files": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
        "invalid_regex": (ErrorClass.VALIDATION, True, 0),
    },
    "find_symbol": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
    },
    "find_usages": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
    },
    "run_tests": {
        "failures": (ErrorClass.PARTIAL_SUCCESS, False, 0),
    },
    "verify": {
        "failures": (ErrorClass.PARTIAL_SUCCESS, False, 0),
    },
    "replace_symbol": {
        "not_found": (ErrorClass.NOT_FOUND, False, 0),
        "guard": (ErrorClass.VALIDATION, True, 0),
    },
}


def _error_class_for(name: str, fingerprint: str) -> tuple[ErrorClass, bool, int] | None:
    """Return (error_class, retryable, retry_after_ms) for a (tool, fingerprint)."""
    tool_map = _ERROR_CLASS_MAP.get(name)
    if tool_map is None:
        return None
    return tool_map.get(fingerprint)


def _classify_result(result: ToolResult, tool_name: str) -> None:
    """Annotate *result* in-place with error_class, retryable, retry_after_ms.

    Called by execute_tool() on every non-success result.  Uses fingerprint
    matching first, then falls back to heuristics from content.
    """
    if result.success:
        return
    if result.error_class is not None:
        return  # already classified

    content = (result.content or "").lower()
    fingerprint = _fingerprint_error(tool_name, result.content or "")

    # Try the explicit map first
    classified = _error_class_for(tool_name, fingerprint)
    if classified is not None:
        result.error_class, result.retryable, result.retry_after_ms = classified
        return

    # Fallback heuristics from content (handles both compact ✗ CODE: and legacy formats)
    if any(kw in content for kw in ("timeout", "timed out", "timed_out", "connection", "network", "unreachable")):
        result.error_class = ErrorClass.TRANSIENT
        result.retryable = True
        result.retry_after_ms = 2000
    elif any(kw in content for kw in ("not_found", "not found", "no such file", "does not exist", "no match")):
        result.error_class = ErrorClass.NOT_FOUND
        result.retryable = False
    elif any(kw in content for kw in ("blocked", "guard", "safety", "permission denied", "unauthorized", "forbidden")):
        result.error_class = ErrorClass.AUTHORIZATION
        result.retryable = False
    elif any(kw in content for kw in ("rate limit", "too many requests", "429")):
        result.error_class = ErrorClass.RATE_LIMIT
        result.retryable = True
        result.retry_after_ms = 5000
    elif any(kw in content for kw in ("invalid", "malformed", "bad", "unknown parameter", "missing", "anchor", "content", "range")):
        result.error_class = ErrorClass.VALIDATION
        result.retryable = True
    else:
        result.error_class = ErrorClass.PERMANENT
        result.retryable = False


_FAILURE_PATTERNS: dict[str, dict[str, str]] = {
    "edit_file": {
        "not_found": "re-read with read_file(hash_lines=True) for current anchors",
        "guard": "read_file(path, hash_lines=True) first",
        "missing": "provide from/from_hash for each edit",
        "range": "re-read file to see current length",
        "anchor": "re-read with read_file(hash_lines=True)",
        "content": "re-read with read_file(hash_lines=True)",
        "whitespace": "copy exact whitespace from read_file output",
        "ambiguous": "use a more specific string",
        "count": "use count=1 or count=-1",
        "hash mismatch": "re-read with read_file(hash_lines=True)",
        "missing edit specification": "provide from/from_hash/to/to_hash/new_text",
    },
    "write_file": {
        "blocked": "use path inside workspace or force=True",
        "guard": "read_file(path) first",
        "exists": "use force=True to overwrite",
    },
    "read_file": {
        "not_found": "check path with list_directory",
        "offset": "reduce offset; use file_info to check size",
    },
    "run_shell": {
        "not_found": "check command spelling and PATH",
        "blocked": "use force=True for safe operations",
        "timed_out": "break into smaller steps or increase timeout",
    },
    "search_files": {
        "not_found": "broaden pattern or enable regex=true",
        "invalid_regex": "check escaping; use raw strings",
    },
    "find_symbol": {
        "not_found": "try find_usages or search_files",
    },
    "find_usages": {
        "not_found": "try search_files for substring match",
    },
    "run_tests": {
        "failures": "use diagnose_failures, fix, re-run",
    },
    "verify": {
        "failures": "review failures, fix, re-run",
    },
    "replace_symbol": {
        "not_found": "check symbol name spelling",
        "guard": "read_file(path) first",
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
    FailurePatternStore).  Both serve different consumers -- startup context
    vs. real-time tool guidance -- so the duplication is intentional.
    """
    if result is None:
        return

    # Lazy import to avoid circular dependency at module level
    from tools import _TOOL_CONTEXT

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
