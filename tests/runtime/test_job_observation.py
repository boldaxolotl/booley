"""JobObserver's explicit-input and refresh-generation contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.runtime import job_records, job_slots
from booley.runtime.job_observation import JobObserver, _observer
from booley.runtime.pid import ProcessIdentity


def write_job(root: Path, run_id: str, **kwargs) -> Path:
    job_records.write_record(
        job_records.JobRecord(run_id, "sim", "2026-10-08T10:00:00Z", 60, status="done", **kwargs),
        root,
    )
    report = root.parent / "flow-reports/sim/1/report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"run_id": run_id, "passed": True}))
    return report


def test_only_explicit_roots_are_observed_and_repeated_roots_keep_one_identity(tmp_path):
    selected = tmp_path / "selected/jobs"
    unselected = tmp_path / ".interactive_logs/other/.runtime/jobs"
    write_job(selected, "one")
    write_job(unselected, "other")

    snapshot = JobObserver(tmp_path).read((selected, selected))

    assert [job.key for job in snapshot.jobs] == [(str(selected), "one")]
    assert snapshot.jobs[0].report == {"run_id": "one", "passed": True}
    assert snapshot.diagnostics == ()


def test_observer_does_not_interpret_caller_owned_binding_schema(tmp_path):
    root = tmp_path / "jobs"
    binding = {"schema": "other-context/v1", "context": "recorded"}
    write_job(root, "one", binding=binding)

    snapshot = JobObserver(tmp_path).read((root,))

    assert snapshot.jobs[0].record.binding == binding
    assert snapshot.diagnostics == ()


def test_metadata_validator_rejects_before_artifact_io_and_isolates_the_record(
    tmp_path, monkeypatch
):
    accepted, rejected = (tmp_path / name / "jobs" for name in ("accepted", "rejected"))
    write_job(accepted, "good")
    forbidden = write_job(rejected, "bad")
    original = Path.read_bytes

    def read_bytes(path):
        assert path != forbidden, "rejected metadata reached artifact observation"
        return original(path)

    def validate(record):
        if record.run_id == "bad":
            raise ValueError("invalid context metadata")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    snapshot = JobObserver(tmp_path, validate_record=validate).read((accepted, rejected))

    assert [job.record.run_id for job in snapshot.jobs] == ["good"]
    assert snapshot.diagnostics == (f"Job record unavailable: {rejected / 'bad.json'}",)


@pytest.mark.parametrize("error", [OSError, job_records.JobRecordError, RuntimeError])
def test_unexpected_metadata_validator_errors_abort_the_refresh(tmp_path, monkeypatch, error):
    roots = [tmp_path / str(number) / "jobs" for number in range(2)]
    for number, root in enumerate(roots):
        write_job(root, str(number))
    failing = False

    def validate(_record):
        if failing:
            raise error("metadata adapter failed")

    observer = JobObserver(tmp_path, validate_record=validate)
    observer.read(roots)
    failing = True

    with pytest.raises(error, match="metadata adapter failed"):
        observer.read(roots[:1])

    failing = False
    monkeypatch.setattr(
        Path, "read_bytes", lambda _: pytest.fail("failed validator committed an empty generation")
    )
    restored = observer.read(roots)
    assert len(restored.jobs) == 2
    assert all(job.report is not None for job in restored.jobs)


def test_partial_refresh_preserves_committed_parses(tmp_path, monkeypatch):
    root, later = (tmp_path / name / "jobs" for name in ("first", "later"))
    report_a = write_job(root, "A")
    report_b = report_a.parent.with_name("2") / "report.json"
    report_b.parent.mkdir()
    report_a.rename(report_b)
    write_job(root, "B")
    failing_report = write_job(later, "later")
    observer = JobObserver(tmp_path)
    assert len(observer.read((root,)).jobs) == 2
    original_read, original_stat = Path.read_bytes, Path.lstat
    before = [
        (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_size) for p in (report_a, report_b)
    ]
    reads, inspected = [], []

    def lstat(path, *args, **kwargs):
        if path == report_b:
            inspected.append(path)
            raise OSError("temporarily hidden report")
        return original_stat(path, *args, **kwargs)

    def read(path):
        reads.append(path)
        if path == failing_report:
            assert inspected, "first endpoint was not indexed before failure"
            raise RuntimeError("interrupted refresh")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with monkeypatch.context() as fault:
        fault.setattr(Path, "lstat", lstat)
        with pytest.raises(RuntimeError, match="interrupted refresh"):
            observer.read((root, later))
    reads.clear()
    restored = observer.read((root,))
    assert sorted(job.record.run_id for job in restored.jobs) == ["A", "B"]
    assert all(job.report is not None for job in restored.jobs)
    assert reads == [], "failed partial refresh discarded committed parses"
    assert [
        (original_read(p), p.stat().st_mtime_ns, p.stat().st_size) for p in (report_a, report_b)
    ] == before


def test_successful_refresh_retires_absent_history_without_repair_or_deletion(
    tmp_path, monkeypatch
):
    roots = [tmp_path / str(number) / "jobs" for number in range(2)]
    reports = [write_job(root, str(number)) for number, root in enumerate(roots)]
    before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_size) for p in reports]
    observer = JobObserver(tmp_path)
    observer.read(roots)
    observer.read(roots[:1])
    original = Path.read_bytes
    reads = []

    def read_bytes(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    restored = observer.read(roots)

    assert len(restored.jobs) == 2
    assert reads == [reports[1]]
    assert [(original(p), p.stat().st_mtime_ns, p.stat().st_size) for p in reports] == before


def rec(run="one", **kwargs):
    return job_records.JobRecord(run, "sim", "2026-10-08T10:00:00Z", 60, **kwargs)


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_multi_roots_duplicate_ids_disconnected_history_and_byte_identity(tmp_path, monkeypatch):

    roots = [
        tmp_path / name / "jobs"
        for name in ("interactive", "active1", "active2", "finished", "abandoned")
    ]
    for number, root in enumerate(roots):
        job_records.write_record(
            rec(
                "duplicate" if number < 2 else str(number),
                session_key="codex:expired",
                status="done" if number > 2 else "running",
            ),
            root,
        )
    monkeypatch.setattr(
        job_slots.SlotStore, "reap", lambda *_a: pytest.fail("observational read reaped work")
    )
    monkeypatch.setattr(
        job_slots.SlotStore, "snapshot", lambda *_a: pytest.fail("used mutating snapshot")
    )
    before = files(tmp_path)
    snapshot = JobObserver(tmp_path, slots_root=tmp_path / "slots").read(roots)
    assert len(snapshot.jobs) == 5
    assert len({job.key for job in snapshot.jobs}) == 5
    assert all(job.record.session_key == "codex:expired" for job in snapshot.jobs)
    assert snapshot.diagnostics == ("ambiguous legacy run ID: duplicate",)
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    "status,pid,identity,state",
    [
        ("running", None, None, "spawning"),
        ("running", 99, None, "unavailable"),
        ("running", 99, ProcessIdentity(99, "missing", 101).to_payload(), "orphaned"),
        ("failed", None, None, "failed"),
        ("done", None, None, "completed"),
        ("cancelled", None, None, "cancelled"),
    ],
)
def test_job_states_without_lifecycle_mutation(tmp_path, status, pid, identity, state):
    root = tmp_path / "jobs"
    job_records.write_record(rec(status=status, pid=pid, process_identity=identity), root)
    before = files(tmp_path)
    assert JobObserver(tmp_path, proc_root=tmp_path / "proc").read((root,)).jobs[0].state == state
    assert files(tmp_path) == before


@pytest.mark.parametrize("slot,state", [("w", "queued"), ("h", "running")])
def test_observational_slot_state_and_corrupt_entry_isolation(tmp_path, monkeypatch, slot, state):
    from booley.runtime.pid import ProcessObservation, ProcessState

    identity = ProcessIdentity(99, "fixture", 101)
    root = tmp_path / "jobs"
    job_records.write_record(rec(pid=99, process_identity=identity.to_payload()), root)
    slots = tmp_path / "slots/heavy"
    slots.mkdir(parents=True)
    token = slots / job_slots._entry_name(slot, 1, 1, 99, 0)
    token.write_text(json.dumps({"owner_identity": identity.to_payload()}))
    (slots / job_slots._entry_name("h", 1, 2, 100, 0)).write_text("[]")
    monkeypatch.setattr(
        _observer, "observe_process", lambda *_a, **_k: ProcessObservation(ProcessState.RUNNING)
    )
    monkeypatch.setattr(job_slots.SlotStore, "reap", lambda *_: pytest.fail("reaped a stale slot"))
    before = files(tmp_path)
    view = JobObserver(tmp_path, slots_root=slots.parent).read((root,)).jobs[0]
    assert view.state == state
    assert files(tmp_path) == before


def test_snapshot_indexes_reports_and_progress_through_parent_link(tmp_path, monkeypatch):
    from booley.flows.progress_lifecycle import progress_document
    from booley.runtime.job_observation import _artifacts as job_artifacts

    real = tmp_path / "real"
    project = real / "project"
    root = project / "logs/.runtime/jobs"
    job_records.write_record(rec(status="done"), root)
    directory = root.parent / "flow-reports/sim/1"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps({"run_id": "one", "passed": True}))
    (directory / "progress.json").write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="one",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
                extra={"stage": "simulation"},
            )
        )
    )
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    before = files(real)
    monkeypatch.setattr(
        job_artifacts,
        "read_progress_for_run",
        lambda *_a, **_k: pytest.fail("indexed progress used fallback"),
    )
    view = JobObserver(alias / "project").read((alias / "project/logs/.runtime/jobs",)).jobs[0]
    assert view.report["passed"] is True
    assert view.stage == "simulation"
    assert files(real) == before


def test_snapshot_missing_progress_reuses_unrelated_checkpoint(tmp_path, monkeypatch):
    from booley.flows import progress_lifecycle

    root = tmp_path / "jobs"
    job_records.write_record(rec("absent", status="done"), root)
    path = root.parent / "flow-reports/sim/1/progress.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            progress_lifecycle.progress_document(
                flow="sim",
                run_id="other",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    original = progress_lifecycle._read_json_object_nofollow
    reads = []

    def read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(progress_lifecycle, "_read_json_object_nofollow", read)
    observer = JobObserver(tmp_path)
    observer.read((root,))
    reads.clear()
    observer.read((root,))
    assert reads == []


def test_snapshot_shared_artifact_directory_checked_once(tmp_path, monkeypatch):
    from dataclasses import replace
    from pathlib import Path

    root = tmp_path / "logs/.runtime/jobs"
    for endpoint in ("sim", "lint"):
        job_records.write_record(replace(rec(endpoint, status="done"), endpoint=endpoint), root)
        path = root.parent / "flow-reports" / endpoint / "1/report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"run_id": endpoint, "passed": True}))
    namespace = root.parent / "flow-reports"
    original = Path.lstat
    checks = []

    def lstat(path, *args, **kwargs):
        if path == namespace:
            checks.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    JobObserver(tmp_path).read((root,))
    assert checks == [namespace]


def test_outside_project_interactive_job_remains_observable_without_artifacts(tmp_path):
    root = tmp_path / "outside/jobs"
    project = tmp_path / "project"
    project.mkdir()
    job_records.write_record(rec(status="done"), root)
    directory = root.parent / "flow-reports/sim/1"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps({"run_id": "one", "passed": True}))
    snapshot = JobObserver(project).read((root,))
    assert len(snapshot.jobs) == 1
    assert snapshot.jobs[0].report is None
    assert f"Job artifact root unavailable: {root}" in snapshot.diagnostics


def test_job_history_keeps_the_newest_records_and_reports_truncation(tmp_path, monkeypatch):
    import os

    roots = [tmp_path / name / "jobs" for name in ("first", "second")]
    # The oldest records sit in the root that is scanned first.
    for root, run in (
        (roots[0], "old"),
        (roots[0], "older"),
        (roots[1], "new"),
        (roots[1], "newest"),
    ):
        job_records.write_record(rec(run, status="done"), root)
    stamps = {"older": 100, "old": 200, "new": 300, "newest": 400}
    for root in roots:
        for path in root.glob("*.json"):
            os.utime(path, (stamps[path.stem], stamps[path.stem]))
    full = JobObserver(tmp_path).read(roots)
    assert len(full.jobs) == 4
    assert not any("truncated" in line for line in full.diagnostics)
    monkeypatch.setattr(_observer, "MAX_JOBS", 2)
    cut = JobObserver(tmp_path).read(roots)
    assert {job.record.run_id for job in cut.jobs} == {"new", "newest"}
    assert "Job history truncated: showing the newest 2 of 4 records" in cut.diagnostics
    monkeypatch.setattr(_observer, "MAX_SCANNED_JOBS", 3)
    bounded = JobObserver(tmp_path).read(roots)
    assert (
        "Job history truncated: showing the newest 2 of the first 3 records examined; "
        "older interactive sessions were not examined"
    ) in bounded.diagnostics
