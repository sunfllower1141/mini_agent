#!/usr/bin/env python3
"""Reproduce the exact execute_tool path with cancel_event."""
import threading, time, sys
from tools import execute_tool
from core.safety import ReadSafetyGate, WriteSafetyGate

rg = ReadSafetyGate('.')
wg = WriteSafetyGate('.', allow_overwrites=True)

tc = {'function': {'name': 'git', 'arguments': '{"subcommand": "status"}'}}

cancel_event = threading.Event()

def run():
    sys.stderr.write('[thread] starting execute_tool...\n')
    sys.stderr.flush()
    t0 = time.monotonic()
    r = execute_tool(tc, wg, rg, cancel_event=cancel_event)
    elapsed = time.monotonic() - t0
    sys.stderr.write(f'[thread] done: {elapsed:.2f}s ok={r.success} content={r.content[:100]}\n')
    sys.stderr.flush()

sys.stderr.write('[main] launching daemon thread...\n')
sys.stderr.flush()
t = threading.Thread(target=run, daemon=True)
t.start()

# Poll like the agent loop does
deadline = time.monotonic() + 30
while t.is_alive() and time.monotonic() < deadline:
    if cancel_event.is_set():
        sys.stderr.write('[main] cancelled!\n')
        sys.stderr.flush()
        break
    t.join(timeout=0.1)

if t.is_alive():
    sys.stderr.write('[main] THREAD STILL ALIVE - HANG!\n')
else:
    sys.stderr.write('[main] thread completed normally\n')
sys.stderr.flush()
