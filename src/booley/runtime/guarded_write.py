"""Clobber-guarded file writes with an explicit ownership policy."""

from __future__ import annotations

import os
import stat
from enum import Enum
from pathlib import Path

from booley.runtime.atomic_files import (
    WriteOnceConflictError,
    atomic_replace_bytes,
    atomic_write_once,
)

# Backup names tried per target: ``<name><suffix>``, then ``.1`` .. ``.99``.
_BACKUP_NAME_LIMIT = 100
# Permission bits a rewrite may preserve; drops setuid/setgid/sticky and group/other write.
_PRESERVED_MODE_MASK = 0o755
_NEW_FILE_MODE = 0o644
# Windows refuses to unlink a read-only temp file, so the staged file stays writable there.
_WINDOWS_STAGED_BITS = 0o200
# Open flags: refuse a link at the final component (POSIX) and never translate bytes (Windows).
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class SymlinkTargetError(OSError):
    """A write target's final path component is a symbolic link."""


class WriteOutcome(Enum):
    """What :func:`guarded_write` did (or, under ``dry_run``, would do)."""

    WRITTEN = "written"  # file created, or booley-owned content refreshed
    UNCHANGED = "unchanged"  # booley-owned file already holds this exact content
    SKIPPED = "skipped"  # create-only: file exists, the user owns it now
    REFUSED = "refused"  # foreign marker-less file, or any symlinked target — left untouched
    BACKED_UP = "backed_up"  # foreign file copied to the first free numbered name, then replaced


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
    if target.is_symlink():
        # Never inspect or write through a link: the destination is not ours to judge.
        return WriteOutcome.REFUSED
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


def _executable_mode(mode: int) -> int:
    """Add an execute bit wherever the matching read bit is set; never widen beyond that."""
    return mode | (mode & 0o444) >> 2


def _target_mode(target: Path, executable: bool) -> int:
    """Return the permission bits a write to *target* publishes."""
    try:
        mode = stat.S_IMODE(target.lstat().st_mode) & _PRESERVED_MODE_MASK
    except FileNotFoundError:
        mode = _NEW_FILE_MODE
    return _executable_mode(mode) if executable else mode


def _executable_mode_pending(target: Path, outcome: WriteOutcome, executable: bool) -> bool:
    return bool(
        os.name != "nt"
        and outcome is WriteOutcome.UNCHANGED
        and executable
        and stat.S_IMODE(target.stat().st_mode) != _target_mode(target, executable)
    )


def _encoded(content: str, newline: str | None) -> bytes:
    """Encode *content* as text-mode ``open("w", newline=...)`` would, before any file is touched."""
    if newline is None:
        newline = os.linesep
    if newline not in ("", "\n"):
        content = content.replace("\n", newline)
    return content.encode("utf-8")


def replace_regular_file(target: Path, data: bytes, *, executable: bool = False) -> None:
    """Atomically replace *target* with *data*, refusing a symlinked final component.

    The existing permission bits are kept (``executable`` only adds execute bits
    where read is set); a new file is 0644, or 0755 when executable. On any
    failure the previous bytes remain and no temporary file is left behind.
    """
    if target.is_symlink():
        raise SymlinkTargetError(f"refusing to write through symlink: {target}")
    mode = _target_mode(target, executable)
    if os.name == "nt":
        mode |= _WINDOWS_STAGED_BITS
    atomic_replace_bytes(target, data, mode=mode)


def _read_regular_file(target: Path) -> tuple[bytes, int]:
    """Read bytes and mode through one no-follow descriptor; a swapped-in link fails."""
    descriptor = os.open(target, os.O_RDONLY | _NO_FOLLOW | _BINARY)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SymlinkTargetError(f"refusing to back up a non-regular file: {target}")
        return stream.read(), stat.S_IMODE(info.st_mode)


def _back_up(target: Path, suffix: str) -> None:
    """Copy *target* to the first free ``<name><suffix>[.N]``; never touch an existing name."""
    data, mode = _read_regular_file(target)
    for number in range(_BACKUP_NAME_LIMIT):
        candidate = target.with_name(target.name + suffix + (f".{number}" if number else ""))
        if os.path.lexists(candidate):
            continue
        try:
            atomic_write_once(candidate, data, mode=mode)
        except WriteOnceConflictError:
            continue  # name taken between the check and the link: try the next one
        return
    raise OSError(f"no free backup name for {target} (tried {_BACKUP_NAME_LIMIT})")


def _heal_executable_mode(target: Path) -> None:
    """Fix the execute bits of an unchanged file via a no-follow descriptor (POSIX only)."""
    if os.name == "nt":
        return
    descriptor = os.open(target, os.O_RDONLY | _NO_FOLLOW)
    try:
        wanted = _target_mode(target, True)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != wanted:
            os.fchmod(descriptor, wanted)
    finally:
        os.close(descriptor)


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
    managed and may optionally back up foreign predecessors. A symlinked target
    is always ``REFUSED`` and never read, written, or chmodded. Writes are
    atomic. ``dry_run`` returns the exact prospective outcome, including
    required executable-mode repair.
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
        assert backup_suffix is not None  # BACKED_UP is only planned with a suffix
        _back_up(target, backup_suffix)
    if outcome is not WriteOutcome.UNCHANGED:
        replace_regular_file(target, _encoded(content, newline), executable=executable)
    elif executable:
        _heal_executable_mode(target)
    return outcome
