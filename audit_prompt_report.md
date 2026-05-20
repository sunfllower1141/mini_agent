# Audit Report: `prompt.py` — Standards Violations

**Auditor**: sub-agent `487902a3`
**File**: `/Users/gabrielmalone/Desktop/mini_agent/prompt.py` (398 lines, 25,880 bytes)
**Criteria checked**:
1. `from __future__ import annotations`
2. Public function type hints
3. Magic numbers → named UPPER_CASE constants
4. Circular imports
5. Global mutable state
6. Tool results as structured dataclasses
7. Explicit control flow (no magic)

---

## Findings

| Severity | File | Line(s) | Issue | Fix |
|----------|------|---------|-------|-----|
| MEDIUM   | prompt.py | 88, 90, 91 | Magic number `15` used three times for the git status file truncation limit. | Define `_GIT_STATUS_MAX_FILES = 15` at module level and reference it consistently. |

---

## Detailed Results

### 1. `from __future__ import annotations` — ✅ PASS
Present at line 8. Compliant.

### 2. Public function type hints — ✅ PASS
The only public function is `build_system_prompt(config: "AgentConfig") -> str:` at line 14. Fully type-annotated. The `"AgentConfig"` string annotation (forward reference) is consistent with the `TYPE_CHECKING` guard.

### 3. Magic numbers — ⚠️ ONE VIOLATION

**`15` — git status truncation limit (lines 88, 90, 91)**

```python
# Line 88
changed = status.split("\n")[:15]
# Line 90
if len(status.split("\n")) > 15:
# Line 91
git_info.append(f"... and {len(status.split(chr(10))) - 15} more files")
```

This is a genuine magic number in code logic. It should be a named module-level constant:
```python
_GIT_STATUS_MAX_FILES = 15
```

**Numbers in `_STATIC_PROMPT` — NOT violations.** The string constant `_STATIC_PROMPT` contains numbers (300, 500, 60, 10, 25, 35, etc.) but these are descriptive prose documenting system behaviour, not values used in program logic. They are documentation, not magic numbers. However, note that values like `10` (default `sub_agent_max_concurrent`) and `25` (default `sub_agent_max_turns`) document config defaults and could drift from actual config — this is a documentation maintenance concern, not a code-quality violation.

### 4. Circular imports — ✅ PASS
Imports are limited to:
- `from __future__ import annotations` (line 8)
- `import os` (line 10)
- `from typing import TYPE_CHECKING` (line 11)
- `from config import AgentConfig` guarded by `if TYPE_CHECKING:` (lines 13-14)
- `import subprocess` is a **local import** inside `build_system_prompt` (line 82)

No circular import risk.

### 5. Global mutable state — ✅ PASS
The only module-level variable is `_STATIC_PROMPT`, which is an immutable string (assigned once via tuple concatenation, never mutated). No mutable global state.

### 6. Tool results as structured dataclasses — N/A
`prompt.py` does not produce `ToolResult` objects. Its sole function returns `str`. This criterion does not apply.

### 7. Control flow — ✅ PASS
Control flow in `build_system_prompt` is straightforward and explicit:
- `while True` directory-walk loop with explicit `parent == search_dir` break condition
- `try/except Exception` for git subprocess calls
- Simple `if/else` branches for safety flags and rules file discovery
- No metaclasses, decorator magic, `__getattr__`, context-manager trickery, or implicit control flow

---

## Summary

| Criterion | Status |
|-----------|--------|
| `from __future__ import annotations` | ✅ |
| Public function type hints | ✅ |
| Magic numbers → named constants | ⚠️ 1 violation (line 88/90/91) |
| Circular imports | ✅ |
| Global mutable state | ✅ |
| Structured tool results | N/A |
| Explicit control flow | ✅ |

**Overall**: `prompt.py` is well-maintained with only one substantive violation: a magic number `15` for the git status file truncation limit that should be extracted to a named constant.
