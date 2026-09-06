"""Dependency-free validation for downloaded demo toolchains."""

from __future__ import annotations

import re
import urllib.parse


def validate_toolchain_provenance(url: str, sha256: str) -> None:
    """Require a plain HTTPS URL and a lowercase SHA-256 identity."""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("toolchain_url must be a plain HTTPS URL")
    if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValueError("toolchain_sha256 must be a lowercase SHA-256 digest")
