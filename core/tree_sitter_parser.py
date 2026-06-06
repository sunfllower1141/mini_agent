#!/usr/bin/env python3
"""
tree_sitter_parser.py — Multi-language source parsing via tree-sitter.

Provides fast, error-tolerant symbol extraction for Python, JavaScript,
and TypeScript.  Falls back to regex/AST when tree-sitter is not installed.

Used by codebase_map and search_ops for symbol indexing, call-graph
construction, and semantic chunking.
"""

from __future__ import annotations

import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Lazy tree-sitter import
# ---------------------------------------------------------------------------

_TREE_SITTER_AVAILABLE = False
_LANGUAGE_MODULES: dict[str, Any] = {}
_PARSERS: dict[str, Any] = {}

try:
    import tree_sitter
    _TREE_SITTER_AVAILABLE = True
except ImportError:
    pass


def _ensure_language(lang_name: str, pkg_name: str) -> bool:
    """Lazily load a tree-sitter language. Returns True if available."""
    if lang_name in _PARSERS:
        return True
    if not _TREE_SITTER_AVAILABLE:
        return False
    try:
        if lang_name not in _LANGUAGE_MODULES:
            mod = __import__(pkg_name, fromlist=["language"])
            _LANGUAGE_MODULES[lang_name] = mod
        lang = tree_sitter.Language(_LANGUAGE_MODULES[lang_name].language())
        parser = tree_sitter.Parser(lang)
        _PARSERS[lang_name] = parser
        return True
    except (ImportError, AttributeError, OSError):
        return False


def _get_parser_for_ext(ext: str) -> Any | None:
    """Get a tree-sitter parser for a file extension, or None."""
    mapping = {
        ".py": ("python", "tree_sitter_python"),
        ".pyi": ("python", "tree_sitter_python"),
        ".js": ("javascript", "tree_sitter_javascript"),
        ".mjs": ("javascript", "tree_sitter_javascript"),
        ".cjs": ("javascript", "tree_sitter_javascript"),
        ".jsx": ("javascript", "tree_sitter_javascript"),
        ".ts": ("typescript", "tree_sitter_typescript"),
        ".tsx": ("typescript", "tree_sitter_typescript"),
    }
    pair = mapping.get(ext)
    if pair is None:
        return None
    lang_name, pkg_name = pair
    if _ensure_language(lang_name, pkg_name):
        return _PARSERS[lang_name]
    return None


# ---------------------------------------------------------------------------
# Query-based symbol extraction
# ---------------------------------------------------------------------------

# Tree-sitter queries for function/class definitions
_PYTHON_QUERY = """
(function_definition
  name: (identifier) @function.name) @function.def

(class_definition
  name: (identifier) @class.name) @class.def

(call
  function: (identifier) @call.target
  (#not-match? @call.target "^(print|len|range|int|str|list|dict|set|tuple|bool|float|type|isinstance|hasattr|getattr|setattr|enumerate|zip|map|filter|sorted|reversed|min|max|sum|abs|open|super|isinstance|any|all|iter|next|ord|chr|round)$")) @call.expr

(call
  function: (attribute
    attribute: (identifier) @call.target)) @call.expr
"""

_TS_QUERY = """
(function_declaration
  name: (identifier) @function.name) @function.def

(method_definition
  name: (property_identifier) @function.name) @function.def

(class_declaration
  name: (identifier) @class.name) @class.def

(export_statement
  declaration: (function_declaration
    name: (identifier) @function.name)) @function.def

(export_statement
  declaration: (class_declaration
    name: (identifier) @class.name)) @class.def

(variable_declarator
  name: (identifier) @function.name
  value: (arrow_function)) @function.def

(call_expression
  function: (identifier) @call.target) @call.expr

(call_expression
  function: (member_expression
    property: (property_identifier) @call.target)) @call.expr
"""

# Fallback regex patterns (used when tree-sitter is unavailable)
_PY_DEF_RE = re.compile(r"^\s*(def|class)\s+(\w+)")
_PY_CALL_RE = re.compile(r"\b(\w+)\s*\(")

_TS_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)",
    re.MULTILINE,
)
_TS_ARROW_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_symbols(
    filepath: str,
) -> tuple[list[dict], list[dict], list[dict]] | None:
    """Extract definitions, calls, and imports from a source file.

    Returns (definitions, calls, imports) or None on failure.
    Each definition: {"kind": "def"|"class", "name": str, "line": int}
    Each call: {"caller": str|None, "callee": str, "line": int}
    Each import: {"module": str, "line": int, "internal": bool}

    Uses tree-sitter when available; falls back to regex/AST.
    """
    ext = os.path.splitext(filepath)[1].lower()
    parser = _get_parser_for_ext(ext)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    if parser is not None:
        return _extract_with_tree_sitter(source, parser, ext)
    else:
        return _extract_with_fallback(source, filepath, ext)


def _extract_with_tree_sitter(
    source: str, parser: Any, ext: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Extract symbols using tree-sitter queries."""
    tree = parser.parse(source.encode("utf-8"))

    if ext == ".py":
        query_str = _PYTHON_QUERY
    else:
        query_str = _TS_QUERY

    lang = parser.language
    try:
        query = lang.query(query_str)
    except Exception:
        return _extract_with_fallback(source, "", ext)

    captures = query.captures(tree.root_node)

    definitions: list[dict] = []
    calls: list[dict] = []
    seen_defs: set[str] = set()
    seen_calls: set[tuple[int, str]] = set()
    imports: list[dict] = []

    # Determine current function context for call attribution
    # Walk the tree to build a line→function mapping
    line_to_func: dict[int, str] = {}

    for node, tag in captures:
        start_line = node.start_point[0] + 1

        if tag in ("function.name", "class.name"):
            name = node.text.decode("utf-8") if node.text else ""
            if name and name not in seen_defs:
                kind = "class" if "class" in tag else "def"
                definitions.append({"kind": kind, "name": name, "line": start_line})
                seen_defs.add(name)
        elif tag == "function.def":
            # Track the line range of this function for call attribution
            name_node = None
            for child in node.children:
                if child.type in ("identifier", "property_identifier"):
                    name_node = child
                    break
            if name_node:
                func_name = name_node.text.decode("utf-8") if name_node.text else ""
                end_line = node.end_point[0] + 1
                for ln in range(start_line, end_line + 1):
                    line_to_func[ln] = func_name
        elif tag == "call.target":
            callee = node.text.decode("utf-8") if node.text else ""
            if callee:
                key = (start_line, callee)
                if key not in seen_calls:
                    caller = line_to_func.get(start_line)
                    calls.append({"caller": caller, "callee": callee, "line": start_line})
                    seen_calls.add(key)

    return definitions, calls, imports


def _extract_with_fallback(
    source: str, filepath: str, ext: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Extract symbols using regex/AST fallback."""
    definitions: list[dict] = []
    calls: list[dict] = []
    imports: list[dict] = []

    lines = source.split("\n")

    if ext == ".py":
        # Use AST for Python (more accurate than regex)
        try:
            import ast
            tree = ast.parse(source)
            return _extract_python_ast(tree, filepath)
        except SyntaxError:
            pass

        # Fall back to regex
        for i, line in enumerate(lines, 1):
            m = _PY_DEF_RE.match(line)
            if m:
                definitions.append({
                    "kind": m.group(1),
                    "name": m.group(2),
                    "line": i,
                })
    elif ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        for i, line in enumerate(lines, 1):
            m = _TS_DEF_RE.match(line)
            if m:
                definitions.append({
                    "kind": "function" if "function" in line else "class",
                    "name": m.group(1),
                    "line": i,
                })
                continue
            m = _TS_ARROW_RE.match(line)
            if m:
                definitions.append({
                    "kind": "function",
                    "name": m.group(1),
                    "line": i,
                })

    return definitions, calls, imports


def _extract_python_ast(
    tree: Any, filepath: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Extract symbols using Python's built-in AST."""
    import ast as _ast
    definitions: list[dict] = []
    calls: list[dict] = []
    imports: list[dict] = []

    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            definitions.append({
                "kind": "def",
                "name": node.name,
                "line": node.lineno,
            })
        elif isinstance(node, _ast.ClassDef):
            definitions.append({
                "kind": "class",
                "name": node.name,
                "line": node.lineno,
            })

    return definitions, calls, imports


# ---------------------------------------------------------------------------
# Symbol-only extraction (lightweight, for index population)
# ---------------------------------------------------------------------------

_SKIP_NAMES: frozenset[str] = frozenset({
    "self", "cls", "True", "False", "None", "__init__", "__name__",
    "__main__", "__file__", "__doc__",
})


def extract_definitions(
    filepath: str,
) -> list[dict]:
    """Lightweight: extract only definitions from a file.

    Returns [{"kind": "def"|"class", "name": str, "line": int}, ...].
    Uses tree-sitter when available, regex fallback otherwise.
    """
    result = extract_symbols(filepath)
    if result is None:
        return []
    return result[0]


def extract_calls(
    filepath: str,
) -> list[dict]:
    """Extract function calls from a file for call-graph construction.

    Returns [{"caller": str|None, "callee": str, "line": int}, ...].
    Uses tree-sitter when available.
    """
    result = extract_symbols(filepath)
    if result is None:
        return []
    return result[1]
