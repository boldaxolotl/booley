"""Validate Ticket-authored Target transitions against destination Git trees.

The module is the single seam for Target Plan policy.  Callers provide the
authoring repositories and their Git-visible paths; the implementation owns
semantic ``.core`` comparison, owned ``tests.toml`` comparison, selector
canonicalization, Target-owned fileset and parameter coverage, Criterion
coverage, and derived removals.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import yaml

from booley.config.project_config import TEST_LISTS_TABLE
from booley.core.models import TargetPlan, TargetPlanEntry, TargetPlanError, TargetPlanRole
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

from .acceptance_targets import canonical_acceptance_bindings, criterion_targets
from .target_surface_edit import (
    TargetSurfaceEditError,
    only_authorized_core_additions,
    only_authorized_insertions,
    toml_table_spans,
    validate_new_core_surface,
)

if TYPE_CHECKING:
    from .ticket_document import TicketSpec


class TargetPlanValidationError(ValueError):
    """A Target Plan does not describe the authoring delta exactly."""


@dataclass(frozen=True)
class TargetPlanAnalysis:
    """Canonical Target Plan data derived from the Ticket and its pinned commits."""

    plan: TargetPlan | None
    removal_targets: tuple[str, ...]
    authored_targets: tuple[str, ...]
    authored_filesets: tuple[str, ...] = ()
    authored_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TargetDefinition:
    canonical: str
    name: str
    body: Any


@dataclass(frozen=True)
class _FilesetDefinition:
    """One core-local fileset and the Targets that may select it."""

    key: str
    path: str
    name: str
    body: Any
    referenced_by: tuple[str, ...]


@dataclass(frozen=True)
class _ParameterDefinition:
    """One core-local parameter declaration and the Targets that select it."""

    key: str
    path: str
    name: str
    body: Any
    referenced_by: tuple[str, ...]


_Change = TypeVar("_Change")


@dataclass(frozen=True)
class _ChangeSet(Generic[_Change]):
    added: tuple[_Change, ...] = ()
    modified: tuple[_Change, ...] = ()
    deleted: tuple[_Change, ...] = ()


@dataclass(frozen=True)
class _SurfaceDelta:
    targets: _ChangeSet[_TargetDefinition] = field(default_factory=_ChangeSet)
    test_tables: _ChangeSet[str] = field(default_factory=_ChangeSet)
    filesets: _ChangeSet[_FilesetDefinition] = field(default_factory=_ChangeSet)
    parameters: _ChangeSet[_ParameterDefinition] = field(default_factory=_ChangeSet)


@dataclass(frozen=True)
class TargetSurfaceFile:
    """Before/current bytes for one authoring surface supplied by the workspace layer."""

    path: str
    baseline: bytes | None
    current: bytes | None


def _core_document(content: bytes | None, *, path: str) -> Mapping[str, Any]:
    if content is None:
        return {}
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise TargetPlanValidationError(f"cannot parse .core {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TargetPlanValidationError(f".core {path} is not a mapping")
    return document


def _core_targets(content: bytes | None, *, path: str) -> dict[str, _TargetDefinition]:
    document = _core_document(content, path=path)
    if not document:
        return {}
    vlnv = document.get("name")
    targets = document.get("targets", {})
    if not isinstance(vlnv, str) or not vlnv:
        raise TargetPlanValidationError(f".core {path} has no valid name")
    if not isinstance(targets, Mapping):
        raise TargetPlanValidationError(f".core {path} has no mapping-valued targets block")
    result = {}
    for name, body in targets.items():
        if not isinstance(name, str):
            raise TargetPlanValidationError(f".core {path} contains a non-string Target name")
        canonical = f"{vlnv}#{name}"
        result[canonical] = _TargetDefinition(canonical, name, body)
    return result


def _core_filesets(content: bytes | None, *, path: str) -> dict[str, Any]:
    document = _core_document(content, path=path)
    filesets = document.get("filesets", {}) if document else {}
    if not isinstance(filesets, Mapping):
        raise TargetPlanValidationError(f".core {path} has no mapping-valued filesets block")
    if any(not isinstance(name, str) for name in filesets):
        raise TargetPlanValidationError(f".core {path} contains a non-string fileset name")
    return dict(filesets)


def _core_parameters(content: bytes | None, *, path: str) -> dict[str, Any]:
    document = _core_document(content, path=path)
    parameters = document.get("parameters", {}) if document else {}
    if not isinstance(parameters, Mapping):
        raise TargetPlanValidationError(f".core {path} has no mapping-valued parameters block")
    if any(not isinstance(name, str) for name in parameters):
        raise TargetPlanValidationError(f".core {path} contains a non-string parameter name")
    return dict(parameters)


def _fileset_references(content: bytes | None, *, path: str) -> dict[str, tuple[str, ...]]:
    document = _core_document(content, path=path)
    if not document:
        return {}
    vlnv = document.get("name")
    targets = document.get("targets", {})
    if not isinstance(vlnv, str) or not isinstance(targets, Mapping):
        return {}
    references: dict[str, set[str]] = {}
    for target_name, body in targets.items():
        if not isinstance(target_name, str) or not isinstance(body, Mapping):
            continue
        canonical = f"{vlnv}#{target_name}"
        try:
            selected = fusesoc_registry.target_fileset_definitions(document, body)
        except FuseSocError as exc:
            raise TargetPlanValidationError(f".core {path}: {exc}") from exc
        for fileset in selected:
            references.setdefault(fileset, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def _fileset_definitions(content: bytes | None, *, path: str) -> dict[str, _FilesetDefinition]:
    filesets = _core_filesets(content, path=path)
    references = _fileset_references(content, path=path)
    return {
        name: _FilesetDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in filesets.items()
    }


def _parameter_references(content: bytes | None, *, path: str) -> dict[str, tuple[str, ...]]:
    document = _core_document(content, path=path)
    if not document:
        return {}
    vlnv = document.get("name")
    targets = document.get("targets", {})
    if not isinstance(vlnv, str) or not isinstance(targets, Mapping):
        return {}
    references: dict[str, set[str]] = {}
    for target_name, body in targets.items():
        if not isinstance(target_name, str) or not isinstance(body, Mapping):
            continue
        canonical = f"{vlnv}#{target_name}"
        for parameter in fusesoc_registry.possible_target_parameter_names(body):
            references.setdefault(parameter, set()).add(canonical)
    return {name: tuple(sorted(targets)) for name, targets in references.items()}


def _parameter_definitions(content: bytes | None, *, path: str) -> dict[str, _ParameterDefinition]:
    parameters = _core_parameters(content, path=path)
    references = _parameter_references(content, path=path)
    return {
        name: _ParameterDefinition(f"{path}#{name}", path, name, body, references.get(name, ()))
        for name, body in parameters.items()
    }


def _table_mapping(content: bytes | None, *, path: str) -> dict[str, Any]:
    if content is None:
        return {}
    try:
        raw = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TargetPlanValidationError(f"cannot parse {path}: {exc}") from exc
    return dict(raw)


def _changed_rows(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[tuple[str, ...], ...]:
    added = tuple(sorted(set(after) - set(before)))
    deleted = tuple(sorted(set(before) - set(after)))
    modified = tuple(sorted(key for key in set(before) & set(after) if before[key] != after[key]))
    return added, modified, deleted


def _merge_changes(
    changes: Iterable[_ChangeSet[_Change]], key: Callable[[_Change], Any]
) -> _ChangeSet[_Change]:
    rows = tuple(changes)
    return _ChangeSet(
        added=tuple(sorted((item for row in rows for item in row.added), key=key)),
        modified=tuple(sorted((item for row in rows for item in row.modified), key=key)),
        deleted=tuple(sorted((item for row in rows for item in row.deleted), key=key)),
    )


def _surface_delta(files: tuple[TargetSurfaceFile, ...]) -> _SurfaceDelta:
    deltas = []
    for surface in files:
        if Path(surface.path).suffix.casefold() == ".core":
            deltas.append(_core_surface_delta(surface))
        elif Path(surface.path).name == "tests.toml":
            deltas.append(_tests_surface_delta(surface))
    return _SurfaceDelta(
        targets=_merge_changes((delta.targets for delta in deltas), lambda item: item.canonical),
        test_tables=_merge_changes((delta.test_tables for delta in deltas), str),
        filesets=_merge_changes((delta.filesets for delta in deltas), lambda item: item.key),
        parameters=_merge_changes((delta.parameters for delta in deltas), lambda item: item.key),
    )


def _validate_shared_core_content(surface: TargetSurfaceFile) -> None:
    if surface.baseline is None:
        return
    before = dict(_core_document(surface.baseline, path=surface.path))
    after = dict(_core_document(surface.current, path=surface.path))
    for section in ("targets", "filesets", "parameters"):
        before.pop(section, None)
        after.pop(section, None)
    if before != after:
        raise TargetPlanValidationError(
            f"Ticket creation cannot modify .core content outside Target inputs: {surface.path}"
        )


def _parameter_delta(surface: TargetSurfaceFile):
    before_bodies = _core_parameters(surface.baseline, path=surface.path)
    after_bodies = _core_parameters(surface.current, path=surface.path)
    added, modified, deleted = _changed_rows(before_bodies, after_bodies)
    if modified or deleted:
        changed = ", ".join(sorted((*modified, *deleted)))
        raise TargetPlanValidationError(
            f"Ticket creation cannot modify or delete existing parameters in "
            f"{surface.path}: {changed}"
        )
    before = _parameter_definitions(surface.baseline, path=surface.path)
    after = _parameter_definitions(surface.current, path=surface.path)
    return added, modified, deleted, before, after


def _core_surface_delta(surface: TargetSurfaceFile) -> _SurfaceDelta:
    _validate_new_core(surface.baseline, surface.current, Path(surface.path))
    _validate_shared_core_content(surface)
    before = _core_targets(surface.baseline, path=surface.path)
    after = _core_targets(surface.current, path=surface.path)
    added, modified, deleted = _changed_rows(before, after)
    before_fileset_bodies = _core_filesets(surface.baseline, path=surface.path)
    after_fileset_bodies = _core_filesets(surface.current, path=surface.path)
    fileset_added, fileset_modified, fileset_deleted = _changed_rows(
        before_fileset_bodies, after_fileset_bodies
    )
    if fileset_modified or fileset_deleted:
        changed = ", ".join(sorted((*fileset_modified, *fileset_deleted)))
        raise TargetPlanValidationError(
            f"Ticket creation cannot modify or delete existing filesets in "
            f"{surface.path}: {changed}"
        )
    before_filesets = _fileset_definitions(surface.baseline, path=surface.path)
    after_filesets = _fileset_definitions(surface.current, path=surface.path)
    parameter_added, parameter_modified, parameter_deleted, before_parameters, after_parameters = (
        _parameter_delta(surface)
    )
    if not modified and not deleted:
        _validate_core_source_boundary(
            surface,
            tuple(after[key].name for key in added),
            tuple(after_filesets[key].name for key in fileset_added),
            tuple(after_parameters[key].name for key in parameter_added),
        )
    return _SurfaceDelta(
        targets=_ChangeSet(
            tuple(after[key] for key in added),
            tuple(after[key] for key in modified),
            tuple(before[key] for key in deleted),
        ),
        filesets=_ChangeSet(
            tuple(after_filesets[key] for key in fileset_added),
            tuple(after_filesets[key] for key in fileset_modified),
            tuple(before_filesets[key] for key in fileset_deleted),
        ),
        parameters=_ChangeSet(
            tuple(after_parameters[key] for key in parameter_added),
            tuple(after_parameters[key] for key in parameter_modified),
            tuple(before_parameters[key] for key in parameter_deleted),
        ),
    )


def _tests_surface_delta(surface: TargetSurfaceFile) -> _SurfaceDelta:
    before = _table_mapping(surface.baseline, path=surface.path)
    after = _table_mapping(surface.current, path=surface.path)
    added, modified, deleted = _changed_rows(before, after)
    invalid = [
        key
        for key in (*added, *modified)
        if key != TEST_LISTS_TABLE and not isinstance(after[key], Mapping)
    ]
    if invalid:
        raise TargetPlanValidationError(
            "tests.toml Target entries must be tables: " + ", ".join(invalid)
        )
    if not modified and not deleted:
        _validate_tests_source_boundary(surface, added)
    return _SurfaceDelta(test_tables=_ChangeSet(added, modified, deleted))


def _validate_core_source_boundary(
    surface: TargetSurfaceFile,
    added_targets: tuple[str, ...],
    added_filesets: tuple[str, ...] = (),
    added_parameters: tuple[str, ...] = (),
) -> None:
    if surface.baseline is None or surface.current is None:
        return
    try:
        baseline = surface.baseline.decode()
        current = surface.current.decode()
        authorized = only_authorized_core_additions(
            baseline,
            current,
            Path(surface.path),
            {
                "targets": added_targets,
                "filesets": added_filesets,
                "parameters": added_parameters,
            },
        )
    except (UnicodeDecodeError, TargetSurfaceEditError) as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    if not authorized:
        raise TargetPlanValidationError(
            f"Ticket creation changed .core bytes outside planned Targets or their inputs: "
            f"{surface.path}"
        )


def _validate_tests_source_boundary(
    surface: TargetSurfaceFile, added_tables: tuple[str, ...]
) -> None:
    if surface.current is None:
        return
    if surface.baseline is None and not added_tables:
        raise TargetPlanValidationError(
            f"new tests.toml {surface.path} must define at least one planned Target table"
        )
    try:
        baseline = surface.baseline.decode() if surface.baseline is not None else ""
        current = surface.current.decode()
        spans = toml_table_spans(current, added_tables)
    except (UnicodeDecodeError, TargetSurfaceEditError) as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    if not only_authorized_insertions(baseline, current, spans):
        raise TargetPlanValidationError(
            f"Ticket creation changed tests.toml bytes outside planned tables: {surface.path}"
        )


def _validate_new_core(baseline: bytes | None, current: bytes | None, path: Path) -> None:
    if baseline is not None or current is None:
        return
    try:
        validate_new_core_surface(current.decode(), path)
    except (UnicodeDecodeError, TargetSurfaceEditError) as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    if not _core_targets(current, path=path.as_posix()):
        raise TargetPlanValidationError(f"new .core {path} must define at least one Target")


def _canonical_entry(catalog: TargetCatalog, entry: TargetPlanEntry) -> TargetPlanEntry:
    try:
        target = catalog.select(entry.target)
        target_identity = target.identity
        baseline_identity = ""
        if entry.role is TargetPlanRole.REPLACEMENT:
            baseline = catalog.select(entry.replaces)
            baseline_identity = baseline.identity
            target_flows = set(target.drivable_by)
            baseline_flows = set(baseline.drivable_by)
            if not target_flows & baseline_flows:
                raise TargetPlanValidationError(
                    "replacement candidate and baseline must use the same Booley Flow: "
                    f"{target_identity} supports {sorted(target_flows)}, "
                    f"{baseline_identity} supports {sorted(baseline_flows)}"
                )
    except FuseSocError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    return TargetPlanEntry(target_identity, entry.role, baseline_identity)


def _validate_test_tables(
    delta: _SurfaceDelta,
    plan: TargetPlan | None,
    provider_test_tables: frozenset[str],
    catalog: TargetCatalog,
) -> None:
    changed_shared = TEST_LISTS_TABLE in {
        *delta.test_tables.added,
        *delta.test_tables.modified,
        *delta.test_tables.deleted,
    }
    if changed_shared:
        raise TargetPlanValidationError(
            f"Ticket creation cannot author the shared [{TEST_LISTS_TABLE}] tests.toml table"
        )
    modified = set(delta.test_tables.modified) - provider_test_tables
    deleted = set(delta.test_tables.deleted) - provider_test_tables
    if modified or deleted:
        changed = sorted(modified | deleted)
        raise TargetPlanValidationError(
            "Ticket creation cannot modify or delete existing tests.toml tables: "
            + ", ".join(changed)
        )
    authored_tables = tuple(
        table for table in delta.test_tables.added if table not in provider_test_tables
    )
    if not authored_tables:
        return
    if plan is None:
        raise TargetPlanValidationError(
            "target_plan omission requires no Ticket-authored tests.toml tables"
        )
    _validate_authored_test_tables(authored_tables, plan, catalog)


def _validate_authored_test_tables(
    authored_tables: tuple[str, ...], plan: TargetPlan, catalog: TargetCatalog
) -> None:
    owners = {
        table: [
            entry.target
            for entry in plan.entries
            if table in {entry.target, entry.target.rsplit("#", 1)[-1]}
        ]
        for table in authored_tables
    }
    unexpected = sorted(table for table, matches in owners.items() if len(matches) != 1)
    if unexpected:
        raise TargetPlanValidationError(
            "tests.toml table additions are not owned by a planned Target: "
            + ", ".join(unexpected)
        )
    ambiguous = sorted(
        table
        for table in authored_tables
        if "#" not in table and catalog.declaration_count(table, include_private=True) > 1
    )
    if ambiguous:
        raise TargetPlanValidationError(
            "ambiguous bare tests.toml table additions require a VLNV-qualified name: "
            + ", ".join(ambiguous)
        )


def _canonical_plan(fields: Mapping[str, Any], catalog: TargetCatalog) -> TargetPlan | None:
    raw_plan = fields.get("target_plan")
    try:
        authored = TargetPlan.from_value(raw_plan) if raw_plan is not None else None
    except TargetPlanError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    if authored is None:
        return None
    entries = [_canonical_entry(catalog, entry).as_dict() for entry in authored.entries]
    try:
        return TargetPlan.from_value(entries)
    except TargetPlanError as exc:
        raise TargetPlanValidationError(f"canonical Target Plan {exc}") from exc


def canonical_target_plan(fields: Mapping[str, Any], project_root: Path) -> TargetPlan | None:
    """Resolve authored Target selectors against a pinned project checkout."""
    try:
        catalog = TargetCatalog.build(project_root)
    except FuseSocError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    return _canonical_plan(fields, catalog)


def _bound_identities(fields: Mapping[str, Any], project_root: Path) -> set[str]:
    bindings = canonical_acceptance_bindings(
        project_root, criterion_targets(fields.get("criteria"))
    )
    return {identity for row in bindings for identity in (row.baseline, row.candidate)}


def _derived_removals(plan: TargetPlan | None) -> tuple[str, ...]:
    if plan is None:
        return ()
    return tuple(
        sorted(
            entry.target if entry.role is TargetPlanRole.EPHEMERAL else entry.replaces
            for entry in plan.entries
            if entry.role is not TargetPlanRole.PERSISTENT
        )
    )


def _validate_surface_coverage(
    delta: _SurfaceDelta,
    canonical: TargetPlan | None,
    provider_targets: frozenset[str],
) -> tuple[set[str], set[str]]:
    added_surface = {item.canonical for item in delta.targets.added}
    disappeared = provider_targets - added_surface
    if disappeared:
        raise TargetPlanValidationError(
            "materialized provider Targets changed or disappeared: "
            + ", ".join(sorted(disappeared))
        )
    added = added_surface - provider_targets
    planned = {entry.target for entry in canonical.entries} if canonical is not None else set()
    if added != planned:
        details = []
        if added - planned:
            details.append("unplanned authored Targets: " + ", ".join(sorted(added - planned)))
        if planned - added:
            details.append(
                "planned Targets not newly authored: " + ", ".join(sorted(planned - added))
            )
        if not details:
            details.append("target_plan omission requires no Ticket-authored Targets")
        raise TargetPlanValidationError("; ".join(details))
    return added, planned


def _validate_plan_bindings(
    fields: Mapping[str, Any],
    root: Path,
    canonical: TargetPlan | None,
    planned: set[str],
    provider_targets: frozenset[str],
    exported_provider_targets: frozenset[str],
) -> None:
    bound = _bound_identities(fields, root)
    required = set(planned)
    if canonical is not None:
        required.update(entry.replaces for entry in canonical.entries if entry.replaces)
    unbound = sorted(required - bound)
    if unbound:
        raise TargetPlanValidationError(
            "Target Plan entries must be bound by Ticket Criteria: " + ", ".join(unbound)
        )
    non_exported = sorted((bound & provider_targets) - exported_provider_targets)
    if non_exported:
        raise TargetPlanValidationError(
            "Ticket Criteria bind non-exported provider Targets: " + ", ".join(non_exported)
        )


def _validate_fileset_coverage(
    delta: _SurfaceDelta,
    planned_targets: set[str],
    provider_targets: frozenset[str],
) -> tuple[str, ...]:
    authorized_targets = planned_targets | set(provider_targets)
    authored: list[str] = []
    for fileset in delta.filesets.added:
        references = set(fileset.referenced_by)
        if not references:
            raise TargetPlanValidationError(
                f"added fileset {fileset.name!r} in {fileset.path} is not referenced "
                "by a planned Target"
            )
        existing = sorted(references - authorized_targets)
        if existing:
            raise TargetPlanValidationError(
                f"added fileset {fileset.name!r} in {fileset.path} changes an existing "
                f"Target's inputs: {', '.join(existing)}"
            )
        if references & planned_targets:
            authored.append(fileset.key)
    return tuple(sorted(authored))


def _validate_parameter_coverage(
    delta: _SurfaceDelta,
    planned_targets: set[str],
    provider_targets: frozenset[str],
) -> tuple[str, ...]:
    authorized_targets = planned_targets | set(provider_targets)
    authored: list[str] = []
    for parameter in delta.parameters.added:
        if "?" in parameter.name:
            raise TargetPlanValidationError(
                f"new parameter declaration {parameter.name!r} in {parameter.path} "
                "uses a conditional key that cannot be owned unambiguously"
            )
        references = set(parameter.referenced_by)
        if not references:
            raise TargetPlanValidationError(
                f"added parameter {parameter.name!r} in {parameter.path} is not referenced "
                "by a planned Target"
            )
        existing = sorted(references - authorized_targets)
        if existing:
            raise TargetPlanValidationError(
                f"added parameter {parameter.name!r} in {parameter.path} changes an existing "
                f"Target's inputs: {', '.join(existing)}"
            )
        if references & planned_targets:
            authored.append(parameter.key)
    return tuple(sorted(authored))


def _validate_replacement_baselines(canonical: TargetPlan | None, added: set[str]) -> None:
    invalid = (
        sorted(
            entry.replaces
            for entry in canonical.entries
            if entry.role is TargetPlanRole.REPLACEMENT and entry.replaces in added
        )
        if canonical is not None
        else []
    )
    if invalid:
        raise TargetPlanValidationError(
            "replacement baselines must exist on the destination baseline: " + ", ".join(invalid)
        )


def analyze_target_plan(
    fields: Mapping[str, Any],
    project_root: Path,
    surface_files: tuple[TargetSurfaceFile, ...],
    *,
    provider_targets: frozenset[str] = frozenset(),
    exported_provider_targets: frozenset[str] = frozenset(),
    provider_test_tables: frozenset[str] = frozenset(),
) -> TargetPlanAnalysis:
    """Return the canonical plan after proving exact Target-surface coverage."""
    delta = _surface_delta(surface_files)
    if delta.targets.modified or delta.targets.deleted:
        changed = [item.canonical for item in (*delta.targets.modified, *delta.targets.deleted)]
        raise TargetPlanValidationError(
            "Ticket creation cannot modify or delete existing Targets: " + ", ".join(changed)
        )
    try:
        catalog = TargetCatalog.build(project_root)
    except FuseSocError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    canonical = _canonical_plan(fields, catalog)
    added, planned = _validate_surface_coverage(delta, canonical, provider_targets)
    authored_filesets = _validate_fileset_coverage(delta, planned, provider_targets)
    authored_parameters = _validate_parameter_coverage(delta, planned, provider_targets)
    _validate_replacement_baselines(canonical, added)
    _validate_test_tables(delta, canonical, provider_test_tables, catalog)
    _validate_plan_bindings(
        fields,
        project_root,
        canonical,
        planned,
        provider_targets,
        exported_provider_targets,
    )
    return TargetPlanAnalysis(
        canonical,
        _derived_removals(canonical),
        tuple(sorted(added)),
        authored_filesets,
        authored_parameters,
    )


def analyze_ticket_spec_target_plan(
    spec: TicketSpec,
    project_root: Path,
    surface_files: tuple[TargetSurfaceFile, ...],
    *,
    provider_targets: frozenset[str] = frozenset(),
    exported_provider_targets: frozenset[str] = frozenset(),
    provider_test_tables: frozenset[str] = frozenset(),
) -> TargetPlanAnalysis:
    """Prove a converted Ticket's derived plan covers its authored Target delta."""
    from .acceptance_targets import criterion_targets_from_spec

    delta = _surface_delta(surface_files)
    if delta.targets.modified or delta.targets.deleted:
        changed = [item.canonical for item in (*delta.targets.modified, *delta.targets.deleted)]
        raise TargetPlanValidationError(
            "Ticket creation cannot modify or delete existing Targets: " + ", ".join(changed)
        )
    catalog = TargetCatalog.build(project_root)
    canonical = spec.target_plan
    if canonical is not None:
        for entry in canonical.entries:
            if catalog.select(entry.target).identity != entry.target:
                raise TargetPlanValidationError(
                    f"derived Target Plan identity {entry.target!r} changed in the Project view"
                )
    added, planned = _validate_surface_coverage(delta, canonical, provider_targets)
    authored_filesets = _validate_fileset_coverage(delta, planned, provider_targets)
    authored_parameters = _validate_parameter_coverage(delta, planned, provider_targets)
    _validate_replacement_baselines(canonical, added)
    _validate_test_tables(delta, canonical, provider_test_tables, catalog)

    bindings = canonical_acceptance_bindings(project_root, criterion_targets_from_spec(spec))
    bound = {identity for row in bindings for identity in (row.baseline, row.candidate)}
    unbound = sorted(planned - bound)
    if unbound:
        raise TargetPlanValidationError(
            "Target Plan entries must be bound by Ticket Criteria: " + ", ".join(unbound)
        )
    non_exported = sorted((bound & provider_targets) - exported_provider_targets)
    if non_exported:
        raise TargetPlanValidationError(
            "Ticket Criteria bind non-exported provider Targets: " + ", ".join(non_exported)
        )
    return TargetPlanAnalysis(
        canonical,
        _derived_removals(canonical),
        tuple(sorted(added)),
        authored_filesets,
        authored_parameters,
    )
