"""#2895: isolated simulation and coverage builds read only fileset-listed headers.

In an ``ignore_native_cores`` Project the shared registry names every source by
absolute path, so FuseSoC exports nothing and each include directory is the
user's real folder. Simulation preparation now resolves through the build's
private source mirror. These tests use real FuseSoC/Edalize and need no
simulator: they pin the prepared include directories, the names reports derive
from the mirror, the dry-run preview and the unchanged non-isolated behavior.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from booley.core.build_paths import work_root_for
from booley.flows.sim import build_session
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    prepare_simulation_build,
    simulation_setup_command,
)
from booley.flows.sim.build_reuse import stock_verilator_recipe
from booley.flows.sim.build_session import snapshot_build_inputs
from booley.flows.sim.coverage_overlay import write_coverage_overlay
from booley.flows.sim.verilator_coverage import VERILATOR_COVERAGE_INSTRUMENTATION
from booley.flows.sim.verilator_coverage_execution import prepare_coverage_collection
from booley.flows.sim.workload import capture_workload_inputs
from booley.fusesoc.fusesoc_registry import setup_command_for_handle
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.flows.test_isolated_build_headers import real_fusesoc  # noqa: F401  (fixture)

pytest.importorskip("fusesoc")
pytest.importorskip("edalize")

_HEADER = {"file_type": "verilogSource", "is_include_file": True}
_TOP = '`include "listed.vh"\nmodule top; endmodule\n'


def _core(
    *,
    dependency: bool = False,
    absolute_include: Path | None = None,
    user_files: bool = True,
    headers: bool = True,
) -> str:
    """The fixture core; every header sits in ``rtl/inc``, one with an ``include_path``."""
    files: list = [
        {"rtl/top.v": {"file_type": "verilogSource"}},
    ]
    if headers:
        files += [
            {"rtl/inc/listed.vh": _HEADER},
            {"rtl/inc/keep.vh": {**_HEADER, "include_path": "rtl/inc"}},
        ]
    if user_files:
        files += [
            {"data/plain.hex": {"file_type": "user"}},
            {"data/copied.hex": {"file_type": "user", "copyto": "copied.hex"}},
        ]
    if absolute_include is not None:
        files.append({absolute_include.as_posix(): _HEADER})
    targets = {
        "sim_icarus": {
            "flow": "sim",
            "flow_options": {"tool": "icarus"},
            "filesets": ["rtl"],
            "toplevel": "top",
        },
        "sim_verilator": {
            "flow": "sim",
            "flow_options": {"tool": "verilator", "verilator_options": ["--timing"]},
            "filesets": ["rtl"],
            "toplevel": "top",
        },
    }
    body: dict = {"name": "acme:lib:design:1", "filesets": {"rtl": {"files": files}}}
    if dependency:
        body["filesets"]["rtl"]["depend"] = ["acme:lib:dep:1"]
    body["targets"] = targets
    return "CAPI=2:\n" + yaml.safe_dump(body, sort_keys=False)


_DEPENDENCY_CORE = "CAPI=2:\n" + yaml.safe_dump(
    {
        "name": "acme:lib:dep:1",
        "filesets": {
            "rtl": {
                "files": [
                    {"dep/dep.v": {"file_type": "verilogSource"}},
                    {"dep/inc/dep.vh": _HEADER},
                ]
            }
        },
        "targets": {"default": {"filesets": ["rtl"]}},
    },
    sort_keys=False,
)


def _write_project(
    root: Path,
    variant: str = "isolated",
    *,
    dependency: bool = False,
    absolute_include: Path | None = None,
) -> Path:
    """Create a Project: ``isolated`` or ``projected`` (stealth, native core) or ``plain``."""
    (root / "rtl" / "inc").mkdir(parents=True)
    (root / "rtl" / "top.v").write_text(_TOP, encoding="utf-8")
    (root / "data").mkdir()
    for name in ("plain", "copied"):
        (root / "data" / f"{name}.hex").write_text("00\n", encoding="utf-8")
    for name in ("listed", "keep", "unlisted"):
        (root / "rtl" / "inc" / f"{name}.vh").write_text(f"// {name} v1\n", encoding="utf-8")
    booley = root / ".booley"
    booley.mkdir()
    stealth = {
        "isolated": "[stealth]\nenabled = true\nignore_native_cores = true\n",
        "projected": "[stealth]\nenabled = true\n",
        "plain": "[flows.sim]\n",
    }[variant]
    (booley / "booley.toml").write_text(stealth, encoding="utf-8")
    cores = booley / "cores" if variant == "isolated" else root
    cores.mkdir(exist_ok=True)
    (cores / "design.core").write_text(
        _core(dependency=dependency, absolute_include=absolute_include), encoding="utf-8"
    )
    if dependency:
        (root / "dep" / "inc").mkdir(parents=True)
        (root / "dep" / "dep.v").write_text("module dep; endmodule\n", encoding="utf-8")
        (root / "dep" / "inc" / "dep.vh").write_text("// dep\n", encoding="utf-8")
        (cores / "dep.core").write_text(_DEPENDENCY_CORE, encoding="utf-8")
    return root


def _handle(root: Path, selector: str) -> TargetHandle:
    return TargetCatalog.build(root).select(selector, for_flow="sim")


def _prepare(root: Path, selector: str, *, name: str = "build") -> PreparedSimulationBuild:
    """Prepare one build into a fresh build root, as a build generation would."""
    handle = _handle(root, selector)
    return prepare_simulation_build(
        handle, build_root=work_root_for(root, "sim", selector, variant=name)
    )


def _recipe_incdirs(prepared: PreparedSimulationBuild) -> list[Path]:
    """Every ``+incdir+`` folder the generated Verilator/Icarus recipe names."""
    folders = []
    for recipe in (*prepared.build_root.glob("*.vc"), *prepared.build_root.glob("*.scr")):
        for line in recipe.read_text(encoding="utf-8").splitlines():
            if line.startswith("+incdir+"):
                folders.append((prepared.build_root / line.removeprefix("+incdir+")).resolve())
    return folders


def _recipe_text(prepared: PreparedSimulationBuild) -> str:
    return "".join(
        recipe.read_text(encoding="utf-8")
        for recipe in (*prepared.build_root.glob("*.vc"), *prepared.build_root.glob("*.scr"))
    )


def _holds(folders: list[Path], name: str) -> bool:
    return any((folder / name).exists() for folder in folders)


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("selector", ["sim_icarus", "sim_verilator"])
def test_isolated_sim_include_dirs_hold_only_listed_headers(tmp_path: Path, selector: str) -> None:
    """AC1: every include directory is inside the build's FuseSoC export."""
    root = _write_project(tmp_path / "proj")
    prepared = _prepare(root, selector)
    export = prepared.build_root.resolve()
    mirror = prepared.work_root.resolve() / "core-mirror"
    folders = [Path(folder).resolve() for folder in prepared.resolved.rtl_include_dirs]
    recipe_folders = _recipe_incdirs(prepared)
    assert folders and recipe_folders
    for folder in (*folders, *recipe_folders):
        assert folder.is_relative_to(export), folder
        assert not folder.is_relative_to(mirror), folder
    assert _holds(folders, "listed.vh")
    assert not _holds(folders, "unlisted.vh")
    # No absolute checkout or generation path reaches the recipe, so the names
    # Verilator records (and coverage point IDs) cannot depend on either.
    text = _recipe_text(prepared)
    assert str(root) not in text
    assert str(prepared.work_root) not in text
    assert mirror.is_dir()


def _prepare_coverage(
    root: Path, *, trace: bool, name: str = "coverage"
) -> tuple[PreparedSimulationBuild, str]:
    """Prepare the coverage (or trace+coverage) build; return it and its overlay VLNV."""
    handle = _handle(root, "sim_verilator")
    overlay = write_coverage_overlay(
        handle, instrumentation=VERILATOR_COVERAGE_INSTRUMENTATION, trace=trace
    )
    try:
        prepared = prepare_simulation_build(
            handle,
            build_root=work_root_for(root, "sim", "sim_verilator", variant=name),
            resolution_vlnv=overlay.vlnv,
        )
    finally:
        overlay.cleanup()
    return prepared, overlay.vlnv


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("trace", [False, True], ids=["plain", "trace"])
def test_isolated_coverage_build_include_dirs_hold_only_listed_headers(
    tmp_path: Path, trace: bool
) -> None:
    """AC1: coverage and trace+coverage builds read only listed headers."""
    root = _write_project(tmp_path / "proj")
    prepared, _vlnv = _prepare_coverage(root, trace=trace)
    folders = [Path(folder).resolve() for folder in prepared.resolved.rtl_include_dirs]
    assert folders
    assert all(folder.is_relative_to(prepared.build_root.resolve()) for folder in folders)
    assert _holds(folders, "listed.vh")
    assert not _holds(folders, "unlisted.vh")
    assert (prepared.work_root / "core-mirror").is_dir()


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_traced_sim_resolves_through_mirror(tmp_path: Path) -> None:
    """AC1 for traced Simulation: the injected packaged dump module keeps its absolute name."""
    from booley.flows.sim.trace_overlay import write_trace_overlay

    root = _write_project(tmp_path / "proj")
    handle = _handle(root, "sim_icarus")
    overlay = write_trace_overlay(handle)
    try:
        prepared = prepare_simulation_build(
            handle,
            build_root=work_root_for(root, "sim", "sim_icarus", variant="trace"),
            resolution_vlnv=overlay.vlnv,
        )
    finally:
        overlay.cleanup()
    folders = [Path(folder).resolve() for folder in prepared.resolved.rtl_include_dirs]
    assert not _holds(folders, "unlisted.vh")
    assert all(folder.is_relative_to(prepared.build_root.resolve()) for folder in folders)
    dump = [item for item in prepared.resolved.files if item.name.endswith("booley_vcd_dump.sv")]
    assert dump
    assert all(Path(item.name).is_absolute() for item in dump)


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_sim_dropped_header_is_not_reachable(tmp_path: Path) -> None:
    """Delisting a header between builds into the same root removes it from the export."""
    root = _write_project(tmp_path / "proj")
    first = _prepare(root, "sim_icarus")
    assert _holds([Path(d) for d in first.resolved.rtl_include_dirs], "listed.vh")
    core = root / ".booley" / "cores" / "design.core"
    body = yaml.safe_load(core.read_text(encoding="utf-8"))
    body["filesets"]["rtl"]["files"] = [
        item for item in body["filesets"]["rtl"]["files"] if "listed.vh" not in str(item)
    ]
    core.write_text("CAPI=2:\n" + yaml.safe_dump(body), encoding="utf-8")
    second = _prepare(root, "sim_icarus")
    assert not _holds([Path(d) for d in second.resolved.rtl_include_dirs], "listed.vh")
    assert not list(second.work_root.rglob("listed.vh"))  # neither exported nor mirrored


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("tool", ["verilator", "iverilog"])
def test_isolated_sim_tool_error_names_unlisted_header(tmp_path: Path, tool: str) -> None:
    """AC1: a source that includes an unlisted header fails the build, naming the header."""
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")
    root = _write_project(tmp_path / "proj")
    (root / "rtl" / "top.v").write_text(
        '`include "unlisted.vh"\nmodule top; endmodule\n', encoding="utf-8"
    )
    prepared = _prepare(root, "sim_verilator" if tool == "verilator" else "sim_icarus")
    result = subprocess.run(
        list(prepared.make_argv),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "unlisted.vh" in result.stdout + result.stderr


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_absolute_include_keeps_reuse_off(tmp_path: Path) -> None:
    """An author-written absolute include keeps its real folder, so Verilator reuse declines."""
    root = _write_project(tmp_path / "proj")
    assert stock_verilator_recipe(_prepare(root, "sim_verilator").build_root) is not None
    outside = root.resolve() / "ext"
    outside.mkdir()
    (outside / "abs.vh").write_text("// abs\n", encoding="utf-8")
    (outside / "other.vh").write_text("// other\n", encoding="utf-8")
    _write_project_core(root, absolute_include=outside / "abs.vh")
    prepared = _prepare(root, "sim_verilator", name="absolute")
    assert _holds(_recipe_incdirs(prepared), "other.vh")
    assert stock_verilator_recipe(prepared.build_root) is None


def _write_project_core(root: Path, **kwargs: Path | bool) -> None:
    (root / ".booley" / "cores" / "design.core").write_text(_core(**kwargs), encoding="utf-8")


def _icarus_key(prepared: PreparedSimulationBuild, root: Path) -> str | None:
    """The Icarus reuse key of one prepared build, with the host tool identity pinned."""
    return build_session._reuse_input_key(prepared, snapshot_build_inputs(prepared), "", root, {})


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_icarus_reuse_key_is_stable_and_follows_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: an isolated Icarus build keys the same across generations until an input changes."""
    monkeypatch.setattr(build_session, "_icarus_tool_identity", lambda: "iverilog-test")
    monkeypatch.setattr(build_session, "_python_recipe_identity", lambda: "recipe-test")
    root = _write_project(tmp_path / "proj")
    _write_project_core(root, user_files=False, headers=False)
    top = root / "rtl" / "top.v"
    top.write_text("module top; endmodule\n", encoding="utf-8")
    first = _icarus_key(_prepare(root, "sim_icarus", name="g1"), root)
    assert first is not None
    assert _icarus_key(_prepare(root, "sim_icarus", name="g2"), root) == first
    top.write_text("module top; // edited\nendmodule\n", encoding="utf-8")
    edited = _icarus_key(_prepare(root, "sim_icarus", name="g3"), root)
    assert edited not in (None, first)


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_mirrored_names_stay_project_relative(tmp_path: Path) -> None:
    """AC3: workload snapshot rows never show the mirror."""
    root = _write_project(tmp_path / "proj")
    prepared = _prepare(root, "sim_verilator")
    rows = {row["path"] for row in capture_workload_inputs(root, prepared.resolved)}
    assert {"rtl/top.v", "rtl/inc/listed.vh", "data/plain.hex"} <= rows
    assert not [path for path in rows if "core-mirror" in path or path.startswith("src/")]


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("variant", ["plain", "projected"])
def test_non_isolated_sim_preparation_is_unchanged(tmp_path: Path, variant: str) -> None:
    """AC4: no mirror, build roots untouched, literal include dirs and names, same key records."""
    root = _write_project(tmp_path / "proj", variant)
    sentinel = work_root_for(root, "sim", "sim_verilator", variant="build") / "sentinel"
    sentinel.mkdir(parents=True)
    prepared = _prepare(root, "sim_verilator")
    assert sentinel.is_dir()
    assert not list(tmp_path.rglob("core-mirror"))
    export = prepared.build_root.resolve() / "src" / "acme_lib_design_1" / "rtl" / "inc"
    assert [Path(d).resolve() for d in prepared.resolved.rtl_include_dirs] == [export]
    rows = {row["path"] for row in capture_workload_inputs(root, prepared.resolved)}
    assert any(path.endswith("src/acme_lib_design_1/rtl/top.v") for path in rows)
    assert prepared.core_closure is not None
    assert all("<generation>" not in value for value in prepared.core_closure.values())


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_sim_dry_run_names_build_mirror(tmp_path: Path) -> None:
    """The preview names the mirror the run renders, never the shared registry, and writes nothing."""
    root = _write_project(tmp_path / "proj")
    handle = _handle(root, "sim_icarus")
    build_root = work_root_for(root, "sim", "sim_icarus", variant="preview")
    command = simulation_setup_command(handle, build_root=build_root)
    assert command[command.index("--cores-root") + 1] == str(build_root.resolve() / "core-mirror")
    assert not build_root.exists()
    assert not list(tmp_path.rglob("core-mirror"))


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("variant", ["plain", "projected"])
def test_non_isolated_sim_dry_run_is_unchanged(tmp_path: Path, variant: str) -> None:
    root = _write_project(tmp_path / "proj", variant)
    handle = _handle(root, "sim_icarus")
    build_root = work_root_for(root, "sim", "sim_icarus", variant="preview")
    assert simulation_setup_command(handle, build_root=build_root) == setup_command_for_handle(
        handle, build_root=build_root
    )


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_sim_dry_run_rejects_unrebasable_core(tmp_path: Path) -> None:
    """A core with a non-literal fileset path fails the preview, as it fails the run."""
    root = _write_project(tmp_path / "proj")
    (root / ".booley" / "cores" / "bad.core").write_text(
        "CAPI=2:\nname: acme:lib:bad:1\nfilesets:\n  rtl:\n    files: ['$SRC/bad.v']\n",
        encoding="utf-8",
    )
    handle = _handle(root, "sim_icarus")
    with pytest.raises(SimulationBuildPreparationError, match="literal fileset paths"):
        simulation_setup_command(handle, build_root=work_root_for(root, "sim", "sim_icarus"))


@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("trace", [False, True], ids=["plain", "trace"])
def test_isolated_coverage_foreign_paths_cover_prepared_headers(
    tmp_path: Path, trace: bool
) -> None:
    """AC3: every header the coverage build exports is named by ``foreign_paths``."""
    root = _write_project(tmp_path / "proj", dependency=True)
    prepared, _vlnv = _prepare_coverage(root, trace=trace)
    request = prepare_coverage_collection(
        _handle(root, "sim_verilator"),
        selected_tests=(),
        artifact_root=tmp_path / "artifacts",
        trace=trace,
    )
    headers = {item.name for item in prepared.resolved.files if item.is_include}
    assert {name for name in headers if "dep.vh" in name or "listed.vh" in name}
    for name in headers:
        assert name in request.target.foreign_paths, name
        # ``src/<core>/<Project-relative path>``
        assert request.target.foreign_paths[name] == name.split("/", 2)[2]
    assert any(f"-booley{'trace' if trace else ''}coverage" in name for name in headers)


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_coverage_recipe_is_checkout_independent(tmp_path: Path) -> None:
    """The names Verilator records (hence coverage point IDs) do not depend on the checkout."""
    recipes = []
    for index in range(2):
        root = _write_project(tmp_path / f"checkout-{index}")
        prepared, _vlnv = _prepare_coverage(root, trace=False)
        recipes.append(_recipe_text(prepared))
    assert recipes[0] == recipes[1]
