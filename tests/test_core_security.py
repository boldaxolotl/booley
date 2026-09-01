"""Tests for core_security: .core provenance + confinement validation (ADR 0022 dec 21).

The validator checks provenance + confinement, NOT content. These tests cover the
three rules: reject fpga_impl hooks, reject expr-params, and require generators/hooks
scripts to be out of the agent's write Scope.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.fusesoc.core_security import (
    CoreViolation,
    fpga_impl_eda_tools,
    validate_core,
    validate_project_cores,
)

# ---------------------------------------------------------------------------
# Helpers: build .core docs as parsed dicts (mirrors test_fusesoc_registry style).
# ---------------------------------------------------------------------------


def _doc(text: str) -> dict:
    """Parse a CAPI2 .core body into a dict (the leading marker is a harmless key)."""
    return yaml.safe_load(textwrap.dedent(text))


def _kinds(violations: list[CoreViolation]) -> set[str]:
    return {v.kind for v in violations}


# A clean sim core: verilator, tagged TB, literal params, no imperative sections.
_CLEAN = """\
    CAPI=2:
    name: ::demo:0
    filesets:
      rtl:
        files:
          - rtl/counter.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_counter.sv: {file_type: systemVerilogSource}
        tags: [tb]
    parameters:
      WIDTH:
        datatype: int
        paramtype: vlogparam
        default: 16
    targets:
      sim:
        flow: sim
        flow_options: {tool: verilator}
        filesets: [rtl, tb]
        toplevel: tb_counter
"""


# ---------------------------------------------------------------------------
# Clean cores pass
# ---------------------------------------------------------------------------


class TestCleanCore:
    def test_clean_sim_core_has_no_violations(self):
        assert validate_core(_doc(_CLEAN), core_file=Path("d.core")) == []

    def test_clean_core_passes_with_scope_too(self, tmp_path: Path):
        # Provenance check is a no-op when there are no imperative sections.
        assert (
            validate_core(
                _doc(_CLEAN),
                core_file=tmp_path / "d.core",
                project_root=tmp_path,
                scope=["rtl/*.sv"],
            )
            == []
        )


# ---------------------------------------------------------------------------
# Check 1: fpga_impl hooks are rejected; in-sandbox hooks are not
# ---------------------------------------------------------------------------


class TestFpgaHooks:
    _FPGA_HOOK = """\
        CAPI=2:
        name: ::demo:0
        targets:
          impl:
            flow: generic
            flow_options: {tool: vivado}
            filesets: [rtl]
            hooks:
              post_build: [run_bitstream]
    """

    def test_fpga_impl_hook_rejected(self):
        v = validate_core(_doc(self._FPGA_HOOK), core_file=Path("d.core"))
        assert _kinds(v) == {"fpga_hook"}
        assert v[0].target == "impl"
        assert "post_build" in v[0].message

    def test_fpga_axis_hook_rejected_with_resolution_tool(self):
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              fpga_core:
                flow: generic
                flow_options: {tool: verilator}
                hooks:
                  post_build: [run_bitstream]
        """
        )
        assert _kinds(validate_core(doc, core_file=Path("d.core"))) == {"fpga_hook"}

    def test_non_fpga_axis_is_not_reclassified_by_vivado(self):
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              synth_core:
                flow: generic
                flow_options: {tool: vivado}
                hooks:
                  post_build: [run_synthesis]
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []

    def test_fpga_impl_generator_allowed(self):
        # Generators run at resolution time (in-sandbox), so fpga_impl may use them.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              impl:
                flow: generic
                flow_options: {tool: vivado}
                generate: [make_xdc]
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []

    def test_sim_hook_not_rejected_by_fpga_check(self):
        # A verilator (in-sandbox) Target hook is fine for the fpga check — only
        # provenance (check 3) governs it.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              sim:
                flow: sim
                flow_options: {tool: verilator}
                hooks:
                  pre_run: [seed_rng]
        """
        )
        assert _kinds(validate_core(doc, core_file=Path("d.core"))) == set()

    def test_unsupported_sim_hook_is_not_classified_as_host_execution(self):
        # Xcelium is no longer a supported execution path. Core validation
        # therefore must not retain any host execution classification;
        # unsupported-target rejection belongs to the Flow eligibility gate.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              sim_xcelium:
                flow: sim
                flow_options: {tool: xcelium}
                hooks:
                  pre_run: [seed_rng]
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []

    def test_empty_hooks_block_is_not_a_violation(self):
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              impl:
                flow_options: {tool: vivado}
                hooks: {}
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []

    def test_default_tool_mirror_is_honored(self):
        # The EDA tool comes from default_tool when flow_options.tool is absent.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            targets:
              impl:
                default_tool: vivado
                hooks:
                  post_run: [x]
        """
        )
        assert _kinds(validate_core(doc, core_file=Path("d.core"))) == {"fpga_hook"}


# ---------------------------------------------------------------------------
# Check 2: expr-params are rejected; literal params pass
# ---------------------------------------------------------------------------


class TestExprParams:
    def test_literal_datatypes_pass(self):
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            parameters:
              A: {datatype: int, paramtype: vlogparam, default: 1}
              B: {datatype: str, paramtype: vlogparam, default: hi}
              C: {datatype: bool, paramtype: vlogparam, default: true}
              D: {datatype: real, paramtype: vlogparam, default: 1.5}
              E: {datatype: file, paramtype: vlogparam, default: x.mem}
            targets:
              sim: {flow_options: {tool: verilator}}
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []

    def test_expr_datatype_rejected(self):
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            parameters:
              MODE: {datatype: expr, paramtype: vlogparam, default: "pkg::Fast"}
            targets:
              sim: {flow_options: {tool: verilator}}
        """
        )
        v = validate_core(doc, core_file=Path("d.core"))
        assert _kinds(v) == {"expr_param"}
        assert "MODE" in v[0].message
        assert v[0].target is None

    def test_legacy_kind_expr_marker_rejected(self):
        # A hand-migration that transcribed configs.toml's {kind: expr} verbatim.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            parameters:
              MODE: {kind: expr, value: "pkg::Fast"}
            targets:
              sim: {flow_options: {tool: verilator}}
        """
        )
        assert _kinds(validate_core(doc, core_file=Path("d.core"))) == {"expr_param"}

    def test_param_without_datatype_is_ignored(self):
        # Nothing to validate — no datatype and no kind marker.
        doc = _doc(
            """\
            CAPI=2:
            name: ::demo:0
            parameters:
              P: {paramtype: vlogparam, default: 1}
            targets:
              sim: {flow_options: {tool: verilator}}
        """
        )
        assert validate_core(doc, core_file=Path("d.core")) == []


# ---------------------------------------------------------------------------
# Check 3: script provenance (out-of-Scope, read-only)
# ---------------------------------------------------------------------------


def _write_core_with_script(tmp_path: Path, script_rel: str, body: str) -> Path:
    """Write a .core plus the referenced script file, return the core path."""
    core = tmp_path / "design.core"
    core.write_text(textwrap.dedent(body), encoding="utf-8")
    script = tmp_path / script_rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# generated\n", encoding="utf-8")
    return core


_GEN_CORE = """\
    CAPI=2:
    name: ::demo:0
    generators:
      make_pkg:
        interpreter: python3
        command: {script}
    targets:
      sim:
        flow_options: {{tool: verilator}}
        generate: [make_pkg]
"""


class TestScriptProvenance:
    def test_out_of_scope_script_passes(self, tmp_path: Path):
        core = _write_core_with_script(
            tmp_path, "scripts/gen.py", _GEN_CORE.format(script="scripts/gen.py")
        )
        doc = yaml.safe_load(core.read_text())
        # Scope covers only rtl/ — the generator script under scripts/ is immutable.
        v = validate_core(doc, core_file=core, project_root=tmp_path, scope=["rtl/*.sv"])
        assert v == []

    def test_in_scope_script_rejected(self, tmp_path: Path):
        core = _write_core_with_script(
            tmp_path, "rtl/gen.py", _GEN_CORE.format(script="rtl/gen.py")
        )
        doc = yaml.safe_load(core.read_text())
        # Scope includes rtl/* — the agent could edit the generator → not confined.
        v = validate_core(doc, core_file=core, project_root=tmp_path, scope=["rtl/*"])
        assert _kinds(v) == {"in_scope_script"}
        assert "rtl/gen.py" in v[0].message

    def test_wildcard_scope_rejects_any_script(self, tmp_path: Path):
        core = _write_core_with_script(
            tmp_path, "scripts/gen.py", _GEN_CORE.format(script="scripts/gen.py")
        )
        doc = yaml.safe_load(core.read_text())
        v = validate_core(doc, core_file=core, project_root=tmp_path, scope=["*"])
        assert _kinds(v) == {"unconfinable_script"}

    def test_provenance_skipped_without_scope(self, tmp_path: Path):
        # No scope supplied → structural-only pass, provenance not checked.
        core = _write_core_with_script(
            tmp_path, "rtl/gen.py", _GEN_CORE.format(script="rtl/gen.py")
        )
        doc = yaml.safe_load(core.read_text())
        assert validate_core(doc, core_file=core, project_root=tmp_path) == []

    def test_scripts_section_pre_run_hook_script(self, tmp_path: Path):
        # A top-level `scripts:` entry referenced by an in-sandbox hook, in-scope.
        body = """\
            CAPI=2:
            name: ::demo:0
            scripts:
              seed:
                cmd: [python, tb/seed.py]
            targets:
              sim:
                flow_options: {tool: verilator}
                hooks:
                  pre_run: [seed]
        """
        core = _write_core_with_script(tmp_path, "tb/seed.py", body)
        doc = yaml.safe_load(core.read_text())
        v = validate_core(doc, core_file=core, project_root=tmp_path, scope=["tb/*"])
        assert _kinds(v) == {"in_scope_script"}


# ---------------------------------------------------------------------------
# Combined / project-level
# ---------------------------------------------------------------------------


class TestProjectLevel:
    def test_validate_project_cores_aggregates(self, tmp_path: Path):
        (tmp_path / "a.core").write_text(
            textwrap.dedent(TestFpgaHooks._FPGA_HOOK), encoding="utf-8"
        )
        (tmp_path / "b.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: ::demo2:0
                parameters:
                  M: {datatype: expr, default: x}
                targets:
                  sim: {flow_options: {tool: verilator}}
            """
            ),
            encoding="utf-8",
        )
        v = validate_project_cores(tmp_path)
        assert _kinds(v) == {"fpga_hook", "expr_param"}

    def test_build_trees_skipped(self, tmp_path: Path):
        # A .core copied into a build/ tree is a resolution artifact, not a source.
        build = tmp_path / "build" / "x"
        build.mkdir(parents=True)
        (build / "stale.core").write_text(
            textwrap.dedent(TestFpgaHooks._FPGA_HOOK), encoding="utf-8"
        )
        assert validate_project_cores(tmp_path) == []


# ---------------------------------------------------------------------------
# [fusesoc]-scoped audits — an unselectable core's script must not FAIL doctor
# on a multi-core repo (SETUP-19).
# ---------------------------------------------------------------------------


class TestFuseSocScopedAudit:
    """When [fusesoc] scopes the Target namespace, the audit follows only the
    selectable Targets' dependency closure — a rogue in-Scope script in an
    unselectable core is no longer flagged (SETUP-19)."""

    @staticmethod
    def _write_multicore(tmp_path: Path) -> None:
        # A selectable core (top) that depends on `dep`, plus an unselectable
        # `rogue` core carrying an in-Scope generator script. Both declare a
        # `sim` target — the very name collision the [fusesoc] scope resolves.
        (tmp_path / "top").mkdir()
        (tmp_path / "top" / "top.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:lib:top:0
                filesets:
                  rtl:
                    files:
                      - top.sv: {file_type: systemVerilogSource}
                    depend: [acme:lib:dep]
                targets:
                  sim:
                    flow: sim
                    flow_options: {tool: verilator}
                    filesets: [rtl]
                """
            ),
            encoding="utf-8",
        )
        (tmp_path / "dep").mkdir()
        (tmp_path / "dep" / "dep.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:lib:dep:0
                filesets:
                  rtl:
                    files:
                      - dep.sv: {file_type: systemVerilogSource}
                targets:
                  default:
                    filesets: [rtl]
                """
            ),
            encoding="utf-8",
        )
        # Rogue core at the project root so its generator path resolves to the
        # root-relative `rtl/rogue_gen.py` the write Scope (`rtl/*`) covers.
        (tmp_path / "rogue.core").write_text(
            textwrap.dedent(
                """\
                CAPI=2:
                name: acme:lib:rogue:0
                generators:
                  gen:
                    interpreter: python3
                    command: rtl/rogue_gen.py
                targets:
                  sim:
                    flow_options: {tool: verilator}
                    generate: [gen]
                """
            ),
            encoding="utf-8",
        )
        # The in-Scope generator script the rogue core would run.
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "rogue_gen.py").write_text("# gen\n", encoding="utf-8")

    def test_unselectable_core_script_not_flagged_when_scoped(self, tmp_path: Path):
        self._write_multicore(tmp_path)
        # `rtl/*` is in the agent's write Scope; the rogue core's generator lives
        # there, but rogue is unreachable from the seeded Target. `sim` is declared
        # by both top and rogue, so the seed is qualified `top#sim` (ADR 0030).
        violations = validate_project_cores(
            tmp_path,
            scope=["rtl/*"],
            seed_targets=["top#sim"],
        )
        assert violations == []

    def test_unselectable_core_script_flagged_without_scope(self, tmp_path: Path):
        # Same tree, no seed Targets → every core audited → the rogue script
        # (in-Scope, agent-mutable) is the expected in_scope_script FAIL.
        self._write_multicore(tmp_path)
        violations = validate_project_cores(tmp_path, scope=["rtl/*"])
        assert _kinds(violations) == {"in_scope_script"}
        assert any("rogue" in v.core_file.name for v in violations)


# ---------------------------------------------------------------------------
# fpga_impl_eda_tools derivation
# ---------------------------------------------------------------------------


def test_fpga_impl_eda_tools_tracks_criteria_map():
    eda_tools = fpga_impl_eda_tools()
    assert "vivado" in eda_tools
    assert "verilator" not in eda_tools
    assert "yosys" not in eda_tools
