"""JSON repair utilities for LLM-generated tool arguments.

Handles common malformations: trailing commas, single quotes, unquoted keys.
Used by execute_tool() before parsing tool call arguments.
"""

from __future__ import annotations

import json
import re


def repair_json(raw: str) -> tuple[object, bool]:
    """Attempt to repair common LLM-generated JSON malformations.

    Returns (parsed_value, was_repaired).  If all repair attempts fail the
    original raw string is re-raised via json.loads so callers see a standard
    JSONDecodeError.

    Repairs attempted (in order, each retried independently, then combinations):
    1. Trailing commas before ``]`` or ``}``
    2. Single-quoted strings → double quotes
    3. Unquoted object keys
    4. 1+2, 1+3, 2+3, 1+2+3 (combinations)
    """

    def _fix_unquoted_keys(text: str) -> str:
        """Quote bare keys but skip content inside double-quoted strings."""
        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == '"':
                # Find end of string (handle backslash escapes)
                j = i + 1
                while j < len(text):
                    if text[j] == '\\' and j + 1 < len(text):
                        j += 2
                        continue
                    if text[j] == '"':
                        j += 1
                        break
                    j += 1
                result.append(text[i:j])
                i = j
            else:
                # Accumulate consecutive non-quoted chars into one segment
                j = i
                while j < len(text) and text[j] != '"':
                    j += 1
                result.append(text[i:j])
                i = j
        # Only apply regex to segments at even indices (outside strings).
        # Use [A-Za-z_]\\w* instead of \\w+ to avoid matching numeric keys
        # like '1:' which would produce '"1":}' instead of leaving '1:' alone.
        for idx in range(0, len(result), 2):
            result[idx] = re.sub(r'([A-Za-z_]\w*)(\s*:)', r'"\1"\2', result[idx])
        return ''.join(result)

    # Individual fixes
    fix1 = re.sub(r',\s*([}\]])', r'\1', raw)

    fix2 = raw
    if "'" in raw:
        fix2 = raw.replace("'", '"')

    fix3 = raw
    if not raw.strip().startswith('['):
        fix3 = _fix_unquoted_keys(raw)

    # Combinations — apply fixes in sequence on copies
    def _apply_combo(base: str, *indices: int) -> str:
        s = base
        for i in indices:
            if i == 1:
                s = re.sub(r',\s*([}\]])', r'\1', s)
            elif i == 2:
                s = s.replace("'", '"')
            elif i == 3:
                if not s.strip().startswith('['):
                    s = _fix_unquoted_keys(s)
        return s

    attempts: list[str] = [
        fix1,
        fix2,
        fix3,
        _apply_combo(raw, 1, 2),
        _apply_combo(raw, 1, 3),
        _apply_combo(raw, 2, 3),
        _apply_combo(raw, 1, 2, 3),
    ]

    for attempt in attempts:
        if attempt == raw:
            continue
        try:
            return json.loads(attempt), True
        except (json.JSONDecodeError, ValueError):
            continue

    # Last resort: try the original
    return json.loads(raw), False
