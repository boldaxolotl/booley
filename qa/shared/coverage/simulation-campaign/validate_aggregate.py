"""Validate refusal of an unfinished aggregate coverage Simulation Campaign resume."""

from __future__ import annotations

import json
from pathlib import Path


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate(
    manifest_path: Path,
    interrupted_attempt_path: Path,
    interrupted_result_path: Path,
    rejection_path: Path,
) -> None:
    """Require refusal of an unfinished coverage aggregate resume without mutation."""
    manifest = _load(manifest_path)
    interrupted = _load(interrupted_attempt_path)
    items = manifest.get("work_items")
    _need(isinstance(items, list) and len(items) == 1, "expected one coverage work item")
    work_item = items[0]
    _need(isinstance(work_item, dict), "coverage work item is invalid")
    _need(work_item.get("kind") == "coverage_aggregate", "work item is not coverage aggregate")
    _need(
        interrupted.get("$schema") == "booley.simulation-attempt/v1",
        "interrupted attempt schema differs",
    )
    _need(
        interrupted.get("work_item_id") == work_item.get("work_item_id"),
        "interrupted attempt does not bind the aggregate work item",
    )
    _need(not interrupted_result_path.exists(), "interrupted aggregate has a terminal result")
    rejection = _load(rejection_path)
    _need(rejection.get("exit_code") == 2, "unfinished coverage resume did not exit 2")
    _need(rejection.get("dry_run_exit_code") == 2, "unfinished coverage dry-run did not exit 2")
    diagnostic = rejection.get("diagnostic")
    _need(
        isinstance(diagnostic, str)
        and "booley flow sim" in diagnostic
        and "--coverage" in diagnostic,
        "refusal does not name a new coverage run",
    )
    _need(
        rejection.get("attempts_before") == rejection.get("attempts_after"),
        "refused resume changed the attempt inventory",
    )
    _need(rejection.get("eda_launches") == 0, "refused resume launched EDA")
    _need(rejection.get("control_exit_code") == 0, "fresh coverage control did not succeed")
