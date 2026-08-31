"""CLI contracts for scriptable upgrade status and acknowledgment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from booley.harness import upgrade_cli, upgrade_review
from booley.harness.booley import _build_parser


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / ".booley_project"
    (project_dir / "runtime").mkdir(parents=True)
    return project_dir


def test_upgrade_status_json_is_scriptable(tmp_path: Path, monkeypatch, capsys) -> None:
    project_dir = _project(tmp_path)
    monkeypatch.setattr("booley.__version__", "1.2.3")
    args = _build_parser().parse_args(["upgrade", "status", "--json"])

    assert upgrade_cli.run(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["condition"] == "current"
    assert payload["reviewed_through"] == "1.2.3"
    assert Path(payload["state_path"]) == upgrade_review.state_path(project_dir)


def test_upgrade_acknowledge_reports_compare_and_swap_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _project(tmp_path)
    monkeypatch.setattr("booley.__version__", "1.2.3")
    args = _build_parser().parse_args(
        ["upgrade", "acknowledge", "--expected-target", "1.2.3", "--json"]
    )

    assert upgrade_cli.run(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["acknowledged"] is False
    assert "does not exist" in payload["error"]


def test_upgrade_acknowledge_human_error_uses_stderr(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path)
    monkeypatch.setattr("booley.__version__", "1.2.3")
    args = _build_parser().parse_args(["upgrade", "acknowledge", "--expected-target", "1.2.3"])

    assert upgrade_cli.run(args, tmp_path) == 2
    assert "ERROR:" in capsys.readouterr().err


def test_unknown_upgrade_command_is_rejected(tmp_path: Path, capsys) -> None:
    _project(tmp_path)

    assert upgrade_cli.run(argparse.Namespace(upgrade_command="mystery"), tmp_path) == 2
    assert "unknown upgrade subcommand" in capsys.readouterr().err


def test_pending_status_is_observation_not_command_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project_dir = _project(tmp_path)
    (project_dir / "runtime" / "doctor_stamp.json").write_text(
        json.dumps({"booley_version": "1.0.0"}), encoding="utf-8"
    )
    monkeypatch.setattr("booley.__version__", "1.2.3")
    args = _build_parser().parse_args(["upgrade", "status", "--json"])

    assert upgrade_cli.run(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["condition"] == "pending"


def test_human_status_rendering_covers_actionable_conditions() -> None:
    path = "/project/runtime/upgrade_review.json"
    current = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.CURRENT,
        "2.0.0",
        path,
        reviewed_through="2.0.0",
    )
    pending = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.PENDING,
        "2.0.0",
        path,
        reviewed_through="1.0.0",
        pending_target="2.0.0",
    )
    stale = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.STALE_RUNTIME,
        "1.0.0",
        path,
        reviewed_through="1.0.0",
        pending_target="2.0.0",
    )
    corrupt = upgrade_review.ReviewStatus(
        upgrade_review.ReviewCondition.CORRUPT,
        "2.0.0",
        path,
        diagnostic="bad JSON",
    )

    assert "current through 2.0.0" in upgrade_cli.render_status(current)
    assert "1.0.0 -> 2.0.0" in upgrade_cli.render_status(pending)
    assert "Refresh or rebuild" in upgrade_cli.render_status(stale)
    assert "corrupt: bad JSON" in upgrade_cli.render_status(corrupt)


def test_human_status_command_uses_renderer(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path)
    monkeypatch.setattr("booley.__version__", "1.2.3")
    args = _build_parser().parse_args(["upgrade", "status"])

    assert upgrade_cli.run(args, tmp_path) == 0
    assert "current through 1.2.3" in capsys.readouterr().out
