"""Describe semantic changes to one Target authoring surface.

An authoring surface is a FuseSoC ``.core`` file or a Project ``tests.toml``
file.  Callers supply the before and after bytes of one surface; this module
parses both sides and returns an immutable delta: Targets added, deleted, and
modified, the fileset and parameter declarations they reference, and
``tests.toml`` table changes.  It applies no acceptance policy: callers decide
which changes they allow.  It raises :class:`SurfaceDiffError` only when a side
cannot be parsed or has an invalid structure.

:func:`canonical_target_declaration` renders one Target's complete declaration
as stable text, independent of key order, whitespace, and comments.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

import yaml

from booley.fusesoc import fusesoc_registry
from booley.targets.domain import FuseSocError


class SurfaceDiffError(ValueError):
    """An authoring surface cannot be parsed or has an invalid structure."""


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
_Value = TypeVar("_Value")
_Definition = TypeVar("_Definition", "FilesetDefinition", "ParameterDefinition")


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
class Modification(Generic[_Value]):
    """One value as declared before and after a change."""

    before: _Value
    after: _Value


@dataclass(frozen=True)
class ParameterSelectionChange:
    """A parameter or define whose selection by a Target changed.

    ``before`` and ``after`` list the Target's CAPI2 parameter specs that select
    ``name`` (``NAME``, ``NAME=value``, or a conditional expression); an empty
    tuple means the side does not select it.  ``paramtype`` comes from the
    core-local declaration, preferring the current side (``vlogdefine`` marks a
    define); it is ``None`` for a parameter declared by a dependency core.
    """

    name: str
    paramtype: str | None
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class TargetChange:
    """What changed in one Target declared on both sides of a ``.core`` change.

    ``changed_fields`` names every top-level Target key whose value differs; the
    remaining fields interpret the changes FuseSoC gives meaning to.  Fileset
    and parameter definitions are listed when the Target selects them on both
    sides and their declaration body changed.  Test entries live in
    ``tests.toml``; see :meth:`SurfaceDelta.test_changes_for`.
    """

    before: TargetDefinition
    after: TargetDefinition
    changed_fields: tuple[str, ...]
    toplevel: Modification[Any] | None
    parameters: tuple[ParameterSelectionChange, ...]
    filesets_added: tuple[str, ...]
    filesets_removed: tuple[str, ...]
    fileset_definitions: tuple[Modification[FilesetDefinition], ...]
    parameter_definitions: tuple[Modification[ParameterDefinition], ...]

    @property
    def canonical(self) -> str:
        """The VLNV-qualified identity of the changed Target."""
        return self.after.canonical


@dataclass(frozen=True)
class TableChange:
    """One top-level ``tests.toml`` table; ``None`` marks the side lacking it."""

    path: str
    name: str
    before: Any
    after: Any


@dataclass(frozen=True)
class SurfaceDelta:
    """The complete semantic change of one or more authoring surfaces.

    ``targets`` compares Target bodies; ``target_changes`` describes every
    Target declared on both sides whose body or selected input declarations
    changed.  ``test_tables`` names changed ``tests.toml`` tables and
    ``test_table_changes`` carries their contents.
    """

    targets: ChangeSet[TargetDefinition] = field(default_factory=ChangeSet)
    test_tables: ChangeSet[str] = field(default_factory=ChangeSet)
    filesets: ChangeSet[FilesetDefinition] = field(default_factory=ChangeSet)
    parameters: ChangeSet[ParameterDefinition] = field(default_factory=ChangeSet)
    target_changes: tuple[TargetChange, ...] = ()
    test_table_changes: tuple[TableChange, ...] = ()

    def test_changes_for(self, canonical: str) -> tuple[TableChange, ...]:
        """Return table changes keyed by a Target's qualified or bare name."""
        names = {canonical, canonical.rsplit("#", 1)[-1]}
        return tuple(change for change in self.test_table_changes if change.name in names)


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
    return _targets_of(core_document(content, path=path), path)


def core_filesets(content: bytes | None, *, path: str) -> dict[str, Any]:
    """Return one ``.core`` side's fileset bodies keyed by name."""
    return _filesets_of(core_document(content, path=path), path)


def core_parameters(content: bytes | None, *, path: str) -> dict[str, Any]:
    """Return one ``.core`` side's parameter declarations keyed by name."""
    return _parameters_of(core_document(content, path=path), path)


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


def _mapping(value: object) -> Mapping[object, Any] | None:
    """Return parsed YAML/TOML data as a mapping, or ``None`` when it is not one."""
    return cast("Mapping[object, Any]", value) if isinstance(value, Mapping) else None


def _targets_of(document: Mapping[str, Any], path: str) -> dict[str, TargetDefinition]:
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


def _filesets_of(document: Mapping[str, Any], path: str) -> dict[str, Any]:
    filesets = _mapping(document.get("filesets", {}) if document else {})
    if filesets is None:
        raise SurfaceDiffError(f".core {path} has no mapping-valued filesets block")
    if any(not isinstance(name, str) for name in filesets):
        raise SurfaceDiffError(f".core {path} contains a non-string fileset name")
    return {str(name): body for name, body in filesets.items()}


def _parameters_of(document: Mapping[str, Any], path: str) -> dict[str, Any]:
    parameters = _mapping(document.get("parameters", {}) if document else {})
    if parameters is None:
        raise SurfaceDiffError(f".core {path} has no mapping-valued parameters block")
    if any(not isinstance(name, str) for name in parameters):
        raise SurfaceDiffError(f".core {path} contains a non-string parameter name")
    return {str(name): body for name, body in parameters.items()}


def _target_bodies(document: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
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


def _selected_filesets(
    document: Mapping[str, Any], body: Mapping[str, Any], path: str
) -> tuple[str, ...]:
    try:
        return tuple(fusesoc_registry.target_fileset_definitions(document, body))
    except FuseSocError as exc:
        raise SurfaceDiffError(f".core {path}: {exc}") from exc


def _fileset_references(document: Mapping[str, Any], path: str) -> dict[str, tuple[str, ...]]:
    references: dict[str, set[str]] = {}
    for canonical, body in _target_bodies(document):
        for fileset in _selected_filesets(document, body, path):
            references.setdefault(fileset, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def _parameter_references(document: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    references: dict[str, set[str]] = {}
    for canonical, body in _target_bodies(document):
        for parameter in fusesoc_registry.possible_target_parameter_names(body):
            references.setdefault(parameter, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def _fileset_definitions(
    document: Mapping[str, Any], bodies: Mapping[str, Any], path: str
) -> dict[str, FilesetDefinition]:
    references = _fileset_references(document, path)
    return {
        name: FilesetDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in bodies.items()
    }


def _parameter_definitions(
    document: Mapping[str, Any], bodies: Mapping[str, Any], path: str
) -> dict[str, ParameterDefinition]:
    references = _parameter_references(document)
    return {
        name: ParameterDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in bodies.items()
    }


@dataclass(frozen=True)
class _CoreSide:
    """One parsed ``.core`` side with every Target-relevant declaration."""

    document: Mapping[str, Any]
    targets: dict[str, TargetDefinition]
    filesets: dict[str, FilesetDefinition]
    parameters: dict[str, ParameterDefinition]


def _core_sides(surface: TargetSurfaceFile) -> tuple[_CoreSide, _CoreSide]:
    """Parse both sides, checking each section on both sides before the next."""
    path = surface.path
    documents = (
        core_document(surface.baseline, path=path),
        core_document(surface.current, path=path),
    )
    targets = [_targets_of(document, path) for document in documents]
    fileset_bodies = [_filesets_of(document, path) for document in documents]
    filesets = [
        _fileset_definitions(document, bodies, path)
        for document, bodies in zip(documents, fileset_bodies, strict=True)
    ]
    parameter_bodies = [_parameters_of(document, path) for document in documents]
    parameters = [
        _parameter_definitions(document, bodies, path)
        for document, bodies in zip(documents, parameter_bodies, strict=True)
    ]
    before, after = (
        _CoreSide(documents[side], targets[side], filesets[side], parameters[side])
        for side in (0, 1)
    )
    return before, after


def _change_set(
    before: Mapping[str, _Change], after: Mapping[str, _Change], keys: tuple[tuple[str, ...], ...]
) -> ChangeSet[_Change]:
    added, modified, deleted = keys
    return ChangeSet(
        tuple(after[key] for key in added),
        tuple(after[key] for key in modified),
        tuple(before[key] for key in deleted),
    )


def diff_core_surface(surface: TargetSurfaceFile) -> SurfaceDelta:
    """Describe every Target, fileset, and parameter change in one ``.core`` file."""
    before, after = _core_sides(surface)

    def bodies(definitions: Mapping[str, Any]) -> dict[str, Any]:
        return {key: definition.body for key, definition in definitions.items()}

    target_changes = tuple(
        change
        for canonical in sorted(set(before.targets) & set(after.targets))
        if (change := _target_change(before, after, canonical, surface.path)) is not None
    )
    return SurfaceDelta(
        targets=_change_set(
            before.targets,
            after.targets,
            changed_rows(bodies(before.targets), bodies(after.targets)),
        ),
        filesets=_change_set(
            before.filesets,
            after.filesets,
            changed_rows(bodies(before.filesets), bodies(after.filesets)),
        ),
        parameters=_change_set(
            before.parameters,
            after.parameters,
            changed_rows(bodies(before.parameters), bodies(after.parameters)),
        ),
        target_changes=target_changes,
    )


def _as_mapping(body: Any) -> Mapping[str, Any]:
    """Treat a non-mapping Target body as declaring nothing."""
    return cast("Mapping[str, Any]", _mapping(body) or {})


def _target_inputs(
    document: Mapping[str, Any], local_parameters: Container[str], body: Any, path: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the local filesets and parameters one Target body selects."""
    target_body = _as_mapping(body)
    filesets = _selected_filesets(document, target_body, path)
    parameter_names = fusesoc_registry.possible_target_parameter_names(target_body)
    return filesets, tuple(name for name in parameter_names if name in local_parameters)


def _modified_inputs(
    before: Mapping[str, _Definition], after: Mapping[str, _Definition], names: set[str]
) -> tuple[Modification[_Definition], ...]:
    return tuple(
        Modification(before[name], after[name])
        for name in sorted(names)
        if before[name].body != after[name].body
    )


def _target_change(
    before: _CoreSide, after: _CoreSide, canonical: str, path: str
) -> TargetChange | None:
    """Describe one Target declared on both sides, or ``None`` when unchanged."""
    old, new = before.targets[canonical], after.targets[canonical]
    old_filesets, old_parameters = _target_inputs(
        before.document, before.parameters, old.body, path
    )
    new_filesets, new_parameters = _target_inputs(after.document, after.parameters, new.body, path)
    fileset_definitions = _modified_inputs(
        before.filesets, after.filesets, set(old_filesets) & set(new_filesets)
    )
    parameter_definitions = _modified_inputs(
        before.parameters, after.parameters, set(old_parameters) & set(new_parameters)
    )
    if old.body == new.body and not fileset_definitions and not parameter_definitions:
        return None
    old_body, new_body = _as_mapping(old.body), _as_mapping(new.body)
    toplevel = (old_body.get("toplevel"), new_body.get("toplevel"))
    return TargetChange(
        before=old,
        after=new,
        changed_fields=tuple(
            sorted(
                key
                for key in {*old_body, *new_body}
                if key not in old_body or key not in new_body or old_body[key] != new_body[key]
            )
        ),
        toplevel=Modification(*toplevel) if toplevel[0] != toplevel[1] else None,
        parameters=_parameter_selection_changes(before, after, old_body, new_body),
        filesets_added=tuple(name for name in new_filesets if name not in old_filesets),
        filesets_removed=tuple(name for name in old_filesets if name not in new_filesets),
        fileset_definitions=fileset_definitions,
        parameter_definitions=parameter_definitions,
    )


def _parameter_selections(body: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Map each parameter name a Target may select to the specs selecting it."""
    selections: dict[str, list[str]] = {}
    # YAML may hold non-string list items despite the registry's annotation.
    specs = cast("Sequence[object]", fusesoc_registry.target_parameter_specs(body))
    for spec in specs:
        if not isinstance(spec, str):
            continue
        for name in fusesoc_registry.possible_target_parameter_names({"parameters": [spec]}):
            selections.setdefault(name, []).append(spec)
    return {name: tuple(selected) for name, selected in selections.items()}


def _paramtype(name: str, *sides: _CoreSide) -> str | None:
    for side in sides:
        definition = side.parameters.get(name)
        body = _mapping(definition.body) if definition is not None else None
        if body is not None:
            paramtype = body.get("paramtype")
            return paramtype if isinstance(paramtype, str) else None
    return None


def _parameter_selection_changes(
    before: _CoreSide,
    after: _CoreSide,
    old_body: Mapping[str, Any],
    new_body: Mapping[str, Any],
) -> tuple[ParameterSelectionChange, ...]:
    old, new = _parameter_selections(old_body), _parameter_selections(new_body)
    return tuple(
        ParameterSelectionChange(
            name, _paramtype(name, after, before), old.get(name, ()), new.get(name, ())
        )
        for name in sorted({*old, *new})
        if old.get(name, ()) != new.get(name, ())
    )


def diff_tests_surface(surface: TargetSurfaceFile) -> SurfaceDelta:
    """Describe every top-level table change in one ``tests.toml`` file."""
    before = tests_toml_tables(surface.baseline, path=surface.path)
    after = tests_toml_tables(surface.current, path=surface.path)
    added, modified, deleted = changed_rows(before, after)
    changes = tuple(
        TableChange(surface.path, name, before.get(name), after.get(name))
        for name in sorted((*added, *modified, *deleted))
    )
    return SurfaceDelta(
        test_tables=ChangeSet(added, modified, deleted), test_table_changes=changes
    )


def diff_surface(surface: TargetSurfaceFile) -> SurfaceDelta:
    """Describe one authoring surface; any other file yields an empty delta."""
    if Path(surface.path).suffix.casefold() == ".core":
        return diff_core_surface(surface)
    if Path(surface.path).name == "tests.toml":
        return diff_tests_surface(surface)
    return SurfaceDelta()


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
        target_changes=tuple(
            sorted(
                (change for delta in rows for change in delta.target_changes),
                key=lambda change: change.canonical,
            )
        ),
        test_table_changes=tuple(
            sorted(
                (change for delta in rows for change in delta.test_table_changes),
                key=lambda change: (change.name, change.path),
            )
        ),
    )


def diff_surfaces(files: Iterable[TargetSurfaceFile]) -> SurfaceDelta:
    """Describe several authoring surfaces as one merged delta."""
    return merge_deltas(diff_surface(surface) for surface in files)


def canonical_target_declaration(
    core: bytes,
    *,
    path: str,
    target: str,
    tests: bytes | None = None,
    tests_path: str = "tests.toml",
) -> str:
    """Render one Target's complete declaration as canonical JSON text.

    ``target`` is the Target's bare or VLNV-qualified name in ``core``.  The
    declaration holds the Target body, the local fileset and parameter
    declarations it selects, and any ``tests`` table keyed by its qualified or
    bare name.  Key order, whitespace, and comments in either source do not
    affect the result.
    """
    document = core_document(core, path=path)
    targets = _targets_of(document, path)
    definition = targets.get(target) or next(
        (item for item in targets.values() if item.name == target), None
    )
    if definition is None:
        raise SurfaceDiffError(f".core {path} does not declare Target {target!r}")
    filesets = _filesets_of(document, path)
    parameters = _parameters_of(document, path)
    selected_filesets, selected_parameters = _target_inputs(
        document, parameters, definition.body, path
    )
    tables = tests_toml_tables(tests, path=tests_path)
    declaration = {
        "target": definition.canonical,
        "body": definition.body,
        "filesets": {name: filesets[name] for name in selected_filesets},
        "parameters": {name: parameters[name] for name in selected_parameters},
        "tests": {
            name: tables[name]
            for name in (definition.canonical, definition.name)
            if name in tables
        },
    }
    return json.dumps(
        _canonical_value(declaration), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical_value(value: Any) -> Any:
    """Convert parsed YAML/TOML values into deterministic JSON-compatible values."""
    mapping = _mapping(value)
    if mapping is not None:
        return {_canonical_key(key): _canonical_value(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        return [_canonical_value(item) for item in cast("Iterable[object]", value)]
    if isinstance(value, set | frozenset):
        members = cast("Iterable[object]", value)
        return {
            "!set": sorted(json.dumps(_canonical_value(item), sort_keys=True) for item in members)
        }
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return f"!{type(value).__name__}:{value!r}"


def _canonical_key(key: Any) -> str:
    return key if isinstance(key, str) else f"!{type(key).__name__}:{key!r}"
