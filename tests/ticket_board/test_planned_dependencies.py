"""Prospective provider surface composition tests."""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.models import TargetPlan
from booley.ticket_board import planned_dependencies
from booley.ticket_board.planned_dependencies import (
    PlannedDependencyError,
    ProviderMaterialization,
    _materialize_provider,
    _merge_provider_target,
    _merge_provider_test_table,
    _Provider,
    target_surface_sha256,
    validate_materialized_surfaces,
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


def test_surface_digest_includes_owned_test_table(tmp_path: Path) -> None:
    _core(tmp_path / "toy.core", "  future:\n    filesets: [rtl]\n")
    project = tmp_path / ".booley_project"
    project.mkdir()
    tests = project / "tests.toml"
    tests.write_text("[future]\nmodule = 'one'\n", encoding="utf-8")
    first = target_surface_sha256(tmp_path, "future")
    tests.write_text("[future]\nmodule = 'two'\n", encoding="utf-8")

    assert target_surface_sha256(tmp_path, "future") != first


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
        "materialize_current_ticket_checkout",
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
