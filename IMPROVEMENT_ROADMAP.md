## Completed
1. ✅ check the web first for solutions to a users problem — hardwired in prompt.py as mandatory rule
2. ✅ remove all safety guards — `_check_destructive` always returns None; ReadSafetyGate/WriteSafetyGate always allow
3. ✅ fix semantic_search hang — `_sem_get_model()` now has 120s timeout, callers catch TimeoutError gracefully

## Pending
4. 
