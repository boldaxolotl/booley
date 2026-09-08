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

    assert harness_main._run_harness(args, tmp_path) == 0

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
    )


def test_non_review_run_emits_no_result_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness_main, "run_ticket", AsyncMock(return_value=None))
    args = argparse.Namespace(ticket="demo", no_transcripts=True)

    assert harness_main._run_harness(args, tmp_path) == 0

    assert capsys.readouterr().out == ""


def test_main_forwards_cli_options_to_ticket_execution(tmp_path, monkeypatch):
    import sys
    from unittest.mock import Mock

    run = AsyncMock(return_value=None)
    logging_setup = Mock()
    monkeypatch.setattr(harness_main, "run_ticket", run)
    monkeypatch.setattr(harness_main, "_setup_logging", logging_setup)
    monkeypatch.setattr(harness_main, "_stamp_developer_pid", Mock())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harness",
            "--ticket",
            "demo",
            "--project-root",
            str(tmp_path),
            "--no-transcripts",
            "--verbose",
        ],
    )

    assert harness_main.main() == 0

    logging_setup.assert_called_once_with(True)
    run.assert_awaited_once_with("demo", tmp_path, save_transcripts=False)


def test_console_logging_keeps_startup_warnings_visible(monkeypatch, capsys):
    import logging

    root = logging.getLogger()
    previous_level = root.level
    monkeypatch.setattr(root, "handlers", [])
    try:
        harness_main._setup_logging(verbose=True)
        assert root.isEnabledFor(logging.DEBUG)
        root.debug("file-only debug detail")
        root.info("Console owns normal output")
        root.warning("startup warning")
    finally:
        root.setLevel(previous_level)

    output = capsys.readouterr()
    assert output.out == ""
    assert "startup warning" in output.err
    assert "file-only debug detail" not in output.err
    assert "Console owns normal output" not in output.err
