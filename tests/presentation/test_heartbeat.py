"""Tests for dependency-light standalone heartbeat presentation."""

from __future__ import annotations

from booley.presentation.heartbeat import format_heartbeat, render_heartbeat


def test_formatter_defines_shared_heartbeat_text() -> None:
    assert format_heartbeat("simulation", "2.0s", "working") == (
        "  * [simulation] elapsed: 2.0s | working"
    )
    assert format_heartbeat("simulation", "2.0s") == "  * [simulation] elapsed: 2.0s"


def test_standalone_renderer_respects_no_color(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    render_heartbeat("simulation", "2.0s", "working")

    assert capsys.readouterr().out == "  * [simulation] elapsed: 2.0s | working\n"


def test_standalone_renderer_dims_tty_output(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("booley.presentation.heartbeat.sys.stdout.isatty", lambda: True)

    render_heartbeat("simulation", "2.0s")

    assert capsys.readouterr().out == "\033[2m  * [simulation] elapsed: 2.0s\033[0m\n"
