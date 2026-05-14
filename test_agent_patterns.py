#!/usr/bin/env python3
"""test_agent_patterns.py — tests for multi-agent coordination pattern helpers."""

import time
import threading
from unittest.mock import MagicMock, patch

from agent_runtime import AgentRuntime, SubAgentResult


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_runtime(task_statuses=None, task_results=None, inboxes=None):
    """Create a mock AgentRuntime with canned status/result/inbox maps."""
    runtime = MagicMock(spec=AgentRuntime)
    runtime.active_count = 0
    runtime._condition = threading.Condition()

    if task_statuses is None:
        task_statuses = {}
    if task_results is None:
        task_results = {}
    if inboxes is None:
        inboxes = {}

    def _get_status(tid):
        return task_statuses.get(tid, "not_found")

    def _get_result(tid):
        return task_results.get(tid)

    def _get_inbox(tid):
        return list(inboxes.get(tid, []))

    runtime.get_status.side_effect = _get_status
    runtime.get_result.side_effect = _get_result
    runtime.get_inbox.side_effect = _get_inbox

    return runtime, task_statuses, task_results, inboxes


def _result(success=True, content="ok", error=None):
    return SubAgentResult(success=success, content=content, error=error)


# ------------------------------------------------------------------
# fan_out tests
# ------------------------------------------------------------------

class TestFanOut:
    """Tests for fan_out."""

    def test_returns_task_ids(self):
        """fan_out spawns one task_id per description."""
        from tools.agent_patterns import fan_out

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.side_effect = lambda desc, *a, **kw: f"tid-{desc[:4]}"
            ids = fan_out(
                ["task-alpha", "task-beta", "task-gamma"],
                runtime=runtime,
                config=MagicMock(),
            )

        assert len(ids) == 3
        assert ids[0].startswith("tid-")
        assert mock_spawn.call_count == 3

    def test_stops_at_max_concurrent(self):
        """fan_out stops spawning when active_count >= _MAX_CONCURRENT."""
        from tools.agent_patterns import fan_out

        runtime = MagicMock(spec=AgentRuntime)
        # Set active_count at the max — fan_out should spawn zero new agents
        runtime.active_count = 5
        runtime._condition = threading.Condition()

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.return_value = "tid"
            ids = fan_out(
                ["a", "b", "c", "d", "e", "f"],
                runtime=runtime,
                config=MagicMock(),
            )

        assert mock_spawn.call_count == 0
        assert ids == []

    def test_passes_shared_context(self):
        """fan_out should serialize shared_input into shared_context."""
        from tools.agent_patterns import fan_out

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.return_value = "tid-ctx"
            fan_out(
                ["one task"],
                shared_input={"key": "value"},
                runtime=runtime,
                config=MagicMock(),
            )

        call_kwargs = mock_spawn.call_args[1]
        shared_ctx = call_kwargs.get("shared_context", "")
        assert '"key"' in shared_ctx
        assert '"value"' in shared_ctx


# ------------------------------------------------------------------
# fan_in tests
# ------------------------------------------------------------------

class TestFanIn:
    """Tests for fan_in."""

    def test_collects_all_results(self):
        """fan_in returns results in order of task_ids."""
        from tools.agent_patterns import fan_in

        res_a = _result(content="a-done")
        res_b = _result(content="b-done")
        res_c = _result(content="c-done")

        runtime, _, _, _ = _make_runtime(
            task_statuses={"a": "completed", "b": "completed", "c": "completed"},
            task_results={"a": res_a, "b": res_b, "c": res_c},
        )

        results = fan_in(["a", "b", "c"], runtime=runtime, timeout=5.0)
        assert len(results) == 3
        assert results[0] is res_a
        assert results[1] is res_b
        assert results[2] is res_c

    def test_not_found_yields_none(self):
        """fan_in returns None for task_ids with 'not_found' status."""
        from tools.agent_patterns import fan_in

        runtime, _, _, _ = _make_runtime(
            task_statuses={"ghost": "not_found"},
            task_results={},
        )

        results = fan_in(["ghost"], runtime=runtime, timeout=2.0)
        assert len(results) == 1
        assert results[0] is None

    def test_empty_list(self):
        """fan_in on empty task_ids returns empty list."""
        from tools.agent_patterns import fan_in

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()
        results = fan_in([], runtime=runtime)
        assert results == []

    def test_respects_timeout(self):
        """fan_in returns None for tasks that timeout.
        
        Uses a threading.Event to control the ready predicate instead of
        relying on a sub-second sleep-based timeout. The Event is never set,
        so wait_for blocks until the deadline expires."""
        from tools.agent_patterns import fan_in
        from threading import Event

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()

        complete_ev = Event()  # never set — simulates a stuck task

        def _get_status(tid):
            if complete_ev.is_set():
                return "completed"
            return "running"

        runtime.get_status.side_effect = _get_status

        results = fan_in(["slow"], runtime=runtime, timeout=0.5)
        assert len(results) == 1
        assert results[0] is None


# ------------------------------------------------------------------
# pipeline tests
# ------------------------------------------------------------------

class TestPipeline:
    """Tests for pipeline."""

    def test_sequential_stages(self):
        """pipeline runs stages one after the other."""
        from tools.agent_patterns import pipeline

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()
        runtime.get_status.return_value = "completed"
        runtime.get_result.return_value = _result(content="stage done")

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.return_value = "tid-stage"
            result = pipeline(
                [
                    {"task": "stage 1"},
                    {"task": "stage 2"},
                    {"task": "stage 3"},
                ],
                runtime=runtime,
                config=MagicMock(),
                timeout=5.0,
            )

        assert mock_spawn.call_count == 3
        assert result is not None
        assert result.success

    def test_early_failure_stops_pipeline(self):
        """A failed stage returns immediately — subsequent stages never spawn."""
        from tools.agent_patterns import pipeline

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()

        call_count = [0]

        def _status_sequence(tid=None):
            return "completed"

        def _result_sequence(tid=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _result(success=True, content="ok")
            else:
                return _result(success=False, content="fail")

        runtime.get_status.side_effect = _status_sequence
        runtime.get_result.side_effect = _result_sequence

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.return_value = "tid-pipe"
            result = pipeline(
                [
                    {"task": "stage 1"},
                    {"task": "stage 2 (fails)"},
                    {"task": "stage 3 (never runs)"},
                ],
                runtime=runtime,
                config=MagicMock(),
                timeout=5.0,
            )

        # Only 2 stages spawned (third never runs due to failure)
        assert mock_spawn.call_count == 2
        assert result is not None
        assert not result.success


# ------------------------------------------------------------------
# barrier tests
# ------------------------------------------------------------------

# Full coord.sync payload schema: barrier, arrived, total are all required
def _sync_msg(barrier_name):
    from tools.agent_messages import AgentMessage
    return AgentMessage(
        sender="worker",
        type="coord.sync",
        payload={"barrier": barrier_name, "arrived": 1, "total": 3},
    )


def _text_msg(body):
    from tools.agent_messages import AgentMessage
    return AgentMessage(sender="worker", type="text", payload={"body": body})


class TestBarrier:
    """Tests for barrier."""

    def test_all_arrived_returns_true(self):
        """barrier returns True when all agents sent coord.sync."""
        from tools.agent_patterns import barrier

        runtime, _, _, inboxes = _make_runtime(
            task_statuses={"a": "running", "b": "running", "c": "running"},
            task_results={},
            inboxes={
                "a": [_sync_msg("phase1")],
                "b": [_sync_msg("phase1")],
                "c": [_sync_msg("phase1")],
            },
        )

        arrived = barrier("phase1", ["a", "b", "c"], runtime=runtime, timeout=2.0)
        assert arrived is True

    def test_partial_arrival_returns_false(self):
        """barrier returns False when some agents haven't arrived.
        
        Uses threading.Event to control inbox delivery instead of relying
        on a sub-second timeout. The Event gates whether agent "b" has arrived."""
        from tools.agent_patterns import barrier
        from threading import Event

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()

        b_arrived = Event()  # never set — simulates agent "b" never arriving

        def _get_inbox(tid):
            if tid == "a":
                return [_sync_msg("phase1")]
            elif tid == "b" and b_arrived.is_set():
                return [_sync_msg("phase1")]
            return []

        runtime.get_inbox.side_effect = _get_inbox
        runtime.get_status.return_value = "running"

        arrived = barrier("phase1", ["a", "b"], runtime=runtime, timeout=0.5)
        assert arrived is False

    def test_wrong_barrier_name_ignored(self):
        """barrier ignores coord.sync messages with a different barrier name.
        
        Uses threading.Event to control delivery of correctly-named messages.
        The Event is never set, so the barrier name never matches."""
        from tools.agent_patterns import barrier
        from threading import Event

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()

        correct_name = Event()  # never set — simulates wrong barrier name

        def _get_inbox(tid):
            if correct_name.is_set():
                return [_sync_msg("phase1")]
            return [_sync_msg("other")]

        runtime.get_inbox.side_effect = _get_inbox
        runtime.get_status.return_value = "running"

        arrived = barrier("phase1", ["a"], runtime=runtime, timeout=0.5)
        assert arrived is False

    def test_empty_task_list_returns_true(self):
        """barrier with no tasks should return True immediately."""
        from tools.agent_patterns import barrier

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()
        arrived = barrier("phase1", [], runtime=runtime, timeout=1.0)
        assert arrived is True

    def test_handles_non_sync_messages(self):
        """barrier ignores messages that aren't coord.sync."""
        from tools.agent_patterns import barrier

        runtime, _, _, inboxes = _make_runtime(
            task_statuses={"a": "running"},
            task_results={},
            inboxes={
                "a": [
                    _text_msg("hello"),
                    _sync_msg("phase1"),
                ],
            },
        )

        arrived = barrier("phase1", ["a"], runtime=runtime, timeout=2.0)
        assert arrived is True


# ------------------------------------------------------------------
# scatter_gather tests
# ------------------------------------------------------------------

class TestScatterGather:
    """Tests for scatter_gather."""

    def test_distributes_items_to_workers(self):
        """scatter_gather replaces {item} in template for each worker."""
        from tools.agent_patterns import scatter_gather

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()
        runtime.get_status.return_value = "completed"
        runtime.get_result.return_value = _result(content="worker done")

        spawn_descriptions = []

        def _fake_spawn(desc, config, runtime, wg, rg, max_turns, **kw):
            spawn_descriptions.append(desc)
            return f"tid-{desc[:8]}"

        with patch("tools.agent_ops._spawn_one", _fake_spawn):
            results = scatter_gather(
                items=["apple", "banana", "cherry"],
                worker_task_template="Process {item}",
                runtime=runtime,
                config=MagicMock(),
            )

        assert len(spawn_descriptions) == 3
        assert "Process apple" in spawn_descriptions[0]
        assert "Process banana" in spawn_descriptions[1]
        assert "Process cherry" in spawn_descriptions[2]
        assert len(results) == 3

    def test_empty_items_returns_empty(self):
        """scatter_gather with empty items returns []."""
        from tools.agent_patterns import scatter_gather

        runtime = MagicMock(spec=AgentRuntime)
        runtime._condition = threading.Condition()

        results = scatter_gather(
            items=[],
            worker_task_template="Process {item}",
            runtime=runtime,
            config=MagicMock(),
        )

        assert results == []

    def test_passes_subscriptions_through(self):
        """scatter_gather forwards subscriptions to fan_out."""
        from tools.agent_patterns import scatter_gather

        runtime = MagicMock(spec=AgentRuntime)
        runtime.active_count = 0
        runtime._condition = threading.Condition()
        runtime.get_status.return_value = "completed"
        runtime.get_result.return_value = _result()

        with patch("tools.agent_ops._spawn_one") as mock_spawn:
            mock_spawn.return_value = "tid-sub"
            scatter_gather(
                items=["x"],
                worker_task_template="Do {item}",
                runtime=runtime,
                config=MagicMock(),
                subscriptions=["handoff.result"],
            )

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs.get("subscriptions") == ["handoff.result"]
