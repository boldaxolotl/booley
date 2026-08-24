"""Focused regression tests for typed host-environment audits."""

import ast
import subprocess
from pathlib import Path

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
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )

    forbidden = ("booley.harness", "booley.mcp", "booley.specialists")
    assert not {
        module for module in imports if any(module.startswith(prefix) for prefix in forbidden)
    }


def test_doctor_does_not_reimplement_host_probe_mechanisms() -> None:
    source = (_ROOT / "src" / "booley" / "harness" / "doctor.py").read_text(encoding="utf-8")

    assert "urllib.request.urlopen" not in source
    assert 'distribution("booley")' not in source
    assert '[docker_exe, "info"]' not in source
