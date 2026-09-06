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
from booley.core.models import TargetPlanRole
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import resolve_checkout_project_dir, runtime_dir

from .acceptance_basis import (
    AcceptanceBasisError,
    ProviderTargetBinding,
    canonical_json,
    load_acceptance_basis,
    materialize_current_ticket_checkout,
)
from .acceptance_targets import criterion_targets
from .frontmatter import parse_frontmatter
from .persistence import atomic_replace_bytes
from .scanner import find_ticket_file
from .target_surface_edit import TargetSurfaceEditError, merge_target_definition

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


def _owned_tests(checkout: Path, target: str) -> tuple[str, Any]:
    tests_path = resolve_checkout_project_dir(checkout) / "tests.toml"
    if not tests_path.is_file():
        return "", None
    try:
        raw = tomllib.loads(tests_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PlannedDependencyError(f"cannot read provider tests.toml: {exc}") from exc
    bare = target.rsplit("#", 1)[-1]
    if target in raw:
        return target, raw[target]
    if bare in raw:
        declarations = fusesoc_registry.target_declarations(checkout).get(bare, [])
        if len(declarations) != 1:
            raise PlannedDependencyError(
                f"provider Target {target!r} has ambiguous bare tests.toml ownership"
            )
        return bare, raw[bare]
    return "", None


def _surface(checkout: Path, target: str) -> tuple[Path, str, str]:
    try:
        ref = fusesoc_registry.resolve_ref(checkout, target)
        document = fusesoc_registry.read_core(ref.core_file)
    except fusesoc_registry.FuseSocError as exc:
        raise PlannedDependencyError(str(exc)) from exc
    targets = document.get("targets")
    if not isinstance(targets, dict) or ref.name not in targets:
        raise PlannedDependencyError(f"provider Target {target!r} has no declaration")
    tests_key, tests = _owned_tests(checkout, target)
    digest = hashlib.sha256(
        canonical_json({"target": targets[ref.name], "tests": tests})
    ).hexdigest()
    try:
        core_path = ref.core_file.resolve().relative_to(checkout.resolve())
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


def _toml_table_block(content: str, key: str) -> str:
    patterns = (
        re.compile(rf"(?m)^\[{re.escape(key)}\]\s*(?:#.*)?$"),
        re.compile(rf'(?m)^\["{re.escape(key)}"\]\s*(?:#.*)?$'),
    )
    match = next((found for pattern in patterns if (found := pattern.search(content))), None)
    if match is None:
        raise PlannedDependencyError(f"provider tests.toml table {key!r} disappeared")
    following = re.search(r"(?m)^\[[^[][^]]*\]\s*(?:#.*)?$", content[match.end() :])
    end = match.end() + following.start() if following is not None else len(content)
    return content[match.start() : end].strip() + "\n"


def _merge_provider_test_table(source: Path, destination: Path, key: str) -> bool:
    if not key:
        return False
    source_tests = resolve_checkout_project_dir(source) / "tests.toml"
    destination_tests = resolve_checkout_project_dir(destination) / "tests.toml"
    if key in _tests_tables(destination_tests):
        return False
    block = _toml_table_block(source_tests.read_text(encoding="utf-8"), key)
    prefix = (
        destination_tests.read_text(encoding="utf-8").rstrip()
        if destination_tests.is_file()
        else ""
    )
    destination_tests.parent.mkdir(parents=True, exist_ok=True)
    destination_tests.write_text("\n\n".join([prefix, block]).lstrip() + "\n", encoding="utf-8")
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
        }
    )


def _load_marker(path: Path) -> ProviderMaterialization:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        bindings = tuple(
            ProviderTargetBinding(
                str(row["provider"]),
                str(row["basis_id"]),
                str(row["target"]),
                str(row["role"]),
                str(row["surface_sha256"]),
            )
            for row in raw["bindings"]
        )
        result = ProviderMaterialization(
            tuple(sorted(bindings)),
            frozenset(str(item) for item in raw["materialized_targets"]),
            frozenset(str(item) for item in raw["exported_targets"]),
            frozenset(str(item) for item in raw["test_tables"]),
            tuple(
                sorted(
                    (str(row["target"]), str(row["sha256"]))
                    for row in raw["surface_digests"]
                    if set(row) == {"target", "sha256"}
                )
            ),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlannedDependencyError(
            f"provider materialization marker is invalid: {path}"
        ) from exc
    if (
        result.surface_digests != tuple(sorted(set(result.surface_digests)))
        or any(not target for target, _digest in result.surface_digests)
        or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for _target, digest in result.surface_digests
        )
    ):
        raise PlannedDependencyError(
            f"provider materialization marker has invalid surface digests: {path}"
        )
    if _serialize(result) != path.read_bytes():
        raise PlannedDependencyError(f"provider materialization marker is not canonical: {path}")
    return result


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
) -> ProviderMaterialization:
    exports = tuple(
        entry
        for entry in provider.basis.target_plan.entries
        if entry.role in {TargetPlanRole.PERSISTENT, TargetPlanRole.REPLACEMENT}
    )
    bindings = []
    materialized_targets: set[str] = set()
    test_tables: set[str] = set()
    surface_digests: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix=f"booley-provider-{provider.slug}-") as directory:
        checkout = materialize_current_ticket_checkout(
            root, provider.basis, Path(directory) / "checkout"
        )
        for entry in exports:
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
            core_path, tests_key, digest = _surface(checkout, entry.target)
            offered[entry.target] = provider.slug
            _merge_provider_target(checkout, workspace, core_path, entry.target)
            materialized_targets.add(entry.target)
            _merge_provider_test_table(checkout, workspace, tests_key)
            if tests_key:
                test_tables.add(tests_key)
            surface_digests.append((entry.target, digest))
            bindings.append(
                ProviderTargetBinding(
                    provider.slug,
                    provider.basis.basis_id,
                    entry.target,
                    entry.role.value,
                    digest,
                )
            )
    return ProviderMaterialization(
        tuple(bindings),
        frozenset(materialized_targets),
        frozenset(entry.target for entry in exports),
        frozenset(test_tables),
        tuple(sorted(surface_digests)),
    )


def _criterion_bound_identities(fields: dict[str, Any], workspace: Path) -> set[str]:
    bound = set()
    for binding in criterion_targets(fields.get("criteria")):
        for selector in (binding.baseline, binding.target):
            try:
                ref = fusesoc_registry.resolve_ref(workspace, selector)
            except fusesoc_registry.FuseSocError:
                continue
            bound.add(f"{ref.vlnv}#{ref.name}")
    return bound


def materialize_planned_dependencies(
    root: Path,
    ticket_path: Path,
    slug: str,
    generation: str,
    workspace: Path,
) -> ProviderMaterialization:
    """Materialize active dependency exports once for one draft generation."""
    marker = _marker_path(root, slug, generation)
    if marker.is_file():
        return _load_marker(marker)
    fields, _body = parse_frontmatter(ticket_path.read_text(encoding="utf-8"))
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    providers = [
        provider
        for dependency in fields.get("dependencies", ())
        if (provider := _provider(root, tickets_dir, str(dependency))) is not None
    ]
    _validate_replacement_owners(providers)
    bindings: list[ProviderTargetBinding] = []
    materialized_targets: set[str] = set()
    exported_targets: set[str] = set()
    test_tables: set[str] = set()
    surface_digests: dict[str, str] = {}
    removals = {target for provider in providers for target in provider.basis.removal_targets}
    offered: dict[str, str] = {}
    for provider in _ordered_providers(providers):
        materialized = _materialize_provider(root, provider, workspace, removals, offered)
        bindings.extend(materialized.bindings)
        materialized_targets.update(materialized.materialized_targets)
        exported_targets.update(materialized.exported_targets)
        test_tables.update(materialized.test_tables)
        surface_digests.update(materialized.surface_digests)
    bound = _criterion_bound_identities(fields, workspace)
    result = ProviderMaterialization(
        tuple(sorted(binding for binding in bindings if binding.target in bound)),
        frozenset(materialized_targets),
        frozenset(exported_targets),
        frozenset(test_tables),
        tuple(sorted(surface_digests.items())),
    )
    atomic_replace_bytes(marker, _serialize(result))
    return result


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
) -> ProviderMaterialization:
    """Require every generation pin to still name the provider's current basis."""
    materialization = load_planned_dependencies(root, slug, generation)
    if not materialization.bindings:
        return materialization
    tickets_dir = resolve_checkout_project_dir(root) / "tickets"
    basis_by_provider: dict[str, str] = {}
    for binding in materialization.bindings:
        basis_id = basis_by_provider.get(binding.provider)
        if basis_id is None:
            provider = _provider(root, tickets_dir, binding.provider)
            if provider is None:
                raise PlannedDependencyError(
                    f"planned provider {binding.provider!r} is no longer basis-published"
                )
            basis_id = provider.basis.basis_id
            basis_by_provider[binding.provider] = basis_id
        if binding.basis_id != basis_id:
            raise PlannedDependencyError(
                f"planned provider {binding.provider!r} published a different Acceptance Basis"
            )
    return materialization


def validate_materialized_surfaces(
    workspace: Path, materialization: ProviderMaterialization
) -> None:
    """Reject edits to any Target surface copied from a planned provider."""
    for target, expected in materialization.surface_digests:
        actual = target_surface_sha256(workspace, target)
        if actual != expected:
            raise PlannedDependencyError(
                f"materialized provider Target {target!r} changed after composition"
            )
