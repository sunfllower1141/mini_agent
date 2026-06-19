#!/usr/bin/env python3
"""
symbol_context_resolver.py -- Resolve imports and class context for AST symbols.

Dirac-equivalent of SymbolContextResolver.ts.  When extracting a function
body, this module finds the relevant imports and enclosing class
properties so the agent sees the full context without reading the
entire file.

Supports Python (via tree-sitter) and TypeScript/JavaScript (via
tree-sitter or regex fallback).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from core.anchor_manager import AnchorStateManager
from core.tree_sitter_parser import _get_parser_for_ext, _TREE_SITTER_AVAILABLE

# ---------------------------------------------------------------------------
# Query strings per language (mirrors Dirac's SymbolContextResolver.getQueryStrings)
# ---------------------------------------------------------------------------

_MAX_CONTEXT_LINES = 30

_LANGUAGE_QUERIES = {
    ".py": {
        "context_query": """
(import_from_statement) @import
(import_statement) @import
(class_definition) @class
(function_definition) @method
(assignment left: (attribute object: (identifier) @self attribute: (identifier) @property)) @property
(identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_definition"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".pyi": {
        "context_query": """
(import_from_statement) @import
(import_statement) @import
(class_definition) @class
(identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_definition"],
        "property_capture_names": [],
        "reference_capture_names": ["ref"],
    },
    ".ts": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(class_heritage) @class.heritage
(public_field_definition) @property
(private_property_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".tsx": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(class_heritage) @class.heritage
(public_field_definition) @property
(private_property_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".js": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(public_field_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".jsx": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(public_field_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".mjs": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(public_field_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
    ".cjs": {
        "context_query": """
(import_statement) @import
(class_declaration) @class
(public_field_definition) @property
(method_definition) @method
(identifier) @ref
(property_identifier) @ref
""",
        "import_capture_name": "import",
        "class_capture_name": "class",
        "class_node_types": ["class_declaration"],
        "property_capture_names": ["property"],
        "reference_capture_names": ["ref"],
    },
}


def _get_query_config(ext: str) -> dict | None:
    """Return the tree-sitter query configuration for a file extension."""
    return _LANGUAGE_QUERIES.get(ext.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_symbol_context(
    *,
    node: Any,
    file_content: str,
    parser: Any,
    ext: str,
    anchors: list[str],
    max_context_lines: int = _MAX_CONTEXT_LINES,
    root_node: Any = None,
) -> str:
    """
    Resolve relevant context (imports and class properties) for a
    given tree-sitter syntax node.

    Returns a multi-line string with:
      - Import statements that are relevant to identifiers used in the node
      - Enclosing class declaration and its used properties
    Capped at *max_context_lines* lines total.

    Args:
        node: tree-sitter SyntaxNode for the target symbol.
        file_content: Full source text of the file.
        parser: tree-sitter Parser instance.
        ext: File extension (with leading dot, e.g. ".py").
        anchors: Pre-computed word anchors for all lines.
        max_context_lines: Max context lines to return (default 30).
        root_node: Optional pre-parsed root node (avoids re-parsing).

    Returns:
        Context string with anchored lines, or empty string.
    """
    if not _TREE_SITTER_AVAILABLE:
        return ""

    config = _get_query_config(ext)
    if not config:
        return ""

    try:
        language = parser.language
        query = language.query(config["context_query"])

        if root_node is None:
            tree = parser.parse(file_content.encode("utf-8"))
            root_node = tree.root_node

        captures = query.captures(root_node)

        # 1. Identify all identifiers used within the target node
        used_identifiers = _get_used_identifiers(node)

        # 2. Identify relevant imports (imports that mention a used identifier)
        relevant_imports = _get_relevant_imports(
            captures,
            used_identifiers,
            config["import_capture_name"],
        )

        # 3. Find enclosing class and its referenced properties
        class_context = _get_class_context(node, captures, used_identifiers, config)

        # 4. Assemble and cap
        return _assemble_context(
            relevant_imports,
            class_context,
            file_content,
            anchors,
            max_context_lines,
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_used_identifiers(node: Any) -> set[str]:
    """Walk a tree-sitter node and collect all identifier tokens."""
    identifiers: set[str] = set()

    def walk(n: Any) -> None:
        if n.type and "identifier" in n.type:
            text = n.text
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            if text:
                identifiers.add(text)
        for i in range(n.child_count):
            child = n.child(i)
            if child:
                walk(child)

    walk(node)
    return identifiers


def _get_relevant_imports(
    captures: list[tuple[Any, str]],
    used_identifiers: set[str],
    import_capture_name: str,
) -> list[Any]:
    """Filter captures to those import nodes mentioning a used identifier."""
    relevant: list[Any] = []
    for capture_node, capture_name in captures:
        if capture_name == import_capture_name:
            import_text = capture_node.text
            if isinstance(import_text, bytes):
                import_text = import_text.decode("utf-8")
            for identifier in used_identifiers:
                # Word-boundary match
                if re.search(rf"\b{re.escape(identifier)}\b", import_text):
                    relevant.append(capture_node)
                    break
    return relevant


def _get_class_context(
    node: Any,
    captures: list[tuple[Any, str]],
    used_identifiers: set[str],
    config: dict,
) -> dict | None:
    """Find the enclosing class and any properties referenced by the node."""
    parent = node.parent
    while parent and parent.type not in config["class_node_types"]:
        parent = parent.parent

    if not parent:
        return None

    class_node = parent
    property_nodes: list[Any] = []

    for capture_node, capture_name in captures:
        if capture_name in config["property_capture_names"]:
            # Walk up from property to see if it belongs to this class
            prop_parent = capture_node.parent
            belongs_to_class = False
            while prop_parent:
                if prop_parent is class_node:
                    belongs_to_class = True
                    break
                prop_parent = prop_parent.parent

            if belongs_to_class:
                # Find the property's name
                name_node = _find_property_name(capture_node)
                if name_node:
                    name_text = name_node.text
                    if isinstance(name_text, bytes):
                        name_text = name_text.decode("utf-8")
                    if name_text in used_identifiers:
                        property_nodes.append(capture_node)

    return {"class_node": class_node, "property_nodes": property_nodes}


def _find_property_name(prop_node: Any) -> Any | None:
    """Extract the name node from a property definition node."""
    # 1. Try the "name" field (standard for many grammars)
    name_node = prop_node.child_by_field_name("name")
    if name_node:
        return name_node

    # 2. Walk children looking for a property_identifier or identifier
    def find_name(n: Any) -> Any | None:
        if n.type in ("property_identifier", "identifier"):
            text = n.text
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            if text and text not in ("self", "this"):
                return n
        for i in range(n.child_count):
            child = n.child(i)
            if child:
                result = find_name(child)
                if result:
                    return result
        return None

    return find_name(prop_node)


def _assemble_context(
    imports: list[Any],
    class_context: dict | None,
    file_content: str,
    anchors: list[str],
    max_lines: int,
) -> str:
    """Assemble relevant imports and class context into an anchored string."""
    file_lines = file_content.split("\n")

    # Collect (line_text, line_index, anchor) tuples
    entries: list[tuple[str, int, str]] = []

    # 1. Add imports
    for imp in imports:
        start = imp.start_point[0]
        end = imp.end_point[0]
        for i in range(start, end + 1):
            if i < len(file_lines) and i < len(anchors):
                entries.append((file_lines[i], i, anchors[i]))

    # 2. Add class head and properties
    if class_context:
        class_node = class_context["class_node"]
        class_start = class_node.start_point[0]
        if class_start < len(file_lines) and class_start < len(anchors):
            entries.append((file_lines[class_start], class_start, anchors[class_start]))

        for prop in class_context["property_nodes"]:
            start = prop.start_point[0]
            end = prop.end_point[0]
            for i in range(start, end + 1):
                if i < len(file_lines) and i < len(anchors):
                    entries.append((file_lines[i], i, anchors[i]))

    # 3. Deduplicate and sort by line index
    seen: set[int] = set()
    sorted_entries: list[tuple[str, int, str]] = []
    for text, idx, anchor in sorted(entries, key=lambda e: e[1]):
        if idx not in seen:
            seen.add(idx)
            sorted_entries.append((text, idx, anchor))

    if not sorted_entries:
        return ""

    # 4. Format with ellipsis gaps and cap
    result_parts: list[str] = []
    last_idx = -1
    line_count = 0

    for text, idx, anchor in sorted_entries:
        if line_count >= max_lines:
            break
        if last_idx != -1 and idx > last_idx + 1:
            result_parts.append("...")
        result_parts.append(f"{anchor}| {text}")
        last_idx = idx
        line_count += 1

    if result_parts and last_idx != -1:
        result_parts.append("...")

    return "\n".join(result_parts)
