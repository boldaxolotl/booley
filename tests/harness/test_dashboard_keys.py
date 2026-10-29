"""Dashboard keys are arrows, Enter and Esc only, and the footer names what applies (#2244).

Assertions read observable state only (navigation, cursor, footer and Help
text, row text), so each one fails in the test body on a Dashboard that still
binds letters and digits or shows Textual's static footer.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from textual.widgets import DataTable, Static

from booley.harness.dashboard.app import HELP, DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot
from tests.harness.test_dashboard import choose_in_bar, open_session, snapshot
from tests.harness.test_dashboard_windows import PROPOSAL_ROW, world

# The keys the Dashboard used to bind: session digits, Help, Jobs filters, Sessions, owner.
RETIRED_KEYS = (*"123456789", "?", "j", "r", "a", "s", "u")
OVERVIEW_FOOTER = "↑↓ move · ⏎ open · ←→ Sessions / Jobs"


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)


def footer(app):
    """The footer line as the user reads it."""
    return str(app.query_one("#footer", Static).render())


def state_of(app):
    """What a navigation key could change: window, every window's history, the cursor."""
    return (
        app.window,
        {name: list(history) for name, history in app.windows.items()},
        app.query_one(DataTable).cursor_row,
    )


async def assert_retired_keys_change_nothing(app, pilot, where):
    before = state_of(app)
    for key in RETIRED_KEYS:
        await pilot.press(key)
        assert state_of(app) == before, f"{key!r} changed the {where}"
        assert app.is_running


@pytest.mark.asyncio
async def test_retired_letter_and_digit_keys_change_nothing():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 30)) as pilot:
        await assert_retired_keys_change_nothing(app, pilot, "overview")
        await open_session(app, pilot, "codex:00")
        assert app.navigation.view == "session"
        await assert_retired_keys_change_nothing(app, pilot, "session detail")
        await pilot.press("escape", "right")
        assert app.navigation.view == "jobs"
        await assert_retired_keys_change_nothing(app, pilot, "Jobs list")
        await pilot.press("enter")
        assert app.navigation.view == "job"  # narrow terminal: Enter opens the Job
        await assert_retired_keys_change_nothing(app, pilot, "narrow Job detail")


@pytest.mark.asyncio
async def test_q_quits_and_is_never_displayed():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        assert app.active_bindings["q"].binding.show is False
        texts = [HELP, footer(app)]
        await open_session(app, pilot, "codex:00")
        texts.append(footer(app))
        await pilot.press("escape", "right")
        texts.append(footer(app))
        await pilot.press("up")
        texts.append(footer(app))
        for text in texts:
            assert not re.search(r"\bq\b|quit", text, re.IGNORECASE), text
        await pilot.press("down", "q")
        assert not app.is_running


@pytest.mark.asyncio
async def test_footer_at_window_top_level():
    box = [snapshot(3)]
    app = DashboardApp(lambda: box[0])
    async with app.run_test(size=(130, 30)) as pilot:
        assert footer(app) == OVERVIEW_FOOTER
        box[0] = replace(snapshot(0), jobs=JobSnapshot(()))
        await app.refresh_snapshot()
        await pilot.pause()
        assert footer(app) == "↑↓ move · ←→ Sessions / Jobs"


@pytest.mark.asyncio
async def test_footer_in_detail_views_adds_esc_back():
    current = world(jobs=1)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await open_session(app, pilot, "codex:00")
        assert app.navigation.view == "session"
        assert footer(app) == "↑↓ move · ⏎ open · ←→ Sessions / Jobs · esc back"
        # A Booley call row is informational: Enter does nothing there.
        assert app.row_keys[-1].startswith("call:")
        app.query_one(DataTable).move_cursor(row=len(app.row_keys) - 1, animate=False)
        await pilot.pause()
        assert footer(app) == "↑↓ move · ←→ Sessions / Jobs · esc back"
        app.query_one(DataTable).move_cursor(row=PROPOSAL_ROW - 1, animate=False)
        await pilot.pause()
        assert footer(app) == "↑↓ move · ⏎ open · ←→ Sessions / Jobs · esc back"
        await pilot.press("enter")
        assert app.navigation.view == "goal"
        assert footer(app) == "↑↓ move · ←→ Sessions / Jobs · esc back"


@pytest.mark.asyncio
async def test_footer_on_jobs_list_adds_filters_and_omits_open_when_wide():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.press("right")
        assert app.navigation.view == "jobs"
        assert footer(app) == "↑↓ move · ←→ Sessions / Jobs · ↑ filters"
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        await pilot.pause()
        assert footer(app) == "↑↓ move · ⏎ open · ←→ Sessions / Jobs · ↑ filters"


@pytest.mark.asyncio
async def test_footer_with_top_bar_focused():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.press("up")
        assert footer(app) == "←→ choose · ⏎ apply · ↓ back to list"
        await pilot.press("down")
        assert footer(app) == OVERVIEW_FOOTER


@pytest.mark.asyncio
async def test_session_rows_carry_no_shortcut_number():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        header = str(next(iter(app.query_one(DataTable).columns.values())).label)
        assert header.startswith("Session"), repr(header)
        _, keys, rows = app._overview()
        session_rows = [
            row[0].plain for key, row in zip(keys, rows, strict=True) if "group:" not in key
        ]
        assert len(session_rows) == 3
        for text in session_rows:
            assert re.match(r"S\d+", text), repr(text)


@pytest.mark.asyncio
async def test_help_describes_arrow_model_and_names_no_removed_key():
    current = snapshot(3)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 40)) as pilot:
        await choose_in_bar(app, pilot, "Help")
        await pilot.press("down")
        assert app.navigation.view == "help"
        text = str(app.query_one("#detail", Static).render())
        for phrase in (
            "↑↓",
            "Enter",
            "Esc",
            "←→",
            "Sessions and Jobs windows",
            "top bar",
            "Running",
            "Finished",
            "All",
            "owner",
            "Click",
            "working",
            "waiting for tool call",
            "waiting for user input",
            "Unknown",
            "read-only",
        ):
            assert phrase in text, phrase
        assert "1-9" not in text and "1\u20139" not in text
        assert not re.search(r"(?:^|· )[1-9?jrasuq] \w", text, re.MULTILINE)
        assert "shortcut" not in text.lower() and "quit" not in text.lower()
