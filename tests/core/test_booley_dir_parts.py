"""Booley Directory path classification: Common vs Checkout Booley State (ADR 0082)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePath, PureWindowsPath

import pytest


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("booley.toml", "versioned"),
        ("cores/ip/deep/x.core", "versioned"),
        ("flow-reports/sim/1/report.json", "source-tied"),
        ("worktrees/s1/file", "common"),
        # T3 Code worktrees live in Common state (#4564).
        ("t3-worktrees/work/t3-1a2b3c4d-0123456789abcdef/rtl/top.sv", "common"),
        (".directory-relocation.json", "common"),  # a dotted name, not a temporary
        (".baseline-wt-1-ab/x", "common"),  # a wildcard pattern
        # The deepest matching pattern wins.
        ("goals/G1/record.json", "common"),
        ("goals/history/G1.md", "versioned"),
        ("runtime/doctor/x", "common"),
        ("runtime/doctor/core-resolve/x", "source-tied"),
        # A Windows spelling classifies like POSIX.
        (PureWindowsPath("goals", "history", "x"), "versioned"),
        (PureWindowsPath("goals", "G1", "x"), "common"),
        # An atomic-write temporary takes the class of the file it replaces.
        ("runtime/.doctor_stamp.json.1234.tmp", "common"),
        (".booley.toml.ab12cd.tmp", "versioned"),
        (".gitignore.ab12cd.tmp", "versioned"),
    ],
)
def test_paths_are_classified(path: str | PurePath, expected: str) -> None:
    """Snapshots copy only Versioned paths and Common state stays in main (ADR 0082, #2849)."""
    from booley.core.booley_dir_parts import (
        BooleyDirClass,
        BooleyDirPart,
        booley_dir_class,
        booley_dir_part,
    )

    assert booley_dir_class(path) is BooleyDirClass(expected)
    part = BooleyDirPart.COMMON if expected == "common" else BooleyDirPart.CHECKOUT
    assert booley_dir_part(path) is part


def test_unclassified_path_is_rejected():
    from booley.core.booley_dir_parts import UnclassifiedBooleyPathError, booley_dir_part

    for path in (
        "not-booley/x",
        "runtime",
        ".runtime",
        "tmp",
        "../booley.toml",
        "/booley.toml",
        ".unknown.1234.tmp",
    ):
        with pytest.raises(UnclassifiedBooleyPathError):
            booley_dir_part(path)


def test_walk_yields_topmost_uniform_entries(tmp_path):
    from booley.core.booley_dir_parts import BooleyDirClass, iter_booley_dir

    def touch(relative: str) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    for relative in (
        "cores/ip/deep/file.core",  # One entry: the whole subtree is versioned.
        "goals/G1/record.json",  # goals/ is Common but goals/history/ is not.
        "goals/history/H1.md",
        "runtime/jobs/j1/state.json",  # runtime/ is a container.
        "runtime/unknown.json",
        "runtime/doctor/core-resolve/x",
        "runtime/doctor/stale.lock",
        "eval_tmp/x",  # Unclassified, no rows below: one entry, not walked.
    ):
        touch(relative)
    (tmp_path / "linked").symlink_to(tmp_path / "cores", target_is_directory=True)
    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "history").mkdir()

    walked = dict(iter_booley_dir(tmp_path))

    assert walked == {
        "cores": BooleyDirClass.VERSIONED,
        "eval_tmp": None,
        "goals/G1": BooleyDirClass.COMMON,
        "goals/history": BooleyDirClass.VERSIONED,
        "linked": None,  # A symlink is a leaf, never followed.
        "runtime/doctor/core-resolve": BooleyDirClass.SOURCE_TIED,
        "runtime/doctor/stale.lock": BooleyDirClass.COMMON,
        "runtime/jobs": BooleyDirClass.COMMON,
        "runtime/unknown.json": None,
        "tickets/history": BooleyDirClass.VERSIONED,
    }
    assert list(walked) == sorted(walked)


def test_copy_snapshot_reports_an_unreadable_source(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

    with pytest.raises(SnapshotError, match="cannot list the Booley Directory for the Snapshot"):
        copy_snapshot(tmp_path / "missing", tmp_path / "copy")


def test_copy_snapshot_names_every_unclassified_path_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.worktree_snapshot import copy_snapshot

    source = tmp_path / "data"
    (source / "eval_tmp").mkdir(parents=True)
    (source / "eval_tmp" / "x").write_text("x\n", encoding="utf-8")
    no_tools = tmp_path / "no-tools"
    no_tools.mkdir()
    monkeypatch.setenv("PATH", str(no_tools))

    assert copy_snapshot(source, tmp_path / "copy") == ["eval_tmp"]


def test_a_configured_directory_never_reclassifies_booley_state(tmp_path: Path) -> None:
    from booley.core.booley_dir_parts import (
        BOOLEY_DIR_PATHS,
        BooleyDirClass,
        iter_booley_dir,
        with_versioned_directory,
    )

    for relative in ("waivers/approved/a.toml", "waivers/other/x"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    rows = with_versioned_directory("waivers/approved")

    assert dict(iter_booley_dir(tmp_path, rows=rows)) == {
        "waivers/approved": BooleyDirClass.VERSIONED,
        "waivers/other": None,
    }
    for name in ("worktrees", "tmp", "cores/waivers"):
        assert with_versioned_directory(name) is BOOLEY_DIR_PATHS


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_copy_snapshot_names_unclassified_paths_and_skips_tool_state(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import copy_snapshot

    source = tmp_path / "data"
    for relative in (
        "eval_tmp/x",
        "cores/ip/x.core",
        "cores/ip/.git/HEAD",
        "cores/ip/rtl.v",
        "cores/ip/.venv/bin/python",
        "cores/ip/__pycache__/m.pyc",
    ):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text("x\n", encoding="utf-8")
    destination = tmp_path / "copy"

    assert copy_snapshot(source, destination) == ["eval_tmp"]

    copied = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    assert [path for path in copied if path.startswith("cores/ip/")] == [
        "cores/ip/rtl.v",
        "cores/ip/x.core",
    ]


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks and FIFOs")
def test_copy_snapshot_replaces_existing_entries_without_following_links(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

    source, destination = tmp_path / "data", tmp_path / "copy"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    for folder in (source / "cores", source / "tools", destination / "cores"):
        folder.mkdir(parents=True)
    (source / "cores" / "run").write_text("new\n", encoding="utf-8")
    (source / "cores" / "link").symlink_to("../rtl")
    os.mkfifo(source / "tools" / "channel")
    (destination / "cores" / "run").symlink_to(outside)
    (destination / "cores" / "link").symlink_to("elsewhere")

    copy_snapshot(source, destination)

    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (destination / "cores" / "run").read_text(encoding="utf-8") == "new\n"
    assert not (destination / "cores" / "run").is_symlink()
    assert (destination / "cores" / "link").readlink() == Path("../rtl")
    assert stat.S_ISFIFO((destination / "tools" / "channel").lstat().st_mode)
    (destination / "cores" / "run").unlink()
    (destination / "cores" / "run").mkdir()
    with pytest.raises(SnapshotError, match="copying the Snapshot failed"):
        copy_snapshot(source, destination)
