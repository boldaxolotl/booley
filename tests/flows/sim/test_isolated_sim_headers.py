"""#2895: isolated simulation and coverage builds read only fileset-listed headers.

In an ``ignore_native_cores`` Project the shared registry names every source by
absolute path, so FuseSoC exports nothing and each include directory is the
user's real folder. Simulation preparation now resolves through the build's
private source mirror. These tests use real FuseSoC/Edalize and need no
simulator: they pin the prepared include directories, the names reports derive
from the mirror, the dry-run preview and the unchanged non-isolated behavior.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
from booley.flows.sim.build_session import SimulationBuildSession, snapshot_build_inputs
from booley.flows.sim.coverage_overlay import write_coverage_overlay
from booley.flows.sim.verilator_coverage import VERILATOR_COVERAGE_INSTRUMENTATION
from booley.flows.sim.verilator_coverage_execution import prepare_coverage_collection
from booley.flows.sim.workload import capture_workload_inputs
from booley.fusesoc.fusesoc_registry import (
    ResolvedFile,
    setup_command_for_handle,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.flows.sim.test_verilator_build_reuse_engine import _edit_keeping_mtime
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
    icarus_options: dict[str, list[str]] | None = None,
    extra_files: tuple[str, ...] = (),
) -> str:
    """The fixture core; every header sits in ``rtl/inc``, one with an ``include_path``."""
    files: list = [
        {"rtl/top.v": {"file_type": "verilogSource"}},
        *({name: {"file_type": "verilogSource"}} for name in extra_files),
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
            "flow_options": {"tool": "icarus", **(icarus_options or {})},
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
    _write_project_core(root, headers=False)
    assert not list(_prepare(root, selector).work_root.rglob("listed.vh"))


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
    assert _holds(folders, "listed.vh")
    assert not _holds(folders, "unlisted.vh")
    assert all(folder.is_relative_to(prepared.build_root.resolve()) for folder in folders)
    dump = [item for item in prepared.resolved.files if item.name.endswith("booley_vcd_dump.sv")]
    assert dump
    assert all(Path(item.name).is_absolute() for item in dump)


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


def _write_project_core(
    root: Path,
    core_path: Path | None = None,
    *,
    absolute_include: Path | None = None,
    user_files: bool = True,
    headers: bool = True,
    icarus_options: dict[str, list[str]] | None = None,
    extra_files: tuple[str, ...] = (),
) -> None:
    """Rewrite the Project core, by default the isolated one under ``.booley/cores``."""
    path = core_path or root / ".booley" / "cores" / "design.core"
    path.write_text(
        _core(
            absolute_include=absolute_include,
            user_files=user_files,
            headers=headers,
            icarus_options=icarus_options,
            extra_files=extra_files,
        ),
        encoding="utf-8",
    )


# The Icarus recipe check reads the Edalize command file with POSIX shell quoting, which
# eats the backslashes of Windows file names, so Icarus reuse never keys there.
icarus_reuse_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="Icarus build reuse declines Windows-spelled file names"
)


def _icarus_key(prepared: PreparedSimulationBuild, root: Path) -> str | None:
    """The Icarus reuse key of one prepared build, with the host tool identity pinned."""
    return build_session._reuse_input_key(prepared, snapshot_build_inputs(prepared), "", root, {})


@icarus_reuse_posix_only
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


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_icarus_listed_header_edit_changes_or_declines_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a listed header never leaves an isolated Icarus build with the same reuse key.

    The key may decline (``None``) because recipes that use includes are not
    keyed today; the invariant holds if a later change keys them safely.
    """
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj")
    first = _icarus_key(_prepare(root, "sim_icarus", name="g1"), root)
    # Keep the old mtime so only the header bytes can move the key.
    _edit_keeping_mtime(root / "rtl" / "inc" / "listed.vh", "// listed v2\n")
    second = _icarus_key(_prepare(root, "sim_icarus", name="g2"), root)
    assert first is None or first != second


@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_mirrored_names_stay_project_relative(tmp_path: Path) -> None:
    """AC3: workload snapshot rows never show the mirror."""
    root = _write_project(tmp_path / "proj")
    prepared = _prepare(root, "sim_verilator")
    rows = {row["path"] for row in capture_workload_inputs(root, prepared.resolved)}
    assert {"rtl/top.v", "rtl/inc/listed.vh", "data/plain.hex"} <= rows
    assert not [path for path in rows if "core-mirror" in path or path.startswith("src/")]


@pytest.mark.parametrize(
    ("name", "core", "expected"),
    [
        ("src/acme_lib_design_1/rtl/top.v", "acme:lib:design:1", "rtl/top.v"),
        ("/abs/rtl/top.v", "acme:lib:design:1", None),
        ("src/acme_lib_other_1/rtl/top.v", "acme:lib:design:1", None),
    ],
    ids=["mirrored", "absolute", "other-core"],
)
def test_mirrored_source_path_names_only_mirrored_files(
    tmp_path: Path, name: str, core: str | None, expected: str | None
) -> None:
    """Only a file under its own core's export folder maps back to a Project path."""
    # Imported here so the module still collects on the base the red run uses.
    from booley.fusesoc.fusesoc_registry import mirrored_source_path

    root = _write_project(tmp_path / "proj")
    assert mirrored_source_path(root, ResolvedFile(name, "verilogSource", core=core)) == expected


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
def test_isolated_sim_dry_run_rejects_entry_that_is_not_a_regular_file(tmp_path: Path) -> None:
    """A fileset entry naming a directory fails the preview, as it fails the run."""
    root = _write_project(tmp_path / "proj")
    (root / "rtl" / "dir.v").mkdir()
    (root / ".booley" / "cores" / "bad.core").write_text(
        "CAPI=2:\nname: acme:lib:bad:1\nfilesets:\n  rtl:\n    files: [rtl/dir.v]\n",
        encoding="utf-8",
    )
    handle = _handle(root, "sim_icarus")
    with pytest.raises(SimulationBuildPreparationError, match="not a regular file"):
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
    for header in headers:
        # FuseSoC joins export names with the host separator; Verilator records them with "/".
        name = header.replace("\\", "/")
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


def _stub_icarus_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the host tool identities so the Icarus key is non-None without ``iverilog``."""
    monkeypatch.setattr(build_session, "_icarus_tool_identity", lambda: "iverilog-test")
    monkeypatch.setattr(build_session, "_python_recipe_identity", lambda: "recipe-test")


def _unstock_options(lib: Path) -> dict[str, dict[str, list[str]]]:
    """Option sets that make ``iverilog`` or Make read files the reuse key never hashes."""
    return {
        "y-attached": {"iverilog_options": [f"-y{lib}"]},
        "extra-c": {"iverilog_options": ["-c", f"{lib / 'm.v'}"]},
        "shell-expansion": {"iverilog_options": [f"-DX$${{IFS}}-y$${{IFS}}{lib}"]},
        "newline-injection": {"iverilog_options": ["-g2012\nEDALIZE_LAUNCHER = sh -c"]},
        "vvp-newline-injection": {"vvp_options": ["-n\nEDALIZE_LAUNCHER = sh -c"]},
        "carriage-return": {"iverilog_options": [f"-DX=\rall: -y {lib}"]},
        "unicode-line-break": {"iverilog_options": [f"-g2012\u2028;iverilog -y{lib}"]},
        "duplicate-rule": {"iverilog_options": ["-g2012\npost_build: run"]},
    }


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("case", list(_unstock_options(Path("lib"))))
def test_isolated_icarus_reuse_declines_option_operands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """An unstock ``iverilog`` option or Makefile line keeps isolated Icarus reuse off."""
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj")
    lib = root.resolve() / "ext"
    lib.mkdir()
    (root / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    _write_project_core(root, user_files=False, headers=False)
    # Control: the stock recipe reuses, so a None key below comes from the option.
    assert _icarus_key(_prepare(root, "sim_icarus", name="control"), root) is not None
    _write_project_core(
        root, user_files=False, headers=False, icarus_options=_unstock_options(lib)[case]
    )
    prepared = _prepare(root, "sim_icarus", name=case)
    makefile = (prepared.build_root / "Makefile").read_bytes().decode("utf-8")
    if case == "shell-expansion":
        assert "$${IFS}" in makefile
    if case == "carriage-return":
        assert "\rall: -y" in makefile
    if case in {"newline-injection", "vvp-newline-injection"}:
        assert "\nEDALIZE_LAUNCHER = " in makefile
    if case == "duplicate-rule":
        assert "\npost_build: run\n" in makefile
    assert _icarus_key(prepared, root) is None


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize(
    "hidden", ["rtl/.a-hidden.v", "rtl/run.log", "rtl/.booley-runtime-inputs/m.v"]
)
def test_isolated_reuse_declines_source_named_like_a_booley_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden: str
) -> None:
    """A listed source the input snapshot skips by name escapes the hash, so reuse declines."""
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj")
    (root / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    _write_project_core(root, user_files=False, headers=False)
    session = SimpleNamespace(handle=SimpleNamespace(project_root=root), variant="")

    def session_key(prepared: PreparedSimulationBuild) -> str | None:
        inputs = snapshot_build_inputs(prepared)
        return SimulationBuildSession.reusable_key(session, prepared, inputs, hooks=False)  # type: ignore[arg-type]

    # Control: an ordinary name keys, so a None key below comes from the file name.
    assert session_key(_prepare(root, "sim_icarus", name="control")) is not None
    (root / hidden).parent.mkdir(exist_ok=True)
    (root / hidden).write_text("module m; endmodule\n", encoding="utf-8")
    _write_project_core(root, user_files=False, headers=False, extra_files=(hidden,))
    assert session_key(_prepare(root, "sim_icarus", name="hidden")) is None


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_icarus_reuse_declines_quoted_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed file name holding a quote declines reuse instead of crashing the keying."""
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj")
    (root / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    _write_project_core(root, user_files=False, headers=False)
    # Control: the plain recipe reuses, so a None key below comes from the file name.
    assert _icarus_key(_prepare(root, "sim_icarus", name="control"), root) is not None
    quoted = 'rtl/it"s.v'
    (root / quoted).write_text("module m; endmodule\n", encoding="utf-8")
    _write_project_core(root, user_files=False, headers=False, extra_files=(quoted,))
    prepared = _prepare(root, "sim_icarus", name="quoted")
    script = next(prepared.build_root.glob("*.scr")).read_text(encoding="utf-8")
    assert any(line.endswith(quoted) for line in script.splitlines())
    assert _icarus_key(prepared, root) is None


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
def test_isolated_icarus_reuse_keeps_stock_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attached ``-g``/``-D`` options are stock, so isolated reuse still keys the same."""
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj")
    options = {"iverilog_options": ["-g2012", "-DWIDTH=8"]}
    _write_project_core(root, user_files=False, headers=False, icarus_options=options)
    (root / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    first = _icarus_key(_prepare(root, "sim_icarus", name="g1"), root)
    assert first is not None
    assert _icarus_key(_prepare(root, "sim_icarus", name="g2"), root) == first


@icarus_reuse_posix_only
@pytest.mark.usefixtures("real_fusesoc")
@pytest.mark.parametrize("variant", ["plain", "projected"])
def test_unisolated_icarus_option_decision_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """AC4: without isolation an operand option still keys, exactly as before this change."""
    _stub_icarus_identity(monkeypatch)
    root = _write_project(tmp_path / "proj", variant)
    lib = root.resolve() / "ext"
    lib.mkdir()
    (root / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    _write_project_core(
        root,
        root / "design.core",
        user_files=False,
        headers=False,
        icarus_options={"iverilog_options": [f"-y{lib}"]},
    )
    assert _icarus_key(_prepare(root, "sim_icarus"), root) is not None
