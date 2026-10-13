"""Snapshot category removal cannot damage live or own-category sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.agent_workspace.isolation import WorkspaceSnapshot, hide_opposite_sources
from tests.conftest import symlink_or_skip


@pytest.fixture
def snapshot(tmp_path: Path) -> WorkspaceSnapshot:
    live, copy = tmp_path / "live", tmp_path / "copy"
    for root in (live, copy):
        for directory in ("rtl", "tb", "data"):
            (root / directory).mkdir(parents=True)
            (root / directory / "source.sv").write_text(f"// {directory}\n")
    return WorkspaceSnapshot(live, copy)


@pytest.mark.parametrize("category,hidden,visible", [("rtl", "tb", "rtl"), ("tb", "rtl", "tb")])
def test_removes_only_opposite_copy(snapshot, category, hidden, visible):
    categories = {"rtl": ("rtl/", "data/"), "tb": ("tb/", "data/")}
    assert hide_opposite_sources(snapshot, category, category_dirs=categories) == [hidden]
    assert not (snapshot.snapshot_root / hidden).exists()
    assert (snapshot.snapshot_root / visible / "source.sv").is_file()
    assert (snapshot.snapshot_root / "data/source.sv").is_file()
    assert (snapshot.real_root / hidden / "source.sv").read_text() == f"// {hidden}\n"


def test_flat_and_nested_sources_and_discovery_links(snapshot):
    root = snapshot.snapshot_root
    (root / "testbench.v").write_text("// flat TB\n")
    nested = root / ".booley_project/scratch/case/tb"
    nested.mkdir(parents=True)
    (nested / "nested.sv").write_text("// nested TB\n")
    cores = root / ".booley_project/cores"
    cores.mkdir()
    symlink_or_skip(cores / "tb", "../../tb", target_is_directory=True)
    symlink_or_skip(cores / "rtl", "../../rtl", target_is_directory=True)
    hide_opposite_sources(
        snapshot,
        "rtl",
        category_dirs={
            "rtl": ("rtl/",),
            "tb": ("tb/", "testbench.v", ".booley_project/cores/tb/"),
        },
    )
    assert not (root / "testbench.v").exists()
    assert not nested.exists()
    assert not (cores / "tb").is_symlink()
    assert (cores / "rtl/source.sv").is_file()
    assert (snapshot.real_root / "tb/source.sv").is_file()


@pytest.mark.parametrize("prefix", [".", "./", "../live/tb/", "/", ".git/"])
def test_invalid_prefix_rejected_before_removing_any_sources(snapshot, prefix):
    with pytest.raises(ValueError):
        hide_opposite_sources(
            snapshot, "rtl", category_dirs={"rtl": ("rtl/",), "tb": ("tb/", prefix)}
        )
    assert (snapshot.snapshot_root / "tb/source.sv").is_file()
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_absolute_live_path_rejected(snapshot):
    with pytest.raises(ValueError):
        hide_opposite_sources(
            snapshot,
            "rtl",
            category_dirs={"rtl": ("rtl/",), "tb": (str(snapshot.real_root / "tb"),)},
        )
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_symlink_ancestor_cannot_delete_live_sources(snapshot):
    root = snapshot.snapshot_root
    symlink_or_skip(root / "outside", snapshot.real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="parent escapes"):
        hide_opposite_sources(
            snapshot, "rtl", category_dirs={"rtl": ("rtl/",), "tb": ("outside/tb/",)}
        )
    assert (snapshot.real_root / "tb/source.sv").is_file()


def test_removing_symlink_does_not_follow_live_target(snapshot):
    root = snapshot.snapshot_root
    symlink_or_skip(root / "outside", snapshot.real_root / "tb", target_is_directory=True)
    hide_opposite_sources(snapshot, "rtl", category_dirs={"rtl": ("rtl/",), "tb": ("outside/",)})
    assert not (root / "outside").is_symlink()
    assert (snapshot.real_root / "tb/source.sv").is_file()


@pytest.mark.parametrize(
    "real,copy", [("live", "live"), ("live", "live/copy"), ("live/copy", "live")]
)
def test_overlapping_snapshot_roots_rejected(tmp_path, real, copy):
    with pytest.raises(ValueError, match="separate"):
        hide_opposite_sources(
            WorkspaceSnapshot(tmp_path / real, tmp_path / copy),
            "rtl",
            category_dirs={"rtl": ("rtl/",), "tb": ("tb/",)},
        )


def test_own_category_ancestor_rejected(snapshot):
    with pytest.raises(ValueError, match="own-category"):
        hide_opposite_sources(
            snapshot, "rtl", category_dirs={"rtl": ("src/rtl/",), "tb": ("src/",)}
        )


def test_agent_cwd_removal_rejected(snapshot):
    with pytest.raises(ValueError, match="agent cwd"):
        hide_opposite_sources(
            snapshot,
            "rtl",
            category_dirs={"rtl": ("rtl/",), "tb": ("tb/",)},
            cwd=snapshot.snapshot_root / "tb",
        )
    assert (snapshot.snapshot_root / "tb/source.sv").is_file()


def test_copied_artifact_sources_are_hidden_without_touching_live_artifacts(snapshot):
    relative = Path(".booley_project/.runtime/edalize/sim/tb/source.sv")
    for root in (snapshot.real_root, snapshot.snapshot_root):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("// copied TB\n")
    hide_opposite_sources(snapshot, "rtl", category_dirs={"rtl": ("rtl/",), "tb": ("tb/",)})
    assert not (snapshot.snapshot_root / relative).exists()
    assert (snapshot.real_root / relative).read_text() == "// copied TB\n"
