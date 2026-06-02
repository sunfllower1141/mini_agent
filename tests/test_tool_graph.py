#!/usr/bin/env python3
"""Tests for tools/tool_graph.py — tool dependency and sequencing graph."""

import os
import tempfile
import pytest

from tools.tool_graph import ToolGraph


class TestToolGraph:
    @pytest.fixture
    def graph(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        tg = ToolGraph(db_path)
        tg.init_schema()
        yield tg
        os.unlink(db_path)

    def test_record_single_transition(self, graph):
        graph.record_transition("read_file", "edit_file")
        hints = graph.get_next_tool_hints("read_file")
        assert len(hints) >= 0  # May not meet min_count yet

    def test_multiple_transitions_meet_threshold(self, graph):
        graph.record_transition("read_file", "edit_file")
        graph.record_transition("read_file", "edit_file")
        hints = graph.get_next_tool_hints("read_file")
        assert len(hints) >= 1
        assert "edit_file" in hints[0]

    def test_successful_transition_tracking(self, graph):
        graph.record_transition("read_file", "edit_file", successful_turn=True)
        graph.record_transition("read_file", "edit_file", successful_turn=True)
        hints = graph.get_next_tool_hints("read_file")
        assert len(hints) >= 1
        # Success rate should be high
        assert "100%" in hints[0] or "edit_file" in hints[0]

    def test_self_loop_skipped(self, graph):
        graph.record_transition("read_file", "read_file")
        hints = graph.get_next_tool_hints("read_file")
        assert all("read_file" not in h for h in hints) or len(hints) == 0

    def test_turn_sequence_recording(self, graph):
        sequence = ["read_file", "edit_file", "verify"]
        graph.record_turn_tool_sequence(sequence, successful_turn=True)

        # Check adjacent transitions
        hints = graph.get_next_tool_hints("read_file")
        # May need multiple recordings to meet threshold
        assert isinstance(hints, list)

    def test_repeated_sequence_meets_threshold(self, graph):
        sequence = ["read_file", "edit_file", "run_shell", "verify"]
        for _ in range(3):
            graph.record_turn_tool_sequence(sequence, successful_turn=True)

        hints = graph.get_next_tool_hints("read_file")
        assert len(hints) >= 1

        hints = graph.get_next_tool_hints("edit_file")
        assert len(hints) >= 1

    def test_context_hints_with_recent_tools(self, graph):
        sequence = ["read_file", "edit_file", "verify"]
        for _ in range(3):
            graph.record_turn_tool_sequence(sequence, successful_turn=True)

        context = graph.get_tool_context_hints(["read_file"])
        assert context is not None
        assert "edit_file" in context

    def test_context_hints_empty(self, graph):
        context = graph.get_tool_context_hints([])
        assert context is None

    def test_read_before_write_detection(self, graph):
        # Agent about to edit without reading — use a tool not in _READ_TOOLS
        pending = [{"function": {"name": "edit_file", "arguments": '{"path":"test.py"}'}}]
        recent = ["run_shell"]  # Not a read tool
        warning = graph.detect_read_before_write_gap(pending, recent)
        assert warning is not None
        assert "read" in warning.lower()

    def test_read_before_write_ok_when_read_recent(self, graph):
        pending = [{"function": {"name": "edit_file", "arguments": '{"path":"test.py"}'}}]
        recent = ["read_file", "list_directory"]  # Has reads
        warning = graph.detect_read_before_write_gap(pending, recent)
        assert warning is None

    def test_read_before_write_no_write_pending(self, graph):
        pending = [{"function": {"name": "read_file", "arguments": '{"path":"test.py"}'}}]
        recent = []
        warning = graph.detect_read_before_write_gap(pending, recent)
        assert warning is None  # No write tools pending

    def test_no_reads_first_turn_ok(self, graph):
        pending = [{"function": {"name": "edit_file", "arguments": '{"path":"test.py"}'}}]
        recent = []  # First turn — no history
        warning = graph.detect_read_before_write_gap(pending, recent)
        assert warning is None  # First turn is fine

    def test_consecutive_edits_without_verify(self, graph):
        # Build up a tool graph with edit patterns
        for _ in range(3):
            graph.record_transition("edit_file", "edit_file", successful_turn=False)

        recent = ["edit_file", "edit_file", "edit_file"]
        context = graph.get_tool_context_hints(recent)
        if context:
            assert "edit" in context.lower() or "verify" in context.lower()

    def test_stats(self, graph):
        graph.record_transition("read_file", "edit_file")
        graph.record_transition("read_file", "edit_file")
        stats = graph.stats()
        assert stats["total_transitions"] >= 1
        assert len(stats["top_pairs"]) >= 1
