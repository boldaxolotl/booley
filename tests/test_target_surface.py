"""Tests for target_surface: the `booley targets` / booley_targets MCP surface."""

from __future__ import annotations

import functools
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.core.checkout_role import SourceCheckoutProjectError
from booley.flows.synth.recipe import synth_target_tools
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.core_projection import (
    CoreProjectionError,
    projected_core_path,
    reconcile_projected_cores,
)
from booley.harness import targets_view
from booley.targets import target_surface
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    AmbiguousTargetError,
    IncompatibleTargetError,
    TargetHandle,
    TargetRef,
    UnknownTargetError,
)
from booley.targets.selection import minimal_selector
from booley.targets.target_surface import (
    TARGET_AWARE_FLOWS,
    collect_surface,
    filter_surface,
    is_glob,
)
from booley.targets.target_surface import (
    detail_payload as _detail_payload,
)
from booley.targets.target_surface import (
    surface_payload as _surface_payload,
)
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import fusesoc_setup_runner
from tests.target_test_support import make_target_handle

detail_payload = functools.partial(_detail_payload, synth_tools=synth_target_tools)
surface_payload = functools.partial(_surface_payload, synth_tools=synth_target_tools)


def select_target(project_root, token, *, for_flow=None):
    return TargetCatalog.build(project_root).select(token, for_flow=for_flow)


def select_targets(project_root, target_arg, *, for_flow=None):
    return TargetCatalog.build(project_root).select_many(target_arg, for_flow=for_flow)


def inspect_target(project_root, handle):
    return TargetCatalog.build(project_root).inspect(handle)


_ALPHA_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:ip:alpha:1.0

    filesets:
      rtl:
        files:
          - rtl/alpha.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_alpha.sv: {file_type: systemVerilogSource}
        tags: [tb]

    targets:
      default:
        filesets: [rtl]
      sim:
        flow: sim
        flow_options:
          tool: verilator
          cocotb_module: tb_alpha_tests
          booley: {doctor: [sim]}
        filesets: [rtl, tb]
        toplevel: tb_alpha
      lint:
        flow: lint
        flow_options: {tool: verible, booley: {doctor: [lint]}}
        filesets: [rtl]
        toplevel: alpha
      synth:
        flow: generic
        flow_options: {tool: yosys, arch: xilinx, booley: {doctor: [synth]}}
        filesets: [rtl]
        toplevel: alpha
    """
)

_BETA_CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:ip:beta:1.0

    filesets:
      rtl:
        files:
          - rtl/beta.sv: {file_type: systemVerilogSource}

    targets:
      default:
        filesets: [rtl]
      lint:
        flow: lint
        flow_options: {tool: verilator}
        filesets: [rtl]
      fpga:
        flow: generic
        flow_options: {tool: verilator}
        filesets: [rtl]
        toplevel: beta
      smoke:
        default_tool: iverilog
        tools:
          iverilog: {}
        filesets: [rtl]
        toplevel: beta
      lint_selftest_bad:
        flow: lint
        flow_options: {tool: verilator, booley: {doctor_selftest: true}}
        filesets: [rtl]
    """
)

_BOOLEY_TOML = "[flows]\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "alpha.core").write_text(_ALPHA_CORE, encoding="utf-8")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "beta.core").write_text(_BETA_CORE, encoding="utf-8")
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(_BOOLEY_TOML, encoding="utf-8")
    return tmp_path


def _rtl(root: Path, *modules: str) -> None:
    (root / "rtl").mkdir(exist_ok=True)
    for module in modules:
        (root / "rtl" / f"{module}.sv").write_text(
            f"module {module}; endmodule\n", encoding="utf-8"
        )


def _synth_core(module: str) -> str:
    return (
        "CAPI=2:\nname: booley::demo:0\nfilesets:\n"
        f"  rtl:\n    files: [rtl/{module}.sv]\ntargets:\n  synth:\n    flow: generic\n"
        f"    flow_options: {{tool: yosys, arch: xilinx}}\n    filesets: [rtl]\n    toplevel: {module}\n"
    )


def _lint_core(toplevel: str) -> str:
    return (
        "CAPI=2:\nname: booley::demo:0\ntargets:\n  lint:\n    flow: lint\n"
        f"    flow_options: {{tool: verible}}\n    toplevel: {toplevel}\n"
    )


def _stealth_core(
    root: Path, core_text: str, stealth: str = "enabled = true", *, ignore: bool = True
) -> Path:
    """Author ``.booley/cores/demo.core`` under *stealth* settings; create its RTL."""
    booley_dir = root / ".booley"
    (booley_dir / "cores").mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(f"[stealth]\n{stealth}\n", encoding="utf-8")
    if ignore:
        (booley_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    _rtl(root, *re.findall(r"rtl/(\w+)\.sv", core_text))
    authored = booley_dir / "cores" / "demo.core"
    authored.write_text(core_text, encoding="utf-8")
    return authored


def _touch(root: Path, *relative: str) -> None:
    for rel in relative:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()


def _entry(surface: target_surface.TargetSurface, selector: str) -> TargetHandle:
    return next(e for e in surface.entries() if e.selector == selector)


class TestTargetInterface:
    def test_all_target_aware_flows_share_canonical_selection_contract(
        self,
        project: Path,
    ) -> None:
        cases = {
            "lint": ("acme:ip:alpha:1.0#lint", "alpha#lint"),
            "sim": ("acme:ip:alpha:1.0#sim", "sim"),
            "synth": ("acme:ip:alpha:1.0#synth", "synth"),
            "fpga": ("acme:ip:beta:1.0#fpga", "fpga"),
        }

        selected = {
            flow: select_target(project, authored, for_flow=flow)
            for flow, (authored, _expected) in cases.items()
        }

        assert {flow: handle.selector for flow, handle in selected.items()} == {
            flow: expected for flow, (_authored, expected) in cases.items()
        }
        assert all(handle.project_root == project.resolve() for handle in selected.values())
        with pytest.raises(AmbiguousTargetError):
            select_target(project, "lint", for_flow="lint")
        with pytest.raises(IncompatibleTargetError):
            select_target(project, "sim", for_flow="lint")

    def test_inspection_names_rejected_core_and_fusesoc_message(self, tmp_path: Path):
        """FuseSoC drops a schema-invalid core; inspection says which and why."""
        authored = tmp_path / "bad.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:ip:bad:0
                targets:
                  good_target:
                    flow: lint
                    flow_options: {tool: verilator}
                  bad_target:
                    flow: lint
                    flow_options: {tool: verilator}
                    booley: {doctor: [lint]}
                """
            ),
            encoding="utf-8",
        )
        handle = select_target(tmp_path, "good_target")

        with pytest.raises(fusesoc_registry.FuseSocError) as excinfo:
            inspect_target(tmp_path, handle)

        message = str(excinfo.value)
        assert "FuseSoC rejected" in message
        assert "bad.core" in message
        assert "targets.bad_target" in message
        assert "booley" in message

    def test_inspection_uses_projected_view_of_stealth_authored_core(self, tmp_path: Path):
        authored = _stealth_core(tmp_path, _synth_core("demo"))

        inspection = inspect_target(tmp_path, select_target(tmp_path, "synth"))

        assert inspection.handle.core_file == authored
        assert [item.path for item in inspection.inputs] == ["rtl/demo.sv"]
        assert inspection.rtl_files == ("rtl/demo.sv",)

    def test_inspection_refreshes_owned_stale_projection(self, tmp_path: Path):
        authored = _stealth_core(tmp_path, _synth_core("old"))
        _rtl(tmp_path, "fresh")
        reconcile_projected_cores(tmp_path)
        authored.write_text(_synth_core("fresh"), encoding="utf-8")

        inspection = inspect_target(tmp_path, select_target(tmp_path, "synth"))

        assert inspection.toplevel == "fresh"
        assert [item.path for item in inspection.inputs] == ["rtl/fresh.sv"]

    def test_inspection_uses_state_core_as_second_library_when_projection_is_disabled(
        self, tmp_path: Path
    ):
        authored = _stealth_core(tmp_path, _lint_core("demo"), "enabled = false")

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "demo"
        assert inspection.inputs == ()

    def test_inspection_ignores_generated_build_core_copy(self, tmp_path: Path):
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = false\n", encoding="utf-8"
        )
        authored = tmp_path / "demo.core"
        authored.write_text(_lint_core("authored"), encoding="utf-8")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "stale.core").write_text(_lint_core("stale"), encoding="utf-8")

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "authored"

    def test_inspection_ignores_state_worktree_core_copies(self, tmp_path: Path):
        authored = _stealth_core(tmp_path, _lint_core("authored"), ignore=False)
        reconcile_projected_cores(tmp_path)
        for state_copy in (
            tmp_path / ".booley" / "worktrees" / "ticket" / "stale.core",
            tmp_path / ".booley" / ".baseline-wt-old" / "stale.core",
        ):
            state_copy.parent.mkdir(parents=True)
            state_copy.write_text(_lint_core("stale"), encoding="utf-8")

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "authored"

    @pytest.mark.parametrize(
        "excluded_tree",
        [Path(".booley/worktrees/ticket"), Path("build/cache")],
        ids=["project-state", "generated-build"],
    )
    def test_inspection_prunes_symlink_aliases_into_excluded_trees(
        self, tmp_path: Path, excluded_tree: Path
    ):
        hidden_root = tmp_path / excluded_tree
        hidden_root.mkdir(parents=True)
        (hidden_root / "hidden.core").write_text(
            "CAPI=2:\nname: booley::hidden:0\n",
            encoding="utf-8",
        )
        (tmp_path / "top.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::top:0
                filesets:
                  rtl:
                    depend: [booley::hidden:0]
                targets:
                  lint:
                    flow: lint
                    flow_options: {tool: verible}
                    filesets: [rtl]
                    toplevel: top
                """
            ),
            encoding="utf-8",
        )
        symlink_or_skip(tmp_path / "shadow-alias", hidden_root, target_is_directory=True)

        with pytest.raises(fusesoc_registry.FuseSocError, match="could not inspect Target"):
            inspect_target(tmp_path, select_target(tmp_path, "lint"))

    def test_inspection_refuses_foreign_expected_projection(self, tmp_path: Path):
        authored = _stealth_core(tmp_path, _lint_core("demo"), ignore=False)
        projected = projected_core_path(tmp_path, authored)
        foreign_content = "CAPI=2:\nname: foreign::core:0\n"
        projected.write_text(foreign_content, encoding="utf-8")

        with pytest.raises(
            fusesoc_registry.FuseSocError,
            match="refusing to overwrite non-Booley file",
        ) as failure:
            inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert isinstance(failure.value.__cause__, CoreProjectionError)
        assert projected.read_text(encoding="utf-8") == foreign_content

    def test_inspection_uses_isolated_registry_when_native_cores_are_ignored(self, tmp_path: Path):
        authored = _stealth_core(
            tmp_path, _synth_core("demo"), "enabled = true\nignore_native_cores = true"
        )
        (tmp_path / "native.core").write_text("not valid CAPI2\n", encoding="utf-8")
        reconcile_projected_cores(tmp_path)

        inspection = inspect_target(tmp_path, select_target(tmp_path, "synth"))

        assert inspection.handle.core_file == authored
        assert [item.path for item in inspection.inputs] == ["rtl/demo.sv"]

    def test_inspection_resolves_conditional_files_with_fusesoc_semantics(self, tmp_path: Path):
        (tmp_path / "conditional.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:ip:conditional:1.0
                filesets:
                  harness:
                    files:
                      - tool_verilator ? (ibex_simple_system_main.cc)
                      - tool_icarus ? (unused_main.cc)
                targets:
                  sim:
                    flow: sim
                    flow_options: {tool: verilator}
                    filesets: [harness]
                    toplevel: ibex_simple_system
                """
            ),
            encoding="utf-8",
        )

        inspection = inspect_target(tmp_path, select_target(tmp_path, "sim"))

        assert inspection.handle.identity == "acme:ip:conditional:1.0#sim"
        assert inspection.handle.selector == "sim"
        assert [item.path for item in inspection.inputs] == ["ibex_simple_system_main.cc"]
        assert inspection.toplevel == "ibex_simple_system"
        assert inspection.eda_tool == "verilator"

    def test_source_inspector_continues_after_one_target_cannot_resolve(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "mixed.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:ip:mixed:1.0
                filesets:
                  broken:
                    depend: [missing:dependency:core]
                  healthy:
                    files: [rtl/healthy.sv]
                targets:
                  lint_broken:
                    flow: lint
                    flow_options: {tool: verilator}
                    filesets: [broken]
                    toplevel: broken
                  lint_healthy:
                    flow: lint
                    flow_options: {tool: verilator}
                    filesets: [healthy]
                    toplevel: healthy
                """
            ),
            encoding="utf-8",
        )
        catalog = TargetCatalog.build(tmp_path)
        broken = catalog.select("lint_broken")
        healthy = catalog.select("lint_healthy")

        with pytest.raises(fusesoc_registry.FuseSocError, match="lint_broken"):
            catalog.inspect(broken)

        assert catalog.inspect(healthy).rtl_files == ("rtl/healthy.sv",)

    def test_selection_keeps_identity_separate_from_callable_selector(self, project: Path):
        selected = select_target(project, "acme:ip:alpha:1.0#lint", for_flow="lint")

        assert selected.identity == "acme:ip:alpha:1.0#lint"
        assert selected.selector == "alpha#lint"
        assert selected.name == "lint"

    def test_handle_carries_canonical_project_provenance(self, project: Path, tmp_path: Path):
        link = tmp_path / "linked-project"
        symlink_or_skip(link, project, target_is_directory=True)

        selected = select_target(link, "sim", for_flow="sim")

        assert selected.project_root == project.resolve()
        assert inspect_target(project, selected).handle is selected

    def test_inspection_rejects_handle_selected_for_another_project(
        self, project: Path, tmp_path: Path
    ) -> None:
        selected = select_target(project, "sim", for_flow="sim")
        other = tmp_path / "other"
        other.mkdir()

        with pytest.raises(fusesoc_registry.FuseSocError, match="selected for Project"):
            inspect_target(other, selected)

    def test_handle_has_no_public_dataclass_constructor(self) -> None:
        # Every field supplied: only the missing factory key may reject it.
        with pytest.raises(TypeError, match=r"created by TargetCatalog\.select\(\)"):
            TargetHandle(
                identity="acme:ip:alpha:1.0#sim",
                selector="sim",
                name="sim",
                vlnv="acme:ip:alpha:1.0",
                core_file=Path("alpha.core"),
                flow="sim",
                eda_tool="verilator",
                drivable_by=("sim",),
                project_root=Path("/project"),
                doctor_private=False,
            )

    def test_endpoint_selection_returns_exact_callable_selectors(self, project: Path):
        selected = select_targets(
            project,
            "acme:ip:alpha:1.0#lint,acme:ip:beta:1.0#fpga",
        )

        assert tuple(item.identity for item in selected) == (
            "acme:ip:alpha:1.0#lint",
            "acme:ip:beta:1.0#fpga",
        )
        assert tuple(item.selector for item in selected) == ("alpha#lint", "fpga")

    def test_inspection_includes_condition_selected_dependency_inputs(self, tmp_path: Path):
        dep = tmp_path / "dep"
        dep.mkdir()
        (dep / "dep.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:lib:dep:1.0
                filesets:
                  rtl: {files: [rtl/dep.sv]}
                targets:
                  default: {filesets: [rtl]}
                """
            ),
            encoding="utf-8",
        )
        (tmp_path / "top.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:ip:top:1.0
                filesets:
                  rtl:
                    depend: [acme:lib:dep]
                    files:
                      - tool_verilator ? (rtl/top.sv)
                targets:
                  sim:
                    flow: sim
                    flow_options: {tool: verilator}
                    filesets: [rtl]
                    toplevel: top
                """
            ),
            encoding="utf-8",
        )

        inspection = inspect_target(tmp_path, select_target(tmp_path, "sim"))

        assert [(item.core, item.path) for item in inspection.inputs] == [
            ("acme:lib:dep:1.0", "dep/rtl/dep.sv"),
            ("acme:ip:top:1.0", "rtl/top.sv"),
        ]


class TestMinimalSelector:
    def test_same_core_name_extends_to_library_segment(self):
        a = TargetRef(name="lint", vlnv="acme:ip:gamma:1.0", core_file=Path("a.core"))
        b = TargetRef(name="lint", vlnv="evil:ip2:gamma:2.0", core_file=Path("b.core"))
        assert minimal_selector(a, [a, b]) == "ip:gamma#lint"
        assert minimal_selector(b, [a, b]) == "ip2:gamma#lint"


class TestCollectSurface:
    def test_groups_by_core_sorted_by_vlnv(self, project: Path):
        surface = collect_surface(project)
        assert [g.vlnv for g in surface.groups] == ["acme:ip:alpha:1.0", "acme:ip:beta:1.0"]
        assert [entry.name for entry in surface.groups[0].entries] == ["lint", "sim", "synth"]
        assert [entry.name for entry in surface.groups[1].entries] == ["fpga", "lint", "smoke"]

    def test_doctor_selftest_is_hidden_from_every_public_surface(self, project: Path):
        assert all(
            entry.name != "lint_selftest_bad" for entry in collect_surface(project).entries()
        )
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            detail_payload(project, "lint_selftest_bad", resolve=False)

    def test_doctor_can_select_its_private_selftest(self, project: Path, monkeypatch):
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            select_target(project, "lint_selftest_bad", for_flow="lint")

        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)

        handle = select_target(project, "lint_selftest_bad", for_flow="lint")
        assert handle.name == "lint_selftest_bad"
        assert handle.selector == "lint_selftest_bad"
        assert handle.doctor_private

    def test_doctor_authority_is_preserved_by_multi_target_selection(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(UnknownTargetError, match="Unknown target"):
            select_targets(project, "lint_selftest_bad")

        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        catalog = TargetCatalog.build(project)
        monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV)

        selected = catalog.select_many(" lint_selftest_bad ", for_flow="lint")

        assert tuple(handle.identity for handle in selected) == (
            "acme:ip:beta:1.0#lint_selftest_bad",
        )
        assert tuple(handle.selector for handle in selected) == ("lint_selftest_bad",)

    def test_ambiguous_name_shows_qualified_selector(self, project: Path):
        surface = collect_surface(project)
        selectors = {e.selector for e in surface.entries()}
        assert "alpha#lint" in selectors
        assert "beta#lint" in selectors
        assert "sim" in selectors  # unique names stay bare

    def test_selectors_round_trip_through_resolve_ref(self, project: Path):
        """Every selector the listing prints must be a valid --target token."""
        surface = collect_surface(project)
        for entry in surface.entries():
            selected = TargetCatalog.build(project).select(entry.selector)
            assert selected.identity == entry.identity

    @pytest.mark.parametrize(
        ("selector", "field", "expected"),
        [
            ("sim", "declared_toplevel", "tb_alpha"),
            ("beta#lint", "declared_toplevel", ""),
            ("sim", "doctor_flows", ("sim",)),
            ("alpha#lint", "doctor_flows", ("lint",)),
            ("beta#lint", "doctor_flows", ()),
            ("sim", "cocotb_module", "tb_alpha_tests"),
            ("sim", "drivable_by", ("sim",)),
            ("synth", "drivable_by", ("synth",)),
            ("fpga", "drivable_by", ("fpga",)),
            ("smoke", "drivable_by", ("sim",)),
        ],
    )
    def test_entry_carries_declared_facts(
        self, project: Path, selector: str, field: str, expected: object
    ):
        assert getattr(_entry(collect_surface(project), selector), field) == expected


_ALPHA = "acme:ip:alpha:1.0"
_BETA = "acme:ip:beta:1.0"


class TestFilterSurface:
    @pytest.mark.parametrize(
        ("for_flow", "glob", "expected"),
        [
            ("sim", None, {_ALPHA: ["sim"], _BETA: ["smoke"]}),
            ("synth", None, {_ALPHA: ["synth"]}),
            # The fpga axis wins over the declared verilator resolution tool.
            ("fpga", None, {_BETA: ["fpga"]}),
            (None, "s*", {_ALPHA: ["sim", "synth"], _BETA: ["smoke"]}),
            (None, "*#lint", {_ALPHA: ["alpha#lint"], _BETA: ["beta#lint"]}),
            (None, "acme:ip:beta#*", {_BETA: ["fpga", "beta#lint", "smoke"]}),
            ("lint", "alpha*", {_ALPHA: ["alpha#lint"]}),
        ],
    )
    def test_filter_keeps_matching_entries_and_drops_empty_groups(
        self, project: Path, for_flow: str | None, glob: str | None, expected: dict
    ):
        surface = filter_surface(collect_surface(project), for_flow=for_flow, glob=glob)
        assert {g.vlnv: [e.selector for e in g.entries] for g in surface.groups} == expected

    def test_for_flow_consumes_catalog_drivability_facts(self, project: Path):
        handle = make_target_handle(
            project,
            "catalog_owned",
            flow="lint",
            eda_tool="verilator",
            drivable_by=("sim",),
        )
        surface = target_surface.TargetSurface(
            groups=(
                target_surface.CoreGroup(
                    vlnv=handle.vlnv,
                    core_file=handle.core_file,
                    entries=(handle,),
                ),
            ),
            warnings=(),
            flow_options={},
        )

        assert tuple(filter_surface(surface, for_flow="sim").entries()) == (handle,)
        assert not tuple(filter_surface(surface, for_flow="lint").entries())

    def test_for_flow_rejects_non_flow(self, project: Path):
        with pytest.raises(ValueError, match=", ".join(TARGET_AWARE_FLOWS)):
            filter_surface(collect_surface(project), for_flow="reviewer")

    def test_observations_survive_filtering(self, project: Path):
        original = collect_surface(project)
        original = target_surface.TargetSurface(original.groups, ("observation",), {})
        surface = filter_surface(original, glob="no-such-target-*")
        assert not surface.groups
        assert surface.warnings == ("observation",)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("soc*", True),
        ("*#lint", True),
        ("s?m", True),
        ("[ab]lint", True),
        ("sim", False),
        ("alpha#lint", False),
        ("acme:ip:alpha:1.0#lint", False),
    ],
)
def test_is_glob(token: str, expected: bool):
    assert is_glob(token) is expected


class TestToplevelDisplay:
    def test_list_valued_toplevel_joins_instead_of_repr(self, tmp_path: Path):
        """Upstream cores declare `toplevel: [testbench]` — must not render as
        Python's ['testbench'] repr (seen live on picorv32)."""
        (tmp_path / "up.core").write_text(
            "CAPI=2:\n"
            "name: acme:ip:up:1.0\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - rtl/up.sv: {file_type: systemVerilogSource}\n"
            "targets:\n"
            "  sim:\n"
            "    flow: sim\n"
            "    flow_options: {tool: icarus}\n"
            "    filesets: [rtl]\n"
            "    toplevel: [testbench]\n",
            encoding="utf-8",
        )
        surface = collect_surface(tmp_path)
        assert _entry(surface, "sim").declared_toplevel == "testbench"

    def test_long_toplevel_is_capped_in_listing_only(self, project: Path):
        long_top = "tool_verilator? (picorv32_wrapper) !tool_verilator? (testbench)"
        entry = _entry(collect_surface(project), "sim")
        patched = make_target_handle(
            project,
            entry.selector,
            vlnv=entry.vlnv,
            flow=entry.flow,
            eda_tool=entry.eda_tool,
            core_file=entry.core_file,
            doctor_flows=entry.doctor_flows,
            cocotb_module=entry.cocotb_module,
            declared_toplevel=long_top,
        )
        group = target_surface.CoreGroup(
            vlnv="acme:ip:alpha:1.0", core_file=project / "alpha/alpha.core", entries=(patched,)
        )
        surface = target_surface.TargetSurface(groups=(group,), warnings=(), flow_options={})
        text = targets_view.render_listing(surface_payload(surface, project))
        assert long_top not in text
        assert "tool_verilator?" in text and "…" in text and "top=" not in text
        # the payload keeps the full string — only the terminal column is capped
        assert surface_payload(surface, project)["cores"][0]["targets"][0]["toplevel"] == long_top


class TestSurfacePayload:
    def test_shape_and_relative_core_paths(self, project: Path):
        payload = surface_payload(collect_surface(project), project)
        assert [c["core_file"] for c in payload["cores"]] == [
            "alpha/alpha.core",
            "beta/beta.core",
        ]
        sim = next(t for t in payload["cores"][0]["targets"] if t["name"] == "sim")
        assert sim == {
            "name": "sim",
            "selector": "sim",
            "flow": "sim",
            "eda_tool": "verilator",
            "cocotb_module": "tb_alpha_tests",
            "toplevel": "tb_alpha",
            "doctor_flows": ["sim"],
            "drivable_by": ["sim"],
            "synth_mode": None,
            "frontend": None,
            "eda_tools": None,
        }
        assert payload["warnings"] == []


class TestRenderListing:
    def test_listing_groups_and_marks_doctor_membership(self, project: Path):
        text = targets_view.render_listing(surface_payload(collect_surface(project), project))
        assert "acme:ip:alpha:1.0  (alpha/alpha.core)" in text
        assert "✓ sim" in text
        assert re.search(r"\bDr\b", text) is None
        assert "cocotb tb_alpha_tests" in text
        assert "booley targets <name>" in text  # legend/hint line

    def test_warnings_are_listed_after_the_footer(self, project: Path):
        payload = surface_payload(collect_surface(project), project)
        payload["warnings"] = ["something odd"]
        text = targets_view.render_listing(payload)
        assert text.splitlines()[-1] == "WARNING: something odd"

    def test_warnings_are_listed_after_an_empty_result(self, project: Path):
        payload = surface_payload(filter_surface(collect_surface(project), glob="zzz*"), project)
        payload["warnings"] = ["something odd"]
        assert targets_view.render_listing(payload).splitlines() == [
            "(no Targets match)",
            "WARNING: something odd",
        ]


class TestDetail:
    def test_cheap_detail_does_not_resolve_builds(self, project: Path):
        # A build root cannot be calculated in a source checkout; the cheap half needs none.
        (project / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        calls: list[dict[str, object]] = []
        payload = detail_payload(
            project, "sim", resolve=False, runner=fusesoc_setup_runner(calls=calls)
        )
        assert payload["selector"] == "sim"
        assert payload["vlnv"] == "acme:ip:alpha:1.0"
        assert payload["doctor_flows"] == ["sim"]
        assert payload["drivable_by"] == ["sim"]
        assert "resolved" not in payload and "resolved_error" not in payload
        assert calls == []
        assert not (project / ".booley" / ".runtime" / "edalize").exists()

    def test_source_checkout_rejection_is_not_a_resolution_error(self, project):
        (project / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            detail_payload(project, "sim")
        assert not (project / ".booley" / ".runtime" / "edalize").exists()

    def test_resolution_error_names_unprovided_dependency(self, tmp_path: Path):
        (tmp_path / "top.core").write_bytes(
            b"CAPI=2:\nname: acme:lib:top:0\nfilesets:\n  rtl:\n    depend: ['>=::vlog_tb_utils:1.1']\n"
            b"targets:\n  sim:\n    flow: sim\n    flow_options: {tool: verilator}\n"
            b"    filesets: [rtl]\n"
        )

        runner = fusesoc_setup_runner(returncode=1, stderr="boom")
        payload = detail_payload(tmp_path, "sim", runner=runner)

        assert payload["resolved_error"] == (
            "fusesoc run --setup --target sim acme:lib:top:0 failed (exit 1): "
            "no Project .core provides '>=::vlog_tb_utils:1.1': the Sandbox has no FuseSoC libraries "
            "and cannot fetch them; copy it into the repository as a .core, or drop the depend: "
            "(check first whether the testbench guards its use with an `ifdef` or defines the module "
            "itself)"
        )

    def test_resolution_error_survives_a_core_edited_during_failure(self, tmp_path: Path):
        core = tmp_path / "top.core"
        core.write_bytes(
            b"CAPI=2:\nname: acme:lib:top:0\nfilesets:\n  rtl:\n    depend: ['::missing:0']\n"
            b"targets:\n  sim:\n    flow: sim\n    flow_options: {tool: verilator}\n"
            b"    filesets: [rtl]\n"
        )

        def failing(cmd, **kwargs):
            core.write_bytes(core.read_bytes() + b"# edited\n")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        payload = detail_payload(tmp_path, "sim", runner=failing)

        assert payload["resolved_error"] == (
            "fusesoc run --setup --target sim acme:lib:top:0 failed (exit 1):"
        )

    def test_unknown_and_ambiguous_tokens_raise(self, project: Path):
        with pytest.raises(UnknownTargetError):
            detail_payload(project, "ghost", resolve=False)
        with pytest.raises(AmbiguousTargetError):
            detail_payload(project, "lint", resolve=False)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (
                OSError("no fusesoc on this host"),
                "could not invoke fusesoc (fusesoc): no fusesoc on this host",
            ),
            (OSError(), "could not invoke fusesoc (fusesoc) with no diagnostic output"),
        ],
        ids=["diagnostic", "silent"],
    )
    def test_invocation_failure_degrades_to_error_field(
        self, project: Path, error: OSError, expected: str
    ):
        _touch(project, "alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv")

        payload = detail_payload(project, "sim", runner=fusesoc_setup_runner(error=error))

        assert "resolved" not in payload
        assert payload["resolved_error"] == expected
        text = targets_view.render_detail(payload)
        assert "Resolved view unavailable" in text
        assert "booley session enter" not in text

    def test_detail_view_shows_warnings_and_empty_resolved_parameters(self, project: Path):
        payload = detail_payload(project, "sim", resolve=False)
        payload["warnings"] = ["something odd"]
        payload["resolved"] = {
            "toplevel": "tb_alpha",
            "eda_tool": None,
            "cocotb_module": None,
            "parameters": {},
            "rtl_hdl_sources": 1,
            "rtl_include_dirs": 0,
            "tb_files": 1,
            "sdc_files": [],
            "xdc_files": [],
            "build_root": "build",
        }
        lines = targets_view.render_detail(payload).splitlines()
        assert lines[-1] == "WARNING: something odd"
        assert any(line.endswith("(none)") and "parameters" in line for line in lines)
        assert any("cocotb" in line and "tb_alpha_tests" in line for line in lines)

    def test_resolved_half_reads_edam(self, project: Path):
        _touch(project, "alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv")
        edam_text = textwrap.dedent(
            """\
            name: acme_ip_alpha_1.0
            toplevel: tb_alpha
            flow_options: {tool: verilator}
            parameters:
              WIDTH: {datatype: int, default: 8, paramtype: vlogparam}
            files:
            - {name: rtl/alpha.sv, file_type: systemVerilogSource}
            - {name: constraints/alpha.sdc, file_type: SDC}
            - name: tb/tb_alpha.sv
              file_type: systemVerilogSource
              tags: [tb]
            """
        )
        edam = ("acme_ip_alpha_1.0/sim/acme_ip_alpha_1.0.eda.yml", edam_text)

        payload = detail_payload(
            project, "sim", runner=fusesoc_setup_runner(edams={"acme:ip:alpha:1.0": edam})
        )

        resolved = payload["resolved"]
        assert resolved["toplevel"] == "tb_alpha"
        assert resolved["eda_tool"] == "verilator"
        assert resolved["rtl_hdl_sources"] == 1
        assert resolved["tb_files"] == 1
        assert resolved["sdc_files"] == ["constraints/alpha.sdc"]
        assert resolved["xdc_files"] == []
        assert resolved["build_root"] == (
            ".booley/.runtime/edalize/targets/sim/acme_ip_alpha_1.0/sim"
        )
        assert resolved["parameters"] == {
            "WIDTH": {"datatype": "int", "default": 8, "paramtype": "vlogparam"}
        }

        text = targets_view.render_detail(payload)
        assert "WIDTH (int) = 8" in text
        assert "constraints/alpha.sdc" in text

    @pytest.mark.parametrize(
        ("token", "vlnv", "backend", "configured_tool"),
        [
            # Legacy tool_options configure a backend other than the authored one.
            ("sim", "acme:ip:alpha:1.0", "tool_options: {icarus: {}}\n", "icarus"),
            ("fpga", "acme:ip:beta:1.0", "", None),
        ],
    )
    def test_resolved_display_uses_configured_backend_without_changing_authored_tool(
        self, project: Path, token: str, vlnv: str, backend: str, configured_tool: str | None
    ) -> None:
        _touch(project, "alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv", "beta/rtl/beta.sv")
        core = vlnv.replace(":", "_")  # FuseSoC's sanitized core name
        edam = (
            f"{core}/{token}/{core}.eda.yml",
            f"name: {core}\ntoplevel: top\n{backend}files: []\n",
        )

        payload = detail_payload(project, token, runner=fusesoc_setup_runner(edams={vlnv: edam}))

        assert payload["eda_tool"] == "verilator"
        assert payload["resolved"]["eda_tool"] == configured_tool

    def test_resolved_half_isolates_cores_sharing_a_target_name(self, project: Path) -> None:
        _touch(project, "alpha/rtl/alpha.sv", "beta/rtl/beta.sv")

        def edam(core: str, top: str, parameter: str) -> tuple[str, str]:
            text = (
                f"name: {core}\ntoplevel: {top}\nflow_options: {{tool: verilator}}\n"
                f"parameters:\n  {parameter}: {{datatype: int, default: 1, paramtype: vlogparam}}\n"
                "files: []\n"
            )
            return f"{core}/lint/{core}.eda.yml", text

        runner = fusesoc_setup_runner(
            edams={
                "acme:ip:alpha:1.0": edam("acme_ip_alpha_1.0", "alpha", "WIDTH"),
                "acme:ip:beta:1.0": edam("acme_ip_beta_1.0", "beta", "DEPTH"),
            }
        )

        detail_payload(project, "alpha#lint", runner=runner)
        payload = detail_payload(project, "beta#lint", runner=runner)

        resolved = payload["resolved"]
        assert resolved["toplevel"] == "beta"
        assert set(resolved["parameters"]) == {"DEPTH"}
        assert resolved["build_root"] == (
            ".booley/.runtime/edalize/targets/beta_lint/acme_ip_beta_1.0/lint"
        )
        text = targets_view.render_detail(payload)
        assert "DEPTH" in text
        assert "WIDTH" not in text
