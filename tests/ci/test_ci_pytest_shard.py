"""Timing evidence contracts for the CI pytest shard plugin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import ci_pytest_shard as shard_plugin


class _Config:
    def __init__(self, output: Path) -> None:
        self.output = output

    def getoption(self, name: str) -> Path | None:
        if name == "--ci-timing-output":
            return self.output
        return None


def test_timing_output_separates_collection_and_execution(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "timings.json"
    config = _Config(output)
    session = SimpleNamespace(config=config)
    clock = iter((10.0, 12.0, 13.0, 17.0, 18.0))
    monkeypatch.setattr(shard_plugin.time, "perf_counter", lambda: next(clock))

    shard_plugin.pytest_configure(config)
    shard_plugin.pytest_sessionstart(session)
    shard_plugin.pytest_collection_finish(session)
    shard_plugin.pytest_runtest_logstart("test_example.py::test_case", ("", 1, ""))
    shard_plugin.pytest_runtest_logreport(
        SimpleNamespace(when="call", nodeid="test_example.py::test_case", duration=1.25)
    )
    shard_plugin.pytest_runtest_logreport(
        SimpleNamespace(when="teardown", nodeid="test_example.py::test_case", duration=0.1)
    )
    shard_plugin.pytest_sessionfinish(session)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["phases"] == {
        "collection_seconds": 2.0,
        "controller_seconds": 8.0,
        "execution_seconds": 4.0,
    }
    assert payload["tests"] == {"test_example.py::test_case": 1.25}


def test_controller_keeps_slowest_worker_collection_time(tmp_path: Path) -> None:
    shard_plugin.pytest_configure(_Config(tmp_path / "unused.json"))
    first = SimpleNamespace(workeroutput={"ci_collection_seconds": 1.5})
    second = SimpleNamespace(workeroutput={"ci_collection_seconds": 2.25})

    shard_plugin.pytest_testnodedown(first, None)
    shard_plugin.pytest_testnodedown(second, None)

    assert max(shard_plugin._COLLECTION_SECONDS) == 2.25


def test_controller_tolerates_worker_crash_before_workeroutput(tmp_path: Path) -> None:
    shard_plugin.pytest_configure(_Config(tmp_path / "unused.json"))

    shard_plugin.pytest_testnodedown(SimpleNamespace(), RuntimeError("worker crashed"))

    assert shard_plugin._COLLECTION_SECONDS == []


def test_timing_output_records_each_reported_phase(tmp_path: Path) -> None:
    output = tmp_path / "timings.json"
    config = _Config(output)
    session = SimpleNamespace(config=config)
    nodeid = "test_example.py::test_case"

    shard_plugin.pytest_configure(config)
    for when, duration in (("setup", 0.5), ("call", 1.25), ("teardown", 0.1)):
        shard_plugin.pytest_runtest_logreport(
            SimpleNamespace(when=when, nodeid=nodeid, duration=duration)
        )
    shard_plugin.pytest_sessionfinish(session)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["test_phases"] == {nodeid: {"setup": 0.5, "call": 1.25, "teardown": 0.1}}
    # Shard balance stays call-only.
    assert payload["tests"] == {nodeid: 1.25}


_PHASE_MODULES = {
    "skipped": (
        "import pytest\n\n\n@pytest.mark.skip(reason='skipped on purpose')\n"
        "def test_case():\n    pass\n"
    ),
    "setup-error": (
        "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('boom')\n\n\n"
        "def test_case(broken):\n    pass\n"
    ),
}


@pytest.mark.parametrize("case", sorted(_PHASE_MODULES))
def test_timing_output_records_only_reported_phases(tmp_path: Path, case: str) -> None:
    """Real pytest reports no call phase here, so the map holds only what was reported."""
    module = tmp_path / "test_phases_case.py"
    module.write_text(_PHASE_MODULES[case], encoding="utf-8")
    config_file = tmp_path / "pytest.ini"
    config_file.write_text("[pytest]\n", encoding="utf-8")
    output = tmp_path / "timings.json"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "PYTEST_XDIST_"))
    }
    environment["PYTHONPATH"] = str(_ROOT / ".github/scripts")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            "-p",
            "ci_pytest_shard",
            "--ci-timing-output",
            str(output),
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "-c",
            str(config_file),
        ],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    (phases,) = payload["test_phases"].values()
    assert set(phases) == {"setup", "teardown"}
    assert all(isinstance(seconds, float) and seconds >= 0 for seconds in phases.values())
    assert payload["tests"] == {}
