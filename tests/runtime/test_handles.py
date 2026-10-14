"""Persistent display identity contracts across processes, failures and legacy state."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import symlink_or_skip

from booley.mcp import session_registry
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import handle_maintenance, handles, job_records

STAMP = "2026-10-14T10:00:00Z"


def attribution(key="codex:one"):
    return Attribution(key, "thread", "/work", "repo:main", namespace="fixture")


def test_session_survives_restart_pruning_and_reregistration(tmp_path):
    registry = SessionRegistry(tmp_path, quiet_after=10)
    registry.upsert(attribution(), "sim", "completed", now=100)
    assert handles.HandleStore(tmp_path).load().labels() == {"codex:one": "S1"}
    assert registry.prune(now=200, namespace="fixture") == 1
    restarted = SessionRegistry(tmp_path)
    restarted.upsert(attribution("codex:two"), "lint", "completed", now=201)
    restarted.upsert(attribution(), "sim", "completed", now=202)
    assert handles.HandleStore(tmp_path).load().labels() == {"codex:one": "S1", "codex:two": "S2"}
    assert len(restarted.snapshot().rows) == 2
    assert not restarted.snapshot().diagnostics


def test_fallback_migration_and_conflicting_allocations(tmp_path):
    store = handles.HandleStore(tmp_path)
    old = "worktree:path:/work"
    with handles.session_writer(tmp_path):
        state = store.load()
        assert state.reserve(old, "S") == "S1"
        assert state.reserve("worktree:repo:main", "S", predecessors=(old,)) == "S1"
        store.save(state)
    assert store.load().labels() == {old: "S1", "worktree:repo:main": "S1"}
    with handles.session_writer(tmp_path):
        state = store.load()
        state.reserve("other-old", "S")
        state.reserve("other-canonical", "S")
        assert state.reserve("other-canonical", "S", predecessors=("other-old",)) == "S3"
        store.save(state)
    assert store.load().labels()["other-old"] == "S2"
    assert store.load().next_number == 4


def test_registry_migration_keeps_handle_after_old_row_was_pruned(tmp_path):
    registry = SessionRegistry(tmp_path, quiet_after=10)
    old = Attribution(
        "worktree:path:/work", "worktree", "/work", "path:/work", namespace="fixture"
    )
    registry.upsert(old, "sim", "completed", now=100)
    registry.prune(now=200, namespace="fixture")
    canonical = Attribution(
        "worktree:repo:main", "worktree", "/work", "repo:main", namespace="fixture"
    )
    registry.upsert(canonical, "lint", "completed", now=201)
    assert handles.HandleStore(tmp_path).load().labels()[canonical.key] == "S1"


def test_fully_legacy_fallback_keeps_job_owner_alias_during_backfill(tmp_path, monkeypatch):
    from dataclasses import replace

    old = Attribution(
        "worktree:path:/work", "worktree", "/work", "path:/work", namespace="fixture"
    )
    canonical = replace(old, key="worktree:repo:main", worktree_key="repo:main")
    registry = SessionRegistry(tmp_path)
    registry.root.mkdir(parents=True)
    registry.path(old.key).write_bytes(session_registry.SessionRow(old, STAMP, STAMP).payload())
    job = job_records.JobRecord(
        "old-job", "sim", STAMP, 60, status="done", session_key=old.key, work_dir="/work"
    )
    root = tmp_path / ".interactive_logs" / "old" / ".runtime" / "jobs"
    job_records.write_record(job, root)
    monkeypatch.setattr(session_registry, "canonical_attribution", lambda _facts: canonical)
    registry.backfill_handles()
    handle_maintenance.backfill_job_handles(tmp_path)
    labels = handles.HandleStore(tmp_path).load().labels()
    assert labels[canonical.key] == labels[old.key] == "S1"


@pytest.mark.parametrize("spelling", ["absolute", "relative", "symlink", "subdirectory"])
def test_job_creation_before_registry_migration_preserves_existing_handle(
    tmp_path, monkeypatch, spelling
):
    from booley.mcp.call_context import CallContext
    from booley.mcp.job_lifecycle import JobLifecycle

    root = tmp_path / "work"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "rtl").mkdir()
    monkeypatch.chdir(root)
    work_dir = root
    if spelling == "relative":
        work_dir = Path()
    elif spelling == "symlink":
        work_dir = tmp_path / "work-alias"
        symlink_or_skip(work_dir, root)
    elif spelling == "subdirectory":
        work_dir = Path("rtl")
    old = Attribution(
        "worktree:path:" + str(root),
        "worktree",
        str(root),
        "path:" + str(root),
        namespace="fixture",
    )
    canonical = Attribution(
        "worktree:repo:main", "worktree", str(root), "repo:main", namespace="fixture"
    )
    registry = SessionRegistry(tmp_path)
    registry.upsert(old, "sim", "completed", now=100)
    monkeypatch.setattr("booley.runtime.booley_dir.resolve_booley_dir", lambda: tmp_path)
    context = CallContext(
        work_dir, tmp_path / "jobs", None, None, None, {}, session_key=canonical.key
    )
    job = job_records.JobRecord("new-job", "sim", STAMP, 60)
    JobLifecycle._stamp_context(job, context)
    registry.upsert(canonical, "sim", "completed", now=101)
    labels = handles.HandleStore(tmp_path).load().labels()
    assert labels[canonical.key] == labels[old.key] == "S1"


def test_corrupt_session_handles_preserve_presence_publication(tmp_path):
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text("broken")
    registry = SessionRegistry(tmp_path)
    registry.upsert(attribution(), "sim", "completed", now=100)
    assert registry.snapshot().rows[0].attribution.key == "codex:one"
    assert store.path.read_text() == "broken"


def test_reservation_survives_failed_row_publication(tmp_path, monkeypatch):
    registry = SessionRegistry(tmp_path)
    real_write = session_registry.atomic_replace_bytes
    monkeypatch.setattr(
        session_registry,
        "atomic_replace_bytes",
        lambda *_a: (_ for _ in ()).throw(OSError("disk")),
    )
    with pytest.raises(OSError, match="disk"):
        registry.upsert(attribution(), "sim", "completed", now=100)
    assert not registry.path("codex:one").exists()
    assert handles.HandleStore(tmp_path).load().labels()["codex:one"] == "S1"
    monkeypatch.setattr(session_registry, "atomic_replace_bytes", real_write)
    registry.upsert(attribution(), "sim", "completed", now=101)
    assert handles.HandleStore(tmp_path).load().next_number == 2


@pytest.mark.parametrize(
    "endpoint,prefix",
    [
        ("sim", "SIM"),
        ("simulate", "SIM"),
        ("sim_baseline", "SIM"),
        ("asic_synthesize", "SYN"),
        ("synth", "SYN"),
        ("lint", "LINT"),
        ("fpga_impl", "FPGA"),
        ("fpga", "FPGA"),
        ("custom", "JOB"),
    ],
)
def test_job_kinds_and_project_wide_counter(tmp_path, endpoint, prefix):
    handles.register_job(tmp_path, "first", endpoint, None)
    handles.register_job(tmp_path, "second", "lint", None)
    handles.register_job(tmp_path, "first", endpoint, None)
    state = handles.HandleStore(tmp_path, jobs=True).load()
    assert state.entries == {"first": f"J-{prefix}1", "second": "J-LINT2"}
    assert state.next_number == 3
    handles.register_job(tmp_path / "another-project", "second", "lint", None)
    assert (
        handles.HandleStore(tmp_path / "another-project", jobs=True).load().entries["second"]
        == "J-LINT1"
    )


def _allocate_worker(root, number, start):
    start.wait(timeout=10)
    handles.register_job(Path(root), f"job-{number}", "sim", None)
    handles.register_job(Path(root), "shared", "lint", None)


def test_concurrent_processes_allocate_unique_stable_jobs(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(4)
    workers = [
        ctx.Process(target=_allocate_worker, args=(str(tmp_path), number, start))
        for number in range(4)
    ]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=20)
            assert not worker.is_alive()
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
    state = handles.HandleStore(tmp_path, jobs=True).load()
    assert len(state.entries) == 5
    assert len(set(state.entries.values())) == 5
    assert state.next_number == 6


def _register_session_worker(root, number, start):
    start.wait(timeout=10)
    registry = SessionRegistry(Path(root))
    for _ in range(100):
        try:
            registry.upsert(attribution(f"codex:{number}"), "sim", "completed", now=100)
            return
        except BlockingIOError:
            time.sleep(0.01)
    raise RuntimeError("session registry remained busy")


def test_concurrent_sessions_use_registry_lock_for_unique_allocations(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(4)
    workers = [
        ctx.Process(target=_register_session_worker, args=(str(tmp_path), number, start))
        for number in range(4)
    ]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=20)
            assert not worker.is_alive()
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
    labels = handles.HandleStore(tmp_path).load().labels()
    assert set(labels.values()) == {"S1", "S2", "S3", "S4"}
    assert len(SessionRegistry(tmp_path).snapshot().rows) == 4


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": True},
        {"schema": 2},
        {"next_number": True},
        {"next_number": 1},
        {"entries": {"one": "S1", "two": "S1"}},
        {"entries": {"one": "S0"}},
        {"aliases": {"old": "absent"}},
        {"aliases": {"one": "one"}},
    ],
)
def test_invalid_map_is_diagnosed_never_reset(tmp_path, mutation):
    store = handles.HandleStore(tmp_path)
    store.save(handles.HandleMap({"one": "S1"}, next_number=2))
    data = json.loads(store.path.read_bytes())
    data.update(mutation)
    store.path.write_text(json.dumps(data))
    before = store.path.read_bytes()
    assert store.snapshot()[1]
    registry = SessionRegistry(tmp_path)
    registry.upsert(attribution(), "lint", "completed", now=100)
    assert registry.snapshot().rows[0].attribution.key == "codex:one"
    assert store.path.read_bytes() == before


def test_missing_read_and_symlink_refusal(tmp_path):
    store = handles.HandleStore(tmp_path)
    assert store.snapshot() == ({}, ())
    assert list(tmp_path.iterdir()) == []
    external = tmp_path / "outside.json"
    external.write_text("{}")
    store.root.mkdir(parents=True)
    symlink_or_skip(store.path, external)
    assert "symlink" in store.snapshot()[1][0]
    assert external.read_text() == "{}"


def test_deeply_nested_handle_map_is_isolated_as_a_diagnostic(tmp_path):
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    depth = sys.getrecursionlimit() + 1000
    store.path.write_text("[" * depth + "0" + "]" * depth)
    before = store.path.read_bytes()
    with pytest.raises(RecursionError):
        json.loads(before)
    assert store.snapshot()[0] == {}
    assert "handles unavailable" in store.snapshot()[1][0]
    assert store.path.read_bytes() == before


def test_legacy_relative_work_dir_does_not_alias_the_current_checkout(tmp_path, monkeypatch):
    root = tmp_path / "current"
    root.mkdir()
    monkeypatch.chdir(root)
    store = handles.HandleStore(tmp_path)
    state = handles.HandleMap()
    old = "worktree:path:" + str(root)
    state.reserve(old, "S")
    store.save(state)
    job = job_records.JobRecord(
        "historical-job",
        "sim",
        STAMP,
        60,
        status="done",
        session_key="worktree:repo:historical",
        work_dir=".",
    )
    job_records.write_record(job, tmp_path / ".interactive_logs" / "old" / ".runtime" / "jobs")
    handle_maintenance.backfill_job_handles(tmp_path)
    assert store.load().labels()[old] == "S1"
    assert store.load().labels()[job.session_key] == "S2"


@pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or not shutil.which("tar"),
    reason="POSIX worktree copy requires bash and tar",
)
def test_worktree_copy_excludes_both_handle_maps_and_keeps_configuration(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    project = parent / ".booley"
    project.mkdir()
    (project / "config.txt").write_text("Project configuration")
    handles.register_job(project, "parent-job", "sim", "codex:one")
    before = handles.HandleStore(project, jobs=True).path.read_bytes()
    destination = tmp_path / "worktree"
    destination.mkdir()
    script = Path(handles.__file__).with_name("worktree_create.sh").read_text()
    start = script.index('if [ "${BOOLEY_WORKTREE_PAIRED_PROJECT:-0}"')
    block = script[start : script.index("\nfi", start) + 3]
    env = {
        **os.environ,
        "CWD": str(parent),
        "WORKTREE_DIR": str(destination),
        "BOOLEY_WORKTREE_PAIRED_PROJECT": "0",
    }
    subprocess.run(
        ["bash", "-o", "pipefail", "-c", block],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    copied = destination / ".booley"
    assert (copied / "config.txt").read_text() == "Project configuration"
    assert not (copied / "runtime" / "sessions").exists()
    assert not (copied / "runtime" / "job-handles").exists()
    assert handles.HandleStore(project, jobs=True).path.read_bytes() == before


def test_dashboard_reader_joins_handles_without_mutating_files(tmp_path):
    from booley.harness.dashboard.jobs import JobReader

    job = job_records.JobRecord(
        "job-one", "sim", STAMP, 60, status="done", session_key="codex:one"
    )
    root = tmp_path / ".interactive_logs" / "old" / ".runtime" / "jobs"
    handles.register_job(tmp_path, job.run_id, job.endpoint, job.session_key)
    job_records.write_record(job, root)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    first = JobReader(tmp_path).read(())
    second = JobReader(tmp_path).read(())
    assert first.jobs[0].handle == second.jobs[0].handle == "J-SIM1"
    assert first.jobs[0].owner_handle == "S1"
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }


def test_legacy_backfill_is_idempotent_and_preserves_record_bytes(tmp_path):
    registry = SessionRegistry(tmp_path)
    registry.root.mkdir(parents=True)
    row = session_registry.SessionRow(attribution(), STAMP, STAMP)
    registry.path(row.attribution.key).write_bytes(row.payload())
    job = job_records.JobRecord("old-job", "fpga", STAMP, 60, session_key="codex:disconnected")
    root = tmp_path / ".interactive_logs" / "old" / ".runtime" / "jobs"
    job_records.write_record(job, root)
    before = (
        registry.path(row.attribution.key).read_bytes(),
        (root / "old-job.json").read_bytes(),
    )
    registry.backfill_handles()
    handle_maintenance.backfill_job_handles(tmp_path)
    handle_maintenance.backfill_job_handles(tmp_path)
    assert handles.HandleStore(tmp_path, jobs=True).load().entries == {"old-job": "J-FPGA1"}
    assert handles.HandleStore(tmp_path).load().labels() == {
        "codex:one": "S1",
        "codex:disconnected": "S2",
    }
    assert before == (
        registry.path(row.attribution.key).read_bytes(),
        (root / "old-job.json").read_bytes(),
    )


def test_job_allocation_failure_is_advisory(tmp_path, monkeypatch, caplog):
    store = handles.HandleStore(tmp_path, jobs=True)
    store.root.mkdir(parents=True)
    store.path.write_text("broken")
    handles.register_job(tmp_path, "job", "sim", "codex:owner")
    assert store.path.read_text() == "broken"
    assert handles.HandleStore(tmp_path).load().labels()["codex:owner"] == "S1"
    assert "Job handle unavailable" in caplog.text
    monkeypatch.setattr(
        handles, "atomic_replace_bytes", lambda *_a: (_ for _ in ()).throw(OSError("disk"))
    )
    handles.register_job(tmp_path / "unwritable", "job", "sim", None)
    assert "disk" in caplog.text


@pytest.mark.parametrize("broken", [False, True])
def test_baseline_allocates_before_launch_and_broken_maps_do_not_block_it(
    tmp_path, monkeypatch, broken
):
    from booley.mcp import sim_baseline_jobs

    store = handles.HandleStore(tmp_path, jobs=True)
    if broken:
        store.root.mkdir(parents=True)
        store.path.write_text("broken")
    launched = []

    def launch(*args, **kwargs):
        if not broken:
            assert store.load().entries == {"baseline-one": "J-SIM1"}
        launched.append(args[0])
        return SimpleNamespace()

    monkeypatch.setattr(sim_baseline_jobs.subprocess, "Popen", launch)
    monkeypatch.setattr(
        sim_baseline_jobs.threading,
        "Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None),
    )
    request = {
        "job_id": "baseline-one",
        "booley_dir": str(tmp_path),
        "jobs_root": str(tmp_path / "jobs"),
        "record": {"worktree_path": "/work", "session_key": "codex:one"},
    }
    sim_baseline_jobs.launch_baseline(tmp_path / "request.json", request)
    assert len(launched) == 1
    assert job_records.read_record("baseline-one", tmp_path / "jobs") is not None


def test_maintenance_failure_does_not_prevent_session_pruning(tmp_path, monkeypatch):
    from booley.mcp.session_observer import SessionObserver

    registry = SessionRegistry(tmp_path, quiet_after=10)
    registry.upsert(attribution(), "sim", "completed", now=100)
    handles.HandleStore(tmp_path).path.write_text("broken")
    observer = SessionObserver(now=lambda: 200)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    monkeypatch.setattr("booley.mcp.session_observer.namespace", lambda: "fixture")
    observer._prune()
    assert registry.snapshot().rows == ()


def test_full_dashboard_reader_keeps_maps_and_registry_bytes_unchanged(tmp_path, monkeypatch):
    from booley.harness.dashboard.model import DashboardReader

    registry = SessionRegistry(tmp_path)
    registry.upsert(attribution(), "sim", "completed", now=100)
    monkeypatch.setattr("booley.harness.dashboard.model.namespace", lambda: "fixture")
    monkeypatch.setattr("booley.harness.dashboard.model.time.time", lambda: 101)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    reader = DashboardReader(tmp_path, tmp_path)
    assert reader.read().session_handles["codex:one"] == "S1"
    assert reader.read().sessions[0].attribution.key == "codex:one"
    assert before == {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
