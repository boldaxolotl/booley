"""Read or copy a source checkout as Git lists it, so transient ignored files cannot race."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.subprocess_budget import run_bounded_child


def source_checkout_files(root: Path, *pathspecs: str) -> list[Path]:
    """Sorted paths of *root*'s Git-listed working-tree files, optionally limited to *pathspecs*.

    Lists tracked files and untracked files Git does not ignore. Skips tracked
    files deleted from the working tree. Fails the test, naming *root*, when
    *root* is not the top of a Git working tree or Git cannot run.
    """
    if _git(root, "rev-parse", "--show-prefix").strip():
        pytest.fail(f"source root {root} is not the top of a Git working tree")
    listed = _git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *pathspecs
    )
    deleted = _git(root, "ls-files", "-z", "--deleted", "--", *pathspecs)
    return [root / name for name in sorted(_names(listed) - _names(deleted))]


def copy_source_checkout(root: Path, destination: Path, *pathspecs: str) -> None:
    """Copy ``source_checkout_files(root, *pathspecs)`` into *destination*, keeping modes."""
    for source in source_checkout_files(root, *pathspecs):
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _git(root: Path, *args: str) -> str:
    """Run Git in *root* and return stdout; fail the test, naming *root*, on any error."""
    try:
        # A hang fails inside run_bounded_child, naming the command and so *root*.
        result = run_bounded_child(["git", "-C", str(root), *args], timeout=60)
    except OSError as error:  # Git cannot start
        pytest.fail(f"cannot run Git for source root {root}: {error}")
    if result.returncode != 0:
        pytest.fail(f"source root {root} is not a Git checkout: {result.stderr.strip()}")
    return result.stdout


def _names(output: str) -> set[str]:
    """Split NUL-separated Git output into a set of path names."""
    return {name for name in output.split("\0") if name}
