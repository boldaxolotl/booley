"""Pytest plugin for deterministic CI sharding and timing evidence."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest
from ci_test_shards import assign_shards, load_timings, write_manifest

_DURATIONS: dict[str, float] = {}
_COLLECTION_SECONDS: list[float] = []
_SESSION_STARTED_AT: float | None = None
_EXECUTION_STARTED_AT: float | None = None
_EXECUTION_FINISHED_AT: float | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("booley-ci-shard")
    group.addoption("--ci-shard-count", type=int)
    group.addoption("--ci-shard-index", type=int)
    group.addoption("--ci-shard-group")
    group.addoption("--ci-timing-model", type=Path)
    group.addoption("--ci-shard-manifest", type=Path)
    group.addoption("--ci-timing-output", type=Path)


def pytest_configure(config: pytest.Config) -> None:
    if not hasattr(config, "workerinput"):
        _DURATIONS.clear()
        _COLLECTION_SECONDS.clear()
        global _EXECUTION_FINISHED_AT, _EXECUTION_STARTED_AT
        _EXECUTION_STARTED_AT = None
        _EXECUTION_FINISHED_AT = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _SESSION_STARTED_AT
    _SESSION_STARTED_AT = time.perf_counter()


def pytest_collection_finish(session: pytest.Session) -> None:
    if _SESSION_STARTED_AT is None:
        return
    duration = time.perf_counter() - _SESSION_STARTED_AT
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["ci_collection_seconds"] = duration
    else:
        _COLLECTION_SECONDS.append(duration)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object | None) -> None:
    del error
    duration = node.workeroutput.get("ci_collection_seconds")
    if isinstance(duration, int | float) and math.isfinite(duration) and duration >= 0:
        _COLLECTION_SECONDS.append(float(duration))


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    del nodeid, location
    global _EXECUTION_STARTED_AT
    if _EXECUTION_STARTED_AT is None:
        _EXECUTION_STARTED_AT = time.perf_counter()


def _required_option(config: pytest.Config, name: str) -> Any:
    value = config.getoption(name)
    if value is None:
        raise pytest.UsageError(f"{name} is required when CI sharding is enabled")
    return value


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard_count = config.getoption("--ci-shard-count")
    if shard_count is None:
        return
    shard_index = _required_option(config, "--ci-shard-index")
    group = _required_option(config, "--ci-shard-group")
    manifest = _required_option(config, "--ci-shard-manifest")
    if not 0 <= shard_index < shard_count:
        raise pytest.UsageError("--ci-shard-index must be within the shard count")
    nodeids = [item.nodeid for item in items]
    shards = assign_shards(
        nodeids, shard_count, load_timings(config.getoption("--ci-timing-model"))
    )
    selected_ids = set(shards[shard_index])
    selected = [item for item in items if item.nodeid in selected_ids]
    deselected = [item for item in items if item.nodeid not in selected_ids]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
    worker = getattr(config, "workerinput", {}).get("workerid")
    if worker in {None, "gw0"}:
        write_manifest(
            manifest,
            group=group,
            shard_index=shard_index,
            shards=shards,
            all_nodeids=nodeids,
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _EXECUTION_FINISHED_AT
    if report.when == "call":
        _DURATIONS[report.nodeid] = report.duration
    if report.when == "teardown":
        _EXECUTION_FINISHED_AT = time.perf_counter()


def pytest_sessionfinish(session: pytest.Session) -> None:
    if hasattr(session.config, "workerinput"):
        return
    output = session.config.getoption("--ci-timing-output")
    if output is None:
        return
    finished_at = time.perf_counter()
    controller_seconds = (
        finished_at - _SESSION_STARTED_AT if _SESSION_STARTED_AT is not None else 0.0
    )
    execution_seconds = (
        _EXECUTION_FINISHED_AT - _EXECUTION_STARTED_AT
        if _EXECUTION_STARTED_AT is not None and _EXECUTION_FINISHED_AT is not None
        else 0.0
    )
    payload = {
        "schema": 1,
        "phases": {
            "collection_seconds": max(_COLLECTION_SECONDS, default=0.0),
            "controller_seconds": controller_seconds,
            "execution_seconds": execution_seconds,
        },
        "tests": dict(sorted(_DURATIONS.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
