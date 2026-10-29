"""Persistent display identity contracts across processes, failures and legacy state."""

import json
import multiprocessing
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import symlink_or_skip

from booley.mcp import session_registry
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import handles, job_records

STAMP = "2026-10-14T10:00:00Z"


def attribution(key="codex:one", **changes):
    return replace(
        Attribution(key, "thread", "/work", "repo:main", namespace="fixture"), **changes
    )


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
        assert state.entries[old] == "S1"
        assert state.aliases["worktree:repo:main"] == old
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
    state = store.load()
    original = (dict(state.entries), dict(state.aliases))
    assert state.reserve("third", "S", predecessors=("worktree:repo:main",)) == "S1"
    assert all(state.entries[k] == v for k, v in original[0].items())
    assert all(state.aliases[k] == v for k, v in original[1].items())
    assert state.labels()[old] == state.labels()["third"] == "S1"


def test_registry_migration_keeps_handle_after_old_row_was_pruned(tmp_path):
    registry = SessionRegistry(tmp_path, quiet_after=10)
    old = attribution("worktree:path:/work", kind="worktree", worktree_key="path:/work")
    registry.upsert(old, "sim", "completed", now=100)
    registry.prune(now=200, namespace="fixture")
    canonical = replace(old, key="worktree:repo:main", worktree_key="repo:main")
    registry.upsert(canonical, "lint", "completed", now=201)
    assert handles.HandleStore(tmp_path).load().labels()[canonical.key] == "S1"


@pytest.mark.parametrize("spelling", ["absolute", "relative", "symlink", "subdirectory"])
def test_job_creation_before_registry_migration_preserves_existing_handle(
    tmp_path, monkeypatch, spelling
):
    from booley.mcp.call_context import CallContext
    from booley.mcp.job_lifecycle import JobLifecycle

    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    (root / "rtl").mkdir()
    monkeypatch.chdir(root)
    work_dir = {
        "absolute": root,
        "relative": Path(),
        "symlink": tmp_path / "alias",
        "subdirectory": Path("rtl"),
    }[spelling]
    if spelling == "symlink":
        symlink_or_skip(work_dir, root)
    old = attribution(
        "worktree:path:" + str(root),
        kind="worktree",
        work_dir=str(root),
        worktree_key="path:" + str(root),
    )
    canonical = replace(old, key="worktree:repo:main", worktree_key="repo:main")
    registry = SessionRegistry(tmp_path)
    registry.upsert(old, "sim", "completed", now=100)
    monkeypatch.setattr("booley.runtime.booley_dir.resolve_booley_dir", lambda: tmp_path)
    context = CallContext(
        work_dir, tmp_path / "jobs", None, None, None, {}, session_key=canonical.key
    )
    JobLifecycle._stamp_context(job_records.JobRecord("new-job", "sim", STAMP, 60), context)
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


def test_job_kinds_and_project_wide_counter(tmp_path_factory):
    for prefix, endpoints in {
        "SIM": ("sim", "simulate", "sim_baseline"),
        "SYN": ("asic_synthesize", "synth"),
        "LINT": ("lint", "lint_rtl"),
        "FPGA": ("fpga_impl", "fpga"),
        "JOB": ("custom",),
    }.items():
        for endpoint in endpoints:
            tmp_path = tmp_path_factory.mktemp(str(endpoint))
            handles.register_job(tmp_path, "first", endpoint, None)
            store = handles.HandleStore(tmp_path, jobs=True)
            state = store.load()
            state.next_number = 1
            store.save(state)
            handles.register_job(tmp_path, "second", "lint", None)
            handles.register_job(tmp_path, "first", endpoint, None)
            state = handles.HandleStore(tmp_path, jobs=True).load()
            assert state.entries == {"first": f"J-{prefix}1", "second": "J-LINT2"}, endpoint
            assert state.next_number == 3, endpoint
            handles.register_job(tmp_path / "another-project", "second", "lint", None)
            other = handles.HandleStore(tmp_path / "another-project", jobs=True).load()
            assert other.entries["second"] == "J-LINT1", endpoint


def _allocate_worker(root, number, start):
    start.wait(timeout=10)
    handles.register_job(Path(root), f"job-{number}", "sim", None)
    handles.register_job(Path(root), "shared", "lint", None)


def test_concurrent_processes_allocate_unique_stable_jobs(tmp_path):
    run_workers(tmp_path, _allocate_worker)
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
    run_workers(tmp_path, _register_session_worker)
    labels = handles.HandleStore(tmp_path).load().labels()
    assert set(labels.values()) == {"S1", "S2", "S3", "S4"}
    assert len(SessionRegistry(tmp_path).snapshot().rows) == 4


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": True},
        {"schema": 2},
        {"next_number": True},
        {"entries": {"one": 1}},
        {"aliases": {"old": 1}},
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


def test_handles_follow_a_symlinked_booley_directory(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    booley_dir = tmp_path / "project" / ".booley"
    booley_dir.parent.mkdir()
    symlink_or_skip(booley_dir, store_dir, target_is_directory=True)

    assert handles.reserve_session_handle(booley_dir, "codex:one") == "S1"
    handles.register_job(booley_dir, "run-1", "sim", "codex:two")

    assert handles.HandleStore(booley_dir).snapshot() == (
        {"codex:one": "S1", "codex:two": "S2"},
        (),
    )
    assert handles.HandleStore(booley_dir, jobs=True).snapshot() == ({"run-1": "J-SIM1"}, ())
    assert (store_dir / "runtime/sessions/handles/map.json").is_file()
    assert (store_dir / "runtime/job-handles/map.json").is_file()

    # A link below the Booley Directory is still refused.
    moved = store_dir / "moved"
    (store_dir / "runtime/job-handles").rename(moved)
    symlink_or_skip(store_dir / "runtime/job-handles", moved, target_is_directory=True)
    labels, diagnostics = handles.HandleStore(booley_dir, jobs=True).snapshot()
    assert labels == {} and "symlink" in diagnostics[0]


@pytest.mark.parametrize(
    "payload",
    ["[" * 50000 + "0" + "]" * 50000, "x" * (16 * 1024 * 1024 + 1)],
    ids=["deeply-nested", "oversized"],
)
def test_deeply_nested_or_oversized_map_is_diagnosed_without_mutation(tmp_path, payload):
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text(payload)
    labels, warnings = store.snapshot()
    assert labels == {}
    assert "handles unavailable" in warnings[0]
    assert store.path.read_text() == payload


@pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or (not shutil.which("tar")),
    reason="POSIX worktree copy requires bash and tar",
)
def test_worktree_copy_excludes_both_handle_maps_and_keeps_configuration(tmp_path):
    parent, destination = (tmp_path / "parent", tmp_path / "worktree")
    project = parent / ".booley"
    project.mkdir(parents=True)
    destination.mkdir()
    (project / "config.txt").write_text("Project configuration")
    handles.register_job(project, "parent-job", "sim", "codex:one")
    before = handles.HandleStore(project, jobs=True).path.read_bytes()
    script = Path(handles.__file__).with_name("worktree_create.sh").read_text()
    start = script.index('if [ "${BOOLEY_WORKTREE_PAIRED_PROJECT:-0}"')
    block = script[start : script.index("\nfi", start) + 3]
    env = dict(
        os.environ,
        CWD=str(parent),
        WORKTREE_DIR=str(destination),
        BOOLEY_WORKTREE_PAIRED_PROJECT="0",
    )
    command = ["bash", "-o", "pipefail", "-c", block]
    subprocess.run(command, env=env, check=True, capture_output=True, timeout=30)
    copied = destination / ".booley"
    assert (copied / "config.txt").read_text() == "Project configuration"
    assert not (copied / "runtime" / "sessions").exists()
    assert not (copied / "runtime" / "job-handles").exists()
    assert handles.HandleStore(project, jobs=True).path.read_bytes() == before


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
    # The scheduler publishes the Job record (and its handle) before launching.
    sim_baseline_jobs._publish_job_record(tmp_path / "request.json", request)
    sim_baseline_jobs.launch_baseline(tmp_path / "request.json", request)
    assert len(launched) == 1
    assert job_records.read_record("baseline-one", tmp_path / "jobs") is not None


def test_full_dashboard_reader_keeps_maps_and_registry_bytes_unchanged(
    tmp_path_factory, monkeypatch
):
    for map_state in ["allocated", "empty", "broken"]:
        tmp_path = tmp_path_factory.mktemp(str(map_state))
        from booley.harness.dashboard.model import DashboardReader

        registry = SessionRegistry(tmp_path)
        registry.upsert(attribution(), "sim", "completed", now=100)
        job = job_records.JobRecord("job-one", "sim", STAMP, 60, session_key="codex:one")
        handles.register_job(tmp_path, job.run_id, job.endpoint, job.session_key)
        job_records.write_record(job, tmp_path / ".interactive_logs/old/.runtime/jobs")
        store = handles.HandleStore(tmp_path)
        if map_state == "empty":
            store.save(handles.HandleMap())
        elif map_state == "broken":
            store.path.write_text("broken")
        monkeypatch.setattr("booley.harness.dashboard.model.namespace", lambda: "fixture")
        monkeypatch.setattr("booley.harness.dashboard.model.time.time", lambda: 101)
        load, reads = handles.HandleStore.load, []

        def counted_load(self, reads=reads, load=load):
            reads.append(self.jobs)
            return load(self)

        monkeypatch.setattr(handles.HandleStore, "load", counted_load)
        before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        reader = DashboardReader(tmp_path, tmp_path)
        for _ in range(2):
            reads.clear()
            snapshot = reader.read()
            assert reads.count(False) == 1, map_state
            assert snapshot.sessions[0].attribution.key == "codex:one", map_state
            assert snapshot.jobs.jobs[0].handle == "J-SIM1", map_state
            assert snapshot.jobs.jobs[0].owner_handle == (
                "S1" if map_state == "allocated" else ""
            ), map_state
            assert snapshot.session_handles == (
                {"codex:one": "S1"} if map_state == "allocated" else {}
            ), map_state
            diagnostics = (*snapshot.diagnostics, *snapshot.jobs.diagnostics)
            assert sum("Session handles unavailable" in d for d in diagnostics) == (
                map_state == "broken"
            ), map_state
            assert not any("pending" in d for d in diagnostics), map_state
        assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}, (
            map_state
        )
        monkeypatch.undo()


def test_pre_reserved_process_handle_is_reused_by_registry(tmp_path, monkeypatch):
    from booley.runtime.pid import ProcessIdentity
    from booley.runtime.session_activity import process_session_key

    reserve = handles.reserve_session_handle
    peer = ProcessIdentity(os.getpid() + 100000, "fixture", 101)
    key = process_session_key(peer)
    assert reserve(tmp_path, key) == "S1"
    store = handles.HandleStore(tmp_path)
    assert store.load().labels() == {key: "S1"}
    registry = SessionRegistry(tmp_path)
    assert registry.snapshot().rows == ()
    assert reserve(tmp_path, key) == "S1"
    monkeypatch.setattr(session_registry, "namespace", lambda *_: "fixture")
    facts = session_registry.resolve_attribution(
        tmp_path, metadata={}, client_name="claude-code", peer=peer
    )
    assert facts.kind == "process" and facts.key == key
    registry.upsert(facts, "status", "completed", now=100)
    registry.upsert(facts, "status", "completed", now=101)
    assert registry.snapshot().rows[0].attribution.key == key
    assert store.load().labels() == {key: "S1"}
    assert store.load().next_number == 2
    assert (
        reserve(tmp_path, process_session_key(ProcessIdentity(peer.pid + 1, "fixture", 102)))
        == "S2"
    )
    assert store.load().next_number == 3


def test_launch_reservation_preserves_corrupt_store(tmp_path):
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text("broken")
    with pytest.raises(ValueError):
        handles.reserve_session_handle(tmp_path, "pid:fixture:7:1")
    assert store.path.read_text() == "broken"


def test_launch_reservation_propagates_save_failure(tmp_path, monkeypatch):
    def fail_save(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(handles, "atomic_replace_bytes", fail_save)
    with pytest.raises(OSError, match="disk full"):
        handles.reserve_session_handle(tmp_path, "pid:fixture:7:1")
    assert not handles.HandleStore(tmp_path).path.exists()
    assert SessionRegistry(tmp_path).snapshot().rows == ()


def test_launch_reservation_uses_the_registry_writer_lock(tmp_path):
    with handles.session_writer(tmp_path), pytest.raises(BlockingIOError):
        handles.reserve_session_handle(tmp_path, "pid:fixture:7:1")
    assert not handles.HandleStore(tmp_path).path.exists()


def run_workers(tmp_path, target):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(4)
    workers = [
        ctx.Process(target=target, args=(str(tmp_path), number, start)) for number in range(4)
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


def test_shape_valid_maps_remain_usable(tmp_path_factory, caplog):
    for counter in [-1, 1, 99]:
        caplog.clear()
        tmp_path = tmp_path_factory.mktemp(str(counter))
        store = handles.HandleStore(tmp_path)
        entries = {"old": "S7", "duplicate": "S7", "unusual": "display", "collision": "S3"}
        aliases = {"prior": "old", "dangling": "missing", "collision": "old"}
        store.save(handles.HandleMap(entries, aliases, counter))
        before = store.path.read_bytes()
        state = store.load()
        assert state.next_number == max(counter, 8), counter
        assert state.labels() == {**entries, "prior": "S7"}, counter
        assert state.reserve("canonical", "S", predecessors=("dangling", "prior")) == "S7", counter
        assert state.entries == entries, counter
        assert state.aliases == {**aliases, "canonical": "old"}, counter
        assert state.reserve("new", "S") == f"S{max(counter, 8)}", counter
        assert store.path.read_bytes() == before, counter
        with pytest.raises(ValueError):
            state.reserve("dangling", "S")
        assert state.aliases["dangling"] == "missing", counter
        registry = SessionRegistry(tmp_path)
        registry.upsert(attribution("dangling"), "sim", "completed", now=100)
        handles.register_job(tmp_path, "job", "sim", "dangling")
        assert registry.snapshot().rows[0].attribution.key == "dangling", counter
        assert handles.HandleStore(tmp_path, jobs=True).load().entries["job"] == "J-SIM1", counter
        assert store.path.read_bytes() == before, counter
        assert "for dangling" in caplog.text and "for Job job" in caplog.text, counter


def test_maintenance_prunes_without_allocating_historical_handles(tmp_path_factory, monkeypatch):
    for broken in [False, True]:
        tmp_path = tmp_path_factory.mktemp(str(broken))
        from booley.mcp.session_observer import SessionObserver

        registry = SessionRegistry(tmp_path, quiet_after=10)
        registry.upsert(attribution(), "sim", "completed", now=100)
        store = handles.HandleStore(tmp_path)
        if broken:
            store.path.write_text("broken")
        else:
            store.path.unlink()
        job = job_records.JobRecord("old", "sim", STAMP, 60, session_key="codex:disconnected")
        root = tmp_path / ".interactive_logs/old/.runtime/jobs"
        job_records.write_record(job, root)
        before = (root / "old.json").read_bytes()
        observer = SessionObserver(now=lambda: 200)
        monkeypatch.setattr(observer, "_registry", lambda registry=registry: registry)
        monkeypatch.setattr("booley.mcp.session_observer.namespace", lambda: "fixture")
        observer._prune()
        assert registry.snapshot().rows == (), broken
        assert not handles.HandleStore(tmp_path, jobs=True).path.exists(), broken
        assert (root / "old.json").read_bytes() == before, broken
        assert store.path.read_text() == "broken" if broken else not store.path.exists(), broken


def _saved_handle_store(tmp_path):
    store = handles.HandleStore(tmp_path)
    with handles.session_writer(tmp_path):
        state = store.load()
        state.reserve("codex:one", "S")
        store.save(state)
    return store


def test_handle_map_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = _saved_handle_store(tmp_path)
    attempts = hold_opens(monkeypatch, {store.path: 1})
    assert store.load().labels() == {"codex:one": "S1"}
    assert attempts[store.path] == 2


def test_lasting_handle_map_sharing_error_is_a_snapshot_diagnostic(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = _saved_handle_store(tmp_path)
    attempts = hold_opens(monkeypatch, {store.path: 99})
    labels, diagnostics = store.snapshot()
    assert labels == {}
    assert "handles unavailable" in diagnostics[0]
    assert attempts[store.path] > 1
