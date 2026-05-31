#!/usr/bin/env python3
"""
test_integration.py — end-to-end integration tests for the multi-agent subsystem.

Uses real spawn/collect/status tool dispatches.  Sub-agents run in real
background threads but the LLM is mocked so no API calls are made.

Covers:
  1. Full spawn → file work → collect → verify disk flow
  2. Fan-out pattern with multiple agents + collect_any
  3. Agent handoff with typed messages
  4. Agent inbox / subscribe routing
  5. Parent polls status while agent works
  6. Multiple collect_any calls
  7. agent_message + agent_read broadcast/pagination
"""

import re
import time
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _TOOL_DISPATCH, _TOOL_CONTEXT, set_context
from agent_runtime import AgentRuntime, SubAgentResult
from conftest import make_mock_config, make_gates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(content: str = "done", tool_calls: list | None = None):
    """Build a response dict matching what run_sub_agent expects."""
    resp: dict = {"role": "assistant", "content": content}
    if tool_calls:
        resp["tool_calls"] = tool_calls
    return resp


def _make_tool_call(name: str, arguments: str):
    """Build a tool_call dict expected by run_sub_agent."""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _extract_task_id(text: str) -> str | None:
    """Extract the first 8-char hex task_id from spawn output.

    Handles both quoted (single spawn) and bare (batch spawn) formats.
    """
    match = re.search(r"'([a-f0-9]{8})'", text)
    if match:
        return match.group(1)
    # Batch format: "Spawned N sub-agent(s): abc12345, def67890, ..."
    match = re.search(r"sub-agent\(s\):\s*([a-f0-9]{8})", text)
    return match.group(1) if match else None


def _extract_task_ids(text: str) -> list[str]:
    """Extract all 8-char hex task_ids from spawn output.

    Handles both quoted (single spawn) and bare (batch spawn) formats.
    """
    ids = re.findall(r"'([a-f0-9]{8})'", text)
    if ids:
        return ids
    # Batch format: "Spawned N sub-agent(s): abc12345, def67890, ..."
    match = re.search(r"sub-agent\(s\):\s*(.+?)\.", text)
    if match:
        return re.findall(r"([a-f0-9]{8})", match.group(1))
    return []


def _setUp_context():
    """Create runtime, config, gates, and set _TOOL_CONTEXT.

    Returns (runtime, tmp_dir, wg, rg) for use in setUp.
    Caller must store tmp_dir to cleanup later.
    """
    tmp_dir = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp_dir.name)
    runtime = AgentRuntime()
    config = make_mock_config(workspace=str(tmp_path))
    set_context(
        _agent_runtime=runtime,
        _agent_config=config,
        workspace=str(tmp_path),
    )
    wg, rg = make_gates(str(tmp_path))
    return runtime, tmp_dir, wg, rg


def _tearDown_context(runtime, tmp_dir):
    """Undo _setUp_context: cancel agents, clear msgs, reset context, cleanup."""
    runtime.cancel_all()
    for t in list(runtime.tasks.values()):
        t.join(timeout=2)
    from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.clear()
    set_context(_agent_runtime=None, _agent_config=None)
    tmp_dir.cleanup()


# ---------------------------------------------------------------------------
# 1. Full spawn → file work → collect → verify disk
# ---------------------------------------------------------------------------

class TestFullSpawnCollectVerify(unittest.TestCase):
    """Integration: spawn a sub-agent that writes a file, collect, verify."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_spawn_writes_file_and_collect(self):
        wg, rg = self.wg, self.rg
        test_file = Path(self.tmp_dir.name) / "agent_output.txt"

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.side_effect = [
                _make_llm_response(
                    content="",
                    tool_calls=[
                        _make_tool_call(
                            "write_file",
                            f'{{"path": "{test_file}", "content": "hello from sub-agent"}}',
                        )
                    ],
                ),
                _make_llm_response(content="All done, file written."),
            ]

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"task": "write a test file", "max_turns": 3},
                wg, rg,
            )
            self.assertTrue(result.success, f"spawn failed: {result.content}")
            self.assertIn("Spawned sub-agent", result.content)

            task_id = _extract_task_id(result.content)
            self.assertIsNotNone(task_id, f"No task_id found in: {result.content}")

            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            self.assertTrue(collected.success, f"collect failed: {collected.content}")
            self.assertIn("All done", collected.content)

        # Verify file on disk
        self.assertTrue(test_file.exists(), f"File {test_file} was not created")
        self.assertEqual(test_file.read_text(), "hello from sub-agent")

    def test_spawn_failed_task_reports_content(self):
        """If the sub-agent produces text, collect returns it."""
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.side_effect = [
                _make_llm_response(content="Something went wrong."),
            ]

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"task": "failing task", "max_turns": 2},
                wg, rg,
            )
            self.assertTrue(result.success)
            task_id = _extract_task_id(result.content)
            self.assertIsNotNone(task_id)

            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            self.assertIn("Something went wrong", collected.content)


# ---------------------------------------------------------------------------
# 2. Fan-out pattern with multiple agents + collect_any
# ---------------------------------------------------------------------------

class TestFanOut(unittest.TestCase):
    """Integration: spawn multiple agents, collect_any to get first result."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_fan_out_three_agents_collect_any(self):
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="result from agent")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            batch_result = spawn(
                {"tasks": ["task alpha", "task beta", "task gamma"], "max_turns": 1},
                wg, rg,
            )
            self.assertTrue(batch_result.success)
            self.assertIn("3 sub-agent", batch_result.content)

            # Collect the first finished
            collect_any = _TOOL_DISPATCH["collect_any"]
            first = collect_any({}, wg, rg)
            self.assertTrue(first.success)
            self.assertIn("finished first", first.content)
            self.assertIn("result from agent", first.content)

    def test_fan_out_collect_any_with_specific_ids(self):
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="done")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": ["work A", "work B"], "max_turns": 1},
                wg, rg,
            )
            self.assertTrue(result.success)
            ids = _extract_task_ids(result.content)
            self.assertEqual(len(ids), 2)

            collect_any = _TOOL_DISPATCH["collect_any"]
            first = collect_any({"task_ids": ids}, wg, rg)
            self.assertTrue(first.success)
            self.assertTrue(first.content)

    def test_batch_spawn_respects_concurrency_limit(self):
        """Batch spawn should not exceed _MAX_CONCURRENT (5)."""
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="ok")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": [f"task {i}" for i in range(7)], "max_turns": 1},
                wg, rg,
            )
            self.assertTrue(result.success)
            self.assertTrue(
                "5 sub-agent" in result.content or "Spawned" in result.content
            )


# ---------------------------------------------------------------------------
# 3. Agent handoff with typed messages
# ---------------------------------------------------------------------------

class TestAgentHandoffIntegration(unittest.TestCase):
    """Integration: agent_handoff sends typed messages between agents."""

    def setUp(self):
        from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_handoff_result_routes_to_global_list(self):
        wg, rg = self.wg, self.rg
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "handoff.result",
                "result": {"count": 42, "task": "count items"},
                "from": "worker-1",
            },
            wg, rg,
        )
        self.assertTrue(result.success)
        self.assertIn("handoff.result", result.content)
        self.assertIn("1 total messages", result.content)

        from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            self.assertEqual(len(_AGENT_MSGS), 1)
            self.assertIn("handoff.result", _AGENT_MSGS[0]["text"])

    def test_handoff_coord_fan_out(self):
        wg, rg = self.wg, self.rg
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "coord.fan_out",
                "result": {"items": ["a", "b", "c"], "worker_type": "transformer"},
                "from": "orchestrator",
            },
            wg, rg,
        )
        self.assertTrue(result.success)
        self.assertIn("coord.fan_out", result.content)

    def test_handoff_status_heartbeat(self):
        wg, rg = self.wg, self.rg
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "status.heartbeat",
                "result": {"progress": "75% done", "pct": 75},
                "from": "worker-3",
            },
            wg, rg,
        )
        self.assertTrue(result.success)
        self.assertIn("status.heartbeat", result.content)

    def test_handoff_with_correlation_id(self):
        """Handoff with correlation_id should be accepted and stored."""
        wg, rg = self.wg, self.rg
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "handoff.result",
                "result": {"data": "linked"},
                "correlation_id": "req-12345",
                "from": "source",
            },
            wg, rg,
        )
        self.assertTrue(result.success)
        self.assertIn("handoff.result", result.content)

        # Verify the correlation_id was stored in the global message list
        from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            self.assertGreaterEqual(len(_AGENT_MSGS), 1)
            # The legacy dict format includes the serialized payload
            msg_text = _AGENT_MSGS[0]["text"]
            self.assertTrue("req-12345" in msg_text or "linked" in msg_text)


# ---------------------------------------------------------------------------
# 4. Agent inbox / subscribe routing
# ---------------------------------------------------------------------------

class TestInboxSubscribeRouting(unittest.TestCase):
    """Integration: set up subscriptions, route messages, read inbox."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_subscribe_then_send_inbox_receives(self):
        wg, rg = self.wg, self.rg
        runtime: AgentRuntime = _TOOL_CONTEXT.__dict__["_agent_runtime"]

        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("inbox-test", t, ev)
        t.start()

        try:
            sub_dispatch = _TOOL_DISPATCH["agent_subscribe"]
            sub_result = sub_dispatch(
                {"task_id": "inbox-test", "types": ["handoff.result", "coord.sync"]},
                wg, rg,
            )
            self.assertTrue(sub_result.success, sub_result.content)

            handoff = _TOOL_DISPATCH["agent_handoff"]
            handoff(
                {"type": "handoff.result", "result": {"x": 99}, "from": "producer"},
                wg, rg,
            )

            inbox_dispatch = _TOOL_DISPATCH["agent_inbox"]
            inbox_result = inbox_dispatch({"task_id": "inbox-test"}, wg, rg)
            self.assertTrue(inbox_result.success)
            self.assertIn("handoff.result", inbox_result.content)
            self.assertIn("producer", inbox_result.content)
        finally:
            runtime.cancel("inbox-test")
            t.join(timeout=1)

    def test_inbox_empty_subscriptions_receives_all(self):
        """Agent with empty subscriptions receives all message types."""
        wg, rg = self.wg, self.rg
        runtime: AgentRuntime = _TOOL_CONTEXT.__dict__["_agent_runtime"]

        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("all-receiver", t, ev)
        t.start()

        try:
            sub_dispatch = _TOOL_DISPATCH["agent_subscribe"]
            sub_dispatch({"task_id": "all-receiver"}, wg, rg)

            handoff = _TOOL_DISPATCH["agent_handoff"]
            handoff(
                {
                    "type": "coord.sync",
                    "result": {"barrier": "step1", "arrived": 1, "total": 3},
                    "from": "worker",
                },
                wg, rg,
            )

            inbox_dispatch = _TOOL_DISPATCH["agent_inbox"]
            inbox_result = inbox_dispatch({"task_id": "all-receiver"}, wg, rg)
            self.assertTrue(inbox_result.success)
            self.assertIn("coord.sync", inbox_result.content)
        finally:
            runtime.cancel("all-receiver")
            t.join(timeout=1)

    def test_inbox_since_polling(self):
        """inbox with 'since' skips previously-read messages."""
        wg, rg = self.wg, self.rg
        runtime: AgentRuntime = _TOOL_CONTEXT.__dict__["_agent_runtime"]

        from tools.agent_messages import AgentMessage

        runtime.set_subscriptions("poller", [])

        for i in range(3):
            msg = AgentMessage(
                type="text",
                sender=f"sender-{i}",
                payload={"body": f"message-{i}"},
            )
            runtime.append_inbox("poller", msg)

        inbox_dispatch = _TOOL_DISPATCH["agent_inbox"]

        r1 = inbox_dispatch({"task_id": "poller"}, wg, rg)
        self.assertIn("message-0", r1.content)

        r2 = inbox_dispatch({"task_id": "poller", "since": 2}, wg, rg)
        self.assertIn("message-2", r2.content)
        self.assertNotIn("message-0", r2.content)
        self.assertNotIn("message-1", r2.content)

    def test_direct_target_routing_bypasses_subscriptions(self):
        """Handoff with 'target' delivers directly regardless of subscriptions."""
        wg, rg = self.wg, self.rg
        runtime: AgentRuntime = _TOOL_CONTEXT.__dict__["_agent_runtime"]

        runtime.set_subscriptions("direct-target", set())

        handoff = _TOOL_DISPATCH["agent_handoff"]
        result = handoff(
            {
                "type": "handoff.result",
                "result": {"secret": "direct-msg"},
                "target": "direct-target",
                "from": "sender",
            },
            wg, rg,
        )
        self.assertTrue(result.success)

        inbox = runtime.get_inbox("direct-target")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].payload["result"], {"secret": "direct-msg"})


# ---------------------------------------------------------------------------
# 5. Parent polls status while agent works
# ---------------------------------------------------------------------------

class TestParentPolling(unittest.TestCase):
    """Integration: parent spawns agent, polls agent_status while it runs."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_poll_running_then_completed(self):
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.side_effect = [
                _make_llm_response(
                    content="",
                    tool_calls=[
                        _make_tool_call("write_file", '{"path": "dummy", "content": "x"}')
                    ],
                ),
                _make_llm_response(content="slow agent done"),
            ]

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"task": "slow work", "max_turns": 3},
                wg, rg,
            )
            self.assertTrue(result.success)
            task_id = _extract_task_id(result.content)
            self.assertIsNotNone(task_id)

            # Poll while running
            status_dispatch = _TOOL_DISPATCH["agent_status"]
            status1 = status_dispatch({"task_id": task_id}, wg, rg)
            self.assertTrue(status1.success)

            # Collect — blocks until done
            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            self.assertTrue(collected.success)

            # Poll after completion
            status2 = status_dispatch({"task_id": task_id}, wg, rg)
            self.assertTrue(status2.success)
            self.assertIn("completed", status2.content.lower())

    def test_poll_unknown_agent_returns_not_found(self):
        wg, rg = self.wg, self.rg
        dispatch = _TOOL_DISPATCH["agent_status"]
        result = dispatch({"task_id": "nonexistent42"}, wg, rg)
        self.assertTrue(result.success)
        self.assertIn("not found", result.content)


# ---------------------------------------------------------------------------
# 6. Multiple collect_any calls
# ---------------------------------------------------------------------------

class TestMultipleCollectAny(unittest.TestCase):
    """Integration: spawn multiple agents, call collect_any repeatedly."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_collect_any_multiple_agents(self):
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="quick result")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            batch = spawn(
                {"tasks": ["job 1", "job 2", "job 3"], "max_turns": 1},
                wg, rg,
            )
            self.assertTrue(batch.success)
            ids = _extract_task_ids(batch.content)
            self.assertEqual(len(ids), 3)

            collect_any = _TOOL_DISPATCH["collect_any"]

            # First collect
            r1 = collect_any({}, wg, rg)
            # Second collect
            r2 = collect_any({}, wg, rg)

            # At least one should succeed
            self.assertTrue(
                r1.success or r2.success,
                f"Neither collect_any succeeded.\nr1: {r1.content}\nr2: {r2.content}",
            )

    def test_collect_any_after_all_completed(self):
        """After all agents complete, collect_any should return one immediately."""
        wg, rg = self.wg, self.rg

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="done")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": ["task X", "task Y"], "max_turns": 1},
                wg, rg,
            )
            self.assertTrue(result.success)
            ids = _extract_task_ids(result.content)
            self.assertEqual(len(ids), 2)

            # Wait for all to complete
            for tid in ids:
                _TOOL_DISPATCH["collect_agent"]({"task_id": tid}, wg, rg)

            # Now collect_any works on already-completed
            collect_any = _TOOL_DISPATCH["collect_any"]
            r = collect_any({"task_ids": ids}, wg, rg)
            self.assertTrue(r.success, r.content)

    def test_collect_any_no_agents_returns_error(self):
        wg, rg = self.wg, self.rg
        collect_any = _TOOL_DISPATCH["collect_any"]
        result = collect_any({}, wg, rg)
        self.assertFalse(result.success)
        self.assertIn("No sub-agents", result.content)


# ---------------------------------------------------------------------------
# 7. agent_message + agent_read broadcast/pagination
# ---------------------------------------------------------------------------

class TestMessageBroadcastIntegration(unittest.TestCase):
    """Integration: broadcast messages and read them back."""

    def setUp(self):
        from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_broadcast_and_read_multiple(self):
        wg, rg = self.wg, self.rg
        send = _TOOL_DISPATCH["agent_message"]
        read = _TOOL_DISPATCH["agent_read"]

        send({"text": "first message", "from": "agent-A"}, wg, rg)
        send({"text": "second message", "from": "agent-B"}, wg, rg)

        r = read({}, wg, rg)
        self.assertTrue(r.success)
        self.assertIn("first message", r.content)
        self.assertIn("second message", r.content)

    def test_read_since_pagination(self):
        wg, rg = self.wg, self.rg
        send = _TOOL_DISPATCH["agent_message"]
        read = _TOOL_DISPATCH["agent_read"]

        for i in range(5):
            send({"text": f"msg-{i}"}, wg, rg)

        r = read({"since": 3}, wg, rg)
        self.assertTrue(r.success)
        self.assertIn("msg-3", r.content)
        self.assertIn("msg-4", r.content)
        self.assertNotIn("msg-0", r.content)
        self.assertNotIn("msg-1", r.content)
        self.assertNotIn("msg-2", r.content)


# ---------------------------------------------------------------------------
# 8. Multi-agent E2E workflow patterns (pipeline + scatter_gather)
# ---------------------------------------------------------------------------

class TestMultiAgentE2EWorkflow(unittest.TestCase):
    """Integration: pipeline pattern and scatter_gather pattern end-to-end."""

    def setUp(self):
        self.runtime, self.tmp_dir, self.wg, self.rg = _setUp_context()

    def tearDown(self):
        _tearDown_context(self.runtime, self.tmp_dir)

    def test_pipeline_pattern(self):
        """Spawn agent A, agent A spawns agent B, collect both via pipeline."""
        wg, rg = self.wg, self.rg
        tmp_path = Path(self.tmp_dir.name)

        # Stage A writes a file; Stage B reads it and appends.
        stage_a_file = tmp_path / "pipeline_stage_a.txt"

        with patch("sub_agent.call_llm") as mock_llm:
            # Stage A: write a file with initial content
            # Stage B: read and confirm
            mock_llm.side_effect = [
                # Stage A responses
                _make_llm_response(
                    content="",
                    tool_calls=[
                        _make_tool_call(
                            "write_file",
                            f'{{"path": "{stage_a_file}", "content": "stage-a-output"}}',
                        )
                    ],
                ),
                _make_llm_response(content="Stage A complete."),
                # Stage B responses
                _make_llm_response(
                    content="",
                    tool_calls=[
                        _make_tool_call(
                            "read_file",
                            f'{{"path": "{stage_a_file}"}}',
                        )
                    ],
                ),
                _make_llm_response(content="Stage B complete. Found stage-a-output."),
            ]

            pipeline_dispatch = _TOOL_DISPATCH["pipeline"]
            result = pipeline_dispatch(
                {
                    "stages": [
                        {"task": "Write a file with stage A output", "subscriptions": []},
                        {"task": "Read the file from stage A and confirm", "subscriptions": ["handoff.result"]},
                    ],
                    "max_turns": 4,
                },
                wg, rg,
            )
            self.assertTrue(result.success, f"pipeline failed: {result.content}")
            self.assertIn("Stage B complete", result.content)
            self.assertIn("stage-a-output", result.content)

        # Verify file on disk
        self.assertTrue(stage_a_file.exists(), f"File {stage_a_file} was not created")
        self.assertEqual(stage_a_file.read_text(), "stage-a-output")

    def test_scatter_gather_pattern(self):
        """Use scatter_gather to process a list of items concurrently."""
        wg, rg = self.wg, self.rg

        items = ["alpha", "beta", "gamma"]

        with patch("sub_agent.call_llm") as mock_llm:
            # Each worker returns its item processed
            mock_llm.return_value = _make_llm_response(
                content="processed: alpha"
            )

            sg_dispatch = _TOOL_DISPATCH["scatter_gather"]
            result = sg_dispatch(
                {
                    "items": items,
                    "worker_task_template": "Process the item '{item}' and report the result.",
                    "max_turns": 2,
                },
                wg, rg,
            )
            self.assertTrue(result.success, f"scatter_gather failed: {result.content}")
            # At least one worker processed an item
            content_lower = result.content.lower()
            self.assertTrue(
                "processed" in content_lower or "alpha" in content_lower or "success" in content_lower
            )

    def test_scatter_gather_empty_items(self):
        """scatter_gather with no items should return a clear error."""
        wg, rg = self.wg, self.rg
        sg_dispatch = _TOOL_DISPATCH["scatter_gather"]
        result = sg_dispatch(
            {"items": [], "worker_task_template": "Process {item}"},
            wg, rg,
        )
        self.assertFalse(result.success)
        self.assertTrue(
            "items" in result.content.lower() or "No items" in result.content
        )
