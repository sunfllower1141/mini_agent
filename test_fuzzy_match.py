
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

if __name__ == "__main__":
    unittest.main()
