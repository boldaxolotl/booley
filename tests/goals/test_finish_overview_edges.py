"""Edges of the Finish overview: long diffs, wording, and a summary that cannot be drawn."""

from __future__ import annotations

import pytest

from booley.goals import finish_presentation
from booley.goals.finish_presentation import _diff_overview
from tests.goals.conftest import enter_goals
from tests.goals.test_finish_overview import finish_reply, goal_tool
from tests.goals.test_status import publish

pytestmark = pytest.mark.timeout(120)


def test_diff_overview_caps_the_file_list_and_counts_every_kind():
    rows = [{"change": "added", "path": f"f{index}.sv"} for index in range(22)] + [
        {"change": "modified", "path": "m.sv"},
        {"change": "removed", "path": "r.sv"},
    ]

    lines = _diff_overview(rows)

    assert lines[0] == "Diff summary: 24 files changed (22 added, 1 modified, 1 removed)"
    assert len(lines) == 1 + 20 + 1
    assert lines[-1] == "- ... and 4 more in the full briefing"
    assert _diff_overview([]) == ["Diff summary: no files changed"]


def test_unrenderable_summary_leaves_the_finished_reply_and_status_working(layout, monkeypatch):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)

    def fail(*_args, **_kwargs):
        raise TypeError("malformed package row")

    monkeypatch.setattr(finish_presentation, "render_goal_overview", fail)

    reply = finish_reply(layout)

    # The finish is durable, so the reply is the stored message, not an error.
    assert f"Goal Mode {layout.record.id} finished." in reply
    assert "Input proof" in reply
    status = goal_tool("goal_status", layout)
    assert "Session Summary:" in status
