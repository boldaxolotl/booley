"""Command-line result contract for one Ticket Mode harness run."""

from __future__ import annotations

import argparse
import json
from unittest.mock import AsyncMock

from booley.harness import __main__ as harness_main
from booley.harness.developer import RUN_RESULT_PREFIX, TicketRunResult


def test_successful_review_emits_stable_package_record(tmp_path, monkeypatch, capsys):
    package_path = tmp_path / "logs" / "demo" / ".runtime" / "triage-prep" / "briefing.json"
    html_path = tmp_path / "logs" / "demo" / "explanation.html"
    run = AsyncMock(
        return_value=TicketRunResult(
            slug="demo",
            review_package_path=package_path,
            html_path=html_path,
        )
    )
    monkeypatch.setattr(harness_main, "run_ticket", run)
    args = argparse.Namespace(ticket="demo", no_transcripts=False)

    assert harness_main._run_harness(args, tmp_path, use_console=True) == 0

    line = capsys.readouterr().out.strip()
    assert line.startswith(RUN_RESULT_PREFIX)
    assert json.loads(line.removeprefix(RUN_RESULT_PREFIX)) == {
        "version": 1,
        "slug": "demo",
        "disposition": "review",
        "review_package_path": str(package_path),
        "html_path": str(html_path),
    }
    run.assert_awaited_once_with(
        "demo",
        tmp_path,
        save_transcripts=True,
        use_console=True,
    )


def test_non_review_run_emits_no_result_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness_main, "run_ticket", AsyncMock(return_value=None))
    args = argparse.Namespace(ticket="demo", no_transcripts=True)

    assert harness_main._run_harness(args, tmp_path, use_console=False) == 0

    assert capsys.readouterr().out == ""
