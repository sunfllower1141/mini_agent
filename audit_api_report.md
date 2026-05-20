# Audit Report: api.py Standards Compliance

## Findings
| Severity | File | Line | Issue | Fix |
|----------|------|------|-------|-----|
| HIGH | api.py | 73 | **Global mutable state** — `_clean_messages_cache: dict[...] = {}` is mutated in-place by `call_llm` (line 229) and `clear_api_cache` (line 269). Violates "No global mutable state unless unavoidable." | Encapsulate in a class with instance state, or use `threading.local()` to at least isolate per-thread, or pass cache explicitly as a parameter. |
| HIGH | api.py | 35–44 | **Raw exception as error mechanism** — `APIError(Exception)` is raised directly (line 255) rather than using a structured dataclass result. Violates "All tool results must be structured dataclasses — never raw exceptions." While `call_llm` is not a tool per se, it is called by `llm.py` (the tool-execution orchestrator) and its error path propagates raw exceptions upward. | Return a structured result (e.g. a `CallLLMResult` dataclass with `success`, `content`, and `error` fields) instead of raising. |
| MEDIUM | api.py | 33, 115 | **Magic number 300** — used as default `max_len` in `truncate_content` (line 51), `format_tool_detail` (line 58), and as the simple-prompt length threshold in `_compute_complexity` (line 115). Three independent uses of the same literal with different semantics. | Define `_DEFAULT_TRUNCATION_LENGTH = 300` and `_SIMPLE_PROMPT_MAX_CHARS = 300` as module-level constants. |
| MEDIUM | api.py | 113 | **Magic number 2000** — used in `_compute_complexity` to cap user-text scanning (`len(user_text) > 2000`). | Define `_COMPLEXITY_TEXT_LIMIT = 2000`. |
| LOW | api.py | 208, 229 | **Magical cache-key via `id(messages)`** — the incremental cleaning cache uses Python object identity (`id(messages)`) as a dict key. This is fragile: if the caller passes a different list object containing the same messages, the cache misses silently. It also relies on the undocumented invariant that the same list object is reused across calls. | Use an explicit cache key (e.g. a tuple of message hashes or a caller-provided session ID) instead of object identity. |
| LOW | api.py | 240 | **Magic string `"mini_agent/1.0"`** — the User-Agent header embeds a version literal. Minor — arguably a constant, not a number, but still hardcoded. | Define `_USER_AGENT = "mini_agent/1.0"`. |

## Items in Compliance
| Check | Status | Details |
|-------|--------|---------|
| `from __future__ import annotations` | ✅ PASS | Line 14 |
| All public functions have type hints | ✅ PASS | `call_llm` (line 179), `truncate_content` (line 51), `format_tool_detail` (line 58), `clear_api_cache` (line 267) — all annotated. |
| No circular imports | ✅ PASS | `api.py` imports from `config`, `retry`, `stream`, `tools.schema` — none of those import back from `api.py` (verified via search). |
| Structured dataclass results (for ToolResult returned/used) | ✅ PASS | `ToolResult` (`tools/__init__.py:80`) is a proper `@dataclass`. `call_llm` itself returns `dict | None` but those are message dicts from the LLM, not tool results. |
| Control flow mostly explicit | ✅ PASS | Stream vs. non-stream dispatch, provider switching, model routing — all straightforward `if/elif/else`. The only "magical" element is the identity-based cache key noted above. |

## Summary
- **6 violations found**: 2 HIGH (global mutable state, raw exception), 2 MEDIUM (magic numbers), 2 LOW (identity-based cache key, hardcoded version string).
- The file is otherwise well-structured with proper type annotations, no circular imports, and clear control flow.
- The global mutable cache (`_clean_messages_cache`) is the most architecturally significant issue — it's a performance optimization that could be refactored into an instance variable or thread-local storage.
