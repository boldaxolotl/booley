"""Host-owned Remembered Project Root inventory contracts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from booley.eda.provisioning import authority
from booley.eda.provisioning.policies.vivado import Inspection
from booley.projects import inventory as project_inventory
from booley.runtime import private_store


def test_remembered_initialized_project_is_listed_as_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()

    assert project_inventory.remember_project(project) == project.resolve()

    assert project_inventory.project_inventory() == (
        project_inventory.ProjectInventoryEntry(
            project_root=str(project.resolve()),
            status=project_inventory.ProjectStatus.PRESENT,
            remembered=True,
            grants=(),
        ),
    )


def test_inventory_joins_grants_and_keeps_deleted_root_visible(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        authority,
        "inspect_installation",
        lambda source, project_root=None: Inspection(source.resolve(), "2025.2", "linux-x86_64"),
    )
    project = tmp_path / "project"
    source = tmp_path / "Xilinx" / "2025.2"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()
    source.mkdir(parents=True)
    project_inventory.remember_project(project)
    authority.register_installation("vivado_2025_2", "vivado", source)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")
    shutil.rmtree(project)

    assert project_inventory.project_inventory() == (
        project_inventory.ProjectInventoryEntry(
            project_root=str(project.resolve()),
            status=project_inventory.ProjectStatus.MISSING,
            remembered=True,
            grants=(
                project_inventory.ProjectGrantSummary(
                    kind="vivado",
                    installation="vivado_2025_2",
                    license_profile=None,
                ),
            ),
        ),
    )


def test_inventory_includes_a_grant_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        authority,
        "inspect_installation",
        lambda source, project_root=None: Inspection(source.resolve(), "2025.2", "linux-x86_64"),
    )
    project = tmp_path / "project"
    source = tmp_path / "Xilinx" / "2025.2"
    (project / ".git").mkdir(parents=True)
    source.mkdir(parents=True)
    authority.register_installation("vivado_2025_2", "vivado", source)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")

    entry = project_inventory.project_inventory()[0]

    assert entry.project_root == str(project.resolve())
    assert entry.remembered is False
    assert entry.status is project_inventory.ProjectStatus.UNINITIALIZED
    assert entry.grants[0].installation == "vivado_2025_2"


def test_discovery_remembers_projects_and_prunes_their_subtrees(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    search_root = tmp_path / "workplace"
    project = search_root / "project"
    nested_fixture = project / "tests" / "fixture"
    second = search_root / "second"
    for root in (project, nested_fixture, second):
        (root / ".git").mkdir(parents=True)
        (root / ".booley_project").mkdir()

    assert project_inventory.discover_projects((search_root,)) == (
        project.resolve(),
        second.resolve(),
    )

    assert [entry.project_root for entry in project_inventory.project_inventory()] == [
        str(project.resolve()),
        str(second.resolve()),
    ]


def test_forget_removes_a_missing_remembered_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()
    project_inventory.remember_project(project)
    shutil.rmtree(project)

    assert project_inventory.forget_project(project) == project

    assert project_inventory.project_inventory() == ()


def test_forget_refuses_a_root_with_a_live_grant(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        authority,
        "inspect_installation",
        lambda source, project_root=None: Inspection(source.resolve(), "2025.2", "linux-x86_64"),
    )
    project = tmp_path / "project"
    source = tmp_path / "Xilinx" / "2025.2"
    (project / ".git").mkdir(parents=True)
    (project / ".booley_project").mkdir()
    source.mkdir(parents=True)
    project_inventory.remember_project(project)
    authority.register_installation("vivado_2025_2", "vivado", source)
    authority.add_grant(project, "vivado", installation="vivado_2025_2")

    with pytest.raises(project_inventory.ProjectInventoryError, match="live Project Grant"):
        project_inventory.forget_project(project)

    assert project_inventory.project_inventory()[0].remembered is True


def test_inventory_rejects_a_symlinked_state_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    inventory_dir = tmp_path / "config" / "booley"
    inventory_dir.mkdir(parents=True, mode=0o700)
    inventory_dir.parent.chmod(0o700)
    inventory_dir.chmod(0o700)
    redirected = tmp_path / "redirected.json"
    redirected.write_text(json.dumps({"schema": 1, "projects": []}), encoding="utf-8")
    (inventory_dir / "projects.json").symlink_to(redirected)

    with pytest.raises(project_inventory.ProjectInventoryError, match="must not be a symlink"):
        project_inventory.project_inventory()


def test_inventory_lock_contention_has_a_bounded_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".booley_project").mkdir(parents=True)
    monkeypatch.setattr(
        private_store,
        "acquire_file_lock",
        lambda _lock: (_ for _ in ()).throw(BlockingIOError()),
    )
    times = iter((0.0, 11.0))
    monkeypatch.setattr(private_store.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(private_store.time, "sleep", lambda _seconds: None)

    with pytest.raises(project_inventory.ProjectInventoryError, match="busy"):
        project_inventory.remember_project(project)


def test_inventory_rejects_a_symlinked_config_directory(tmp_path: Path, monkeypatch) -> None:
    platform_root = tmp_path / "config"
    platform_root.mkdir(mode=0o700)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    (platform_root / "booley").symlink_to(redirected, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(platform_root))
    project = tmp_path / "project"
    (project / ".booley_project").mkdir(parents=True)

    with pytest.raises(project_inventory.ProjectInventoryError, match="must not be a symlink"):
        project_inventory.remember_project(project)

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
def test_inventory_rejects_invalid_boundary_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    inventory_dir = tmp_path / "config" / "booley"
    inventory_dir.mkdir(parents=True, mode=0o700)
    inventory_dir.parent.chmod(0o700)
    inventory_dir.chmod(0o700)
    state = inventory_dir / "projects.json"
    state.write_text(json.dumps(document), encoding="utf-8")
    state.chmod(0o600)

    with pytest.raises(project_inventory.ProjectInventoryError, match="unreadable"):
        project_inventory.project_inventory()


def test_inventory_wraps_authority_store_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        authority,
        "load_state",
        lambda: (_ for _ in ()).throw(authority.AuthorityError("corrupt authority")),
    )

    with pytest.raises(
        project_inventory.ProjectInventoryError, match="cannot read Project Grants"
    ):
        project_inventory.project_inventory()
