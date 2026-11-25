"""Root AGENTS.md/CLAUDE.md links to the canonical guidance file.

The Booley Directory is bind-mounted at ``/booley`` inside the Session Runtime,
outside the workspace, so a link must name the repo-local path to read on the
host too (F-13). Tests not marked POSIX-only run on Windows, where the links
fall back to hardlinks or copies.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.harness.setup.guidance_links import (
    CANON_NAME,
    LINK_NAMES,
    ensure_guidance_links,
    guidance_entry_current,
    plan_guidance_links,
)
from booley.runtime.devcontainer import devcontainer_path
from booley.runtime.init_plan import Ownership


def _make_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (project_root, booley_dir, canon) with a git repo and canon file."""
    project_root = tmp_path / "repo"
    booley_dir = project_root / ".booley"
    booley_dir.mkdir(parents=True)
    (project_root / ".git" / "info").mkdir(parents=True)
    canon = booley_dir / CANON_NAME
    canon.write_text("# AGENTS\n", encoding="utf-8")
    return project_root, booley_dir, canon


def _dangling_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")


def test_creates_both_links_readable_from_the_host_and_reruns_as_current(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)

    links = ensure_guidance_links(project_root, booley_dir)

    assert {p.name for p in links} == set(LINK_NAMES)
    for link in links:
        assert link.read_text(encoding="utf-8") == "# AGENTS\n"
        if sys.platform != "win32":
            # Relative to the repo-local canon, so it survives a relocated checkout.
            assert link.readlink() == Path(".booley") / CANON_NAME
    rerun = plan_guidance_links(project_root, booley_dir)
    assert {action.ownership for action in rerun.actions} == {Ownership.MANAGED_CURRENT}


@pytest.mark.parametrize("layout", ["repo-local", "sandbox-mount", "relocated", "outside"])
def test_links_name_the_checkouts_own_booley_directory(tmp_path: Path, layout: str):
    """Name ``<own Booley Directory>/AGENTS.md``, never ``../booley/AGENTS.md`` (F-13)."""
    project_root = tmp_path / "repo"
    (project_root / ".git" / "info").mkdir(parents=True)
    own = project_root / ("state" if layout == "relocated" else ".booley")
    if layout == "relocated":
        (project_root / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    if layout == "outside":
        own = tmp_path / "elsewhere"
        (project_root / "booley.toml").write_text(
            f'[project]\ndir = "{own.as_posix()}"\n', encoding="utf-8"
        )
    own.mkdir(parents=True)
    (own / CANON_NAME).write_text("# AGENTS\n", encoding="utf-8")
    selected = own
    if layout in {"sandbox-mount", "relocated"}:
        selected = tmp_path / "booley"  # the directory as the Sandbox mounts it
        selected.mkdir()
        (selected / CANON_NAME).write_text("# AGENTS\n", encoding="utf-8")

    plan = plan_guidance_links(project_root, selected)

    expected = own.resolve() if layout == "outside" else own
    assert {target.link_target for target in plan.request.targets} == {expected / CANON_NAME}


@pytest.mark.parametrize("entry", ["hardlink", "copy", "absent"])
def test_only_a_link_to_the_canon_is_current(tmp_path: Path, entry: str):
    """A hardlink resolves to itself, so it is recognized by identity; a copy goes stale."""
    canon = tmp_path / CANON_NAME
    canon.write_text("x\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    if entry == "hardlink":
        try:
            os.link(canon, link)
        except OSError:
            pytest.skip("hardlinks not supported here")
    elif entry == "copy":
        link.write_text("x\n", encoding="utf-8")

    assert guidance_entry_current(tmp_path, link, canon.resolve()) is (entry == "hardlink")


def test_refuses_foreign_untracked_root_file(tmp_path: Path):
    project_root, booley_dir, canon = _make_project(tmp_path)
    foreign = project_root / "AGENTS.md"
    foreign.write_text("project-specific instructions\n", encoding="utf-8")

    with pytest.raises(OSError, match="foreign untracked guidance entry"):
        ensure_guidance_links(project_root, booley_dir)

    assert foreign.read_text(encoding="utf-8") == "project-specific instructions\n"
    assert not foreign.is_symlink()
    assert canon.read_text(encoding="utf-8") == "# AGENTS\n"


def test_blocker_prevents_partial_guidance_link_creation(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)
    claude = project_root / "CLAUDE.md"
    claude.write_text("mine\n", encoding="utf-8")

    plan = plan_guidance_links(project_root, booley_dir)

    assert [action.path.name for action in plan.blockers] == ["CLAUDE.md"]
    with pytest.raises(OSError, match="foreign untracked guidance entry"):
        ensure_guidance_links(project_root, booley_dir, plan=plan)
    assert not (project_root / "AGENTS.md").exists()
    assert claude.read_text(encoding="utf-8") == "mine\n"


def test_init_records_guidance_precondition_change_as_error(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)
    plan = plan_guidance_links(project_root, booley_dir)
    foreign = project_root / "AGENTS.md"
    foreign.write_text("changed after planning\n", encoding="utf-8")
    ctx = InitContext(project_root=project_root)

    # private: `booley init` plans and applies in one call, so only the step itself
    # can be handed a plan the filesystem changed under.
    init_cmd._step_guidance_links(ctx, plan)

    assert [(result.name, result.status, result.detail) for result in ctx.results] == [
        ("guidance_links", "err", "filesystem precondition changed")
    ]
    assert foreign.read_text(encoding="utf-8") == "changed after planning\n"
    assert not (project_root / "CLAUDE.md").exists()


def test_preserves_matching_tracked_root_file(tmp_path: Path):
    project_root, booley_dir, canon = _make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    root_agents = project_root / "AGENTS.md"
    root_agents.write_bytes(canon.read_bytes())
    subprocess.run(["git", "add", "AGENTS.md"], cwd=project_root, check=True)

    ensure_guidance_links(project_root, booley_dir)

    assert root_agents.is_file()
    assert not root_agents.is_symlink()
    assert root_agents.read_bytes() == canon.read_bytes()
    assert (project_root / "CLAUDE.md").read_bytes() == canon.read_bytes()


def test_refuses_to_overwrite_stale_tracked_root_file(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    root_agents = project_root / "AGENTS.md"
    root_agents.write_text("# TRACKED PROJECT GUIDANCE\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=project_root, check=True)

    with pytest.raises(OSError, match="tracked root guidance file differs"):
        ensure_guidance_links(project_root, booley_dir)

    assert root_agents.read_text(encoding="utf-8") == "# TRACKED PROJECT GUIDANCE\n"
    assert not root_agents.is_symlink()


def test_git_info_exclude_gains_each_link_once(tmp_path: Path):
    project_root, booley_dir, _ = _make_project(tmp_path)
    exclude_path = project_root / ".git" / "info" / "exclude"
    exclude_path.write_text("# pre-existing\n/build\n", encoding="utf-8")

    ensure_guidance_links(project_root, booley_dir)
    ensure_guidance_links(project_root, booley_dir)

    lines = exclude_path.read_text(encoding="utf-8").splitlines()
    assert "/build" in lines
    assert lines.count("/AGENTS.md") == lines.count("/CLAUDE.md") == 1


@pytest.mark.parametrize("recorded_in", ["mount-history", "issued-spec"])
def test_repairs_a_dangling_link_through_a_recorded_former_mount(tmp_path: Path, recorded_in):
    """The F-13 breakage: a link left pointing through a former Sandbox mount."""
    project_root, booley_dir, _canon = _make_project(tmp_path)
    if recorded_in == "mount-history":
        history = booley_dir / ".runtime" / "sandbox-mounts.json"
        history.parent.mkdir()
        history.write_text(json.dumps({"schema": 1, "mounts": ["/former-mount"]}))
    else:
        spec = devcontainer_path(project_root)
        spec.parent.mkdir(parents=True)
        spec.write_text(json.dumps({"remoteEnv": {"BOOLEY_DIR": "/former-mount"}}))
    link = project_root / "AGENTS.md"
    _dangling_link(link, Path("..") / "former-mount" / CANON_NAME)

    assert not link.exists()  # dangling, as seen from the host
    ensure_guidance_links(project_root, booley_dir)
    assert link.read_text(encoding="utf-8") == "# AGENTS\n"


def test_reports_an_unreadable_mount_history_as_an_os_error(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)
    history = booley_dir / ".runtime" / "sandbox-mounts.json"
    history.parent.mkdir()
    history.write_text("not json", encoding="utf-8")

    with pytest.raises(OSError, match="malformed mount history"):
        ensure_guidance_links(project_root, booley_dir)


def test_preserves_an_unrelated_dangling_link(tmp_path: Path):
    project_root, booley_dir, _canon = _make_project(tmp_path)
    link = project_root / "AGENTS.md"
    _dangling_link(link, Path("..") / "temporarily-unmounted" / CANON_NAME)
    original = link.readlink()

    with pytest.raises(OSError, match="foreign untracked guidance entry"):
        ensure_guidance_links(project_root, booley_dir)

    assert link.is_symlink()
    assert link.readlink() == original
    assert not (project_root / "CLAUDE.md").exists()


def test_refuses_a_foreign_stale_copy(tmp_path: Path):
    """An untracked regular file is user-owned unless positively recognized."""
    project_root, booley_dir, _canon = _make_project(tmp_path)
    link = project_root / "AGENTS.md"
    link.write_text("# STALE\n", encoding="utf-8")

    with pytest.raises(OSError, match="foreign untracked guidance entry"):
        ensure_guidance_links(project_root, booley_dir)
    assert link.read_text(encoding="utf-8") == "# STALE\n"


def test_missing_canon_raises(tmp_path: Path):
    project_root = tmp_path / "repo"
    booley_dir = project_root / ".booley"
    booley_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        ensure_guidance_links(project_root, booley_dir)
