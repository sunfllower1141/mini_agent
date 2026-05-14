#!/usr/bin/env python3
"""Echo MCP server for testing.

A minimal MCP-compatible stdio server that advertises three tools:
- echo: echoes back a message
- add: adds two numbers
- fail: always returns isError=True (for error testing)

Reads line-delimited JSON-RPC from stdin, writes responses to stdout.
No external dependencies.
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the message",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"}
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "Always returns an error (for testing error paths)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why it failed",
                }
            },
            "required": [],
        },
    },
]


def handle_request(req: dict) -> dict:
    """Dispatch a JSON-RPC request to the appropriate handler."""
    method = req.get("method", "")
    rid = req.get("id")

    if rid is None:
        # Notification — acknowledge silently
        return {"jsonrpc": "2.0", "id": None}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "echo-test-server",
                    "version": "1.0.0",
                },
            },
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"tools": TOOLS},
        }
    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        if tool_name == "echo":
            text = args.get("message", "")
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        elif tool_name == "add":
            a = args.get("a", 0)
            b = args.get("b", 0)
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": str(a + b)}],
                    "isError": False,
                },
            }
        elif tool_name == "fail":
            reason = args.get("reason", "unknown")
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [
                        {"type": "text", "text": f"Intentional failure: {reason}"}
                    ],
                    "isError": True,
                },
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [
                        {"type": "text", "text": f"Unknown tool: {tool_name}"}
                    ],
                    "isError": True,
                },
            }
    elif method == "notifications/initialized":
        # Client sent initialized notification — nothing to respond
        pass
    else:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }

    # For notifications/initialized (no id), return nothing
    return {"jsonrpc": "2.0", "id": None}


def main() -> None:
    """Main loop: read line, handle, write response."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        # Only write responses that have an id (skip notification acks)
        if resp.get("id") is not None or resp.get("result") is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
