"""P1 parent/child display-selection agreement tests."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pytest

from booley.harness import __main__ as child
from booley.harness import booley as parent


@dataclass
class _Stdout:
    tty: bool

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


@pytest.mark.parametrize(
    ("case", "tty", "no_console", "environment", "expected"),
    [
        ("SEL-01", True, False, {}, True),
        ("SEL-02", True, True, {}, False),
        ("SEL-03", True, False, {"BOOLEY_CONSOLE": "0"}, False),
        ("SEL-04", True, False, {"NO_COLOR": "1"}, False),
        ("SEL-05", True, False, {"TERM": "dumb"}, False),
        ("SEL-06", False, False, {}, False),
        ("SEL-07", True, True, {}, False),
    ],
)
def test_parent_and_child_make_identical_selection(
    case: str,
    tty: bool,
    no_console: bool,
    environment: dict[str, str],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent chrome and child TUI must never disagree."""
    for name in ("BOOLEY_CONSOLE", "NO_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "stdout", _Stdout(tty))
    args = argparse.Namespace(no_console=no_console)

    parent_choice = parent._will_use_console(args)
    child_choice = child._detect_console(args)

    assert parent_choice == expected, case
    assert child_choice == expected, case
    assert parent_choice == child_choice, case
