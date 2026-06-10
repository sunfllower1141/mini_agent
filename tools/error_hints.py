#!/usr/bin/env python3
"""error_hints.py — heuristic error hints and failure-pattern learning for mini_agent.

Provides:
  - _ERROR_HINTS        — heuristic hint table for common tool failures
  - _build_error_hint   — build a self-correction hint for LLM on tool failure
  - _fingerprint_error  — extract a stable error fingerprint from a tool result
  - _FAILURE_PATTERNS   — recovery strategies keyed by (tool_name, fingerprint)
  - _learn_from_failure — detect repeated failures, escalate, inject recovery hints
"""

from __future__ import annotations

import sqlite3

from tools.result import ToolResult
from tools.schema import TOOLS
from logging_setup import get_logger

_log = get_logger("error_hints")

# Cache: pre-built valid params strings for _build_error_hint (P0.2 perf)
# Maps tool name -> (valid_params_str, required_set)
_TOOL_PARAM_CACHE: dict[str, tuple[str, set[str]]] = {}

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
        if "not been read" in cl or "read_file first" in cl or "read the file before" in cl:
            return "read-before-edit"
        if "modified after last read" in cl or "stored mtime" in cl or "stale" in cl:
            return "stale"
        if "not found" in cl or "does not exist" in cl:
            return "not found"
        if "whitespace" in cl or "indentation" in cl or "tab" in cl or "trailing" in cl:
            return "whitespace"
        if "ambiguous" in cl or "multiple" in cl or "appears" in cl:
            return "ambiguous"
        if "count" in cl or "invalid count" in cl:
            return "count"
        if "blocked" in cl or "safety" in cl:
            return "blocked"
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
        "read-before-edit": "You must read the file with read_file BEFORE editing it. The system enforces this to ensure you have current content. Read the file first, then try the edit again.",
        "stale": "The file was modified externally after your last read. Re-read with read_file to get the latest content before editing.",
        "not found": "The string must match exactly -- check whitespace, indentation, and line endings. Try read_file first to see the exact text.",
        "whitespace": "Whitespace mismatch. Try copying the exact text from read_file output, including all leading/trailing spaces.",
        "ambiguous": "Multiple matches found. Use a more specific old_string or set count=-1 to replace all.",
        "count": "Invalid count value. Use count=1 (first only) or count=-1 (all occurrences).",
        "blocked": "Edit blocked by safety layer. Use a path inside the workspace or enable unrestricted mode.",
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
    # Guard: skip all persistence during interpreter shutdown to avoid
    # cascading "no such table" / "disk I/O error" noise.
    try:
        from memory.memory import is_shutting_down
        if is_shutting_down():
            return
    except ImportError:
        pass

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
