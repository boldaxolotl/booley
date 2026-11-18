"""Shared helpers for hostile persisted Coverage Record fixtures."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

from booley.flows.sim.coverage_record import CoverageRecord, DurableTargetIdentity
from booley.flows.sim.coverage_record_store import decode_coverage_record


def decode_document(document: dict, target: DurableTargetIdentity) -> CoverageRecord:
    """Decode a single-document test Coverage Record through the store's one decode."""
    return decode_coverage_record(document, list(document["points"]), target)


def rewrite_manifest_as_v4(manifest: dict) -> None:
    """Turn a freshly published manifest into what Booley 0.3.0 wrote (v4).

    Runs named a bare Test, with no Test Run identity.
    """
    manifest["$schema"] = "booley.coverage-campaign/v4"
    for run in manifest["tests"]["runs"]:
        for field in ("test_run", "variant", "seed", "fingerprint"):
            del run[field]


def corrupt_coverage_record_with_duplicate_negative_point(
    coverage_record_path: Path, *, run_id: str = "run:reset"
) -> None:
    """Rewrite one Coverage Record with duplicate-point and negative-hit defects."""
    manifest = json.loads(coverage_record_path.read_text(encoding="utf-8"))
    points_path = coverage_record_path.parent / manifest["point_store"]["path"]
    records = [json.loads(line) for line in gzip.decompress(points_path.read_bytes()).splitlines()]
    duplicate = copy.deepcopy(records[-1])
    duplicate["hits_by_run"] = {run_id: -1}
    records.append(duplicate)
    raw = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )
    compressed = gzip.compress(raw, mtime=0)
    points_path.write_bytes(compressed)
    rollup_values = {
        "total_points": 2,
        "eligible_points": 2,
        "covered_points": 1,
        "waived_points": 0,
        "percent": 50.0,
    }
    manifest["rollups"][0].update(rollup_values)
    manifest["source_rollups"][0]["rollups"][0].update(rollup_values)
    coverage_record_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
