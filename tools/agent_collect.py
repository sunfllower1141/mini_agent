#!/usr/bin/env python3
"""
agent_collect.py -- sub-agent status and collection tools for mini_agent.

Tools: agent_status, collect_agent, collect_any

agent_status polls for sub-agent completion without blocking.
collect_agent blocks until a sub-agent finishes.
collect_any returns the first finishing sub-agent from a set.
"""

from __future__ import annotations

import time

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from agents.agent_runtime import AgentRuntime, SubAgentResult

# agent_status


# ---------------------------------------------------------------------------





@_register("agent_status")


def _agent_status(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Check the status of a sub-agent without blocking.





    Returns 'running', 'completed' with a result summary, or 'not_found'.


    """


    from tools import _TOOL_CONTEXT





    task_id = args.get("task_id", "")


    if not task_id:


        return ToolResult(success=False, content="Missing required parameter: 'task_id'.")





    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    if runtime is None:


        return ToolResult(


            success=False,


            content="Agent runtime not initialized.",


        )





    status = runtime.get_status(task_id)


    if status == "not_found":


        return ToolResult(


            success=True,


            content=f"Sub-agent '{task_id}' not found (may have completed or never existed).",


        )





    if status == "running":


        active = runtime.active_count


        content = f"Sub-agent '{task_id}' is still running. ({active} total active)"


        # Include auto-snapshot if available


        snap = runtime.get_snapshot(task_id)


        if snap is not None:


            age = ""  # snapshot timestamps are monotonic; just show turn info


            content += (


                f"\n  Turn: {snap['turn']}/{snap['turns_budget']}"


                f" | Tools called: {snap['tool_calls_made']}"


                f"\n  Last tool: {snap['last_tool'] or '(none)'}"


            )


            if snap["last_tool_summary"]:


                summary = snap["last_tool_summary"]


                if len(summary) > 120:


                    summary = summary[:120] + "..."


                content += f"\n  Result: {summary}"


            if snap.get("last_action") in ("thinking", "calling_llm"):


                content += f"\n  Status: {snap['last_action']}"


                if snap.get("streamed_tokens"):


                    content += f" ({snap['streamed_tokens']} tokens streamed)"


                if snap.get("thought_snippet"):


                    content += f"\n  Thought: ...{snap['thought_snippet'][-120:]}"


            if snap["last_error"]:


                content += f"\n  WARNING: Last error: {snap['last_error'][:120]}"


            if snap["scratchpad_snippet"]:


                snippet = snap["scratchpad_snippet"]


                if len(snippet) > 150:


                    snippet = snippet[-150:]


                content += f"\n  Scratchpad: {snippet}"


            # If last heartbeat was a while ago, nudge


            import time as _time


            age_s = _time.monotonic() - snap["timestamp"]


            if age_s > 60:


                content += f"\n  WARNING: Last snapshot {age_s:.0f}s ago -- agent may be stuck or between turns."


        else:


            content += "\n  (no snapshot yet -- agent may not have completed a turn)"


        return ToolResult(success=True, content=content)





    # Completed


    result = runtime.get_result(task_id)


    runtime._collected.add(task_id)


    if result is None:


        return ToolResult(success=True, content=f"Sub-agent '{task_id}' completed but result not found.")





    summary = (


        f"Sub-agent '{task_id}': completed.\n"


        f"  Success: {result.success}\n"


        f"  Turns used: {result.turns_used}\n"


        f"  Tool calls: {result.tool_calls_made}\n"


        f"  Summary: {result.content[:500]}{'...' if len(result.content) > 500 else ''}"


    )


    if result.error:


        summary += f"\n  Error: {result.error}"





    return ToolResult(success=True, content=summary)








@_summarize("agent_status")


def _agent_status_summary(args: dict) -> str:


    return f"agent_status({args.get('task_id', '?')})"








# ---------------------------------------------------------------------------


# collect_agent


# ---------------------------------------------------------------------------





_COLLECT_TIMEOUT = 30  # seconds to wait for sub-agent completion (kept moderate


                       # so parent agent can check for user interjections between


                       # polls -- use agent_extend + collect_agent again if needed)








@_register("collect_agent")


def _collect_agent(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Block until a sub-agent completes, then return its full result."""


    from tools import _TOOL_CONTEXT





    task_id = args.get("task_id", "")


    if not task_id:


        return ToolResult(success=False, content="Missing required parameter: 'task_id'.")





    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    if runtime is None:


        return ToolResult(


            success=False,


            content="Agent runtime not initialized.",


        )





    status = runtime.get_status(task_id)


    if status == "not_found":


        return ToolResult(


            success=False,


            content=f"Sub-agent '{task_id}' not found.",


        )





    if status == "running":


        # Wait for completion using condition.wait_for with predicate,


        # which atomically checks status under the lock to avoid lost wakeups.


        def _completed():


            return runtime.get_status(task_id) != "running"


        with runtime._condition:


            runtime._condition.wait_for(_completed, timeout=_COLLECT_TIMEOUT)





        # If still running after timeout, report back -- don't cancel.


        # The parent can extend turns or collect again later.


        final_status = runtime.get_status(task_id)


        if final_status == "running":


            return ToolResult(


                success=False,


                content=(


                    f"Sub-agent '{task_id}' is still running after {_COLLECT_TIMEOUT}s. "


                    "It continues in the background. Use agent_status, agent_extend, "


                    "or collect_agent again to wait longer."


                ),


            )





    result = runtime.get_result(task_id)


    runtime._collected.add(task_id)


    if result is None:


        return ToolResult(


            success=False,


            content=f"Sub-agent '{task_id}' completed but no result was stored.",


        )





    content = (


        f"Sub-agent '{task_id}' result:\n"


        f"  Success: {result.success}\n"


        f"  Turns used: {result.turns_used}\n"


        f"  Tool calls: {result.tool_calls_made}\n"


        f"  Content:\n{result.content}\n"


    )


    if result.scratchpad:


        content += f"  Scratchpad (final):\n{result.scratchpad[:500]}\n"


    if result.error:


        content += f"  Error: {result.error}\n"





    return ToolResult(


        success=result.success,


        content=content,


    )








@_summarize("collect_agent")


def _collect_agent_summary(args: dict) -> str:


    return f"collect_agent({args.get('task_id', '?')})"








# ---------------------------------------------------------------------------


# collect_any


# ---------------------------------------------------------------------------





_COLLECT_ANY_TIMEOUT = 60   # seconds to wait for any sub-agent (was 10s; 60s


                            # reduces orchestrator polling frequency and context


                            # bloat from repeated orchestration injections)








@_register("collect_any")


def _collect_any(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Collect the first sub-agent that finishes.





    If any have already completed, returns immediately.


    Otherwise polls until one completes or timeout.


    """


    from tools import _TOOL_CONTEXT





    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    if runtime is None:


        return ToolResult(


            success=False,


            content="Agent runtime not initialized.",


        )





    task_ids = args.get("task_ids", None)


    if task_ids is not None and not isinstance(task_ids, list):


        return ToolResult(


            success=False,


            content="'task_ids' must be a list of task ID strings.",


        )





    # Determine which tasks to check


    if task_ids:


        candidates = task_ids


    else:


        with runtime._lock:


            # Include both running tasks and completed results


            candidates = set(runtime.tasks.keys()) | set(runtime.results.keys())


            # Exclude already-collected results


            candidates -= runtime._collected





    if not candidates:


        return ToolResult(


            success=False,


            content="No sub-agents to collect.",


        )





    # Check for already-completed


    candidates = list(candidates)  # materialize for iteration


    for tid in candidates:


        status = runtime.get_status(tid)


        if status == "completed":


            result = runtime.get_result(tid)


            if result is not None:


                runtime._collected.add(tid)


                return _format_collect_any(tid, result)





    # Wait for any completion using condition.wait_for with predicate,


    # which atomically checks status under the lock to avoid lost wakeups.


    def _any_completed():


        for tid in candidates:


            if runtime.get_status(tid) == "completed":


                return True


        return False


    with runtime._condition:


        runtime._condition.wait_for(_any_completed, timeout=_COLLECT_ANY_TIMEOUT)





    for tid in candidates:


        status = runtime.get_status(tid)


        if status == "completed":


            result = runtime.get_result(tid)


            if result is not None:


                runtime._collected.add(tid)


                return _format_collect_any(tid, result)





    # Report which sub-agents are still running so the parent can retry


    still_running = [tid for tid in candidates if runtime.get_status(tid) == "running"]


    return ToolResult(


        success=False,


        content=(


            f"No sub-agent completed within {_COLLECT_ANY_TIMEOUT}s. "


            f"Still running: {still_running if still_running else 'none'}. "


            "Use collect_any again to retry."


        ),


    )








def _format_collect_any(task_id: str, result: SubAgentResult) -> ToolResult:


    """Format a collected sub-agent result."""


    content = (


        f"Sub-agent '{task_id}' finished first:\n"


        f"  Success: {result.success}\n"


        f"  Turns used: {result.turns_used}\n"


        f"  Tool calls: {result.tool_calls_made}\n"


        f"  Content:\n{result.content}\n"


    )


    if result.error:


        content += f"  Error: {result.error}\n"


    return ToolResult(success=result.success, content=content)








@_summarize("collect_any")


def _collect_any_summary(args: dict) -> str:


    tids = args.get("task_ids")


    if tids:


        return f"collect_any([{len(tids)} ids])"


    return "collect_any()"





# ---------------------------------------------------------------------------


