"""Doctor's T3 Code checks: the pinned install, the server in the Sandbox, the host relay."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.boundary import require_dict, require_str
from booley.core.resources import package_data_dir
from booley.runtime import incontainer_setup, platform_paths, session_runtime
from booley.runtime.incontainer_setup import T3_PORT

_VERSION_TIMEOUT_S = 30
_SERVER = f"127.0.0.1:{T3_PORT} in the Sandbox"


def pinned_version() -> str:
    """The T3 Code release the Sandbox Image installs, from the packaged manifest."""
    manifest = package_data_dir() / "docker" / "agent-clis-package.json"
    document = require_dict(json.loads(manifest.read_text(encoding="utf-8")))
    return require_str(require_dict(document.get("t3code"), field="t3code"), "version")


def inspect_install(command: list[str], remedy: Callable[[], str]) -> DiagnosticReport:
    """Run *command* (``t3 --version``, however it is reached) and compare it with the pin."""
    findings = Findings()
    version = pinned_version()
    try:
        result = platform_paths.run_captured_bounded(command, timeout=_VERSION_TIMEOUT_S)
        reported = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        reported = ""
    if reported == f"t3 v{version}":
        findings.pass_(f"T3 Code {version} installed (the version the manifest pins)")
    else:
        findings.fail(
            f"T3 Code {version} is not installed: t3 --version reported {reported or 'nothing'}",
            f"recreate the Sandbox: {remedy()}",
        )
    return findings.report()


def inspect_ui(project_root: Path) -> DiagnosticReport:
    """On the host: whether T3 Code answers in the running Sandbox and its relay runs."""
    findings = Findings()
    try:
        state = session_runtime.ui_state(project_root)
    except session_runtime.SessionError as exc:
        findings.skip(f"T3 Code server and relay checks could not run: {exc}")
        return findings.report()
    if state is None:
        _not_applicable(findings, "the Sandbox is not running")
    elif state.vscode:
        _not_applicable(findings, "VS Code owns the Sandbox, which has no T3 Code relay")
    elif state.t3 == "off" and state.relay is None and state.relay_error is None:
        # No relay is recorded: `session up` started no UI that is still meant to run.
        _not_applicable(findings, "the UI is off (booley session up --no-ui)")
    else:
        _judge_server(findings, state.t3)
        _judge_relay(findings, state)
    return findings.report()


def inspect_ui_in_sandbox() -> DiagnosticReport:
    """Inside the Sandbox: whether T3 Code answers. The relay and its record are on the host."""
    findings = Findings()
    if incontainer_setup.t3_serving():
        findings.pass_(f"T3 Code answers on {_SERVER}")
    else:
        findings.skip(
            "T3 Code does not answer in the Sandbox; from inside, Doctor cannot tell a "
            "--no-ui start from a stopped server - run booley doctor on the host"
        )
    findings.skip("T3 Code relay check skipped - it runs from the host")
    return findings.report()


def _not_applicable(findings: Findings, reason: str) -> None:
    for check in ("server", "relay"):
        findings.skip(f"T3 Code {check} check not applicable - {reason}")


def _judge_server(findings: Findings, t3: str) -> None:
    if t3 == "running":
        findings.pass_(f"T3 Code answers on {_SERVER}")
    elif t3 == "off":
        findings.fail(
            f"T3 Code does not answer on {_SERVER}",
            "run `booley session up`, which starts T3 Code",
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
