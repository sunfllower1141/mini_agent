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
import platform
import re
import threading
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

# Windows reserved device names — os.walk can encounter files/dirs with
# these names (nul, con, prn, aux, com1-9, lpt1-9) which resolve to
# NT namespace paths like \\.\nul, breaking os.path.relpath.
_WIN_RESERVED: set[str] = {
    "nul", "con", "prn", "aux",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _is_win_reserved(name: str) -> bool:
    """Check if a bare name (no extension) is a Windows reserved device name."""
    stem, _ = os.path.splitext(name.lower())
    return stem in _WIN_RESERVED


def _safe_relpath(filepath: str, start: str) -> str | None:
    """os.path.relpath that tolerates different Windows mounts."""
    try:
        return os.path.relpath(filepath, start)
    except ValueError:
        return None

# File extensions we can extract symbols from
PYTHON_EXT: set[str] = {".py"}
TS_JS_EXT: set[str] = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

# Max symbols to show per file (keep context compact)
MAX_SYMBOLS_PER_FILE: int = 20
# Max imports to show per file
MAX_IMPORTS_PER_FILE: int = 10
# Max lines of output before truncation
MAX_OUTPUT_LINES: int = 80

# --- Incremental map cache ---
# Path → FileSymbols for all indexed files.  Updated incrementally when
# files are written/edited so the agent's context stays current without
# a full workspace re-scan.
_MAP_CACHE: dict[str, FileSymbols] = {}
_MAP_CACHE_LOCK = threading.Lock()
_MAP_CACHE_WORKSPACE_PACKAGES: set[str] = set()
_MAP_CACHE_WORKSPACE: str = ""


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
# Tree-sitter extraction adapters (fall back to AST/regex)
# ---------------------------------------------------------------------------

def _extract_python_with_treesitter(
    filepath: str, rel_path: str, workspace_packages: set[str],
) -> FileSymbols | None:
    """Extract Python symbols via tree-sitter. Returns None if unavailable."""
    try:
        from core.tree_sitter_parser import extract_symbols
    except ImportError:
        return None
    result = extract_symbols(filepath)
    if result is None:
        return None
    defs, _calls, imports = result
    if not defs and not imports:
        return None
    # Count lines
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
    except OSError:
        line_count = 0
    sym = FileSymbols(path=rel_path, line_count=line_count)
    sym.is_test = any(kw in os.path.basename(filepath).lower()
                      for kw in ("test", "spec"))
    for d in defs:
        if d["kind"] == "class":
            sym.classes.append(d["name"])
        elif d["kind"] == "def":
            sym.functions.append(d["name"])
            if d["name"] == "main" or d["name"] == "__main__":
                sym.has_main = True
    for imp in imports:
        mod = imp["module"]
        if imp.get("internal"):
            sym.imports_internal.append(mod)
        else:
            sym.imports_external.append(mod.split(".")[0] if "." in mod else mod)
    sym.imports_internal = sorted(set(sym.imports_internal))
    sym.imports_external = sorted(set(sym.imports_external))
    return sym


def _extract_ts_with_treesitter(
    filepath: str, rel_path: str, workspace_packages: set[str],
) -> FileSymbols | None:
    """Extract TypeScript/JS symbols via tree-sitter. Returns None if unavailable."""
    try:
        from core.tree_sitter_parser import extract_symbols
    except ImportError:
        return None
    result = extract_symbols(filepath)
    if result is None:
        return None
    defs, _calls, imports = result
    if not defs and not imports:
        return None
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
    except OSError:
        line_count = 0
    sym = FileSymbols(path=rel_path, line_count=line_count)
    sym.is_test = any(kw in os.path.basename(filepath).lower()
                      for kw in ("test", "spec"))
    for d in defs:
        name = d["name"]
        if name.startswith("_"):
            continue
        if name[0].isupper():
            sym.classes.append(name)
        else:
            sym.functions.append(name)
    for imp in imports:
        mod = imp["module"]
        if imp.get("internal"):
            sym.imports_internal.append(mod)
        elif mod.startswith("."):
            sym.imports_internal.append(mod)
        else:
            sym.imports_external.append(mod.split("/")[0] if "/" in mod else mod)
    sym.imports_internal = sorted(set(sym.imports_internal))
    sym.imports_external = sorted(set(sym.imports_external))
    return sym


# ---------------------------------------------------------------------------
# Incremental update helpers
# ---------------------------------------------------------------------------

def _extract_single_file(
    filepath: str, rel_path: str, workspace_packages: set[str],
) -> FileSymbols | None:
    """Extract symbols from a single file (called for incremental updates).

    Uses tree-sitter when available (more accurate, error-tolerant),
    falls back to AST (Python) or regex (JS/TS).
    """
    _, ext = os.path.splitext(filepath)
    if ext in PYTHON_EXT:
        # Try tree-sitter first for Python (more accurate, handles syntax errors)
        sym = _extract_python_with_treesitter(filepath, rel_path, workspace_packages)
        if sym is not None:
            return sym
        return _extract_python_symbols(filepath, rel_path, workspace_packages)
    elif ext in TS_JS_EXT:
        sym = _extract_ts_with_treesitter(filepath, rel_path, workspace_packages)
        if sym is not None:
            return sym
        return _extract_ts_symbols(filepath, rel_path, workspace_packages)
    return None


def update_file_in_map(filepath: str, workspace: str) -> None:
    """Re-extract symbols for a single file and update the cached map.

    Call this after writing/editing a file so the codebase map stays
    current without a full workspace re-scan.
    """
    global _MAP_CACHE, _MAP_CACHE_WORKSPACE_PACKAGES, _MAP_CACHE_WORKSPACE
    if not os.path.isfile(filepath):
        return
    rel_path = _safe_relpath(filepath, workspace)
    if rel_path is None:
        return
    if os.path.basename(filepath).startswith("."):
        return
    if _MAP_CACHE_WORKSPACE != workspace or not _MAP_CACHE_WORKSPACE_PACKAGES:
        _MAP_CACHE_WORKSPACE_PACKAGES = _discover_workspace_packages(workspace)
        _MAP_CACHE_WORKSPACE = workspace
    sym = _extract_single_file(filepath, rel_path, _MAP_CACHE_WORKSPACE_PACKAGES)
    with _MAP_CACHE_LOCK:
        if sym is not None:
            _MAP_CACHE[rel_path] = sym
        else:
            _MAP_CACHE.pop(rel_path, None)


def remove_file_from_map(filepath: str, workspace: str) -> None:
    """Remove a file from the cached codebase map (e.g. on deletion)."""
    global _MAP_CACHE
    rel_path = _safe_relpath(filepath, workspace)
    if rel_path is None:
        return
    with _MAP_CACHE_LOCK:
        _MAP_CACHE.pop(rel_path, None)


def invalidate_map() -> None:
    """Clear the cached codebase map, forcing a full rebuild next call."""
    global _MAP_CACHE, _MAP_CACHE_WORKSPACE_PACKAGES, _MAP_CACHE_WORKSPACE
    with _MAP_CACHE_LOCK:
        _MAP_CACHE.clear()
        _MAP_CACHE_WORKSPACE_PACKAGES.clear()
        _MAP_CACHE_WORKSPACE = ""


def _discover_workspace_packages(workspace: str) -> set[str]:
    """Discover top-level packages in the workspace."""
    packages: set[str] = set()
    try:
        for entry in os.listdir(workspace):
            full = os.path.join(workspace, entry)
            if not os.path.isdir(full) or entry.startswith("."):
                continue
            if entry in SKIP_DIRS:
                continue
            if os.path.isfile(os.path.join(full, "__init__.py")):
                packages.add(entry)
            if os.path.isfile(os.path.join(full, "package.json")):
                packages.add(entry)
    except OSError:
        pass
    return packages


def get_cached_map() -> dict[str, FileSymbols]:
    """Return a snapshot of the current cached map (thread-safe)."""
    with _MAP_CACHE_LOCK:
        return dict(_MAP_CACHE)


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

    global _MAP_CACHE, _MAP_CACHE_WORKSPACE_PACKAGES, _MAP_CACHE_WORKSPACE

    # --- Fast path: use cached map if workspace hasn't changed ---
    with _MAP_CACHE_LOCK:
        cache_valid = (
            _MAP_CACHE_WORKSPACE == workspace
            and _MAP_CACHE_WORKSPACE_PACKAGES
            and _MAP_CACHE
        )
        if cache_valid:
            all_symbols = list(_MAP_CACHE.values())
        else:
            all_symbols = None

    if all_symbols is None:
        # Phase 1: Discover workspace packages
        workspace_packages: set[str] = _discover_workspace_packages(workspace)

        # Phase 2: Walk and extract symbols
        new_cache: dict[str, FileSymbols] = {}
        all_symbols = []
        try:
            for dirpath, dirnames, filenames in os.walk(workspace):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in SKIP_DIRS
                    and not d.startswith(".")
                    and not _is_win_reserved(d)
                )

                for fname in sorted(filenames):
                    if fname.startswith("."):
                        continue
                    if _is_win_reserved(fname):
                        continue
                    filepath = os.path.join(dirpath, fname)
                    rel_path = _safe_relpath(filepath, workspace)
                    if rel_path is None:
                        continue

                    symbols = _extract_single_file(
                        filepath, rel_path, workspace_packages,
                    )

                    if symbols is not None:
                        all_symbols.append(symbols)
                        new_cache[rel_path] = symbols

        except OSError:
            pass

        # Populate cache for incremental updates
        with _MAP_CACHE_LOCK:
            _MAP_CACHE = new_cache
            _MAP_CACHE_WORKSPACE_PACKAGES = workspace_packages
            _MAP_CACHE_WORKSPACE = workspace

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
