"""Shared foundation for the ``booley init`` step modules.

Holds the console output helpers, the per-step result record, the mutable
:class:`InitContext` threaded through every init step, and the single
clobber-guarded file writer (:func:`guarded_write`) every scaffolding step
uses. Extracted from ``init_cmd.py`` so the sibling step modules
(``init_docker_image``, ``init_git_hooks``, ``init_skills``) can build on this
foundation without importing back from the coordinator, which would be a
circular import.

:func:`guarded_write` and :class:`WriteOutcome` live in
:mod:`booley.runtime.guarded_write` so callers outside the Harness can use
them; they are re-exported here so existing init-step imports keep working.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from booley.harness.colors import accent, bold_chrome, chrome, green, red, yellow
from booley.runtime.guarded_write import WriteOutcome, guarded_write

# Public surface of this module (defined here + re-exported above). Listing the
# re-exports keeps them from tripping the unused-import (F401) linter.
__all__ = [
    "InitContext",
    "StepResult",
    "WriteOutcome",
    "banner",
    "configure_progress_output",
    "err",
    "guarded_write",
    "info",
    "note",
    "ok",
    "skip",
    "warn",
]

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def configure_progress_output() -> None:
    """Make newline-delimited host progress visible through redirected stdout."""
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {green('[OK]')} {msg}")


def skip(msg: str) -> None:
    print(f"  {accent('[--]')} {msg}")


def note(msg: str) -> None:
    """Print an advisory that needs no action -- weaker than :func:`warn`."""
    print(f"  {chrome('[ii]')} {msg}")


def warn(msg: str) -> None:
    print(f"  {yellow('[!!]')} {msg}")


def err(msg: str) -> None:
    print(f"  {red('[XX]')} {msg}")


def banner(msg: str) -> None:
    print()
    print(bold_chrome(f"=== {msg} ==="))


# ---------------------------------------------------------------------------
# Step result tracking
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    name: str
    status: str  # "ok", "skip", "warn", "err"
    detail: str = ""


@dataclass
class InitContext:
    """Mutable state shared across init steps."""

    results: list[StepResult] = field(default_factory=list)
    project_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    check_only: bool = False
    force: bool = False
    verbose: bool = False
    fix_line_endings: bool = False
    interactive: bool = field(default_factory=sys.stdin.isatty)
    show_step_banners: bool = True
    #: Display number of the last step banner printed by :meth:`step_banner`.
    #: A step's *identity* is its ``record`` key, never this number.
    _step_no: int = 0

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(StepResult(name, status, detail))

    def step_banner(self, title: str) -> None:
        """Print the next step banner, numbered contiguously from 1.

        The number is allocated here, at print time, from the steps that
        actually run — it is not baked into the call site. Hardcoded literals
        left permanent holes as steps were retired (the sequence read
        1, 2, 3, 5, 8, 9, 9b, 10, 10b … 12), and a first-time reader has no way
        to tell a retired number apart from a step that silently failed or was
        suppressed (F-2). A conditional step (``--scaffold``) or a
        single-step run (``--seed``) therefore renumbers rather than skips.
        """
        if not self.show_step_banners:
            return
        self._step_no += 1
        banner(f"Step {self._step_no} — {title}")
