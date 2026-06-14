---
name: test
description: Test execution and diagnosis -- run tests, verify changes, diagnose failures.
version: "1.0"
author: mini_agent
category: software-development
tools:
  - run_tests
  - verify
  - diagnose_failures
---

# Test Skill

Run and diagnose tests. Use for:

- **run_tests** -- execute pytest with path and keyword filters; captures output and parses results
- **verify** -- run a single-file syntax check or targeted test to validate a change
- **diagnose_failures** -- analyze test failures with exception inspection, stack trace review, and fix suggestions

## Testing Workflow
1. Make a change
2. Run `verify` on the changed file for syntax + quick test
3. Run focused tests with `run_tests` on affected module
4. If failures, use `diagnose_failures` to understand root cause
5. Fix, repeat from step 2

## Best Practices
- Prefer file-scoped test runs over project-wide (`pytest path/to/test.py -v`)
- Use `-k` for keyword filtering when running large test suites
- Run ALL relevant tests after refactoring, not just the changed module
- Never commit with failing tests
