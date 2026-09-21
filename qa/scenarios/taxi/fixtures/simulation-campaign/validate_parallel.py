"""Validate retained Taxi bounded-parallel Simulation Campaign evidence."""

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


def _intervals(document: dict[str, object]) -> list[dict[str, object]]:
    value = document.get("intervals")
    _need(isinstance(value, list) and value, "process intervals are missing")
    intervals: list[dict[str, object]] = []
    for item in value:
        _need(isinstance(item, dict), "process interval is invalid")
        _need(
            isinstance(item.get("start_ns"), int)
            and isinstance(item.get("end_ns"), int)
            and item["start_ns"] < item["end_ns"],
            "process interval bounds are invalid",
        )
        intervals.append(item)
    return intervals


def _peak_overlap(intervals: list[dict[str, object]]) -> int:
    events = []
    for item in intervals:
        events.append((item["start_ns"], 1))
        events.append((item["end_ns"], -1))
    active = 0
    peak = 0
    for _at_ns, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def validate_heavy_cap(timeline_path: Path, max_heavy: int) -> dict[str, int]:
    """Require measured simulator and real SlotStore samples to respect the cap."""
    _need(max_heavy > 1, "heavy-cap Check requires max_heavy greater than one")
    timeline = _load(timeline_path)
    intervals = _intervals(timeline)
    peak = _peak_overlap(intervals)
    _need(1 < peak <= max_heavy, "simulator overlap does not prove the heavy cap")
    samples = timeline.get("slot_samples")
    _need(isinstance(samples, list) and samples, "SlotStore samples are missing")
    outer_id = timeline.get("outer_execution_id")
    _need(isinstance(outer_id, str) and outer_id, "outer execution identity is missing")
    observed_outer = False
    for sample in samples:
        _need(isinstance(sample, dict), "SlotStore sample is invalid")
        holders = sample.get("heavy_holders")
        _need(isinstance(holders, list), "heavy-holder sample is missing")
        _need(len(holders) <= max_heavy, "SlotStore heavy holders exceed max_heavy")
        observed_outer = observed_outer or outer_id in holders
    _need(observed_outer, "SlotStore samples do not include the borrowed outer Job")
    return {"max_heavy": max_heavy, "peak_simulators": peak}


def validate_attempt_isolation(timeline_path: Path) -> None:
    """Require one common relative output name with distinct owned content."""
    intervals = _intervals(_load(timeline_path))
    _need(len(intervals) >= 3, "attempt-isolation Check requires at least three attempts")
    relative_paths = {item.get("relative_output") for item in intervals}
    _need(relative_paths == {"qa-shared-name.txt"}, "relative output names differ")
    directories = [item.get("run_directory") for item in intervals]
    tokens = [item.get("token") for item in intervals]
    _need(all(isinstance(value, str) and value for value in directories), "run directory is missing")
    _need(all(isinstance(value, str) and value for value in tokens), "attempt token is missing")
    _need(len(set(directories)) == len(directories), "parallel run directories collide")
    _need(len(set(tokens)) == len(tokens), "parallel attempt tokens collide")
    for item in intervals:
        _need(item.get("token") == item.get("output_text"), "attempt output is contaminated")


def validate_continue_after_failure(
    manifest_path: Path, summary_path: Path, result_paths: list[Path]
) -> None:
    """Require pass/fail/pass terminals rendered in immutable manifest order."""
    expected = ["slow-first", "slow-fail", "slow-last"]
    manifest = _load(manifest_path)
    work_items = manifest.get("work_items")
    _need(isinstance(work_items, list), "manifest work items are missing")
    manifest_order = []
    for item in work_items:
        _need(isinstance(item, dict), "manifest work item is invalid")
        selection = item.get("selection")
        _need(isinstance(selection, dict), "manifest selection is missing")
        manifest_order.extend(selection.get("names", []))
    _need(manifest_order == expected, "manifest order differs from pass/fail/pass")
    results = [_load(path) for path in result_paths]
    _need(len(results) == 3, "not every work item published a terminal result")
    result_order = [item.get("test") for item in results]
    grades = [item.get("grade") for item in results]
    _need(result_order == expected, "result order differs from the manifest")
    _need(grades == ["pass", "fail", "pass"], "terminal grades differ")
    summary = _load(summary_path)
    _need(summary.get("ordered_tests") == expected, "summary order differs")
    _need(summary.get("strict_grade") == "fail", "summary is not a strict failure")
