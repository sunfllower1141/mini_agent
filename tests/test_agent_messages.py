#!/usr/bin/env python3
"""
test_agent_messages.py -- tests for the typed inter-agent message system.

Covers:
    - AgentMessage construction and validation
    - MSG_TYPE_REGISTRY lookup
    - register_message_type
    - _validate_payload (valid and invalid)
    - AgentMessage.to_legacy_dict
    - AgentMessage.to_dict
    - Unknown type raises ValueError
    - Missing payload key raises ValueError
    - Wrong payload type raises ValueError
    - correlation_id default None
"""

import unittest
import time

from tools.agent_messages import (
    AgentMessage,
    MSG_TYPE_REGISTRY,
    register_message_type,
    _validate_payload,
    _route_message,
)


class TestAgentMessageConstruction(unittest.TestCase):
    """Unit tests for AgentMessage dataclass and validation."""

    def test_text_type_constructs(self):
        """text type should validate with proper payload."""
        msg = AgentMessage(
            type="text",
            sender="agent-123",
            payload={"body": "hello world"},
        )
        self.assertEqual(msg.type, "text")
        self.assertEqual(msg.sender, "agent-123")
        self.assertEqual(msg.payload["body"], "hello world")
        self.assertIsNone(msg.correlation_id)

    def test_handoff_result_constructs(self):
        """handoff.result should validate with result and task keys."""
        msg = AgentMessage(
            type="handoff.result",
            sender="worker-1",
            payload={"result": {"count": 42}, "task": "count items"},
            correlation_id="corr-abc",
        )
        self.assertEqual(msg.correlation_id, "corr-abc")
        self.assertEqual(msg.payload["result"]["count"], 42)

    def test_status_heartbeat_constructs(self):
        """status.heartbeat should validate with progress and pct."""
        msg = AgentMessage(
            type="status.heartbeat",
            sender="worker-2",
            payload={"progress": "half done", "pct": 50.0},
        )
        self.assertEqual(msg.payload["pct"], 50.0)

    def test_unknown_type_raises_valueerror(self):
        """Unknown message types should raise ValueError on construction."""
        with self.assertRaises(ValueError) as ctx:
            AgentMessage(
                type="nonexistent.type",
                sender="test",
                payload={},
            )
        self.assertIn("Unknown message type", str(ctx.exception))
        self.assertIn("nonexistent.type", str(ctx.exception))

    def test_missing_payload_key_raises_valueerror(self):
        """Missing required payload keys should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            AgentMessage(
                type="handoff.result",
                sender="test",
                payload={"result": {}},  # missing 'task'
            )
        self.assertIn("Missing required payload key", str(ctx.exception))
        self.assertIn("task", str(ctx.exception))

    def test_wrong_payload_type_raises_valueerror(self):
        """Wrong payload value types should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            AgentMessage(
                type="handoff.ack",
                sender="test",
                payload={"accepted": "not_a_bool", "reason": "ok"},
            )
        self.assertIn("Payload key 'accepted'", str(ctx.exception))
        self.assertIn("expected boolean", str(ctx.exception))

    def test_unknown_payload_key_raises_valueerror(self):
        """Extra unknown payload keys should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            AgentMessage(
                type="text",
                sender="test",
                payload={"body": "hi", "extra_field": 123},
            )
        self.assertIn("Unknown payload key", str(ctx.exception))
        self.assertIn("extra_field", str(ctx.exception))

    def test_correlation_id_default_none(self):
        """correlation_id should default to None."""
        msg = AgentMessage(
            type="text",
            sender="a",
            payload={"body": "x"},
        )
        self.assertIsNone(msg.correlation_id)

    def test_timestamp_default_set(self):
        """timestamp should be set to monotonic time."""
        before = time.monotonic()
        msg = AgentMessage(
            type="text",
            sender="a",
            payload={"body": "x"},
        )
        after = time.monotonic()
        self.assertGreaterEqual(msg.timestamp, before)
        self.assertLessEqual(msg.timestamp, after)


class TestToLegacyDict(unittest.TestCase):
    """Tests for to_legacy_dict backward compatibility."""

    def test_text_type_legacy_format(self):
        """text type should produce the old flat-dict format."""
        msg = AgentMessage(
            type="text",
            sender="backend",
            payload={"body": "API /stats ready"},
        )
        legacy = msg.to_legacy_dict()
        self.assertEqual(legacy["text"], "API /stats ready")
        self.assertEqual(legacy["from"], "backend")

    def test_non_text_type_legacy_format(self):
        """Non-text types should still serialize to flat dict."""
        msg = AgentMessage(
            type="handoff.result",
            sender="worker-1",
            payload={"result": {"x": 1}, "task": "compute"},
        )
        legacy = msg.to_legacy_dict()
        self.assertIn("handoff.result", legacy["text"])
        self.assertEqual(legacy["from"], "worker-1")


class TestToDict(unittest.TestCase):
    """Tests for to_dict full serialization."""

    def test_full_dict_serialization(self):
        msg = AgentMessage(
            type="coord.sync",
            sender="agent-a",
            payload={"barrier": "phase1", "arrived": 3, "total": 5},
            correlation_id="sync-001",
        )
        d = msg.to_dict()
        self.assertEqual(d["type"], "coord.sync")
        self.assertEqual(d["sender"], "agent-a")
        self.assertEqual(d["payload"]["barrier"], "phase1")
        self.assertEqual(d["payload"]["arrived"], 3)
        self.assertEqual(d["correlation_id"], "sync-001")
        self.assertIsInstance(d["timestamp"], float)


class TestRegisterMessageType(unittest.TestCase):
    """Tests for dynamic message type registration."""

    def test_register_adds_to_registry(self):
        """register_message_type should add entry to MSG_TYPE_REGISTRY."""
        register_message_type(
            "custom.event",
            {"event_name": "string", "data": "object"},
            "A custom event for testing.",
        )
        self.assertIn("custom.event", MSG_TYPE_REGISTRY)
        entry = MSG_TYPE_REGISTRY["custom.event"]
        self.assertEqual(entry["description"], "A custom event for testing.")
        self.assertIn("event_name", entry["schema"])
        self.assertIn("data", entry["schema"])

    def test_registered_type_constructs(self):
        """After registration, the type should be usable."""
        register_message_type(
            "custom.status",
            {"code": "number", "message": "string"},
            "Custom status message.",
        )
        msg = AgentMessage(
            type="custom.status",
            sender="test",
            payload={"code": 200, "message": "OK"},
        )
        self.assertEqual(msg.type, "custom.status")

    def test_registered_type_validates_payload(self):
        """Newly registered types should still validate payload."""
        register_message_type(
            "custom.data",
            {"value": "number"},
            "Test data.",
        )
        with self.assertRaises(ValueError):
            AgentMessage(
                type="custom.data",
                sender="test",
                payload={"value": "not a number"},
            )


class TestValidatePayload(unittest.TestCase):
    """Direct tests for _validate_payload."""

    def test_valid_payload_passes(self):
        """Valid payload should not raise."""
        _validate_payload(
            {"body": "hello"},
            {"body": "string"},
        )

    def test_missing_key_raises(self):
        """Missing required key should raise ValueError."""
        with self.assertRaises(ValueError):
            _validate_payload(
                {},
                {"body": "string"},
            )

    def test_type_mismatch_raises(self):
        """Wrong type should raise ValueError."""
        with self.assertRaises(ValueError):
            _validate_payload(
                {"pct": "fifty"},
                {"pct": "number"},
            )

    def test_boolean_validates(self):
        """Boolean type should be checked."""
        _validate_payload(
            {"accepted": True, "reason": "OK"},
            {"accepted": "boolean", "reason": "string"},
        )
        with self.assertRaises(ValueError):
            _validate_payload(
                {"accepted": 1, "reason": "OK"},
                {"accepted": "boolean", "reason": "string"},
            )


class TestRouteMessage(unittest.TestCase):
    """Tests for _route_message routing logic."""

    def setUp(self):
        import threading
        self.lock = threading.Lock()
        self.inboxes: dict = {}
        self.subscriptions: dict = {}

    def test_direct_target_delivery(self):
        """Target delivery bypasses subscriptions."""
        msg = AgentMessage(
            type="text",
            sender="parent",
            payload={"body": "direct"},
        )
        _route_message(
            msg, self.inboxes, self.subscriptions, self.lock,
            target="agent-1",
        )
        self.assertIn("agent-1", self.inboxes)
        self.assertEqual(len(self.inboxes["agent-1"]), 1)
        self.assertEqual(self.inboxes["agent-1"][0].payload["body"], "direct")

    def test_subscription_routing(self):
        """Only subscribed agents receive matching type messages."""
        self.subscriptions["agent-a"] = {"handoff.result"}
        self.subscriptions["agent-b"] = {"status.heartbeat"}

        msg = AgentMessage(
            type="handoff.result",
            sender="worker",
            payload={"result": {"x": 1}, "task": "compute"},
        )
        _route_message(
            msg, self.inboxes, self.subscriptions, self.lock,
        )
        self.assertIn("agent-a", self.inboxes)
        self.assertNotIn("agent-b", self.inboxes)

    def test_empty_subscriptions_get_all(self):
        """Agents with empty subscriptions receive everything."""
        self.subscriptions["agent-a"] = set()  # empty = all
        self.subscriptions["agent-b"] = {"handoff.result"}

        msg = AgentMessage(
            type="status.heartbeat",
            sender="worker",
            payload={"progress": "50%", "pct": 50.0},
        )
        _route_message(
            msg, self.inboxes, self.subscriptions, self.lock,
        )
        # agent-a gets it (empty subscriptions = all)
        self.assertIn("agent-a", self.inboxes)
        # agent-b does NOT get it (not subscribed to heartbeat)
        self.assertNotIn("agent-b", self.inboxes)


if __name__ == "__main__":
    unittest.main()
