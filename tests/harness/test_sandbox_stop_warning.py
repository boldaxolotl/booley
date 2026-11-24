"""``booley session down`` names the Standalone Jobs that die with the Sandbox (#2300)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from booley.harness import sandbox_stop_warning
from booley.harness.standalone_job import standalone_jobs_dir
from booley.runtime import booley_dir as booley_dir_module
from booley.runtime import job_records, session_runtime
from booley.runtime.pid import capture_process_identity
from booley.runtime.platform_paths import run_captured_bounded
from tests.harness.session_docker_fake import (
    SessionDockerFake,
    install_sandbox_exec,
    route_session_commands,
    run_cli,
)
from tests.time_budget import hang_guard_s

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the Sandbox probe reads Linux /proc; Sandboxes are Linux containers",
)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
STARTED_AT = "2027-01-15T06:58:00Z"  # 1h 02m before NOW
DEAD_PID = 4_194_305  # above the Linux pid_max ceiling: never a live process


def _write(
    booley_dir: Path,
    run_id: str,
    *,
    status: str = "running",
    identity: dict[str, Any] | None = None,
    argv: list[str] | None = None,
) -> None:
    record = job_records.JobRecord(
        run_id=run_id,
        endpoint="sim",
        started_at=STARTED_AT,
        timeout_s=60,
        argv=argv or ["booley", "flow", "sim", "--target", "sim_fifo"],
        status=status,
        work_dir="/workspace/wt",
        process_identity=identity,
    )
    job_records.write_record_strict(record, booley_dir / standalone_jobs_dir(Path()))


def _own_identity() -> dict[str, Any]:
    identity = capture_process_identity(os.getpid())
    assert identity is not None
    return identity.to_payload()


Probe = Callable[[list[str]], subprocess.CompletedProcess[str]]


class _SessionDown:
    """Run `booley session down` for one Session of a Project whose Booley Directory is here."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path
        self._capsys = capsys
        self.root = tmp_path / "project"
        self.booley_dir = self.root / ".booley"
        self.booley_dir.mkdir(parents=True)
        monkeypatch.setattr(sandbox_stop_warning, "wall_clock", lambda: NOW)
        monkeypatch.chdir(self.root)
        monkeypatch.setenv("BOOLEY_DIR", str(self.booley_dir))
        booley_dir_module.reset_cache()

    def __call__(
        self, probe: Probe | None = None, *, running: bool = True
    ) -> tuple[str, list[tuple[list[str], dict[str, Any]]]]:
        """Return stderr and the probe calls; the command exits 0 and removes the Session."""
        fake = SessionDockerFake()
        name = session_runtime.session_container_name(self.root)
        fake.add_session(name, self.root)
        fake.containers[name]["State"]["Running"] = running
        probed = route_session_commands(
            self._monkeypatch, self._tmp_path, fake.run, fake.containers, probe=probe
        )
        code = run_cli(self._monkeypatch, ["-C", str(self.root), "session", "down"])
        out = self._capsys.readouterr()
        assert code == 0, out.err
        assert out.out == f"removed {name}\n"
        return out.err, probed

    def real_probe(self) -> Probe:
        """Run the shipped probe in a `docker exec` that executes on this host."""
        install_sandbox_exec(self._tmp_path, self._monkeypatch, self.booley_dir)
        return lambda argv: run_captured_bounded(argv, timeout=hang_guard_s())


@pytest.fixture
def session_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> _SessionDown:
    return _SessionDown(monkeypatch, tmp_path, capsys)


def _answer(running: list[Any], unreadable: list[str] | None = None) -> Probe:
    stdout = json.dumps({"running": running, "unreadable": unreadable or []})
    return lambda argv: subprocess.CompletedProcess(argv, 0, stdout, "")


def test_session_down_names_running_standalone_job_and_completes(
    session_down: _SessionDown,
) -> None:
    record = job_records.JobRecord(
        run_id="r1",
        endpoint="sim",
        started_at=STARTED_AT,
        timeout_s=60,
        argv=["booley", "flow", "sim", "--target", "sim_fifo"],
        work_dir="/workspace/wt",
    )

    err, probed = session_down(_answer([record.to_dict()]))

    assert err == (
        "warning: Standalone Job flow sim (Target sim_fifo, worktree /workspace/wt) "
        "has run for 1h 02m and stops with the Sandbox\n"
    )
    ((argv, kwargs),) = probed
    assert (
        argv[argv.index("-i") + 1]
        == f"id-{session_runtime.session_container_name(session_down.root)}"
    )
    assert argv[argv.index("python3") :][:2] == ["python3", "-I"]
    assert kwargs["stdin"] == subprocess.DEVNULL


@linux_only
def test_probe_keeps_only_live_running_records(session_down: _SessionDown) -> None:
    own = _own_identity()
    booley = session_down.booley_dir

    def write(run_id: str, **kwargs: Any) -> None:
        _write(booley, run_id, argv=["booley", "flow", "sim", "--target", run_id], **kwargs)

    write("live", identity=own)
    write("no-identity", identity=None)
    write("dead", identity={**own, "pid": DEAD_PID})
    write("reused", identity={**own, "start_token": own["start_token"] + 1})
    write("earlier-sandbox", identity={**own, "identity_scope": "pid:[1]"})
    write("finished", status="done", identity=own)
    # An identity the probe cannot judge counts as live: warn rather than hide the Job.
    write("scopeless", identity={"pid": own["pid"], "start_token": own["start_token"]})

    err, _probed = session_down(session_down.real_probe())

    assert sorted(re.findall(r"\(Target (\S+),", err)) == ["live", "no-identity", "scopeless"]
    assert err.count("\n") == 3


@linux_only
def test_probe_reports_nothing_without_records_dir(session_down: _SessionDown) -> None:
    err, _probed = session_down(session_down.real_probe())

    assert err == ""


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (
            subprocess.CompletedProcess([], 1, "", "docker: no such container\n"),
            "no such container",
        ),
        (subprocess.CompletedProcess([], 0, "not json", ""), "Expecting value"),
        (subprocess.CompletedProcess([], 0, '{"running": "x"}', ""), "no 'running' list"),
        (OSError("docker not found"), "docker not found"),
        (subprocess.TimeoutExpired("docker", 15), "timed out"),
        (None, "the Sandbox is not running"),
    ],
    ids=["exit-code", "not-json", "bad-shape", "no-docker", "timeout", "sandbox-stopped"],
)
def test_session_down_skips_check_and_completes(
    session_down: _SessionDown, probe: Any, reason: str
) -> None:
    def run_probe(_argv: list[str]) -> subprocess.CompletedProcess[str]:
        if isinstance(probe, BaseException):
            raise probe
        return probe

    err, _probed = session_down(run_probe, running=probe is not None)

    assert err.startswith("warning: skipped the running Standalone Job check: ")
    assert reason in err
    assert err.count("\n") == 1


@linux_only
def test_unreadable_record_is_named_and_live_jobs_still_warned(session_down: _SessionDown) -> None:
    booley = session_down.booley_dir
    _write(booley, "live", identity=_own_identity(), argv=["booley", "specialist", "rtl"])
    records_dir = booley / standalone_jobs_dir(Path())
    (records_dir / "broken.json").write_bytes(b"{not json")
    (records_dir / "bad-schema.json").write_bytes(b'{"status": "running", "run_id": "bad-schema"}')

    err, _probed = session_down(session_down.real_probe())

    assert err.splitlines() == [
        "warning: Standalone Job specialist sim (worktree /workspace/wt) "
        "has run for 1h 02m and stops with the Sandbox",
        "warning: skipped the running Standalone Job check: "
        "unreadable Job records: bad-schema.json, broken.json",
    ]


@pytest.mark.parametrize(
    "bad_fields",
    [{"argv": {"x": 1, "y": 2}}, {"started_at": 5}],
    ids=["argv-not-a-list", "started-at-not-a-string"],
)
def test_wrong_typed_running_record_is_unreadable_not_a_crash(
    session_down: _SessionDown, bad_fields: dict[str, Any]
) -> None:
    good = job_records.JobRecord(
        run_id="good", endpoint="sim", started_at=STARTED_AT, timeout_s=60, argv=["booley", "flow"]
    )
    bad = {**good.to_dict(), "run_id": "bad", **bad_fields}

    err, _probed = session_down(_answer([good.to_dict(), bad]))

    assert err.splitlines() == [
        "warning: Standalone Job flow sim (worktree unknown) has run for 1h 02m "
        "and stops with the Sandbox",
        "warning: skipped the running Standalone Job check: unreadable Job records: bad.json",
    ]
