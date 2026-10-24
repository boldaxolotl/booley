"""Contracts for deterministic CI test sharding."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_changes import build_test_matrix
from ci_test_shards import (
    ShardError,
    TimingModel,
    assign_shards,
    load_timings,
    nodeid_digest,
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


@pytest.mark.parametrize("duration", [0, float("nan"), float("inf"), float("-inf")])
def test_timing_model_rejects_invalid_values(tmp_path: Path, duration: float) -> None:
    path = tmp_path / "timings.json"
    path.write_text(
        json.dumps({"schema": 1, "default_seconds": 1, "tests": {"test::bad": duration}}),
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("selected", "test::a", "selected must be a list"),
        ("selected", ["test::a", 3], "selected must be a list"),
        ("expected_count", True, "expected_count must be an integer"),
        ("expected_sha256", "not-a-digest", "must be a lowercase SHA-256"),
    ],
)
def test_manifest_verifier_rejects_malformed_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    paths = _write_group(tmp_path, (("test::a",), ("test::b",)))
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    payload[field] = value
    paths[0].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShardError, match=message):
        verify_manifests(paths, group="windows", shard_count=2)


def test_manifest_verifier_rejects_incorrect_expected_count(tmp_path: Path) -> None:
    paths = _write_group(tmp_path, (("test::a",), ("test::b",)))
    payload = json.loads(paths[1].read_text(encoding="utf-8"))
    payload["expected_count"] = 1
    paths[1].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShardError, match="expected test count"):
        verify_manifests(paths, group="windows", shard_count=2)


_LINUX_TIMING_MODEL = _ROOT / ".github/ci/linux-test-timings.json"
_LINUX_NODEIDS = ("test::a", "test::b", "test::c", "test::d", "test::e")


def _write_linux_download(
    root: Path, selected: dict[str, tuple[tuple[str, ...], ...]]
) -> dict[str, list[Path]]:
    """Write the manifests the shared linux-shards download holds, plus Windows evidence."""
    paths: dict[str, list[Path]] = {}
    for group, shards in {**selected, "windows": (("test::w",), ("test::x",))}.items():
        everything = tuple(nodeid for shard in shards for nodeid in shard)
        paths[group] = []
        for index in range(len(shards)):
            path = root / f"shard-{group}-{index}" / "shard-manifest.json"
            write_manifest(
                path, group=group, shard_index=index, shards=shards, all_nodeids=everything
            )
            paths[group].append(path)
    return paths


def _linux_groups() -> list[str]:
    """Group names the dispatched three-shard matrix emits."""
    include = build_test_matrix(10, linux_shard_count=3)["include"]
    return sorted({str(entry["group"]) for entry in include if entry["os"] == "ubuntu-latest"})


def _edit_manifest(path: Path, **fields: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload | fields), encoding="utf-8")


def _verify_download(download: Path, group: str) -> int:
    return verify_manifests(
        sorted(download.rglob("shard-manifest.json")), group=group, shard_count=3
    )


def test_linux_timing_model_holds_measured_weights_above_the_default() -> None:
    """The checked-in model stores measured medians, each above the default weight."""
    timings = load_timings(_LINUX_TIMING_MODEL)

    assert timings.tests, "the Linux timing model holds no measured weights"
    assert all(weight > timings.default_seconds for weight in timings.tests.values())


def test_linux_group_assigns_tests_absent_from_the_linux_timing_model(tmp_path: Path) -> None:
    assert _LINUX_TIMING_MODEL.is_file()
    model = json.loads(_LINUX_TIMING_MODEL.read_text(encoding="utf-8"))
    model["tests"]["test::a"] = 9.0
    modelled = tmp_path / "model.json"
    modelled.write_text(json.dumps(model), encoding="utf-8")
    timings = load_timings(modelled)
    shards = assign_shards(_LINUX_NODEIDS, 3, timings)
    groups = _linux_groups()
    _write_linux_download(tmp_path / "download", dict.fromkeys(groups, shards))

    assert groups == ["linux-3.11", "linux-3.14"]
    assert "test::e" not in timings.tests
    assert sum("test::e" in shard for shard in shards) == 1
    for group in groups:
        assert _verify_download(tmp_path / "download", group) == len(_LINUX_NODEIDS)


def test_linux_group_verifier_rejects_omitted_duplicated_and_inconsistent_ids(
    tmp_path: Path,
) -> None:
    shards = assign_shards(_LINUX_NODEIDS, 3, TimingModel(1.0, {}))

    def broken(name: str, group: str, shard_index: int, **fields: object) -> Path:
        download = tmp_path / name
        paths = _write_linux_download(download, dict.fromkeys(_linux_groups(), shards))
        _edit_manifest(paths[group][shard_index], **fields)
        return download

    scenarios = (
        ("omitted", "linux-3.14", 1, {"selected": shards[1][:-1]}, "union mismatch"),
        (
            "duplicated",
            "linux-3.11",
            1,
            {"selected": [*shards[1], shards[2][0]]},
            "more than once",
        ),
        (
            "inconsistent",
            "linux-3.14",
            2,
            {"expected_sha256": nodeid_digest(["test::other", *_LINUX_NODEIDS])},
            "eligible node-ID set",
        ),
    )
    for name, group, shard_index, fields, message in scenarios:
        download = broken(name, group, shard_index, **fields)
        healthy_group = "linux-3.11" if group == "linux-3.14" else "linux-3.14"

        with pytest.raises(ShardError, match=message):
            _verify_download(download, group)
        assert _verify_download(download, healthy_group) == len(_LINUX_NODEIDS)
