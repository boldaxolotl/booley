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
from typing import cast

import pytest

from booley.core.file_lock import LockContentionError, acquire_file_lock
from booley.flows.sim import build_session
from booley.flows.sim.build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    project_compile_surface,
    simulation_build_slot,
)
from booley.targets.domain import TargetHandle


def test_slot_uses_durable_target_identity_not_selector_spelling(tmp_path: Path) -> None:
    (tmp_path / ".booley_project").mkdir()
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


def test_compile_surface_does_not_require_project_initialization(tmp_path: Path) -> None:
    source = tmp_path / "counter.sv"
    source.write_text("module counter; endmodule\n", encoding="utf-8")
    generated = tmp_path / ".booley_project" / ".runtime" / "generated.sv"
    generated.parent.mkdir(parents=True)
    generated.write_text("module generated; endmodule\n", encoding="utf-8")

    assert set(project_compile_surface(tmp_path)) == {"counter.sv"}


def _handle(root: Path) -> TargetHandle:
    (root / ".booley_project").mkdir(exist_ok=True)
    return cast(
        TargetHandle,
        SimpleNamespace(project_root=root, identity="acme:lib:demo:1#sim"),
    )


def test_generation_requires_lease_and_rejects_symlinked_generation_root(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    with pytest.raises(SimulationBuildSlotError, match="not leased"):
        session.new_generation()
    with pytest.raises(SimulationBuildSlotError, match="not leased"):
        session.capture_inputs(SimpleNamespace(work_root=tmp_path))
    with session:
        first = session.new_generation()
        assert first.is_dir()
        assert first.parent == session.slot / "g"
    (session.slot / "g").rename(session.slot / "old-generations")
    (session.slot / "g").symlink_to(session.slot / "old-generations", target_is_directory=True)
    with session, pytest.raises(SimulationBuildSlotError, match="symlink"):
        session.new_generation()


def test_generated_inputs_reject_symlinks_and_exclude_runtime_outputs(tmp_path: Path) -> None:
    root = tmp_path / "generation"
    root.mkdir()
    source = root / "source.sv"
    source.write_text("module demo; endmodule", encoding="utf-8")
    (root / "run.log").write_text("ignored", encoding="utf-8")
    (root / ".booley-adapter-result.json").write_text("ignored", encoding="utf-8")
    prepared = SimpleNamespace(work_root=root)
    assert build_session._build_input_hashes(prepared) == {
        "source.sv": hashlib.sha256(source.read_bytes()).hexdigest()
    }
    (root / "alias.sv").symlink_to(source)
    with pytest.raises(SimulationBuildSlotError, match="symlinked"):
        build_session._build_input_hashes(prepared)


@pytest.mark.parametrize(
    "pointer",
    [
        {"generation": "../escape", "build_root": "build"},
        {"generation": "0" * 16, "build_root": "../escape"},
        {"generation": "0" * 16, "build_root": "/tmp"},
    ],
)
def test_retained_pointer_rejects_path_escape(tmp_path: Path, pointer: dict[str, str]) -> None:
    slot = tmp_path / "slot"
    build = slot / "g" / ("0" * 16) / "build"
    build.mkdir(parents=True)
    (slot / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    assert build_session._retained_build_root(slot) is None


def test_retained_pointer_accepts_only_contained_directory(tmp_path: Path) -> None:
    slot = tmp_path / "slot"
    build = slot / "g" / ("0" * 16) / "build"
    build.mkdir(parents=True)
    pointer = {"generation": "0" * 16, "build_root": "build"}
    current = slot / "current.json"
    current.write_text(json.dumps(pointer), encoding="utf-8")
    assert build_session._retained_build_root(slot) == (build.parent, build)
    build.rmdir()
    build.symlink_to(tmp_path, target_is_directory=True)
    assert build_session._retained_build_root(slot) is None
    current.unlink()
    current.symlink_to(tmp_path / "outside")
    assert build_session._retained_build_root(slot) is None


def test_manifest_hashes_reject_missing_changed_and_escaping_files(tmp_path: Path) -> None:
    image = tmp_path / "image"
    image.mkdir()
    binary = image / "image.vvp"
    binary.write_bytes(b"first")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert build_session._matching_hashes(image, {"image.vvp": digest})
    assert not build_session._matching_hashes(image, ["image.vvp"])
    assert not build_session._matching_hashes(image, {1: digest})
    assert not build_session._matching_hashes(image, {"../outside": digest})
    assert not build_session._matching_hashes(image, {"/tmp/outside": digest})
    binary.write_bytes(b"changed")
    assert not build_session._matching_hashes(image, {"image.vvp": digest})
    binary.unlink()
    binary.symlink_to(tmp_path / "outside")
    assert not build_session._matching_hashes(image, {"image.vvp": digest})


def test_reuse_manifest_rejection_names_authentication_failure(tmp_path: Path) -> None:
    root = tmp_path / "generation"
    build = root / "build"
    build.mkdir(parents=True)
    source = root / "source.sv"
    source.write_bytes(b"source")
    image = build / "image.vvp"
    image.write_bytes(b"image")
    candidate = SimpleNamespace(target_identity="target")
    manifest = {
        "schema": 1,
        "target_identity": "target",
        "generation": root.name,
        "input_key": "key",
        "reusable": True,
        "inputs": {"source.sv": hashlib.sha256(source.read_bytes()).hexdigest()},
        "artifacts": {"image.vvp": hashlib.sha256(image.read_bytes()).hexdigest()},
    }
    reject = SimulationBuildSession._hit_rejection
    assert reject(root, build, manifest, candidate, "key") is None
    assert reject(root, build, [], candidate, "key") == "malformed manifest"
    assert reject(root, build, {**manifest, "schema": 2}, candidate, "key") == "schema mismatch"
    assert (
        reject(root, build, {**manifest, "target_identity": "other"}, candidate, "key")
        == "wrong Target"
    )
    assert reject(root, build, manifest, candidate, "other") == "changed source, recipe, or tool"
    source.write_bytes(b"tampered")
    assert reject(root, build, manifest, candidate, "key") == "changed source"
    source.write_bytes(b"source")
    image.write_bytes(b"tampered")
    assert reject(root, build, manifest, candidate, "key") == "changed artifact"


def test_tool_identity_closes_over_modules_and_shared_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "install"
    bin_dir = prefix / "bin"
    ivl = prefix / "lib" / "ivl"
    bin_dir.mkdir(parents=True)
    ivl.mkdir(parents=True)
    tools = {name: bin_dir / name for name in ("iverilog", "vvp", "make", "sh")}
    for path in tools.values():
        path.write_bytes(b"\x7fELFtool")
    module = ivl / "module.vpi"
    module.write_bytes(b"module")
    library = tmp_path / "libdependency.so"
    library.write_bytes(b"dependency")
    monkeypatch.setattr(build_session.shutil, "which", lambda name: str(tools[name]))
    monkeypatch.setattr(build_session, "_elf_dependencies", lambda _path: (library,))
    first = build_session._icarus_tool_identity()
    assert first is not None
    module.write_bytes(b"new module")
    assert build_session._icarus_tool_identity() != first
    library.unlink()
    assert build_session._icarus_tool_identity() is None
    monkeypatch.setattr(build_session.shutil, "which", lambda _name: None)
    assert build_session._icarus_tool_identity() is None


def test_elf_dependency_scan_declines_missing_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "compiler"
    binary.write_bytes(b"script")
    assert build_session._elf_dependencies(binary) is None
    binary.write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        build_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="lib => not found"),
    )
    assert build_session._elf_dependencies(binary) is None
    monkeypatch.setattr(
        build_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="lib => /tmp/lib.so"),
    )
    assert build_session._elf_dependencies(binary) == (Path("/tmp/lib.so"),)


@pytest.mark.parametrize(
    "core",
    [
        "invalid",
        {"generators": ["gen"]},
        {"targets": "invalid"},
        {"targets": {"sim": {"hooks": ["hook"]}}},
    ],
)
def test_generated_core_behavior_disables_reuse(core: object) -> None:
    assert build_session._has_generated_core_behavior(core)
    assert not build_session._has_generated_core_behavior({"targets": {"sim": {}}})


def test_source_closure_rejects_include_and_changed_staging(tmp_path: Path) -> None:
    core = tmp_path / "demo.core"
    core.write_text("", encoding="utf-8")
    original = tmp_path / "top.sv"
    original.write_text("module top; endmodule", encoding="utf-8")
    work = tmp_path / "work"
    source = work / "src" / "demo" / "top.sv"
    source.parent.mkdir(parents=True)
    source.write_bytes(original.read_bytes())
    item = SimpleNamespace(
        file_type="systemVerilogSource",
        name="src/demo/top.sv",
        absolute=lambda _root: source,
    )
    prepared = SimpleNamespace(
        resolved=SimpleNamespace(files=(item,)), build_root=work, work_root=work
    )
    assert not build_session._has_unsupported_source(prepared, core)
    source.write_text('`include "dependency.svh"', encoding="utf-8")
    with pytest.raises(SimulationBuildSlotError, match="changed during"):
        build_session._has_unsupported_source(prepared, core)
    original.write_bytes(source.read_bytes())
    assert build_session._has_unsupported_source(prepared, core)
    item.file_type = "user"
    assert build_session._has_unsupported_source(prepared, core)
    item.file_type = "systemVerilogSource"
    item.name = "outside.sv"
    assert build_session._has_unsupported_source(prepared, core)


def test_icarus_recipe_declines_external_and_extra_commands(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    source = work / "top.sv"
    source.write_text("module top; endmodule", encoding="utf-8")
    descriptor = work / "demo.scr"
    makefile = work / "Makefile"
    makefile.write_text(
        "all:\n\t$(EDALIZE_LAUNCHER) iverilog top.sv\n\t$(EDALIZE_LAUNCHER) vvp demo\n",
        encoding="utf-8",
    )
    prepared = SimpleNamespace(build_root=work, work_root=work)
    assert build_session._has_unsupported_icarus_recipe(prepared)
    descriptor.write_text("top.sv\n", encoding="utf-8")
    assert not build_session._has_unsupported_icarus_recipe(prepared)
    descriptor.write_text("-I/tmp\n", encoding="utf-8")
    assert build_session._has_unsupported_icarus_recipe(prepared)
    descriptor.write_text("../outside.sv\n", encoding="utf-8")
    assert build_session._has_unsupported_icarus_recipe(prepared)
    descriptor.write_text("\ntop.sv\n", encoding="utf-8")
    makefile.write_text("all:\n\tiverilog top.sv\n", encoding="utf-8")
    assert build_session._has_unsupported_icarus_recipe(prepared)


def test_fresh_image_rejects_missing_lease_manifest_and_changed_inputs(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    with session:
        work = session.new_generation()
        build = work / "build"
        build.mkdir()
        source = work / "source.sv"
        source.write_bytes(b"source")
        image = build / "demo.vvp"
        image.write_bytes(b"image")
        descriptor = build / "demo.scr"
        descriptor.write_bytes(b"descriptor")
        prepared = SimpleNamespace(
            work_root=work, build_root=build, eda_tool="icarus", target_identity="target"
        )
        manifest = build / ".booley-build-manifest.json"
        record = {
            "schema": 1,
            "generation": work.name,
            "target_identity": "target",
            "reusable": False,
            "inputs": {"source.sv": hashlib.sha256(source.read_bytes()).hexdigest()},
            "artifacts": {
                "demo.scr": hashlib.sha256(descriptor.read_bytes()).hexdigest(),
                "demo": hashlib.sha256(image.read_bytes()).hexdigest(),
            },
        }
        image.rename(build / "demo")
        manifest.write_text(json.dumps(record), encoding="utf-8")
        session.verify_fresh_image(prepared)
        source.write_bytes(b"changed")
        with pytest.raises(SimulationBuildSlotError, match="inputs changed"):
            session.verify_fresh_image(prepared)
        source.write_bytes(b"source")
        manifest.write_text(json.dumps({**record, "reusable": True}), encoding="utf-8")
        with pytest.raises(SimulationBuildSlotError, match="identity changed"):
            session.verify_fresh_image(prepared)
        manifest.write_text(json.dumps(record), encoding="utf-8")
        image = build / "demo"
        image.write_bytes(b"changed")
        with pytest.raises(SimulationBuildSlotError, match="image changed"):
            session.verify_fresh_image(prepared)
        image.write_bytes(b"image")
        extra = build / "other.vpi"
        extra.write_bytes(b"native module")
        with pytest.raises(SimulationBuildSlotError, match="image set changed"):
            session.verify_fresh_image(prepared)
        extra.unlink()
        manifest.unlink()
        with pytest.raises(SimulationBuildSlotError, match="cannot verify"):
            session.verify_fresh_image(prepared)
    with pytest.raises(SimulationBuildSlotError, match="not leased"):
        session.verify_fresh_image(prepared)


def test_reuse_closure_declines_dynamic_core_metadata_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "demo.core"
    core.write_text("CAPI=2:\nname: acme:demo:core:1\n", encoding="utf-8")
    edam = tmp_path / "demo.eda.yml"
    prepared = SimpleNamespace(
        eda_tool="icarus",
        target_identity="target",
        environment={},
        resolved=SimpleNamespace(cocotb_module="", files=(), edam_path=edam),
        build_root=tmp_path,
        work_root=tmp_path,
    )
    for data in (
        "invalid",
        {"hooks": ["dynamic"]},
        {"cores": {}},
        {"cores": {"one": {"dependencies": ["other"]}}},
        {"cores": {"one": {"core_file": "../other.core"}}},
    ):
        edam.write_text(json.dumps(data), encoding="utf-8")
        assert build_session._eligible_core_file(prepared, tmp_path) is None
    edam.write_text(json.dumps({"cores": {"one": {"core_file": "demo.core"}}}))
    assert build_session._eligible_core_file(prepared, tmp_path) is None
    assert build_session._reuse_input_key(prepared, {}, "variant", tmp_path) is None
    monkeypatch.setenv("MAKEFLAGS", "-j2")
    assert build_session._reuse_input_key(prepared, {}, "", tmp_path) is None
    monkeypatch.delenv("MAKEFLAGS")
    assert build_session._reuse_input_key(prepared, {}, "", tmp_path) is None


def test_lease_rejects_symlinked_lock_and_unsafe_cache_pointer(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    session.slot.mkdir(parents=True)
    lock = session.slot / "lease.lock"
    lock.symlink_to(tmp_path / "outside")
    with pytest.raises(SimulationBuildSlotError, match="symlink"):
        session.__enter__()
    lock.unlink()
    with session:
        candidate = SimpleNamespace(target_identity="target")
        assert session.try_reuse(candidate, "key") is None
        assert session.cache_decision == "missing provenance"
        (session.slot / "current.json").write_text(
            json.dumps({"generation": "outside", "build_root": "build"}), encoding="utf-8"
        )
        assert session.try_reuse(candidate, "key") is None
        assert session.cache_decision == "malformed provenance"
        with pytest.raises(SimulationBuildSlotError, match="unsafe candidate"):
            session.discard_candidate(tmp_path)


def test_authorization_rejects_unleased_escaped_and_changed_builds(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    outside = SimpleNamespace(work_root=tmp_path, build_root=tmp_path)
    with pytest.raises(SimulationBuildSlotError, match="not leased"):
        session.authorize_fresh_image(outside, {})
    with session:
        with pytest.raises(SimulationBuildSlotError, match="escaped leased slot"):
            session.authorize_fresh_image(outside, {})
        work = session.new_generation()
        build = work / "build"
        build.mkdir()
        source = work / "top.sv"
        source.write_bytes(b"changed")
        prepared = SimpleNamespace(work_root=work, build_root=build, eda_tool="verilator")
        with pytest.raises(SimulationBuildSlotError, match="input changed"):
            session.authorize_fresh_image(prepared, {"top.sv": "old digest"})
        with pytest.raises(SimulationBuildSlotError, match="recipe changed"):
            session.authorize_fresh_image(
                prepared, {"top.sv": hashlib.sha256(source.read_bytes()).hexdigest()}, key="old"
            )


def test_reuse_rejects_symlinked_manifest_and_changed_provenance(tmp_path: Path) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    with session:
        work = session.new_generation()
        build = work / "build"
        build.mkdir()
        (session.slot / "current.json").write_text(
            json.dumps({"generation": work.name, "build_root": "build"}), encoding="utf-8"
        )
        candidate = SimpleNamespace(target_identity="target")
        manifest = build / ".booley-build-manifest.json"
        manifest.symlink_to(tmp_path / "outside")
        assert session.try_reuse(candidate, "key") is None
        assert session.cache_decision == "malformed provenance"
        manifest.unlink()
        manifest.write_text(json.dumps({"schema": 2}), encoding="utf-8")
        assert session.try_reuse(candidate, "key") is None
        assert session.cache_decision == "schema mismatch"


def test_runtime_image_selection_rejects_missing_or_ambiguous_images(tmp_path: Path) -> None:
    prepared = SimpleNamespace(
        build_root=tmp_path,
        eda_tool="icarus",
        resolved=SimpleNamespace(cocotb_module=""),
        toplevel="top",
    )
    with pytest.raises(SimulationBuildSlotError, match="one Icarus image descriptor"):
        build_session._runtime_artifacts(prepared)
    descriptor = tmp_path / "top.scr"
    descriptor.write_text("top.sv", encoding="utf-8")
    (tmp_path / "top").write_bytes(b"image")
    assert build_session._runtime_artifacts(prepared) == (descriptor, tmp_path / "top")
    (tmp_path / "other.scr").write_text("other.sv", encoding="utf-8")
    with pytest.raises(SimulationBuildSlotError, match="one Icarus image descriptor"):
        build_session._runtime_artifacts(prepared)
    prepared.eda_tool = "verilator"
    with pytest.raises(SimulationBuildSlotError, match="one Verilator image"):
        build_session._runtime_artifacts(prepared)
    binary = tmp_path / "Vtop"
    binary.write_bytes(b"image")
    if os.name != "nt":
        with pytest.raises(SimulationBuildSlotError, match="not executable"):
            build_session._runtime_artifacts(prepared)


def test_compile_surface_tracks_symlinks_and_rejects_broken_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.sv"
    source.write_text("module source; endmodule", encoding="utf-8")
    link = tmp_path / "alias.sv"
    link.symlink_to(source.name)
    assert "alias.sv -> source.sv" in project_compile_surface(tmp_path)
    source.unlink()
    with pytest.raises(SimulationBuildSlotError, match="not a file"):
        project_compile_surface(tmp_path)


def test_fresh_verification_rejects_other_generation_and_symlinked_manifest(
    tmp_path: Path,
) -> None:
    session = SimulationBuildSession(_handle(tmp_path))
    with session:
        work = session.new_generation()
        build = work / "build"
        build.mkdir()
        prepared = SimpleNamespace(work_root=tmp_path, build_root=build)
        with pytest.raises(SimulationBuildSlotError, match="outside its leased generation"):
            session.verify_fresh_image(prepared)
        prepared.work_root = work
        manifest = build / ".booley-build-manifest.json"
        manifest.symlink_to(tmp_path / "outside")
        with pytest.raises(SimulationBuildSlotError, match="manifest is a symlink"):
            session.verify_fresh_image(prepared)


@pytest.mark.skipif(os.name == "nt", reason="POSIX lease inheritance")
def test_parent_death_does_not_release_live_child_build_lease(tmp_path: Path) -> None:
    (tmp_path / ".booley_project").mkdir()
    marker = tmp_path / "child-started"
    handle = cast(
        TargetHandle,
        SimpleNamespace(project_root=tmp_path, identity="acme:lib:demo:1#sim"),
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
        "with SimulationBuildSession(SimpleNamespace(project_root=root, identity='acme:lib:demo:1#sim')):\n"
        "    Probe()._execute_local(['sh', '-c', 'touch ' + str(marker) + '; sleep 2'], timeout=5)\n"
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
        deadline = time.monotonic() + 5
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        with (
            (simulation_build_slot(handle) / "lease.lock").open("a+") as lock,
            pytest.raises(LockContentionError),
        ):
            acquire_file_lock(lock)
        time.sleep(2.2)
        with SimulationBuildSession(handle):
            pass
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
