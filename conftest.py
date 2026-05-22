#!/usr/bin/env python3
"""conftest.py — pytest configuration for mini_agent.

- Excludes benchmark tests by default (use --run-benchmarks to include).
- Orders benchmarks last when included to minimize cross-test hangs.
"""

import os
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-benchmarks",
        action="store_true",
        default=False,
        help="Include benchmark tests (excluded by default)",
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
    if collection_path.name == "test_benchmarks.py":
        return True
    return False


def pytest_collection_modifyitems(config, items) -> None:
    """Run benchmark tests last when included via --run-benchmarks."""
    benchmark_items = []
    other_items = []
    for item in items:
        if os.path.basename(item.location[0]) == "test_benchmarks.py":
            benchmark_items.append(item)
        else:
            other_items.append(item)
    items[:] = other_items + benchmark_items
