"""Job resource history: every Flow command is measured and recorded (#2470)."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.live_progress import observe_stage
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import resource_history
from booley.runtime.project_gitignore import PROJECT_GITIGNORE
from booley.runtime.timefmt import rfc3339_from_datetime, utc_now_rfc3339
from tests.flows.test_flow_base import EchoFlow, _env_with_state
from tests.subprocess_budget import run_bounded_child

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

RECORD_FIELDS = {
    "key",
    "flow",
    "finished_at",
    "outcome",
    "returncode",
    "oom_kill_delta",
    "solo",
    "sample_interval_s",
    "peak_memory_mb",
    "peak_rss_mb",
    "peak_tmp_mb",
    "cpu_user_s",
    "cpu_sys_s",
    "wall_s",
    "container_peak_delta_mb",
}


@pytest.fixture(autouse=True)
def _fresh_label():
    """Stage labels are per invocation; sim tests in this worker must not leak theirs."""
    job_resources.clear_label()


@pytest.fixture
def flow(tmp_path: Path) -> EchoFlow:
    from booley.criteria.state import DevelopmentState

    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    echo = EchoFlow()
    with patch.dict(os.environ, _env_with_state(state_file)):
        echo.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
    return echo


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _history(tmp_path: Path) -> list[dict[str, Any]]:
    return resource_history.load_records(tmp_path)


def _record(key: str, **fields: Any) -> dict[str, Any]:
    measured = {"peak_memory_mb": None, "wall_s": None, "solo": None}
    return {
        "key": {"target": key, "phase": "p", "flags": [], "test_run": ""},
        "container_peak_delta_mb": None,
        "finished_at": utc_now_rfc3339(),
        **measured,
        **fields,
    }


@linux_only
def test_command_records_every_field_and_keeps_its_exit_code(flow, tmp_path):
    result = flow._execute_local(
        _python("import time; b = bytearray(16 << 20); time.sleep(0.6); raise SystemExit(3)")
    )

    assert result.returncode == 3
    (record,) = _history(tmp_path)
    assert set(record) == RECORD_FIELDS
    assert record["key"] == {"target": "test", "phase": "echo_tool", "flags": [], "test_run": ""}
    assert record["outcome"] == "exit_nonzero" and record["returncode"] == 3
    assert record["peak_rss_mb"] >= 16
    assert record["peak_memory_mb"] >= record["peak_rss_mb"]
    assert record["cpu_user_s"] is not None and record["wall_s"] >= 0.6
    assert record["sample_interval_s"] == 0.25


# A child forked by a non-main thread. The thread stays alive in ``wait()`` for
# the whole sampling window: Linux reparents a dead thread's children to a live
# one, which the main thread's ``children`` file would already list.
_THREAD_CHILD = """
import subprocess, sys, threading, time

CHILD = (
    "import sys; b = bytearray(64 << 20); b[::4096] = bytes(len(b[::4096])); "
    "print('ready', flush=True); sys.stdin.read()"
)
spawned = []
ready = threading.Event()


def spawn():
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    spawned.append(child)
    child.stdout.readline()
    ready.set()
    child.wait()


worker = threading.Thread(target=spawn)
worker.start()
ready.wait()
time.sleep(1.0)
spawned[0].stdin.close()
worker.join()
"""


@linux_only
def test_child_of_a_non_main_thread_counts_toward_peak(flow, tmp_path):
    flow._execute_local(_python(_THREAD_CHILD))

    (record,) = _history(tmp_path)
    assert record["peak_rss_mb"] >= 64


@linux_only
@pytest.mark.parametrize("on_tmpfs", [True, False], ids=["tmpfs", "disk"])
def test_tmpdir_counts_as_memory_only_on_tmpfs(flow, tmp_path, monkeypatch, on_tmpfs):
    monkeypatch.setattr(job_resources, "tmpdir_in_memory", lambda _path: on_tmpfs)
    code = (
        "import os, time\n"
        "with open(os.path.join(os.environ['TMPDIR'], 'big'), 'wb') as f: f.write(b'x' * (32 << 20))\n"
        "print(os.environ['TMPDIR']); time.sleep(0.6)"
    )
    result = flow._execute_local(_python(code))

    (record,) = _history(tmp_path)
    assert record["peak_tmp_mb"] >= 32
    if on_tmpfs:
        assert record["peak_memory_mb"] >= record["peak_tmp_mb"] + 5
    else:
        assert record["peak_memory_mb"] == record["peak_rss_mb"]
    assert not Path(result.stdout.strip()).exists(), "the Job's TMPDIR is removed afterwards"


@linux_only
def test_tmpfs_detection_uses_the_longest_mount_point(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(
        b"1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        b"2 1 0:2 / /tmp rw - tmpfs tmpfs rw\n"
        b"3 2 8:2 / /tmp/disk rw - ext4 /dev/sda2 rw\n"
        b"4 1 0:3 / /my\\040tmp rw - tmpfs tmpfs rw\n"
    )
    monkeypatch.setattr(job_resources, "_MOUNTINFO", mountinfo)

    assert job_resources.tmpdir_in_memory(Path("/tmp/a")) is True
    assert job_resources.tmpdir_in_memory(Path("/tmp/disk/a")) is False
    assert job_resources.tmpdir_in_memory(Path("/tmpx/a")) is False
    assert job_resources.tmpdir_in_memory(Path("/my tmp/a")) is True
    monkeypatch.setattr(job_resources, "_MOUNTINFO", tmp_path / "missing")
    assert job_resources.tmpdir_in_memory(Path("/tmp/a")) is False


@linux_only
def test_synth_make_records_yosys_and_openroad_phases(flow, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("yosys", "openroad"):
        (bin_dir / tool).symlink_to(os.path.realpath(sys.executable))
    stage = "import time; b = bytearray({mb} << 20); time.sleep(0.6)"
    script = f"yosys -c '{stage.format(mb=8)}' && openroad -c '{stage.format(mb=24)}'"
    with (
        patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}),
        job_resources.resource_phase("core", "synth", split=job_resources.SYNTH_PHASES),
    ):
        assert flow._execute_local(["sh", "-c", script]).returncode == 0

    records = {record["key"]["phase"]: record for record in _history(tmp_path)}
    assert set(records) == {"synth", "yosys", "openroad"}
    assert 24 <= records["openroad"]["peak_rss_mb"] < records["synth"]["peak_rss_mb"] + 1
    assert 8 <= records["yosys"]["peak_rss_mb"] < records["openroad"]["peak_rss_mb"]
    for phase in ("yosys", "openroad"):
        assert records[phase]["wall_s"] >= 0.25 and records[phase]["cpu_user_s"] is not None


def test_unmeasurable_values_are_absent_not_zero(flow, tmp_path, monkeypatch):
    monkeypatch.setattr(job_resources, "process_tree", lambda pid: None)
    monkeypatch.setattr(job_resources, "_children_cpu_s", lambda: None)
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: None)
    monkeypatch.setattr(job_resources, "_container_memory_bytes", lambda: None)

    assert flow._execute_local(_python("pass")).returncode == 0

    (record,) = _history(tmp_path)
    for field in ("peak_rss_mb", "peak_memory_mb", "cpu_user_s", "oom_kill_delta"):
        assert record[field] is None, field
    assert record["container_peak_delta_mb"] is None
    assert record["peak_tmp_mb"] == 0


@pytest.mark.parametrize(("code", "outcome"), [(0, "exit_zero"), (1, "oom_kill")])
def test_container_oom_kill_is_blamed_only_on_a_failed_command(
    flow, tmp_path, monkeypatch, code, outcome
):
    counts = iter([0, 1])
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: next(counts))

    flow._execute_local(_python(f"raise SystemExit({code})"))

    (record,) = _history(tmp_path)
    assert (record["outcome"], record["oom_kill_delta"]) == (outcome, 1)


@linux_only
def test_concurrent_commands_fall_back_to_sampled_cpu(flow, tmp_path):
    ticket = job_resources._IN_PROCESS.enter()  # another command of this Job is running
    try:
        flow._execute_local(
            # Burn CPU time, not wall time, so a loaded host cannot starve the check.
            _python("import time\nwhile time.process_time() < 0.8: pass")
        )
    finally:
        job_resources._IN_PROCESS.leave(ticket)

    (record,) = _history(tmp_path)
    assert record["solo"] is not True
    # Sampling misses the last interval and a loaded host stretches it: only
    # the fallback itself (a measured, nonzero value) is deterministic.
    assert record["cpu_user_s"] > 0


def test_history_failure_never_changes_the_result(flow, monkeypatch):
    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(job_resources, "append_records", broken)
    assert flow._execute_local(_python("raise SystemExit(4)")).returncode == 4


@pytest.mark.parametrize("others", [False, True, None])
def test_solo_runs_record_container_delta(flow, tmp_path, monkeypatch, others):
    monkeypatch.setattr(job_resources, "_other_jobs_hold_slots", lambda: others)
    readings = iter([100 << 20, *([140 << 20] * 50)])
    monkeypatch.setattr(job_resources, "_container_memory_bytes", lambda: next(readings))

    flow._execute_local(_python("pass"))

    (record,) = _history(tmp_path)
    assert record["solo"] is (None if others is None else not others)
    assert record["container_peak_delta_mb"] == (40.0 if others is False else None)


def _proc_argv() -> list[str]:
    return Path("/proc/self/cmdline").read_text().split("\0")[:-1]


@linux_only
def test_other_jobs_are_holders_from_another_process(tmp_path, monkeypatch):
    from booley.runtime import job_slots

    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
    store = job_slots.SlotStore(tmp_path / "slots")
    assert job_resources._other_jobs_hold_slots() is False
    token = store.acquire(job_slots.CLASS_HEAVY, pid=os.getpid(), argv=_proc_argv())
    assert token.is_holder
    assert job_resources._other_jobs_hold_slots() is False, "this process's own slot"
    monkeypatch.setattr(job_resources.os, "getpid", lambda: token.pid + 1)
    assert job_resources._other_jobs_hold_slots() is True
    monkeypatch.setattr(job_resources.os, "getppid", lambda: token.pid)
    assert job_resources._other_jobs_hold_slots() is False, "a baseline child's inherited slot"
    monkeypatch.undo()
    store.release(token)


def _label_seen(flow_object, method: str, *args: Any) -> job_resources.ResourceLabel | None:
    seen: list[job_resources.ResourceLabel | None] = []

    def execute(*_args, **_kwargs):
        seen.append(job_resources.current_label())
        return SubprocessResult(returncode=0)

    with (
        patch.object(flow_object, "_execute_boundary", side_effect=execute),
        patch.object(flow_object, "_get_timeout", return_value=60),
        # Only synth builds its boundary command from a configured plan.
        patch.object(flow_object, "_synth_boundary_cmd", return_value=["make"], create=True),
    ):
        getattr(flow_object, method)(*args)
    return seen[0]


@pytest.mark.parametrize(
    ("flow_class", "method", "phase", "split"),
    [
        (LintFlow, "_run_linter", "lint", None),
        (FpgaImplFlow, "_run_vivado", "fpga_impl", None),
        (AsicSynthesizeFlow, "_run_synth_make", "synth", job_resources.SYNTH_PHASES),
    ],
)
def test_flows_label_their_commands(flow_class, method, phase, split):
    # A logical synth plan; the other Flows take the command itself.
    plan = SimpleNamespace(spec=SimpleNamespace(timing=SimpleNamespace(mode="logical")))
    command = plan if flow_class is AsicSynthesizeFlow else ["make"]
    label = _label_seen(flow_class(), method, "core", command, 4096)
    assert label == job_resources.ResourceLabel("core", phase, split=split, source_bytes=4096)
    assert job_resources.current_label() is None


def _recorded_keys(
    label: job_resources.ResourceLabel | None, phases: dict[str, job_resources.Usage] | None = None
) -> list[Any]:
    """History keys of the records one successful command with *label* writes."""
    from booley.runtime.resource_history import history_key

    assert label is not None
    result = SimpleNamespace(returncode=0, timed_out=False, duration_s=1.0)
    measurement = job_resources.JobMeasurement(
        0.25, job_resources.Usage(), phases or {}, None, None, None
    )
    return [
        history_key(record)
        for record in job_resources.history_records("flow", label, (), measurement, result)
    ]


def test_flow_requests_map_to_the_keys_their_recorder_writes():
    from booley.flows.job_estimates import JobRequest, phase_keys

    logical_plan = SimpleNamespace(spec=SimpleNamespace(timing=SimpleNamespace(mode="logical")))
    for flow_class, method, flow_name, command in (
        (LintFlow, "_run_linter", "lint", ["make"]),
        (FpgaImplFlow, "_run_vivado", "fpga", ["make"]),
        (AsicSynthesizeFlow, "_run_synth_make", "synth", logical_plan),
    ):
        label = _label_seen(flow_class(), method, "core", command, 4096)
        # Synthesis also records a split tool row; the request lists only the whole command.
        split = {"yosys": job_resources.Usage()} if flow_name == "synth" else None
        recorded = _recorded_keys(label, split)

        assert list(phase_keys(JobRequest(flow_name, "core"))) == recorded[:1]
        assert [key.phase for key in recorded[1:]] == (["yosys"] if flow_name == "synth" else [])


@linux_only
def test_custom_flow_request_maps_to_its_flow_name_key(flow, tmp_path):
    from booley.flows.job_estimates import JobRequest, phase_keys

    assert flow._execute_local(_python("pass")).returncode == 0

    (record,) = _history(tmp_path)
    assert list(phase_keys(JobRequest("echo_tool", "test"))) == [
        resource_history.history_key(record)
    ]


def test_simulation_stages_label_build_and_run_commands():
    observe_stage("tb", "building")
    assert job_resources.current_label() == job_resources.ResourceLabel("tb", "sim_build")
    observe_stage("tb", "executing", tests=("smoke", "irq"))
    assert job_resources.current_label() == job_resources.ResourceLabel(
        "tb", "sim_run", ("smoke", "irq")
    )
    job_resources.clear_label()


@pytest.mark.parametrize(
    ("coverage", "trace", "flags"),
    [(False, False, ()), (True, True, ("coverage", "trace"))],
)
def test_simulation_flags_key_the_history(coverage, trace, flags):
    sim = SimulateFlow()
    sim._args = type("Request", (), {"coverage": coverage, "trace": trace})()
    assert sim._resource_flags() == flags


def _append_many(booley_dir: str, writer: int) -> None:
    for index in range(10):
        resource_history.append_records(Path(booley_dir), [_record(f"w{writer}-{index}")])


def test_parallel_writers_never_lose_or_corrupt_records(tmp_path):
    context = multiprocessing.get_context("spawn")
    writers = [context.Process(target=_append_many, args=(str(tmp_path), n)) for n in range(6)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=60)
        assert writer.exitcode == 0

    keys = {record["key"]["target"] for record in _history(tmp_path)}
    assert keys == {f"w{n}-{index}" for n in range(6) for index in range(10)}


def test_history_keeps_twenty_per_key_and_every_key(tmp_path):
    keys, per_key = 120, 21
    resource_history.append_records(
        tmp_path, [_record(f"k{k}", seq=n) for n in range(per_key) for k in range(keys)]
    )
    resource_history.append_records(tmp_path, [_record("k0", seq=per_key)])

    records = _history(tmp_path)
    assert len(records) > 2000
    for k in range(keys):
        seqs = [r["seq"] for r in records if r["key"]["target"] == f"k{k}"]
        newest = per_key if k == 0 else per_key - 1
        assert seqs == list(range(newest - 19, newest + 1))


def test_key_expires_ninety_days_after_its_newest_record(tmp_path):
    def aged(key: str, days: int) -> dict[str, Any]:
        then = datetime.now(UTC) - timedelta(days=days)
        return _record(key, finished_at=rfc3339_from_datetime(then))

    resource_history.append_records(
        tmp_path,
        [
            aged("old", 91),
            aged("recent", 89),
            aged("stale-but-touched", 91),
            aged("stale-but-touched", 0),
        ],
    )
    resource_history.append_records(tmp_path, [_record("fresh")])

    kept = [r["key"]["target"] for r in _history(tmp_path)]
    assert "old" not in kept
    assert kept.count("recent") == 1
    assert kept.count("stale-but-touched") == 2
    assert "fresh" in kept


def test_history_is_ignored_by_git(tmp_path):
    run_bounded_child(["git", "init", "-q", str(tmp_path)], check=True)
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / ".gitignore").write_text(PROJECT_GITIGNORE)
    resource_history.append_records(booley_dir, [_record("x")])
    path = resource_history.history_path(booley_dir)
    run_bounded_child(["git", "-C", str(tmp_path), "check-ignore", "-q", str(path)], check=True)


def test_report_shows_spread_ratio_and_solo_calibration(tmp_path, capsys):
    from booley.harness import resources_cli

    samples = [(100.0, 10.0, False, None), (200.0, 20.0, True, 250.0), (400.0, 30.0, True, 380.0)]
    resource_history.append_records(
        tmp_path,
        [
            _record("core", peak_memory_mb=m, wall_s=w, solo=s, container_peak_delta_mb=c)
            for m, w, s, c in samples
        ],
    )
    (summary,) = resource_history.summarize(_history(tmp_path))
    assert summary["runs"] == 3 and summary["solo_runs"] == 2
    assert summary["peak_memory_mb"] == {"min": 100.0, "median": 200.0, "max": 400.0, "ratio": 2.0}
    assert summary["wall_s"]["ratio"] == 1.5
    # Paired per run (1.25 and 0.95); a max-vs-max comparison would hide the 25% shortfall.
    assert summary["solo_container_over_rss"]["min"] == pytest.approx(0.95)
    assert summary["solo_container_over_rss"]["max"] == 1.25

    assert resources_cli.run(type("Args", (), {"json": False})(), tmp_path) == 0
    text = capsys.readouterr().out
    assert "core p: 3 runs; peak memory 100 MiB / 200 MiB / 400 MiB (max/median 2.00)" in text
    assert "wall 10s / 20s / 30s (max/median 1.50)" in text
    assert "solo runs 2: container peak / peak memory 0.95 / 1.10 / 1.25" in text
    assert "outside TMPDIR" in text
    assert resources_cli.run(type("Args", (), {"json": True})(), tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == [summary]


def test_cgroup_oom_counter_parser(tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\noom 3\noom_kill 2\n", encoding="utf-8")
    monkeypatch.setattr(job_resources, "_CGROUP_MEMORY_EVENTS", events)
    assert job_resources.cgroup_oom_kill_count() == 2


def test_report_handles_empty_unreadable_and_unmeasured_history(tmp_path, capsys):
    from booley.harness import resources_cli

    args = type("Args", (), {"json": False})()
    assert resources_cli.run(args, tmp_path) == 0
    assert "No Job resource history yet" in capsys.readouterr().out

    resource_history.append_records(tmp_path, [_record("core")])
    assert resources_cli.run(args, tmp_path) == 0
    assert "peak memory unmeasured; wall unmeasured" in capsys.readouterr().out

    resource_history.history_path(tmp_path).write_text("not json", encoding="utf-8")
    assert resources_cli.run(args, tmp_path) == 2
    assert "cannot read the Job resource history" in capsys.readouterr().err


def test_records_carry_source_bytes_only_when_known(flow, tmp_path):
    job_resources.label_commands("core", "sim_build", source_bytes=1234)
    assert flow._execute_local(_python("pass")).returncode == 0
    job_resources.label_commands("core", "sim_build")
    assert flow._execute_local(_python("pass")).returncode == 0

    known, unknown = _history(tmp_path)
    assert known["source_bytes"] == 1234
    # Unknown is absent, never zero.
    assert "source_bytes" not in unknown
    assert set(unknown) == RECORD_FIELDS


def test_resolved_source_bytes_sums_distinct_files_or_is_unknown(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"abc")
    (tmp_path / "b.sv").write_bytes(b"12345")

    def target(*names: str) -> SimpleNamespace:
        files = tuple(SimpleNamespace(absolute=lambda root, n=n: Path(root) / n) for n in names)
        return SimpleNamespace(files=files, build_root=tmp_path)

    assert job_resources.resolved_source_bytes(target("a.sv", "b.sv", "a.sv")) == 8
    assert job_resources.resolved_source_bytes(target("a.sv", "missing.sv")) is None
    assert job_resources.resolved_source_bytes(target()) is None


def test_doctor_started_runs_write_no_records(flow, tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_INTERNAL_DOCTOR_RUN", "1")
    assert flow._execute_local(_python("pass")).returncode == 0
    assert _history(tmp_path) == []

    monkeypatch.delenv("BOOLEY_INTERNAL_DOCTOR_RUN")
    assert flow._execute_local(_python("pass")).returncode == 0
    assert len(_history(tmp_path)) == 1


def test_history_written_before_source_bytes_still_loads(tmp_path):
    old_key = {"target": "core", "phase": "sim_run", "flags": [], "test_run": "smoke"}
    old_record = {
        "key": old_key,
        "flow": "simulate",
        "finished_at": "2026-01-01T00:00:00Z",
        "outcome": "exit_zero",
        "returncode": 0,
        "oom_kill_delta": 0,
        "solo": True,
        "sample_interval_s": 0.25,
        "peak_memory_mb": 100.0,
        "peak_rss_mb": 90.0,
        "peak_tmp_mb": 10.0,
        "cpu_user_s": 1.0,
        "cpu_sys_s": 0.1,
        "wall_s": 2.0,
        "container_peak_delta_mb": 105.0,
    }
    path = resource_history.history_path(tmp_path)
    path.parent.mkdir(parents=True)
    document = {"$schema": resource_history.SCHEMA, "records": [old_record]}
    path.write_bytes(json.dumps(document).encode("utf-8"))

    label = job_resources.ResourceLabel("core", "sim_run", ("smoke",), source_bytes=77)
    result = SimpleNamespace(returncode=0, timed_out=False, duration_s=1.0)
    measurement = job_resources.JobMeasurement(0.25, job_resources.Usage(), {}, None, None, None)
    (new_record,) = job_resources.history_records("simulate", label, (), measurement, result)
    assert new_record["source_bytes"] == 77 and new_record["key"] == old_key
    resource_history.append_records(tmp_path, [new_record])

    records = _history(tmp_path)
    assert "source_bytes" not in records[0] and records[1]["source_bytes"] == 77
    (summary,) = resource_history.summarize(records)
    assert summary["key"] == old_key and summary["runs"] == 2
    assert "core sim_run tests=smoke: 2 runs" in resource_history.render_report([summary])


def test_records_carry_the_job_thread_count_only_when_set(flow, tmp_path):
    from booley.flows.memory_admission import fixed_job_threads

    job_resources.label_commands("core", "sim_build")
    with fixed_job_threads(4):
        assert flow._execute_local(_python("pass")).returncode == 0
    assert flow._execute_local(_python("pass")).returncode == 0

    counted, uncounted = _history(tmp_path)
    assert counted["threads"] == 4
    # No count is absent, never zero.
    assert set(uncounted) == RECORD_FIELDS
