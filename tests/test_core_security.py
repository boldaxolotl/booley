"""Tests for core_security: .core provenance + confinement validation (ADR 0022 dec 21).

The validator checks provenance + confinement, NOT content. These tests cover the
three rules: reject fpga_impl hooks, reject expr-params, and require generators/hooks
scripts to be out of the agent's write Scope.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from booley.fusesoc.core_security import (
    CoreViolation,
    validate_core,
    validate_project_cores,
)
from booley.targets.catalog import TargetCatalog
from tests.conftest import symlink_or_skip


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

_FPGA_HOOK_CORE = """\
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


def test_clean_core_passes_with_scope_too(tmp_path: Path):
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


def _target(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": "::demo:0", "targets": {name: fields}}


@pytest.mark.parametrize(
    ("name", "target", "expected"),
    [
        pytest.param(
            "impl",
            {
                "flow": "generic",
                "flow_options": {"tool": "vivado"},
                "hooks": {"post_build": ["run_bitstream"]},
            },
            {"fpga_hook"},
            id="fpga-impl-hook",
        ),
        # The axis classifies a target as fpga, whatever tool resolves it (33c664cf0).
        pytest.param(
            "fpga_core",
            {
                "flow": "generic",
                "flow_options": {"tool": "verilator"},
                "hooks": {"post_build": ["run_bitstream"]},
            },
            {"fpga_hook"},
            id="fpga-axis-with-verilator",
        ),
        pytest.param(
            "synth_core",
            {
                "flow": "generic",
                "flow_options": {"tool": "vivado"},
                "hooks": {"post_build": ["run_synthesis"]},
            },
            set(),
            id="non-fpga-axis-with-vivado",
        ),
        # Generators run at resolution time (in-sandbox), so fpga_impl may use them.
        pytest.param(
            "impl",
            {"flow": "generic", "flow_options": {"tool": "vivado"}, "generate": ["make_xdc"]},
            set(),
            id="fpga-generator",
        ),
        # An in-sandbox Target hook is only governed by provenance, not the fpga check.
        pytest.param(
            "sim",
            {
                "flow": "sim",
                "flow_options": {"tool": "verilator"},
                "hooks": {"pre_run": ["seed_rng"]},
            },
            set(),
            id="sim-hook",
        ),
        # Xcelium is no longer a supported execution path: core validation keeps no host
        # execution classification, and the Flow eligibility gate rejects the target.
        pytest.param(
            "sim_xcelium",
            {
                "flow": "sim",
                "flow_options": {"tool": "xcelium"},
                "hooks": {"pre_run": ["seed_rng"]},
            },
            set(),
            id="unsupported-sim-hook",
        ),
        pytest.param(
            "impl", {"flow_options": {"tool": "vivado"}, "hooks": {}}, set(), id="empty-hooks"
        ),
        # The EDA tool comes from default_tool when flow_options.tool is absent.
        pytest.param(
            "impl",
            {"default_tool": "vivado", "hooks": {"post_run": ["x"]}},
            {"fpga_hook"},
            id="default-tool",
        ),
    ],
)
def test_fpga_hook_rule(name: str, target: dict[str, Any], expected: set[str]):
    violations = validate_core(_target(name, **target), core_file=Path("d.core"))

    assert _kinds(violations) == expected
    for violation in violations:
        assert violation.target == name
        assert next(iter(target["hooks"])) in violation.message


_LITERAL_PARAMS = {
    "A": {"datatype": "int", "paramtype": "vlogparam", "default": 1},
    "B": {"datatype": "str", "paramtype": "vlogparam", "default": "hi"},
    "C": {"datatype": "bool", "paramtype": "vlogparam", "default": True},
    "D": {"datatype": "real", "paramtype": "vlogparam", "default": 1.5},
    "E": {"datatype": "file", "paramtype": "vlogparam", "default": "x.mem"},
}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (_LITERAL_PARAMS, set()),
        (
            {"MODE": {"datatype": "expr", "paramtype": "vlogparam", "default": "pkg::Fast"}},
            {"expr_param"},
        ),
        # A hand-migration that transcribed configs.toml's {kind: expr} verbatim.
        ({"MODE": {"kind": "expr", "value": "pkg::Fast"}}, {"expr_param"}),
        # Nothing to validate: no datatype and no kind marker.
        ({"P": {"paramtype": "vlogparam", "default": 1}}, set()),
    ],
    ids=["literal", "expr-datatype", "legacy-kind", "no-datatype"],
)
def test_expr_param_rule(params: dict[str, Any], expected: set[str]):
    doc = {
        "name": "::demo:0",
        "parameters": params,
        "targets": {"sim": {"flow_options": {"tool": "verilator"}}},
    }

    violations = validate_core(doc, core_file=Path("d.core"))

    assert _kinds(violations) == expected
    for violation in violations:
        assert "MODE" in violation.message
        assert violation.target is None


def _write_script(tmp_path: Path, script_rel: str) -> Path:
    """Write the script a core references and return the core path."""
    script = tmp_path / script_rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# generated\n", encoding="utf-8")
    return tmp_path / "design.core"


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
    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            (["scripts/gen.py"], {"in_scope_script"}),
            (["rtl/*.sv"], set()),
            ([], set()),
            (None, set()),
            (["*"], {"unconfinable_script"}),
            (["* [new]"], {"in_scope_script"}),
        ],
    )
    def test_provenance_decisions(self, tmp_path, scope, expected):
        core = _write_script(tmp_path, "scripts/gen.py")
        doc = _doc(_GEN_CORE.format(script="scripts/gen.py"))

        violations = validate_core(doc, core_file=core, project_root=tmp_path, scope=scope)

        assert _kinds(violations) == expected
        for violation in violations:
            assert "scripts/gen.py" in violation.message

    @pytest.mark.parametrize("via_symlink", [False, True])
    def test_external_script_retains_canonical_scope_classification(
        self, tmp_path: Path, via_symlink: bool
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "generator.py"
        outside.write_text("# fixture generator\n")
        token = "../generator.py"
        if via_symlink:
            symlink_or_skip(project / "generator.py", outside)
            token = "generator.py"
        core = project / "design.core"
        doc = _doc(_GEN_CORE.format(script=token))
        # Existing provenance matching is not a blanket filesystem confinement check.
        for scope in (None, [], ["rtl"], ["generator.py"]):
            assert validate_core(doc, core_file=core, project_root=project, scope=scope) == []
        for scope, kind in [
            ([str(outside.resolve())], "in_scope_script"),
            (["*"], "unconfinable_script"),
        ]:
            violations = validate_core(doc, core_file=core, project_root=project, scope=scope)
            assert _kinds(violations) == {kind}
            assert str(outside.resolve()) in violations[0].message

    def test_script_under_symlinked_booley_directory_is_project_relative(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        store = tmp_path / "store"
        (store / "cores").mkdir(parents=True)
        (store / "cores" / "gen.py").write_text("# generated\n", encoding="utf-8")
        symlink_or_skip(project / ".booley", store, target_is_directory=True)
        core = project / ".booley" / "cores" / "x.core"
        doc = _doc(_GEN_CORE.format(script="gen.py"))

        violations = validate_core(
            doc, core_file=core, project_root=project, scope=[".booley/cores/gen.py"]
        )

        assert _kinds(violations) == {"in_scope_script"}
        assert ".booley/cores/gen.py" in violations[0].message
        assert str(store) not in violations[0].message

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
        core = _write_script(tmp_path, "tb/seed.py")
        v = validate_core(_doc(body), core_file=core, project_root=tmp_path, scope=["tb/*"])
        assert _kinds(v) == {"in_scope_script"}


class TestProjectLevel:
    def test_validate_project_cores_aggregates(self, tmp_path: Path):
        (tmp_path / "a.core").write_text(textwrap.dedent(_FPGA_HOOK_CORE), encoding="utf-8")
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
        (build / "stale.core").write_text(textwrap.dedent(_FPGA_HOOK_CORE), encoding="utf-8")
        assert validate_project_cores(tmp_path) == []


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
        # Rogue core at the Project Root so its generator path resolves to the
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
        handle = TargetCatalog.build(tmp_path).select("top#sim")
        violations = validate_project_cores(
            tmp_path,
            scope=["rtl/*"],
            audit_scope=TargetCatalog.build(tmp_path).core_closure([handle]),
        )
        assert violations == []

    def test_unselectable_core_script_flagged_without_scope(self, tmp_path: Path):
        # Same tree, no seed Targets → every core audited → the rogue script
        # (in-Scope, agent-mutable) is the expected in_scope_script FAIL.
        self._write_multicore(tmp_path)
        violations = validate_project_cores(tmp_path, scope=["rtl/*"])
        assert _kinds(violations) == {"in_scope_script"}
        assert any("rogue" in v.core_file.name for v in violations)
