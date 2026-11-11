"""``booley session down`` names the Standalone Jobs that die with the Sandbox (#2300)."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from booley.harness import booley
from booley.harness.standalone_job import standalone_jobs_dir
from booley.runtime import job_records
from booley.runtime import session_runtime as sr
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_admission import Sandbox

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the Sandbox probe reads Linux /proc; Sandboxes are Linux containers",
)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
STARTED_AT = "2027-01-15T06:58:00Z"  # 1h 02m before NOW
DEAD_PID = 4_194_305  # above the Linux pid_max ceiling: never a live process
SANDBOX = Sandbox(
    container_id="abc123",
    name="booley-sandbox",
    project_root="/project",
    project_id=None,
    created_at=datetime(2027, 1, 15, tzinfo=UTC),
    started_at=None,
    running=True,
    vscode=False,
)


def _load(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the module under test inside the test body and freeze its clock."""
    warning = importlib.import_module("booley.harness.sandbox_stop_warning")
    monkeypatch.setattr(warning, "wall_clock", lambda: NOW)
    return warning


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


def _real_probe(warning: Any, booley_dir: Path):
    """Stand-in for ``docker exec``: run the shipped probe locally against ``booley_dir``."""

    def run(_sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", warning._PROBE, warning._RECORDS_RELPATH],
            env={**os.environ, "BOOLEY_DIR": str(booley_dir)},
            capture_output=True,
            text=True,
            check=False,
        )

    return run


def _probe_output(warning: Any, booley_dir: Path) -> dict[str, Any]:
    result = _real_probe(warning, booley_dir)(SANDBOX)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _session_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    sandbox: Sandbox | None = SANDBOX,
) -> tuple[int, str, str]:
    """Run ``booley session down`` with only the Docker-facing ``down`` replaced."""

    def fake_down(_root: Path, *, before_stop=None) -> sr.DownResult:
        assert before_stop is not None
        before_stop(sandbox)
        return sr.DownResult(headless=True)

    monkeypatch.setattr(sr, "down", fake_down)
    monkeypatch.setattr(sr, "session_container_name", lambda _root: "booley-sandbox")
    args = booley._build_parser().parse_args(["session", "down"])
    code = booley._cmd_session(args, tmp_path)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_session_down_names_running_standalone_job_and_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning = _load(monkeypatch)
    record = job_records.JobRecord(
        run_id="r1",
        endpoint="sim",
        started_at=STARTED_AT,
        timeout_s=60,
        argv=["booley", "flow", "sim", "--target", "sim_fifo"],
        work_dir="/workspace/wt",
    )
    stdout = json.dumps({"running": [record.to_dict()], "unreadable": []})
    monkeypatch.setattr(
        warning, "_run_probe", lambda _s: subprocess.CompletedProcess([], 0, stdout, "")
    )

    code, out, err = _session_down(monkeypatch, tmp_path, capsys)

    assert code == 0
    assert out == "removed booley-sandbox\n"
    assert err == (
        "warning: Standalone Job flow sim (Target sim_fifo, worktree /workspace/wt) "
        "has run for 1h 02m and stops with the Sandbox\n"
    )


@linux_only
def test_probe_keeps_only_live_running_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = _own_identity()
    _write(tmp_path, "live", identity=own)
    _write(tmp_path, "no-identity", identity=None)
    _write(tmp_path, "dead", identity={**own, "pid": DEAD_PID})
    _write(tmp_path, "reused", identity={**own, "start_token": own["start_token"] + 1})
    _write(tmp_path, "earlier-sandbox", identity={**own, "identity_scope": "pid:[1]"})
    _write(tmp_path, "finished", status="done", identity=own)

    output = _probe_output(_load(monkeypatch), tmp_path)

    assert sorted(item["run_id"] for item in output["running"]) == ["live", "no-identity"]
    assert output["unreadable"] == []


@linux_only
def test_probe_reports_nothing_without_records_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _probe_output(_load(monkeypatch), tmp_path) == {"running": [], "unreadable": []}


def test_session_down_output_unchanged_without_running_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning = _load(monkeypatch)
    stdout = json.dumps({"running": [], "unreadable": []})
    monkeypatch.setattr(
        warning, "_run_probe", lambda _s: subprocess.CompletedProcess([], 0, stdout, "")
    )

    code, out, err = _session_down(monkeypatch, tmp_path, capsys)

    assert (code, out, err) == (0, "removed booley-sandbox\n", "")


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    probe: Any,
    reason: str,
) -> None:
    warning = _load(monkeypatch)

    def run_probe(_sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
        if isinstance(probe, BaseException):
            raise probe
        return probe

    monkeypatch.setattr(warning, "_run_probe", run_probe)
    sandbox = SANDBOX if probe is not None else None

    code, out, err = _session_down(monkeypatch, tmp_path, capsys, sandbox=sandbox)

    assert code == 0
    assert out == "removed booley-sandbox\n"
    assert err.startswith("warning: skipped the running Standalone Job check: ")
    assert reason in err
    assert err.count("\n") == 1


@linux_only
def test_unreadable_record_is_named_and_live_jobs_still_warned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning = _load(monkeypatch)
    _write(tmp_path, "live", identity=_own_identity(), argv=["booley", "specialist", "rtl"])
    records_dir = tmp_path / standalone_jobs_dir(Path())
    (records_dir / "broken.json").write_bytes(b"{not json")
    (records_dir / "bad-schema.json").write_bytes(b'{"status": "running", "run_id": "bad-schema"}')
    monkeypatch.setattr(warning, "_run_probe", _real_probe(warning, tmp_path))

    code, out, err = _session_down(monkeypatch, tmp_path, capsys)

    assert code == 0
    assert out == "removed booley-sandbox\n"
    assert err.splitlines() == [
        "warning: Standalone Job specialist sim (worktree /workspace/wt) "
        "has run for 1h 02m and stops with the Sandbox",
        "warning: skipped the running Standalone Job check: "
        "unreadable Job records: bad-schema.json, broken.json",
    ]
