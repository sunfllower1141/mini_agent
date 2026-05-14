#!/usr/bin/env python3
"""
agent_messages.py — typed inter-agent messages for mini_agent.

Provides:
    AgentMessage     — typed, schema-validated message dataclass
    MSG_TYPE_REGISTRY — dict of known message type names → {schema, description}
    register_message_type() — register a new typed message schema
    _validate_payload() — validate payload against a type schema
    _route_message()  — deliver a message to subscribed agent inboxes
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field


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
            continue  # unknown type string — allow it
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
            # Direct delivery — bypass subscription routing
            inbox = inboxes.setdefault(target, [])
            inbox.append(msg)
            return

        # Subscription-based routing
        routed = False
        for task_id, subs in subscriptions.items():
            if not subs:
                # Empty subscriptions → receive everything
                inbox = inboxes.setdefault(task_id, [])
                inbox.append(msg)
                routed = True
            elif msg.type in subs:
                inbox = inboxes.setdefault(task_id, [])
                inbox.append(msg)
                routed = True
