"""#2893: isolated Vivado, synthesis and lint builds read only listed headers.

In an ``ignore_native_cores`` Project the shared registry names every source by
absolute path, so FuseSoC exports nothing and each include directory is the
user's real folder. These tests use real FuseSoC and fake only Edalize's Tcl
generation and the EDA tools, so they pin the resolved include directories, the
Vivado cache key and the non-isolated Projects' unchanged behavior.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from booley.core.build_paths import work_root_for
from booley.criteria.state import DevelopmentState
from booley.flows import edam as edam_layer
from booley.flows.fpga.backends.vivado import cache as fpga_cache
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.implementation_comparison import (
    TargetExecutionRef,
    resolve_target_execution_ref,
)
from booley.flows.lint.flow import LintFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from tests.fusesoc_test_support import symlinked_booley_dir

pytest.importorskip("fusesoc")
pytest.importorskip("edalize")

_REAL_RESOLVE_TARGET_HANDLE = fusesoc_registry.resolve_target_handle
_TOP = '`include "listed.vh"\nmodule top; endmodule\n'


def _core(
    *,
    version: int = 0,
    listed: bool = True,
    extra_fileset: dict | None = None,
    extra_target_filesets: list[str] = (),
    absolute_include: Path | None = None,
    outside_include: str | None = None,
) -> str:
    """Render the fixture core; every header sits in ``rtl/inc``."""
    typed = {"file_type": "verilogSource"}
    header = {"file_type": "verilogSource", "is_include_file": True}
    files: list = [{"rtl/top.v": typed}, {"rtl/inc/keep.vh": header}]
    if listed:
        files.append({"rtl/inc/listed.vh": header})
    if absolute_include is not None:
        files.append({absolute_include.as_posix(): header})
    if outside_include is not None:
        files.append({outside_include: header})
    filesets = {
        "rtl": {"files": files},
        "constraints": {"files": [{"constraints/timing.xdc": {"file_type": "xdc"}}]},
    }
    if extra_fileset:
        filesets.update(extra_fileset)
    rtl = ["rtl", *extra_target_filesets]
    targets = {
        "fpga": {
            "default_tool": "vivado",
            "flow": "generic",
            "flow_options": {"tool": "vivado", "part": "xc7a35tcpg236-1"},
            "filesets": [*rtl, "constraints"],
            "toplevel": "top",
        },
        "synth": {
            "default_tool": "yosys",
            "flow": "generic",
            "flow_options": {"tool": "yosys", "arch": "xilinx"},
            "filesets": rtl,
            "toplevel": "top",
        },
        "lint": {
            "default_tool": "verilator",
            "flow": "lint",
            "flow_options": {"tool": "verilator", "verilator_options": ["-Wall"]},
            "filesets": rtl,
            "toplevel": "top",
        },
    }
    body = {"name": f"::design:{version}", "filesets": filesets, "targets": targets}
    return "CAPI=2:\n" + yaml.safe_dump(body, sort_keys=False)


def _write_project(
    root: Path,
    variant: str,
    *,
    symlinked_booley: Path | None = None,
    **core_kwargs,
) -> Path:
    """Create a Project: ``isolated``, ``projected`` (stealth, native core) or ``plain``."""
    root.mkdir(parents=True, exist_ok=True)
    if symlinked_booley is not None:
        symlinked_booley_dir(root, symlinked_booley)
    (root / "rtl" / "inc").mkdir(parents=True)
    (root / "constraints").mkdir()
    (root / "rtl" / "top.v").write_text(_TOP, encoding="utf-8")
    for name in ("listed", "keep", "unlisted"):
        (root / "rtl" / "inc" / f"{name}.vh").write_text(f"// {name} v1\n", encoding="utf-8")
    (root / "constraints" / "timing.xdc").write_text("create_clock\n", encoding="utf-8")
    booley = root / ".booley"
    booley.mkdir(exist_ok=True)
    stealth = {
        "isolated": "[stealth]\nenabled = true\nignore_native_cores = true\n",
        "projected": "[stealth]\nenabled = true\n",
        "plain": "[flows.fpga]\n",
    }[variant]
    (booley / "booley.toml").write_text(stealth, encoding="utf-8")
    _write_core(root, variant, **core_kwargs)
    return root


def _core_path(root: Path, variant: str) -> Path:
    if variant in {"plain", "projected"}:
        return root / "design.core"
    (root / ".booley" / "cores").mkdir(parents=True, exist_ok=True)
    return root / ".booley" / "cores" / "design.core"


def _write_core(root: Path, variant: str, **core_kwargs) -> None:
    _core_path(root, variant).write_text(_core(**core_kwargs), encoding="utf-8")


def _real_fusesoc(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    """Route resolution through the installed FuseSoC with private user config."""
    config = scratch / "isolated-fusesoc.conf"
    config.write_text("[main]\n", encoding="utf-8")
    command = [
        sys.executable,
        "-c",
        "from fusesoc.main import main; main()",
        "--config",
        str(config),
    ]
    env = dict(os.environ)
    env.pop("FUSESOC_CORES", None)
    for kind in ("CONFIG", "CACHE", "DATA"):
        env[f"XDG_{kind}_HOME"] = str(scratch / f"isolated-{kind.lower()}")

    def resolve(*args, **kwargs):
        return _REAL_RESOLVE_TARGET_HANDLE(*args, **{**kwargs, "fusesoc_cmd": command, "env": env})

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)


@pytest.fixture
def real_fusesoc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    state = tmp_path / "state.json"
    DevelopmentState.load(state).save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
    scratch = tmp_path.parent / f"{tmp_path.name}-fusesoc"
    scratch.mkdir()
    _real_fusesoc(monkeypatch, scratch)


def _fake_vivado_tcl(_flow, edam, work_root) -> None:
    """Stand in for Edalize's Tcl generation (no Vivado needed)."""
    Path(work_root).mkdir(parents=True, exist_ok=True)
    (Path(work_root) / f"{edam['name']}.tcl").write_text(
        "set_property generic {} [get_filesets sources_1]\n", encoding="utf-8"
    )


def _fpga_flow(root: Path) -> FpgaImplFlow:
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "fpga", "--work-dir", str(root)])
    flow.read_state()
    return flow


def _fpga_dirs(flow: FpgaImplFlow) -> list[Path]:
    """Every folder Vivado resolves a relative include in."""
    recipe = flow._resolve_fpga_recipe("fpga")
    folders = [*recipe.include_dirs, *(p.parent for p in (*recipe.sv_files, *recipe.v_files))]
    return [Path(folder) for folder in folders]


def _fpga_fingerprint(flow: FpgaImplFlow) -> str:
    with patch.object(edam_layer, "configure", side_effect=_fake_vivado_tcl):
        return flow._prepare_fpga_command("fpga").fingerprint


def _fpga_fusesoc_root(root: Path) -> Path:
    return work_root_for(root, "fpga", "fpga", variant="fusesoc")


def _holds(folders: list[Path], name: str) -> bool:
    return any((folder / name).exists() for folder in folders)


def _lint_incdirs(root: Path) -> tuple[Path, list[Path], str]:
    """Run lint setup; return the build root, its ``+incdir+`` folders and the .vc text."""
    handle = TargetCatalog.build(root).select("lint", for_flow="lint")
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(root), "--target", "lint"])
    flow.read_state()
    prepared = flow._prepare_lint_command(handle)
    build_root = Path(prepared.resolved.build_root)
    vc = next(build_root.glob("*.vc")).read_text(encoding="utf-8")
    incdirs = [
        (build_root / line.removeprefix("+incdir+")).resolve()
        for line in vc.splitlines()
        if line.startswith("+incdir+")
    ]
    return build_root, incdirs, vc


# ---------------------------------------------------------------------------
# Red set: isolated builds read only listed headers
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_fpga_include_dirs_hold_only_listed_headers(tmp_path: Path) -> None:
    root = _write_project(tmp_path / "proj", "isolated")
    folders = _fpga_dirs(_fpga_flow(root))
    export = _fpga_fusesoc_root(root).resolve()
    assert folders
    assert all(folder.resolve().is_relative_to(export) for folder in folders), folders
    assert _holds(folders, "listed.vh")
    assert not _holds(folders, "unlisted.vh")


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("layout", ["plain-dir", "symlinked-booley-dir"])
@pytest.mark.parametrize("folder", ["rtl/inc", "tb"])
def test_isolated_tb_file_in_exported_sources_is_in_vivado_key(
    tmp_path: Path, layout: str, folder: str
) -> None:
    """A test-only header Vivado can read moves the key: via an include dir or a ``../``."""
    sim_only = {
        "files": [
            {
                f"{folder}/sim_only.vh": {
                    "file_type": "verilogSource" if folder == "rtl/inc" else "user",
                    "is_include_file": folder == "rtl/inc",
                }
            }
        ],
        "tags": ["tb"],
    }
    external = tmp_path / "external-booley" if layout == "symlinked-booley-dir" else None
    root = _write_project(tmp_path / "proj", "isolated", symlinked_booley=external)
    header = root / folder / "sim_only.vh"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text("// sim v1\n", encoding="utf-8")
    _write_core(
        root, "isolated", extra_fileset={"simfs": sim_only}, extra_target_filesets=["simfs"]
    )
    flow = _fpga_flow(root)
    recipe = flow._resolve_fpga_recipe("fpga")
    # Test-only, so it can enter the key only through reachable_dirs.
    assert all(Path(item.name).name != "sim_only.vh" for item in recipe.resolved.rtl_files)
    before = _fpga_fingerprint(flow)
    assert _fpga_fingerprint(flow) == before  # an unchanged mirror rebuild keeps the key
    header.write_text("// sim v2\n", encoding="utf-8")
    assert _fpga_fingerprint(flow) != before


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_absolute_in_checkout_include_folder_is_in_vivado_key(tmp_path: Path) -> None:
    root = _write_project(tmp_path / "proj", "isolated")
    ext = root.resolve() / "ext" / "inc"
    ext.mkdir(parents=True)
    (ext / "ext.vh").write_text("// ext\n", encoding="utf-8")
    (ext / "other.vh").write_text("// other v1\n", encoding="utf-8")
    _write_core(root, "isolated", absolute_include=ext / "ext.vh")
    flow = _fpga_flow(root)
    before = _fpga_fingerprint(flow)
    (ext / "other.vh").write_text("// other v2\n", encoding="utf-8")
    assert _fpga_fingerprint(flow) != before


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_synth_include_dirs_hold_only_listed_headers(tmp_path: Path) -> None:
    """AC1: synthesis ``--inc-dir`` and ``--extra-rtl`` stay inside the synth work root."""
    root = _write_project(tmp_path / "proj", "isolated")
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "synth", "--work-dir", str(root)])
    flow.read_state()
    resolved = flow._resolve_synth_target("synth")
    cmd: list[str] = []
    flow._append_rtl_source_args(cmd, resolved, root)
    folders = {
        "--inc-dir": [root / cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--inc-dir"],
        "--extra-rtl": [
            (root / cmd[i + 1]).parent for i, arg in enumerate(cmd) if arg == "--extra-rtl"
        ],
    }
    work_root = flow._synth_work_root("synth").resolve()
    for flag, paths in folders.items():
        assert paths, flag
        assert all(path.resolve().is_relative_to(work_root) for path in paths), (flag, paths)
        assert not _holds(paths, "unlisted.vh"), flag


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_lint_vc_names_only_listed_headers(tmp_path: Path) -> None:
    """AC2: lint's generated ``.vc`` names only export folders."""
    root = _write_project(tmp_path / "proj", "isolated")
    build_root, incdirs, vc = _lint_incdirs(root)
    assert incdirs
    assert all(folder.is_relative_to(build_root.resolve()) for folder in incdirs), incdirs
    assert not _holds(incdirs, "unlisted.vh")
    assert str(root) not in vc


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("flow_name", ["fpga", "lint"])
def test_isolated_dropped_header_is_not_reachable(tmp_path: Path, flow_name: str) -> None:
    """A header delisted between runs must not stay readable through the old export."""
    root = _write_project(tmp_path / "proj", "isolated")

    def folders() -> list[Path]:
        if flow_name == "fpga":
            return _fpga_dirs(_fpga_flow(root))
        return _lint_incdirs(root)[1]

    assert _holds(folders(), "listed.vh")
    _write_core(root, "isolated", listed=False)
    after = folders()
    assert _holds(after, "keep.vh")
    assert not _holds(after, "listed.vh")


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("which", ["candidate", "baseline"])
def test_execution_ref_resolves_through_mirror(tmp_path: Path, which: str) -> None:
    root = _write_project(tmp_path / "proj", "isolated")
    checkout = root
    if which == "baseline":
        checkout = tmp_path / "baseline-copy"
        shutil.copytree(root, checkout, symlinks=True)
    handle = TargetCatalog.build(checkout).select("fpga", for_flow="fpga")
    ref = TargetExecutionRef(handle.identity, handle.selector, handle.vlnv)
    build_root = tmp_path / f"{which}-build"
    resolved = resolve_target_execution_ref(handle, ref, build_root=build_root)
    assert (build_root / "core-mirror").is_dir()
    folders = list(resolved.rtl_include_dirs)
    assert folders
    assert all(folder.resolve().is_relative_to(build_root.resolve()) for folder in folders)
    assert not _holds(folders, "unlisted.vh")


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator unavailable")
def test_lint_tool_error_names_unlisted_header(tmp_path: Path) -> None:
    root = _write_project(tmp_path / "proj", "isolated")
    (root / "rtl" / "top.v").write_text('`include "unlisted.vh"\nmodule top; endmodule\n')
    build_root, _incdirs, _vc = _lint_incdirs(root)
    run = subprocess.run(
        ["make", "-C", str(build_root)], capture_output=True, text=True, check=False
    )
    assert run.returncode != 0
    assert "unlisted.vh" in run.stdout + run.stderr


# ---------------------------------------------------------------------------
# Guards: pass before and after the fix
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_outside_checkout_fpga_name_fails_loudly(tmp_path: Path) -> None:
    root = _write_project(tmp_path / "proj", "isolated")
    outside = tmp_path / "shared" / "inc"
    outside.mkdir(parents=True)
    (outside / "ext.vh").write_text("// ext\n", encoding="utf-8")
    _write_core(root, "isolated", outside_include="../shared/inc/ext.vh")
    flow = _fpga_flow(root)
    with (
        patch.object(edam_layer, "configure", side_effect=_fake_vivado_tcl),
        pytest.raises(edam_layer.EdamSecurityError),
    ):
        flow._prepare_fpga_command("fpga")


def _export_inc(work_root: Path, target: str) -> Path:
    """Literal FuseSoC export folder of ``rtl/inc`` for the fixture core."""
    return (work_root / "design_0" / target / "src" / "design_0" / "rtl" / "inc").resolve()


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("variant", ["projected", "plain"])
def test_non_isolated_builds_are_unchanged(tmp_path: Path, variant: str) -> None:
    """AC4: no mirror, build roots untouched, literal include dirs, key as before."""
    root = _write_project(tmp_path / "proj", variant)
    fpga_root = _fpga_fusesoc_root(root)
    lint_root = work_root_for(root, "lint", "lint")
    for sentinel_root in (fpga_root, lint_root):
        (sentinel_root / "sentinel").mkdir(parents=True)
    flow = _fpga_flow(root)
    recipe = flow._resolve_fpga_recipe("fpga")
    assert [Path(d).resolve() for d in recipe.include_dirs] == [_export_inc(fpga_root, "fpga")]
    assert recipe.reachable_dirs == ()
    # The key is the pre-#2893 one: computed without any reachable_dirs argument.
    with patch.object(edam_layer, "configure", side_effect=_fake_vivado_tcl):
        edam = flow._materialize_fpga_project(recipe, work_root_for(root, "fpga", "fpga"))
        prepared = flow._prepare_fpga_command("fpga")
    assert prepared.fingerprint == fpga_cache.input_fingerprint(
        recipe.resolved,
        edam,
        out_of_context=recipe.out_of_context,
        recipe_sha256=recipe.recipe_fingerprint,
    )
    _build_root, incdirs, _vc = _lint_incdirs(root)
    assert incdirs == [_export_inc(lint_root, "lint")]
    synth = AsicSynthesizeFlow()
    synth.parse_args(["--target", "synth", "--work-dir", str(root)])
    synth.read_state()
    resolved = synth._resolve_synth_target("synth")
    assert [d.resolve() for d in resolved.rtl_include_dirs] == [
        _export_inc(synth._synth_work_root("synth"), "synth")
    ]
    assert not list(tmp_path.rglob("core-mirror"))
    assert (fpga_root / "sentinel").is_dir()
    assert (lint_root / "sentinel").is_dir()
