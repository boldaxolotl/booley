"""Tests for dependency-light standalone heartbeat presentation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from booley.presentation.colors import dim
from booley.presentation.heartbeat import render_heartbeat


@pytest.mark.parametrize(
    ("colors_enabled", "tty", "extra", "expected"),
    [
        (False, True, "working", "  * [simulation] elapsed: 2.0s | working\n"),
        (True, True, "", "\033[2m  * [simulation] elapsed: 2.0s\033[0m\n"),
        (True, False, "", "  * [simulation] elapsed: 2.0s\n"),
    ],
    ids=["colors-disabled", "tty", "pipe"],
)
def test_standalone_renderer_dims_only_a_colored_tty(
    monkeypatch, capsys, colors_enabled, tty, extra, expected
) -> None:
    # NO_COLOR and TERM=dumb are read once, by booley.presentation.colors at import
    # (tested in tests/presentation/test_colors.py); the renderer only consults the result.
    monkeypatch.setattr("booley.presentation.colors.COLORS_ENABLED", colors_enabled)
    monkeypatch.setattr("booley.presentation.heartbeat.sys.stdout.isatty", lambda: tty)

    render_heartbeat("simulation", "2.0s", extra, style=dim)

    assert capsys.readouterr().out == expected


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
