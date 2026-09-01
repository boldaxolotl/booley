"""Shared foundation for the ``booley init`` step modules.

Holds the console output helpers, the per-step result record, the mutable
:class:`InitContext` threaded through every init step, and the single
clobber-guarded file writer (:func:`guarded_write`) every scaffolding step
uses. Extracted from ``init_cmd.py`` so the sibling step modules
(``init_docker_image``, ``init_git_hooks``, ``init_skills``) can build on this
foundation without importing back from the coordinator, which would be a
circular import.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from booley.harness.colors import accent, bold_chrome, chrome, green, red, yellow

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


# ---------------------------------------------------------------------------
# Clobber-guarded scaffold writes
# ---------------------------------------------------------------------------


class WriteOutcome(Enum):
    """What :func:`guarded_write` did (or, under ``dry_run``, would do)."""

    WRITTEN = "written"  # file created, or booley-owned content refreshed
    UNCHANGED = "unchanged"  # booley-owned file already holds this exact content
    SKIPPED = "skipped"  # create-only: file exists, the user owns it now
    REFUSED = "refused"  # marker missing from existing file — foreign, left untouched
    BACKED_UP = "backed_up"  # foreign file copied aside, then overwritten


def guarded_write(
    target: Path,
    content: str,
    *,
    owner_marker: str | None = None,
    backup_suffix: str | None = None,
    dry_run: bool = False,
    newline: str | None = None,
    executable: bool = False,
) -> WriteOutcome:
    """Write a scaffolded file without ever clobbering user-owned content.

    The one clobber-guard for init's write sites, replacing the historical
    per-site schemes (bare ``.exists()``, ``_SYSTEMD_MARKER``, "already ours"
    hook content sniffs). Ownership policy:

    - ``owner_marker=None`` — *create-only*: the file is user-owned the moment
      it exists; an existing file is never touched (``SKIPPED``). For
      skeletons the user fills in (booley.toml, the .core).
    - ``owner_marker=<str>`` — *managed*: a file containing the marker is
      booley-owned and is refreshed when the content differs; one without it
      is foreign and is left untouched (``REFUSED``) — unless
      ``backup_suffix`` is given, in which case the foreign file is first
      copied to ``<name><backup_suffix>`` and then overwritten
      (``BACKED_UP``). *content* must itself carry the marker, or the next
      run would refuse booley's own file.

    ``dry_run`` computes the outcome without touching the filesystem (the
    ``--check-only`` contract). ``newline="\\n"`` forces LF (shell scripts —
    a CRLF shebang is an ENOENT in the container, QA_REPORT D0a).
    ``executable`` sets +x, also on ``UNCHANGED`` so a stripped bit heals.

    The docker-image files keep their richer scheme (``_GENERATED_HEADER`` +
    ``# booley:keep`` in ``project_image.py``): there an *edited* generated
    file must flip to user-owned, which a containment marker cannot express.
    """
    if owner_marker is not None and owner_marker not in content:
        raise ValueError(
            f"guarded_write content for {target.name} lacks its own owner marker "
            f"{owner_marker!r} — the next init run would refuse booley's own file"
        )

    exists = target.exists()
    if not exists:
        outcome = WriteOutcome.WRITTEN
    elif owner_marker is None:
        return WriteOutcome.SKIPPED
    else:
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = None  # unreadable — treat as foreign
        if existing is not None and owner_marker in existing:
            outcome = WriteOutcome.UNCHANGED if existing == content else WriteOutcome.WRITTEN
        elif backup_suffix is not None:
            outcome = WriteOutcome.BACKED_UP
        else:
            return WriteOutcome.REFUSED

    if dry_run:
        return outcome

    if outcome is WriteOutcome.BACKED_UP:
        shutil.copy2(target, target.with_name(target.name + backup_suffix))
    if outcome is not WriteOutcome.UNCHANGED:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline=newline) as f:
            f.write(content)
    if executable:
        target.chmod(target.stat().st_mode | 0o755)
    return outcome
