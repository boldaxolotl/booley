"""The FPGA Flow's Vivado startup owner grades a bounded batch start without Doctor.

Every assertion reads the returned ``DiagnosticReport``. The owner modules are
imported inside helpers so each test fails on its own while they are missing.
``subprocess.run`` is replaced on the stdlib module the owner calls at run time
except where a test runs the real startup script against a fake ``vivado``.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.runtime import session_runtime
from tests.subprocess_budget import run_bounded_child

_LABEL = "fpga: Vivado batch startup"
_DEPENDENCY_FIX = "verify Vivado runtime dependencies in the Sandbox Image"


def _owner() -> Any:
    """The Vivado startup owner module, imported where used."""
    return importlib.import_module("booley.flows.fpga.vivado_startup")


def _inspect(
    root: Path, *, available: bool = True, inside: bool = True, command: Any = None
) -> DiagnosticReport:
    request = importlib.import_module("booley.flows.runtime_reality").FlowRuntimeInspectionRequest(
        flow_name="fpga",
        targets=("fpga_board",),
        project_root=root,
        runtime_available=available,
        inside_sandbox=inside,
        sandbox_command=command if command is not None else (lambda argv: argv),
    )
    return _owner().inspect_vivado_startup(request)


def _summary(report: DiagnosticReport) -> list[tuple[Severity, str, str]]:
    return [(f.severity, f.message, f.fix) for f in report.findings]


def _script_run(monkeypatch, outcome: Any) -> list[tuple[list[str], dict[str, Any]]]:
    """Replace the owner's ``subprocess.run``; *outcome* is a result or an exception."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome[0], outcome[1], outcome[2])

    monkeypatch.setattr(_owner().subprocess, "run", run)
    return calls


def _fake_vivado(directory: Path, body: str) -> Path:
    """Put a fake ``vivado`` shell script first on PATH; return its bin directory."""
    fake = directory / "bin"
    fake.mkdir()
    vivado = fake / "vivado"
    vivado.write_text("#!/bin/sh\n" + body)
    vivado.chmod(0o755)
    return fake


def test_rc0_without_marker_fails(tmp_path, monkeypatch):
    """Exit 0 without this attempt's marker FAILs with the tool's error line or a generic cause."""
    cases = [
        (
            "ERROR: Tcl initialization failed: libncurses.so.5",
            "",
            f"{_LABEL}: ERROR: Tcl initialization failed: libncurses.so.5",
        ),
        ("", "", f"{_LABEL}: current Tcl startup marker missing (exit 0)"),
        ("", "  fatal: bad install  ", f"{_LABEL}: fatal: bad install"),
    ]
    for stdout, stderr, message in cases:
        calls = _script_run(monkeypatch, (0, stdout, stderr))
        report = _inspect(tmp_path)
        assert _summary(report) == [(Severity.FAIL, message, _DEPENDENCY_FIX)]
        assert calls[0][1]["timeout"] == 30


def test_actual_batch_marker_and_owned_directory(tmp_path, monkeypatch):
    """Only a real run echoing this attempt's marker PASSes; side files stay Sandbox-local."""
    for mode in ("good", "missing", "stale", "nonzero"):
        case = tmp_path / mode
        case.mkdir()
        body = "mkdir .Xil\ntouch vivado_pid123.str\n" + (
            "echo BOOLEY_VIVADO_STARTUP_stale\n"
            if mode == "stale"
            else "exit 0\n"
            if mode == "missing"
            else 'sed -n "s/^puts //p" "$6"\n' + ("exit 1\n" if mode == "nonzero" else "exit 0\n")
        )
        fake = _fake_vivado(case, body)
        monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("TMPDIR", str(case))
        monkeypatch.chdir(case)

        report = _inspect(case)

        assert (Severity.PASS in {f.severity for f in report.findings}) is (mode == "good"), mode
        assert not (case / ".Xil").exists()
        assert not (case / "vivado_pid123.str").exists()
        assert set(case.iterdir()) == {fake}


def test_inner_timeout_is_distinct(tmp_path, monkeypatch):
    """The inner ``timeout`` exit codes (124 and the 137 kill) name the 27-second bound."""
    for returncode in (124, 137):
        _script_run(monkeypatch, (returncode, "", ""))
        assert _summary(_inspect(tmp_path)) == [
            (Severity.FAIL, f"{_LABEL}: Vivado did not start within 27s", _DEPENDENCY_FIX)
        ]


def test_outer_timeout_fails(tmp_path, monkeypatch):
    """A runtime that never answers within 30 seconds FAILs about transport, not Vivado."""
    _script_run(monkeypatch, subprocess.TimeoutExpired(["sandbox"], 30))

    assert _summary(_inspect(tmp_path)) == [
        (
            Severity.FAIL,
            f"{_LABEL}: runtime did not complete within 30s",
            "check Sandbox transport and Vivado startup",
        )
    ]


def test_unavailable_runtime_skips(tmp_path, monkeypatch):
    """Without a runtime the probe SKIPs and never builds a Sandbox command."""
    calls = _script_run(monkeypatch, (0, "", ""))
    built: list[list[str]] = []

    report = _inspect(
        tmp_path,
        available=False,
        inside=False,
        command=lambda argv: built.append(argv) or argv,
    )

    assert _summary(report) == [(Severity.SKIP, f"{_LABEL} - container runtime unavailable", "")]
    assert built == []
    assert calls == []


def test_probe_error_fails(tmp_path, monkeypatch):
    """A Sandbox start failure or an OS error is a probe-error FAIL, cut to 500 characters."""
    fix = "run 'booley init --seed' and retry"
    start_failure = session_runtime.SessionError("sandbox did not start")

    def failing_builder(_argv):
        raise start_failure

    assert _summary(_inspect(tmp_path, command=failing_builder)) == [
        (Severity.FAIL, f"{_LABEL}: runtime/probe error: sandbox did not start", fix)
    ]

    long_message = "x" * 700
    _script_run(monkeypatch, OSError(long_message))
    assert _summary(_inspect(tmp_path)) == [
        (Severity.FAIL, f"{_LABEL}: runtime/probe error: {long_message[:500]}", fix)
    ]


# The probe only runs inside the Linux Sandbox, and termination is observed via
# /proc. Elsewhere (MSYS sh/timeout on Windows) the 0.2s budget races shell
# startup and process-group semantics differ, so the test would prove nothing.
@pytest.mark.skipif(sys.platform != "linux", reason="Linux Sandbox process-group contract")
def test_timeout_kills_child_process_group(tmp_path, monkeypatch):
    """The startup script kills Vivado's whole process group, even a child that lingers."""
    for delayed_exit in (False, True):
        case = tmp_path / f"delayed-{delayed_exit}"
        case.mkdir()
        pid_file = case / "child.pid"
        child = (
            "sh -c 'trap \"sleep 0.1; exit 0\" TERM; sleep 60' >/dev/null 2>&1"
            if delayed_exit
            else "sleep 60"
        )
        fake = _fake_vivado(case, f'{child} &\necho $! > "{pid_file}"\nwait\n')
        monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("TMPDIR", str(case))
        captured: list[list[str]] = []

        def sandbox_command(argv, captured=captured):
            captured.append(list(argv))
            return argv

        # The owner's own run is stubbed to the inner-timeout exit; the captured
        # script is then run with a shortened budget, as the Sandbox would run it.
        _script_run(monkeypatch, (124, "", ""))
        report = _inspect(case, command=sandbox_command)
        assert [f.severity for f in report.findings] == [Severity.FAIL]

        argv = captured[0]
        assert "timeout --kill-after=2s 27s" in argv[-1]
        assert "--foreground" not in argv[-1]
        argv[-1] = argv[-1].replace("27s", "0.2s")
        result = run_bounded_child(argv, timeout=5)
        assert result.returncode == 124
        pid = int(pid_file.read_text())
        stat = Path(f"/proc/{pid}/stat")
        # timeout waits for its direct child; group members finish asynchronously.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                state = stat.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"Vivado child {pid} did not terminate within 3 seconds")
        assert set(case.iterdir()) == {fake, pid_file}
