# Audit Report: tui_pt.py `_export_to_file` Fix

## Findings
| Severity | File | Line | Issue | Fix |
|----------|------|------|-------|-----|
| HIGH | tui_pt.py | 696-703 | Missing `write_gate.check()` — exports would bypass workspace safety gates | Added `self.write_gate.check(path)` with error message in tools_buf, matching tui.py pattern |
| MEDIUM | tui_pt.py | 699 | `\\n\\n` literal backslash-n in export format string instead of actual newlines | Replaced manual format string with `export_conversation_markdown()` from memory module, matching tui.py |
| LOW | tui_pt.py | 696 | No import of `export_conversation_markdown` — hand-rolled export was fragile | Added `from memory import export_conversation_markdown` |

## Before
```python
def _export_to_file(self, path: str) -> None:
    """Export conversation to a markdown file."""
    with open(path, "w") as f:
        f.write("# mini_agent Conversation\\n\\n")
        for msg in self.messages:
            role = msg["role"].upper()
            content = msg.get("content", "")
            f.write(f"## {role}\\n\\n{content}\\n\\n")
```

## After
```python
def _export_to_file(self, path: str) -> None:
    """Export conversation to a markdown file."""
    from memory import export_conversation_markdown
    ok, reason = self.write_gate.check(path)
    if not ok:
        self.tools_buf.append(f"Export blocked: {reason}")
        return
    md = export_conversation_markdown(self.messages)
    with open(path, "w") as f:
        f.write(md)
```

## Verification
- Edit applied successfully (1 occurrence replaced)
- Tests pass (verify: 11+101 tests, no new failures)
- No new lint issues in tui_pt.py
