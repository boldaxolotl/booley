"""Policy for the one authoritative Booley installation on a host."""

from __future__ import annotations

import sys
from pathlib import Path

_INSTALLED_PACKAGE_DIRS = {"site-packages", "dist-packages"}
_EPHEMERAL_PARTS = {".runtime", ".worktrees", "qa-runs"}


def host_install_error(
    package_resource: Path,
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
) -> str | None:
    """Explain why *package_resource* cannot own machine-global integrations."""
    active_prefix = Path(sys.prefix) if prefix is None else prefix
    interpreter_prefix = Path(sys.base_prefix) if base_prefix is None else base_prefix
    if active_prefix.resolve() != interpreter_prefix.resolve():
        return (
            "Booley is running from a virtual environment; machine-global resources "
            "may only be managed by the canonical host-installed wheel"
        )
    resolved = package_resource.resolve()
    if not any(part in _INSTALLED_PACKAGE_DIRS for part in resolved.parts):
        return (
            f"Booley package resource is not from an installed wheel: {resolved}; "
            "run the canonical host `booley bootstrap`"
        )
    if _EPHEMERAL_PARTS.intersection(resolved.parts):
        return (
            f"Booley package resource is inside ephemeral workspace state: {resolved}; "
            "temporary worktrees and QA runs cannot manage machine-global resources"
        )
    return None
