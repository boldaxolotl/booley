"""Clobber-guarded file writes with an explicit ownership policy."""

from __future__ import annotations

import os
import shutil
from enum import Enum
from pathlib import Path


class WriteOutcome(Enum):
    """What :func:`guarded_write` did (or, under ``dry_run``, would do)."""

    WRITTEN = "written"  # file created, or booley-owned content refreshed
    UNCHANGED = "unchanged"  # booley-owned file already holds this exact content
    SKIPPED = "skipped"  # create-only: file exists, the user owns it now
    REFUSED = "refused"  # marker missing from existing file — foreign, left untouched
    BACKED_UP = "backed_up"  # foreign file copied aside, then overwritten


def _read_existing_text(target: Path, *, preserve_newlines: bool) -> str:
    """Read managed text with exact line endings when the writer pins them."""
    if not preserve_newlines:
        return target.read_text(encoding="utf-8", errors="replace")
    with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _planned_write_outcome(
    target: Path,
    content: str,
    *,
    owner_marker: str | None,
    backup_suffix: str | None,
    newline: str | None,
) -> WriteOutcome:
    if not target.exists():
        return WriteOutcome.WRITTEN
    if owner_marker is None:
        return WriteOutcome.SKIPPED
    try:
        existing = _read_existing_text(target, preserve_newlines=newline is not None)
    except OSError:
        existing = None
    if existing is not None and owner_marker in existing:
        return WriteOutcome.UNCHANGED if existing == content else WriteOutcome.WRITTEN
    return WriteOutcome.BACKED_UP if backup_suffix is not None else WriteOutcome.REFUSED


def _executable_mode_pending(target: Path, outcome: WriteOutcome, executable: bool) -> bool:
    return bool(
        os.name != "nt"
        and outcome is WriteOutcome.UNCHANGED
        and executable
        and target.stat().st_mode & 0o111 != 0o111
    )


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
    """Reconcile one scaffold file according to its explicit ownership policy.

    Marker-free files become user-owned when created. Marker-bearing files are
    managed and may optionally back up foreign predecessors. ``dry_run`` returns
    the exact prospective outcome, including required executable-mode repair.
    """
    if owner_marker is not None and owner_marker not in content:
        raise ValueError(
            f"guarded_write content for {target.name} lacks its own owner marker "
            f"{owner_marker!r} — the next init run would refuse booley's own file"
        )

    outcome = _planned_write_outcome(
        target,
        content,
        owner_marker=owner_marker,
        backup_suffix=backup_suffix,
        newline=newline,
    )
    if outcome in (WriteOutcome.SKIPPED, WriteOutcome.REFUSED):
        return outcome
    if dry_run:
        return (
            WriteOutcome.WRITTEN
            if _executable_mode_pending(target, outcome, executable)
            else outcome
        )

    if outcome is WriteOutcome.BACKED_UP:
        shutil.copy2(target, target.with_name(target.name + backup_suffix))
    if outcome is not WriteOutcome.UNCHANGED:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline=newline) as f:
            f.write(content)
    if executable:
        target.chmod(target.stat().st_mode | 0o755)
    return outcome
