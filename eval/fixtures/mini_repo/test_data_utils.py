"""Tests for data_utils. Must pass after the refactor split."""


def test_imports_still_work():
    """All functions should still be importable from data_utils after refactor."""
    from data_utils import load_csv, load_json, load_text
    from data_utils import transform_normalize, transform_filter_positive, transform_scale

    # Basic sanity
    assert callable(load_csv)
    assert callable(load_json)
    assert callable(load_text)
    assert callable(transform_normalize)
    assert callable(transform_filter_positive)
    assert callable(transform_scale)
