"""Diagnose why Popen with Git Bash hangs."""
import subprocess
import sys
import time

BASH = r"C:\Program Files\Git\bin\bash.exe"

print("=== Test 1: shell=True (cmd.exe wrapping bash) ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        'echo hello_from_cmd', shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out, err = proc.communicate(timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print("=== Test 2: bash.exe -c with stdin=PIPE (no input) ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        [BASH, "-c", "echo hello_from_bash"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.PIPE,
    )
    out, err = proc.communicate(input='', timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print("=== Test 3: bash.exe -c with stdin=DEVNULL ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        [BASH, "-c", "echo hello_from_bash_devnull"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL,
    )
    out, err = proc.communicate(timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print("=== Test 4: bash.exe -c with stdin=None (inherit) ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        [BASH, "-c", "echo hello_from_bash_inherit"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=None,
    )
    out, err = proc.communicate(timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print("=== Test 5: cmd.exe /c (shell=True) ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        'cmd.exe /c "echo hello_from_cmd_exe"', shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out, err = proc.communicate(timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print("=== Test 6: Direct python subprocess (no bash) ===")
t0 = time.time()
try:
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('hello_from_python')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out, err = proc.communicate(timeout=5)
    print(f"  rc={proc.returncode} out={out!r} err={err!r}  ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"  FAIL: {e}  ({time.time()-t0:.1f}s)")

print()
print("DONE")
