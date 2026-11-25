"""Filesystem lease behavior across Simulation subprocess ownership."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from booley.core.file_lock import LockContentionError, acquire_file_lock
from booley.flows.run_log import RUN_LOG_NAME
from booley.flows.sim import build_reuse, build_session
from booley.flows.sim.build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    TargetCompileSurface,
    project_compile_surface,
    resolve_target_compile_surface,
    simulation_build_slot,
    snapshot_build_inputs,
    snapshot_generation_files,
)
from booley.fusesoc import selftest_overlay
from booley.fusesoc.core_projection import isolated_core_path
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.flows.sim.test_build_reuse import _core_behind_linked_booley_dir, _handle
from tests.flows.sim.test_coverage_invocation import project
from tests.time_budget import hang_guard_s, scaled


def test_resolved_compile_surface_is_target_scoped_and_snapshot_is_walk_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path)
    header = tmp_path / "rtl" / "counter.svh"
    header.write_text("`define COUNTER_WIDTH 8\n", encoding="utf-8")
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text(encoding="utf-8")
        .replace(
            "files: [rtl/counter.sv]",
            "files: [rtl/counter.sv, {rtl/counter.svh: {is_include_file: true}}]",
        )
        .replace("  rtl:\n", "  rtl:\n    depend: [acme:demo:dependency:1]\n"),
        encoding="utf-8",
    )
    dependency = tmp_path / "dependency.core"
    dependency.write_text("CAPI=2:\nname: acme:demo:dependency:1\n", encoding="utf-8")
    unselected = tmp_path / "rtl" / "unselected.sv"
    unselected.write_text("module unselected; endmodule\n", encoding="utf-8")
    ticket_source = tmp_path / ".booley/worktrees/T-1/rtl/copy.sv"
    ticket_source.parent.mkdir(parents=True)
    ticket_source.write_text("module copy; endmodule\n", encoding="utf-8")
    handle = TargetCatalog.build(tmp_path).select("sim_0", for_flow="sim")

    surface = resolve_target_compile_surface(handle)
    identities = {path.relative_to(tmp_path).as_posix() for path in surface.authored_paths}

    assert {"rtl/counter.sv", "rtl/counter.svh", "counter.core", "dependency.core"} <= identities
    assert "rtl/unselected.sv" not in identities
    assert ".booley/worktrees/T-1/rtl/copy.sv" not in identities
    before = project_compile_surface(surface)
    monkeypatch.setattr(os, "walk", lambda *_args, **_kwargs: pytest.fail("snapshot walked"))
    unselected.write_text("module changed; endmodule\n", encoding="utf-8")
    ticket_source.write_text("module changed_copy; endmodule\n", encoding="utf-8")
    assert project_compile_surface(surface) == before
    config = tmp_path / ".booley/booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[stealth]\nenabled = true\n", encoding="utf-8")
    assert project_compile_surface(surface) != before


def test_compile_surface_resolution_normalizes_a_stale_target(tmp_path: Path) -> None:
    project(tmp_path)
    catalog = TargetCatalog.build(tmp_path)
    handle = catalog.select("sim_0", for_flow="sim")
    core = tmp_path / "counter.core"
    core.write_text(core.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(SimulationBuildSlotError, match=r"cannot resolve compile inputs.*Target"):
        resolve_target_compile_surface(handle)


def test_slot_uses_durable_target_identity_not_selector_spelling(tmp_path: Path) -> None:
    (tmp_path / ".booley").mkdir()
    first = cast(
        TargetHandle,
        SimpleNamespace(project_root=tmp_path, identity="vendor:lib:one:1#sim", selector="a-b"),
    )
    alias = cast(
        TargetHandle,
        SimpleNamespace(project_root=tmp_path, identity=first.identity, selector="a_b"),
    )
    second = cast(
        TargetHandle,
        SimpleNamespace(project_root=tmp_path, identity="vendor:lib:two:1#sim", selector="a_b"),
    )
    assert simulation_build_slot(first) == simulation_build_slot(alias)
    assert simulation_build_slot(first) != simulation_build_slot(second)
    assert len(simulation_build_slot(first).name) <= 20


def test_compile_surface_includes_only_resolved_operational_core_projections(
    tmp_path: Path,
) -> None:
    authored = tmp_path / ".booley" / "cores" / "design.core"
    authored.parent.mkdir(parents=True)
    authored.write_text("CAPI=2:\nname: ::design:0\n", encoding="utf-8")
    overlay = authored.with_name("design.booleytrace.core")
    generated = isolated_core_path(tmp_path, overlay)
    generated.parent.mkdir(parents=True)
    generated.write_text(
        "CAPI=2:\n"
        "# Booley stealth core projection: .booley/cores/design.booleytrace.core\n"
        "name: ::design-booleytrace:0\n",
        encoding="utf-8",
    )
    foreign = generated.parent / "foreign.core"
    foreign.write_text("CAPI=2:\nname: ::foreign:0\n", encoding="utf-8")
    linked = generated.parent / "booley-isolated-linked.core"
    linked.symlink_to(generated.name)

    compile_surface = TargetCompileSurface(tmp_path, (authored, foreign, linked), (generated,))
    surface = project_compile_surface(compile_surface)
    assert authored.relative_to(tmp_path).as_posix() in surface
    assert generated.relative_to(tmp_path).as_posix() not in surface
    assert foreign.relative_to(tmp_path).as_posix() in surface
    assert any(name.startswith(linked.relative_to(tmp_path).as_posix()) for name in surface)

    linked.unlink()
    compile_surface = TargetCompileSurface(tmp_path, (authored, foreign), (generated,))
    surface = project_compile_surface(compile_surface)
    prepared_surface = project_compile_surface(compile_surface, include_operational_cores=True)
    assert generated.relative_to(tmp_path).as_posix() in prepared_surface
    generated.write_text(generated.read_text(encoding="utf-8") + "# mutated\n", encoding="utf-8")
    assert project_compile_surface(compile_surface) == surface
    assert (
        project_compile_surface(compile_surface, include_operational_cores=True)
        != prepared_surface
    )


def test_compile_surface_hashes_foreign_isolated_cores_without_decoding(tmp_path: Path) -> None:
    registry = isolated_core_path(tmp_path, tmp_path / ".booley/cores/design.core").parent
    registry.mkdir(parents=True)
    invalid_utf8 = registry / "booley-isolated-invalid.core"
    invalid_utf8.write_bytes(b"\xff\n")
    invalid_source = registry / "booley-isolated-foreign.core"
    invalid_source.write_text(
        "CAPI=2:\n# Booley stealth core projection: outside.core\n", encoding="utf-8"
    )

    compile_surface = TargetCompileSurface(tmp_path, (invalid_utf8, invalid_source), ())
    surface = project_compile_surface(compile_surface)
    assert invalid_utf8.relative_to(tmp_path).as_posix() in surface
    assert invalid_source.relative_to(tmp_path).as_posix() in surface


def test_generation_requires_lease(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path), wait_s=60)
    with pytest.raises(SimulationBuildSlotError, match="not leased"):
        session.new_generation()
    with session:
        first = session.new_generation()
        assert first.is_dir()
        assert first.parent == session.slot / "g"


def test_generated_inputs_reject_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "generation"
    root.mkdir()
    source = root / "source.sv"
    source.write_text("module demo; endmodule", encoding="utf-8")
    prepared = SimpleNamespace(work_root=root)
    (root / "alias.sv").symlink_to(source)
    with pytest.raises(SimulationBuildSlotError, match="symlinked"):
        build_session.snapshot_build_inputs(prepared)


def test_elf_dependency_scan_declines_missing_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "compiler"
    binary.write_bytes(b"script")
    assert build_reuse.elf_dependencies(binary) is None
    binary.write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        build_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="lib => not found"),
    )
    assert build_reuse.elf_dependencies(binary) is None
    monkeypatch.setattr(
        build_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="lib => /tmp/lib.so"),
    )
    assert build_reuse.elf_dependencies(binary) == (Path("/tmp/lib.so"),)


def test_discard_candidate_removes_only_its_generation(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path), wait_s=60)
    with session:
        discarded = session.new_generation()
        retained = session.new_generation()
        abandoned = discarded.parent / f".booley-doctor-run-cwd-{discarded.name}"
        abandoned.mkdir()

        session.discard_candidate(discarded)
        session.discard_candidate(discarded)

        assert not discarded.exists()
        assert abandoned.is_dir()
        assert retained.is_dir()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits that root ignores",
)
def test_lease_exit_prunes_unpromoted_generations_and_survives_a_failed_delete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handle = _handle(tmp_path)
    with SimulationBuildSession(handle, wait_s=60):
        pass  # a lease that never allocated a generation has no g/ to prune
    assert caplog.text == ""
    session = SimulationBuildSession(handle, wait_s=60)
    with session:
        pruned = session.new_generation()
        stuck = session.new_generation()
        view = pruned.parent / f".booley-doctor-run-cwd-{pruned.name}"
        view.mkdir()
        (stuck / "locked").mkdir()
        (stuck / "locked" / "file").write_text("x", encoding="utf-8")
        (stuck / "locked").chmod(0o500)
    try:
        assert not pruned.exists()
        assert view.is_dir()
        assert stuck.is_dir()
        assert str(stuck) in caplog.text

        generations = session.slot / "g"
        caplog.clear()
        generations.chmod(0o000)
        try:
            with SimulationBuildSession(handle, wait_s=60):
                pass
        finally:
            generations.chmod(0o700)
        assert str(generations) in caplog.text
    finally:
        (stuck / "locked").chmod(0o700)

    with SimulationBuildSession(handle, wait_s=60):
        pass
    assert not stuck.exists()
    assert view.is_dir()


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_lease_exit_never_prunes_through_a_linked_generations_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handle = _handle(tmp_path)
    outside = tmp_path / "elsewhere" / "0123456789abcdef"
    outside.mkdir(parents=True)
    session = SimulationBuildSession(handle, wait_s=60)
    with session:
        (session.slot / "g").symlink_to(outside.parent, target_is_directory=True)
    assert outside.is_dir()
    assert "not pruning" in caplog.text


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
def test_projected_core_shadow_is_not_a_generated_build_input(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path), wait_s=60)
    overlay = selftest_overlay.bad_overlay_dir(tmp_path / ".booley", "sim") / "fixture.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("bad\n", encoding="utf-8")
    (tmp_path / ".booley-projected-demo.core").write_text("core\n", encoding="utf-8")
    with session:
        generation = session.new_generation()
        build_input = generation / "input.sv"
        build_input.write_text("module demo; endmodule\n", encoding="utf-8")
        shadow = selftest_overlay.doctor_runtime_view_path(tmp_path, generation.parent, "a" * 32)
        selftest_overlay.stage_bad_run_overlay(tmp_path / ".booley", "sim", tmp_path, shadow)

        assert (shadow / ".booley-projected-demo.core").is_symlink()
        assert session.capture_inputs(SimpleNamespace(work_root=generation)) == {
            "input.sv": hashlib.sha256(build_input.read_bytes()).hexdigest()
        }


def test_runtime_image_selection_rejects_missing_or_ambiguous_images(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path), wait_s=60)
    prepared = SimpleNamespace(
        build_root=tmp_path,
        eda_tool="icarus",
        resolved=SimpleNamespace(cocotb_module=""),
        toplevel="top",
    )
    with pytest.raises(SimulationBuildSlotError, match="one Icarus image descriptor"):
        session.retained_artifacts(prepared)
    descriptor = tmp_path / "top.scr"
    descriptor.write_text("top.sv", encoding="utf-8")
    (tmp_path / "top").write_bytes(b"image")
    assert session.retained_artifacts(prepared) == (descriptor, tmp_path / "top")
    (tmp_path / "other.scr").write_text("other.sv", encoding="utf-8")
    with pytest.raises(SimulationBuildSlotError, match="one Icarus image descriptor"):
        session.retained_artifacts(prepared)
    prepared.eda_tool = "verilator"
    with pytest.raises(SimulationBuildSlotError, match="one Verilator image"):
        session.retained_artifacts(prepared)
    binary = tmp_path / "Vtop"
    binary.write_bytes(b"image")
    if os.name != "nt":
        with pytest.raises(SimulationBuildSlotError, match="not executable"):
            session.retained_artifacts(prepared)


def test_compile_surface_tracks_symlinks_and_rejects_broken_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.sv"
    source.write_text("module source; endmodule", encoding="utf-8")
    link = tmp_path / "alias.sv"
    link.symlink_to(source.name)
    surface = TargetCompileSurface(tmp_path, (link,), ())
    assert "alias.sv -> source.sv" in project_compile_surface(surface)
    source.unlink()
    with pytest.raises(SimulationBuildSlotError, match="not a file"):
        project_compile_surface(surface)


@pytest.mark.skipif(os.name == "nt", reason="POSIX lease inheritance")
def test_parent_death_does_not_release_live_child_build_lease(tmp_path: Path) -> None:
    (tmp_path / ".booley").mkdir()
    marker = tmp_path / "child-started"
    handle = cast(
        TargetHandle, SimpleNamespace(project_root=tmp_path, identity="acme:lib:demo:1#sim")
    )
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from booley.flows.base import FlowMechanics\n"
        "from booley.flows.sim.build_session import SimulationBuildSession\n"
        "root, marker = map(Path, sys.argv[1:])\n"
        "class Probe(FlowMechanics):\n"
        "    def _get_cwd(self): return root\n"
        "with SimulationBuildSession(SimpleNamespace(project_root=root, identity='acme:lib:demo:1#sim'), wait_s=60):\n"
        f"    Probe()._execute_local(['sh', '-c', 'touch ' + str(marker) + '; sleep {scaled(2)}'],"
        f" timeout={hang_guard_s()})\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(marker)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + hang_guard_s()
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=hang_guard_s())
        with (
            (simulation_build_slot(handle) / "lease.lock").open("a+") as lock,
            pytest.raises(LockContentionError),
        ):
            acquire_file_lock(lock)
        with SimulationBuildSession(handle, wait_s=60):
            pass
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=hang_guard_s())


def test_generation_snapshot_keeps_reserved_names_the_input_snapshot_ignores(
    tmp_path: Path,
) -> None:
    work = tmp_path / "g"
    build = work / "build"
    build.mkdir(parents=True)
    for name in ("top.sv", ".booley-adapter-x.json", ".a-1.json", RUN_LOG_NAME):
        (build / name).write_text(name, encoding="utf-8")
    (work / ".booley-runtime-inputs").mkdir()
    (work / ".booley-runtime-inputs" / "fw.hex").write_text("fw", encoding="utf-8")
    prepared = SimpleNamespace(work_root=work, build_root=build)

    assert set(snapshot_build_inputs(prepared)) == {"build/top.sv"}
    # Only Booley's live run log is excluded from the Pre-Sim write detector,
    # which also records every directory.
    assert set(snapshot_generation_files(prepared)) == {
        "build/",
        "build/top.sv",
        "build/.booley-adapter-x.json",
        "build/.a-1.json",
        ".booley-runtime-inputs/",
        ".booley-runtime-inputs/fw.hex",
    }


_ICARUS_MAKEFILE = "all:\n\t$(EDALIZE_LAUNCHER) iverilog top.sv\n\t$(EDALIZE_LAUNCHER) vvp demo\n"
_ICARUS_CORE = "CAPI=2:\nname: a:b:demo:1\n"
_STOCK_SOURCE = ("systemVerilogSource", "src/demo/top.sv")
_STOCK_SCRIPT = "\nsrc/demo/top.sv\n"  # a blank line is tolerated


@pytest.fixture
def icarus_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Icarus prefix on ``PATH`` with no recipe-rewriting Make variables (POSIX only)."""
    if os.name == "nt":
        pytest.skip("Icarus build reuse keys only on POSIX (icarus_reuse_posix_only)")
    prefix = tmp_path / "install"
    (prefix / "lib" / "ivl").mkdir(parents=True)
    (prefix / "bin").mkdir()
    for name in ("iverilog", "vvp", "make", "sh"):
        (prefix / "bin" / name).write_text(f"#!/bin/sh\n# {name}\n", encoding="utf-8")
        (prefix / "bin" / name).chmod(0o755)
    (prefix / "lib" / "ivl" / "module.vpi").write_bytes(b"module")
    monkeypatch.setenv("PATH", str(prefix / "bin"))
    for name in ("EDALIZE_LAUNCHER", "MAKEFLAGS", "MFLAGS"):
        monkeypatch.delenv(name, raising=False)
    return prefix


def _write_icarus_recipe(
    work: Path, scripts: tuple[str, ...] = (_STOCK_SCRIPT,), makefile: str = _ICARUS_MAKEFILE
) -> None:
    for index, text in enumerate(scripts):
        (work / f"demo{index}.scr").write_text(text, encoding="utf-8")
    (work / "Makefile").write_text(makefile, encoding="utf-8")


def _icarus_generation(
    tmp_path: Path,
    *,
    core_text: str = _ICARUS_CORE,
    edam: object = None,
    core_outside: bool = False,
    files: tuple[tuple[str, str], ...] = (_STOCK_SOURCE,),
    staged_text: str = "module top; endmodule",
    source_text: str | None = None,
    scripts: tuple[str, ...] = (_STOCK_SCRIPT,),
    makefile: str = _ICARUS_MAKEFILE,
    cocotb_module: str = "",
    variant: str = "",
) -> tuple[SimulationBuildSession, SimpleNamespace]:
    """A session and the prepared build of one stock Icarus generation, tweaked by overrides."""
    project = tmp_path / "project"
    work = project / "work"
    (work / "src" / "demo").mkdir(parents=True)
    core = (tmp_path / "outside.core") if core_outside else project / "demo.core"
    core.write_text(core_text, encoding="utf-8")
    (project / "top.sv").write_text(
        staged_text if source_text is None else source_text, encoding="utf-8"
    )
    staged = work / "src" / "demo" / "top.sv"
    staged.write_text(staged_text, encoding="utf-8")
    document = (
        {"cores": {"one": {"core_file": os.path.relpath(core, work)}}} if edam is None else edam
    )
    edam_path = work / "demo.eda.yml"
    edam_path.write_text(json.dumps(document), encoding="utf-8")
    _write_icarus_recipe(work, scripts, makefile)
    items = tuple(
        SimpleNamespace(file_type=file_type, name=name, absolute=lambda _root: staged.resolve())
        for file_type, name in files
    )
    prepared = SimpleNamespace(
        eda_tool="icarus",
        target_identity="target",
        environment={},
        resolved=SimpleNamespace(cocotb_module=cocotb_module, files=items, edam_path=edam_path),
        build_root=work,
        work_root=work,
    )
    return SimulationBuildSession(_handle(project), variant, wait_s=60), prepared


def _key(session: SimulationBuildSession, prepared: SimpleNamespace) -> str | None:
    return session.reusable_key(
        cast(Any, prepared), snapshot_build_inputs(cast(Any, prepared)), hooks=False
    )


@pytest.mark.usefixtures("icarus_install")
@pytest.mark.parametrize("case", ["plain", "project_core", "stealth_core"])
def test_stock_icarus_generation_keys(tmp_path: Path, case: str) -> None:
    """A stock generation keys, also when its core sits behind a linked Booley Directory (#2191)."""
    if case == "plain":
        session, prepared = _icarus_generation(tmp_path)
    else:
        project, edam, _core = _core_behind_linked_booley_dir(tmp_path, case)
        (edam.parent / "top.sv").write_text("module top; endmodule", encoding="utf-8")
        _write_icarus_recipe(edam.parent, ("top.sv\n",))
        session = SimulationBuildSession(_handle(project), wait_s=60)
        prepared = SimpleNamespace(
            eda_tool="icarus",
            target_identity="target",
            environment={},
            resolved=SimpleNamespace(cocotb_module="", files=(), edam_path=edam),
            build_root=edam.parent,
            work_root=edam.parent,
        )

    key = _key(session, prepared)

    assert key is not None
    assert _key(session, prepared) == key


_DECLINED_GENERATIONS: dict[str, dict[str, Any]] = {
    "include": {"source_text": "x", "staged_text": '`include "dependency.svh"'},
    "non-hdl-type": {"files": (("vhdlSource", "src/demo/top.sv"),)},
    "user-file": {"files": (("user", "src/demo/top.sv"),)},
    "path-outside-src": {"files": (("systemVerilogSource", "outside.sv"),)},
    "no-script": {"scripts": ()},
    "two-scripts": {"scripts": (_STOCK_SCRIPT, _STOCK_SCRIPT)},
    "include-dir-line": {"scripts": ("-I/tmp\n",)},
    "script-escapes-work": {"scripts": ("../outside.sv\n",)},
    "makefile-without-launcher": {"makefile": "all:\n\tiverilog top.sv\n"},
    "edam-invalid": {"edam": "invalid"},
    "edam-hooks": {
        "edam": {"hooks": ["dynamic"], "cores": {"one": {"core_file": "../demo.core"}}}
    },
    "edam-no-cores": {"edam": {"cores": {}}},
    "edam-dependencies": {
        "edam": {"cores": {"one": {"dependencies": ["other"], "core_file": "../demo.core"}}}
    },
    "core-outside-project": {"core_outside": True},
    "core-invalid": {"core_text": "invalid\n"},
    "core-generators": {"core_text": _ICARUS_CORE + "generators: {gen: {}}\n"},
    "core-targets-invalid": {"core_text": _ICARUS_CORE + "targets: invalid\n"},
    "core-target-hooks": {"core_text": _ICARUS_CORE + "targets: {sim: {hooks: [hook]}}\n"},
    "cocotb": {"cocotb_module": "tb"},
    "variant": {"variant": "trace"},
    "make-variable": {"environment": {"MAKEFLAGS": "-j2"}},
}


@pytest.mark.usefixtures("icarus_install")
@pytest.mark.parametrize("overrides", _DECLINED_GENERATIONS.values(), ids=_DECLINED_GENERATIONS)
def test_icarus_reuse_declines_unprovable_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    assert _key(*_icarus_generation(tmp_path / "control")) is not None
    # An include must also sit in the Project original, or the staging check fires first.
    if "staged_text" in overrides:
        overrides = {**overrides, "source_text": overrides["staged_text"]}
    for name, value in overrides.get("environment", {}).items():
        monkeypatch.setenv(name, value)
    overrides = {k: v for k, v in overrides.items() if k != "environment"}

    assert _key(*_icarus_generation(tmp_path / "declined", **overrides)) is None


@pytest.mark.usefixtures("icarus_install")
def test_icarus_reuse_refuses_staging_that_changed(tmp_path: Path) -> None:
    session, prepared = _icarus_generation(tmp_path, source_text="module top; endmodule")
    (tmp_path / "project" / "top.sv").write_text("module edited; endmodule", encoding="utf-8")

    with pytest.raises(SimulationBuildSlotError, match="changed during"):
        _key(session, prepared)


def test_icarus_key_follows_the_tool_install(
    tmp_path: Path, icarus_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "libdependency.so"
    library.write_bytes(b"dependency")
    iverilog = icarus_install / "bin" / "iverilog"
    iverilog.write_bytes(b"\x7fELFtool")
    monkeypatch.setattr(
        build_reuse, "elf_dependencies", lambda path: (library,) if path == iverilog else ()
    )
    session, prepared = _icarus_generation(tmp_path)
    first = _key(session, prepared)
    assert first is not None

    (icarus_install / "lib" / "ivl" / "module.vpi").write_bytes(b"new module")
    assert _key(session, prepared) not in (None, first)

    library.unlink()
    assert _key(session, prepared) is None
    library.write_bytes(b"dependency")
    (icarus_install / "bin" / "vvp").unlink()
    assert _key(session, prepared) is None
