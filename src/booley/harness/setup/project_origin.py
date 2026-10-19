"""Explicit creation provenance in the selected Project configuration."""

from pathlib import Path
from stat import S_IMODE
from typing import Literal

from booley.runtime.atomic_files import atomic_replace_bytes

ProjectOrigin = Literal["scaffold", "port"]
_PREFIX = "# booley-init-origin:"


def read_project_origin(config: Path) -> ProjectOrigin | None:
    """Read an exact origin marker; missing or unreadable markers are unknown."""
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for origin in ("scaffold", "port"):
        if f"{_PREFIX} {origin}" in lines:
            return origin
    return None


def record_scaffold_origin(config: Path) -> None:
    """Record provenance only after all scaffold files were written."""
    atomic_replace_bytes(
        config,
        f"{_PREFIX} scaffold\n".encode() + config.read_bytes(),
        mode=S_IMODE(config.stat().st_mode),
    )
