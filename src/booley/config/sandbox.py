"""Validated, operation-free Session Image naming policy."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from booley.core.project_dir import resolve_checkout_project_dir

SANDBOX_IMAGE = "booley-sandbox"


def project_image_name(project_root: Path) -> str:
    """Return the deterministic Docker-safe tag for a generated Project image."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", project_root.name.lower()).strip("-._")
    return f"{slug or 'project'}-{SANDBOX_IMAGE}"


def project_sandbox_image(project_root: Path) -> str:
    """Return the validated Project-selected Session Image or the default."""
    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        return SANDBOX_IMAGE
    toml_path = project_dir / "booley.toml"
    if not toml_path.is_file():
        return SANDBOX_IMAGE
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return SANDBOX_IMAGE
    raw = data.get("sandbox", {}).get("image", "")
    if isinstance(raw, str) and raw.strip():
        return raw
    if (project_dir / "docker" / "Dockerfile").is_file():
        return project_image_name(project_root)
    return SANDBOX_IMAGE
