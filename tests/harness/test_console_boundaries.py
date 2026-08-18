"""P0/P1 numeric and malformed-input boundary coverage for Console widgets."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.widgets import Static

from booley.harness.console.criteria_format import _format_metric
from booley.harness.console.widgets import McpToolCompletionMark, StatusBar, _render_entry_line

from .console_scenario import ConsoleTestApp


def _status_text(bar: StatusBar) -> str:
    return str(bar.query_one("#status-text", Static).render())


@pytest.mark.asyncio
async def test_status_token_rounding_boundaries_are_stable() -> None:
    """STS-03: document the current compact thousands/millions display rule."""
    app = ConsoleTestApp()
    cases = [
        (0, "0 out"),
        (999, "999 out"),
        (1000, "1k out"),
        (1499, "1k out"),
        (1500, "2k out"),
        # Rounds to "1000k" at the boundary rather than jumping to "1.0m":
        # the unit switch keys off the raw count, not the rounded one.
        (999_999, "1000k out"),
        (1_056_000, "1.1m out"),
        (12_500_000, "12.5m out"),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        for tokens, expected in cases:
            bar._output_tokens = tokens
            bar._refresh_display()
            assert expected in _status_text(bar)


@pytest.mark.asyncio
async def test_status_context_is_absolute_and_shows_the_limit() -> None:
    """STS-03b: context replaces rather than accumulates, and renders used/limit."""
    app = ConsoleTestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        bar.update_counters(context_tokens=90_000, context_limit=1_000_000)
        assert "context 90k/1.0m" in _status_text(bar)
        # A later, larger reading replaces the earlier one (not 90k + 142k).
        bar.update_counters(context_tokens=142_000)
        assert "context 142k/1.0m" in _status_text(bar)
        # Compaction shrinks it back down -- the number must be able to fall.
        bar.update_counters(context_tokens=30_000)
        assert "context 30k/1.0m" in _status_text(bar)


@pytest.mark.asyncio
async def test_status_context_without_a_known_limit_omits_denominator() -> None:
    """An unpriced model (no published window) shows the raw figure only."""
    app = ConsoleTestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        bar.update_counters(context_tokens=12_000)
        text = _status_text(bar)
        assert "context 12k" in text
        assert "12k/" not in text


@pytest.mark.asyncio
async def test_status_shows_wall_and_active_budgets_with_wait_reason() -> None:
    app = ConsoleTestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        bar.set_developer_budget(
            wall_elapsed_seconds=9 * 3600 + 55 * 60,
            active_elapsed_seconds=24 * 60 + 18,
            wall_limit_seconds=12 * 3600,
            active_limit_seconds=30 * 60,
            paused=True,
            pause_reason="waiting for asic_synthesize",
        )
        text = _status_text(bar)
        assert "wall 9h55m/12h" in text
        assert "active 24m18s/30m" in text
        assert "(waiting for asic_synthesize)" in text


@pytest.mark.asyncio
async def test_status_duration_and_raw_cost_sum_boundaries() -> None:
    """STS-02/04/06: refresh changes time only and rounds cumulative cost once."""
    app = ConsoleTestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(StatusBar)
        bar._start_time = 0.0
        duration_cases = [
            (0.0, "elapsed: 0s"),
            (59.4, "elapsed: 59s"),
            (59.9, "elapsed: 60s"),
            (60.0, "elapsed: 1m00s"),
            (90.0, "elapsed: 1m30s"),
            (3600.0, "elapsed: 1h00m00s"),
            (3 * 3600 + 5, "elapsed: 3h00m05s"),
            (10 * 3600 + 23 * 60 + 45, "elapsed: 10h23m45s"),
        ]
        for now, expected in duration_cases:
            with patch("booley.harness.console.widgets.time.monotonic", return_value=now):
                bar._refresh_elapsed()
            assert expected in _status_text(bar)

        for cost in (0.001, 0.001, 0.001, 0.001, 0.001):
            bar.update_counters(cost_usd=cost)
        before = (bar._output_tokens, bar._cost_usd, bar._lines_added, bar._lines_removed)
        assert "$0.01" in _status_text(bar)
        bar._refresh_elapsed()
        assert (
            bar._output_tokens,
            bar._cost_usd,
            bar._lines_added,
            bar._lines_removed,
        ) == before


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0.0, "0s"),
        (59.4, "59s"),
        (59.9, "60s"),
        (60.0, "1.0m"),
        (90.0, "1.5m"),
        (3 * 3600.0, "180.0m"),
    ],
)
def test_summary_duration_boundaries(duration: float, expected: str) -> None:
    """MAIN-08: off-screen summaries use a stable duration representation."""
    mark = McpToolCompletionMark(0, 2, "lint", None, 0, duration, 0.0, "clean")
    assert expected in _render_entry_line(mark).plain


@pytest.mark.parametrize("exit_code", [0, 1, 2, -1, 99])
def test_unknown_exit_codes_keep_a_visible_indicator(exit_code: int) -> None:
    """MAIN-07: no exit code is silently presented as successful."""
    line = _render_entry_line(
        McpToolCompletionMark(0, 2, "future_tool", None, exit_code, 1, 0, "")
    )
    assert line.plain[0] in {"✓", "✗", "!"}
    if exit_code != 0:
        assert line.plain[0] != "✓"


@pytest.mark.parametrize(
    ("key", "entry", "expected"),
    [
        ("coverage_toggle", {"detail": None, "params": None}, ""),
        ("coverage_toggle", {"detail": {"toggle": {"pct": "bad"}}}, ""),
        (
            "coverage_toggle",
            {"detail": {"toggle": {"pct": "87.5"}}, "params": {"min_pct": "80"}},
            "88% (>=80%)",
        ),
        ("fpga_impl_ok", {"detail": {"lut_count": "bad", "wns_ns": []}}, ""),
        (
            "fpga_impl_ok",
            {"detail": {"lut_count": "1200", "ff_count": 12, "wns_ns": "-0.25"}},
            "1.2k LUTs | 12 FFs | WNS -0.25ns",
        ),
        ("synthesis_ok", {"detail": {"cells": object(), "per_clock": []}}, ""),
        ("mutation_score", {"detail": {"detected": "bad", "total_valid": 0}}, ""),
        ("lint_clean", {"detail": "not-a-table"}, ""),
        ("review_correctness", None, ""),
    ],
)
def test_malformed_metric_detail_degrades_without_raising(
    key: str,
    entry: dict | None,
    expected: str,
) -> None:
    """CRT-04/05: external metric payloads cannot crash header rendering."""
    assert _format_metric(key, entry) == expected
