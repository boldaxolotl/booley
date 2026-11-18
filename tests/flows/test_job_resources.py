"""Job resource history: every Flow command is measured and recorded (#2470)."""

from __future__ import annotations

import contextlib
import json
import math
import multiprocessing
import os
import sys
import threading
import time
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
from booley.flows.memory_admission import fixed_job_threads
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.live_progress import observe_stage
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import resource_history, runtime_context
from booley.runtime.project_gitignore import PROJECT_GITIGNORE
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    supervised_execution_scope,
)
from booley.runtime.timefmt import rfc3339_from_datetime, utc_now_rfc3339
from tests.flows.test_flow_base import EchoFlow, _flow_env
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s, scaled

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")

SELF_SIGKILL = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

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
    "peak_memory_with_swap_mb",
    "peak_swap_mb",
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

    echo = EchoFlow()
    with patch.dict(os.environ, _flow_env()):
        echo.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
    return echo


class CommandFlow(EchoFlow):
    """A Flow that runs the command a test gives it and keeps the measured result."""

    command: list[str]
    result: SubprocessResult | None = None

    def _build_command(self) -> list[str]:
        return self.command

    def _interpret_result(self, result: SubprocessResult):
        self.result = result
        return super()._interpret_result(result)


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``run(command)``: the command through the Flow's public endpoint."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv("BOOLEY_SLUG", "test")

    def run_command(command: list[str]) -> SubprocessResult:
        command_flow = CommandFlow()
        command_flow.command = command
        command_flow.execute_cli(["--target", "test", "--work-dir", str(tmp_path)])
        assert command_flow.result is not None
        return command_flow.result

    return run_command


def _execute(flow_object: EchoFlow, command: list[str], **options: Any) -> SubprocessResult:
    # Private seam: through execute_cli the endpoint's admission sets a Job thread
    # count on every record, reads the container OOM counter for its own requeue
    # decision and clears the stage label when the invocation starts, so one
    # command's OOM blame, label and thread count are observable only below it.
    return flow_object._execute_local(command, **options)


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
def test_command_records_every_field_and_keeps_its_exit_code(run, tmp_path):
    result = run(
        _python("import time; b = bytearray(16 << 20); time.sleep(0.6); raise SystemExit(3)")
    )

    assert result.returncode == 3
    (record,) = _history(tmp_path)
    # Endpoint admission also records the Job thread count.
    assert set(record) == RECORD_FIELDS | {"threads", "job"}
    assert record["key"] == {"target": "test", "phase": "echo_tool", "flags": [], "test_run": ""}
    assert record["outcome"] == "exit_nonzero" and record["returncode"] == 3
    assert record["peak_rss_mb"] >= 16
    assert record["peak_memory_mb"] >= record["peak_rss_mb"]
    assert record["peak_swap_mb"] is not None
    assert record["peak_memory_with_swap_mb"] >= record["peak_memory_mb"]
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
def test_child_of_a_non_main_thread_counts_toward_peak(run, tmp_path):
    run(_python(_THREAD_CHILD))

    (record,) = _history(tmp_path)
    assert record["peak_rss_mb"] >= 64


@linux_only
@pytest.mark.parametrize("on_tmpfs", [True, False], ids=["tmpfs", "disk"])
def test_tmpdir_counts_as_memory_only_on_tmpfs(run, tmp_path, monkeypatch, on_tmpfs):
    monkeypatch.setattr(job_resources, "tmpdir_in_memory", lambda _path: on_tmpfs)
    code = (
        "import os, time\n"
        "with open(os.path.join(os.environ['TMPDIR'], 'big'), 'wb') as f: f.write(b'x' * (32 << 20))\n"
        "print(os.environ['TMPDIR']); time.sleep(0.6)"
    )
    result = run(_python(code))

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
    # Private seam: the kernel's mountinfo path is fixed; the test points it at a fixture.
    monkeypatch.setattr(job_resources, "_MOUNTINFO", mountinfo)

    assert job_resources.tmpdir_in_memory(Path("/tmp/a")) is True
    assert job_resources.tmpdir_in_memory(Path("/tmp/disk/a")) is False
    assert job_resources.tmpdir_in_memory(Path("/tmpx/a")) is False
    assert job_resources.tmpdir_in_memory(Path("/my tmp/a")) is True
    mountinfo.unlink()
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
        assert _execute(flow, ["sh", "-c", script]).returncode == 0

    records = {record["key"]["phase"]: record for record in _history(tmp_path)}
    assert set(records) == {"synth", "yosys", "openroad"}
    assert 24 <= records["openroad"]["peak_rss_mb"] < records["synth"]["peak_rss_mb"] + 1
    assert 8 <= records["yosys"]["peak_rss_mb"] < records["openroad"]["peak_rss_mb"]
    for phase in ("yosys", "openroad"):
        assert records[phase]["wall_s"] >= 0.25 and records[phase]["cpu_user_s"] is not None


@linux_only
def test_synth_history_threads_equal_the_openroad_thread_count(tmp_path, monkeypatch):
    from booley.flows.memory_admission import fixed_job_threads

    synth = AsicSynthesizeFlow()
    with patch.dict(os.environ, _flow_env()):
        synth.parse_args(["--target", "core", "--work-dir", str(tmp_path)])
    synth._execution_role = "baseline"  # no reuse slot
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "make.argv"
    fake_make = bin_dir / "make"
    fake_make.write_bytes(f'#!/bin/sh\necho "$@" >> {argv_log}\n'.encode())
    fake_make.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    plan = SimpleNamespace(
        build_dir=tmp_path / "build",
        spec=SimpleNamespace(timing=SimpleNamespace(mode="physical")),
    )
    job_resources.label_commands("core", "synth")

    with fixed_job_threads(3):
        # Private seam: the synth make step runs only after Yosys configuration.
        synth._run_synth_make("core", plan, None)

    commands = argv_log.read_text(encoding="utf-8").splitlines()
    assert commands[-1].endswith(" openroad BOOLEY_OPENROAD_THREAD_OPTION=-threads 3")
    records = _history(tmp_path)
    assert records
    assert {record["threads"] for record in records} == {3}


def test_unmeasurable_values_are_absent_not_zero(run, tmp_path, monkeypatch):
    monkeypatch.setattr(job_resources, "process_tree", lambda pid, **_: None)
    # Private seam: host readings; the test fixes what the kernel would report.
    monkeypatch.setattr(job_resources, "_children_cpu_s", lambda: None)
    monkeypatch.setattr(
        job_resources, "cgroup_memory_events", lambda: job_resources.MemoryEvents(None, None)
    )
    monkeypatch.setattr(job_resources, "_container_memory_bytes", lambda: None)

    assert run(_python("pass")).returncode == 0

    (record,) = _history(tmp_path)
    for field in ("peak_rss_mb", "peak_memory_mb", "cpu_user_s", "oom_kill_delta"):
        assert record[field] is None, field
    assert record["container_peak_delta_mb"] is None
    assert record["peak_tmp_mb"] == 0


def test_swapped_job_records_peak_swap_and_its_next_estimate_includes_it(
    run, tmp_path, monkeypatch
):
    from booley.flows.job_estimates import ResourceEstimator
    from booley.runtime.resource_history import HistoryKey

    mib = 1 << 20
    samples = iter([(400 * mib, 0)])  # then every sample: 200 resident, 300 swapped out

    def tree(pid, **_):
        resident, swapped = next(samples, (200 * mib, 300 * mib))
        return {pid: job_resources._Proc("echo", resident, 0, 0, swapped)}

    # Private seam: the host /proc sample; the test fixes what the kernel would report.
    monkeypatch.setattr(job_resources, "process_tree", tree)
    monkeypatch.setattr(job_resources, "tmpdir_in_memory", lambda _path: False)
    # Windows has no os.sysconf; the sampled CPU ticks need the clock rate.
    monkeypatch.setattr(os, "sysconf", lambda _name: 100, raising=False)

    assert run(_python("pass")).returncode == 0

    (record,) = _history(tmp_path)
    assert record["peak_rss_mb"] == 400.0
    assert record["peak_swap_mb"] == 300.0
    # Per sample (200 + 300), not the sum of the two peaks (400 + 300).
    assert record["peak_memory_with_swap_mb"] == 500.0
    estimate = ResourceEstimator(_history(tmp_path), memory_margin=1.25).estimate_keys(
        [HistoryKey("test", "echo_tool", (), "")]
    )
    assert estimate.phases[0].memory_mb.value == 625.0


def test_read_swap_bytes_parses_vmswap(tmp_path):
    (tmp_path / "42").mkdir()
    status = tmp_path / "42" / "status"
    status.write_bytes(b"Name:\tx\nVmRSS:\t  10 kB\nVmSwap:\t    2048 kB\n")

    assert job_resources._read_swap_bytes(42, proc_root=tmp_path) == 2 * 1024 * 1024

    status.write_bytes(b"Name:\tx\nVmRSS:\t  10 kB\n")  # a kernel thread has no VmSwap
    assert job_resources._read_swap_bytes(42, proc_root=tmp_path) is None
    assert job_resources._read_swap_bytes(43, proc_root=tmp_path) is None


def test_specialist_record_keeps_the_largest_swap_peaks():
    mib = 1 << 20

    def window(swap: int, with_swap: int) -> job_resources.AgentCallUsage:
        usage = job_resources.Usage(
            peak_rss=300 * mib,
            peak_memory=300 * mib,
            peak_swap=swap * mib,
            peak_with_swap=with_swap * mib,
        )
        measurement = job_resources.JobMeasurement(0.25, usage, {}, None, None, None)
        return job_resources.AgentCallUsage(measurement, 1.0)

    record = job_resources.specialist_record("reviewer", [window(50, 400), window(120, 380)], 0)

    assert record["peak_swap_mb"] == 120.0
    assert record["peak_memory_with_swap_mb"] == 400.0


def _oom_counter(monkeypatch, counts, limit=None) -> None:
    """The container reads *counts* of OOM kills in turn, and *limit* of Sandbox-limit events.

    Without *limit* the Sandbox reached its limit as often as it saw a kill.
    """

    def events() -> job_resources.MemoryEvents:
        kills = next(counts)
        return job_resources.MemoryEvents(kills, kills if limit is None else next(limit))

    monkeypatch.setattr(job_resources, "cgroup_memory_events", events)


_MAKE_KILLED = (
    "import sys; sys.stderr.write('make: *** [Makefile:3: all] Killed\\n'); raise SystemExit(2)"
)
_COMPILER_KILLED = (
    "print('g++: fatal error: Killed signal terminated program cc1plus')\n"
    "print('make: *** [obj.o] Error 1')\n"
    "raise SystemExit(2)"
)


@pytest.mark.parametrize(
    ("code", "returncode", "oom_killed", "oom_blamed", "outcome"),
    [
        ("raise SystemExit(0)", 0, False, None, "exit_zero"),
        # Another Job's OOM kill is not blamed on the command's own failure.
        ("raise SystemExit(1)", 1, False, False, "exit_nonzero"),
        pytest.param(SELF_SIGKILL, None, True, True, "oom_kill", marks=posix_only),
        (_MAKE_KILLED, 2, True, None, "oom_kill"),
        (_COMPILER_KILLED, 2, True, None, "oom_kill"),
    ],
    ids=["exit-zero", "own-failure", "sigkill", "make-killed", "compiler-killed"],
)
def test_oom_blame_follows_the_commands_own_evidence(
    flow, tmp_path, monkeypatch, code, returncode, oom_killed, oom_blamed, outcome
):
    _oom_counter(monkeypatch, iter([0, 1]))

    result = _execute(flow, _python(code))

    if returncode is not None:
        assert result.returncode == returncode
    assert result.oom_killed is oom_killed
    if oom_blamed is not None:
        assert result.oom_blamed is oom_blamed
    (record,) = _history(tmp_path)
    assert (record["outcome"], record["oom_kill_delta"]) == (outcome, 1)


@pytest.mark.parametrize(
    ("judged", "result", "outcome"),
    [
        (None, {"returncode": 3}, "exit_nonzero"),
        (True, {"returncode": 3, "timed_out": True}, "timeout"),
        (True, {"returncode": 3, "oom_killed": True}, "oom_kill"),
        (True, {"returncode": 3}, "exit_zero"),
        (False, {"returncode": 0}, "exit_nonzero"),
    ],
    ids=["unjudged", "timeout", "oom-kill", "judged-success", "judged-failure"],
)
def test_judged_commands_follow_the_judgment_except_for_stops(tmp_path, judged, result, outcome):
    label = job_resources.ResourceLabel("test", "echo_tool")
    process = SimpleNamespace(
        **{
            "timed_out": False,
            "oom_killed": False,
            "host_oom_kill": False,
            "duration_s": 1.0,
            **result,
        }
    )
    measurement = job_resources.JobMeasurement(0.25, job_resources.Usage(), {}, None, None, None)

    with job_resources.judged_commands() as commands:
        job_resources.record_command("flow", label, (), measurement, process)
        assert _history(tmp_path) == []  # held until the block ends
        if judged is not None:
            commands.judge(judged)
            commands.judge(not judged, command=5)  # no such command: ignored

    (record,) = _history(tmp_path)
    assert (record["outcome"], record["returncode"]) == (outcome, result["returncode"])


@posix_only
def test_own_kill_without_a_sandbox_limit_oom_is_a_host_kill(flow, tmp_path, monkeypatch):
    # The kill counter rose but the Sandbox never reached its own limit.
    _oom_counter(monkeypatch, iter([0, 1]), iter([0, 0]))

    result = _execute(flow, _python(SELF_SIGKILL))

    assert result.oom_killed and result.host_oom_kill and result.oom_blamed
    (record,) = _history(tmp_path)
    assert record["outcome"] == "host_oom_kill"


@posix_only
def test_own_kill_with_a_sandbox_limit_oom_is_not_a_host_kill(flow, tmp_path, monkeypatch):
    _oom_counter(monkeypatch, iter([0, 1]), iter([0, 1]))

    result = _execute(flow, _python(SELF_SIGKILL))

    assert result.oom_killed and not result.host_oom_kill
    (record,) = _history(tmp_path)
    assert record["outcome"] == "oom_kill"


def test_unreadable_limit_count_is_not_a_host_kill():
    assert job_resources.host_oom_kill(_measurement(limit=0))
    assert not job_resources.host_oom_kill(_measurement(limit=2))
    assert not job_resources.host_oom_kill(_measurement(limit=None))


def test_nonzero_exit_without_a_kill_sign_is_no_kill_of_either_kind(flow, tmp_path, monkeypatch):
    # The counter rose (another Job's kill), but this command only failed.
    _oom_counter(monkeypatch, iter([0, 1]), iter([0, 0]))

    result = _execute(flow, _python("raise SystemExit(1)"))

    assert not result.oom_killed and not result.host_oom_kill
    (record,) = _history(tmp_path)
    assert record["outcome"] == "exit_nonzero"


def test_shell_child_kill_report_is_a_kill_sign():
    cases = [
        ("/work/build.sh: line 12: 4242 Killed                  g++ -c a.cc\n", True),
        ("/work/my scripts/build.sh: line 3: 99 Killed  sh x.sh\n", True),
        ("bash: line 1: 2741064 Killed                     sh self.sh\n", True),
        ("Killed\n", True),
        # Only a whole line is a report; a mention mid-line proves nothing.
        ("echo Killed the process\n", False),
        ("note: Killed is a word\n", False),
    ]
    for stderr, expected in cases:
        result = SubprocessResult(returncode=1, stderr=stderr)

        killed = job_resources.own_oom_kill(_measurement(), result, cancelled=False)

        assert killed is expected, stderr


def test_fresh_kill_log_of_the_command_is_a_kill_sign(tmp_path):
    log = tmp_path / "runme.log"
    log.write_text('rdiArgs.sh: line 57: 77 Killed "$RDI_PROG"\n', encoding="utf-8", newline="\n")
    result = SubprocessResult(returncode=1, stderr="make: *** Error 1")
    now = time.time()

    def killed(**options: Any) -> bool:
        return job_resources.own_oom_kill(_measurement(), result, cancelled=False, **options)

    assert killed(kill_logs=[log], since=now - 60)
    # A previous run's log, a missing log and no log at all show no kill.
    assert not killed(kill_logs=[log], since=now + 60)
    assert not killed(kill_logs=[tmp_path / "absent.log"], since=0)
    assert not killed()


def test_a_run_log_the_command_wrote_makes_its_failure_an_own_kill(flow, tmp_path, monkeypatch):
    # Vivado writes bash's kill report to runme.log, not to the command's streams.
    _oom_counter(monkeypatch, iter([0, 1]), iter([0, 0]))
    log = tmp_path / "runme.log"
    writes_log = (
        f"open({str(log)!r}, 'w').write('rdiArgs.sh: line 57: 77 Killed \"$RDI_PROG\"\\n');"
        "raise SystemExit(1)"
    )

    with job_resources.resource_phase("test", "fpga_impl", kill_logs=(log,)):
        result = _execute(flow, _python(writes_log))

    assert result.oom_killed and result.host_oom_kill
    (record,) = _history(tmp_path)
    assert record["outcome"] == "host_oom_kill"


def test_a_stale_run_log_does_not_blame_the_next_command(flow, tmp_path, monkeypatch):
    _oom_counter(monkeypatch, iter([0, 1]), iter([0, 0]))
    log = tmp_path / "runme.log"
    log.write_text('rdiArgs.sh: line 57: 77 Killed "$RDI_PROG"\n', encoding="utf-8", newline="\n")
    long_ago = time.time() - 3600
    os.utime(log, (long_ago, long_ago))

    with job_resources.resource_phase("test", "fpga_impl", kill_logs=(log,)):
        result = _execute(flow, _python("raise SystemExit(1)"))

    assert not result.oom_killed


@posix_only
def test_unreadable_counter_or_timeout_is_never_oom_killed(flow, monkeypatch):
    # An unreadable counter proves nothing, even for a command that killed itself.
    _oom_counter(monkeypatch, iter([None, None]))
    assert not _execute(flow, _python(SELF_SIGKILL)).oom_killed

    # Booley's own timeout kill is not the OOM killer's.
    _oom_counter(monkeypatch, iter([0, 1]))
    timed_out = _execute(
        flow, _python("import time; time.sleep(30)"), timeout=math.ceil(scaled(1))
    )
    assert timed_out.timed_out and not timed_out.oom_killed


def _measurement(
    delta: int | None = 1, kills: int = 0, limit: int | None = None
) -> job_resources.JobMeasurement:
    return job_resources.JobMeasurement(
        interval_s=1.0,
        overall=job_resources.Usage(),
        phases={},
        cpu_s=None,
        oom_kill_delta=delta,
        solo=None,
        descendant_kills=kills,
        limit_oom_delta=limit,
    )


@pytest.mark.parametrize(
    ("result", "kills", "expected"),
    [
        (SubprocessResult(returncode=137), 0, True),
        (
            SubprocessResult(
                returncode=1, stderr="x\n%Error: Verilator threw signal 9.  Suggest\n"
            ),
            0,
            True,
        ),
        (SubprocessResult(returncode=1), 1, True),
        # A command that exited 0 is never blamed, whatever it reported or printed.
        (SubprocessResult(returncode=0), 1, False),
        (SubprocessResult(returncode=0, stderr="make: *** [x] Killed\n"), 0, False),
        # A kill Booley's timeout sent is not the OOM killer's.
        (SubprocessResult(returncode=-9, timed_out=True), 1, False),
        (
            SubprocessResult(returncode=1, timed_out=True, stdout="make: *** [x] Killed\n"),
            0,
            False,
        ),
        # The launcher's line starts its own line; a mention mid-line proves nothing.
        (SubprocessResult(returncode=2, stdout="echo make: *** [x] Killed\n"), 0, False),
    ],
)
def test_own_oom_kill_evidence_rules(result, kills, expected):
    assert (
        job_resources.own_oom_kill(_measurement(kills=kills), result, cancelled=False) is expected
    )


@posix_only
def test_command_killed_by_a_cancelled_scope_is_not_oom_killed(flow, tmp_path, monkeypatch):
    _oom_counter(monkeypatch, iter([0, 1]))
    started, cancelled = tmp_path / "started", threading.Event()
    scope = SupervisedExecutionScope(None, tmp_path / ".booley", cancelled.is_set)

    def cancel_once_running() -> None:
        deadline = time.monotonic() + hang_guard_s()
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled.set()

    canceller = threading.Thread(target=cancel_once_running)
    canceller.start()
    try:
        with supervised_execution_scope(scope):
            result = _execute(
                flow,
                # Ignores SIGTERM, so the cancellation ends in SIGKILL, sent by Booley.
                _python(
                    "import pathlib, signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"pathlib.Path({str(started)!r}).touch()\n"
                    "time.sleep(30)"
                ),
            )
    finally:
        canceller.join()

    assert result.returncode != 0 and not result.oom_killed


@linux_only
def test_oom_killed_simulator_counts_as_its_commands_oom_kill(flow, tmp_path, monkeypatch):
    _oom_counter(monkeypatch, iter([0, 1]))
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.setenv("PYTHONPATH", str(Path(job_resources.__file__).parents[2]))
    # What a simulator adapter does: supervise the simulator, then exit by verdict.
    adapter = f"""
import os, sys
from pathlib import Path
from booley.flows.sim.backends.supervision import SimulatorLaunch, run_supervised

run_cwd = Path({str(tmp_path / "run")!r})
run_cwd.mkdir()
launch = SimulatorLaunch(
    label="fake",
    command=[sys.executable, "-c", {SELF_SIGKILL!r}],
    run_cwd=run_cwd,
    env=dict(os.environ),
    timeout_s={hang_guard_s()},
)
run_supervised(launch)
raise SystemExit(1)
"""

    result = _execute(flow, _python(adapter))

    assert result.returncode == 1 and result.oom_killed


@linux_only
def test_concurrent_commands_fall_back_to_sampled_cpu(run, tmp_path):
    # Private seam: another command of this Job running in this process cannot be
    # started from the test without a second Flow thread.
    in_process = job_resources._IN_PROCESS
    ticket = in_process.enter()
    try:
        run(
            # Burn CPU time, not wall time, so a loaded host cannot starve the check.
            _python("import time\nwhile time.process_time() < 0.8: pass")
        )
    finally:
        in_process.leave(ticket)

    (record,) = _history(tmp_path)
    assert record["solo"] is not True
    # Sampling misses the last interval and a loaded host stretches it: only
    # the fallback itself (a measured, nonzero value) is deterministic.
    assert record["cpu_user_s"] > 0


def test_history_failure_never_changes_the_result(run, monkeypatch):
    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(job_resources, "append_records", broken)
    assert run(_python("raise SystemExit(4)")).returncode == 4


@pytest.mark.parametrize("others", [False, True, None])
def test_solo_runs_record_container_delta(run, tmp_path, monkeypatch, others):
    # Private seam: host readings; the test fixes what the kernel would report.
    monkeypatch.setattr(job_resources, "_other_jobs_hold_slots", lambda: others)
    readings = iter([100 << 20, *([140 << 20] * 50)])
    monkeypatch.setattr(job_resources, "_container_memory_bytes", lambda: next(readings))

    run(_python("pass"))

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
    # Private seam: the solo flag of a record is this check's only public effect, and a
    # record cannot show the "own slot" and "inherited slot" cases the test separates.
    holders = job_resources._other_jobs_hold_slots
    assert holders() is False
    token = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=os.getpid(),
        argv=_proc_argv(),
        memory=job_slots.MemoryClaim(budget_mb=1024, run_alone=True),
    )
    assert token.is_holder
    assert holders() is False, "this process's own slot"
    monkeypatch.setattr(job_resources.os, "getpid", lambda: token.pid + 1)
    assert holders() is True
    monkeypatch.setattr(job_resources.os, "getppid", lambda: token.pid)
    assert holders() is False, "a baseline child's inherited slot"
    monkeypatch.undo()
    store.release(token)


def _label_seen(flow_object, method: str, *args: Any) -> job_resources.ResourceLabel | None:
    # Private seam: a built-in Flow labels its command only inside its run step,
    # which needs its EDA tool; the step is called directly with the launcher replaced.
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


def _recorded_keys(
    label: job_resources.ResourceLabel | None, phases: dict[str, job_resources.Usage] | None = None
) -> list[Any]:
    """History keys of the records one successful command with *label* writes."""
    from booley.runtime.resource_history import history_key

    assert label is not None
    result = SimpleNamespace(returncode=0, timed_out=False, duration_s=1.0, oom_killed=False)
    measurement = job_resources.JobMeasurement(
        0.25, job_resources.Usage(), phases or {}, None, None, None
    )
    return [
        history_key(record)
        for record in job_resources.history_records("flow", label, (), measurement, result)
    ]


@pytest.mark.parametrize(
    ("flow_class", "method", "phase", "flow_name", "split"),
    [
        # Private seam: each method is the Flow's run step; see _label_seen.
        (LintFlow, "_run_linter", "lint", "lint", None),
        (FpgaImplFlow, "_run_vivado", "fpga_impl", "fpga", None),
        (AsicSynthesizeFlow, "_run_synth_make", "synth", "synth", job_resources.SYNTH_PHASES),
    ],
)
def test_flows_label_their_commands_and_requests_map_to_their_keys(
    flow_class, method, phase, flow_name, split
):
    from booley.flows.job_estimates import JobRequest, phase_keys

    # A logical synth plan; the other Flows take the command itself.
    plan = SimpleNamespace(spec=SimpleNamespace(timing=SimpleNamespace(mode="logical")))
    command = plan if flow_class is AsicSynthesizeFlow else ["make"]
    label = _label_seen(flow_class(), method, "core", command, 4096)
    assert label == job_resources.ResourceLabel("core", phase, split=split, source_bytes=4096)
    assert job_resources.current_label() is None

    # Synthesis also records a split tool row; the request lists only the whole command.
    recorded = _recorded_keys(label, {"yosys": job_resources.Usage()} if split else None)
    assert list(phase_keys(JobRequest(flow_name, "core"))) == recorded[:1]
    assert [key.phase for key in recorded[1:]] == (["yosys"] if split else [])


@linux_only
def test_custom_flow_request_maps_to_its_flow_name_key(run, tmp_path):
    from booley.flows.job_estimates import JobRequest, phase_keys

    assert run(_python("pass")).returncode == 0

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
    # Private seam: the flags key a record only when the Simulation Flow runs a simulator.
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
        writer.join(timeout=hang_guard_s())
        assert writer.exitcode == 0

    keys = {record["key"]["target"] for record in _history(tmp_path)}
    assert keys == {f"w{n}-{index}" for n in range(6) for index in range(10)}


def test_repeated_commands_record_only_failures(flow, tmp_path):
    with job_resources.repeated_commands():
        _execute(flow, _python("pass"))
        _execute(flow, _python("raise SystemExit(3)"))

    (record,) = _history(tmp_path)
    assert record["returncode"] == 3


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


def test_history_counts_and_keeps_jobs_not_records(tmp_path):
    def job(n: int, count: int) -> list[dict[str, Any]]:
        # Job ids sort by start, as execute_endpoint's do.
        return [_record("k", job=f"{n:03d}", seq=i) for i in range(count)]

    resource_history.append_records(tmp_path, job(0, 1))
    for n in range(1, 20):
        resource_history.append_records(tmp_path, job(n, 2))
    resource_history.append_records(tmp_path, [*job(20, 2), _record("old")])
    # Job 0 is already evicted: a late record of it neither returns nor evicts Job 1.
    resource_history.append_records(tmp_path, job(0, 1))

    records = _history(tmp_path)
    kept = [r for r in records if r["key"]["target"] == "k"]
    assert {r["job"] for r in kept} == {f"{n:03d}" for n in range(1, 21)}
    assert len(kept) == 40
    assert {s["key"]["target"]: s["runs"] for s in resource_history.summarize(records)} == {
        "k": 20,
        "old": 1,
    }


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param("not json", id="malformed"),
        pytest.param(
            '{"$schema": "booley.job-resource-history/v0", "records": []}', id="foreign-schema"
        ),
    ],
)
def test_unreadable_history_is_kept_aside(tmp_path, caplog, unreadable):
    path = resource_history.history_path(tmp_path)
    path.parent.mkdir(parents=True)
    first = path.with_name("history.json.unreadable-20261118T000000Z")
    second = path.with_name("history.json.unreadable-20261118T000000Z-2")

    with patch("booley.runtime.timefmt.compact_utc_now", return_value="20261118T000000Z"):
        path.write_bytes(unreadable.encode())
        resource_history.append_records(tmp_path, [_record("a")])
        assert first.is_file()
        assert first.read_text(encoding="utf-8") == unreadable
        assert str(first) in caplog.text

        resource_history.append_records(tmp_path, [_record("b")])
        assert not second.exists()
        assert [r["key"]["target"] for r in _history(tmp_path)] == ["a", "b"]

        path.write_bytes((unreadable + "\n").encode())
        resource_history.append_records(tmp_path, [_record("c")])

    assert first.read_text(encoding="utf-8") == unreadable
    assert second.read_text(encoding="utf-8") == unreadable + "\n"
    assert str(second) in caplog.text
    assert [r["key"]["target"] for r in _history(tmp_path)] == ["c"]


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


def test_report_shows_peak_swap_only_when_nonzero(tmp_path, capsys):
    from booley.harness import resources_cli

    resource_history.append_records(
        tmp_path,
        [
            _record("a", peak_memory_mb=100.0, wall_s=1.0, peak_swap_mb=0.0),
            _record("b", peak_memory_mb=100.0, wall_s=1.0, peak_swap_mb=256.0),
            _record("c", peak_memory_mb=100.0, wall_s=1.0),  # recorded before swap was measured
        ],
    )

    assert resources_cli.run(type("Args", (), {"json": False})(), tmp_path) == 0
    lines = {line.split(" p:")[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "peak swap 256 MiB / 256 MiB / 256 MiB (max/median 1.00)" in lines["b"]
    assert "swap" not in lines["a"] and "swap" not in lines["c"]
    assert resources_cli.run(type("Args", (), {"json": True})(), tmp_path) == 0
    by_key = {s["key"]["target"]: s["peak_swap_mb"] for s in json.loads(capsys.readouterr().out)}
    assert by_key["b"]["max"] == 256.0 and by_key["c"] is None


def test_memory_events_reads_both_counters_from_one_snapshot(tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    # Private seam: host readings; the test fixes what the kernel would report.
    monkeypatch.setattr(job_resources, "_CGROUP_MEMORY_EVENTS", events)
    events.write_bytes(b"low 0\nhigh 0\noom 3\noom_kill 2\n")
    assert job_resources.cgroup_memory_events() == job_resources.MemoryEvents(2, 3)
    # A counter that is absent or garbled is unknown, the other stays known.
    events.write_bytes(b"oom_kill x\noom 1\n")
    assert job_resources.cgroup_memory_events() == job_resources.MemoryEvents(None, 1)
    events.write_bytes(b"low 0\n")
    assert job_resources.cgroup_memory_events() == job_resources.MemoryEvents(None, None)
    events.unlink()
    assert job_resources.cgroup_memory_events() == job_resources.MemoryEvents(None, None)


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


def test_resolved_source_bytes_sums_distinct_files_or_is_unknown(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"abc")
    (tmp_path / "b.sv").write_bytes(b"12345")

    def target(*names: str) -> SimpleNamespace:
        files = tuple(SimpleNamespace(absolute=lambda root, n=n: Path(root) / n) for n in names)
        return SimpleNamespace(files=files, build_root=tmp_path)

    assert job_resources.resolved_source_bytes(target("a.sv", "b.sv", "a.sv")) == 8
    assert job_resources.resolved_source_bytes(target("a.sv", "missing.sv")) is None
    assert job_resources.resolved_source_bytes(target()) is None


def test_doctor_started_runs_write_no_records(run, tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_INTERNAL_DOCTOR_RUN", "1")
    assert run(_python("pass")).returncode == 0
    assert _history(tmp_path) == []

    monkeypatch.delenv("BOOLEY_INTERNAL_DOCTOR_RUN")
    assert run(_python("pass")).returncode == 0
    assert len(_history(tmp_path)) == 1


@pytest.mark.parametrize(
    ("field", "value", "context"),
    [
        ("source_bytes", 1234, contextlib.nullcontext),
        ("threads", 4, lambda: fixed_job_threads(4)),
    ],
)
def test_records_carry_a_label_field_only_when_known(flow, tmp_path, field, value, context):
    job_resources.label_commands("core", "sim_build", source_bytes=1234)
    with context():
        assert _execute(flow, _python("pass")).returncode == 0
    job_resources.label_commands("core", "sim_build")
    assert _execute(flow, _python("pass")).returncode == 0

    known, unknown = _history(tmp_path)
    assert known[field] == value
    # Unknown is absent, never zero.
    assert field not in unknown
    assert set(unknown) == RECORD_FIELDS


def test_history_keeps_and_reports_specialist_keys_apart(tmp_path, capsys):
    shared = {"target": "", "phase": "specialist:reviewer", "flags": [], "test_run": ""}
    records = []
    for index in range(21):
        # A Target record and a Specialist record with the very same four key fields.
        records.append(_record("", peak_memory_mb=float(index)))
        records[-1]["key"] = dict(shared)
        records.append(_record("", peak_memory_mb=float(index)))
        records[-1]["key"] = {**shared, "specialist": "reviewer"}

    resource_history.append_records(tmp_path, records)

    kept = resource_history.load_records(tmp_path)
    assert len(kept) == 40
    for name in (None, "reviewer"):
        group = [r for r in kept if resource_history.specialist_name(r) == name]
        assert [r["peak_memory_mb"] for r in group] == [float(i) for i in range(1, 21)]

    summaries = resource_history.summarize(kept)
    assert [s["key"].get("specialist") for s in summaries] == [None, "reviewer"]
    assert [s["runs"] for s in summaries] == [20, 20]
    assert (
        resource_history.render_report(summaries)
        .splitlines()[1]
        .startswith("Specialist reviewer: 20 runs; peak memory ")
    )
