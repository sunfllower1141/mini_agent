#!/usr/bin/env python3
"""Minimal LSP-compatible stdio server for testing.

Handles the subset of LSP used by tools/lsp.py:

  initialize          -> returns capabilities
  initialized         -> notification, no response
  textDocument/didOpen -> notification, sends publishDiagnostics
  textDocument/definition  -> hardcoded location
  textDocument/references  -> two hardcoded locations
  textDocument/hover       -> markdown hover
  shutdown -> null result
  exit     -> notification, exits process

Reads line-delimited JSON from stdin, writes responses to stdout.
No external dependencies.
"""

from __future__ import annotations

import json
import sys

# ---------------------------------------------------------------------------
# Hardcoded test data
# ---------------------------------------------------------------------------

_TEST_URI = "file:///test/fixture.py"

_SERVER_CAPABILITIES = {
    "definitionProvider": True,
    "referencesProvider": True,
    "hoverProvider": True,
}

_DEFINITION_RESULT = {
    "uri": _TEST_URI,
    "range": {
        "start": {"line": 5, "character": 4},
        "end": {"line": 5, "character": 10},
    },
}

_REFERENCES_RESULT = [
    {
        "uri": _TEST_URI,
        "range": {
            "start": {"line": 10, "character": 2},
            "end": {"line": 10, "character": 8},
        },
    },
    {
        "uri": _TEST_URI,
        "range": {
            "start": {"line": 20, "character": 0},
            "end": {"line": 20, "character": 6},
        },
    },
]

_HOVER_RESULT = {
    "contents": {
        "kind": "markdown",
        "value": "**mock_function**\n\nTest hover result from lsp_mock_server.\n\n```python\ndef mock_function(x: int) -> str: ...\n```",
    },
    "range": {
        "start": {"line": 5, "character": 4},
        "end": {"line": 5, "character": 10},
    },
}

_DIAGNOSTICS = [
    {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 10},
        },
        "severity": 1,  # Error
        "source": "mock-lsp",
        "code": "E001",
        "message": "Mock diagnostic: unused import 'os'",
    },
    {
        "range": {
            "start": {"line": 3, "character": 4},
            "end": {"line": 3, "character": 14},
        },
        "severity": 2,  # Warning
        "source": "mock-lsp",
        "code": "W001",
        "message": "Mock diagnostic: variable 'x' is never used",
    },
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _make_response(rid, result):
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _make_error(rid, code, message):
    """Build a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _write(obj):
    """Write a JSON object as a single line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle_initialize(rid, _params):
    """Return server capabilities."""
    return _make_response(rid, {
        "capabilities": _SERVER_CAPABILITIES,
        "serverInfo": {
            "name": "lsp-mock-server",
            "version": "1.0.0",
        },
    })


def handle_definition(rid, _params):
    """Return a hardcoded definition location."""
    return _make_response(rid, _DEFINITION_RESULT)


def handle_references(rid, _params):
    """Return two hardcoded reference locations."""
    return _make_response(rid, _REFERENCES_RESULT)


def handle_hover(rid, _params):
    """Return a hardcoded markdown hover."""
    return _make_response(rid, _HOVER_RESULT)


def handle_shutdown(rid, _params):
    """Acknowledge shutdown with null result."""
    return _make_response(rid, None)


def handle_notification(method, params):
    """Process a notification (no id -> side effects only, no response)."""
    if method == "textDocument/didOpen":
        uri = params.get("textDocument", {}).get("uri", _TEST_URI)
        # Respond with diagnostics for any didOpen URI (real LSP servers do this)
        _write({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": _DIAGNOSTICS},
        })
    elif method == "exit":
        sys.exit(0)
    # initialized and other notifications are silently ignored


def dispatch(req):
    """Route a parsed JSON-RPC message to the correct handler."""
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params", {})

    if rid is None:
        # Notification -- no response expected
        handle_notification(method, params)
        return None

    # Request -- dispatch by method
    if method == "initialize":
        return handle_initialize(rid, params)
    elif method == "textDocument/definition":
        return handle_definition(rid, params)
    elif method == "textDocument/references":
        return handle_references(rid, params)
    elif method == "textDocument/hover":
        return handle_hover(rid, params)
    elif method == "shutdown":
        return handle_shutdown(rid, params)
    else:
        return _make_error(rid, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    """Read line-delimited JSON from stdin, dispatch, write responses."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = dispatch(req)
        if resp is not None:
            _write(resp)


if __name__ == "__main__":
    main()
