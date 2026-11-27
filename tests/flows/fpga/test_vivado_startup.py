"""The FPGA Flow's Vivado startup owner grades a bounded batch start without Doctor.

Every assertion reads the returned ``DiagnosticReport``. The request's Sandbox
command builder is the boundary: a test hands it a builder that runs a canned
process, fails as a Sandbox start would, or returns the real startup script to run
against a fake ``vivado``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.flows.base import SubprocessResult
from booley.flows.fpga.vivado_startup import inspect_vivado_startup
from booley.flows.runtime_reality import FlowRuntimeInspectionRequest
from booley.runtime import session_runtime
from booley.runtime.runtime_context import RuntimeLocation
from booley.runtime.sandbox_apply import Attachment
from tests.canned_process_support import canned_command
from tests.runtime.test_sandbox_remedy_workflows import VSCODE_INIT
from tests.sandbox_venue_fake import place
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import scaled

_LABEL = "fpga: Vivado batch startup"
_DEPENDENCY_FIX = "verify Vivado runtime dependencies in the Sandbox Image"


def _inspect(
    root: Path, *, available: bool = True, command: Callable[[list[str]], list[str]] = list
) -> DiagnosticReport:
    request = FlowRuntimeInspectionRequest(
        flow_name="fpga",
        targets=("fpga_board",),
        project_root=root,
        runtime_available=available,
        inside_sandbox=available,
        sandbox_command=command,
    )
    return inspect_vivado_startup(request)


def _summary(report: DiagnosticReport) -> list[tuple[Severity, str, str]]:
    return [(f.severity, f.message, f.fix) for f in report.findings]


def _fake_vivado(directory: Path, body: str) -> Path:
    """Put a fake ``vivado`` shell script first on PATH; return its bin directory."""
    fake = directory / "bin"
    fake.mkdir()
    vivado = fake / "vivado"
    vivado.write_text("#!/bin/sh\n" + body)
    vivado.chmod(0o755)
    return fake


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "cause"),
    [
        (
            0,
            "ERROR: Tcl initialization failed: libncurses.so.5",
            "",
            "ERROR: Tcl initialization failed: libncurses.so.5",
        ),
        (0, "", "", "current Tcl startup marker missing (exit 0)"),
        (0, "", "  fatal: bad install  ", "fatal: bad install"),
        # The inner ``timeout`` exit codes, 124 and the 137 kill, name its bound.
        (124, "", "", "Vivado did not start within 27s"),
        (137, "", "", "Vivado did not start within 27s"),
    ],
    ids=["error-line", "no-marker", "stderr-line", "inner-timeout", "inner-kill"],
)
def test_failed_startup_names_its_cause(
    tmp_path: Path, returncode: int, stdout: str, stderr: str, cause: str
) -> None:
    """A start without this attempt's marker FAILs with the tool's error line or a generic cause."""
    result = SubprocessResult(returncode=returncode, stdout=stdout, stderr=stderr)

    report = _inspect(tmp_path, command=lambda _argv: canned_command(result))

    assert _summary(report) == [(Severity.FAIL, f"{_LABEL}: {cause}", _DEPENDENCY_FIX)]


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


def test_unavailable_runtime_skips(tmp_path):
    """Without a runtime the probe SKIPs and never builds a Sandbox command."""
    built: list[list[str]] = []

    report = _inspect(tmp_path, available=False, command=lambda argv: built.append(argv) or argv)

    assert _summary(report) == [(Severity.SKIP, f"{_LABEL} - container runtime unavailable", "")]
    assert built == []


_REMEDY = VSCODE_INIT + ", then retry"


@pytest.mark.parametrize(
    ("error", "finding"),
    [
        # The real bound is the runner's 30-second timeout, too long to wait for here.
        (
            subprocess.TimeoutExpired(["sandbox"], 30),
            (
                Severity.FAIL,
                ": runtime did not complete within 30s",
                "check Sandbox transport and Vivado startup",
            ),
        ),
        (
            session_runtime.RecoveryPendingError(),
            (Severity.SKIP, " skipped - Sandbox recovery is pending on this host", ""),
        ),
        (
            session_runtime.SessionError("sandbox did not start"),
            (Severity.FAIL, ": runtime/probe error: sandbox did not start", _REMEDY),
        ),
        # An OS error's text is cut to 500 characters.
        (OSError("x" * 700), (Severity.FAIL, ": runtime/probe error: " + "x" * 500, _REMEDY)),
    ],
    ids=["outer-timeout", "recovery-pending", "sandbox-start", "os-error"],
)
def test_sandbox_failure_is_a_finding(tmp_path, monkeypatch, error, finding):
    # The remedy is the host's: tell the host from the Sandbox, and answer the Docker
    # inventory that names how the Project's Sandbox is entered.
    place(monkeypatch, RuntimeLocation.HOST)
    monkeypatch.setattr(session_runtime, "sandbox_attachment", lambda _root: Attachment.VSCODE)

    def failing_builder(_argv):
        raise error

    severity, message, fix = finding
    assert _summary(_inspect(tmp_path, command=failing_builder)) == [
        (severity, _LABEL + message, fix)
    ]


def test_sandbox_command_that_cannot_start_is_a_probe_error(tmp_path, monkeypatch):
    """A Sandbox command the runner cannot start FAILs as a probe error, not a crash."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")

    report = _inspect(tmp_path, command=lambda _argv: [str(tmp_path / "missing-sandbox-cli")])

    [finding] = report.findings
    assert finding.severity is Severity.FAIL
    assert finding.message.startswith(f"{_LABEL}: runtime/probe error: ")


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

        # The probe's own run answers the inner-timeout exit; the captured script
        # is then run with a shortened budget, as the Sandbox would run it.
        def sandbox_command(argv, captured=captured):
            captured.append(list(argv))
            return canned_command(SubprocessResult(returncode=124))

        report = _inspect(case, command=sandbox_command)
        assert [f.severity for f in report.findings] == [Severity.FAIL]

        argv = captured[0]
        assert "timeout --kill-after=2s 27s" in argv[-1]
        assert "--foreground" not in argv[-1]
        argv[-1] = argv[-1].replace("27s", f"{scaled(0.2):g}s")
        result = run_bounded_child(argv)
        assert result.returncode == 124
        pid = int(pid_file.read_text())
        stat = Path(f"/proc/{pid}/stat")
        # timeout waits for its direct child; group members finish asynchronously.
        deadline_s = scaled(3)
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                state = stat.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"Vivado child {pid} did not terminate within {deadline_s:g} seconds")
        assert set(case.iterdir()) == {fake, pid_file}
