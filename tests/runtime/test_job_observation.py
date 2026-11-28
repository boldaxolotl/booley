"""JobObserver's explicit-input and refresh-generation contracts."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest
from tests.runtime.test_job_artifacts import artifact_reads, progress
from tests.sharing_error_support import hold_opens

from booley.runtime import job_records, job_slots

# private: _observer is imported only to lower its record bounds and set its clock (see uses).
from booley.runtime.job_observation import JobObserver, _observer
from booley.runtime.pid import ProcessIdentity, capture_process_identity

# A PID above the kernel limit, absent from the empty proc root the tests pass.
DEAD = ProcessIdentity(4_194_310, "boot", 1)


def write_job(root: Path, run_id: str, **kwargs) -> Path:
    job_records.write_record(
        job_records.JobRecord(run_id, "sim", "2026-10-08T10:00:00Z", 60, status="done", **kwargs),
        root,
    )
    report = root.parent / "flow-reports/sim/1/report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"run_id": run_id, "passed": True}))
    return report


def rec(run="one", **kwargs):
    return job_records.JobRecord(run, "sim", "2026-10-08T10:00:00Z", 60, **kwargs)


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def stamps(*paths):
    return [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size) for path in paths]


@pytest.fixture
def me() -> ProcessIdentity:
    """The test process: a real, live Job owner on every platform."""
    identity = capture_process_identity(os.getpid())
    assert identity is not None
    return identity


def own(**extra):
    """A claim Job admission writes for the Job's own process, labelled with its run ID."""
    return {"job": job_slots.JobLabel("one", "sim", "a").to_payload(), **extra}


def mem(mib):
    return job_slots.MemoryClaim(1000.0, reservation_mb=mib).to_payload()


def holding(mib, run):
    label = job_slots.JobLabel(run, "synth", "a").to_payload()
    return {"created_at": "2026-10-08T10:00:00Z", "memory": mem(mib), "job": label}


def claims(tmp_path, me, entries, **record):
    """Observe the Job of *me* beside memory-class *entries* ``(kind, pid, payload)``.

    A ``None`` PID is an entry of the Job's own process. The read must change no file.
    """
    root = tmp_path / "jobs"
    job_records.write_record(rec(pid=me.pid, process_identity=me.to_payload(), **record), root)
    memory = tmp_path / "slots/memory"
    memory.mkdir(parents=True)
    mine = {"owner_identity": me.to_payload()}
    for seq, (kind, other, entry) in enumerate(entries, 1):
        pid, payload = (me.pid, {**mine, **entry}) if other is None else (other, entry)
        # private: entry names are the slot store's on-disk format, and SlotStore.submit
        # admits, reaps and refuses the corrupt, unlabelled and foreign entries these cases
        # need, so no public call writes them.
        name = job_slots._entry_name(kind, 1, seq, pid, 0)
        (memory / name).write_text(json.dumps(payload), encoding="utf-8")
    before = files(tmp_path)
    view = JobObserver(tmp_path, slots_root=memory.parent).read((root,)).jobs[0]
    assert files(tmp_path) == before
    return view


def test_only_explicit_roots_are_observed_and_repeated_roots_keep_one_identity(tmp_path):
    selected = tmp_path / "selected/jobs"
    unselected = tmp_path / "job_logs/other/.runtime/jobs"
    write_job(selected, "one")
    write_job(unselected, "other")

    snapshot = JobObserver(tmp_path).read((selected, selected))

    assert [job.key for job in snapshot.jobs] == [(str(selected), "one")]
    assert snapshot.jobs[0].report == {"run_id": "one", "passed": True}
    assert snapshot.diagnostics == ()


def test_record_finished_after_listing_is_not_orphaned(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    job_records.write_record(rec(pid=DEAD.pid, process_identity=DEAD.to_payload()), root)
    stat = tmp_path / "proc" / str(DEAD.pid) / "stat"
    read_text = Path.read_text

    def owner_exits_as_observed(path, *args, **kwargs):
        if path != stat:
            return read_text(path, *args, **kwargs)
        done = job_records.read_record("one", root)
        assert done is not None
        done.status, done.exit_code, done.ended_at = "done", 0, "2026-10-08T10:00:05Z"
        job_records.write_record(done, root)
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "read_text", owner_exits_as_observed)

    snapshot = JobObserver(tmp_path, proc_root=tmp_path / "proc").read((root,))

    assert [(job.record.status, job.state) for job in snapshot.jobs] == [("done", "completed")]


def test_metadata_validator_rejects_before_artifact_io_and_isolates_the_record(
    tmp_path, monkeypatch
):
    accepted, rejected = (tmp_path / name / "jobs" for name in ("accepted", "rejected"))
    allowed = write_job(accepted, "good")
    forbidden = write_job(rejected, "bad")
    reads = artifact_reads(monkeypatch)

    def validate(record):
        if record.run_id == "bad":
            raise ValueError("invalid context metadata")

    snapshot = JobObserver(tmp_path, validate_record=validate).read((accepted, rejected))

    assert [job.record.run_id for job in snapshot.jobs] == ["good"]
    assert snapshot.diagnostics == (f"Job record unavailable: {rejected / 'bad.json'}",)
    assert allowed in reads and forbidden not in reads, "rejected metadata reached artifacts"


@pytest.mark.parametrize("error", [None, OSError, job_records.JobRecordError, RuntimeError])
def test_only_a_successful_refresh_commits_its_artifact_generation(tmp_path, monkeypatch, error):
    roots = [tmp_path / str(number) / "jobs" for number in range(2)]
    reports = [write_job(root, str(number)) for number, root in enumerate(roots)]
    before = stamps(*reports)
    failing = []

    def validate(_record):
        if failing:
            raise failing[0]("metadata adapter failed")

    observer = JobObserver(tmp_path, validate_record=validate)
    observer.read(roots)
    if error is None:
        observer.read(roots[:1])
    else:
        failing.append(error)
        with pytest.raises(error, match="metadata adapter failed"):
            observer.read(roots[:1])
        failing.clear()

    reads = artifact_reads(monkeypatch)
    restored = observer.read(roots)

    assert len(restored.jobs) == 2
    assert all(job.report is not None for job in restored.jobs)
    # A successful refresh retired the unselected root's parse; a failed one committed nothing.
    assert reads == ([reports[1]] if error is None else [])
    assert stamps(*reports) == before, "a refresh repaired or touched a report"


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
    before = stamps(report_a, report_b)
    original_open, original_stat = Path.open, Path.lstat
    inspected = []

    def lstat(path, *args, **kwargs):
        if path == report_b:
            inspected.append(path)
            raise OSError("temporarily hidden report")
        return original_stat(path, *args, **kwargs)

    def opening(path, *args, **kwargs):
        if path == failing_report:
            assert inspected, "first endpoint was not indexed before failure"
            raise RuntimeError("interrupted refresh")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "lstat", lstat)
        fault.setattr(Path, "open", opening)
        with pytest.raises(RuntimeError, match="interrupted refresh"):
            observer.read((root, later))
    reads = artifact_reads(monkeypatch)
    restored = observer.read((root,))
    assert sorted(job.record.run_id for job in restored.jobs) == ["A", "B"]
    assert all(job.report is not None for job in restored.jobs)
    assert reads == [], "failed partial refresh discarded committed parses"
    assert stamps(report_a, report_b) == before


def test_multi_roots_duplicate_ids_disconnected_history_and_byte_identity(tmp_path):
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
    before = files(tmp_path)
    snapshot = JobObserver(tmp_path).read(roots)
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
def test_observational_slot_state_and_corrupt_entry_isolation(tmp_path, me, slot, state):
    gone = ProcessIdentity(4_194_305, "fixture", 101).to_payload()
    entries = [
        (slot, None, own()),
        ("h", 100, []),
        # A reaping read would delete this dead owner's entry.
        ("h", 4_194_305, {"owner_identity": gone}),
    ]
    view = claims(tmp_path, me, entries)
    assert (view.state, view.queue) == (state, "")


@pytest.mark.parametrize(
    "entries,position,queue",
    [
        (
            [
                ("h", 7, holding(200.0, "run-1")),
                ("h", 8, holding(500.0, "run-2")),
                ("w", None, own(memory=mem(400.0))),
            ],
            1,
            "position 1: needs 400 MiB; 300 MiB free of the 1,000 MiB Memory Budget; "
            "largest holders: run-2 (synth, a) 500 MiB, run-1 (synth, a) 200 MiB",
        ),
        (
            [
                ("h", 7, holding(700.0, "run-1")),
                ("w", 8, holding(400.0, "run-2")),
                ("w", None, own(memory=mem(100.0))),
            ],
            2,
            "position 2: waits in queue order behind run-2 (synth, a); "
            "needs 100 MiB; 300 MiB free of the 1,000 MiB Memory Budget; "
            "largest holders: run-1 (synth, a) 700 MiB",
        ),
    ],
    ids=["memory", "queue-order"],
)
def test_queued_job_reports_its_wait(tmp_path, me, entries, position, queue):
    view = claims(tmp_path, me, entries)
    assert (view.state, view.queue_position, view.queue) == ("queued", position, queue)


# A work item's claim carries its Job's run ID and says it is a work item (or no label).
_ITEM_LABEL = {"run_id": "one", "flow": "sim", "target": "a", "test": "alpha", "work_item": True}
# A mutation coordinator: a memory-class companion claim reserving nothing.
_COORDINATOR = ("h", None, own(memory={"budget_mb": 1000, "reservation_mb": 0, "companion": True}))
# `booley session enter` exports the execution ID, so a CLI Job's own claim is execution-owned.
_EXECUTION = {"owner_kind": "execution", "execution_id": "a1" * 16}
# The item's claim is unlabelled: it inherits the session's execution ID too.
CLI_CAMPAIGN_ITEM_WAITS = [
    ("h", None, own(**_EXECUTION, memory=_COORDINATOR[2]["memory"])),
    ("w", None, {**_EXECUTION, "memory": mem(400.0)}),
]


def _beside_other_holder(kind):
    mine = own(**_EXECUTION, memory=mem(400.0))
    other = {"created_at": "2026-10-08T10:00:00Z", "memory": mem(500.0)}
    return [("h", 7, other), (kind, None, mine)]


@pytest.mark.parametrize(
    "entries,state,position",
    [
        # A work item's own claim: same PID, but a child execution of the Job.
        ([("h", None, own()), ("w", None, {"owner_kind": "execution"})], "running", None),
        ([_COORDINATOR, ("h", None, {})], "running", None),
        ([_COORDINATOR, ("w", None, {})], "queued", None),
        ([_COORDINATOR, ("h", None, {"job": _ITEM_LABEL})], "running", None),
        ([_COORDINATOR, ("w", None, {"job": _ITEM_LABEL})], "queued", None),
        (_beside_other_holder("w"), "queued", 1),
        (_beside_other_holder("h"), "running", None),
        (CLI_CAMPAIGN_ITEM_WAITS, "queued", 1),
    ],
    ids=[
        "child-lane-claims",
        "item-holds",
        "item-waits",
        "labelled-item-holds",
        "labelled-item-waits",
        "own-execution-waits",
        "own-execution-holds",
        "cli-campaign-item-waits",
    ],
)
def test_job_state_follows_the_claim_that_speaks_for_it(tmp_path, me, entries, state, position):
    # A waiting claim must outrank a record that says the run already started.
    started = "2026-10-08T10:00:01Z" if state == "queued" else None
    view = claims(tmp_path, me, entries, run_started_at=started)
    assert (view.state, view.queue_position) == (state, position)


def test_snapshot_indexes_reports_and_progress_through_parent_link(tmp_path):
    real = tmp_path / "real"
    project = real / "project"
    root = project / "logs/.runtime/jobs"
    path = write_job(root, "one")
    progress(path.with_name("progress.json"), "one", stage="simulation")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    before = files(real)
    view = JobObserver(alias / "project").read((alias / "project/logs/.runtime/jobs",)).jobs[0]
    assert view.report["passed"] is True
    assert view.stage == "simulation"
    assert files(real) == before


def test_outside_project_interactive_job_remains_observable_without_artifacts(tmp_path):
    root = tmp_path / "outside/jobs"
    project = tmp_path / "project"
    project.mkdir()
    write_job(root, "one")
    snapshot = JobObserver(project).read((root,))
    assert len(snapshot.jobs) == 1
    assert snapshot.jobs[0].report is None
    assert f"Job artifact root unavailable: {root}" in snapshot.diagnostics


def test_job_history_keeps_the_newest_records_and_reports_truncation(tmp_path, monkeypatch):
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
    # private: the bounds are 4,096 and 16,384 records, too many to write per test.
    monkeypatch.setattr(_observer, "MAX_JOBS", 2)
    cut = JobObserver(tmp_path).read(roots)
    assert {job.record.run_id for job in cut.jobs} == {"new", "newest"}
    assert "Job history truncated: showing the newest 2 of 4 records" in cut.diagnostics
    monkeypatch.setattr(_observer, "MAX_SCANNED_JOBS", 3)
    bounded = JobObserver(tmp_path).read(roots)
    assert (
        "Job history truncated: showing the newest 2 of the first 3 records examined; "
        "finished Jobs of older interactive sessions are not shown"
    ) in bounded.diagnostics


def test_history_cuts_never_drop_unfinished_records(tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "a/jobs", tmp_path / "b/jobs"
    records = (
        (root_a, rec("fin-new", status="done"), 400),
        (root_a, rec("fin-old", status="done"), 300),
        (root_a, rec("orphan", pid=DEAD.pid, process_identity=DEAD.to_payload()), 150),
        (root_a, rec("spawn-a"), 100),
        (root_b, rec("spawn-b"), 50),
    )
    for root, record, stamp in records:
        job_records.write_record(record, root)
        os.utime(root / f"{record.run_id}.json", (stamp, stamp))
    # private: the bounds are 4,096 and 16,384 records, too many to write per test.
    monkeypatch.setattr(_observer, "MAX_JOBS", 2)
    monkeypatch.setattr(_observer, "MAX_SCANNED_JOBS", 4)
    empty = tmp_path / "proc"
    empty.mkdir()

    snapshot = JobObserver(tmp_path, proc_root=empty).read((root_a, root_b))

    assert sorted(job.record.run_id for job in snapshot.jobs) == [
        "fin-new",
        "fin-old",
        "spawn-a",
        "spawn-b",
    ]
    assert (
        "Job history truncated: showing the newest 2 of the first 4 records examined; "
        "finished Jobs of older interactive sessions are not shown"
    ) in snapshot.diagnostics


_AGES = itertools.count(1_000)


def _age(root: Path) -> None:
    """Back-date *root* to a mtime unlike any earlier one.

    Windows reports a directory's creation time as its ctime, so only the mtime tells the
    index that the directory changed; a repeated value would look like no change there.
    """
    stamp = next(_AGES)
    os.utime(root, (stamp, stamp))


def test_unfinished_index_sees_new_records_and_retries_unreadable_ones(tmp_path, monkeypatch):
    root = tmp_path / "old/jobs"
    job_records.write_record(rec("done", status="done"), root)
    _age(root)
    observer = JobObserver(tmp_path)
    assert observer.read((), unfinished_roots=(root,)).jobs == ()

    job_records.write_record(rec("first"), root)
    _age(root)
    assert [job.record.run_id for job in observer.read((), unfinished_roots=(root,)).jobs] == [
        "first"
    ]

    job_records.write_record(rec("second"), root)
    _age(root)
    # A sharing error that outlasts the read retries once.
    hold_opens(monkeypatch, {root / "second.json": 2})
    assert [job.record.run_id for job in observer.read((), unfinished_roots=(root,)).jobs] == [
        "first"
    ]
    assert sorted(
        job.record.run_id for job in observer.read((), unfinished_roots=(root,)).jobs
    ) == ["first", "second"]


def test_recently_changed_root_is_relisted(tmp_path, monkeypatch):
    root = tmp_path / "old/jobs"
    job_records.write_record(rec("done", status="done"), root)
    _age(root)
    stamp = root.stat().st_mtime_ns
    listed = []
    scandir = os.scandir

    def spy(path):
        if path == root:
            listed.append(path)
        return scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    observer = JobObserver(tmp_path)
    # private: time_ns is the clock name the observer module owns (principle 16), and only a
    # clock just past the mtime pins the QUIET_NS margin.
    monkeypatch.setattr(_observer, "time_ns", lambda: stamp + 1_000_000_000)
    observer.read((), unfinished_roots=(root,))
    observer.read((), unfinished_roots=(root,))
    assert listed.count(root) == 2
    monkeypatch.setattr(_observer, "time_ns", lambda: stamp + 3_000_000_000)
    observer.read((), unfinished_roots=(root,))
    observer.read((), unfinished_roots=(root,))
    assert listed.count(root) == 3


def test_failed_directory_listing_is_retried_not_cached_as_empty(tmp_path, monkeypatch):
    root = tmp_path / "old/jobs"
    job_records.write_record(rec("first"), root)
    _age(root)
    scandir = os.scandir
    failing = [True]

    def flaky(path):
        if failing and path == root:
            failing.clear()
            raise OSError("too many open files")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    observer = JobObserver(tmp_path)
    assert observer.read((), unfinished_roots=(root,)).jobs == ()
    assert [job.record.run_id for job in observer.read((), unfinished_roots=(root,)).jobs] == [
        "first"
    ]
