"""Canonical Simulation invocation paths and atomic JSON publication."""

import json
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.booley_dir_links import is_booley_dir_entry
from booley.core.booley_layout import (
    DOCTOR_TMP_DIR,
    GOALS_DIR_NAME,
    INTERACTIVE_LOGS_DIR_NAME,
    BooleyLayoutError,
)
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.file_lock import nonblocking_file_lock
from booley.runtime.session_paths import RUNTIME_DIR


def target_report_directory(invocation: Path, selector: str) -> Path:
    """Encode qualified selectors as one portable, collision-free path component."""
    return invocation / "targets" / quote(selector, safe="")


def write_simulation_record_json(path: Path, document: dict[str, object]) -> None:
    """Publish a complete JSON document, never a partially written report."""
    text = json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n"
    atomic_replace_bytes(path, text.encode("utf-8"))


def write_compatibility_projection(
    path: Path,
    document: dict[str, object],
    *,
    acceptance_committed: bool,
    publication_checkpoint: Callable[[str], None] | None = None,
) -> None:
    """Atomically publish a ``simulation.json`` acceptance projection.

    Callers publish ``complete=False`` while Simulation Record evidence is terminal but
    acceptance is pending, then replace it with ``complete=True`` only after
    the Acceptance Journal transaction and mutable state selection are durable.
    """
    projection = dict(document)
    projection["complete"] = acceptance_committed
    checkpoint = publication_checkpoint or (lambda _boundary: None)
    checkpoint("before:compatibility_projection")
    write_simulation_record_json(path, projection)
    checkpoint("after:compatibility_projection")


def simulation_booley_dir(project_root: Path) -> Path | None:
    """Return the checkout's lexical Booley Directory entry, or ``None`` without one.

    This is the derivation a Simulation place that knows its Project Root uses to learn
    which single symlinked component (#2191) it may follow; an invocation path, which has no
    Project Root, goes through ``invocation_booley_dir``. A checkout with no Booley
    Directory, or a layout the resolver refuses, has no entry to follow.
    """
    try:
        return resolve_checkout_booley_dir(project_root)
    except (FileNotFoundError, BooleyLayoutError, SourceCheckoutProjectError):
        return None


# Booley Directory children whose ``<id>/.runtime`` holds Flow reports for agent runs:
# Interactive Mode MCP sessions and Goal Mode Goal Records.
_RUNTIME_OWNER_DIRS = frozenset({INTERACTIVE_LOGS_DIR_NAME, GOALS_DIR_NAME})


def invocation_booley_dir(root: Path) -> Path | None:
    """Return the Booley Directory entry an invocation root lives under, if it is one.

    *root* is ``<reports_root>/sim/<N>``. The
    reports root is ``<entry>/flow-reports``, ``<entry>/.runtime/flow-reports``, deep
    Doctor's ``<entry>/tmp/doctor/flow-reports``, or a per-run runtime root
    ``<entry>/<owner>/<id>/.runtime/flow-reports`` (see :data:`_RUNTIME_OWNER_DIRS`);
    the candidate counts only when it is exactly the configured Booley Directory of the
    checkout containing it (#2191). Custom report roots get ``None`` and stay strict.
    """
    reports_root = root.parent.parent
    if reports_root.name != "flow-reports":
        return None
    candidate = reports_root.parent
    if _ends_with(candidate, DOCTOR_TMP_DIR):
        candidate = candidate.parents[len(DOCTOR_TMP_DIR.parts) - 1]
    elif candidate.name == RUNTIME_DIR:
        candidate = candidate.parent
        # Per-run runtime roots: strip exactly one ``<owner>/<id>`` pair.
        if candidate.parent.name in _RUNTIME_OWNER_DIRS:
            candidate = candidate.parent.parent
    if simulation_booley_dir(candidate.parent) != candidate:
        return None
    return candidate


def _ends_with(path: Path, suffix: Path) -> bool:
    """Whether *path*'s last components are exactly *suffix*'s (lexical only)."""
    return path.parts[-len(suffix.parts) :] == suffix.parts


def report_link_ancestor(path: Path, booley_dir: Path | None = None) -> Path | None:
    """Return the first of *path* and its parents that is a link other than the entry."""
    for candidate in (path, *path.parents):
        if is_report_link(candidate) and not is_booley_dir_entry(candidate, booley_dir):
            return candidate
    return None


@contextmanager
def simulation_record_invocation_lock(
    invocation: Path, *, booley_dir: Path | None = None
) -> Iterator[None]:
    """Exclude a second producer or resume of this invocation; OS ownership ends automatically on interruption."""
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
