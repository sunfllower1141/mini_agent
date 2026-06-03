#!/usr/bin/env python3
"""
context_inject.py — per-turn context injection for the agent orchestrator.

Injects scratchpad, git diff, orchestration status, progress reminders,
circuit breaker warnings, self-critique, and strategy hints into the
message list before each LLM API call.

Extracted from llm.py to keep the orchestrator focused on the main loop.
"""

from __future__ import annotations

import os
import subprocess as _sp
import sys
import threading
from collections import deque
from typing import Any

from api import APIError
from safety import ReadSafetyGate
from tools import _TOOL_CONTEXT, get_modified_files
from logging_setup import get_logger
from interject import poll_interjections

_log = get_logger("context_inject")

# ---------------------------------------------------------------------------
# Named constants for context injection intervals
# ---------------------------------------------------------------------------


import json
from collections import Counter

# Circuit breaker constants and helpers (shared with llm.py)
_CIRCUIT_WINDOW: int = 6
_CIRCUIT_THRESHOLD: int = 3

def _tool_call_key(tc: dict) -> str:
    """Stable hash key for a tool call: name + normalized args."""
    fn = tc["function"]
    name = fn["name"]
    try:
        args_normalized = json.dumps(
            json.loads(fn["arguments"]), sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        args_normalized = fn["arguments"]
    return f"{name}:{args_normalized}"

def _check_circuit(recent_keys: list[str]) -> str | None:
    """Return a warning message if the circuit is tripped, otherwise None."""
    if len(recent_keys) < _CIRCUIT_THRESHOLD:
        return None
    counts = Counter(recent_keys)
    for key, count in counts.items():
        if count >= _CIRCUIT_THRESHOLD:
            return (
                f"\u26a0\ufe0f Circuit breaker: you have called '{key}' {count} times "
                f"in the last {len(recent_keys)} tool calls. "
                "The same call keeps being made with identical arguments. "
                "Stop, diagnose why it isn't working, and try a different "
                "approach rather than repeating it."
            )
    return None

SUB_AGENT_RESULT_PREVIEW = 120  # max chars for sub-agent result in context message

# Context injection intervals
PROGRESS_INTERVAL = 5               # turns between progress reminders
SCRATCHPAD_NUDGE_START_TURN = 5     # first turn to check scratchpad staleness
SCRATCHPAD_NUDGE_INTERVAL = 3       # interval for scratchpad staleness nudge
MODIFIED_FILES_CHECKPOINT_TURN = 2  # turn to show modified-files checkpoint



# One-time context injection flags are stored on _TOOL_CONTEXT
# (_scratchpad_injected, _git_diff_injected) — no module-level globals.


# ---------------------------------------------------------------------------
# Context injection helpers — each appends one kind of context message.
# ---------------------------------------------------------------------------


def _inject_handoff_context(
    messages: list[dict], *, workspace_root: str = "",
) -> None:
    """Inject HANDOFF.md content at session start (one-time per session)."""
    if _TOOL_CONTEXT._handoff_injected or not workspace_root:
        return
    _TOOL_CONTEXT._handoff_injected = True
    handoff_path = os.path.join(workspace_root, "HANDOFF.md")
    if not os.path.isfile(handoff_path):
        return
    try:
        with open(handoff_path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return
    if not content:
        return
    messages.append({
        "role": "user",
        "content": (
            "Session handoff from your previous session "
            "(you wrote this at the end of last session):\n\n"
            + content
        ),
        "_transient": True,
    })


def _inject_scratchpad_context(
    messages: list[dict], *, memory_store: Any = None,
) -> None:
    """Inject current scratchpad content (one-time per session)."""
    if _TOOL_CONTEXT._scratchpad_injected or memory_store is None:
        return
    _TOOL_CONTEXT._scratchpad_injected = True
    scratchpad = memory_store.get_scratchpad()
    if scratchpad.strip():
        messages.append({
            "role": "user",
            "content": (
                "Your scratchpad (current working notes — use write_scratchpad "
                "to update):\n\n" + scratchpad
            ),
            "_transient": True,
        })


def _inject_git_diff(
    messages: list[dict], *, memory_store: Any = None,
    read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject recent git diff (one-time per session)."""
    if _TOOL_CONTEXT._git_diff_injected or memory_store is None or read_gate is None:
        return
    _TOOL_CONTEXT._git_diff_injected = True
    try:
        result = _sp.run(
            ["git", "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=5,
            cwd=read_gate.workspace_root,
        )
        if result.stdout.strip():
            messages.append({
                "role": "user",
                "content": (
                    "Recent git changes since last commit:\n\n"
                    + result.stdout.strip()
                    + "\n\nFocus on these files first when making changes."
                ),
                "_transient": True,
            })
    except (OSError, _sp.TimeoutExpired) as exc:
        print(f"  ⚠ git diff failed: {exc}", file=sys.stderr, flush=True)


def _inject_orchestration_context(messages: list[dict]) -> None:
    """Inject sub-agent orchestration status ONLY when state changes.

    Suppresses injection when nothing new happened since last turn:
    - No new completions (pending_results unchanged)
    - No new broadcast messages
    - No auto-extensions performed
    - Running set unchanged

    This prevents ~500-1000 tokens of orchestration overhead per turn
    when sub-agents are steadily working but nothing has completed yet.
    """
    try:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            return
        running_ids = runtime.get_running_ids()
        pending = runtime.get_pending_results()
        if not running_ids and not pending:
            return

        # --- Compute a state fingerprint to detect changes ---
        pending_fp = tuple(
            (tid, r.success, r.content[:80])
            for tid, r in pending
        )
        running_fp = tuple(sorted(running_ids))
        all_msgs = getattr(runtime, "messages", None)
        msg_fp = len(all_msgs) if all_msgs is not None else 0

        # Check if anything changed since last injection
        last_state = getattr(_TOOL_CONTEXT, "_last_orch_state", None)
        current_state = (pending_fp, running_fp, msg_fp)
        if last_state == current_state and not (
            # Always inject if running_ids is non-empty on first turn or if
            # there are pending results we haven't reported yet
            pending and getattr(_TOOL_CONTEXT, "_last_pending_reported", 0) < len(pending)
        ):
            return
        _TOOL_CONTEXT._last_orch_state = current_state
        _TOOL_CONTEXT._last_pending_reported = len(pending)

        parts: list[str] = []
        if pending:
            parts.append("Sub-agent(s) COMPLETED since your last turn:")
            for tid, result in pending:
                status = "OK" if result.success else "FAILED"
                parts.append(
                    f"  - {tid}: [{status}] {result.content[:SUB_AGENT_RESULT_PREVIEW]}"
                    f"{'...' if len(result.content) > SUB_AGENT_RESULT_PREVIEW else ''}"
                )
            parts.append("")
        if running_ids:
            parts.append(
                f"{len(running_ids)} sub-agent(s) still RUNNING: "
                f"{', '.join(running_ids)}"
            )
            parts.append(
                "Use agent_status() to check each or collect_any() to grab "
                "the first result. Do NOT redo their work."
            )
        # --- Inject new broadcast messages from sub-agents ---
        if all_msgs is not None:
            msg_count = len(all_msgs)
            last_seen = getattr(_TOOL_CONTEXT, "_last_msg_count", 0)
            if msg_count > last_seen:
                new_msgs = all_msgs[last_seen:]
                _TOOL_CONTEXT._last_msg_count = msg_count
                parts.append("New message(s) from sub-agents:")
                for m in new_msgs[-5:]:  # cap at 5 most recent
                    text = m.get('text', '')
                    sender = m.get('from', '')
                    parts.append(f"  [{sender}] {text[:200]}")
                if len(new_msgs) > 5:
                    parts.append(f"  ... ({len(new_msgs)-5} more)")
                parts.append("Use agent_inbox or agent_read to view full messages.")
        # --- Auto-extend productive sub-agents running low on turns ---
        if running_ids:
            for tid in running_ids:
                status_snap = runtime.status_snapshots.get(tid, {})
                turns_budget = status_snap.get("turns_budget", 0)
                current_turn = status_snap.get("turn", 0)
                remaining = turns_budget - current_turn
                if remaining <= 3 and remaining > 0:
                    # Only extend if agent is making forward progress
                    last_action = status_snap.get("last_action", "")
                    if last_action and last_action != "idle":
                        runtime.extend_turns(tid, 10)
                        parts.append(f"  🔄 Auto-extended '{tid}' (+10 turns, {remaining} left)")
        if parts:
            messages.append({
                "role": "user",
                "content": "\n".join(parts),
                "_transient": True,
            })
    except (APIError, AttributeError, KeyError, ValueError, TypeError) as exc:
        _log.warning("orchestration context failed: %s", exc)


def _inject_interjections(messages: list[dict]) -> None:
    """Inject any pending user interjections (every turn)."""
    interjections = poll_interjections()
    for msg_text in interjections:
        messages.append({
            "role": "user",
            "content": msg_text,
        })


# Consecutive read-only turn counter lives on _TOOL_CONTEXT (no module-level global).
_READ_ONLY_NUDGE_THRESHOLD: int = 3  # turns of pure reads before nudge


def _inject_progress_check(messages: list[dict], *, turn_count: int) -> None:
    """Inject periodic progress reminder every PROGRESS_INTERVAL turns.

    Also injects a sufficiency nudge when the agent has spent several
    consecutive turns only reading (no writes / shell executions).
    """
    if turn_count <= 1:
        return

    # --- Read-only sufficiency nudge (every turn, once threshold reached) ---
    if _TOOL_CONTEXT._consecutive_read_only_turns >= _READ_ONLY_NUDGE_THRESHOLD:
        messages.append({
            "role": "user",
            "content": (
                f"You've spent {_TOOL_CONTEXT._consecutive_read_only_turns} turns reading "
                "code without making changes. If you have enough context to "
                "answer the user, do so NOW. If you need more, state what "
                "SPECIFIC information you're still missing — don't just keep "
                "reading files broadly."
            ),
            "_transient": True,
        })

    if turn_count % PROGRESS_INTERVAL != 0:
        return
    reminder = (
        f"You have been working for {turn_count} turns. "
        "Briefly assess your progress: are you making headway, "
        "stuck in a loop, or done? If you can wrap up now, "
        "give the final answer. If you truly need more turns, "
        "continue — but be specific about what remains."
    )
    messages.append({"role": "user", "content": reminder, "_transient": True})


def _inject_modified_files_checkpoint(
    messages: list[dict], *, read_gate: ReadSafetyGate | None = None,
) -> None:
    """Inject modified-files checkpoint (turn 2 only)."""
    if not get_modified_files():
        return
    mod_list = "\n".join(f"  - {f}" for f in get_modified_files())
    test_hint = ""
    for mf in get_modified_files():
        base = os.path.basename(mf)
        if base.startswith("test_") and base.endswith(".py"):
            test_hint += f"\n  Relevant test: {base}"
        elif base.endswith(".py") and not base.startswith("test_"):
            candidate = f"test_{base}"
            dp = os.path.dirname(mf)
            test_path = os.path.join(dp, candidate) if dp else candidate
            if os.path.isfile(os.path.join(read_gate.workspace_root, test_path)):
                test_hint += f"\n  Relevant test: {test_path}"
    ckpt = (
        f"Files modified this session:\n{mod_list}\n"
        f"Running `verify` or `run_tests`{test_hint if test_hint else ''} "
        f"after changes is recommended."
    )
    messages.append({"role": "user", "content": ckpt, "_transient": True})


def _inject_circuit_breaker(
    messages: list[dict], *, recent_tool_keys: deque[str] | None = None,
) -> None:
    """Inject circuit breaker warning if recent calls are repetitive."""
    if recent_tool_keys is None:
        return
    warning = _check_circuit(recent_tool_keys)
    if warning:
        messages.append({"role": "user", "content": warning, "_transient": True})


def _inject_scratchpad_nudge(messages: list[dict], *, turn_count: int) -> None:
    """Inject scratchpad staleness nudge every SCRATCHPAD_NUDGE_INTERVAL turns
    after SCRATCHPAD_NUDGE_START_TURN."""
    if turn_count < SCRATCHPAD_NUDGE_START_TURN:
        return
    if (turn_count - 1) % SCRATCHPAD_NUDGE_INTERVAL != 0:
        return
    if not _TOOL_CONTEXT._scratchpad_updated:
        messages.append({
            "role": "user",
            "content": (
                "⚠️ Your scratchpad hasn't been updated in several turns. "
                "Consider using write_scratchpad to capture your current "
                "plan, progress, and decisions before continuing.\n\n"
                "Good scratchpad format:\n"
                "  GOAL: [1 line — what the user wants]\n"
                "  DONE: [what you've accomplished so far]\n"
                "  NEXT: [exactly what you'll do next turn — be specific]\n"
                "  QUESTIONS: [anything you're uncertain about]\n"
                "Keep it short — this is for YOUR memory, not the user."
            ),
            "_transient": True,
        })
    _TOOL_CONTEXT._scratchpad_updated = False


def _inject_plan_status(messages: list[dict]) -> None:
    """Inject active plan status if a plan is in progress and no sub-agents are running."""
    plan_steps = _TOOL_CONTEXT._plan_steps
    if not plan_steps:
        return
    # Suppress plan when sub-agents are running — avoids confusion
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        if runtime.get_running_ids():
            return
    plan_done = _TOOL_CONTEXT._plan_done
    lines = [f"Active plan ({len(plan_done)}/{len(plan_steps)} done):"]
    for i, s in enumerate(plan_steps, 1):
        mark = "✓" if (i - 1) in plan_done else "○"
        lines.append(f"  [{mark}] {i}. {s}")
    lines.append("Use plan_status to mark steps complete as you finish them.")
    messages.append({
        "role": "user",
        "content": "\n".join(lines),
        "_transient": True,
    })


# ---------------------------------------------------------------------------
# Main context injector — orchestrates all helpers.
# ---------------------------------------------------------------------------



def _inject_system_reminder(messages: list[dict], *, turn_count: int) -> None:
    """Re-inject critical rules near end of long contexts to fight
    'instruction centrifugation' — the system prompt fading as context grows.

    Triggers when message count > 20 (~6-7 turns), repeats every 8 messages
    for DeepSeek (prone to tool-call loops), 12 for other providers.
    """
    if len(messages) <= 20:
        return
    # Provider-specific interval: DeepSeek benefits from more frequent reminders
    provider = getattr(_TOOL_CONTEXT, "_provider", None) or "deepseek"
    _REMINDER_INTERVAL = 8 if provider == "deepseek" else 12
    if not hasattr(_TOOL_CONTEXT, '_system_reminder_last_msg_count'):
        _TOOL_CONTEXT._system_reminder_last_msg_count = 0
    if len(messages) - _TOOL_CONTEXT._system_reminder_last_msg_count < _REMINDER_INTERVAL:
        return
    _TOOL_CONTEXT._system_reminder_last_msg_count = len(messages)

    reminder = (
        "⚠️ SYSTEM REMINDER (context is long — critical rules):\n\n"
        "LOOP PREVENTION:\n"
        "- Same tool + same args 2x = STUCK. Switch approach immediately.\n"
        "- edit_file MUST be preceded by read_file in the same batch.\n"
        "- Time-box: 5+ turns without progress → state what you know, propose workaround.\n"
        "- Context grows stale — trust write_scratchpad and plan over old tool results.\n\n"
        "EFFICIENCY:\n"
        "- Batch ALL independent tool calls in ONE response (parallel execution).\n"
        "- Update write_scratchpad every 3 turns.\n"
        "- Long commands: background=True, poll task_status once.\n\n"
        "CURRENT TURN: {turn_count}. Remember: stale context is worse than no context."
    ).format(turn_count=turn_count)

    messages.append({"role": "user", "content": reminder, "_transient": True})


def _compress_stale_tool_results(messages: list[dict]) -> None:
    """Compress tool results older than 15 messages behind the tail
    to first-line only, saving context while preserving key info.

    Only compresses tool messages whose content has multiple lines.
    Already-compressed results (marked with '… (truncated)') are skipped.
    """
    STALE_THRESHOLD = 15
    tail = len(messages)
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        age = tail - i
        if age <= STALE_THRESHOLD:
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if "… (truncated)" in content or "… (compressed)" in content:
            continue
        lines = content.split("\n")
        if len(lines) <= 2:
            continue
        first_line = lines[0]
        total_lines = len(lines)
        total_chars = len(content)
        m["content"] = (
            f"{first_line}\n… (compressed: {total_lines} lines, {total_chars} chars)"
        )


def _inject_failure_pattern_warnings(
    msg: dict, messages: list[dict],
) -> None:
    """Inject failure pattern warnings for an assistant message's tool calls.

    Called between API call and tool execution so warnings target the
    CURRENT turn's tool choices.  (M7: deduplicated — removed the
    pre-API-call variant that re-warned about the previous turn's
    tool_calls that had already been warned post-API.)
    """
    try:
        fps = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
        if fps is None:
            return
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return
        from tools.failure_learning import build_self_learning_context
        warning = build_self_learning_context(fps, tool_calls)
        if warning:
            messages.append({
                "role": "user",
                "content": warning,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_self_critique(messages: list[dict], *, turn_count: int) -> None:
    """Inject self-critique context when failure clusters are detected.

    Uses the SelfCritique instance to analyze recent tool results and
    generate corrective guidance when the agent appears stuck.

    Also detects consecutive same-tool failures (e.g. edit_file failing
    3+ times with 'not found') and injects a targeted nudge.
    """
    try:
        # --- Consecutive same-tool failure detection ---
        _CONSECUTIVE_FAILURE_THRESHOLD = 3
        _CRITICAL_FAILURE_TOOLS = {"edit_file", "write_file", "run_shell"}
        recent_failures: list[tuple[str, bool]] = []  # (tool_name, success)
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                for prev_msg in messages:
                    if prev_msg.get("role") == "assistant":
                        for tc in prev_msg.get("tool_calls", []):
                            if tc.get("id") == tcid:
                                name = tc.get("function", {}).get("name", "")
                                try:
                                    import json as _json
                                    data = _json.loads(msg.get("content", "{}"))
                                    success = data.get("success", True)
                                except Exception:
                                    success = True
                                recent_failures.append((name, success))
                                break
            if len(recent_failures) >= 8:
                break
        # Check for consecutive same-tool failures
        if len(recent_failures) >= _CONSECUTIVE_FAILURE_THRESHOLD:
            first_name, _ = recent_failures[0]
            if all(name == first_name and not success for name, success in recent_failures[:_CONSECUTIVE_FAILURE_THRESHOLD]):
                if first_name in _CRITICAL_FAILURE_TOOLS:
                    tool_hints = {
                        "edit_file": "STOP using edit_file. Use read_file FIRST to see the exact text, then copy-paste the exact old_string. You're editing blind.",
                        "write_file": "STOP using write_file repeatedly. Verify the file path and content before retrying.",
                        "run_shell": "STOP retrying the same shell command. It's failing consistently. Try a different approach or diagnose the error output.",
                    }
                    hint = tool_hints.get(first_name, f"STOP retrying {first_name}. It has failed {_CONSECUTIVE_FAILURE_THRESHOLD}+ times. Switch approach.")
                    messages.append({
                        "role": "user",
                        "content": f"⚠️ {hint}",
                        "_transient": True,
                    })
                    return  # Don't also inject the general self-critique

        # --- Original self-critique logic ---
        sc = getattr(_TOOL_CONTEXT, "_self_critique", None)
        if sc is None:
            return
        # Collect recent tool results from messages
        recent_results: list[tuple[dict, object]] = []
        from tools import ToolResult as TR
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                # Find the matching tool call
                for prev_msg in messages:
                    if prev_msg.get("role") == "assistant":
                        for tc in prev_msg.get("tool_calls", []):
                            if tc.get("id") == tcid:
                                try:
                                    import json
                                    data = json.loads(msg.get("content", "{}"))
                                    result = TR(
                                        success=data.get("success", False),
                                        content=data.get("content", ""),
                                        hint=data.get("hint", ""),
                                    )
                                    recent_results.append((tc, result))
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                break
            if len(recent_results) >= 30:  # Scan last 30 tool results (was 10 — P6 fix)
                break

        if recent_results:
            critique_msg = sc.assess_turn_results(recent_results, turn_count)
            if critique_msg:
                messages.append({
                    "role": "user",
                    "content": critique_msg,
                    "_transient": True,
                })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_strategy_hint(messages: list[dict]) -> None:
    """#5 Auto tool strategy hints — suggest optimal search tool.

    Also detects when the agent is using search_files for symbol-like
    patterns (single CamelCase/snake_case identifiers) and nudges
    toward find_symbol instead.
    """
    try:
        # --- Part A: keyword-based hints from latest user message ---
        for msg in reversed(messages):
            if msg["role"] == "user":
                last = msg["content"]
                break
        else:
            return
        hint = ""
        if any(kw in last.lower() for kw in ("find where", "locate", "where is", "find_symbol", "function ", "class ", "def ")):
            hint = "[Hint: Use find_symbol for fast symbol lookup. Use search_files for text patterns.]"
        elif any(kw in last.lower() for kw in ("refactor", "all callers", "who uses", "references")):
            hint = "[Hint: Use find_usages to find all callers, then edit_file for targeted changes.]"
        elif any(kw in last.lower() for kw in ("semantic", "similar", "feels like", "find code that")):
            hint = "[Hint: Use semantic_search for meaning-based code search.]"

        # --- Part B: detect search_files being used for symbol search ---
        if not hint:
            import re as _re
            _symbol_pattern = _re.compile(r"^(search_files|find_symbol|read_file)$")
            text_patterns_seen = 0
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        if name == "search_files":
                            args_raw = fn.get("arguments", "{}")
                            try:
                                import json as _json
                                args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                            except Exception:
                                args = {}
                            pattern = args.get("pattern", "")
                            # Symbol-like: no spaces, contains _ or mixed case
                            if pattern and " " not in pattern and (
                                "_" in pattern or (pattern != pattern.lower() and pattern != pattern.upper())
                            ):
                                text_patterns_seen += 1
            if text_patterns_seen >= 2:
                hint = "[Hint: You've been using search_files for patterns that look like symbol names. Try find_symbol — it's ~10x faster and gives exact line numbers.]"

        if hint:
            # Track injected hints in a set for O(1) dedup
            if not hasattr(_inject_strategy_hint, '_injected'):
                _inject_strategy_hint._injected = set()
            if hint in _inject_strategy_hint._injected:
                return
            _inject_strategy_hint._injected.add(hint)
            # Insert after the last system message, or at index 1 if none found
            insert_at = 1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    insert_at = i + 1
                    break
            # Avoid inserting beyond list bounds
            if insert_at <= len(messages):
                messages.insert(insert_at, {"role": "system", "content": hint})
    except (KeyError, IndexError, TypeError, ValueError):
        pass


def _inject_tool_graph_context(messages: list[dict]) -> None:
    """Inject tool sequencing hints from the ToolGraph.

    Analyzes recent tool usage and suggests optimal sequencing patterns
    learned from past sessions (e.g., "after read_file, most agents follow
    with edit_file").
    """
    try:
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is None:
            return

        # Collect recent tool names from the conversation
        recent_tools: list[str] = []
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        recent_tools.insert(0, name)  # chronological order
            if len(recent_tools) >= 10:
                break

        context_msg = tg.get_tool_context_hints(recent_tools)
        if context_msg:
            messages.append({
                "role": "user",
                "content": context_msg,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_experience_context(
    messages: list[dict],
    memory_store=None,
) -> None:
    """Inject relevant past experiences from project_knowledge.

    Searches project_knowledge for entries relevant to the current
    conversation context and injects matching learnings.
    """
    if memory_store is None:
        return
    try:
        from tools.failure_learning import build_experience_context

        # Extract context from the last user message or recent tool outputs
        search_context = ""
        for msg in reversed(messages):
            if msg.get("role") == "user" and not msg.get("_transient"):
                search_context = msg.get("content", "")[:200]
                break

        if not search_context:
            return

        # Build experience context using a synthetic "tool call" to trigger
        # keyword-based retrieval
        ctx_msg = build_experience_context(
            memory_store,
            tool_name="",  # Empty = search all
            args={"command": search_context},
            limit=2,
        )
        if ctx_msg:
            messages.append({
                "role": "user",
                "content": ctx_msg,
                "_transient": True,
            })
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_pre_execution_context(
    messages: list[dict],
    pending_tool_calls: list[dict],
    turn_count: int,
) -> None:
    """Inject self-learning context before executing tool calls.

    Runs AFTER the API response, when we know which tools will be called.
    Injects:
      - Failure pattern warnings (from FailurePatternStore)
      - Mistake notebook entries (generalized fixes)
      - Tool graph read-before-write detection

    All injected messages are marked _transient so they aren't persisted.
    """
    try:
        # --- 1. Failure pattern warnings ---
        fps = getattr(_TOOL_CONTEXT, "_failure_pattern_store", None)
        if fps is not None:
            from tools.failure_learning import build_self_learning_context
            warning = build_self_learning_context(fps, pending_tool_calls)
            if warning:
                messages.append({
                    "role": "user",
                    "content": warning,
                    "_transient": True,
                })

        # --- 2. Mistake notebook entries ---
        mn = getattr(_TOOL_CONTEXT, "_mistake_notebook", None)
        if mn is not None:
            mn.distill(turn_count)  # Trigger distillation if cooldown elapsed
            notebook_ctx = mn.build_notebook_context(
                pending_tool_calls, turn_count=turn_count,
            )
            if notebook_ctx:
                messages.append({
                    "role": "user",
                    "content": notebook_ctx,
                    "_transient": True,
                })

        # --- 3. Tool graph: read-before-write detection ---
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is not None:
            # Collect recent tools for context
            recent_tools: list[str] = []
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        name = tc.get("function", {}).get("name", "")
                        if name:
                            recent_tools.insert(0, name)
                if len(recent_tools) >= 10:
                    break

            gap_warning = tg.detect_read_before_write_gap(
                pending_tool_calls, recent_tools,
            )
            if gap_warning:
                messages.append({
                    "role": "user",
                    "content": gap_warning,
                    "_transient": True,
                })

    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _record_tool_sequence_to_graph(
    tool_results: list[tuple[dict, object]],
) -> None:
    """Record a turn's tool execution sequence to the ToolGraph."""
    try:
        tg = getattr(_TOOL_CONTEXT, "_tool_graph", None)
        if tg is None:
            return

        tool_names = [
            tc.get("function", {}).get("name", "")
            for tc, _ in tool_results
            if tc.get("function", {}).get("name", "")
        ]
        if len(tool_names) >= 2:
            tg.record_turn_tool_sequence(tool_names)
    except (AttributeError, KeyError, ValueError, TypeError):
        pass


def _inject_context(
    messages: list[dict],
    *,
    turn_count: int,
    memory_store: Any = None,
    read_gate: ReadSafetyGate | None = None,
    recent_tool_keys: deque[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Inject all context messages for the current turn.

    Delegates to smaller helpers for one-time and per-turn injections.
    """
    # One-time injections (first turn only)
    _inject_handoff_context(
        messages,
        workspace_root=read_gate.workspace_root if read_gate else "",
    )
    _inject_scratchpad_context(messages, memory_store=memory_store)
    _inject_git_diff(messages, memory_store=memory_store, read_gate=read_gate)

    # Per-turn injections
    _inject_orchestration_context(messages)
    _inject_interjections(messages)


    _inject_progress_check(messages, turn_count=turn_count)

    if turn_count == MODIFIED_FILES_CHECKPOINT_TURN and (
        hasattr(read_gate, "workspace_root") if read_gate else False
    ):
        _inject_modified_files_checkpoint(messages, read_gate=read_gate)

    _inject_circuit_breaker(messages, recent_tool_keys=recent_tool_keys)
    _inject_scratchpad_nudge(messages, turn_count=turn_count)
    _inject_strategy_hint(messages)
    _inject_plan_status(messages)
    _inject_self_critique(messages, turn_count=turn_count)
    _inject_tool_graph_context(messages)
    _inject_experience_context(messages, memory_store=memory_store)

    # Context-quality defences (research-backed: 25% fill degrades quality)
    _compress_stale_tool_results(messages)
    _inject_system_reminder(messages, turn_count=turn_count)

