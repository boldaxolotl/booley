"""Describe semantic changes to one Target authoring surface.

An authoring surface is a FuseSoC ``.core`` file or a Project ``tests.toml``
file.  Callers supply the before and after bytes of one surface; this module
parses each side and reports which Targets, fileset declarations, parameter
declarations, and ``tests.toml`` tables were added, modified, or deleted.  It
applies no acceptance policy: callers decide which changes they allow.

Unparseable input and invalid section shapes raise :class:`SurfaceDiffError`.
Malformed values inside a Target body (for example a scalar ``parameters``
entry) reach the FuseSoC helpers unchanged and may raise their own errors,
such as ``TypeError``.  Delta objects are frozen dataclasses, but the parsed
declaration bodies they carry are plain mutable dicts and lists.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, cast

import yaml

from booley.fusesoc import fusesoc_registry
from booley.targets.domain import FuseSocError


class SurfaceDiffError(ValueError):
    """An authoring surface cannot be parsed or has an invalid section shape."""


@dataclass(frozen=True)
class TargetSurfaceFile:
    """Before/current bytes for one authoring surface supplied by the workspace layer."""

    path: str
    baseline: bytes | None
    current: bytes | None


@dataclass(frozen=True)
class TargetDefinition:
    """One Target declaration and its VLNV-qualified identity."""

    canonical: str
    name: str
    body: Any


@dataclass(frozen=True)
class FilesetDefinition:
    """One core-local fileset and the Targets that may select it."""

    key: str
    path: str
    name: str
    body: Any
    referenced_by: tuple[str, ...]


@dataclass(frozen=True)
class ParameterDefinition:
    """One core-local parameter declaration and the Targets that select it."""

    key: str
    path: str
    name: str
    body: Any
    referenced_by: tuple[str, ...]


_Change = TypeVar("_Change")


@dataclass(frozen=True)
class ChangeSet(Generic[_Change]):
    """Rows added, modified, or deleted between two sides of a surface.

    Added and modified rows carry their current value; deleted rows carry their
    baseline value.
    """

    added: tuple[_Change, ...] = ()
    modified: tuple[_Change, ...] = ()
    deleted: tuple[_Change, ...] = ()


@dataclass(frozen=True)
class SurfaceDelta:
    """The semantic change of one or more authoring surfaces."""

    targets: ChangeSet[TargetDefinition] = field(default_factory=ChangeSet)
    test_tables: ChangeSet[str] = field(default_factory=ChangeSet)
    filesets: ChangeSet[FilesetDefinition] = field(default_factory=ChangeSet)
    parameters: ChangeSet[ParameterDefinition] = field(default_factory=ChangeSet)


def _mapping(value: object) -> Mapping[object, Any] | None:
    """Return parsed YAML/TOML data as a mapping, or ``None`` when it is not one."""
    return cast("Mapping[object, Any]", value) if isinstance(value, Mapping) else None


def core_document(content: bytes | None, *, path: str) -> Mapping[str, Any]:
    """Parse one ``.core`` side; an absent side is an empty mapping."""
    if content is None:
        return {}
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise SurfaceDiffError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise SurfaceDiffError(f".core {path} is not a mapping")
    return cast("Mapping[str, Any]", document)


def core_targets(content: bytes | None, *, path: str) -> dict[str, TargetDefinition]:
    """Return one ``.core`` side's Targets keyed by VLNV-qualified identity."""
    document = core_document(content, path=path)
    if not document:
        return {}
    vlnv = document.get("name")
    targets = _mapping(document.get("targets", {}))
    if not isinstance(vlnv, str) or not vlnv:
        raise SurfaceDiffError(f".core {path} has no valid name")
    if targets is None:
        raise SurfaceDiffError(f".core {path} has no mapping-valued targets block")
    result: dict[str, TargetDefinition] = {}
    for name, body in targets.items():
        if not isinstance(name, str):
            raise SurfaceDiffError(f".core {path} contains a non-string Target name")
        canonical = f"{vlnv}#{name}"
        result[canonical] = TargetDefinition(canonical, name, body)
    return result


def core_filesets(content: bytes | None, *, path: str) -> dict[str, Any]:
    """Return one ``.core`` side's fileset bodies keyed by name."""
    document = core_document(content, path=path)
    filesets = _mapping(document.get("filesets", {}) if document else {})
    if filesets is None:
        raise SurfaceDiffError(f".core {path} has no mapping-valued filesets block")
    if any(not isinstance(name, str) for name in filesets):
        raise SurfaceDiffError(f".core {path} contains a non-string fileset name")
    return {str(name): body for name, body in filesets.items()}


def core_parameters(content: bytes | None, *, path: str) -> dict[str, Any]:
    """Return one ``.core`` side's parameter declarations keyed by name."""
    document = core_document(content, path=path)
    parameters = _mapping(document.get("parameters", {}) if document else {})
    if parameters is None:
        raise SurfaceDiffError(f".core {path} has no mapping-valued parameters block")
    if any(not isinstance(name, str) for name in parameters):
        raise SurfaceDiffError(f".core {path} contains a non-string parameter name")
    return {str(name): body for name, body in parameters.items()}


def _target_bodies(
    document: Mapping[str, Any],
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield ``(canonical, body)`` for each Target whose body is a mapping."""
    if not document:
        return
    vlnv = document.get("name")
    targets = _mapping(document.get("targets", {}))
    if not isinstance(vlnv, str) or targets is None:
        return
    for target_name, body in targets.items():
        target_body = _mapping(body)
        if isinstance(target_name, str) and target_body is not None:
            yield f"{vlnv}#{target_name}", cast("Mapping[str, Any]", target_body)


def _fileset_references(content: bytes | None, *, path: str) -> dict[str, tuple[str, ...]]:
    document = core_document(content, path=path)
    references: dict[str, set[str]] = {}
    for canonical, body in _target_bodies(document):
        try:
            selected = fusesoc_registry.target_fileset_definitions(document, body)
        except FuseSocError as exc:
            raise SurfaceDiffError(f".core {path}: {exc}") from exc
        for fileset in selected:
            references.setdefault(fileset, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def fileset_definitions(content: bytes | None, *, path: str) -> dict[str, FilesetDefinition]:
    """Return one ``.core`` side's filesets with the Targets that may select each."""
    filesets = core_filesets(content, path=path)
    references = _fileset_references(content, path=path)
    return {
        name: FilesetDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in filesets.items()
    }


def _parameter_references(content: bytes | None, *, path: str) -> dict[str, tuple[str, ...]]:
    document = core_document(content, path=path)
    references: dict[str, set[str]] = {}
    for canonical, body in _target_bodies(document):
        for parameter in fusesoc_registry.possible_target_parameter_names(body):
            references.setdefault(parameter, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def parameter_definitions(content: bytes | None, *, path: str) -> dict[str, ParameterDefinition]:
    """Return one ``.core`` side's parameters with the Targets that may select each."""
    parameters = core_parameters(content, path=path)
    references = _parameter_references(content, path=path)
    return {
        name: ParameterDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in parameters.items()
    }


def tests_toml_tables(content: bytes | None, *, path: str) -> dict[str, Any]:
    """Return one ``tests.toml`` side's top-level entries; an absent side is empty."""
    if content is None:
        return {}
    try:
        raw = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SurfaceDiffError(f"cannot parse {path}: {exc}") from exc
    return dict(raw)


def changed_rows(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[tuple[str, ...], ...]:
    """Return sorted added, modified, and deleted keys between two mappings."""
    added = tuple(sorted(set(after) - set(before)))
    deleted = tuple(sorted(set(before) - set(after)))
    modified = tuple(sorted(key for key in set(before) & set(after) if before[key] != after[key]))
    return added, modified, deleted


def change_set(
    before: Mapping[str, _Change], after: Mapping[str, _Change], rows: tuple[tuple[str, ...], ...]
) -> ChangeSet[_Change]:
    """Select the changed rows named by :func:`changed_rows` from both sides."""
    added, modified, deleted = rows
    return ChangeSet(
        tuple(after[key] for key in added),
        tuple(after[key] for key in modified),
        tuple(before[key] for key in deleted),
    )


def diff_core_surface(surface: TargetSurfaceFile) -> SurfaceDelta:
    """Describe the Target, fileset, and parameter changes in one ``.core`` file."""
    path = surface.path
    before = core_targets(surface.baseline, path=path)
    after = core_targets(surface.current, path=path)
    target_rows = changed_rows(before, after)
    fileset_rows = changed_rows(
        core_filesets(surface.baseline, path=path), core_filesets(surface.current, path=path)
    )
    before_filesets = fileset_definitions(surface.baseline, path=path)
    after_filesets = fileset_definitions(surface.current, path=path)
    parameter_rows = changed_rows(
        core_parameters(surface.baseline, path=path), core_parameters(surface.current, path=path)
    )
    before_parameters = parameter_definitions(surface.baseline, path=path)
    after_parameters = parameter_definitions(surface.current, path=path)
    return SurfaceDelta(
        targets=change_set(before, after, target_rows),
        filesets=change_set(before_filesets, after_filesets, fileset_rows),
        parameters=change_set(before_parameters, after_parameters, parameter_rows),
    )


def diff_tests_surface(surface: TargetSurfaceFile) -> SurfaceDelta:
    """Describe the top-level table changes in one ``tests.toml`` file."""
    before = tests_toml_tables(surface.baseline, path=surface.path)
    after = tests_toml_tables(surface.current, path=surface.path)
    return SurfaceDelta(test_tables=ChangeSet(*changed_rows(before, after)))


def _merge_changes(
    changes: Iterable[ChangeSet[_Change]], key: Callable[[_Change], Any]
) -> ChangeSet[_Change]:
    rows = tuple(changes)
    return ChangeSet(
        added=tuple(sorted((item for row in rows for item in row.added), key=key)),
        modified=tuple(sorted((item for row in rows for item in row.modified), key=key)),
        deleted=tuple(sorted((item for row in rows for item in row.deleted), key=key)),
    )


def merge_deltas(deltas: Iterable[SurfaceDelta]) -> SurfaceDelta:
    """Combine per-file deltas into one delta with deterministic ordering."""
    rows = tuple(deltas)
    return SurfaceDelta(
        targets=_merge_changes((delta.targets for delta in rows), lambda item: item.canonical),
        test_tables=_merge_changes((delta.test_tables for delta in rows), str),
        filesets=_merge_changes((delta.filesets for delta in rows), lambda item: item.key),
        parameters=_merge_changes((delta.parameters for delta in rows), lambda item: item.key),
    )
