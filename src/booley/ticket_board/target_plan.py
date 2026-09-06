"""Validate Ticket-authored Target transitions against destination Git trees.

The module is the single seam for Target Plan policy.  Callers provide the
authoring repositories and their Git-visible paths; the implementation owns
semantic ``.core`` comparison, owned ``tests.toml`` comparison, selector
canonicalization, Criterion coverage, and derived removals.
"""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from booley.config.project_config import TEST_LISTS_TABLE
from booley.core.models import TargetPlan, TargetPlanEntry, TargetPlanError, TargetPlanRole
from booley.fusesoc import fusesoc_registry

from .acceptance_targets import canonical_acceptance_bindings, criterion_targets


class TargetPlanValidationError(ValueError):
    """A Target Plan does not describe the authoring delta exactly."""


@dataclass(frozen=True)
class TargetPlanAnalysis:
    """Canonical Target Plan data committed into an Acceptance Basis record."""

    plan: TargetPlan | None
    removal_targets: tuple[str, ...]
    authored_targets: tuple[str, ...]


@dataclass(frozen=True)
class _TargetDefinition:
    canonical: str
    name: str
    body: Any


@dataclass(frozen=True)
class _SurfaceDelta:
    added: tuple[_TargetDefinition, ...]
    modified: tuple[_TargetDefinition, ...]
    deleted: tuple[_TargetDefinition, ...]
    added_test_tables: tuple[str, ...]
    modified_test_tables: tuple[str, ...]
    deleted_test_tables: tuple[str, ...]


def _git_file(repository: Path, path: str) -> bytes | None:
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", "--", path],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if listed.returncode != 0:
        raise TargetPlanValidationError(
            f"cannot inspect destination baseline for {path}: {listed.stderr.strip()}"
        )
    if not listed.stdout.strip():
        return None
    shown = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=repository,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if shown.returncode != 0:
        raise TargetPlanValidationError(
            f"cannot read destination baseline for {path}: "
            f"{shown.stderr.decode(errors='replace').strip()}"
        )
    return shown.stdout


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
        if fusesoc_registry.core_target_is_doctor_selftest(document, name):
            continue
        canonical = f"{vlnv}#{name}"
        result[canonical] = _TargetDefinition(canonical, name, body)
    return result


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


def _surface_delta(repositories: tuple[tuple[Path, tuple[str, ...]], ...]) -> _SurfaceDelta:
    added: list[_TargetDefinition] = []
    modified: list[_TargetDefinition] = []
    deleted: list[_TargetDefinition] = []
    added_tables: list[str] = []
    modified_tables: list[str] = []
    deleted_tables: list[str] = []
    for repository, paths in repositories:
        for path in paths:
            candidate = repository / path
            if candidate.suffix.casefold() == ".core":
                baseline_content = _git_file(repository, path)
                current = candidate.read_bytes() if candidate.is_file() else None
                if baseline_content is not None:
                    baseline_document = dict(_core_document(baseline_content, path=path))
                    current_document = dict(_core_document(current, path=path))
                    baseline_document.pop("targets", None)
                    current_document.pop("targets", None)
                    if baseline_document != current_document:
                        raise TargetPlanValidationError(
                            f"Ticket creation cannot modify .core content outside targets: {path}"
                        )
                before = _core_targets(baseline_content, path=path)
                after = _core_targets(current, path=path)
                row_added, row_modified, row_deleted = _changed_rows(before, after)
                added.extend(after[key] for key in row_added)
                modified.extend(after[key] for key in row_modified)
                deleted.extend(before[key] for key in row_deleted)
                continue
            if candidate.name != "tests.toml":
                continue
            before_tables = _table_mapping(_git_file(repository, path), path=path)
            current = candidate.read_bytes() if candidate.is_file() else None
            after_tables = _table_mapping(current, path=path)
            row_added, row_modified, row_deleted = _changed_rows(before_tables, after_tables)
            added_tables.extend(row_added)
            modified_tables.extend(row_modified)
            deleted_tables.extend(row_deleted)
    return _SurfaceDelta(
        tuple(sorted(added, key=lambda item: item.canonical)),
        tuple(sorted(modified, key=lambda item: item.canonical)),
        tuple(sorted(deleted, key=lambda item: item.canonical)),
        tuple(sorted(added_tables)),
        tuple(sorted(modified_tables)),
        tuple(sorted(deleted_tables)),
    )


def _canonical_entry(root: Path, entry: TargetPlanEntry) -> TargetPlanEntry:
    try:
        target = fusesoc_registry.resolve_ref(root, entry.target)
        target_identity = f"{target.vlnv}#{target.name}"
        baseline_identity = ""
        if entry.role is TargetPlanRole.REPLACEMENT:
            baseline = fusesoc_registry.resolve_ref(root, entry.replaces)
            baseline_identity = f"{baseline.vlnv}#{baseline.name}"
    except fusesoc_registry.FuseSocError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    return TargetPlanEntry(target_identity, entry.role, baseline_identity)


def _validate_test_tables(
    delta: _SurfaceDelta,
    plan: TargetPlan | None,
    provider_test_tables: frozenset[str],
) -> None:
    changed_shared = TEST_LISTS_TABLE in {
        *delta.added_test_tables,
        *delta.modified_test_tables,
        *delta.deleted_test_tables,
    }
    if changed_shared:
        raise TargetPlanValidationError(
            f"Ticket creation cannot author the shared [{TEST_LISTS_TABLE}] tests.toml table"
        )
    modified = set(delta.modified_test_tables) - provider_test_tables
    deleted = set(delta.deleted_test_tables) - provider_test_tables
    if modified or deleted:
        changed = sorted(modified | deleted)
        raise TargetPlanValidationError(
            "Ticket creation cannot modify or delete existing tests.toml tables: "
            + ", ".join(changed)
        )
    authored_tables = tuple(
        table for table in delta.added_test_tables if table not in provider_test_tables
    )
    if not authored_tables:
        return
    if plan is None:
        raise TargetPlanValidationError(
            "target_plan omission requires no Ticket-authored tests.toml tables"
        )
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


def _canonical_plan(fields: Mapping[str, Any], project_root: Path) -> TargetPlan | None:
    raw_plan = fields.get("target_plan")
    try:
        authored = TargetPlan.from_value(raw_plan) if raw_plan is not None else None
    except TargetPlanError as exc:
        raise TargetPlanValidationError(str(exc)) from exc
    if authored is None:
        return None
    entries = [_canonical_entry(project_root, entry).as_dict() for entry in authored.entries]
    try:
        return TargetPlan.from_value(entries)
    except TargetPlanError as exc:
        raise TargetPlanValidationError(f"canonical Target Plan {exc}") from exc


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


def analyze_target_plan(
    fields: Mapping[str, Any],
    project_root: Path,
    repositories: tuple[tuple[Path, tuple[str, ...]], ...],
    *,
    provider_targets: frozenset[str] = frozenset(),
    exported_provider_targets: frozenset[str] = frozenset(),
    provider_test_tables: frozenset[str] = frozenset(),
) -> TargetPlanAnalysis:
    """Return the canonical plan after proving exact Target-surface coverage."""
    delta = _surface_delta(repositories)
    if delta.modified or delta.deleted:
        changed = [item.canonical for item in (*delta.modified, *delta.deleted)]
        raise TargetPlanValidationError(
            "Ticket creation cannot modify or delete existing Targets: " + ", ".join(changed)
        )
    canonical = _canonical_plan(fields, project_root)
    added_surface = {item.canonical for item in delta.added}
    unexpected_provider_targets = provider_targets - added_surface
    if unexpected_provider_targets:
        raise TargetPlanValidationError(
            "materialized provider Targets changed or disappeared: "
            + ", ".join(sorted(unexpected_provider_targets))
        )
    added = added_surface - provider_targets
    planned = {entry.target for entry in canonical.entries} if canonical is not None else set()
    if added != planned:
        missing = sorted(added - planned)
        extra = sorted(planned - added)
        details = []
        if missing:
            details.append("unplanned authored Targets: " + ", ".join(missing))
        if extra:
            details.append("planned Targets not newly authored: " + ", ".join(extra))
        if not details:
            details.append("target_plan omission requires no Ticket-authored Targets")
        raise TargetPlanValidationError("; ".join(details))
    invalid_baselines = (
        sorted(
            entry.replaces
            for entry in canonical.entries
            if entry.role is TargetPlanRole.REPLACEMENT and entry.replaces in added
        )
        if canonical is not None
        else []
    )
    if invalid_baselines:
        raise TargetPlanValidationError(
            "replacement baselines must exist on the destination baseline: "
            + ", ".join(invalid_baselines)
        )
    _validate_test_tables(delta, canonical, provider_test_tables)
    bound = _bound_identities(fields, project_root)
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
    return TargetPlanAnalysis(canonical, _derived_removals(canonical), tuple(sorted(added)))
