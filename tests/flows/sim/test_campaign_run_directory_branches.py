"""Edge branches of the run-directory parent and run-lock handling (#1971)."""

from __future__ import annotations

import dataclasses
import types
from pathlib import Path

import pytest

from booley.flows.sim.campaign import run_directory as rd
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from booley.runtime.file_lock import LockTimeoutError

PARENT_MARKER = ".booley-simulation-parent.json"


def _run(root: Path, test: str = "t") -> tuple[rd.RunDirectory, dict[str, str]]:
    run = rd.expand_run_directory(
        "work/{campaign}/{test}/{attempt}",
        project_root=root,
        campaign_id="c",
        target_key="sim",
        work_item_key=test,
        attempt_key="a1",
    )
    return run, {"campaign_id": "c", "work_item_id": test, "attempt_id": "a1"}


def _make_marked_parent(root: Path) -> Path:
    parent = root / "work/c"
    parent.mkdir(parents=True)
    (parent / PARENT_MARKER).write_text("{}", encoding="utf-8")
    return parent


def test_template_root_of_a_literal_path_is_none(tmp_path: Path) -> None:
    assert rd._template_root("literal/dir", tmp_path / "literal/dir") is None


def test_lock_wait_timeout_closes_handle_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "x.lock"
    held = rd._acquire_current_lock(lock_path)
    monkeypatch.setattr(rd, "_LOCK_TIMEOUT_S", 0.05)
    try:
        with pytest.raises(LockTimeoutError):
            rd._acquire_current_lock(lock_path)
    finally:
        held.close()


def test_lock_that_never_stays_current_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(rd, "_lock_is_current", lambda lock, path: False)
    with pytest.raises(LockTimeoutError, match="timed out waiting"):
        rd._acquire_current_lock(tmp_path / "x.lock")


def test_windows_discard_unlinks_after_close_and_keeps_a_busy_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "sys", types.SimpleNamespace(platform="win32"))
    lock_path = tmp_path / "x.lock"
    with rd._held_path_lock(lock_path, discard=True):
        pass
    assert not lock_path.exists()

    real_unlink = Path.unlink

    def busy(self: Path, missing_ok: bool = False) -> None:
        if self == lock_path:
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy)
    with rd._held_path_lock(lock_path, discard=True):
        pass
    assert lock_path.exists()


def test_run_without_template_root_creates_and_cleans_only_the_attempt(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    run = dataclasses.replace(run, template_root=None)
    with rd.claimed_run_directory(run, identity=identity) as directory:
        assert (directory / ".booley-simulation-attempt.json").is_file()
    assert not run.path.exists()
    assert (tmp_path / "work/c/t").is_dir()  # unrecorded parents stay


def test_cleanup_without_template_root_leaves_parents(tmp_path: Path) -> None:
    run, _ = _run(tmp_path)
    parent = _make_marked_parent(tmp_path)
    rd._remove_booley_parents(dataclasses.replace(run, template_root=None))
    assert parent.is_dir()


def test_creation_restarts_when_an_ancestor_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path)
    real = rd._create_marked_chain
    calls: list[int] = []

    def flaky(*args: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise FileNotFoundError("vanished")
        real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(rd, "_create_marked_chain", flaky)
    with rd.claimed_run_directory(run, identity=identity) as directory:
        assert directory.is_dir()
    assert len(calls) == 2


def test_creation_gives_up_after_repeated_vanishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path)

    def always_vanishes(*args: object) -> None:
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(rd, "_create_marked_chain", always_vanishes)
    with (
        pytest.raises(SimulationCampaignIntegrityError, match="could not be created"),
        rd.claimed_run_directory(run, identity=identity),
    ):
        pytest.fail("body must not run")


def test_linked_parent_is_never_removed(tmp_path: Path) -> None:
    run, _ = _run(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / PARENT_MARKER).write_text("{}", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "work/c").symlink_to(target, target_is_directory=True)
    rd._remove_booley_parents(run)
    assert (target / PARENT_MARKER).is_file()


def test_parent_with_non_file_marker_is_kept(tmp_path: Path) -> None:
    run, _ = _run(tmp_path)
    parent = tmp_path / "work/c"
    (parent / PARENT_MARKER).mkdir(parents=True)
    rd._remove_booley_parents(run)
    assert (parent / PARENT_MARKER).is_dir()


def test_parent_removal_stops_when_rmdir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _run(tmp_path)
    parent = _make_marked_parent(tmp_path)

    def refuse(self: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(Path, "rmdir", refuse)
    rd._remove_booley_parents(run)
    assert parent.is_dir()


def test_interrupted_cleanup_ignores_an_unowned_run(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    literal = dataclasses.replace(run, owned=False, template_root=None)
    assert rd.cleanup_interrupted_run_directory(literal, identity=identity) is False
