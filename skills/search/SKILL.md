---
name: search
description: Advanced code search -- find usages, semantic search, recall past turns.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - find_symbol
  - find_usages
  - semantic_search
  - recall_turn
---

# Search Skill

Advanced codebase search beyond grep. Use for:

- **find_symbol** -- fast symbol lookup by name (function, class, method); returns file + line
- **find_usages** -- find all callers/usages of a symbol across the codebase
- **semantic_search** -- find code by meaning, not exact text; uses sentence-transformer embeddings
- **recall_turn** -- search past conversation turns for previously discussed approaches

## When to Use Each
| Tool | When |
|------|------|
| `find_symbol` | You know the function/class name | 
| `find_usages` | You need all callers of a function |
| `semantic_search` | You know what the code DOES but not its name |
| `recall_turn` | User references something from a previous conversation |

## Best Practices
- Use `find_symbol` over `search_files` for symbol lookups (much faster)
- `semantic_search` requires the workspace to have Python files to index
- `recall_turn` searches across ALL past sessions, not just current
