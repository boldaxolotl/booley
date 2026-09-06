#!/usr/bin/env python3
"""Deterministically balance and verify pytest shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ShardError(ValueError):
    """Raised when shard inputs or evidence violate the CI contract."""


@dataclass(frozen=True)
class TimingModel:
    default_seconds: float
    tests: Mapping[str, float]


def _positive_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ShardError(f"{field} must be a positive number")
    return float(value)


def load_timings(path: Path | None) -> TimingModel:
    """Load trusted historical durations used only to balance selected tests."""
    if path is None or not path.exists():
        return TimingModel(default_seconds=1.0, tests={})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ShardError(f"cannot read timing model {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ShardError("timing model must be a schema-1 object")
    default = _positive_number(payload.get("default_seconds"), field="default_seconds")
    raw_tests = payload.get("tests")
    if not isinstance(raw_tests, dict):
        raise ShardError("timing model tests must be an object")
    tests = {
        nodeid: _positive_number(duration, field=f"tests[{nodeid!r}]")
        for nodeid, duration in raw_tests.items()
        if isinstance(nodeid, str) and nodeid
    }
    if len(tests) != len(raw_tests):
        raise ShardError("timing model test IDs must be non-empty strings")
    return TimingModel(default_seconds=default, tests=tests)


def assign_shards(
    nodeids: Sequence[str], shard_count: int, timings: TimingModel
) -> tuple[tuple[str, ...], ...]:
    """Assign every unique node ID once using deterministic LPT balancing."""
    if shard_count < 1:
        raise ShardError("shard_count must be positive")
    if len(nodeids) != len(set(nodeids)):
        raise ShardError("collected node IDs must be unique")
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count
    weighted = sorted(
        nodeids,
        key=lambda nodeid: (-timings.tests.get(nodeid, timings.default_seconds), nodeid),
    )
    for nodeid in weighted:
        index = min(range(shard_count), key=lambda candidate: (loads[candidate], candidate))
        shards[index].append(nodeid)
        loads[index] += timings.tests.get(nodeid, timings.default_seconds)
    return tuple(tuple(sorted(shard)) for shard in shards)


def nodeid_digest(nodeids: Iterable[str]) -> str:
    """Return a stable digest for a set of collected node IDs."""
    payload = "".join(f"{nodeid}\n" for nodeid in sorted(nodeids)).encode()
    return hashlib.sha256(payload).hexdigest()


def write_manifest(
    path: Path,
    *,
    group: str,
    shard_index: int,
    shards: Sequence[Sequence[str]],
    all_nodeids: Sequence[str],
) -> None:
    """Persist one shard's exact selection and the first shard's reference set."""
    payload = {
        "schema": 1,
        "group": group,
        "shard_index": shard_index,
        "shard_count": len(shards),
        "expected_count": len(all_nodeids),
        "expected_sha256": nodeid_digest(all_nodeids),
        "selected": list(shards[shard_index]),
    }
    if shard_index == 0:
        payload["expected"] = sorted(all_nodeids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ShardError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ShardError(f"{path} must contain a schema-1 object")
    return payload


def verify_manifests(paths: Sequence[Path], *, group: str, shard_count: int) -> int:
    """Prove a shard group covers its exact eligible node-ID set once."""
    manifests = [_read_manifest(path) for path in paths]
    matching = [manifest for manifest in manifests if manifest.get("group") == group]
    indexes = {manifest.get("shard_index") for manifest in matching}
    if indexes != set(range(shard_count)) or len(matching) != shard_count:
        raise ShardError(f"{group} manifests must contain indexes 0..{shard_count - 1}")
    if any(manifest.get("shard_count") != shard_count for manifest in matching):
        raise ShardError(f"{group} manifests disagree about shard count")
    reference = next(manifest for manifest in matching if manifest["shard_index"] == 0)
    expected = reference.get("expected")
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ShardError(f"{group} shard zero must carry the expected node IDs")
    selected = [item for manifest in matching for item in manifest.get("selected", [])]
    if len(selected) != len(set(selected)):
        raise ShardError(f"{group} selected at least one node ID more than once")
    if set(selected) != set(expected):
        missing = len(set(expected) - set(selected))
        extra = len(set(selected) - set(expected))
        raise ShardError(f"{group} shard union mismatch: missing={missing}, extra={extra}")
    digest = nodeid_digest(expected)
    if any(manifest.get("expected_sha256") != digest for manifest in matching):
        raise ShardError(f"{group} manifests disagree about the eligible node-ID set")
    return len(expected)


def _manifest_paths(root: Path) -> list[Path]:
    return sorted(root.rglob("shard-manifest.json"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    args = parser.parse_args()
    try:
        count = verify_manifests(
            _manifest_paths(args.root), group=args.group, shard_count=args.shard_count
        )
    except ShardError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        f"Verified {count} eligible tests exactly once across {args.shard_count} {args.group} shards"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
