"""The MCP server inspection owner grades the in-Sandbox probe without Doctor.

Every assertion reads the returned ``DiagnosticReport``: severity, message, fix,
check ID and the details with their verbosity. The owner module is imported inside
a helper so this file collects, and each test fails on its own, before the owner
exists. ``subprocess.run`` is replaced on the owner module's own binding; no
container is ever started.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.runtime.booley_dir import reset_cache

_ALL_TOOLS = ["bwave", "lint", "mutation", "reviewer", "sim", "synth"]
_ADVISORY_ID = "interactive.mcp-tool-advisory-missing"


def _owner() -> Any:
    """The MCP server inspection owner module, imported where used."""
    return importlib.import_module("booley.mcp.server_inspection")


def _payload(tools: list[str] | None = None, **overrides: object) -> dict[str, object]:
    """A healthy probe payload, with *overrides* applied."""
    payload: dict[str, object] = {
        "tools": _ALL_TOOLS if tools is None else tools,
        "errors": [],
        "logs_dir_ok": True,
    }
    payload.update(overrides)
    return payload


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["docker"], returncode, stdout=stdout, stderr=stderr)


class _Probe:
    """Runs the owner against one scripted ``subprocess.run`` answer."""

    def __init__(self, monkeypatch, tmp_path: Path, answer: object) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.root = tmp_path / "rtl"
        self.root.mkdir(parents=True, exist_ok=True)
        self._answer = answer
        monkeypatch.setattr(_owner().subprocess, "run", self._run)

    def _run(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    def inspect(
        self,
        booley_toml: dict[str, Any] | None = None,
        *,
        fpga_covered: bool = False,
        booley_dir: Path | None = None,
    ) -> DiagnosticReport:
        owner = _owner()
        request = owner.McpServerInspectionRequest(
            project_root=self.root,
            booley_dir=booley_dir or self.root / ".booley",
            docker_exe="docker",
            image="booley:test",
            booley_toml=booley_toml or {},
            fpga_covered=fpga_covered,
        )
        return owner.inspect_mcp_server(request)


def _summary(report: DiagnosticReport) -> list[tuple[Severity, str]]:
    return [(finding.severity, finding.message) for finding in report.findings]


def test_probe_start_failure_fails(monkeypatch, tmp_path):
    failures = (OSError("no docker"), subprocess.TimeoutExpired(["docker"], 60))
    for failure in failures:
        report = _Probe(monkeypatch, tmp_path, failure).inspect()

        assert [f.severity for f in report.findings] == [Severity.FAIL]
        assert report.findings[0].message == f"MCP server probe failed to start: {failure}"
        assert report.findings[0].fix == "rebuild sandbox image"
        assert report.details == ()


def test_probe_nonzero_exit_fails_with_always_shown_excerpt(monkeypatch, tmp_path):
    probe = _Probe(monkeypatch, tmp_path, _completed(returncode=3, stderr="boom: it failed\n"))

    report = probe.inspect()

    assert _summary(report) == [(Severity.FAIL, "MCP server probe failed with exit 3")]
    assert report.findings[0].fix == "rebuild sandbox image"
    assert [(d.message, d.verbose_only, d.after_finding) for d in report.details] == [
        ("    boom: it failed", False, 1)
    ]


def test_probe_invalid_json_fails_with_always_shown_excerpt(monkeypatch, tmp_path):
    for stdout in ("not json at all\n", "[1, 2]\n", "\n  \n"):
        report = _Probe(monkeypatch, tmp_path, _completed(stdout)).inspect()

        assert _summary(report) == [(Severity.FAIL, "MCP server probe did not return valid JSON")]
        assert report.findings[0].fix == "rebuild sandbox image"
        assert all(not d.verbose_only for d in report.details), stdout
    shown = _Probe(monkeypatch, tmp_path, _completed("not json at all\n")).inspect()
    assert [d.message for d in shown.details] == ["    not json at all"]


def test_malformed_payload_fails(monkeypatch, tmp_path):
    for payload in ({"errors": []}, {"tools": [1], "errors": []}, {"tools": [], "errors": "x"}):
        report = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload))).inspect()

        assert _summary(report) == [(Severity.FAIL, "MCP server probe returned malformed payload")]
        assert report.findings[0].fix == "rebuild sandbox image"


def test_discovery_errors_fail_with_first_error_as_fix(monkeypatch, tmp_path):
    payload = _payload(errors=["first problem", "second problem"])

    report = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload))).inspect()

    assert _summary(report) == [(Severity.FAIL, "MCP tool discovery reported 2 error(s)")]
    assert report.findings[0].fix == "first problem"


def test_log_setup_failure_fails(monkeypatch, tmp_path):
    for payload in (_payload(logs_dir_ok=False), {"tools": _ALL_TOOLS, "errors": []}):
        report = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload))).inspect()

        assert _summary(report) == [(Severity.FAIL, "MCP interactive log setup failed")]
        assert report.findings[0].fix == "check .booley mount and permissions"


def test_missing_required_endpoint_fails(monkeypatch, tmp_path):
    payload = _payload(["bwave", "lint", "sim"])

    report = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload))).inspect()

    assert _summary(report) == [(Severity.FAIL, "MCP missing required endpoint(s): synth")]
    assert report.findings[0].fix == "fix [flows.<name>].enabled or reinstall Booley"
    # A disabled Flow is not required; B-Wave always is.
    toml = {"flows": {"synth": {"enabled": False}}}
    disabled = _Probe(monkeypatch, tmp_path, _completed(json.dumps(_payload(["lint", "sim"]))))
    assert _summary(disabled.inspect(toml)) == [
        (Severity.FAIL, "MCP missing required endpoint(s): bwave")
    ]


def test_missing_advisory_endpoint_warns_with_unchanged_check_id(monkeypatch, tmp_path):
    payload = _payload(["bwave", "lint", "sim", "synth", "reviewer"])

    report = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload))).inspect()

    assert _summary(report) == [
        (Severity.PASS, "MCP server exposes 5 MCP tool(s)"),
        (Severity.WARN, "MCP missing documented interactive endpoint(s): mutation"),
    ]
    warning = report.findings[1]
    assert (warning.check_id, warning.fix) == (_ADVISORY_ID, "")
    assert report.health.exit_code == 0


def test_fpga_endpoint_required_only_when_covered_and_enabled(monkeypatch, tmp_path):
    payload = _payload()
    probe = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload)))
    expected = [(Severity.FAIL, "MCP missing required endpoint(s): fpga")]

    assert _summary(probe.inspect(fpga_covered=True)) == expected
    assert _summary(probe.inspect(fpga_covered=False))[0][0] is Severity.PASS
    off = {"flows": {"fpga": {"enabled": False}}}
    assert _summary(probe.inspect(off, fpga_covered=True))[0][0] is Severity.PASS
    covered = _Probe(
        monkeypatch, tmp_path, _completed(json.dumps(_payload([*_ALL_TOOLS, "fpga"])))
    )
    assert _summary(covered.inspect(fpga_covered=True))[0][0] is Severity.PASS


def test_explicit_opt_out_drops_advisory_endpoint(monkeypatch, tmp_path):
    payload = _payload(["bwave", "lint", "sim", "synth"])
    probe = _Probe(monkeypatch, tmp_path, _completed(json.dumps(payload)))

    both = probe.inspect()
    reviewer_off = probe.inspect({"specialists": {"reviewer": {"enabled": False}}})
    mutation_off = probe.inspect({"flows": {"mutation": {"enabled": False}}})
    both_off = probe.inspect(
        {
            "specialists": {"reviewer": {"enabled": False}},
            "flows": {"mutation": {"enabled": False}},
        }
    )
    odd_table = probe.inspect({"specialists": "not a table"})

    assert both.findings[1].message.endswith("mutation, reviewer")
    assert reviewer_off.findings[1].message.endswith("endpoint(s): mutation")
    assert mutation_off.findings[1].message.endswith("endpoint(s): reviewer")
    assert [f.severity for f in both_off.findings] == [Severity.PASS]
    assert odd_table.findings[1].message.endswith("mutation, reviewer")


def test_healthy_probe_passes_and_lists_tools_verbose_only(monkeypatch, tmp_path):
    stdout = "starting up\n" + json.dumps(_payload()) + "\n\n  \n"
    probe = _Probe(monkeypatch, tmp_path, _completed(stdout))

    report = probe.inspect()

    assert _summary(report) == [(Severity.PASS, "MCP server exposes 6 MCP tool(s)")]
    assert report.health.clean
    assert [(d.message, d.verbose_only, d.after_finding) for d in report.details] == [
        ("    bwave, lint, mutation, reviewer, sim, synth", True, 1)
    ]


def test_probe_command_mounts_project_and_relocated_booley_dir(monkeypatch, tmp_path):
    root = tmp_path / "rtl"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    reset_cache()
    probe = _Probe(monkeypatch, tmp_path, _completed(json.dumps(_payload())))
    host_booley_dir = root / "tools" / "booley"

    probe.inspect(booley_dir=host_booley_dir)

    (cmd, kwargs), *rest = probe.calls
    assert rest == []
    assert cmd[:5] == ["docker", "run", "--init", "--rm", "-v"]
    assert cmd[5].endswith(":/work")
    assert cmd[6:8] == ["-w", "/work"]
    assert "BOOLEY_DIR=/work/tools/booley" in cmd
    assert "BOOLEY_MCP_MODE=interactive" in cmd
    assert cmd[-4:] == ["booley:test", "python", "-m", "booley.mcp.probe"]
    assert "-c" not in cmd
    assert kwargs["cwd"] == root
    assert kwargs["timeout"] == 60
    assert kwargs["env"]["BOOLEY_DIR"] == str(host_booley_dir)
    assert kwargs["env"]["BOOLEY_MCP_MODE"] == "interactive"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True

    default = _Probe(monkeypatch, tmp_path / "plain", _completed(json.dumps(_payload())))
    reset_cache()
    default.inspect()
    assert "BOOLEY_DIR=/work/.booley" in default.calls[0][0]
