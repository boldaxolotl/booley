"""Shared fixtures for sim unit tests.

Stand-ins: the simulator launch, its programs on the search path, and the deadline timer.
"""

from __future__ import annotations

import os
import stat
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.backends.supervision import SimulatorLaunch


class _StubTimer:
    """``threading.Timer`` stand-in that never fires, or fires on ``start()`` when *fire*."""

    fire = False

    def __init__(self, _interval, callback, *_args, **_kwargs):
        self.callback, self.daemon = callback, False

    def start(self):
        if self.fire:
            self.callback()

    def cancel(self):
        pass


@pytest.fixture
def never_firing_timer(monkeypatch):
    """Only a per-line deadline check can end a run."""
    monkeypatch.setattr(threading, "Timer", _StubTimer)


@pytest.fixture
def instant_timer(monkeypatch):
    """The wall-clock deadline has always already passed."""
    monkeypatch.setattr(threading, "Timer", type("_Instant", (_StubTimer,), {"fire": True}))


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in sim tests."""
    proj_dir = tmp_path / ".booley"
    proj_dir.mkdir()
    (proj_dir / "booley.toml").write_text('[flows.sim]\nsandbox = "sandboxed"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(proj_dir))


@dataclass
class FakeLaunch:
    """The adapters' ``run_supervised``: records each launch, replays canned output."""

    output: list[str] = field(default_factory=lambda: ["[SIM_RESULT] PASSED\n"])
    returncode: int = 0
    termination: RunTermination = field(default_factory=RunTermination)
    during: Callable[[SimulatorLaunch], None] = lambda launch: None
    launches: list[SimulatorLaunch] = field(default_factory=list)

    def __call__(self, launch: SimulatorLaunch):
        self.launches.append(launch)
        self.during(launch)
        for line in self.output:
            if launch.on_line is not None:
                launch.on_line(line)
        proc = type(
            "_Exited",
            (),
            {"returncode": self.returncode, "pid": 0, "poll": lambda s: s.returncode},
        )()
        return deque(self.output), proc, self.termination


@pytest.fixture
def fake_launch(monkeypatch) -> FakeLaunch:
    """Replace the public ``run_supervised`` every adapter calls with a ``FakeLaunch``."""
    fake = FakeLaunch()
    for adapter in ("cocotb", "icarus", "verilator"):
        monkeypatch.setattr(f"booley.flows.sim.backends.{adapter}.run_supervised", fake)
    return fake


@pytest.fixture
def tool_stub(tmp_path, monkeypatch) -> Callable[[str, str], Path]:
    """``install(name, body)`` puts an executable shell stub first on ``PATH`` (POSIX only)."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, body: str) -> Path:
        path = bin_dir / name
        path.write_bytes(f"#!/bin/sh\n{body}\n".encode())
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    return install


_COCOTB_CONFIG_ANSWERS = {
    "--libpython": "/usr/lib/libpython3.13.so",
    "--python-bin": "/usr/bin/python3",
    "--lib-name-path": "/site/cocotb/libs/libcocotbvpi_icarus.so",
    "--pygpi-entry-point": "/site/cocotb/simulator.so,initialize",
}


@pytest.fixture
def cocotb_config(tool_stub) -> Callable[..., Path]:
    """Install a ``cocotb-config`` stub; ``version=None`` makes ``--version`` exit 2."""

    def install(version: str | None = "2.1.0", libpath: str | None = None) -> Path:
        answers = dict(_COCOTB_CONFIG_ANSWERS)
        if libpath is not None:
            answers["--lib-name-path"] = libpath
        cases = "".join(f'  {flag}) echo "{text}";;\n' for flag, text in answers.items())
        # The Icarus library query must name its interface and simulator; any other fails.
        cases = cases.replace(
            "  --lib-name-path) ", '  --lib-name-path) [ "$2 $3" = "vpi icarus" ] || exit 2; '
        )
        version_case = (
            f'  --version) echo "{version}";;\n' if version else "  --version) exit 2;;\n"
        )
        return tool_stub("cocotb-config", f'case "$1" in\n{version_case}{cases}esac')

    return install
