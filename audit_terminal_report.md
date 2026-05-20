# terminal.py Standards Audit

## Findings
| Severity | File | Line | Issue | Fix |
|----------|------|------|-------|-----|
| MEDIUM | terminal.py | 33 | Magic numbers `-11`, `-12` for Windows console handles. Documented in comment but not named constants. | Define `_STD_OUTPUT_HANDLE = -11`, `_STD_ERROR_HANDLE = -12` |
| MEDIUM | terminal.py | 42 | Magic numbers `0x0004` (ENABLE_VIRTUAL_TERMINAL_PROCESSING) and `0x0001` (ENABLE_PROCESSED_OUTPUT). Commented but not named. | Define `_ENABLE_VIRTUAL_TERMINAL = 0x0004`, `_ENABLE_PROCESSED_OUTPUT = 0x0001` |
| MEDIUM | terminal.py | 35 | Magic numbers `0` and `-1` for invalid handle checks. Semantics unclear without constants. | Define `_INVALID_HANDLE_VALUE = -1` and document `0` as `NULL_HANDLE` |
| LOW | terminal.py | 44-45 | `except Exception: pass` silently swallows all errors. Masking real import/API failures. | Catch specific exceptions (`except (OSError, AttributeError): pass`) or at minimum log the failure |
| LOW | terminal.py | 16, 25 | `_WINDOWS_ANSI_ENABLED` is global mutable state. Mutated once at import time (line 47). | Acceptable (one-time init flag, guarded by `global`), but document the lifecycle explicitly |
| INFO | terminal.py | 47 | `_enable_windows_ansi()` called at module import — side effect. | By design; documented in module docstring. No change needed. |

## Criteria Passed

| Criterion | Status |
|-----------|--------|
| `from __future__ import annotations` (line 11) | ✅ PASS |
| All public functions have type hints (`c`, `format_table`) | ✅ PASS |
| No circular imports (imports only `os`, `sys`) | ✅ PASS |
| Tool results as structured dataclasses | ✅ N/A (no tool results in this module) |
| Control flow explicit (no decorator magic, metaclasses, etc.) | ✅ PASS |

## Details

### Magic Numbers (3 violations)
The Windows console API constants are documented in inline comments but never extracted to named module-level constants. The project rules require: *"No magic numbers; use named constants."* Lines 33, 35, and 42 all use unnamed integer literals whose meaning requires reading the adjacent comment.

### Silent Exception Swallowing (1 violation)
Line 44-45 catches the broadest possible `Exception` and silently discards it. This hides genuine failures (e.g. `ctypes` missing entirely on Windows ARM, or permissions issues). At minimum, the failure should be logged; ideally the `except` clause should be narrowed to the specific exceptions the Windows API can raise.

### Global Mutable State (1 borderline)
`_WINDOWS_ANSI_ENABLED` is technically global mutable state, but it's set exactly once at import time and used as a guard to prevent double-initialization. This is the *"unless unavoidable"* exception the rules permit. No action required.
