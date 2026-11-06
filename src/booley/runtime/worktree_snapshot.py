"""Choose what a Snapshot copies from the Main Booley Directory (ADR 0082).

``worktree_create.sh`` runs this module as ``python -m booley.runtime.worktree_snapshot
<booley-dir> <list-file>``. The Snapshot is an allowlist read from
:data:`booley.core.booley_dir_parts.BOOLEY_DIR_PATHS`: only paths classified as
Checkout state versioned with the RTL are copied. Common state, state tied to
one checkout's sources, and paths no row covers stay in the Main Booley
Directory; the unclassified ones are named so a missing row is noticed.

Stdlib-only (plus the classification module).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from booley.core.booley_dir_parts import BooleyDirClass, iter_booley_dir

SKIPPED_PREFIX = "Snapshot skipped "


def snapshot_entries(source: Path) -> tuple[list[str], list[str]]:
    """Return ``(copied, unclassified)`` paths of the Booley Directory *source*.

    Raises:
        OSError: *source* or a directory below it cannot be listed.
    """
    copied: list[str] = []
    unclassified: list[str] = []
    for path, booley_class in iter_booley_dir(source):
        if booley_class is BooleyDirClass.VERSIONED:
            copied.append(path)
        elif booley_class is None:
            unclassified.append(path)
    return copied, unclassified


def skipped_line(path: str) -> str:
    """Return the line naming an unclassified *path* the Snapshot did not copy."""
    return (
        f"{SKIPPED_PREFIX}{path}: no Booley Directory classification covers it; "
        "it stays in the Main Booley Directory"
    )


def main(argv: list[str]) -> int:
    """Write the NUL-separated ``./``-prefixed copy list and print the skipped paths."""
    source, list_file = Path(argv[0]), Path(argv[1])
    try:
        copied, unclassified = snapshot_entries(source)
        list_file.write_bytes(b"".join(os.fsencode(f"./{path}") + b"\0" for path in copied))
    except OSError as exc:
        print(f"ERROR: cannot list the Booley Directory for the Snapshot: {exc}", file=sys.stderr)
        return 1
    for path in unclassified:
        print(skipped_line(path), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
