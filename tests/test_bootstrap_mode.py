"""Tests for booley.runtime.bootstrap_mode — TOP_LEVEL vs NESTED detection."""

from __future__ import annotations

from unittest import mock

import pytest

from booley.runtime.bootstrap_mode import (
    BootstrapMode,
    should_run_outer_bookkeeping,
)


class TestBootstrapModeDetect:
    def test_default_is_top_level(self):
        # Strip the marker entirely.
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BOOLEY_MCP_NESTED", None)
            assert BootstrapMode.detect() is BootstrapMode.TOP_LEVEL

    def test_marker_equals_1_is_nested(self):
        with mock.patch.dict("os.environ", {"BOOLEY_MCP_NESTED": "1"}):
            assert BootstrapMode.detect() is BootstrapMode.NESTED

    @pytest.mark.parametrize("val", ["", "0", "true", "yes", "2"])
    def test_other_values_are_top_level(self, val):
        # Strict "1" check — anything else is TOP_LEVEL. Guard against accidental
        # truthy reinterpretation.
        with mock.patch.dict("os.environ", {"BOOLEY_MCP_NESTED": val}):
            assert BootstrapMode.detect() is BootstrapMode.TOP_LEVEL


class TestModeFlags:
    def test_top_level_flags(self):
        m = BootstrapMode.TOP_LEVEL
        assert m.is_top_level is True

    def test_nested_flags(self):
        m = BootstrapMode.NESTED
        assert m.is_top_level is False


class TestShouldRunOuterBookkeeping:
    """Chokepoint behavior: True iff TOP_LEVEL."""

    def test_top_level_runs_bookkeeping(self):
        import os

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("BOOLEY_MCP_NESTED", None)
            assert should_run_outer_bookkeeping() is True

    def test_nested_skips_bookkeeping(self):
        with mock.patch.dict("os.environ", {"BOOLEY_MCP_NESTED": "1"}):
            assert should_run_outer_bookkeeping() is False
