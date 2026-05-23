#!/usr/bin/env python3
"""Tests for tools/agent_patterns.py — fan_out, fan_in, pipeline, barrier, scatter_gather."""

from __future__ import annotations

import unittest
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from agent_runtime import AgentRuntime, SubAgentResult


class TestFanOut(unittest.TestCase):
    """fan_out spawns N workers from a list of task descriptions."""

    @patch("tools._TOOL_CONTEXT")
    @patch("tools.agent_ops._spawn_one")
    @patch("tools.agent_ops._MAX_CONCURRENT", 10)
    def test_fan_out_two_descriptions(self, mock_spawn_one, mock_ctx):
        """fan_out with 2 descs spawns 2 agents and returns their task_ids."""
        mock_runtime = MagicMock(spec=AgentRuntime)
        mock_runtime.active_count = 0
        mock_config = MagicMock()
        mock_ctx._agent_runtime = mock_runtime
        mock_ctx._agent_config = mock_config

        mock_spawn_one.side_effect = ["task-aaa", "task-bbb"]

        from tools.agent_patterns import fan_out

        task_ids = fan_out(
            descriptions=["do thing A", "do thing B"],
            max_turns=10,
        )

        self.assertEqual(task_ids, ["task-aaa", "task-bbb"])
        self.assertEqual(mock_spawn_one.call_count, 2)

        # Verify first call args (positional: task, config, runtime, wg, rg, max_turns)
        call_args_0 = mock_spawn_one.call_args_list[0]
        self.assertEqual(call_args_0[0][0], "do thing A")  # desc
        self.assertEqual(call_args_0[0][5], 10)  # max_turns is positional arg 6

        call_args_1 = mock_spawn_one.call_args_list[1]
        self.assertEqual(call_args_1[0][0], "do thing B")  # desc
        self.assertEqual(call_args_1[0][5], 10)

    @patch("tools._TOOL_CONTEXT")
    @patch("tools.agent_ops._spawn_one")
    @patch("tools.agent_ops._MAX_CONCURRENT", 10)
    def test_fan_out_respects_max_concurrent(self, mock_spawn_one, mock_ctx):
        """fan_out stops spawning when active_count >= _MAX_CONCURRENT."""
        mock_runtime = MagicMock(spec=AgentRuntime)
        mock_runtime.active_count = 10  # already at max
        mock_config = MagicMock()
        mock_ctx._agent_runtime = mock_runtime
        mock_ctx._agent_config = mock_config

        from tools.agent_patterns import fan_out

        task_ids = fan_out(
            descriptions=["do thing A", "do thing B", "do thing C"],
            max_turns=10,
        )

        self.assertEqual(task_ids, [])
        mock_spawn_one.assert_not_called()


class TestFanIn(unittest.TestCase):
    """fan_in collects results from a list of task_ids."""

    def _make_result(self, content: str, success: bool = True) -> SubAgentResult:
        return SubAgentResult(
            success=success,
            content=content,
            turns_used=2,
            tool_calls_made=1,
            scratchpad="",
            error=None,
        )

    @patch("tools._TOOL_CONTEXT")
    def test_fan_in_all_completed(self, mock_ctx):
        """fan_in collects results when all tasks are completed."""
        runtime = AgentRuntime()

        # Pre-populate results so get_status returns "completed"
        result_a = self._make_result("result A")
        result_b = self._make_result("result B")
        runtime.results["task-aaa"] = result_a
        runtime.results["task-bbb"] = result_b

        mock_ctx._agent_runtime = runtime

        from tools.agent_patterns import fan_in

        results = fan_in(
            task_ids=["task-aaa", "task-bbb"],
            runtime=runtime,
            timeout=5.0,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].content, "result A")
        self.assertEqual(results[1].content, "result B")

    @patch("tools._TOOL_CONTEXT")
    def test_fan_in_not_found_returns_none(self, mock_ctx):
        """fan_in returns None for not_found tasks."""
        runtime = AgentRuntime()
        mock_ctx._agent_runtime = runtime

        from tools.agent_patterns import fan_in

        results = fan_in(
            task_ids=["nonexistent-task"],
            runtime=runtime,
            timeout=1.0,
        )

        self.assertEqual(results, [None])


class TestPipeline(unittest.TestCase):
    """pipeline runs stages sequentially."""

    def _make_result(self, content: str) -> SubAgentResult:
        return SubAgentResult(
            success=True,
            content=content,
            turns_used=3,
            tool_calls_made=2,
            scratchpad="",
        )

    @patch("tools._TOOL_CONTEXT")
    @patch("tools.agent_ops._spawn_one")
    def test_pipeline_stages_execute_in_order(self, mock_spawn_one, mock_ctx):
        """pipeline runs stages sequentially: spawn → wait → spawn → wait."""
        runtime = AgentRuntime()
        mock_config = MagicMock()
        mock_ctx._agent_runtime = runtime
        mock_ctx._agent_config = mock_config

        # Each spawn returns a unique task_id
        mock_spawn_one.side_effect = ["tid-stage-0", "tid-stage-1", "tid-stage-2"]

        # Store results directly so get_status returns "completed" for each
        runtime.results["tid-stage-0"] = self._make_result("stage 0 output")
        runtime.results["tid-stage-1"] = self._make_result("stage 1 output")
        runtime.results["tid-stage-2"] = self._make_result("stage 2 output")

        # Also set up subscriptions for each task so pipeline can read
        runtime.set_subscriptions("tid-stage-0", [])
        runtime.set_subscriptions("tid-stage-1", ["handoff.result"])
        runtime.set_subscriptions("tid-stage-2", ["handoff.result"])

        from tools.agent_patterns import pipeline

        result = pipeline(
            stages=[
                {"task": "stage 0: init"},
                {"task": "stage 1: process", "subscriptions": ["handoff.result"]},
                {"task": "stage 2: finish", "subscriptions": ["handoff.result"]},
            ],
            runtime=runtime,
            config=mock_config,
            max_turns=10,
            timeout=30.0,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.content, "stage 2 output")
        self.assertEqual(mock_spawn_one.call_count, 3)

        # Verify spawn order
        spawned_descs = [call[0][0] for call in mock_spawn_one.call_args_list]
        self.assertEqual(spawned_descs[0], "stage 0: init")
        self.assertEqual(spawned_descs[1], "stage 1: process")
        self.assertEqual(spawned_descs[2], "stage 2: finish")

    @patch("tools._TOOL_CONTEXT")
    @patch("tools.agent_ops._spawn_one")
    def test_pipeline_stops_on_failure(self, mock_spawn_one, mock_ctx):
        """pipeline returns early if a stage fails."""
        runtime = AgentRuntime()
        mock_config = MagicMock()
        mock_ctx._agent_runtime = runtime
        mock_ctx._agent_config = mock_config

        mock_spawn_one.side_effect = ["tid-0", "tid-1"]

        # First stage succeeds, second fails
        runtime.results["tid-0"] = self._make_result("stage 0 ok")
        runtime.results["tid-1"] = SubAgentResult(
            success=False, content="", turns_used=1, tool_calls_made=0, error="boom"
        )
        runtime.set_subscriptions("tid-0", [])
        runtime.set_subscriptions("tid-1", [])
        runtime.set_subscriptions("tid-0", ["handoff.result"])
        runtime.set_subscriptions("tid-1", ["handoff.result"])

        from tools.agent_patterns import pipeline

        result = pipeline(
            stages=[
                {"task": "stage 0"},
                {"task": "stage 1"},
                {"task": "stage 2 (never runs)"},
            ],
            runtime=runtime,
            config=mock_config,
            max_turns=10,
            timeout=30.0,
        )

        self.assertIsNotNone(result)  # Returns the failed result, not None
        self.assertFalse(result.success)
        # stage 2 should never have spawned
        self.assertEqual(mock_spawn_one.call_count, 2)


class TestBarrier(unittest.TestCase):
    """barrier blocks until all task_ids send coord.sync."""

    def _make_sync_msg(self, barrier_name: str) -> MagicMock:
        msg = MagicMock()
        msg.type = "coord.sync"
        msg.payload = {"barrier": barrier_name}
        return msg

    @patch("tools._TOOL_CONTEXT")
    def test_barrier_all_synced(self, mock_ctx):
        """barrier returns True when all agents have sent coord.sync."""
        runtime = AgentRuntime()
        mock_ctx._agent_runtime = runtime

        # Pre-populate inboxes with sync messages
        msg_a = self._make_sync_msg("phase-1")
        msg_b = self._make_sync_msg("phase-1")
        runtime.inboxes["tid-a"] = [msg_a]
        runtime.inboxes["tid-b"] = [msg_b]

        from tools.agent_patterns import barrier

        success = barrier(
            name="phase-1",
            task_ids=["tid-a", "tid-b"],
            runtime=runtime,
            timeout=5.0,
        )

        self.assertTrue(success)

    @patch("tools._TOOL_CONTEXT")
    def test_barrier_not_all_synced(self, mock_ctx):
        """barrier returns False when not all agents have synced."""
        runtime = AgentRuntime()
        mock_ctx._agent_runtime = runtime

        # Only one agent sent sync
        msg_a = self._make_sync_msg("phase-1")
        runtime.inboxes["tid-a"] = [msg_a]
        runtime.inboxes["tid-b"] = []  # no sync message

        from tools.agent_patterns import barrier

        success = barrier(
            name="phase-1",
            task_ids=["tid-a", "tid-b"],
            runtime=runtime,
            timeout=0.5,
        )

        self.assertFalse(success)

    @patch("tools._TOOL_CONTEXT")
    def test_barrier_differs_by_name(self, mock_ctx):
        """barrier only counts messages with the matching barrier name."""
        runtime = AgentRuntime()
        mock_ctx._agent_runtime = runtime

        msg_a1 = self._make_sync_msg("phase-1")
        msg_a2 = self._make_sync_msg("phase-2")  # wrong name
        runtime.inboxes["tid-a"] = [msg_a1, msg_a2]
        runtime.inboxes["tid-b"] = [msg_a2]  # only phase-2

        from tools.agent_patterns import barrier

        # Asking for phase-1 should fail because tid-b only has phase-2
        success = barrier(
            name="phase-1",
            task_ids=["tid-a", "tid-b"],
            runtime=runtime,
            timeout=0.5,
        )

        self.assertFalse(success)


class TestScatterGather(unittest.TestCase):
    """scatter_gather distributes items to workers via template."""

    @patch("tools.agent_patterns.fan_in")
    @patch("tools.agent_patterns.fan_out")
    def test_scatter_gather_template_substitution(self, mock_fan_out, mock_fan_in):
        """scatter_gather substitutes {item} into each worker's task template."""
        mock_fan_out.return_value = ["tid-0", "tid-1", "tid-2"]

        result_0 = SubAgentResult(success=True, content="processed apple")
        result_1 = SubAgentResult(success=True, content="processed banana")
        result_2 = SubAgentResult(success=True, content="processed cherry")
        mock_fan_in.return_value = [result_0, result_1, result_2]

        from tools.agent_patterns import scatter_gather

        results = scatter_gather(
            items=["apple", "banana", "cherry"],
            worker_task_template="Process the {item} and return it.",
            max_turns=10,
        )

        # Verify fan_out was called with substituted descriptions (positional arg 0)
        call_args = mock_fan_out.call_args
        self.assertIsNotNone(call_args)
        descriptions = call_args[0][0]
        self.assertEqual(descriptions[0], "Process the apple and return it.")
        self.assertEqual(descriptions[1], "Process the banana and return it.")
        self.assertEqual(descriptions[2], "Process the cherry and return it.")

        # Verify results
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].content, "processed apple")
        self.assertEqual(results[1].content, "processed banana")
        self.assertEqual(results[2].content, "processed cherry")

    @patch("tools.agent_patterns.fan_in")
    @patch("tools.agent_patterns.fan_out")
    def test_scatter_gather_empty_items(self, mock_fan_out, mock_fan_in):
        """scatter_gather with empty items list returns empty list."""
        mock_fan_out.return_value = []
        mock_fan_in.return_value = []

        from tools.agent_patterns import scatter_gather

        results = scatter_gather(
            items=[],
            worker_task_template="Process {item}",
        )

        self.assertEqual(results, [])

    @patch("tools.agent_patterns.fan_in")
    @patch("tools.agent_patterns.fan_out")
    def test_scatter_gather_passes_max_turns(self, mock_fan_out, mock_fan_in):
        """scatter_gather forwards max_turns to fan_out."""
        mock_fan_out.return_value = ["tid-0"]
        mock_fan_in.return_value = [
            SubAgentResult(success=True, content="done")
        ]

        from tools.agent_patterns import scatter_gather

        scatter_gather(
            items=["item1"],
            worker_task_template="Process {item}",
            max_turns=25,
        )

        self.assertEqual(mock_fan_out.call_args[1]["max_turns"], 25)


if __name__ == "__main__":
    unittest.main()
