"""Classify a checkout before Project-owned state or policy is used."""

from __future__ import annotations

import tomllib
from pathlib import Path


class SourceCheckoutProjectError(RuntimeError):
    """Raised when a Project-only operation targets Booley's own source."""


def _source_layout(root: Path) -> bool:
    """Return whether *root* has Booley's distinctive source-tree layout."""
    return (root / "src" / "booley" / "__init__.py").is_file()


def is_booley_source_checkout(root: Path) -> bool:
    """Return whether *root* is a checkout of Booley's own source.

    The tracked marker is authoritative for current checkouts.  The package
    name plus source layout keeps older branches and forks recognizable.  A
    damaged pyproject fails closed when the distinctive layout is still
    present: broken metadata must not turn Booley itself into a Project.
    """
    checkout = Path(root).resolve()
    pyproject = checkout / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return _source_layout(checkout)

    tool = document.get("tool", {})
    booley = tool.get("booley", {}) if isinstance(tool, dict) else {}
    marker = booley.get("source_checkout") if isinstance(booley, dict) else None
    if marker is True:
        return True

    project = document.get("project", {})
    legacy_identity = isinstance(project, dict) and project.get("name") == "booley-rtl"
    return legacy_identity and _source_layout(checkout)


def source_checkout_root(start: Path) -> Path | None:
    """Return the enclosing Booley Source Checkout, if any."""
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if is_booley_source_checkout(candidate):
            return candidate
    return None


def require_project_checkout(root: Path) -> Path:
    """Return a resolved Project candidate, rejecting Booley source."""
    checkout = Path(root).resolve()
    source_root = source_checkout_root(checkout)
    if source_root is not None:
        raise SourceCheckoutProjectError(
            "Booley's own Source Checkout cannot be initialized or used as a Project. "
            f"Remove or migrate any stale {source_root / '.booley_project'} separately."
        )
    return checkout
