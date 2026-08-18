"""Tests for ``_recover_if_criteria_met`` — the crash-recovery gate.

Regression context: the gate used to read ``getattr(state, "all_mandatory_met",
False)``, but ``all_mandatory_met`` is a METHOD — getattr returned the bound
method (always truthy), so EVERY crash "recovered", printing a bogus
"criteria met" line even for a 0-token auth failure at agent launch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from booley.dev_support.development_state import CriterionEntry
from booley.harness.developer import _invoke_developer_agent, _recover_if_criteria_met
from booley.harness.models import TicketContext
from booley.ticket_board.paths import ticket_runtime_file


@pytest.fixture
def ctx(tmp_path):
    """Minimal TicketContext whose logs_dir lives under tmp_path."""
    project_root = tmp_path / "proj"
    (project_root / ".booley_project" / "tickets" / "logs" / "tkt").mkdir(parents=True)
    return TicketContext(
        slug="tkt",
        ticket_path=tmp_path / "tkt.md",
        ticket_type="refactor",
        branch="main",
        summary="s",
        project_root=project_root,
    )


def _write_state(ctx: TicketContext, criteria: dict[str, CriterionEntry]) -> None:
    import json

    path = ticket_runtime_file(ctx.logs_dir, "booley_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"criteria": {k: v.to_dict() for k, v in criteria.items()}}
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestRecoverIfCriteriaMet:
    def test_no_state_file_means_no_recovery(self, ctx):
        assert _recover_if_criteria_met(ctx, 1, crash_reason="boom") is None

    def test_unmet_mandatory_blocks_recovery(self, ctx):
        """The original bug: a launch-time crash (nothing met) must NOT recover."""
        _write_state(
            ctx, {"review_rtl_functional_done": CriterionEntry(met=False, mandatory=True)}
        )
        assert _recover_if_criteria_met(ctx, 1, crash_reason="auth failure") is None

    def test_empty_criteria_blocks_recovery(self, ctx):
        """all() over an empty dict is vacuously true — an empty state means the
        agent crashed before doing any work, so there is nothing to recover."""
        _write_state(ctx, {})
        assert _recover_if_criteria_met(ctx, 1, crash_reason="boom") is None

    def test_all_mandatory_met_recovers(self, ctx, capsys):
        _write_state(
            ctx,
            {
                "review_rtl_functional_done": CriterionEntry(met=True, mandatory=True),
                "optional_extra": CriterionEntry(met=False, mandatory=False),
            },
        )
        assert _recover_if_criteria_met(ctx, 1, crash_reason="stream parser died") is not None


@pytest.mark.asyncio
async def test_cancelled_developer_is_recorded_as_resumable(ctx):
    watcher = MagicMock()
    budget = MagicMock()
    with (
        patch(
            "booley.harness.developer._launch_developer_agent",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch(
            "booley.harness.developer._start_display_watcher",
            return_value=(watcher, MagicMock()),
        ),
        patch("booley.harness.developer.record_crash") as record_crash,
        patch("booley.harness.developer.fail_ticket") as fail_ticket,
        patch("booley.harness.developer.terminal.raw"),
        pytest.raises(asyncio.CancelledError),
    ):
        await _invoke_developer_agent(
            ctx,
            ctx.project_root,
            "user prompt",
            "system prompt",
            ctx.logs_dir / "developer.jsonl",
            [],
            4,
            budget,
        )

    reason = "Developer Agent cancelled: CancelledError: "
    record_crash.assert_called_once_with(ctx.logs_dir, run_index=4, reason=reason)
    fail_ticket.assert_called_once_with(
        ctx,
        reason,
        "developer",
        run_index=4,
        crashed=True,
    )
    budget.stop_active.assert_called_once_with()
    watcher.stop.assert_called_once_with()
