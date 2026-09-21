"""Validate retained Taxi bounded-parallel Simulation Campaign evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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
    child_samples: set[str] = set()
    previous_sample = -1
    for sample in samples:
        _need(isinstance(sample, dict), "SlotStore sample is invalid")
        holders = sample.get("heavy_holders")
        waiters = sample.get("heavy_waiters")
        at_ns = sample.get("at_ns")
        _need(isinstance(at_ns, int), "SlotStore sample timestamp is missing")
        _need(at_ns >= previous_sample, "SlotStore samples are not timestamp ordered")
        previous_sample = at_ns
        _need(isinstance(holders, list), "heavy-holder sample is missing")
        _need(isinstance(waiters, list), "heavy-waiter sample is missing")
        _need(len(set(holders + waiters)) == len(holders + waiters), "slot sample duplicates a claim")
        _need(len(holders) <= max_heavy, "SlotStore heavy holders exceed max_heavy")
        _need(outer_id in holders, "SlotStore sample omits the borrowed outer Job")
        child_samples.update(value for value in holders + waiters if value != outer_id)
    final = timeline.get("final_slot_state")
    _need(
        final == {"heavy_holders": [], "heavy_waiters": []},
        "final SlotStore inspection is not empty",
    )
    transitioned = _validate_claim_transitions(timeline)
    _need(child_samples == set(transitioned), "SlotStore samples do not account for every child")
    _validate_sample_accounting(samples, outer_id, transitioned)
    _validate_interval_accounting(timeline, transitioned)
    return {"max_heavy": max_heavy, "peak_simulators": peak}


def _validate_claim_transitions(
    timeline: dict[str, object],
) -> dict[str, list[dict[str, object]]]:
    transitions = timeline.get("claim_transitions")
    _need(isinstance(transitions, list) and transitions, "claim transitions are missing")
    by_child: dict[str, list[dict[str, object]]] = {}
    previous = -1
    for transition in transitions:
        _need(isinstance(transition, dict), "claim transition is invalid")
        child = transition.get("child_execution_id")
        state = transition.get("state")
        at_ns = transition.get("at_ns")
        _need(isinstance(child, str) and child, "claim child identity is missing")
        _need(state in {"submitted", "promoted", "released"}, "claim state is invalid")
        _need(isinstance(at_ns, int) and at_ns >= previous, "claim timestamps are invalid")
        previous = at_ns
        by_child.setdefault(child, []).append(transition)
    _need(
        all(
            [event["state"] for event in events]
            == ["submitted", "promoted", "released"]
            for events in by_child.values()
        ),
        "claim transitions do not end in exact release",
    )
    return by_child


def _validate_sample_accounting(samples, outer_id, transitions) -> None:
    for sample in samples:
        at_ns = sample["at_ns"]
        expected_holders: set[str] = set()
        expected_waiters: set[str] = set()
        for child, events in transitions.items():
            prior = [event["state"] for event in events if event["at_ns"] <= at_ns]
            state = prior[-1] if prior else None
            if state == "submitted":
                expected_waiters.add(child)
            elif state == "promoted":
                expected_holders.add(child)
        actual_holders = set(sample["heavy_holders"]) - {outer_id}
        actual_waiters = set(sample["heavy_waiters"])
        _need(actual_holders == expected_holders, "holder sample contradicts transitions")
        _need(actual_waiters == expected_waiters, "waiter sample contradicts transitions")


def _validate_interval_accounting(timeline: dict[str, object], transitions) -> None:
    intervals = _intervals(timeline)
    interval_children = {item.get("child_execution_id") for item in intervals}
    _need(interval_children == set(transitions), "process intervals do not bind every exact child")
    _need(len({item.get("attempt_id") for item in intervals}) == len(intervals), "attempt ids repeat")
    for interval in intervals:
        child = interval["child_execution_id"]
        events = transitions[child]
        _need(events[1]["at_ns"] <= interval["start_ns"], "process started before promotion")
        _need(events[2]["at_ns"] >= interval["end_ns"], "claim released before process exit")


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
    children: set[str] = set()
    work_items: set[str] = set()
    for item in intervals:
        _need(item.get("token") == item.get("output_text"), "attempt output is contaminated")
        child = item.get("child_execution_id")
        work_item = item.get("work_item_id")
        _need(isinstance(child, str) and child, "exact child identity is missing")
        _need(isinstance(work_item, str) and work_item, "work-item identity is missing")
        children.add(child)
        work_items.add(work_item)
        _validate_isolated_artifacts(item, timeline_path.parent)
    _need(len(children) == len(intervals), "process intervals reuse a child identity")
    _need(len(work_items) == len(intervals), "process intervals reuse a work item")


def _validate_isolated_artifacts(item: dict[str, object], evidence_root: Path) -> None:
    token = item.get("token")
    attempt = item.get("attempt_id")
    _need(isinstance(attempt, str) and attempt, "Simulation Attempt identity is missing")
    for kind in ("output", "log", "trace", "runtime_input"):
        artifact = item.get(kind)
        _need(isinstance(artifact, dict), f"{kind} evidence is missing")
        _need(artifact.get("token") == token, f"{kind} evidence is contaminated")
        path = artifact.get("path")
        digest = artifact.get("sha256")
        _need(isinstance(path, str) and attempt in path, f"{kind} path is not attempt-scoped")
        _need(isinstance(digest, str) and _DIGEST.fullmatch(digest), f"{kind} digest is invalid")
        relative = Path(path)
        _need(not relative.is_absolute() and ".." not in relative.parts, f"{kind} path escapes")
        retained = evidence_root / relative
        _need(retained.is_file() and not retained.is_symlink(), f"{kind} file is not retained")
        raw = retained.read_bytes()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        _need(actual == digest, f"{kind} retained digest differs")
        _need(raw.rstrip(b"\n") == str(token).encode(), f"{kind} retained token differs")


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
    manifest_digest = manifest.get("manifest_sha256")
    _need(
        isinstance(manifest_digest, str) and _DIGEST.fullmatch(manifest_digest),
        "manifest digest is missing",
    )
    expected_ids = [item.get("work_item_id") for item in work_items]
    _need(
        all(
            result.get("manifest_sha256") == manifest_digest
            and result.get("work_item_id") == work_item_id
            and isinstance(result.get("attempt_id"), str)
            and isinstance(result.get("result_sha256"), str)
            and _DIGEST.fullmatch(result["result_sha256"])
            for result, work_item_id in zip(results, expected_ids, strict=True)
        ),
        "Simulation Results do not authenticate manifest/work-item/attempt identity",
    )
    summary = _load(summary_path)
    _need(summary.get("ordered_tests") == expected, "summary order differs")
    _need(summary.get("strict_grade") == "fail", "summary is not a strict failure")
    _need(summary.get("manifest_sha256") == manifest_digest, "summary manifest digest differs")
    _need(summary.get("completed") == expected_ids, "summary completion identities differ")
