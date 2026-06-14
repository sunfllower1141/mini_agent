#!/usr/bin/env python3
"""
agent_spawn.py -- sub-agent spawning tools for mini_agent.

Tools: spawn_agent

spawn_agent launches sub-agents in background threads and returns
task_ids immediately (never blocks the parent).
"""

from __future__ import annotations

import threading
import time
import uuid

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from agents.agent_runtime import AgentRuntime, SubAgentResult






def _parse_max_turns(raw) -> "int | ToolResult":


    """Parse max_turns from args (soft cap -- loop self-governs)."""


    try:


        mt = int(raw)


    except (TypeError, ValueError):


        return ToolResult(


            success=False,


            content=f"max_turns must be an integer, got: {raw}",


        )


    return max(1, mt)








def _spawn_one(


    task: str,


    config,


    runtime: AgentRuntime,


    wg: WriteSafetyGate,


    rg: ReadSafetyGate,


    max_turns: int,


    *,


    cancel_event: threading.Event | None = None,


    visible: bool = False,


    shared_context: str = "",


    subscriptions: list[str] | None = None,


    reserved_files: list[str] | None = None,


    parent_depth: int = 0,


    max_depth: int = 3,


    parent_task_id: str = "",


    subagent_callback: callable | None = None,


) -> str:


    """Spawn a single sub-agent thread. Returns the task_id."""


    from tools import _TOOL_CONTEXT


    from agents.sub_agent import run_sub_agent





    task_id = str(uuid.uuid4())[:8]


    if cancel_event is None:


        cancel_event = threading.Event()





    # Generate a short human-readable name from the task text


    words = task.strip().split()


    short_name = "_".join(w for w in words[:3] if w.isalnum() or w in ("-",))


    if not short_name:


        short_name = "agent"


    short_name = short_name.lower()[:24]





    # Capture the sub-agent callback NOW (before the thread starts) to avoid


    # a race condition: the parent clears _TOOL_CONTEXT._subagent_callback


    # after run_agent_turn returns, which may happen before the sub-agent


    # thread gets to read it.  Capturing here in the parent thread guarantees


    # we have the right value.


    if subagent_callback is None:


        subagent_callback = getattr(_TOOL_CONTEXT, "_subagent_callback", None)


    # DEBUG: log whether the callback was captured


    import sys as _sys_debug


    _sys_debug.__stderr__.write(f"[DEBUG _spawn_one] task={short_name} callback={'SET' if subagent_callback else 'NONE'}\n")





    def _runner() -> None:


        import sys as _sys


        sub_cb = subagent_callback  # captured in parent thread, no race


        # ---- Redirect stderr to buffer to prevent UI corruption ----


        from io import StringIO as _StringIO


        _stderr_buf = _StringIO()


        _saved_stderr = _sys.stderr


        _sys.stderr = _stderr_buf


        # ---- end stderr redirect ----


        # Set depth context for tools called by this sub-agent


        current_depth = parent_depth + 1


        _TOOL_CONTEXT._agent_depth = current_depth


        _TOOL_CONTEXT._agent_max_depth = max_depth


        _TOOL_CONTEXT._agent_task_id = task_id


        original_stream = config.stream


        # Notify Electron via sub-agent callback (if wired)


        if sub_cb:


            sub_cb("start", {"task_id": task_id, "parent_id": parent_task_id, "name": short_name, "desc": task})


        try:


            if visible:


                config.stream = True


                # Log to file for debugging


                import os as _os


                _os.makedirs("logs", exist_ok=True)


                _log_path = f"logs/sub_agent_{task_id}.log"


                with open(_log_path, "a", encoding="utf-8") as _lf:


                    _lf.write(f"\n--- [sub {task_id}] START: {task[:200]} ---\n")


            else:


                config.stream = False  # suppress raw token streaming for invisible sub-agents


            result = run_sub_agent(


                task=task,


                config=config,


                write_gate=wg,


                read_gate=rg,


                max_turns=max_turns,


                cancel_event=cancel_event,


                parent_depth=parent_depth,


                max_depth=max_depth,


                shared_context=shared_context,


                task_id=task_id,


                parent_task_id=parent_task_id,


                subagent_callback=sub_cb,


            )


            runtime.store_result(task_id, result)


            # Notify Electron via sub-agent callback (if wired)


            if sub_cb:


                sub_cb("end", {"task_id": task_id, "ok": result.success, "content": result.content[:500]})


        finally:


            config.stream = original_stream


            # ---- Restore stderr + flush buffer to disk only if non-empty ----


            _sys.stderr = _saved_stderr


            _stderr_text = _stderr_buf.getvalue()


            _stderr_buf.close()


            if _stderr_text:


                import os as _os


                _os.makedirs("logs", exist_ok=True)


                with open(f"logs/sub_agent_{task_id}_stderr.log", "w", encoding="utf-8") as _lf:


                    _lf.write(_stderr_text)


            # ---- end restore stderr ----


            # Restore agent context so parent isn't polluted


            _TOOL_CONTEXT._agent_depth = parent_depth


            _TOOL_CONTEXT._agent_task_id = parent_task_id


            # Always release file reservations, even on crash.


            # store_result (called in try) would normally do this, but if


            # run_sub_agent raises an unhandled exception, files are leaked.


            from tools import release_all_files


            release_all_files(task_id)





    thread = threading.Thread(target=_runner, daemon=True, name=f"subagent-{task_id}")


    # Determine parent task_id (passed explicitly by caller)


    parent_id = parent_task_id


    runtime.register(task_id, thread, cancel_event, max_turns,


                     label=short_name, parent_task_id=parent_id)


    # Reserve files if specified


    if reserved_files:


        from tools import _FILE_RESERVATIONS, _FILE_RESERVATIONS_LOCK


        with _FILE_RESERVATIONS_LOCK:


            for f in reserved_files:


                _FILE_RESERVATIONS[f] = task_id


    # Set subscriptions if provided


    if subscriptions is not None:


        runtime.set_subscriptions(task_id, subscriptions)


    thread.start()


    return task_id








@_register("spawn_agent")


def _spawn_agent(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:


    """Spawn a sub-agent to work on a task in the background.





    Returns a task_id immediately.  Use agent_status to poll or


    collect_agent to block until completion.





    Supports batch spawn via 'tasks' (list of task strings) in addition


    to single 'task' spawn.





    Set 'synchronous'=true to block until the sub-agent completes and


    return its result directly (agent-as-tool pattern).


    """


    from tools import _TOOL_CONTEXT





    # --- batch spawn (tasks=list) ---


    tasks_list = args.get("tasks", None)


    if tasks_list is not None:


        if not isinstance(tasks_list, list):


            return ToolResult(


                success=False,


                content="'tasks' must be a list of task description strings.",


            )


        valid_tasks = [t for t in tasks_list if isinstance(t, str) and t.strip()]


        if not valid_tasks:


            return ToolResult(


                success=False,


                content="No sub-agents could be spawned: 'tasks' must be a non-empty list.",


            )





        shared_context = args.get("shared_context", "")


        max_turns = _parse_max_turns(args.get("max_turns", _DEFAULT_MAX_TURNS))


        if isinstance(max_turns, ToolResult):


            return max_turns


        visible = args.get("visible", False)


        subscriptions = args.get("subscriptions", None)





        runtime: AgentRuntime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


        if runtime is None:


            return ToolResult(


                success=False,


                content="Agent runtime not initialized. Multi-agent support is unavailable.",


            )





        config = getattr(_TOOL_CONTEXT, "_agent_config", None)


        if config is None:


            return ToolResult(


                success=False,


                content="Agent config not available in tool context.",


            )





        parent_task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")


        synchronous = args.get("synchronous", False)


        task_ids = []


        _max_concurrent = getattr(getattr(_TOOL_CONTEXT, "_agent_config", None), "sub_agent_max_concurrent", _MAX_CONCURRENT)


        for task in valid_tasks:


            if runtime.active_count >= _max_concurrent:


                break


            tid = _spawn_one(task, config, runtime, wg, rg, max_turns,


                             cancel_event=None, visible=visible,


                             shared_context=shared_context,


                             subscriptions=subscriptions,


                             parent_depth=getattr(_TOOL_CONTEXT, "_agent_depth", 0),


                             max_depth=getattr(_TOOL_CONTEXT, "_agent_max_depth", 3),


                             parent_task_id=parent_task_id)


            task_ids.append(tid)





        if not task_ids:


            return ToolResult(


                success=False,


                content=f"Too many sub-agents running ({runtime.active_count} active, "


                        f"max {_MAX_CONCURRENT}). Wait for some to complete before spawning more.",


            )





        # --- synchronous mode: block until all complete (agent-as-tool pattern) ---


        if synchronous:


            results = []


            for tid in task_ids:


                def _ready(tid=tid):


                    return runtime.get_status(tid) != "running"


                with runtime._condition:


                    runtime._condition.wait_for(_ready, timeout=_COLLECT_TIMEOUT)


                status = runtime.get_status(tid)


                if status == "completed":


                    result = runtime.get_result(tid)


                    if result is not None:


                        runtime._collected.add(tid)


                        results.append(result)


            if not results:


                return ToolResult(


                    success=False,


                    content="Synchronous spawn: no agents completed in time.",


                )


            lines = [f"Synchronous batch: {len(results)}/{len(task_ids)} completed:"]


            for r in results:


                lines.append(f"  - [{r.success}] {r.content[:200]}")


            return ToolResult(success=True, content="\n".join(lines))





        return ToolResult(


            success=True,


            content=(


                f"Spawned {len(task_ids)} sub-agent(s): {', '.join(task_ids)}.\n"


                f"Use agent_status or collect_agent to check results.\n"


                f"Use collect_any() to grab the fastest completion."


            ),


        )





    # --- single task spawn ---


    task = args.get("task", "")


    if not task.strip():


        return ToolResult(


            success=False,


            content="Missing required parameter: 'task' (the task description for the sub-agent).",


        )





    max_turns = _parse_max_turns(args.get("max_turns", _DEFAULT_MAX_TURNS))


    if isinstance(max_turns, ToolResult):


        return max_turns


    visible = args.get("visible", False)


    shared_context = args.get("shared_context", "")


    subscriptions = args.get("subscriptions", None)





    runtime: AgentRuntime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    if runtime is None:


        return ToolResult(


            success=False,


            content="Agent runtime not initialized. Multi-agent support is unavailable.",


        )





    config = getattr(_TOOL_CONTEXT, "_agent_config", None)


    _max_concurrent = getattr(config, "sub_agent_max_concurrent", _MAX_CONCURRENT)


    if runtime.active_count >= _max_concurrent:


        return ToolResult(


            success=False,


            content=(


                f"Too many sub-agents running ({runtime.active_count} active, "


                f"max {_MAX_CONCURRENT}). Wait for some to complete with "


                f"agent_status or collect_agent before spawning more."


            ),


        )





    config = getattr(_TOOL_CONTEXT, "_agent_config", None)


    if config is None:


        return ToolResult(


            success=False,


            content="Agent config not available in tool context.",


        )





    parent_task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")


    synchronous = args.get("synchronous", False)


    task_id = _spawn_one(task, config, runtime, wg, rg, max_turns,


                         cancel_event=None, visible=visible,


                         shared_context=shared_context,


                         subscriptions=subscriptions,


                         parent_depth=getattr(_TOOL_CONTEXT, "_agent_depth", 0),


                         max_depth=getattr(_TOOL_CONTEXT, "_agent_max_depth", 3),


                         parent_task_id=parent_task_id)





    # --- synchronous mode: block until complete (agent-as-tool pattern) ---


    if synchronous:


        def _ready():


            return runtime.get_status(task_id) != "running"


        with runtime._condition:


            runtime._condition.wait_for(_ready, timeout=_COLLECT_TIMEOUT)


        status = runtime.get_status(task_id)


        if status == "completed":


            result = runtime.get_result(task_id)


            if result is not None:


                runtime._collected.add(task_id)


                return ToolResult(


                    success=result.success,


                    content=(


                        f"Synchronous agent '{task_id}' completed:\n"


                        f"  Success: {result.success}\n"


                        f"  Turns: {result.turns_used}\n"


                        f"  Content:\n{result.content}"


                    ),


                )


        return ToolResult(


            success=False,


            content=f"Synchronous agent '{task_id}' did not complete in time.",


        )





    return ToolResult(


        success=True,


        content=(


            f"Spawned sub-agent '{task_id}' with {max_turns} turn budget.\n"


            f"Task: {task[:200]}{'...' if len(task) > 200 else ''}\n"


            f"Use agent_status('{task_id}') to poll or "


            f"collect_agent('{task_id}') to block until done."


        ),


    )








@_summarize("spawn_agent")


def _spawn_agent_summary(args: dict) -> str:


    tasks_list = args.get("tasks")


    if tasks_list and isinstance(tasks_list, list):


        return f"spawn_agent(tasks=[{len(tasks_list)} items])"


    task = args.get("task", "?")


    preview = task[:60]


    if len(task) > 60:


        preview += "..."


    return f"spawn_agent(\"{preview}\")"








# ---------------------------------------------------------------------------


