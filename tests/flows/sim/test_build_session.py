"""Filesystem lease behavior across Simulation subprocess ownership."""

from __future__ import annotations

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
from booley.flows.sim.build_session import (
    SimulationBuildSession,
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
    assert len(simulation_build_slot(first).name) <= 42


def test_compile_surface_does_not_require_project_initialization(tmp_path: Path) -> None:
    source = tmp_path / "counter.sv"
    source.write_text("module counter; endmodule\n", encoding="utf-8")
    generated = tmp_path / ".booley_project" / ".runtime" / "generated.sv"
    generated.parent.mkdir(parents=True)
    generated.write_text("module generated; endmodule\n", encoding="utf-8")

    assert set(project_compile_surface(tmp_path)) == {"counter.sv"}


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
