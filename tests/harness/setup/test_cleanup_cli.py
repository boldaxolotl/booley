"""CLI adapter coverage for manifest-owned cleanup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from booley.harness.setup import cleanup_cli
from booley.harness.setup.cleanup import prepare_run, preview_cleanup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cleanup_cli.add_subparser(commands)
    return parser


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """Parse like the root command, which normalizes an absent root alias to empty."""
    args = parser.parse_args(argv)
    args.project_root = ""
    return args


def test_cleanup_cli_runs_prepare_record_preview_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / ".booley"
    project.mkdir()
    parser = _parser()

    args = _parse(parser, ["cleanup", "prepare", "--run-id", "cli", "--json"])
    assert cleanup_cli.run(args, tmp_path) == 0
    assert "scratch_root" in json.loads(capsys.readouterr().out)

    capture = project / "tmp" / "setup" / "cli" / "capture.log"
    capture.write_text("capture\n", encoding="utf-8")
    args = _parse(
        parser,
        [
            "cleanup",
            "record",
            str(capture),
            "--run-id",
            "cli",
            "--producer",
            "doctor",
            "--class",
            "capture",
            "--job-record",
            "job.json",
        ],
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert "capture.log" in capsys.readouterr().out

    plan = preview_cleanup(tmp_path, run_id="cli")
    args = _parse(parser, ["cleanup", "preview", "--run-id", "cli", "--json"])
    assert cleanup_cli.run(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["digest"] == plan.digest

    args = _parse(
        parser,
        [
            "cleanup",
            "apply",
            "--run-id",
            "cli",
            "--digest",
            plan.digest,
            "--json",
        ],
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["unresolved"] == []


def test_cleanup_cli_reports_stale_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    prepare_run(tmp_path, "cli-stale")
    args = _parse(
        _parser(),
        [
            "cleanup",
            "apply",
            "--run-id",
            "cli-stale",
            "--digest",
            "stale",
        ],
    )

    assert cleanup_cli.run(args, tmp_path) == 2
    assert "stale" in capsys.readouterr().err


def test_cleanup_cli_rejects_unknown_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    args = argparse.Namespace(cleanup_command="unknown", project_root="")

    assert cleanup_cli.run(args, tmp_path) == 2
    assert "operation is required" in capsys.readouterr().err
