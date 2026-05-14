"""Lint all Python source files by compiling them.

Catches syntax errors that ``run_tests`` would miss if the broken module
is never imported by a test (e.g. mini_agent.py which is the entry point).
"""

import os
import py_compile

ROOT = os.path.dirname(__file__)
SOURCES = [
    "mini_agent.py",
    "tui.py",
    "llm.py",
    "prompt.py",
    "terminal.py",
    "config.py",
    "safety.py",
    "memory.py",
    "tools/__init__.py",
    "tools/file_ops.py",
    "tools/shell_ops.py",
    "tools/search_ops.py",
]


def test_all_sources_compile():
    for path in SOURCES:
        full = os.path.join(ROOT, path)
        py_compile.compile(full, doraise=True)
