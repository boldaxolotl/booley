"""Ticket Board path-selection contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.ticket_board.helpers import tickets_dir_from_project_root


@pytest.fixture(autouse=True)
def _clear_board_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TICKETS_DIR", "BOOLEY_CONTROL_PROJECT_ROOT", "BOOLEY_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_explicit_tickets_dir_overrides_every_project_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "isolated-board"
    control = tmp_path / "control"
    generation = tmp_path / "generation-project"
    monkeypatch.setenv("TICKETS_DIR", str(override))
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", str(control))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(generation))

    assert tickets_dir_from_project_root(control) == override


@pytest.mark.parametrize("layout", ["colocated", "configured", "legacy"])
def test_matching_control_root_selects_its_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    if layout == "colocated":
        project_dir = control / ".booley_project"
    elif layout == "configured":
        project_dir = control / "control-data"
        (control / "booley.toml").write_text('[project]\ndir = "control-data"\n', encoding="utf-8")
    else:
        project_dir = control / ".booley" / "project"
    (project_dir / "tickets").mkdir(parents=True)
    generation = tmp_path / "generation" / ".booley_project"
    (generation / "tickets").mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", str(control))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(generation))

    assert tickets_dir_from_project_root(control / ".") == project_dir / "tickets"


def test_unrelated_control_root_does_not_redirect_generation_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control"
    selected_root = tmp_path / "generation-checkout"
    project_dir = tmp_path / "generation-project"
    (project_dir / "tickets").mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", str(control))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))

    assert tickets_dir_from_project_root(selected_root) == project_dir / "tickets"


def test_project_dir_env_remains_the_ordinary_bind_mount_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "work"
    work_board = work_root / ".booley_project" / "tickets"
    mounted_project = tmp_path / "booley-project"
    mounted_board = mounted_project / "tickets"
    work_board.mkdir(parents=True)
    mounted_board.mkdir(parents=True)
    (work_board / "same-ticket.md").write_text("same", encoding="utf-8")
    (mounted_board / "same-ticket.md").write_text("same", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(mounted_project))

    assert tickets_dir_from_project_root(work_root) == mounted_board


@pytest.mark.parametrize("layout", ["colocated", "legacy"])
def test_project_root_conventions_apply_without_environment_selection(
    tmp_path: Path, layout: str
) -> None:
    root = tmp_path / "project"
    if layout == "colocated":
        tickets = root / ".booley_project" / "tickets"
    else:
        tickets = root / ".booley" / "project" / "tickets"
    tickets.mkdir(parents=True)

    assert tickets_dir_from_project_root(root) == tickets


def test_empty_environment_values_do_not_suppress_valid_lower_priority_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    tickets = root / ".booley_project" / "tickets"
    tickets.mkdir(parents=True)
    monkeypatch.setenv("TICKETS_DIR", "")
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", "")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", "")

    assert tickets_dir_from_project_root(root) == tickets
