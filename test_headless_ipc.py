"""Tests for headless_ipc.py Ã¢ the JSON-line IPC layer."""
from __future__ import annotations

import io
import json
import queue
import threading
import time

import pytest

from headless_ipc import (
    StdoutEmitter, StdinReader, Command, make_callbacks,
    EVT_READY, EVT_STREAM_TOKEN, EVT_TOOL_START, EVT_TOOL_END, EVT_TOOL_OUTPUT,
)


# ---------------------------------------------------------------------------
# StdoutEmitter
# ---------------------------------------------------------------------------

class _CollectingStream:
    """In-memory stream that records every write/flush call.  Thread-safe."""
    def __init__(self) -> None:
        self.buf: list[str] = []
        self.flushes = 0
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self.buf.append(s)
        return len(s)

    def flush(self) -> None:
        with self._lock:
            self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.buf)


def _lines(stream: _CollectingStream) -> list[dict]:
    return [json.loads(l) for l in stream.text.splitlines() if l.strip()]


def test_emit_writes_one_json_line_with_required_keys():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    em.emit(EVT_READY, {"model": "x", "workspace": "/tmp"})
    msgs = _lines(s)
    assert len(msgs) == 1
    assert msgs[0]["type"] == EVT_READY
    assert msgs[0]["data"] == {"model": "x", "workspace": "/tmp"}
    assert isinstance(msgs[0]["ts"], float)


def test_emit_defaults_empty_data_dict():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    em.emit("custom")
    assert _lines(s)[0]["data"] == {}


def test_emit_flushes_after_every_line():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    em.emit("a")
    em.emit("b")
    em.emit("c")
    assert s.flushes == 3


def test_emit_each_line_is_newline_terminated_and_single_line():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    em.emit("tok", {"token": "hello\nworld"})   # data with embedded newline
    lines = s.text.splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["data"]["token"] == "hello\nworld"  # newline survives in payload


def test_emit_is_thread_safe_no_interleaving():
    """100 threads each emit 50 events; every output line must parse as JSON."""
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)

    def worker(tid: int) -> None:
        for i in range(50):
            em.emit("tok", {"tid": tid, "i": i, "pad": "x" * 200})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()

    msgs = _lines(s)
    assert len(msgs) == 100 * 50
    # All parse cleanly Ã¢ no interleaved partial writes.
    seen = {(m["data"]["tid"], m["data"]["i"]) for m in msgs}
    assert len(seen) == 100 * 50


def test_emit_silently_drops_after_close():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    em.emit("a")
    em.close()
    em.emit("b")
    assert [m["type"] for m in _lines(s)] == ["a"]


def test_emit_handles_broken_pipe_gracefully():
    class BrokenStream:
        def write(self, _): raise BrokenPipeError("UI gone")
        def flush(self): pass
    em = StdoutEmitter(stream=BrokenStream())
    em.emit("a")
    em.emit("b")   # must not raise; emitter should have self-closed


# ---------------------------------------------------------------------------
# StdinReader
# ---------------------------------------------------------------------------

def test_reader_parses_lines_into_commands():
    stream = io.StringIO(
        '{"type":"user.message","data":{"text":"hi"}}\n'
        '{"type":"user.cancel"}\n'
    )
    q: queue.Queue = queue.Queue()
    r = StdinReader(stream=stream, out=q)
    r.start(); r.join(timeout=2)

    a = q.get(timeout=1); b = q.get(timeout=1); end = q.get(timeout=1)
    assert isinstance(a, Command) and a.type == "user.message"
    assert a.data == {"text": "hi"}
    assert isinstance(b, Command) and b.type == "user.cancel"
    assert b.data == {}
    assert end is None     # EOF sentinel


def test_reader_skips_malformed_json_but_keeps_reading():
    stream = io.StringIO(
        '{not json\n'
        '{"type":"ok"}\n'
    )
    errors: list[str] = []
    q: queue.Queue = queue.Queue()
    r = StdinReader(stream=stream, out=q, on_error=errors.append)
    r.start(); r.join(timeout=2)

    cmd = q.get(timeout=1)
    assert cmd.type == "ok"
    assert q.get(timeout=1) is None
    assert len(errors) == 1
    assert "bad JSON" in errors[0]


def test_reader_skips_non_object_messages():
    stream = io.StringIO('[1,2,3]\n"hello"\n{"type":"ok"}\n')
    errors: list[str] = []
    q: queue.Queue = queue.Queue()
    r = StdinReader(stream=stream, out=q, on_error=errors.append)
    r.start(); r.join(timeout=2)

    cmd = q.get(timeout=1)
    assert cmd.type == "ok"
    assert q.get(timeout=1) is None
    assert len(errors) == 2


def test_reader_ignores_blank_lines():
    stream = io.StringIO('\n\n  \n{"type":"x"}\n\n')
    q: queue.Queue = queue.Queue()
    r = StdinReader(stream=stream, out=q)
    r.start(); r.join(timeout=2)

    cmd = q.get(timeout=1)
    assert cmd.type == "x"
    assert q.get(timeout=1) is None


def test_reader_normalises_non_dict_data_to_empty():
    stream = io.StringIO('{"type":"x","data":"not-a-dict"}\n')
    q: queue.Queue = queue.Queue()
    r = StdinReader(stream=stream, out=q)
    r.start(); r.join(timeout=2)

    cmd = q.get(timeout=1)
    assert cmd.type == "x" and cmd.data == {}


# ---------------------------------------------------------------------------
# make_callbacks Ã¢ wiring layer
# ---------------------------------------------------------------------------

def test_callbacks_emit_expected_event_types():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    cb = make_callbacks(em, agent_id="orchestrator")

    cb["on_token"]("hello")
    cb["on_tool_start"]("read_file(...)", parallel=False)
    cb["on_tool_output"]("line of output")
    cb["on_tool_end"](True, "ok", diff_preview=None)

    types = [m["type"] for m in _lines(s)]
    assert types == [EVT_STREAM_TOKEN, EVT_TOOL_START, EVT_TOOL_OUTPUT, EVT_TOOL_END]


def test_callbacks_pair_tool_start_end_with_seq():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    cb = make_callbacks(em, agent_id="a1")

    cb["on_tool_start"]("t1")
    cb["on_tool_end"](True, "ok")
    cb["on_tool_start"]("t2")
    cb["on_tool_output"]("partial")
    cb["on_tool_end"](False, "fail")

    msgs = _lines(s)
    starts = [m for m in msgs if m["type"] == EVT_TOOL_START]
    ends   = [m for m in msgs if m["type"] == EVT_TOOL_END]
    outs   = [m for m in msgs if m["type"] == EVT_TOOL_OUTPUT]
    assert [m["data"]["seq"] for m in starts] == [1, 2]
    assert [m["data"]["seq"] for m in ends]   == [1, 2]
    # The pending output belongs to seq=2 (the active tool when it was emitted)
    assert outs[0]["data"]["seq"] == 2


def test_callbacks_include_agent_id_in_every_event():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    cb = make_callbacks(em, agent_id="sub-7")

    cb["on_token"]("x")
    cb["on_tool_start"]("t")
    cb["on_tool_output"]("o")
    cb["on_tool_end"](True, "ok")

    for m in _lines(s):
        assert m["data"]["agent_id"] == "sub-7"


def test_callbacks_tool_end_carries_diff_preview():
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    cb = make_callbacks(em, agent_id="o")

    cb["on_tool_start"]("edit_file(...)")
    cb["on_tool_end"](True, "ok", diff_preview="-old\n+new\n")

    end = [m for m in _lines(s) if m["type"] == EVT_TOOL_END][0]
    assert end["data"]["diff_preview"] == "-old\n+new\n"
    assert end["data"]["ok"] is True


def test_callbacks_thread_safe_under_concurrent_streaming():
    """Many threads spamming on_token while another runs tool start/end."""
    s = _CollectingStream()
    em = StdoutEmitter(stream=s)
    cb = make_callbacks(em, agent_id="o")

    stop = threading.Event()
    def tokens():
        while not stop.is_set():
            cb["on_token"]("t")
    def tools():
        for _ in range(20):
            cb["on_tool_start"]("op")
            cb["on_tool_end"](True, "ok")

    ts = [threading.Thread(target=tokens) for _ in range(4)]
    for t in ts: t.start()
    tools_thread = threading.Thread(target=tools)
    tools_thread.start()
    tools_thread.join(timeout=5)
    stop.set()
    for t in ts: t.join(timeout=2)

    # Every line parses; tool_start and tool_end counts agree (20 each).
    msgs = _lines(s)
    n_start = sum(1 for m in msgs if m["type"] == EVT_TOOL_START)
    n_end   = sum(1 for m in msgs if m["type"] == EVT_TOOL_END)
    assert n_start == 20 and n_end == 20
