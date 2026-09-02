"""Focused regression tests for the authored Doctor Target matrix."""

import ast
from pathlib import Path
from types import SimpleNamespace

from booley.audit import target_matrix
from booley.fusesoc import fusesoc_registry

_ROOT = Path(__file__).resolve().parents[2]


def _ref(tmp_path: Path, name: str, vlnv: str, *flows: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        vlnv=vlnv,
        core_file=tmp_path / f"{name}.core",
        doctor_flows=flows,
    )


def test_doctor_targets_reads_core_metadata_and_fails_soft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        fusesoc_registry,
        "doctor_target_selectors",
        lambda root, flow: ["sim_fast", "sim_full"],
    )
    assert target_matrix.doctor_targets(tmp_path, "sim") == ("sim_fast", "sim_full")

    def fail(_root, _flow):
        raise fusesoc_registry.FuseSocError("bad core")

    monkeypatch.setattr(fusesoc_registry, "doctor_target_selectors", fail)
    assert target_matrix.doctor_targets(tmp_path, "sim") == ()


def test_matrix_resolves_selected_keys_and_doctor_axes(tmp_path, monkeypatch) -> None:
    sim = _ref(tmp_path, "soc", "vendor:lib:soc:1", "sim")
    synth = _ref(tmp_path, "soc_synth", "vendor:lib:soc:1", "synth")
    refs = {"vendor:lib:soc:1#soc": sim, "soc_synth": synth}
    monkeypatch.setattr(fusesoc_registry, "doctor_target_seed", lambda root: list(refs))
    monkeypatch.setattr(fusesoc_registry, "resolve_ref", lambda root, token: refs[token])
    monkeypatch.setattr(
        fusesoc_registry,
        "target_declarations",
        lambda root: {"soc": [sim], "soc_synth": [synth]},
    )

    matrix = target_matrix.build_doctor_target_matrix(tmp_path)

    assert matrix.seed_targets == tuple(refs)
    assert matrix.is_selected("soc", "vendor:lib:soc:1")
    assert matrix.is_selected("soc_synth", "vendor:lib:soc:1")
    assert not matrix.is_selected("other", "vendor:lib:soc:1")
    assert matrix.axes() == {"soc": "sim", "soc_synth": "synth"}


def test_matrix_skips_unresolvable_seed_tokens(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fusesoc_registry, "doctor_target_seed", lambda root: ["missing"])

    def fail(_root, _token):
        raise fusesoc_registry.FuseSocError("missing")

    monkeypatch.setattr(fusesoc_registry, "resolve_ref", fail)
    monkeypatch.setattr(fusesoc_registry, "target_declarations", lambda root: {})

    matrix = target_matrix.build_doctor_target_matrix(tmp_path)

    assert matrix.seed_targets == ("missing",)
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
