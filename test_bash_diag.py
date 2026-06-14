"""Test if bash subprocess hangs with stdin=DEVNULL on Windows."""
import subprocess
import sys
import time

BASH = r"C:\Program Files\Git\bin\bash.exe"

def test(label, cmd, stdin_mode, timeout=5):
    print(f"[{label}] bash -c {cmd!r} stdin={stdin_mode} ...", flush=True)
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            [BASH, "-c", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **({stdin_mode: subprocess.DEVNULL} if stdin_mode == "DEVNULL" else 
               {stdin_mode: subprocess.PIPE} if stdin_mode == "PIPE" else {}),
        )
        input_data = "" if stdin_mode == "PIPE" else None
        out, err = proc.communicate(input=input_data, timeout=timeout)
        dt = time.time() - t0
        status = "OK" if proc.returncode == 0 else f"rc={proc.returncode}"
        print(f"  {status} out={out!r} err={err!r} ({dt:.1f}s)", flush=True)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        dt = time.time() - t0
        print(f"  TIMEOUT after {dt:.1f}s", flush=True)
        return False
    except Exception as e:
        dt = time.time() - t0
        print(f"  ERROR: {e} ({dt:.1f}s)", flush=True)
        return False

print("=== Windows Bash Subprocess Diagnostics ===", flush=True)
print(f"Bash path: {BASH}", flush=True)

tests = [
    ("echo_only", "echo hello", "DEVNULL"),
    ("echo_stderr", "echo hello && echo world >&2", "DEVNULL"),
    ("ls_cwd", "ls -la 2>&1 | head -5", "DEVNULL"),
    ("sleep_1", "sleep 1 && echo done", "DEVNULL"),
    ("cat_devnull", "cat < /dev/null", "DEVNULL"),
    ("cat_noredir", "cat", "DEVNULL"),
    ("python_print", 'python3 -c "print(42)"', "DEVNULL"),
    ("exit_0", "exit 0", "DEVNULL"),
]

results = []
for label, cmd, stdin_mode in tests:
    ok = test(label, cmd, stdin_mode)
    results.append((label, ok))

print()
print("=== Summary ===", flush=True)
for label, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}: {label}", flush=True)
print("DONE", flush=True)
