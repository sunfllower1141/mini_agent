#!/usr/bin/env python3
"""Test git subprocess in a daemon thread -- reproduce the hang."""
import threading, time, subprocess, os, platform, sys

def run_in_thread():
    sys.stderr.write('[thread] starting git status...\n')
    sys.stderr.flush()
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GCM_INTERACTIVE'] = 'Never'
    kwargs = dict(cwd='.', capture_output=True, text=True, timeout=10, env=env)
    if platform.system() == 'Windows':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run(['git', 'status', '--short'], **kwargs)
    sys.stderr.write(f'[thread] done: rc={r.returncode} out={repr(r.stdout[:50])}\n')
    sys.stderr.flush()

sys.stderr.write('[main] launching daemon thread...\n')
sys.stderr.flush()
t = threading.Thread(target=run_in_thread, daemon=True)
t.start()
t.join(timeout=15)
if t.is_alive():
    sys.stderr.write('[main] THREAD STILL ALIVE after 15s - HANG CONFIRMED!\n')
else:
    sys.stderr.write('[main] thread completed OK\n')
sys.stderr.flush()
