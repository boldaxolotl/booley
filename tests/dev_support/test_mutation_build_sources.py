"""Private compiler routing requires source identity, not matching basenames."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fusesoc.vlnv import Vlnv

from booley.dev_support.mutation_build_sources import (
    materialize_build_mutation,
    validate_mutation_closure,
)
from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.specialists.mutation_tester import MutationSpec


def _fixture(tmp_path: Path, header=False, copyto=None, tool=None, name=None, ambiguous=False):
    name = name or "acme:lib:dut:1"
    source = tmp_path / ("message.svh" if header else "dut.sv")
    body = 'module dut; initial begin $display("BASELINE"); $finish; end endmodule\n'
    source.write_bytes(('`define MESSAGE "BASELINE"\r\n' if header else body).encode())
    if header:
        (tmp_path / "dut.sv").write_text(
            '`include "message.svh"\n' + body.replace('"BASELINE"', "`MESSAGE")
        )
    attributes = {"is_include_file": header, "include_path": "."}
    if copyto:
        attributes["copyto"] = copyto
    files = (["dut.sv"] if header else []) + [{source.name: attributes}]
    fileset = {"files": files, "file_type": "systemVerilogSource"}
    target = {"filesets": ["rtl"], "default_tool": tool or "icarus", "toplevel": "dut"}
    target["tools"] = {"verilator": {"mode": "binary"}}
    data = {"name": name, "filesets": {"rtl": fileset}, "targets": {"sim": target}}
    if ambiguous:
        (tmp_path / "other.sv").write_bytes(source.read_bytes())
        data["filesets"]["rtl"]["files"] = [source.name, {"other.sv": attributes}]
        data["filesets"]["unused"] = {"files": [{source.name: attributes}]}
    core = tmp_path / "dut.core"
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    root = tmp_path / "build"
    (root / "alias").mkdir(parents=True)
    command = ["fusesoc", "--cores-root", str(tmp_path), "run", "--setup"]
    _run([*command, "--work-root", str(root), "--target", "sim", name], tmp_path)
    resolved = SimpleNamespace(build_root=root, edam_path=next(root.glob("*.eda.yml")))
    edam = yaml.safe_load(resolved.edam_path.read_text())
    if not tool:
        for recipe in [root / "Makefile", *root.glob("*.scr"), *root.glob("*.vc")]:
            recipe.unlink(missing_ok=True)
    spec = MutationSpec(1, "literal", source.name, 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], tmp_path, [source.name])
    return source, root / edam["files"][-1]["name"], variants, resolved, edam


def _run(argv, cwd):
    return subprocess.check_output(argv, cwd=cwd, stderr=subprocess.STDOUT, timeout=120).decode()


@pytest.mark.parametrize(
    "defect",
    ["missing", "copy", "empty", "wrong_core", "abs", "changed", "live", "type", "ambiguous"],
)
def test_unproven_or_unsafe_export_is_invalid(tmp_path, defect):
    source, staged, variants, resolved, edam = _fixture(
        tmp_path, False, "alias/dut.sv", "icarus", ambiguous=defect == "ambiguous"
    )
    core = tmp_path / "dut.core"
    data = yaml.safe_load(core.read_text())
    exported = resolved.build_root / "src/acme_lib_dut_1" / source.name
    if defect in ("missing", "copy"):
        (staged if defect == "missing" else exported).unlink()
    elif defect == "empty":
        edam["files"] = []
    elif defect == "wrong_core":
        edam["files"][0]["core"] = "other:lib:dut:1"
    elif defect in ("abs", "live"):
        edam["files"][0]["name"] = str(source)
        if defect == "abs":
            data["filesets"]["rtl"]["files"] = [{str(source): {}}]
    elif defect == "type":
        edam["files"][0]["file_type"] = "user"
    elif defect == "changed":
        staged.write_text("changed\n")
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises((MutationVariantError, FileNotFoundError)):
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize(
    "behavior",
    ["generators", "generate", "scripts", "provider", "hooks", "target_generate", "target_hooks"],
)
def test_direct_and_generated_dual_consumption_rejected_before_setup(tmp_path, behavior):
    source, staged, _variants, _resolved, _edam = _fixture(tmp_path)
    core = tmp_path / "dut.core"
    data = yaml.safe_load(core.read_text())
    if behavior.startswith("target_"):
        data["targets"]["sim"][behavior[7:]] = ["derivative"]
    else:
        data[behavior] = {"derivative": {"command": "generate pristine derivative"}}
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    with pytest.raises(MutationVariantError, match="unsupported generated/imperative"):
        validate_mutation_closure(frozenset([core]))
    assert staged.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("tool", [None, "icarus", "verilator"])
@pytest.mark.parametrize(
    "header,copyto", [(False, None), (True, None), (True, "alias/renamed.svh"), (True, "alias")]
)
def test_private_exports(tmp_path, header, copyto, tool):
    if tool and not shutil.which("iverilog" if tool == "icarus" else tool):
        pytest.skip(f"{tool} unavailable: required Sandbox compiler case")
    source, staged, variants, resolved, _edam = _fixture(tmp_path, header, copyto, tool)
    pristine = source.read_bytes()
    try:
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    except FileNotFoundError:
        pytest.fail("EDAM exports must not require build recipes")
    assert staged.read_bytes() == pristine.replace(b"BASELINE", b"MUTANT")
    exported = resolved.build_root / "src/acme_lib_dut_1" / source.name
    assert exported.read_bytes() == staged.read_bytes()
    if tool:
        _run(["make", "binary" if tool == "verilator" else "all"], resolved.build_root)
        command = ["./Vdut"] if tool == "verilator" else ["make", "run"]
        assert "MUTANT" in _run(command, resolved.build_root)
    assert source.read_bytes() == pristine


@pytest.mark.parametrize("dependency_name", ["other:lib:dut:1", "other:lib:dut", "other-dut"])
def test_identical_basename_dependency_uses_declaring_core_identity(tmp_path, dependency_name):
    source, staged, variants, resolved, edam = _fixture(tmp_path)
    other = tmp_path / "dependency"
    other.mkdir()
    dependency_source, dependency_staged, _, _dependency, dependency_edam = _fixture(
        other, name=dependency_name
    )
    edam["cores"][str(Vlnv(dependency_name))] = {"core_file": str(other / "dut.core")}
    entry = dependency_edam["files"][0]
    dependency_export = resolved.build_root / entry["name"]
    shutil.copytree(dependency_staged.parent, dependency_export.parent)
    edam["files"] = 2 * [*edam["files"], entry]
    edam["files"][0]["name"] = edam["files"][0]["name"].replace("src/", "src//./")
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert staged.read_bytes() == source.read_bytes().replace(b"BASELINE", b"MUTANT")
    assert dependency_export.read_bytes() == dependency_source.read_bytes() == source.read_bytes()
