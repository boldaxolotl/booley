"""Discover committed program inputs directly referenced by Project configuration."""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import cast

_PROGRAM_SUFFIXES = frozenset({".py", ".sh", ".tcl", ".pl", ".rb"})
_PROGRAM_BASENAMES = frozenset({"makefile", "gnumakefile"})


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in cast(Mapping[object, object], value).values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _walk_strings(child)


def referenced_program_paths(
    value: object,
    *,
    search_roots: Iterable[Path],
    project_root: Path,
    strict: bool = False,
) -> tuple[Path, ...]:
    """Return existing in-Project programs directly referenced by configuration."""
    root = project_root.resolve()
    candidates: set[Path] = set()
    for raw in _walk_strings(value):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        for token in tokens:
            candidate = token.strip("'\";,()")
            path = PurePosixPath(candidate)
            if not _looks_like_program_path(path, candidate):
                continue
            matched = False
            for search_root in search_roots:
                resolved = (search_root / candidate).resolve()
                if not resolved.is_relative_to(root):
                    if strict:
                        raise ValueError(
                            f"referenced program cannot be mapped to the Project: {candidate}"
                        )
                    continue
                if resolved.is_relative_to(root) and resolved.is_file():
                    candidates.add(resolved)
                    matched = True
                    break
            if strict and not matched:
                raise ValueError(f"referenced program is unavailable: {candidate}")
    return tuple(sorted(candidates))


def _looks_like_program_path(path: PurePosixPath, token: str) -> bool:
    return (
        path.suffix.casefold() in _PROGRAM_SUFFIXES
        or path.name.casefold() in _PROGRAM_BASENAMES
        or "/" in token
        or "\\" in token
    )
