"""Private compiler routing requires source identity, not matching basenames."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fusesoc.vlnv import Vlnv
from tests.booley_checkouts_support import commit_all, init_repo, isolate_git, snapshot
from tests.conftest import symlink_or_skip
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.subprocess_budget import run_bounded_child

from booley.dev_support.mutation_build_sources import (
    materialize_build_mutation,
    validate_mutation_closure,
)
from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.specialists.mutation_tester import (
    _RUNTIME_INPUTS_MARKER,
    MutationSpec,
    MutationTesterSpecialist,
    UnsupportedSimTargetError,
    _record_build_markers,
)


def _fixture(tmp_path, header=False, copyto=None, eda_tool=None, name=None, defect=None):
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
    files = (["dut.sv"] if header else []) + [
        {source.as_posix() if defect == "abs" else source.name: attributes}
    ]
    fileset = {"files": files, "file_type": "systemVerilogSource"}
    target = {"filesets": ["rtl"], "default_tool": eda_tool or "icarus", "toplevel": "dut"}
    target["tools"] = {"verilator": {"mode": "binary"}}
    data = {"name": name, "filesets": {"rtl": fileset}, "targets": {"sim": target}}
    if defect == "ambiguous":
        (tmp_path / "other.sv").write_bytes(source.read_bytes())
        data["filesets"]["rtl"]["files"] = [source.name, {"other.sv": attributes}]
        data["filesets"]["unused"] = {"files": [{source.name: attributes}]}
    core = tmp_path / "dut.core"
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data))
    root = tmp_path / "build"
    (root / "alias").mkdir(parents=True)
    command = ["fusesoc", "--cores-root", str(tmp_path), "run", "--setup"]
    if defect == "live":
        command.append("--no-export")
    _run([*command, "--work-root", str(root), "--target", "sim", name], tmp_path)
    resolved = SimpleNamespace(build_root=root, edam_path=next(root.glob("*.eda.yml")))
    edam = yaml.safe_load(resolved.edam_path.read_text())
    if not eda_tool:
        for recipe in [root / "Makefile", *root.glob("*.scr"), *root.glob("*.vc")]:
            recipe.unlink(missing_ok=True)
    spec = MutationSpec(1, "literal", source.name, 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], tmp_path, [source.name])
    return source, root / edam["files"][-1]["name"], variants, resolved, edam


def _run(argv, cwd):
    result = run_bounded_child(argv, cwd=cwd, check=True)
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    "defect",
    ["missing", "copy", "empty", "wrong_core", "abs", "changed", "live", "type", "ambiguous"],
)
def test_unproven_or_unsafe_export_is_invalid(tmp_path, defect):
    source, staged, variants, resolved, edam = _fixture(
        tmp_path, False, "alias/dut.sv", "icarus", defect=defect
    )
    exported = resolved.build_root / "src/acme_lib_dut_1" / source.name
    if defect in ("missing", "copy"):
        (staged if defect == "missing" else exported).unlink()
    elif defect == "empty":
        edam["files"] = []
    elif defect == "wrong_core":
        edam["files"][0]["core"] = "other:lib:dut:1"
    elif defect == "type":
        edam["files"][0]["file_type"] = "user"
    elif defect == "changed":
        staged.write_text("changed\n")
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


@pytest.mark.parametrize("eda_tool", [None, "icarus", "verilator"])
@pytest.mark.parametrize(
    "header,copyto", [(False, None), (True, None), (True, "alias/renamed.svh"), (True, "alias")]
)
def test_private_exports(tmp_path, header, copyto, eda_tool):
    if eda_tool and not shutil.which("iverilog" if eda_tool == "icarus" else eda_tool):
        pytest.skip(f"{eda_tool} unavailable: required Sandbox compiler case")
    source, staged, variants, resolved, _edam = _fixture(tmp_path, header, copyto, eda_tool)
    pristine = source.read_bytes()
    try:
        materialize_build_mutation(variants, 1, tmp_path, resolved)
    except FileNotFoundError:
        pytest.fail("EDAM exports must not require build recipes")
    assert staged.read_bytes() == pristine.replace(b"BASELINE", b"MUTANT")
    exported = resolved.build_root / "src/acme_lib_dut_1" / source.name
    assert exported.read_bytes() == staged.read_bytes()
    if eda_tool:
        _run(["make", "binary" if eda_tool == "verilator" else "all"], resolved.build_root)
        command = ["./Vdut"] if eda_tool == "verilator" else ["make", "run"]
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


# ---------------------------------------------------------------------------
# #2851: ignore_native_cores Projects mutate a private mirror, never the checkout
# ---------------------------------------------------------------------------

_DUT = "module dut; initial begin $display(`MESSAGE); $finish; end endmodule\n"
_HEADER = '`define MESSAGE "BASELINE"\n'
_STEALTH_CORE = {
    "name": "acme:lib:dut:1",
    "filesets": {
        "rtl": {
            "files": [
                "rtl/dut.sv",
                {"rtl/inc/message.svh": {"is_include_file": True, "include_path": "rtl/inc"}},
            ],
            "file_type": "systemVerilogSource",
        }
    },
    "targets": {"sim": {"filesets": ["rtl"], "default_tool": "icarus", "toplevel": "dut"}},
}


def _write_ignore_native_project(root: Path) -> None:
    """Write sources, a native core (ignored) and one stealth-authored core."""
    (root / "rtl" / "inc").mkdir(parents=True, exist_ok=True)
    (root / "rtl" / "dut.sv").write_text(_DUT, encoding="utf-8")
    (root / "rtl" / "inc" / "message.svh").write_text(_HEADER, encoding="utf-8")
    native = {**_STEALTH_CORE, "name": "native:lib:dut:1"}
    (root / "native.core").write_text("CAPI=2:\n" + yaml.safe_dump(native), encoding="utf-8")
    booley = root / ".booley"
    (booley / "cores").mkdir(parents=True, exist_ok=True)
    (booley / "booley.toml").write_text(
        "[stealth]\nenabled = true\nignore_native_cores = true\n", encoding="utf-8"
    )
    (booley / "cores" / "dut.core").write_text(
        "CAPI=2:\n" + yaml.safe_dump(_STEALTH_CORE), encoding="utf-8"
    )


def _tree_snapshot(root: Path, excluded: Path) -> dict[Path, bytes]:
    """Bytes of every regular file below *root*, skipping *excluded* and git internals."""
    excluded = excluded.resolve()
    return {
        path: path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.resolve().is_relative_to(excluded)
        and ".git" not in path.relative_to(root).parts
    }


def _ignore_native_layout(tmp_path: Path, monkeypatch, layout: str) -> tuple[Path, Path]:
    """Return ``(project root, build path)`` for one Project layout."""
    isolate_git(tmp_path, monkeypatch)
    if layout == "symlinked-booley-dir":
        root = tmp_path / "proj"
        root.mkdir()
        symlinked_booley_dir(root, tmp_path / "external-booley")
        _write_ignore_native_project(root)
        return root, root / ".booley" / ".runtime" / "mutant_1"
    if layout == "main":
        root = tmp_path / "main"
        init_repo(root)
        _write_ignore_native_project(root)
        return root, root / ".booley" / ".runtime" / "mutant_1"
    main = tmp_path / "main"
    init_repo(main)
    _write_ignore_native_project(main)
    (main / ".gitignore").write_text(".booley/\n", encoding="utf-8")
    commit_all(main)
    worktree = snapshot(main, name="wt")
    # Goal runs place the variant build under the main Booley Directory,
    # outside the worktree root.
    return worktree, main / ".booley" / ".runtime" / "mutant_1"


@pytest.mark.parametrize("layout", ["main", "symlinked-booley-dir", "linked-worktree"])
def test_ignore_native_cores_mutation_writes_only_private_export(tmp_path, monkeypatch, layout):
    root, build_path = _ignore_native_layout(tmp_path, monkeypatch, layout)
    resolve = MutationTesterSpecialist._resolve_elab_target
    # First use projects the shared registry; that is not a mutation side effect.
    resolve("sim", root, tmp_path / "pristine-build")
    # The pristine baseline builds from the same kind of private mirror, so it
    # cannot pass on a header only the checkout holds.
    assert (tmp_path / "pristine-build" / "core-mirror").is_dir()
    spec = MutationSpec(1, "literal", "rtl/inc/message.svh", 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], root, ["rtl/inc/message.svh"])
    before = _tree_snapshot(root, excluded=build_path)
    before_external = _tree_snapshot(tmp_path / "external-booley", build_path)
    _handle, prepared = resolve("sim", root, build_path, (variants, 1))
    exported = list(Path(prepared.resolved.build_root).rglob("message.svh"))
    assert exported
    assert (build_path / "core-mirror").is_dir()
    assert all(b"MUTANT" in path.read_bytes() for path in exported)
    assert all(path.resolve().is_relative_to(build_path.resolve()) for path in exported)
    assert _tree_snapshot(root, excluded=build_path) == before
    assert _tree_snapshot(tmp_path / "external-booley", build_path) == before_external
    assert (root / "rtl" / "inc" / "message.svh").read_text() == _HEADER
    if shutil.which("iverilog"):
        _run(["make", "all"], Path(prepared.resolved.build_root))
        assert "MUTANT" in _run(["make", "run"], Path(prepared.resolved.build_root))


def test_isolated_mutant_marker_skips_uncopied_user_files(tmp_path, monkeypatch):
    """Only ``copyto`` inputs are staged into the run folder; a mirrored user file is not."""
    root, build_path = _ignore_native_layout(tmp_path, monkeypatch, "main")
    (root / "data").mkdir()
    for name in ("plain.hex", "copied.hex"):
        (root / "data" / name).write_text("00\n", encoding="utf-8")
    core = root / ".booley" / "cores" / "dut.core"
    body = yaml.safe_load(core.read_text(encoding="utf-8"))
    body["filesets"]["rtl"]["files"] += [
        {"data/plain.hex": {"file_type": "user"}},
        {"data/copied.hex": {"file_type": "user", "copyto": "copied.hex"}},
    ]
    core.write_text("CAPI=2:\n" + yaml.safe_dump(body), encoding="utf-8")
    spec = MutationSpec(1, "literal", "rtl/inc/message.svh", 1, "BASELINE", "MUTANT")
    variants = MutationVariantPlan.resolve([spec], root, ["rtl/inc/message.svh"])
    _handle, prepared = MutationTesterSpecialist._resolve_elab_target(
        "sim", root, build_path, (variants, 1)
    )
    _record_build_markers(build_path, prepared, root)
    marker = build_path / _RUNTIME_INPUTS_MARKER
    assert json.loads(marker.read_text(encoding="utf-8")) == ["copied.hex"]


def test_pinned_build_fallback_refuses_in_isolated_project(tmp_path, monkeypatch):
    """A lost baseline marker must not re-resolve into (and clear) the built image."""
    root, _build_path = _ignore_native_layout(tmp_path, monkeypatch, "main")
    build_path = tmp_path / "baseline-build"
    build_path.mkdir()
    image = build_path / "compiled" / "image"
    image.parent.mkdir()
    image.write_text("built\n", encoding="utf-8")
    specialist = MutationTesterSpecialist.__new__(MutationTesterSpecialist)
    with pytest.raises(UnsupportedSimTargetError, match="baseline build marker missing"):
        specialist._pinned_build("sim", root, build_path)
    assert image.read_text(encoding="utf-8") == "built\n"


def test_ignore_native_cores_guard_refuses_out_of_checkout_source(tmp_path, monkeypatch):
    root, build_path = _ignore_native_layout(tmp_path, monkeypatch, "main")
    outside = tmp_path / "outside.sv"
    outside.write_text(_DUT, encoding="utf-8")
    symlink_or_skip(root / "rtl" / "ext.sv", outside)
    core = root / ".booley" / "cores" / "dut.core"
    data = yaml.safe_load(core.read_text(encoding="utf-8"))
    data["filesets"]["rtl"]["files"].append("rtl/ext.sv")
    core.write_text("CAPI=2:\n" + yaml.safe_dump(data), encoding="utf-8")
    spec = MutationSpec(1, "literal", "rtl/ext.sv", 1, "$finish", "$stop")
    variants = MutationVariantPlan.resolve([spec], root, ["rtl/ext.sv"])
    with pytest.raises(MutationVariantError, match="absolute mutation export"):
        MutationTesterSpecialist._resolve_elab_target("sim", root, build_path, (variants, 1))
    assert outside.read_text(encoding="utf-8") == _DUT
