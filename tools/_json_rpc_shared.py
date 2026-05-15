"""Shared JSON-RPC subprocess utilities.

Extracted from mcp_client.py and lsp.py which had nearly identical
_drain_stderr and is_connected implementations.
"""

from __future__ import annotations

import subprocess
import threading


def drain_stderr(
    process: subprocess.Popen | None,
    thread_name: str = "jsonrpc-stderr",
) -> threading.Thread | None:
    """Start a daemon thread to drain stderr from *process*.

    Prevents pipe-buffer deadlock when the subprocess writes to stderr
    but nobody reads it.  The thread runs until EOF on the pipe.
    """
    if process is None or process.stderr is None:
        return None
    thread = threading.Thread(
        target=_drain,
        args=(process.stderr,),
        daemon=True,
        name=thread_name,
    )
    thread.start()
    return thread


def _drain(stderr_stream) -> None:
    """Read and discard lines from *stderr_stream* until EOF."""
    try:
        for _line in stderr_stream:
            pass
    except (OSError, ValueError):
        pass


def is_subprocess_connected(process: subprocess.Popen | None) -> bool:
    """Check if a subprocess is alive (poll returns None)."""
    return process is not None and process.poll() is None
