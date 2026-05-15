#!/usr/bin/env python3
"""
sub_agent.py — sub-agent engine for mini_agent multi-agent support.

Provides:
    SubAgentResult  — structured result from a completed sub-agent run
    AgentRuntime    — thread-safe registry of running sub-agent tasks
    run_sub_agent   — spawns an isolated agent loop in a background thread

A sub-agent gets its own message list, tool cache, and scratchpad.
It shares the parent's workspace, safety gates, and API config.
The runtime registry is designed to be extended later for inter-agent
communication (inboxes), dependency tracking, and persistent agents.
"""

from __future__ import annotations

import threading
import uuid

from safety import ReadSafetyGate, WriteSafetyGate
from agent_runtime import SubAgentResult, AgentRuntime
from api import APIError, call_deepseek, truncate_content


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
    tui_queue=None,       # Queue for TUI subagent pane streaming
    tui_task_id: str = "",  # task_id for TUI streaming prefix
    task_id: str = "",     # task_id for direct runtime lookup (avoids O(N) scan)
    parent_task_id: str = "",  # orchestrator task_id for agent_handoff targeting
) -> SubAgentResult:
    """Run a sub-agent loop in the current thread (called from a background thread).

    The sub-agent gets:
    - A fresh messages list (system prompt + task as user message)
    - Its own tool cache
    - Its own _MODIFIED_FILES tracking
    - Its own scratchpad (in-memory only — no SQLite for sub-agents)

    Sub-agents CAN spawn further sub-agents up to *max_depth*.
    Current depth is *parent_depth* + 1.  Tools are blocked when at max_depth.

    Returns a SubAgentResult with success, content, and metadata.
    """
    current_depth = parent_depth + 1
    from tools import (
        execute_tool, clear_tool_cache, tool_summary,
        _TOOL_CACHE, _MODIFIED_FILES, _CACHEABLE, _TOOL_CONTEXT,
    )
    from tools.schema import TOOLS
    from prompt import build_system_prompt

    # Thread-local agent ID for file reservation enforcement
    from tools.file_ops import _current_agent_id as _agent_tl
    _agent_tl.task_id = task_id

    # --- build messages for sub-agent ---
    messages: list[dict] = [
        {"role": "system", "content": _SUB_AGENT_SYSTEM_PROMPT},
        {"role": "system", "content": build_system_prompt(config)},
    ]
    if current_depth >= max_depth:
        messages.append({
            "role": "system",
            "content": (
                f"You are at depth {current_depth}/{max_depth}. "
                "You CANNOT spawn further sub-agents — you are a leaf worker. "
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

    # main loop — uses while + dynamic max_turns re-read so parent can extend budget
    _extension_requested = False  # only ping once when running low
    while turn_count < max_turns:
        turn_count += 1
        # Re-read max_turns from runtime (parent may have extended it)
        runtime_ctx = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime_ctx is not None and task_id:
            updated = runtime_ctx.get_max_turns(task_id)
            if updated is not None and updated > max_turns:
                max_turns = updated
                _extension_requested = False  # reset so we can ping again if needed

        # --- Budget warning: auto-ping orchestrator when running low ---
        if not _extension_requested and max_turns - turn_count <= 2:
            _extension_requested = True
            try:
                from tools.agent_ops import _agent_handoff
                _agent_handoff({
                    "type": "status.error",
                    "from": task_id,
                    "result": {
                        "need_extension": True,
                        "task_id": task_id,
                        "turns_remaining": max_turns - turn_count,
                        "message": f"Sub-agent has {max_turns - turn_count} turns left. Please extend."
                    }
                }, None, None)
            except APIError:
                pass  # best-effort, don't crash the sub-agent
            except Exception:
                pass  # best-effort, don't crash the sub-agent

        if cancel_event is not None and cancel_event.is_set():
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

        # Call the LLM — stream to TUI subagent pane or stderr if config.stream is set
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
                return _wrapped

            if tui_queue is not None:
                def _on_token_sub(t: str) -> None:
                    tui_queue.put(("sub_token", tui_task_id, t))
                on_token = _make_streaming_wrapper(_on_token_sub)
            else:
                import sys as _sys
                def _on_token_stderr(t: str) -> None:
                    _sys.stderr.write(t)
                    _sys.stderr.flush()
                on_token = _make_streaming_wrapper(_on_token_stderr)
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
            from memory import _total_tokens, _compress_tool_results, _prune_by_tokens, _summarize_pruned, _strip_orphaned_tool_results
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
            # Always strip orphaned tool messages — pruning can delete
            # assistant(tool_calls) but leave orphaned tool results.
            messages = _strip_orphaned_tool_results(messages)
            # Clear API message cache — _strip_orphaned_tool_results creates
            # a new list, and Python may reuse the memory address, causing
            # the cache to serve stale cleaned messages with orphaned tools.
            from api import clear_api_cache
            clear_api_cache()

            # --- Communication nudge: every 3 turns, remind the agent to coordinate ---
            if turn_count % 3 == 0:
                messages.append({
                    "role": "user",
                    "content": (
                        "[COMMUNICATION NUDGE] You have been working for {t} turns.\n"
                        "1. Check your **agent_inbox** for messages from the orchestrator or siblings.\n"
                        "2. Check **agent_read** for broadcast messages.\n"
                        "3. Send a **status.heartbeat** via agent_handoff summarizing progress.\n"
                        "4. If editing shared files, broadcast intent via **agent_message**.\n"
                        "5. If a sibling works on the same file, coordinate via **agent_handoff**.\n"
                    ).format(t=turn_count),
                    "_transient": True,
                })

            msg = call_deepseek(
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
            return SubAgentResult(
                success=False,
                content=detail,
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error=detail,
            )

        if cancel_event is not None and cancel_event.is_set():
            return SubAgentResult(
                success=False,
                content="Cancelled by parent.",
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error="Cancelled",
            )

        if msg is None:
            return SubAgentResult(
                success=False,
                content="No response from LLM.",
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
                error="No response",
            )

        # No tool calls → final answer
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            messages.append(msg)
            content = msg.get("content", "")
            return SubAgentResult(
                success=True,
                content=content[:2000],  # cap to avoid blowing parent context
                turns_used=turn_count,
                tool_calls_made=tool_calls_made,
                scratchpad=_scratchpad,
            )

        # Execute tool calls
        messages.append(msg)
        for tc in tool_calls:
            tool_calls_made += 1
            fn = tc.get("function", {})
            name = fn.get("name", "")

            # Stream tool start to TUI
            if tui_queue is not None:
                tui_queue.put(("sub_tool", "start", name, getattr(_TOOL_CONTEXT, "_agent_task_id", "")))

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
                r_content = r_content[:_MAX_TOOL_RESULT_CHARS] + f"\n… (truncated at {_MAX_TOOL_RESULT_CHARS} chars)"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps({"success": result.success, "content": r_content}),
            })
            # Stream tool end to TUI
            if tui_queue is not None:
                ok = result.success
                detail = result.content[:100] if result.content else ""
                tui_queue.put(("sub_tool", "end", name, ok, detail))

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
            from memory import _compress_tool_results, _prune_by_tokens, _summarize_pruned, _strip_orphaned_tool_results
            messages, _ = _compress_tool_results(messages, keep_recent=_SUB_COMPRESSION_KEEP_RECENT)
            messages, pruned = _prune_by_tokens(
                messages, max_tokens=_SUB_MAX_TOKENS, max_messages=_SUB_MAX_MESSAGES,
            )
            messages = _strip_orphaned_tool_results(messages)
            if pruned:
                from memory import _summarize_pruned
                summary = _summarize_pruned(pruned)
                if summary:
                    messages.insert(0, {"role": "user", "content": summary})

    # Exhausted turns
    return SubAgentResult(
        success=False,
        content="Sub-agent exceeded turn budget.",
        turns_used=turn_count,
        tool_calls_made=tool_calls_made,
        scratchpad=_scratchpad,
        error="Turn budget exhausted",
    )


# ---------------------------------------------------------------------------
# Sub-agent system prompt
# ---------------------------------------------------------------------------

_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a sub-agent — a worker that completes one specific task "
    "delegated to you by a parent agent.\n"
    "\n"
    "Behavior:\n"
    "- Work on the task you were given. Do not expand scope.\n"
    "- Use tools as needed to complete your work.\n"
    "- When done, produce a concise final answer summarizing what you did, "
    "what files you changed, and any results.\n"
    "- Do not ask clarifying questions — just do the work and report back.\n"
    "- If you encounter an error you cannot fix, report it clearly in your "
    "final answer rather than looping.\n"
    "- Keep your response focused and under 2000 characters.\n"
    "\n"
    "You MAY spawn sub-agents (spawn_agent) to parallelize independent "
    "subtasks. When you do, follow the same orchestrator rules as the "
    "parent: monitor, collect, and extend — but do NOT duplicate work "
    "you've delegated. Your sub-agents inherit your depth + 1.\n"
    "\n"
    "Communication (you are NOT working in isolation):\n"
    "- **agent_message** — broadcast progress updates to the orchestrator "
    "  and all sibling agents. Use this whenever you start a new phase, "
    "  finish a piece of work, or discover something others might need.\n"
    "- **agent_inbox** — check your own inbox every turn! The orchestrator "
    "  or siblings may have sent you handoffs, coordination messages, or "
    "  requests. Your task_id is injected below — use it with agent_inbox.\n"
    "- **agent_handoff** — send typed structured results to specific agents "
    "  (or to subscribers). Use 'status.heartbeat' every ~3 turns to tell "
    "  the orchestrator what you're doing (summary, progress, next step). "
    "  Use 'handoff.result' to pass structured output to a sibling that "
    "  needs it.\n"
    "- **agent_read** — read broadcast messages from siblings and parent "
    "  (flat broadcast stream, different from your typed inbox).\n"
    "- **agent_subscribe** — narrow your inbox to specific message types "
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
