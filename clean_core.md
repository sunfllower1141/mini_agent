# Core Module Code Review

Review of `prompt.py`, `config.py`, `safety.py`, `retry.py`, `terminal.py`.

---

## 1. Naming Conventions ✅ Mostly Clean

Generally clear names. A few inconsistencies:

- **`terminal.py:30-33`** — `DIM` is public uppercase; `_RESET`, `_RED`, `_GREEN`, `_YELLOW`, `_CYAN` are private uppercase with leading underscore. Inconsistent visibility convention. Pick one style.
- **`config.py:242`** — `r = _sp.run(...)` single-letter var; acceptable for local scope but borderline.
- **`retry.py:50`** — `post = session.post ...` — reusable alias, fine.
- **`terminal.py:35`** — `c(text, code)` — terse but acceptable for a colour wrapper.

## 2. Function Length ⚠️ 2 Over-80-Line Candidates

- **`config.py:init_session()` (~70 lines)** — Does: config loading, gate creation, symbol index, MCP init, memory load/prune, session setup, requests.Session config, atexit. The TODO at line 290 is about `build_startup_context`, but `init_session` itself is the real candidate for splitting (e.g. extract MCP init, memory pruning, and session creation into helpers).
- **`config.py:build_startup_context()` (~70 lines)** — Self-TODO on line 290 confirms this. Tree generation, STATE.txt tailing, and git log are three distinct concerns that could be separate helpers.

## 3. Magic Numbers ✅ None Found

All numeric constants are named (e.g. `_MAX_RETRIES=3`, `HTTP_READ_TIMEOUT=120`, `TREE_TRUNCATION_LINES=60`). One minor note in **`retry.py:30`**: the `2` and `0.5` in `(2 ** attempt) * (0.5 + random.random())` are formula constants — could be `BACKOFF_BASE=2` and `BACKOFF_JITTER=0.5` for readability, but not egregious.

## 4. Docstring Accuracy ⚠️ Minor Issues

- **`retry.py` docstring** — says "Non-retryable errors raise immediately". This is misleading: `RequestException` errors are retried up to `_MAX_RETRIES` times and only re-raised on exhaustion. Only non-retryable *status codes* (e.g. 400) are returned immediately. Wording could be more precise.
- **`config.py:build_startup_context()`** — says "Saves the agent discovery turns" which is informal but accurate.
- All other docstrings match actual behaviour.

## 5. Error Handling ⚠️ 3 Notable Issues

### Bare `except Exception` / Silent Swallowing

- **`config.py:237`** — `_load_dotenv()` swallows `OSError` with bare `pass`. While intentional (env file is optional), this hides permission errors. Could log at debug level.
- **`config.py:224`** — `_load_toml_from_workspace()` catches `Exception as exc` — too broad. Should be `OSError | tomllib.TOMLDecodeError`.
- **`config.py:380, 386`** — `build_startup_context()` catches `Exception: pass` on both STATE.txt read and git log. Silent failure means a corrupt STATE.txt or git error goes unnoticed. Acceptable for a "best-effort" startup context, but a `print(..., file=sys.stderr)` on failure would help debugging.
- **`config.py:494`** — MCP init catches `Exception as exc` — too broad. Should catch specific MCP errors.
- **`prompt.py:32`** — `open(rules_path)` with no try/except. If `.mini_agent.rules` exists but is unreadable (permissions), this crashes the session start.

## 6. Type Hints ✅ Good, With 2 Vague Returns

- **`config.py:switch_session() -> dict`** — Vague. Should be `dict[str, Any]` or a typed dict.
- **`config.py:init_session() -> dict`** — Same issue. The return shape (config, write_gate, read_gate, memory, messages, session) is well-documented but not enforced by type system.
- **`config.py:parse_args() -> object`** — Return type `object` is too generic. Should be `argparse.Namespace`.
- **`retry.py:37`** — `session` parameter is untyped. Should be `requests.Session | types.ModuleType`.
- Everything else has type hints on public functions. Good.

## 7. Imports ✅ Organized, No Circular Risk

- **Circular import handling**: `config.py` uses deferred imports inside `switch_session()` and `init_session()` for `MemoryStore`, `build_system_prompt`, etc. — standard pattern. No circular risk.
- **`config.py:330`** — `import subprocess as _sp` inside `build_startup_context()` is a local import. This hides a dependency but avoids top-level cost. Consider moving to top-level with `import subprocess as _sp` at module scope if it's called once per startup anyway.
- **`config.py:518`** — `import functools`, `import atexit` inside `init_session()`. Same pattern. Minor consistency nit: some helpers import at top, some inc-lined.
- No unused imports found in any module.

---

## Summary

| Area | Grade |
|---|---|
| Naming | ✅ Good (minor inconsistency in terminal.py) |
| Function Length | ⚠️ 2 functions near/over 70 lines, good candidates to split |
| Magic Numbers | ✅ None |
| Docstring Accuracy | ⚠️ retry.py wording slightly misleading |
| Error Handling | ⚠️ 2 `except Exception` too broad, 1 missing try/except in prompt.py |
| Type Hints | ✅ Good, 2 vague `-> dict` returns |
| Imports | ✅ Clean, no circular risk |

### Top 3 Actions

1. **`config.py:init_session()`** — split into ~3 helpers: MCP setup, memory pruning, session creation.
2. **`prompt.py:32`** — wrap rules file read in try/except for permission errors.
3. **`config.py:224, 494`** — narrow `except Exception` to specific exception types.
