#!/usr/bin/env python3
"""constants.py — shared constants for mini_agent.

Centralized home for canonical values used across multiple modules,
avoiding duplicate definitions in core/ and tools/.

Modules should import from here rather than defining their own copies.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Directory names to skip during os.walk / tree traversal.
#
# Used by: codebase_map.py, knowledge_graph.py, prompt.py (startup context),
#          shell_ops.py, search_ops.py.
# ---------------------------------------------------------------------------
SKIP_DIRS: set[str] = {
    ".git", ".hg", ".svn",
    "__pycache__",
    ".venv", "venv",
    "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build",
    ".tox", ".eggs",
}
