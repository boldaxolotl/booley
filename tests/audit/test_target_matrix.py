"""Focused regression tests for the authored Doctor Target matrix."""

import ast
from pathlib import Path

from booley.audit import target_matrix

_ROOT = Path(__file__).resolve().parents[2]


_CORE = """\
CAPI=2:
name: vendor:lib:soc:1
targets:
  soc: {flow_options: {booley: {doctor: [sim]}}}
  soc_synth: {flow_options: {booley: {doctor: [synth]}}}
  plain: {}
"""


def test_matrix_selects_doctor_targets_and_their_axes(tmp_path: Path) -> None:
    (tmp_path / "soc.core").write_bytes(_CORE.encode())

    matrix = target_matrix.build_doctor_target_matrix(tmp_path)

    assert matrix.seed_targets == ("soc", "soc_synth")
    assert matrix.is_selected("soc", "vendor:lib:soc:1")
    assert matrix.is_selected("soc_synth", "vendor:lib:soc:1")
    assert not matrix.is_selected("plain", "vendor:lib:soc:1")
    assert matrix.axes() == {"soc": "sim", "soc_synth": "synth"}
    assert target_matrix.doctor_targets(tmp_path, "sim") == ("soc",)


def test_matrix_fails_soft_when_the_catalog_cannot_be_built(tmp_path: Path) -> None:
    """The same VLNV authored in the Project and in the state zone is a core collision."""
    (tmp_path / "soc.core").write_bytes(_CORE.encode())
    (tmp_path / ".booley" / "cores").mkdir(parents=True)
    (tmp_path / ".booley" / "cores" / "soc.core").write_bytes(_CORE.encode())

    matrix = target_matrix.build_doctor_target_matrix(tmp_path)

    assert target_matrix.doctor_targets(tmp_path, "sim") == ()
    assert matrix.seed_targets == ()
    assert not matrix.selected_keys


def test_doctor_does_not_reimplement_target_matrix_mechanisms() -> None:
    doctor_path = _ROOT / "src" / "booley" / "harness" / "doctor.py"
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_doctor_target_matcher" not in function_names
    assert "_doctor_axis_by_target" not in function_names


def test_target_matrix_does_not_depend_on_presentation_layers() -> None:
    module_path = _ROOT / "src" / "booley" / "audit" / "target_matrix.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )

    forbidden = ("booley.harness", "booley.mcp", "booley.specialists")
    assert not {
        module for module in imports if any(module.startswith(prefix) for prefix in forbidden)
    }
