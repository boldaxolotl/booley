"""Pytest plugin for deterministic CI sharding and timing evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ci_test_shards import assign_shards, load_timings, write_manifest

_DURATIONS: dict[str, float] = {}


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
    if report.when == "call":
        _DURATIONS[report.nodeid] = report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    if hasattr(session.config, "workerinput"):
        return
    output = session.config.getoption("--ci-timing-output")
    if output is None:
        return
    payload = {
        "schema": 1,
        "tests": dict(sorted(_DURATIONS.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
