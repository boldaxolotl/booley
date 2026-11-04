"""Simulator resolution for the TB Reviewer's ``## Simulation Context`` (#2636)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc.fusesoc_registry import canonical_project_path
from booley.specialists.review_simulators import (
    ScopeSimulators,
    render_simulation_context,
    resolve_scope_simulators,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError
from tests.fusesoc_test_support import write_core

_CORE = """\
CAPI=2:
name: ::demo_core:0

filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb_ver:
    files:
      - tb/tb_ver.sv: {file_type: systemVerilogSource}
      - tb/tb_both.sv: {file_type: systemVerilogSource}
      - tb/helper.cpp: {file_type: cppSource}
    tags: [tb]
  tb_ice:
    files:
      - tb/tb_ice.sv: {file_type: systemVerilogSource}
      - tb/tb_both.sv: {file_type: systemVerilogSource}
    tags: [tb]
  tb_iv:
    files:
      - tb/tb_iv.sv: {file_type: systemVerilogSource}
    tags: [tb]
  tb_legacy:
    files:
      - tb/tb_legacy.sv: {file_type: systemVerilogSource}
    tags: [tb]
  tb_xsim:
    files:
      - tb/tb_xsim.sv: {file_type: systemVerilogSource}
      - tb/test_cocotb.py: {file_type: user}
    tags: [tb]
  tb_lint:
    files:
      - tb/tb_lint_only.sv: {file_type: systemVerilogSource}
    tags: [tb]

targets:
  default:
    filesets: [rtl]
  sim_ver:
    flow: sim
    flow_options:
      tool: verilator
    filesets: [rtl, tb_ver]
    toplevel: tb_ver
  sim_ice:
    flow: sim
    flow_options:
      tool: icarus
    filesets: [rtl, tb_ice]
    toplevel: tb_ice
  sim_iv:
    flow: sim
    flow_options:
      tool: iverilog
    filesets: [rtl, tb_iv]
    toplevel: tb_iv
  sim_xsim:
    flow: sim
    flow_options:
      tool: xsim
    filesets: [rtl, tb_xsim]
    toplevel: tb_xsim
  sim_legacy:
    default_tool: verilator
    filesets: [rtl, tb_legacy]
    toplevel: tb_legacy
  lint_all:
    flow: lint
    flow_options:
      tool: verilator
    filesets: [rtl, tb_lint]
    toplevel: counter
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    write_core(tmp_path, _CORE)
    return tmp_path


def _by_path(sims: ScopeSimulators) -> dict[str, tuple[str, ...]]:
    return dict(sims.files)


def test_sim_targets_map_files_to_simulators(project: Path) -> None:
    sims = resolve_scope_simulators(
        project,
        ["tb/tb_ver.sv", "tb/tb_ice.sv", "tb/tb_iv.sv", "tb/tb_both.sv", "./tb/helper.cpp"],
    )

    assert _by_path(sims) == {
        "tb/helper.cpp": ("Verilator",),
        "tb/tb_both.sv": ("Icarus", "Verilator"),
        "tb/tb_ice.sv": ("Icarus",),
        "tb/tb_iv.sv": ("Icarus",),
        "tb/tb_ver.sv": ("Verilator",),
    }
    assert not sims.unresolved
    assert sims.skipped == ()


def test_xsim_target_and_cocotb_file_resolve(project: Path) -> None:
    sims = resolve_scope_simulators(project, ["tb/tb_xsim.sv", "tb/test_cocotb.py"])

    assert _by_path(sims) == {
        "tb/tb_xsim.sv": ("Vivado xsim",),
        "tb/test_cocotb.py": ("Vivado xsim",),
    }


def test_legacy_verilator_target_counts_as_simulation(project: Path) -> None:
    sims = resolve_scope_simulators(project, ["tb/tb_legacy.sv"])

    assert _by_path(sims) == {"tb/tb_legacy.sv": ("Verilator",)}


def test_lint_only_and_unreferenced_files_are_unknown(project: Path) -> None:
    (project / "tb" / "stray.sv").write_text("")

    sims = resolve_scope_simulators(project, ["tb/tb_lint_only.sv", "tb/stray.sv"])

    assert _by_path(sims) == {"tb/stray.sv": (), "tb/tb_lint_only.sv": ()}
    assert "tb/stray.sv: unknown (no simulation Target uses this file)" in (
        render_simulation_context(sims)
    )


def test_project_without_cores_is_unknown(tmp_path: Path) -> None:
    sims = resolve_scope_simulators(tmp_path, ["tb/tb.sv"])

    assert _by_path(sims) == {"tb/tb.sv": ()}
    assert not sims.unresolved


def test_support_files_are_not_listed(project: Path) -> None:
    sims = resolve_scope_simulators(project, ["tb/tb_ver.sv", "docs/notes.md", "run.tcl"])

    assert [path for path, _ in sims.files] == ["tb/tb_ver.sv"]


def test_unresolvable_catalog_reports_unknown(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_root: Path) -> TargetCatalog:
        raise FuseSocError("cannot read cores")

    monkeypatch.setattr(TargetCatalog, "build", staticmethod(broken))

    sims = resolve_scope_simulators(project, ["tb/tb_ver.sv"])

    assert sims.unresolved
    assert _by_path(sims) == {"tb/tb_ver.sv": ()}
    assert "tb/tb_ver.sv: unknown (the Project's Targets could not be resolved)" in (
        render_simulation_context(sims)
    )


def test_uninspectable_target_marks_context_incomplete(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_inspect = TargetCatalog.inspect

    def flaky(self: TargetCatalog, handle):
        if handle.name == "sim_ice":
            raise RuntimeError("fileset unreadable")
        return real_inspect(self, handle)

    monkeypatch.setattr(TargetCatalog, "inspect", flaky)

    sims = resolve_scope_simulators(project, ["tb/tb_ver.sv", "tb/tb_ice.sv"])

    assert _by_path(sims) == {"tb/tb_ice.sv": (), "tb/tb_ver.sv": ("Verilator",)}
    assert len(sims.skipped) == 1
    assert "sim_ice" in sims.skipped[0]
    text = render_simulation_context(sims)
    assert "may be incomplete" in text
    # An uninspected Target must not be reported as "no Target uses this file".
    assert "- tb/tb_ice.sv: unknown (no inspected simulation Target uses this file)" in text
    assert "no simulation Target uses" not in text


def test_unresolvable_scoped_path_reports_unknown(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = canonical_project_path

    def loopy(root, path):
        if str(path) == "tb/tb_ver.sv":
            raise OSError("symlink loop")
        return real(root, path)

    monkeypatch.setattr("booley.specialists.review_simulators.canonical_project_path", loopy)

    sims = resolve_scope_simulators(project, ["tb/tb_ver.sv"])

    assert sims.unresolved
    assert _by_path(sims) == {"tb/tb_ver.sv": ()}


def test_render_lists_files_and_limits_marked_rules() -> None:
    text = render_simulation_context(
        ScopeSimulators(files=(("tb/a.sv", ("Icarus", "Verilator")), ("tb/b.sv", ())))
    )

    assert text.startswith("## Simulation Context\n")
    assert "- tb/a.sv: Icarus, Verilator" in text
    assert "- tb/b.sv: unknown (no simulation Target uses this file)" in text
    assert "`[<Simulator> only]`" in text
    assert "`advisory`" in text
    assert "may be incomplete" not in text
