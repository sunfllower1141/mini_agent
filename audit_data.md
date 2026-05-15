# Data Layer Audit Report

Audited files: `memory.py` (1173 lines), `safety.py` (273 lines), `tools/file_ops.py` (506 lines), `tools/schema.py` (994 lines), `tools/__init__.py` (533 lines).

---

## (a) Type Hints — Do all public functions have them?

### ✅ memory.py — ALL public functions fully type-annotated
All named constants (module-level), private helpers (`_get_tool_content`, `_estimate_tokens`, `_total_tokens`, `_find_tool_call_name`, `_compress_*`, `_summarize_pruned`, `_prune_by_tokens`, `_db_path`, `_row_to_msg`, `_clean_messages`, `_migrate_old_paths`, `_migrate_json`) and the `MemoryStore` class (all methods including `__init__`, `load`, `save`, `clear`, `get_scratchpad`, `set_scratchpad`, etc.) have full parameter and return type hints. No issues.

### ✅ safety.py — ALL functions fully type-annotated
`SafetyResult` and `DiffPreview` are `@dataclass(frozen=True)` with typed fields. `ReadSafetyGate.__init__`, `check`, `WriteSafetyGate.__init__`, `check`, `generate_diff`, `approve`, `_format_new_file`, `_format_diff`: all have parameter annotations and return types.

### ✅ tools/file_ops.py — ALL functions fully type-annotated
`_apply_single_edit` uses the `_EditResult = tuple[str, ToolResult]` type alias. All `@_register`/`@_summarize` functions (`_read_file`, `_write_file`, `_edit_file`, `_list_directory`, `_file_info`) have `(args: dict, wg: WriteSafetyGate, rg: ReadSafetyGate) -> ToolResult` signatures. Private helpers `_auto_advance_plan`, `_backup_before_write` annotated.

### ⚠️ tools/__init__.py — Two minor gaps
| Function | Issue |
|---|---|
| `AgentContext.__init__(self)` | Missing `-> None` return annotation |
| `_register(name: str)` | Decorator factory — no return type (`-> Callable`) |
| `_summarize(name: str)` | Same as `_register` |

These are decorative/internal functions, not public API. **Low severity.**

### ✅ tools/schema.py — No functions exist; purely a `TOOLS` list. N/A.

---

## (b) Magic Numbers — Hard-coded numeric literals

### ✅ memory.py — ALL numbers extracted to named constants
`_CHARS_PER_TOKEN = 4`, `_MIN_TOKEN_ESTIMATE = 1`, `_COMPRESSION_KEEP_RECENT = 6`, `_COMPRESSION_MAX_LINES = 5`, `_COMPRESSION_MAX_FIRST_LINE = 500`, `_SUMMARY_PREVIEW_LENGTH = 120`, `_SUMMARY_PATH_PREVIEW = 80`, `_SUMMARY_MAX_TURNS = 3`, `_SUMMARY_MAX_FILES = 5`, `_SUMMARY_MAX_COMMANDS = 3`, `_MARKDOWN_TOOL_RESULT_PREVIEW = 500`, `MemoryStore.DEFAULT_MAX_MESSAGES = 500`, `MemoryStore.DEFAULT_MAX_TOKENS = 800_000`. Zero magic numbers in the code body.

### ✅ safety.py — No magic numbers
ANSI escape sequences are stored as class-level string constants (`_GREEN`, `_RED`, etc.).

### ✅ tools/file_ops.py — Good, one borderline
`_DEFAULT_READ_LINES = 300` and `_ABSOLUTE_MAX_LINES = 1000` are named constants. The timestamp format string `"%Y%m%d_%H%M%S"` in `_backup_before_write` is a format specifier, not a magic number. **Clean.**

### ✅ tools/__init__.py — No magic numbers.
The `_CACHEABLE` frozenset holds tool name strings. No numeric literals in logic.

**Verdict: No magic numbers found in any audited file.**

---

## (c) Structured Dataclass Results — Is every return type a structured dataclass?

### ✅ safety.py
- `SafetyResult` → `@dataclass(frozen=True)` ✓
- `DiffPreview` → `@dataclass(frozen=True)` ✓
- All method return types are either `SafetyResult` or `DiffPreview`. ✓

### ⚠️ tools/__init__.py — ToolResult is NOT a @dataclass
`ToolResult` is a plain class with `__init__` and `to_dict()`/`to_json()` methods, **not** decorated with `@dataclass`. It IS structured (always returns `success: bool` + `content: str` + optional `hint`/`diff_preview`), but strictly does not meet the "dataclass" requirement.

Project rule: *"All tool results must be structured dataclasses — never raw exceptions."*

### ⚠️ tools/file_ops.py — Returns ToolResult (same issue above)
Internal helper `_apply_single_edit` returns `_EditResult = tuple[str, ToolResult]` — a tuple, not a dataclass. However this is a private helper, not a public return type; the public `@_register` functions all return `ToolResult`.

### ✅ memory.py — Not a tool layer
Returns `list[dict]` which is appropriate for storage-layer operations. Not subject to "all tool results" rule since these aren't tool functions.

**Verdict: `ToolResult` should ideally be a `@dataclass`. Currently it is a plain class with `__init__`. The `_EditResult` tuple alias is a minor internal violation.**

---

## (d) Global Mutable State — Module-level variables

### memory.py — 4 globals (for per-save caching + token accumulator)
| Variable | Type | Description |
|---|---|---|
| `_TOOL_PARSE_CACHE` | `dict[int, str]` | Per-save cache: id(msg) → extracted text |
| `_TOKEN_EST_CACHE` | `dict[int, int]` | Per-save cache: id(msg) → estimated token count |
| `_ACCUM_COUNT` | `int` | Running accumulator: message count seen |
| `_ACCUM_TOTAL` | `int` | Running accumulator: total token estimate |

### safety.py — ZERO global mutable state. Only class defs and constants.

### tools/file_ops.py — 3 globals (session state)
| Variable | Type | Description |
|---|---|---|
| `_current_agent_id` | `threading.local` | Current sub-agent task ID (thread-local) |
| `_BACKUPS` | `dict[str, str]` | resolved_path → backup path (session undo) |
| `_FILE_CACHE` | `dict[str, tuple[str, float]]` | Cross-turn file content cache (path → (content, mtime)) |

### tools/__init__.py — 12 globals (registry, context, locks, caches)
| Variable | Type | Description |
|---|---|---|
| `_TOOL_DISPATCH` | `dict[str, callable]` | Tool name → implementation function |
| `_TOOL_SUMMARIES` | `dict[str, callable]` | Tool name → summary function |
| `_TOOL_CONTEXT` | `AgentContext` | Shared mutable context across tools + agent loop |
| `_TOOL_CACHE` | `dict[str, ToolResult]` | Per-turn read-only tool result cache |
| `_MODIFIED_FILES` | `set[str]` | Files modified in current session (for verify) |
| `_MODIFIED_FILES_LOCK` | `threading.Lock` | Thread safety for modified files |
| `_TASK_REGISTRY` | `dict[str, subprocess.Popen]` | Background shell task registry |
| `_FILE_RESERVATIONS` | `dict[str, str]` | file_path → owning agent task_id |
| `_FILE_RESERVATIONS_LOCK` | `threading.Lock` | Thread safety for file reservations |
| `_AGENT_RUNTIME` | `None` (AgentRuntime) | AgentRuntime — set by init_session |
| `_MCP_MANAGER` | `None` (McpClientManager) | MCP client manager — set by init_session |
| `_CACHEABLE` | `frozenset[str]` | Immutable — not mutable state |

### tools/schema.py — ZERO. Purely a `TOOLS` list (immutable after import).

**Total: 19 module-level mutable variables across the 5 audited files. The largest concentrations are in `tools/__init__.py` (12) and `memory.py` (4), which is architecturally acceptable since these are registries/caches/context.**

---

## (e) Circular Imports — Check all import lines

### ✅ safety.py — Standard library only (`difflib`, `os`, `dataclasses`). No circular deps.

### ✅ memory.py — Standard library only (`json`, `os`, `sqlite3`, `warnings`, `typing`). No circular deps.

### ✅ tools/schema.py — No imports at all.

### ⚠️ tools/file_ops.py → tools/__init__.py → tools/file_ops.py (pattern, not a crash)
- `tools/file_ops.py` imports from `tools` (`_register`, `_summarize`, `ToolResult`, `_TOOL_CONTEXT`, etc.) and from `safety`.
- `tools/__init__.py` imports from `tools.schema` (safe), then at the bottom imports `from tools import file_ops, shell_ops, search_ops, agent_ops, lsp, ...`.

This creates a **dependency cycle** (`__init__` → `file_ops` → `__init__`), but it is handled correctly via Python's **late-import pattern**: all names that `file_ops.py` needs from `__init__.py` (`_register`, `_summarize`, `ToolResult`, `_TOOL_CONTEXT`, etc.) are defined **before** the `from tools import file_ops` line in `__init__.py`. Python's partially-loaded module mechanism provides them.

**Verdict: Safe by convention (the "import at bottom of __init__.py" pattern). Not an actual runtime issue. Worth documenting but not fixing.**

### ✅ tools/__init__.py → tools/schema.py: One-way, schema has no imports. Safe.

---

## Summary

| Check | Result |
|---|---|
| (a) Type hints missing | ✅ **Clean** — Only `AgentContext.__init__` missing `-> None`; `_register`/`_summarize` missing return types (low severity) |
| (b) Magic numbers | ✅ **Clean** — Zero magic numbers; all extracted to named constants |
| (c) Structured dataclass returns | ⚠️ **ToolResult** is a plain class, not `@dataclass`. Functions correctly but violates the letter of the rule. `_EditResult` is a bare tuple. |
| (d) Global mutable state | 📊 **19 total** across all files: acceptable registries/caches/context w/ thread-safe locks where needed |
| (e) Circular imports | ⚠️ **Pattern exists** (`__init__` ↔ `file_ops`), but handled safely via late imports |
