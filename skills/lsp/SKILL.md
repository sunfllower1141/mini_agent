---
name: lsp
description: Language Server Protocol integration -- go-to-definition, find references, hover docs, diagnostics.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - lsp_definition
  - lsp_references
  - lsp_hover
  - lsp_diagnostics
---

# LSP Skill

Precise code intelligence via the Language Server Protocol. Use for:

- **lsp_definition** -- jump to definition of any symbol (function, class, variable)
- **lsp_references** -- find all usages of a symbol across the codebase
- **lsp_hover** -- get type information, docstrings, and signature on hover
- **lsp_diagnostics** -- get compiler/linter warnings and errors for current file

## When to Use LSP
- **Understanding unfamiliar code**: hover a symbol to see its type and docs
- **Refactoring safely**: find all references before renaming or changing a function
- **Diagnosing issues**: check diagnostics for type errors, unused imports, etc.
- **Navigating large codebases**: jump-to-definition is faster than grep/search

## Best Practices
- Prefer LSP over grep for understanding code structure
- Check diagnostics BEFORE making changes to avoid introducing new errors
- Use `lsp_references` to assess blast radius before editing widely-used functions
