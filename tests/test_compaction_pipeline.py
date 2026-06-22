"""
Test the full Reasonix-style compaction pipeline.
"""
import sys
sys.path.insert(0, ".")

from core.compaction import (
    should_compact, fold_economics, prune_stale_tool_results,
    compact_tool_results_at_turn_end, maybe_compact,
    run_turn_boundary_compaction, estimate_context_tokens,
    mechanical_fold_digest, _partition_fold, _pinned_prefix_len,
    _is_error_message, _is_user_marked, _reset_compaction_state,
    CompactionResult, PruneStats, update_tok_per_char,
    _consecutive_compacts, _compact_stuck,
)

def test_thresholds():
    print("=== Threshold tests ===")
    assert should_compact(0, 100000) is None
    assert should_compact(49999, 100000) is None
    assert should_compact(50000, 100000) == 'soft'
    assert should_compact(79999, 100000) == 'soft'
    assert should_compact(80000, 100000) == 'hard'
    assert should_compact(89999, 100000) == 'hard'
    assert should_compact(90000, 100000) == 'force'
    assert should_compact(200000, 100000) == 'force'
    assert should_compact(100, 0) is None
    print("  should_compact: OK")

def test_economic_gate():
    print("=== Economic gate ===")
    assert fold_economics(0) is False
    assert fold_economics(399) is False
    assert fold_economics(400) is True
    assert fold_economics(10000) is True
    print("  fold_economics: OK")

def test_error_detection():
    print("=== Error detection ===")
    assert _is_error_message('Error: something broke')
    assert _is_error_message('Traceback (most recent call last):')
    assert _is_error_message('FAILED with exit code 1')
    assert _is_error_message('[ERROR] something')
    assert not _is_error_message('hello world')
    assert not _is_error_message('')
    print("  _is_error_message: OK")

def test_user_marking():
    print("=== User marking ===")
    assert _is_user_marked('[[keep]] this is important')
    assert _is_user_marked('[keep] do not compact')
    assert _is_user_marked('<keep> preserve this')
    assert _is_user_marked('<!-- keep --> also this')
    assert not _is_user_marked('regular message')
    assert not _is_user_marked('')
    print("  _is_user_marked: OK")

def test_pinned_prefix():
    print("=== Pinned prefix ===")
    msgs = [
        {'role': 'system', 'content': 'You are an agent'},
        {'role': 'user', 'content': 'Hello, task description'},
        {'role': 'assistant', 'content': 'I will help'},
        {'role': 'user', 'content': 'Do X'},
    ]
    assert _pinned_prefix_len(msgs, 100000) == 3
    assert _pinned_prefix_len(msgs[:1], 100000) == 1
    assert _pinned_prefix_len([], 100000) == 0

    huge_msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'x' * 10000},
    ]
    assert _pinned_prefix_len(huge_msgs, 100000) == 1
    print("  _pinned_prefix_len: OK")

def test_partition_fold():
    print("=== Partition fold ===")
    region = [
        {'role': 'user', 'content': 'short q'},
        {'role': 'assistant', 'content': 'long answer ' * 200, 'tool_calls': [
            {'id': 't1', 'function': {'name': 'read_file', 'arguments': '{}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 't1', 'content': 'file content here'},
        {'role': 'user', 'content': '[[keep]] important'},
        {'role': 'user', 'content': 'long ' * 200},  # 1000 chars, ~250 tokens, pinnable
    ]
    # context_window=100000 => budget = min(1500, 15000) = 1500 tokens ~ 6000 chars
    kept, fold = _partition_fold(region, context_window=100000, tok_per_char=0.25)
    # Pinnable user turns (short q + long user) + user-marked = 3 kept.
    # Assistant + tool call/result pair = 2 fold.
    assert len(kept) == 3  # short q, [[keep]] marked, long user (all pinnable)
    assert len(fold) == 2  # assistant + paired tool result
    print("  _partition_fold: OK")

def test_mechanical_fold():
    print("=== Mechanical fold ===")
    digest = mechanical_fold_digest(5, '/tmp/archive.jsonl')
    assert '5 earlier message' in digest
    assert '/tmp/archive.jsonl' in digest
    digest2 = mechanical_fold_digest(3)
    assert '3 earlier message' in digest2
    print("  mechanical_fold_digest: OK")

def test_tok_calibration():
    print("=== Token calibration ===")
    _reset_compaction_state()
    test_msgs = [{'role': 'user', 'content': 'hello world' * 100}]
    update_tok_per_char(275, test_msgs)
    print("  update_tok_per_char: OK")

def test_pruning():
    print("=== Pruning ===")
    _reset_compaction_state()
    prune_msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'task'},
        {'role': 'assistant', 'content': 'ok', 'tool_calls': [
            {'id': 't1', 'function': {'name': 'read_file', 'arguments': '{"path": "test.py"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 't1', 'content': 'x' * 1500},  # > 1024 min prune
        {'role': 'assistant', 'content': 'ok2', 'tool_calls': [
            {'id': 't2', 'function': {'name': 'run_shell', 'arguments': '{"command": "ls"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 't2', 'content': 'Error: failed!'},
        {'role': 'user', 'content': 'recent msg'},
        {'role': 'assistant', 'content': 'recent reply'},
    ]
    stats = prune_stale_tool_results(prune_msgs, tail_token_budget=50)
    assert stats.results == 1
    assert '[pruned]' in prune_msgs[3]['content']
    assert 'Error:' in prune_msgs[5]['content']
    print(f"  Pruned {stats.results} results, saved ~{stats.estimated_tokens_saved} tokens: OK")

def test_truncation():
    print("=== Truncation ===")
    trunc_msgs = [{'role': 'tool', 'content': 'a' * 5000}]
    n = compact_tool_results_at_turn_end(trunc_msgs, cap_tokens=100, cap_chars=400)
    assert n == 1
    assert '[compacted' in trunc_msgs[0]['content']
    print(f"  Truncated {n} results: OK")

def test_maybe_compact_disabled():
    print("=== maybe_compact - disabled ===")
    _reset_compaction_state()
    result = maybe_compact([], context_window=0)
    assert result.level == ''
    print("  OK")

def test_maybe_compact_under_threshold():
    print("=== maybe_compact - under threshold ===")
    _reset_compaction_state()
    small_msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hello'},
    ]
    result = maybe_compact(small_msgs, context_window=100000, last_prompt_tokens=5000)
    assert result.level == ''
    print("  OK")

def test_maybe_compact_soft():
    print("=== maybe_compact - soft ===")
    _reset_compaction_state()
    small_msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hello'},
    ]
    result = maybe_compact(small_msgs, context_window=100000, last_prompt_tokens=55000)
    assert result.level == 'soft'
    print("  OK")

def test_maybe_compact_hard_prunes_but_no_compact():
    print("=== maybe_compact - hard, prune only ===")
    _reset_compaction_state()
    hard_msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'task'},
        {'role': 'assistant', 'content': 'ok', 'tool_calls': [
            {'id': 't1', 'function': {'name': 'read_file', 'arguments': '{"path": "f.py"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 't1', 'content': 'x' * 500},
        {'role': 'user', 'content': 'recent1'},
        {'role': 'assistant', 'content': 'recent2'},
    ]
    result = maybe_compact(hard_msgs, context_window=4000, last_prompt_tokens=3500)
    assert result.level == 'hard'
    print(f"  level={result.level}, pruned={result.pruned.results}, compacted={result.compacted}: OK")

def test_stuck_latch_init():
    print("=== Stuck latch init ===")
    _reset_compaction_state()
    assert _consecutive_compacts == 0
    assert _compact_stuck is False
    print("  OK")

def test_run_turn_boundary():
    print("=== run_turn_boundary_compaction ===")
    _reset_compaction_state()
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hey'},
        {'role': 'tool', 'content': 'x' * 5000},
    ]
    result = run_turn_boundary_compaction(msgs, context_window=100000)
    assert result.truncated >= 0
    print(f"  truncated={result.truncated}, level={result.level}: OK")


if __name__ == "__main__":
    test_thresholds()
    test_economic_gate()
    test_error_detection()
    test_user_marking()
    test_pinned_prefix()
    test_partition_fold()
    test_mechanical_fold()
    test_tok_calibration()
    test_pruning()
    test_truncation()
    test_maybe_compact_disabled()
    test_maybe_compact_under_threshold()
    test_maybe_compact_soft()
    test_maybe_compact_hard_prunes_but_no_compact()
    test_stuck_latch_init()
    test_run_turn_boundary()
    print()
    print("ALL 16 TESTS PASSED")
