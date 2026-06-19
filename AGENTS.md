# Agent Rules

Behavioral rules the agent MUST follow. Architecture facts go in STATE.txt.
Long-term facts and preferences go in core memory.

## Git Operations
- The `git` and `diff` tools have been **removed**. All git commands go through `run_shell`:
  - `git status --short`
  - `git diff`
  - `git add -A`
  - `git commit -m "..."`
  - `git push origin branch:branch`
  - `git log --oneline`

## Read/Write Guardrails (ACI)
- **Read-before-edit**: MUST `read_file` any `.py` file before editing it. New files exempt.
  Tracked via `_READ_FILES` set. Rejects hallucinated edits to unseen code.
- **Hash-anchored reads mandatory**: ALL `read_file` calls MUST use `hash_lines=True` as default.
  Only fall back to normal `read_file` when you need raw content without hash prefixes (rare).
  This ensures every read feeds directly into hash-anchored `edit_file` / `edit_lines` without a second read.
- **Syntax validation**: EVERY `.py` write/edit passes through `compile()` first.
  SyntaxErrors are caught BEFORE disk write. Returns exact line number + pointer.
- **Workspace isolation**: All reads/writes bounded to workspace directory.
- **Plan-before-edit**: Declare a plan (`plan` tool) before multi-step code changes.
  Steps auto-complete on file writes.

## Shell Safety
- **Dangerous command detection**: 9 patterns blocked by default (`rm -rf`, `git push --force`,
  `sudo`, `chmod 777`, `dd`, `mkfs`, raw disk redirect, `format`). Requires `force=True`.
- **Empty output**: Shell commands with exit 0 + no output return `"Command completed successfully
  (no output)."` — never an empty string.
- **Search overflow**: When search_files hits the 200-result cap, guide toward precision:
  "use a more specific pattern, subdirectory, or find_symbol."

## Tool Result Conventions
- **Per-result budget**: Individual tool results truncated at 8000 chars during compression.
  Truncated results include offset guidance.
- **Tool cache TTL**: 30-second TTL on cached `read_file` results. Writes invalidate cache.

## Post-Edit Verification
- After editing files, check callers via knowledge graph (`find_callers_of_file`).
- Post-edit verification injection fires every 6 turns + whenever new files modified since last check.
- Git blame included in edit risk briefing (top authors per file).

## Confidence & Knowledge
- **Confidence nudge**: After 3+ search misses, 2+ tool failures, or 6+ read-only turns,
  agent is nudged to use `web_search`. 4-turn cooldown.
## Memory Architecture
- **Core memory**: Long-term persistent facts, preferences, conventions (SQLite).
- **AGENTS.md**: Behavioral rules only (this file).
- **STATE.txt**: Architecture map + task index.
- **CHANGELOG.md**: Historical audit trail.

## Provider Fallback
- On 429/5xx, auto-failover to fallback providers (DeepSeek → Claude).
- Provider-specific params stripped from fallback payloads.
- Streaming disabled during fallback.

## Prompt Cache
- DeepSeek requests set `prompt_cache_key: mini_agent-v1-{tool_count}` for KV-cache stickiness.

## Memory Pruning
- Two-tier: gentle zone (last 7-20 msgs, truncate at 16K chars/20 lines), aggressive zone (21+, per-tool-type compression).
- System prompt (index 0) NEVER compressed/pruned — critical for API prompt caching.
