#!/usr/bin/env python3
"""
agent_patterns.py — multi-agent coordination pattern helpers.

Provides Python-API helpers (not tools) that the parent agent or
orchestrator can call to coordinate sub-agents:

    fan_out()     — spawn N workers from a list of task descriptions
    fan_in()      — collect all results from a list of task_ids
    pipeline()    — run stages in sequence, each receiving the prior's handoff
    barrier()     — block until all task_ids have sent coord.sync for a barrier
    scatter_gather() — fan-out with per-worker input slices
"""

from __future__ import annotations

import time
import threading

from agent_runtime import AgentRuntime, SubAgentResult
from safety import ReadSafetyGate, WriteSafetyGate
from tools import ToolResult, _register, _summarize, _TOOL_CONTEXT


def fan_out(
    descriptions: list[str],
    shared_input: dict | None = None,
    runtime: AgentRuntime | None = None,
    config=None,
    wg=None,
    rg=None,
    max_turns: int = 15,
    visible: bool = False,
    subscriptions: list[str] | None = None,
) -> list[str]:
    """Spawn N workers from a list of task descriptions.

    Returns a list of task_ids that can be passed to fan_in().

    Args:
        descriptions: List of task strings, one per worker.
        shared_input: Optional dict passed as shared_context to all workers.
        runtime: AgentRuntime instance (pulled from _TOOL_CONTEXT if None).
        config: AgentConfig instance.
        wg, rg: Safety gates.
        max_turns: Turn budget per worker.
        visible: Stream sub-agent output.
        subscriptions: Message types each worker subscribes to.

    Returns:
        List of task_id strings.
    """
    from tools import _TOOL_CONTEXT
    from tools.agent_ops import _spawn_one, _MAX_CONCURRENT

    if runtime is None:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            raise RuntimeError("Agent runtime not initialized.")

    if config is None:
        config = getattr(_TOOL_CONTEXT, "_agent_config", None)
        if config is None:
            raise RuntimeError("Agent config not available.")

    import json
    shared_ctx = ""
    if shared_input:
        shared_ctx = json.dumps(shared_input)

    task_ids = []
    for desc in descriptions:
        if runtime.active_count >= _MAX_CONCURRENT:
            break
        tid = _spawn_one(
            desc, config, runtime, wg, rg, max_turns,
            cancel_event=None, visible=visible,
            shared_context=shared_ctx,
            subscriptions=subscriptions,
        )
        task_ids.append(tid)

    return task_ids


def fan_in(
    task_ids: list[str],
    runtime: AgentRuntime | None = None,
    timeout: float = 120.0,
) -> list[SubAgentResult]:
    """Collect results from all task_ids. Blocks until all complete or timeout.

    Returns results in the same order as task_ids (None for timed-out tasks).
    """
    from tools import _TOOL_CONTEXT

    if runtime is None:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            raise RuntimeError("Agent runtime not initialized.")

    results: list[SubAgentResult | None] = [None] * len(task_ids)

    for i, tid in enumerate(task_ids):
        # Each task gets the full timeout independently — earlier tasks
        # no longer starve later ones.        # Use wait_for with predicate to avoid lost-wakeup race.
        def _ready(tid=tid):
            status = runtime.get_status(tid)
            return status != "running"

        with runtime._condition:
            runtime._condition.wait_for(_ready, timeout=timeout)

        status = runtime.get_status(tid)
        if status == "completed":
            results[i] = runtime.get_result(tid)
        elif status == "not_found":
            results[i] = None

    return results


def pipeline(
    stages: list[dict],
    runtime: AgentRuntime | None = None,
    config=None,
    wg=None,
    rg=None,
    max_turns: int = 15,
    timeout: float = 300.0,
) -> SubAgentResult | None:
    """Run stages in sequence, each receiving the prior stage's result.

    Each stage is a dict with:
        task: str             — task description
        subscriptions: list[str] — message types to subscribe to

    Each stage after the first subscribes to "handoff.result" and receives
    the previous stage's output via its inbox.

    Returns the final stage's SubAgentResult, or None if any stage fails.
    """
    from tools import _TOOL_CONTEXT
    from tools.agent_ops import _spawn_one

    if runtime is None:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            raise RuntimeError("Agent runtime not initialized.")

    if config is None:
        config = getattr(_TOOL_CONTEXT, "_agent_config", None)
        if config is None:
            raise RuntimeError("Agent config not available.")

    prev_result = None
    for i, stage in enumerate(stages):
        task = stage["task"]
        subs = stage.get("subscriptions", [])

        shared_ctx = ""
        if i > 0 and prev_result is not None:
            import json
            # Pass previous result as shared context
            shared_ctx = json.dumps({
                "previous_result": prev_result.to_dict(),
                "stage": i,
            })

        tid = _spawn_one(
            task, config, runtime, wg, rg, max_turns,
            cancel_event=None, visible=False,
            shared_context=shared_ctx,
        )
        runtime.set_subscriptions(tid, subs)

        # Wait for this stage to complete using wait_for to avoid lost wakeups
        def _stage_ready(tid=tid):
            return runtime.get_status(tid) != "running"

        with runtime._condition:
            runtime._condition.wait_for(_stage_ready, timeout=timeout)

        status = runtime.get_status(tid)
        if status == "completed":
            prev_result = runtime.get_result(tid)
        elif status == "not_found":
            prev_result = None
        else:
            runtime.cancel(tid)
            prev_result = None

        if prev_result is None or not prev_result.success:
            return prev_result

    return prev_result


def barrier(
    name: str,
    task_ids: list[str],
    runtime: AgentRuntime | None = None,
    timeout: float = 120.0,
) -> bool:
    """Block until all task_ids have sent a coord.sync message for *name*.

    Returns True if all agents reached the barrier, False on timeout.
    Uses condition.wait for event-driven wakeup instead of pure polling.
    """
    from tools import _TOOL_CONTEXT

    if runtime is None:
        runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
        if runtime is None:
            raise RuntimeError("Agent runtime not initialized.")

    total = len(task_ids)
    arrived: set[str] = set()

    deadline = time.monotonic() + timeout
    while len(arrived) < total and time.monotonic() < deadline:
        for tid in task_ids:
            if tid in arrived:
                continue
            inbox = runtime.get_inbox(tid)
            for msg in inbox:
                if msg.type == "coord.sync" and msg.payload.get("barrier") == name:
                    arrived.add(tid)
                    break
        if len(arrived) >= total:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Use condition.wait for event-driven wakeup instead of pure sleep.
        # store_result() notifies _condition when any agent completes, so
        # we wake on any change rather than polling blindly.
        with runtime._condition:
            runtime._condition.wait(timeout=min(0.2, remaining))

    return len(arrived) >= total


def scatter_gather(
    items: list,
    worker_task_template: str,
    runtime: AgentRuntime | None = None,
    config=None,
    wg=None,
    rg=None,
    max_turns: int = 15,
    timeout: float = 120.0,
    subscriptions: list[str] | None = None,
) -> list[SubAgentResult | None]:
    """Fan-out with per-worker input slices.

    Each worker gets one item from *items* injected into its task description.
    Uses shared_context to pass the item data.

    Args:
        items: List of items to distribute (one per worker).
        worker_task_template: Task description with "{item}" placeholder.
        subscriptions: Message types each worker subscribes to.
    """
    descriptions = [
        worker_task_template.replace("{item}", str(item))
        for item in items
    ]

    task_ids = fan_out(
        descriptions,
        shared_input=None,
        runtime=runtime,
        config=config,
        wg=wg,
        rg=rg,
        max_turns=max_turns,
        subscriptions=subscriptions,
    )

    if not task_ids:
        return []

    return fan_in(task_ids, runtime=runtime, timeout=timeout)


# ============================================================================
# Tool wrappers — registered as LLM-callable tools
# ============================================================================


@_register("fan_out")
def _fan_out(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Spawn N workers from a list of task descriptions.

    Required:
        descriptions: list[str] — one task string per worker.

    Optional:
        shared_input: dict — injected as shared_context to all workers.
        max_turns: int — turn budget per worker (default 15).
        visible: bool — stream sub-agent output (default false).
        subscriptions: list[str] — message types each worker subscribes to.
    """
    descriptions = args.get("descriptions", [])
    if not descriptions:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'descriptions' (list[str]).",
        )

    shared_input = args.get("shared_input", None)
    max_turns = args.get("max_turns", 15)
    visible = args.get("visible", False)
    subscriptions = args.get("subscriptions", None)

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    if config is None:
        return ToolResult(success=False, content="Agent config not available.")

    try:
        task_ids = fan_out(
            descriptions,
            shared_input=shared_input,
            runtime=runtime,
            config=config,
            wg=_wg,
            rg=_rg,
            max_turns=max_turns,
            visible=visible,
            subscriptions=subscriptions,
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"fan_out failed: {exc}")

    return ToolResult(
        success=True,
        content=f"Spawned {len(task_ids)} workers: {task_ids}",
    )


@_summarize("fan_out")
def _fan_out_summary(args: dict) -> str:
    descs = args.get("descriptions", [])
    return f"fan_out({len(descs)} workers)"


@_register("fan_in")
def _fan_in(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Collect results from all task_ids. Blocks until all complete or timeout.

    Note: _pipe dependencies from fan_out results are handled automatically
    by the orchestrator (llm.py); no explicit _pipe parameter is needed here.

    Required:
        task_ids: list[str] — task IDs to collect results from.

    Optional:
        timeout: float — max seconds to wait (default 120).
    """
    task_ids = args.get("task_ids", [])
    if not task_ids:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task_ids' (list[str]).",
        )

    timeout = args.get("timeout", 120.0)

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    try:
        results = fan_in(task_ids, runtime=runtime, timeout=timeout)
    except Exception as exc:
        return ToolResult(success=False, content=f"fan_in failed: {exc}")

    parts = []
    for i, (tid, res) in enumerate(zip(task_ids, results)):
        if res is None:
            parts.append(f"  [{i}] {tid}: timed out / not found")
        elif res.success:
            preview = str(res.content)[:200]
            parts.append(f"  [{i}] {tid}: OK — {preview}")
        else:
            parts.append(f"  [{i}] {tid}: FAILED — {res.error}")

    return ToolResult(
        success=True,
        content="fan_in results:\n" + "\n".join(parts),
    )


@_summarize("fan_in")
def _fan_in_summary(args: dict) -> str:
    tids = args.get("task_ids", [])
    return f"fan_in({len(tids)} tasks)"


@_register("pipeline")
def _pipeline(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Run stages in sequence, each receiving the prior stage's handoff.

    Required:
        stages: list[dict] — each dict has 'task' (str) and optional 'subscriptions' (list[str]).

    Optional:
        max_turns: int — turn budget per stage (default 15).
        timeout: float — max seconds overall (default 300).
    """
    stages = args.get("stages", [])
    if not stages:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'stages' (list[dict]).",
        )

    max_turns = args.get("max_turns", 15)
    timeout = args.get("timeout", 300.0)

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    if config is None:
        return ToolResult(success=False, content="Agent config not available.")

    try:
        result = pipeline(
            stages,
            runtime=runtime,
            config=config,
            wg=_wg,
            rg=_rg,
            max_turns=max_turns,
            timeout=timeout,
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"pipeline failed: {exc}")

    if result is None:
        return ToolResult(success=False, content="Pipeline returned no result.")

    return ToolResult(
        success=result.success,
        content=f"Pipeline final stage: success={result.success}, turns={result.turns_used}, content:\n{result.content}",
    )


@_summarize("pipeline")
def _pipeline_summary(args: dict) -> str:
    stages = args.get("stages", [])
    return f"pipeline({len(stages)} stages)"


@_register("barrier")
def _barrier(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Block until all task_ids send coord.sync for a named barrier.

    Required:
        name: str — barrier name to wait on.
        task_ids: list[str] — agents that must arrive.

    Optional:
        timeout: float — max seconds to wait (default 120).
    """
    name = args.get("name", "")
    if not name:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'name' (str).",
        )

    task_ids = args.get("task_ids", [])
    if not task_ids:
        return ToolResult(success=False, content="Missing required parameter: 'task_ids' (list[str]).")

    timeout = args.get("timeout", 120.0)

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    try:
        arrived = barrier(name, task_ids, runtime=runtime, timeout=timeout)
    except Exception as exc:
        return ToolResult(success=False, content=f"barrier failed: {exc}")

    if arrived:
        content = f"Barrier '{name}': all {len(task_ids)} agents arrived."
        return ToolResult(success=True, content=content)
    else:
        content = f"Barrier '{name}': timed out waiting for {len(task_ids)} agents."
        return ToolResult(success=False, content=content)


@_summarize("barrier")
def _barrier_summary(args: dict) -> str:
    name = args.get("name", "?")
    tids = args.get("task_ids", [])
    return f"barrier('{name}', {len(tids)} tasks)"


@_register("scatter_gather")
def _scatter_gather(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Fan-out with per-worker input slices. Each worker gets one item.

    Required:
        items: list — items to distribute (one per worker).
        worker_task_template: str — task description with '{item}' placeholder.

    Optional:
        max_turns: int — turn budget per worker (default 15).
        timeout: float — max seconds to wait for all (default 120).
        subscriptions: list[str] — message types each worker subscribes to.
    """
    items = args.get("items", [])
    if not items:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'items' (list).",
        )

    template = args.get("worker_task_template", "")
    if not template:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'worker_task_template' (str).",
        )

    max_turns = args.get("max_turns", 15)
    timeout = args.get("timeout", 120.0)
    subscriptions = args.get("subscriptions", None)

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(success=False, content="Agent runtime not initialized.")

    config = getattr(_TOOL_CONTEXT, "_agent_config", None)
    if config is None:
        return ToolResult(success=False, content="Agent config not available.")

    try:
        results = scatter_gather(
            items,
            worker_task_template=template,
            runtime=runtime,
            config=config,
            wg=_wg,
            rg=_rg,
            max_turns=max_turns,
            timeout=timeout,
            subscriptions=subscriptions,
        )
    except Exception as exc:
        return ToolResult(success=False, content=f"scatter_gather failed: {exc}")

    parts = []
    for i, (item, res) in enumerate(zip(items, results)):
        if res is None:
            parts.append(f"  [{i}] {item!r}: timed out / not found")
        elif res.success:
            preview = str(res.content)[:200]
            parts.append(f"  [{i}] {item!r}: OK — {preview}")
        else:
            parts.append(f"  [{i}] {item!r}: FAILED — {res.error}")

    return ToolResult(
        success=True,
        content=f"scatter_gather ({len(items)} items):\n" + "\n".join(parts),
    )


@_summarize("scatter_gather")
def _scatter_gather_summary(args: dict) -> str:
    items = args.get("items", [])
    return f"scatter_gather({len(items)} items)"
