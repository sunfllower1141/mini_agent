
import unittest
from tools.file_ops import _fuzzy_find, _line_match, _find_closest_lines


class TestFuzzyFind(unittest.TestCase):
    """Tests for the cascading 3-pass whitespace-tolerant file matching."""

    # -- Exact match (pass 1) --

    def test_exact_match(self):
        content = "hello world"
        result = _fuzzy_find(content, "hello")
        self.assertEqual(result, (0, 5))

    def test_exact_match_mid_content(self):
        content = "prefix hello world suffix"
        result = _fuzzy_find(content, "hello world")
        self.assertEqual(result, (7, 18))

    def test_exact_match_not_found(self):
        content = "hello world"
        result = _fuzzy_find(content, "goodbye")
        self.assertIsNone(result)

    # -- Trailing whitespace tolerance (pass 2) --

    def test_trailing_whitespace_tolerance(self):
        content = "hello   \nworld"
        search = "hello\nworld"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        self.assertEqual(content[result[0]:result[1]], "hello   \nworld")

    def test_trailing_whitespace_reverse(self):
        content = "hello\nworld"
        search = "hello   \nworld"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Full indentation tolerance (pass 3) --

    def test_indentation_tolerance_spaces_vs_tabs(self):
        content = "\tif x:\n\t    pass"
        search = "    if x:\n        pass"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_indentation_mixed(self):
        content = "  def foo():\n    return 1"
        search = "def foo():\n    return 1"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Multi-line matching --

    def test_multiline_fuzzy(self):
        content = "line1   \n  line2\nline3   "
        search = "line1\n  line2\nline3"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Ambiguous match safety --

    def test_ambiguous_match_refused(self):
        content = "foo\nbar\nfoo\nbar"
        search = "foo\nbar"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)  # exact match is unambiguous
        # For fuzzy: two identical fuzzy matches should return None
        content2 = "  foo\n\tbar\n  foo\n\tbar"
        search2 = "foo\nbar"
        result2 = _fuzzy_find(content2, search2)
        self.assertIsNone(result2)

    # -- Empty edge cases --

    def test_empty_content(self):
        result = _fuzzy_find("", "hello")
        self.assertIsNone(result)

    def test_empty_search(self):
        result = _fuzzy_find("hello", "")
        self.assertIsNone(result)

    def test_both_empty(self):
        result = _fuzzy_find("", "")
        self.assertIsNone(result)

    # -- Single line fuzzy --

    def test_single_line_trailing_whitespace(self):
        content = "hello world    "
        search = "hello world"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_single_line_leading_whitespace(self):
        content = "    hello world"
        search = "hello world"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Original whitespace preserved --

    def test_original_whitespace_preserved(self):
        content = "hello   \n  world"
        search = "hello\nworld"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        start, end = result
        self.assertEqual(content[start:end], "hello   \n  world")


class TestLineMatch(unittest.TestCase):
    """Tests for the _line_match helper."""

    def test_right_trim_exact(self):
        cl = ["hello", "world"]
        sl = ["hello", "world"]
        result = _line_match(cl, sl, trim="right")
        self.assertIsNotNone(result)

    def test_right_trim_trailing(self):
        cl = ["hello   ", "world  "]
        sl = ["hello", "world"]
        result = _line_match(cl, sl, trim="right")
        self.assertIsNotNone(result)

    def test_all_trim_indentation(self):
        cl = ["  hello", "\tworld"]
        sl = ["hello", "world"]
        result = _line_match(cl, sl, trim="all")
        self.assertIsNotNone(result)

    def test_no_match(self):
        cl = ["hello", "world"]
        sl = ["goodbye"]
        result = _line_match(cl, sl, trim="right")
        self.assertIsNone(result)

    def test_search_longer_than_content(self):
        cl = ["hello"]
        sl = ["hello", "world"]
        result = _line_match(cl, sl, trim="right")
        self.assertIsNone(result)

    def test_ambiguous(self):
        cl = ["foo", "bar", "foo", "bar"]
        sl = ["foo"]
        result = _line_match(cl, sl, trim="right")
        self.assertIsNone(result)



class TestFuzzyFindPass4(unittest.TestCase):
    """Tests for the 4th-pass normalized-content fuzzy matching."""

    # -- Tab vs space normalization --
    def test_tabs_vs_spaces(self):
        content = "\t\tif x:\n\t\t    pass"
        search = "    if x:\n        pass"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        start, end = result
        self.assertEqual(content[start:end], "\t\tif x:\n\t\t    pass")

    def test_mixed_tabs_spaces(self):
        content = "  def foo():\n    return 1"
        search = "\tdef foo():\n\treturn 1"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- CRLF normalization --
    def test_crlf_normalization(self):
        content = "hello\r\nworld\r\n"
        search = "hello\nworld"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        start, end = result
        # CRLF is normalized in matching; the returned region should contain the matched content
        matched = content[start:end].replace('\r', '')
        self.assertIn("helloworld", matched.replace('\n', ''))

    # -- Collapsed whitespace --
    def test_collapsed_extra_spaces(self):
        content = "hello    world\n  foo  bar"
        search = "hello world\nfoo bar"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Exact still wins over fuzzy --
    def test_exact_wins_over_fuzzy(self):
        content = "  hello world\n  foo bar"
        search = "  hello world\n  foo bar"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        start, end = result
        # Should match at start since exact match found it
        self.assertEqual(content[start:end], search)

    # -- Ambiguous fuzzy match refused --
    def test_ambiguous_fuzzy_refused(self):
        content = "  hello world\n  foo bar\n  hello world\n  foo bar"
        search = "hello world\nfoo bar"
        result = _fuzzy_find(content, search)
        self.assertIsNone(result)

    # -- Single line fuzzy --
    def test_single_line_tab_to_space(self):
        content = "\t\treturn x + y"
        search = "    return x + y"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    # -- Empty still fails --
    def test_empty_search_still_fails(self):
        result = _fuzzy_find("hello", "")
        self.assertIsNone(result)


class TestFindClosestLines(unittest.TestCase):
    """Tests for the _find_closest_lines diagnostic helper."""

    def test_exact_match_found(self):
        content_lines = ["def foo():", "    return 1", ""]
        search_lines = ["def foo():", "    return 1"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        self.assertEqual(result['line'], 1)
        self.assertEqual(result['lines'], ["def foo():", "    return 1"])

    def test_tab_mismatch(self):
        content_lines = ["\tdef foo():", "\t    return 1"]
        search_lines = ["    def foo():", "        return 1"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        # Should match since normalization handles tabs
        self.assertEqual(result['line'], 1)

    def test_whitespace_diff_shown(self):
        content_lines = ["def bar():", "    return 42"]
        search_lines = ["def foo():", "    return 1"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        self.assertIn("expected", result['diff_hint'].lower())


# ---------------------------------------------------------------------------
# New features: quote normalization, Unicode whitespace, indentation
# preservation, confidence scoring, read-before-edit
# ---------------------------------------------------------------------------

from tools.file_ops import (
    _normalize_quotes,
    _normalize_unicode_whitespace,
    _canonicalize_for_match,
    _preserve_indentation,
    _READ_FILES,
)


class TestQuoteNormalization(unittest.TestCase):
    """Tests for curly/smart quote → ASCII normalization."""

    def test_curly_double_quotes(self):
        content = '\u201cHello world\u201d'
        search = '"Hello world"'
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)
        start, end = result
        # Should match the original curly-quoted content
        self.assertIn('Hello world', content[start:end])

    def test_curly_single_quotes(self):
        content = "\u2018test\u2019"
        search = "'test'"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_mixed_quotes_in_code(self):
        content = 'x = \u201cfoo\u201d + \u2018bar\u2019'
        search = 'x = "foo" + \'bar\''
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_normalize_quotes_helper(self):
        self.assertEqual(_normalize_quotes('\u201cHello\u201d'), '"Hello"')
        self.assertEqual(_normalize_quotes("\u2018Hi\u2019"), "'Hi'")
        self.assertEqual(_normalize_quotes("plain"), "plain")


class TestUnicodeWhitespaceNormalization(unittest.TestCase):
    """Tests for Unicode whitespace → ASCII space normalization."""

    def test_nbsp_to_space(self):
        content = "hello\u00a0world"
        search = "hello world"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_figure_space(self):
        content = "col1\u2007col2"
        search = "col1 col2"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_narrow_nbsp(self):
        content = "a\u202fb"
        search = "a b"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_zero_width_chars_removed(self):
        content = "hello\u200bworld"
        search = "helloworld"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_soft_hyphen_removed(self):
        content = "break\u00adpoint"
        search = "breakpoint"
        result = _fuzzy_find(content, search)
        self.assertIsNotNone(result)

    def test_normalize_unicode_ws_helper(self):
        self.assertEqual(_normalize_unicode_whitespace("a\u00a0b"), "a b")
        self.assertEqual(_normalize_unicode_whitespace("a\u200bb"), "ab")
        self.assertEqual(_normalize_unicode_whitespace("plain"), "plain")

    def test_canonicalize_combines_both(self):
        self.assertEqual(_canonicalize_for_match('\u201cHello\u00a0World\u201d'), '"Hello World"')


class TestIndentationPreservation(unittest.TestCase):
    """Tests for _preserve_indentation."""

    def test_spaces_to_tabs_preserved(self):
        # File uses tabs, model outputs spaces
        old = "    if x:\n        pass"
        new = "    if x:\n        pass\n        log()"
        file_region = "\tif x:\n\t\tpass"
        result = _preserve_indentation(old, new, file_region)
        self.assertIn('\tif x:', result)
        self.assertIn('\t\tpass', result)

    def test_tabs_to_spaces_preserved(self):
        # File uses spaces, model outputs tabs
        old = "\tif x:\n\t\tpass"
        new = "\tif x:\n\t\tpass\n\t\tlog()"
        file_region = "    if x:\n        pass"
        result = _preserve_indentation(old, new, file_region)
        self.assertIn('    if x:', result)
        self.assertIn('        pass', result)

    def test_relative_indent_increase(self):
        old = "def foo():\n    return 1"
        new = "def foo():\n    return 1\n    return 2"
        file_region = "def foo():\n  return 1"
        result = _preserve_indentation(old, new, file_region)
        # Should use 2-space indent from file
        self.assertIn('  return 1', result)
        self.assertIn('  return 2', result)

    def test_single_line_no_preservation(self):
        result = _preserve_indentation("old", "new", "old")
        self.assertEqual(result, "new")

    def test_extra_lines_use_last_offset(self):
        old = "def foo():\n    pass"
        new = "def foo():\n    pass\n    x = 1\n    y = 2"
        file_region = "def foo():\n\tpass"
        result = _preserve_indentation(old, new, file_region)
        self.assertIn('\tx = 1', result)
        self.assertIn('\ty = 2', result)


class TestReadBeforeEdit(unittest.TestCase):
    """Tests for read-before-edit enforcement."""

    def setUp(self):
        self._saved = set(_READ_FILES)

    def tearDown(self):
        _READ_FILES.clear()
        _READ_FILES.update(self._saved)

    def test_read_tracks_file(self):
        _READ_FILES.clear()
        self.assertNotIn("/tmp/test.py", _READ_FILES)
        _READ_FILES.add("/tmp/test.py")
        self.assertIn("/tmp/test.py", _READ_FILES)


class TestConfidenceScoring(unittest.TestCase):
    """Tests for confidence scoring in _find_closest_lines."""

    def test_exact_match_has_confidence_100(self):
        content_lines = ["def foo():", "    return 1"]
        search_lines = ["def foo():", "    return 1"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        self.assertEqual(result['match_ratio'], 1.0)
        self.assertEqual(result['matched_lines'], 2)

    def test_partial_match_has_lower_confidence(self):
        # 3 lines: 2 match, 1 has different content = 2/3 confidence
        content_lines = ["def foo():", "    return 42", "    extra"]
        search_lines = ["def foo():", "    return 42", "    different"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result['match_ratio'], 2/3)
        self.assertEqual(result['matched_lines'], 2)

    def test_different_content_zero_confidence(self):
        content_lines = ["def bar():", "    return 42"]
        search_lines = ["def foo():", "    return 1"]
        result = _find_closest_lines(content_lines, search_lines)
        self.assertIsNotNone(result)
        self.assertEqual(result['match_ratio'], 0.0)  # completely different
        self.assertEqual(result['matched_lines'], 0)


if __name__ == "__main__":
    unittest.main()
