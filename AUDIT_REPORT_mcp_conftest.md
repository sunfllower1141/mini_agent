# Audit Report: mcp_server.py & conftest.py

**Audited files:**
- `/Users/gabrielmalone/Desktop/mini_agent/mcp_server.py` (54 lines)
- `/Users/gabrielmalone/Desktop/mini_agent/conftest.py` (46 lines)

**Scope:** Bugs, logic errors, race conditions, error handling, code smells, architectural issues. Security excluded.

---

## Findings

| Severity | File | Line(s) | Issue | Fix |
|----------|------|---------|-------|-----|
| **BUG** | mcp_server.py | 43 | `except Exception as e` is overly broad. Catches `KeyboardInterrupt`, `SystemExit`, `MemoryError`, etc. — none of which should be swallowed here. Only `OSError` (and subclasses like `FileNotFoundError`, `PermissionError`) can be raised by `os.scandir`. | Replace with `except OSError as e:`. |
| **BUG** | mcp_server.py | 53-54 | `mcp.run(transport="stdio")` has no try/except. If stdio is unavailable (e.g., running in a non-TTY environment, or already closed), this will crash with an unhelpful traceback. | Wrap `mcp.run()` in a try/except that logs and exits cleanly. |
| **SMELL** | mcp_server.py | 41 | Loop variable `_` is used (`.is_file()`). Python convention: `_` signals "unused/discard." Calling methods on `_` is misleading to readers. | Rename to `entry` or `dirent`. |
| **SMELL** | mcp_server.py | 40-44 | `count_files` returns errors as strings (`"Error: ..."`) indistinguishable from valid results (`"0"`). A caller cannot tell "directory is empty" from "directory does not exist" without string-parsing. | Return structured error via MCP error mechanism, or raise an exception that FastMCP surfaces. |
| **SMELL** | mcp_server.py | 22, 28 | `str(a + b)` — float-to-string conversion has no format control. Can produce inconsistent precision across Python versions (e.g., `"0.30000000000000004"`). | Use `f"{a + b:.10g}"` or an explicit format. |
| **SMELL** | mcp_server.py | 48-50 | `sha256` uses `text.encode()` (default UTF-8) with no error handling. If `text` contains lone surrogates, `UnicodeEncodeError` propagates uncaught — inconsistent with `count_files` which does catch errors. | Either wrap in try/except or use `text.encode("utf-8", errors="replace")`. |
| **SMELL** | mcp_server.py | 11 | `from mcp.server.fastmcp import FastMCP` — no import guard. If `mcp` package is not installed, the module fails at import time with a raw traceback. | Wrap in try/except with a clear error message, or defer import to `__main__` guard. |
| **SMELL** | conftest.py | 30 | `config.getoption("--run-benchmarks", default=False)` — the `default` kwarg is redundant since `pytest_addoption` (line 12-17) already sets `default=False`. | Remove the `default=False` argument. |
| **SMELL** | conftest.py | 42 | `"test_benchmarks" in item.nodeid` — substring match on the full nodeid string. Would incorrectly match tests like `test_something.py::test_benchmarks_comparison` or any path containing "test_benchmarks" as a substring. | Check `item.fspath` or `item.location[0]` (the file path) instead: `item.fspath.parts[-1] == "test_benchmarks.py"` or `os.path.basename(item.location[0]) == "test_benchmarks.py"`. |
| **NOTE** | mcp_server.py | 19-50 | All five tools return `str`, creating an opaque API. Callers must string-parse both data and errors. FastMCP supports raising exceptions for errors — this would give structured error signaling. | Use exceptions for errors, return typed data for success. |
| **NOTE** | conftest.py | 20-34 | `pytest_ignore_collect` has no corresponding test. If this filter breaks (e.g., due to a pytest upgrade changing the hook signature), it won't be caught by CI. | Add a test that verifies benchmark files are excluded by default and included with `--run-benchmarks`. |

---

## Summary by Severity

| Category | Count |
|----------|-------|
| BUG | 2 |
| SMELL | 7 |
| NOTE | 2 |
| **Total** | **11** |

---

## Detailed Analysis

### mcp_server.py

**Architecture:** A dead-simple MCP server exposing five utility tools via `FastMCP` over stdio. No shared state, no concurrency concerns. The design is intentionally minimal, which is appropriate for its role.

**Error handling is the primary concern.** Only `count_files` catches exceptions, and it catches too broadly. The other four tools (`add`, `multiply`, `now`, `sha256`) let exceptions propagate raw. This inconsistency means:
- `count_files("/nonexistent")` → returns `"Error: [Errno 2] ..."` (string)
- `sha256("\ud800")` → crashes the MCP connection with `UnicodeEncodeError` (exception)

**String-return API:** Every tool returns `str`. While functional, this blurs the line between data and errors. FastMCP supports raising exceptions that get serialized as structured MCP errors — using that would let callers distinguish "result" from "error" programmatically.

**Import-time coupling:** The `from mcp.server.fastmcp import FastMCP` at module top means any importer of this file (e.g., test discovery, documentation tools) must have the `mcp` package installed. This is a minor nuisance because the file is only useful as a standalone MCP server — delaying the import to the `__main__` guard would be kinder.

### conftest.py

**Architecture:** Standard pytest root-level conftest with two hooks: collection filtering (`pytest_ignore_collect`) and item ordering (`pytest_collection_modifyitems`). Clean separation — one decides what to skip, the other orders what remains.

**The venv check (line 27) is actually correct** — `parts` is a tuple from `pathlib.Path.parts`, so `"venv" in parts` checks for exact equality of a path component, not substring. This was initially flagged in analysis but is fine on closer inspection.

**The benchmark identification (line 42) is imprecise.** Using `"test_benchmarks" in item.nodeid` is a substring match on the full node ID string, which could match non-benchmark tests. In practice this is unlikely to cause problems (no other test file is named similarly), but it's fragile.

**No tests for the conftest itself.** These hooks control which tests run and in what order — if they break silently, benchmarks could be skipped (false negative) or run out of order (flaky CI). A parametrized test verifying collection behavior would be valuable.
