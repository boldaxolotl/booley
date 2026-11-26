"""Validated, operation-free Sandbox Image naming policy."""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.boundary import as_dict

SANDBOX_IMAGE = "booley-sandbox"


def project_image_name(project_root: Path) -> str:
    """Return the deterministic Docker-safe tag for a generated Project image."""
    return project_image_name_for_identity(str(project_root.resolve()))


def project_image_name_for_identity(identity: str) -> str:
    """Name the image for an already canonical stored root without touching the filesystem."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", Path(identity).name.lower()).strip("-._")
    owner = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"{slug or 'project'}-{SANDBOX_IMAGE}-{owner}"


def project_sandbox_image(project_root: Path) -> str:
    """Return the validated Project-selected Sandbox Image or the default."""
    try:
        booley_dir = resolve_checkout_booley_dir(project_root)
    except FileNotFoundError:
        return project_image_name(project_root)
    toml_path = booley_dir / "booley.toml"
    if not toml_path.is_file():
        return project_image_name(project_root)
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return project_image_name(project_root)
    sandbox = as_dict(data.get("sandbox"), default={}) or {}
    return _selected_from_config(project_root, booley_dir, sandbox)


def _selected_from_config(
    project_root: Path,
    booley_dir: Path,
    sandbox: dict[str, object],
) -> str:
    raw = sandbox.get("image", "")
    logical = raw.strip() if isinstance(raw, str) and raw.strip() else SANDBOX_IMAGE
    if logical in {SANDBOX_IMAGE, "booley-sandbox-riscv", project_image_name(project_root)}:
        return project_image_name(project_root)
    return logical
