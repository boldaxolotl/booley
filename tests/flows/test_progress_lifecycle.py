"""Progress lifecycle and supervisor-repair contracts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from booley.flows import progress_lifecycle as progress_module
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    progress_document,
    read_progress_for_run,
    repair_progress_after_reap,
    supersede_progress,
    validate_progress_shape,
)


def test_normal_completion_publishes_once() -> None:
    phases: list[str] = []
    with ProgressLifecycle(phases.append) as lifecycle:
        lifecycle.complete()
    assert phases == ["complete"]


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


def test_persistent_cleanup_failure_does_not_mask_interrupt() -> None:
    def publish(_phase: str) -> None:
        raise OSError("disk unavailable")

    interrupt = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as raised, ProgressLifecycle(publish):
        raise interrupt
    assert raised.value is interrupt


def test_persistent_cleanup_failure_without_original_error_is_reported() -> None:
    def publish(_phase: str) -> None:
        raise OSError("disk unavailable")

    with (
        pytest.raises(ProgressPublicationError, match="aborted retry failed"),
        ProgressLifecycle(publish),
    ):
        pass


def test_lifecycle_rejects_second_terminal_publication() -> None:
    lifecycle = ProgressLifecycle(lambda _phase: None)
    lifecycle.complete()

    with pytest.raises(RuntimeError, match="already terminalized"):
        lifecycle.complete()


def test_contradictory_terminal_shape_is_rejected() -> None:
    document = progress_document(
        flow="sim",
        run_id="run-1",
        phase="running",
        targets=("sim_a",),
        completed_targets=(),
        detail={},
    )
    document["complete"] = True
    with pytest.raises(ValueError, match="complete and phase disagree"):
        validate_progress_shape(document)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"complete": "yes"}, "boolean complete"),
        ({"phase": "unknown"}, "unsupported progress phase"),
        ({"targets": ["sim_a", "sim_a"]}, "repeats Target identities"),
        ({"completed_targets": ["sim_a", "sim_a"]}, "repeats completed"),
        ({"pending_targets": []}, "do not partition"),
        ({"targets": [1]}, "list of strings"),
    ],
)
def test_invalid_progress_shapes_are_rejected(updates: dict[str, object], message: str) -> None:
    document = progress_document(
        flow="sim",
        run_id="run-1",
        phase="running",
        targets=("sim_a",),
        completed_targets=(),
        detail={},
    )
    document.update(updates)

    with pytest.raises(ValueError, match=message):
        validate_progress_shape(document)


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
    def fail_lookup(*_args: object) -> None:
        raise OSError("report root disappeared")

    monkeypatch.setattr(progress_module, "read_progress_for_run", fail_lookup)

    assert not repair_progress_after_reap((tmp_path,), "sim", "run-1")


def test_supervisor_repair_cannot_overwrite_concurrent_resume_supersession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    _write_progress(path)
    supersede_at_replace = threading.Event()
    repair_at_replace = threading.Event()
    allow_supersede = threading.Event()
    original_replace = Path.replace

    def controlled_replace(source: Path, destination: Path) -> Path:
        if threading.current_thread().name == "supersede":
            supersede_at_replace.set()
            assert allow_supersede.wait(timeout=5)
        elif threading.current_thread().name == "repair":
            repair_at_replace.set()
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", controlled_replace)
    results: dict[str, bool] = {}
    supersede = threading.Thread(
        name="supersede",
        target=lambda: results.setdefault(
            "supersede", supersede_progress(path, new_invocation=2, new_run_id="resume-run")
        ),
    )
    repair = threading.Thread(
        name="repair",
        target=lambda: results.setdefault(
            "repair", repair_progress_after_reap((tmp_path,), "sim", "run-1")
        ),
    )

    supersede.start()
    assert supersede_at_replace.wait(timeout=5)
    repair.start()
    repair_at_replace.wait(timeout=0.5)
    allow_supersede.set()
    supersede.join(timeout=5)
    repair.join(timeout=5)

    assert results == {"supersede": True, "repair": False}
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


def test_supersede_is_idempotent_and_preserves_origin_identity(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    original = _write_progress(path, run_id="origin-run")
    supersede_progress(path, new_invocation=2, new_run_id="resume-run")
    first = json.loads(path.read_text(encoding="utf-8"))
    supersede_progress(path, new_invocation=3, new_run_id="later-run")
    second = json.loads(path.read_text(encoding="utf-8"))
    assert first == second
    assert first["phase"] == "superseded"
    assert first["run_id"] == "origin-run"
    assert first["completed_targets"] == original["completed_targets"]
    assert first["pending_targets"] == original["pending_targets"]
    assert first["superseded_by"] == {"invocation": 2, "run_id": "resume-run"}


def test_supersede_ignores_nonresumable_phase_and_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    document = _write_progress(path)
    document["phase"] = "starting"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert not supersede_progress(path, new_invocation=2, new_run_id="resume-run")
    document["phase"] = "running"
    path.write_text(json.dumps(document), encoding="utf-8")

    def fail_replace(*_args: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(progress_module, "_replace_if_unchanged", fail_replace)

    assert not supersede_progress(path, new_invocation=2, new_run_id="resume-run")


def test_progress_reader_rejects_oversize_and_nonobject_json(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "1" / "progress.json"
    path.parent.mkdir(parents=True)
    path.write_text('"' + "x" * (2 * 1024 * 1024) + '"', encoding="utf-8")
    assert not supersede_progress(path, new_invocation=2, new_run_id="resume-run")
    path.write_text("[]", encoding="utf-8")
    assert not supersede_progress(path, new_invocation=2, new_run_id="resume-run")


def test_containing_root_skips_unrelated_roots_and_rejects_outside_path(
    tmp_path: Path,
) -> None:
    progress = tmp_path / "reports" / "sim" / "1" / "progress.json"
    assert (
        progress_module._containing_root(progress, (tmp_path / "unrelated", tmp_path / "reports"))
        == tmp_path / "reports"
    )

    with pytest.raises(ValueError, match="outside configured report roots"):
        progress_module._containing_root(progress, (tmp_path / "unrelated",))
