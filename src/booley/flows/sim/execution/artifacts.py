"""Simulation artifact naming and authorization helpers."""

from __future__ import annotations

import glob
import hashlib
import re
from pathlib import Path

_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def artifact_path_component(value: str) -> str:
    """Return one traversal-safe, bounded component representing *value*."""
    if _SAFE_COMPONENT_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"~sha256-{digest}"


def configured_trace_path(
    candidate: Path,
    patterns: tuple[str, ...],
    search_roots: tuple[Path, ...],
) -> bool:
    """Return whether a configured exact path or glob authorizes *candidate*."""
    resolved = candidate.resolve()
    for pattern in patterns:
        bases = (Path("/"),) if Path(pattern).is_absolute() else search_roots
        for base in bases:
            rendered = Path(pattern) if Path(pattern).is_absolute() else base / pattern
            if glob.has_magic(str(rendered)):
                anchor = Path("/") if rendered.is_absolute() else Path()
                pattern_from_anchor = str(rendered).removeprefix("/")
                if any(match.resolve() == resolved for match in anchor.glob(pattern_from_anchor)):
                    return True
            elif rendered.resolve() == resolved:
                return True
    return False


__all__ = ["artifact_path_component", "configured_trace_path"]
