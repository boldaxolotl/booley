"""Contract tests for the Project-scoped Target catalog."""

from __future__ import annotations

import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.baseline_worktree import baseline_worktree
from booley.flows.sim.trace_overlay import write_trace_overlay
from booley.fusesoc import fusesoc_registry, selftest_overlay, target_inspection
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.harness.doctor import _check_sim_target_tb_staging
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_repositories import paired_project_repository
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    AmbiguousTargetError,
    DuplicateTargetError,
    ForeignTargetHandleError,
    IncompatibleTargetError,
    StaleTargetCatalogError,
    UnknownTargetError,
)
from tests.conftest import require_symlinks, symlink_or_skip


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


def test_catalog_rejects_duplicate_canonical_target_identities(project: Path) -> None:
    catalog = TargetCatalog.build(project)

    with pytest.raises(DuplicateTargetError, match=r"'lint_a'.*already selected by 'lint_a'"):
        catalog.select_many("lint_a,lint_a", for_flow="lint")
    with pytest.raises(
        DuplicateTargetError,
        match=r"'alpha#lint_a'.*acme:ip:alpha:1.0#lint_a.*'lint_a'",
    ):
        catalog.select_many("lint_a,alpha#lint_a", for_flow="lint")


def test_sim_selection_error_names_supported_tool_declarations(tmp_path: Path) -> None:
    _write_core(
        tmp_path,
        "missing-tool.core",
        "acme:ip:missing:1.0",
        """
        sim_missing:
          flow: sim
          filesets: []
          toplevel: top
        """,
    )

    with pytest.raises(IncompatibleTargetError) as exc:
        TargetCatalog.build(tmp_path).select("sim_missing", for_flow="sim")

    message = str(exc.value)
    assert "flow_options.tool" in message
    assert "default_tool" in message
    assert "Verilator or Icarus" in message


def test_catalog_select_many_preserves_authored_order(project: Path) -> None:
    selected = TargetCatalog.build(project).select_many("lint_b,lint_a", for_flow="lint")

    assert [handle.name for handle in selected] == ["lint_b", "lint_a"]


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


@pytest.mark.parametrize(
    ("target_body", "expected_flow", "expected_tool"),
    (
        (
            "flow: sim\nflow_options:\n  tool: verilator\n  make_options: [OPT_FAST=-O3]",
            ("OPT_FAST=-O3",),
            (),
        ),
        (
            "default_tool: verilator\ntools:\n  verilator:\n    make_options: [-j7]",
            (),
            ("-j7",),
        ),
    ),
    ids=("modern-flow-api", "legacy-tool-api"),
)
def test_inspection_exposes_condition_selected_backend_options(
    tmp_path: Path,
    target_body: str,
    expected_flow: tuple[str, ...],
    expected_tool: tuple[str, ...],
) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    _write_core(
        tmp_path,
        "options.core",
        "acme:ip:options:1.0",
        f"sim:\n{textwrap.indent(target_body, '  ')}\n  filesets: [rtl]\n  toplevel: a\n",
    )

    catalog = TargetCatalog.build(tmp_path)
    inspection = catalog.inspect(catalog.select("sim", for_flow="sim"))

    assert tuple(inspection.flow_options.get("make_options", ())) == expected_flow
    assert tuple(inspection.tool_options.get("make_options", ())) == expected_tool


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
    (root / ".booley_project").mkdir()
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
    _git(project, "commit", "-m", "Booley Directory")
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


def test_inspection_rejects_invalid_parameter_before_missing_source_deferral(
    tmp_path: Path,
) -> None:
    (tmp_path / "invalid.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:invalid:1.0\n"
        "filesets:\n"
        "  rtl: {files: [rtl/not-yet-authored.sv]}\n"
        "parameters:\n"
        "  ENABLE_ZBB: {datatype: int, paramtype: vlogparam, default: 0}\n"
        "targets:\n"
        "  lint_new:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "    parameters: [ENABLE_ZBB=not-an-int]\n"
        "    toplevel: future\n",
        encoding="utf-8",
    )

    catalog = TargetCatalog.build(tmp_path)

    with pytest.raises(fusesoc_registry.FuseSocError, match="invalid literal"):
        catalog.inspect(catalog.select("lint_new"))


def test_inspection_resolves_parameter_supplied_by_dependency(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (tmp_path / "dependency.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:dependency:1.0\n"
        "parameters:\n"
        "  WIDTH: {datatype: int, paramtype: vlogparam, default: 8}\n"
        "targets:\n"
        "  default: {parameters: [WIDTH]}\n",
        encoding="utf-8",
    )
    (tmp_path / "top.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:top:1.0\n"
        "filesets:\n"
        "  rtl: {files: [rtl/top.sv], depend: [acme:ip:dependency:1.0]}\n"
        "targets:\n"
        "  lint:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "    parameters: [WIDTH=16]\n"
        "    toplevel: top\n",
        encoding="utf-8",
    )

    inspection = TargetCatalog.build(tmp_path).inspect(
        TargetCatalog.build(tmp_path).select("lint")
    )

    assert inspection.parameters["WIDTH"]["default"] == 16


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


@pytest.mark.parametrize("eda_tool", ["verible", "veriblelint"])
@pytest.mark.parametrize("missing", [False, True])
def test_mixed_lint_unfiltered_conditional_inputs_control(
    tmp_path, monkeypatch, eda_tool, missing
):
    for filename in ("top.sv", "rules.cfg", "waivers.txt"):
        (tmp_path / filename).write_text("module top; endmodule\n" if filename == "top.sv" else "")
    selection = (
        f"default_tool: {eda_tool}"
        if missing
        else f"flow_options: {{tool: {eda_tool}, ruleset: all}}"
    )
    (tmp_path / "conditional.core").write_text(
        "CAPI=2:\nname: acme:ip:conditional:1.0\nfilesets:\n  rtl:\n    files: [top.sv]\n    file_type: systemVerilogSource\n  config:\n    files:\n      - rules.cfg: {file_type: veribleLintRules}\n      - waivers.txt: {file_type: veribleLintWaiver}\ntargets:\n  lint_style:\n    flow: lint\n"
        f"    {selection}\n    filesets: [rtl, 'tool_{eda_tool} ? (config)']\n    toplevel: top\n"
    )
    from booley.fusesoc import fusesoc_registry

    original = fusesoc_registry.target_source_files_for_ref
    copied = []

    def capture(root, ref, **kwargs):
        copied.append(ref)
        return original(root, ref, **kwargs)

    monkeypatch.setattr(fusesoc_registry, "target_source_files_for_ref", capture)
    catalog = TargetCatalog.build(tmp_path)
    handle = catalog.select("lint_style")
    inspection = catalog.inspect(handle)
    assert handle.eda_tool == eda_tool
    assert handle.lint_flow_eda_tool_missing is missing
    assert copied[-1].lint_flow_eda_tool_missing is missing
    assert {item.path for item in inspection.inputs} == {"top.sv", "rules.cfg", "waivers.txt"}
    assert inspection.eda_tool == eda_tool
    if not missing:
        assert inspection.flow_options["tool"] == eda_tool
        assert inspection.flow_options["ruleset"] == "all"


@pytest.mark.parametrize(
    "name,flow,eda_tool,expected",
    [
        ("synth_vehicle", "lint", "yosys", "synth"),
        ("implementation", "lint", "vivado", "fpga"),
        ("fpga_top", "lint", "verible", "fpga"),
        ("sim_control", "sim", "verilator", "sim"),
    ],
)
def test_mixed_lint_other_flow_classification_control(tmp_path, name, flow, eda_tool, expected):
    (tmp_path / "control.core").write_text(
        "CAPI=2:\nname: acme:ip:control:1.0\ntargets:\n"
        + f"  {name}:\n    flow: {flow}\n    default_tool: {eda_tool}\n"
    )
    handle = TargetCatalog.build(tmp_path).select(name, for_flow=expected)
    assert handle.eda_tool == eda_tool
    assert expected in handle.drivable_by


def _author_alias_core(root: Path, layout: str) -> tuple[str, str]:
    core_root = root / "nested" if layout == "nested" else root
    core_root.mkdir(exist_ok=True)
    for directory, module in (("rtl", "dut"), ("tb", "tb")):
        (core_root / directory).mkdir()
        (core_root / directory / f"{module}.sv").write_text(f"module {module}; endmodule\n")
    tb_decl = "tb/tb.sv"
    prefix = "nested/" if layout == "nested" else ""
    if layout in {"recursive", "declared"}:
        require_symlinks(root)
        if layout == "recursive":
            (root / "lib").mkdir()
            symlink_or_skip(root / "lib/self", Path(".."), target_is_directory=True)
            assert (root / "lib/self").is_dir()
            assert (root / "lib/self").resolve() == root.resolve()
            prefix = "lib/self/"
        else:
            symlink_or_skip(root / "rtl/tblink", "../tb", target_is_directory=True)
            tb_decl = "rtl/tblink/tb.sv"
    (core_root / "design.core").write_text(
        "CAPI=2:\nname: ::alias_dut:0\nfilesets:\n"
        "  rtl:\n    files: [rtl/dut.sv]\n    file_type: systemVerilogSource\n"
        f"  tb:\n    files: [{tb_decl}]\n    tags: [tb]\n"
        "    file_type: systemVerilogSource\ntargets:\n"
        "  sim_dut:\n    flow: sim\n    flow_options: {tool: icarus}\n"
        "    filesets: [rtl, tb]\n    toplevel: tb\n"
    )
    return prefix, tb_decl


@pytest.mark.parametrize("layout", ["control", "recursive", "declared", "nested"])
@pytest.mark.parametrize("strip_tags", [False, True])
def test_catalog_restores_tb_tags_across_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, strip_tags: bool
) -> None:
    root = tmp_path
    prefix, tb_decl = _author_alias_core(root, layout)
    if layout == "recursive":
        original_plan = fusesoc_registry.prepare_core_library_plan

        def with_alias_library(project_root):
            plan = original_plan(project_root)
            return replace(
                plan,
                roots=(*plan.roots, root / "lib/self"),
                ignored_dirs=(*plan.ignored_dirs, frozenset({str((root / "lib").resolve())})),
            )

        # Real duplicate-core discovery, independent of platform traversal order.
        monkeypatch.setattr(fusesoc_registry, "prepare_core_library_plan", with_alias_library)
    stripped_calls = []
    if strip_tags:
        from fusesoc.capi2.core import CoreInterface as Core

        original = Core.get_files

        def without_tags(self, flags):
            stripped_calls.append(self)
            return [{**item, "tags": []} for item in original(self, flags)]

        monkeypatch.setattr(Core, "get_files", without_tags)
    catalog = TargetCatalog.build(root)
    inspection = catalog.inspect(catalog.select("sim_dut"))
    if strip_tags:
        assert stripped_calls
    assert tuple(item.path for item in inspection.inputs) == (
        prefix + "rtl/dut.sv",
        prefix + tb_decl,
    )
    assert inspection.rtl_files == (prefix + "rtl/dut.sv",)
    assert inspection.tb_files == (prefix + tb_decl,)
    assert inspection.inputs[1].tags.count("tb") == 1
    assert all(item.file_type == "systemVerilogSource" for item in inspection.inputs)
    passed, failed = [], []
    _check_sim_target_tb_staging(
        "sim_dut", inspection.sources, passed.append, lambda *args: failed.append(args)
    )
    assert passed == ["sim Target 'sim_dut' TB fileset tagged"]
    assert failed == []


@pytest.mark.parametrize("tags", [("other",), ("other", "tb")])
def test_tb_tag_restoration_preserves_input_metadata(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    from types import SimpleNamespace

    (tmp_path / "tb").mkdir()
    symlink_or_skip(tmp_path / "alias", "tb", target_is_directory=True)
    item = {
        "name": "alias/tb.sv",
        "tags": tags,
        "file_type": "systemVerilogSource",
        "is_include_file": True,
        "copyto": "copied.sv",
    }
    core = SimpleNamespace(
        name="::test:0", core_file=str(tmp_path / "test.core"), get_files=lambda flags: [item]
    )
    inspected = target_inspection._inspect_inputs(tmp_path, [core], {}, tb_paths=["tb/tb.sv"])
    assert inspected[0].path == "alias/tb.sv"
    assert inspected[0].tags == ("other", "tb")
    assert inspected[0].is_include is True
    assert inspected[0].file_type == item["file_type"]
    assert inspected[0].core == core.name
    assert inspected[0].attributes == {key: value for key, value in item.items() if key != "name"}
    assert item["tags"] == tags


@pytest.mark.parametrize(
    "default,reference,expected",
    [
        ("", "NAME", ""),
        ("fallback", "NAME=", ""),
        ("", "NAME=other", "other"),
        (None, "NAME", None),
        ("fallback", "NAME", "fallback"),
    ],
)
def test_inspection_preserves_selected_explicit_empty_string(
    tmp_path, default, reference, expected
):
    import yaml

    parameter = {"datatype": "str", "paramtype": "vlogparam"}
    if default is not None:
        parameter["default"] = default
    (tmp_path / "proof.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "proof:string:empty:1",
                "parameters": {"NAME": parameter},
                "targets": {
                    "sim": {
                        "flow": "sim",
                        "flow_options": {"tool": "verilator"},
                        "parameters": [reference],
                        "toplevel": "top",
                    }
                },
            }
        )
    )
    catalog = TargetCatalog.build(tmp_path)
    handle = catalog.select("sim", for_flow="sim")
    parameters = catalog.inspect(handle).parameters
    assert parameters["NAME"].get("default") == expected
    if expected is None:
        assert "default" not in parameters["NAME"]


@pytest.mark.parametrize(
    "dependency,local,reference,expected",
    [
        ("", None, "NAME", ""),
        ("", {}, "NAME", None),
        ("dependency", {}, "NAME", "dependency"),
        ("", {"default": "local"}, "NAME", "local"),
        ("", None, "NAME=assigned", "assigned"),
        ("dependency", None, "NAME=", ""),
    ],
)
def test_empty_dependency_intent_preserves_upstream_nonempty_precedence(
    tmp_path, dependency, local, reference, expected
):
    import yaml

    metadata = {"datatype": "str", "paramtype": "vlogparam"}
    dependency_core = {
        "name": "proof:string:dependency:1",
        "parameters": {"NAME": {**metadata, "default": dependency}},
        "targets": {"default": {"parameters": ["NAME"]}},
    }
    top = {
        "name": "proof:string:top:1",
        "filesets": {"rtl": {"depend": ["proof:string:dependency:1"]}},
        "targets": {
            "sim": {
                "flow": "sim",
                "flow_options": {"tool": "verilator"},
                "filesets": ["rtl"],
                "parameters": [reference],
                "toplevel": "top",
            }
        },
    }
    if local is not None:
        top["parameters"] = {"NAME": {**metadata, **local}}
    for filename, data in (("dep.core", dependency_core), ("top.core", top)):
        (tmp_path / filename).write_text("CAPI=2:\n" + yaml.safe_dump(data))
    catalog = TargetCatalog.build(tmp_path)
    actual = catalog.inspect(catalog.select("sim", for_flow="sim")).parameters["NAME"]
    assert actual.get("default") == expected
    if expected is None:
        assert "default" not in actual


def test_authored_flag_selects_empty_parameter_and_dependency(tmp_path):
    import yaml

    from booley.fusesoc import fusesoc_registry

    (tmp_path / "dependency.sv").write_text("module dependency; endmodule\n")
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    (tmp_path / "conditional_tb.sv").write_text("module conditional_tb; endmodule\n")

    (tmp_path / "dep.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "proof:string:dependency:1",
                "parameters": {
                    "NAME": {"datatype": "str", "paramtype": "vlogparam", "default": ""}
                },
                "filesets": {"rtl": {"files": ["dependency.sv"]}},
                "targets": {"default": {"parameters": ["NAME"], "filesets": ["rtl"]}},
            }
        )
    )
    (tmp_path / "top.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "proof:string:conditional:1",
                "filesets": {
                    "rtl": {
                        "files": ["top.sv", {"owned ? (conditional_tb.sv)": {"tags": ["tb"]}}],
                        "depend": ["owned ? (proof:string:dependency:1)"],
                    },
                },
                "targets": {
                    "sim": {
                        "flow": "sim",
                        "flow_options": {"tool": "verilator"},
                        "flags": {"owned": True},
                        "filesets": ["rtl"],
                        "parameters": ["owned ? (NAME)"],
                        "toplevel": "top",
                    }
                },
            }
        )
    )
    catalog = TargetCatalog.build(tmp_path)
    handle = catalog.select("sim", for_flow="sim")
    inspection = catalog.inspect(handle)
    assert inspection.parameters["NAME"]["default"] == ""
    assert {item.path for item in inspection.inputs} == {"top.sv", "dependency.sv"}
    ref = fusesoc_registry._resolve_ref(tmp_path, "sim")
    sources = fusesoc_registry.target_source_files_for_ref(tmp_path, ref)
    assert "conditional_tb.sv" in sources.tb_files
    assert "top.sv" in sources.rtl_source_files
