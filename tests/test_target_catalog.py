"""Contract tests for the Project-scoped Target catalog."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.flows.baseline_worktree import baseline_worktree
from booley.flows.sim.trace_overlay import write_trace_overlay
from booley.fusesoc import fusesoc_registry, selftest_overlay, target_inspection
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_repositories import paired_project_repository
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
        "targets:\n" + target_block,
        encoding="utf-8",
    )
    return path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _init_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")


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
        catalog.list()
    with pytest.raises(StaleTargetCatalogError):
        catalog.select("lint_a")
    with pytest.raises(StaleTargetCatalogError):
        catalog.inspect(handle)
    with pytest.raises(StaleTargetCatalogError):
        catalog.core_closure([handle])
    with pytest.raises(StaleTargetCatalogError):
        fusesoc_registry.setup_command_for_handle(handle, build_root=project / "build")


def test_trace_overlay_rejects_stale_handle_before_writing(project: Path) -> None:
    handle = TargetCatalog.build(project).select("alpha#sim", for_flow="sim")
    (project / "alpha.core").write_text(
        (project / "alpha.core").read_text(encoding="utf-8") + "\n# drifted\n",
        encoding="utf-8",
    )

    with pytest.raises(StaleTargetCatalogError):
        write_trace_overlay(handle)

    assert not tuple(project.rglob(f"*{TRACE_OVERLAY_MARKER}*"))


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
    with pytest.raises(ForeignTargetHandleError):
        second.core_closure([handle])


def test_catalog_rejects_unknown_flow_filter(project: Path) -> None:
    with pytest.raises(ValueError, match="target-aware"):
        TargetCatalog.build(project).list(for_flow="unknown")


def test_catalog_fails_loudly_when_frozen_document_is_missing(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TargetCatalog.build(project)
    monkeypatch.setattr(catalog._state, "documents", {})  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match="omitted declaration document"):
        catalog.list()


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

    observed = [catalog.inspect(handle).rtl_files for handle in (a, b, a)]

    assert observed == [("rtl/a.sv",), ("rtl/b.sv",), ("rtl/a.sv",)]
    assert managers == 1
    assert resolutions == 2


def test_cached_inspection_mappings_are_deeply_immutable(project: Path) -> None:
    catalog = TargetCatalog.build(project)
    handle = catalog.select("lint_a")
    inspection = catalog.inspect(handle)

    with pytest.raises(TypeError):
        inspection.flow_options["tool"] = "other"  # type: ignore[index]
    with pytest.raises(TypeError):
        inspection.inputs[0].attributes["mutated"] = True  # type: ignore[index]

    cached = catalog.inspect(handle)
    assert cached.flow_options["tool"] == "verilator"
    assert "mutated" not in cached.inputs[0].attributes


def test_inspection_normalizes_windows_fileset_paths_on_posix(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    (tmp_path / "portable.core").write_text(
        "CAPI=2:\n"
        "name: acme:portable:path:1.0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files: ['rtl\\\\a.sv']\n"
        "targets:\n"
        "  lint:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "    toplevel: a\n",
        encoding="utf-8",
    )

    catalog = TargetCatalog.build(tmp_path)
    inspection = catalog.inspect(catalog.select("lint", for_flow="lint"))

    assert inspection.rtl_files == ("rtl/a.sv",)


def test_real_baseline_worktree_receives_an_independent_catalog(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repository(root)
    (root / ".gitignore").write_text("/.booley_project/\n", encoding="utf-8")
    (root / "rtl").mkdir()
    (root / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    _write_core(
        root,
        "design.core",
        "acme:worktree:design:1.0",
        """
        lint:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: baseline_top
        """,
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline_sha = _git(root, "rev-parse", "HEAD")
    core = root / "design.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace("baseline_top", "active_top"),
        encoding="utf-8",
    )
    _git(root, "add", "design.core")
    _git(root, "commit", "-m", "active")
    active_catalog = TargetCatalog.build(root)
    active = active_catalog.select("lint")

    with baseline_worktree(root, baseline_sha) as checkout:
        baseline_catalog = TargetCatalog.build(checkout)
        baseline = baseline_catalog.select("lint")

        assert active.declared_toplevel == "active_top"
        assert baseline.declared_toplevel == "baseline_top"
        assert baseline.project_root == checkout.resolve()
        with pytest.raises(ForeignTargetHandleError):
            active_catalog.inspect(baseline)
        with pytest.raises(ForeignTargetHandleError):
            fusesoc_registry.require_current_target_handle(
                active,
                project_root=baseline.project_root,
            )


def test_paired_project_worktree_targets_belong_to_outer_checkout(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    _init_repository(outer)
    (outer / ".gitignore").write_text("/.booley_project\n", encoding="utf-8")
    (outer / "README.md").write_text("outer\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-m", "outer")

    project = tmp_path / "project-data"
    _init_repository(project)
    (project / "cores" / "rtl").mkdir(parents=True)
    (project / "cores" / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    _write_core(
        project / "cores",
        "paired.core",
        "acme:paired:design:1.0",
        """
        lint:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: a
        """,
    )
    _git(project, "add", ".")
    _git(project, "commit", "-m", "project data")
    _git(project, "worktree", "add", "--detach", str(outer / ".booley_project"), "HEAD")

    paired = paired_project_repository(outer)
    assert paired is not None
    catalog = TargetCatalog.build(outer)
    handle = catalog.select("lint", for_flow="lint")

    assert paired.worktree == outer / ".booley_project"
    assert handle.project_root == outer.resolve()
    assert handle.core_file == (paired.worktree / "cores" / "paired.core").resolve()


def test_external_project_directory_does_not_retarget_catalog_root(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "project-data"
    external.mkdir()
    (checkout / "booley.toml").write_text('[project]\ndir = "../project-data"\n', encoding="utf-8")
    (checkout / "rtl").mkdir()
    (checkout / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    _write_core(
        checkout,
        "external.core",
        "acme:external:checkout:1.0",
        """
        lint:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: a
        """,
    )

    catalog = TargetCatalog.build(checkout)
    handle = catalog.select("lint", for_flow="lint")

    assert resolve_checkout_project_dir(checkout) == external.resolve()
    assert handle.project_root == checkout.resolve()
    assert handle.core_file == (checkout / "external.core").resolve()


def test_prospective_surface_refresh_rebuilds_its_catalog(tmp_path: Path) -> None:
    prospective = tmp_path / "prospective"
    (prospective / "rtl").mkdir(parents=True)
    (prospective / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    (prospective / "rtl" / "b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    _write_core(
        prospective,
        "surface.core",
        "acme:surface:prospective:1.0",
        """
        sim:
          flow: sim
          flow_options: {tool: icarus}
          filesets: [rtl]
          toplevel: a
        """,
    )
    catalog = TargetCatalog.build(prospective)
    handle = catalog.select("sim", for_flow="sim")
    _write_core(
        prospective,
        "provider-composed.core",
        "acme:provider:composed:1.0",
        """
        lint:
          flow: lint
          flow_options: {tool: verilator}
          filesets: [rtl]
          toplevel: b
        """,
    )
    with pytest.raises(StaleTargetCatalogError):
        catalog.inspect(handle)
    refreshed = TargetCatalog.build(prospective)
    assert refreshed.select("lint", for_flow="lint").identity.endswith("#lint")


def test_handle_setup_does_not_resolve_selector_again(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = TargetCatalog.build(project).select("lint_a")

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("execution re-resolved an authored token")

    monkeypatch.setattr(fusesoc_registry, "_resolve_ref", unexpected_resolution)

    command = fusesoc_registry.setup_command_for_handle(
        handle,
        build_root=project / "build",
    )

    assert command[-3:] == ["--target", "lint_a", "acme:ip:alpha:1.0"]
