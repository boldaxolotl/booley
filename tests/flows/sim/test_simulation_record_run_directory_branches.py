"""Edge branches of the run-directory parent and run-lock handling (#1971)."""

from __future__ import annotations

import dataclasses
import os
import types
from pathlib import Path

import pytest

from booley.flows.sim.record import run_directory as rd
from booley.flows.sim.record.codec import SimulationRecordIntegrityError, canonical_json_bytes
from booley.runtime.file_lock import LockTimeoutError
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.mount_change_support import simulate_mount_change

PARENT_MARKER = ".booley-simulation-parent.json"


def _run(root: Path, test: str = "t") -> tuple[rd.RunDirectory, dict[str, str]]:
    run = rd.expand_run_directory(
        "work/{record}/{test}/{attempt}",
        project_root=root,
        simulation_record_id="c",
        target_key="sim",
        work_item_key=test,
        attempt_key="a1",
    )
    return run, {"simulation_record_id": "c", "work_item_id": test, "attempt_id": "a1"}


def _make_marked_parent(root: Path) -> Path:
    parent = root / "work/c"
    parent.mkdir(parents=True)
    (parent / PARENT_MARKER).write_text("{}", encoding="utf-8")
    return parent


def test_template_root_of_a_literal_path_is_none(tmp_path: Path) -> None:
    """A restored owned document without placeholders has no template root to keep."""
    path = tmp_path.resolve() / "literal/dir"
    document = {
        "configured": "literal/dir",
        "resolved": str(path),
        "collision_key": os.path.normcase(str(path)),
        "owned": True,
    }

    run = rd.restore_run_directory_from_project_data(document, project_data=tmp_path.resolve())

    assert run.template_root is None


def test_lock_wait_timeout_closes_handle_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path)
    # Private seam: the production wait is 60 s; only a lowered timeout lets a
    # held lock expire inside a test.
    monkeypatch.setattr(rd, "_LOCK_TIMEOUT_S", 0.05)
    with rd.claimed_run_directory(run, identity=identity), pytest.raises(LockTimeoutError):
        rd.cleanup_interrupted_run_directory(run, identity=identity)
    # The timed-out waiter closed its handle: the released lock is free again.
    assert rd.cleanup_interrupted_run_directory(run, identity=identity) is False


def test_lock_that_never_stays_current_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path)
    # Private seams: the timeout is lowered for speed, and every retry must see
    # a replaced lock file, which no real filesystem produces on demand.
    monkeypatch.setattr(rd, "_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(rd, "_lock_is_current", lambda lock, path: False)
    with (
        pytest.raises(LockTimeoutError, match="timed out waiting"),
        rd.claimed_run_directory(run, identity=identity),
    ):
        pytest.fail("body must not run")


def test_windows_discard_unlinks_after_close_and_keeps_a_busy_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "sys", types.SimpleNamespace(platform="win32"))
    run, identity = _run(tmp_path)
    assert rd.cleanup_interrupted_run_directory(run, identity=identity) is False
    assert not run.lock_path.exists()

    real_unlink = Path.unlink

    def busy(self: Path, missing_ok: bool = False) -> None:
        if self == run.lock_path:
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy)
    assert rd.cleanup_interrupted_run_directory(run, identity=identity) is False
    assert run.lock_path.exists()


def test_run_without_template_root_creates_and_cleans_only_the_attempt(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    run = dataclasses.replace(run, template_root=None)
    # A marked, otherwise empty parent: only a template root lets cleanup remove it.
    marked = tmp_path / "work/c/t"
    marked.mkdir(parents=True)
    (marked / PARENT_MARKER).write_text("{}", encoding="utf-8")
    with rd.claimed_run_directory(run, identity=identity) as directory:
        assert (directory / ".booley-simulation-attempt.json").is_file()
    assert not run.path.exists()
    assert (marked / PARENT_MARKER).is_file()  # unrecorded parents stay


def _vanishing_mkdir(monkeypatch: pytest.MonkeyPatch, root: Path, times: int) -> list[Path]:
    """Make ``root.mkdir`` raise FileNotFoundError ``times`` times; return every call."""
    real_mkdir = Path.mkdir
    calls: list[Path] = []

    def mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == root:
            calls.append(self)
            if len(calls) <= times:
                raise FileNotFoundError("vanished")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", mkdir)
    return calls


@pytest.mark.parametrize(("vanishings", "calls_made"), [(1, 2), (99, 3)])
def test_creation_restarts_when_an_ancestor_vanishes_then_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vanishings: int, calls_made: int
) -> None:
    run, identity = _run(tmp_path)
    calls = _vanishing_mkdir(monkeypatch, tmp_path / "work", times=vanishings)
    if vanishings == 1:
        with rd.claimed_run_directory(run, identity=identity) as directory:
            assert directory.is_dir()
    else:
        with (
            pytest.raises(SimulationRecordIntegrityError, match="could not be created"),
            rd.claimed_run_directory(run, identity=identity),
        ):
            pytest.fail("body must not run")
    assert len(calls) == calls_made


def test_linked_parent_is_never_removed(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / PARENT_MARKER).write_text("{}", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "work/c").symlink_to(target, target_is_directory=True)
    rd.cleanup_interrupted_run_directory(run, identity=identity)
    assert (target / PARENT_MARKER).is_file()


def test_parent_with_non_file_marker_is_kept(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    parent = tmp_path / "work/c"
    (parent / PARENT_MARKER).mkdir(parents=True)
    rd.cleanup_interrupted_run_directory(run, identity=identity)
    assert (parent / PARENT_MARKER).is_dir()


def test_parent_removal_stops_when_rmdir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path)
    parent = _make_marked_parent(tmp_path)

    def refuse(self: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(Path, "rmdir", refuse)
    rd.cleanup_interrupted_run_directory(run, identity=identity)
    assert parent.is_dir()


def test_interrupted_cleanup_ignores_an_unowned_run(tmp_path: Path) -> None:
    run, identity = _run(tmp_path)
    literal = tmp_path / "literal"
    literal.mkdir()
    marker = literal / ".booley-simulation-attempt.json"
    marker.write_bytes(
        canonical_json_bytes(identity)
    )  # a matching marker would be removed if owned
    unowned = dataclasses.replace(run, path=literal, owned=False, template_root=None)

    assert rd.cleanup_interrupted_run_directory(unowned, identity=identity) is False
    assert marker.is_file()


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_templated_run_directory_follows_only_the_booley_dir_entry(
    tmp_path: Path, linked: str
) -> None:
    """#2191: a templated run_cwd under the Booley Directory crosses only the entry link."""
    project = tmp_path / "project"
    project.mkdir()
    entry = project / ".booley"
    symlinked_booley_dir(project, tmp_path / "external")
    if linked == "below":
        # A link below the entry (.runtime/runs) stays refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        (entry / ".runtime").mkdir()
        symlink_or_skip(entry / ".runtime" / "runs", outside, target_is_directory=True)
    run = rd.expand_run_directory(
        ".booley/.runtime/runs/{attempt}",
        project_root=project,
        simulation_record_id="c",
        target_key="sim",
        work_item_key="t",
        attempt_key="a1",
    )
    identity = {"simulation_record_id": "c", "work_item_id": "t", "attempt_id": "a1"}

    if linked == "below":
        with (
            pytest.raises(SimulationRecordIntegrityError, match="contains a link"),
            rd.claimed_run_directory(run, identity=identity),
        ):
            pass
        return
    with rd.claimed_run_directory(run, identity=identity) as directory:
        assert (directory / ".booley-simulation-attempt.json").is_file()


def _former_mount_document(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A persisted run directory an older writer left under a former mount of *root*."""
    former = root.parent / f"{root.name}-former-mount"
    simulate_mount_change(monkeypatch, root, former)
    old = former / "work/c/t/a1"
    return {
        "configured": "work/{record}/{test}/{attempt}",
        "resolved": str(old),
        "collision_key": os.path.normcase(str(old)),
        "owned": True,
    }


@pytest.mark.parametrize("foreign_key", [False, True], ids=["restores", "foreign-key-refused"])
def test_run_directory_recorded_under_a_former_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_key: bool
) -> None:
    root = tmp_path.resolve()
    document = _former_mount_document(root, monkeypatch)
    if foreign_key:
        document["collision_key"] = os.path.normcase(
            str(root.parent / f"{root.name}-former-mount/elsewhere")
        )
        with pytest.raises(SimulationRecordIntegrityError, match="collision key disagrees"):
            rd.restore_run_directory_from_project_data(document, project_data=root)
        return

    run = rd.restore_run_directory_from_project_data(document, project_data=root)

    assert run.path == root / "work/c/t/a1"
    assert run.collision_key == os.path.normcase(str(root / "work/c/t/a1"))


def test_run_directory_inside_the_evidence_folder_is_refused(tmp_path: Path) -> None:
    evidence = tmp_path / "attempts" / "0001-a1" / "execution-evidence"
    inside, _ = _run(tmp_path)
    inside = dataclasses.replace(inside, path=evidence / "run")
    same = dataclasses.replace(inside, path=evidence)
    beside, _ = _run(tmp_path)

    rd.require_run_directory_outside(beside, evidence)
    for run in (inside, same):
        with pytest.raises(SimulationRecordIntegrityError, match="evidence folder"):
            rd.require_run_directory_outside(run, evidence)
