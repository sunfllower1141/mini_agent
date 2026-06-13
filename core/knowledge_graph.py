#!/usr/bin/env python3
"""
knowledge_graph.py -- Entity-relationship graph for codebase understanding.

Builds a typed graph from symbol definitions, calls, and imports.
Exposes query tools: find_related (neighborhood), trace_path (shortest path).
"""

from __future__ import annotations

import os
import re
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    """A typed directed edge between two entities."""
    source: str       # source entity name
    target: str       # target entity name
    kind: str         # "calls", "imports", "inherits", "defines"
    filepath: str | None = None
    line: int | None = None


@dataclass
class Entity:
    """A code entity (function, class, module)."""
    name: str
    kind: str         # "def", "class", "module"
    filepath: str | None = None
    line: int | None = None
    edges_out: list[Edge] = field(default_factory=list)
    edges_in: list[Edge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph store
# ---------------------------------------------------------------------------

_GRAPH: dict[str, Entity] = {}       # name -> Entity
_GRAPH_BUILT = False
_GRAPH_LOCK = threading.Lock()
_GRAPH_WORKSPACE: str = ""

# Directories to skip
_SKIP_DIRS: set[str] = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".tox", ".eggs",
}

# Names we skip in call edges
_SKIP_CALL_NAMES: frozenset[str] = frozenset({
    "self", "cls", "True", "False", "None", "print", "len", "range",
    "int", "str", "list", "dict", "set", "tuple", "bool", "float",
    "type", "isinstance", "hasattr", "getattr", "setattr", "enumerate",
    "zip", "map", "filter", "sorted", "reversed", "min", "max", "sum",
    "abs", "open", "super", "any", "all", "iter", "next", "ord", "chr",
    "round", "Exception", "ValueError", "TypeError", "KeyError",
    "__init__", "__name__", "__main__", "__file__", "__doc__",
})


def _add_edge(
    source: str, target: str, kind: str,
    filepath: str | None = None, line: int | None = None,
) -> None:
    """Add a typed edge between two entities, creating nodes as needed."""
    for name in (source, target):
        if name not in _GRAPH:
            _GRAPH[name] = Entity(name=name, kind="unknown")
    edge = Edge(source=source, target=target, kind=kind, filepath=filepath, line=line)
    _GRAPH[source].edges_out.append(edge)
    _GRAPH[target].edges_in.append(edge)


def build_knowledge_graph(root: str) -> None:
    """Build entity-relationship graph from workspace source files.

    Extracts definitions, calls, imports, and class inheritance from
    .py, .js, .ts files.  Uses AST for Python, regex for JS/TS.
    Thread-safe: clears and rebuilds the graph.

    Args:
        root: Workspace root directory.
    """
    global _GRAPH, _GRAPH_BUILT, _GRAPH_WORKSPACE

    with _GRAPH_LOCK:
        if _GRAPH_BUILT and _GRAPH_WORKSPACE == root:
            return
        _GRAPH = {}
        _GRAPH_WORKSPACE = root

    import ast as _ast

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root)

            # Module entity
            mod_name = rel.replace("/", ".").replace("\\", ".")
            if mod_name.endswith(ext):
                mod_name = mod_name[:-len(ext)]
            _add_module(mod_name, rel)

            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            if ext == ".py":
                _extract_python_graph(source, fpath, mod_name)
            else:
                _extract_ts_graph(source, fpath, mod_name)

    with _GRAPH_LOCK:
        _GRAPH_BUILT = True


def _add_module(name: str, filepath: str) -> None:
    """Add a module entity."""
    if name not in _GRAPH:
        _GRAPH[name] = Entity(name=name, kind="module", filepath=filepath)


def _extract_python_graph(source: str, fpath: str, mod_name: str) -> None:
    """Extract graph edges from Python source using AST."""
    import ast as _ast
    try:
        tree = _ast.parse(source, filename=fpath)
    except SyntaxError:
        return

    # --- Class hierarchy tracking ---
    current_class: list[str] = []

    class GraphVisitor(_ast.NodeVisitor):
        def visit_ClassDef(self, node):
            name = node.name
            full_name = f"{current_class[-1]}.{name}" if current_class else name
            # Mark entity kind
            if full_name in _GRAPH:
                _GRAPH[full_name].kind = "class"
                _GRAPH[full_name].filepath = fpath
                _GRAPH[full_name].line = node.lineno
            else:
                _GRAPH[full_name] = Entity(
                    name=full_name, kind="class",
                    filepath=fpath, line=node.lineno,
                )
            # Module defines this class
            _add_edge(mod_name, full_name, "defines", fpath, node.lineno)

            # Inheritance edges
            for base in node.bases:
                base_name = _ast.unparse(base) if hasattr(_ast, "unparse") else _get_name(base)
                if base_name and base_name not in _SKIP_CALL_NAMES:
                    _add_edge(full_name, base_name, "inherits", fpath, node.lineno)

            current_class.append(full_name)
            self.generic_visit(node)
            current_class.pop()

        def visit_FunctionDef(self, node):
            full_name = f"{current_class[-1]}.{node.name}" if current_class else node.name
            if full_name in _GRAPH:
                _GRAPH[full_name].kind = "def"
                _GRAPH[full_name].filepath = fpath
                _GRAPH[full_name].line = node.lineno
            else:
                _GRAPH[full_name] = Entity(
                    name=full_name, kind="def",
                    filepath=fpath, line=node.lineno,
                )
            _add_edge(mod_name, full_name, "defines", fpath, node.lineno)

            # Extract calls within this function
            for child in _ast.walk(node):
                if isinstance(child, _ast.Call):
                    callee = _resolve_call_name_ast(child)
                    if callee and callee not in _SKIP_CALL_NAMES:
                        _add_edge(full_name, callee, "calls", fpath, child.lineno)

            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_Import(self, node):
            for alias in node.names:
                _add_edge(mod_name, alias.name, "imports", fpath, node.lineno)

        def visit_ImportFrom(self, node):
            if node.module:
                _add_edge(mod_name, node.module, "imports", fpath, node.lineno)

    GraphVisitor().visit(tree)


def _extract_ts_graph(source: str, fpath: str, mod_name: str) -> None:
    """Extract graph edges from TypeScript/JavaScript source using regex."""
    # Function/class definitions
    def_pat = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(function|class)\s+(\w+)",
        re.MULTILINE,
    )
    arrow_pat = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",
        re.MULTILINE,
    )
    import_pat = re.compile(
        r"""import\s+(?:[\w*\s{}]*\s+from\s+)?['"]([^'"]+)['"]""",
    )

    for m in def_pat.finditer(source):
        name = m.group(2)
        line = source[:m.start()].count("\n") + 1
        kind = "class" if m.group(1) == "class" else "def"
        if name in _GRAPH:
            _GRAPH[name].kind = kind
            _GRAPH[name].filepath = fpath
            _GRAPH[name].line = line
        else:
            _GRAPH[name] = Entity(name=name, kind=kind, filepath=fpath, line=line)
        _add_edge(mod_name, name, "defines", fpath, line)

    for m in arrow_pat.finditer(source):
        name = m.group(1)
        line = source[:m.start()].count("\n") + 1
        if name not in _GRAPH:
            _GRAPH[name] = Entity(name=name, kind="def", filepath=fpath, line=line)
        _add_edge(mod_name, name, "defines", fpath, line)

    for m in import_pat.finditer(source):
        mod = m.group(1)
        line = source[:m.start()].count("\n") + 1
        _add_edge(mod_name, mod, "imports", fpath, line)


def _resolve_call_name_ast(node: Any) -> str | None:
    """Resolve a Call node's function name from AST."""
    import ast as _ast
    func = node.func
    if isinstance(func, _ast.Name):
        return func.id
    if isinstance(func, _ast.Attribute):
        return func.attr
    return None


def _get_name(node: Any) -> str | None:
    """Extract name from an AST node (for inheritance)."""
    import ast as _ast
    if isinstance(node, _ast.Name):
        return node.id
    if isinstance(node, _ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------


def find_related(name: str, depth: int = 1) -> list[dict]:
    """Find all entities directly related to the given entity name.

    Returns edges grouped by direction and kind.
    """
    if name not in _GRAPH:
        return []

    entity = _GRAPH[name]
    results: list[dict] = []

    # Outgoing
    for edge in entity.edges_out:
        results.append({
            "direction": "out",
            "kind": edge.kind,
            "target": edge.target,
            "file": edge.filepath or "",
            "line": edge.line or 0,
        })

    # Incoming
    for edge in entity.edges_in:
        results.append({
            "direction": "in",
            "kind": edge.kind,
            "target": edge.source,
            "file": edge.filepath or "",
            "line": edge.line or 0,
        })

    return results


def trace_path(from_name: str, to_name: str, max_depth: int = 5) -> list[list[str]] | None:
    """Find shortest paths between two entities in the graph (BFS).

    Returns a list of paths, each being a list of entity names.
    Returns None if no path found.
    """
    if from_name not in _GRAPH or to_name not in _GRAPH:
        return None

    if from_name == to_name:
        return [[from_name]]

    # BFS
    queue: deque[tuple[str, list[str]]] = deque()
    queue.append((from_name, [from_name]))
    visited: set[str] = {from_name}
    paths: list[list[str]] = []
    found_depth = None

    while queue:
        current, path = queue.popleft()

        if found_depth is not None and len(path) > found_depth:
            break

        if len(path) > max_depth:
            continue

        entity = _GRAPH.get(current)
        if entity is None:
            continue

        for edge in entity.edges_out:
            neighbor = edge.target
            if neighbor == to_name:
                paths.append(path + [to_name])
                found_depth = len(path)
                continue
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return paths if paths else None


def get_subgraph(name: str, depth: int = 2) -> dict:
    """Get a subgraph centered on an entity, extending N hops.

    Returns a dict with "nodes" (list of entity names) and "edges" (list of edge dicts).
    """
    if name not in _GRAPH:
        return {"nodes": [], "edges": []}

    visited: set[str] = set()
    edges: list[dict] = []
    queue: deque[tuple[str, int]] = deque()
    queue.append((name, 0))
    visited.add(name)

    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        entity = _GRAPH.get(current)
        if entity is None:
            continue

        for edge in entity.edges_out:
            edges.append({
                "source": edge.source,
                "target": edge.target,
                "kind": edge.kind,
                "file": edge.filepath or "",
                "line": edge.line or 0,
            })
            if edge.target not in visited:
                visited.add(edge.target)
                queue.append((edge.target, d + 1))

        for edge in entity.edges_in:
            edges.append({
                "source": edge.source,
                "target": edge.target,
                "kind": edge.kind,
                "file": edge.filepath or "",
                "line": edge.line or 0,
            })
            if edge.source not in visited:
                visited.add(edge.source)
                queue.append((edge.source, d + 1))

    return {"nodes": sorted(visited), "edges": edges}


def find_symbols_in_file(filepath: str) -> list[Entity]:
    """Return all entities (defs/classes) defined in a given file.

    Matches against the filepath stored on each entity.
    Handles both absolute and relative paths by matching suffixes.
    """
    results: list[Entity] = []
    for entity in _GRAPH.values():
        if entity.filepath and (
            entity.filepath == filepath
            or entity.filepath.endswith("/" + filepath)
            or entity.filepath.endswith("\\" + filepath)
            or entity.filepath == os.path.abspath(filepath)
        ):
            results.append(entity)
    return results


def find_callers_of_file(filepath: str) -> list[dict]:
    """Return all caller entities that call into symbols defined in a file.

    Returns list of {caller, callee, file, kind} dicts.
    Useful for pre-edit risk: "if I change this file, these callers may break."
    """
    symbols = find_symbols_in_file(filepath)
    symbol_names = {s.name for s in symbols}

    callers: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for sym_name in symbol_names:
        entity = _GRAPH.get(sym_name)
        if entity is None:
            continue
        for edge in entity.edges_in:
            if edge.kind == "calls":
                key = (edge.source, edge.target)
                if key not in seen:
                    seen.add(key)
                    caller_ent = _GRAPH.get(edge.source)
                    caller_file = caller_ent.filepath if caller_ent else edge.filepath
                    callers.append({
                        "caller": edge.source,
                        "callee": edge.target,
                        "file": caller_file or "",
                    })

    # Deduplicate by caller file
    caller_files: dict[str, list[str]] = {}
    for c in callers:
        f = c["file"]
        if f not in caller_files:
            caller_files[f] = []
        caller_files[f].append(c["caller"])

    return [
        {"file": f, "callers": sorted(set(names))}
        for f, names in caller_files.items()
    ]


def ensure_graph_built(workspace: str) -> bool:
    """Build the knowledge graph if not already built. Returns True if available."""
    if not _GRAPH_BUILT or _GRAPH_WORKSPACE != workspace:
        try:
            build_knowledge_graph(workspace)
        except Exception:
            return False
    return _GRAPH_BUILT


def get_graph_stats() -> dict:
    """Return summary statistics about the knowledge graph."""
    entities_by_kind: dict[str, int] = defaultdict(int)
    edges_by_kind: dict[str, int] = defaultdict(int)
    total_edges = 0

    for entity in _GRAPH.values():
        entities_by_kind[entity.kind] += 1
        for edge in entity.edges_out:
            edges_by_kind[edge.kind] += 1
            total_edges += 1

    return {
        "total_entities": len(_GRAPH),
        "entities_by_kind": dict(entities_by_kind),
        "total_edges": total_edges,
        "edges_by_kind": dict(edges_by_kind),
        "workspace": _GRAPH_WORKSPACE,
    }
