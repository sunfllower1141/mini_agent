#!/usr/bin/env python3
"""
sub_agent.py -- sub-agent engine for mini_agent multi-agent support.

Provides:
    SubAgentResult  -- structured result from a completed sub-agent run
    AgentRuntime    -- thread-safe registry of running sub-agent tasks
    run_sub_agent   -- spawns an isolated agent loop in a background thread

A sub-agent gets its own message list, tool cache, and scratchpad.
It shares the parent's workspace, safety gates, and API config.
The runtime registry is designed to be extended later for inter-agent
communication (inboxes), dependency tracking, and persistent agents.
"""

from __future__ import annotations

import sys
import threading
import time

from core.safety import ReadSafetyGate, WriteSafetyGate
from .agent_runtime import SubAgentResult
from api import APIError, call_llm
from logging_setup import get_logger

_sub_log = get_logger("sub_agent")


# ---------------------------------------------------------------------------
# Named constants (avoid magic numbers)
# ---------------------------------------------------------------------------
_SHARED_CONTEXT_CAP = 4_000          # max chars for shared_context from parent
_TASK_CAP = 8_000                    # max chars for task description
_SUB_MAX_TOKENS = 64_000             # max tokens before pruning sub-agent context
_SUB_MAX_MESSAGES = 50               # max messages before pruning sub-agent context
_SUB_COMPRESSION_THRESHOLD = 15      # start compressing when messages exceed this
_SUB_COMPRESSION_KEEP_RECENT = 4     # keep this many recent messages uncompressed
_SUB_SAFETY_TOKEN_CEILING = 32_000   # hard cap: force-prune before API call if over this
_MAX_TOOL_RESULT_CHARS = 5_000       # max chars in a single tool result before truncation
_STREAM_SNAP_EVERY = 200             # tokens between streaming-snapshot updates
_TURN_INTERVAL_COMM_NUDGE = 3        # turns between communication nudges


# ---------------------------------------------------------------------------
# Sub-agent loop (runs in a background thread)
# ---------------------------------------------------------------------------

def run_sub_agent(
    task: str,
    config,  # AgentConfig
    write_gate: WriteSafetyGate,
    read_gate: ReadSafetyGate,
    *,
    max_turns: int = 15,
    cancel_event: threading.Event | None = None,
    parent_depth: int = 0,
    max_depth: int = 3,
    shared_context: str = "",
    stream: bool = False,
    task_id: str = "",     # task_id for direct runtime lookup (avoids O(N) scan)
    parent_task_id: str = "",  # orchestrator task_id for agent_handoff targeting
    subagent_callback: callable | None = None,  # for Electron UI sub-agent pane events
) -> SubAgentResult:
    """Run a sub-agent loop in the current thread (called from a background thread).

    The sub-agent gets:
    - A fresh messages list (system prompt + task as user message)
    - Its own tool cache
    - Its own _MODIFIED_FILES tracking
    - Its own scratchpad (in-memory only -- no SQLite for sub-agents)

    Sub-agents CAN spawn further sub-agents up to *max_depth*.
    Current depth is *parent_depth* + 1.  Tools are blocked when at max_depth.

    Returns a SubAgentResult with success, content, and metadata.
    """
    current_depth = parent_depth + 1
    from tools import (
        execute_tool, _CACHEABLE, _TOOL_CONTEXT,
    )

    # -- Plan isolation (step 1): save parent plan, give sub-agent clean slate --
    _saved_plan_steps = getattr(_TOOL_CONTEXT, '_plan_steps', [])
    _saved_plan_done = getattr(_TOOL_CONTEXT, '_plan_done', set())
    _TOOL_CONTEXT._plan_steps = []
    _TOOL_CONTEXT._plan_done = set()

    def _restore_plan():
        """Restore parent plan state on exit (guaranteed by try/finally)."""
        _TOOL_CONTEXT._plan_steps = _saved_plan_steps
        _TOOL_CONTEXT._plan_done = _saved_plan_done

    from tools.schema import TOOLS
    from core.prompt import build_system_prompt

    # Thread-local agent ID for file reservation enforcement
    from tools.file_ops import _current_agent_id as _agent_tl
    from tools.schema import TOOLS, SUB_AGENT_TOOLS

    # --- build messages for sub-agent ---
    # Sub-agents get a minimal system prompt: behavior rules + essential tools only.
    # The full tool schema (50+ tools at ~15K tokens) is NOT sent -- sub-agents
    # only need read_file, write_file, edit_file, search_files, find_symbol,
    # find_usages, list_directory, run_shell, and sub-agent coordination tools.
    # This saves ~15K tokens per sub-agent context.
    _sub_tools = [t for t in TOOLS if t["function"]["name"] in SUB_AGENT_TOOLS]

    messages: list[dict] = [
        {"role": "system", "content": _SUB_AGENT_SYSTEM_PROMPT},
    ]
    # Build a lightweight system prompt with only the tools sub-agents need
    _sub_system = build_system_prompt(config)
    # Replace the full tool listing with the subset
    if _sub_tools:
        _sub_system += "\n\n## Available Tools (subset for workers)\n"
        for t in _sub_tools:
            fn = t["function"]
            desc = fn.get("description", "")
            # Keep description to one line
            if "\n" in desc:
                desc = desc.split("\n")[0]
            _sub_system += f"\n- **{fn['name']}**: {desc}"
    messages.append({"role": "system", "content": _sub_system})
    if current_depth >= max_depth:
        messages.append({
            "role": "system",
            "content": (
                f"You are at depth {current_depth}/{max_depth}. "
                "You CANNOT spawn further sub-agents -- you are a leaf worker. "
                "Complete your task directly."
            ),
        })
    else:
        messages.append({
            "role": "system",
            "content": (
                f"You are at depth {current_depth}/{max_depth}. "
                "You MAY spawn sub-agents (spawn_agent) for independent subtasks. "
                "Your sub-agents will be at depth {current_depth + 1}.\n"
                "\n"
                "Orchestrator rules (same as the parent):\n"
                "- Once you spawn sub-agents, you are an orchestrator for THOSE tasks. "
                "Do NOT duplicate, pre-empt, or race the work you delegated.\n"
                "- After spawning, your job is to monitor (agent_status), collect "
                "(collect_agent/collect_any), and extend (agent_extend). You may also "
                "work on independent tasks you did NOT delegate.\n"
                "- Poll sub-agents every turn. If all are running, report status and wait.\n"
                "- Extend proactively: after ~10 turns of work, grant +10 more with "
                "agent_extend (max 35 total).\n"
                "- Use collect_any to grab the first ready result and keep the pipeline moving.\n"
                "- Only cancel if an agent repeats the same error 3+ times or exhausts 35 turns.\n"
                "- Track task IDs, what each agent is doing, and collection status "
                "in your scratchpad (write_scratchpad)."
            ),
        })
    if shared_context:
        # Cap shared_context to avoid blowing the first API call
        if len(shared_context) > _SHARED_CONTEXT_CAP:
            shared_context = shared_context[:_SHARED_CONTEXT_CAP] + "\n...[shared_context truncated to {_SHARED_CONTEXT_CAP} chars]"
        messages.append({
            "role": "system",
            "content": (
                "Shared context from parent agent (API contracts, coordination info, etc.):\n"
                + shared_context
            ),
        })
    # Inject task_id so the sub-agent can identify itself for agent_inbox / agent_handoff
    if task_id:
        messages.append({
            "role": "system",
            "content": (
                f"Your agent task ID is: {task_id}\n"
                f"Your parent (orchestrator) task ID is: {parent_task_id}\n"
                f"Use your own ID with agent_inbox(). Use the parent ID as target with agent_handoff(target=...)."
            ),
        })
    # Cap task to prevent oversized user message from blowing the first API call
    if len(task) > _TASK_CAP:
        task = task[:_TASK_CAP] + "\n...[task truncated to {_TASK_CAP} chars]"
    messages.append({"role": "user", "content": task})

    turn_count = 0
    tool_calls_made = 0
    local_modified: set[str] = set()
    local_cache: dict = {}
    _scratchpad: str = ""  # tracked locally so SubAgentResult can return it

    # Override tool dispatch for sub-agent: block spawn_agent/agent_status/collect_agent
    # to prevent recursion.  We monkey-patch only what the sub-agent sees.

    import json
    import requests

    # -- Helper: write sub-agent report to file (step 4) --
    def _write_report(result: SubAgentResult) -> SubAgentResult:
        import os as _os
        _os.makedirs("reports", exist_ok=True)
        _label = task_id  # unique per sub-agent
        _path = f"reports/{_label}.md"
        try:
            with open(_path, "w", encoding="utf-8") as _f:
                _f.write(f"# Sub-agent report: {_label}\n\n")
                _f.write(f"**Task**: {task[:200]}\n\n")
                _f.write(f"**Success**: {result.success}\n")
                _f.write(f"**Turns**: {result.turns_used}\n")
                _f.write(f"**Tool calls**: {result.tool_calls_made}\n")
                if result.error:
                    _f.write(f"**Error**: {result.error}\n\n")
                _f.write(f"\n## Result\n\n{result.content}\n")
                if result.scratchpad:
                    _f.write(f"\n## Scratchpad\n\n{result.scratchpad}\n")
            # Auto-cleanup: keep only the most recent N reports by mtime
            _MAX_REPORTS = 20
            _report_dir = "reports"
            try:
                _all_reports = sorted(
                    [_os.path.join(_report_dir, f) for f in _os.listdir(_report_dir) if f.endswith(".md")],
                    key=_os.path.getmtime,
                )
                for _old in _all_reports[:-_MAX_REPORTS]:
                    _os.remove(_old)
            except OSError:
                pass  # best-effort cleanup
            # Auto-cleanup: keep only the most recent N stderr logs by mtime
            _MAX_LOGS = 20
            _log_dir = "logs"
            try:
                _all_logs = sorted(
                    [_os.path.join(_log_dir, f) for f in _os.listdir(_log_dir) if f.endswith("_stderr.log")],
                    key=_os.path.getmtime,
                )
                for _old in _all_logs[:-_MAX_LOGS]:
                    _os.remove(_old)
            except OSError:
                pass  # best-effort cleanup
            # Smart inline preview: prioritize findings/structured content over preamble.
            # Scan for finding markers; if found, show those. Otherwise fall back to head truncation.
            _content = result.content
            _finding_markers = [
                "## Findings", "## Issues", "| Severity |", "| File |",
                "### [FAIL] CRITICAL", "### [FAIL] HIGH", "### [RED]", "### [YELLOW]",
                "**CRITICAL**", "**HIGH**", "| Priority |",
            ]
            _best_idx = len(_content)  # fallback: show from start
            for _marker in _finding_markers:
                _idx = _content.find(_marker)
                if _idx != -1 and _idx < _best_idx:
                    _best_idx = _idx
            _preview_start = 0
            if _best_idx < len(_content) and _best_idx > 0:
                # Found a marker -- show from 50 chars before it, or from start if near beginning
                _preview_start = max(0, _best_idx - 50)
                _preview = _content[_preview_start:_preview_start + 500]
            else:
                _preview = _content[:300]
            _truncated = len(_content) > len(_preview) + _preview_start if _best_idx < len(_content) else len(_content) > 300
            result.content = f"[report: {_path}] {'...' if _best_idx > 50 else ''}{_preview}{'...' if _truncated else ''}"
        except OSError:
            pass  # can't write report; return inline as fallback
        return result

    # -- Helper: build SubAgentResult with current local state --
    def _make_result(success: bool, content: str, error: str | None = None) -> SubAgentResult:
        return SubAgentResult(
            success=success, content=content,
            turns_used=turn_count, tool_calls_made=tool_calls_made,
            scratchpad=_scratchpad, error=error,
        )

    # main loop -- no hard turn cap.  Termination is based on:
    # cancellation, hung detection, error loops, or reaching the runtime's
    # _ABSOLUTE_MAX_TURNS (default 200, configurable via extend_turns).
    _HUNG_TIMEOUT = 300  # seconds without a tool call before considered hung
    _ERROR_LOOP_THRESHOLD = 3  # same error fingerprint this many times -> stuck
    _extension_requested = False
    _last_tool_time = time.monotonic()
    _recent_errors: list[str] = []  # fingerprints of last few errors

    # Absolute safety cap comes from the runtime, not hardcoded here.
    _safety_cap = max_turns * 10 if max_turns < 50 else 200
    while True:
        turn_count += 1
        # Safety net: runtime-level absolute cap (default 200)
        if turn_count > _safety_cap:
            _restore_plan()
            return _write_report(_make_result(
                success=False,
                content=f"Sub-agent exhausted absolute safety cap ({_safety_cap} turns).",
                error="Exhausted safety cap",
            ))
        # -- Progress detection (step 3): hung check --
        _now = time.monotonic()
        if turn_count > 1 and _now - _last_tool_time > _HUNG_TIMEOUT:
            _restore_plan()
            return _write_report(_make_result(
                success=False,
                content=f"Sub-agent hung: no tool calls for {_HUNG_TIMEOUT}s.",
                error="Hung (no tool calls)",
            ))
        # -- Progress detection: error loop --
        if len(_recent_errors) >= _ERROR_LOOP_THRESHOLD and len(set(_recent_errors[-_ERROR_LOOP_THRESHOLD:])) == 1:
            _restore_plan()
            return _write_report(_make_result(
                success=False,
                content=f"Sub-agent stuck: same error '{_recent_errors[-1]}' {_ERROR_LOOP_THRESHOLD}x consecutively.",
                error=f"Error loop: {_recent_errors[-1]}",
            ))
        # Re-read max_turns from runtime (parent may have extended it) -- used
        # only for the soft budget warning, not as a hard cap.
        runtime_ctx = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime_ctx is not None and task_id:
            updated = runtime_ctx.get_max_turns(task_id)
            if updated is not None and updated > max_turns:
                max_turns = updated
                _extension_requested = False

        # --- Budget warning: auto-ping orchestrator when running low ---
        if not _extension_requested and max_turns - turn_count <= 2:
            _extension_requested = True
            try:
                from tools.agent_messages import _agent_handoff
                _agent_handoff({
                    "type": "status.error",
                    "from": task_id,
                    "result": {
                        "need_extension": True,
                        "task_id": task_id,
                        "turns_remaining": max_turns - turn_count,
                        "message": f"Sub-agent has {max_turns - turn_count} turns left. Please extend."
                    }
                }, write_gate, read_gate)
            except APIError as exc:
                print(f"  WARNING: auto-ping failed: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"  WARNING: auto-ping failed: {exc}", file=sys.stderr, flush=True)

        if cancel_event is not None and cancel_event.is_set():
            _restore_plan()
            return SubAgentResult(
                success=False,
                content="Cancelled by parent.",
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error="Cancelled",
            )


        # --- Pre-call snapshot: tell the orchestrator we're about to call the LLM ---
        _pre_snap = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if _pre_snap is not None and task_id:
            _pre_snap.update_snapshot(
                task_id=task_id, turn=turn_count, turns_budget=max_turns,
                last_action="calling_llm", last_tool="", last_tool_summary="",
                scratchpad_snippet=_scratchpad[-200:] if _scratchpad else "",
                tool_calls_made=tool_calls_made,
            )

        # Call the LLM -- stream to TUI subagent pane or stderr if config.stream is set
        on_token = None
        if config.stream:
            # Accumulator for periodic streaming-snapshot updates
            _stream_buf: list[str] = []
            _stream_count = [0]  # mutable counter for closure
              # tokens between streaming-snapshot updates
            _snap_rt = getattr(_TOOL_CONTEXT, "_agent_runtime", None)

            def _make_streaming_wrapper(inner_on_token):
                def _wrapped(t: str) -> None:
                    inner_on_token(t)
                    _stream_count[0] += 1
                    if _stream_count[0] % _STREAM_SNAP_EVERY == 0 and _snap_rt is not None and task_id:
                        _stream_buf.append(t)
                        _snap_rt.update_snapshot(
                            task_id=task_id, turn=turn_count, turns_budget=max_turns,
                            last_action="thinking", last_tool="", last_tool_summary="",
                            scratchpad_snippet=_scratchpad[-200:] if _scratchpad else "",
                            tool_calls_made=tool_calls_made,
                            thought_snippet="".join(_stream_buf[-6:])[-200:],  # last ~6 chunks, capped
                            streamed_tokens=_stream_count[0],
                        )
                    # Emit thought to Electron UI callback (batched every ~20 tokens to avoid spam)
                    if subagent_callback and _stream_count[0] % 20 == 0:
                        try:
                            subagent_callback("thought", {
                                "task_id": task_id,
                                "text": "".join(_stream_buf[-20:]),
                            })
                        except Exception:
                            _sub_log.debug("subagent callback 'thought' failed", exc_info=True)
                return _wrapped

            # Write streaming tokens to log file for debugging
            import os as _os
            _os.makedirs("logs", exist_ok=True)
            _log_path = f"logs/sub_agent_{task_id}.log"
            def _on_token_log(t: str) -> None:
                try:
                    with open(_log_path, "a", encoding="utf-8") as _lf:
                        _lf.write(t)
                except OSError:
                    pass  # best-effort logging
            on_token = _make_streaming_wrapper(_on_token_log)
        try:
            # Sub-agents use a cheaper/faster model for worker tasks.
            # Save and restore to avoid mutating the shared config object.
            _saved_model = config.model
            _saved_key = config.api_key
            config.model = config.sub_agent_model
            if config.sub_agent_api_key:
                config.api_key = config.sub_agent_api_key

            # --- Pre-call token budget check ---
            # Estimate total tokens and force-prune if over safety ceiling.
            from memory.memory import _total_tokens, _compress_tool_results, _prune_by_tokens, _summarize_pruned, _strip_orphaned_tool_results
            est = _total_tokens(messages)
            if est > _SUB_SAFETY_TOKEN_CEILING:
                messages, _ = _compress_tool_results(messages, keep_recent=_SUB_COMPRESSION_KEEP_RECENT)
                messages, pruned = _prune_by_tokens(
                    messages, max_tokens=_SUB_SAFETY_TOKEN_CEILING, max_messages=_SUB_MAX_MESSAGES,
                )
                if pruned:
                    summary = _summarize_pruned(pruned)
                    if summary:
                        messages.insert(0, {"role": "user", "content": summary})
            # Always strip orphaned tool messages -- pruning can delete
            # assistant(tool_calls) but leave orphaned tool results.
            messages = _strip_orphaned_tool_results(messages)
            # Clear API message cache -- _strip_orphaned_tool_results creates
            # a new list, and Python may reuse the memory address, causing
            # the cache to serve stale cleaned messages with orphaned tools.
            from api import clear_api_cache
            clear_api_cache()

            # --- Turn-budget awareness: when running low, force the agent to wrap up ---
            _turns_left = max_turns - turn_count
            if _turns_left <= 3 and _turns_left > 0:
                messages.append({
                    "role": "system",
                    "content": (
                        f"WARNING: WRAP-UP: You have {_turns_left} turns remaining. "
                        "STOP reading files. STOP investigating further. "
                        "You MUST write your findings NOW.\n"
                        "1. Compile all findings you've gathered into a structured report.\n"
                        "2. Use write_file to write the report to disk (reports/<your-task-id>.md).\n"
                        "3. In your final message, present the findings summary as a table with "
                        "Severity, File, Line, Issue, and Fix columns.\n"
                        "4. Do NOT read any more files. Work only from what you already have."
                    ),
                    "_transient": True,
                })
            elif _turns_left <= 0:
                messages.append({
                    "role": "system",
                    "content": (
                        "? FINAL TURN: You are out of turns. "
                        "Do NOT call any tools that read files. "
                        "Write your report with whatever findings you have, even if incomplete. "
                        "A partial report is better than no report."
                    ),
                    "_transient": True,
                })

            # --- Communication nudge: only inject if agent has unread inbox messages ---
            # Previously injected every 3 turns (~500 tokens each), but most agents
            # never use the messaging system. Now opt-in: only nudge when there's
            # actually something to read.
            if turn_count % _TURN_INTERVAL_COMM_NUDGE == 0:
                _runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
                _has_unread = False
                if _runtime is not None and task_id:
                    inbox = _runtime.get_inbox(task_id)
                    # Check if there are messages the agent hasn't seen yet
                    _last_inbox_count = getattr(
                        _TOOL_CONTEXT, f"_agent_{task_id}_last_inbox_count", 0
                    )
                    if len(inbox) > _last_inbox_count:
                        _has_unread = True
                    # Also check global broadcasts
                    all_msgs = getattr(_runtime, "messages", None)
                    if all_msgs is not None:
                        _last_bcast = getattr(
                            _TOOL_CONTEXT, f"_agent_{task_id}_last_bcast_count", 0
                        )
                        if len(all_msgs) > _last_bcast:
                            _has_unread = True
                if _has_unread:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[COMMUNICATION NUDGE] You have unread messages.\n"
                            "1. Check your **agent_inbox** for direct messages.\n"
                            "2. Check **agent_read** for broadcast messages.\n"
                            "3. Send a **status.heartbeat** via agent_handoff summarizing progress.\n"
                        ),
                        "_transient": True,
                    })

            msg = call_llm(
                messages, config,
                session=requests,
                cancel_event=cancel_event,
                on_token=on_token,
            )
            config.model = _saved_model
            config.api_key = _saved_key
        except APIError as exc:
            # API error with structured detail
            detail = f"API call failed [{exc.status_code}]: {exc.body}"
            _restore_plan()
            return SubAgentResult(
                success=False,
                content=detail,
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error=f"APIError({exc.status_code})",
            )
        except Exception as exc:
            # On 400, dump message structure for debugging
            detail = f"API call failed: {exc}"
            if "tool" in str(exc).lower() and "preceding" in str(exc).lower():
                roles = [m.get("role", "?") for m in messages]
                tc_ids = [m.get("tool_call_id", "-")[:12] if m.get("role") == "tool" else "-" for m in messages]
                detail += f" | Roles: {roles}"
                detail += f" | ToolIDs: {tc_ids}"
            _restore_plan()
            return SubAgentResult(
                success=False,
                content=detail,
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error=detail,
            )

        if cancel_event is not None and cancel_event.is_set():
            _restore_plan()
            return SubAgentResult(
                success=False,
                content="Cancelled by parent.",
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error="Cancelled",
            )

        if msg is None:
            _restore_plan()
            return SubAgentResult(
                success=False,
                content="No response from LLM.",
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error="No response",
            )

        # No tool calls -> final answer
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            messages.append(msg)
            content = msg.get("content", "")
            _restore_plan()
            return _write_report(_make_result(
                success=True,
                content=content[:2000],
            ))

        # Execute tool calls
        messages.append(msg)
        for tc in tool_calls:
            tool_calls_made += 1
            fn = tc.get("function", {})
            name = fn.get("name", "")


            # Emit tool_start to Electron UI callback
            if subagent_callback:
                try:
                    _fn = tc.get("function", {})
                    _tname = _fn.get("name", "")
                    _targs = _fn.get("arguments", "{}")
                    subagent_callback("tool_start", {"task_id": task_id, "tool_name": _tname, "tool_args": _targs})
                except Exception:
                    _sub_log.debug("subagent callback 'tool_start' failed", exc_info=True)

            # --- Depth guard: block spawn/status/collect at max depth ---
            if current_depth >= max_depth and name in ("spawn_agent", "agent_status", "collect_agent", "collect_any", "agent_extend"):
                from tools import ToolResult as TR
                result = TR(
                    success=False,
                    content=(
                        f"Tool '{name}' is not available at max depth ({max_depth}). "
                        "Complete your assigned task directly."
                    ),
                )
            else:
                # Execute with the parent's gates (sub-agent shares workspace)
                try:
                    # Check local cache for read-only tools
                    raw_args = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        parsed = {}

                    if name in _CACHEABLE:
                        cache_key = json.dumps([name, parsed], sort_keys=True)
                        if cache_key in local_cache:
                            result = local_cache[cache_key]
                        else:
                            result = execute_tool(tc, write_gate, read_gate)
                            local_cache[cache_key] = result
                    else:
                        result = execute_tool(tc, write_gate, read_gate)

                    # -- Progress tracking (step 3) --
                    _last_tool_time = time.monotonic()
                    if not result.success and result.content:
                        _fingerprint = result.content[:60].strip().lower()
                        _recent_errors.append(_fingerprint)
                        if len(_recent_errors) > 20:
                            _recent_errors[:] = _recent_errors[-20:]
                    else:
                        _recent_errors.clear()  # success resets error streak

                    # Track files modified
                    if name in ("write_file", "edit_file") and result.success:
                        filepath = parsed.get("path", "")
                        if filepath:
                            local_modified.add(filepath)
                    # Track scratchpad content
                    if name == "write_scratchpad" and result.success:
                        _scratchpad = parsed.get("content", "")

                    # Check cancellation after each tool execution
                    if cancel_event is not None and cancel_event.is_set():
                        _restore_plan()
                        return SubAgentResult(
                            success=False,
                            content="Cancelled by parent during tool execution.",
                            turns_used=turn_count,
                            tool_calls_made=tool_calls_made,
                            scratchpad=_scratchpad,
                            error="Cancelled during tool execution",
                        )
                except APIError as exc:
                    from tools import ToolResult as TR
                    result = TR(
                        success=False,
                        content=f"API error during tool execution [{exc.status_code}]: {exc.body}",
                    )
                except Exception as exc:
                    from tools import ToolResult as TR
                    result = TR(
                        success=False,
                        content=f"Tool execution error: {exc}",
                    )

            # Append tool result message (truncate oversized content)
            r_content = result.content
            if len(r_content) > _MAX_TOOL_RESULT_CHARS:
                r_content = r_content[:_MAX_TOOL_RESULT_CHARS] + f"\n... (truncated at {_MAX_TOOL_RESULT_CHARS} chars)"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps({"success": result.success, "content": r_content}),
            })

            # Emit tool_end to Electron UI callback
            if subagent_callback:
                try:
                    subagent_callback("tool_end", {
                        "task_id": task_id,
                        "tool_name": name,
                        "ok": result.success,
                        "content": result.content[:500] if result.content else "",
                    })
                except Exception:
                    _sub_log.debug("subagent callback 'tool_end' failed", exc_info=True)

        # --- Auto-snapshot: record status every turn so the parent can peek
        #     with agent_status without waiting for a heartbeat. ---
        runtime_snap = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime_snap is not None and task_id:
            # 'name' and 'result' are the last tool executed in the loop above
            last_tool_name = name
            last_summary = result.content[:120] if result.content else ""
            last_err = result.content[:120] if not result.success else None
            runtime_snap.update_snapshot(
                task_id=task_id,
                turn=turn_count,
                turns_budget=max_turns,
                last_action="tool_call",
                last_tool=last_tool_name,
                last_tool_summary=last_summary,
                scratchpad_snippet=_scratchpad[-200:] if _scratchpad else "",
                tool_calls_made=tool_calls_made,
                last_error=last_err,
            )

        # --- Memory management: compress old tool results and prune to
        #     keep the sub-agent's context within API limits.  Without
        #     this, sub-agents grow unbounded messages and hit 400 errors.
        #     Run every turn (not just every 5th) once we have enough
        #     messages, because a single turn can produce massive tool output.
        if len(messages) > _SUB_COMPRESSION_THRESHOLD:
            from memory.memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned, _strip_orphaned_tool_results
            messages, _ = _compress_tool_results(messages, keep_recent=_SUB_COMPRESSION_KEEP_RECENT)
            messages, pruned = _prune_by_tokens(
                messages, max_tokens=_SUB_MAX_TOKENS, max_messages=_SUB_MAX_MESSAGES,
            )
            messages = _strip_orphaned_tool_results(messages)
            if pruned:
                from memory.memory import _summarize_pruned
                summary = _summarize_pruned(pruned)
                if summary:
                    messages.insert(0, {"role": "user", "content": summary})

    # -- Safety cap exhausted (step 3: should only happen if hung/loop detection fails) --
    _restore_plan()
    return _write_report(_make_result(
        success=False,
        content=f"Sub-agent hit safety cap ({max_turns} turns).",
        error="Safety cap exhausted",
    ))


# ---------------------------------------------------------------------------
# Sub-agent system prompt
# ---------------------------------------------------------------------------

_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a sub-agent -- a worker that completes one specific task "
    "delegated to you by a parent agent.\n"
    "\n"
    "Behavior:\n"
    "- Work on the task you were given. Do not expand scope.\n"
    "- Use tools as needed to complete your work.\n"
    "- When done, produce a concise final answer summarizing what you did, "
    "what files you changed, and any results.\n"
    "- Do not ask clarifying questions -- just do the work and report back.\n"
    "- If you encounter an error you cannot fix, report it clearly in your "
    "final answer rather than looping.\n"
    "- Keep your response focused and under 2000 characters.\n"
    "\n"
    "COMPLETION CRITERIA -- You are DONE when:\n"
    "1. You have WRITTEN your findings/report to disk using write_file.\n"
    "2. Your final message contains a summary table of findings.\n"
    "3. If you cannot complete the full task, write a PARTIAL report with "
    "whatever you have. An incomplete report is always better than nothing.\n"
    "\n"
    "REPORT FORMAT -- For audit/investigation tasks, structure output as:\n"
    "## Findings\n"
    "| Severity | File | Line | Issue | Fix |\n"
    "|----------|------|------|-------|-----|\n"
    "Put findings FIRST. Explanation, methodology, and preamble go AFTER "
    "the findings table. This ensures the orchestrator sees results even "
    "if your output is truncated.\n"
    "\n"
    "SCOUT-THEN-DRILL -- When analyzing many files:\n"
    "1. FIRST: use search_files or find_symbol to identify candidate "
    "files/locations (cheap, 1-2 turns).\n"
    "2. SECOND: read only the 3-5 most relevant files deeply (expensive).\n"
    "3. THIRD: write findings immediately -- do NOT keep reading more files.\n"
    "Never try to read ALL files in a codebase. You will run out of context "
    "and produce nothing.\n"
    "\n"
    "You MAY spawn sub-agents (spawn_agent) to parallelize independent "
    "subtasks. When you do, follow the same orchestrator rules as the "
    "parent: monitor, collect, and extend -- but do NOT duplicate work "
    "you've delegated. Your sub-agents inherit your depth + 1.\n"
    "\n"
    "Communication (you are NOT working in isolation):\n"
    "- **agent_message** -- broadcast progress updates to the orchestrator "
    "  and all sibling agents. Use this whenever you start a new phase, "
    "  finish a piece of work, or discover something others might need.\n"
    "- **agent_inbox** -- check your own inbox every turn! The orchestrator "
    "  or siblings may have sent you handoffs, coordination messages, or "
    "  requests. Your task_id is injected below -- use it with agent_inbox.\n"
    "- **agent_handoff** -- send typed structured results to specific agents "
    "  (or to subscribers). Use 'status.heartbeat' every ~3 turns to tell "
    "  the orchestrator what you're doing (summary, progress, next step). "
    "  Use 'handoff.result' to pass structured output to a sibling that "
    "  needs it.\n"
    "- **agent_read** -- read broadcast messages from siblings and parent "
    "  (flat broadcast stream, different from your typed inbox).\n"
    "- **agent_subscribe** -- narrow your inbox to specific message types "
    "  if you only care about certain handoffs.\n"
    "- **Do not work silently.** The orchestrator cannot see your tool "
    "  output unless you broadcast or handoff. Send a heartbeat every 3 "
    "  turns so the orchestrator knows you're alive and making progress.\n"
    "- **Check inbox first each turn:** call agent_inbox before doing "
    "  other work. The orchestrator may have sent you important updates.\n"
    "\n"
    "Shared file coordination (CRITICAL when siblings work on same files):\n"
    "- **Before editing ANY file**, broadcast via agent_message: which file, "
    "  what section/lines you're about to change, and why. Wait 1-2 turns "
    "  for siblings to acknowledge before writing.\n"
    "- **After editing a file**, broadcast what you changed (file, lines, "
    "  summary). This lets siblings rebase their mental model.\n"
    "- **Check agent_read EVERY turn** for sibling broadcasts about file "
    "  modifications. If a sibling just edited a file you need, read it "
    "  fresh before making your own edits.\n"
    "- **When reading a file for the first time**, broadcast which file "
    "  you're reading and what section you're responsible for. This "
    "  establishes ownership.\n"
    "- **If you see a conflict** (you need to edit the same region as a "
    "  sibling), use agent_handoff to send your changes to them instead, "
    "  or wait for them to finish before you start.\n"
)
