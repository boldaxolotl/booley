"""Stable admission lock shared by Goal entry, worktree creation and layout migration."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from booley.core.booley_layout import require_current_layout, validate_environment
from booley.core.file_lock import release_file_lock, wait_for_file_lock


@contextmanager
def layout_gate(root: Path, *, timeout_s: float = 30) -> Generator[None]:
    """Admit a mutation while retaining the unmoved Git common-directory lock."""
    validate_environment()
    require_current_layout(root)
    marker = root / ".git"
    if marker.is_file():
        raw = marker.read_text().strip().removeprefix("gitdir: ")
        admin = (root / raw).resolve()
        common = admin / "commondir"
        marker = (admin / common.read_text().strip()).resolve() if common.is_file() else admin
    if not marker.is_dir():
        # Goal entry/worktree creation require Git independently; no lock is needed
        # for their non-Git validation failure.
        yield
        return
    directory = marker / "booley"
    directory.mkdir(exist_ok=True)
    with (directory / "directory-layout.lock").open("a+") as handle:
        wait_for_file_lock(handle, timeout_s=timeout_s)
        try:
            require_current_layout(root)
            yield
        finally:
            release_file_lock(handle)
