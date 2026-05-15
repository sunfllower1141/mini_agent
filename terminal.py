#!/usr/bin/env python3
"""
terminal.py — ANSI colour helpers for mini_agent output.

Colours are automatically disabled when stderr is not a TTY or the user
passes ``--no-color``.
"""
from __future__ import annotations

import sys

def _color_enabled() -> bool:
    """Lazily check if ANSI colour output is enabled.
    
    Evaluated at call time, not import time, so TUI takeover of stderr
    doesn't affect the result.
    """
    return sys.stderr.isatty() and "--no-color" not in sys.argv

_RESET  = "\033[0m"
DIM     = "\033[2m"
_RED    = "\033[31m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"


def c(text: str, code: str) -> str:
    """Wrap *text* in an ANSI colour code, stripping when colours are off."""
    if _color_enabled():
        return f"{code}{text}{_RESET}"
    return text


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a consistently padded pipe-delimited table.

    Example:
        format_table(["Col", "Desc"], [["a", "first"], ["b", "second"]])
        →
        | Col | Desc   |
        |-----|--------|
        | a   | first  |
        | b   | second |
    """
    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    def _row(cells: list[str], sep: str = "|") -> str:
        parts = []
        for i, cell in enumerate(cells):
            if i < len(col_widths):
                parts.append(f" {cell.ljust(col_widths[i])} ")
            else:
                parts.append(f" {cell} ")
        return sep.join([""] + parts + [""])

    parts = [
        _row(headers),
        _row(["-" * w for w in col_widths], sep="|"),
    ]
    for row in rows:
        parts.append(_row(row))

    # Also add a small row between header and first data row
    return "\n".join(parts)
