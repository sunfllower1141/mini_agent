#!/usr/bin/env python3
"""conftest.py -- pytest configuration and shared test helpers for mini_agent.

- Excludes benchmark tests by default (use --run-benchmarks to include).
- Orders benchmarks last when included to minimize cross-test hangs.
- Centralises frequently-duplicated helpers: make_tool_call, make_gates,
  make_mock_config, and pytest fixtures for safety gates + agent context.
"""

from __future__ import annotations

import json
import os
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests (may require external services)"
    )
    config.addinivalue_line(
        "markers", "benchmark: marks tests as benchmarks (excluded by default, use --run-benchmarks)"
    )
    config.addinivalue_line(
        "markers", "slow: marks tests as slow-running (>10s per test)"
    )


# ---------------------------------------------------------------------------
# pytest CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--run-benchmarks",
        action="store_true",
        default=False,
        help="Include benchmark tests (excluded by default)",
    )
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="Include slow tests (excluded by default: sub-agent threads, AgentRuntime, git, desktop ops)",
    )
    parser.addoption(
        "--swebench",
        action="store_true",
        default=False,
        help="Include SWE-bench benchmarks (requires network + datasets library)",
    )
    parser.addoption(
        "--swebench-max-tasks",
        type=int,
        default=5,
        help="Maximum number of SWE-bench tasks to run (default: 5)",
    )


def pytest_ignore_collect(collection_path, config):
    """Skip benchmarks by default, and ignore venv site-packages tests.

    Run benchmarks explicitly with --run-benchmarks.
    """
    # Never collect tests inside a virtualenv
    parts = collection_path.parts
    if "venv" in parts or ".venv" in parts:
        return True

    if config.getoption("--run-benchmarks"):
        return False
    if collection_path.name in ("test_benchmarks.py", "test_eval_integration.py"):
        return True
    return False


def pytest_collection_modifyitems(config, items) -> None:
    """Run benchmark tests last when included via --run-benchmarks.
    Deselect slow-marked tests unless --run-slow is set.
    """
    run_slow = config.getoption("--run-slow", default=False)

    kept = []
    deselected = []
    benchmark_items = []
    other_items = []

    for item in items:
        # Deselect slow tests unless --run-slow
        is_slow = any(marker.name == "slow" for marker in item.iter_markers())
        if is_slow and not run_slow:
            deselected.append(item)
            continue
        # Separate benchmarks for ordering
        basename = os.path.basename(item.location[0])
        if basename in ("test_benchmarks.py", "test_eval_integration.py"):
            benchmark_items.append(item)
        else:
            other_items.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = other_items + benchmark_items


# ---------------------------------------------------------------------------
# Shared helper factories  (usable by both unittest.TestCase and pytest tests)
# ---------------------------------------------------------------------------


def make_tool_call(name: str, /, **kwargs) -> dict:
    """Build a tool-call dict for passing to ``execute_tool()``.

    Keyword arguments are JSON-serialised as the tool's ``arguments`` string.
    """
    return {
        "id": "call_test",
        "function": {
            "name": name,
            "arguments": json.dumps(kwargs),
        },
    }


def make_gates(workspace: str = "/tmp") -> tuple:
    """Return ``(WriteSafetyGate, ReadSafetyGate)`` for *workspace*.

    The write-gate has ``allow_overwrites=True`` for test convenience.
    """
    from core.safety import ReadSafetyGate, WriteSafetyGate

    return WriteSafetyGate(workspace, allow_overwrites=True), ReadSafetyGate(workspace)


def make_mock_config(**overrides) -> object:
    """Create a lightweight mock config object with sensible test defaults.

    Pass any keyword argument to override a default field (e.g.
    ``make_mock_config(sub_agent_max_turns=10)``).
    """
    defaults: dict = {
        "model": "test-model",
        "api_key": "test-key",
        "api_url": "https://test.api",
        "stream": False,
        "verbose": True,
        "sub_agent_model": "test-model",
        "sub_agent_api_key": "",
        "sub_agent_max_concurrent": 5,
        "sub_agent_max_turns": 5,
        "workspace": "/tmp",
        "unrestricted": False,
        "allow_overwrites": True,
        "approve_write_ops": False,
        "memory_filename": ":memory:",
        "max_messages": 500,
        "max_tokens": 200000,
        "exa_api_key": "",
        "openai_api_key": "",
    }
    defaults.update(overrides)

    # Build a new type so isinstance checks in app code don't accidentally match
    cfg_type = type("MockConfig", (), defaults)
    return cfg_type()


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_log_api_error():
    """Prevent tests from writing MagicMock strings into the real api_error.log."""
    from unittest.mock import patch
    with patch("api.log_api_error") as mock:
        yield mock


@pytest.fixture
def gates(tmp_path):
    """Safety gates rooted in a temporary directory."""
    from core.safety import ReadSafetyGate, WriteSafetyGate

    wg = WriteSafetyGate(str(tmp_path))
    rg = ReadSafetyGate(str(tmp_path))
    return wg, rg


@pytest.fixture
def configured_context(tmp_path, monkeypatch):
    """Set up ``_TOOL_CONTEXT`` with an ``AgentRuntime`` and mock config.

    Cleans up all sub-agent threads and global message state on teardown.
    """
    from agents.agent_runtime import AgentRuntime
    from tools import set_context

    runtime = AgentRuntime()
    config = make_mock_config(workspace=str(tmp_path))

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
    from tools.agent_messages import _AGENT_MSGS, _AGENT_MSGS_LOCK

    with _AGENT_MSGS_LOCK:
        _AGENT_MSGS.clear()
    set_context(_agent_runtime=None, _agent_config=None)
