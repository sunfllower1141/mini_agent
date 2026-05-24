"""Lint all Python source files by compiling them.

Catches syntax errors that ``run_tests`` would miss if the broken module
is never imported by a test.
"""

import os
import py_compile
import unittest

ROOT = os.path.dirname(__file__)
SOURCES = [
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


class TestLintSources(unittest.TestCase):
    def test_all_sources_compile(self):
        for path in SOURCES:
            full = os.path.join(ROOT, path)
            py_compile.compile(full, doraise=True)
