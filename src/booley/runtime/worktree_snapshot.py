"""Choose what a Snapshot copies from the Main Booley Directory (ADR 0082).

``worktree_create.sh`` runs this module as ``python -m booley.runtime.worktree_snapshot
<booley-dir> <list-file>``. The Snapshot is an allowlist read from
:data:`booley.core.booley_dir_parts.BOOLEY_DIR_PATHS`: only paths classified as
Checkout state versioned with the RTL are copied. Common state, state tied to
one checkout's sources, and paths no row covers stay in the Main Booley
Directory. An unclassified path is named so a missing row is noticed, unless Git
tracks or ignores every file below it. The ``[coverage.waivers]`` directory
anchored to the project data repository is copied too.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from booley.config.coverage_waiver_inputs import parse_coverage_waiver_config
from booley.core.booley_dir_parts import (
    BOOLEY_DIR_PATHS,
    BooleyDirClass,
    iter_booley_dir,
    with_versioned_directory,
)
from booley.core.config_paths import resolve_toml
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.platform_paths import run_captured_bounded

SKIPPED_PREFIX = "Snapshot skipped "


_GIT_TIMEOUT_SECONDS = 30


def _waiver_directory(source: Path) -> str | None:
    """Return the project-data waiver directory configured in *source*, if any."""
    try:
        config = tomllib.loads(resolve_toml(source).read_text(encoding="utf-8"))
        waivers = parse_coverage_waiver_config(config)
    except (OSError, ValueError):
        # A missing or invalid table is the coverage run's finding to report;
        # worktree creation must not fail on it.
        return None
    if waivers is None or waivers.anchor != "project_data_repository":
        return None
    return waivers.directory


def _left_behind(source: Path, unclassified: list[str]) -> list[str]:
    """Return the *unclassified* paths with a file Git neither tracks nor ignores.

    Every path comes back when Git cannot answer.
    """
    if not unclassified:
        return []
    argv = ["git", "--literal-pathspecs", "ls-files", "-z", "--others", "--exclude-standard"]
    try:
        result = run_captured_bounded(
            [*argv, "--", *unclassified],
            timeout=_GIT_TIMEOUT_SECONDS,
            cwd=source,
            env=inherited_git_environment(),
            text=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unclassified
    if result.returncode != 0:
        return unclassified
    # A nested repository is listed as ``dir/``.
    listed = [os.fsdecode(name).rstrip("/") for name in result.stdout.split(b"\0") if name]
    return [
        path
        for path in unclassified
        if any(name == path or name.startswith(f"{path}/") for name in listed)
    ]


def snapshot_entries(source: Path) -> tuple[list[str], list[str]]:
    """Return ``(copied, named)`` paths of the Booley Directory *source*.

    *named* holds the unclassified paths Git neither tracks nor ignores.

    Raises:
        OSError: *source* or a directory below it cannot be listed.
    """
    directory = _waiver_directory(source)
    rows = BOOLEY_DIR_PATHS if directory is None else with_versioned_directory(directory)
    copied: list[str] = []
    unclassified: list[str] = []
    for path, booley_class in iter_booley_dir(source, rows=rows):
        if booley_class is BooleyDirClass.VERSIONED:
            copied.append(path)
        elif booley_class is None:
            unclassified.append(path)
    return copied, _left_behind(source, unclassified)


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
