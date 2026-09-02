"""Focused regression tests for typed host-environment audits."""

import importlib.metadata
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests.architecture.production import assert_no_dependencies

import booley
from booley.audit import host_environment

_ROOT = Path(__file__).resolve().parents[2]


def test_python_version_audit_preserves_pass_and_actionable_failure() -> None:
    healthy = host_environment.audit_python_version((3, 14), (3, 11))
    stale = host_environment.audit_python_version((3, 10), (3, 11))

    assert healthy == host_environment.EnvironmentFinding(
        host_environment.EnvironmentSeverity.PASS,
        "Python 3.14",
    )
    assert stale.severity is host_environment.EnvironmentSeverity.FAIL
    assert stale.fix == "install python3.11+"


def test_container_runtime_skips_inside_session_without_discovery() -> None:
    discovered: list[str] = []
    audit = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=True,
        which=lambda name: discovered.append(name) or "/usr/bin/docker",
    )

    assert audit.executable is None
    assert audit.finding.severity is host_environment.EnvironmentSeverity.SKIP
    assert discovered == []


def test_container_runtime_reports_missing_and_running_states() -> None:
    missing = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=False,
        which=lambda _name: None,
    )
    running = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=False,
        which=lambda _name: "/usr/bin/docker",
        run=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert missing.finding.severity is host_environment.EnvironmentSeverity.FAIL
    assert missing.finding.message == "container runtime not on PATH"
    assert running.executable == "/usr/bin/docker"
    assert running.finding.severity is host_environment.EnvironmentSeverity.PASS


def test_container_runtime_distinguishes_permission_denied() -> None:
    audit = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=False,
        which=lambda _name: "docker",
        run=lambda args, **_kwargs: subprocess.CompletedProcess(args, 1, "", "permission denied"),
    )

    assert audit.finding.message == "container runtime permission denied"
    assert audit.finding.fix


def test_host_environment_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "host_environment.py"
    assert_no_dependencies(
        paths=(module_path,),
        target_prefixes=("booley.harness", "booley.mcp", "booley.specialists"),
    )


def test_doctor_does_not_reimplement_host_probe_mechanisms() -> None:
    source = (_ROOT / "src" / "booley" / "harness" / "doctor.py").read_text(encoding="utf-8")

    assert "urllib.request.urlopen" not in source
    assert 'distribution("booley")' not in source
    assert '[docker_exe, "info"]' not in source


@dataclass
class _Distribution:
    version: str

    @property
    def metadata(self) -> dict[str, str]:
        return {"Version": self.version}


def test_legacy_audit_describes_active_source_checkout_truthfully(monkeypatch) -> None:
    def distribution(name: str):
        if name == "booley":
            return _Distribution("0.0.9")
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    monkeypatch.setattr(booley, "__dist_name__", None)

    finding = host_environment.audit_legacy_distribution()

    assert finding.severity is host_environment.EnvironmentSeverity.FAIL
    assert "active import resolves to the source checkout" in finding.message
    assert "is the one supplying `import booley`" not in finding.message
