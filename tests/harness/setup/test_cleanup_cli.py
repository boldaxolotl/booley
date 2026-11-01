"""CLI adapter coverage for marker-guarded run-root cleanup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from booley.harness.setup import cleanup_cli


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


def test_cleanup_cli_runs_prepare_preview_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    parser = _parser()

    args = _parse(parser, ["cleanup", "prepare", "--run-id", "cli", "--json"])
    assert cleanup_cli.run(args, tmp_path) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "prepared"
    capture = Path(prepared["run_root"]) / "capture.log"
    capture.write_text("capture\n", encoding="utf-8")

    args = _parse(parser, ["cleanup", "preview", "--run-id", "cli", "--json"])
    assert cleanup_cli.run(args, tmp_path) == 0
    assert "capture.log" in {row["path"] for row in json.loads(capsys.readouterr().out)["files"]}

    args = _parse(parser, ["cleanup", "apply", "--run-id", "cli"])
    assert cleanup_cli.run(args, tmp_path) == 0
    assert capsys.readouterr().out.startswith("removed: ")
    assert not capture.parent.exists()


def test_cleanup_cli_rejects_removed_operation_and_options() -> None:
    for argv in (
        ["cleanup", "record", "x", "--run-id", "r", "--producer", "p", "--class", "c"],
        ["cleanup", "apply", "--run-id", "r", "--digest", "d"],
        ["cleanup", "preview", "--run-id", "r", "--retention", "minimal"],
        ["cleanup", "preview", "--run-id", "r", "--cache-disposition", "preserve"],
        ["cleanup", "preview"],
        ["cleanup", "apply"],
    ):
        with pytest.raises(SystemExit) as raised:
            _parser().parse_args(argv)
        assert raised.value.code == 2, argv
