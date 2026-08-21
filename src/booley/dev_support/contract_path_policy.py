"""Dependency-free path policy shared with the standalone commit hook."""

from __future__ import annotations

from pathlib import PurePosixPath

CONTROL_SUFFIXES = frozenset({".core", ".sdc", ".xdc"})
PROJECT_CONTROL_FILES = frozenset(
    {".booley_project/tests.toml", ".booley_project/booley.toml"}
)
PROJECT_CONTROL_PREFIXES = (".booley_project/hooks/", ".booley_project/generators/")


def normalize_contract_path(path: str) -> str:
    """Return a repository-relative POSIX spelling for policy checks."""
    return path.replace("\\", "/").strip().removeprefix("./")


def is_static_contract_path(path: str) -> bool:
    """Recognize paths that can belong to the immutable Target surface."""
    normalized = normalize_contract_path(path)
    if PurePosixPath(normalized).suffix.casefold() in CONTROL_SUFFIXES:
        return True
    if normalized in PROJECT_CONTROL_FILES:
        return True
    return normalized.startswith(PROJECT_CONTROL_PREFIXES)
