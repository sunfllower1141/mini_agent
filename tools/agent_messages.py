#!/usr/bin/env python3
"""
agent_messages.py -- typed inter-agent messages for mini_agent.

Provides:
    AgentMessage     -- typed, schema-validated message dataclass
    MSG_TYPE_REGISTRY -- dict of known message type names -> {schema, description}
    register_message_type() -- register a new typed message schema
    _validate_payload() -- validate payload against a type schema
    _route_message()  -- deliver a message to subscribed agent inboxes
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field

from core.safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult, _TOOL_CONTEXT


# ---------------------------------------------------------------------------
# Shared state for inter-agent messaging
# ---------------------------------------------------------------------------

_AGENT_MSGS: list[dict] = []
_AGENT_MSGS_MAX = 1000        # ring-buffer cap: keep last N messages
_AGENT_MSGS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Type registry
# ---------------------------------------------------------------------------

MSG_TYPE_REGISTRY: dict[str, dict] = {}


def register_message_type(name: str, schema: dict, description: str) -> None:
    """Register a typed message schema.

    Args:
        name: Unique type name (e.g. "handoff.result").
        schema: Dict mapping payload field names to type strings
                (e.g. {"result": "object", "task": "string"}).
        description: Human-readable description of this message type.
    """
    MSG_TYPE_REGISTRY[name] = {
        "schema": schema,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Built-in message types
# ---------------------------------------------------------------------------

register_message_type("text", {
    "body": "string",
}, "Backward-compat wrapper for old agent_message text broadcasts.")

register_message_type("handoff.result", {
    "result": "object",
    "task": "string",
}, "Structured result from a completed sub-task, handed off to the next agent.")

register_message_type("handoff.request", {
    "task": "string",
    "input_schema": "object",
}, "Agent requests work to be done by another agent.")

register_message_type("handoff.ack", {
    "accepted": "boolean",
    "reason": "string",
}, "Receiver acknowledges a handoff.")

register_message_type("status.heartbeat", {
    "progress": "string",
    "pct": "number",
}, "Progress update from a running agent.")

register_message_type("status.error", {
    "error": "string",
    "phase": "string",
}, "Agent hit an unrecoverable error.")

register_message_type("coord.fan_out", {
    "items": "object",
    "worker_type": "string",
}, "Parent fans out items to worker pool.")

register_message_type("coord.fan_in", {
    "results": "object",
    "worker_count": "number",
}, "Worker sends result back to collector.")

register_message_type("coord.sync", {
    "barrier": "string",
    "arrived": "number",
    "total": "number",
}, "Barrier synchronization message.")


# ---------------------------------------------------------------------------
# AgentMessage dataclass
# ---------------------------------------------------------------------------

@dataclass
class AgentMessage:
    """A typed, schema-validated message between agents.

    Validates on construction: type must be registered, payload must
    match the registered schema.
    """
    type: str
    sender: str
    timestamp: float = field(default_factory=time.monotonic)
    payload: dict = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self):
        if self.type not in MSG_TYPE_REGISTRY:
            raise ValueError(
                f"Unknown message type: {self.type!r}. "
                f"Known types: {sorted(MSG_TYPE_REGISTRY.keys())}"
            )
        schema = MSG_TYPE_REGISTRY[self.type]["schema"]
        _validate_payload(self.payload, schema)

    def to_legacy_dict(self) -> dict:
        """Convert to the legacy flat-dict format used by agent_read.

        For 'text' type messages, this mirrors the old {"text": ..., "from": ...}.
        For other types, serializes the full AgentMessage.
        """
        if self.type == "text":
            return {
                "text": self.payload.get("body", ""),
                "from": self.sender,
            }
        # Other types still appear in the flat list for backward compat
        return {
            "text": f"[{self.type}] {self.payload}",
            "from": self.sender,
        }

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict of the full message."""
        return {
            "type": self.type,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, (dict, list)),
}


def _validate_payload(payload: dict, schema: dict) -> None:
    """Validate payload keys and types against the registered schema.

    Raises ValueError if validation fails.
    """
    # Check for required keys (all schema keys are required)
    for key, expected_type in schema.items():
        if key not in payload:
            raise ValueError(
                f"Missing required payload key {key!r} for message. "
                f"Expected keys: {sorted(schema.keys())}"
            )

    for key, value in payload.items():
        expected = schema.get(key)
        if expected is None:
            raise ValueError(
                f"Unknown payload key {key!r}. "
                f"Valid keys: {sorted(schema.keys())}"
            )
        checker = _TYPE_CHECKERS.get(expected)
        if checker is None:
            continue  # unknown type string -- allow it
        if not checker(value):
            raise ValueError(
                f"Payload key {key!r}: expected {expected}, got {type(value).__name__}"
            )


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------

def _route_message(
    msg: AgentMessage,
    inboxes: dict[str, list[AgentMessage]],
    subscriptions: dict[str, set[str]],
    lock: threading.Lock,
    target: str | None = None,
) -> None:
    """Deliver a typed message to subscribed agent inboxes.

    If *target* is set, delivers only to that task_id (bypassing subscriptions).
    Otherwise routes to all agents subscribed to msg.type.

    Agents with empty/no subscriptions receive ALL message types
    (backward-compatible default behavior).
    """
    with lock:
        if target is not None:
            # Direct delivery -- bypass subscription routing
            inbox = inboxes.setdefault(target, [])
            inbox.append(msg)
            return

        # Subscription-based routing
        routed = False
        for task_id, subs in subscriptions.items():
            if not subs:
                # Empty subscriptions -> receive everything
                inbox = inboxes.setdefault(task_id, [])
                inbox.append(msg)
                routed = True
            elif msg.type in subs:
                inbox = inboxes.setdefault(task_id, [])
                inbox.append(msg)
                routed = True

        # Agents with no subscription entry at all receive ALL message types
        # (matching the docstring: "agents with empty/no subscriptions receive ALL")
        for task_id in inboxes:
            if task_id not in subscriptions:
                inbox = inboxes[task_id]
                inbox.append(msg)
                routed = True



# ---------------------------------------------------------------------------
# agent_message
# ---------------------------------------------------------------------------

@_register("agent_message")
def _agent_message(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Broadcast a message visible to parent and sibling sub-agents."""

    text = args.get("text", "")
    if not text:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'text'.",
        )
    sender = args.get("from", "")

    # Create typed AgentMessage for routing
    try:
        msg = AgentMessage(
            type="text",
            sender=sender,
            payload={"body": text},
        )
    except ValueError as exc:
        return ToolResult(
            success=False,
            content=f"Invalid message: {exc}",
        )

    # Append to legacy flat list (backward compat)
    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.append(msg.to_legacy_dict())
        if len(_AGENT_MSGS) > _AGENT_MSGS_MAX:
            _AGENT_MSGS[:] = _AGENT_MSGS[-_AGENT_MSGS_MAX:]
        count = len(_AGENT_MSGS)

    # Route to subscribed inboxes
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        _route_message(
            msg,
            runtime.inboxes,
            runtime.subscriptions,
            runtime._lock,
            target=None,
        )

    return ToolResult(
        success=True,
        content=f"Message broadcast. ({count} total messages)",
    )


@_summarize("agent_message")
def _agent_message_summary(args: dict) -> str:
    text = args.get("text", "?")
    preview = text[:50]
    if len(text) > 50:
        preview += "..."
    return f"agent_message(\"{preview}\")"


# ---------------------------------------------------------------------------
# agent_read
# ---------------------------------------------------------------------------

@_register("agent_read")
def _agent_read(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read broadcast messages from other sub-agents and the parent.

    Returns messages in chronological order.  Use 'since' to only
    get messages with index >= that value (for polling).
    """
    since = args.get("since", None)
    if since is not None:
        try:
            since = int(since)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                content="'since' must be an integer index.",
            )

    with _AGENT_MSGS_LOCK:
        if since is not None:
            msgs = _AGENT_MSGS[since:]
        else:
            msgs = list(_AGENT_MSGS)

    if not msgs:
        return ToolResult(
            success=True,
            content="No new messages.",
        )

    lines = []
    base_idx = since if since is not None else 0
    for i, m in enumerate(msgs):
        idx = base_idx + i
        sender = f" from={m['from']}" if m.get("from") else ""
        lines.append(f"[{idx}]{sender} {m['text']}")

    return ToolResult(
        success=True,
        content="\n".join(lines),
    )


@_summarize("agent_read")
def _agent_read_summary(args: dict) -> str:
    since = args.get("since")
    if since is not None:
        return f"agent_read(since={since})"
    return "agent_read()"


# ---------------------------------------------------------------------------
# agent_handoff
# ---------------------------------------------------------------------------

@_register("agent_handoff")
def _agent_handoff(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Produce a typed result and route it to subscribed agents.

    Parameters:
        type: str              -- message type (default \"handoff.result\")
        result: dict           -- structured result payload
        correlation_id: str    -- optional correlation ID
        target: str | None     -- if set, deliver only to this task_id
    """

    msg_type = args.get("type", "handoff.result")
    if msg_type not in MSG_TYPE_REGISTRY:
        return ToolResult(
            success=False,
            content=f"Unknown handoff message type: {msg_type!r}. "
                    f"Use a registered type like 'handoff.result', 'handoff.ack', etc.",
        )

    result_payload = args.get("result", None)
    if result_payload is None:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'result'.",
        )
    if not isinstance(result_payload, dict):
        return ToolResult(
            success=False,
            content="'result' must be a dict.",
        )

    correlation_id = args.get("correlation_id", None)
    target = args.get("target", None)
    sender = args.get("from", "")

    # Build the payload according to type
    payload = {}
    if msg_type == "handoff.result":
        payload = {"result": result_payload, "task": str(result_payload)}
    elif msg_type == "handoff.request":
        payload = {"task": str(result_payload), "input_schema": result_payload}
    elif msg_type == "handoff.ack":
        payload = {"accepted": bool(result_payload.get("accepted", True)),
                    "reason": str(result_payload.get("reason", ""))}
    elif msg_type == "status.heartbeat":
        payload = {"progress": str(result_payload.get("progress", "")),
                    "pct": float(result_payload.get("pct", 0))}
    elif msg_type == "status.error":
        payload = {"error": str(result_payload.get("error", "")),
                    "phase": str(result_payload.get("phase", ""))}
    elif msg_type == "coord.fan_out":
        payload = {"items": result_payload, "worker_type": str(result_payload.get("worker_type", ""))}
    elif msg_type == "coord.fan_in":
        payload = {"results": result_payload, "worker_count": int(result_payload.get("worker_count", 0))}
    elif msg_type == "coord.sync":
        payload = {"barrier": str(result_payload.get("barrier", "")),
                    "arrived": int(result_payload.get("arrived", 1)),
                    "total": int(result_payload.get("total", 1))}

    try:
        msg = AgentMessage(
            type=msg_type,
            sender=sender,
            payload=payload,
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        return ToolResult(
            success=False,
            content=f"Invalid handoff message: {exc}",
        )

    # Append to legacy flat list (backward compat)
    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.append(msg.to_legacy_dict())
        if len(_AGENT_MSGS) > _AGENT_MSGS_MAX:
            _AGENT_MSGS[:] = _AGENT_MSGS[-_AGENT_MSGS_MAX:]
        count = len(_AGENT_MSGS)

    # Route to subscribed inboxes
    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is not None:
        _route_message(
            msg,
            runtime.inboxes,
            runtime.subscriptions,
            runtime._lock,
            target=target,
        )
        # Also append to runtime.messages for orchestrator visibility
        runtime.messages.append(msg.to_legacy_dict())

    target_info = f" to '{target}'" if target else ""
    return ToolResult(
        success=True,
        content=f"Handoff {msg_type!r} sent{target_info}. ({count} total messages)",
    )


@_summarize("agent_handoff")
def _agent_handoff_summary(args: dict) -> str:
    msg_type = args.get("type", "handoff.result")
    return f"agent_handoff(type=\"{msg_type}\")"


# ---------------------------------------------------------------------------
# agent_inbox
# ---------------------------------------------------------------------------

@_register("agent_inbox")
def _agent_inbox(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Read the typed inbox for a specific agent (task_id).

    Use 'since' to only get messages with index >= that value (for polling).
    """

    task_id = args.get("task_id", "")
    if not task_id:
        # Default to the caller's own task_id (supports the parent orchestrator
        # checking its own inbox without needing to know its ID).
        task_id = getattr(_TOOL_CONTEXT, "_agent_task_id", "")
        if not task_id:
            return ToolResult(
                success=False,
                content="Missing required parameter: 'task_id'.",
            )

    since = args.get("since", None)
    if since is not None:
        try:
            since = int(since)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                content="'since' must be an integer index.",
            )

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    inbox = runtime.get_inbox(task_id)
    if inbox is None:
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )
    if since is not None:
        inbox = inbox[since:]

    if not inbox:
        return ToolResult(
            success=True,
            content="No new messages in inbox.",
        )

    lines = []
    for i, msg in enumerate(inbox):
        idx = (since if since is not None else 0) + i
        lines.append(f"[{idx}] [{msg.type}] from={msg.sender}: {msg.payload}")

    return ToolResult(
        success=True,
        content="\n".join(lines),
    )


@_summarize("agent_inbox")
def _agent_inbox_summary(args: dict) -> str:
    since = args.get("since")
    if since is not None:
        return f"agent_inbox({args.get('task_id', '?')}, since={since})"
    return f"agent_inbox({args.get('task_id', '?')})"


# ---------------------------------------------------------------------------
# agent_subscribe
# ---------------------------------------------------------------------------

@_register("agent_subscribe")
def _agent_subscribe(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Declare or update message type subscriptions for an agent at runtime.

    Parameters:
        task_id: str       -- the agent to update
        types: list[str]   -- message types to subscribe to (empty = all)
    """

    task_id = args.get("task_id", "")
    if not task_id:
        return ToolResult(
            success=False,
            content="Missing required parameter: 'task_id'.",
        )

    types = args.get("types", None)
    if types is not None and not isinstance(types, list):
        return ToolResult(
            success=False,
            content="'types' must be a list of message type strings.",
        )

    runtime = getattr(_TOOL_CONTEXT, "_agent_runtime", None)
    if runtime is None:
        return ToolResult(
            success=False,
            content="Agent runtime not initialized.",
        )

    if runtime.get_status(task_id) == "not_found":
        return ToolResult(
            success=False,
            content=f"Sub-agent '{task_id}' not found.",
        )

    if types is None:
        # Default: subscribe to all (clear subscriptions)
        runtime.set_subscriptions(task_id, [])
        return ToolResult(
            success=True,
            content=f"Sub-agent '{task_id}' now receives all message types (default).",
        )

    # Validate types
    unknown = [t for t in types if t not in MSG_TYPE_REGISTRY]
    if unknown:
        return ToolResult(
            success=False,
            content=f"Unknown message type(s): {unknown}. "
                    f"Known types: {sorted(MSG_TYPE_REGISTRY.keys())}",
        )

    runtime.set_subscriptions(task_id, types)
    return ToolResult(
        success=True,
        content=f"Sub-agent '{task_id}' subscribed to: {types}",
    )


@_summarize("agent_subscribe")
def _agent_subscribe_summary(args: dict) -> str:
    types = args.get("types", [])
    return f"agent_subscribe({args.get('task_id', '?')}, types={types})"

