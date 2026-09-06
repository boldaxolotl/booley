"""Contracts for deterministic CI test sharding."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_test_shards import (
    ShardError,
    TimingModel,
    assign_shards,
    load_timings,
    verify_manifests,
    write_manifest,
)


def test_longest_processing_time_balances_known_slow_tests() -> None:
    nodeids = ("test::slow", "test::medium", "test::fast-a", "test::fast-b")
    timings = TimingModel(
        default_seconds=1.0,
        tests={"test::slow": 9.0, "test::medium": 7.0},
    )

    shards = assign_shards(nodeids, 2, timings)

    assert shards == (("test::slow",), ("test::fast-a", "test::fast-b", "test::medium"))


def test_unknown_tests_are_included_deterministically() -> None:
    nodeids = ("test::c", "test::a", "test::b")

    first = assign_shards(nodeids, 2, TimingModel(default_seconds=2.0, tests={}))
    second = assign_shards(tuple(reversed(nodeids)), 2, TimingModel(2.0, {}))

    assert first == second
    assert {nodeid for shard in first for nodeid in shard} == set(nodeids)


def test_duplicate_collected_nodeids_fail_loudly() -> None:
    with pytest.raises(ShardError, match="must be unique"):
        assign_shards(("test::same", "test::same"), 2, TimingModel(1.0, {}))


def test_timing_model_rejects_non_positive_values(tmp_path: Path) -> None:
    path = tmp_path / "timings.json"
    path.write_text(
        json.dumps({"schema": 1, "default_seconds": 1, "tests": {"test::bad": 0}}),
        encoding="utf-8",
    )

    with pytest.raises(ShardError, match="positive number"):
        load_timings(path)


def _write_group(tmp_path: Path, shards: tuple[tuple[str, ...], ...]) -> list[Path]:
    all_nodeids = tuple(nodeid for shard in shards for nodeid in shard)
    paths = []
    for index in range(len(shards)):
        path = tmp_path / str(index) / "shard-manifest.json"
        write_manifest(
            path,
            group="windows",
            shard_index=index,
            shards=shards,
            all_nodeids=all_nodeids,
        )
        paths.append(path)
    return paths


def test_manifest_verifier_proves_exact_union(tmp_path: Path) -> None:
    paths = _write_group(tmp_path, (("test::a", "test::c"), ("test::b",)))

    assert verify_manifests(paths, group="windows", shard_count=2) == 3


def test_manifest_verifier_rejects_duplicate_selection(tmp_path: Path) -> None:
    paths = _write_group(tmp_path, (("test::a",), ("test::b",)))
    payload = json.loads(paths[1].read_text(encoding="utf-8"))
    payload["selected"] = ["test::a"]
    paths[1].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShardError, match="more than once"):
        verify_manifests(paths, group="windows", shard_count=2)


def test_manifest_verifier_rejects_missing_shard(tmp_path: Path) -> None:
    paths = _write_group(tmp_path, (("test::a",), ("test::b",)))

    with pytest.raises(ShardError, match=r"indexes 0\.\.1"):
        verify_manifests(paths[:1], group="windows", shard_count=2)
