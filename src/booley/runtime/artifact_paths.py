"""Bounded field projection of available artifact paths, never inline log content."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

_PATH_KEYS = frozenset(
    {
        "path",
        "paths",
        "artifacts",
        "log_path",
        "report_path",
        "transcript_path",
        "stdout_path",
        "stderr_path",
    }
)


def available_paths(payload: Mapping[str, Any], roots: tuple[Path, ...]) -> tuple[str, ...]:
    """Project only explicitly named, existing files; missing artifacts remain unavailable."""
    pending = [(payload, False)]
    paths = []
    for _ in range(4096):
        if not pending:
            break
        value, is_path = pending.pop()
        if isinstance(value, Mapping):
            pending.extend((child, is_path or key in _PATH_KEYS) for key, child in value.items())
        elif isinstance(value, list):
            pending.extend((child, is_path) for child in value[:4096])
        elif is_path and isinstance(value, str):
            try:
                path = Path(value)
                candidates = (
                    (path,) if path.is_absolute() else tuple(root / path for root in roots)
                )
                existing = next(
                    (candidate for candidate in candidates if candidate.is_file()), None
                )
                if existing is not None:
                    paths.append(str(existing.absolute()))
            except OSError:
                continue
    return tuple(dict.fromkeys(paths))
