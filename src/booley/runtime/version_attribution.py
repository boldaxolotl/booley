"""Attribute the runtime version to the exact Booley code being imported."""

from __future__ import annotations

import importlib.metadata
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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


def _source_root(package_file: Path) -> Path | None:
    resolved = package_file.resolve()
    if resolved.name != "__init__.py" or resolved.parent.name != "booley":
        return None
    if resolved.parent.parent.name != "src":
        return None
    root = resolved.parents[2]
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot read Booley project metadata from {pyproject}: {exc}") from exc
    return root if project.get("name") == "booley-rtl" else None


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
