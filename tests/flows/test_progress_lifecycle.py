"""Progress lifecycle and supervisor-repair contracts."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from booley.flows import progress_lifecycle as progress_module
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    abort_open_progress,
    after_progress_publication,
    progress_document,
    read_progress_for_run,
    repair_progress_after_reap,
    retire_progress_of_requeued_attempt,
    write_progress_json,
)
from tests.conftest import symlink_or_skip
from tests.sharing_error_support import flaky, hold_replaces, no_retry_delay, sharing_error
from tests.time_budget import hang_guard_s


def test_normal_completion_publishes_once() -> None:
    phases: list[str] = []
    with ProgressLifecycle(phases.append) as lifecycle:
        lifecycle.complete()
    assert phases == ["complete"]


def test_signal_during_a_lock_held_write_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "progress.json"
    events: list[str] = []
    original = tempfile.mkstemp

    def handler() -> None:
        events.append(f"handler saw {json.loads(path.read_text(encoding='utf-8'))['phase']}")
        abort_open_progress()

    def signalled_mkstemp(*args: Any, **kwargs: Any) -> Any:
        if not events:
            # The signal lands while this write holds the progress lock.
            events.append("signal")
            after_progress_publication(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(progress_module.tempfile, "mkstemp", signalled_mkstemp)

    def publish(phase: str) -> None:
        write_progress_json(
            path,
            progress_document(
                flow="f", run_id="r", phase=phase, targets=["a"], completed_targets=[], detail={}
            ),
            lock_timeout_s=0.2,
        )

    with ProgressLifecycle(publish):
        publish("running")

    assert events == ["signal", "handler saw running"]  # it ran after the write landed
    assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "aborted"


def test_second_signal_during_the_abort_waits_for_it() -> None:
    events: list[str] = []

    def publish(phase: str) -> None:
        events.append(f"publish {phase}")
        if phase == "aborted":  # a second signal lands before the abort is written
            after_progress_publication(lambda: events.append("second"))
            events.append("abort written")

    with ProgressLifecycle(publish):
        after_progress_publication(abort_open_progress)

    assert events == ["publish aborted", "abort written", "second"]


def test_signal_during_the_terminal_publication_waits_for_it() -> None:
    events: list[str] = []

    def publish(phase: str) -> None:
        after_progress_publication(lambda: events.append("handler") or abort_open_progress())
        events.append(f"published {phase}")

    with ProgressLifecycle(publish) as lifecycle:
        lifecycle.complete()

    assert events == ["published complete", "handler"]  # no `aborted` follows a landed `complete`


def test_early_return_terminalizes_aborted() -> None:
    phases: list[str] = []

    def invoke() -> None:
        with ProgressLifecycle(phases.append):
            return

    invoke()
    assert phases == ["aborted"]


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt(), SystemExit(3)])
def test_exception_terminalizes_and_preserves_control_flow(error: BaseException) -> None:
    phases: list[str] = []
    with pytest.raises(type(error)) as raised, ProgressLifecycle(phases.append):
        raise error
    assert raised.value is error
    assert phases == ["aborted"]


def test_failed_complete_repairs_aborted_but_remains_an_error() -> None:
    phases: list[str] = []

    def publish(phase: str) -> None:
        phases.append(phase)
        if phase == "complete":
            raise OSError("rename failed")

    with (
        pytest.raises(ProgressPublicationError, match="rename failed"),
        ProgressLifecycle(publish) as lifecycle,
    ):
        lifecycle.complete()
    assert phases == ["complete", "aborted"]


def test_failed_aborted_publication_retries_without_masking_failure() -> None:
    phases: list[str] = []

    def publish(phase: str) -> None:
        phases.append(phase)
        if len(phases) == 1:
            raise OSError("transient rename failure")

    failure = RuntimeError("Flow failed")
    with pytest.raises(RuntimeError, match="Flow failed") as raised, ProgressLifecycle(publish):
        raise failure
    assert raised.value is failure
    assert phases == ["aborted", "aborted"]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(KeyboardInterrupt(), KeyboardInterrupt), (None, ProgressPublicationError)],
    ids=["interrupt", "no-original-error"],
)
def test_persistent_cleanup_failure_is_reported_without_masking(
    raised: BaseException | None, expected: type[BaseException]
) -> None:
    def publish(_phase: str) -> None:
        raise OSError("disk unavailable")

    with pytest.raises(expected) as caught, ProgressLifecycle(publish):
        if raised is not None:
            raise raised
    if raised is None:
        assert "aborted retry failed" in str(caught.value)
    else:
        assert caught.value is raised


def test_lifecycle_rejects_second_terminal_publication() -> None:
    lifecycle = ProgressLifecycle(lambda _phase: None)
    lifecycle.complete()

    with pytest.raises(RuntimeError, match="already terminalized"):
        lifecycle.complete()


def _write_progress(path: Path, *, run_id: str = "run-1") -> dict[str, object]:
    document = progress_document(
        flow="sim",
        run_id=run_id,
        phase="running",
        targets=("sim_a", "sim_b"),
        completed_targets=("sim_a",),
        detail={"sim_a": {"passed": True}},
        extra={"coverage": True},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def test_repair_after_reap_preserves_target_evidence(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    original = _write_progress(path)
    assert repair_progress_after_reap((tmp_path,), "sim", "run-1")
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["complete"] is True
    assert repaired["phase"] == "aborted"
    assert repaired["completed_targets"] == original["completed_targets"]
    assert repaired["pending_targets"] == original["pending_targets"]


def test_repair_after_reap_ignores_missing_and_terminal_progress(tmp_path: Path) -> None:
    assert not repair_progress_after_reap((tmp_path,), "sim", "run-1")
    path = tmp_path / "sim" / "1" / "progress.json"
    document = _write_progress(path)
    document.update({"phase": "complete", "complete": True})
    path.write_text(json.dumps(document), encoding="utf-8")

    assert not repair_progress_after_reap((tmp_path,), "sim", "run-1")


def test_repair_after_reap_treats_lookup_error_as_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_lookup(*_args: object, **_kwargs: object) -> None:
        raise OSError("report root disappeared")

    monkeypatch.setattr(progress_module, "read_progress_for_run", fail_lookup)

    assert not repair_progress_after_reap((tmp_path,), "sim", "run-1")


def test_progress_reader_follows_only_the_booley_dir_entry(tmp_path: Path) -> None:
    real = tmp_path / "real-booley-dir"
    entry = tmp_path / "project" / ".booley"
    entry.parent.mkdir()
    real.mkdir()  # the pin needs an existing target
    symlink_or_skip(entry, real)
    reports = entry / "runtime" / "flow-reports"
    path = real / "runtime" / "flow-reports" / "sim" / "1" / "progress.json"
    _write_progress(path)

    found = read_progress_for_run((reports,), "sim", "run-1", booley_dir=entry)
    assert found is not None
    assert found[1]["run_id"] == "run-1"
    assert repair_progress_after_reap((reports,), "sim", "run-1", booley_dir=entry)
    assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "aborted"

    # A link below the entry stays refused even when the entry is named.
    elsewhere = tmp_path / "elsewhere"
    _write_progress(elsewhere / "sim" / "1" / "progress.json", run_id="run-2")
    linked_reports = entry / "linked-reports"
    symlink_or_skip(linked_reports, elsewhere)
    assert read_progress_for_run((linked_reports,), "sim", "run-2", booley_dir=entry) is None
    assert not repair_progress_after_reap((linked_reports,), "sim", "run-2", booley_dir=entry)


def test_supervisor_repair_cannot_overwrite_concurrent_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    _write_progress(path)
    retire_at_replace = threading.Event()
    repair_waiting = threading.Event()
    allow_retire = threading.Event()
    original_replace = Path.replace

    def controlled_replace(source: Path, destination: Path) -> Path:
        if threading.current_thread().name == "retire":
            retire_at_replace.set()
            assert allow_retire.wait(timeout=hang_guard_s())
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", controlled_replace)
    real_wait_for_file_lock = progress_module.wait_for_file_lock

    def announcing_wait_for_file_lock(*args: Any, **kwargs: Any) -> Any:
        # The repair thread has read the stale document and now waits for the lock
        # the paused retirement holds.
        if threading.current_thread().name == "repair":
            repair_waiting.set()
        return real_wait_for_file_lock(*args, **kwargs)

    monkeypatch.setattr(progress_module, "wait_for_file_lock", announcing_wait_for_file_lock)
    results: dict[str, bool | None] = {}
    retire = threading.Thread(
        name="retire",
        target=lambda: results.setdefault(
            "retire", retire_progress_of_requeued_attempt(path, endpoint="sim", run_id="run-1")
        ),
    )
    repair = threading.Thread(
        name="repair",
        target=lambda: results.setdefault(
            "repair", repair_progress_after_reap((tmp_path,), "sim", "run-1")
        ),
    )

    retire.start()
    assert retire_at_replace.wait(timeout=hang_guard_s())
    repair.start()
    assert repair_waiting.wait(timeout=hang_guard_s())
    allow_retire.set()
    retire.join(timeout=hang_guard_s())
    repair.join(timeout=hang_guard_s())

    assert results == {"retire": None, "repair": False}
    assert json.loads(path.read_text())["phase"] == "superseded"


def test_run_lookup_rejects_wrong_run_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    _write_progress(path, run_id="other")
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None
    path.unlink()
    outside = tmp_path / "outside.json"
    _write_progress(outside)
    path.symlink_to(outside)
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None


def test_run_lookup_skips_nonnumeric_invocation_and_unreadable_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_progress(tmp_path / "sim" / "latest" / "progress.json")
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None
    endpoint_root = tmp_path / "sim"
    original_iterdir = Path.iterdir

    def fail_endpoint_listing(path: Path):
        if path == endpoint_root:
            raise OSError("directory disappeared")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_endpoint_listing)

    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None


def test_progress_reader_rejects_oversize_and_nonobject_json(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    path.parent.mkdir(parents=True)
    path.write_text('"' + "x" * (2 * 1024 * 1024) + '"', encoding="utf-8")
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None
    path.write_text("[]", encoding="utf-8")
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None


def test_run_lookup_skips_unrelated_roots(tmp_path: Path) -> None:
    progress = tmp_path / "reports" / "sim" / "1" / "progress.json"
    _write_progress(progress)
    found = read_progress_for_run((tmp_path / "unrelated", tmp_path / "reports"), "sim", "run-1")
    assert found is not None
    assert found[0] == progress


def test_containing_root_rejects_outside_path(tmp_path: Path) -> None:
    progress = tmp_path / "reports" / "sim" / "1" / "progress.json"
    with pytest.raises(ValueError, match="outside configured report roots"):
        # Private seam: every public caller derives the path from its roots, so
        # a path outside them is unreachable from the public functions.
        progress_module._containing_root(progress, (tmp_path / "unrelated",))


def test_default_progress_reader_refuses_links_above_report_root(tmp_path):
    real = tmp_path / "real"
    path = real / "project/reports/sim/1/progress.json"
    _write_progress(path, run_id="one")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert read_progress_for_run((alias / "project/reports",), "sim", "one") is None


@pytest.mark.parametrize("held", [1, 100])
def test_progress_write_retries_replace_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: int
) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    document = _write_progress(path)
    attempts = hold_replaces(monkeypatch, {path: held})
    if held > 1:
        with pytest.raises(PermissionError):
            write_progress_json(path, document)
        assert attempts[path] > 1
        return
    write_progress_json(path, {**document, "phase": "complete", "complete": True})
    assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "complete"
    assert attempts[path] == 2


def _flaky_progress_open(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    no_retry_delay(monkeypatch)
    opener, calls = flaky(progress_module.open_regular_nofollow, failures)
    monkeypatch.setattr(progress_module, "open_regular_nofollow", opener)
    return calls


@pytest.mark.parametrize("failures", [1, 100])
def test_progress_read_retries_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    _write_progress(path)
    calls = _flaky_progress_open(monkeypatch, failures)
    if failures > 1:
        with pytest.raises(PermissionError):
            read_progress_for_run((tmp_path,), "sim", "run-1")
        assert calls[0] > 1
        return
    found = read_progress_for_run((tmp_path,), "sim", "run-1")
    assert found is not None
    assert found[0] == path
    assert calls == [2]


def test_progress_read_keeps_posix_permission_error_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_progress(tmp_path / "sim" / "1" / "progress.json")
    monkeypatch.setattr("sys.platform", "linux")

    def deny(*_args: object, **_kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(progress_module, "open_regular_nofollow", deny)
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None


def test_held_checkpoint_of_another_run_does_not_hide_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = tmp_path / "sim" / "1" / "progress.json"
    held = tmp_path / "sim" / "2" / "progress.json"
    _write_progress(mine)
    _write_progress(held, run_id="other")
    os.utime(held, (mine.stat().st_atime + 10, mine.stat().st_mtime + 10))  # newest first
    no_retry_delay(monkeypatch)
    real_open = progress_module.open_regular_nofollow

    def open_unless_held(path: Path, **kwargs: object) -> int:
        if Path(path) == held:
            raise sharing_error()
        return real_open(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(progress_module, "open_regular_nofollow", open_unless_held)
    found = read_progress_for_run((tmp_path,), "sim", "run-1")
    assert found is not None
    assert found[0] == mine


def test_retired_progress_of_a_requeued_attempt_is_not_read_for_its_run(tmp_path: Path) -> None:
    killed = tmp_path / "sim" / "1" / "progress.json"
    original = _write_progress(killed)
    other_run = tmp_path / "sim" / "2" / "progress.json"
    _write_progress(other_run, run_id="run-2")
    other_flow = tmp_path / "synth" / "1" / "progress.json"
    _write_progress(other_flow)
    other_flow.write_text(
        json.dumps({**json.loads(other_flow.read_bytes()), "flow": "synth"}), encoding="utf-8"
    )
    other_flow_before = other_flow.read_bytes()
    other_run_before = other_run.read_bytes()

    retire_progress_of_requeued_attempt(killed, endpoint="sim", run_id="run-1")

    retired = json.loads(killed.read_text(encoding="utf-8"))
    assert retired["phase"] == "superseded"
    assert retired["complete"] is True
    assert retired["run_id"] == ""
    assert retired["superseded_by"] == {
        "reason": "requeued after out-of-memory",
        "run_id": "run-1",
    }
    # What the killed attempt observed is kept.
    assert retired["completed_targets"] == original["completed_targets"]
    assert retired["detail"] == original["detail"]
    assert read_progress_for_run((tmp_path,), "sim", "run-1") is None
    found = read_progress_for_run((tmp_path,), "sim", "run-2")
    assert found is not None and found[0] == other_run
    # Another run's or another Flow's document, and a missing one, are left alone.
    retire_progress_of_requeued_attempt(other_run, endpoint="sim", run_id="run-1")
    retire_progress_of_requeued_attempt(other_flow, endpoint="sim", run_id="run-1")
    retire_progress_of_requeued_attempt(
        tmp_path / "sim" / "3" / "progress.json", endpoint="sim", run_id="run-1"
    )
    retire_progress_of_requeued_attempt(other_run, endpoint="sim", run_id="")
    assert other_run.read_bytes() == other_run_before
    assert other_flow.read_bytes() == other_flow_before
    # Retiring twice is a no-op: the run id is already empty.
    retire_progress_of_requeued_attempt(killed, endpoint="sim", run_id="run-1")
    assert json.loads(killed.read_text(encoding="utf-8")) == retired
