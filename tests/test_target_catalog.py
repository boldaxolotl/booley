"""Contract tests for the Project-scoped Target catalog."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from booley.fusesoc import fusesoc_registry, selftest_overlay, target_inspection
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    AmbiguousTargetError,
    ForeignTargetHandleError,
    IncompatibleTargetError,
    StaleTargetCatalogError,
    UnknownTargetError,
)


def _write_core(root: Path, filename: str, vlnv: str, targets: str) -> Path:
    path = root / filename
    target_block = textwrap.indent(textwrap.dedent(targets).strip() + "\n", "  ")
    path.write_text(
        "CAPI=2:\n"
        f"name: {vlnv}\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - target_lint_a ? (rtl/a.sv)\n"
        "      - target_lint_b ? (rtl/b.sv)\n"
        "targets:\n"
        + target_block,
        encoding="utf-8",
    )
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    (tmp_path / "rtl" / "b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    _write_core(
        tmp_path,
        "alpha.core",
        "acme:ip:alpha:1.0",
        """
        lint_a:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: a
        lint_b:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: b
        sim:
          flow: sim
          flow_options: {tool: icarus}
          filesets: [rtl]
          toplevel: a
        lint_private:
          flow: lint
          flow_options:
            tool: verilator
            booley: {doctor_selftest: true}
          filesets: [rtl]
          toplevel: a
        """,
    )
    _write_core(
        tmp_path,
        "beta.core",
        "acme:ip:beta:1.0",
        """
        sim:
          flow: sim
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: b
        """,
    )
    return tmp_path


def test_catalog_selects_and_lists_from_one_snapshot(project: Path) -> None:
    catalog = TargetCatalog.build(project)

    assert [handle.name for handle in catalog.list(for_flow="lint")] == ["lint_a", "lint_b"]
    assert catalog.select("alpha#sim", for_flow="sim").identity == "acme:ip:alpha:1.0#sim"
    with pytest.raises(AmbiguousTargetError):
        catalog.select("sim")
    with pytest.raises(IncompatibleTargetError):
        catalog.select("lint_a", for_flow="sim")
    with pytest.raises(UnknownTargetError):
        catalog.select("lint_private")


def test_doctor_authority_exposes_private_targets(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)

    handle = TargetCatalog.build(project).select("lint_private", for_flow="lint")

    assert handle.doctor_private is True


def test_unrelated_malformed_core_does_not_hide_valid_targets(project: Path) -> None:
    (project / "broken.core").write_text("CAPI=2:\n- not\n- a\n- mapping\n", encoding="utf-8")

    catalog = TargetCatalog.build(project)

    assert catalog.select("lint_a").identity == "acme:ip:alpha:1.0#lint_a"


def test_catalog_rejects_same_root_drift(project: Path) -> None:
    catalog = TargetCatalog.build(project)
    handle = catalog.select("lint_a")
    (project / "alpha.core").write_text(
        (project / "alpha.core").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )

    with pytest.raises(StaleTargetCatalogError):
        catalog.inspect(handle)
    with pytest.raises(StaleTargetCatalogError):
        fusesoc_registry.setup_command_for_handle(handle, build_root=project / "build")


def test_catalog_rejects_handle_from_another_snapshot(project: Path) -> None:
    first = TargetCatalog.build(project)
    handle = first.select("lint_a")
    (project / "alpha.core").write_text(
        (project / "alpha.core").read_text(encoding="utf-8") + "\n# generation two\n",
        encoding="utf-8",
    )
    second = TargetCatalog.build(project)

    with pytest.raises(ForeignTargetHandleError):
        second.inspect(handle)


def test_catalog_reuses_condition_safe_inspection(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managers = 0
    resolutions = 0
    real_manager = target_inspection.CoreManager
    real_get_depends = target_inspection.CoreManager.get_depends

    def counting_manager(*args, **kwargs):
        nonlocal managers
        managers += 1
        return real_manager(*args, **kwargs)

    def counting_get_depends(self, *args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        return real_get_depends(self, *args, **kwargs)

    monkeypatch.setattr(real_manager, "get_depends", counting_get_depends)
    monkeypatch.setattr(target_inspection, "CoreManager", counting_manager)
    catalog = TargetCatalog.build(project)
    a = catalog.select("lint_a")
    b = catalog.select("lint_b")

    observed = [
        catalog.inspect(handle).rtl_files
        for handle in (a, b, a)
    ]

    assert observed == [("rtl/a.sv",), ("rtl/b.sv",), ("rtl/a.sv",)]
    assert managers == 1
    assert resolutions == 2


def test_handle_setup_does_not_resolve_selector_again(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = TargetCatalog.build(project).select("lint_a")

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("execution re-resolved an authored token")

    monkeypatch.setattr(fusesoc_registry, "resolve_ref", unexpected_resolution)

    command = fusesoc_registry.setup_command_for_handle(
        handle,
        build_root=project / "build",
    )

    assert command[-3:] == ["--target", "lint_a", "acme:ip:alpha:1.0"]
