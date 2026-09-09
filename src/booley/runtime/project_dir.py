"""Runtime directories and the stable Project-directory compatibility interface."""

from __future__ import annotations

from pathlib import Path

from booley.core.project_dir import (
    PROJECT_DIR_NAME,
    checkout_project_dir_relative_to,
    init_project_dir_scope,
    project_dir_for_init,
    reset_cache,
    resolve_checkout_project_dir,
    resolve_project_dir,
)

__all__ = [
    "PROJECT_DIR_NAME",
    "checkout_project_dir_relative_to",
    "init_project_dir_scope",
    "project_dir_for_init",
    "reset_cache",
    "resolve_checkout_project_dir",
    "resolve_project_dir",
]


def checkout_runtime_dir(project_root: Path) -> Path:
    """Return the runtime tree anchored to one explicitly selected checkout."""
    directory = resolve_checkout_project_dir(project_root) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def runtime_dir(start: Path | None = None) -> Path:
    """Return and create the active Project's transient ``.runtime`` tree."""
    directory = resolve_project_dir(start) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
