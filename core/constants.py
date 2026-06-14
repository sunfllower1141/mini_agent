#!/usr/bin/env python3
"""constants.py — shared constants for mini_agent.

Centralized home for canonical values used across multiple modules,
avoiding duplicate definitions in core/ and tools/.  No internal project
imports — safe to import from anywhere without circular-dependency risk.

Modules should import from here rather than defining their own copies.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# File / directory names
# ---------------------------------------------------------------------------

CONFIG_FILENAME = ".mini_agent.toml"
MEMORY_FILENAME = ".mini_agent_memory.db"

# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

DEFAULT_API_PROVIDER = "deepseek"  # "deepseek", "claude", "xai", "openrouter", or "ollama"

# ---------------------------------------------------------------------------
# Truncation / timeout / connection-pool constants
# ---------------------------------------------------------------------------

TREE_TRUNCATION_LINES = 60    # max lines in workspace tree before truncating
GIT_LOG_TIMEOUT = 5           # seconds to wait for git log
GIT_LOG_COUNT = 5             # number of recent commits to show on startup
HTTP_CONNECT_TIMEOUT = 30     # seconds to establish HTTP connection
HTTP_READ_TIMEOUT = 120       # seconds to read HTTP response
HTTP_POOL_CONNECTIONS = 2     # max connections per host
HTTP_POOL_MAXSIZE = 4         # max total pool size

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
