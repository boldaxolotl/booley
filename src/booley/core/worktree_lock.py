"""Per-Project lock shared by worktree creation, its rollback and removal."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from booley.core.booley_layout import ensure_layout_metadata
from booley.core.file_lock import release_file_lock, wait_for_file_lock

logger = logging.getLogger(__name__)


@contextmanager
def worktree_mutation_lock(root: Path, *, timeout_s: float = 30) -> Generator[None]:
    """Serialize worktree creation, its rollback and removal within one Project."""
    directory = ensure_layout_metadata(root)
    with (directory / "worktree-mutation.lock").open("a+") as handle:
        wait_for_file_lock(
            handle,
            timeout_s=timeout_s,
            on_wait=lambda: logger.warning(
                "another worktree change of this Project is running; waiting up to %gs",
                timeout_s,
            ),
        )
        try:
            yield
        finally:
            release_file_lock(handle)
