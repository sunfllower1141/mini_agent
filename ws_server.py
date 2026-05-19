#!/usr/bin/env python3
"""
ws_server.py — WebSocket event server for the mini_agent Electron UI.

Pushes real-time agent activity (tool calls, stream tokens, agent positions)
to connected Electron frontends. Receives user interactions (node clicks, chat
messages) and routes them back to the agent loop.

Architecture:
    — asyncio event loop runs in a background daemon thread
    — emit() is thread-safe via asyncio.run_coroutine_threadsafe()
    — Single writer, multiple readers pattern (one agent, many UI clients)

Protocol (Python → UI):
    graph.init         Full workspace tree snapshot
    tool.start         Tool call started {name, args, agent_id, file_path}
    tool.result        Tool call finished {name, success, summary, agent_id, file_path}
    stream.token       LLM output token {token, agent_id}
    stream.thinking    LLM thinking token {token, agent_id}
    agent.position     Agent's open files {agent_id, open_files: [path], color}
    agent.heartbeat    Periodic status ping

Protocol (UI → Python):
    ui.click_node      User clicked a file/dir node {file_path}
    ui.inspect_node    User right-clicked inspect {file_path}
    ui.send_message    User chat input {text}
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

# ---------------------------------------------------------------------------
# Event emitter — thread-safe bridge between sync agent code and async WS
# ---------------------------------------------------------------------------

class EventEmitter:
    """Thread-safe publish/subscribe for WebSocket events.

    emit() can be called from any thread.  Events are serialized to JSON
    and pushed to all connected clients via the asyncio event loop.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocketServerProtocol] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Message handlers registered by the agent to process UI→Python messages
        self._ui_handlers: dict[str, Callable] = {}
        # Buffer for messages that arrive before the loop is running
        self._pending: list[str] = []

    # ------------------------------------------------------------------
    # Client management (called from asyncio thread)
    # ------------------------------------------------------------------

    async def add_client(self, ws: WebSocketServerProtocol) -> None:
        with self._lock:
            self._clients.add(ws)

    async def remove_client(self, ws: WebSocketServerProtocol) -> None:
        with self._lock:
            self._clients.discard(ws)

    # ------------------------------------------------------------------
    # emit — thread-safe, usable from synchronous agent code
    # ------------------------------------------------------------------

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Push an event to all connected WebSocket clients.

        Thread-safe: may be called from any thread.  If the asyncio loop
        is not yet running, the event is buffered and flushed on connect.
        """
        payload = json.dumps({
            "type": event_type,
            "data": data or {},
            "ts": time.time(),
        })
        loop = self._loop
        if loop is None or not loop.is_running():
            with self._lock:
                self._pending.append(payload)
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), loop)

    async def _broadcast(self, payload: str) -> None:
        """Send payload to all connected clients, removing dead ones."""
        with self._lock:
            clients = set(self._clients)
        for ws in clients:
            try:
                await ws.send(payload)
            except websockets.ConnectionClosed:
                with self._lock:
                    self._clients.discard(ws)

    # ------------------------------------------------------------------
    # UI message routing
    # ------------------------------------------------------------------

    def on(self, event_type: str, handler: Callable[[dict], Any]) -> None:
        """Register a handler for UI→Python messages (e.g. 'ui.click_node')."""
        self._ui_handlers[event_type] = handler

    async def _handle_message(self, message: str) -> None:
        """Dispatch an incoming UI message to registered handlers."""
        try:
            msg = json.loads(message)
            event_type = msg.get("type", "")
            data = msg.get("data", {})

            # Route ui.send_message and ui.click_node to the inbox queue
            if event_type in ("ui.send_message", "ui.click_node"):
                ui_inbox.put((event_type, data))
                return

            handler = self._ui_handlers.get(event_type)
            if handler:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, handler, data)
        except (json.JSONDecodeError, Exception):
            pass  # Ignore malformed client messages

    # ------------------------------------------------------------------
    # Event loop lifecycle
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio loop reference and flush pending events."""
        self._loop = loop
        # Flush buffered events
        with self._lock:
            pending = self._pending
            self._pending = []
        for payload in pending:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), loop)


# Singleton — one emitter shared across the agent process
emitter = EventEmitter()

# Queue for incoming UI messages that the agent main loop polls.
# Thread-safe: ws_server pushes, agent loop pops.
import queue
ui_inbox: queue.Queue = queue.Queue()


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

async def _handler(ws: WebSocketServerProtocol) -> None:
    """Per-connection handler.  Registers client, sends graph.init, relays UI→Python messages."""
    await emitter.add_client(ws)
    try:
        # Send graph.init to every new client (use buffered if available, else build fresh)
        with emitter._lock:
            if emitter._pending:
                for payload in emitter._pending:
                    try:
                        await ws.send(payload)
                    except websockets.ConnectionClosed:
                        break
        # Always send a fresh graph.init on connect
        try:
            import os as _os
            workspace = _os.environ.get("AGENT_WORKSPACE", _os.getcwd())
            tree = build_workspace_tree(workspace)
            await ws.send(json.dumps({
                "type": "graph.init",
                "data": tree,
                "ts": time.time(),
            }))
        except Exception:
            pass

        # Listen for UI→Python messages
        async for message in ws:
            await emitter._handle_message(message)
    except websockets.ConnectionClosed:
        pass
    finally:
        await emitter.remove_client(ws)


async def _start_server(host: str, port: int) -> None:
    """Start the WebSocket server (runs on the asyncio event loop)."""
    loop = asyncio.get_running_loop()
    emitter.set_loop(loop)
    async with websockets.serve(_handler, host, port):
        await asyncio.Future()  # run forever


def _run_loop(host: str, port: int) -> None:
    """Entry point for the background thread. Creates and runs the event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_start_server(host, port))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


def start(host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    """Start the WebSocket server in a background daemon thread.

    Returns the thread handle.  The server runs until the process exits.
    Call this once at agent startup.

    Example:
        >>> from ws_server import start, emitter
        >>> start()
        >>> emitter.emit("tool.start", {"name": "read_file", "file_path": "foo.py"})
    """
    thread = threading.Thread(
        target=_run_loop,
        args=(host, port),
        name="ws-server",
        daemon=True,
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Convenience: build and emit workspace tree
# ---------------------------------------------------------------------------

def build_workspace_tree(workspace_root: str, max_nodes: int = 5000) -> dict:
    """Walk the workspace and build a tree structure for graph.init.

    Filters hidden dirs, venvs, node_modules, .git, and caps at max_nodes.
    Returns: {nodes: [{id, label, type, parent?}], edges: [{source, target}]}
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    root = Path(workspace_root).resolve()
    root_id = str(root)

    # Directories to skip entirely
    skip_dirs = {
        ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
        "node_modules", "venv", ".venv", "env", ".env",
        ".mini_agent_backups", "reports", ".tox", ".eggs",
        "dist", "build", "*.egg-info",
    }

    # Walk the tree
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter dirnames in-place
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in skip_dirs
        ]

        dir_id = str(Path(dirpath).resolve())
        if dir_id == root_id:
            nodes.append({"id": dir_id, "label": root.name, "type": "directory"})
        else:
            nodes.append({"id": dir_id, "label": os.path.basename(dirpath), "type": "directory"})
            parent_id = str(Path(dirpath).resolve().parent)
            edges.append({"source": parent_id, "target": dir_id})

        for fname in filenames:
            if fname.startswith("."):
                continue
            # Skip large binary-ish files
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".pyd", ".exe", ".dll"):
                continue
            file_path = str((Path(dirpath) / fname).resolve())
            nodes.append({"id": file_path, "label": fname, "type": "file"})
            edges.append({"source": dir_id, "target": file_path})

        # Cap total nodes
        if len(nodes) >= max_nodes:
            break

    return {"nodes": nodes[:max_nodes], "edges": edges[:max_nodes * 2]}


def emit_graph_init(workspace_root: str) -> None:
    """Build the workspace tree and emit it as graph.init."""
    tree = build_workspace_tree(workspace_root)
    emitter.emit("graph.init", tree)

# Track open files per agent for position updates
_agent_open_files: dict[str, set[str]] = {}

def emit_agent_position(agent_id: str, file_path: str) -> None:
    """Emit the agent's current position (set of active files).

    Call this whenever the agent reads/writes a file.  The UI uses it to
    show a persistent pulsing glow on files the agent is actively working on.
    """
    if agent_id not in _agent_open_files:
        _agent_open_files[agent_id] = set()
    _agent_open_files[agent_id].add(file_path)
    # Limit to last 5 files per agent
    if len(_agent_open_files[agent_id]) > 5:
        _agent_open_files[agent_id] = set(list(_agent_open_files[agent_id])[-5:])
    emitter.emit("agent.position", {
        "agent_id": agent_id,
        "open_files": list(_agent_open_files[agent_id]),
    })
