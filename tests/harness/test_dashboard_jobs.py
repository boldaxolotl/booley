"""Read-only retained Jobs, exact reports and resource-unavailable contracts."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from booley.core.booley_dir import reset_cache
from booley.harness.dashboard.jobs import JobReader
from booley.harness.dashboard.resources import Resources, ResourceSampler
from booley.harness.standalone_job import standalone_jobs_dir
from booley.runtime import job_records, job_slots
from booley.runtime.pid import ProcessIdentity
from booley.runtime.runtime_context import RuntimeLocation
from tests.git_support import git
from tests.sandbox_venue_fake import place


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A Project with a Main Booley Directory and one linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "config", "core.autocrlf", "false")
    (main / "booley.toml").write_text("# root config\n", encoding="utf-8")
    (main / "rtl.v").write_text("module top; endmodule\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "base")
    (main / ".git" / "info" / "exclude").write_text("/.booley\n", encoding="utf-8")
    control = main / ".booley"
    worktree = control / "worktrees" / "wt"
    control.mkdir()
    git(main, "worktree", "add", "-q", "-b", "work", str(worktree))
    for booley_dir in (control, worktree / ".booley"):
        (booley_dir / "mcp_tools").mkdir(parents=True, exist_ok=True)
        (booley_dir / "booley.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(control))
    reset_cache()
    yield SimpleNamespace(main=main, control=control, worktree=worktree)
    reset_cache()


def snapshot_jobs(
    booley_dir, *, interactive_root=None, slots_root=None, proc_root=None, reader=None, now=None
):
    options = {} if proc_root is None else {"proc_root": proc_root}
    reader = reader or JobReader(booley_dir, slots_root=slots_root, **options)
    return reader.read(interactive_root=interactive_root, now=now)


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def rec(run="one", **kwargs):
    return job_records.JobRecord(run, "sim", "2026-10-08T10:00:00Z", 60, **kwargs)


def test_exact_run_report_never_latest_foreign_and_missing_artifacts(tmp_path):
    root = tmp_path / "logs/.runtime/jobs"
    job_records.write_record(rec(status="done", exit_code=0), root)
    reports = root.parent / "flow-reports"
    reports.mkdir()
    (reports / "sim.json").write_text(json.dumps({"run_id": "foreign", "passed": True}))
    view = snapshot_jobs(tmp_path, interactive_root=root).jobs[0]
    assert view.report is None
    (reports / "sim.json").write_text(json.dumps({"run_id": "one", "passed": True}))
    assert (
        snapshot_jobs(tmp_path, interactive_root=root).jobs[0].report_path == reports / "sim.json"
    )
    (reports / "sim/1").mkdir(parents=True)
    exact = reports / "sim/1/report.json"
    exact.write_text(json.dumps({"run_id": "one", "passed": False, "summary": "design failed"}))
    view = snapshot_jobs(tmp_path, interactive_root=root).jobs[0]
    assert view.state == "completed"
    assert view.report["passed"] is False
    assert view.report_path == exact


def test_artifact_bound_keeps_newest_report_and_complete_exact_progress(tmp_path, monkeypatch):
    from booley.flows.progress_lifecycle import progress_document

    # Two artifacts stand in for the scan bound of 4,096, a private constant: reaching it
    # through the reader would write 4,097 reports.
    monkeypatch.setattr("booley.runtime.job_observation._artifacts.MAX_ARTIFACTS", 2)
    root = tmp_path / "jobs"
    for index in range(4):
        run = str(index)
        job_records.write_record(rec(run, status="done"), root)
        directory = tmp_path / "flow-reports/sim" / run
        directory.mkdir(parents=True)
        progress = progress_document(
            flow="sim", run_id=run, phase="complete", targets=[], completed_targets=[], detail={}
        )
        for name, data in (
            ("report.json", {"run_id": run, "passed": True}),
            ("progress.json", progress),
        ):
            (directory / name).write_bytes(json.dumps(data).encode())
            os.utime(directory / name, (100 + index, 100 + index))
    before = files(tmp_path)
    snapshot = snapshot_jobs(tmp_path, interactive_root=root)
    jobs = {job.record.run_id: job for job in snapshot.jobs}
    assert jobs["3"].report == {"run_id": "3", "passed": True}
    assert jobs["0"].report is None
    assert {job.stage for job in jobs.values()} == {"complete"}
    assert any("exceeds scan bound" in line for line in snapshot.diagnostics)
    assert files(tmp_path) == before


def test_missing_cgroup_and_root_leave_every_resource_unavailable(tmp_path):
    missing = tmp_path / "missing"
    assert ResourceSampler(missing).sample(missing, now=10) == Resources()


def test_server_job_lifecycle_does_not_include_other_interactive_roots(tmp_path, monkeypatch):
    from booley.mcp import server

    (tmp_path / "job_logs/historic/.runtime/jobs").mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "job_logs/current"))
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    assert server.job_roots() == (tmp_path / "job_logs/current/.runtime/jobs",)


def test_sandbox_capacity_follows_the_cpuset(tmp_path, monkeypatch):
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


def test_dashboard_artifacts_are_indexed_once_and_reparsed_only_when_changed(
    tmp_path, monkeypatch
):
    from booley.flows.progress_lifecycle import progress_document

    root = tmp_path / "job_logs/current/.runtime/jobs"
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
    reader = JobReader(tmp_path)
    assert len(reader.read().jobs) == 2
    assert len(reads) == 4
    reader.read()
    assert len(reads) == 4
    paths[0].write_text(json.dumps({"run_id": "one", "passed": False, "summary": "changed"}))
    updated = reader.read()
    assert len(reads) == 5
    assert (
        next(job for job in updated.jobs if job.record.run_id == "one").report["passed"] is False
    )


def test_historical_interactive_roots_keep_only_newest_bounded_set(tmp_path):
    from booley.harness.dashboard.jobs import MAX_INTERACTIVE_ROOTS, retained_job_roots

    current = tmp_path / "current/.runtime/jobs"
    sessions = [
        tmp_path / f"job_logs/{index}/.runtime/jobs" for index in range(MAX_INTERACTIVE_ROOTS + 2)
    ]
    for index, root in enumerate(sessions):
        root.mkdir(parents=True)
        os.utime(root, (100 + index, 100 + index))
    retained = retained_job_roots(tmp_path, interactive_root=current)
    assert retained.history == (current, *reversed(sessions[2:]))
    assert retained.older == (sessions[1], sessions[0])


def test_dashboard_job_view_preserves_every_observation_field(tmp_path, monkeypatch):
    from dataclasses import MISSING, fields

    from booley.runtime.job_observation import JobObservation, JobObserver, ObservationSnapshot

    root = tmp_path / "jobs"
    observation = JobObservation(
        root,
        rec(status="done"),
        "completed",
        report={"run_id": "one", "passed": False},
        report_path=tmp_path / "report.json",
        stage="simulation",
        diagnostic="isolated observation",
        artifacts=(str(tmp_path / "trace.vcd"),),
        queue="needs 400 MiB",
        queue_position=3,
    )
    monkeypatch.setattr(
        JobObserver,
        "read",
        lambda _self, _roots, **_kw: ObservationSnapshot((observation,), ("history truncated",)),
    )

    snapshot = JobReader(tmp_path).read(interactive_root=root)

    # New fields must be given explicit non-default fixture values before this test passes.
    assert all(field.default_factory is MISSING for field in fields(JobObservation))
    assert all(
        getattr(observation, field.name) != field.default for field in fields(JobObservation)
    )
    assert all(
        getattr(snapshot.jobs[0], field.name) == getattr(observation, field.name)
        for field in fields(JobObservation)
    )
    assert snapshot.diagnostics == ("history truncated",)


def test_report_under_an_allowed_extra_root_is_found(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    root = project / "logs/.runtime/jobs"
    job_records.write_record(rec(status="done", exit_code=0, report_root=str(outside)), root)
    report = outside / "flow-reports/sim/1/report.json"
    report.parent.mkdir(parents=True)
    report.write_bytes(b'{"run_id": "one", "passed": true}')
    reader = JobReader(project)

    assert reader.read(interactive_root=root).jobs[0].report is None

    found = reader.read(interactive_root=root, artifact_roots=(outside,)).jobs[0]
    assert found.report == {"run_id": "one", "passed": True}
    assert found.report_path == report


@pytest.mark.parametrize("malformed", [{"work_dir": []}, {"thread": ["x"]}, {"client": 5}])
def test_malformed_job_is_isolated_from_other_records(tmp_path, malformed):
    root = tmp_path / "jobs"
    job_records.write_record(rec("good", status="done"), root)
    payload = rec("bad").to_dict()
    payload.update(malformed)
    (root / "bad.json").write_text(json.dumps(payload))
    observed = snapshot_jobs(tmp_path, interactive_root=root)
    assert [job.record.run_id for job in observed.jobs] == ["good"]
    assert "bad.json" in str(observed.diagnostics)


def test_standalone_job_lists_with_log_and_report(layout, monkeypatch, capsys):
    """A shell Flow run shows in the Dashboard with its own report."""
    from booley.flows.lint.flow import LintFlow
    from booley.harness import booley as cli
    from booley.mcp.registry import McpToolInfo
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.invocation_doubles import replace_run

    def make_lint():
        flow = LintFlow()
        replace_run(monkeypatch, flow, EndpointOutcome)
        return flow

    info = McpToolInfo(name="lint", path="lint.py", description="lint", kind="flow")
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
    monkeypatch.setattr(cli, "_discover_project_mcp_tools", lambda _root: [info])
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: make_lint)
    monkeypatch.delenv("BOOLEY_RUN_ID", raising=False)
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
    parser = cli._build_parser()
    args = cli._normalize_args(
        parser,
        parser.parse_args(
            ["flow", "lint", "-C", str(layout.worktree), "--target", "top", "--quiet"]
        ),
    )

    assert cli._cmd_flow(args, layout.worktree) == 0

    capsys.readouterr()
    (view,) = snapshot_jobs(layout.control).jobs
    assert view.record.client is None
    assert view.state == "completed"
    assert view.report is not None  # found under the checkout's reports, by the run's own id
    assert view.report_path.name == "report.json"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--target", "top"], "top"),
        (["--target=top#sim"], "top#sim"),
        (["--target"], None),
        (["lint", "--report-dir", "x"], None),
    ],
)
def test_target_arg_reads_both_spellings(argv, expected):
    assert job_records.target_arg(argv) == expected


def _old_session_jobs_world(tmp_path):
    """65 recent session roots; the oldest holds every kind of record."""

    logs = tmp_path / "job_logs"
    for index in range(65):
        (logs / f"new{index}/.runtime/jobs").mkdir(parents=True)
        os.utime(logs / f"new{index}/.runtime/jobs", (2_000_000_000, 2_000_000_000))
    old = logs / "old/.runtime/jobs"
    standalone = standalone_jobs_dir(tmp_path)
    identities = {pid: ProcessIdentity(pid, "boot", 1) for pid in range(4_194_306, 4_194_312)}
    spawning, queued, running, standalone_run, orphan = (
        rec(name, status="running", **extra)
        for name, extra in (
            ("spawning", {}),
            ("queued", {"pid": 4_194_306, "process_identity": identities[4_194_306].to_payload()}),
            (
                "running",
                {
                    "pid": 4_194_307,
                    "process_identity": identities[4_194_307].to_payload(),
                    "run_started_at": "2026-10-08T10:00:01Z",
                },
            ),
            (
                "standalone",
                {
                    "pid": 4_194_308,
                    "process_identity": identities[4_194_308].to_payload(),
                    "run_started_at": "2026-10-08T10:00:01Z",
                },
            ),
            ("orphan", {"pid": 4_194_309, "process_identity": identities[4_194_309].to_payload()}),
        )
    )
    for record in (spawning, queued, running, orphan):
        job_records.write_record(record, old)
    job_records.write_record(rec("finished", status="done", exit_code=0), old)
    job_records.write_record(standalone_run, standalone)
    directory = job_slots.slots_dir(tmp_path) / "memory"
    directory.mkdir(parents=True)
    claim = job_slots.MemoryClaim(6144.0, reservation_mb=100.0).to_payload()
    for state, seq, pid, owner in (
        ("h", 1, 4_194_310, None),
        ("w", 2, 4_194_306, identities[4_194_306]),
    ):
        name = job_slots._entry_name(state, 1, seq, pid, 0)
        payload = {"created_at": "2026-10-08T10:00:00Z", "memory": claim}
        if owner is not None:
            payload["owner_identity"] = owner.to_payload()
            payload["job"] = job_slots.JobLabel("queued", "sim", "").to_payload()
        (directory / name).write_bytes(json.dumps(payload).encode())
    for path in (old, standalone):
        os.utime(path, (1_000, 1_000))
    return identities


def test_reader_lists_unfinished_jobs_of_old_sessions(tmp_path, monkeypatch):
    from booley.runtime.job_observation import _observer
    from booley.runtime.pid import ProcessObservation, ProcessState

    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    identities = _old_session_jobs_world(tmp_path)
    dead = identities[4_194_309]

    def observe(identity, *, proc_root):
        state = ProcessState.DEAD if identity == dead else ProcessState.RUNNING
        return ProcessObservation(state)

    monkeypatch.setattr(_observer, "observe_process", observe)

    jobs = JobReader(tmp_path, slots_root=job_slots.slots_dir(tmp_path)).read().jobs

    assert {job.record.run_id: job.state for job in jobs} == {
        "spawning": "spawning",
        "queued": "queued",
        "running": "running",
        "standalone": "running",
    }


def test_refresh_skips_records_of_unchanged_old_sessions(tmp_path):
    from booley.harness.dashboard.jobs import MAX_INTERACTIVE_ROOTS

    roots = [
        tmp_path / f"job_logs/{index}/.runtime/jobs" for index in range(MAX_INTERACTIVE_ROOTS + 1)
    ]
    for index, root in enumerate(roots):
        job_records.write_record(rec(f"r{index}", status="done"), root)
        os.utime(root, (1_000 + index, 1_000 + index))
    reader = JobReader(tmp_path)
    assert len(reader.read().jobs) == MAX_INTERACTIVE_ROOTS
    # Rewritten in place, which no writer does, the oldest session's record leaves its
    # directory unchanged: a refresh that parsed it again would list it as unfinished.
    old = roots[0] / "r0.json"
    old.write_text(old.read_text().replace('"done"', '"running"'))
    os.utime(old, (2_000, 2_000))
    assert len(reader.read().jobs) == MAX_INTERACTIVE_ROOTS
