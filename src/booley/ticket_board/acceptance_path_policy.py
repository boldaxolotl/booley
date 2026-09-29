"""Dependency-free path policy shared with the standalone commit hook."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import PurePosixPath

CONTROL_SUFFIXES = frozenset({".core", ".sdc", ".xdc"})
GENERATED_DIRECTORY_NAMES = frozenset({".runtime", "_build", "build"})
PROJECT_CONTROL_FILES = frozenset(
    {"booley.toml", ".booley_project/tests.toml", ".booley_project/booley.toml"}
)
PROJECT_CONTROL_PREFIXES = (
    ".booley_project/hooks/",
    ".booley_project/.managed/",
    ".booley_project/generators/",
)


def normalize_acceptance_path(path: str) -> str:
    """Return a repository-relative POSIX spelling for policy checks."""
    return path.replace("\\", "/").strip().removeprefix("./")


def is_static_acceptance_path(path: str) -> bool:
    """Recognize paths that can belong to the immutable Target surface."""
    normalized = normalize_acceptance_path(path)
    parsed = PurePosixPath(normalized)
    if any(part in GENERATED_DIRECTORY_NAMES for part in parsed.parts):
        return False
    if parsed.suffix.casefold() in CONTROL_SUFFIXES:
        return True
    if normalized in PROJECT_CONTROL_FILES:
        return True
    return normalized.startswith(PROJECT_CONTROL_PREFIXES)


def is_protected_acceptance_path(
    path: str,
    protected: Collection[str],
    *,
    include_protected_parent: bool = False,
) -> bool:
    """Return whether *path* is an immutable acceptance-control location."""
    normalized = normalize_acceptance_path(path)
    if (
        is_static_acceptance_path(normalized)
        or normalized == "FUSESOC_IGNORE"
        or normalized.endswith("/FUSESOC_IGNORE")
        or normalized in protected
        or any(normalized.startswith(item.rstrip("/") + "/") for item in protected)
    ):
        return True
    return include_protected_parent and any(
        item.startswith(normalized.rstrip("/") + "/") for item in protected
    )
