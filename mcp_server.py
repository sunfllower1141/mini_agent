#!/usr/bin/env python3
"""A simple MCP server exposing useful tools via stdio transport.

Tools:
    add         — add two numbers
    multiply    — multiply two numbers
    now         — get current date/time
    count_files — count files in a directory
    sha256      — compute SHA-256 hash of a string
"""
from mcp.server.fastmcp import FastMCP
import os
import hashlib
from datetime import datetime, timezone

mcp = FastMCP("mini-agent-tools")


@mcp.tool()
def add(a: float, b: float) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)


@mcp.tool()
def multiply(a: float, b: float) -> str:
    """Multiply two numbers together and return the result."""
    return str(a * b)


@mcp.tool()
def now() -> str:
    """Get the current date and time in ISO 8601 format (UTC)."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def count_files(path: str) -> str:
    """Count how many files (non-directory) are in a given directory path."""
    try:
        count = sum(1 for _ in os.scandir(path) if _.is_file())
        return str(count)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def sha256(text: str) -> str:
    """Compute the SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode()).hexdigest()


if __name__ == "__main__":
    mcp.run(transport="stdio")
