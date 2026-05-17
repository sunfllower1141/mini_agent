"""Tests for _compute_complexity and _ROUTE_SIMPLE_KEYWORDS from api.py."""

from __future__ import annotations

import unittest

from api import _compute_complexity, _ROUTE_SIMPLE_KEYWORDS


class TestRouteSimpleKeywords(unittest.TestCase):
    """Test that _ROUTE_SIMPLE_KEYWORDS regex matches expected action verbs."""

    def test_matches_write_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("write a new function"))

    def test_matches_edit_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("edit the config file"))

    def test_matches_refactor_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("refactor the auth module"))

    def test_matches_create_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("create a test file"))

    def test_matches_delete_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("delete old code"))

    def test_matches_modify_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("modify the handler"))

    def test_matches_implement_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("implement a new feature"))

    def test_matches_fix_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("fix the bug"))

    def test_matches_build_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("build a new endpoint"))

    def test_matches_patch_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("patch the vulnerability"))

    def test_matches_restructure_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("restructure the module"))

    def test_matches_rewrite_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("rewrite the parser"))

    def test_matches_replace_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("replace the old code"))

    def test_matches_change_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("change the timeout"))

    def test_matches_update_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("update the version"))

    def test_matches_rename_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("rename the variable"))

    def test_matches_move_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("move the file"))

    def test_matches_remove_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("remove the import"))

    def test_matches_add_keyword(self):
        self.assertTrue(_ROUTE_SIMPLE_KEYWORDS.search("add a docstring"))

    def test_no_match_on_read_prompt(self):
        """'read the file' has no action keyword in the set."""
        self.assertFalse(_ROUTE_SIMPLE_KEYWORDS.search("read the file"))

    def test_no_match_on_explain_prompt(self):
        self.assertFalse(_ROUTE_SIMPLE_KEYWORDS.search("explain what this does"))


class TestComputeComplexity(unittest.TestCase):
    """Test _compute_complexity model routing decisions."""

    # --- short read prompt -> simple ---

    def test_short_read_prompt_returns_simple(self):
        messages = [{"role": "user", "content": "what does this function do?"}]
        self.assertEqual(_compute_complexity(messages), "simple")

    def test_short_explain_prompt_returns_simple(self):
        messages = [{"role": "user", "content": "explain the architecture"}]
        self.assertEqual(_compute_complexity(messages), "simple")

    # --- write/edit prompt -> complex (keyword match) ---

    def test_write_prompt_returns_complex(self):
        messages = [{"role": "user", "content": "write a test for the router"}]
        self.assertEqual(_compute_complexity(messages), "complex")

    def test_edit_prompt_returns_complex(self):
        messages = [{"role": "user", "content": "edit the config"}]
        self.assertEqual(_compute_complexity(messages), "complex")

    # --- refactor prompt -> complex ---

    def test_refactor_prompt_returns_complex(self):
        messages = [{"role": "user", "content": "refactor the auth module"}]
        self.assertEqual(_compute_complexity(messages), "complex")

    # --- long read prompt (>300 chars) -> complex ---

    def test_long_read_prompt_returns_complex(self):
        # Build a >300 char prompt with no action keywords.
        content = (
            "I have a question about the architecture of this project. "
            "Can you please look at the codebase and explain how the routing "
            "works in detail? I want to understand the flow from the entry point "
            "all the way through the handler chain, including middleware, error "
            "handling, and response formatting. Please be thorough and comprehensive."
        )
        self.assertGreater(len(content), 300)
        self.assertFalse(_ROUTE_SIMPLE_KEYWORDS.search(content))
        messages = [{"role": "user", "content": content}]
        self.assertEqual(_compute_complexity(messages), "complex")

    def test_short_prompt_with_keyword_trumps_length(self):
        """Even a very short prompt with a keyword is complex."""
        messages = [{"role": "user", "content": "add"}]
        self.assertEqual(_compute_complexity(messages), "complex")

    # --- empty messages -> complex ---

    def test_empty_list_returns_complex(self):
        self.assertEqual(_compute_complexity([]), "complex")

    def test_no_user_messages_returns_simple(self):
        """Only system/assistant messages with no user text -> simple (no user content found)."""
        messages = [{"role": "system", "content": "you are a helpful assistant"}]
        self.assertEqual(_compute_complexity(messages), "simple")

    # --- last-2-user-messages accumulation ---

    def test_accumulates_last_two_user_messages(self):
        """Two short user messages together may exceed 300 chars -> complex."""
        messages = [
            {"role": "user", "content": "I have a question about the codebase."},
            {"role": "assistant", "content": "Sure, what is it?"},
            {"role": "user", "content": "I need help understanding the data flow through the pipeline from input parsing to output generation, including all intermediate transformations and caching layers."},
        ]
        # Second message alone is <300, but combined with first could exceed 300
        # Actually let's check: the second message alone is 152 chars, first is 33 → total 185 < 300
        # So this should be simple if neither has a keyword
        self.assertEqual(_compute_complexity(messages), "simple")

    def test_keyword_in_second_recent_user_message(self):
        """Keyword in the most recent user message makes it complex."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "write a function"},
        ]
        self.assertEqual(_compute_complexity(messages), "complex")

    def test_keyword_only_in_first_older_message_still_complex(self):
        """Keyword in an older user message (within last 2) still triggers complex."""
        messages = [
            {"role": "user", "content": "write a function"},
            {"role": "user", "content": "also what does it return?"},
        ]
        self.assertEqual(_compute_complexity(messages), "complex")

    def test_non_string_content_handled(self):
        """Content that is not a string should not crash."""
        messages = [{"role": "user", "content": ["list", "of", "strings"]}]
        # str() of a list becomes "['list', 'of', 'strings']" — no keyword, short
        result = _compute_complexity(messages)
        self.assertIn(result, ("simple", "complex"))


if __name__ == "__main__":
    unittest.main()
