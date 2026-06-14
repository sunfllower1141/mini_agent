#!/usr/bin/env python3
"""Test execute_tool with git -- reproduce the hang."""
import sys, time
sys.stderr.write('[start] importing...\n')
sys.stderr.flush()

from tools import execute_tool
from core.safety import ReadSafetyGate, WriteSafetyGate

sys.stderr.write('[start] setting up gates...\n')
sys.stderr.flush()

rg = ReadSafetyGate('.')
wg = WriteSafetyGate('.', allow_overwrites=True)

tool_call = {
    'function': {
        'name': 'git',
        'arguments': '{"subcommand": "status"}'
    }
}

sys.stderr.write('[start] calling execute_tool...\n')
sys.stderr.flush()

t0 = time.monotonic()
result = execute_tool(tool_call, wg, rg)
elapsed = time.monotonic() - t0

sys.stderr.write(f'[done] ({elapsed:.2f}s) success={result.success}\n')
sys.stderr.write(f'[done] content={result.content[:200]}\n')
sys.stderr.flush()
