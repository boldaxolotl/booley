"""Timing evidence contracts for the CI pytest shard plugin."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
