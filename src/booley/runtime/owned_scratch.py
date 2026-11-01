"""Scratch directories that carry their owner and never outlive it.

Nested agent homes and read-only Specialist workspaces live in the system
temp directory. A normal exit removes them, but a SIGKILL-ed or cancelled run
cannot clean up after itself. Each such directory therefore records the owning
process in a marker file, and a later start of the MCP server or of a
Specialist agent call removes directories whose owner is gone.

The sweep is conservative: it removes only real directories owned by the
current user, with a known Booley prefix and a valid marker whose owner is
observably dead. Anything else, including live owners, unreadable markers and
unmarked directories, is left alone.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

from booley.core.process_identity import ProcessIdentity

from .filesystem_utils import safe_rmtree
from .pid import DEAD, REUSED, ZOMBIE, capture_process_identity, observe_process

logger = logging.getLogger(__name__)

OWNER_MARKER = ".booley-owner.json"
MARKER_KIND = "booley-owned-scratch"
MARKER_VERSION = 1
# A marker is a few hundred bytes; refuse to parse anything bigger.
MAX_MARKER_BYTES = 4096
OWNED_PREFIXES = (
    "codex-nested-home-",
    "booley-specialist-ro-",
    "booley-coverage-analysis-",
    "booley-coverage-campaign-",
    "booley-developer-probe-",
)
# Owner states that prove the owning process no longer exists. UNKNOWN is
# deliberately absent: an unreadable observation never justifies deletion.
_GONE_STATES = frozenset({DEAD, ZOMBIE, REUSED})


def scratch_root() -> Path:
    """Directory holding Booley scratch (the system temp directory)."""
    return Path(tempfile.gettempdir())


def mark_owned(directory: Path) -> None:
    """Record the current process as the owner of *directory*.

    Without a capturable process identity the directory stays unmarked: it is
    still removed by its owner but is never swept.
    """
    identity = capture_process_identity(os.getpid())
    if identity is None:
        logger.debug("No process identity; %s stays unmarked", directory)
        return
    marker = {"kind": MARKER_KIND, "version": MARKER_VERSION, "owner": identity.to_payload()}
    staging = directory / f"{OWNER_MARKER}.tmp"
    staging.write_text(json.dumps(marker), encoding="utf-8")
    # Publish atomically so a concurrent sweep never reads a partial marker.
    staging.replace(directory / OWNER_MARKER)


def remove_owned(directory: Path) -> None:
    """Remove *directory*, logging instead of raising when that fails."""
    try:
        safe_rmtree(directory, protect_git_root=False)
    except OSError as exc:
        logger.warning("Could not remove scratch %s: %s", directory, exc)


@contextlib.contextmanager
def owned_tempdir(prefix: str) -> Iterator[Path]:
    """Create a private, owner-marked temp directory removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root()))
    try:
        mark_owned(directory)
        yield directory
    finally:
        remove_owned(directory)


def _read_owner(directory: Path) -> ProcessIdentity | None:
    """Return the owner recorded in *directory*'s marker, or ``None`` if invalid."""
    marker = directory / OWNER_MARKER
    status = marker.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_MARKER_BYTES:
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("kind") != MARKER_KIND
        or data.get("version") != MARKER_VERSION
    ):
        return None
    return ProcessIdentity.from_payload(data.get("owner"))


def _is_stale(entry: Path) -> bool:
    """Whether *entry* is a Booley-marked directory of the current user with a gone owner."""
    status = entry.lstat()
    if not stat.S_ISDIR(status.st_mode):
        return False
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        return False
    try:
        owner = _read_owner(entry)
    except FileNotFoundError:
        return False
    return owner is not None and observe_process(owner).state in _GONE_STATES


def sweep_stale_scratch(root: Path | None = None) -> list[Path]:
    """Remove scratch whose owning process is gone; return what was removed.

    Never raises: a failure on one entry is logged and the sweep continues.
    """
    base = root if root is not None else scratch_root()
    removed: list[Path] = []
    try:
        entries = [entry for entry in base.iterdir() if entry.name.startswith(OWNED_PREFIXES)]
    except OSError as exc:
        logger.debug("Cannot list scratch root %s: %s", base, exc)
        return removed
    for entry in entries:
        try:
            if _is_stale(entry):
                safe_rmtree(entry, protect_git_root=False)
                removed.append(entry)
        except OSError as exc:
            logger.warning("Could not reclaim stale scratch %s: %s", entry, exc)
    return removed
