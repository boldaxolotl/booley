"""Host-owned remembered Project path inventory contracts."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from booley.core import private_store
from booley.projects import inventory as project_inventory
from tests import eda_grant_support


def _project(root: Path, *, booley: bool = True) -> Path:
    (root / ".git").mkdir(parents=True)
    if booley:
        (root / ".booley").mkdir()
    return root


def _grant(root: Path) -> None:
    """A real license-only Project Grant: it needs no installation inspection."""
    eda_grant_support.register_license()
    eda_grant_support.add_grant(root, license_profile="registered")


_REGISTERED = project_inventory.ProjectGrantSummary("vivado", None, "registered")


@pytest.mark.parametrize("case", ["present", "deleted", "grant-only"])
def test_inventory_joins_status_memory_and_grants(tmp_path: Path, case: str) -> None:
    project = _project(tmp_path / "project", booley=case != "grant-only")
    if case != "grant-only":
        assert project_inventory.remember_project(project) == project.resolve()
    if case != "present":
        _grant(project)
    if case == "deleted":
        shutil.rmtree(project)
    status = {
        "present": project_inventory.ProjectStatus.PRESENT,
        "deleted": project_inventory.ProjectStatus.MISSING,
        "grant-only": project_inventory.ProjectStatus.UNINITIALIZED,
    }[case]

    assert project_inventory.project_inventory() == (
        project_inventory.ProjectInventoryEntry(
            project_root=str(project.resolve()),
            status=status,
            remembered=case != "grant-only",
            grants=() if case == "present" else (_REGISTERED,),
        ),
    )


def test_discovery_remembers_projects_and_prunes_their_subtrees(tmp_path: Path) -> None:
    search_root = tmp_path / "workplace"
    project = _project(search_root / "project")
    _project(project / "tests" / "fixture")
    second = _project(search_root / "second")

    assert project_inventory.discover_projects((search_root,)) == (
        project.resolve(),
        second.resolve(),
    )

    assert [entry.project_root for entry in project_inventory.project_inventory()] == [
        str(project.resolve()),
        str(second.resolve()),
    ]


def test_inventory_rejects_a_symlinked_state_file(tmp_path: Path) -> None:
    project_inventory.remember_project(_project(tmp_path / "project"))
    redirected = tmp_path / "redirected.json"
    redirected.write_text(json.dumps({"schema": 1, "projects": []}), encoding="utf-8")
    project_inventory.state_path().unlink()
    project_inventory.state_path().symlink_to(redirected)

    with pytest.raises(project_inventory.ProjectInventoryError, match="must not be a symlink"):
        project_inventory.project_inventory()


def test_inventory_lock_contention_has_a_bounded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "project")
    times = iter((0.0, 11.0))  # the deadline check comes before any sleep
    with project_inventory.locked_protected_roots():
        monkeypatch.setattr(private_store, "monotonic", lambda: next(times))

        with pytest.raises(project_inventory.ProjectInventoryError, match="busy"):
            project_inventory.remember_project(project)


def test_inventory_rejects_a_symlinked_config_directory(tmp_path: Path) -> None:
    config = project_inventory.state_path().parent
    config.parent.mkdir(mode=0o700)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    config.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(project_inventory.ProjectInventoryError, match="must not be a symlink"):
        project_inventory.remember_project(_project(tmp_path / "project"))

    assert not (redirected / "projects.json").exists()


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema": 1},
        {"schema": 2, "projects": []},
        {"schema": 1, "projects": "not-a-list"},
        {"schema": 1, "projects": [12]},
        {"schema": 1, "projects": ["relative/project"]},
        {"schema": 1, "projects": ["/project", "/project"]},
    ],
)
def test_inventory_rejects_invalid_boundary_documents(tmp_path: Path, document: object) -> None:
    project_inventory.remember_project(_project(tmp_path / "project"))
    project_inventory.state_path().write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(project_inventory.ProjectInventoryError, match="unreadable"):
        project_inventory.project_inventory()


def test_purge_uses_the_stored_spelling_of_a_case_folded_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a case-folding OS a case variant names the stored root, so the Grant guard sees it."""
    project = _project(tmp_path / "Project")
    project_inventory.remember_project(project)
    stored = str(project.resolve())
    variant = Path(stored.swapcase())
    monkeypatch.setattr(os.path, "normcase", str.lower)  # the Windows folding rule
    observed: list[str] = []

    forgotten = project_inventory.forget_project(
        variant, before_forget=lambda root: observed.append(str(root)), allow_forgotten=True
    )

    assert str(forgotten) == stored and observed == [stored]
    assert project_inventory.project_inventory() == ()

    project_inventory.remember_project(project)
    _grant(project)
    with pytest.raises(project_inventory.ProjectInventoryError, match="live Project Grant"):
        project_inventory.forget_project(variant, allow_forgotten=True)
    assert project_inventory.project_inventory()[0].remembered


@pytest.mark.parametrize("folder", ["present", "deleted"])
def test_live_grant_refusal_names_the_revoke_command(tmp_path: Path, folder: str) -> None:
    project = _project(tmp_path / "My Project")
    project_inventory.remember_project(project)
    stored = str(project.resolve())
    _grant(project)
    if folder == "deleted":
        shutil.rmtree(project)

    with pytest.raises(project_inventory.ProjectInventoryError) as caught:
        project_inventory.forget_project(project)

    assert str(caught.value) == (
        f"cannot forget {stored}: revoke its live Project Grant first with "
        f"`booley eda grant revoke --kind vivado '{stored}'`"
    )
    assert project_inventory.project_inventory()[0].remembered


def test_unremembered_root_refusal_names_both_fixes(tmp_path: Path) -> None:
    path = tmp_path / "never"

    with pytest.raises(project_inventory.ProjectInventoryError) as caught:
        project_inventory.forget_project(path)

    assert str(caught.value) == (
        f"Project Root is not remembered: {path}; copy the exact path from `booley projects`, "
        "or for a deleted folder pass the exact absolute root to "
        "`booley projects forget --purge`"
    )
