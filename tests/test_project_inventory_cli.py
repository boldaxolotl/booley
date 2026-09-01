"""Public ``booley projects`` command contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from booley.projects import cli as project_inventory_cli
from booley.projects import inventory as project_inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    project_inventory_cli.add_subparser(parser.add_subparsers(dest="command"))
    return parser


def test_projects_json_has_a_versioned_nested_contract(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()
    project_inventory.remember_project(project)

    args = _parser().parse_args(["projects", "--json"])

    assert project_inventory_cli.run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "projects": [
            {
                "project_root": str(project.resolve()),
                "status": "present",
                "remembered": True,
                "grants": [],
            }
        ],
    }


def test_projects_discover_accepts_json_after_the_subcommand(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    search_root = tmp_path / "workplace"
    project = search_root / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()

    args = _parser().parse_args(["projects", "discover", str(search_root), "--json"])

    assert project_inventory_cli.run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "discovered": [str(project.resolve())],
    }


def test_projects_forget_reports_the_exact_root(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".booley_project").mkdir(parents=True)
    project_inventory.remember_project(project)

    args = _parser().parse_args(["projects", "forget", str(project), "--json"])

    assert project_inventory_cli.run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "forgotten": str(project.resolve()),
    }


def test_projects_human_output_groups_status_and_grants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        project_inventory,
        "project_inventory",
        lambda: (
            project_inventory.ProjectInventoryEntry(
                "/deleted/project",
                project_inventory.ProjectStatus.MISSING,
                False,
                (project_inventory.ProjectGrantSummary("vivado", "vivado_2025_2", "site"),),
            ),
        ),
    )

    assert project_inventory_cli.run(_parser().parse_args(["projects"])) == 0

    output = capsys.readouterr().out
    assert "/deleted/project [missing; grant only]" in output
    assert "vivado_2025_2" in output
    assert "site" in output


def test_projects_discover_human_output_lists_remembered_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(project_inventory, "discover_projects", lambda _roots: (project,))

    args = _parser().parse_args(["projects", "discover", str(tmp_path)])

    assert project_inventory_cli.run(args) == 0
    assert f"Remembered 1 Project root(s):\n  {project}" in capsys.readouterr().out


def test_projects_forget_human_output_names_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(project_inventory, "forget_project", lambda _project: project)

    args = _parser().parse_args(["projects", "forget", str(project)])

    assert project_inventory_cli.run(args) == 0
    assert f"Forgot Remembered Project Root: {project}" in capsys.readouterr().out


def test_projects_human_output_explains_empty_inventory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(project_inventory, "project_inventory", lambda: ())

    assert project_inventory_cli.run(_parser().parse_args(["projects"])) == 0

    assert "No Remembered Project Roots or Project Grants" in capsys.readouterr().out


def test_projects_human_output_explains_root_without_grants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        project_inventory,
        "project_inventory",
        lambda: (
            project_inventory.ProjectInventoryEntry(
                "/project", project_inventory.ProjectStatus.PRESENT, True, ()
            ),
        ),
    )

    assert project_inventory_cli.run(_parser().parse_args(["projects"])) == 0

    assert "Grants: none" in capsys.readouterr().out


def test_projects_reports_inventory_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> tuple[project_inventory.ProjectInventoryEntry, ...]:
        raise project_inventory.ProjectInventoryError("corrupt inventory")

    monkeypatch.setattr(project_inventory, "project_inventory", fail)

    assert project_inventory_cli.run(_parser().parse_args(["projects"])) == 2

    assert "ERROR: corrupt inventory" in capsys.readouterr().err
