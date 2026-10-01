"""Select one physical Project-data namespace for linked worktrees."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from booley.core.project_dir import resolve_checkout_project_dir, resolve_project_dir


def worktree_state_dir(project_root: Path, *, project_dir: Path | None = None) -> Path:
    """Use a checkout-local spelling only when it names the selected state."""
    root = project_root.resolve()
    selected = project_dir.resolve() if project_dir is not None else resolve_project_dir(root)
    local = resolve_checkout_project_dir(root)
    try:
        local.relative_to(root)
        if local.samefile(selected):
            return local
    except (OSError, ValueError):
        pass
    return selected


def ticket_workspace_path(project_root: Path, slug: str) -> Path:
    """Return the outer Ticket Workspace under its selected state directory."""
    if not slug or Path(slug).name != slug or slug in {".", ".."}:
        raise ValueError(f"invalid Ticket Workspace name: {slug!r}")
    return worktree_state_dir(project_root) / "worktrees" / slug


def relative_worktree_paths(project_root: Path) -> bool:
    """Whether this layout can carry relative links between host and Sandbox."""
    root = project_root.resolve()
    if not _git_supports_relative_paths() or (root / ".git").is_file():
        return False
    try:
        worktree_state_dir(root).relative_to(root)
    except ValueError:
        return False
    if root == Path("/work"):
        alias = Path("/booley-project")
        return (
            alias.is_symlink()
            and alias.readlink() == Path("/work/.booley_project")
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
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.match(r"git version (\d+)\.(\d+)", result.stdout)
    return (
        result.returncode == 0 and match is not None and tuple(map(int, match.groups())) >= (2, 48)
    )
