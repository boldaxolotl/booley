"""Record and prune per-checkout pytest basetemps in the shared tools cache.

Agent Readiness pins POSIX broad runs to ``<tools root>/pytest-basetemp-<hash>``
(see ``agent_readiness.pytest_basetemp``). The folder lives outside the checkout,
so removing a worktree leaves it behind and the hash cannot be mapped back to
its owner. Each session therefore records its resolved checkout in a sibling
``<basetemp>.checkout`` marker (pytest wipes only the basetemp itself, so the
sibling survives) and deletes marked folders whose checkout no longer exists.

The path is fixed per checkout and pytest wipes it at the start of every
session, so a second concurrent session from the same checkout would delete
the first one's ``tmp_path`` trees mid-run. Each session therefore holds an
exclusive lock on a sibling ``<basetemp>.lock`` and refuses to start while
another session holds it.

The readiness check stays read-only; ``tests/conftest.py`` calls
``record_and_prune`` once from the pytest controller.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import IO

from booley.core.file_lock import LockContentionError, acquire_file_lock
from booley.dev_support import agent_readiness

_PREFIX = "pytest-basetemp-"
_MARKER_SUFFIX = ".checkout"
_LOCK_SUFFIX = ".lock"
# Entries a pytest basetemp never holds at its top level; their presence means
# the folder is something else (a virtualenv, a repository) and is never pruned.
_FOREIGN_ENTRIES = ("pyvenv.cfg", ".git", "bin")

_LOG = logging.getLogger(__name__)

# Lock files held open for the life of this process. The OS releases the lock
# when its holder exits, so a crashed session never leaves a stale claim behind.
_HELD_LOCKS: list[IO[bytes]] = []


class BasetempInUseError(RuntimeError):
    """Another live pytest session already owns this basetemp."""


def record_and_prune(basetemp: Path | str | None, checkout: Path) -> tuple[Path, ...]:
    """Claim ``basetemp`` for this session, record ``checkout`` as its owner, and prune.

    A no-op on Windows (pytest's default basetemp is used there) and unless
    ``basetemp`` is a ``pytest-basetemp-*`` folder directly in the tools root.
    Raises :class:`BasetempInUseError` while another session holds the folder.
    Only folders with a marker naming a missing checkout are deleted; deletion
    errors are logged, not raised, and keep the marker so a later run retries.
    Returns the folders deleted.
    """
    if sys.platform == "win32" or basetemp is None:
        return ()
    tools_root = agent_readiness.agent_tools_root().resolve()
    own = Path(basetemp).resolve()
    if own.parent != tools_root or not own.name.startswith(_PREFIX):
        return ()
    # Prune before claiming and recording: on a disk out of inodes the deletions
    # are what make room for the lock and the marker. This run's own folder is
    # never pruned: a marker left for it can only name this checkout's path,
    # which exists.
    pruned = tuple(
        folder
        for folder in map(_folder_for, _markers(tools_root))
        if folder != own and _prune(folder)
    )
    _claim(own)
    _write_marker(_marker_for(own), checkout.resolve())
    return pruned


def _claim(folder: Path) -> None:
    """Hold an exclusive lock on ``folder``'s sibling lock file until this process exits."""
    lock = folder.with_name(folder.name + _LOCK_SUFFIX)
    try:
        handle = lock.open("ab")
    except OSError as error:
        # Like the marker, an unwritable cache never fails the session; it only
        # leaves this run unprotected against a concurrent one.
        _LOG.warning("could not lock pytest basetemp %s: %s", folder, error)
        return
    try:
        acquire_file_lock(handle)
    except LockContentionError:
        handle.close()
        raise BasetempInUseError(
            f"another pytest session is using --basetemp={folder}, and pytest deletes it "
            "at session start; wait for that session or pass a different --basetemp"
        ) from None
    _HELD_LOCKS.append(handle)


def _markers(tools_root: Path) -> list[Path]:
    """Return every basetemp marker in the tools root; none when it cannot be listed."""
    try:
        return sorted(tools_root.glob(f"{_PREFIX}*{_MARKER_SUFFIX}"))
    except OSError as error:
        _LOG.warning("could not list pytest basetemp markers in %s: %s", tools_root, error)
        return []


def _marker_for(folder: Path) -> Path:
    return folder.with_name(folder.name + _MARKER_SUFFIX)


def _folder_for(marker: Path) -> Path:
    return marker.with_name(marker.name.removesuffix(_MARKER_SUFFIX))


def _write_marker(marker: Path, checkout: Path) -> None:
    """Publish the marker atomically so a concurrent pruner never reads half a path.

    A failed write (full disk, read-only cache) is logged, never raised: the
    folder then stays unmarked, which only means it is never pruned.
    """
    staging = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(f"{checkout}\n", encoding="utf-8")
        staging.replace(marker)
    except OSError as error:
        _LOG.warning("could not record pytest basetemp owner %s: %s", marker, error)
        staging.unlink(missing_ok=True)


def _recorded_checkout(marker: Path) -> Path | None:
    """Return the marker's absolute checkout path, or ``None`` when unreadable."""
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    path = Path(recorded)
    return path if recorded and path.is_absolute() else None


def _checkout_removed(checkout: Path) -> bool:
    """Return whether ``checkout`` is definitely gone.

    Only "not found" counts. Any other error (permissions, an unmounted volume)
    leaves the owner unknown, and an unknown owner keeps its folder.
    """
    try:
        checkout.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _prune(folder: Path) -> bool:
    """Delete one orphaned basetemp and its marker; return whether it was deleted."""
    marker = _marker_for(folder)
    owner = _recorded_checkout(marker)
    if owner is None or not _checkout_removed(owner):
        return False
    if folder.is_symlink() or any((folder / entry).exists() for entry in _FOREIGN_ENTRIES):
        return False
    removed = folder.is_dir()
    try:
        if removed:
            shutil.rmtree(folder)
        marker.unlink(missing_ok=True)
        folder.with_name(folder.name + _LOCK_SUFFIX).unlink(missing_ok=True)
    except OSError as error:
        # Never fail the test session over cache cleanup; the marker remains.
        _LOG.warning("could not prune orphaned pytest basetemp %s: %s", folder, error)
        return False
    return removed
