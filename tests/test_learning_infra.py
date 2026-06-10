"""test_learning_infra.py — validate the 6 self-learning infrastructure fixes."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from tools.result import (
    ToolResult,
    TYPED_REASON_READ_BEFORE_EDIT,
    TYPED_REASON_NOT_FOUND,
    TYPED_REASON_CIRCUIT,
    TYPED_REASON_STALE,
)
from tools.error_hints import _fingerprint_error, _FAILURE_PATTERNS
from tools.failure_learning import _fingerprint_error as _fl_fingerprint


# ---------------------------------------------------------------------------
# Fix 1: Distinct fingerprints for "read-before-edit" and "stale"
# ---------------------------------------------------------------------------

class TestDistinctFingerprints:
    """New fingerprints don't collide with existing ones."""

    def test_read_before_edit_fingerprint(self):
        assert _fingerprint_error("edit_file", "has not been read yet") == "read-before-edit"
        assert _fingerprint_error("edit_file", "use read_file first to read the file") == "read-before-edit"
        assert _fingerprint_error("edit_file", "read the file before editing it") == "read-before-edit"

    def test_stale_fingerprint(self):
        assert _fingerprint_error("edit_file", "was modified after last read_file") == "stale"
        assert _fingerprint_error("edit_file", "stored mtime: 1.000, current: 2.000") == "stale"

    def test_read_before_edit_has_recovery_pattern(self):
        assert "read-before-edit" in _FAILURE_PATTERNS["edit_file"]
        rbe = _FAILURE_PATTERNS["edit_file"]["read-before-edit"].lower()
        assert "read" in rbe and "file" in rbe

    def test_stale_has_recovery_pattern(self):
        assert "stale" in _FAILURE_PATTERNS["edit_file"]
        assert "re-read" in _FAILURE_PATTERNS["edit_file"]["stale"].lower()

    def test_blocked_fingerprint(self):
        assert _fingerprint_error("edit_file", "blocked by safety layer") == "blocked"
        assert "blocked" in _FAILURE_PATTERNS["edit_file"]

    def test_failure_learning_duplicate_fingerprints_match(self):
        """Both copies of _fingerprint_error produce the same output."""
        test_cases = [
            ("not been read yet in this session", "read-before-edit"),
            ("modified after last read", "stale"),
            ("not found in", "not found"),
            ("whitespace mismatch", "whitespace"),
            ("safety layer blocked", "blocked"),
        ]
        for content, expected in test_cases:
            assert _fingerprint_error("edit_file", content) == expected
            assert _fl_fingerprint("edit_file", content) == expected


# ---------------------------------------------------------------------------
# Fix 2: Reflexion prompt on edit_file failure
# ---------------------------------------------------------------------------

class TestReflexionPrompt:
    """Reflexion prompt injects structured post-mortem questions."""

    def test_reflexion_prompt_importable(self):
        from tools.file_ops import _reflexion_prompt
        rp = _reflexion_prompt("not_found")
        assert "REFLEXION PROMPT" in rp
        assert "What went wrong" in rp
        assert "incorrect assumption" in rp
        assert "do differently" in rp
        assert "remember()" in rp

    def test_reflexion_prompt_includes_failure_kind(self):
        from tools.file_ops import _reflexion_prompt
        rp = _reflexion_prompt("stale")
        assert "stale" in rp


# ---------------------------------------------------------------------------
# Fix 3: Circuit breaker hard stop
# ---------------------------------------------------------------------------

class TestCircuitBreakerHardStop:
    """Circuit breaker now rejects tool calls (not just warns)."""

    def test_circuit_breaker_trips_after_three(self):
        """After 3 identical calls, the circuit breaker rejects the call."""
        from tools import execute_tool, _TOOL_CONTEXT
        from core.safety import WriteSafetyGate, ReadSafetyGate

        tmpdir = tempfile.mkdtemp()
        # Create a file inside the workspace so safety gate passes
        test_file = os.path.join(tmpdir, "f.py")
        with open(test_file, "w") as f:
            f.write("x")

        # Set up recent_tool_keys with 3 identical calls
        key = f'edit_file:{{"count": 1, "new_string": "y", "old_string": "x", "path": "{test_file}"}}'
        _TOOL_CONTEXT._recent_tool_keys = deque([key, key, key])

        wg = WriteSafetyGate(workspace_root=tmpdir)
        rg = ReadSafetyGate(workspace_root=tmpdir, unrestricted=False)

        tc = {
            "function": {
                "name": "edit_file",
                "arguments": json.dumps({
                    "path": test_file,
                    "old_string": "x",
                    "new_string": "y",
                    "count": 1,
                }),
            }
        }

        # This should trip the circuit breaker
        result = execute_tool(tc, wg, rg)
        # Clean up
        _TOOL_CONTEXT._recent_tool_keys = None

        # Should fail with circuit breaker message
        assert result.success is False
        assert "CIRCUIT BREAKER" in result.content

    def test_circuit_breaker_typed_error(self):
        """Circuit breaker result includes typed_error envelope."""
        from tools import execute_tool, _TOOL_CONTEXT
        from core.safety import WriteSafetyGate, ReadSafetyGate

        tmpdir = tempfile.mkdtemp()
        test_file = os.path.join(tmpdir, "x.py")
        with open(test_file, "w") as f:
            f.write("hello")

        key = f'read_file:{{"path": "{test_file}"}}'
        _TOOL_CONTEXT._recent_tool_keys = deque([key, key, key])

        wg = WriteSafetyGate(workspace_root=tmpdir)
        rg = ReadSafetyGate(workspace_root=tmpdir, unrestricted=False)

        tc = {
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": test_file}),
            }
        }

        result = execute_tool(tc, wg, rg)
        _TOOL_CONTEXT._recent_tool_keys = None

        assert result.typed_error is not None
        assert result.typed_error["reason"] == "circuit_breaker"
        assert result.typed_error["retry_budget"] == 0


# ---------------------------------------------------------------------------
# Fix 4: Plan-mode gate
# ---------------------------------------------------------------------------

class TestPlanGate:
    """Plan gate warns when editing files outside plan scope."""

    def test_plan_gate_injects_when_plan_active(self):
        from core.context_inject import _inject_plan_gate
        from tools import _TOOL_CONTEXT

        # Set up a plan
        _TOOL_CONTEXT._plan = {
            "steps": [
                {"description": "Edit file_ops.py to add safety checks"},
                {"description": "Update test_fuzzy_match.py"},
            ]
        }

        # Create an assistant message editing a file NOT in the plan
        messages = [
            {"role": "user", "content": "do stuff"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": json.dumps({
                                "path": "tools/error_hints.py",
                                "old_string": "x",
                                "new_string": "y",
                            }),
                        },
                    }
                ],
            },
        ]

        _inject_plan_gate(messages)

        # Should have injected a warning
        plan_warnings = [m for m in messages if "PLAN GATE" in (m.get("content") or "")]
        assert len(plan_warnings) == 1
        assert "error_hints.py" in plan_warnings[0]["content"]

        # Clean up
        _TOOL_CONTEXT._plan = None

    def test_plan_gate_silent_when_file_in_plan(self):
        from core.context_inject import _inject_plan_gate
        from tools import _TOOL_CONTEXT

        _TOOL_CONTEXT._plan = {
            "steps": [
                {"description": "Edit file_ops.py to add safety checks"},
            ]
        }

        messages = [
            {"role": "user", "content": "do stuff"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": json.dumps({
                                "path": "tools/file_ops.py",
                                "old_string": "x",
                                "new_string": "y",
                            }),
                        },
                    }
                ],
            },
        ]

        _inject_plan_gate(messages)

        plan_warnings = [m for m in messages if "PLAN GATE" in (m.get("content") or "")]
        assert len(plan_warnings) == 0

        _TOOL_CONTEXT._plan = None

    def test_plan_gate_silent_no_plan(self):
        from core.context_inject import _inject_plan_gate
        from tools import _TOOL_CONTEXT

        _TOOL_CONTEXT._plan = None
        messages = [
            {"role": "user", "content": "do stuff"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": json.dumps({
                                "path": "tools/error_hints.py",
                                "old_string": "x",
                                "new_string": "y",
                            }),
                        },
                    }
                ],
            },
        ]

        _inject_plan_gate(messages)
        plan_warnings = [m for m in messages if "PLAN GATE" in (m.get("content") or "")]
        assert len(plan_warnings) == 0


# ---------------------------------------------------------------------------
# Fix 5: Typed error envelope
# ---------------------------------------------------------------------------

class TestTypedErrorEnvelope:
    """ToolResult now carries machine-actionable typed_error info."""

    def test_with_typed_error_method(self):
        tr = ToolResult(success=False, content="fail").with_typed_error(
            "not_found", retry_budget=2, suggested_action="read file first"
        )
        assert tr.typed_error["reason"] == "not_found"
        assert tr.typed_error["retry_budget"] == 2
        assert "read file first" in tr.typed_error["suggested_action"]

    def test_typed_error_in_to_dict(self):
        tr = ToolResult(success=False, content="fail").with_typed_error(
            "read_before_edit", retry_budget=1, suggested_action="call read_file"
        )
        d = tr.to_dict()
        assert "typed_error" in d
        assert d["typed_error"]["reason"] == "read_before_edit"

    def test_typed_error_not_in_success_dict(self):
        tr = ToolResult(success=True, content="ok")
        d = tr.to_dict()
        assert "typed_error" not in d

    def test_typed_constants_available(self):
        assert TYPED_REASON_READ_BEFORE_EDIT == "read_before_edit"
        assert TYPED_REASON_NOT_FOUND == "not_found"
        assert TYPED_REASON_CIRCUIT == "circuit_breaker"
        assert TYPED_REASON_STALE == "stale"


# ---------------------------------------------------------------------------
# Fix 6: Learning nudge
# ---------------------------------------------------------------------------

class TestLearningNudge:
    """Learning nudge reminds agent to use remember() after failures."""

    def test_learning_nudge_injects_after_failure(self):
        from core.context_inject import _inject_learning_nudge
        from tools import _TOOL_CONTEXT

        _TOOL_CONTEXT._last_learning_nudge_turn = -999  # reset cooldown

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": '{"success": false, "content": "Edit failed: not found"}',
                "_transient": False,
            },
        ]

        _inject_learning_nudge(messages, turn_count=3)

        nudges = [m for m in messages if "LEARNING NUDGE" in (m.get("content") or "")]
        assert len(nudges) == 1
        assert "remember()" in nudges[0]["content"]
        assert "edit_file" in nudges[0]["content"]

    def test_learning_nudge_respects_cooldown(self):
        from core.context_inject import _inject_learning_nudge
        from tools import _TOOL_CONTEXT

        _TOOL_CONTEXT._last_learning_nudge_turn = 3  # just nudged

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": '{"success": false, "content": "fail"}',
                "_transient": False,
            },
        ]

        _inject_learning_nudge(messages, turn_count=4)

        nudges = [m for m in messages if "LEARNING NUDGE" in (m.get("content") or "")]
        assert len(nudges) == 0  # cooldown not expired

    def test_learning_nudge_silent_on_success(self):
        from core.context_inject import _inject_learning_nudge
        from tools import _TOOL_CONTEXT

        _TOOL_CONTEXT._last_learning_nudge_turn = -999

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc1",
                "content": '{"success": true, "content": "OK: replaced 1 occurrence"}',
                "_transient": False,
            },
        ]

        _inject_learning_nudge(messages, turn_count=3)

        nudges = [m for m in messages if "LEARNING NUDGE" in (m.get("content") or "")]
        assert len(nudges) == 0  # no nudge on success
