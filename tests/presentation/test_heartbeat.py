"""Tests for dependency-light standalone heartbeat presentation."""

from __future__ import annotations

import ast
from pathlib import Path

from booley.harness.colors import dim
from booley.presentation.heartbeat import format_heartbeat, render_heartbeat


def test_formatter_defines_shared_heartbeat_text() -> None:
    assert format_heartbeat("simulation", "2.0s", "working") == (
        "  * [simulation] elapsed: 2.0s | working"
    )
    assert format_heartbeat("simulation", "2.0s") == "  * [simulation] elapsed: 2.0s"


def test_standalone_renderer_stays_plain_when_colors_are_disabled(monkeypatch, capsys) -> None:
    # NO_COLOR and TERM=dumb are read once, by booley.harness.colors at import
    # (pinned in tests/harness/test_colors.py); the renderer only consults the result.
    monkeypatch.setattr("booley.harness.colors.COLORS_ENABLED", False)
    monkeypatch.setattr("booley.presentation.heartbeat.sys.stdout.isatty", lambda: True)

    render_heartbeat("simulation", "2.0s", "working", style=dim)

    assert capsys.readouterr().out == "  * [simulation] elapsed: 2.0s | working\n"


def test_standalone_renderer_dims_tty_output(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("booley.harness.colors.COLORS_ENABLED", True)
    monkeypatch.setattr("booley.presentation.heartbeat.sys.stdout.isatty", lambda: True)

    render_heartbeat("simulation", "2.0s", style=dim)

    assert capsys.readouterr().out == "\033[2m  * [simulation] elapsed: 2.0s\033[0m\n"


def test_standalone_renderer_stays_plain_on_a_pipe(monkeypatch, capsys) -> None:
    monkeypatch.setattr("booley.harness.colors.COLORS_ENABLED", True)
    monkeypatch.setattr("booley.presentation.heartbeat.sys.stdout.isatty", lambda: False)

    render_heartbeat("simulation", "2.0s", style=dim)

    assert capsys.readouterr().out == "  * [simulation] elapsed: 2.0s\n"


def test_every_standalone_heartbeat_caller_passes_a_style() -> None:
    """A bare `render=render_heartbeat` would leave that backend's heartbeat undimmed."""
    src = Path(__file__).resolve().parents[2] / "src" / "booley"
    bare = [
        f"{path.relative_to(src)}:{node.lineno}"
        for path in sorted(src.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.keyword)
        and node.arg == "render"
        and isinstance(node.value, ast.Name)
        and node.value.id == "render_heartbeat"
    ]
    assert bare == []
