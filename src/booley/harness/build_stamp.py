"""The wheel's baked-in build commit — one implementation, two build paths.

A wheel install has no adjacent ``.git``, so ``booley --version`` inside the
sandbox can only name the commit it was built from if the build bakes the answer
in. It does that by generating ``src/booley/_build_commit.py`` (gitignored,
transient) just before ``python -m build`` runs, which
``booley.harness.booley._packaged_commit`` reads back at runtime.

Both wheel builders go through this module — ``src/booley/data/docker/build.sh``
and ``booley init``'s in-process build
(``init_docker_image._docker_build_wheel``). init used to build unstamped, so
every init-driven image reported a bare ``booley <version>`` and the prescribed
"does the wheel match the commit?" freshness check was unanswerable exactly
where it matters (F-3).

Kept dependency-free (stdlib only, no imports from the rest of the harness) so
``build.sh`` can call it with nothing but ``PYTHONPATH=src``.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path

#: Generated stamp module, relative to the Booley repo root. Gitignored.
STAMP_RELPATH = "src/booley/_build_commit.py"

_GIT_TIMEOUT_S = 15


def stamp_path(booley_root: Path) -> Path:
    """Where :func:`write_build_stamp` writes the generated stamp module."""
    return booley_root / STAMP_RELPATH


def _git_output(booley_root: Path, *args: str) -> str:
    """``git -C <root> <args>`` stdout, or ``""`` when git can't answer.

    Not a git checkout, no git on PATH, a hung index — all mean the same thing
    to the caller (no commit to stamp), and none of them may abort a build that
    is otherwise fine, so they collapse to the empty string.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(booley_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def resolve_build_commit(booley_root: Path) -> str:
    """Short HEAD of *booley_root*, ``+dirty`` when the tree has local changes.

    Returns ``""`` when there is no commit to report (not a checkout, no git).
    A dirty tree produces a wheel that matches no commit, so it says so rather
    than claiming the HEAD it almost-but-not-quite is.
    """
    commit = _git_output(booley_root, "rev-parse", "--short", "HEAD").strip()
    if not commit:
        return ""
    if _git_output(booley_root, "status", "--porcelain").strip():
        commit += "+dirty"
    return commit


def resolve_source_updated_at(booley_root: Path) -> str:
    """ISO-8601 commit time of HEAD, or ``""`` when git cannot answer."""
    return _git_output(booley_root, "log", "-1", "--format=%cI", "HEAD").strip()


def write_build_stamp(booley_root: Path) -> str:
    """Generate the stamp module for *booley_root*; return the stamped commit.

    An unknown commit is still stamped (as ``COMMIT = ""``) so the runtime
    reader has a module to import either way.
    """
    commit = resolve_build_commit(booley_root)
    target = stamp_path(booley_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '"""Source commit baked in at wheel-build time (generated — do not edit)."""\n'
        "\n"
        f'COMMIT = "{commit}"\n',
        encoding="utf-8",
    )
    return commit


@contextlib.contextmanager
def build_stamp(booley_root: Path) -> Iterator[str]:
    """Stamp for the duration of a wheel build, then remove the stamp.

    The stamp only has to survive until the wheel is built. Leaving it behind
    makes the checkout report a baked commit it does not have (and fails
    ``test_absent_stamp_module_yields_none``), so removal is unconditional.
    """
    commit = write_build_stamp(booley_root)
    try:
        yield commit
    finally:
        stamp_path(booley_root).unlink(missing_ok=True)
