"""Lint all Python source files by compiling them.

Catches syntax errors that ``run_tests`` would miss if the broken module
is never imported by a test.
"""

import os
import py_compile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))  # project root, not tests/
SOURCES = [
    "core/llm.py",
    "core/prompt.py",
    "terminal.py",
    "core/config.py",
    "core/safety.py",
    "memory/memory.py",
    "tools/__init__.py",
    "tools/file_ops.py",
    "tools/shell_ops.py",
    "tools/search_ops.py",
]


class TestLintSources(unittest.TestCase):
    def test_all_sources_compile(self):
        for path in SOURCES:
            full = os.path.join(ROOT, path)
            py_compile.compile(full, doraise=True)
