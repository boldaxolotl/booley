"""The Flow runtime-reality owner grades each Target's EDA binary without Doctor.

Every assertion reads the returned ``DiagnosticReport``. The owner module is
imported inside a helper so each test fails on its own while the owner is
missing. ``subprocess.run`` and ``shutil.which`` are replaced on the stdlib
modules the owner calls at run time; no container is ever started.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.runtime import session_runtime
from tests.docker_cli_guard import route_bounded_capture_through_subprocess_run

_BAKE_FIX = "bake verilator into the Sandbox Image and rebuild (booley init --force)"


def _owner() -> Any:
    """The Flow runtime-reality owner module, imported where used."""
    return importlib.import_module("booley.flows.runtime_reality")


def _write_lint_core(root: Path, targets: dict[str, str]) -> None:
    """Write one ``.core`` whose lint Targets map name -> ``flow_options`` tool."""
    body = "".join(
        f"  {name}:\n    flow: lint\n    flow_options: {{tool: {tool}}}\n"
        for name, tool in targets.items()
    )
    (root / "lint.core").write_text(
        "CAPI=2:\nname: acme:ip:lint:1.0\ntargets:\n" + body, encoding="utf-8"
    )


def _request(
    root: Path,
    targets: tuple[str, ...] = ("lint_style",),
    *,
    flow_name: str = "lint",
    available: bool = True,
    inside: bool = False,
    command: Any = None,
) -> Any:
    return _owner().FlowRuntimeInspectionRequest(
        flow_name=flow_name,
        targets=targets,
        project_root=root,
        runtime_available=available,
        inside_sandbox=inside,
        sandbox_command=command if command is not None else (lambda argv: argv),
    )


def _summary(report: DiagnosticReport) -> list[tuple[Severity, str, str]]:
    return [(f.severity, f.message, f.fix) for f in report.findings]


class _HostProbe:
    """Scripts ``subprocess.run`` for the host path and records every call."""

    def __init__(self, monkeypatch, *, returncode: int = 0, error: Exception | None = None):
        self.sandbox_argvs: list[list[str]] = []
        self.run_calls: list[tuple[list[str], dict[str, Any]]] = []
        self._returncode = returncode
        self._error = error
        route_bounded_capture_through_subprocess_run(monkeypatch, _owner())
        monkeypatch.setattr(_owner().subprocess, "run", self._run)

    def command(self, argv: list[str]) -> list[str]:
        self.sandbox_argvs.append(argv)
        if isinstance(self._error, session_runtime.SessionError):
            raise self._error
        return ["sandbox", *argv]

    def _run(self, argv, **kwargs):
        self.run_calls.append((argv, kwargs))
        if self._error is not None and not isinstance(self._error, session_runtime.SessionError):
            raise self._error
        return subprocess.CompletedProcess(argv, self._returncode, "", "")


def test_present_binary_passes(tmp_path, monkeypatch):
    """A binary found by ``command -v`` (host) or ``which`` (inside) is one PASS."""
    _write_lint_core(tmp_path, {"lint_style": "verilator"})
    expected = [(Severity.PASS, "lint: 'verilator' on the Sandbox PATH", "")]

    probe = _HostProbe(monkeypatch, returncode=0)
    report = _owner().inspect_flow_runtime_reality(_request(tmp_path, command=probe.command))
    assert _summary(report) == expected
    assert probe.sandbox_argvs == [["sh", "-c", "command -v verilator"]]
    assert [(argv, kw["timeout"]) for argv, kw in probe.run_calls] == [
        (["sandbox", "sh", "-c", "command -v verilator"], 30)
    ]

    monkeypatch.setattr(_owner().shutil, "which", lambda binary: "/bin/" + binary)
    inside = _owner().inspect_flow_runtime_reality(_request(tmp_path, inside=True))
    assert _summary(inside) == expected


def test_missing_binary_fails(tmp_path, monkeypatch):
    """An absent binary FAILs with the host or in-Sandbox wording and the bake fix."""
    _write_lint_core(tmp_path, {"lint_style": "verilator"})

    probe = _HostProbe(monkeypatch, returncode=1)
    report = _owner().inspect_flow_runtime_reality(_request(tmp_path, command=probe.command))
    assert _summary(report) == [
        (Severity.FAIL, "lint: 'verilator' is not on the issued Sandbox PATH", _BAKE_FIX)
    ]

    monkeypatch.setattr(_owner().shutil, "which", lambda binary: None)
    inside = _owner().inspect_flow_runtime_reality(_request(tmp_path, inside=True))
    assert _summary(inside) == [
        (Severity.FAIL, "lint: 'verilator' is not on this container's PATH", _BAKE_FIX)
    ]


def test_unavailable_runtime_skips(tmp_path, monkeypatch):
    """Without a runtime the probe SKIPs and never builds a Sandbox command."""
    _write_lint_core(tmp_path, {"lint_style": "verilator"})
    probe = _HostProbe(monkeypatch)

    report = _owner().inspect_flow_runtime_reality(
        _request(tmp_path, available=False, command=probe.command)
    )

    assert _summary(report) == [
        (
            Severity.SKIP,
            "lint: 'verilator' on the Sandbox PATH - container runtime unavailable",
            "",
        )
    ]
    assert probe.sandbox_argvs == []
    assert probe.run_calls == []


def test_probe_error_fails(tmp_path, monkeypatch):
    """A Sandbox start failure or a missing runtime executable is a probe-error FAIL."""
    _write_lint_core(tmp_path, {"lint_style": "verilator"})
    fix = "run 'booley init --seed' and retry"
    for error in (
        session_runtime.SessionError("sandbox did not start"),
        FileNotFoundError("docker"),
    ):
        probe = _HostProbe(monkeypatch, error=error)
        report = _owner().inspect_flow_runtime_reality(_request(tmp_path, command=probe.command))
        assert _summary(report) == [
            (
                Severity.FAIL,
                f"lint: 'verilator' on the Sandbox PATH (runtime/probe error: {error})",
                fix,
            )
        ]


def test_binary_selection_follows_target_eda_tool(tmp_path, monkeypatch):
    """Each Target's tool maps to one executable; duplicates and bad Targets drop out."""
    _write_lint_core(
        tmp_path,
        {
            "lint_a": "verilator",
            "lint_b": "verible",
            "lint_c": "veriblelint",
            "lint_d": "verilator",
        },
    )
    (tmp_path / "sim.core").write_text(
        "CAPI=2:\nname: acme:ip:sim:1.0\ntargets:\n"
        "  sim_icarus:\n    flow: sim\n    flow_options: {tool: icarus}\n"
        "  sim_yosys:\n    flow: sim\n    flow_options: {tool: yosys}\n",
        encoding="utf-8",
    )
    probe = _HostProbe(monkeypatch)
    request = _request(
        tmp_path,
        ("lint_a", "lint_b", "lint_c", "lint_d", "missing_target", "sim_icarus", "sim_yosys"),
        command=probe.command,
    )

    _owner().inspect_flow_runtime_reality(request)

    assert [argv[-1] for argv in probe.sandbox_argvs] == [
        "command -v verilator",
        "command -v verible-verilog-lint",
        "command -v iverilog",
        "command -v yosys",
    ]


def test_unbuildable_catalog_probes_nothing(tmp_path, monkeypatch):
    """A Project whose ``.core`` files cannot be read yields an empty report."""
    (tmp_path / "broken.core").write_text("CAPI=2:\nname: [unterminated\n", encoding="utf-8")
    probe = _HostProbe(monkeypatch)

    report = _owner().inspect_flow_runtime_reality(_request(tmp_path, command=probe.command))

    assert report.findings == ()
    assert probe.sandbox_argvs == []
