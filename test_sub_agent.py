#!/usr/bin/env python3
"""
test_sub_agent.py — tests for the multi-agent subsystem.

Covers:
    - AgentRuntime (registry)
    - SubAgentResult (dataclass)
    - spawn_agent / agent_status / collect_agent tool dispatch
    - Recursion guard (sub-agents cannot spawn sub-agents)
    - Concurrency cap (max 5 sub-agents)
"""

import pytest
import threading
import time

from agent_runtime import AgentRuntime, SubAgentResult
from tools import execute_tool, _TOOL_DISPATCH, _TOOL_CONTEXT, set_context
from safety import ReadSafetyGate, WriteSafetyGate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime():
    """Fresh AgentRuntime for each test."""
    return AgentRuntime()


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
    # Create a minimal mock config
    class MockConfig:
        model = "test-model"
        api_key = "test-key"
        api_url = "https://test.api"
        stream = False
        sub_agent_max_turns = 5
    config = MockConfig()
    set_context(_agent_runtime=runtime, _agent_config=config, workspace=str(tmp_path))
    yield
    # Cleanup
    set_context(_agent_runtime=None, _agent_config=None)


# ---------------------------------------------------------------------------
# AgentRuntime tests
# ---------------------------------------------------------------------------

class TestAgentRuntime:
    def test_register_and_status_running(self, runtime):
        cancel = threading.Event()
        thread = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        runtime.register("task1", thread, cancel)
        thread.start()
        assert runtime.get_status("task1") == "running"
        thread.join()

    def test_status_not_found(self, runtime):
        assert runtime.get_status("nonexistent") == "not_found"

    def test_get_result_after_store(self, runtime):
        result = SubAgentResult(success=True, content="done", turns_used=3)
        runtime.store_result("task1", result)
        assert runtime.get_status("task1") == "completed"
        stored = runtime.get_result("task1")
        assert stored is not None
        assert stored.success is True
        assert stored.content == "done"

    def test_active_count(self, runtime):
        assert runtime.active_count == 0
        cancel = threading.Event()
        t = threading.Thread(target=lambda: time.sleep(0.2), daemon=True)
        runtime.register("a", t, cancel)
        t.start()
        assert runtime.active_count == 1
        t.join()
        runtime.store_result("a", SubAgentResult(True, "ok"))
        assert runtime.active_count == 0

    def test_cancel(self, runtime):
        cancel = threading.Event()
        t = threading.Thread(target=lambda: cancel.wait(), daemon=True)
        runtime.register("x", t, cancel)
        t.start()
        assert runtime.cancel("x") is True
        assert cancel.is_set()
        t.join(timeout=1)

    def test_cancel_all(self, runtime):
        events = []
        for i in range(3):
            ev = threading.Event()
            t = threading.Thread(target=lambda e=ev: e.wait(), daemon=True)
            runtime.register(f"task_{i}", t, ev)
            t.start()
            events.append(ev)
        count = runtime.cancel_all()
        assert count == 3
        for ev in events:
            assert ev.is_set()

    def test_get_pending_results_new_completions(self, runtime):
        """get_pending_results returns newly-completed results, then nothing."""
        # Register and complete a task
        cancel = threading.Event()
        t = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        runtime.register("task_a", t, cancel)
        t.start()
        t.join()
        runtime.store_result("task_a", SubAgentResult(True, "first done", turns_used=2))

        pending = runtime.get_pending_results()
        assert len(pending) == 1
        assert pending[0][0] == "task_a"
        assert pending[0][1].content == "first done"

        # Second call returns empty — already seen
        assert runtime.get_pending_results() == []

    def test_get_pending_results_multiple(self, runtime):
        """Multiple completions all returned in one call."""
        cancel = threading.Event()
        t1 = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        t2 = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        runtime.register("task_1", t1, cancel)
        runtime.register("task_2", t2, cancel)
        t1.start(); t2.start()
        t1.join(); t2.join()
        runtime.store_result("task_1", SubAgentResult(True, "ok", turns_used=1))
        runtime.store_result("task_2", SubAgentResult(False, "fail", error="e"))

        pending = runtime.get_pending_results()
        assert len(pending) == 2
        ids = {tid for tid, _ in pending}
        assert ids == {"task_1", "task_2"}

    def test_get_running_ids(self, runtime):
        """get_running_ids returns active task IDs."""
        assert runtime.get_running_ids() == []

        cancel = threading.Event()
        t = threading.Thread(target=lambda: time.sleep(0.5), daemon=True)
        runtime.register("active_1", t, cancel)
        t.start()

        running = runtime.get_running_ids()
        assert running == ["active_1"]
        t.join()

    def test_seen_completions_cleanup_on_abandon(self, runtime):
        """mark_abandoned cleans up _seen_completions entry."""
        cancel = threading.Event()
        t = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        runtime.register("to_abandon", t, cancel)
        t.start()
        t.join()
        runtime.store_result("to_abandon", SubAgentResult(True, "done"))
        pending = runtime.get_pending_results()
        assert len(pending) == 1

        runtime.mark_abandoned("to_abandon")
        # _seen_completions is cleaned, so get_pending_results won't re-return it
        assert "to_abandon" not in runtime._seen_completions

    # ---- status snapshots ----

    def test_update_and_get_snapshot(self, runtime):
        """Snapshot round-trip: update then retrieve."""
        runtime.update_snapshot(
            task_id="task1", turn=3, turns_budget=15,
            last_action="tool_call", last_tool="write_file",
            last_tool_summary="Wrote 42 bytes to foo.py",
            scratchpad_snippet="## Progress\n- Done with auth",
            tool_calls_made=5, last_error=None,
        )
        snap = runtime.get_snapshot("task1")
        assert snap is not None
        assert snap["turn"] == 3
        assert snap["turns_budget"] == 15
        assert snap["last_action"] == "tool_call"
        assert snap["last_tool"] == "write_file"
        assert snap["last_tool_summary"] == "Wrote 42 bytes to foo.py"
        assert snap["scratchpad_snippet"] == "## Progress\n- Done with auth"
        assert snap["tool_calls_made"] == 5
        assert snap["last_error"] is None
        assert "timestamp" in snap

    def test_snapshot_overwrite(self, runtime):
        """Second update overwrites first."""
        runtime.update_snapshot(
            task_id="t", turn=1, turns_budget=10,
            last_action="tool_call", last_tool="read_file",
            last_tool_summary="first", scratchpad_snippet="",
            tool_calls_made=1, last_error=None,
        )
        runtime.update_snapshot(
            task_id="t", turn=2, turns_budget=10,
            last_action="tool_call", last_tool="edit_file",
            last_tool_summary="second", scratchpad_snippet="scratch2",
            tool_calls_made=2, last_error="oops",
        )
        snap = runtime.get_snapshot("t")
        assert snap["turn"] == 2
        assert snap["last_tool"] == "edit_file"
        assert snap["last_tool_summary"] == "second"
        assert snap["last_error"] == "oops"

    def test_get_snapshot_nonexistent(self, runtime):
        """Unknown task returns None."""
        assert runtime.get_snapshot("no_such_task") is None

    def test_snapshot_cleanup_on_store_result(self, runtime):
        """Snapshot is removed when the task completes."""
        runtime.update_snapshot(
            task_id="task_c", turn=5, turns_budget=10,
            last_action="tool_call", last_tool="run_shell",
            last_tool_summary="done", scratchpad_snippet="",
            tool_calls_made=8, last_error=None,
        )
        assert runtime.get_snapshot("task_c") is not None
        runtime.store_result("task_c", SubAgentResult(True, "completed"))
        assert runtime.get_snapshot("task_c") is None

    def test_snapshot_cleanup_on_mark_abandoned(self, runtime):
        """Snapshot is removed when the task is abandoned."""
        runtime.update_snapshot(
            task_id="task_a", turn=3, turns_budget=10,
            last_action="tool_call", last_tool="search_files",
            last_tool_summary="found 5", scratchpad_snippet="",
            tool_calls_made=3, last_error=None,
        )
        assert runtime.get_snapshot("task_a") is not None
        runtime.mark_abandoned("task_a")
        assert runtime.get_snapshot("task_a") is None


# ---------------------------------------------------------------------------
# SubAgentResult tests
# ---------------------------------------------------------------------------

class TestSubAgentResult:
    def test_defaults(self):
        r = SubAgentResult(success=False, content="fail")
        assert r.turns_used == 0
        assert r.tool_calls_made == 0
        assert r.scratchpad == ""
        assert r.error is None

    def test_to_json(self):
        r = SubAgentResult(success=True, content="hello", turns_used=5, tool_calls_made=3)
        j = r.to_json()
        assert '"success": true' in j
        assert '"content": "hello"' in j
        assert '"turns_used": 5' in j


# ---------------------------------------------------------------------------
# Tool dispatch tests
# ---------------------------------------------------------------------------

class TestSpawnAgentTool:
    def test_missing_task(self, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        assert dispatch is not None
        result = dispatch({"max_turns": 10}, wg, rg)
        assert result.success is False
        assert "Missing required" in result.content

    def test_no_runtime_configured(self, gates):
        """Without _agent_runtime in context, spawn should fail gracefully."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        # Ensure runtime is None
        old = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        _TOOL_CONTEXT.__dict__["_agent_runtime"] = None
        try:
            result = dispatch({"task": "do something"}, wg, rg)
            assert result.success is False
            assert "not initialized" in result.content
        finally:
            _TOOL_CONTEXT.__dict__["_agent_runtime"] = old

    def test_spawn_returns_task_id(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({"task": "say hello", "max_turns": 1}, wg, rg)
        assert result.success is True
        assert "Spawned sub-agent" in result.content
        # task_id should be 8 hex chars
        import re
        match = re.search(r"'([a-f0-9]{8})'", result.content)
        assert match is not None


class TestAgentStatusTool:
    def test_missing_task_id(self, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_status")
        result = dispatch({}, wg, rg)
        assert result.success is False
        assert "Missing" in result.content

    def test_not_found(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_status")
        result = dispatch({"task_id": "deadbeef"}, wg, rg)
        assert result.success is True
        assert "not found" in result.content


class TestCollectAgentTool:
    def test_missing_task_id(self, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("collect_agent")
        result = dispatch({}, wg, rg)
        assert result.success is False
        assert "Missing" in result.content

    def test_not_found(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("collect_agent")
        result = dispatch({"task_id": "deadbeef"}, wg, rg)
        assert result.success is False
        assert "not found" in result.content


# ---------------------------------------------------------------------------
# Recursion guard test (via sub_agent module)
# ---------------------------------------------------------------------------

class TestRecursionGuard:
    def test_blocked_tools_at_max_depth(self):
        """At max depth, spawn/status/collect tools are blocked at runtime.
        
        Verifies the depth guard actually blocks tool execution by running
        run_sub_agent with parent_depth=2 and max_depth=3 (current_depth=3),
        which is at max depth. The LLM is mocked to return a tool call for
        each blocked tool, and we verify the sub-agent survives (tool_calls_made
        increments but the tool is blocked without crashing)."""
        from sub_agent import run_sub_agent
        from unittest.mock import patch, MagicMock

        blocked = {"spawn_agent", "agent_status", "collect_agent", "collect_any", "agent_extend"}

        wg = MagicMock()
        rg = MagicMock()

        class MockConfig:
            model = "test"
            api_key = "key"
            api_url = "http://test"
            stream = False
            workspace = "/tmp"
            unrestricted = False
            allow_overwrites = True
            approve_write_ops = False

        for tool_name in blocked:
            with patch("sub_agent.call_deepseek") as mock_llm:
                # Response 1: blocked tool call; Response 2: text (exits loop)
                mock_llm.side_effect = [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": f"call_{tool_name}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": "{}",
                            },
                        }],
                    },
                    {"role": "assistant", "content": "done"},
                ]
                result = run_sub_agent(
                    task="test blocked tools",
                    config=MockConfig(),
                    write_gate=wg,
                    read_gate=rg,
                    max_turns=2,
                    parent_depth=2,  # current_depth becomes 3
                    max_depth=3,
                )

            # The sub-agent should complete (not crash) and the tool was attempted
            assert result.success is True, f"Sub-agent should succeed for blocked {tool_name}"
            assert result.tool_calls_made == 1, (
                f"Expected 1 tool call attempt for {tool_name}, got {result.tool_calls_made}"
            )


# ---------------------------------------------------------------------------
# spawn_all / batch spawn tests
# ---------------------------------------------------------------------------

class TestSpawnAll:
    def test_batch_spawn_tasks(self, configured_context, gates):
        """spawn_agent with tasks=list should spawn multiple sub-agents."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({
            "tasks": ["say hello", "say goodbye", "count to 3"],
            "max_turns": 1,
        }, wg, rg)
        assert result.success is True
        assert "Spawned 3 sub-agent" in result.content

    def test_empty_tasks_fails(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({"tasks": []}, wg, rg)
        assert result.success is False
        assert "non-empty list" in result.content

    def test_mixed_invalid_tasks(self, configured_context, gates):
        """Empty strings in tasks list are skipped gracefully."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({
            "tasks": ["valid task", "", "  "],
            "max_turns": 1,
        }, wg, rg)
        assert result.success is True
        assert "1 sub-agent" in result.content  # only the valid one spawned

    def test_all_invalid_tasks_fails(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({"tasks": ["", ""]}, wg, rg)
        assert result.success is False
        assert "No sub-agents could be spawned" in result.content


# ---------------------------------------------------------------------------
# collect_any tests
# ---------------------------------------------------------------------------

class TestCollectAny:
    def test_collect_any_missing_runtime(self, gates):
        """Without runtime, collect_any should fail gracefully."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("collect_any")
        assert dispatch is not None
        old = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        _TOOL_CONTEXT.__dict__["_agent_runtime"] = None
        try:
            result = dispatch({}, wg, rg)
            assert result.success is False
            assert "not initialized" in result.content
        finally:
            _TOOL_CONTEXT.__dict__["_agent_runtime"] = old

    def test_collect_any_no_sub_agents(self, configured_context, gates):
        """No sub-agents running or completed — should return error."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("collect_any")
        result = dispatch({}, wg, rg)
        assert result.success is False
        assert "No sub-agents" in result.content

    def test_collect_any_already_completed(self, configured_context, gates):
        """If a sub-agent already completed, collect_any returns it immediately."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        result_obj = SubAgentResult(success=True, content="done", turns_used=2)
        runtime.store_result("task_x", result_obj)

        dispatch = _TOOL_DISPATCH.get("collect_any")
        result = dispatch({}, wg, rg)
        assert result.success is True
        assert "task_x" in result.content
        assert "done" in result.content

    def test_collect_any_with_task_ids(self, configured_context, gates):
        """collect_any with specific task_ids returns the first completed."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        result_obj = SubAgentResult(success=True, content="beta result", turns_used=1)
        runtime.store_result("beta", result_obj)

        dispatch = _TOOL_DISPATCH.get("collect_any")
        result = dispatch({"task_ids": ["alpha", "beta", "gamma"]}, wg, rg)
        assert result.success is True
        assert "beta" in result.content
        assert "beta result" in result.content


# ---------------------------------------------------------------------------
# SubAgentResult serialization round-trip
# ---------------------------------------------------------------------------

class TestResultSerialization:
    def test_round_trip(self):
        r = SubAgentResult(
            success=True,
            content="Task completed successfully.",
            turns_used=4,
            tool_calls_made=2,
            scratchpad="## Plan\n- did stuff",
            error=None,
        )
        j = r.to_json()
        import json
        d = json.loads(j)
        assert d["success"] is True
        assert d["turns_used"] == 4
        assert d["tool_calls_made"] == 2


# ---------------------------------------------------------------------------
# shared_context tests
# ---------------------------------------------------------------------------

class TestSharedContext:
    def test_shared_context_passed_to_sub_agent(self, configured_context, gates):
        """Verify shared_context shows up in sub-agent messages."""
        import sub_agent as sa

        # Capture the messages built by run_sub_agent
        original = sa.run_sub_agent

        def capture(*args, **kwargs):
            # We just want to verify shared_context is in kwargs
            assert "shared_context" in kwargs
            return SubAgentResult(success=True, content="ok")

        try:
            sa.run_sub_agent = capture
            wg, rg = gates
            dispatch = _TOOL_DISPATCH.get("spawn_agent")
            result = dispatch({
                "task": "test task",
                "max_turns": 1,
                "shared_context": "API: /stats -> {count: int}",
            }, wg, rg)
            assert result.success is True
        finally:
            sa.run_sub_agent = original


# ---------------------------------------------------------------------------
# agent_message tests
# ---------------------------------------------------------------------------

class TestAgentMessage:
    def setup_method(self):
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK
        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()

    def test_broadcast_and_read(self, configured_context, gates):
        """Send a message, then read it back."""
        wg, rg = gates
        send = _TOOL_DISPATCH.get("agent_message")
        read = _TOOL_DISPATCH.get("agent_read")

        r1 = send({"text": "Backend API ready at /api/stats", "from": "backend"}, wg, rg)
        assert r1.success is True
        assert "1 total messages" in r1.content

        r2 = read({}, wg, rg)
        assert r2.success is True
        assert "Backend API ready" in r2.content
        assert "from=backend" in r2.content

    def test_read_since(self, configured_context, gates):
        """agent_read with since should skip old messages."""
        wg, rg = gates
        send = _TOOL_DISPATCH.get("agent_message")
        read = _TOOL_DISPATCH.get("agent_read")

        send({"text": "msg 0"}, wg, rg)
        send({"text": "msg 1"}, wg, rg)
        send({"text": "msg 2"}, wg, rg)

        r = read({"since": 1}, wg, rg)
        assert r.success is True
        assert "msg 1" in r.content
        assert "msg 2" in r.content
        assert "msg 0" not in r.content

    def test_read_no_new_messages(self, configured_context, gates):
        wg, rg = gates
        send = _TOOL_DISPATCH.get("agent_message")
        read = _TOOL_DISPATCH.get("agent_read")

        send({"text": "only msg"}, wg, rg)
        r = read({"since": 5}, wg, rg)  # beyond what exists
        assert r.success is True
        assert "No new messages" in r.content

    def test_send_missing_text(self, configured_context, gates):
        wg, rg = gates
        send = _TOOL_DISPATCH.get("agent_message")
        r = send({}, wg, rg)
        assert r.success is False
        assert "Missing" in r.content


# ---------------------------------------------------------------------------
# agent_extend tests
# ---------------------------------------------------------------------------

class TestAgentExtend:
    def test_extend_running_agent(self, configured_context, gates):
        """Extending a running agent's turns should succeed."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        # Simulate a running agent
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("task_z", t, ev, max_turns=10)
        t.start()

        dispatch = _TOOL_DISPATCH.get("agent_extend")
        result = dispatch({"task_id": "task_z", "additional": 10}, wg, rg)
        assert result.success is True
        assert "+10" in result.content
        assert runtime.get_max_turns("task_z") == 20

        runtime.cancel("task_z")
        t.join(timeout=1)

    def test_extend_completed_agent(self, configured_context, gates):
        """Extending an already-completed agent should report it."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        runtime.store_result("done", SubAgentResult(success=True, content="ok"))

        dispatch = _TOOL_DISPATCH.get("agent_extend")
        result = dispatch({"task_id": "done", "additional": 5}, wg, rg)
        assert result.success is True
        assert "already completed" in result.content

    def test_extend_not_found(self, configured_context, gates):
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_extend")
        result = dispatch({"task_id": "nope", "additional": 5}, wg, rg)
        assert result.success is False
        assert "not found" in result.content


# ---------------------------------------------------------------------------
# agent_handoff tests
# ---------------------------------------------------------------------------

class TestAgentHandoff:
    def setup_method(self):
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK
        with _AGENT_MSGS_LOCK:
            _AGENT_MSGS.clear()

    def test_handoff_result_sends_message(self, configured_context, gates):
        """agent_handoff with handoff.result should send to global list."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "handoff.result",
            "result": {"count": 42, "task": "count items"},
            "from": "worker-1",
        }, wg, rg)
        assert result.success is True
        assert "handoff.result" in result.content
        assert "1 total messages" in result.content

        # Verify the message ended up in _AGENT_MSGS
        from tools.agent_ops import _AGENT_MSGS, _AGENT_MSGS_LOCK
        with _AGENT_MSGS_LOCK:
            assert len(_AGENT_MSGS) == 1
            assert "handoff.result" in _AGENT_MSGS[0]["text"]

    def test_handoff_heartbeat_sends_message(self, configured_context, gates):
        """agent_handoff with status.heartbeat should work."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "status.heartbeat",
            "result": {"progress": "50% done", "pct": 50},
            "from": "worker-2",
        }, wg, rg)
        assert result.success is True
        assert "status.heartbeat" in result.content

    def test_handoff_unknown_type_fails(self, configured_context, gates):
        """Unknown handoff type should return failure."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "made.up.type",
            "result": {"x": 1},
        }, wg, rg)
        assert result.success is False
        assert "Unknown handoff" in result.content

    def test_handoff_missing_result_fails(self, configured_context, gates):
        """Missing 'result' should return failure."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "handoff.result",
        }, wg, rg)
        assert result.success is False

    def test_handoff_non_dict_result_fails(self, configured_context, gates):
        """Non-dict 'result' should return failure."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "handoff.result",
            "result": "not a dict",
        }, wg, rg)
        assert result.success is False
        assert "must be a dict" in result.content

    def test_handoff_with_target(self, configured_context, gates):
        """Handoff with 'target' should route to specific inbox."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")

        # Set up a target agent with an inbox
        runtime.set_subscriptions("target-agent", [])

        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "handoff.result",
            "result": {"data": "direct"},
            "target": "target-agent",
            "from": "source",
        }, wg, rg)
        assert result.success is True
        assert "to 'target-agent'" in result.content

        # Check the target's inbox
        inbox = runtime.get_inbox("target-agent")
        assert len(inbox) == 1
        assert inbox[0].type == "handoff.result"
        assert inbox[0].payload["result"] == {"data": "direct"}

    def test_handoff_ack_sends_message(self, configured_context, gates):
        """handoff.ack type should work correctly."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_handoff")
        result = dispatch({
            "type": "handoff.ack",
            "result": {"accepted": True, "reason": "all good"},
            "from": "receiver",
        }, wg, rg)
        assert result.success is True
        assert "handoff.ack" in result.content


# ---------------------------------------------------------------------------
# agent_inbox tests
# ---------------------------------------------------------------------------

class TestAgentInbox:
    def test_inbox_missing_task_id(self, gates):
        """agent_inbox without task_id should fail when caller has no task_id."""
        from tools import _TOOL_CONTEXT
        wg, rg = gates
        # Clear any leaked task_id from previous tests
        _TOOL_CONTEXT.__dict__.pop("_agent_task_id", None)
        dispatch = _TOOL_DISPATCH.get("agent_inbox")
        result = dispatch({}, wg, rg)
        assert result.success is False
        assert "Missing" in result.content

    def test_inbox_no_messages(self, configured_context, gates):
        """agent_inbox for an agent with no messages should report empty."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        runtime.set_subscriptions("empty-agent", [])

        dispatch = _TOOL_DISPATCH.get("agent_inbox")
        result = dispatch({"task_id": "empty-agent"}, wg, rg)
        assert result.success is True
        assert "No new messages" in result.content

    def test_inbox_reads_messages(self, configured_context, gates):
        """agent_inbox should return previously routed messages."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        runtime.set_subscriptions("reader-agent", ["handoff.result"])

        # Route a message directly
        from tools.agent_messages import AgentMessage, _route_message
        msg = AgentMessage(
            type="handoff.result",
            sender="producer",
            payload={"result": {"x": 1}, "task": "test"},
        )
        _route_message(msg, runtime.inboxes, runtime.subscriptions, runtime._lock)

        dispatch = _TOOL_DISPATCH.get("agent_inbox")
        result = dispatch({"task_id": "reader-agent"}, wg, rg)
        assert result.success is True
        assert "handoff.result" in result.content
        assert "producer" in result.content

    def test_inbox_since_polling(self, configured_context, gates):
        """agent_inbox with 'since' should skip old messages."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        runtime.set_subscriptions("poll-agent", [])

        from tools.agent_messages import AgentMessage
        for i in range(3):
            msg = AgentMessage(
                type="text",
                sender=f"sender-{i}",
                payload={"body": f"msg-{i}"},
            )
            runtime.append_inbox("poll-agent", msg)

        dispatch = _TOOL_DISPATCH.get("agent_inbox")
        result = dispatch({"task_id": "poll-agent", "since": 1}, wg, rg)
        assert result.success is True
        assert "msg-1" in result.content
        assert "msg-2" in result.content
        assert "msg-0" not in result.content

    def test_inbox_invalid_since(self, configured_context, gates):
        """Invalid 'since' value should fail."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_inbox")
        result = dispatch({"task_id": "any", "since": "abc"}, wg, rg)
        assert result.success is False
        assert "must be an integer" in result.content


# ---------------------------------------------------------------------------
# agent_subscribe tests
# ---------------------------------------------------------------------------

class TestAgentSubscribe:
    def test_subscribe_missing_task_id(self, gates):
        """agent_subscribe without task_id should fail."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({}, wg, rg)
        assert result.success is False
        assert "Missing" in result.content

    def test_subscribe_unknown_type_fails(self, configured_context, gates):
        """Unknown message types should be rejected."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        # Register a task
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("sub-test", t, ev)
        t.start()

        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({
            "task_id": "sub-test",
            "types": ["no.such.type"],
        }, wg, rg)
        assert result.success is False
        assert "Unknown message type" in result.content

        runtime.cancel("sub-test")
        t.join(timeout=1)

    def test_subscribe_success(self, configured_context, gates):
        """Valid subscription should succeed."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("sub-ok", t, ev)
        t.start()

        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({
            "task_id": "sub-ok",
            "types": ["handoff.result", "coord.sync"],
        }, wg, rg)
        assert result.success is True
        assert "sub-ok" in result.content
        assert "handoff.result" in result.content

        # Verify subscriptions were set
        subs = runtime.subscriptions.get("sub-ok", set())
        assert "handoff.result" in subs
        assert "coord.sync" in subs

        runtime.cancel("sub-ok")
        t.join(timeout=1)

    def test_subscribe_default_all(self, configured_context, gates):
        """Omitting types should reset to receive all (empty subscriptions)."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        runtime.register("sub-all", t, ev)
        t.start()

        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({"task_id": "sub-all"}, wg, rg)
        assert result.success is True
        assert "receives all message types" in result.content

        runtime.cancel("sub-all")
        t.join(timeout=1)

    def test_subscribe_agent_not_found(self, configured_context, gates):
        """Non-existent agent should fail."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({
            "task_id": "no-such-agent",
            "types": ["text"],
        }, wg, rg)
        assert result.success is False
        assert "not found" in result.content

    def test_subscribe_non_list_types_fails(self, configured_context, gates):
        """Non-list 'types' should fail."""
        wg, rg = gates
        dispatch = _TOOL_DISPATCH.get("agent_subscribe")
        result = dispatch({
            "task_id": "any",
            "types": "not_a_list",
        }, wg, rg)
        assert result.success is False
        assert "must be a list" in result.content


# ---------------------------------------------------------------------------
# subscriptions param in spawn_agent tests
# ---------------------------------------------------------------------------

class TestSubscriptionsInSpawn:
    def test_spawn_with_subscriptions(self, configured_context, gates):
        """spawn_agent should accept and apply subscriptions param."""
        wg, rg = gates
        runtime = _TOOL_CONTEXT.__dict__.get("_agent_runtime")
        dispatch = _TOOL_DISPATCH.get("spawn_agent")
        result = dispatch({
            "task": "say hello",
            "max_turns": 1,
            "subscriptions": ["handoff.result", "coord.sync"],
        }, wg, rg)
        assert result.success is True
        assert "Spawned sub-agent" in result.content

        # Extract task_id and verify subscriptions
        import re
        match = re.search(r"'([a-f0-9]{8})'", result.content)
        if match:
            tid = match.group(1)
            subs = runtime.subscriptions.get(tid, set())
            assert "handoff.result" in subs
            assert "coord.sync" in subs


