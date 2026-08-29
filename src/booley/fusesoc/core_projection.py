"""Stealth-only projection of authored FuseSoC cores into the project root.

The authoritative cores stay versioned under ``.booley_project/cores/``.  A
small generated copy at the RTL repository root gives FuseSoC the repository
itself as the core root, so filesets can name pristine upstream sources
directly instead of reaching them through a farm of resolution symlinks.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

PROJECTED_CORE_PREFIX = ".booley-projected-"
PROJECTED_CORE_GLOB = f"{PROJECTED_CORE_PREFIX}*.core"
ISOLATED_REGISTRY_SUBDIR = Path(".booley_project/tmp/fusesoc-isolated-cores")
_ISOLATED_CORE_PREFIX = "booley-isolated-"
_MARKER_PREFIX = "# Booley stealth core projection: "


class CoreProjectionError(RuntimeError):
    """A projected core could not be created without risking user data."""


@dataclass(frozen=True)
class ProjectionResult:
    """Filesystem changes made by one projection reconciliation."""

    written: tuple[Path, ...]
    removed: tuple[Path, ...]


def projection_enabled(project_root: Path | str) -> bool:
    """Whether explicit stealth mode enables root-level core projections.

    The old missing-key fallback remains valid for commit-message sanitation,
    but a filesystem projection requires the project's explicit consent.
    New setups always persist ``enabled``, so this also provides a safe legacy
    boundary for projects using ADR 0036 resolution symlinks.
    """
    stealth = _stealth_config(project_root)
    return stealth.get("enabled") is True


def native_cores_ignored(project_root: Path | str) -> bool:
    """Whether stealth resolution excludes repository-native ``.core`` files."""
    stealth = _stealth_config(project_root)
    return stealth.get("enabled") is True and stealth.get("ignore_native_cores") is True


def _stealth_config(project_root: Path | str) -> dict[str, Any]:
    config = Path(project_root) / ".booley_project" / "booley.toml"
    try:
        with config.open("rb") as stream:
            stealth = tomllib.load(stream).get("stealth")
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return dict(stealth) if isinstance(stealth, dict) else {}


def authoritative_cores(project_root: Path | str) -> tuple[Path, ...]:
    """Return projection-eligible cores under ``.booley_project/cores``."""
    core_root = Path(project_root) / ".booley_project" / "cores"
    if not core_root.is_dir() or (core_root / "FUSESOC_IGNORE").is_file():
        return ()
    cores = (path for path in core_root.rglob("*.core") if not _under_ignore(path, core_root))
    return tuple(sorted(cores))


def projected_core_path(project_root: Path | str, core_file: Path) -> Path:
    """Return the deterministic root-level projection path for *core_file*."""
    root = Path(project_root)
    core_root = root / ".booley_project" / "cores"
    try:
        relative = core_file.relative_to(core_root).as_posix()
    except ValueError as exc:
        raise CoreProjectionError(f"core is outside the stealth core root: {core_file}") from exc
    encoded = quote(relative, safe="")
    return root / f"{PROJECTED_CORE_PREFIX}{encoded}"


def reconcile_projected_cores(project_root: Path | str) -> ProjectionResult:
    """Create current projections and safely remove stale owned projections."""
    root = Path(project_root)
    expected = _expected_projections(root) if projection_enabled(root) else {}
    written: list[Path] = []
    for destination, content in expected.items():
        if _write_projection(destination, content):
            written.append(destination)

    removed: list[Path] = []
    for candidate in root.glob(PROJECTED_CORE_GLOB):
        if candidate in expected or not _is_owned_projection(candidate):
            continue
        candidate.unlink()
        removed.append(candidate)
    return ProjectionResult(tuple(sorted(written)), tuple(sorted(removed)))


def isolated_registry_root(project_root: Path | str) -> Path:
    """Return the private FuseSoC registry used to exclude native cores."""
    return Path(project_root) / ISOLATED_REGISTRY_SUBDIR


def isolated_core_path(project_root: Path | str, core_file: Path) -> Path:
    """Return the private-registry projection for one authoritative core."""
    root = Path(project_root)
    core_root = root / ".booley_project" / "cores"
    try:
        relative = core_file.relative_to(core_root).as_posix()
    except ValueError as exc:
        raise CoreProjectionError(f"core is outside the stealth core root: {core_file}") from exc
    return isolated_registry_root(root) / f"{_ISOLATED_CORE_PREFIX}{quote(relative, safe='')}"


def reconcile_isolated_registry(project_root: Path | str) -> ProjectionResult:
    """Materialize only stealth-authored cores in a private FuseSoC registry."""
    root = Path(project_root)
    registry = isolated_registry_root(root)
    registry.mkdir(parents=True, exist_ok=True)
    expected = _expected_isolated_cores(root) if native_cores_ignored(root) else {}
    written = [path for path, content in expected.items() if _write_projection(path, content)]
    removed: list[Path] = []
    for candidate in registry.glob("*.core"):
        if candidate in expected:
            continue
        if not _is_owned_projection(candidate):
            raise CoreProjectionError(
                f"foreign .core file blocks the private FuseSoC registry: {candidate}"
            )
        candidate.unlink()
        removed.append(candidate)
    marker = registry / "FUSESOC_IGNORE"
    if marker.exists():
        raise CoreProjectionError(f"FUSESOC_IGNORE blocks the private FuseSoC registry: {marker}")
    return ProjectionResult(tuple(sorted(written)), tuple(sorted(removed)))


def projection_issues(project_root: Path | str) -> tuple[str, ...]:
    """Describe missing, stale, foreign, or disabled projections without writes."""
    root = Path(project_root)
    expected = _expected_projections(root) if projection_enabled(root) else {}
    issues: list[str] = []
    for destination, content in expected.items():
        if not destination.exists():
            issues.append(f"missing {destination.name}")
            continue
        if not _is_owned_projection(destination):
            issues.append(f"foreign file blocks {destination.name}")
            continue
        if destination.read_text(encoding="utf-8") != content:
            issues.append(f"stale {destination.name}")
    for candidate in root.glob(PROJECTED_CORE_GLOB):
        if candidate not in expected and _is_owned_projection(candidate):
            issues.append(f"stale {candidate.name}")
    return tuple(sorted(issues))


def _expected_projections(root: Path) -> dict[Path, str]:
    return {
        projected_core_path(root, core): _render_projection(root, core)
        for core in authoritative_cores(root)
    }


def _expected_isolated_cores(root: Path) -> dict[Path, str]:
    return {
        isolated_core_path(root, core): _render_isolated_core(root, core)
        for core in authoritative_cores(root)
    }


def _render_projection(root: Path, core_file: Path) -> str:
    try:
        content = core_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoreProjectionError(f"could not read authoritative core {core_file}: {exc}") from exc
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "CAPI=2:":
        raise CoreProjectionError(f"authoritative core lacks leading CAPI=2: marker: {core_file}")
    source = core_file.relative_to(root).as_posix()
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    first_line = lines[0] if lines[0].endswith(("\n", "\r")) else lines[0] + newline
    return "".join((first_line, f"{_MARKER_PREFIX}{source}{newline}", *lines[1:]))


def _render_isolated_core(root: Path, core_file: Path) -> str:
    try:
        doc = yaml.safe_load(core_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CoreProjectionError(
            f"could not parse authoritative core {core_file}: {exc}"
        ) from exc
    if not isinstance(doc, Mapping) or "CAPI=2" not in doc:
        raise CoreProjectionError(f"authoritative core lacks leading CAPI=2: marker: {core_file}")
    body = dict(doc)
    body.pop("CAPI=2", None)
    imperative = sorted(
        key for key in ("generate", "generators", "provider", "scripts") if body.get(key)
    )
    if imperative:
        names = ", ".join(imperative)
        raise CoreProjectionError(
            f"native-core isolation cannot safely rebase {names} in {core_file}"
        )
    _absolutize_filesets(body, root.resolve(), core_file)
    source = core_file.relative_to(root).as_posix()
    rendered = yaml.safe_dump(body, sort_keys=False)
    return f"CAPI=2:\n{_MARKER_PREFIX}{source}\n{rendered}"


def _absolutize_filesets(body: dict[str, Any], root: Path, core_file: Path) -> None:
    filesets = body.get("filesets") or {}
    if not isinstance(filesets, Mapping):
        raise CoreProjectionError(f"filesets is not a mapping in {core_file}")
    for name, fileset in filesets.items():
        if not isinstance(fileset, dict):
            raise CoreProjectionError(f"filesets.{name} is not a mapping in {core_file}")
        files = fileset.get("files")
        if files is None:
            continue
        if not isinstance(files, list):
            raise CoreProjectionError(f"filesets.{name}.files is not an array in {core_file}")
        fileset["files"] = [_absolutize_file_entry(entry, root, core_file) for entry in files]


def _absolutize_file_entry(entry: Any, root: Path, core_file: Path) -> Any:
    if isinstance(entry, str):
        return _absolute_fileset_path(entry, root, core_file)
    if not isinstance(entry, Mapping) or len(entry) != 1:
        raise CoreProjectionError(f"invalid fileset entry in {core_file}: {entry!r}")
    path, raw_attrs = next(iter(entry.items()))
    attrs = dict(raw_attrs) if isinstance(raw_attrs, Mapping) else raw_attrs
    if isinstance(attrs, dict) and isinstance(attrs.get("include_path"), str):
        attrs["include_path"] = _absolute_fileset_path(attrs["include_path"], root, core_file)
    return {_absolute_fileset_path(str(path), root, core_file): attrs}


def _absolute_fileset_path(raw: str, root: Path, core_file: Path) -> str:
    if "?" in raw or "$" in raw:
        raise CoreProjectionError(
            f"native-core isolation requires literal fileset paths; found {raw!r} in {core_file}"
        )
    path = Path(raw)
    return str(path if path.is_absolute() else (root / path).resolve(strict=False))


def _write_projection(destination: Path, content: str) -> bool:
    if destination.exists():
        if not _is_owned_projection(destination):
            raise CoreProjectionError(
                f"refusing to overwrite non-Booley file at projected core path {destination}"
            )
        if destination.read_text(encoding="utf-8") == content:
            return False
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        Path(temporary).chmod(0o644)
        Path(temporary).replace(destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


def _is_owned_projection(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as stream:
            stream.readline()
            return stream.readline().startswith(_MARKER_PREFIX)
    except OSError:
        return False


def _under_ignore(path: Path, core_root: Path) -> bool:
    directory = path.parent
    while True:
        if (directory / "FUSESOC_IGNORE").is_file():
            return True
        if directory in (core_root, directory.parent):
            return False
        directory = directory.parent
