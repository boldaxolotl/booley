"""Runtime provenance for the Booley package and sandbox image."""

from __future__ import annotations

import os
from dataclasses import dataclass

from booley.runtime.timefmt import format_human_datetime


@dataclass(frozen=True)
class BuildMetadata:
    """The Booley code and sandbox-image provenance visible at runtime."""

    version: str
    revision: str
    source_updated_at: str
    image_built_at: str
    payload_fingerprint: str


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
    import booley

    attribution = booley.version_attribution
    checkout_revision, checkout_updated_at = attribution.source_git_metadata()
    image_version = os.environ.get("BOOLEY_VERSION", "")
    package_matches_image = not image_version or image_version == booley.__version__
    image_revision = os.environ.get("BOOLEY_SOURCE_REVISION", "")
    image_updated_at = os.environ.get("BOOLEY_SOURCE_UPDATED_AT", "")
    is_distribution = attribution.distribution_name is not None
    return BuildMetadata(
        version=booley.__version__,
        revision=(
            checkout_revision
            or (_baked_revision() if is_distribution else "")
            or (image_revision if is_distribution and package_matches_image else "")
        ),
        source_updated_at=(
            checkout_updated_at
            or (image_updated_at if is_distribution and package_matches_image else "")
        ),
        image_built_at=os.environ.get("BOOLEY_IMAGE_BUILT_AT", ""),
        payload_fingerprint=(
            (_embedded_payload_fingerprint() if is_distribution else "")
            or (
                os.environ.get("BOOLEY_PAYLOAD_FINGERPRINT", "")
                if is_distribution and package_matches_image
                else ""
            )
        ),
    )


def _embedded_payload_fingerprint() -> str:
    try:
        from booley._build_commit import PAYLOAD_FINGERPRINT
    except (ImportError, AttributeError):
        return ""
    return PAYLOAD_FINGERPRINT


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
