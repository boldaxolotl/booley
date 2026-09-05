"""Tests for target_surface: the `booley targets` / booley_targets MCP surface."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.fusesoc.core_projection import (
    CoreProjectionError,
    projected_core_path,
    reconcile_projected_cores,
)
from booley.fusesoc.fusesoc_registry import TargetRef, minimal_selector
from booley.targets import target_surface
from booley.targets.target import (
    TargetHandle,
    TargetSourceInspector,
    inspect_target,
    select_target,
    select_targets,
)
from booley.targets.target_surface import (
    TARGET_AWARE_FLOWS,
    collect_surface,
    detail_payload,
    filter_surface,
    flow_can_drive,
    is_glob,
    render_detail,
    render_listing,
    surface_payload,
)
from tests.conftest import symlink_or_skip

# ---------------------------------------------------------------------------
# Fixture project: two cores, one ambiguous target name, one legacy target,
# and explicit per-Target Doctor membership.
# ---------------------------------------------------------------------------

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
    (tmp_path / ".booley_project").mkdir()
    (tmp_path / ".booley_project" / "booley.toml").write_text(_BOOLEY_TOML, encoding="utf-8")
    return tmp_path


def _entry(surface: target_surface.TargetSurface, selector: str) -> target_surface.TargetEntry:
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
        with pytest.raises(fusesoc_registry.AmbiguousTargetError):
            select_target(project, "lint", for_flow="lint")
        with pytest.raises(fusesoc_registry.IncompatibleTargetError):
            select_target(project, "sim", for_flow="lint")

    def test_inspection_uses_projected_view_of_stealth_authored_core(self, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "demo.sv").write_text(
            "module demo; endmodule\n",
            encoding="utf-8",
        )
        authored = cores / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                filesets:
                  rtl:
                    files: [rtl/demo.sv]
                targets:
                  synth:
                    flow: generic
                    flow_options: {tool: yosys, arch: xilinx}
                    filesets: [rtl]
                    toplevel: demo
                """
            ),
            encoding="utf-8",
        )
        inspection = inspect_target(tmp_path, select_target(tmp_path, "synth"))
        refs = fusesoc_registry.enumerate_targets(tmp_path)
        sources = TargetSourceInspector(tmp_path).inspect(refs["synth"])

        assert inspection.handle.core_file == authored
        assert [item.path for item in inspection.inputs] == ["rtl/demo.sv"]
        assert sources.rtl_source_files == ("rtl/demo.sv",)

    def test_inspection_refreshes_owned_stale_projection(self, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "old.sv").write_text(
            "module old; endmodule\n",
            encoding="utf-8",
        )
        (tmp_path / "rtl" / "fresh.sv").write_text(
            "module fresh; endmodule\n",
            encoding="utf-8",
        )
        authored = cores / "demo.core"
        core_template = textwrap.dedent(
            """\
            CAPI=2:
            name: booley::demo:0
            filesets:
              rtl:
                files: [rtl/{module}.sv]
            targets:
              synth:
                flow: generic
                flow_options: {{tool: yosys, arch: xilinx}}
                filesets: [rtl]
                toplevel: {module}
            """
        )
        authored.write_text(core_template.format(module="old"), encoding="utf-8")
        reconcile_projected_cores(tmp_path)
        authored.write_text(core_template.format(module="fresh"), encoding="utf-8")

        inspection = inspect_target(tmp_path, select_target(tmp_path, "synth"))

        assert inspection.toplevel == "fresh"
        assert [item.path for item in inspection.inputs] == ["rtl/fresh.sv"]

    def test_inspection_uses_state_core_as_second_library_when_projection_is_disabled(
        self, tmp_path: Path
    ):
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = false\n",
            encoding="utf-8",
        )
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        authored = cores / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                targets:
                  lint:
                    flow: lint
                    flow_options: {tool: verible}
                    toplevel: demo
                """
            ),
            encoding="utf-8",
        )

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "demo"
        assert inspection.inputs == ()

    def test_inspection_ignores_generated_build_core_copy(self, tmp_path: Path):
        (tmp_path / ".booley_project").mkdir()
        (tmp_path / ".booley_project" / "booley.toml").write_text(
            "[stealth]\nenabled = false\n",
            encoding="utf-8",
        )
        authored = tmp_path / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                targets:
                  lint:
                    flow: lint
                    flow_options: {tool: verible}
                    toplevel: authored
                """
            ),
            encoding="utf-8",
        )
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "stale.core").write_text(
            authored.read_text(encoding="utf-8").replace("authored", "stale"),
            encoding="utf-8",
        )

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "authored"

    def test_inspection_ignores_state_worktree_core_copies(self, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        authored = cores / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                targets:
                  lint:
                    flow: lint
                    flow_options: {tool: verible}
                    toplevel: authored
                """
            ),
            encoding="utf-8",
        )
        reconcile_projected_cores(tmp_path)
        for state_copy in (
            project_dir / "worktrees" / "ticket" / "stale.core",
            project_dir / ".baseline-wt-old" / "stale.core",
        ):
            state_copy.parent.mkdir(parents=True)
            state_copy.write_text(
                authored.read_text(encoding="utf-8").replace("authored", "stale"),
                encoding="utf-8",
            )

        inspection = inspect_target(tmp_path, select_target(tmp_path, "lint"))

        assert inspection.handle.core_file == authored
        assert inspection.toplevel == "authored"

    @pytest.mark.parametrize(
        "excluded_tree",
        [Path(".booley_project/worktrees/ticket"), Path("build/cache")],
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
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\n",
            encoding="utf-8",
        )
        authored = cores / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                targets:
                  lint:
                    flow: lint
                    flow_options: {tool: verible}
                    toplevel: demo
                """
            ),
            encoding="utf-8",
        )
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
        project_dir = tmp_path / ".booley_project"
        cores = project_dir / "cores"
        cores.mkdir(parents=True)
        (project_dir / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "demo.sv").write_text("module demo; endmodule\n", encoding="utf-8")
        authored = cores / "demo.core"
        authored.write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: booley::demo:0
                filesets:
                  rtl:
                    files: [rtl/demo.sv]
                targets:
                  synth:
                    flow: generic
                    flow_options: {tool: yosys, arch: xilinx}
                    filesets: [rtl]
                    toplevel: demo
                """
            ),
            encoding="utf-8",
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

    def test_source_inspector_keeps_target_conditions_isolated_in_a_b_a_order(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "conditional.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:ip:conditional:1.0
                filesets:
                  selected:
                    files:
                      - target_lint_a ? (rtl/a.sv)
                      - target_lint_b ? (rtl/b.sv)
                targets:
                  lint_a:
                    flow: lint
                    flow_options: {tool: verilator}
                    filesets: [selected]
                    toplevel: a
                  lint_b:
                    flow: lint
                    flow_options: {tool: verilator}
                    filesets: [selected]
                    toplevel: b
                """
            ),
            encoding="utf-8",
        )
        refs = fusesoc_registry.enumerate_targets(tmp_path)
        inspector = TargetSourceInspector(tmp_path)

        observed = [
            inspector.inspect(refs[name]).rtl_source_files
            for name in ("lint_a", "lint_b", "lint_a")
        ]

        assert observed == [("rtl/a.sv",), ("rtl/b.sv",), ("rtl/a.sv",)]

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
        refs = fusesoc_registry.enumerate_targets(tmp_path)
        inspector = TargetSourceInspector(tmp_path)

        with pytest.raises(fusesoc_registry.FuseSocError, match="lint_broken"):
            inspector.inspect(refs["lint_broken"])

        assert inspector.inspect(refs["lint_healthy"]).rtl_source_files == ("rtl/healthy.sv",)

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
        with pytest.raises(TypeError):
            TargetHandle()

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


# ---------------------------------------------------------------------------
# flow_can_drive
# ---------------------------------------------------------------------------


class TestFlowCanDrive:
    def _ref(
        self,
        flow: str | None,
        eda_tool: str | None,
        *,
        name: str = "t",
    ) -> TargetRef:
        return TargetRef(
            name=name,
            vlnv="a:b:c:1.0",
            core_file=Path("x.core"),
            eda_tool=eda_tool,
            flow=flow,
        )

    def test_sim_flow_drives_sim_targets(self):
        ref = self._ref("sim", "verilator")
        assert flow_can_drive("sim", ref)
        assert not flow_can_drive("lint", ref)
        assert not flow_can_drive("synth", ref)
        assert not flow_can_drive("fpga", ref)

    def test_sim_flow_drives_canonical_icarus_target(self):
        ref = self._ref("sim", "icarus")
        assert flow_can_drive("sim", ref)

    def test_lint_flow_drives_lint_only(self):
        ref = self._ref("lint", "verible")
        assert flow_can_drive("lint", ref)
        assert not flow_can_drive("sim", ref)

    def test_generic_flow_splits_on_eda_tool(self):
        yosys = self._ref("generic", "yosys")
        vivado = self._ref("generic", "vivado")
        assert flow_can_drive("synth", yosys)
        assert not flow_can_drive("fpga", yosys)
        assert flow_can_drive("fpga", vivado)
        assert not flow_can_drive("synth", vivado)

    def test_fpga_axis_ignores_resolution_tool(self):
        ref = self._ref("generic", "verilator", name="fpga_core_fast")
        assert flow_can_drive("fpga", ref)

    def test_non_fpga_axis_overrides_vivado_fallback(self):
        ref = self._ref("generic", "vivado", name="synth_core_fast")
        assert not flow_can_drive("fpga", ref)

    def test_legacy_flowless_target_falls_back_to_eda_tool_family(self):
        """A `tools:`-style Target (flow=None) must not vanish from --for."""
        legacy_sim = self._ref(None, "iverilog")
        assert flow_can_drive("sim", legacy_sim)
        assert not flow_can_drive("lint", legacy_sim)

    @pytest.mark.parametrize("eda_tool", ["xcelium", "vcs"])
    def test_unsupported_commercial_simulators_are_not_drivable(self, eda_tool: str):
        """Vendor .cores may enumerate them, but Booley must not advertise support."""
        for declared_flow in ("sim", None):
            ref = self._ref(declared_flow, eda_tool)
            assert not flow_can_drive("sim", ref)

    def test_specialist_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"mutation_tester.*not a target-aware"):
            flow_can_drive("mutation_tester", self._ref("sim", "verilator"))

    def test_retired_elab_name_is_rejected(self):
        with pytest.raises(ValueError, match=r"elab.*not a target-aware"):
            flow_can_drive("elab", self._ref("sim", "verilator"))


# ---------------------------------------------------------------------------
# minimal_selector (fusesoc_registry)
# ---------------------------------------------------------------------------


class TestMinimalSelector:
    def _ref(self, vlnv: str) -> TargetRef:
        return TargetRef(name="lint", vlnv=vlnv, core_file=Path(f"{vlnv}.core"))

    def test_unique_name_stays_bare(self):
        ref = self._ref("acme:ip:alpha:1.0")
        assert minimal_selector(ref, [ref]) == "lint"

    def test_ambiguous_name_gets_shortest_qualifier(self):
        a = self._ref("acme:ip:alpha:1.0")
        b = self._ref("acme:ip:beta:1.0")
        assert minimal_selector(a, [a, b]) == "alpha#lint"
        assert minimal_selector(b, [a, b]) == "beta#lint"

    def test_same_core_name_extends_to_library_segment(self):
        a = self._ref("acme:ip:gamma:1.0")
        b = self._ref("evil:ip2:gamma:2.0")
        assert minimal_selector(a, [a, b]) == "ip:gamma#lint"
        assert minimal_selector(b, [a, b]) == "ip2:gamma#lint"


# ---------------------------------------------------------------------------
# collect_surface
# ---------------------------------------------------------------------------


class TestCollectSurface:
    def test_groups_by_core_sorted_by_vlnv(self, project: Path):
        surface = collect_surface(project)
        assert [g.vlnv for g in surface.groups] == ["acme:ip:alpha:1.0", "acme:ip:beta:1.0"]
        assert [e.ref.name for e in surface.groups[0].entries] == ["lint", "sim", "synth"]
        assert [e.ref.name for e in surface.groups[1].entries] == ["fpga", "lint", "smoke"]

    def test_doctor_selftest_is_hidden_from_every_public_surface(self, project: Path):
        assert fusesoc_registry.resolve_ref(project, "lint_selftest_bad").doctor_selftest
        assert all(e.ref.name != "lint_selftest_bad" for e in collect_surface(project).entries())
        with pytest.raises(fusesoc_registry.UnknownTargetError, match="Unknown target"):
            detail_payload(project, "lint_selftest_bad", resolve=False)

    def test_doctor_can_select_its_private_selftest(self, project: Path, monkeypatch):
        with pytest.raises(fusesoc_registry.UnknownTargetError, match="Unknown target"):
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
        with pytest.raises(fusesoc_registry.UnknownTargetError, match="Unknown target"):
            select_targets(project, "lint_selftest_bad")

        resolve_selected_ref = fusesoc_registry.resolve_selected_ref

        def resolve_once(root: Path | str, token: str) -> TargetRef:
            ref = resolve_selected_ref(root, token)
            monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV)
            return ref

        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        monkeypatch.setattr(fusesoc_registry, "resolve_selected_ref", resolve_once)

        selected = select_targets(project, " lint_selftest_bad ", for_flow="lint")

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
            assert fusesoc_registry.resolve_ref(project, entry.selector) == entry.ref

    def test_declared_toplevel_is_read(self, project: Path):
        surface = collect_surface(project)
        assert _entry(surface, "sim").toplevel == "tb_alpha"
        assert _entry(surface, "beta#lint").toplevel == ""  # undeclared

    def test_doctor_membership_comes_from_target_metadata(self, project: Path):
        surface = collect_surface(project)
        assert _entry(surface, "sim").doctor_flows == ("sim",)
        assert _entry(surface, "alpha#lint").doctor_flows == ("lint",)
        assert _entry(surface, "beta#lint").doctor_flows == ()

    def test_surface_has_no_separate_target_wiring_warnings(self, project: Path):
        assert collect_surface(project).warnings == ()

    def test_cocotb_module_enumerated(self, project: Path):
        assert _entry(collect_surface(project), "sim").ref.cocotb_module == "tb_alpha_tests"

    def test_drivable_by(self, project: Path):
        surface = collect_surface(project)
        assert _entry(surface, "sim").drivable_by == ("sim",)
        assert _entry(surface, "synth").drivable_by == ("synth",)
        assert _entry(surface, "fpga").drivable_by == ("fpga",)
        assert _entry(surface, "smoke").drivable_by == ("sim",)


# ---------------------------------------------------------------------------
# filter_surface
# ---------------------------------------------------------------------------


class TestFilterSurface:
    def test_for_flow_keeps_only_drivable(self, project: Path):
        surface = filter_surface(collect_surface(project), for_flow="sim")
        assert sorted(e.ref.name for e in surface.entries()) == ["sim", "smoke"]

    def test_for_flow_drops_empty_groups(self, project: Path):
        surface = filter_surface(collect_surface(project), for_flow="synth")
        assert [g.vlnv for g in surface.groups] == ["acme:ip:alpha:1.0"]

    def test_for_fpga_uses_axis_instead_of_resolution_tool(self, project: Path):
        surface = filter_surface(collect_surface(project), for_flow="fpga")
        assert [entry.ref.name for entry in surface.entries()] == ["fpga"]
        assert _entry(surface, "fpga").ref.eda_tool == "verilator"

    def test_for_flow_rejects_non_flow(self, project: Path):
        with pytest.raises(ValueError, match=", ".join(TARGET_AWARE_FLOWS)):
            filter_surface(collect_surface(project), for_flow="reviewer")

    def test_glob_matches_bare_name(self, project: Path):
        surface = filter_surface(collect_surface(project), glob="s*")
        assert sorted(e.ref.name for e in surface.entries()) == ["sim", "smoke", "synth"]

    def test_glob_matches_selector_and_qualified_form(self, project: Path):
        by_selector = filter_surface(collect_surface(project), glob="*#lint")
        assert sorted(e.selector for e in by_selector.entries()) == ["alpha#lint", "beta#lint"]
        qualified = filter_surface(collect_surface(project), glob="acme:ip:beta#*")
        assert sorted(e.ref.name for e in qualified.entries()) == ["fpga", "lint", "smoke"]

    def test_filters_compose(self, project: Path):
        surface = filter_surface(collect_surface(project), for_flow="lint", glob="alpha*")
        assert [e.selector for e in surface.entries()] == ["alpha#lint"]

    def test_observations_survive_filtering(self, project: Path):
        original = collect_surface(project)
        original = target_surface.TargetSurface(original.groups, ("observation",))
        surface = filter_surface(original, glob="no-such-target-*")
        assert not surface.groups
        assert surface.warnings == ("observation",)


# ---------------------------------------------------------------------------
# is_glob
# ---------------------------------------------------------------------------


class TestIsGlob:
    def test_metacharacters_are_globs(self):
        assert is_glob("soc*")
        assert is_glob("*#lint")
        assert is_glob("s?m")
        assert is_glob("[ab]lint")

    def test_names_and_qualifiers_are_not(self):
        assert not is_glob("sim")
        assert not is_glob("alpha#lint")
        assert not is_glob("acme:ip:alpha:1.0#lint")


# ---------------------------------------------------------------------------
# toplevel display (list-valued CAPI2 toplevels, width cap)
# ---------------------------------------------------------------------------


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
        assert _entry(surface, "sim").toplevel == "testbench"

    def test_long_toplevel_is_capped_in_listing_only(self, project: Path):
        long_top = "tool_verilator? (picorv32_wrapper) !tool_verilator? (testbench)"
        entry = _entry(collect_surface(project), "sim")
        patched = target_surface.TargetEntry(
            ref=entry.ref,
            selector=entry.selector,
            toplevel=long_top,
            doctor_flows=entry.doctor_flows,
            drivable_by=entry.drivable_by,
        )
        group = target_surface.CoreGroup(
            vlnv="acme:ip:alpha:1.0", core_file=project / "alpha/alpha.core", entries=(patched,)
        )
        surface = target_surface.TargetSurface(groups=(group,), warnings=())
        text = render_listing(surface, project)
        assert long_top not in text
        assert "top=tool_verilator?" in text and "…" in text
        # the payload keeps the full string — only the terminal column is capped
        assert surface_payload(surface, project)["cores"][0]["targets"][0]["toplevel"] == long_top


# ---------------------------------------------------------------------------
# payloads + rendering
# ---------------------------------------------------------------------------


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
        }
        assert payload["warnings"] == []


class TestRenderListing:
    def test_listing_groups_and_marks_doctor_membership(self, project: Path):
        text = render_listing(collect_surface(project), project)
        assert "acme:ip:alpha:1.0  (alpha/alpha.core)" in text
        assert "Dr sim" in text
        assert "cocotb=tb_alpha_tests" in text
        assert "booley targets <name>" in text  # legend/hint line

    def test_empty_filter_result(self, project: Path):
        surface = filter_surface(collect_surface(project), glob="zzz*")
        assert "(no Targets match)" in render_listing(surface, project)


# ---------------------------------------------------------------------------
# detail_payload + render_detail
# ---------------------------------------------------------------------------


class TestDetail:
    def test_cheap_half(self, project: Path):
        payload = detail_payload(project, "sim", resolve=False)
        assert payload["selector"] == "sim"
        assert payload["vlnv"] == "acme:ip:alpha:1.0"
        assert payload["doctor_flows"] == ["sim"]
        assert payload["drivable_by"] == ["sim"]
        assert "resolved" not in payload and "resolved_error" not in payload

    def test_unknown_and_ambiguous_tokens_raise(self, project: Path):
        with pytest.raises(fusesoc_registry.UnknownTargetError):
            detail_payload(project, "ghost", resolve=False)
        with pytest.raises(fusesoc_registry.AmbiguousTargetError):
            detail_payload(project, "lint", resolve=False)

    def test_resolution_failure_degrades_to_error_field(self, project: Path):
        def failing_runner(*args, **kwargs):
            raise OSError("no fusesoc on this host")

        # sources exist so the preflight passes and the runner is reached
        for rel in ("alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv"):
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).touch()

        payload = detail_payload(project, "sim", runner=failing_runner)
        assert "resolved" not in payload
        assert "no fusesoc" in payload["resolved_error"]
        text = render_detail(payload)
        assert "Resolved view unavailable" in text
        assert "booley session enter" not in text

    def test_silent_invocation_failure_has_actionable_error(self, project: Path):
        def failing_runner(*args, **kwargs):
            raise OSError()

        for rel in ("alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv"):
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).touch()

        payload = detail_payload(project, "sim", runner=failing_runner)

        assert payload["resolved_error"] == (
            "could not invoke fusesoc (fusesoc) with no diagnostic output"
        )

    def test_resolved_half_reads_edam(self, project: Path):
        from booley.flows import edam as edam_layer

        for rel in ("alpha/rtl/alpha.sv", "alpha/tb/tb_alpha.sv"):
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).touch()

        build_root = edam_layer.work_root_for(project, "targets", "sim")
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

        def fake_runner(cmd, **kwargs):
            edam = build_root / "acme_ip_alpha_1.0" / "sim" / "alpha.eda.yml"
            edam.parent.mkdir(parents=True, exist_ok=True)
            edam.write_text(edam_text, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        payload = detail_payload(project, "sim", runner=fake_runner)
        resolved = payload["resolved"]
        assert resolved["toplevel"] == "tb_alpha"
        assert resolved["rtl_hdl_sources"] == 1
        assert resolved["tb_files"] == 1
        assert resolved["sdc_files"] == ["constraints/alpha.sdc"]
        assert resolved["xdc_files"] == []

        text = render_detail(payload)
        assert "WIDTH (int) = 8" in text
        assert "constraints/alpha.sdc" in text
