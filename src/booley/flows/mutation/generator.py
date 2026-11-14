"""Seeded, stable-ID Mutants in the text-replacement applier's form (ADR 0088).

Site finding lives in ``operators``; this module owns the Mutant record, the
stable ID, and the seeded round-robin selection.  It never imports pyslang.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from booley.core.boundary import BoundaryError, require_int, require_positive_int
from booley.flows.mutation.operators import (
    OPERATOR_NAMES,
    PYSLANG_MISSING_MESSAGE,
    DesignFile,
    MissingPyslangError,
    MutantGeneratorError,
    Site,
    SourceParseError,
    find_design_sites,
    find_sites,
)

# Bump whenever the operator table, site rules, anchoring, ID recipe, or selection
# rule changes: the same inputs give the same Mutants only for the same version.
GENERATOR_VERSION = "7"

_ID_LENGTH = 20  # hex characters of the SHA-256 digest kept in a Mutant ID


def parser_version() -> str:
    """The installed pyslang version, which a run records as part of its identity."""
    try:
        return metadata.version("pyslang")
    except metadata.PackageNotFoundError as exc:
        raise MissingPyslangError(PYSLANG_MISSING_MESSAGE) from exc


@dataclass(frozen=True)
class Mutant:
    """One Mutant; ``file``, ``line``, ``original_code`` and ``mutated_code`` are applier input."""

    id: str
    operator: str
    file: str
    line: int
    original_code: str
    mutated_code: str
    start: int  # byte offsets of original_code in the file
    end: int


@dataclass(frozen=True)
class MutantSelection:
    """Every candidate Mutant in pick order, and how many were asked for.

    The first ``requested`` are the selection; the rest are the reserve that replaces a
    Mutant a run drops, in the same order.
    """

    ranked: tuple[Mutant, ...]
    requested: int
    # Why the Target's elaboration was not used and each scope file was elaborated alone;
    # None when it was used, or when no design was given.
    fallback: str | None = None

    @property
    def mutants(self) -> tuple[Mutant, ...]:
        """The selected Mutants: the first ``requested`` candidates."""
        return self.ranked[: self.requested]


@dataclass(frozen=True)
class TargetDesign:
    """What the Target's build compiles: its top modules, HDL sources and parameter overrides."""

    top: tuple[str, ...]
    # HDL sources in Target order, relative to the root (absolute outside the Project).
    files: tuple[str, ...]
    parameters: Mapping[str, str]  # override value text by parameter name


def generate_mutants(
    root: Path,
    files: Sequence[str],
    count: int,
    seed: int,
    *,
    defines: Mapping[str, str] = MappingProxyType({}),
    include_dirs: Sequence[str] = (),
    design: TargetDesign | None = None,
) -> MutantSelection:
    """Select up to ``count`` Mutants of ``files`` (relative to ``root``) for ``seed``.

    ``defines`` and ``include_dirs`` (relative to ``root``) configure the
    preprocessor like the build, so the live ``ifdef`` branch is mutated.  With a
    ``design``, the facts come from elaborating the whole Target; if that fails, or
    without a design, each file is elaborated alone and the selection says why.
    """
    require_positive_int(count, field="count")
    if require_int(seed, field="seed") < 0:
        raise BoundaryError(f"seed must be a non-negative integer, got {seed!r}")
    if not files:
        raise BoundaryError("files must name at least one source file")
    names = _relative_paths(root, files, field="files")
    include_paths = [root / rel for rel in _relative_paths(root, include_dirs, "include_dirs")]
    sources = {rel: _read_source(root, rel) for rel in names}
    fallback = None
    if design is None:
        sites = _per_file_sites(root, sources, defines, include_paths)
    else:
        found = find_design_sites(
            _design_files(root, sources, design),
            design.top,
            [f"{name}={value}" for name, value in design.parameters.items()],
            defines,
            include_paths,
        )
        sites, fallback = found.sites, found.fallback
    candidates: dict[str, Mutant] = {}  # by ID: identical sites collapse to one Mutant
    for rel, source in sources.items():
        for site in sites[rel]:
            if (mutant := _to_mutant(source, rel, site)) is not None:
                candidates[mutant.id] = mutant
    return _select(list(candidates.values()), count, seed, fallback)


def _relative_paths(root: Path, paths: Sequence[str], field: str) -> Sequence[str]:
    """Return ``paths`` if they are unique, relative POSIX paths that stay under ``root``."""
    if len(set(paths)) != len(paths):
        raise BoundaryError(f"{field} must not repeat a path")
    base = root.resolve()
    for rel in paths:
        if (
            PurePosixPath(rel).is_absolute()
            or "\\" in rel
            or not (base / rel).resolve().is_relative_to(base)
        ):
            raise BoundaryError(f"{field} entry must be a relative path under the root: {rel!r}")
    return paths


def _read_source(root: Path, rel: str) -> bytes:
    """The bytes of scope file ``rel``."""
    try:
        return (root / rel).read_bytes()
    except OSError as exc:
        raise MutantGeneratorError(f"cannot read source file {rel}: {exc}") from exc


def _per_file_sites(
    root: Path,
    sources: Mapping[str, bytes],
    defines: Mapping[str, str],
    include_dirs: Sequence[Path],
) -> dict[str, list[Site]]:
    """The sites of each file, each elaborated alone; every file that does not parse is reported."""
    sites: dict[str, list[Site]] = {}
    failures: list[tuple[str, str]] = []
    for rel, source in sources.items():
        try:
            sites[rel] = find_sites(source, rel, root / rel, defines, include_dirs)
        except SourceParseError as exc:
            failures.extend(exc.failures)
    if failures:
        raise SourceParseError(failures)
    return sites


def _design_files(
    root: Path, sources: Mapping[str, bytes], design: TargetDesign
) -> list[DesignFile]:
    """The Target's files in Target order: scope files with their bytes, the rest as context.

    A scope file the Target does not list is added at the end: it still gets sites.
    """
    by_path = {(root / rel).resolve(): rel for rel in sources}
    files: list[DesignFile] = []
    for entry in design.files:
        path = root / entry
        rel = by_path.pop(path.resolve(), None)
        if rel is None:
            files.append(DesignFile(entry, path, None, is_scope=False))
        else:
            files.append(DesignFile(rel, path, sources[rel], is_scope=True))
    files.extend(
        DesignFile(rel, root / rel, sources[rel], is_scope=True) for rel in by_path.values()
    )
    return files


def _to_mutant(source: bytes, rel: str, site: Site) -> Mutant | None:
    """Anchor a site for the applier and give it a stable ID; None if it cannot be anchored."""
    start, end = _anchor(source, site.start, site.end)
    try:
        original = source[start:end].decode("utf-8")
        mutated_bytes = (
            source[start : site.start] + site.replacement.encode() + source[site.end : end]
        )
        mutated = mutated_bytes.decode("utf-8")
        site_text = source[site.start : site.end].decode("utf-8")
    except UnicodeDecodeError:
        return None  # a non-UTF-8 byte in the text: the applier cannot match it
    # The ID covers the minimal site, so widening for the applier never changes it.
    relative = (site.start - site.unit_start, site.end - site.unit_start)
    fields = (site.operator, rel, site.unit_key, *map(str, relative), site_text, site.replacement)
    digest = hashlib.sha256("\0".join(fields).encode()).hexdigest()
    line = source.count(b"\n", 0, start) + 1
    return Mutant(digest[:_ID_LENGTH], site.operator, rel, line, original, mutated, start, end)


def _anchor(source: bytes, start: int, end: int) -> tuple[int, int]:
    """Return the span the applier can match: the site, or its whole line(s) if repeated.

    The applier needs the original text exactly once among occurrences that
    start on its line.  The full line span from a line start always is: a second
    occurrence starting at column k > 0 would need its text, which holds no
    newline before the line's own end, to match the line's newline.
    """
    site = source[start:end]
    line_start = source.rfind(b"\n", 0, start) + 1
    line_end = source.find(b"\n", start)
    repeats = 0
    offset = source.find(site, line_start)
    while 0 <= offset <= (line_end if line_end >= 0 else len(source)) and repeats < 2:
        repeats += 1
        offset = source.find(site, offset + 1)
    if repeats == 1:
        return start, end
    last_line_end = source.find(b"\n", end)
    return line_start, len(source) if last_line_end < 0 else last_line_end


def _select(
    candidates: list[Mutant], count: int, seed: int, fallback: str | None
) -> MutantSelection:
    """Rank each operator's candidates by hash(seed, ID) and order them round-robin."""

    def rank(mutant: Mutant) -> tuple[str, str]:
        return hashlib.sha256(f"{seed}\0{mutant.id}".encode()).hexdigest(), mutant.id

    queues = [sorted((m for m in candidates if m.operator == n), key=rank) for n in OPERATOR_NAMES]
    picked = [m for row in zip_longest(*queues) for m in row if m is not None]
    return MutantSelection(tuple(picked), requested=count, fallback=fallback)
