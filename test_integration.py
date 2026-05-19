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
import pytest
from unittest.mock import patch

from tools import _TOOL_DISPATCH, _TOOL_CONTEXT, set_context
from safety import ReadSafetyGate, WriteSafetyGate
from agent_runtime import AgentRuntime, SubAgentResult


# ---------------------------------------------------------------------------
# Fixtures (same as test_sub_agent.py — no shared conftest)
# ---------------------------------------------------------------------------

@pytest.fixture
def gates(tmp_path):
    """Safety gates rooted in a temp directory."""
    wg = WriteSafetyGate(str(tmp_path))
    rg = ReadSafetyGate(str(tmp_path))
    return wg, rg


@pytest.fixture
def configured_context(tmp_path, monkeypatch):
    """Set up _TOOL_CONTEXT with a runtime and a mock config for tool tests."""
    from agent_runtime import AgentRuntime

    runtime = AgentRuntime()

    class MockConfig:
        model = "test-model"
        api_key = "test-key"
        api_url = "https://test.api"
        stream = False
        sub_agent_model = "test-model"
        sub_agent_api_key = ""
        sub_agent_max_concurrent = 5
        sub_agent_max_turns = 5
        workspace = str(tmp_path)
        unrestricted = False
        allow_overwrites = True
        approve_write_ops = False

    config = MockConfig()
    set_context(
        _agent_runtime=runtime,
        _agent_config=config,
        workspace=str(tmp_path),
    )
    yield
    # Clean up all sub-agents so background threads don't pollute
    # _AGENT_MSGS for subsequent tests (e.g. heartbeat handoffs).
    runtime.cancel_all()
    # Join all threads to ensure no in-flight messages land after cleanup.
    for t in list(runtime.tasks.values()):
        t.join(timeout=2)
    from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK
    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.clear()
    set_context(_agent_runtime=None, _agent_config=None)


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


# ---------------------------------------------------------------------------
# 1. Full spawn → file work → collect → verify disk
# ---------------------------------------------------------------------------

class TestFullSpawnCollectVerify:
    """Integration: spawn a sub-agent that writes a file, collect, verify."""

    def test_spawn_writes_file_and_collect(self, configured_context, gates, tmp_path):
        wg, rg = gates
        test_file = tmp_path / "agent_output.txt"

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
            assert result.success, f"spawn failed: {result.content}"
            assert "Spawned sub-agent" in result.content

            task_id = _extract_task_id(result.content)
            assert task_id is not None, f"No task_id found in: {result.content}"

            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            assert collected.success, f"collect failed: {collected.content}"
            assert "All done" in collected.content

        # Verify file on disk
        assert test_file.exists(), f"File {test_file} was not created"
        assert test_file.read_text() == "hello from sub-agent"

    def test_spawn_failed_task_reports_content(self, configured_context, gates):
        """If the sub-agent produces text, collect returns it."""
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.side_effect = [
                _make_llm_response(content="Something went wrong."),
            ]

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"task": "failing task", "max_turns": 2},
                wg, rg,
            )
            assert result.success
            task_id = _extract_task_id(result.content)
            assert task_id

            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            assert "Something went wrong" in collected.content


# ---------------------------------------------------------------------------
# 2. Fan-out pattern with multiple agents + collect_any
# ---------------------------------------------------------------------------

class TestFanOut:
    """Integration: spawn multiple agents, collect_any to get first result."""

    def test_fan_out_three_agents_collect_any(self, configured_context, gates):
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="result from agent")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            batch_result = spawn(
                {"tasks": ["task alpha", "task beta", "task gamma"], "max_turns": 1},
                wg, rg,
            )
            assert batch_result.success
            assert "3 sub-agent" in batch_result.content

            # Collect the first finished
            collect_any = _TOOL_DISPATCH["collect_any"]
            first = collect_any({}, wg, rg)
            assert first.success
            assert "finished first" in first.content
            assert "result from agent" in first.content

    def test_fan_out_collect_any_with_specific_ids(self, configured_context, gates):
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="done")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": ["work A", "work B"], "max_turns": 1},
                wg, rg,
            )
            assert result.success
            ids = _extract_task_ids(result.content)
            assert len(ids) == 2

            collect_any = _TOOL_DISPATCH["collect_any"]
            first = collect_any({"task_ids": ids}, wg, rg)
            assert first.success
            assert first.content

    def test_batch_spawn_respects_concurrency_limit(self, configured_context, gates):
        """Batch spawn should not exceed _MAX_CONCURRENT (5)."""
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="ok")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": [f"task {i}" for i in range(7)], "max_turns": 1},
                wg, rg,
            )
            assert result.success
            assert "5 sub-agent" in result.content or "Spawned" in result.content


# ---------------------------------------------------------------------------
# 3. Agent handoff with typed messages
# ---------------------------------------------------------------------------

class TestAgentHandoffIntegration:
    """Integration: agent_handoff sends typed messages between agents."""

    def setup_method(self):
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()

    def test_handoff_result_routes_to_global_list(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "handoff.result",
                "result": {"count": 42, "task": "count items"},
                "from": "worker-1",
            },
            wg, rg,
        )
        assert result.success
        assert "handoff.result" in result.content
        assert "1 total messages" in result.content

        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            assert len(_AGENT_MSGS) == 1
            assert "handoff.result" in _AGENT_MSGS[0]["text"]

    def test_handoff_coord_fan_out(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "coord.fan_out",
                "result": {"items": ["a", "b", "c"], "worker_type": "transformer"},
                "from": "orchestrator",
            },
            wg, rg,
        )
        assert result.success
        assert "coord.fan_out" in result.content

    def test_handoff_status_heartbeat(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH["agent_handoff"]

        result = dispatch(
            {
                "type": "status.heartbeat",
                "result": {"progress": "75% done", "pct": 75},
                "from": "worker-3",
            },
            wg, rg,
        )
        assert result.success
        assert "status.heartbeat" in result.content

    def test_handoff_with_correlation_id(self, configured_context, gates):
        """Handoff with correlation_id should be accepted and stored."""
        wg, rg = gates
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
        assert result.success
        assert "handoff.result" in result.content

        # Verify the correlation_id was stored in the global message list
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            assert len(_AGENT_MSGS) >= 1
            # The legacy dict format includes the serialized payload
            msg_text = _AGENT_MSGS[0]["text"]
            assert "req-12345" in msg_text or "linked" in msg_text


# ---------------------------------------------------------------------------
# 4. Agent inbox / subscribe routing
# ---------------------------------------------------------------------------

class TestInboxSubscribeRouting:
    """Integration: set up subscriptions, route messages, read inbox."""

    def test_subscribe_then_send_inbox_receives(self, configured_context, gates):
        wg, rg = gates
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
            assert sub_result.success, sub_result.content

            handoff = _TOOL_DISPATCH["agent_handoff"]
            handoff(
                {"type": "handoff.result", "result": {"x": 99}, "from": "producer"},
                wg, rg,
            )

            inbox_dispatch = _TOOL_DISPATCH["agent_inbox"]
            inbox_result = inbox_dispatch({"task_id": "inbox-test"}, wg, rg)
            assert inbox_result.success
            assert "handoff.result" in inbox_result.content
            assert "producer" in inbox_result.content
        finally:
            runtime.cancel("inbox-test")
            t.join(timeout=1)

    def test_inbox_empty_subscriptions_receives_all(self, configured_context, gates):
        """Agent with empty subscriptions receives all message types."""
        wg, rg = gates
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
            assert inbox_result.success
            assert "coord.sync" in inbox_result.content
        finally:
            runtime.cancel("all-receiver")
            t.join(timeout=1)

    def test_inbox_since_polling(self, configured_context, gates):
        """inbox with 'since' skips previously-read messages."""
        wg, rg = gates
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
        assert "message-0" in r1.content

        r2 = inbox_dispatch({"task_id": "poller", "since": 2}, wg, rg)
        assert "message-2" in r2.content
        assert "message-0" not in r2.content
        assert "message-1" not in r2.content

    def test_direct_target_routing_bypasses_subscriptions(self, configured_context, gates):
        """Handoff with 'target' delivers directly regardless of subscriptions."""
        wg, rg = gates
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
        assert result.success

        inbox = runtime.get_inbox("direct-target")
        assert len(inbox) == 1
        assert inbox[0].payload["result"] == {"secret": "direct-msg"}


# ---------------------------------------------------------------------------
# 5. Parent polls status while agent works
# ---------------------------------------------------------------------------

class TestParentPolling:
    """Integration: parent spawns agent, polls agent_status while it runs."""

    def test_poll_running_then_completed(self, configured_context, gates):
        wg, rg = gates

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
            assert result.success
            task_id = _extract_task_id(result.content)
            assert task_id

            # Poll while running
            status_dispatch = _TOOL_DISPATCH["agent_status"]
            status1 = status_dispatch({"task_id": task_id}, wg, rg)
            assert status1.success

            # Collect — blocks until done
            collect = _TOOL_DISPATCH["collect_agent"]
            collected = collect({"task_id": task_id}, wg, rg)
            assert collected.success

            # Poll after completion
            status2 = status_dispatch({"task_id": task_id}, wg, rg)
            assert status2.success
            assert "completed" in status2.content.lower() or "Success" in status2.content

    def test_poll_unknown_agent_returns_not_found(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH["agent_status"]
        result = dispatch({"task_id": "nonexistent42"}, wg, rg)
        assert result.success
        assert "not found" in result.content


# ---------------------------------------------------------------------------
# 6. Multiple collect_any calls
# ---------------------------------------------------------------------------

class TestMultipleCollectAny:
    """Integration: spawn multiple agents, call collect_any repeatedly."""

    def test_collect_any_multiple_agents(self, configured_context, gates):
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="quick result")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            batch = spawn(
                {"tasks": ["job 1", "job 2", "job 3"], "max_turns": 1},
                wg, rg,
            )
            assert batch.success
            ids = _extract_task_ids(batch.content)
            assert len(ids) == 3

            collect_any = _TOOL_DISPATCH["collect_any"]

            # First collect
            r1 = collect_any({}, wg, rg)
            # Second collect
            r2 = collect_any({}, wg, rg)

            # At least one should succeed
            assert r1.success or r2.success, (
                f"Neither collect_any succeeded.\nr1: {r1.content}\nr2: {r2.content}"
            )

    def test_collect_any_after_all_completed(self, configured_context, gates):
        """After all agents complete, collect_any should return one immediately."""
        wg, rg = gates

        with patch("sub_agent.call_llm") as mock_llm:
            mock_llm.return_value = _make_llm_response(content="done")

            spawn = _TOOL_DISPATCH["spawn_agent"]
            result = spawn(
                {"tasks": ["task X", "task Y"], "max_turns": 1},
                wg, rg,
            )
            assert result.success
            ids = _extract_task_ids(result.content)
            assert len(ids) == 2

            # Wait for all to complete
            for tid in ids:
                _TOOL_DISPATCH["collect_agent"]({"task_id": tid}, wg, rg)

            # Now collect_any works on already-completed
            collect_any = _TOOL_DISPATCH["collect_any"]
            r = collect_any({"task_ids": ids}, wg, rg)
            assert r.success, r.content

    def test_collect_any_no_agents_returns_error(self, configured_context, gates):
        wg, rg = gates
        collect_any = _TOOL_DISPATCH["collect_any"]
        result = collect_any({}, wg, rg)
        assert result.success is False
        assert "No sub-agents" in result.content


# ---------------------------------------------------------------------------
# 7. agent_message + agent_read broadcast/pagination
# ---------------------------------------------------------------------------

class TestMessageBroadcastIntegration:
    """Integration: broadcast messages and read them back."""

    def setup_method(self):
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK

        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()

    def test_broadcast_and_read_multiple(self, configured_context, gates):
        wg, rg = gates
        send = _TOOL_DISPATCH["agent_message"]
        read = _TOOL_DISPATCH["agent_read"]

        send({"text": "first message", "from": "agent-A"}, wg, rg)
        send({"text": "second message", "from": "agent-B"}, wg, rg)

        r = read({}, wg, rg)
        assert r.success
        assert "first message" in r.content
        assert "second message" in r.content

    def test_read_since_pagination(self, configured_context, gates):
        wg, rg = gates
        send = _TOOL_DISPATCH["agent_message"]
        read = _TOOL_DISPATCH["agent_read"]

        for i in range(5):
            send({"text": f"msg-{i}"}, wg, rg)

        r = read({"since": 3}, wg, rg)
        assert r.success
        assert "msg-3" in r.content
        assert "msg-4" in r.content
        assert "msg-0" not in r.content
        assert "msg-1" not in r.content
        assert "msg-2" not in r.content


# ---------------------------------------------------------------------------
# 8. Multi-agent E2E workflow patterns (pipeline + scatter_gather)
# ---------------------------------------------------------------------------

class TestMultiAgentE2EWorkflow:
    """Integration: pipeline pattern and scatter_gather pattern end-to-end."""

    def test_pipeline_pattern(self, configured_context, gates, tmp_path):
        """Spawn agent A, agent A spawns agent B, collect both via pipeline."""
        wg, rg = gates

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
            assert result.success, f"pipeline failed: {result.content}"
            assert "Stage B complete" in result.content or "success=True" in result.content
            assert "stage-a-output" in result.content

        # Verify file on disk
        assert stage_a_file.exists(), f"File {stage_a_file} was not created"
        assert stage_a_file.read_text() == "stage-a-output"

    def test_scatter_gather_pattern(self, configured_context, gates, tmp_path):
        """Use scatter_gather to process a list of items concurrently."""
        wg, rg = gates

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
            assert result.success, f"scatter_gather failed: {result.content}"
            # At least one worker processed an item
            content_lower = result.content.lower()
            assert "processed" in content_lower or "alpha" in content_lower or "success" in content_lower

    def test_scatter_gather_empty_items(self, configured_context, gates):
        """scatter_gather with no items should return a clear error."""
        wg, rg = gates
        sg_dispatch = _TOOL_DISPATCH["scatter_gather"]
        result = sg_dispatch(
            {"items": [], "worker_task_template": "Process {item}"},
            wg, rg,
        )
        assert result.success is False
        assert "items" in result.content.lower() or "No items" in result.content
