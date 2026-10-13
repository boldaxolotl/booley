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
    _response_tokens,
    materialize_build_mutation,
    validate_mutation_closure,
)
from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.specialists.mutation_tester import MutationSpec


def _fixture(tmp_path: Path, *, header=False, copyto=False, include_path=False):
    scope = "nested/rtl/dut.svh" if header else "nested/rtl/dut.sv"
    source = tmp_path / scope
    source.parent.mkdir(parents=True)
    source.write_text("assign y = a + b;\n")
    attributes = {"file_type": "systemVerilogSource"}
    if header:
        attributes["is_include_file"] = True
    if copyto:
        attributes["copyto"] = "alias/dut.svh" if header else "alias/dut.sv"
    if include_path:
        attributes["include_path"] = "rtl"
    data = {
        "name": "acme:lib:dut:1",
        "filesets": {"rtl": {"files": [{"rtl/" + source.name: attributes}]}},
        "targets": {"sim": {"filesets": ["rtl"]}},
    }
    core = tmp_path / "nested/dut.core"
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    root = tmp_path / "build"
    exported = root / "src/acme_lib_dut_1/rtl" / source.name
    exported.parent.mkdir(parents=True)
    exported.write_bytes(source.read_bytes())
    name = attributes.get("copyto", str(exported.relative_to(root)))
    staged = root / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(source.read_bytes())
    entry = {"name": name, "core": data["name"], "file_type": "systemVerilogSource"}
    if header:
        entry["is_include_file"] = True
        entry["include_path"] = (
            "src/acme_lib_dut_1/rtl" if include_path else str(Path(name).parent)
        )
    edam = {"cores": {data["name"]: {"core_file": "../nested/dut.core"}}, "files": [entry]}
    (root / "Makefile").write_text("iverilog -c dut.scr\n")
    edam_path = root / "dut.eda.yml"
    edam_path.write_text(yaml.safe_dump(edam))
    (root / "dut.scr").write_text("+incdir+" + entry["include_path"] if header else name)
    spec = MutationSpec(1, "operator", scope, 1, "a + b", "a - b")
    variants = MutationVariantPlan.resolve([spec], tmp_path, [scope])
    return source, staged, variants, SimpleNamespace(build_root=root, edam_path=edam_path), edam


@pytest.mark.parametrize(
    "header,copyto", [(False, False), (False, True), (True, False), (True, True)]
)
def test_private_compiler_alias_changes_without_touching_live_source(tmp_path, header, copyto):
    source, staged, variants, resolved, _edam = _fixture(tmp_path, header=header, copyto=copyto)
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert staged.read_text() == "assign y = a - b;\n"
    assert source.read_text() == "assign y = a + b;\n"


def test_combined_copyto_include_path_rejects_unconsumed_alias(tmp_path):
    source, staged, variants, resolved, _edam = _fixture(
        tmp_path, header=True, copyto=True, include_path=True
    )
    with pytest.raises(MutationVariantError, match="unproven private include lookup"):
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert source.read_bytes() == staged.read_bytes()


@pytest.mark.parametrize(
    "defect", ["missing", "wrong_core", "absolute", "symlink", "hardlink", "changed"]
)
def test_unproven_or_unsafe_export_is_invalid(tmp_path, defect):
    source, staged, variants, resolved, edam = _fixture(tmp_path)
    if defect == "missing":
        edam["files"] = []
    elif defect == "wrong_core":
        edam["files"][0]["core"] = "other:lib:dut:1"
    elif defect == "absolute":
        edam["files"][0]["name"] = str(source)
    elif defect in ("symlink", "hardlink"):
        staged.unlink()
        if defect == "symlink":
            staged.symlink_to(source)
        else:
            staged.hardlink_to(source)
    else:
        staged.write_text("changed\n")
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    with pytest.raises(MutationVariantError):
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert source.read_text() == "assign y = a + b;\n"


@pytest.mark.parametrize(
    "behavior",
    ["generators", "generate", "scripts", "provider", "hooks", "target_generate", "target_hooks"],
)
def test_direct_and_generated_dual_consumption_rejected_before_setup(tmp_path, behavior):
    source, staged, _variants, _resolved, _edam = _fixture(tmp_path)
    core = tmp_path / "nested/dut.core"
    data = yaml.safe_load(core.read_text())
    if behavior.startswith("target_"):
        data["targets"]["sim"][behavior[7:]] = ["derivative"]
    else:
        data[behavior] = {"derivative": {"command": "generate pristine derivative"}}
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    with pytest.raises(MutationVariantError, match="unsupported generated/imperative"):
        validate_mutation_closure(frozenset([core]))
    assert staged.read_bytes() == source.read_bytes()


def test_real_fusesoc_compiler_reads_private_mutant(tmp_path):
    if not shutil.which("iverilog"):
        pytest.skip("iverilog unavailable: real compiler consumption fixture")
    source = tmp_path / "dut.sv"
    source.write_text('module dut; initial begin $display("BASELINE"); $finish; end endmodule\n')
    core = tmp_path / "dut.core"
    core.write_text(
        "CAPI=2:\nname: acme:lib:dut:1\nfilesets:\n  rtl:\n    files: [dut.sv]\n    file_type: systemVerilogSource\ntargets:\n  sim:\n    default_tool: icarus\n    filesets: [rtl]\n    toplevel: dut\n"
    )
    root = tmp_path / "build"
    subprocess.run(
        [
            "fusesoc",
            "--cores-root",
            str(tmp_path),
            "run",
            "--setup",
            "--build-root",
            str(root),
            "--target",
            "sim",
            "acme:lib:dut:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    edam_path = next(root.rglob("*.eda.yml"))
    resolved = SimpleNamespace(build_root=edam_path.parent, edam_path=edam_path)
    spec = MutationSpec(1, "literal", "dut.sv", 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], tmp_path, ["dut.sv"])
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    subprocess.run(["make"], cwd=resolved.build_root, check=True, capture_output=True, timeout=30)
    output = subprocess.run(
        ["make", "run"],
        cwd=resolved.build_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "MUTANT" in output.stdout
    assert "BASELINE" in source.read_text()


@pytest.mark.parametrize("copyto", [False, True])
def test_real_header_include_path_copyto_routing(tmp_path, copyto):
    if not shutil.which("iverilog"):
        pytest.skip("iverilog unavailable: real header compiler routing fixture")
    (tmp_path / "rtl").mkdir()
    source = tmp_path / "rtl/message.svh"
    source.write_text('`define MESSAGE "BASELINE"\n')
    (tmp_path / "dut.sv").write_text(
        '`include "message.svh"\nmodule dut; initial begin $display(`MESSAGE); $finish; end endmodule\n'
    )
    attrs = {"is_include_file": True, "include_path": "rtl"}
    if copyto:
        attrs["copyto"] = "alias/message.svh"
    core = {
        "name": "acme:lib:dut:1",
        "filesets": {
            "rtl": {
                "file_type": "systemVerilogSource",
                "files": ["dut.sv", {"rtl/message.svh": attrs}],
            }
        },
        "targets": {"sim": {"default_tool": "icarus", "filesets": ["rtl"], "toplevel": "dut"}},
    }
    (tmp_path / "dut.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))
    root = tmp_path / "build"
    subprocess.run(
        [
            "fusesoc",
            "--cores-root",
            str(tmp_path),
            "run",
            "--setup",
            "--build-root",
            str(root),
            "--target",
            "sim",
            "acme:lib:dut:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    edam_path = next(root.rglob("*.eda.yml"))
    resolved = SimpleNamespace(build_root=edam_path.parent, edam_path=edam_path)
    baseline = subprocess.run(
        ["make", "run"],
        cwd=resolved.build_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "BASELINE" in baseline.stdout
    spec = MutationSpec(1, "literal", "rtl/message.svh", 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], tmp_path, ["rtl/message.svh"])
    if copyto:
        # FuseSoC routes include_path to the exported header while EDAM names
        # copyto. Decline this unproven alias instead of scoring a pristine build.
        with pytest.raises(MutationVariantError, match="unproven private include lookup"):
            materialize_build_mutation(variants, 1, tmp_path, resolved)
    else:
        materialize_build_mutation(variants, 1, tmp_path, resolved)
        subprocess.run(
            ["make", "clean"], cwd=resolved.build_root, check=True, capture_output=True, timeout=30
        )
        mutant = subprocess.run(
            ["make", "run"],
            cwd=resolved.build_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert "MUTANT" in mutant.stdout
    assert "BASELINE" in source.read_text()


@pytest.mark.parametrize("dependency_name", ["other:lib:dut:1", "other:lib:dut", "other-dut"])
def test_identical_basename_dependency_uses_declaring_core_identity(tmp_path, dependency_name):
    source, staged, variants, resolved, edam = _fixture(tmp_path)
    other = tmp_path / "dependency"
    (other / "rtl").mkdir(parents=True)
    dependency_source = other / "rtl/dut.sv"
    dependency_source.write_bytes(source.read_bytes())
    (other / "dut.core").write_text(
        "CAPI=2:\n"
        + yaml.safe_dump({"name": dependency_name, "filesets": {"rtl": {"files": ["rtl/dut.sv"]}}})
    )
    canonical = str(Vlnv(dependency_name))
    name = f"src/{Vlnv(dependency_name).sanitized_name}/rtl/dut.sv"
    dependency_export = Path(resolved.build_root) / name
    dependency_export.parent.mkdir(parents=True)
    dependency_export.write_bytes(source.read_bytes())
    edam["cores"][canonical] = {"core_file": "../dependency/dut.core"}
    edam["files"].append({"core": canonical, "name": name})
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    with (Path(resolved.build_root) / "dut.scr").open("a") as stream:
        stream.write("\n" + name)
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert b"a - b" in staged.read_bytes()
    assert dependency_export.read_bytes() == dependency_source.read_bytes() == source.read_bytes()


def test_every_proven_alias_and_duplicate_entry_is_mutated_once(tmp_path):
    source, staged, variants, resolved, edam = _fixture(tmp_path)
    core = tmp_path / "nested/dut.core"
    data = yaml.safe_load(core.read_text())
    data["filesets"]["alias"] = {"files": [{"rtl/dut.sv": {"copyto": "alias/dut.sv"}}]}
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    alias = Path(resolved.build_root) / "alias/dut.sv"
    alias.parent.mkdir()
    alias.write_bytes(source.read_bytes())
    entry = {"name": "alias/dut.sv", "core": "acme:lib:dut:1"}
    edam["files"].extend([entry, entry])
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    with (Path(resolved.build_root) / "dut.scr").open("a") as stream:
        stream.write("\nalias/dut.sv")
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert staged.read_bytes() == alias.read_bytes() == b"assign y = a - b;\n"
    assert source.read_bytes() == b"assign y = a + b;\n"


@pytest.mark.parametrize("declared", ["acme:lib:dut", "dut"])
def test_shorthand_core_identity_is_canonicalized(tmp_path, declared):
    source, staged, variants, resolved, edam = _fixture(tmp_path)
    canonical = str(Vlnv(declared))
    core = tmp_path / "nested/dut.core"
    data = yaml.safe_load(core.read_text())
    data["name"] = declared
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    new_staged = Path(resolved.build_root) / "src" / Vlnv(declared).sanitized_name / "rtl/dut.sv"
    new_staged.parent.mkdir(parents=True)
    new_staged.write_bytes(staged.read_bytes())
    name = str(new_staged.relative_to(resolved.build_root))
    edam["cores"] = {canonical: {"core_file": "../nested/dut.core"}}
    edam["files"] = [{"name": name, "core": canonical}]
    resolved.edam_path.write_text(yaml.safe_dump(edam))
    (Path(resolved.build_root) / "dut.scr").write_text(name)
    materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert new_staged.read_bytes() == b"assign y = a - b;\n"
    assert source.read_bytes() == b"assign y = a + b;\n"


@pytest.mark.parametrize("shadow_location", ["include", "source", "working_directory"])
def test_competing_header_lookup_is_rejected_before_mutation(tmp_path, shadow_location):
    source, staged, variants, resolved, _edam = _fixture(tmp_path, header=True)
    root = Path(resolved.build_root)
    directory = root if shadow_location == "working_directory" else root / "other"
    directory.mkdir(exist_ok=True)
    (directory / staged.name).write_bytes(staged.read_bytes())
    response = "+incdir+" + str(staged.parent.relative_to(root))
    if shadow_location == "include":
        response = "+incdir+other\n" + response
    elif shadow_location == "source":
        (directory / "top.sv").write_text('`include "dut.svh"\n')
        response += "\nother/top.sv"
    (root / "dut.scr").write_text(response)
    with pytest.raises(MutationVariantError, match="ambiguous private include lookup"):
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    assert staged.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("windows", [False, True])
def test_response_tokens_preserve_platform_path_semantics(windows):
    text = (
        r'"src\private core\dut.sv" +incdir+src\headers # comment'
        if windows
        else r'"src/private core/dut.sv" +incdir+src/headers # comment'
    )
    expected = (
        [r"src\private core\dut.sv", r"+incdir+src\headers"]
        if windows
        else ["src/private core/dut.sv", "+incdir+src/headers"]
    )
    assert _response_tokens(text, windows=windows) == expected


def test_posix_response_tokens_keep_escaped_space_contract():
    assert _response_tokens(r"src/private\ core/dut.sv", windows=False) == [
        "src/private core/dut.sv"
    ]
