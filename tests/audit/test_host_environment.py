"""Focused regression tests for typed host-environment audits."""

import ast
import email.utils
import importlib.metadata
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.command_runner_fake import CannedRunner, FixedLookup, refusing_runner

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
    lookup = FixedLookup("/usr/bin/docker")
    audit = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=True,
        which=lookup,
        run=refusing_runner,
    )

    assert audit.executable is None
    assert audit.finding.severity is host_environment.EnvironmentSeverity.SKIP
    assert lookup.names == []


@pytest.mark.parametrize(
    ("found", "runner", "executable", "severity", "message"),
    [
        pytest.param(
            None,
            refusing_runner,
            None,
            host_environment.EnvironmentSeverity.FAIL,
            "container runtime not on PATH",
            id="missing",
        ),
        pytest.param(
            "/usr/bin/docker",
            CannedRunner(),
            "/usr/bin/docker",
            host_environment.EnvironmentSeverity.PASS,
            None,
            id="running",
        ),
        pytest.param(
            "docker",
            CannedRunner(1, "permission denied"),
            None,
            host_environment.EnvironmentSeverity.FAIL,
            "container runtime permission denied",
            id="permission",
        ),
        pytest.param(
            "docker",
            CannedRunner(1, "unavailable"),
            None,
            host_environment.EnvironmentSeverity.FAIL,
            "container runtime not running",
            id="stopped",
        ),
        pytest.param(
            "docker",
            CannedRunner(raises=subprocess.TimeoutExpired("docker", 10)),
            None,
            host_environment.EnvironmentSeverity.FAIL,
            "container runtime probe timed out",
            id="timeout",
        ),
    ],
)
def test_container_runtime_reports_each_host_state(
    found: str | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    executable: str | None,
    severity: host_environment.EnvironmentSeverity,
    message: str | None,
) -> None:
    audit = host_environment.probe_container_runtime(
        "docker",
        inside_session_runtime=False,
        which=FixedLookup(found),
        run=runner,
    )

    assert audit.executable == executable
    assert audit.finding.severity is severity
    if message is not None:
        assert audit.finding.message == message
        assert audit.finding.fix


_REFERENCE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _DateResponse:
    def __init__(self) -> None:
        self.headers = {"Date": email.utils.format_datetime(_REFERENCE, usegmt=True)}

    def __enter__(self) -> "_DateResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.mark.parametrize(
    ("offset_hours", "online", "severity", "words"),
    [
        pytest.param(
            0,
            True,
            host_environment.EnvironmentSeverity.PASS,
            ["agrees with true UTC"],
            id="aligned",
        ),
        pytest.param(
            4,
            True,
            host_environment.EnvironmentSeverity.WARN,
            ["ahead of", "w32tm /resync"],
            id="ahead",
        ),
        pytest.param(-4, True, host_environment.EnvironmentSeverity.WARN, ["behind"], id="behind"),
        pytest.param(
            0,
            False,
            host_environment.EnvironmentSeverity.SKIP,
            ["no reference time reachable"],
            id="offline",
        ),
    ],
)
def test_host_clock_compares_with_an_http_date(
    monkeypatch: pytest.MonkeyPatch,
    offset_hours: int,
    online: bool,
    severity: host_environment.EnvironmentSeverity,
    words: list[str],
) -> None:
    def urlopen(_request: object, timeout: float = 0) -> _DateResponse:
        if not online:
            raise urllib.error.URLError("offline")
        return _DateResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    finding = host_environment.probe_host_clock(
        now=lambda: _REFERENCE + timedelta(hours=offset_hours)
    )

    assert finding.severity is severity
    text = f"{finding.message} {finding.fix or ''}"
    for word in words:
        assert word in text


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
