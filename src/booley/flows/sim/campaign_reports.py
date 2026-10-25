"""Canonical Simulation invocation paths and atomic JSON publication."""

import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.booley_dir_links import is_booley_dir_entry
from booley.runtime.file_lock import nonblocking_file_lock


def target_report_directory(invocation: Path, selector: str) -> Path:
    """Encode qualified selectors as one portable, collision-free path component."""
    return invocation / "targets" / quote(selector, safe="")


def write_campaign_json(path: Path, document: dict[str, object]) -> None:
    """Publish a complete JSON document, never a partially written report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".coverage-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_compatibility_projection(
    path: Path,
    document: dict[str, object],
    *,
    acceptance_committed: bool,
    publication_checkpoint: Callable[[str], None] | None = None,
) -> None:
    """Atomically publish a ``simulation.json`` acceptance projection.

    Callers publish ``complete=False`` while Campaign evidence is terminal but
    acceptance is pending, then replace it with ``complete=True`` only after
    the Acceptance Journal transaction and mutable state selection are durable.
    """
    projection = dict(document)
    projection["complete"] = acceptance_committed
    checkpoint = publication_checkpoint or (lambda _boundary: None)
    checkpoint("before:compatibility_projection")
    write_campaign_json(path, projection)
    checkpoint("after:compatibility_projection")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def simulation_booley_dir(project_root: Path) -> Path | None:
    """Return the checkout's lexical Booley Directory entry, or ``None`` without one.

    This is the one derivation every Simulation place uses to learn which single
    symlinked component (#2191) it may follow. A checkout with no Booley Directory
    has no entry to follow.
    """
    try:
        return resolve_checkout_booley_dir(project_root)
    except FileNotFoundError:
        return None


def report_link_ancestor(path: Path, booley_dir: Path | None = None) -> Path | None:
    """Return the first of *path* and its parents that is a link other than the entry."""
    for candidate in (path, *path.parents):
        if is_report_link(candidate) and not is_booley_dir_entry(candidate, booley_dir):
            return candidate
    return None


@contextmanager
def campaign_invocation_lock(
    invocation: Path, *, booley_dir: Path | None = None
) -> Iterator[None]:
    """Exclude pruning and execution; OS ownership ends automatically on interruption."""
    lock = invocation.with_name(f".invocation-{invocation.name}.lock")
    if report_link_ancestor(lock, booley_dir) is not None:
        raise ValueError("Invocation lock paths must not contain symlinks")
    with lock.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
        yield


def is_report_link(path: Path) -> bool:
    """Recognize symlinks and Windows junction/reparse paths without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
