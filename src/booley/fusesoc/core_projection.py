"""Stealth-only projection of authored FuseSoC cores into the Project Root.

The authoritative cores stay versioned under ``.booley/cores/``.  A
small generated copy at the RTL repository root gives FuseSoC the repository
itself as the core root, so filesets can name pristine upstream sources
directly instead of reaching them through a farm of resolution symlinks.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from booley.core.booley_dir import BOOLEY_DIR_NAME, in_checkout_booley_dir
from booley.core.booley_dir_links import resolve_through_entry

PROJECTED_CORE_PREFIX = ".booley-projected-"
PROJECTED_CORE_GLOB = f"{PROJECTED_CORE_PREFIX}*.core"
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
    config = in_checkout_booley_dir(Path(project_root)) / "booley.toml"
    try:
        with config.open("rb") as stream:
            stealth = tomllib.load(stream).get("stealth")
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return dict(stealth) if isinstance(stealth, dict) else {}


STATE_CORES_SUBDIR = "cores"
"""Stealth authored-cores subdirectory of the Booley Directory (ADR 0036)."""


def stealth_core_root(project_root: Path | str) -> Path:
    """Return the stealth authored-cores root, ``<Booley Directory>/cores`` (ADR 0036).

    The Booley Directory is the checkout's own (``[project].dir``, else ``.booley``).
    Purely structural -- no ``BOOLEY_DIR`` or process cache -- so discovery stays a
    deterministic function of the tree it is handed.
    """
    return in_checkout_booley_dir(Path(project_root)) / STATE_CORES_SUBDIR


def authoritative_cores(project_root: Path | str) -> tuple[Path, ...]:
    """Return projection-eligible cores under the stealth core root."""
    core_root = stealth_core_root(project_root)
    if not core_root.is_dir() or (core_root / "FUSESOC_IGNORE").is_file():
        return ()
    cores = (path for path in core_root.rglob("*.core") if not _under_ignore(path, core_root))
    return tuple(sorted(cores))


def stealth_core_relative(project_root: Path | str, path: Path) -> str | None:
    """Return *path* below the stealth core root as POSIX text, or ``None``.

    ``<root>/.booley`` may be a symlink to another directory, so the same core
    is spelled ``<root>/.booley/cores/x.core`` by callers that walk the Project
    and ``<target>/cores/x.core`` by those that hold a resolved path (the Target
    catalog, the core-library plan).  Both spellings of the root are accepted;
    the lexical one is tried first, so a stealth core in the plain layout costs
    no ``resolve()`` (any other path pays one).
    *path* itself is never resolved: a ``.core`` link inside the root that
    points elsewhere stays outside it.
    """
    core_root = stealth_core_root(project_root)
    for base in (core_root, core_root.resolve()):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


def lexical_stealth_path(project_root: Path | str, path: Path) -> Path:
    """Re-spell a stealth core path as ``<stealth core root>/<rel>``.

    Paths outside the stealth core root are returned unchanged.
    """
    relative = stealth_core_relative(project_root, path)
    if relative is None:
        return path
    return stealth_core_root(project_root) / relative


def _require_stealth_relative(project_root: Path | str, core_file: Path) -> str:
    """Return the stealth-root-relative path of *core_file* or refuse it."""
    relative = stealth_core_relative(project_root, core_file)
    if relative is None:
        raise CoreProjectionError(f"core is outside the stealth core root: {core_file}")
    return relative


def projected_core_path(project_root: Path | str, core_file: Path) -> Path:
    """Return the deterministic root-level projection path for *core_file*."""
    root = Path(project_root)
    relative = _require_stealth_relative(root, core_file)
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
    return in_checkout_booley_dir(Path(project_root)) / "tmp" / "fusesoc-isolated-cores"


def isolated_core_path(project_root: Path | str, core_file: Path) -> Path:
    """Return the private-registry projection for one authoritative core."""
    root = Path(project_root)
    relative = _require_stealth_relative(root, core_file)
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
    issues = _projection_drift(expected)
    for candidate in root.glob(PROJECTED_CORE_GLOB):
        if candidate not in expected and _is_owned_projection(candidate):
            issues.append(f"stale {candidate.name}")
    return tuple(sorted(issues))


def registry_issues(project_root: Path | str) -> tuple[str, ...]:
    """Describe what :func:`reconcile_isolated_registry` would change, without writes.

    Empty unless stealth resolution excludes native cores, the only case that
    uses the private registry.
    """
    root = Path(project_root)
    if not native_cores_ignored(root):
        return ()
    registry = isolated_registry_root(root)
    if not registry.is_dir():
        return ("missing private registry",)
    expected = _expected_isolated_cores(root)
    issues = _projection_drift(expected)
    issues += [
        f"stale {candidate.name}"
        if _is_owned_projection(candidate)
        else f"foreign {candidate.name}"
        for candidate in registry.glob("*.core")
        if candidate not in expected
    ]
    if (registry / "FUSESOC_IGNORE").exists():
        issues.append("FUSESOC_IGNORE blocks the private registry")
    return tuple(sorted(issues))


def _projection_drift(expected: Mapping[Path, str]) -> list[str]:
    """Expected projections that are missing, foreign or stale on disk."""
    issues: list[str] = []
    for destination, content in expected.items():
        if not destination.exists():
            issues.append(f"missing {destination.name}")
        elif not _is_owned_projection(destination):
            issues.append(f"foreign file blocks {destination.name}")
        elif destination.read_text(encoding="utf-8") != content:
            issues.append(f"stale {destination.name}")
    return issues


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
    body = _parse_rebasable_core(core_file)
    resolved_root = root.resolve()
    _rewrite_fileset_paths(
        body,
        core_file,
        lambda raw, _is_file: _absolute_fileset_path(raw, resolved_root, core_file),
    )
    source = core_file.relative_to(root).as_posix()
    rendered = yaml.safe_dump(body, sort_keys=False)
    return f"CAPI=2:\n{_MARKER_PREFIX}{source}\n{rendered}"


def _parse_rebasable_core(core_file: Path) -> dict[str, Any]:
    """Parse an authored core and refuse any key whose paths cannot be rebased."""
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
    return body


def render_private_mirror(project_root: Path | str, mirror_root: Path) -> dict[Path, Path]:
    """Write a private core registry that names in-checkout files relatively.

    The shared isolated registry names every source by absolute path, so
    FuseSoC exports nothing and a build that edits its export would edit the
    checkout.  Each authored core is rendered here with its in-checkout fileset
    files copied under *mirror_root* at their Project-relative paths and named
    relatively, so FuseSoC exports them into the build.  Files that resolve
    outside the checkout stay absolute (and uncopied).

    *mirror_root* must not exist: callers hand over a freshly emptied build
    folder, and a leftover mirror is an error rather than silent reuse.

    Returns each resolved authored core mapped to its mirror core.
    """
    mirror_root.mkdir(parents=True, exist_ok=False)
    return _mirror_cores(project_root, mirror_root, write=True)


def describe_private_mirror(project_root: Path | str, mirror_root: Path) -> dict[Path, Path]:
    """Return what :func:`render_private_mirror` would map, writing nothing.

    Every authored core is still parsed and rewritten, so a core the real
    render rejects (generators, ``$``/``?`` paths, malformed filesets, a
    fileset entry that is not a regular file) is rejected here with the same
    error.
    """
    return _mirror_cores(project_root, mirror_root, write=False)


def _mirror_cores(project_root: Path | str, mirror_root: Path, *, write: bool) -> dict[Path, Path]:
    root = Path(project_root)
    resolved_root = root.resolve()
    mirrored: dict[Path, Path] = {}
    for core_file in authoritative_cores(root):
        relative = _require_stealth_relative(root, core_file)
        body = _parse_rebasable_core(core_file)
        _rewrite_fileset_paths(
            body, core_file, _mirror_rewriter(resolved_root, mirror_root, core_file, copy=write)
        )
        source = core_file.relative_to(root).as_posix()
        destination = mirror_root / f"{_ISOLATED_CORE_PREFIX}{quote(relative, safe='')}"
        if write:
            rendered = yaml.safe_dump(body, sort_keys=False)
            destination.write_text(
                f"CAPI=2:\n{_MARKER_PREFIX}{source}\n{rendered}", encoding="utf-8"
            )
        mirrored[core_file.resolve()] = destination
    return mirrored


def _mirror_rewriter(
    resolved_root: Path, mirror_root: Path, core_file: Path, *, copy: bool
) -> Callable[[str, bool], str]:
    """Return a path rewriter that copies in-checkout files under *mirror_root*."""

    def rewrite(raw: str, is_file: bool) -> str:
        absolute = _absolute_fileset_path(raw, resolved_root, core_file)
        if Path(raw).is_absolute():
            return absolute  # an absolute name is the author's own boundary choice
        try:
            relative = Path(absolute).relative_to(resolved_root)
        except ValueError:
            return absolute  # resolves outside the checkout: never copied, never mutable
        if is_file:
            _copy_into_mirror(Path(absolute), mirror_root / relative, core_file, copy=copy)
        return relative.as_posix()

    return rewrite


def _copy_into_mirror(source: Path, destination: Path, core_file: Path, *, copy: bool) -> None:
    """Copy one regular file; a missing source is left for the source preflight.

    With ``copy`` false nothing is written but the same refusals apply, so a
    dry-run preview rejects exactly the entries the real render rejects.
    """
    if not source.exists():
        return
    if not source.is_file():
        raise CoreProjectionError(f"mirrored fileset entry is not a regular file: {source}")
    if not copy:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except OSError as exc:
        raise CoreProjectionError(f"could not mirror {source} for {core_file}: {exc}") from exc


def _rewrite_fileset_paths(
    body: dict[str, Any], core_file: Path, rewrite: Callable[[str, bool], str]
) -> None:
    """Rewrite every fileset file name and ``include_path`` through *rewrite*.

    *rewrite* receives the raw text and whether it names a file (``True``) or an
    include directory (``False``).
    """
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
        fileset["files"] = [_rewrite_file_entry(entry, core_file, rewrite) for entry in files]


def _rewrite_file_entry(entry: Any, core_file: Path, rewrite: Callable[[str, bool], str]) -> Any:
    if isinstance(entry, str):
        return rewrite(entry, True)
    if not isinstance(entry, Mapping) or len(entry) != 1:
        raise CoreProjectionError(f"invalid fileset entry in {core_file}: {entry!r}")
    path, raw_attrs = next(iter(entry.items()))
    attrs = dict(raw_attrs) if isinstance(raw_attrs, Mapping) else raw_attrs
    if isinstance(attrs, dict) and isinstance(attrs.get("include_path"), str):
        attrs["include_path"] = rewrite(attrs["include_path"], False)
    return {rewrite(str(path), True): attrs}


def _absolute_fileset_path(raw: str, root: Path, core_file: Path) -> str:
    if "?" in raw or "$" in raw:
        raise CoreProjectionError(
            f"native-core isolation requires literal fileset paths; found {raw!r} in {core_file}"
        )
    path = Path(raw)
    # A file spelled through a symlinked Booley Directory keeps its
    # ``<root>/.booley/...`` spelling; the physical target would read as
    # escaping the Project.
    absolute = (
        path if path.is_absolute() else resolve_through_entry(root / BOOLEY_DIR_NAME, root / path)
    )
    # FuseSoC 2.4.7 parses fileset paths as expressions and rejects the
    # backslashes emitted by Windows' ``str(Path)`` representation.
    return absolute.as_posix()


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
