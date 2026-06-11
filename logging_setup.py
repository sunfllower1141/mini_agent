#!/usr/bin/env python3
"""
logging_setup.py — centralized structured logging for mini_agent.

Provides a single ``AgentLogger`` that writes JSON-lines to a rotating log
file and text to stderr.  Also maintains in-memory error counters so the
agent can self-diagnose failure patterns at runtime.

Usage:
    from logging_setup import get_logger
    log = get_logger("module_name")
    log.info("Something happened")
    log.warning("Tool %s failed: %s", tool_name, error)
    log.error("Unrecoverable", exc_info=True)

Log files:
    ~/.mini_agent/logs/agent.log        — all events (rotating, 5×10MB)
    ~/.mini_agent/logs/api_error.log    — API-level errors (HTTP, timeout)
    ~/.mini_agent/logs/error_traces.log — full tracebacks for crashes
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from collections import defaultdict


def _ts() -> str:
    """Return an ISO-8601 UTC timestamp with microseconds (cross-platform).

    Python's ``%f`` in ``strftime`` is not available on Windows, so we
    compute the microseconds field manually from ``time.time()``.
    """
    t = time.time()
    sec = int(t)
    usec = int((t - sec) * 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{usec:06d}Z"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.expanduser("~"), ".mini_agent", "logs")
AGENT_LOG = os.path.join(LOG_DIR, "agent.log")
API_ERROR_LOG = os.path.join(LOG_DIR, "api_error.log")
ERROR_TRACES_LOG = os.path.join(LOG_DIR, "error_traces.log")
PROMPT_LOG = os.path.join(LOG_DIR, "prompts.log")

os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory error counters (per-session, thread-safe)
# ---------------------------------------------------------------------------

_ERROR_COUNTERS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
# Structure: {tool_name: {"total_failures": N, "not_found": N, "timeout": N, ...}}
_ERROR_COUNTERS_LOCK = threading.Lock()

def _increment_error_counter(tool_name: str, error_fingerprint: str) -> None:
    """Increment the error counter for a tool+error combination."""
    with _ERROR_COUNTERS_LOCK:
        _ERROR_COUNTERS[tool_name]["total_failures"] += 1
        _ERROR_COUNTERS[tool_name][error_fingerprint] += 1

def get_error_summary() -> str:
    """Return a compact summary of all tracked tool errors this session.

    Intended for injection into the system prompt when error rates are
    elevated, so the LLM can adjust its strategy.
    """
    with _ERROR_COUNTERS_LOCK:
        if not _ERROR_COUNTERS:
            return ""
        lines = ["## Session Error Summary"]
        total = 0
        for tool_name in sorted(_ERROR_COUNTERS.keys()):
            counts = _ERROR_COUNTERS[tool_name]
            total_fail = counts.get("total_failures", 0)
            if total_fail == 0:
                continue
            total += total_fail
            # Top 3 error types
            top_errors = sorted(
                [(k, v) for k, v in counts.items() if k != "total_failures"],
                key=lambda x: x[1], reverse=True,
            )[:3]
            error_detail = ", ".join(f"{k}:{v}" for k, v in top_errors)
            lines.append(f"- **{tool_name}**: {total_fail} failures ({error_detail})")
        if total == 0:
            return ""
        lines.insert(0, f"## Session Error Summary ({total} total failures)")
        return "\n".join(lines)

def has_elevated_errors(threshold: int = 5) -> bool:
    """Return True if any tool has exceeded the failure threshold."""
    with _ERROR_COUNTERS_LOCK:
        for counts in _ERROR_COUNTERS.values():
            if counts.get("total_failures", 0) >= threshold:
                return True
    return False

# ---------------------------------------------------------------------------
# JSON-lines formatter
# ---------------------------------------------------------------------------

class _JsonLinesFormatter(logging.Formatter):
    """Format log records as JSON lines for machine parsing."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # Override to use cross-platform _ts() instead of strftime %f
        return _ts()

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            obj["traceback"] = traceback.format_exception(*record.exc_info)
        # Attach any extra fields passed via `extra=`
        for key in ("event_type", "tool_name", "error_fingerprint", "turn", "provider", "status_code", "session"):
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        return json.dumps(obj, default=str)

# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

_ROOT_LOGGER: logging.Logger | None = None
_SETUP_DONE: bool = False
_SETUP_LOCK = threading.Lock()


def get_logger(name: str = "mini_agent") -> logging.Logger:
    """Return a configured logger for *name*.

    On first call sets up the root logger with rotating file + stderr
    handlers.  Subsequent calls return child loggers that inherit the
    configuration.
    """
    global _ROOT_LOGGER, _SETUP_DONE

    with _SETUP_LOCK:
        if not _SETUP_DONE:
            _setup_root_logger()
            _SETUP_DONE = True

    return logging.getLogger(f"mini_agent.{name}")


def _setup_root_logger() -> None:
    """Configure the root mini_agent logger with handlers."""
    global _ROOT_LOGGER

    root = logging.getLogger("mini_agent")
    root.setLevel(logging.DEBUG)
    root.propagate = False  # Don't bubble to the root logger

    # --- Rotating file handler: all events as JSON lines ---
    file_handler = logging.handlers.RotatingFileHandler(
        AGENT_LOG,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonLinesFormatter())
    root.addHandler(file_handler)

    # --- Stderr handler: WARNING+ only, human-readable ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(
        logging.Formatter(
            "  ⚠ [%(name)s] %(levelname)s: %(message)s",
        )
    )
    root.addHandler(stderr_handler)

    _ROOT_LOGGER = root

    # Log the startup marker
    root.info("logging_setup initialized | pid=%d | log_dir=%s", os.getpid(), LOG_DIR)


# ---------------------------------------------------------------------------
# Specialised writers for the two orphaned log files
# ---------------------------------------------------------------------------

def log_api_error(
    provider: str,
    model: str,
    status_code: int | None,
    error_body: str,
    *,
    turn: int = 0,
    session: str = "",
) -> None:
    """Write a structured API error entry to api_error.log."""
    logger = get_logger("api")
    entry: dict = {
        "ts": _ts(),
        "provider": provider,
        "model": model,
        "status_code": status_code,
        "error": error_body[:500],
        "turn": turn,
        "session": session,
    }
    try:
        with open(API_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # Last-resort: don't crash on log write failure
    logger.warning(
        "API error | provider=%s model=%s status=%s body=%s",
        provider, model, status_code, error_body[:200],
        extra={"provider": provider, "status_code": status_code},
    )


def log_prompt(
    messages: list[dict],
    *,
    provider: str = "",
    model: str = "",
    turn: int = 0,
    session: str = "",
) -> None:
    """Write the full LLM prompt (messages array) to prompts.log.

    Called once per API call from ``call_llm()`` in api.py.  Writes a
    JSON-lines entry with the complete message list plus metadata so
    every prompt is auditable.
    """
    # Rough token estimate: char count / 4
    msg_json = json.dumps(messages, default=str, ensure_ascii=False)
    est_tokens = len(msg_json) // 4

    entry: dict = {
        "ts": _ts(),
        "provider": provider,
        "model": model,
        "turn": turn,
        "message_count": len(messages),
        "estimated_tokens": est_tokens,
        "session": session,
        "messages": messages,
    }
    try:
        with open(PROMPT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Last-resort: don't crash on log write failure

    # Also write a lightweight info line to the main agent.log
    logger = get_logger("prompts")
    logger.debug(
        "Prompt sent | provider=%s model=%s turn=%d messages=%d est_tokens=%d",
        provider, model, turn, len(messages), est_tokens,
    )


def log_error_trace(
    error_type: str,
    message: str,
    *,
    exc_info: bool = False,
    extra: dict | None = None,
) -> None:
    """Write a full error trace to error_traces.log.

    Use this for unexpected crashes, unhandled exceptions, and any error
    that needs a full traceback for post-mortem analysis.
    """
    logger = get_logger("traces")
    entry: dict = {
        "ts": _ts(),
        "type": error_type,
        "message": message,
    }
    if extra:
        entry.update(extra)
    if exc_info:
        entry["traceback"] = traceback.format_exc()

    try:
        with open(ERROR_TRACES_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # Last-resort
    logger.error(
        "%s: %s", error_type, message,
        exc_info=exc_info,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Tool-level helpers
# ---------------------------------------------------------------------------

def log_tool_failure(
    tool_name: str,
    error_content: str,
    *,
    fingerprint: str = "",
    turn: int = 0,
) -> None:
    """Log a tool failure and increment the error counter.

    Called from ``execute_tool`` after every ToolResult(success=False).
    """
    if not fingerprint:
        fingerprint = _fingerprint_from_content(tool_name, error_content)

    _increment_error_counter(tool_name, fingerprint)

    logger = get_logger("tools")
    logger.warning(
        "Tool failure | tool=%s fingerprint=%s content=%.200s",
        tool_name, fingerprint, error_content,
        extra={"event_type": "tool_failure", "tool_name": tool_name,
               "error_fingerprint": fingerprint, "turn": turn},
    )


def log_tool_success(tool_name: str, turn: int = 0) -> None:
    """Log a successful tool call (debug level — not noisy in stderr)."""
    logger = get_logger("tools")
    logger.debug(
        "Tool success | tool=%s", tool_name,
        extra={"event_type": "tool_success", "tool_name": tool_name, "turn": turn},
    )


def _fingerprint_from_content(tool_name: str, content: str) -> str:
    """Extract a minimal error fingerprint from tool result content.

    This is a fast, standalone version that does not depend on
    failure_learning.py (which may not be importable if the error
    happens during bootstrap).
    """
    import hashlib

    cl = content.lower()
    if tool_name == "edit_file":
        if "not found" in cl or "does not exist" in cl:
            return "not_found"
        if "whitespace" in cl or "indentation" in cl or "tab" in cl:
            return "whitespace"
        if "ambiguous" in cl or "multiple" in cl:
            return "ambiguous"
    elif tool_name == "write_file":
        if "blocked" in cl or "safety" in cl:
            return "blocked"
    elif tool_name == "read_file":
        if "not found" in cl or "no such file" in cl:
            return "not_found"
    elif tool_name == "search_files":
        if "no matches" in cl or "not found" in cl:
            return "not_found"
        if "invalid" in cl and "regex" in cl:
            return "invalid_regex"
    elif tool_name == "run_shell":
        if "not found" in cl or "command not found" in cl:
            return "not_found"
        if "blocked" in cl or "destructive" in cl:
            return "blocked"
        if "timed out" in cl or "timeout" in cl:
            return "timed_out"
    elif tool_name in ("find_symbol", "find_usages"):
        if "no match" in cl or "not found" in cl:
            return "not_found"
    elif tool_name in ("run_tests", "verify"):
        if "fail" in cl or "FAILED" in cl:
            return "test_failures"
    # Generic fallback
    return "generic:" + hashlib.md5(content[:120].encode()).hexdigest()[:12]
