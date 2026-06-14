#!/usr/bin/env python3
"""
agent_ops.py -- agent lifecycle and utility tools for mini_agent.

Tools: agent_extend, agent_cancel, wait_for_agent, restore_file,
       session_stats, recall_turn, remember, read_image

agent_extend extends a sub-agent turn budget.
agent_cancel terminates a running sub-agent.
wait_for_agent blocks until a sub-agent completes or needs attention.
restore_file reverts a file to its backup.
session_stats returns cost and runtime statistics.
recall_turn replays a previous turn for debugging.
remember captures a learning to project knowledge.
read_image analyzes an image file via the LLM.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import threading
import time

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT
from tools.agent_collect import _format_collect_any
from agents.agent_runtime import AgentRuntime, SubAgentResult

# agent_extend


# ---------------------------------------------------------------------------





@_register("agent_extend")


def _agent_extend(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Extend the turn budget of a running sub-agent."""


    from tools import _TOOL_CONTEXT





    task_id = args.get("task_id", "")


    if not task_id:


        return ToolResult(


            success=False,


            content="Missing required parameter: 'task_id'.",


        )





    additional = args.get("additional", 10)


    try:


        additional = int(additional)


    except (TypeError, ValueError):


        return ToolResult(


            success=False,


            content=f"'additional' must be an integer, got: {additional}",


        )


    if additional < 1:


        return ToolResult(


            success=False,


            content="'additional' must be a positive integer.",


        )





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





    if status == "completed":


        return ToolResult(


            success=True,


            content=f"Sub-agent '{task_id}' has already completed.",


        )





    ok = runtime.extend_turns(task_id, additional)


    if not ok:


        return ToolResult(


            success=False,


            content=f"Failed to extend turns for '{task_id}'.",


        )





    new_max = runtime.get_max_turns(task_id)


    return ToolResult(


        success=True,


        content=f"Extended sub-agent '{task_id}' by +{additional} turns "


                f"(new max: {new_max}).",


    )








@_summarize("agent_extend")


def _agent_extend_summary(args: dict) -> str:


    return f"agent_extend({args.get('task_id', '?')}, +{args.get('additional', 10)})"








# ---------------------------------------------------------------------------


# agent_cancel


# ---------------------------------------------------------------------------





@_register("agent_cancel")


def _agent_cancel(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Cancel a running sub-agent by setting its cancel event."""


    from tools import _TOOL_CONTEXT





    task_id = args.get("task_id", "")


    if not task_id:


        return ToolResult(


            success=False,


            content="Missing required parameter: 'task_id'.",


        )





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





    if status == "completed":


        return ToolResult(


            success=True,


            content=f"Sub-agent '{task_id}' has already completed.",


        )





    ok = runtime.cancel(task_id)


    if not ok:


        return ToolResult(


            success=False,


            content=f"Failed to cancel sub-agent '{task_id}'.",


        )





    return ToolResult(


        success=True,


        content=f"Sub-agent '{task_id}' cancellation requested.",


    )








@_summarize("agent_cancel")


def _agent_cancel_summary(args: dict) -> str:


    return f"agent_cancel({args.get('task_id', '?')})"





# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------


# restore_file -- session undo


# ---------------------------------------------------------------------------





# _BACKUPS lives in file_ops.py (shared with _backup_before_write)


from tools.file_ops import _BACKUPS


import shutil


import os as _os_restore








@_register("restore_file")


def _restore_file(args: dict, wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Restore a file from its session backup (undo the last write/edit)."""


    path = args["path"]


    safety_result = wg.check(path)


    if not safety_result.allowed:


        return ToolResult(


            success=False,


            content=f"Restore blocked by safety layer: {safety_result.reason}",


        )


    resolved = safety_result.resolved_path





    if resolved not in _BACKUPS:


        return ToolResult(


            success=False,


            content=f"No backup available for '{resolved}'. Only files modified this session can be restored.",


            hint="No backup exists. Either the file hasn't been modified this session, or it was already restored.",


        )





    backup_path = _BACKUPS[resolved]


    try:


        shutil.copy2(backup_path, resolved)


        del _BACKUPS[resolved]


        from tools import _MODIFIED_FILES, _MODIFIED_FILES_LOCK


        with _MODIFIED_FILES_LOCK:


            _MODIFIED_FILES.discard(safety_result.resolved_path)


        return ToolResult(


            success=True,


            content=f"Restored '{resolved}' from backup ({_os_restore.path.basename(backup_path)}).",


        )


    except Exception as e:


        return ToolResult(


            success=False,


            content=f"Error restoring '{resolved}': {e}",


        )








@_summarize("restore_file")


def _restore_file_summary(args: dict) -> str:


    return f"restore_file({args.get('path', '?')})"








# ---------------------------------------------------------------------------


# recall_turn -- retrieve a summary of a past turn


# ---------------------------------------------------------------------------





@_register("session_stats")


def _session_stats(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Show session statistics: turns, tokens, context usage, active sub-agents."""


    turn_history = getattr(_TOOL_CONTEXT, "_turn_history", None) or {}


    turns_used = len(turn_history)


    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)


    token_count = 0


    if memory_store is not None:


        token_count = memory_store.token_count


    CONTEXT_BUDGET = 800_000


    pct_used = (token_count / CONTEXT_BUDGET * 100) if CONTEXT_BUDGET else 0





    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    active_agents = len(runtime.get_running_ids()) if runtime else 0


    completed_agents = len(runtime.results) if runtime else 0





    # --- Cache hit stats (from api.py:_report_cache_hit) ---


    cache_stats = getattr(_TOOL_CONTEXT, "_cache_stats", None) or {}


    cache_hits = cache_stats.get("hits", 0)


    cache_misses = cache_stats.get("misses", 0)


    cache_calls = cache_stats.get("calls", 0)


    input_tokens = cache_stats.get("input_tokens", 0)


    output_tokens = cache_stats.get("output_tokens", 0)


    total_cache_tokens = cache_hits + cache_misses


    hit_rate_pct = (cache_hits / total_cache_tokens * 100) if total_cache_tokens > 0 else 0





    # --- Cost savings ---


    provider = getattr(_TOOL_CONTEXT, "_provider", None) or "deepseek"


    try:


        from core.config import PROVIDER_DEFAULTS


        pd = PROVIDER_DEFAULTS.get(provider)


        if pd and pd.input_price > 0:


            # Cost if all input tokens were cache misses


            cost_without_cache = input_tokens / 1_000_000 * pd.input_price


            # Actual cost: cache_hit tokens at hit_price, cache_miss tokens at input_price


            actual_input_cost = (


                cache_hits / 1_000_000 * pd.cache_hit_price +


                cache_misses / 1_000_000 * pd.input_price


            )


            # Add output cost


            output_cost = output_tokens / 1_000_000 * pd.output_price


            actual_total = actual_input_cost + output_cost


            saved = cost_without_cache - actual_input_cost


        else:


            saved = 0.0


            actual_total = 0.0


    except Exception:


        saved = 0.0


        actual_total = 0.0





    lines = [


        f"Turns used:    {turns_used}",


        f"Context tokens: {token_count} / {CONTEXT_BUDGET} ({pct_used:.1f}% used)",


        f"Sub-agents:     {active_agents} active, {completed_agents} completed",


    ]


    if cache_calls > 0:


        lines.append(


            f"API calls:      {cache_calls} | "


            f"input {input_tokens:,} tok | output {output_tokens:,} tok"


        )


        lines.append(


            f"Cache hit rate: {hit_rate_pct:.1f}% "


            f"({cache_hits:,} cached / {total_cache_tokens:,} tokens)"


        )


        if saved > 0:


            lines.append(


                f"Cost:          ${actual_total:.4f} "


                f"(saved ${saved:.4f} via cache)"


            )


        elif actual_total > 0:


            lines.append(f"Cost:          ${actual_total:.4f}")


    plan = getattr(_TOOL_CONTEXT, "_plan_steps", [])


    plan_done = getattr(_TOOL_CONTEXT, "_plan_done", set())


    if plan:


        done_count = len(plan_done)  # plan_done stores 0-based indices, just count


        lines.append(f"Plan:           {done_count}/{len(plan)} steps done")





    return ToolResult(success=True, content="\\n".join(lines))








@_summarize("session_stats")


def _session_stats_summary(args: dict) -> str:


    return "session_stats"








@_register("recall_turn")


def _recall_turn(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Return a summary of what happened on a given turn number."""


    turn = args.get("turn", 0)


    if not isinstance(turn, int) or turn < 1:


        return ToolResult(success=False, content="turn must be a positive integer")





    history = _TOOL_CONTEXT._turn_history


    if turn not in history:


        available = sorted(history.keys()) if history else []


        return ToolResult(


            success=True,


            content=(


                f"No record of turn {turn}. "


                + (f"Available turns: {available}" if available else "No turns recorded yet.")


            ),


        )





    return ToolResult(success=True, content=f"Turn {turn}:\n{history[turn]}")








@_summarize("recall_turn")


def _recall_turn_summary(args: dict) -> str:


    return f"recall_turn({args.get('turn', '?')})"








# ---------------------------------------------------------------------------


# remember -- store project knowledge in the persistent knowledge base


# ---------------------------------------------------------------------------








@_register("remember")


def _remember(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Store a project-level learning that persists across sessions.





    Saved to the ``project_knowledge`` table in the session SQLite DB.


    Auto-categorizes the learning if no category is provided.


    Returns a summary of what was stored.


    """


    topic = args.get("topic", "")


    detail = args.get("detail", "")


    category = args.get("category", "")


    if not topic.strip():


        return ToolResult(


            success=False,


            content="Missing required parameter: 'topic' (short topic label for this learning).",


        )





    # Auto-categorize if no category provided


    if not category:


        try:


            from tools.failure_learning import suggest_category, KNOWLEDGE_CATEGORIES


            category = suggest_category(topic, detail)


            if category not in KNOWLEDGE_CATEGORIES:


                category = "general"


        except ImportError:


            category = "general"





    memory_store = getattr(_TOOL_CONTEXT, "_memory_store", None)


    topic_preview = topic[:200] + ("..." if len(topic) > 200 else "")


    detail_preview = detail[:200] + ("..." if len(detail) > 200 else "")


    if memory_store is not None:


        try:


            conn = memory_store._get_conn()


            conn.execute(


                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",


                (category, topic, detail),


            )


            conn.commit()


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





    # Fallback: try SQLite directly via scratchpad_path


    db_path = getattr(_TOOL_CONTEXT, "scratchpad_path", None)


    if db_path:


        try:


            import sqlite3


            conn = sqlite3.connect(db_path)


            conn.execute("PRAGMA busy_timeout=5000")


            conn.execute(


                "INSERT INTO project_knowledge (category, summary, detail) VALUES (?, ?, ?)",


                (category, topic, detail),


            )


            conn.commit()


            conn.close()


        except Exception as e:


            return ToolResult(


                success=True,


                content=f"Remember noted, but DB fallback failed: {e}",


            )


        return ToolResult(


            success=True,


            content=(


                f"Stored in project knowledge (DB fallback) [{category}]:\\n"


                f"  Topic: {topic_preview}\\n"


                f"  Detail: {detail_preview}"


            ),


        )





    return ToolResult(


        success=True,


        content=(


            f"Remember noted (no persistent storage available) [{category}]:\\n"


            f"  Topic: {topic_preview}"


        ),


    )








@_summarize("remember")


def _remember_summary(args: dict) -> str:


    topic = args.get("topic", "?")


    preview = topic[:60]


    if len(topic) > 60:


        preview += "..."


    return f"remember(\"{preview}\")"








# ---------------------------------------------------------------------------


# read_image -- describe an image using GPT-4o


# ---------------------------------------------------------------------------





_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"})








def _guess_mime_type(path: str) -> str:


    """Guess MIME type from file extension."""


    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""


    return {


        "png": "image/png",


        "jpg": "image/jpeg",


        "jpeg": "image/jpeg",


        "gif": "image/gif",


        "webp": "image/webp",


        "bmp": "image/bmp",


        "tiff": "image/tiff",


    }.get(ext, "image/png")








@_register("read_image")


def _read_image(args: dict, _wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult:


    """Read an image file, send it to GPT-4o, and return a text description."""


    import base64


    import requests





    path = args.get("path", "")


    if not path:


        return ToolResult(success=False, content="Missing required parameter: 'path'.")





    # Safety check: ensure the file is within the workspace


    sr = rg.check(path)


    if not sr.allowed:


        return ToolResult(success=False, content=f"Read blocked: {sr.reason}")





    # Validate file exists and is an image


    import os as _os


    resolved = sr.resolved_path


    if not _os.path.isfile(resolved):


        return ToolResult(success=False, content=f"File not found: {path}")





    ext = _os.path.splitext(resolved)[1].lower()


    if ext not in _IMAGE_EXTENSIONS:


        return ToolResult(


            success=False,


            content=f"Unsupported image format: {ext}. Supported: {sorted(_IMAGE_EXTENSIONS)}",


        )





    mime_type = _guess_mime_type(resolved)





    # Read and base64-encode the image


    try:


        with open(resolved, "rb") as f:


            image_bytes = f.read()


        b64_data = base64.b64encode(image_bytes).decode("utf-8")


    except Exception as e:


        return ToolResult(success=False, content=f"Failed to read image: {e}")





    # Get API key from tool context


    openai_api_key = _TOOL_CONTEXT.openai_api_key or ""


    if not openai_api_key:


        return ToolResult(


            success=False,


            content="OpenAI API key not configured. Set OPENAI_API_KEY env var or openai_api_key in .mini_agent.toml.",


        )





    # Build the GPT-4o request


    payload = {


        "model": "gpt-4o",


        "messages": [


            {


                "role": "user",


                "content": [


                    {


                        "type": "text",


                        "text": "Describe this image in detail. What do you see?",


                    },


                    {


                        "type": "image_url",


                        "image_url": {


                            "url": f"data:{mime_type};base64,{b64_data}",


                            "detail": "auto",


                        },


                    },


                ],


            }


        ],


        "max_tokens": 1000,


    }





    try:


        resp = requests.post(


            "https://api.openai.com/v1/chat/completions",


            headers={


                "Authorization": f"Bearer {openai_api_key}",


                "Content-Type": "application/json",


            },


            json=payload,


            timeout=60,


        )


        if resp.status_code != 200:


            return ToolResult(


                success=False,


                content=f"OpenAI API error ({resp.status_code}): {resp.text[:500]}",


            )


        data = resp.json()


        description = data["choices"][0]["message"]["content"]


        return ToolResult(success=True, content=description)


    except requests.exceptions.Timeout:


        return ToolResult(success=False, content="OpenAI API request timed out (60s).")


    except Exception as e:


        return ToolResult(success=False, content=f"OpenAI API request failed: {e}")








# ---------------------------------------------------------------------------


# wait_for_agent


# ---------------------------------------------------------------------------





@_register("wait_for_agent")


def _wait_for_agent(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:


    """Block until any sub-agent completes, needs extension, or timeout expires.





    Wakes on three events:


    1. Any agent completes (returns result immediately)


    2. Any agent exhausts its turn budget (returns so orchestrator can extend)


    3. New messages arrive in the orchestrator's inbox





    Sleeps with exponential backoff (1s->2s->4s...->30s) between polls


    to minimize LLM token burn.


    """


    from tools import _TOOL_CONTEXT





    task_ids = args.get("task_ids", [])


    timeout = args.get("timeout", 120)





    if not task_ids:


        return ToolResult(success=False, content="Missing required parameter: 'task_ids'.")





    runtime: AgentRuntime | None = getattr(_TOOL_CONTEXT, "_agent_runtime", None)


    if runtime is None:


        return ToolResult(success=False, content="Agent runtime not initialized.")





    last_inbox_count = len(runtime.messages)  # track new messages





    def _check_once() -> str | None:


        """Return reason to wake, or None if still waiting."""


        nonlocal last_inbox_count


        # Check for completed agents


        for tid in task_ids:


            s = runtime.get_status(tid)


            if s == "completed":


                result = runtime.get_result(tid)


                if result is not None:


                    runtime._collected.add(tid)


                    return "completed:" + tid


            # Check for hung/error agents via snapshot timestamps


            snap = runtime.get_snapshot(tid)


            if snap and s == "running":


                age_s = time.monotonic() - snap["timestamp"]


                if age_s > 300:  # 5 min since last snapshot -> likely hung


                    return "hung:" + tid


                if snap.get("last_error") and snap["turn"] > 3:


                    return "error:" + tid


        # Check for new inbox messages


        if len(runtime.messages) > last_inbox_count:


            last_inbox_count = len(runtime.messages)


            return "new_message"


        return None





    # Check immediately


    reason = _check_once()


    if reason is not None:


        if reason.startswith("completed:"):


            tid = reason.split(":", 1)[1]


            return _format_collect_any(tid, runtime.get_result(tid))


        if reason.startswith("hung:") or reason.startswith("error:"):


            tid = reason.split(":", 1)[1]


            return ToolResult(


                success=False,


                content=f"Agent '{tid}' may be stuck ({reason.split(':',1)[0]}). Check agent_status for details. Use agent_extend or agent_cancel.",


            )


        return ToolResult(


            success=False,


            content=f"Agent needs attention: {reason}. Use agent_status to check.",


        )





    deadline = time.time() + timeout


    delay = 1.0





    while time.time() < deadline:


        reason = _check_once()


        if reason is not None:


            if reason.startswith("completed:"):


                tid = reason.split(":", 1)[1]


                return _format_collect_any(tid, runtime.get_result(tid))


            if reason in ("new_message",):


                return ToolResult(


                    success=False,


                    content="New message(s) in inbox. Check agent_inbox.",


                )


            tid = reason.split(":", 1)[1] if ":" in reason else ""


            return ToolResult(


                success=False,


                content=f"Agent '{tid}' needs attention: {reason}. Use agent_status to check.",


            )





        time.sleep(min(delay, deadline - time.time()))


        delay = min(delay * 2, 30.0)





    still_running = [tid for tid in task_ids if runtime.get_status(tid) == "running"]


    return ToolResult(


        success=False,


        content=(


            f"Timeout after {timeout}s. Still running: {still_running}. "


            "Use agent_extend then retry."


        ),


    )








@_summarize("wait_for_agent")


def _wait_for_agent_summary(args: dict) -> str:


    tids = args.get("task_ids", [])


    timeout = args.get("timeout", 120)


    return f"wait_for_agent([{len(tids)} ids], {timeout}s)"





@_summarize("read_image")


def _read_image_summary(args: dict) -> str:


    path = args.get("path", "?")


    return f"read_image({path})"


