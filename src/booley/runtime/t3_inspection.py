"""Doctor's UI checks: T3 Code's install, server and host relay, and the web Dashboard."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.boundary import require_dict, require_str
from booley.core.resources import package_data_dir
from booley.runtime import incontainer_setup, platform_paths, runtime_context, session_runtime
from booley.runtime.incontainer_setup import T3_PORT
from booley.runtime.sandbox_layout import DASHBOARD_PORT

_VERSION_TIMEOUT_S = 30
_SERVER = f"127.0.0.1:{T3_PORT} in the Sandbox"
_DASHBOARD = f"127.0.0.1:{DASHBOARD_PORT} in the Sandbox"


def pinned_version() -> str:
    """The T3 Code release the Sandbox Image installs, from the packaged manifest."""
    manifest = package_data_dir() / "docker" / "agent-clis-package.json"
    document = require_dict(json.loads(manifest.read_text(encoding="utf-8")))
    return require_str(require_dict(document.get("t3code"), field="t3code"), "version")


def inspect_install(command: list[str], remedy: Callable[[], str]) -> DiagnosticReport:
    """Run *command* (``t3 --version``, however it is reached) and compare it with the pin."""
    findings = Findings()
    version = pinned_version()
    reported = failure = ""
    try:
        result = platform_paths.run_captured_bounded(command, timeout=_VERSION_TIMEOUT_S)
        reported = result.stdout.strip()
        if result.returncode:
            # The last stderr line says why: no `t3`, or Docker could not run the image.
            why = result.stderr.strip().splitlines()[-1:]
            failure = ": ".join([f"exited {result.returncode}", *why])
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = f"could not run: {exc}"
    if failure:
        findings.fail(
            f"T3 Code {version} installed check failed: t3 --version {failure}",
            f"rerun `booley doctor`; if it fails again, recreate the Sandbox: {remedy()}",
        )
    elif reported == f"t3 v{version}":
        findings.pass_(f"T3 Code {version} installed (the version the manifest pins)")
    else:
        findings.fail(
            f"T3 Code {version} is not installed: t3 --version reported {reported or 'nothing'}",
            f"recreate the Sandbox: {remedy()}",
        )
    return findings.report()


def inspect_ui(project_root: Path) -> DiagnosticReport:
    """On the host: whether T3 Code and the Dashboard answer in the Sandbox and the relay runs."""
    findings = Findings()
    try:
        # Doctor skips what needs the Sandbox until a lifecycle command recovers it.
        if session_runtime.recovery_pending():
            raise session_runtime.RecoveryPendingError
        state = session_runtime.ui_state(project_root)
    except session_runtime.SessionError as exc:
        findings.skip(f"T3 Code server and relay checks could not run: {exc}")
        return findings.report()
    if state is None:
        _ui_not_applicable(findings, "the Sandbox is not running")
    elif state.vscode:
        _not_applicable(findings, "VS Code owns the Sandbox, which has no T3 Code relay")
        _judge_dashboard(findings, state)
    elif (
        state.t3 in ("off", incontainer_setup.T3_PORT_TAKEN)
        and state.relay is None
        and state.relay_error is None
    ):
        # No relay is recorded: `session up` started no UI that is still meant to run.
        _ui_not_applicable(findings, "the UI is off (booley session up --no-ui)")
    else:
        _judge_server(findings, state.t3)
        _judge_relay(findings, state)
        _judge_dashboard(findings, state)
    return findings.report()


def inspect_ui_in_sandbox() -> DiagnosticReport:
    """Inside the Sandbox: whether T3 Code and the Dashboard answer; the relay is on the host."""
    findings = Findings()
    _judge_server_in_sandbox(findings, incontainer_setup.t3_state())
    if runtime_context.in_hand_written_sandbox():
        findings.skip("T3 Code relay check not applicable - Booley did not create this Sandbox")
    else:
        findings.skip("T3 Code relay check skipped - it runs from the host")
    if incontainer_setup.dashboard_state() == "running":
        findings.pass_(f"Dashboard answers on {_DASHBOARD}")
    else:
        _skip_unproven(findings, "Dashboard does not answer in the Sandbox")
    return findings.report()


def _judge_server_in_sandbox(findings: Findings, t3: str) -> None:
    if t3 == "running":
        findings.pass_(f"T3 Code answers on {_SERVER}")
        return
    what = "T3 Code does not answer in the Sandbox"
    if t3 == incontainer_setup.T3_PORT_TAKEN:
        what = f"{_SERVER} is held by another process"
    _skip_unproven(findings, what)


def _skip_unproven(findings: Findings, what: str) -> None:
    """Skip a server that does not answer, as seen from inside the Sandbox."""
    # A hand-written Sandbox has no host-side `booley` to send the user to.
    if not runtime_context.in_hand_written_sandbox():
        what += (
            "; from inside, Doctor cannot tell a --no-ui start from a stopped server "
            "- run booley doctor on the host"
        )
    findings.skip(what)


def _not_applicable(findings: Findings, reason: str) -> None:
    for check in ("server", "relay"):
        findings.skip(f"T3 Code {check} check not applicable - {reason}")


def _ui_not_applicable(findings: Findings, reason: str) -> None:
    _not_applicable(findings, reason)
    findings.skip(f"Dashboard check not applicable - {reason}")


def _judge_dashboard(findings: Findings, state: session_runtime.UiState) -> None:
    if state.dashboard == "running":
        findings.pass_(f"Dashboard answers on {_DASHBOARD}")
    elif state.dashboard != "off":
        findings.fail(
            "Doctor cannot tell whether the Dashboard answers in the Sandbox",
            "run `booley session up`, then rerun `booley doctor`",
        )
    elif state.vscode:
        # `session up` runs no hook in this Sandbox; the start hook VS Code runs does.
        findings.fail(
            f"Dashboard does not answer on {_DASHBOARD}",
            "run `Dev Containers: Rebuild Container` in VS Code, which starts the Dashboard",
        )
    else:
        findings.fail(
            f"Dashboard does not answer on {_DASHBOARD}",
            "run `booley session up`, which starts the Dashboard",
        )


def _judge_server(findings: Findings, t3: str) -> None:
    if t3 == "running":
        findings.pass_(f"T3 Code answers on {_SERVER}")
    elif t3 == "off":
        findings.fail(
            f"T3 Code does not answer on {_SERVER}",
            "run `booley session up`, which starts T3 Code",
        )
    elif t3 == incontainer_setup.T3_PORT_TAKEN:
        findings.fail(
            f"{_SERVER} is held by another process, so T3 Code is off",
            "stop that process, then run `booley session up`",
        )
    else:
        findings.fail(
            "Doctor cannot tell whether T3 Code answers in the Sandbox",
            "run `booley session up`, then rerun `booley doctor`",
        )


def _judge_relay(findings: Findings, state: session_runtime.UiState) -> None:
    relay = state.relay
    if state.relay_error is not None:
        findings.fail(
            state.relay_error, "rerun `booley doctor` after the repair the message names"
        )
    elif relay is not None and relay.running:
        findings.pass_(f"T3 Code relay runs on 127.0.0.1:{relay.port}")
    elif relay is not None or state.t3 == "running":
        findings.fail(
            "the T3 Code relay is not running for this Sandbox",
            "run `booley session up`, which starts the relay",
        )
    else:
        findings.skip(
            "T3 Code relay check skipped - no relay is recorded and Doctor cannot tell "
            "whether T3 Code runs"
        )
