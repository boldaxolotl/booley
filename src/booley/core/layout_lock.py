"""Stable admission lock shared by Goal entry, worktree creation and layout migration."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from booley.core.booley_layout import (
    ensure_layout_metadata,
    reject_retired_path,
    require_current_layout,
    validate_environment,
)
from booley.core.file_lock import release_file_lock, wait_for_file_lock


@contextmanager
def layout_gate(root: Path, *, timeout_s: float = 30) -> Generator[None]:
    """Admit a mutation while retaining the unmoved Git common-directory lock."""
    validate_environment()
    reject_retired_path(root.absolute())
    require_current_layout(root)
    directory = ensure_layout_metadata(root)
    with (directory / "directory-layout.lock").open("a+") as handle:
        wait_for_file_lock(handle, timeout_s=timeout_s)
        try:
            require_current_layout(root)
            yield
        finally:
            release_file_lock(handle)
