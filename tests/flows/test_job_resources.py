"""Job resource history: every Flow command is measured and recorded (#2470)."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
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
from tests.flows.test_flow_base import EchoFlow, _env_with_state

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


@linux_only
def test_file_in_job_tmpdir_counts_as_peak_memory(flow, tmp_path):
    code = (
        "import os, time\n"
        "with open(os.path.join(os.environ['TMPDIR'], 'big'), 'wb') as f: f.write(b'x' * (32 << 20))\n"
        "print(os.environ['TMPDIR']); time.sleep(0.6)"
    )
    result = flow._execute_local(_python(code))

    (record,) = _history(tmp_path)
    assert record["peak_tmp_mb"] >= 32
    assert record["peak_memory_mb"] >= record["peak_tmp_mb"] + 5
    assert not Path(result.stdout.strip()).exists(), "the Job's TMPDIR is removed afterwards"


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
    label = _label_seen(flow_class(), method, "core", ["make"])
    assert label == job_resources.ResourceLabel("core", phase, split=split)
    assert job_resources.current_label() is None


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


def test_history_is_bounded_and_survives_a_restart(tmp_path, monkeypatch):
    for index in range(resource_history.MAX_SAMPLES_PER_KEY + 5):
        resource_history.append_records(tmp_path, [_record("same", seq=index)])
    monkeypatch.setattr(resource_history, "MAX_RECORDS", 22)
    resource_history.append_records(tmp_path, [_record("a"), _record("b"), _record("c")])

    records = json.loads(resource_history.history_path(tmp_path).read_text())["records"]
    assert len(records) == 22
    assert [r["seq"] for r in records if "seq" in r] == list(range(6, 25))


def test_history_is_ignored_by_git(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / ".gitignore").write_text(PROJECT_GITIGNORE)
    resource_history.append_records(booley_dir, [_record("x")])
    path = resource_history.history_path(booley_dir)
    subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "-q", str(path)], check=True, timeout=30
    )


def test_cgroup_oom_counter_parser(tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\noom 3\noom_kill 2\n", encoding="utf-8")
    monkeypatch.setattr(job_resources, "_CGROUP_MEMORY_EVENTS", events)
    assert job_resources.cgroup_oom_kill_count() == 2
