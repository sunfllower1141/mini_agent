#!/usr/bin/env python3
"""Test parallel git + list_directory execution."""
import concurrent.futures, time, sys
from tools import execute_tool
from core.safety import ReadSafetyGate, WriteSafetyGate

rg = ReadSafetyGate('.')
wg = WriteSafetyGate('.', allow_overwrites=True)

tc_git = {'function': {'name': 'git', 'arguments': '{"subcommand": "status"}'}}
tc_ls = {'function': {'name': 'list_directory', 'arguments': '{"path": "."}'}}

def run(tc, label):
    sys.stderr.write(f'[{label}] starting...\n')
    sys.stderr.flush()
    t0 = time.monotonic()
    r = execute_tool(tc, wg, rg)
    elapsed = time.monotonic() - t0
    sys.stderr.write(f'[{label}] {elapsed:.2f}s ok={r.success}\n')
    sys.stderr.flush()
    return r

sys.stderr.write('[main] starting ThreadPoolExecutor...\n')
sys.stderr.flush()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    f1 = pool.submit(run, tc_git, 'git')
    f2 = pool.submit(run, tc_ls, 'list_dir')
    r1 = f1.result(timeout=30)
    r2 = f2.result(timeout=30)
sys.stderr.write('[main] Both done\n')
sys.stderr.flush()
