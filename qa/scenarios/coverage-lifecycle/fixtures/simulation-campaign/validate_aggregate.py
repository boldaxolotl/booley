"""Validate retained aggregate coverage Simulation Campaign recovery evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _load(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, raw


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate(
    manifest_path: Path,
    interrupted_attempt_path: Path,
    interrupted_result_path: Path,
    resumed_result_path: Path,
    coverage_path: Path,
    reference_path: Path,
    projection_paths: list[Path],
) -> None:
    """Require whole-aggregate retry and one authenticated origin reference."""
    manifest, _ = _load(manifest_path)
    interrupted, _ = _load(interrupted_attempt_path)
    resumed, _ = _load(resumed_result_path)
    _validate_attempts(manifest, interrupted, interrupted_result_path, resumed)
    coverage, coverage_raw = _load(coverage_path)
    reference, _ = _load(reference_path)
    _validate_reference(coverage, coverage_raw, reference, resumed)
    _validate_projections(projection_paths)


def _validate_attempts(manifest, interrupted, interrupted_result_path, resumed) -> None:
    items = manifest.get("work_items")
    _need(isinstance(items, list) and len(items) == 1, "expected one coverage work item")
    work_item = items[0]
    _need(isinstance(work_item, dict), "coverage work item is invalid")
    _need(work_item.get("kind") == "coverage_aggregate", "work item is not coverage aggregate")
    _need(
        interrupted.get("work_item_id") == resumed.get("work_item_id") == work_item.get("work_item_id"),
        "coverage attempts do not bind the aggregate work item",
    )
    _need(interrupted.get("attempt_id") != resumed.get("attempt_id"), "aggregate retry reused an attempt")
    _need(
        interrupted.get("$schema") == "booley.simulation-attempt/v1",
        "interrupted attempt schema differs",
    )
    _need(not interrupted_result_path.exists(), "interrupted aggregate has a terminal result")
    _need(resumed.get("state") == "completed", "resumed aggregate did not complete")


def _validate_reference(coverage, coverage_raw, reference, resumed) -> None:
    _need(
        reference.get("$schema") == "booley.coverage-campaign-reference/v1",
        "coverage reference schema differs",
    )
    nested = reference.get("coverage_campaign")
    _need(isinstance(nested, dict), "nested coverage reference is missing")
    _need(nested.get("sha256") == _digest(coverage_raw), "nested coverage digest differs")
    _need(nested.get("bytes") == len(coverage_raw), "nested coverage byte count differs")
    _need(nested.get("campaign_id") == coverage.get("campaign_id"), "nested campaign identity differs")
    _need(nested.get("path_base") == "origin_target", "nested path base differs")
    invocation = coverage.get("invocation")
    _need(isinstance(invocation, dict), "nested coverage invocation is missing")
    _need(
        reference.get("origin_invocation_id") == invocation.get("id"),
        "origin invocation identity differs",
    )
    _need(
        reference.get("producer_invocation_id") == resumed.get("producer_invocation_id"),
        "producer invocation identity differs",
    )
    _need(reference.get("simulation_attempt_id") == resumed.get("attempt_id"), "attempt identity differs")
    _need(reference.get("simulation_work_item_id") == resumed.get("work_item_id"), "work-item identity differs")


def _validate_projections(projection_paths: list[Path]) -> None:
    _need(len(projection_paths) == 2, "origin and resumed projections are both required")
    for path in projection_paths:
        projection, _ = _load(path)
        _need(projection.get("coverage_campaign") == "coverage.json", "projection pointer differs")
        _need(
            projection.get("coverage_campaign_base") == "origin_target",
            "projection does not select the origin Target",
        )
