"""Assertion adapters for migrated diagnostic contract tests.

They observe public result values; no production diagnostic implementation is
recreated here. Existing message assertions remain useful during the migration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley import __version__
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.eda.config import parse_eda_config
from booley.harness.doctor_waivers import warning
from booley.runtime import inspection


def _record_report(report, *, passed=None, warned=None, skipped=None, failed=None, noted=None):
    sinks = {"pass": passed, "warn": warned, "skip": skipped, "fail": failed, "note": noted}
    for finding in report.findings:
        sink = sinks[finding.severity]
        if sink is None:
            continue
        message = finding.message
        if finding.severity == "warn":
            message = warning(
                finding.check_id, message, subject=finding.subject, dedupe=finding.dedupe
            )
        if finding.fix:
            sink(message, finding.fix)
        else:
            sink(message)


def _record_audit(audit, **sinks):
    findings = tuple(
        DiagnosticFinding(Severity(f.severity), f.message, f.fix, f.check_id, f.subject)
        for f in audit.findings
    )
    _record_report(DiagnosticReport(findings), **sinks)
    return audit.is_valid


def _record_environment(finding, **sinks):
    _record_report(
        DiagnosticReport(
            (
                DiagnosticFinding(
                    Severity(finding.severity), finding.message, finding.fix, finding.check_id
                ),
            )
        ),
        **sinks,
    )


def _record_container(audit, **sinks):
    _record_environment(audit.finding, **sinks)
    return audit.executable


def _inspection_request(project, docker_exe):
    return inspection.RuntimeInspectionRequest(
        project.project_root,
        "booley-sandbox",
        docker_exe,
        eda=parse_eda_config(project.booley_toml.get("eda")),
        fpga_enabled=project.booley_toml.get("flows", {}).get("fpga", {}).get("enabled", True),
    )


class _Rec:
    """Collects doctor check outcomes as (level, message) tuples.

    The signatures mirror ``_Reporter``'s exactly — ``warn_``/``fail_`` take an
    optional fix hint. A stub that accepted fewer args than the real reporter is
    what let the ``warn_(msg, fix)`` TypeError reach a user's `doctor` run: the
    checks that pass fix hints to ``_warn`` had no host-side coverage, so the
    crash only surfaced on a real cocotb project.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.fix_hints: list[str] = []

    def p(self, m: str) -> None:
        self.events.append(("pass", m))

    def w(self, m: str, fix: str = "") -> None:
        self.events.append(("warn", m))

    def n(self, m: str) -> None:
        self.events.append(("note", m))

    def s(self, m: str) -> None:
        self.events.append(("skip", m))

    def f(self, m: str, fix: str = "") -> None:
        self.events.append(("fail", m))
        if fix:
            self.fix_hints.append(fix)

    def fails(self) -> list[str]:
        return [m for lvl, m in self.events if lvl == "fail"]

    def kinds(self) -> set[str]:
        return {lvl for lvl, _ in self.events}


def _runtime_probe_subprocess(other_stdout: str):
    def run(argv, **_kwargs):
        stdout = (
            f"{__version__}\n"
            if "import booley; print(booley.__version__)" in argv
            else other_stdout
        )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def _issued_runtime_state(tmp_path: Path):
    from booley.runtime import session_issuance as runtime_spec

    image = "sha256:" + "a" * 64
    issuance = runtime_spec.Issuance(
        version=runtime_spec.STAMP_VERSION,
        project_root=str(tmp_path),
        spec_sha256="b" * 64,
        image=image,
        image_id=image,
        keeper_image=runtime_spec.keeper_image(tmp_path),
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="d" * 64,
        file_sha256="c" * 64,
    )
    spec = {
        "image": image,
        "remoteUser": "agent",
        "workspaceFolder": "/work",
        "workspaceMount": "source=${localWorkspaceFolder},target=/work,type=bind",
        "mounts": [],
        "containerEnv": {},
        "remoteEnv": {},
        "runArgs": [
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "4096",
            "--network",
            "booley-egress",
        ],
    }
    labels = dict(item.split("=", 1) for item in runtime_spec.labels(issuance))
    state = {
        "Image": image,
        "Config": {
            "Image": image,
            "User": "agent",
            "WorkingDir": "/work",
            "Env": [],
            "Labels": labels,
        },
        "HostConfig": {
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "Privileged": False,
            "PidMode": "",
            "IpcMode": "private",
            "UsernsMode": "",
            "Devices": [],
            "DeviceRequests": None,
            "PortBindings": {},
            "PublishAllPorts": False,
            "PidsLimit": 4096,
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 0,
        },
        "NetworkSettings": {"Networks": {"booley-egress": {}}},
        "Mounts": [
            {
                "Destination": "/work",
                "Source": str(tmp_path),
                "Type": "bind",
                "RW": True,
            }
        ],
    }
    return issuance, spec, labels, state


def _isolate_runtime_files(monkeypatch):
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", "/booley-project")
    is_dir = Path.is_dir
    exists = Path.exists
    hidden = {
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/root/.ssh",
        "/home/agent/.ssh",
        "/root/.config/booley/eda",
        "/home/agent/.config/booley/eda",
    }
    monkeypatch.setattr(
        Path, "is_dir", lambda p: True if str(p) == "/booley-project" else is_dir(p)
    )
    monkeypatch.setattr(Path, "exists", lambda p: False if str(p) in hidden else exists(p))
