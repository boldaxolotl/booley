"""Read-only retained Jobs, exact reports, uncertainty and resource-unavailable contracts."""

from __future__ import annotations

import json

import pytest

from booley.harness.dashboard.app import job_detail
from booley.harness.dashboard.resources import ResourceSampler
from booley.runtime import job_records, job_slots
from booley.runtime.job_snapshot import snapshot_jobs
from booley.runtime.pid import ProcessIdentity


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def rec(run="one", **kwargs):
    return job_records.JobRecord(run, "sim", "2026-10-08T10:00:00Z", 60, **kwargs)


def test_multi_roots_duplicate_ids_disconnected_history_and_byte_identity(tmp_path, monkeypatch):
    from booley.runtime import job_snapshot

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
    monkeypatch.setattr(job_snapshot, "retained_job_roots", lambda *_a, **_k: tuple(roots))
    monkeypatch.setattr(
        job_slots.SlotStore, "reap", lambda *_a: pytest.fail("observational read reaped work")
    )
    monkeypatch.setattr(
        job_slots.SlotStore, "snapshot", lambda *_a: pytest.fail("used mutating snapshot")
    )
    before = files(tmp_path)
    snapshot = snapshot_jobs(tmp_path, slots_root=tmp_path / "slots")
    assert len(snapshot.jobs) == 5
    assert len({job.key for job in snapshot.jobs}) == 5
    assert all(job.record.session_key == "codex:expired" for job in snapshot.jobs)
    assert snapshot.diagnostics == ("ambiguous legacy run ID: duplicate",)
    assert files(tmp_path) == before


def test_exact_run_report_never_latest_foreign_and_missing_artifacts(tmp_path):
    root = tmp_path / "logs/.runtime/jobs"
    job_records.write_record(rec(status="done", exit_code=0), root)
    reports = root.parent / "flow-reports"
    reports.mkdir()
    (reports / "sim.json").write_text(json.dumps({"run_id": "foreign", "passed": True}))
    view = snapshot_jobs(tmp_path, interactive_root=root).jobs[0]
    assert view.report is None
    (reports / "sim/1").mkdir(parents=True)
    exact = reports / "sim/1/report.json"
    exact.write_text(json.dumps({"run_id": "one", "passed": False, "summary": "design failed"}))
    view = snapshot_jobs(tmp_path, interactive_root=root).jobs[0]
    assert view.state == "completed"
    assert view.report["passed"] is False
    assert "Design verdict: False" in job_detail(view)
    assert str(exact) in job_detail(view)
    assert "Peak memory: —" in job_detail(view)


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
    assert (
        snapshot_jobs(tmp_path, interactive_root=root, proc_root=tmp_path / "proc").jobs[0].state
        == state
    )
    assert files(tmp_path) == before


def test_resources_use_sandbox_capacity_and_limits_no_sleep(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for name, value in {
        "cpu.max": "200000 100000",
        "cpu.stat": "usage_usec 1000000\n",
        "memory.current": "1024",
        "memory.max": "2048",
    }.items():
        (cgroup / name).write_text(value)
    sampler = ResourceSampler(cgroup)
    assert sampler.sample(tmp_path, now=10).cpu_percent is None
    (cgroup / "cpu.stat").write_text("usage_usec 2000000\n")
    sample = sampler.sample(tmp_path, now=11)
    assert sample.cpu_percent == 50
    assert (sample.memory, sample.memory_limit) == (1024, 2048)
    assert sample.disk_free > 0
    missing = ResourceSampler(tmp_path / "missing").sample(tmp_path / "missing", now=10)
    assert (
        missing.cpu_percent is missing.memory is missing.memory_limit is missing.disk_free is None
    )


@pytest.mark.parametrize("slot,state", [("w", "queued"), ("h", "running")])
def test_observational_slot_state_and_corrupt_entry_isolation(tmp_path, monkeypatch, slot, state):
    from booley.runtime import job_snapshot
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
        job_snapshot, "observe_process", lambda *_a, **_k: ProcessObservation(ProcessState.RUNNING)
    )
    monkeypatch.setattr(job_slots.SlotStore, "reap", lambda *_: pytest.fail("reaped a stale slot"))
    before = files(tmp_path)
    view = snapshot_jobs(tmp_path, interactive_root=root, slots_root=slots.parent).jobs[0]
    assert view.state == state
    assert files(tmp_path) == before


def test_process_resource_delta_and_reused_identity_are_honest(tmp_path, monkeypatch):
    from booley.harness.dashboard import resources

    identity = ProcessIdentity(99, "fixture", 101)
    directory = tmp_path / "99"
    directory.mkdir()
    fields = ["0"] * 20
    fields[11] = "100"
    fields[12] = "50"
    (directory / "stat").write_text("99 (flow) " + " ".join(fields))
    (directory / "status").write_text("VmRSS: 100 kB\nVmHWM: 200 kB\n")
    monkeypatch.setattr(resources, "capture_process_identity", lambda *_a, **_k: identity)
    monkeypatch.setattr(resources.os, "sysconf", lambda _: 100, raising=False)
    sampler = resources.ProcessSampler(tmp_path)
    (directory / "task/99").mkdir(parents=True)
    (directory / "task/99/children").write_text("")
    first = sampler.sample(identity, now=10, capacity=2)
    assert first.cpu_percent is None
    assert (first.memory, first.peak_memory) == (102400, 102400)
    fields[11] = "200"
    (directory / "stat").write_text("99 (flow) " + " ".join(fields))
    assert sampler.sample(identity, now=11, capacity=2).cpu_percent == 100
    observations = iter([identity, ProcessIdentity(99, "fixture", 102)])
    monkeypatch.setattr(
        resources, "capture_process_identity", lambda *_a, **_k: next(observations)
    )
    assert sampler.sample(identity, now=12, capacity=2) == resources.ProcessResources()


def test_retained_goal_roots_are_discovered_without_current_sessions(tmp_path):
    from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
    from booley.goals.paths import record_paths
    from booley.runtime.job_snapshot import retained_job_roots

    expected = []
    for number, state in enumerate((GoalState.ACTIVE, GoalState.FINISHED, GoalState.ABANDONED)):
        record = GoalRecord(
            f"fixture{number}-20261008T100000Z",
            state,
            WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main"),
            str(tmp_path),
            "goal/fixture",
            "refs/heads/main",
            "a" * 40,
            "2026-10-08T10:00:00Z",
            revision=1,
        )
        paths = record_paths(tmp_path, record.id)
        paths.root.mkdir(parents=True)
        paths.record_file.write_text(json.dumps(record.to_json()))
        expected.append(paths.jobs_dir)
    assert set(retained_job_roots(tmp_path)) == set(expected)


def test_reported_metrics_and_legacy_elapsed_are_not_invented():
    from dataclasses import replace
    from pathlib import Path

    from booley.harness.dashboard.app import _elapsed
    from booley.runtime.job_snapshot import JobView

    view = JobView(
        Path("/jobs"),
        rec(status="done"),
        "completed",
        report={
            "estimated_fmax_mhz": 210.5,
            "cells": 42,
            "elapsed_s": 5,
            "failure_reason": "timing threshold missed",
            "stdout": "raw log must remain hidden",
        },
    )
    rendered = job_detail(view, now=1792000000)
    assert "210.5" in rendered and '"cells": 42' in rendered
    assert "timing threshold missed" in rendered
    assert "raw log must remain hidden" not in rendered
    assert _elapsed(view, now=1792000000) == "5s"
    assert _elapsed(view, now=1892000000) == "5s"
    assert _elapsed(replace(view, report=None), now=1892000000) == "unavailable"


def test_job_resources_include_eda_descendants_and_reject_child_reuse(tmp_path, monkeypatch):
    from booley.harness.dashboard.resources import ProcessResources, ProcessSampler
    from booley.runtime.pid import capture_process_identity
    from tests.mcp_tools.test_session_registry import _fake_process

    for pid in (98765, 98766):
        _fake_process(tmp_path, pid, str(pid))
        directory = tmp_path / str(pid)
        (directory / f"task/{pid}").mkdir(parents=True)
        (directory / f"task/{pid}/children").write_text("98766" if pid == 98765 else "")
        (directory / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t200 kB\n")
    identity = capture_process_identity(98765, proc_root=tmp_path)
    sampler = ProcessSampler(tmp_path)
    first = sampler.sample(identity, now=10, capacity=2)
    assert first.memory == 204800  # wrapper + EDA child
    child = tmp_path / "98766/stat"
    fields = child.read_text().rsplit(")", 1)[1].split()
    fields[11] = "100"
    child.write_text("98766 (eda) " + " ".join(fields))
    monkeypatch.setattr(
        "booley.harness.dashboard.resources.os.sysconf", lambda _: 100, raising=False
    )
    assert sampler.sample(identity, now=11, capacity=2).cpu_percent == 100
    from booley.harness.dashboard import resources

    real_capture = resources.capture_process_identity
    calls = 0

    def capture(pid, **kw):
        nonlocal calls
        if pid == 98766:
            calls += 1
            if calls == 2:
                return None
        return real_capture(pid, **kw)

    monkeypatch.setattr(resources, "capture_process_identity", capture)
    assert sampler.sample(identity, now=12, capacity=2) == ProcessResources()


def test_server_job_lifecycle_does_not_include_other_interactive_roots(tmp_path, monkeypatch):
    from booley.mcp import server
    from booley.runtime.job_snapshot import retained_job_roots

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    current = tmp_path / ".interactive_logs/current/.runtime/jobs"
    historic = tmp_path / ".interactive_logs/historic/.runtime/jobs"
    historic.mkdir(parents=True)
    monkeypatch.setattr(server, "container_jobs_root", lambda: current)
    monkeypatch.setattr(server, "resolve_project_dir", lambda: tmp_path)
    assert set(retained_job_roots(tmp_path, interactive_root=current)) == {current, historic}
    assert server.job_roots() == (current,)


def test_job_cpu_is_percent_of_one_core_independent_of_sandbox_capacity(tmp_path, monkeypatch):
    from booley.harness.dashboard import resources
    from tests.mcp_tools.test_session_registry import _fake_process

    _fake_process(tmp_path, 98765, "101")
    directory = tmp_path / "98765"
    (directory / "task/98765").mkdir(parents=True)
    (directory / "task/98765/children").write_text("")
    (directory / "status").write_text("VmRSS: 100 kB\n")
    identity = resources.capture_process_identity(98765, proc_root=tmp_path)
    sampler = resources.ProcessSampler(tmp_path)
    assert sampler.sample(identity, now=10, capacity=24).cpu_percent is None
    fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
    fields[11] = "100"
    (directory / "stat").write_text("98765 (flow) " + " ".join(fields))
    monkeypatch.setattr(resources.os, "sysconf", lambda _: 100, raising=False)
    assert sampler.sample(identity, now=11, capacity=24).cpu_percent == 100


def test_sandbox_capacity_is_explicit_in_resource_projection(tmp_path, monkeypatch):
    from booley.harness.dashboard import app

    for name, value in {
        "cpu.max": "max 100000",
        "cpu.stat": "usage_usec 0",
        "cpuset.cpus.effective": "0-23",
    }.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setattr("booley.harness.dashboard.resources.os.cpu_count", lambda: 24)
    sampler = ResourceSampler(tmp_path)
    sampler.sample(tmp_path, now=10)
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000")
    sample = sampler.sample(tmp_path, now=11)
    assert sample.cpu_percent == pytest.approx(100 / 24)
    assert "Sandbox CPU 4.2% of 24 CPUs" in app.resource_summary(sample)


@pytest.mark.parametrize("stage", [None, "simulation"])
def test_running_job_details_use_live_elapsed_reported_stage_and_declared_tool(
    tmp_path, monkeypatch, stage
):
    import time

    from booley.flows.progress_lifecycle import progress_document
    from booley.runtime.pid import capture_process_identity
    from tests.mcp_tools.test_session_registry import _fake_process

    work = tmp_path / "work"
    work.mkdir()
    (work / "fixture.core").write_text(
        "CAPI=2:\nname: acme:test:fixture:1.0\ntargets:\n  sim_top:\n"
        "    flow: sim\n    toplevel: top\n    flow_options:\n      tool: icarus\n"
    )
    proc = tmp_path / "proc"
    _fake_process(proc, 98765, "101")
    identity = capture_process_identity(98765, proc_root=proc)
    root = tmp_path / "logs/.runtime/jobs"
    job_records.write_record(
        rec(
            pid=98765,
            process_identity=identity.to_payload(),
            work_dir=str(work),
            argv=["--target", "sim_top"],
            run_started_at="2026-10-08T10:00:00Z",
        ),
        root,
    )
    progress = root.parent / "flow-reports/sim/1/progress.json"
    progress.parent.mkdir(parents=True)
    document = progress_document(
        flow="sim",
        run_id="one",
        phase="starting",
        targets=["sim_top"],
        completed_targets=[],
        detail={},
        extra={} if stage is None else {"stage": stage},
    )
    progress.write_text(json.dumps(document))
    before = files(tmp_path)
    monkeypatch.setattr(time, "time", lambda: 1791453605)
    view = snapshot_jobs(tmp_path, interactive_root=root, proc_root=proc).jobs[0]
    text = job_detail(view)
    assert "Elapsed: 5s" in text
    assert f"Stage: {stage or '—'}" in text
    assert "EDA tool: icarus" in text
    assert files(tmp_path) == before
    (work / "fixture.core").unlink()
    missing = snapshot_jobs(tmp_path, interactive_root=root, proc_root=proc).jobs[0]
    assert "EDA tool: —" in job_detail(missing)


def test_dashboard_artifacts_are_indexed_once_and_reparsed_only_when_changed(
    tmp_path, monkeypatch
):
    from booley.flows.progress_lifecycle import progress_document
    from booley.harness.dashboard.model import DashboardReader

    root = tmp_path / ".interactive_logs/current/.runtime/jobs"
    paths = []
    for run in ("one", "two"):
        job_records.write_record(rec(run, status="done"), root)
        directory = root.parent / "flow-reports/sim" / str(len(paths) + 1)
        directory.mkdir(parents=True)
        report = directory / "report.json"
        report.write_text(json.dumps({"run_id": run, "passed": True}))
        progress = directory / "progress.json"
        progress.write_text(
            json.dumps(
                progress_document(
                    flow="sim",
                    run_id=run,
                    phase="complete",
                    targets=[],
                    completed_targets=[],
                    detail={},
                )
            )
        )
        paths.append(report)
    reads = []
    loads = json.loads

    def read(raw, *args, **kwargs):
        data = loads(raw, *args, **kwargs)
        if isinstance(data, dict) and "run_id" in data and ("phase" in data or "passed" in data):
            reads.append(data["run_id"])
        return data

    monkeypatch.setattr(json, "loads", read)
    reader = DashboardReader(tmp_path, tmp_path)
    assert len(reader.read().jobs.jobs) == 2
    assert len(reads) == 4
    reader.read()
    assert len(reads) == 4
    paths[0].write_text(json.dumps({"run_id": "one", "passed": False, "summary": "changed"}))
    updated = reader.read()
    assert len(reads) == 5
    assert (
        next(job for job in updated.jobs.jobs if job.record.run_id == "one").report["passed"]
        is False
    )


def test_historical_interactive_roots_keep_only_newest_bounded_set(tmp_path, monkeypatch):
    import os

    from booley.runtime import job_snapshot

    monkeypatch.setattr(job_snapshot, "MAX_INTERACTIVE_ROOTS", 3, raising=False)
    expected = []
    for index in range(5):
        root = tmp_path / f".interactive_logs/{index}/.runtime/jobs"
        root.mkdir(parents=True)
        os.utime(root, (100 + index, 100 + index))
        if index >= 2:
            expected.append(root)
    assert set(job_snapshot.retained_job_roots(tmp_path)) == set(expected)


@pytest.mark.parametrize(
    "malformed", [{"session_key": 5}, {"binding": []}, {"binding": {"schema": "future"}}]
)
def test_malformed_job_is_isolated_from_other_records(tmp_path, malformed):
    root = tmp_path / "jobs"
    job_records.write_record(rec("good", status="done"), root)
    payload = rec("bad").to_dict()
    payload.update(malformed)
    (root / "bad.json").write_text(json.dumps(payload))
    observed = snapshot_jobs(tmp_path, interactive_root=root)
    assert [job.record.run_id for job in observed.jobs] == ["good"]
    assert "bad.json" in str(observed.diagnostics)
