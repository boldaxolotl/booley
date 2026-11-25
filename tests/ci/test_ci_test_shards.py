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


def test_skipped_nodes_add_no_shard_load() -> None:
    """A skipped node does no work, so its recorded duration must not push real work aside."""
    nodeids = ("t::slow", "t::fast", "t::skip-a", "t::skip-b")
    timings = TimingModel(
        default_seconds=1.0, tests={"t::slow": 9.0, "t::skip-a": 50.0, "t::skip-b": 40.0}
    )

    shards = assign_shards(nodeids, 2, timings, skipped={"t::skip-a", "t::skip-b"})

    assert shards == (("t::skip-a", "t::slow"), ("t::fast", "t::skip-b"))


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


def _edit_manifest(path: Path, **fields: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload | fields), encoding="utf-8")


@pytest.mark.parametrize(
    ("index", "fields", "message"),
    [
        (1, {"selected": ["test::a"]}, "more than once"),
        (None, {}, r"indexes 0\.\.1"),
        (0, {"selected": "test::a"}, "selected must be a list"),
        (0, {"selected": ["test::a", 3]}, "selected must be a list"),
        (0, {"expected_count": True}, "expected_count must be an integer"),
        (0, {"expected_sha256": "not-a-digest"}, "must be a lowercase SHA-256"),
        (1, {"expected_count": 1}, "expected test count"),
    ],
)
def test_manifest_verifier_rejects(
    tmp_path: Path, index: int | None, fields: dict[str, object], message: str
) -> None:
    paths = _write_group(tmp_path, (("test::a",), ("test::b",)))
    if index is None:
        paths = paths[:1]  # the second shard's manifest is missing
    else:
        _edit_manifest(paths[index], **fields)

    with pytest.raises(ShardError, match=message):
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

    assert groups == ["linux-3.11"]
    assert "test::e" not in timings.tests
    assert sum("test::e" in shard for shard in shards) == 1
    for group in groups:
        assert _verify_download(tmp_path / "download", group) == len(_LINUX_NODEIDS)


@pytest.mark.parametrize(
    ("shard_index", "field", "message"),
    [
        (1, "omitted", "union mismatch"),
        (1, "duplicated", "more than once"),
        (2, "inconsistent", "eligible node-ID set"),
    ],
)
def test_linux_group_verifier_rejects_omitted_duplicated_and_inconsistent_ids(
    tmp_path: Path, shard_index: int, field: str, message: str
) -> None:
    shards = assign_shards(_LINUX_NODEIDS, 3, TimingModel(1.0, {}))
    edits: dict[str, dict[str, object]] = {
        "omitted": {"selected": shards[1][:-1]},
        "duplicated": {"selected": [*shards[1], shards[2][0]]},
        "inconsistent": {"expected_sha256": nodeid_digest(["test::other", *_LINUX_NODEIDS])},
    }
    paths = _write_linux_download(tmp_path, dict.fromkeys(_linux_groups(), shards))
    _edit_manifest(paths["linux-3.11"][shard_index], **edits[field])

    with pytest.raises(ShardError, match=message):
        _verify_download(tmp_path, "linux-3.11")
