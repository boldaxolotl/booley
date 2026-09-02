"""Attribute the runtime version to the exact Booley code being imported."""

from __future__ import annotations

import importlib.metadata
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.runtime.checkout_role import is_booley_source_checkout

_GIT_TIMEOUT_SECONDS = 5


class VersionOrigin(StrEnum):
    """Authoritative source of a reported Booley semantic version."""

    SOURCE = "source"
    DISTRIBUTION = "distribution"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class VersionAttribution:
    """A semantic version and the installation/source that supplied it."""

    version: str
    origin: VersionOrigin
    source_root: Path | None = None
    distribution_name: str | None = None

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("version attribution requires a non-empty version")
        expected = {
            VersionOrigin.SOURCE: (True, False),
            VersionOrigin.DISTRIBUTION: (False, True),
            VersionOrigin.FALLBACK: (False, False),
        }[self.origin]
        actual = (self.source_root is not None, self.distribution_name is not None)
        if actual != expected:
            raise ValueError(f"invalid {self.origin} version attribution")

    def source_git_metadata(self) -> tuple[str, str]:
        """Return revision and last-commit time for the attributed source checkout."""
        root = self.source_root
        if root is None or not (root / ".git").exists():
            return "", ""
        revision = _git_output(root, "rev-parse", "--short", "HEAD")
        if revision and _git_output(root, "status", "--porcelain"):
            revision += "+dirty"
        updated_at = _git_output(root, "log", "-1", "--format=%cI", "HEAD")
        return revision, updated_at


def _git_output(root: Path, *args: str) -> str:
    """Return stripped Git output, or an empty string when Git cannot answer."""
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


def _source_root(package_file: Path) -> Path | None:
    resolved = package_file.resolve()
    if resolved.name != "__init__.py" or resolved.parent.name != "booley":
        return None
    if resolved.parent.parent.name != "src":
        return None
    root = resolved.parents[2]
    return root if is_booley_source_checkout(root) else None


def _read_source_version(root: Path) -> str:
    version_file = root / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read Booley source version from {version_file}: {exc}"
        ) from exc
    if not version:
        raise RuntimeError(f"Booley source version is empty: {version_file}")
    return version


def _distribution_attribution(package_file: Path) -> VersionAttribution | None:
    resolved = package_file.resolve()
    for name in ("booley-rtl", "booley"):
        for dist in importlib.metadata.distributions(name=name):
            files = dist.files
            if not files or not any(
                dist.locate_file(path).resolve() == resolved for path in files
            ):
                continue
            version = dist.version.strip()
            if not version:
                raise RuntimeError(f"owning {name} distribution has no version metadata")
            return VersionAttribution(
                version=version,
                origin=VersionOrigin.DISTRIBUTION,
                distribution_name=name,
            )
    return None


def resolve_version_attribution(package_file: Path) -> VersionAttribution:
    """Resolve the version belonging to the code at *package_file*."""
    root = _source_root(package_file)
    if root is not None:
        return VersionAttribution(
            version=_read_source_version(root),
            origin=VersionOrigin.SOURCE,
            source_root=root,
        )
    return _distribution_attribution(package_file) or VersionAttribution(
        version="0.0.0-dev",
        origin=VersionOrigin.FALLBACK,
    )
