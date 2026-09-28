"""Shared helpers for hostile persisted Coverage Campaign fixtures."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path


def corrupt_v3_campaign_with_duplicate_negative_point(
    campaign_path: Path, *, run_id: str = "run:reset"
) -> None:
    """Rewrite one V3 Campaign with duplicate-point and negative-hit defects."""
    manifest = json.loads(campaign_path.read_text(encoding="utf-8"))
    points_path = campaign_path.parent / manifest["point_store"]["path"]
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
    manifest["point_store"].update(
        sha256="sha256:" + hashlib.sha256(compressed).hexdigest(),
        bytes=len(compressed),
        uncompressed_bytes=len(raw),
        point_count=2,
    )
    rollup_values = {
        "total_points": 2,
        "eligible_points": 2,
        "covered_points": 1,
        "waived_points": 0,
        "percent": 50.0,
    }
    manifest["rollups"][0].update(rollup_values)
    manifest["source_rollups"][0]["rollups"][0].update(rollup_values)
    campaign_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
