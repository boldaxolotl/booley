"""Doctor's Sandbox-side check of the HTTP MCP server (issue #4227)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.sandbox_venue_fake import place

from booley.audit.diagnostic_results import Severity
from booley.runtime import incontainer_setup as reg
from booley.runtime.runtime_context import RuntimeLocation


@pytest.fixture
def closed_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A loopback port that is bound, so nobody else takes it, but never listened on."""
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", str(port))
        yield port


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


@pytest.mark.parametrize("hand_written", [False, True])
def test_not_serving_fails_with_log_tail_and_remedy(
    closed_port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hand_written: bool
) -> None:
    if hand_written:
        place(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)
    log = tmp_path / "server.log"
    lines = [f"line {number}" for number in range(1, 13)]
    log.write_text("\n\n".join(lines) + "\n\n", encoding="utf-8", newline="\n")

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest(("claude",), log))

    assert [finding.severity for finding in report.findings] == [Severity.FAIL]
    finding = report.findings[0]
    assert f"127.0.0.1:{closed_port}" in finding.message
    assert ("incontainer_register" in finding.fix) is hand_written
    assert ("postStartCommand" in finding.fix) is hand_written
    assert ("booley init --seed" in finding.fix) is not hand_written
    assert "rebuild or reopen" in finding.fix
    assert [detail.message for detail in report.details] == [
        f"last 8 line(s) of {log}:",
        *(f"    line {number}" for number in range(5, 13)),
    ]
    assert all(detail.verbose_only is False for detail in report.details)


@pytest.mark.parametrize("kind", ["absent", "unreadable", "empty"])
def test_not_serving_reports_unusable_log(closed_port: int, tmp_path: Path, kind: str) -> None:
    log = tmp_path / "server.log"
    if kind == "unreadable":
        log.mkdir()
    elif kind == "empty":
        log.write_bytes(b"\n  \n")

    report = reg.inspect_http_server(reg.HttpServerInspectionRequest(("codex",), log))

    assert [finding.severity for finding in report.findings] == [Severity.FAIL]
    assert "booley init --seed" in report.findings[0].fix
    assert f"127.0.0.1:{closed_port}" in report.findings[0].message
    (detail,) = report.details
    assert f"server log {log} is {kind}" in detail.message
    assert detail.verbose_only is False


def test_no_agent_cli_skips(tmp_path: Path) -> None:
    report = reg.inspect_http_server(reg.HttpServerInspectionRequest((), tmp_path / "x.log"))

    assert [finding.severity for finding in report.findings] == [Severity.SKIP]
    assert "allows no Agent CLI" in report.findings[0].message
