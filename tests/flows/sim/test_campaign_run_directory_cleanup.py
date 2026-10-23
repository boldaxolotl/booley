"""Booley-created empty run_cwd parents and run-lock files are cleaned up (#1971)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from booley.flows.sim.campaign import run_directory as run_directory_module
from booley.flows.sim.campaign.run_directory import (
    RunDirectory,
    claimed_run_directory,
    cleanup_interrupted_run_directory,
    expand_run_directory,
    restore_run_directory,
)

TEMPLATE = "work/{campaign}/{test}/{attempt}"
ATTEMPT_MARKER = ".booley-simulation-attempt.json"
PARENT_MARKER = ".booley-simulation-parent.json"


def _run(
    root: Path, campaign: str, test: str, attempt: str = "a1", template: str = TEMPLATE
) -> tuple[RunDirectory, dict[str, str]]:
    """Expand one templated run directory and the identity that claims it."""
    run = expand_run_directory(
        template,
        project_root=root,
        campaign_id=campaign,
        target_key="sim",
        work_item_key=test,
        attempt_key=attempt,
    )
    identity = {"campaign_id": campaign, "work_item_id": test, "attempt_id": attempt}
    return run, identity


def _document(run: RunDirectory, configured: str = TEMPLATE) -> dict[str, object]:
    """Return the persisted run_directory document ``restore_run_directory`` reads."""
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


def test_repeated_templated_campaigns_leave_no_empty_parents(tmp_path: Path) -> None:
    for campaign in ("c1", "c2", "c3"):
        for test in ("t1", "t2"):
            run, identity = _run(tmp_path, campaign, test)
            with claimed_run_directory(run, identity=identity) as directory:
                assert directory.is_dir()
    assert (tmp_path / "work").is_dir()
    assert list((tmp_path / "work").iterdir()) == []


def test_cleanup_keeps_preexisting_parent_and_template_root(tmp_path: Path) -> None:
    (tmp_path / "work/c1").mkdir(parents=True)
    for campaign in ("c1", "c2"):
        run, identity = _run(tmp_path, campaign, "t")
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


def _claim_siblings_in_parallel(tmp_path: Path) -> None:
    """Claim eight sibling attempts of one campaign behind a barrier; none may fail."""
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def claim(index: int) -> None:
        run, identity = _run(tmp_path, "c", f"t{index}")
        try:
            barrier.wait()
            with claimed_run_directory(run, identity=identity):
                time.sleep(0.01)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assert below
            errors.append(error)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_concurrent_sibling_attempts_keep_each_others_directories(tmp_path: Path) -> None:
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
    _claim_siblings_in_parallel(tmp_path)
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize("state", ["attempt-survives", "parents-survive"])
def test_resume_cleanup_removes_recorded_empty_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    run, identity = _run(tmp_path, "c", "t")
    if state == "attempt-survives":
        # Crash before tree removal: the attempt dir and its marked parents survive.

        def interrupted(_root: Path) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(run_directory_module, "_remove_owned_tree", interrupted)
        with pytest.raises(KeyboardInterrupt), claimed_run_directory(run, identity=identity):
            pass
        monkeypatch.undo()
        assert (run.path / ATTEMPT_MARKER).is_file()
    else:
        # Crash after tree removal: only the marked, otherwise empty parents survive.
        for parent in (tmp_path / "work/c", tmp_path / "work/c/t"):
            parent.mkdir(parents=True, exist_ok=True)
            (parent / PARENT_MARKER).write_bytes(b"{}")
        assert not run.path.exists()
    restored = restore_run_directory(_document(run), project_root=tmp_path)
    cleanup_interrupted_run_directory(restored, identity=identity)
    assert not run.path.exists()
    assert not (tmp_path / "work/c").exists()
    assert (tmp_path / "work").is_dir()


def test_resume_cleanup_leaves_unrecorded_parents(tmp_path: Path) -> None:
    run, identity = _run(tmp_path, "c", "t")
    # Legacy layout: plain mkdir(parents=True) plus the attempt marker only.
    run.path.mkdir(parents=True)
    (run.path / ATTEMPT_MARKER).write_bytes(
        run_directory_module.canonical_json_bytes(dict(identity))
    )
    restored = restore_run_directory(_document(run), project_root=tmp_path)
    assert cleanup_interrupted_run_directory(restored, identity=identity) is True
    assert not run.path.exists()
    assert (tmp_path / "work/c/t").is_dir()


def test_templated_run_locks_do_not_accumulate(tmp_path: Path) -> None:
    for campaign in ("c1", "c2"):
        for test in ("t1", "t2", "t3"):
            run, identity = _run(tmp_path, campaign, test)
            with claimed_run_directory(run, identity=identity):
                pass
    run, identity = _run(tmp_path, "c3", "t1")
    restored = restore_run_directory(_document(run), project_root=tmp_path)
    cleanup_interrupted_run_directory(restored, identity=identity)
    assert _lock_names(tmp_path) in ([], ["parents.lock"])


def test_unlinked_run_lock_still_serializes_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = "work/{test}"
    sections: list[tuple[float, float]] = []
    guard = threading.Lock()
    waiting = threading.Event()
    real_wait = run_directory_module.wait_for_file_lock

    def spy(handle: object, **kwargs: object) -> None:
        # Flag the moment the waiter holds the old lock inode and is about to block.
        if threading.current_thread().name == "waiter":
            waiting.set()
        real_wait(handle, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_directory_module, "wait_for_file_lock", spy)

    def critical(attempt: str) -> None:
        run, identity = _run(tmp_path, "c", "t", attempt, template)
        with claimed_run_directory(run, identity=identity):
            start = time.monotonic()
            time.sleep(0.2)
            with guard:
                sections.append((start, time.monotonic()))

    holder, holder_identity = _run(tmp_path, "c", "t", "holder", template)
    holder_cm = claimed_run_directory(holder, identity=holder_identity)
    holder_cm.__enter__()
    waiter = threading.Thread(target=critical, args=("waiter",), name="waiter")
    waiter.start()
    assert waiting.wait(timeout=30)
    time.sleep(0.1)
    holder_cm.__exit__(None, None, None)
    late = threading.Thread(target=critical, args=("late",), name="late")
    late.start()
    waiter.join()
    late.join()
    ordered = sorted(sections)
    assert len(ordered) == 2
    assert ordered[0][1] <= ordered[1][0]
    assert [name for name in _lock_names(tmp_path) if name != "parents.lock"] == []
