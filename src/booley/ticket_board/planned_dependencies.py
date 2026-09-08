"""Materialize and pin Target surfaces exported by dependency Tickets."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.project_config import TEST_LISTS_TABLE
from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.core.models import TargetPlanRole
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import resolve_checkout_project_dir, runtime_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

from .acceptance_basis import (
    AcceptanceBasisError,
    ProviderTargetBinding,
    canonical_json,
    load_acceptance_basis,
    materialize_basis_checkout,
    provider_binding_from_mapping,
    selector_matches_canonical,
)
from .acceptance_targets import (
    criterion_targets,
    deferable_rtl_or_tb_input,
    scope_allows_new_path,
)
from .frontmatter import parse_frontmatter
from .persistence import atomic_replace_bytes
from .scanner import find_ticket_file, scan_all_tickets
from .target_surface_edit import (
    TargetSurfaceEditError,
    merge_target_definition,
    toml_table_block,
)

_PROVIDER_STATES = frozenset({"waiting", "queued", "running", "blocked", "review"})


class PlannedDependencyError(RuntimeError):
    """A planned dependency surface cannot be pinned or materialized safely."""


@dataclass(frozen=True)
class ProviderMaterialization:
    """Machine-owned provider data used by basis validation and publication."""

    bindings: tuple[ProviderTargetBinding, ...] = ()
    materialized_targets: frozenset[str] = frozenset()
    exported_targets: frozenset[str] = frozenset()
    test_tables: frozenset[str] = frozenset()
    surface_digests: tuple[tuple[str, str], ...] = ()
    dependencies: tuple[str, ...] = ()
    placeholder_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Provider:
    slug: str
    fields: dict[str, Any]
    basis: Any


def _marker_path(root: Path, slug: str, generation: str) -> Path:
    return runtime_dir(root) / "acceptance" / "providers" / slug / f"{generation}.json"


def _tests_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PlannedDependencyError(f"cannot read provider tests.toml: {exc}") from exc
    return set(raw) - {TEST_LISTS_TABLE}


def _owned_tests(checkout: Path, target: str, catalog: TargetCatalog) -> tuple[str, Any]:
    tests_path = resolve_checkout_project_dir(checkout) / "tests.toml"
    if not tests_path.is_file():
        return "", None
    try:
        raw = tomllib.loads(tests_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PlannedDependencyError(f"cannot read provider tests.toml: {exc}") from exc
    bare = target.rsplit("#", 1)[-1]
    if target in raw:
        return _owned_test_table(target, raw[target])
    if bare in raw:
        if catalog.declaration_count(bare, include_private=True) != 1:
            raise PlannedDependencyError(
                f"provider Target {target!r} has ambiguous bare tests.toml ownership"
            )
        return _owned_test_table(bare, raw[bare])
    return "", None


def _owned_test_table(key: str, value: Any) -> tuple[str, Any]:
    if not isinstance(value, dict):
        raise PlannedDependencyError(f"provider tests.toml entry {key!r} is not a table")
    return key, value


def _surface(checkout: Path, target: str) -> tuple[Path, str, str]:
    try:
        catalog = TargetCatalog.build(checkout)
        handle = catalog.select(target)
        document = fusesoc_registry.read_core(handle.core_file)
    except FuseSocError as exc:
        raise PlannedDependencyError(str(exc)) from exc
    targets = document.get("targets")
    if not isinstance(targets, dict) or handle.name not in targets:
        raise PlannedDependencyError(f"provider Target {target!r} has no declaration")
    tests_key, tests = _owned_tests(checkout, handle.identity, catalog)
    controls = {key: value for key, value in document.items() if key != "targets"}
    digest = hashlib.sha256(
        canonical_json({"controls": controls, "target": targets[handle.name], "tests": tests})
    ).hexdigest()
    try:
        core_path = handle.core_file.resolve().relative_to(checkout.resolve())
    except ValueError as exc:
        raise PlannedDependencyError(
            f"provider Target {target!r} is outside its checkout"
        ) from exc
    return core_path, tests_key, digest


def target_surface_sha256(checkout: Path, target: str) -> str:
    """Return the normalized Target plus owned-test-table identity."""
    return _surface(checkout, target)[2]


def _provider(root: Path, tickets_dir: Path, slug: str) -> _Provider | None:
    path, status = find_ticket_file(tickets_dir, slug)
    if path is None or status is None or status == "done":
        return None
    if status not in _PROVIDER_STATES:
        return None
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    if fields.get("acceptance_basis") is None:
        return None
    try:
        basis = load_acceptance_basis(root, slug, fields, body)
    except AcceptanceBasisError as exc:
        raise PlannedDependencyError(
            f"provider {slug!r} has an invalid Acceptance Basis: {exc}"
        ) from exc
    if basis.target_plan is None:
        return None
    return _Provider(slug, fields, basis)


def _ordered_providers(providers: list[_Provider]) -> list[_Provider]:
    by_slug = {provider.slug: provider for provider in providers}
    ordered: list[_Provider] = []
    pending = dict(by_slug)
    while pending:
        ready = sorted(
            (
                provider
                for provider in pending.values()
                if not (set(provider.fields.get("dependencies", ())) & set(pending))
            ),
            key=lambda provider: provider.slug,
        )
        if not ready:
            raise PlannedDependencyError("planned provider dependency graph is cyclic")
        for provider in ready:
            ordered.append(provider)
            pending.pop(provider.slug)
    return ordered


def _merge_provider_target(source: Path, destination: Path, relative: Path, target: str) -> bool:
    source_file = source / relative
    destination_file = destination / relative
    source_document = fusesoc_registry.read_core(source_file)
    source_name = target.rsplit("#", 1)[-1]
    source_targets = source_document.get("targets")
    if not isinstance(source_targets, dict) or source_name not in source_targets:
        raise PlannedDependencyError(f"provider Target {target!r} disappeared")
    try:
        return merge_target_definition(source_file, destination_file, source_name)
    except (OSError, TargetSurfaceEditError) as exc:
        raise PlannedDependencyError(str(exc)) from exc


def _merge_provider_test_table(source: Path, destination: Path, key: str) -> bool:
    if not key:
        return False
    source_tests = resolve_checkout_project_dir(source) / "tests.toml"
    destination_tests = resolve_checkout_project_dir(destination) / "tests.toml"
    if key in _tests_tables(destination_tests):
        return False
    try:
        block = toml_table_block(source_tests.read_text(encoding="utf-8"), key)
    except TargetSurfaceEditError as exc:
        raise PlannedDependencyError(str(exc)) from exc
    prefix = (
        destination_tests.read_text(encoding="utf-8").rstrip()
        if destination_tests.is_file()
        else ""
    )
    destination_tests.parent.mkdir(parents=True, exist_ok=True)
    content = "\n\n".join([prefix, block]).lstrip() + "\n"
    atomic_replace_bytes(destination_tests, content.encode(), mode=0o644)
    return True


def _serialize(materialization: ProviderMaterialization) -> bytes:
    return canonical_json(
        {
            "bindings": [binding.as_dict() for binding in materialization.bindings],
            "materialized_targets": sorted(materialization.materialized_targets),
            "exported_targets": sorted(materialization.exported_targets),
            "test_tables": sorted(materialization.test_tables),
            "surface_digests": [
                {"target": target, "sha256": digest}
                for target, digest in materialization.surface_digests
            ],
            "dependencies": list(materialization.dependencies),
            "placeholder_paths": sorted(materialization.placeholder_paths),
        }
    )


def _load_marker(path: Path) -> ProviderMaterialization:
    try:
        payload = path.read_bytes()
        raw = require_dict(json.loads(payload), field="provider marker")
        expected = {
            "bindings",
            "materialized_targets",
            "exported_targets",
            "test_tables",
            "surface_digests",
            "dependencies",
            "placeholder_paths",
        }
        if set(raw) != expected:
            raise BoundaryError("provider marker has invalid fields")
        bindings = tuple(
            provider_binding_from_mapping(row)
            for row in require_list(raw.get("bindings"), field="provider marker.bindings")
        )
        result = ProviderMaterialization(
            tuple(sorted(bindings)),
            frozenset(_marker_strings(raw, "materialized_targets")),
            frozenset(_marker_strings(raw, "exported_targets")),
            frozenset(_marker_strings(raw, "test_tables")),
            tuple(sorted(_marker_digests(raw))),
            tuple(_marker_strings(raw, "dependencies")),
            frozenset(_marker_strings(raw, "placeholder_paths")),
        )
    except (
        AcceptanceBasisError,
        BoundaryError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise PlannedDependencyError(
            f"provider materialization marker is invalid: {path}"
        ) from exc
    if not _valid_marker_digests(result.surface_digests):
        raise PlannedDependencyError(
            f"provider materialization marker has invalid surface digests: {path}"
        )
    _validate_materialization(result)
    if _serialize(result) != payload:
        raise PlannedDependencyError(f"provider materialization marker is not canonical: {path}")
    return result


def _valid_marker_digests(digests: tuple[tuple[str, str], ...]) -> bool:
    return (
        digests == tuple(sorted(set(digests)))
        and all(target for target, _digest in digests)
        and all(re.fullmatch(r"[0-9a-f]{64}", digest) for _target, digest in digests)
    )


def _validate_materialization(materialization: ProviderMaterialization) -> None:
    bindings = {binding.target: binding for binding in materialization.bindings}
    if len(bindings) != len(materialization.bindings):
        raise PlannedDependencyError("provider materialization has duplicate Target bindings")
    digests = dict(materialization.surface_digests)
    targets = set(bindings)
    if not (
        targets
        == set(digests)
        == set(materialization.materialized_targets)
        == set(materialization.exported_targets)
    ):
        raise PlannedDependencyError("provider materialization Target sets do not align")
    if any(binding.surface_sha256 != digests[target] for target, binding in bindings.items()):
        raise PlannedDependencyError("provider materialization binding digest does not align")
    if {binding.provider for binding in bindings.values()} - set(materialization.dependencies):
        raise PlannedDependencyError("provider materialization binding is not a dependency")
    if any(not _valid_relative_path(path) for path in materialization.placeholder_paths):
        raise PlannedDependencyError("provider materialization has invalid placeholder paths")


def _valid_relative_path(path: str) -> bool:
    candidate = Path(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts


def _marker_strings(raw: dict[Any, Any], key: str) -> tuple[str, ...]:
    values = require_list(raw.get(key), field=f"provider marker.{key}")
    if not all(isinstance(value, str) and value for value in values):
        raise BoundaryError(f"provider marker.{key} must contain non-empty strings")
    return tuple(values)


def _marker_digests(raw: dict[Any, Any]) -> tuple[tuple[str, str], ...]:
    rows = require_list(raw.get("surface_digests"), field="provider marker.surface_digests")
    result = []
    for value in rows:
        row = require_dict(value, field="provider marker.surface digest")
        if set(row) != {"target", "sha256"}:
            raise BoundaryError("provider marker surface digest has invalid fields")
        result.append((require_str(row, "target"), require_str(row, "sha256")))
    return tuple(result)


def _validate_replacement_owners(providers: list[_Provider]) -> None:
    replacement_owners: dict[str, str] = {}
    for provider in providers:
        for entry in provider.basis.target_plan.entries:
            if entry.role is not TargetPlanRole.REPLACEMENT:
                continue
            previous = replacement_owners.get(entry.replaces)
            if previous is not None:
                raise PlannedDependencyError(
                    f"unordered provider Tickets {previous!r} and {provider.slug!r} both "
                    f"replace {entry.replaces!r}"
                )
            replacement_owners[entry.replaces] = provider.slug


def _materialize_provider(
    root: Path,
    provider: _Provider,
    workspace: Path,
    removals: set[str],
    offered: dict[str, str],
    effective_targets: set[str],
) -> ProviderMaterialization:
    exports = tuple(
        entry for entry in _provider_exports(provider) if entry.target in effective_targets
    )
    bindings = []
    materialized_targets: set[str] = set()
    test_tables: set[str] = set()
    surface_digests: list[tuple[str, str]] = []
    placeholder_paths: set[str] = set()
    with tempfile.TemporaryDirectory(prefix=f"booley-provider-{provider.slug}-") as directory:
        checkout = materialize_basis_checkout(root, provider.basis, Path(directory) / "checkout")
        for entry in exports:
            _claim_provider_export(provider, entry, removals, offered)
            core_path, tests_key, digest = _surface(checkout, entry.target)
            _merge_provider_target(checkout, workspace, core_path, entry.target)
            materialized_targets.add(entry.target)
            _merge_provider_test_table(checkout, workspace, tests_key)
            if tests_key:
                test_tables.add(tests_key)
            surface_digests.append((entry.target, digest))
            placeholder_paths.update(_provider_placeholders(checkout, provider, entry.target))
            bindings.append(
                ProviderTargetBinding(
                    provider.slug,
                    provider.basis.basis_id,
                    entry.target,
                    entry.role.value,
                    digest,
                )
            )
    return _provider_materialization(
        bindings,
        materialized_targets,
        exports,
        test_tables,
        surface_digests,
        placeholder_paths,
    )


def _claim_provider_export(
    provider: _Provider,
    entry: Any,
    removals: set[str],
    offered: dict[str, str],
) -> None:
    previous = offered.get(entry.target)
    if previous is not None:
        raise PlannedDependencyError(
            f"provider Targets are ambiguous: {entry.target!r} is offered by "
            f"{previous!r} and {provider.slug!r}"
        )
    if entry.target in removals:
        raise PlannedDependencyError(
            f"provider Target {entry.target!r} is removed by another dependency"
        )
    offered[entry.target] = provider.slug


def _provider_materialization(
    bindings: list[ProviderTargetBinding],
    materialized_targets: set[str],
    exports: tuple[Any, ...],
    test_tables: set[str],
    surface_digests: list[tuple[str, str]],
    placeholder_paths: set[str],
) -> ProviderMaterialization:
    return ProviderMaterialization(
        tuple(bindings),
        frozenset(materialized_targets),
        frozenset(entry.target for entry in exports),
        frozenset(test_tables),
        tuple(sorted(surface_digests)),
        (),
        frozenset(placeholder_paths),
    )


def _provider_placeholders(checkout: Path, provider: _Provider, target: str) -> tuple[str, ...]:
    scope = provider.fields.get("scope")
    if not isinstance(scope, list) or not any(
        isinstance(item, str) and item.endswith(" [new]") for item in scope
    ):
        return ()
    try:
        inputs = _inspect_provider_inputs(checkout, target)
    except (OSError, FuseSocError) as exc:
        raise PlannedDependencyError(
            f"cannot inspect provider Target {target!r} inputs: {exc}"
        ) from exc
    placeholders = []
    for item in inputs:
        path = item.path.replace("\\", "/").removeprefix("./")
        candidate = checkout / path
        if not scope_allows_new_path(scope, path):
            continue
        if not candidate.is_file() or candidate.stat().st_size != 0:
            continue
        if not deferable_rtl_or_tb_input(item):
            raise PlannedDependencyError(
                f"provider Target {target!r} defers non-RTL/TB input {path!r}"
            )
        try:
            candidate.resolve().relative_to(checkout.resolve())
        except ValueError as exc:
            raise PlannedDependencyError(
                f"provider Target {target!r} placeholder escapes its checkout: {path}"
            ) from exc
        placeholders.append(path)
    return tuple(sorted(set(placeholders)))


def _inspect_provider_inputs(checkout: Path, target: str) -> tuple[Any, ...]:
    catalog = TargetCatalog.build(checkout)
    return catalog.inspect(catalog.select(target)).inputs


def _provider_exports(provider: _Provider) -> tuple[Any, ...]:
    return tuple(
        entry
        for entry in provider.basis.target_plan.entries
        if entry.role in {TargetPlanRole.PERSISTENT, TargetPlanRole.REPLACEMENT}
    )


def _ticket_dependencies(fields: dict[str, Any]) -> tuple[str, ...]:
    dependencies = fields.get("dependencies", ())
    if not isinstance(dependencies, (list, tuple)) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise PlannedDependencyError("Ticket dependencies are not canonical strings")
    return tuple(dependencies)


def _require_marker_dependencies(
    materialization: ProviderMaterialization, dependencies: tuple[str, ...]
) -> ProviderMaterialization:
    if materialization.dependencies != dependencies:
        raise PlannedDependencyError(
            "Ticket dependencies changed after provider materialization; recreate the workspace"
        )
    return materialization


def _active_providers(root: Path, tickets_dir: Path) -> list[_Provider]:
    providers = []
    for ticket in scan_all_tickets(tickets_dir):
        if ticket.get("status") not in _PROVIDER_STATES:
            continue
        slug = Path(str(ticket.get("file", ""))).stem
        provider = _provider(root, tickets_dir, slug)
        if provider is not None:
            providers.append(provider)
    return providers


def _export_owners(providers: list[_Provider]) -> dict[str, str]:
    offered: dict[str, str] = {}
    for provider in providers:
        for entry in _provider_exports(provider):
            previous = offered.get(entry.target)
            if previous is not None and previous != provider.slug:
                raise PlannedDependencyError(
                    f"provider Targets are ambiguous: {entry.target!r} is offered by "
                    f"{previous!r} and {provider.slug!r}"
                )
            offered[entry.target] = provider.slug
    by_slug = {provider.slug: provider for provider in providers}
    for remover in providers:
        for target in remover.basis.removal_targets:
            owner = offered.get(target)
            if owner is None or owner == remover.slug:
                continue
            if not _provider_depends_on(remover, owner, by_slug):
                raise PlannedDependencyError(
                    f"provider Target {target!r} is offered by {owner!r} "
                    f"and removed by {remover.slug!r}"
                )
            offered.pop(target)
    return offered


def _provider_depends_on(
    provider: _Provider, dependency: str, providers: dict[str, _Provider]
) -> bool:
    pending = list(provider.fields.get("dependencies", ()))
    visited = set()
    while pending:
        slug = pending.pop()
        if slug == dependency:
            return True
        if slug in visited:
            continue
        visited.add(slug)
        nested = providers.get(slug)
        if nested is not None:
            pending.extend(nested.fields.get("dependencies", ()))
    return False


def _required_provider_slugs(fields: dict[str, Any], providers: list[_Provider]) -> set[str]:
    owners = _export_owners(providers)
    required = set()
    for binding in criterion_targets(fields.get("criteria")):
        for selector in (binding.target, binding.baseline):
            matches = {
                provider
                for target, provider in owners.items()
                if selector_matches_canonical(selector, target)
            }
            if len(matches) > 1:
                raise PlannedDependencyError(
                    f"criterion Target {selector!r} is offered by ambiguous providers"
                )
            required.update(matches)
    return required


def _validate_provider_dependencies(
    fields: dict[str, Any], dependencies: tuple[str, ...], providers: list[_Provider]
) -> None:
    _validate_retiring_provider_targets(fields, providers)
    missing = sorted(_required_provider_slugs(fields, providers) - set(dependencies))
    if missing:
        raise PlannedDependencyError(
            "Ticket is missing provider dependencies: " + ", ".join(missing)
        )


def _validate_retiring_provider_targets(
    fields: dict[str, Any], providers: list[_Provider]
) -> None:
    retiring = {
        target: provider.slug
        for provider in providers
        for target in provider.basis.removal_targets
    }
    for binding in criterion_targets(fields.get("criteria")):
        for selector in (binding.target, binding.baseline):
            matches = [
                (target, provider)
                for target, provider in retiring.items()
                if selector_matches_canonical(selector, target)
            ]
            if matches:
                target, provider = matches[0]
                raise PlannedDependencyError(
                    f"criterion Target {selector!r} names {provider!r}'s retiring "
                    f"provider Target {target!r}"
                )


def materialize_planned_dependencies(
    root: Path,
    ticket_path: Path,
    slug: str,
    generation: str,
    workspace: Path,
) -> ProviderMaterialization:
    """Materialize active dependency exports once for one draft generation."""
    fields, _body = parse_frontmatter(ticket_path.read_text(encoding="utf-8"))
    dependencies = _ticket_dependencies(fields)
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    active = _active_providers(root, tickets_dir)
    _validate_replacement_owners(active)
    _validate_provider_dependencies(fields, dependencies, active)
    providers = _dependency_providers(root, tickets_dir, dependencies, active)
    marker = _marker_path(root, slug, generation)
    if marker.is_file():
        materialization = _require_marker_dependencies(_load_marker(marker), dependencies)
        _validate_marker_exports(materialization, providers)
        _restore_materialized_surfaces(root, providers, workspace, materialization)
        validate_materialized_surfaces(workspace, materialization)
        return materialization
    result = _compose_providers(root, providers, workspace, dependencies)
    _validate_materialization(result)
    atomic_replace_bytes(marker, _serialize(result))
    return result


def _validate_marker_exports(
    materialization: ProviderMaterialization, providers: list[_Provider]
) -> None:
    effective = set(_export_owners(providers))
    expected = {
        (provider.slug, entry.target, entry.role.value)
        for provider in providers
        for entry in _provider_exports(provider)
        if entry.target in effective
    }
    actual = {(row.provider, row.target, row.role) for row in materialization.bindings}
    if actual != expected:
        raise PlannedDependencyError(
            "planned provider exports changed after materialization; recreate the workspace"
        )


def _restore_materialized_surfaces(
    root: Path,
    providers: list[_Provider],
    workspace: Path,
    materialization: ProviderMaterialization,
) -> None:
    by_slug = {provider.slug: provider for provider in providers}
    bindings: dict[str, list[ProviderTargetBinding]] = {}
    for binding in materialization.bindings:
        bindings.setdefault(binding.provider, []).append(binding)
    missing = sorted(set(bindings) - set(by_slug))
    if missing:
        raise PlannedDependencyError(
            "planned provider Tickets are no longer basis-published: " + ", ".join(missing)
        )
    for provider_slug, rows in bindings.items():
        _restore_provider_surfaces(root, by_slug[provider_slug], rows, workspace)


def _restore_provider_surfaces(
    root: Path,
    provider: _Provider,
    bindings: list[ProviderTargetBinding],
    workspace: Path,
) -> None:
    exported = {(entry.target, entry.role.value) for entry in _provider_exports(provider)}
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"booley-provider-restore-{provider.slug}-"
        ) as directory:
            checkout = materialize_basis_checkout(
                root, provider.basis, Path(directory) / "checkout"
            )
            _validate_refreshed_bindings(provider, bindings, exported, checkout)
            for binding in bindings:
                core_path, tests_key, _digest = _surface(checkout, binding.target)
                _merge_provider_target(checkout, workspace, core_path, binding.target)
                _merge_provider_test_table(checkout, workspace, tests_key)
    except (AcceptanceBasisError, OSError, TargetSurfaceEditError) as exc:
        raise PlannedDependencyError(
            f"cannot restore planned provider {provider.slug!r}: {exc}"
        ) from exc


def _dependency_providers(
    root: Path,
    tickets_dir: Path,
    dependencies: tuple[str, ...],
    active: list[_Provider],
) -> list[_Provider]:
    by_slug = {provider.slug: provider for provider in active}
    providers = []
    for dependency in dependencies:
        provider = by_slug.get(dependency) or _provider(root, tickets_dir, dependency)
        if provider is not None:
            providers.append(provider)
    _validate_replacement_owners(providers)
    return providers


def _compose_providers(
    root: Path,
    providers: list[_Provider],
    workspace: Path,
    dependencies: tuple[str, ...],
) -> ProviderMaterialization:
    bindings: list[ProviderTargetBinding] = []
    materialized_targets: set[str] = set()
    exported_targets: set[str] = set()
    test_tables: set[str] = set()
    surface_digests: dict[str, str] = {}
    placeholder_paths: set[str] = set()
    removals = {target for provider in providers for target in provider.basis.removal_targets}
    effective_targets = set(_export_owners(providers))
    offered: dict[str, str] = {}
    for provider in _ordered_providers(providers):
        materialized = _materialize_provider(
            root, provider, workspace, removals, offered, effective_targets
        )
        bindings.extend(materialized.bindings)
        materialized_targets.update(materialized.materialized_targets)
        exported_targets.update(materialized.exported_targets)
        test_tables.update(materialized.test_tables)
        surface_digests.update(materialized.surface_digests)
        placeholder_paths.update(materialized.placeholder_paths)
    return ProviderMaterialization(
        tuple(sorted(bindings)),
        frozenset(materialized_targets),
        frozenset(exported_targets),
        frozenset(test_tables),
        tuple(sorted(surface_digests.items())),
        dependencies,
        frozenset(placeholder_paths),
    )


def load_planned_dependencies(root: Path, slug: str, generation: str) -> ProviderMaterialization:
    """Load the provider materialization pinned for a draft generation."""
    marker = _marker_path(root, slug, generation)
    if not marker.is_file():
        return ProviderMaterialization()
    return _load_marker(marker)


def validate_planned_dependencies(
    root: Path,
    slug: str,
    generation: str,
    ticket_path: Path,
) -> ProviderMaterialization:
    """Require every generation pin to still name the provider's current basis."""
    materialization = load_planned_dependencies(root, slug, generation)
    fields, _body = parse_frontmatter(ticket_path.read_text(encoding="utf-8"))
    dependencies = _ticket_dependencies(fields)
    _require_marker_dependencies(materialization, dependencies)
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    active = _active_providers(root, tickets_dir)
    _validate_replacement_owners(active)
    _validate_provider_dependencies(fields, dependencies, active)
    providers = _dependency_providers(root, tickets_dir, dependencies, active)
    _validate_marker_exports(materialization, providers)
    if not materialization.bindings:
        return materialization
    by_slug = {provider.slug: provider for provider in providers}
    by_provider: dict[str, list[ProviderTargetBinding]] = {}
    for binding in materialization.bindings:
        by_provider.setdefault(binding.provider, []).append(binding)
    for slug_key, bindings in by_provider.items():
        provider = by_slug.get(slug_key)
        if provider is None:
            raise PlannedDependencyError(
                f"planned provider {slug_key!r} is no longer basis-published"
            )
        if any(binding.basis_id != provider.basis.basis_id for binding in bindings):
            _validate_refreshed_provider(root, provider, bindings)
    return materialization


def _validate_refreshed_provider(
    root: Path, provider: _Provider, bindings: list[ProviderTargetBinding]
) -> None:
    exported = {(entry.target, entry.role.value) for entry in _provider_exports(provider)}
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"booley-provider-check-{provider.slug}-"
        ) as directory:
            checkout = materialize_basis_checkout(
                root, provider.basis, Path(directory) / "checkout"
            )
            _validate_refreshed_bindings(provider, bindings, exported, checkout)
    except (AcceptanceBasisError, OSError) as exc:
        raise PlannedDependencyError(
            f"cannot validate refreshed provider {provider.slug!r}: {exc}"
        ) from exc


def _validate_refreshed_bindings(
    provider: _Provider,
    bindings: list[ProviderTargetBinding],
    exported: set[tuple[str, str]],
    checkout: Path,
) -> None:
    for binding in bindings:
        if (binding.target, binding.role) not in exported:
            raise PlannedDependencyError(
                f"planned provider {provider.slug!r} no longer exports {binding.target!r}"
            )
        if target_surface_sha256(checkout, binding.target) != binding.surface_sha256:
            raise PlannedDependencyError(
                f"planned provider Target {binding.target!r} changed after pinning"
            )


def validate_materialized_surfaces(
    workspace: Path, materialization: ProviderMaterialization
) -> None:
    """Reject edits to any Target surface copied from a planned provider."""
    test_tables = set()
    for target, expected in materialization.surface_digests:
        _core, test_table, actual = _surface(workspace, target)
        if actual != expected:
            raise PlannedDependencyError(
                f"materialized provider Target {target!r} changed after composition"
            )
        if test_table:
            test_tables.add(test_table)
    if test_tables != set(materialization.test_tables):
        raise PlannedDependencyError("materialized provider tests.toml ownership changed")
