"""Runtime provenance for the Booley package and sandbox image."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.timefmt import format_human_datetime

_GIT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class BuildMetadata:
    """The Booley code and sandbox-image provenance visible at runtime."""

    version: str
    revision: str
    source_updated_at: str
    image_built_at: str


def _checkout_root() -> Path | None:
    """Return the source checkout containing the imported package, if any."""
    import booley

    package_dir = Path(booley.__file__).resolve().parent
    for candidate in (package_dir, *package_dir.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_output(root: Path, *args: str) -> str:
    """Return stripped git output, or an empty string when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _checkout_metadata() -> tuple[str, str]:
    """Return revision and last-commit time for an imported source checkout."""
    root = _checkout_root()
    if root is None:
        return "", ""
    revision = _git_output(root, "rev-parse", "--short", "HEAD")
    if revision and _git_output(root, "status", "--porcelain"):
        revision += "+dirty"
    updated_at = _git_output(root, "log", "-1", "--format=%cI", "HEAD")
    return revision, updated_at


def _baked_revision() -> str:
    """Read the legacy wheel-build revision stamp when available."""
    try:
        from booley._build_commit import COMMIT
    except (ImportError, AttributeError):
        return ""
    return COMMIT


def current_build_metadata() -> BuildMetadata:
    """Return provenance for the code actually imported by this process.

    A bind-mounted development checkout takes precedence over image metadata;
    installed wheels fall back to values baked into the image build.
    """
    from booley import __version__

    checkout_revision, checkout_updated_at = _checkout_metadata()
    image_version = os.environ.get("BOOLEY_VERSION", "")
    package_matches_image = not image_version or image_version == __version__
    image_revision = os.environ.get("BOOLEY_SOURCE_REVISION", "")
    image_updated_at = os.environ.get("BOOLEY_SOURCE_UPDATED_AT", "")
    return BuildMetadata(
        version=__version__,
        revision=(
            checkout_revision
            or _baked_revision()
            or (image_revision if package_matches_image else "")
        ),
        source_updated_at=(
            checkout_updated_at or (image_updated_at if package_matches_image else "")
        ),
        image_built_at=os.environ.get("BOOLEY_IMAGE_BUILT_AT", ""),
    )


def _format_timestamp(value: str) -> str:
    """Render a timestamp in the user's local timezone."""
    if not value or value == "unknown":
        return "unknown"
    try:
        return format_human_datetime(value)
    except ValueError:
        return value


def format_status_line() -> str:
    """Return the one-line build provenance shown by ``booley_status``."""
    metadata = current_build_metadata()
    revision = f" ({metadata.revision})" if metadata.revision else ""
    return (
        f"Booley: {metadata.version}{revision}; last updated "
        f"{_format_timestamp(metadata.source_updated_at)}; sandbox image built "
        f"{_format_timestamp(metadata.image_built_at)}."
    )
