"""Record and prune per-checkout pytest basetemps in the shared tools cache.

Agent Readiness pins POSIX broad runs to ``<tools root>/pytest-basetemp-<hash>``
(see ``agent_readiness.pytest_basetemp``). The folder lives outside the checkout,
so removing a worktree leaves it behind and the hash cannot be mapped back to
its owner. Each session therefore records its resolved checkout in a sibling
``<basetemp>.checkout`` marker (pytest wipes only the basetemp itself, so the
sibling survives) and deletes marked folders whose checkout no longer exists.

The readiness check stays read-only; ``tests/conftest.py`` calls
``record_and_prune`` once from the pytest controller.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from booley.dev_support import agent_readiness

_PREFIX = "pytest-basetemp-"
_MARKER_SUFFIX = ".checkout"
# Entries a pytest basetemp never holds at its top level; their presence means
# the folder is something else (a virtualenv, a repository) and is never pruned.
_FOREIGN_ENTRIES = ("pyvenv.cfg", ".git", "bin")

_LOG = logging.getLogger(__name__)


def record_and_prune(basetemp: Path | str | None, checkout: Path) -> tuple[Path, ...]:
    """Record ``checkout`` as the owner of ``basetemp`` and prune orphaned siblings.

    A no-op on Windows (pytest's default basetemp is used there) and unless
    ``basetemp`` is a ``pytest-basetemp-*`` folder directly in the tools root.
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
    # Prune before recording: on a disk out of inodes the deletions are what
    # make room for the marker. This run's own folder is never pruned: a marker
    # left for it can only name this checkout's path, which exists.
    pruned = tuple(
        folder
        for folder in map(_folder_for, _markers(tools_root))
        if folder != own and _prune(folder)
    )
    _write_marker(_marker_for(own), checkout.resolve())
    return pruned


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
    except OSError as error:
        # Never fail the test session over cache cleanup; the marker remains.
        _LOG.warning("could not prune orphaned pytest basetemp %s: %s", folder, error)
        return False
    return removed
