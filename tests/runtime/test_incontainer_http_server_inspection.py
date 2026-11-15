"""Doctor's Sandbox-side check of the HTTP MCP server (issue #4227)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.runtime import incontainer_setup as reg


def _stub_probe(monkeypatch: pytest.MonkeyPatch, serving: bool) -> list[int]:
    asked: list[int] = []

    def probe(port: int, *, timeout: float = 0.5) -> bool:
        asked.append(port)
        return serving

    monkeypatch.setattr(reg, "_port_is_serving", probe)
    return asked


def test_serving_port_passes_with_restart_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", str(port))
        report = reg.inspect_http_server(
            reg.HttpServerInspectionRequest(("claude",), tmp_path / "absent.log")
        )

    assert [finding.severity for finding in report.findings] == [Severity.PASS]
    message = report.findings[0].message
    assert f"127.0.0.1:{port}" in message
    assert "booley" in message
    assert "start it again" in message
    assert report.details == ()


def test_not_serving_fails_with_log_tail_and_remedy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", "48123")
    asked = _stub_probe(monkeypatch, serving=False)
    log = tmp_path / "server.log"
    lines = [f"line {number}" for number in range(1, 13)]
    log.write_text("\n\n".join(lines) + "\n\n", encoding="utf-8", newline="\n")

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest(("claude",), log))

    assert asked == [48123]
    assert [finding.severity for finding in report.findings] == [Severity.FAIL]
    finding = report.findings[0]
    assert "127.0.0.1:48123" in finding.message
    assert "booley init --seed" in finding.fix
    assert "rebuild or reopen" in finding.fix
    assert [detail.message for detail in report.details] == [
        f"last 8 line(s) of {log}:",
        *(f"    line {number}" for number in range(5, 13)),
    ]
    assert all(detail.verbose_only is False for detail in report.details)


@pytest.mark.parametrize("kind", ["absent", "unreadable"])
def test_not_serving_reports_unusable_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    _stub_probe(monkeypatch, serving=False)
    log = tmp_path / "server.log"
    if kind == "unreadable":
        log.mkdir()

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest(("codex",), log))

    assert [finding.severity for finding in report.findings] == [Severity.FAIL]
    assert "booley init --seed" in report.findings[0].fix
    assert len(report.details) == 1
    assert f"is {kind}" in report.details[0].message
    assert report.details[0].verbose_only is False


def test_not_serving_reports_empty_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_probe(monkeypatch, serving=False)
    log = tmp_path / "server.log"
    log.write_bytes(b"\n  \n")

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest(("codex",), log))

    assert [detail.message for detail in report.details] == [f"server log {log} is empty"]


def test_no_agent_cli_skips_without_probing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked = _stub_probe(monkeypatch, serving=False)

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest((), tmp_path / "x.log"))

    assert [finding.severity for finding in report.findings] == [Severity.SKIP]
    assert "allows no Agent CLI" in report.findings[0].message
    assert asked == []
