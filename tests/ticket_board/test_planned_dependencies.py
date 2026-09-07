"""Prospective provider surface composition tests."""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.models import TargetPlan
from booley.ticket_board import (
    acceptance_basis,
    acceptance_targets,
    planned_dependencies,
    target_surface_edit,
    workspace_ops,
)
from booley.ticket_board.acceptance_basis import ProviderTargetBinding
from booley.ticket_board.planned_dependencies import (
    PlannedDependencyError,
    ProviderMaterialization,
    _materialize_provider,
    _merge_provider_target,
    _merge_provider_test_table,
    _Provider,
    load_planned_dependencies,
    materialize_planned_dependencies,
    target_surface_sha256,
    validate_materialized_surfaces,
    validate_planned_dependencies,
)


def _core(path: Path, targets: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"CAPI=2:\nname: acme:lib:toy:1.0\ntargets:\n{targets}",
        encoding="utf-8",
    )


def test_provider_target_merge_preserves_existing_surface(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _core(source / "toy.core", "  future:\n    filesets: [rtl]\n")
    _core(destination / "toy.core", "  current:\n    filesets: [rtl]\n")

    assert _merge_provider_target(source, destination, Path("toy.core"), "acme:lib:toy:1.0#future")

    text = (destination / "toy.core").read_text(encoding="utf-8")
    assert text == (
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "targets:\n"
        "  current:\n"
        "    filesets: [rtl]\n"
        "  future:\n"
        "    filesets: [rtl]\n"
    )
    assert not _merge_provider_target(
        source, destination, Path("toy.core"), "acme:lib:toy:1.0#future"
    )


def test_new_provider_core_copies_only_exported_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _core(
        source / "toy.core",
        "  exported:\n    filesets: [rtl]\n  private:\n    filesets: [rtl]\n",
    )

    assert _merge_provider_target(
        source,
        destination,
        Path("toy.core"),
        "acme:lib:toy:1.0#exported",
    )

    text = (destination / "toy.core").read_text(encoding="utf-8")
    assert "exported:" in text
    assert "private:" not in text


def test_new_provider_core_rejects_non_target_build_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    core = source / "toy.core"
    core.parent.mkdir(parents=True)
    core.write_text(
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "filesets: {rtl: {files: [toy.sv]}}\n"
        "targets:\n"
        "  exported:\n"
        "    filesets: [rtl]\n",
        encoding="utf-8",
    )

    with pytest.raises(PlannedDependencyError, match="outside Target definitions"):
        _merge_provider_target(
            source,
            destination,
            Path("toy.core"),
            "acme:lib:toy:1.0#exported",
        )


def test_inline_targets_mapping_blocks_narrow_provider_edit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "toy.core").write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\ntargets: {future: {filesets: [rtl]}}\n",
        encoding="utf-8",
    )

    with pytest.raises(PlannedDependencyError, match="inline targets mapping"):
        _merge_provider_target(
            source,
            tmp_path / "destination",
            Path("toy.core"),
            "acme:lib:toy:1.0#future",
        )


def test_provider_test_tables_compose_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / ".booley_project").mkdir(parents=True)
    (destination / ".booley_project").mkdir(parents=True)
    (source / ".booley_project" / "tests.toml").write_text(
        "[future]\nmodule = 'future'\n", encoding="utf-8"
    )
    (destination / ".booley_project" / "tests.toml").write_text(
        "[current]\nmodule = 'current'\n", encoding="utf-8"
    )

    assert _merge_provider_test_table(source, destination, "future")

    tables = tomllib.loads(
        (destination / ".booley_project" / "tests.toml").read_text(encoding="utf-8")
    )
    assert tables == {
        "current": {"module": "current"},
        "future": {"module": "future"},
    }


def test_provider_test_table_copy_includes_nested_tables(tmp_path: Path) -> None:
    source = tmp_path / "source/.booley_project"
    destination = tmp_path / "destination/.booley_project"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "tests.toml").write_text(
        "[future]\nmodule = 'future'\n[future.env]\nMODE = 'fast'\n",
        encoding="utf-8",
    )

    assert _merge_provider_test_table(source.parent, destination.parent, "future")
    tables = tomllib.loads((destination / "tests.toml").read_text(encoding="utf-8"))

    assert tables["future"]["env"] == {"MODE": "fast"}


def test_provider_test_table_copy_accepts_literal_quoted_header(tmp_path: Path) -> None:
    source = tmp_path / "source/.booley_project"
    destination = tmp_path / "destination/.booley_project"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    key = "acme:lib:core:1.0#sim"
    (source / "tests.toml").write_text(
        f"['{key}']\nmodule = 'future'\n['{key}'.env]\nMODE = 'fast'\n",
        encoding="utf-8",
    )

    assert _merge_provider_test_table(source.parent, destination.parent, key)
    tables = tomllib.loads((destination / "tests.toml").read_text(encoding="utf-8"))

    assert tables[key]["env"] == {"MODE": "fast"}


def test_surface_digest_includes_owned_test_table(tmp_path: Path) -> None:
    _core(tmp_path / "toy.core", "  future:\n    filesets: [rtl]\n")
    project = tmp_path / ".booley_project"
    project.mkdir()
    tests = project / "tests.toml"
    tests.write_text("[future]\nmodule = 'one'\n", encoding="utf-8")
    first = target_surface_sha256(tmp_path, "future")
    tests.write_text("[future]\nmodule = 'two'\n", encoding="utf-8")

    assert target_surface_sha256(tmp_path, "future") != first


def test_surface_digest_includes_shared_core_controls(tmp_path: Path) -> None:
    core = tmp_path / "toy.core"
    core.write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\nfilesets: {rtl: {files: [one.sv]}}\n"
        "targets:\n  future:\n    filesets: [rtl]\n",
        encoding="utf-8",
    )
    first = target_surface_sha256(tmp_path, "future")
    core.write_text(core.read_text(encoding="utf-8").replace("one.sv", "two.sv"), encoding="utf-8")

    assert target_surface_sha256(tmp_path, "future") != first


def test_public_materialization_rejects_missing_provider_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\ncriteria: {mandatory: {sim_pass: [future]}}\ndependencies: []\n---\n",
        encoding="utf-8",
    )
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value(
                [{"target": "acme:lib:toy:1.0#future", "role": "persistent"}]
            ),
            removal_targets=(),
        ),
    )
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_args: [provider])
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="missing provider dependencies: provider"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_public_materialization_rejects_ambiguous_active_exports(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ncriteria: {}\ndependencies: []\n---\n", encoding="utf-8")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    providers = [
        _Provider(slug, {}, SimpleNamespace(target_plan=plan, removal_targets=()))
        for slug in ("first", "second")
    ]
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_args: providers)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="provider Targets are ambiguous"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_public_materialization_rejects_sibling_replacements(tmp_path: Path, monkeypatch) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ncriteria: {}\ndependencies: []\n---\n", encoding="utf-8")
    providers = [
        _Provider(
            slug,
            {},
            SimpleNamespace(
                target_plan=TargetPlan.from_value(
                    [{"target": target, "role": "replacement", "replaces": "baseline"}]
                ),
                removal_targets=("baseline",),
            ),
        )
        for slug, target in (("first", "next-a"), ("second", "next-b"))
    ]
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_args: providers)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="both replace 'baseline'"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_consumer_criteria_reject_provider_target_being_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\ncriteria: {mandatory: {lint_clean: [old]}}\ndependencies: [provider]\n---\n",
        encoding="utf-8",
    )
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value(
                [
                    {
                        "target": "acme:lib:toy:1.0#new",
                        "role": "replacement",
                        "replaces": "acme:lib:toy:1.0#old",
                    }
                ]
            ),
            removal_targets=("acme:lib:toy:1.0#old",),
        ),
    )
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_args: [provider])
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="retiring provider Target"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_provider_dependency_inference_accepts_partial_vlnv_selector() -> None:
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value(
                [{"target": "vendor:ibex:core:1.0#future", "role": "persistent"}]
            ),
            removal_targets=(),
        ),
    )
    fields = {"criteria": {"mandatory": {"sim_pass": ["ibex:core#future"]}}}

    assert planned_dependencies._required_provider_slugs(fields, [provider]) == {"provider"}


def test_provider_dependency_inference_allows_ordered_replacement_chain() -> None:
    first, second = _ordered_chain_providers()
    fields = {"criteria": {"mandatory": {"sim_pass": ["latest"]}}}

    assert planned_dependencies._required_provider_slugs(fields, [first, second]) == {"second"}


def _ordered_chain_providers() -> tuple[_Provider, _Provider]:
    first = _Provider(
        "first",
        {},
        SimpleNamespace(
            basis_id="a" * 64,
            target_plan=TargetPlan.from_value(
                [{"target": "acme:lib:toy:1.0#middle", "role": "persistent"}]
            ),
            removal_targets=(),
        ),
    )
    second = _Provider(
        "second",
        {"dependencies": ["first"]},
        SimpleNamespace(
            basis_id="b" * 64,
            target_plan=TargetPlan.from_value(
                [
                    {
                        "target": "acme:lib:toy:1.0#latest",
                        "role": "replacement",
                        "replaces": "acme:lib:toy:1.0#middle",
                    }
                ]
            ),
            removal_targets=("acme:lib:toy:1.0#middle",),
        ),
    )
    return first, second


def test_public_materialization_skips_superseded_ordered_export(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\ncriteria: {mandatory: {sim_pass: [latest]}}\ndependencies: [first, second]\n---\n",
        encoding="utf-8",
    )
    first, second = _ordered_chain_providers()
    seen = []

    def materialize(_root, provider, _workspace, _removals, _offered, effective):
        seen.append((provider.slug, set(effective)))
        if provider.slug == "first":
            return ProviderMaterialization()
        binding = ProviderTargetBinding(
            "second", "b" * 64, "acme:lib:toy:1.0#latest", "replacement", "c" * 64
        )
        return ProviderMaterialization(
            bindings=(binding,),
            materialized_targets=frozenset({binding.target}),
            exported_targets=frozenset({binding.target}),
            surface_digests=((binding.target, binding.surface_sha256),),
        )

    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_: [first, second])
    monkeypatch.setattr(planned_dependencies, "_materialize_provider", materialize)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    result = materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)

    assert result.exported_targets == {"acme:lib:toy:1.0#latest"}
    assert all("acme:lib:toy:1.0#middle" not in targets for _slug, targets in seen)


def test_provider_basis_refresh_with_unchanged_surface_remains_valid(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [provider]\n---\n", encoding="utf-8")
    source = tmp_path / "provider"
    _core(source / "toy.core", "  future:\n    filesets: []\n")
    digest = target_surface_sha256(source, "future")
    binding = ProviderTargetBinding("provider", "a" * 64, "future", "persistent", digest)
    materialization = ProviderMaterialization(
        bindings=(binding,),
        materialized_targets=frozenset({"future"}),
        exported_targets=frozenset({"future"}),
        surface_digests=(("future", digest),),
        dependencies=("provider",),
    )
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            basis_id="b" * 64,
            target_plan=TargetPlan.from_value([{"target": "future", "role": "persistent"}]),
            removal_targets=(),
        ),
    )
    monkeypatch.setattr(
        planned_dependencies, "load_planned_dependencies", lambda *_: materialization
    )
    monkeypatch.setattr(planned_dependencies, "_provider", lambda *_: provider)
    monkeypatch.setattr(planned_dependencies, "materialize_basis_checkout", lambda *_: source)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    assert validate_planned_dependencies(tmp_path, "consumer", "a" * 16, ticket) == materialization


def test_public_marker_load_rejects_misaligned_target_sets(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker.json"
    marker.write_bytes(
        planned_dependencies._serialize(
            ProviderMaterialization(
                materialized_targets=frozenset({"future"}),
                exported_targets=frozenset({"future"}),
                surface_digests=(("future", "b" * 64),),
            )
        )
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_args: marker)

    with pytest.raises(PlannedDependencyError, match="Target sets do not align"):
        load_planned_dependencies(tmp_path, "consumer", "a" * 16)


def test_public_marker_load_rejects_binding_outside_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    binding = ProviderTargetBinding("provider", "a" * 64, "future", "persistent", "b" * 64)
    marker = tmp_path / "marker.json"
    marker.write_bytes(
        planned_dependencies._serialize(
            ProviderMaterialization(
                bindings=(binding,),
                materialized_targets=frozenset({"future"}),
                exported_targets=frozenset({"future"}),
                surface_digests=(("future", "b" * 64),),
            )
        )
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_args: marker)

    with pytest.raises(PlannedDependencyError, match="not a dependency"):
        load_planned_dependencies(tmp_path, "consumer", "a" * 16)


def test_materialized_surface_rejects_injected_test_table_marker(tmp_path: Path) -> None:
    _core(tmp_path / "toy.core", "  future:\n    filesets: []\n")
    digest = target_surface_sha256(tmp_path, "future")
    materialization = ProviderMaterialization(
        surface_digests=(("future", digest),),
        test_tables=frozenset({"injected"}),
    )

    with pytest.raises(PlannedDependencyError, match=r"tests\.toml ownership changed"):
        validate_materialized_surfaces(tmp_path, materialization)


def test_public_marker_load_rejects_non_exportable_binding_role(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "marker.json"
    binding = {
        "provider": "provider",
        "basis_id": "a" * 64,
        "target": "future",
        "role": "ephemeral",
        "surface_sha256": "b" * 64,
    }
    marker.write_text(
        planned_dependencies.canonical_json(
            {
                "bindings": [binding],
                "materialized_targets": [],
                "exported_targets": [],
                "test_tables": [],
                "surface_digests": [],
                "dependencies": [],
            }
        ).decode(),
        encoding="utf-8",
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_args: marker)

    with pytest.raises(PlannedDependencyError, match="marker is invalid"):
        load_planned_dependencies(tmp_path, "consumer", "a" * 16)


def test_public_materialization_retries_after_atomic_surface_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [provider]\ncriteria: {}\n---\n", encoding="utf-8")
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _core(source / "toy.core", "  future:\n    filesets: []\n")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    provider = _Provider(
        "provider", {}, SimpleNamespace(target_plan=plan, basis_id="a" * 64, removal_targets=())
    )
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_args: [])
    monkeypatch.setattr(planned_dependencies, "_provider", lambda *_args: provider)
    monkeypatch.setattr(planned_dependencies, "materialize_basis_checkout", lambda *_args: source)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)
    real_replace = target_surface_edit.atomic_replace_bytes
    attempts = []

    def fail_once(path, payload, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError("surface interrupted")
        return real_replace(path, payload, **kwargs)

    monkeypatch.setattr(target_surface_edit, "atomic_replace_bytes", fail_once)
    with pytest.raises(PlannedDependencyError, match="surface interrupted"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, workspace)
    result = materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, workspace)

    assert result.materialized_targets == {"future"}
    assert (workspace / "toy.core").is_file()


def test_provider_materialization_uses_published_basis_surface(
    tmp_path: Path, monkeypatch
) -> None:
    published = tmp_path / "published"
    mutable = tmp_path / "mutable-ticket-ref"
    workspace = tmp_path / "workspace"
    _core(published / "toy.core", "  future:\n    filesets: [published]\n")
    _core(mutable / "toy.core", "  future:\n    filesets: [unpublished]\n")
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value([{"target": "future", "role": "persistent"}]),
            basis_id="a" * 64,
        ),
    )
    monkeypatch.setattr(planned_dependencies, "materialize_basis_checkout", lambda *_: published)
    monkeypatch.setattr(
        acceptance_basis, "materialize_current_ticket_checkout", lambda *_: mutable
    )

    result = _materialize_provider(tmp_path, provider, workspace, set(), {}, {"future"})

    copied = (workspace / "toy.core").read_text(encoding="utf-8")
    assert "published" in copied
    assert "unpublished" not in copied
    assert result.surface_digests == (("future", target_surface_sha256(published, "future")),)


def _materialize_placeholder_provider(tmp_path: Path, monkeypatch):
    source = tmp_path / "published"
    workspace = tmp_path / "workspace"
    _core(source / "toy.core", "  future:\n    filesets: [rtl]\n")
    placeholder = source / "rtl/future.sv"
    placeholder.parent.mkdir()
    placeholder.touch()
    provider = _Provider(
        "provider",
        {"scope": ["rtl/future.sv [new]"]},
        SimpleNamespace(
            target_plan=TargetPlan.from_value([{"target": "future", "role": "persistent"}]),
            basis_id="a" * 64,
        ),
    )
    target_input = SimpleNamespace(path="rtl/future.sv", file_type="systemVerilogSource", tags=())
    monkeypatch.setattr(planned_dependencies, "materialize_basis_checkout", lambda *_: source)
    monkeypatch.setattr(
        planned_dependencies,
        "_inspect_provider_inputs",
        lambda *_: (target_input,),
    )
    result = _materialize_provider(tmp_path, provider, workspace, set(), {}, {"future"})
    return workspace, target_input, result


def test_provider_new_input_is_not_copied_into_consumer(tmp_path: Path, monkeypatch) -> None:
    workspace, _target_input, result = _materialize_placeholder_provider(tmp_path, monkeypatch)

    assert result.placeholder_paths == {"rtl/future.sv"}
    assert not (workspace / "rtl/future.sv").exists()


def test_provider_new_input_is_a_consumer_validation_exemption(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, target_input, result = _materialize_placeholder_provider(tmp_path, monkeypatch)
    fields = {"scope": []}
    effective = workspace_ops._with_provider_placeholders(fields, result.placeholder_paths)
    assert fields == {"scope": []}
    assert effective["scope"] == ["rtl/future.sv [new]"]

    catalog = SimpleNamespace(select=lambda *_args, **_kwargs: SimpleNamespace(selector="future"))
    monkeypatch.setattr(
        acceptance_targets,
        "TargetCatalog",
        SimpleNamespace(build=lambda *_args: catalog),
    )
    monkeypatch.setattr(
        acceptance_targets,
        "_missing_target_inputs",
        lambda *_args: (target_input,),
    )
    criteria = {"criteria": {"mandatory": {"lint_clean": ["future"]}}}
    assert acceptance_targets.validate_criterion_targets(criteria, workspace)
    assert (
        acceptance_targets.validate_criterion_targets(
            {**criteria, "scope": effective["scope"]}, workspace
        )
        == []
    )


def test_existing_marker_restores_surfaces_into_recreated_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [provider]\ncriteria: {}\n---\n", encoding="utf-8")
    source = tmp_path / "published"
    first_workspace = tmp_path / "first-workspace"
    recreated = tmp_path / "recreated-workspace"
    _core(source / "toy.core", "  future:\n    filesets: [rtl]\n")
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value([{"target": "future", "role": "persistent"}]),
            basis_id="a" * 64,
            removal_targets=(),
        ),
    )
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_: [])
    monkeypatch.setattr(planned_dependencies, "_provider", lambda *_: provider)
    monkeypatch.setattr(planned_dependencies, "materialize_basis_checkout", lambda *_: source)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    expected = materialize_planned_dependencies(
        tmp_path, ticket, "consumer", "a" * 16, first_workspace
    )
    restored = materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, recreated)

    assert restored == expected
    assert (recreated / "toy.core").read_bytes() == (first_workspace / "toy.core").read_bytes()


def test_existing_empty_marker_rechecks_new_active_provider(tmp_path: Path, monkeypatch) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\ncriteria: {mandatory: {sim_pass: [future]}}\ndependencies: []\n---\n",
        encoding="utf-8",
    )
    marker = tmp_path / "marker.json"
    marker.write_bytes(planned_dependencies._serialize(ProviderMaterialization()))
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value(
                [{"target": "acme:lib:toy:1.0#future", "role": "persistent"}]
            ),
            removal_targets=(),
        ),
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_: marker)
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_: [provider])
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="missing provider dependencies"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_existing_empty_marker_rejects_new_export_from_existing_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\ncriteria: {mandatory: {sim_pass: [future]}}\ndependencies: [provider]\n---\n",
        encoding="utf-8",
    )
    marker = tmp_path / "marker.json"
    marker.write_bytes(
        planned_dependencies._serialize(ProviderMaterialization(dependencies=("provider",)))
    )
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(
            target_plan=TargetPlan.from_value(
                [{"target": "acme:lib:toy:1.0#future", "role": "persistent"}]
            ),
            removal_targets=(),
        ),
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_: marker)
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_: [provider])
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="provider exports changed"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_existing_marker_rechecks_new_ambiguous_provider(tmp_path: Path, monkeypatch) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [first]\ncriteria: {}\n---\n", encoding="utf-8")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    providers = [
        _Provider(
            slug,
            {},
            SimpleNamespace(target_plan=plan, removal_targets=(), basis_id="a" * 64),
        )
        for slug in ("first", "second")
    ]
    marker = tmp_path / "marker.json"
    marker.write_bytes(
        planned_dependencies._serialize(ProviderMaterialization(dependencies=("first",)))
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_: marker)
    monkeypatch.setattr(planned_dependencies, "_active_providers", lambda *_: providers)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)

    with pytest.raises(PlannedDependencyError, match="ambiguous"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_materialization_classifies_already_merged_provider_surface(
    tmp_path: Path, monkeypatch
) -> None:
    """A retry after file writes but before marker publication remains recoverable."""
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    provider = _Provider(
        "provider",
        {},
        SimpleNamespace(target_plan=plan, basis_id="a" * 64),
    )
    monkeypatch.setattr(
        planned_dependencies,
        "materialize_basis_checkout",
        lambda *_args: tmp_path / "provider-checkout",
    )
    monkeypatch.setattr(
        planned_dependencies,
        "_surface",
        lambda *_args: (Path("toy.core"), "future", "b" * 64),
    )
    monkeypatch.setattr(
        planned_dependencies,
        "_merge_provider_target",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        planned_dependencies,
        "_merge_provider_test_table",
        lambda *_args: False,
    )

    result = _materialize_provider(
        tmp_path,
        provider,
        tmp_path / "workspace",
        set(),
        {},
        {"future"},
    )

    assert result.materialized_targets == {"future"}
    assert result.test_tables == {"future"}
    assert result.surface_digests == (("future", "b" * 64),)


def test_materialized_provider_surface_cannot_be_authored_over(tmp_path: Path) -> None:
    core = tmp_path / "toy.core"
    _core(core, "  future:\n    filesets: [rtl]\n")
    expected = target_surface_sha256(tmp_path, "future")
    materialization = ProviderMaterialization(
        surface_digests=(("acme:lib:toy:1.0#future", expected),)
    )
    _core(core, "  future:\n    filesets: [different]\n")

    with pytest.raises(PlannedDependencyError, match="changed after composition"):
        validate_materialized_surfaces(tmp_path, materialization)


def test_public_materialization_rejects_changed_ticket_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [new-provider]\n---\n", encoding="utf-8")
    marker = tmp_path / "marker.json"
    marker.write_bytes(
        planned_dependencies._serialize(ProviderMaterialization(dependencies=("old-provider",)))
    )
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_args: marker)

    with pytest.raises(PlannedDependencyError, match="dependencies changed"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)


def test_public_materialization_pins_every_exported_provider_target(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [provider]\n---\n", encoding="utf-8")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    provider = _Provider(
        "provider",
        {"dependencies": []},
        SimpleNamespace(target_plan=plan, removal_targets=()),
    )
    binding = ProviderTargetBinding(
        "provider", "a" * 64, "acme:lib:toy:1.0#future", "persistent", "b" * 64
    )
    materialized = ProviderMaterialization(
        bindings=(binding,),
        materialized_targets=frozenset({binding.target}),
        exported_targets=frozenset({binding.target}),
        surface_digests=((binding.target, binding.surface_sha256),),
    )
    monkeypatch.setattr(planned_dependencies, "_provider", lambda *_args: provider)
    monkeypatch.setattr(planned_dependencies, "_materialize_provider", lambda *_args: materialized)
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)
    monkeypatch.setattr(
        planned_dependencies, "_marker_path", lambda *_args: tmp_path / "marker.json"
    )

    result = materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)

    assert result.bindings == (binding,)
    assert result.dependencies == ("provider",)


def test_public_materialization_retries_after_marker_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\ndependencies: [provider]\n---\n", encoding="utf-8")
    plan = TargetPlan.from_value([{"target": "future", "role": "persistent"}])
    provider = _Provider(
        "provider",
        {"dependencies": []},
        SimpleNamespace(target_plan=plan, removal_targets=()),
    )
    binding = ProviderTargetBinding("provider", "a" * 64, "future", "persistent", "b" * 64)
    materialized = ProviderMaterialization(
        bindings=(binding,),
        exported_targets=frozenset({"future"}),
        materialized_targets=frozenset({"future"}),
        surface_digests=(("future", "b" * 64),),
    )
    marker = tmp_path / "marker.json"
    calls = []
    writes = []
    monkeypatch.setattr(planned_dependencies, "_provider", lambda *_args: provider)
    monkeypatch.setattr(
        planned_dependencies,
        "_materialize_provider",
        lambda *_args: calls.append(True) or materialized,
    )
    monkeypatch.setattr(planned_dependencies, "resolve_checkout_project_dir", lambda root: root)
    monkeypatch.setattr(planned_dependencies, "_marker_path", lambda *_args: marker)

    def write_once(path, payload):
        writes.append(True)
        if len(writes) == 1:
            raise OSError("marker interrupted")
        path.write_bytes(payload)

    monkeypatch.setattr(planned_dependencies, "atomic_replace_bytes", write_once)

    with pytest.raises(OSError, match="marker interrupted"):
        materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)
    result = materialize_planned_dependencies(tmp_path, ticket, "consumer", "a" * 16, tmp_path)

    assert result.dependencies == ("provider",)
    assert calls == [True, True]
    assert marker.is_file()
