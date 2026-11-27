"""Persistent Job handle contracts across processes, failures and legacy state."""

import json
import multiprocessing
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip
from tests.time_budget import hang_guard_s

from booley.harness.dashboard.jobs import JobReader
from booley.runtime import handles, job_records

STAMP = "2026-10-14T10:00:00Z"


def test_job_kinds_and_project_wide_counter(tmp_path_factory):
    for prefix, endpoints in {
        "SIM": ("sim", "simulate", "sim_baseline"),
        "SYN": ("asic_synthesize", "synth"),
        "LINT": ("lint", "lint_rtl"),
        "FPGA": ("fpga_impl", "fpga"),
        "MEAS": ("measure",),
        "MUT": ("mutation",),
        "JOB": ("custom", "reviewer"),
    }.items():
        for endpoint in endpoints:
            tmp_path = tmp_path_factory.mktemp(str(endpoint))
            handles.register_job(tmp_path, "first", endpoint)
            store = handles.HandleStore(tmp_path)
            state = store.load()
            state.next_number = 1
            store.save(state)
            handles.register_job(tmp_path, "second", "lint")
            handles.register_job(tmp_path, "first", "measure")  # stored kind wins
            state = handles.HandleStore(tmp_path).load()
            assert state.entries == {"first": f"J-{prefix}1", "second": "J-LINT2"}, endpoint
            assert state.next_number == 3, endpoint
            handles.register_job(tmp_path / "another-project", "second", "lint")
            other = handles.HandleStore(tmp_path / "another-project").load()
            assert other.entries["second"] == "J-LINT1", endpoint


def _allocate_worker(root, number, start):
    start.wait(timeout=hang_guard_s())
    handles.register_job(Path(root), f"job-{number}", "sim")
    handles.register_job(Path(root), "shared", "lint")


def test_concurrent_processes_allocate_unique_stable_jobs(tmp_path):
    run_workers(tmp_path, _allocate_worker)
    state = handles.HandleStore(tmp_path).load()
    assert len(state.entries) == 5
    assert len(set(state.entries.values())) == 5
    assert state.next_number == 6


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
    store.save(handles.HandleMap({"one": "J-SIM1"}, next_number=2))
    data = json.loads(store.path.read_bytes())
    data.update(mutation)
    store.path.write_text(json.dumps(data))
    before = store.path.read_bytes()
    assert store.snapshot()[1]
    handles.register_job(tmp_path, "job", "lint")
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

    handles.register_job(booley_dir, "run-1", "sim")

    assert handles.HandleStore(booley_dir).snapshot() == ({"run-1": "J-SIM1"}, ())
    assert (store_dir / "runtime/job-handles/map.json").is_file()

    # A link below the Booley Directory is still refused.
    moved = store_dir / "moved"
    (store_dir / "runtime/job-handles").rename(moved)
    symlink_or_skip(store_dir / "runtime/job-handles", moved, target_is_directory=True)
    labels, diagnostics = handles.HandleStore(booley_dir).snapshot()
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


def test_job_allocation_failure_is_advisory(tmp_path, monkeypatch, caplog):
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text("broken")
    handles.register_job(tmp_path, "job", "sim")
    assert store.path.read_text() == "broken"
    assert "Job handle unavailable" in caplog.text
    monkeypatch.setattr(
        handles, "atomic_replace_bytes", lambda *_a: (_ for _ in ()).throw(OSError("disk"))
    )
    handles.register_job(tmp_path / "unwritable", "job", "sim")
    assert "disk" in caplog.text


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
            worker.join(timeout=2 * hang_guard_s())
            assert not worker.is_alive()
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=hang_guard_s())


def test_shape_valid_maps_remain_usable(tmp_path_factory):
    for counter in [-1, 1, 99]:
        tmp_path = tmp_path_factory.mktemp(str(counter))
        store = handles.HandleStore(tmp_path)
        entries = {"old": "J-SIM7", "duplicate": "J-SIM7", "unusual": "display"}
        aliases = {"prior": "old", "dangling": "missing"}
        store.save(handles.HandleMap(dict(entries), dict(aliases), counter))
        before = store.path.read_bytes()
        state = store.load()
        assert state.next_number == max(counter, 8), counter
        assert store.snapshot() == (entries, ()), counter
        assert state.reserve("old", "J-SIM") == "J-SIM7", counter
        assert state.reserve("new", "J-SIM") == f"J-SIM{max(counter, 8)}", counter
        assert state.aliases == aliases, counter
        assert store.path.read_bytes() == before, counter


def test_an_emptied_map_restarts_numbering_and_drops_stored_aliases():
    state = handles.HandleMap({"one": "J-SIM1", "two": "J-SIM2"}, {"prior": "one"}, 3)
    state.drop({"one"})
    assert (state.entries, state.next_number) == ({"two": "J-SIM2"}, 3)
    state.drop({"two"})
    assert state == handles.HandleMap()


def test_map_written_with_labels_still_loads(tmp_path):
    # Maps written by an earlier release carry a ``names`` member.
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    legacy = {"schema": 1, "entries": {"k": "J-SIM4"}, "aliases": {}, "next_number": 5}
    store.path.write_bytes(json.dumps({**legacy, "names": {}}).encode())
    assert store.snapshot() == ({"k": "J-SIM4"}, ())
    handles.register_job(tmp_path, "new", "sim")
    assert store.load().entries == {"k": "J-SIM4", "new": "J-SIM5"}


def _saved_handle_store(tmp_path):
    handles.register_job(tmp_path, "run-1", "sim")
    return handles.HandleStore(tmp_path)


def test_handle_map_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = _saved_handle_store(tmp_path)
    attempts = hold_opens(monkeypatch, {store.path: 1})
    assert store.load().entries == {"run-1": "J-SIM1"}
    assert attempts[store.path] == 2


def test_lasting_handle_map_sharing_error_is_a_snapshot_diagnostic(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = _saved_handle_store(tmp_path)
    attempts = hold_opens(monkeypatch, {store.path: 99})
    labels, diagnostics = store.snapshot()
    assert labels == {}
    assert "handles unavailable" in diagnostics[0]
    assert attempts[store.path] > 1


def test_dashboard_names_expired_unallocated_job_by_run_id(tmp_path):
    from booley.runtime.timefmt import rfc3339_from_epoch

    now = 1_800_000_000.0
    root = tmp_path / "job_logs/old/.runtime/jobs"
    for run_id, ended_ago in [
        ("sim-expired", 2 * 86400),
        ("sim-recent", 3600),
        ("sim-running", None),
    ]:
        running = ended_ago is None
        age = 3 * 86400 if running else ended_ago
        record = job_records.JobRecord(
            run_id,
            "sim",
            rfc3339_from_epoch(now - age - 10),
            60,
            status=job_records.STATUS_RUNNING if running else job_records.STATUS_DONE,
            ended_at=None if running else rfc3339_from_epoch(now - age),
        )
        job_records.write_record(record, root)
    views = {j.record.run_id: j for j in JobReader(tmp_path).read(now=now).jobs}
    assert {run_id: view.handle for run_id, view in views.items()} == {
        "sim-expired": "sim-expired",
        "sim-recent": "",
        "sim-running": "",
    }
