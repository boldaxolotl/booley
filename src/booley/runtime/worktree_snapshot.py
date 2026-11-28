"""Choose and copy what a Snapshot holds of the Main Booley Directory (ADR 0082).

The Snapshot is an allowlist read from
:data:`booley.core.booley_dir_parts.BOOLEY_DIR_PATHS`: only paths classified as
Checkout state versioned with the RTL are copied. Common state, state tied to
one checkout's sources, and paths no row covers stay in the Main Booley
Directory. An unclassified path is named so a missing row is noticed, unless Git
tracks or ignores every file below it. The ``[coverage.waivers]`` directory
anchored to the project data repository is copied too.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

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

# Nested repositories and tool caches inside copied configuration stay behind.
_LEFT_OUT_NAMES = frozenset({".git", ".venv", "__pycache__"})


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


class SnapshotError(RuntimeError):
    """The Snapshot could not be listed or copied."""


def _unlink_existing(path: Path) -> None:
    """Remove a symlink or non-directory at *path* without following it."""
    if os.path.lexists(path):
        path.unlink()


def _place(source: Path, destination: Path) -> None:
    """Copy one entry, replacing what stands at *destination* as an archive extraction does."""
    mode = source.lstat().st_mode
    if stat.S_ISSOCK(mode):
        return
    if stat.S_ISDIR(mode):
        if destination.is_symlink() or not destination.is_dir():
            _unlink_existing(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir()):
            if entry.name not in _LEFT_OUT_NAMES:
                _place(entry, destination / entry.name)
        shutil.copystat(source, destination, follow_symlinks=False)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    _unlink_existing(destination)
    if stat.S_ISLNK(mode):
        destination.symlink_to(source.readlink())
    elif stat.S_ISREG(mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    elif stat.S_ISFIFO(mode):
        os.mkfifo(destination, stat.S_IMODE(mode))
    else:
        raise OSError(f"cannot copy special file {source}")


def _refuse_linked_ancestors(destination: Path, path: str) -> None:
    """Refuse a *path* below a symlink, which could alias the source it is copied from."""
    parent = destination
    for part in PurePosixPath(path).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise OSError(f"{parent} is a symbolic link, so {path} cannot be copied below it")


def copy_snapshot(source: Path, destination: Path) -> list[str]:
    """Copy the Snapshot of the Booley Directory *source* into *destination*.

    Returns the unclassified paths it left behind; the caller names them.

    Raises:
        SnapshotError: *source* cannot be listed or an entry cannot be copied.
    """
    try:
        copied, unclassified = snapshot_entries(source)
    except OSError as exc:
        raise SnapshotError(f"cannot list the Booley Directory for the Snapshot: {exc}") from exc
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for path in copied:
            if _LEFT_OUT_NAMES.isdisjoint(PurePosixPath(path).parts):
                _refuse_linked_ancestors(destination, path)
                _place(source / path, destination / path)
    except OSError as exc:
        raise SnapshotError(f"copying the Snapshot failed: {exc}") from exc
    return unclassified
