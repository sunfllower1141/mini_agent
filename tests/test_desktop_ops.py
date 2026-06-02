#!/usr/bin/env python3
"""Tests for tools/desktop_ops.py — desktop UI automation."""

from __future__ import annotations

import platform
import sys

import pytest

from tools.desktop_ops import (
    _detect_providers,
    _get_best_provider,
    _get_providers,
    _provider_list,
    _install_instructions,
    _truncate,
    _timestamp,
    PLATFORM,
)


class TestProviderDetection:
    """Tests for platform and provider detection."""

    def test_platform_is_valid(self):
        """PLATFORM should be one of the three supported values."""
        assert PLATFORM in ("Darwin", "Windows", "Linux")

    def test_detect_providers_returns_dict(self):
        """_detect_providers returns a dict of str→bool."""
        providers = _detect_providers()
        assert isinstance(providers, dict)
        for k, v in providers.items():
            assert isinstance(k, str)
            assert isinstance(v, bool)

    def test_detect_providers_has_platform_specific(self):
        """Each platform should detect its own relevant providers."""
        providers = _detect_providers()
        if PLATFORM == "Darwin":
            assert "osascript" in providers
            assert "atomacos" in providers
            assert "pyobjc" in providers
        elif PLATFORM == "Windows":
            assert "uiautomation" in providers
            assert "pywinauto" in providers
        elif PLATFORM == "Linux":
            assert "pyatspi" in providers or "dogtail" in providers

    def test_osascript_available_on_macos(self):
        """osascript should be available on macOS."""
        if PLATFORM == "Darwin":
            providers = _detect_providers()
            assert providers.get("osascript") is True, (
                "osascript should always be available on macOS"
            )

    def test_provider_cache(self):
        """Provider detection should be cached."""
        p1 = _get_providers()
        p2 = _get_providers()
        assert p1 is p2  # Same object, cached

    def test_best_provider_returns_str_or_none(self):
        """_get_best_provider returns a string or None."""
        best = _get_best_provider()
        assert best is None or isinstance(best, str)

    def test_provider_list(self):
        """_provider_list returns a human-readable string."""
        result = _provider_list()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "Available" in result or "Not installed" in result

    def test_install_instructions(self):
        """_install_instructions returns platform-specific text."""
        text = _install_instructions()
        assert isinstance(text, str)
        assert len(text) > 50
        if PLATFORM == "Darwin":
            assert "atomacos" in text.lower() or "macos" in text.lower()
        elif PLATFORM == "Windows":
            assert "uiautomation" in text.lower() or "win32" in text.lower()


class TestHelpers:
    """Tests for helper functions."""

    def test_truncate_short(self):
        """Short text should not be truncated."""
        assert _truncate("hello", 10) == "hello"

    def test_truncate_long(self):
        """Long text should be truncated with ellipsis."""
        result = _truncate("x" * 100, 10)
        assert len(result) <= 10 + 50  # allows for truncation message
        assert "…" in result or "..." in result

    def test_truncate_exact(self):
        """Exact-length text should not be truncated."""
        result = _truncate("hello", 5)
        assert result == "hello"

    def test_timestamp(self):
        """_timestamp returns a string in expected format."""
        ts = _timestamp()
        assert isinstance(ts, str)
        # Format: YYYYMMDD_HHMMSS
        assert len(ts) == 15
        assert "_" in ts


class TestToolRegistration:
    """Tests for tool registration in the dispatch table."""

    def test_desktop_tools_in_dispatch(self):
        """All 5 desktop tools should be in _TOOL_DISPATCH."""
        from tools import _TOOL_DISPATCH
        expected = [
            "desktop_snapshot",
            "desktop_click",
            "desktop_type",
            "desktop_find",
            "desktop_screenshot",
        ]
        for name in expected:
            assert name in _TOOL_DISPATCH, (
                f"Missing dispatch for '{name}'"
            )
            assert callable(_TOOL_DISPATCH[name])

    def test_desktop_tools_in_schema(self):
        """All 5 desktop tools should have schemas in TOOLS."""
        from tools.schema import TOOLS
        desktop_names = [
            t["function"]["name"]
            for t in TOOLS
            if t["function"]["name"].startswith("desktop_")
        ]
        assert len(desktop_names) >= 5
        for name in ["desktop_snapshot", "desktop_click", "desktop_type",
                     "desktop_find", "desktop_screenshot"]:
            assert name in desktop_names, f"Missing schema for '{name}'"

    def test_desktop_tools_have_summaries(self):
        """All 5 desktop tools should have summary functions."""
        from tools import _TOOL_SUMMARIES
        for name in ["desktop_snapshot", "desktop_click", "desktop_type",
                     "desktop_find", "desktop_screenshot"]:
            assert name in _TOOL_SUMMARIES, (
                f"Missing summary for '{name}'"
            )
            assert callable(_TOOL_SUMMARIES[name])

    def test_desktop_skill_registered(self):
        """The 'desktop' skill should be in SKILLS."""
        from tools.skills import SKILLS
        assert "desktop" in SKILLS
        assert len(SKILLS["desktop"]) == 5
        assert "desktop_snapshot" in SKILLS["desktop"]
        assert "desktop_click" in SKILLS["desktop"]
        assert "desktop_type" in SKILLS["desktop"]
        assert "desktop_find" in SKILLS["desktop"]
        assert "desktop_screenshot" in SKILLS["desktop"]

    def test_use_skill_desktop_available(self):
        """'desktop' should be listed in the use_skill schema."""
        from tools.skills import USE_SKILL_SCHEMA
        desc = USE_SKILL_SCHEMA["function"]["description"]
        assert "desktop" in desc


class TestSkillActivation:
    """Tests for skill activation/deactivation."""

    def test_activate_desktop_skill(self):
        """Activating 'desktop' should work."""
        from tools.skills import activate_skill, reset_skills, active_skills
        reset_skills()
        ok, msg = activate_skill("desktop")
        assert ok is True
        assert "desktop" in active_skills()
        assert "desktop_snapshot" in msg

    def test_activate_idempotent(self):
        """Activating twice should be idempotent."""
        from tools.skills import activate_skill, reset_skills
        reset_skills()
        ok1, _ = activate_skill("desktop")
        ok2, msg2 = activate_skill("desktop")
        assert ok1 is True
        assert ok2 is True
        assert "already active" in msg2

    def test_unknown_skill(self):
        """Activating an unknown skill should fail."""
        from tools.skills import activate_skill, reset_skills
        reset_skills()
        ok, msg = activate_skill("nonexistent_xyz")
        assert ok is False
        assert "Unknown" in msg

    def test_desktop_in_get_active_tool_names(self):
        """After activation, desktop tools appear in active names."""
        from tools.skills import (
            activate_skill, reset_skills, get_active_tool_names,
        )
        reset_skills()
        activate_skill("desktop")
        names = get_active_tool_names()
        assert "desktop_snapshot" in names
        assert "desktop_click" in names


class TestDesktopSnapshotErrors:
    """Tests for error paths in desktop_snapshot."""

    def test_snapshot_missing_args_ignored(self):
        """desktop_snapshot takes no args but should handle them gracefully."""
        from tools.desktop_ops import _desktop_snapshot
        result = _desktop_snapshot({}, None, None)
        # Even on failure, we should get a ToolResult (not an exception)
        assert result is not None
        assert hasattr(result, "success")
        assert hasattr(result, "content")

    def test_click_missing_role(self):
        """desktop_click without role should fail."""
        from tools.desktop_ops import _desktop_click
        result = _desktop_click({}, None, None)
        assert result.success is False
        assert "role" in result.content.lower()

    def test_click_missing_name(self):
        """desktop_click without name should fail."""
        from tools.desktop_ops import _desktop_click
        result = _desktop_click({"role": "button"}, None, None)
        assert result.success is False
        assert "name" in result.content.lower()

    def test_type_missing_text(self):
        """desktop_type without text should fail."""
        from tools.desktop_ops import _desktop_type
        result = _desktop_type({}, None, None)
        assert result.success is False
        assert "text" in result.content.lower()

    def test_find_missing_query(self):
        """desktop_find without query should fail."""
        from tools.desktop_ops import _desktop_find
        result = _desktop_find({}, None, None)
        assert result.success is False
        assert "query" in result.content.lower()

    def test_screenshot_no_args(self):
        """desktop_screenshot with no args should work or give clear error."""
        from tools.desktop_ops import _desktop_screenshot
        result = _desktop_screenshot({}, None, None)
        # Should either succeed (mss installed) or give clear install instructions
        assert result is not None
        assert hasattr(result, "success")
        if not result.success:
            # Should mention mss
            assert "mss" in result.content.lower()


class TestSummaries:
    """Tests for summary functions."""

    def test_snapshot_summary(self):
        from tools.desktop_ops import _desktop_snapshot_summary
        result = _desktop_snapshot_summary({})
        assert isinstance(result, str)
        assert "desktop_snapshot" in result

    def test_click_summary(self):
        from tools.desktop_ops import _desktop_click_summary
        result = _desktop_click_summary({"role": "button", "name": "OK"})
        assert "button" in result
        assert "OK" in result

    def test_type_summary(self):
        from tools.desktop_ops import _desktop_type_summary
        result = _desktop_type_summary({"text": "hello world"})
        assert "hello" in result

    def test_find_summary(self):
        from tools.desktop_ops import _desktop_find_summary
        result = _desktop_find_summary({"query": "search term"})
        assert "search" in result

    def test_screenshot_summary(self):
        from tools.desktop_ops import _desktop_screenshot_summary
        result = _desktop_screenshot_summary({})
        assert isinstance(result, str)
        assert "desktop_screenshot" in result
