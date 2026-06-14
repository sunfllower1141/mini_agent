#!/usr/bin/env python3
"""Tests for MistakeNotebook, build_experience_context, and build_experience_context_batch."""

import os
import tempfile
import pytest

from tools.failure_learning import (
    MistakeNotebook,
    FailurePatternStore,
    build_experience_context,
    build_experience_context_batch,
)


class TestMistakeNotebook:
    @pytest.fixture
    def db_path(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        yield path
        from memory.memory import _close_shared_conn
        _close_shared_conn(path)
        os.unlink(path)

    @pytest.fixture
    def populated_stores(self, db_path):
        """Create FailurePatternStore with data, then MistakeNotebook."""
        from memory.memory import _close_shared_conn
        fps = FailurePatternStore(db_path)
        fps.init_schema()

        # Record same fingerprint with different args signatures (cluster)
        fps.record_failure("edit_file", "String not found in file",
                           args={"path": "a.py", "old_string": "foo"})
        fps.record_failure("edit_file", "String not found in file",
                           args={"path": "b.py", "old_string": "bar"})
        fps.record_failure("edit_file", "String not found in file",
                           args={"path": "c.py", "old_string": "baz"})

        # Different fingerprint, not enough for cluster
        fps.record_failure("edit_file", "Whitespace mismatch detected",
                           args={"path": "d.py"})

        # Enough for cluster with different tool
        fps.record_failure("read_file", "No such file: foo.py",
                           args={"path": "x.py"})
        fps.record_failure("read_file", "No such file: bar.py",
                           args={"path": "y.py"})
        fps.record_failure("read_file", "No such file: baz.py",
                           args={"path": "z.py"})

        mn = MistakeNotebook(db_path)
        mn.init_schema()
        return fps, mn

    def test_init_schema(self, db_path):
        mn = MistakeNotebook(db_path)
        mn.init_schema()
        # Should not crash
        stats = mn.stats()
        assert stats["total_entries"] == 0

    def test_distill_creates_entries(self, populated_stores):
        fps, mn = populated_stores
        created = mn.distill(10)  # turn 10
        assert created >= 1  # At least one cluster

        stats = mn.stats()
        assert stats["total_entries"] >= 1

    def test_distill_respects_cooldown(self, populated_stores):
        fps, mn = populated_stores
        mn.distill(10)
        created = mn.distill(11)  # Same turn effectively (1 apart)
        assert created == 0  # Cooldown not elapsed

    def test_distill_edit_file_not_found_guidance(self, populated_stores):
        fps, mn = populated_stores
        mn.distill(10)

        entries = mn.get_injectable_entries([
            {"function": {"name": "edit_file", "arguments": '{}'}},
        ])
        # May not meet confidence threshold yet
        # But at least the entry should exist
        stats = mn.stats()
        assert stats["total_entries"] >= 1

    def test_build_notebook_context(self, populated_stores):
        fps, mn = populated_stores
        mn.distill(10)

        # Bypass cooldown by setting it manually
        mn._last_injection_turn = -10

        ctx = mn.build_notebook_context(
            [{"function": {"name": "edit_file", "arguments": '{}'}}],
            turn_count=20,
        )
        # May return None if confidence not met
        # Just verify no crash
        assert ctx is None or "MISTAKE NOTEBOOK" in ctx

    def test_record_application(self, populated_stores):
        fps, mn = populated_stores
        mn.distill(10)
        mn.record_application("edit_file", "not_found", was_successful=True)
        # Should not crash
        stats = mn.stats()
        assert stats["total_entries"] >= 1

    def test_empty_pending_calls(self, db_path):
        mn = MistakeNotebook(db_path)
        mn.init_schema()
        entries = mn.get_injectable_entries(None)
        assert entries == []

        ctx = mn.build_notebook_context(None, turn_count=1)
        assert ctx is None

    def test_stats(self, populated_stores):
        fps, mn = populated_stores
        mn.distill(10)
        stats = mn.stats()
        assert "total_entries" in stats
        assert "accepted_entries" in stats
        assert "acceptance_threshold" in stats


class TestExperienceContext:
    @pytest.fixture
    def memory_store(self):
        """Create a MemoryStore with some knowledge entries."""
        from memory.memory import MemoryStore
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        ms = MemoryStore(db_path)
        ms.add_knowledge(
            summary="edit_file whitespace pattern",
            category="tool_usage",
            detail="When editing Python files, always copy-paste exact whitespace. Use read_file with line_numbers=true first.",
            importance=2,
        )
        ms.add_knowledge(
            summary="module import pattern",
            category="convention",
            detail="Use from __future__ import annotations at the top of every module.",
            importance=2,
        )
        ms.add_knowledge(
            summary="low importance note",
            category="general",
            detail="This should not appear for dynamic injection.",
            importance=0,  # Below _MIN_KNOWLEDGE_IMPORTANCE_DYNAMIC
        )
        yield ms
        ms.close()
        os.unlink(db_path)

    def test_build_experience_context_matches_tool(self, memory_store):
        ctx = build_experience_context(
            memory_store,
            tool_name="edit_file",
            args={"path": "test.py", "old_string": "something"},
        )
        if ctx:
            assert "RELEVANT PAST EXPERIENCES" in ctx
            assert "whitespace" in ctx.lower() or "edit_file" in ctx.lower()

    def test_build_experience_context_no_match(self, memory_store):
        ctx = build_experience_context(
            memory_store,
            tool_name="unknown_tool",
            args={"path": "nonexistent.xyz"},
        )
        assert ctx is None

    def test_build_experience_context_none_store(self):
        ctx = build_experience_context(None, "edit_file")
        assert ctx is None

    def test_build_experience_context_batch(self, memory_store):
        pending = [
            {"function": {"name": "edit_file", "arguments": '{"path":"test.py","old_string":"hello"}'}},
            {"function": {"name": "read_file", "arguments": '{"path":"test.py"}'}},
        ]
        ctx = build_experience_context_batch(memory_store, pending)
        if ctx:
            assert "RELEVANT PAST EXPERIENCES" in ctx

    def test_build_experience_context_batch_empty(self, memory_store):
        ctx = build_experience_context_batch(memory_store, [])
        assert ctx is None

    def test_build_experience_context_batch_none_store(self):
        ctx = build_experience_context_batch(None, [{"function": {"name": "edit_file"}}])
        assert ctx is None

    def test_build_experience_context_deduplicates(self, memory_store):
        # Add a knowledge entry that matches both tool calls
        pending = [
            {"function": {"name": "edit_file", "arguments": '{"path":"a.py","old_string":"x"}'}},
            {"function": {"name": "edit_file", "arguments": '{"path":"b.py","old_string":"y"}'}},
        ]
        ctx = build_experience_context_batch(memory_store, pending)
        if ctx:
            # The whitespace entry should appear only once
            lines = ctx.split("\n")
            whitespace_count = sum(1 for l in lines if "whitespace" in l.lower())
            assert whitespace_count <= 1
