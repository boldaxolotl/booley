"""Resolve feedback storage without conflating Booley source with a Project."""

from __future__ import annotations

from pathlib import Path

from booley.runtime.booley_dir import BOOLEY_DIR_NAME, resolve_booley_dir
from booley.runtime.checkout_role import is_booley_source_checkout
from booley.runtime.git import git_common_dir

SOURCE_FEEDBACK_DIR_NAME = "booley-feedback"


def feedback_storage_dir(checkout_root: Path) -> Path:
    """Return the durable local directory owned by feedback commands.

    Projects keep findings with their Project state.  A Booley source checkout
    is not a Project, so its dogfood findings live under ``$GIT_COMMON_DIR``;
    linked worktrees therefore share one untracked store.
    """
    root = Path(checkout_root).resolve()
    if is_booley_source_checkout(root):
        return git_common_dir(root) / SOURCE_FEEDBACK_DIR_NAME
    try:
        return resolve_booley_dir(root)
    except FileNotFoundError:
        return root / BOOLEY_DIR_NAME
