"""Select one physical Booley Directory namespace for linked worktrees."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from booley.core.booley_dir import (
    BOOLEY_DIR_NAME,
    BooleyDirOutsideCheckoutError,
    own_booley_dir_relative,
    resolve_booley_dir,
    resolve_checkout_booley_dir,
)
from booley.core.booley_layout import BooleyLayoutError
from booley.runtime import platform_paths


class BooleyDirMismatchError(ValueError):
    """The ambient or configured Booley Directory is not the checkout's own, usable one."""


def worktree_state_dir(project_root: Path, *, booley_dir: Path | None = None) -> Path:
    """Use a checkout-local spelling only when it names the selected state."""
    root = project_root.resolve()
    selected = booley_dir.resolve() if booley_dir is not None else resolve_booley_dir(root)
    local = resolve_checkout_booley_dir(root)
    try:
        local.relative_to(root)
        if local.samefile(selected):
            return local
    except (OSError, ValueError):
        pass
    return selected


def relative_worktree_paths(project_root: Path) -> bool:
    """Whether this layout can carry relative links between host and Sandbox."""
    root = project_root.resolve()
    if not _git_supports_relative_paths() or (root / ".git").is_file():
        return False
    try:
        # Git writes relative metadata from physical paths, so test the physical location.
        worktree_state_dir(root).resolve().relative_to(root)
    except ValueError:
        return False
    if root == Path("/work"):
        alias = Path("/booley-project")
        return (
            alias.is_symlink()
            and alias.readlink() == Path("/work/.booley")
            and alias.lstat().st_uid == 0
            and Path("/").stat().st_uid == 0
            and not Path("/").stat().st_mode & 0o022
        )
    return True


def worktree_creation_config(project_root: Path) -> tuple[str, ...]:
    """Override only this creation when the selected topology is not portable."""
    return (
        () if relative_worktree_paths(project_root) else ("-c", "worktree.useRelativePaths=false")
    )


def _git_supports_relative_paths() -> bool:
    try:
        result = platform_paths.run_captured_bounded(
            ["git", "--version"], timeout=10, errors="strict"
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"git version (\d+)\.(\d+)", result.stdout)
    return (
        result.returncode == 0 and match is not None and tuple(map(int, match.groups())) >= (2, 48)
    )


def _is_same_directory(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return False


def _require_ambient_booley_dir(root: Path, own: Path, ambient: str) -> None:
    """Refuse a ``BOOLEY_DIR`` that does not name *own*, the checkout's Booley Directory."""
    selected = Path(ambient)
    # An absent default ``.booley`` can only match by spelling.
    if Path(os.path.normpath(selected.absolute())) == own or _is_same_directory(selected, own):
        return
    try:
        inside = selected.resolve(strict=True).is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        raise BooleyDirOutsideCheckoutError(ambient, root)
    raise BooleyDirMismatchError(
        f"BOOLEY_DIR={ambient} is not this checkout's Booley Directory {own}; "
        "unset BOOLEY_DIR or point it at that directory"
    )


def worktree_booley_dir(project_root: Path) -> PurePosixPath:
    """Return the checkout's Booley Directory relative to its root, or refuse worktree creation.

    Raises:
        BooleyDirOutsideCheckoutError: the directory (or a host ``BOOLEY_DIR``) is
            outside the checkout.
        BooleyDirMismatchError: ``BOOLEY_DIR`` names another directory inside the
            checkout, or a configured ``[project].dir`` does not exist.
    """
    root = Path(os.path.normpath(project_root.absolute()))
    relative = own_booley_dir_relative(root)
    own = root / relative
    # A default ``.booley`` may be absent (creation then has nothing to copy);
    # a configured one that is missing is a typo that would silently copy nothing.
    if own != root / BOOLEY_DIR_NAME and not own.is_dir():
        raise BooleyDirMismatchError(f"Booley Directory {own} does not exist")
    ambient = os.environ.get("BOOLEY_DIR")
    if ambient:
        _require_ambient_booley_dir(root, own, ambient)
    return relative


def main(argv: list[str] | None = None) -> int:
    """Print the checkout's Booley Directory relative to its root, for the worktree script.

    ``<root> --ambient`` applies :func:`worktree_booley_dir` (the checkout being
    branched from); ``<root>`` alone reads only that checkout's own configuration
    (the new worktree, whose ambient selection is irrelevant).
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    ambient = "--ambient" in arguments
    roots = [a for a in arguments if a != "--ambient"]
    if len(roots) != 1:
        print("usage: worktree_paths <root> [--ambient]", file=sys.stderr)
        return 2
    root = Path(roots[0])
    try:
        relative = worktree_booley_dir(root) if ambient else own_booley_dir_relative(root)
    except (ValueError, BooleyLayoutError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(relative)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
