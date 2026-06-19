#!/usr/bin/env python3
"""
ast_ops.py -- AST-native tools for mini_agent.

Implements Dirac-style AST-precise tools built on tree-sitter:
  - get_file_skeleton: Extract structural outline (class/function signatures only)
  - get_function: Extract complete implementation of specific functions/methods
  - replace_symbol: Replace AST node ranges precisely (future)
  - rename_symbol: Rename symbols across files (future)

These tools are significantly more token-efficient than read_file for
understanding file structure, and more reliable than edit_file for
function-level replacements.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Optional

from core.anchor_manager import AnchorStateManager, format_line_with_anchor
from core.symbol_context_resolver import resolve_symbol_context
from core.tree_sitter_parser import _get_parser_for_ext, _TREE_SITTER_AVAILABLE


# Unicode box-drawing character used by read_file hash_lines output
_BOX = "\u2502"  # │


def _format_anchored(lineno: int, gutter: int, anchor: str, content: str) -> str:
    """Format a line matching read_file(hash_lines=True) output.

    Produces: ``{lineno:>gutter} {anchor}│ {content}``
    This ensures AST-native tool output is pipeable to edit_file/edit_lines
    without requiring a second read_file call.
    """
    return f"{lineno:>{gutter}} {anchor}{_BOX} {content}"


# ---------------------------------------------------------------------------
# Hash caching for functions (skip re-reading unchanged functions)
# ---------------------------------------------------------------------------

# Cache: "path::function_name#anchored" -> hash_hex
_FUNCTION_HASH_CACHE: dict[str, str] = {}


def _hash_content(text: str) -> str:
    """Compute a content hash for change detection."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# get_file_skeleton
# ---------------------------------------------------------------------------

def get_file_skeleton(
    file_path: str,
    *,
    include_anchors: bool = False,
    show_call_graph: bool = False,
    task_id: Optional[str] = None,
    _source: Optional[str] = None,
) -> str:
    """
    Extract the structural skeleton of a source file.

    Strips all implementation bodies, keeping only:
      - Function/method signatures
      - Class definitions
      - Decorators, export keywords, JSDoc comments

    Args:
        file_path: Absolute path to the source file.
        include_anchors: If True, prefix lines with stable word anchors.
        show_call_graph: If True, include call-graph annotations.
        task_id: Optional task ID for anchor state.
        _source: Pre-read file content (caller provides to avoid redundant disk I/O).

    Returns:
        Skeleton text, or error message if parsing fails.
    """
    ext = os.path.splitext(file_path)[1].lower()
    parser = _get_parser_for_ext(ext)

    if parser is None:
        return f"Unsupported file type: {ext}"

    if _source is not None:
        source = _source
    else:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return f"Could not read file: {e}"

    if not source.strip():
        return "Empty file (no definitions found)"

    lines = source.split("\n")
    gutter_width = max(len(str(len(lines))), 1)

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception as e:
        return f"Could not parse {ext} file: {e}"

    root = tree.root_node

    # Collect definition nodes
    definitions = _collect_definitions(root, ext, source)

    if not definitions:
        return "No definitions found in file"

    # Get anchors if requested
    anchors: Optional[list[str]] = None
    if include_anchors:
        anchors = AnchorStateManager.reconcile(file_path, lines, task_id)

    # Build skeleton output
    out_lines: list[str] = []
    for defn in definitions:
        start_line = defn["start_line"]
        end_line = defn["end_line"]
        kind = defn["kind"]
        name = defn.get("name", "?")
        signature = defn.get("signature", "")

        # Show the signature line
        sig_line = lines[start_line].strip() if start_line < len(lines) else ""
        if include_anchors and anchors and start_line < len(anchors):
            anchor = anchors[start_line]
            out_lines.append(_format_anchored(start_line + 1, gutter_width, anchor, sig_line))
        else:
            out_lines.append(f"{start_line + 1}: {sig_line}")

        # Show any decorators/annotations above
        for d in defn.get("decorators", []):
            dec_line = lines[d].strip() if d < len(lines) else ""
            if include_anchors and anchors and d < len(anchors):
                out_lines.insert(-1, _format_anchored(d + 1, gutter_width, anchors[d], dec_line))
            else:
                out_lines.insert(-1, f"{d + 1}: {dec_line}")

        # Show docstring if present (first line only)
        if defn.get("docstring"):
            ds_line = defn["docstring"]
            if include_anchors and anchors and ds_line < len(anchors):
                out_lines.append(_format_anchored(ds_line + 1, gutter_width, anchors[ds_line], lines[ds_line].strip()))
            else:
                out_lines.append(f"{ds_line + 1}: {lines[ds_line].strip()}")

        # Body placeholder
        body_start = defn.get("body_start_line", start_line + 1)
        body_end = defn.get("body_end_line", end_line)
        body_line_count = body_end - body_start
        if body_line_count > 0:
            out_lines.append(f"    ... ({body_line_count} implementation lines) ...")

        # Show call graph if requested
        if show_call_graph and defn.get("calls"):
            calls = defn["calls"][:5]
            call_str = ", ".join(calls)
            out_lines.append(f"    calls: [{call_str}]")

        out_lines.append("")  # blank line between definitions

    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# get_function
# ---------------------------------------------------------------------------

def get_function(
    file_path: str,
    function_names: list[str],
    *,
    include_anchors: bool = False,
    task_id: Optional[str] = None,
    _source: Optional[str] = None,
) -> tuple[str, list[str]]:
    """
    Extract the complete implementation of specific functions/methods.

    Args:
        file_path: Absolute path to the source file.
        function_names: List of function/method names to extract.
        include_anchors: If True, prefix lines with stable word anchors.
        task_id: Optional task ID for anchor state.
        _source: Pre-read file content (caller provides to avoid redundant disk I/O).

    Returns:
        (formatted_content, found_names) tuple.
        formatted_content contains the extracted functions separated by ---.
        found_names lists which functions were actually found.
    """
    ext = os.path.splitext(file_path)[1].lower()
    parser = _get_parser_for_ext(ext)

    if parser is None:
        return f"Unsupported file type: {ext}", []

    if _source is not None:
        source = _source
    else:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return f"Could not read file: {e}", []

    if not source.strip():
        return "Empty file (no functions found)", []

    lines = source.split("\n")
    gutter_width = max(len(str(len(lines))), 1)

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception as e:
        return f"Could not parse {ext} file: {e}", []

    root = tree.root_node
    definitions = _collect_definitions(root, ext, source)

    # Get anchors if requested
    anchors: Optional[list[str]] = None
    if include_anchors:
        anchors = AnchorStateManager.reconcile(file_path, lines, task_id)

    # Look up function names (support dot-separated paths like "Class.method")
    name_set = set(function_names)
    found_names: list[str] = []
    results: list[str] = []

    for defn in definitions:
        name = defn.get("name", "")
        # Match against full name or just the function name
        matches = name in name_set

        if not matches:
            # Check if any requested name matches this definition's name
            for fn in function_names:
                # Support "ClassName.method_name" pattern
                if "." in fn:
                    parent_name = defn.get("parent_name", "")
                    full_name = f"{parent_name}.{name}" if parent_name else name
                    if full_name == fn or name == fn.split(".")[-1]:
                        matches = True
                        break
                elif fn == name:
                    matches = True
                    break

        if not matches:
            continue

        found_names.append(name)

        # Extract the full source range
        start_byte = defn["start_byte"]
        end_byte = defn["end_byte"]
        func_source = source[start_byte:end_byte]

        # Compute hash for caching
        func_hash = _hash_content(func_source)
        cache_key = f"{file_path}::{name}#{'anchored' if include_anchors else 'plain'}"
        last_hash = _FUNCTION_HASH_CACHE.get(cache_key)

        header = f"--- {file_path} :: {name} ---"

        if last_hash and last_hash == func_hash:
            results.append(
                f"{header}\n"
                f"no changes have been made to the function since your last read "
                f"(Hash: {func_hash})"
            )
        else:
            _FUNCTION_HASH_CACHE[cache_key] = func_hash

            # Add anchors if requested
            if include_anchors and anchors:
                start_line = defn["start_line"]
                end_line = defn["end_line"]
                anchored_lines = []
                for i in range(start_line, min(end_line, len(lines), len(anchors))):
                    anchored_lines.append(_format_anchored(i + 1, gutter_width, anchors[i], lines[i]))
                anchored_body = "\n".join(anchored_lines)

                # Resolve import/class context (Dirac SymbolContextResolver pattern)
                context_str = ""
                try:
                    def_node = _find_definition_node(
                        tree.root_node, source, name,
                        parent_name=defn.get("parent_name", ""),
                    )
                    if def_node:
                        context_str = resolve_symbol_context(
                            node=def_node,
                            file_content=source,
                            parser=parser,
                            ext=ext,
                            anchors=anchors,
                            root_node=tree.root_node,
                        )
                except Exception:
                    pass  # Context resolution is best-effort

                func_hash_line = f"[Function Hash: {func_hash}]"
                if context_str:
                    anchor_header = (
                        "All Hash Anchors provided below are stable and can "
                        "be used with edit_file directly."
                    )
                    results.append(
                        f"{header}\n{func_hash_line}\n{anchor_header}\n"
                        f"{context_str}\n{anchored_body}"
                    )
                else:
                    results.append(
                        f"{header}\n{func_hash_line}\n{anchored_body}"
                    )
            else:
                results.append(f"{header}\n{func_source}")

    if not results:
        return (
            f"None of the requested functions ({', '.join(function_names)}) "
            f"were found in {file_path}",
            [],
        )

    return "\n\n".join(results), found_names


# ---------------------------------------------------------------------------
# Definition collection (shared by skeleton and function extraction)
# ---------------------------------------------------------------------------

def _collect_definitions(
    root: Any, ext: str, source: str
) -> list[dict[str, Any]]:
    """Walk the AST and collect all top-level and nested definitions."""
    definitions: list[dict[str, Any]] = []

    if ext in (".py", ".pyi"):
        _collect_python_definitions(root, source, definitions, parent_name="")
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        _collect_ts_definitions(root, source, definitions, parent_name="")

    return definitions


def _collect_python_definitions(
    node: Any, source: str, results: list[dict], parent_name: str = ""
) -> None:
    """Recursively collect Python function/class definitions."""
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte]
            body_node = node.child_by_field_name("body")
            params_node = node.child_by_field_name("parameters")

            # Extract signature
            sig_start = node.start_byte
            sig_end = (
                body_node.start_byte if body_node else node.end_byte
            )
            signature = source[sig_start:sig_end].split("\n")[0].strip()

            # Collect decorators
            decorators = []
            prev = node.prev_sibling
            while prev and prev.type == "decorator":
                decorators.append(prev.start_point[0])
                prev = prev.prev_sibling

            # Detect docstring
            docstring_line = None
            if body_node and body_node.named_children:
                first_stmt = body_node.named_children[0]
                if first_stmt.type == "expression_statement":
                    expr_child = first_stmt.named_children[0] if first_stmt.named_children else None
                    if expr_child and expr_child.type == "string":
                        docstring_line = first_stmt.start_point[0]

            full_name = f"{parent_name}.{name}" if parent_name else name

            results.append({
                "kind": "function" if not parent_name else "method",
                "name": full_name,
                "parent_name": parent_name,
                "signature": signature,
                "decorators": decorators,
                "docstring": docstring_line,
                "start_line": node.start_point[0],
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "body_start_line": body_node.start_point[0] if body_node else node.start_point[0] + 1,
                "body_end_line": body_node.end_point[0] + 1 if body_node else node.end_point[0] + 1,
                "calls": _collect_calls(body_node, source) if body_node else [],
            })

            # Recurse into body for nested definitions
            if body_node:
                _collect_python_definitions(body_node, source, results, full_name)

    elif node.type == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte]
            body_node = node.child_by_field_name("body")
            full_name = f"{parent_name}.{name}" if parent_name else name

            # Collect decorators
            decorators = []
            prev = node.prev_sibling
            while prev and prev.type == "decorator":
                decorators.append(prev.start_point[0])
                prev = prev.prev_sibling

            results.append({
                "kind": "class",
                "name": full_name,
                "parent_name": parent_name,
                "signature": source[node.start_byte : node.end_byte].split("\n")[0].strip(),
                "decorators": decorators,
                "docstring": None,
                "start_line": node.start_point[0],
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "body_start_line": body_node.start_point[0] if body_node else node.start_point[0] + 1,
                "body_end_line": body_node.end_point[0] + 1 if body_node else node.end_point[0] + 1,
                "calls": [],
            })

            # Recurse into class body
            if body_node:
                _collect_python_definitions(body_node, source, results, full_name)

    else:
        # Recurse into children
        for child in node.named_children:
            _collect_python_definitions(child, source, results, parent_name)


def _collect_ts_definitions(
    node: Any, source: str, results: list[dict], parent_name: str = ""
) -> None:
    """Recursively collect TypeScript/JavaScript function/class definitions."""
    if node.type in ("function_declaration", "method_definition"):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte]
            body_node = node.child_by_field_name("body")
            full_name = f"{parent_name}.{name}" if parent_name else name

            # Check for export
            is_exported = False
            parent = node.parent
            while parent:
                if parent.type == "export_statement":
                    is_exported = True
                    break
                parent = parent.parent

            results.append({
                "kind": "function",
                "name": full_name,
                "parent_name": parent_name,
                "signature": source[node.start_byte : body_node.start_byte if body_node else node.end_byte].split("\n")[0].strip(),
                "decorators": [],
                "docstring": None,
                "start_line": node.start_point[0],
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "body_start_line": body_node.start_point[0] if body_node else node.start_point[0] + 1,
                "body_end_line": body_node.end_point[0] + 1 if body_node else node.end_point[0] + 1,
                "calls": _collect_calls(body_node, source) if body_node else [],
                "exported": is_exported,
            })

            if body_node:
                _collect_ts_definitions(body_node, source, results, full_name)

    elif node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte]
            body_node = node.child_by_field_name("body")
            full_name = f"{parent_name}.{name}" if parent_name else name

            results.append({
                "kind": "class",
                "name": full_name,
                "parent_name": parent_name,
                "signature": source[node.start_byte : node.end_byte].split("\n")[0].strip(),
                "decorators": [],
                "docstring": None,
                "start_line": node.start_point[0],
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "body_start_line": body_node.start_point[0] if body_node else node.start_point[0] + 1,
                "body_end_line": body_node.end_point[0] + 1 if body_node else node.end_point[0] + 1,
                "calls": [],
            })

            if body_node:
                _collect_ts_definitions(body_node, source, results, full_name)

    elif node.type == "arrow_function":
        # Variable declarator with arrow function
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            name_node = parent.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte : name_node.end_byte]
                full_name = f"{parent_name}.{name}" if parent_name else name
                results.append({
                    "kind": "function",
                    "name": full_name,
                    "parent_name": parent_name,
                    "signature": source[parent.start_byte : node.start_byte].strip(),
                    "decorators": [],
                    "docstring": None,
                    "start_line": parent.start_point[0],
                    "end_line": node.end_point[0] + 1,
                    "start_byte": parent.start_byte,
                    "end_byte": node.end_byte,
                    "body_start_line": node.start_point[0],
                    "body_end_line": node.end_point[0] + 1,
                    "calls": _collect_calls(node, source),
                })

    else:
        for child in node.named_children:
            _collect_ts_definitions(child, source, results, parent_name)


def _collect_calls(node: Any, source: str) -> list[str]:
    """Collect function call names within a node."""
    calls: list[str] = []
    if node is None:
        return calls

    def _walk(n: Any) -> None:
        if n.type == "call":
            func_node = n.child_by_field_name("function")
            if func_node:
                if func_node.type == "identifier":
                    calls.append(source[func_node.start_byte : func_node.end_byte])
                elif func_node.type == "attribute":
                    attr_node = func_node.child_by_field_name("attribute")
                    if attr_node:
                        calls.append(source[attr_node.start_byte : attr_node.end_byte])
        for child in n.named_children:
            _walk(child)

    _walk(node)
    return list(dict.fromkeys(calls))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# replace_symbol (Dirac-style AST-precise replacement)
# ---------------------------------------------------------------------------

def replace_symbol(
    file_path: str,
    symbol_name: str,
    new_text: str,
    *,
    symbol_type: str = "function",
) -> str:
    """
    Replace a symbol (function/method/class) with new code using AST byte ranges.

    This is more reliable than edit_file because it targets the exact AST node
    range rather than relying on string matching.

    Args:
        file_path: Absolute path to the source file.
        symbol_name: Name of the symbol to replace (supports "Class.method").
        new_text: Complete replacement text including all decorators, JSDoc, etc.
        symbol_type: "function", "method", or "class".

    Returns:
        Success/error message.
    """
    ext = os.path.splitext(file_path)[1].lower()
    parser = _get_parser_for_ext(ext)

    if parser is None:
        return f"Unsupported file type: {ext}"

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return f"Could not read file: {e}"

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception as e:
        return f"Could not parse file: {e}"

    definitions = _collect_definitions(tree.root_node, ext, source)

    # Find the symbol
    target = None
    for defn in definitions:
        name = defn.get("name", "")
        if "." in symbol_name:
            if name == symbol_name:
                target = defn
                break
        else:
            if name == symbol_name or name.endswith(f".{symbol_name}"):
                target = defn
                break

    if target is None:
        return f"Symbol '{symbol_name}' not found in {file_path}"

    start_byte = target["start_byte"]
    end_byte = target["end_byte"]

    # Replace
    new_source = source[:start_byte] + new_text + source[end_byte:]

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_source)
    except OSError as e:
        return f"Error writing file: {e}"

    # Invalidate anchor state for this file
    AnchorStateManager.clear_state(file_path)

    return (
        f"Successfully replaced symbol '{symbol_name}' in {file_path}. "
        f"Any existing hash anchors for this symbol are now stale."
    )


# ---------------------------------------------------------------------------
# get_symbol_range (Dirac-style precise AST range lookup)
# ---------------------------------------------------------------------------


def get_symbol_range(
    file_path: str,
    symbol: str,
    *,
    type: Optional[str] = None,
    _source: Optional[str] = None,
) -> dict[str, Any] | None:
    """
    Get the precise byte range of a symbol for use with symbol-level
    replacements.

    Returns a dict with:
      - startIndex: byte offset of the symbol start
      - endIndex: byte offset of the symbol end
      - startLine: 0-indexed line
      - nameText: symbol name

    The range includes leading decorators/annotations, export keywords,
    and docstrings, mirroring Dirac's getExtendedRange logic.

    Args:
        _source: Pre-read file content (caller provides to avoid redundant disk I/O).
    """
    ext = os.path.splitext(file_path)[1].lower()
    parser = _get_parser_for_ext(ext)
    if parser is None:
        return None

    if _source is not None:
        source = _source
    else:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError):
            return None

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return None

    definitions = _collect_definitions(tree.root_node, ext, source)
    target = _match_symbol(definitions, symbol, type)
    if target is None:
        return None

    # Compute extended range (include decorators, export wrappers, etc.)
    def_node = _find_definition_node(
        tree.root_node, source, target.get("name", symbol),
        parent_name=target.get("parent_name", ""),
    )

    if def_node:
        start_index, end_index, start_line = _get_extended_range(def_node, source)
    else:
        start_index = target["start_byte"]
        end_index = target["end_byte"]
        start_line = target["start_line"]

    return {
        "startIndex": start_index,
        "endIndex": end_index,
        "startLine": start_line,
        "nameText": target["name"],
    }


# ---------------------------------------------------------------------------
# AST node helpers
# ---------------------------------------------------------------------------


def _match_symbol(
    definitions: list[dict],
    symbol: str,
    symbol_type: Optional[str] = None,
) -> dict | None:
    """Find a definition by name and optional type."""
    normalized_req = symbol.replace("::", ".")

    for defn in definitions:
        name = defn.get("name", "")
        normalized_name = name.replace("::", ".")
        kind = defn.get("kind", "")

        if normalized_name == normalized_req or normalized_name.endswith(
            "." + normalized_req
        ):
            if _are_types_compatible(kind, symbol_type):
                return defn

    return None


def _are_types_compatible(def_kind: str, req_type: Optional[str]) -> bool:
    """Check if a definition kind matches the requested type."""
    if req_type is None:
        return True
    if def_kind == req_type:
        return True
    # Synonyms: function === method
    if def_kind in ("function", "method") and req_type in ("function", "method"):
        return True
    return False


def _find_definition_node(
    root_node: Any,
    source: str,
    name: str,
    parent_name: str = "",
) -> Any | None:
    """
    Walk the tree-sitter AST to find the node matching a given
    function/method/class name.
    """
    # Try to match by walking the tree and comparing names
    ext = _guess_ext_from_tree(root_node)
    if ext is None:
        return None

    target_name = name.split(".")[-1]  # Last component
    return _walk_for_def(root_node, source, target_name, parent_name, ext)


def _guess_ext_from_tree(root_node: Any) -> str | None:
    """Guess file extension from tree-sitter language."""
    lang_name = getattr(root_node, "grammar_name", None)
    if lang_name is None:
        try:
            lang_name = root_node.language.name
        except Exception:
            return None
    lang_map = {
        "python": ".py",
        "javascript": ".js",
        "typescript": ".ts",
        "tsx": ".tsx",
    }
    return lang_map.get(lang_name)


def _walk_for_def(
    node: Any, source: str, target_name: str, parent_name: str, ext: str,
) -> Any | None:
    """Recursively walk AST to find a definition matching target_name."""
    if ext in (".py", ".pyi"):
        def_types = ("function_definition", "class_definition")
    else:
        def_types = (
            "function_declaration", "method_definition", "class_declaration",
            "arrow_function",
        )

    for i in range(node.child_count):
        child = node.child(i)
        if child and child.type in def_types:
            name_node = child.child_by_field_name("name")
            if name_node:
                child_name = source[name_node.start_byte:name_node.end_byte]
                child_parent = _get_enclosing_class_name(child, source, ext)
                full = f"{child_parent}.{child_name}" if child_parent else child_name

                if full == f"{parent_name}.{target_name}" if parent_name else full == target_name:
                    return child

                # Also check body for nested defs
                body = child.child_by_field_name("body")
                if body:
                    result = _walk_for_def(body, source, target_name, full, ext)
                    if result:
                        return result

    return None


def _get_enclosing_class_name(node: Any, source: str, ext: str) -> str | None:
    """Walk up from a node to find the enclosing class name."""
    if ext in (".py", ".pyi"):
        class_type = "class_definition"
    else:
        class_type = "class_declaration"

    parent = node.parent
    while parent:
        if parent.type == class_type:
            name_node = parent.child_by_field_name("name")
            if name_node:
                return source[name_node.start_byte:name_node.end_byte]
        parent = parent.parent

    return None


def _get_extended_range(
    target_node: Any, file_content: str,
) -> tuple[int, int, int]:
    """
    Compute the extended byte range of a definition, including
    decorators, export wrappers, and JSDoc comments.

    Mirrors Dirac's ASTAnchorBridge.getExtendedRange.
    """
    start_index = target_node.start_byte
    end_index = target_node.end_byte
    start_line = target_node.start_point[0]

    current = target_node

    # Extend upward through wrappers
    wrapper_types = (
        "export_statement", "decorated_definition",
        "ambient_declaration",
    )
    while current.parent and current.parent.type in wrapper_types:
        current = current.parent
        start_index = current.start_byte
        end_index = current.end_byte
        start_line = current.start_point[0]

    # Include leading comments and decorators
    while current.previous_named_sibling:
        prev = current.previous_named_sibling
        prev_type = prev.type
        if prev_type in ("comment", "decorator", "attribute") or "comment" in prev_type:
            start_index = prev.start_byte
            start_line = prev.start_point[0]
            current = prev
        else:
            break

    return start_index, end_index, start_line
