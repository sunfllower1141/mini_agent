#!/usr/bin/env python3
"""
codebase_map.py — structural codebase map for agent context injection.

Generates a compact (~2-5K token) symbol-level map of the workspace:
  - Module → file grouping with public symbols (classes, functions)
  - Import relationships between internal modules
  - Entry-point detection
  - Supports Python (AST), TypeScript/JavaScript (regex fallback)

Used by build_startup_context() in prompt.py to give the agent a
structural understanding without exploratory tool calls.
"""

from __future__ import annotations

import ast
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories to skip entirely during the walk
SKIP_DIRS: set[str] = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", ".eggs",
    "*.egg-info",
}

# File extensions we can extract symbols from
PYTHON_EXT: set[str] = {".py"}
TS_JS_EXT: set[str] = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

# Max symbols to show per file (keep context compact)
MAX_SYMBOLS_PER_FILE: int = 20
# Max imports to show per file
MAX_IMPORTS_PER_FILE: int = 10
# Max lines of output before truncation
MAX_OUTPUT_LINES: int = 80


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileSymbols:
    """Extracted symbols from a single source file."""
    path: str                          # relative path from workspace root
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    imports_internal: list[str] = field(default_factory=list)  # internal module imports
    imports_external: list[str] = field(default_factory=list)  # third-party/stdlib
    has_main: bool = False
    is_test: bool = False
    line_count: int = 0


@dataclass
class ModuleGroup:
    """A group of files under a common directory prefix."""
    prefix: str
    files: list[FileSymbols] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Python AST extraction
# ---------------------------------------------------------------------------

def _is_internal_import(module_name: str, workspace_packages: set[str]) -> bool:
    """Return True if module_name is internal to the workspace."""
    if not module_name:
        return False
    # Check if the top-level package matches a workspace package
    top = module_name.split(".")[0]
    return top in workspace_packages


def _extract_python_symbols(
    filepath: str, rel_path: str, workspace_packages: set[str],
) -> FileSymbols | None:
    """Parse a Python file and extract classes, functions, and imports."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    line_count = source.count("\n") + 1

    # Skip empty files
    if not source.strip():
        return None

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        # Non-parseable Python — still record as a file, just without symbols
        return FileSymbols(path=rel_path, line_count=line_count)

    sym = FileSymbols(path=rel_path, line_count=line_count)
    sym.is_test = "test" in os.path.basename(filepath).lower()

    for node in ast.iter_child_nodes(tree):
        # --- Classes ---
        if isinstance(node, ast.ClassDef):
            sym.classes.append(node.name)

        # --- Functions / async functions ---
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip private / dunder unless it's __init__
            is_public = not node.name.startswith("_") or node.name == "__init__"
            if is_public:
                sym.functions.append(node.name)
            if node.name == "main" or node.name == "__main__":
                sym.has_main = True

        # --- Top-level imports ---
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if _is_internal_import(mod, workspace_packages):
                    sym.imports_internal.append(alias.name)
                else:
                    sym.imports_external.append(mod)

        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            mod = node.module.split(".")[0]
            if _is_internal_import(mod, workspace_packages):
                sym.imports_internal.append(node.module)
            else:
                sym.imports_external.append(mod)

    # Deduplicate
    sym.imports_internal = sorted(set(sym.imports_internal))
    sym.imports_external = sorted(set(sym.imports_external))

    # If the file has no public symbols and no internal imports, it's likely
    # a data/config file — still worth listing but skip if it's empty of meaning
    return sym


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction (regex-based, no parser dependency)
# ---------------------------------------------------------------------------

_TS_EXPORT_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
    re.MULTILINE,
)
_TS_IMPORT_RE = re.compile(
    r"""import\s+(?:[\w*\s{}]*\s+from\s+)?['"]([^'"]+)['"]""",
)


def _extract_ts_symbols(
    filepath: str, rel_path: str, workspace_packages: set[str],
) -> FileSymbols | None:
    """Extract symbols from TypeScript/JavaScript using regex heuristics."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    line_count = source.count("\n") + 1
    if not source.strip():
        return None

    sym = FileSymbols(path=rel_path, line_count=line_count)
    sym.is_test = any(kw in os.path.basename(filepath).lower()
                      for kw in ("test", "spec"))

    # Find exported/declared symbols
    for m in _TS_EXPORT_RE.finditer(source):
        name = m.group(1)
        if name.startswith("_"):
            continue
        # Heuristic: uppercase first char → likely a class/type/component
        if name[0].isupper():
            sym.classes.append(name)
        else:
            sym.functions.append(name)

    # Find imports
    for m in _TS_IMPORT_RE.finditer(source):
        mod = m.group(1)
        if mod.startswith("."):
            # Relative import — resolve to internal
            sym.imports_internal.append(mod)
        elif mod.startswith("@"):
            # Scoped package — might be internal or external
            sym.imports_external.append(mod)
        else:
            # Could be workspace package or external
            top = mod.split("/")[0]
            if _is_internal_import(top, workspace_packages):
                sym.imports_internal.append(mod)
            else:
                sym.imports_external.append(mod)

    sym.imports_internal = sorted(set(sym.imports_internal))
    sym.imports_external = sorted(set(sym.imports_external))
    return sym


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_codebase_map(
    workspace: str,
    *,
    max_output_lines: int = MAX_OUTPUT_LINES,
) -> str:
    """Generate a compact structural map of the workspace.

    Walks the workspace, extracts symbols from source files, groups
    by directory prefix, and renders a token-efficient summary with
    module grouping, symbol tables, and an import graph.

    Args:
        workspace: Absolute path to the workspace root.
        max_output_lines: Truncation threshold for the output.

    Returns:
        A markdown-formatted string suitable for context injection.
    """
    if not os.path.isdir(workspace):
        return ""

    # Phase 1: Discover workspace packages (top-level dirs with __init__.py or package.json)
    workspace_packages: set[str] = set()
    try:
        for entry in os.listdir(workspace):
            full = os.path.join(workspace, entry)
            if not os.path.isdir(full) or entry.startswith("."):
                continue
            if entry in SKIP_DIRS:
                continue
            # Check for Python package marker
            if os.path.isfile(os.path.join(full, "__init__.py")):
                workspace_packages.add(entry)
            # Check for JS/TS package marker
            if os.path.isfile(os.path.join(full, "package.json")):
                workspace_packages.add(entry)
    except OSError:
        pass

    # Phase 2: Walk and extract symbols
    all_symbols: list[FileSymbols] = []
    try:
        for dirpath, dirnames, filenames in os.walk(workspace):
            # Filter out skipped dirs
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            )

            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                filepath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(filepath, workspace)

                _, ext = os.path.splitext(fname)
                symbols: FileSymbols | None = None

                if ext in PYTHON_EXT:
                    symbols = _extract_python_symbols(
                        filepath, rel_path, workspace_packages,
                    )
                elif ext in TS_JS_EXT:
                    symbols = _extract_ts_symbols(
                        filepath, rel_path, workspace_packages,
                    )

                if symbols is not None:
                    all_symbols.append(symbols)

    except OSError:
        pass

    if not all_symbols:
        return ""

    # Phase 3: Group by directory prefix
    groups: dict[str, list[FileSymbols]] = defaultdict(list)
    for sym in all_symbols:
        # Determine group prefix (first 1-2 dir components)
        parts = sym.path.replace("\\", "/").split("/")
        if len(parts) == 1:
            prefix = "(root)"
        else:
            # Use the first directory component, or first two if single depth is common
            prefix = parts[0] + "/"
            if len(parts) > 2:
                # Check if this is a deep nesting — use two components
                prefix = "/".join(parts[:2]) + "/"
        groups[prefix].append(sym)

    # Phase 4: Render output
    lines: list[str] = []
    lines.append("## Codebase Structure (symbol-level map)")

    # Sort groups: non-root first, then root — prioritize source dirs
    def _group_sort_key(kv: tuple[str, list[FileSymbols]]) -> tuple[int, str]:
        prefix = kv[0]
        # Source-like dirs first
        is_source = any(
            prefix.startswith(d)
            for d in ("src", "lib", "core", "tools", "memory", "agents", "eval")
        )
        return (0 if is_source else 1, prefix)

    sorted_groups = sorted(groups.items(), key=_group_sort_key)

    for prefix, files in sorted_groups:
        lines.append(f"\n### {prefix}")
        for sym in files:
            # Build one-liner per file
            parts: list[str] = []
            if sym.classes:
                shown = sym.classes[:MAX_SYMBOLS_PER_FILE]
                parts.append(f"classes: {', '.join(shown)}")
                if len(sym.classes) > MAX_SYMBOLS_PER_FILE:
                    parts[-1] += f" (+{len(sym.classes) - MAX_SYMBOLS_PER_FILE})"
            if sym.functions:
                shown = sym.functions[:MAX_SYMBOLS_PER_FILE]
                parts.append(f"fn: {', '.join(shown)}")
                if len(sym.functions) > MAX_SYMBOLS_PER_FILE:
                    parts[-1] += f" (+{len(sym.functions) - MAX_SYMBOLS_PER_FILE})"

            # Internal imports (most useful for dependency understanding)
            internal = [i for i in sym.imports_internal
                        if not i.startswith(".")]  # skip relative
            if internal:
                shown = internal[:MAX_IMPORTS_PER_FILE]
                parts.append(f"→ {', '.join(shown)}")
                if len(internal) > MAX_IMPORTS_PER_FILE:
                    parts[-1] += f" (+{len(internal) - MAX_IMPORTS_PER_FILE})"

            if parts:
                line = f"  {sym.path}  ({'; '.join(parts)})"
            else:
                line = f"  {sym.path}"

            if sym.is_test:
                line += "  [test]"
            elif sym.has_main:
                line += "  [entry]"

            lines.append(line)

    # Phase 5: Entry point summary
    entry_files = [s for s in all_symbols if s.has_main]
    if entry_files:
        lines.append("\n### Entry Points")
        for sym in entry_files:
            lines.append(f"  {sym.path}")

    # Phase 6: Truncate if too long
    if len(lines) > max_output_lines:
        lines = lines[:max_output_lines]
        lines.append(f"\n... ({len(all_symbols)} total source files, map truncated)")

    return "\n".join(lines)
