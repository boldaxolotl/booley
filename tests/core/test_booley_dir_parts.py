"""Booley Directory path classification: Common vs Checkout Booley State (ADR 0082)."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePath, PureWindowsPath

import pytest


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("booley.toml", "versioned"),
        ("cores/ip/deep/x.core", "versioned"),
        ("flow-reports/sim/1/report.json", "source-tied"),
        ("worktrees/s1/file", "common"),
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


def test_snapshot_main_reports_an_unreadable_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime import worktree_snapshot

    status = worktree_snapshot.main([str(tmp_path / "missing"), str(tmp_path / "list")])

    assert status == 1
    assert "cannot list the Booley Directory for the Snapshot" in capsys.readouterr().err


def test_snapshot_main_names_every_unclassified_path_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime import worktree_snapshot

    source = tmp_path / "data"
    (source / "eval_tmp").mkdir(parents=True)
    (source / "eval_tmp" / "x").write_text("x\n", encoding="utf-8")
    no_tools = tmp_path / "no-tools"
    no_tools.mkdir()
    monkeypatch.setenv("PATH", str(no_tools))

    status = worktree_snapshot.main([str(source), str(tmp_path / "list")])

    assert status == 0
    assert "Snapshot skipped eval_tmp" in capsys.readouterr().err


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
def test_snapshot_main_names_every_unclassified_path_outside_a_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime import worktree_snapshot

    source = tmp_path / "data"
    (source / "eval_tmp").mkdir(parents=True)
    (source / "eval_tmp" / "x").write_text("x\n", encoding="utf-8")

    assert worktree_snapshot.main([str(source), str(tmp_path / "list")]) == 0
    assert "Snapshot skipped eval_tmp" in capsys.readouterr().err
