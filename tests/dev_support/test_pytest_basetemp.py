"""Orphaned per-checkout pytest basetemps in the shared tools cache are pruned."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests import conftest

from booley.dev_support import agent_readiness, pytest_basetemp


@pytest.fixture
def tools_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the shared tools root at an isolated directory on a POSIX host."""
    root = tmp_path / "agent-tools"
    root.mkdir()
    monkeypatch.setattr(agent_readiness, "agent_tools_root", lambda: root)
    monkeypatch.setattr(pytest_basetemp, "sys", SimpleNamespace(platform="linux"))
    return root


def _record_and_prune(basetemp: Path | str | None, checkout: Path) -> tuple[Path, ...]:
    with pytest_basetemp.record_and_prune(basetemp, checkout) as pruned:
        return pruned


def _checkout(tmp_path: Path, name: str) -> Path:
    checkout = tmp_path / "checkouts" / name
    checkout.mkdir(parents=True)
    return checkout


def _basetemp(tools_root: Path, name: str, owner: Path | str | None) -> Path:
    """Create a populated basetemp folder, recording ``owner`` in its marker."""
    folder = tools_root / f"pytest-basetemp-{name}"
    (folder / "popen-gw0" / "test_x0").mkdir(parents=True)
    if owner is not None:
        folder.with_name(f"{folder.name}.checkout").write_text(f"{owner}\n", encoding="utf-8")
    return folder


def test_marker_records_the_resolved_checkout(tools_root: Path, tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "live")
    own = tools_root / "pytest-basetemp-own"
    _record_and_prune(str(own), checkout / "sub" / "..")
    marker = tools_root / "pytest-basetemp-own.checkout"
    assert marker.read_text(encoding="utf-8").strip() == str(checkout.resolve())


def test_orphan_folder_and_marker_are_pruned(tools_root: Path, tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "live")
    orphan = _basetemp(tools_root, "orphan", tmp_path / "removed-worktree")
    (tools_root / "pytest-basetemp-orphan.lock").touch()
    pruned = _record_and_prune(tools_root / "pytest-basetemp-own", checkout)
    assert pruned == (orphan,)
    assert not orphan.exists()
    assert not (tools_root / "pytest-basetemp-orphan.checkout").exists()
    assert (tools_root / "pytest-basetemp-orphan.lock").exists()


def test_live_unmarked_and_foreign_folders_are_kept(tools_root: Path, tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "live")
    other = _checkout(tmp_path, "other")
    live = _basetemp(tools_root, "live", other)
    unmarked = _basetemp(tools_root, "legacy", None)
    blank = _basetemp(tools_root, "blank", "")
    venv = _basetemp(tools_root, "venv", tmp_path / "gone")
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    repo = _basetemp(tools_root, "repo", tmp_path / "gone")
    (repo / ".git").mkdir()
    binaries = _basetemp(tools_root, "bin", tmp_path / "gone")
    (binaries / "bin").mkdir()
    assert _record_and_prune(tools_root / "pytest-basetemp-own", checkout) == ()
    for folder in (live, unmarked, blank, venv, repo, binaries):
        assert (folder / "popen-gw0" / "test_x0").is_dir(), folder


def test_own_folder_is_kept_even_if_its_marker_names_a_missing_checkout(
    tools_root: Path, tmp_path: Path
) -> None:
    # A stale marker from a checkout that was moved must not delete this run's root.
    own = _basetemp(tools_root, "own", tmp_path / "moved-away")
    checkout = _checkout(tmp_path, "live")
    assert _record_and_prune(own, checkout) == ()
    assert own.is_dir()


@pytest.mark.parametrize("failure", ["delete", "marker-write"])
def test_io_failures_do_not_fail_the_session(
    tools_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    orphan = _basetemp(tools_root, "orphan", tmp_path / "gone")

    def refuse(path: Path) -> None:
        raise PermissionError(f"denied: {path}")

    def full_disk(self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device")

    if failure == "delete":
        monkeypatch.setattr(pytest_basetemp.shutil, "rmtree", refuse)
    else:  # a disk out of inodes must not crash every session: pruning still frees space
        monkeypatch.setattr(Path, "replace", full_disk)
    checkout = _checkout(tmp_path, "live")
    pruned = _record_and_prune(tools_root / "pytest-basetemp-own", checkout)
    names = sorted(path.name for path in tools_root.iterdir())
    if failure == "delete":
        assert pruned == ()
        # The marker survives so a later session retries the deletion.
        assert (tools_root / "pytest-basetemp-orphan.checkout").is_file()
    else:
        assert pruned == (orphan,)
        # Neither the marker nor its staging file is left behind; the session lock stays.
        assert names == ["pytest-basetemp-orphan.lock", "pytest-basetemp-own.lock"]


def test_folder_is_kept_when_its_checkout_cannot_be_checked(
    tools_root: Path, tmp_path: Path
) -> None:
    # An unreadable parent makes stat fail with EACCES: the owner is unknown, not gone.
    locked = tmp_path / "locked"
    (locked / "worktree").mkdir(parents=True)
    held = _basetemp(tools_root, "held", locked / "worktree")
    locked.chmod(0)
    try:
        if os.access(locked, os.X_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        checkout = _checkout(tmp_path, "live")
        assert _record_and_prune(tools_root / "pytest-basetemp-own", checkout) == ()
    finally:
        locked.chmod(0o700)
    assert held.is_dir()


def test_symlinked_folder_is_never_followed_or_pruned(tools_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    (target / "keep").mkdir(parents=True)
    link = tools_root / "pytest-basetemp-link"
    link.symlink_to(target, target_is_directory=True)
    (tools_root / "pytest-basetemp-link.checkout").write_text(
        f"{tmp_path / 'gone'}\n", encoding="utf-8"
    )
    checkout = _checkout(tmp_path, "live")
    assert _record_and_prune(tools_root / "pytest-basetemp-own", checkout) == ()
    assert link.is_symlink()
    assert (target / "keep").is_dir()


def test_marker_without_folder_is_removed_but_not_reported(
    tools_root: Path, tmp_path: Path
) -> None:
    stale = tools_root / "pytest-basetemp-stale.checkout"
    stale.write_text(f"{tmp_path / 'gone'}\n", encoding="utf-8")
    checkout = _checkout(tmp_path, "live")
    assert _record_and_prune(tools_root / "pytest-basetemp-own", checkout) == ()
    assert not stale.exists()


@pytest.mark.parametrize(
    ("platform", "basetemp"),
    [
        ("win32", "tools"),
        ("linux", None),
        ("linux", "elsewhere"),
        ("linux", "unprefixed"),
    ],
)
def test_noop_off_posix_or_without_tools_root_basetemp(
    tools_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    basetemp: str | None,
) -> None:
    monkeypatch.setattr(pytest_basetemp.sys, "platform", platform)
    orphan = _basetemp(tools_root, "orphan", tmp_path / "gone")
    chosen = {
        "tools": tools_root / "pytest-basetemp-own",
        "elsewhere": tmp_path / "manual" / "pytest-basetemp-own",
        "unprefixed": tools_root / "manual-run",
        None: None,
    }[basetemp]
    checkout = _checkout(tmp_path, "live")
    assert _record_and_prune(chosen, checkout) == ()
    assert orphan.is_dir()
    assert sorted(path.name for path in tools_root.iterdir()) == [
        "pytest-basetemp-orphan",
        "pytest-basetemp-orphan.checkout",
    ]


@pytest.mark.parametrize(("worker", "pruned"), [(False, True), (True, False)])
def test_only_the_pytest_controller_prunes(
    tools_root: Path, tmp_path: Path, worker: bool, pruned: bool
) -> None:
    orphan = _basetemp(tools_root, "orphan", tmp_path / "gone")
    option = SimpleNamespace(basetemp=str(tools_root / "pytest-basetemp-own"))
    with ExitStack() as stack:
        config = SimpleNamespace(
            option=option, add_cleanup=stack.callback, **({"workerinput": {}} if worker else {})
        )
        conftest.prune_orphan_basetemps(config)  # type: ignore[arg-type]
        assert orphan.exists() is not pruned


@pytest.mark.skipif(sys.platform == "win32", reason="Windows runs use pytest's default basetemp")
def test_pruner_leaves_a_locked_basetemp_until_its_session_ends(
    tools_root: Path, tmp_path: Path
) -> None:
    checkout = _checkout(tmp_path, "removed")
    live = _basetemp(tools_root, "live", checkout)
    other = tools_root / "pytest-basetemp-other"
    with pytest_basetemp.record_and_prune(live, checkout):
        checkout.rmdir()
        assert _record_and_prune(other, tmp_path) == ()
        assert live.is_dir()
        with pytest.raises(pytest_basetemp.BasetempLockError, match="another pytest session"):
            _record_and_prune(live, checkout)
    assert _record_and_prune(other, tmp_path) == (live,)
    assert not live.exists()


def test_unlockable_basetemp_is_refused(tools_root: Path, tmp_path: Path) -> None:
    (tools_root / "pytest-basetemp-own.lock").mkdir()
    with pytest.raises(pytest_basetemp.BasetempLockError, match="could not lock pytest"):
        _record_and_prune(tools_root / "pytest-basetemp-own", tmp_path)
    assert not (tools_root / "pytest-basetemp-own.checkout").exists()
