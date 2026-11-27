"""Booley-created run_cwd parents and run locks are cleaned up (#1971); claims serialize."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.record import run_directory as run_directory_module
from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from booley.flows.sim.record.run_directory import (
    RunDirectory,
    claimed_run_directory,
    cleanup_interrupted_run_directory,
    expand_run_directory,
    restore_run_directory_from_project_data,
)
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from tests.job_slot_steps import (
    FINISH_STEP_CAP,
    Actor,
    FakeProcessWorld,
    StepScheduler,
    Worker,
    stepped_file_lock_wait,
    stepped_root,
)

TEMPLATE = "work/{record}/{test}/{attempt}"
ATTEMPT_MARKER = ".booley-simulation-attempt.json"
PARENT_MARKER = ".booley-simulation-parent.json"


def _restore(run: RunDirectory, root: Path) -> RunDirectory:
    """Reconstruct a persisted run directory the way resume and prune do."""
    project_data = resolve_checkout_booley_dir(root)
    return restore_run_directory_from_project_data(
        _document(run), project_data=project_data, booley_dir=project_data
    )


def _run(
    root: Path, simulation_record: str, test: str, attempt: str = "a1", template: str = TEMPLATE
) -> tuple[RunDirectory, dict[str, str]]:
    """Expand one templated run directory and the identity that claims it."""
    run = expand_run_directory(
        template,
        project_root=root,
        simulation_record_id=simulation_record,
        target_key="sim",
        work_item_key=test,
        attempt_key=attempt,
    )
    identity = {
        "simulation_record_id": simulation_record,
        "work_item_id": test,
        "attempt_id": attempt,
    }
    return run, identity


def _document(run: RunDirectory, configured: str = TEMPLATE) -> dict[str, object]:
    """Return the persisted run_directory document ``restore_run_directory_from_project_data`` reads."""
    return {
        "kind": "templated",
        "configured": configured,
        "resolved": str(run.path),
        "collision_key": run.collision_key,
        "owned": run.owned,
    }


def _lock_names(root: Path) -> list[str]:
    """Names of the files in the run-lock directory of the checkout under ``root``."""
    locks = run_directory_module.checkout_runtime_dir(root) / "simulation-run-locks"
    return sorted(path.name for path in locks.iterdir()) if locks.is_dir() else []


LOCK_WAIT = ("lock_wait", "")


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Iterator[StepScheduler]:
    """Claims run one filesystem step at a time; the run-lock wait is a scheduling point."""
    monkeypatch.setattr(run_directory_module, "wait_for_file_lock", stepped_file_lock_wait)
    machine = StepScheduler(FakeProcessWorld())
    yield machine
    machine.abort()


def _start_claim(
    scheduler: StepScheduler,
    index: int,
    run: RunDirectory,
    identity: dict[str, str],
    events: list[str],
) -> Worker:
    """Start claiming *run* as actor *index*; the claim's body is one file write."""
    actor = Actor(index, scheduler.world, scheduler, Path(), lambda *_: False)
    stepped = replace(run, path=stepped_root(run.path, scheduler))

    def claim() -> None:
        with claimed_run_directory(stepped, identity=identity) as path:
            events.append(f"{actor.name} in")
            (path / f"{actor.name}.log").write_text(actor.name, encoding="utf-8")
            events.append(f"{actor.name} out")

    return scheduler.start(actor, actor.name, claim)


def _grant_until(scheduler: StepScheduler, worker: Worker, *stops: tuple[str, str]) -> None:
    """Grant *worker* steps until it ends or its next step is one of *stops*."""
    for _ in range(FINISH_STEP_CAP):
        scheduler.grant(worker)
        if worker.state != "parked" or worker.pending in stops:
            return
    raise AssertionError(f"{worker.label} never reached {stops}")


def _drain_ok(scheduler: StepScheduler) -> None:
    """Run every claim to its end; a claim left unfinished or ended by an OSError fails."""
    scheduler.drain()
    assert [w.oserror for w in scheduler.workers if w.state != "done" or w.oserror] == []


def test_cleanup_keeps_preexisting_parent_and_template_root(tmp_path: Path) -> None:
    (tmp_path / "work/c1").mkdir(parents=True)
    for simulation_record in ("c1", "c2"):
        run, identity = _run(tmp_path, simulation_record, "t")
        with claimed_run_directory(run, identity=identity):
            pass
    assert (tmp_path / "work").is_dir()
    assert list((tmp_path / "work/c1").iterdir()) == []
    assert not (tmp_path / "work/c2").exists()


def test_cleanup_stops_at_parent_with_foreign_content(tmp_path: Path) -> None:
    run, identity = _run(tmp_path, "c", "t")
    with claimed_run_directory(run, identity=identity):
        (tmp_path / "work/c/notes.txt").write_text("mine", encoding="utf-8")
    assert not (tmp_path / "work/c/t").exists()
    assert (tmp_path / "work/c/notes.txt").read_text(encoding="utf-8") == "mine"


def test_overlapping_sibling_attempts_keep_each_others_directories(tmp_path: Path) -> None:
    # Sequential overlap: the creator of the shared parent finishes before its sibling.
    first, first_identity = _run(tmp_path, "c", "t1")
    second, second_identity = _run(tmp_path, "c", "t2")
    first_cm = claimed_run_directory(first, identity=first_identity)
    second_cm = claimed_run_directory(second, identity=second_identity)
    first_cm.__enter__()
    live = second_cm.__enter__()
    (live / "payload.log").write_text("live", encoding="utf-8")
    first_cm.__exit__(None, None, None)
    assert not (tmp_path / "work/c/t1").exists()
    assert (live / "payload.log").read_text(encoding="utf-8") == "live"
    assert (live / ATTEMPT_MARKER).is_file()
    assert (tmp_path / "work/c").is_dir()
    second_cm.__exit__(None, None, None)
    assert not (tmp_path / "work/c").exists()
    assert list((tmp_path / "work").iterdir()) == []


def _kill_at(
    monkeypatch: pytest.MonkeyPatch, method: str, name: str, *, empty_first: bool = False
) -> None:
    """Make ``Path.<method>`` raise ``KeyboardInterrupt`` for the path named ``name``."""
    real = getattr(Path, method)

    def killed(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == name:
            if empty_first:
                real(self, b"")
            raise KeyboardInterrupt
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, killed)


def _killed_claim(
    monkeypatch: pytest.MonkeyPatch,
    run: RunDirectory,
    identity: dict[str, str],
    method: str,
    name: str,
    **kw: bool,
) -> None:
    """Run a claim that a kill stops at ``Path.<method>`` on ``name``."""
    _kill_at(monkeypatch, method, name, **kw)
    with pytest.raises(KeyboardInterrupt), claimed_run_directory(run, identity=identity):
        pass
    monkeypatch.undo()


@pytest.mark.parametrize(
    "state",
    [
        "attempt-survives",
        "parents-survive",
        "killed-before-attempt-marker",
        "killed-with-empty-attempt-marker",
        "killed-before-parent-marker",
        "killed-before-attempt-mkdir",
        "recovery-killed-before-parent-rmdir",
    ],
)
def test_resume_cleanup_removes_recorded_empty_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    run, identity = _run(tmp_path, "c", "t")
    if state == "attempt-survives":
        # Crash before tree removal: the attempt dir and its marked parents survive.

        real_unlink = Path.unlink

        def interrupted(self: Path, missing_ok: bool = False) -> None:
            if self.name == ATTEMPT_MARKER:
                raise KeyboardInterrupt
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", interrupted)
        with pytest.raises(KeyboardInterrupt), claimed_run_directory(run, identity=identity):
            pass
        monkeypatch.undo()
        assert (run.path / ATTEMPT_MARKER).is_file()
    elif state.startswith("killed-") or state.startswith("recovery-"):
        # Killed while creating: the directories exist and Booley has not marked them.
        method, marker = "write_bytes", PARENT_MARKER if "parent" in state else ATTEMPT_MARKER
        if "mkdir" in state:
            # The note is written, the directory not yet confirmed.
            method, marker = "mkdir", "a1"
        _killed_claim(monkeypatch, run, identity, method, marker, empty_first="empty" in state)
        if state.startswith("recovery-"):
            _kill_at(monkeypatch, "rmdir", "c")
            with pytest.raises(KeyboardInterrupt):
                cleanup_interrupted_run_directory(_restore(run, tmp_path), identity=identity)
            monkeypatch.undo()
    else:
        # Crash after tree removal: only the marked, otherwise empty parents survive.
        for parent in (tmp_path / "work/c", tmp_path / "work/c/t"):
            parent.mkdir(parents=True, exist_ok=True)
            (parent / PARENT_MARKER).write_bytes(b"{}")
        assert not run.path.exists()
    restored = _restore(run, tmp_path)
    cleanup_interrupted_run_directory(restored, identity=identity)
    assert not run.path.exists()
    assert not (tmp_path / "work/c").exists()
    assert (tmp_path / "work").is_dir()
    assert _lock_names(tmp_path) in ([], ["parents.lock"])
    with claimed_run_directory(run, identity=identity):
        pass
    assert not (tmp_path / "work/c").exists()


@pytest.mark.parametrize(
    "state",
    ["user-content", "replaced-directory", "preexisting-parent", "preexisting-empty-directory"],
)
def test_recovery_after_killed_creation_spares_what_booley_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    run, identity = _run(tmp_path, "c", "t")
    if state == "preexisting-parent":
        (tmp_path / "work/c").mkdir(parents=True)
    if state == "preexisting-empty-directory":
        run.path.mkdir(parents=True)
    elif state == "replaced-directory":
        _killed_claim(monkeypatch, run, identity, "mkdir", "a1")
        run.path.mkdir()
        (run.path / ATTEMPT_MARKER).write_bytes(b"")
    else:
        _killed_claim(monkeypatch, run, identity, "write_bytes", ATTEMPT_MARKER)
    if state == "user-content":
        (run.path / "mine.txt").write_bytes(b"keep")
    restored = _restore(run, tmp_path)
    if state == "preexisting-parent":
        assert cleanup_interrupted_run_directory(restored, identity=identity) is True
        assert not (tmp_path / "work/c/t").exists()
        assert (tmp_path / "work/c").is_dir()
        return
    with pytest.raises(
        SimulationRecordIntegrityError,
        match=r"refusing to clean unowned run.*move the directory elsewhere",
    ):
        cleanup_interrupted_run_directory(restored, identity=identity)
    assert run.path.is_dir()
    assert state != "user-content" or (run.path / "mine.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("who", ["recovery", "uninterrupted-run"])
def test_noted_parent_the_user_filled_keeps_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    run, identity = _run(tmp_path, "c", "t")
    _killed_claim(monkeypatch, run, identity, "mkdir", "c")  # note written, no directory yet
    mine = tmp_path / "work/c"
    mine.mkdir()
    (mine / PARENT_MARKER).mkdir()  # not a file: Booley never writes or removes through it
    (mine / "mine.txt").write_bytes(b"mine")
    if who == "recovery":
        cleanup_interrupted_run_directory(_restore(run, tmp_path), identity=identity)
    else:
        other, other_identity = _run(tmp_path, "c", "t2")
        with claimed_run_directory(other, identity=other_identity):
            pass
    assert (mine / "mine.txt").read_bytes() == b"mine"
    assert (mine / PARENT_MARKER).is_dir()
    assert [entry.name for entry in (mine / PARENT_MARKER).iterdir()] == []


def test_noted_empty_parent_the_user_created_is_removed_only_by_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, identity = _run(tmp_path, "c", "t")
    _killed_claim(monkeypatch, run, identity, "mkdir", "c")
    (tmp_path / "work/c").mkdir()
    other, other_identity = _run(tmp_path, "c", "t2")
    with claimed_run_directory(other, identity=other_identity):
        pass
    assert (tmp_path / "work/c").is_dir()
    cleanup_interrupted_run_directory(_restore(run, tmp_path), identity=identity)
    assert not (tmp_path / "work/c").exists()


def test_resume_cleanup_leaves_unrecorded_parents(tmp_path: Path) -> None:
    run, identity = _run(tmp_path, "c", "t")
    # Legacy layout: plain mkdir(parents=True) plus the attempt marker only.
    run.path.mkdir(parents=True)
    (run.path / ATTEMPT_MARKER).write_bytes(
        run_directory_module.canonical_json_bytes(dict(identity))
    )
    restored = _restore(run, tmp_path)
    assert cleanup_interrupted_run_directory(restored, identity=identity) is True
    assert not run.path.exists()
    assert (tmp_path / "work/c/t").is_dir()


def test_templated_run_locks_do_not_accumulate(tmp_path: Path) -> None:
    for simulation_record in ("c1", "c2"):
        for test in ("t1", "t2", "t3"):
            run, identity = _run(tmp_path, simulation_record, test)
            with claimed_run_directory(run, identity=identity):
                pass
    run, identity = _run(tmp_path, "c3", "t1")
    restored = _restore(run, tmp_path)
    cleanup_interrupted_run_directory(restored, identity=identity)
    assert _lock_names(tmp_path) in ([], ["parents.lock"])


def test_unlinked_run_lock_still_serializes_waiters(
    tmp_path: Path, scheduler: StepScheduler
) -> None:
    events: list[str] = []
    claims = [_run(tmp_path, "c", "t", attempt, "work/{test}") for attempt in ("h", "w", "l")]
    holder = _start_claim(scheduler, 0, *claims[0], events)
    _grant_until(scheduler, holder, ("write_text", "A.log"))
    waiter = _start_claim(scheduler, 1, *claims[1], events)
    _grant_until(scheduler, waiter, LOCK_WAIT)
    scheduler.finish(holder)  # POSIX unlinks the lock the waiter has open; Windows keeps it
    late = _start_claim(scheduler, 2, *claims[2], events)
    _grant_until(scheduler, late, ("write_text", "C.log"))  # holds the lock; a new file on POSIX
    _grant_until(scheduler, waiter, LOCK_WAIT, ("write_text", "B.log"))
    assert waiter.pending == LOCK_WAIT
    _drain_ok(scheduler)
    assert events == ["A in", "A out", "C in", "C out", "B in", "B out"]
    assert [name for name in _lock_names(tmp_path) if name != "parents.lock"] == []


def test_sibling_cleanup_waits_for_a_sibling_filling_the_parent(
    tmp_path: Path, scheduler: StepScheduler
) -> None:
    events: list[str] = []
    remover = _start_claim(scheduler, 0, *_run(tmp_path, "c", "t", "a1"), events)
    # The remover found the shared parent work/c/t holding only its marker and removes it next.
    _grant_until(scheduler, remover, ("unlink", PARENT_MARKER))
    filler = _start_claim(scheduler, 1, *_run(tmp_path, "c", "t", "a2"), events)
    _grant_until(scheduler, filler, LOCK_WAIT, ("write_text", "B.log"))
    scheduler.finish(remover)
    _drain_ok(scheduler)
    assert list((tmp_path / "work").iterdir()) == []


def test_canonical_collision_keys_serialize_aliased_claims(
    tmp_path: Path, scheduler: StepScheduler
) -> None:
    literal = tmp_path / "literal"
    literal.mkdir()
    (first, mine), (alias, theirs) = (
        _run(tmp_path, "c", "t", attempt, str(configured))
        for attempt, configured in (("a1", literal), ("a2", literal / ".." / "literal"))
    )
    events: list[str] = []
    owner = _start_claim(scheduler, 0, first, mine, events)
    _grant_until(scheduler, owner, ("write_text", "A.log"))
    other = _start_claim(scheduler, 1, alias, theirs, events)
    _grant_until(scheduler, other, LOCK_WAIT, ("write_text", "B.log"))
    assert other.pending == LOCK_WAIT
    _drain_ok(scheduler)
    assert first.collision_key == alias.collision_key
    assert events == ["A in", "A out", "B in", "B out"]
